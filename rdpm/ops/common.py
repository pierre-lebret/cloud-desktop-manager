"""Briques partagées entre les opérations."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .. import fmt, rdp
from ..constants import L_OP, L_OP_HOST, L_OP_TS, OP_LABELS
from ..hetzner.errors import ActionFailed, UserError, WaitTimeout, api_code
from ..hetzner.waiting import wait_actions, wait_server_status
from ..models import ServerInfo
from ..pricing import billed_hours, session_cost
from ..retention import snapshots_to_delete
from .base import Operation


def mark_op(op: Operation, server_id: int, name: str, **extra) -> None:
    updates = {L_OP: name, L_OP_TS: int(time.time()), L_OP_HOST: op.ctx.host}
    updates.update({k: str(v) for k, v in extra.items()})
    op.ctx.backend.update_server_labels(server_id, updates)


def clear_op(op: Operation, server_id: int) -> None:
    op.ctx.backend.update_server_labels(server_id, remove=OP_LABELS)


def delete_server_with_retry(op: Operation, server_id: int, attempts: int = 3) -> None:
    """Supprime le serveur ; la demande DELETE part avant toute vérification d'annulation."""
    backend = op.ctx.backend
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            action_id = backend.delete_server(server_id)
        except Exception as exc:  # noqa: BLE001
            if api_code(exc) == "not_found":
                return
            last = exc
            if api_code(exc) not in ("locked", "conflict", "rate_limit_exceeded", "network", None):
                break
            op.log(f"Suppression refusée ({api_code(exc)}), nouvel essai…", "warning")
            op.sleep(5 * (attempt + 1))
            continue
        wait_actions(backend, [action_id] if action_id else [], op, timeout_s=300)
        wait_server_status(backend, server_id, {"deleted"}, op, timeout_s=120, interval=3)
        return
    raise UserError("Serveur NON supprimé — il est toujours facturé",
                    f"Réessayez la suppression. Détail : {last}", code="delete_failed", retryable=True)


def cleanup_after_delete(op: Operation, server: ServerInfo, fixed_ip_id: int | None) -> None:
    """Oublie l'identifiant TERMSRV d'une IP dynamique : Hetzner peut la redonner à un inconnu."""
    if server.ipv4 and server.ipv4_id != fixed_ip_id:
        rdp.delete_termsrv_cred(server.ipv4)
        op.ctx.config.remove_cred(server.ipv4)
        if op.slug:
            rdp.delete_rdp_file(op.slug)


def record_session(op: Operation, server: ServerInfo) -> float:
    static = op.ctx.static()
    now = datetime.now(timezone.utc)
    cost = session_cost(server, static.pricing, now) if static else 0.0
    op.ctx.sessions.append({
        "slug": op.slug, "name": op.name, "server_type": server.server_type,
        "location": server.location, "start": server.created.isoformat(), "end": now.isoformat(),
        "hours_billed": billed_hours(server.created, now), "cost": round(cost, 4),
    })
    return cost


def session_summary(server: ServerInfo, cost: float) -> str:
    elapsed = (datetime.now(timezone.utc) - server.created).total_seconds()
    return f"{fmt.duration(elapsed)}, ≈ {fmt.eur(cost)}"


def apply_retention(op: Operation, new_image_id: int, forced: bool) -> None:
    """Jamais bloquant : une rétention ratée ne doit pas faire échouer une sauvegarde."""
    try:
        snaps = op.ctx.backend.list_snapshots(op.slug)
        doomed = snapshots_to_delete(snaps, op.ctx.config.get("retention"), {new_image_id}, forced)
        for snap in doomed:
            op.ctx.backend.delete_image(snap.id)
            op.log(f"Ancienne sauvegarde du {fmt.date_short(snap.created)} supprimée (rétention)")
    except Exception as exc:  # noqa: BLE001
        op.log(f"Rétention non appliquée : {exc}", "warning")


def wait_snapshot_ready(op: Operation, image_id: int, action_id: int | None) -> int:
    """Attend le snapshot puis vérifie qu'il est « available » avant toute suppression."""
    backend = op.ctx.backend
    progress = lambda pct: op.set_progress(pct, f"Snapshot {pct} %")  # noqa: E731
    try:
        if action_id:
            try:
                wait_actions(backend, [action_id], op, interval=5, on_progress=progress,
                             timeout_s=float(op.ctx.config.get("snapshot_timeout_s")))
            except WaitTimeout:
                op.log("Snapshot anormalement long, on continue d'attendre (serveur conservé)", "warning")
                op.set_phase("Snapshot anormalement long…", op.progress)
                wait_actions(backend, [action_id], op, interval=15, on_progress=progress,
                             timeout_s=24 * 3600)
    except ActionFailed:
        try:
            img = backend.get_image(image_id)
            if img and not img.available:
                backend.delete_image(image_id)
        except Exception:  # noqa: BLE001
            pass
        raise UserError("Le snapshot a échoué — le serveur est conservé (éteint, toujours facturé)",
                        "Réessayez la sauvegarde, rallumez le bureau, ou fermez-le sans sauvegarder.",
                        code="snapshot_failed", retryable=True) from None
    # Sans action (reprise), on suit directement le statut de l'image.
    deadline = time.monotonic() + (180 if action_id else float(op.ctx.config.get("snapshot_timeout_s")))
    while True:
        img = backend.get_image(image_id)
        if img is None and not action_id:
            raise UserError("Le snapshot interrompu a échoué — le serveur est conservé",
                            "Relancez la sauvegarde.", code="snapshot_failed", retryable=True)
        if img and img.available and img.image_size is not None:
            op.log(f"Snapshot vérifié : {fmt.gb(img.image_size)}")
            return image_id
        if time.monotonic() > deadline:
            raise UserError("Snapshot non confirmé — le serveur n'a PAS été supprimé",
                            "Vérifiez l'historique des sauvegardes puis réessayez.", code="snapshot_unverified")
        op.set_phase("Vérification du snapshot…" if action_id else "Snapshot en cours…",
                     100 if action_id else None)
        op.sleep(5)
