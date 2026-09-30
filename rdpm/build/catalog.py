"""Données du formulaire « Créer un Windows de référence » : éditions, langues, claviers, fuseaux.

L'installation sans surveillance s'appuie sur reinstall.sh (github.com/bin456789/reinstall, GPLv3),
figé à un commit et vérifié par SHA-256 avant exécution.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from dataclasses import dataclass

REINSTALL_COMMIT = "b333811ede8dc87d47785ff1456d339ba3dde122"
REINSTALL_SHA256 = "5349b7416db39fa87caa0bc3df0f3a8e06e9401f4d3e7ad3031def5b4203fea5"
REINSTALL_RAW = f"https://raw.githubusercontent.com/bin456789/reinstall/{REINSTALL_COMMIT}"

SYSTEM_IMAGE = "ubuntu-24.04"
MIN_ISO_BYTES = 1 << 30

_MS = "https://software-static.download.prss.microsoft.com"


@dataclass(frozen=True)
class Edition:
    key: str
    label: str
    iso_template: str | None      # {lang} remplacé par le code de langue ; None = ISO personnalisée
    image_name: str | None
    eval_days: int | None         # licence d'évaluation, None pour une ISO personnalisée
    eval_rearms: int | None = None  # prolongations « slmgr /rearm » disponibles après installation

    @property
    def custom(self) -> bool:
        return self.iso_template is None


EDITIONS: dict[str, Edition] = {
    "server2025": Edition(
        "server2025", "Windows Server 2025 Évaluation (recommandé)",
        _MS + "/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/"
        "26100.1742.240906-0331.ge_release_svc_refresh_SERVER_EVAL_x64FRE_{lang}.iso",
        "Windows Server 2025 SERVERDATACENTER", 180),   # nom WIM (le « Display Name » diffère)
    "server2022": Edition(
        "server2022", "Windows Server 2022 Évaluation",
        _MS + "/sg/download/888969d5-f34g-4e03-ac9d-1f9786c66749/SERVER_EVAL_x64FRE_{lang}.iso",
        "Windows Server 2022 SERVERDATACENTER", 180),
    "custom": Edition("custom", "ISO personnalisée (URL + nom d'image, ta licence)", None, None, None),
}
DEFAULT_EDITION = "server2025"


@dataclass(frozen=True)
class Language:
    code: str
    label: str
    admin_account: str   # nom localisé du compte administrateur intégré
    keyboard: str        # identifiant de disposition par défaut (langue:clavier)
    timezone: str


LANGS: dict[str, Language] = {
    "fr-fr": Language("fr-fr", "Français", "Administrateur", "040c:0000040c", "Romance Standard Time"),
    "en-us": Language("en-us", "English", "Administrator", "0409:00000409", "GMT Standard Time"),
    "de-de": Language("de-de", "Deutsch", "Administrator", "0407:00000407", "W. Europe Standard Time"),
    "es-es": Language("es-es", "Español", "Administrador", "0c0a:0000040a", "Romance Standard Time"),
    "it-it": Language("it-it", "Italiano", "Administrator", "0410:00000410", "W. Europe Standard Time"),
}
DEFAULT_LANG = "fr-fr"

# identifiant → libellé (disposition)
KEYBOARDS: dict[str, str] = {
    "040c:0000040c": "AZERTY — France",
    "080c:0000080c": "AZERTY — Belgique",
    "100c:0000100c": "QWERTZ — Suisse romande",
    "0409:00000409": "QWERTY — États-Unis",
    "0809:00000809": "QWERTY — Royaume-Uni",
    "1009:00011009": "QWERTY — Canada (multilingue)",
    "0407:00000407": "QWERTZ — Allemagne",
    "0807:00000807": "QWERTZ — Suisse alémanique",
    "0c0a:0000040a": "QWERTY — Espagne",
    "0410:00000410": "QWERTY — Italie",
}

TIMEZONES: dict[str, str] = {
    "Romance Standard Time": "Paris, Bruxelles, Madrid (UTC+1)",
    "W. Europe Standard Time": "Berlin, Zurich, Rome, Amsterdam (UTC+1)",
    "GMT Standard Time": "Londres, Dublin, Lisbonne (UTC+0)",
    "Central European Standard Time": "Varsovie, Prague (UTC+1)",
    "FLE Standard Time": "Helsinki, Kiev (UTC+2)",
    "Morocco Standard Time": "Casablanca (UTC+1)",
    "Eastern Standard Time": "New York, Montréal (UTC−5)",
    "Central Standard Time": "Chicago (UTC−6)",
    "Pacific Standard Time": "Los Angeles (UTC−8)",
    "Tokyo Standard Time": "Tokyo (UTC+9)",
    "Singapore Standard Time": "Singapour (UTC+8)",
    "AUS Eastern Standard Time": "Sydney (UTC+10)",
    "UTC": "UTC",
}

_PW_LOWER = "abcdefghijkmnpqrstuvwxyz"
_PW_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_PW_DIGIT = "23456789"
_PW_SPECIAL = "!+-_.="   # jamais ' " $ \\ espace : le mot de passe passe entre quotes dans un script sh


def iso_url(edition: str, lang: str) -> str | None:
    ed = EDITIONS[edition]
    return ed.iso_template.format(lang=lang) if ed.iso_template else None


def admin_account(lang: str) -> str:
    return LANGS.get(lang, LANGS[DEFAULT_LANG]).admin_account


def default_keyboard(lang: str) -> str:
    return LANGS.get(lang, LANGS[DEFAULT_LANG]).keyboard


def default_timezone(lang: str) -> str:
    return LANGS.get(lang, LANGS[DEFAULT_LANG]).timezone


def local_timezone() -> str | None:
    """Fuseau Windows du poste (tzutil /g), le meilleur défaut pour le bureau distant."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["tzutil", "/g"], capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000).stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    return out or None


def generate_password(length: int = 16) -> str:
    """Quatre classes garanties (exigence de complexité Windows), sans caractère spécial shell."""
    pools = (_PW_LOWER, _PW_UPPER, _PW_DIGIT, _PW_SPECIAL)
    chars = [secrets.choice(pool) for pool in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(max(length, 8) - len(chars))]
    # mélange sans biais, puis on évite un spécial en première position (lisibilité)
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    if chars[0] in _PW_SPECIAL:
        k = next(i for i, c in enumerate(chars) if c not in _PW_SPECIAL)
        chars[0], chars[k] = chars[k], chars[0]
    return "".join(chars)


def password_is_safe(password: str) -> bool:
    return bool(password) and all(c.isalnum() or c in _PW_SPECIAL for c in password)
