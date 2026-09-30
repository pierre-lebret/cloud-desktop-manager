"""Lancement d'un bureau : serveur créé depuis un snapshot, disque conservé si besoin."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from .. import license, netutil, rdp
from ..constants import (
    DEFAULT_FIREWALL_NAME, L_CERT, L_IMAGE, L_MANAGED, L_OP, L_OP_HOST, L_OP_TS, L_OS, L_ROLE, OP_LABELS,
    OP_LAUNCHING, OS_LABELS, OS_WINDOWS, ROLE_DESKTOP, ROLE_DESKTOP_FIREWALL, ROLE_RDP_FIREWALL,
)
from ..hetzner.errors import UserError, to_user_error
from ..hetzner.waiting import wait_actions, wait_server_status
from ..labels import desktop_labels, server_name
from .base import OpContext, Operation
from .common import delete_server_with_retry
from .resources import desktop_firewall_name


@dataclass
class LaunchParams:
    snapshot_id: int
    snapshot_disk: int
    server_type: str
    location: str
    base_type: str | None = None          # création sur ce type puis change_type (disque conservé)
    firewall_id: int | None = None           # liste d'accès commune à tous les bureaux
    desktop_firewall_id: int | None = None   # liste d'accès propre au bureau (s'ajoute à la commune)
    create_firewall_cidr: str | None = None  # crée la liste commune avec cette adresse
    allow_cidr: str | None = None            # adresse à autoriser avant la création du serveur…
    allow_scope: str = "shared"              # …sur la liste commune (« shared ») ou celle du bureau (« desktop »)
    allow_description: str = "Mon IP"
    volume_ids: list[int] = field(default_factory=list)
    primary_ip_id: int | None = None
    rdp_user: str = "Administrator"
    auto_connect: bool = True
    os_name: str = OS_WINDOWS                # système attendu (carte provisoire) ; le snapshot fait foi


class LaunchOp(Operation):
    kind = "launch"
    title = "Lancement"

    def __init__(self, ctx: OpContext, slug: str, name: str, params: LaunchParams) -> None:
        super().__init__(ctx, slug, name)
        self.params = params
        self.server_id: int | None = None
        self.os_name = params.os_name

    def execute(self) -> None:
        p, backend = self.params, self.ctx.backend
        self.set_phase("Préparation…", cancellable=True)
        firewall_ids = self._prepare_firewalls()
        self.check()

        create_type = p.base_type or p.server_type
        labels = desktop_labels(self.slug, **{
            L_ROLE: ROLE_DESKTOP, L_IMAGE: p.snapshot_id, L_OP: OP_LAUNCHING,
            L_OP_TS: int(time.time()), L_OP_HOST: self.ctx.host})
        carried = self._carried_labels()
        labels.update(carried)
        self.os_name = carried.get(L_OS, self.params.os_name)
        self.set_phase(f"Création du serveur ({create_type}, {p.location})…", 0)
        server, action_ids = backend.create_server(
            name=server_name(self.slug), server_type=create_type, image_id=p.snapshot_id,
            location=p.location, labels=labels, firewall_ids=firewall_ids,
            volume_ids=p.volume_ids, primary_ip_id=p.primary_ip_id, start=p.base_type is None)
        self.server_id = server.id
        self.log(f"Serveur {server.name} créé ({create_type}, {p.location})")
        wait_actions(backend, action_ids, self, timeout_s=900,
                     on_progress=lambda pct: self.set_progress(pct, f"Création du serveur… {pct} %"))

        if p.base_type:
            self._switch_type(server.id)

        srv = backend.update_server_labels(server.id, remove=OP_LABELS) or backend.get_server(server.id)
        self.ctx.config.update_prefs(self.slug, last_type=p.server_type, last_location=p.location,
                                     rdp_user=p.rdp_user)
        ip = srv.ipv4 if srv else None
        if ip:
            password = self.ctx.creds.get_password(self.slug)
            if password and rdp.write_termsrv_cred(ip, p.rdp_user, password):
                self.ctx.config.add_cred(ip, self.slug)
            if rdp.trust_certificate(ip, carried.get(L_CERT), p.rdp_user):
                self.ctx.config.add_cred(ip, self.slug)
            rdp.write_rdp_file(self.slug, ip, p.rdp_user, self.os_name)
        self.result = {"server_id": server.id, "ip": ip, "auto_connect": p.auto_connect}
        self.success_message = f"« {self.name} » démarre ({ip or 'sans IPv4'}) — le système arrive…"

    def _carried_labels(self) -> dict[str, str]:
        """Labels du snapshot qui suivent le bureau sur son serveur : système (Linux, distribution, certificat)
        et licence d'évaluation telle qu'elle sera après le démarrage (prolongation automatique)."""
        try:
            snap = self.ctx.backend.get_image(self.params.snapshot_id)
        except Exception:  # noqa: BLE001 - ces labels ne doivent jamais bloquer un lancement
            return {}
        if snap is None:
            return {}
        labels = {k: v for k, v in snap.labels.items() if k in OS_LABELS}
        lic = license.from_labels(snap.labels)
        if lic is not None:
            lic, rearmed = license.at_boot(lic, date.today())
            if rearmed:
                self.log("Licence d'évaluation : prolongée automatiquement au démarrage (180 jours, un "
                         "redémarrage de plus)")
            labels.update(license.to_labels(lic))
        return labels

    def _prepare_firewalls(self) -> list[int]:
        """Pare-feux à appliquer au serveur : liste commune et liste propre au bureau (créées au besoin)."""
        p, backend = self.params, self.ctx.backend
        shared, own = p.firewall_id, p.desktop_firewall_id
        if p.create_firewall_cidr:
            rules = netutil.with_source_added([], p.create_firewall_cidr, p.allow_description)
            fw = backend.create_firewall(DEFAULT_FIREWALL_NAME, {L_MANAGED: "1", L_ROLE: ROLE_RDP_FIREWALL},
                                         rules)
            self.log(f"Pare-feu {fw.name} créé (RDP depuis {p.create_firewall_cidr})")
            shared = fw.id
        elif p.allow_cidr:
            target = own if p.allow_scope == "desktop" else shared
            if target:
                ids = backend.modify_firewall_rules(
                    target, lambda rules: netutil.with_source_added(rules, p.allow_cidr, p.allow_description))
                wait_actions(backend, ids, self, timeout_s=120)
            elif p.allow_scope == "desktop":
                fw = backend.create_firewall(
                    desktop_firewall_name(self.slug), desktop_labels(self.slug, **{L_ROLE: ROLE_DESKTOP_FIREWALL}),
                    netutil.with_source_added([], p.allow_cidr, p.allow_description))
                own = fw.id
            where = "ce bureau" if p.allow_scope == "desktop" else "tous les bureaux"
            self.log(f"IP {p.allow_cidr} autorisée pour {where}")
        return [fid for fid in (shared, own) if fid]

    def _switch_type(self, server_id: int) -> None:
        p, backend = self.params, self.ctx.backend
        wait_server_status(backend, server_id, {"off"}, self, timeout_s=300, interval=3)
        self.set_phase(f"Passage en {p.server_type} (disque conservé à {p.snapshot_disk} Go)…")
        try:
            action_id = backend.change_type(server_id, p.server_type, upgrade_disk=False)
            wait_actions(backend, [action_id], self, timeout_s=900)
        except Exception as exc:  # noqa: BLE001
            err = to_user_error(exc)
            choice = self.ask(
                "Changement de type impossible",
                f"{err.message}.\n\nLe serveur existe déjà en {p.base_type}. Le démarrer tel quel, "
                "ou le supprimer ?",
                [("start", f"Démarrer en {p.base_type}", "primary"), ("delete", "Supprimer le serveur", "danger")],
                default="start")
            if choice == "delete":
                delete_server_with_retry(self, server_id)
                self.server_id = None
                raise UserError("Lancement abandonné : le serveur a été supprimé") from None
        self.set_phase("Démarrage du serveur…")
        wait_actions(backend, [backend.server_action(server_id, "poweron")], self, timeout_s=300)

    def on_failure(self) -> None:
        if self.server_id:
            self.ctx.backend.update_server_labels(self.server_id, remove=OP_LABELS)
