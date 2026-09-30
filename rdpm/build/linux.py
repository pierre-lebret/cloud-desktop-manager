"""Données du formulaire « Créer un bureau Linux » : distributions, langues, fuseaux, sources figées d'xrdp.

Le bureau est XFCE (léger, sans compositeur : aucun rendu OpenGL logiciel sur un serveur sans GPU) servi par
xrdp compilé depuis les sources officielles avec x264, pour un affichage H.264 (RDP GFX) fluide même en vidéo.
Les dépôts des distributions n'ont que xrdp 0.10.1, sans H.264.
"""

from __future__ import annotations

import getpass
import re
import unicodedata
from dataclasses import dataclass

from . import catalog


@dataclass(frozen=True)
class Distro:
    key: str
    label: str
    image: str              # image système Hetzner
    family: str             # « ubuntu » ou « debian »
    note: str


DISTROS: dict[str, Distro] = {
    "ubuntu-26.04": Distro("ubuntu-26.04", "Ubuntu 26.04 LTS (recommandé)", "ubuntu-26.04", "ubuntu",
                           "Support jusqu'en 2031 · Firefox depuis le dépôt officiel de Mozilla (sans snap)."),
    "debian-13": Distro("debian-13", "Debian 13", "debian-13", "debian",
                        "Plus sobre · support jusqu'en 2028 (LTS 2030) · Firefox ESR."),
}
DEFAULT_DISTRO = "ubuntu-26.04"


@dataclass(frozen=True)
class Locale:
    code: str
    label: str
    locale: str             # LANG
    language: str           # LANGUAGE (ordre de repli des traductions)
    xkb: tuple[str, str]    # disposition de la console (la session suit le clavier du PC Windows)
    timezone: str
    firefox_lang: str | None
    ubuntu_packs: tuple[str, ...]


LOCALES: dict[str, Locale] = {
    "fr": Locale("fr", "Français", "fr_FR.UTF-8", "fr_FR:fr", ("fr", ""), "Europe/Paris", "fr",
                 ("language-pack-fr", "language-pack-gnome-fr")),
    "en": Locale("en", "English", "en_US.UTF-8", "en_US:en", ("us", ""), "Europe/London", None, ()),
    "de": Locale("de", "Deutsch", "de_DE.UTF-8", "de_DE:de", ("de", ""), "Europe/Berlin", "de",
                 ("language-pack-de", "language-pack-gnome-de")),
    "es": Locale("es", "Español", "es_ES.UTF-8", "es_ES:es", ("es", ""), "Europe/Paris", "es-ES",
                 ("language-pack-es", "language-pack-gnome-es")),
    "it": Locale("it", "Italiano", "it_IT.UTF-8", "it_IT:it", ("it", ""), "Europe/Berlin", "it",
                 ("language-pack-it", "language-pack-gnome-it")),
}
DEFAULT_LOCALE = "fr"

# Fuseau IANA → libellé (mêmes villes que le formulaire Windows).
TIMEZONES: dict[str, str] = {
    "Europe/Paris": "Paris, Bruxelles, Madrid (UTC+1)",
    "Europe/Berlin": "Berlin, Zurich, Rome, Amsterdam (UTC+1)",
    "Europe/London": "Londres, Dublin, Lisbonne (UTC+0)",
    "Europe/Warsaw": "Varsovie, Prague (UTC+1)",
    "Europe/Helsinki": "Helsinki, Kiev (UTC+2)",
    "Africa/Casablanca": "Casablanca (UTC+1)",
    "America/Toronto": "New York, Montréal (UTC−5)",
    "America/Chicago": "Chicago (UTC−6)",
    "America/Los_Angeles": "Los Angeles (UTC−8)",
    "Asia/Tokyo": "Tokyo (UTC+9)",
    "Asia/Singapore": "Singapour (UTC+8)",
    "Australia/Sydney": "Sydney (UTC+10)",
    "UTC": "UTC",
}

