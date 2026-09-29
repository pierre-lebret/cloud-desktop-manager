"""Opérations courtes : volumes, pare-feu, IP fixe, adoption, snapshots, suppression, nettoyage."""

from __future__ import annotations

from .. import fmt, netutil
from ..constants import (
    DEFAULT_FIREWALL_NAME, L_MANAGED, L_ROLE, OP_LABELS, ROLE_DESKTOP, ROLE_RDP_FIREWALL,
)
from ..hetzner.waiting import wait_actions
from ..labels import desktop_labels, format_description, parse_description
from ..models import FirewallInfo, PrimaryIpInfo, ServerInfo, SnapshotInfo, VolumeInfo
from .base import OpContext, Operation
from .common import delete_server_with_retry


class AddVolumeOp(Operation):
    title = "Ajout d'un volume"

    def __init__(self, ctx: OpContext, slug: str, name: str, server_id: int, size: int, volume_name: str):
        super().__init__(ctx, slug, name)
        self.server_id, self.size, self.volume_name = server_id, size, volume_name

    def execute(self) -> None:
        self.set_phase(f"Création du volume {self.volume_name} ({self.size} Go)…")
        vol, ids = self.ctx.backend.create_volume(self.size, self.volume_name, desktop_labels(self.slug),
                                                  self.server_id)
        wait_actions(self.ctx.backend, ids, self, timeout_s=300)
        self.result = {"volume_id": vol.id}
        self.followup = "volume_help"
        self.success_message = f"Volume « {vol.name} » ({vol.size} Go) attaché à « {self.name} »"


class VolumeActionOp(Operation):
    title = "Volume"

    def __init__(self, ctx: OpContext, slug: str | None, name: str, volume: VolumeInfo, action: str,
                 server_id: int | None = None, size: int | None = None):
        super().__init__(ctx, slug, name)
        self.volume, self.action, self.server_id, self.size = volume, action, server_id, size

    def execute(self) -> None:
        backend, vol = self.ctx.backend, self.volume
        if self.action == "attach":
            self.set_phase(f"Attachement de {vol.name}…")
            wait_actions(backend, [backend.attach_volume(vol.id, self.server_id)], self, timeout_s=300)
            self.success_message = f"Volume « {vol.name} » attaché"
        elif self.action == "detach":
            self.set_phase(f"Détachement de {vol.name}…")
            wait_actions(backend, [backend.detach_volume(vol.id)], self, timeout_s=300)
            self.success_message = f"Volume « {vol.name} » détaché (toujours facturé)"
        elif self.action == "resize":
            self.set_phase(f"Agrandissement de {vol.name} à {self.size} Go…")
            wait_actions(backend, [backend.resize_volume(vol.id, self.size)], self, timeout_s=300)
            self.followup = "resize_help"
            self.success_message = f"Volume « {vol.name} » agrandi à {self.size} Go"
        elif self.action == "delete":
            self.set_phase(f"Suppression de {vol.name}…")
            if vol.server_id:
                wait_actions(backend, [backend.detach_volume(vol.id)], self, timeout_s=300)
            backend.delete_volume(vol.id)
            self.success_message = f"Volume « {vol.name} » supprimé"


class FirewallOp(Operation):
    title = "Pare-feu"
    blocks_desktop = False

    def __init__(self, ctx: OpContext, firewall_id: int, add: list[tuple[str, str]] = (),
                 remove: list[str] = (), normalize_udp: bool = False):
        super().__init__(ctx, None, "Pare-feu")
        self.firewall_id, self.add, self.remove, self.normalize_udp = firewall_id, list(add), list(remove), normalize_udp

    def execute(self) -> None:
        def mutate(rules):
            for cidr in self.remove:
                rules = netutil.with_source_removed(rules, cidr)
            for cidr, desc in self.add:
                rules = netutil.with_source_added(rules, cidr, desc)
            return netutil.with_udp_normalized(rules) if self.normalize_udp else rules
        self.set_phase("Mise à jour du pare-feu…")
        wait_actions(self.ctx.backend, self.ctx.backend.modify_firewall_rules(self.firewall_id, mutate),
                     self, timeout_s=120)
        parts = [f"{c} autorisée" for c, _ in self.add] + [f"{c} retirée" for c in self.remove]
        self.success_message = "Pare-feu : " + (", ".join(parts) if parts else "règles UDP ajoutées")


