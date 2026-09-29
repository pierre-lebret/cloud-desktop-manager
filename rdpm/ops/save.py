"""Sauvegarde (snapshot), fermeture avec ou sans sauvegarde, reprise après interruption.

Règle d'or : le serveur n'est supprimé qu'après confirmation que le snapshot est « available ».
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .. import fmt
from ..constants import (
    L_FORCED, L_LOC, L_OP_IMAGE, L_SRC_SERVER, L_TYPE, OP_CHECKPOINT, OP_DISCARDING, OP_SAVING,
)
from ..hetzner.errors import ActionFailed, OpCancelled, UserError, WaitTimeout
from ..hetzner.waiting import wait_actions, wait_server_status
from ..labels import desktop_labels, format_description
from ..models import ServerInfo
from .base import OpContext, Operation
from .common import (
    apply_retention, cleanup_after_delete, clear_op, delete_server_with_retry, mark_op, record_session,
    session_summary, wait_snapshot_ready,
)


class SaveOp(Operation):
    title = "Sauvegarde"

    def __init__(self, ctx: OpContext, slug: str, name: str, server_id: int, close: bool,
                 fixed_ip_id: int | None = None, quit_mode: bool = False) -> None:
        super().__init__(ctx, slug, name)
        self.kind = "save" if close else "checkpoint"
        self.title = "Sauvegarde et fermeture" if close else "Sauvegarde"
        self.server_id = server_id
        self.close = close
        self.closes_server = close
        self.fixed_ip_id = fixed_ip_id
        self.quit_mode = quit_mode
        self.forced = False
        self.image_id: int | None = None
        self._server_gone = False

    def execute(self) -> None:
        srv = self.ctx.backend.get_server(self.server_id)
        if srv is None:
            raise UserError("Serveur introuvable (déjà supprimé ?)")
        mark_op(self, srv.id, OP_SAVING if self.close else OP_CHECKPOINT)
        self.forced = self.stop_windows(srv)
        self.image_id = self.snapshot(srv)
        if self.close:
            self.finish_close(srv)
        else:
            self.set_phase("Redémarrage de Windows…")
            wait_actions(self.ctx.backend, [self.ctx.backend.server_action(srv.id, "poweron")], self,
                         timeout_s=300)
            clear_op(self, srv.id)
            self.success_message = f"« {self.name} » sauvegardé — Windows redémarre"
        apply_retention(self, self.image_id, self.forced)

    # --- étapes ----------------------------------------------------------------------------
    def stop_windows(self, srv: ServerInfo) -> bool:
        """Arrêt propre (ACPI), puis décision de l'utilisateur si Windows traîne. Vrai si forcé."""
        backend = self.ctx.backend
        if srv.status == "off":
            return False
        self.set_phase("Arrêt de Windows…", cancellable=True)
        if srv.status != "stopping":
            try:
                wait_actions(backend, [backend.server_action(srv.id, "shutdown")], self, timeout_s=120)
            except ActionFailed as exc:
                self.log(f"Signal d'arrêt refusé ({exc}), attente de l'arrêt quand même", "warning")
        start = time.monotonic()
        timeout = float(self.ctx.config.get("shutdown_timeout_s"))
        while True:
            try:
                wait_server_status(
                    backend, srv.id, {"off"}, self, timeout_s=timeout, interval=5,
                    on_tick=lambda _e: self.set_phase(
                        f"Arrêt de Windows… {fmt.clock(time.monotonic() - start)}", cancellable=True))
                self.log("Windows est arrêté")
                return False
            except WaitTimeout:
                pass
            choice = self.ask(
                "Windows ne s'arrête pas",
                f"« {self.name} » n'a pas fini de s'arrêter après {fmt.duration(time.monotonic() - start)} "
                "(mises à jour Windows en cours ?).\n\nForcer l'arrêt revient à débrancher la prise : "
                "les fichiers non enregistrés peuvent être perdus.",
                [("wait", "Attendre 5 min de plus", "default"), ("force", "Forcer l'arrêt", "danger"),
                 ("cancel", "Annuler (laisser allumé)", "default")],
                default="force" if self.quit_mode else "wait",
                countdown_s=int(self.ctx.config.get("quit_force_countdown_s")) if self.quit_mode else None)
            if choice == "cancel":
                raise OpCancelled("Sauvegarde annulée : le serveur reste allumé")
            if choice == "force":
                self.set_phase("Arrêt forcé…")
                wait_actions(backend, [backend.server_action(srv.id, "poweroff")], self, timeout_s=120)
                wait_server_status(backend, srv.id, {"off"}, self, timeout_s=180, interval=3)
                self.log("Arrêt forcé effectué", "warning")
                return True
            timeout = 300.0
            self.set_phase("Arrêt de Windows…", cancellable=True)

    def snapshot(self, srv: ServerInfo) -> int:
        backend = self.ctx.backend
        self.set_phase("Snapshot : démarrage…", 0)
        labels = desktop_labels(self.slug, **{L_SRC_SERVER: srv.id, L_TYPE: srv.server_type,
                                              L_LOC: srv.location, L_FORCED: int(self.forced)})
        image_id, action_id = backend.create_snapshot(
            srv.id, format_description(self.name, datetime.now(timezone.utc)), labels)
        backend.update_server_labels(srv.id, {L_OP_IMAGE: image_id})
        self.log(f"Snapshot {image_id} en cours de création")
        return self.wait_snapshot(image_id, action_id)

    def wait_snapshot(self, image_id: int, action_id: int | None) -> int:
        return wait_snapshot_ready(self, image_id, action_id)

    def finish_close(self, srv: ServerInfo) -> None:
        self.set_phase("Suppression du serveur…", None)
        delete_server_with_retry(self, srv.id)
        self._server_gone = True
        cleanup_after_delete(self, srv, self.fixed_ip_id)
        cost = record_session(self, srv)
        self.success_message = f"« {self.name} » sauvegardé et fermé — {session_summary(srv, cost)}"

    def on_failure(self) -> None:
        if not self._server_gone and self.ctx.backend.get_server(self.server_id):
            clear_op(self, self.server_id)


