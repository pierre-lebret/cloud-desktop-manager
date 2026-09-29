"""Rétention des snapshots d'un bureau (fonction pure)."""

from __future__ import annotations

from .models import SnapshotInfo


def snapshots_to_delete(snapshots: list[SnapshotInfo], keep: int,
                        protect_ids: set[int] | frozenset[int] = frozenset(),
                        new_was_forced: bool = False) -> list[SnapshotInfo]:
    """Garde les `keep` plus récents (hors épinglés), jamais le dernier disponible.

    Si la dernière sauvegarde a suivi un arrêt forcé, on garde aussi la plus récente sauvegarde
    propre, au cas où Windows aurait été abîmé par la coupure.
    """
    keep = max(1, int(keep))
    available = sorted((s for s in snapshots if s.available), key=lambda s: (s.created, s.id), reverse=True)
    unpinned = [s for s in available if not s.protected]
    kept = set(protect_ids) | {s.id for s in unpinned[:keep]}
    if new_was_forced:
        clean = next((s for s in unpinned if not s.forced), None)
        if clean:
            kept.add(clean.id)
    doomed = [s for s in unpinned if s.id not in kept]
    if len(available) - len(doomed) < 1:
        doomed = doomed[1:]
    return doomed
