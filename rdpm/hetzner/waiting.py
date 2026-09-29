"""Attentes robustes : échéance en temps réel, annulation, progression.

Les attentes par défaut de hcloud abandonnent après ~2 minutes, trop court pour un snapshot.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol

from ..models import ServerInfo
from .errors import ActionFailed, WaitTimeout


class Waiter(Protocol):
    def check(self) -> None: ...
    def sleep(self, seconds: float) -> None: ...


def wait_actions(backend, action_ids: list[int], waiter: Waiter, *, timeout_s: float,
                 interval: float = 3.0, on_progress: Callable[[int], None] | None = None) -> None:
    pending = [a for a in action_ids if a]
    progress = {aid: 0 for aid in pending}
    deadline = time.monotonic() + timeout_s
    while pending:
        waiter.check()
        states = backend.get_actions(pending)
        for aid in list(pending):
            state = states.get(aid)
            if state is None:
                continue
            if state.status == "error":
                raise ActionFailed(state)
            if state.status == "success":
                pending.remove(aid)
                progress[aid] = 100
            else:
                progress[aid] = state.progress
        if on_progress and progress:
            on_progress(int(sum(progress.values()) / len(progress)))
        if not pending:
            return
        if time.monotonic() > deadline:
            raise WaitTimeout("L'action Hetzner n'est pas terminée dans le délai imparti")
        waiter.sleep(interval)


def wait_server_status(backend, server_id: int, statuses: set[str], waiter: Waiter, *,
                       timeout_s: float, interval: float = 5.0,
                       on_tick: Callable[[float], None] | None = None) -> ServerInfo | None:
    """Attend qu'un serveur atteigne un des statuts (« deleted » = introuvable)."""
    start = time.monotonic()
    while True:
        waiter.check()
        server = backend.get_server(server_id)
        if server is None:
            if "deleted" in statuses:
                return None
            raise WaitTimeout("Le serveur a disparu pendant l'attente")
        if server.status in statuses:
            return server
        elapsed = time.monotonic() - start
        if on_tick:
            on_tick(elapsed)
        if elapsed > timeout_s:
            raise WaitTimeout(f"Le serveur n'a pas atteint l'état attendu en {int(timeout_s)} s")
        waiter.sleep(interval)
