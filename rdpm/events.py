"""Événements envoyés par les threads de travail au thread Tk (via une SimpleQueue)."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from .models import Inventory, StaticData


@dataclass
class LogEvent:
    level: str
    message: str
    slug: str | None = None


@dataclass
class ToastEvent:
    kind: str
    message: str


@dataclass
class OpProgress:
    op: Any


@dataclass
class OpFinished:
    op: Any


@dataclass
class NeedDecision:
    op: Any
    title: str
    message: str
    options: list[tuple[str, str, str]]  # (clé, libellé, style)
    default: str | None
    countdown_s: int | None
    future: Future = field(default_factory=Future)


@dataclass
class RefreshRequest:
    pass


@dataclass
class InventoryLoaded:
    inventory: Inventory | None
    error: Exception | None = None
    started: float = 0.0


@dataclass
class StaticLoaded:
    static: StaticData | None
    error: Exception | None = None


@dataclass
class ProbeResult:
    server_id: int
    ok: bool


@dataclass
class PublicIpResult:
    ip: str | None
