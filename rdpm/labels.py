"""Slugs, labels Hetzner et descriptions de snapshots."""

from __future__ import annotations

import re
import socket
import time
import unicodedata
from datetime import datetime

from .constants import L_DESKTOP, L_MANAGED

# Règle Hetzner : 63 caractères max, alphanumérique au début et à la fin, - _ . au milieu.
_VALUE_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?$")
DESC_SEP = " — "
SLUG_MAX = 40


def slugify(text: str, max_len: int = SLUG_MAX) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or "bureau"


def unique_slug(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    for i in range(2, 1000):
        suffix = f"-{i}"
        candidate = base[: SLUG_MAX - len(suffix)].strip("-") + suffix
        if candidate not in existing:
            return candidate
    raise ValueError("Impossible de générer un identifiant unique")


def is_valid_label_value(value: str) -> bool:
    return len(value) <= 63 and bool(_VALUE_RE.match(value))


def desktop_labels(slug: str, **extra: object) -> dict[str, str]:
    labels = {L_MANAGED: "1", L_DESKTOP: slug}
    for key, value in extra.items():
        labels[key.replace("_", "-")] = str(value)
    return labels


def merge_labels(old: dict[str, str], updates: dict[str, str] | None = None,
                 remove: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """Les écritures de labels Hetzner remplacent l'ensemble : on fusionne côté client."""
    merged = {k: v for k, v in old.items() if k not in remove}
    for key, value in (updates or {}).items():
        value = str(value)
        if not is_valid_label_value(value):
            raise ValueError(f"Valeur de label invalide pour {key!r} : {value!r}")
        merged[key] = value
    return merged


def format_description(name: str, when: datetime) -> str:
    return f"{name}{DESC_SEP}{when.astimezone():%Y-%m-%d %H:%M}"


def parse_description(description: str | None) -> tuple[str, datetime | None]:
    if not description:
        return "", None
    name, sep, stamp = description.rpartition(DESC_SEP)
    if sep:
        try:
            return name, datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return description, None


def server_name(slug: str) -> str:
    return f"rdpm-{slug}"


def tmp_server_name(slug: str) -> str:
    return f"rdpm-tmp-{slug}-{int(time.time())}"


def host_label() -> str:
    return slugify(socket.gethostname(), max_len=63)
