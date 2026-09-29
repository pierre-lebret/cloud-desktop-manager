"""Regroupement de l'inventaire en bureaux et machine à états (fonctions pures)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

from .constants import ROLE_DESKTOP, ROLE_TMP
from .models import Desktop, FirewallInfo, Inventory, PrimaryIpInfo, ServerInfo, SnapshotInfo, VolumeInfo


class DState(str, Enum):
    EMPTY = "empty"
    ARCHIVED = "archived"
    SNAPSHOT_PENDING = "snapshot_pending"
    PENDING = "pending"
    LAUNCHING = "launching"
    BOOTING = "booting"
    READY = "ready"
    UNREACHABLE = "unreachable"
    STOPPING = "stopping"
    OFF_BILLED = "off_billed"
    SAVING = "saving"
    CHECKPOINTING = "checkpointing"
    DISCARDING = "discarding"
    DUPLICATING = "duplicating"
    BUILDING = "building"
    BUSY = "busy"
    INTERRUPTED = "interrupted"
    REMOTE_OP = "remote_op"
    CONFLICT = "conflict"


# (libellé, couleur du thème)
STATE_STYLE: dict[DState, tuple[str, str]] = {
    DState.EMPTY: ("Vide", "muted"),
    DState.ARCHIVED: ("Archivé", "muted"),
    DState.SNAPSHOT_PENDING: ("Snapshot en cours", "accent"),
    DState.PENDING: ("En préparation", "accent"),
    DState.LAUNCHING: ("Création…", "info"),
    DState.BOOTING: ("Démarrage de Windows…", "info"),
    DState.READY: ("Prêt", "success"),
    DState.UNREACHABLE: ("RDP injoignable", "warning"),
    DState.STOPPING: ("Arrêt…", "info"),
    DState.OFF_BILLED: ("Éteint — facturé", "warning"),
    DState.SAVING: ("Sauvegarde…", "accent"),
    DState.CHECKPOINTING: ("Sauvegarde (reste allumé)", "accent"),
    DState.DISCARDING: ("Suppression…", "danger"),
    DState.DUPLICATING: ("Duplication…", "accent"),
    DState.BUILDING: ("Installation de Windows…", "accent"),
    DState.BUSY: ("Opération en cours…", "info"),
    DState.INTERRUPTED: ("Opération interrompue", "warning"),
    DState.REMOTE_OP: ("Opération sur un autre poste", "warning"),
    DState.CONFLICT: ("Conflit : plusieurs serveurs", "danger"),
}

SERVER_STATES = {DState.BOOTING, DState.READY, DState.UNREACHABLE, DState.STOPPING, DState.OFF_BILLED}


class Act(str, Enum):
    LAUNCH = "launch"
    CONNECT = "connect"
    SAVE_CLOSE = "save_close"
    CHECKPOINT = "checkpoint"
    DISCARD = "discard"
    POWER_ON = "power_on"
    REBOOT = "reboot"
    ADD_VOLUME = "add_volume"
    VOLUMES = "volumes"
    FIREWALL = "firewall"
    COPY_IP = "copy_ip"
    COPY_PASSWORD = "copy_password"
    HISTORY = "history"
    DUPLICATE = "duplicate"
    RENAME = "rename"
    FIXED_IP = "fixed_ip"
    CREDENTIALS = "credentials"
    DELETE = "delete"
    RESUME = "resume"
    IGNORE_OP = "ignore_op"
    RESOLVE = "resolve"
    LICENSE = "license"
    CANCEL_OP = "cancel_op"


ACT_LABELS: dict[Act, str] = {
    Act.LAUNCH: "Lancer",
    Act.CONNECT: "Connecter",
    Act.SAVE_CLOSE: "Sauvegarder & fermer",
    Act.CHECKPOINT: "Sauvegarder (reste allumé)",
    Act.DISCARD: "Fermer sans sauvegarder…",
    Act.POWER_ON: "Démarrer",
    Act.REBOOT: "Redémarrer Windows",
    Act.ADD_VOLUME: "Ajouter un volume…",
    Act.VOLUMES: "Volumes…",
    Act.FIREWALL: "Autoriser une IP…",
    Act.COPY_IP: "Copier l'IP",
    Act.COPY_PASSWORD: "Copier le mot de passe",
    Act.HISTORY: "Historique des sauvegardes…",
    Act.DUPLICATE: "Dupliquer…",
    Act.RENAME: "Renommer…",
    Act.FIXED_IP: "IP fixe…",
    Act.CREDENTIALS: "Identifiants RDP…",
    Act.DELETE: "Supprimer le bureau…",
    Act.RESUME: "Reprendre",
    Act.IGNORE_OP: "Ignorer l'opération",
    Act.RESOLVE: "Résoudre…",
    Act.LICENSE: "Licence d'évaluation…",
    Act.CANCEL_OP: "Annuler",
}

_RUNNING_ACTS = [Act.CONNECT, Act.SAVE_CLOSE, Act.CHECKPOINT, Act.DISCARD, Act.ADD_VOLUME,
                 Act.VOLUMES, Act.FIREWALL, Act.COPY_IP, Act.COPY_PASSWORD, Act.REBOOT,
                 Act.CREDENTIALS, Act.HISTORY, Act.RENAME, Act.LICENSE]

_ALLOWED: dict[DState, list[Act]] = {
    DState.EMPTY: [Act.CREDENTIALS, Act.DELETE],
    DState.ARCHIVED: [Act.LAUNCH, Act.HISTORY, Act.DUPLICATE, Act.RENAME, Act.VOLUMES, Act.FIXED_IP,
                      Act.CREDENTIALS, Act.LICENSE, Act.DELETE],
    DState.SNAPSHOT_PENDING: [Act.HISTORY],
    DState.PENDING: [],
    DState.LAUNCHING: [Act.CANCEL_OP],
    DState.BOOTING: [Act.CONNECT, Act.SAVE_CLOSE, Act.DISCARD, Act.COPY_IP, Act.ADD_VOLUME,
                     Act.VOLUMES, Act.FIREWALL, Act.CREDENTIALS, Act.HISTORY],
    DState.READY: _RUNNING_ACTS,
    DState.UNREACHABLE: _RUNNING_ACTS,
    DState.STOPPING: [Act.SAVE_CLOSE, Act.DISCARD],
    DState.OFF_BILLED: [Act.SAVE_CLOSE, Act.POWER_ON, Act.DISCARD, Act.ADD_VOLUME, Act.VOLUMES,
                        Act.CREDENTIALS, Act.HISTORY],
    DState.SAVING: [Act.CANCEL_OP],
    DState.CHECKPOINTING: [Act.CANCEL_OP],
    DState.DISCARDING: [],
    DState.DUPLICATING: [],
    DState.BUILDING: [Act.CANCEL_OP],
    DState.BUSY: [],
    DState.INTERRUPTED: [Act.RESUME, Act.IGNORE_OP, Act.DISCARD],
    DState.REMOTE_OP: [Act.RESUME, Act.IGNORE_OP, Act.DISCARD],
    DState.CONFLICT: [Act.RESOLVE],
}

_PRIMARY: dict[DState, Act] = {
    DState.ARCHIVED: Act.LAUNCH,
    DState.BOOTING: Act.CONNECT,
    DState.READY: Act.CONNECT,
    DState.UNREACHABLE: Act.CONNECT,
    DState.OFF_BILLED: Act.SAVE_CLOSE,
    DState.STOPPING: Act.SAVE_CLOSE,
    DState.INTERRUPTED: Act.RESUME,
    DState.REMOTE_OP: Act.RESUME,
    DState.CONFLICT: Act.RESOLVE,
}

OP_STATES = {
    "launch": DState.LAUNCHING,
    "save": DState.SAVING,
    "checkpoint": DState.CHECKPOINTING,
    "discard": DState.DISCARDING,
    "duplicate": DState.DUPLICATING,
    "build": DState.BUILDING,
}


@dataclass
class Grouping:
    desktops: list[Desktop] = field(default_factory=list)
    unmanaged_snapshots: list[SnapshotInfo] = field(default_factory=list)
    unmanaged_servers: list[ServerInfo] = field(default_factory=list)
    tmp_servers: list[ServerInfo] = field(default_factory=list)
    orphan_volumes: list[VolumeInfo] = field(default_factory=list)
    unassigned_ips: list[PrimaryIpInfo] = field(default_factory=list)
    adoptable_firewalls: list[FirewallInfo] = field(default_factory=list)

    def desktop(self, slug: str) -> Desktop | None:
        return next((d for d in self.desktops if d.slug == slug), None)


def group_inventory(inv: Inventory, name_for: Callable[[str], str | None],
                    placeholder_slugs: Iterable[str] = ()) -> Grouping:
    g = Grouping()
    by_slug: dict[str, Desktop] = {}

    def get(slug: str) -> Desktop:
        if slug not in by_slug:
            by_slug[slug] = Desktop(slug=slug, name=slug)
        return by_slug[slug]

    for snap in sorted(inv.snapshots, key=lambda s: (s.created, s.id), reverse=True):
        if snap.slug:
            get(snap.slug).snapshots.append(snap)
        else:
            g.unmanaged_snapshots.append(snap)

    for srv in sorted(inv.servers, key=lambda s: s.created):
        if not srv.managed or not srv.slug:
            g.unmanaged_servers.append(srv)
        elif srv.role == ROLE_TMP:
            g.tmp_servers.append(srv)
        elif srv.role == ROLE_DESKTOP:
            d = get(srv.slug)
            if d.server is None:
                d.server = srv
            else:
                d.extra_servers.append(srv)
        else:
            g.unmanaged_servers.append(srv)

    for slug in placeholder_slugs:
        if slug not in by_slug:
            get(slug).placeholder = True

    server_owner = {d.server.id: d for d in by_slug.values() if d.server}
    for vol in inv.volumes:
        owner = by_slug.get(vol.slug) if vol.slug else None
        if owner is None and vol.server_id is not None:
            owner = server_owner.get(vol.server_id)
        if owner:
            owner.volumes.append(vol)
        elif vol.server_id is None:
            g.orphan_volumes.append(vol)

    for ip in inv.primary_ips:
        if ip.slug and ip.slug in by_slug and not ip.auto_delete:
            by_slug[ip.slug].fixed_ip = ip
        elif ip.assignee_id is None:
            g.unassigned_ips.append(ip)

    if inv.rdp_firewall is None:
        g.adoptable_firewalls = [f for f in inv.firewalls if f.has_rdp_rules and not f.managed]

    for d in by_slug.values():
        d.name = name_for(d.slug) or (d.latest.display_name if d.latest else None) or \
            (d.snapshots[0].display_name if d.snapshots else None) or d.slug

    g.desktops = sorted(by_slug.values(), key=lambda d: (d.server is None, d.name.lower()))
    return g


def derive_state(desktop: Desktop, op_kind: str | None, probe_ok: bool | None,
                 running_for_s: float | None, host: str, boot_timeout_s: float) -> DState:
    if desktop.extra_servers:
        return DState.CONFLICT
    if op_kind:
        return OP_STATES.get(op_kind, DState.BUSY)
    srv = desktop.server
    if srv is None:
        if desktop.latest:
            return DState.ARCHIVED
        if desktop.pending_snapshot:
            return DState.SNAPSHOT_PENDING
        return DState.PENDING if desktop.placeholder else DState.EMPTY
    if srv.op:
        return DState.REMOTE_OP if srv.op_host and srv.op_host != host else DState.INTERRUPTED
    if srv.status == "running":
        if probe_ok:
            return DState.READY
        if running_for_s is not None and running_for_s > boot_timeout_s:
            return DState.UNREACHABLE
        return DState.BOOTING
    if srv.status == "off":
        return DState.OFF_BILLED
    if srv.status in ("initializing", "starting"):
        return DState.BOOTING
    if srv.status == "stopping":
        return DState.STOPPING
    if srv.status == "deleting":
        return DState.DISCARDING
    return DState.BUSY


def allowed_actions(state: DState) -> list[Act]:
    return list(_ALLOWED.get(state, []))


def primary_action(state: DState) -> Act | None:
    return _PRIMARY.get(state)
