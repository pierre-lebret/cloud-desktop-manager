"""Lecture de /reinstall.log (journal de trans.sh dans l'environnement Alpine) : avancement, erreurs.

Format : `***** TEXTE *****` en couleur ANSI pour chaque étape (texte en majuscules), `***** ERROR *****`
suivi du message, et la progression d'aria2c pour le téléchargement de l'ISO.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BANNER = re.compile(r"^\*{5} (.+?) \*{5}$")
_ARIA = re.compile(r"\((\d{1,3})%\)")
_IMAGE_CANDIDATE = re.compile(r"^\s*echo '(.+)' >/image-name\s*$")

# (préfixe de bannière en majuscules, pourcentage, libellé)
MARKERS: list[tuple[str, int, str]] = [
    ("PROCESS WINDOWS ISO", 10, "Téléchargement de l'ISO Windows…"),
    ("IMAGES COUNT", 60, "Préparation de l'image…"),
    ("SELECTED IMAGE INFO", 62, "Préparation de l'image…"),
    ("ADD DRIVERS", 70, "Injection des pilotes VirtIO…"),
    ("MOUNT BOOT.WIM", 75, "Injection des pilotes VirtIO…"),
    ("AUTOUNATTEND.XML", 78, "Fichier de réponses (RDP, mot de passe)…"),
    ("UNMOUNT BOOT.WIM", 80, "Pilotes injectés…"),
    ("BOOT.WIM SIZE", 82, "Pilotes injectés…"),
    ("MOUNT INSTALL.WIM", 85, "Scripts de premier démarrage…"),
    ("UNMOUNT INSTALL.WIM", 90, "Installeur prêt…"),
    ("HOLD 2", 95, "Installeur prêt, personnalisation…"),
]


@dataclass
class ImagePrompt:
    requested: str
    candidates: list[str] = field(default_factory=list)


@dataclass
class LogStatus:
    percent: int = 0
    phase: str = "Démarrage de l'environnement d'installation…"
    error: str | None = None
    image_prompt: ImagePrompt | None = None
    hold2: bool = False


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_reinstall_log(text: str) -> LogStatus:
    status = LogStatus()
    lines = [line.rstrip() for line in strip_ansi(text.replace("\r", "\n")).split("\n")]
    download_pct = None
    for i, line in enumerate(lines):
        banner = _BANNER.match(line.strip())
        if banner:
            head = banner.group(1).strip()
            if head == "ERROR":
                message = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
                if message.startswith("Invalid image name:"):
                    requested = message.split(":", 1)[1].strip()
                    cands = [m.group(1) for l in lines[i + 1:] for m in [_IMAGE_CANDIDATE.match(l)] if m]
                    status.image_prompt = ImagePrompt(requested, cands)
                    status.error = None
                else:
                    status.error = message or "erreur sans message"
                continue
            for prefix, pct, phase in MARKERS:
                if head.startswith(prefix):
                    if pct > status.percent:
                        status.percent, status.phase = pct, phase
                    if prefix == "HOLD 2":
                        status.hold2 = True
                    break
            continue
        if status.percent <= 10 and ("iB/" in line or "B/s" in line):
            m = _ARIA.search(line)
            if m:
                download_pct = int(m.group(1))
    if download_pct is not None and status.percent <= 60 and status.percent >= 10:
        # 10 → 60 % pendant le téléchargement
        pct = 10 + download_pct // 2
        if pct > status.percent:
            status.percent = pct
            status.phase = f"Téléchargement de l'ISO Windows… {download_pct} %"
    return status
