"""Exécuteur d'opérations : au plus une opération bloquante par bureau."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from ..events import OpFinished
from ..hetzner.errors import UserError
from .base import Operation


class BusyError(UserError):
    def __init__(self, op: Operation) -> None:
        super().__init__(f"Une opération est déjà en cours sur ce bureau : {op.title}", code="busy")


class OperationRunner:
    def __init__(self, max_workers: int = 8) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="op")
        self._lock = threading.Lock()
        self._active: dict[str, Operation] = {}

    def submit(self, op: Operation) -> Operation:
        with self._lock:
            if op.slug and op.blocks_desktop:
                current = self._for_slug(op.slug)
                if current:
                    raise BusyError(current)
            self._active[op.id] = op
        self._pool.submit(self._run, op)
        return op

    def _run(self, op: Operation) -> None:
        try:
            op.run()
        finally:
            with self._lock:
                self._active.pop(op.id, None)
            op.ctx.emit(OpFinished(op))

    def _for_slug(self, slug: str) -> Operation | None:
        return next((o for o in self._active.values() if o.slug == slug and o.blocks_desktop), None)

    def for_slug(self, slug: str) -> Operation | None:
        with self._lock:
            return self._for_slug(slug)

    def active(self) -> list[Operation]:
        with self._lock:
            return list(self._active.values())

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
