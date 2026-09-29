"""Copie archivée d'un bureau.

Hetzner ne sait pas copier une image : on crée un serveur temporaire jamais démarré (IPv6 seule,
rien d'exposé), on le snapshotte sous le nouveau bureau, puis on le supprime quoi qu'il arrive.
Le type temporaire a exactement le disque du snapshot pour ne pas changer son disk_size.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..constants import L_LOC, L_ROLE, L_SRC_SERVER, L_TYPE, ROLE_TMP
from ..hetzner.errors import UserError
from ..hetzner.waiting import wait_actions, wait_server_status
from ..labels import desktop_labels, format_description, tmp_server_name
from ..models import SnapshotInfo
from ..offers import cheapest_base_anywhere
from .base import OpContext, Operation
from .common import delete_server_with_retry, wait_snapshot_ready


class DuplicateOp(Operation):
    kind = "duplicate"
    title = "Duplication"

    def __init__(self, ctx: OpContext, src_slug: str, snapshot: SnapshotInfo, new_slug: str,
                 new_name: str) -> None:
        super().__init__(ctx, new_slug, new_name)
        self.src_slug = src_slug
        self.snapshot = snapshot
        self.tmp_server_id: int | None = None

    def execute(self) -> None:
        backend, snap = self.ctx.backend, self.snapshot
        static = self.ctx.static()
        choice = cheapest_base_anywhere(static.server_types, list(static.locations), snap.disk_size,
                                        snap.architecture, prefer=snap.labels.get(L_LOC))
        if choice is None:
            raise UserError(f"Aucun type de serveur à {snap.disk_size} Go disponible pour la copie",
                            "Réessayez plus tard, ou utilisez « Dupliquer et lancer ».")
        location, stype = choice
        self.set_phase(f"Serveur temporaire ({stype.name}, {location})…", 0)
        srv, action_ids = backend.create_server(
            name=tmp_server_name(self.slug), server_type=stype.name, image_id=snap.id, location=location,
            labels=desktop_labels(self.slug, **{L_ROLE: ROLE_TMP}), start=False,
            enable_ipv4=False, enable_ipv6=True)
        self.tmp_server_id = srv.id
        try:
            wait_actions(backend, action_ids, self, timeout_s=900,
                         on_progress=lambda pct: self.set_progress(pct, f"Serveur temporaire… {pct} %"))
            wait_server_status(backend, srv.id, {"off"}, self, timeout_s=300, interval=3)
            self.set_phase("Copie du snapshot…", 0)
            labels = desktop_labels(self.slug, **{L_SRC_SERVER: srv.id, L_TYPE: snap.labels.get(L_TYPE, stype.name),
                                                  L_LOC: snap.labels.get(L_LOC, location)})
            image_id, action_id = backend.create_snapshot(
                srv.id, format_description(self.name, datetime.now(timezone.utc)), labels)
            wait_snapshot_ready(self, image_id, action_id)
        finally:
            self.set_phase("Suppression du serveur temporaire…")
            delete_server_with_retry(self, srv.id)
            self.tmp_server_id = None
        self.ctx.config.copy_prefs(self.src_slug, self.slug, self.name)
        self.ctx.creds.copy_password(self.src_slug, self.slug)
        self.success_message = f"Copie « {self.name} » créée (archivée)"