class CreateFirewallOp(Operation):
    title = "Création du pare-feu"
    blocks_desktop = False

    def __init__(self, ctx: OpContext, cidr: str, description: str, server_ids: list[int] = ()):
        super().__init__(ctx, None, "Pare-feu")
        self.cidr, self.description, self.server_ids = cidr, description, list(server_ids)

    def execute(self) -> None:
        backend = self.ctx.backend
        fw = backend.create_firewall(DEFAULT_FIREWALL_NAME, {L_MANAGED: "1", L_ROLE: ROLE_RDP_FIREWALL},
                                     netutil.with_source_added([], self.cidr, self.description))
        if self.server_ids:
            wait_actions(backend, backend.apply_firewall(fw.id, self.server_ids), self, timeout_s=120)
        self.success_message = f"Pare-feu {fw.name} créé : RDP limité à {self.cidr}"


class AdoptFirewallOp(Operation):
    title = "Adoption du pare-feu"
    blocks_desktop = False

    def __init__(self, ctx: OpContext, firewall: FirewallInfo, server_ids: list[int] = ()):
        super().__init__(ctx, None, firewall.name)
        self.firewall, self.server_ids = firewall, list(server_ids)

    def execute(self) -> None:
        backend = self.ctx.backend
        backend.update_firewall_labels(self.firewall.id, {L_MANAGED: "1", L_ROLE: ROLE_RDP_FIREWALL})
        wait_actions(backend, backend.modify_firewall_rules(self.firewall.id, netutil.with_udp_normalized),
                     self, timeout_s=120)
        missing = [sid for sid in self.server_ids if sid not in self.firewall.applied_server_ids]
        if missing:
            wait_actions(backend, backend.apply_firewall(self.firewall.id, missing), self, timeout_s=120)
        self.success_message = f"Pare-feu « {self.firewall.name} » géré (UDP 3389 ajouté pour RDP)"


class ApplyFirewallOp(Operation):
    title = "Application du pare-feu"

    def __init__(self, ctx: OpContext, slug: str, name: str, firewall_id: int, server_id: int):
        super().__init__(ctx, slug, name)
        self.firewall_id, self.server_id = firewall_id, server_id

    def execute(self) -> None:
        wait_actions(self.ctx.backend, self.ctx.backend.apply_firewall(self.firewall_id, [self.server_id]),
                     self, timeout_s=120)
        self.success_message = f"Pare-feu appliqué à « {self.name} »"


class FixedIpOp(Operation):
    title = "IP fixe"

    def __init__(self, ctx: OpContext, slug: str, name: str, mode: str, location: str | None = None,
                 ip: PrimaryIpInfo | None = None):
        super().__init__(ctx, slug, name)
        self.mode, self.location, self.ip = mode, location, ip

    def execute(self) -> None:
        backend = self.ctx.backend
        if self.mode == "create":
            ip = backend.create_primary_ip(f"rdpm-{self.slug}-ip", self.location, desktop_labels(self.slug))
            self.success_message = f"IP fixe {ip.ip} réservée pour « {self.name} » ({self.location})"
        elif self.mode == "adopt":
            backend.update_primary_ip(self.ip, desktop_labels(self.slug), name=f"rdpm-{self.slug}-ip")
            self.success_message = f"IP {self.ip.ip} désormais fixe pour « {self.name} »"
        elif self.mode == "release":
            backend.delete_primary_ip(self.ip.id)
            self.success_message = f"IP fixe {self.ip.ip} supprimée (−0,50 €/mois)"
        self.log(self.success_message)


class AdoptSnapshotOp(Operation):
    title = "Importation"

    def __init__(self, ctx: OpContext, slug: str, name: str, snapshot: SnapshotInfo, pin: bool):
        super().__init__(ctx, slug, name)
        self.snapshot, self.pin = snapshot, pin

    def execute(self) -> None:
        backend = self.ctx.backend
        backend.update_image(self.snapshot.id, format_description(self.name, self.snapshot.created),
                             desktop_labels(self.slug))
        if self.pin and not self.snapshot.protected:
            wait_actions(backend, [backend.set_image_protection(self.snapshot.id, True)], self, timeout_s=60)
        self.success_message = f"Bureau « {self.name} » importé"


class AdoptServerOp(Operation):
    title = "Adoption du serveur"

    def __init__(self, ctx: OpContext, slug: str, name: str, server: ServerInfo):
        super().__init__(ctx, slug, name)
        self.server = server

    def execute(self) -> None:
        self.ctx.backend.update_server_labels(self.server.id, desktop_labels(self.slug, **{L_ROLE: ROLE_DESKTOP}))
        self.success_message = f"Serveur {self.server.name} géré comme « {self.name} »"


class RenameOp(Operation):
    title = "Renommage"

    def __init__(self, ctx: OpContext, slug: str, new_name: str, snapshots: list[SnapshotInfo]):
        super().__init__(ctx, slug, new_name)
        self.snapshots = snapshots

    def execute(self) -> None:
        for snap in self.snapshots:
            _, when = parse_description(snap.description)
            stamp = when.astimezone() if when else snap.created
            self.ctx.backend.update_image(snap.id, format_description(self.name, stamp))
        self.ctx.config.update_prefs(self.slug, display_name=self.name)
        self.success_message = f"Bureau renommé en « {self.name} »"