class DiscardOp(Operation):
    kind = "discard"
    title = "Fermeture sans sauvegarde"
    closes_server = True

    def __init__(self, ctx: OpContext, slug: str, name: str, server_id: int,
                 fixed_ip_id: int | None = None) -> None:
        super().__init__(ctx, slug, name)
        self.server_id = server_id
        self.fixed_ip_id = fixed_ip_id

    def execute(self) -> None:
        srv = self.ctx.backend.get_server(self.server_id)
        if srv is None:
            return
        self.set_phase("Suppression du serveur…")
        mark_op(self, srv.id, OP_DISCARDING)
        delete_server_with_retry(self, srv.id)
        cleanup_after_delete(self, srv, self.fixed_ip_id)
        cost = record_session(self, srv)
        self.success_message = f"« {self.name} » fermé sans sauvegarde — {session_summary(srv, cost)}"

    def on_failure(self) -> None:
        if self.ctx.backend.get_server(self.server_id):
            clear_op(self, self.server_id)


class ResumeOp(SaveOp):
    """Termine une opération interrompue (application fermée ou plantée en cours de route)."""

    title = "Reprise"

    def __init__(self, ctx: OpContext, slug: str, name: str, server: ServerInfo,
                 fixed_ip_id: int | None = None) -> None:
        super().__init__(ctx, slug, name, server.id, close=server.op != OP_CHECKPOINT,
                         fixed_ip_id=fixed_ip_id)
        self.kind = "save" if server.op in (OP_SAVING, OP_CHECKPOINT) else "other"
        self.label_op = server.op
        self.closes_server = server.op in (OP_SAVING, OP_DISCARDING)

    def execute(self) -> None:
        srv = self.ctx.backend.get_server(self.server_id)
        if srv is None:
            self.success_message = "Le serveur n'existe plus : rien à reprendre"
            return
        if self.label_op == OP_DISCARDING:
            self.set_phase("Suppression du serveur…")
            delete_server_with_retry(self, srv.id)
            self._server_gone = True
            cleanup_after_delete(self, srv, self.fixed_ip_id)
            record_session(self, srv)
            self.success_message = f"« {self.name} » : fermeture terminée"
            return
        if self.label_op not in (OP_SAVING, OP_CHECKPOINT):
            clear_op(self, srv.id)
            self.success_message = f"« {self.name} » : état remis à jour"
            return
        image = self.ctx.backend.get_image(srv.op_image) if srv.op_image else None
        if image is None:
            self.log("Aucun snapshot valide : la sauvegarde reprend depuis le début")
            super().execute()
            return
        self.image_id = self.wait_snapshot(image.id, None) if not image.available else image.id
        if self.close:
            self.finish_close(srv)
        else:
            wait_actions(self.ctx.backend, [self.ctx.backend.server_action(srv.id, "poweron")], self,
                         timeout_s=300)
            clear_op(self, srv.id)
            self.success_message = f"« {self.name} » : sauvegarde reprise et terminée"
        apply_retention(self, self.image_id, image.forced)