# Fuseau Windows du poste (tzutil /g) → fuseau IANA équivalent, pour préremplir le formulaire.
WIN_TZ_TO_IANA: dict[str, str] = {
    "Romance Standard Time": "Europe/Paris",
    "W. Europe Standard Time": "Europe/Berlin",
    "GMT Standard Time": "Europe/London",
    "Central European Standard Time": "Europe/Warsaw",
    "Central Europe Standard Time": "Europe/Warsaw",
    "FLE Standard Time": "Europe/Helsinki",
    "Morocco Standard Time": "Africa/Casablanca",
    "Eastern Standard Time": "America/Toronto",
    "Central Standard Time": "America/Chicago",
    "Pacific Standard Time": "America/Los_Angeles",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Singapore Standard Time": "Asia/Singapore",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "UTC": "UTC",
}


# --- sources figées (téléchargées puis vérifiées sur le serveur) -------------------------------------------
@dataclass(frozen=True)
class Source:
    name: str
    version: str
    url: str
    sha256: str | None = None     # archive de release
    commit: str | None = None     # dépôt git (pas d'archive publiée)


XRDP = Source("xrdp", "0.10.6.1",
              "https://github.com/neutrinolabs/xrdp/releases/download/v0.10.6.1/xrdp-0.10.6.1.tar.gz",
              sha256="2f7beb5a3b2529c8d72dc0df9b8cdca31ab0e0c14d1e3421210f5e6ec0ab3b75")
XORGXRDP = Source("xorgxrdp", "0.10.5",
                  "https://github.com/neutrinolabs/xorgxrdp/releases/download/v0.10.5/xorgxrdp-0.10.5.tar.gz",
                  sha256="a5d03435f0ef48bf3d5010e63d9264f2334e7063cba3ecd8d4c0a15616a4f712")
PIPEWIRE_XRDP = Source("pipewire-module-xrdp", "0.2", "https://github.com/neutrinolabs/pipewire-module-xrdp.git",
                       commit="748f562d616590c678d5b06722f3fe5ae9707465")

# Clé de signature du dépôt APT de Mozilla (empreinte publiée par Mozilla).
MOZILLA_KEY_FINGERPRINT = "35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3"

MIN_DISK_GB = 20
RECOMMENDED_RAM_GB = 4


# --- compte utilisateur ------------------------------------------------------------------------------------
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Comptes et groupes système (useradd échoue si un groupe du même nom existe déjà).
RESERVED_USERNAMES = frozenset({
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp", "proxy", "www-data",
    "backup", "list", "irc", "gnats", "nobody", "nogroup", "sshd", "messagebus", "syslog", "xrdp", "polkitd",
    "pipewire", "adm", "tty", "disk", "kmem", "dialout", "fax", "voice", "cdrom", "floppy", "tape", "sudo",
    "audio", "dip", "video", "plugdev", "staff", "games", "users", "input", "render", "netdev", "lxd", "ubuntu",
    "debian", "admin", "operator", "shadow", "utmp", "src", "sasl", "crontab", "ssl-cert", "systemd-journal",
    "_apt", "uuidd", "tcpdump", "tss", "landscape", "fwupd-refresh", "usbmux", "rtkit", "colord", "avahi",
})


def username_problem(name: str) -> str | None:
    """Pourquoi ce nom de compte Linux est refusé, None s'il convient."""
    if not name:
        return "Indique un identifiant."
    if not _USERNAME_RE.match(name):
        return "Minuscules, chiffres, - et _ uniquement, en commençant par une lettre (32 caractères max)."
    if name in RESERVED_USERNAMES or name.startswith("systemd-"):
        return f"« {name} » est réservé par le système : choisis un autre identifiant."
    return None


def default_username() -> str:
    """Identifiant du compte Windows local, nettoyé pour Linux ; « bureau » à défaut."""
    try:
        raw = getpass.getuser()
    except Exception:  # noqa: BLE001
        raw = ""
    ascii_name = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"[^a-z0-9_-]+", "", ascii_name.replace(" ", "-"))
    name = name.lstrip("0123456789-_")[:32]
    return name if name and username_problem(name) is None else "bureau"


def default_timezone(locale_code: str) -> str:
    """Fuseau du poste s'il est connu, sinon celui de la langue."""
    local = WIN_TZ_TO_IANA.get(catalog.local_timezone() or "")
    return local or LOCALES.get(locale_code, LOCALES[DEFAULT_LOCALE]).timezone
