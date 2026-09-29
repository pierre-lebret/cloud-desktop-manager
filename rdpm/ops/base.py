"""Opération longue exécutée dans un thread de travail, avec phases, annulation et décisions."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable

from ..config import AppConfig, SessionLog
from ..events import LogEvent, NeedDecision, OpProgress
from ..hetzner.errors import OpCancelled, UserError, to_user_error
from ..models import StaticData
from ..rdp import CredentialStore

log = logging.getLogger("rdpm.ops")


@dataclass
class OpContext:
    backend: object
    config: AppConfig
    creds: CredentialStore
    sessions: SessionLog
    emit: Callable[[object], None]
    host: str
    hard_stop: threading.Event
    static: Callable[[], StaticData | None]
    register_secret: Callable[[str], None] | None = None  # masque un secret dans app.log


class Operation:
    kind = "other"          # launch / save / checkpoint / discard / duplicate / other
    title = "Opération"
    blocks_desktop = True
    closes_server = False   # vrai si l'opération supprime le serveur du bureau

    def __init__(self, ctx: OpContext, slug: str | None, name: str | None = None) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.ctx = ctx
        self.slug = slug
        self.name = name or slug or ""
        self.phase = "En attente…"
        self.progress: float | None = None
        self.cancellable = False
        self.started = time.monotonic()
        self.outcome: str | None = None  # ok / failed / cancelled
        self.error: UserError | None = None
        self.success_message: str | None = None
        self.followup: str | None = None
        self.result: dict = {}
        self._cancel = threading.Event()

    # --- contrat pour les sous-classes -----------------------------------------------------
    def execute(self) -> None:
        raise NotImplementedError

    def on_failure(self) -> None:
        """Nettoyage best-effort après un échec ou une annulation."""

    # --- exécution -------------------------------------------------------------------------
    def run(self) -> None:
        try:
            self.execute()
            self.outcome = "ok"
        except OpCancelled as exc:
            self.outcome = "cancelled"
            try:
                self.log(str(exc) or "Opération annulée", "warning")
            finally:
                self._safe_cleanup()   # même si la journalisation elle-même échoue
        except Exception as exc:  # noqa: BLE001 - toute erreur doit remonter proprement à l'UI
            self.outcome = "failed"
            self.error = to_user_error(exc)
            try:
                log.exception("Échec de %s (%s)", self.title, self.slug)
                self.log(f"Échec : {self.error}", "error")
            finally:
                self._safe_cleanup()

    def _safe_cleanup(self) -> None:
        try:
            self.on_failure()
        except Exception:  # noqa: BLE001
            log.exception("Nettoyage après échec impossible")

    # --- Waiter (utilisé par hetzner.waiting) ----------------------------------------------
    def check(self) -> None:
        if self.ctx.hard_stop.is_set():
            raise OpCancelled("Application fermée pendant l'opération")
        if self.cancellable and self._cancel.is_set():
            raise OpCancelled("Opération annulée")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self.ctx.hard_stop.wait(min(0.25, remaining)):
                self.check()

    def request_cancel(self) -> bool:
        if not self.cancellable:
            return False
        self._cancel.set()
        return True

    # --- retour d'information --------------------------------------------------------------
    def set_phase(self, phase: str, progress: float | None = None, cancellable: bool = False) -> None:
        self.phase = phase
        self.progress = progress
        self.cancellable = cancellable
        self.ctx.emit(OpProgress(self))

    def set_progress(self, percent: int, phase: str | None = None) -> None:
        self.progress = percent
        if phase:
            self.phase = phase
        self.ctx.emit(OpProgress(self))

    def log(self, message: str, level: str = "info") -> None:
        getattr(log, "warning" if level == "warning" else "error" if level == "error" else "info")(
            "[%s] %s", self.slug or "-", message)
        self.ctx.emit(LogEvent(level, message, self.slug))

    def ask(self, title: str, message: str, options: list[tuple[str, str, str]],
            default: str | None = None, countdown_s: int | None = None) -> str:
        event = NeedDecision(self, title, message, options, default, countdown_s)
        previous = (self.phase, self.progress, self.cancellable)
        self.set_phase(f"En attente de ta décision : {title.lower()}")
        self.ctx.emit(event)
        try:
            while True:
                try:
                    return event.future.result(timeout=0.5)
                except FutureTimeout:
                    if self.ctx.hard_stop.is_set():
                        raise OpCancelled("Application fermée pendant une décision") from None
        finally:
            self.set_phase(*previous)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started
