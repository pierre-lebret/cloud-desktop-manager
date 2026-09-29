"""Préférences locales (config.json) et historique des sessions (sessions.jsonl).

L'état des bureaux vit dans les labels Hetzner ; ce fichier ne garde que des préférences.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from .constants import CONFIG_PATH, DEFAULT_RDP_USER, DEFAULT_SETTINGS, SESSIONS_PATH

log = logging.getLogger(__name__)


@dataclass
class DesktopPrefs:
    display_name: str | None = None
    rdp_user: str = DEFAULT_RDP_USER
    last_type: str | None = None
    last_location: str | None = None
    auto_connect: bool | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "DesktopPrefs":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class AppConfig:
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    desktops: dict[str, DesktopPrefs] = field(default_factory=dict)
    managed_creds: dict[str, str] = field(default_factory=dict)  # ip -> slug (identifiants TERMSRV écrits)
    path: Path = CONFIG_PATH

    def __post_init__(self) -> None:
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        cfg = cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cfg
        except (OSError, ValueError) as exc:
            log.warning("Configuration illisible (%s), valeurs par défaut utilisées", exc)
            return cfg
        cfg.settings.update(raw.get("settings") or {})
        cfg.desktops = {k: DesktopPrefs.from_dict(v) for k, v in (raw.get("desktops") or {}).items()}
        cfg.managed_creds = dict(raw.get("managed_creds") or {})
        return cfg

    def save(self) -> None:
        with self._lock:
            data = {"settings": self.settings,
                    "desktops": {k: asdict(v) for k, v in self.desktops.items()},
                    "managed_creds": self.managed_creds}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def get(self, key: str):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def prefs(self, slug: str) -> DesktopPrefs:
        with self._lock:
            return self.desktops.setdefault(slug, DesktopPrefs())

    def update_prefs(self, slug: str, **changes) -> None:
        with self._lock:
            prefs = self.prefs(slug)
            for key, value in changes.items():
                setattr(prefs, key, value)
            self.save()

    def copy_prefs(self, src: str, dst: str, display_name: str) -> None:
        with self._lock:
            base = asdict(self.prefs(src)) if src in self.desktops else {}
            base["display_name"] = display_name
            self.desktops[dst] = DesktopPrefs.from_dict(base)
            self.save()

    def forget_desktop(self, slug: str) -> None:
        with self._lock:
            self.desktops.pop(slug, None)
            self.save()

    def add_cred(self, ip: str, slug: str) -> None:
        with self._lock:
            self.managed_creds[ip] = slug
            self.save()

    def remove_cred(self, ip: str) -> None:
        with self._lock:
            if self.managed_creds.pop(ip, None) is not None:
                self.save()


class SessionLog:
    """Une ligne JSON par session fermée : durée et coût, pour l'estimation mensuelle."""

    def __init__(self, path: Path = SESSIONS_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def month_total(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        total = 0.0
        for rec in self.read_all():
            try:
                end = datetime.fromisoformat(rec["end"])
            except (KeyError, ValueError):
                continue
            if end.year == now.year and end.month == now.month:
                total += float(rec.get("cost") or 0)
        return total