class SnapshotActionOp(Operation):
    title = "Sauvegarde"

    def __init__(self, ctx: OpContext, slug: str | None, name: str, snapshot: SnapshotInfo, action: str):
        super().__init__(ctx, slug, name)
        self.snapshot, self.action = snapshot, action

    def execute(self) -> None:
        backend, snap = self.ctx.backend, self.snapshot
        label = f"sauvegarde du {fmt.date_short(snap.created)}"
        if self.action in ("pin", "unpin"):
            wait_actions(backend, [backend.set_image_protection(snap.id, self.action == "pin")], self,
                         timeout_s=60)
            self.success_message = f"{label.capitalize()} {'épinglée' if self.action == 'pin' else 'désépinglée'}"
        elif self.action == "delete":
            if snap.protected:
                wait_actions(backend, [backend.set_image_protection(snap.id, False)], self, timeout_s=60)
            backend.delete_image(snap.id)
            self.success_message = f"{label.capitalize()} supprimée ({fmt.gb(snap.image_size)} libérés)"


class DeleteDesktopOp(Operation):
    title = "Suppression du bureau"

    def __init__(self, ctx: OpContext, slug: str, name: str, snapshots: list[SnapshotInfo],
                 volumes: list[VolumeInfo], fixed_ip: PrimaryIpInfo | None, forget_password: bool):
        super().__init__(ctx, slug, name)
        self.snapshots, self.volumes, self.fixed_ip, self.forget_password = snapshots, volumes, fixed_ip, forget_password

    def execute(self) -> None:
        backend = self.ctx.backend
        for i, snap in enumerate(self.snapshots, 1):
            self.set_phase(f"Suppression des sauvegardes ({i}/{len(self.snapshots)})…")
            if snap.protected:
                wait_actions(backend, [backend.set_image_protection(snap.id, False)], self, timeout_s=60)
            backend.delete_image(snap.id)
        for vol in self.volumes:
            self.set_phase(f"Suppression du volume {vol.name}…")
            backend.delete_volume(vol.id)
        if self.fixed_ip:
            backend.delete_primary_ip(self.fixed_ip.id)
        if self.forget_password:
            self.ctx.creds.delete_password(self.slug)
            self.ctx.config.forget_desktop(self.slug)
        self.success_message = f"Bureau « {self.name} » supprimé"


class PowerOp(Operation):
    title = "Alimentation"

    def __init__(self, ctx: OpContext, slug: str, name: str, server_id: int, action: str):
        super().__init__(ctx, slug, name)
        self.server_id, self.action = server_id, action

    def execute(self) -> None:
        verb = {"poweron": "Démarrage", "reboot": "Redémarrage", "reset": "Réinitialisation"}[self.action]
        self.set_phase(f"{verb}…")
        wait_actions(self.ctx.backend, [self.ctx.backend.server_action(self.server_id, self.action)], self,
                     timeout_s=300)
        self.result = {"server_id": self.server_id}
        self.success_message = f"« {self.name} » : {verb.lower()} demandé"


class ClearOpLabelsOp(Operation):
    title = "Mise à jour"

    def __init__(self, ctx: OpContext, slug: str, name: str, server_id: int):
        super().__init__(ctx, slug, name)
        self.server_id = server_id

    def execute(self) -> None:
        self.ctx.backend.update_server_labels(self.server_id, remove=OP_LABELS)
        self.success_message = f"« {self.name} » : opération interrompue ignorée"


class CleanupOp(Operation):
    """Suppression d'une ressource dormante (IP, volume, snapshot, serveur temporaire)."""

    title = "Nettoyage"
    blocks_desktop = False

    def __init__(self, ctx: OpContext, kind: str, resource, label: str):
        super().__init__(ctx, None, label)
        self.resource_kind, self.resource = kind, resource

    def execute(self) -> None:
        backend, res = self.ctx.backend, self.resource
        self.set_phase(f"Suppression : {self.name}…")
        if self.resource_kind == "ip":
            backend.delete_primary_ip(res.id)
        elif self.resource_kind == "volume":
            backend.delete_volume(res.id)
        elif self.resource_kind == "snapshot":
            if res.protected:
                wait_actions(backend, [backend.set_image_protection(res.id, False)], self, timeout_s=60)
            backend.delete_image(res.id)
        elif self.resource_kind == "server":
            delete_server_with_retry(self, res.id)
        self.success_message = f"{self.name} : supprimé"
