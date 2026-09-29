"""Formatage à la française des montants, durées et dates."""

from __future__ import annotations

from datetime import datetime, timezone


SERVER_STATUS_FR = {
    "running": "allumé", "off": "éteint", "initializing": "en création", "starting": "démarrage",
    "stopping": "arrêt en cours", "deleting": "suppression", "migrating": "migration",
    "rebuilding": "reconstruction", "unknown": "inconnu",
}


def status(value: str) -> str:
    return SERVER_STATUS_FR.get(value, value)


def eur(amount: float, decimals: int = 2) -> str:
    text = f"{amount:,.{decimals}f}".replace(",", " ").replace(".", ",")
    return f"{text} €"


def eur_h(amount: float) -> str:
    return f"{eur(amount, 4)}/h"


def eur_m(amount: float) -> str:
    return f"{eur(amount, 2)}/mois"


def gb(size: float | None) -> str:
    if size is None:
        return "? Go"
    if size >= 100:
        return f"{size:.0f} Go"
    return f"{size:.1f} Go".replace(".", ",")


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d}"
    days, hours = divmod(hours, 24)
    return f"{days} j {hours} h"


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}:{sec:02d}"


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "jamais"
    now = now or datetime.now(timezone.utc)
    delta = (now - when).total_seconds()
    if delta < 60:
        return "à l'instant"
    if delta < 3600:
        return f"il y a {int(delta // 60)} min"
    if delta < 86400:
        return f"il y a {int(delta // 3600)} h"
    return f"il y a {int(delta // 86400)} j"


def date_short(when: datetime | None) -> str:
    if when is None:
        return "—"
    return when.astimezone().strftime("%d/%m %H:%M")


def date_long(when: datetime | None) -> str:
    if when is None:
        return "—"
    return when.astimezone().strftime("%d/%m/%Y à %H:%M")
