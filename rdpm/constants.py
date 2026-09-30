from __future__ import annotations

import os
from pathlib import Path

# APP_NAME fixe le dossier %APPDATA% et le service du Gestionnaire d'identifiants : ne pas le changer
# (les préférences, mots de passe et clés API existants y sont rangés).
APP_NAME = "cloud-desktop-manager"
APP_TITLE = "Cloud Desktop Manager"

APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
LOG_DIR = APP_DIR / "logs"
RDP_DIR = APP_DIR / "rdp"
CONFIG_PATH = APP_DIR / "config.json"
SESSIONS_PATH = APP_DIR / "sessions.jsonl"
STATIC_CACHE_PATH = APP_DIR / "static_cache.json"
BUILD_DIR = APP_DIR / "build"  # clés SSH éphémères des constructions en cours
# Clé d'administration des bureaux (installation de logiciels depuis l'application) : créée une fois,
# posée sur chaque bureau construit ; le port SSH n'est ouvert, à ton IP, que le temps d'une opération.
ADMIN_KEY_PATH = APP_DIR / "keys" / "admin_ed25519"

KEYRING_SERVICE = APP_NAME
KEYRING_TOKEN_USER = "api-token"

# Labels Hetzner : la source de vérité de l'application.
L_MANAGED = "rdpm"
L_DESKTOP = "rdpm-desktop"
L_ROLE = "rdpm-role"
L_OP = "rdpm-op"
L_OP_TS = "rdpm-op-ts"
L_OP_HOST = "rdpm-op-host"
L_OP_IMAGE = "rdpm-op-image"
L_IMAGE = "rdpm-image"
L_SRC_SERVER = "rdpm-src-server"
L_TYPE = "rdpm-type"
L_LOC = "rdpm-loc"
L_FORCED = "rdpm-forced"
L_OS = "rdpm-os"          # système du bureau (posé sur ses snapshots) ; absent = Windows
L_DISTRO = "rdpm-distro"  # distribution d'un bureau Linux (ubuntu-26.04, debian-13)
L_XRDP = "rdpm-xrdp"      # version d'xrdp compilée dans un bureau Linux
L_CERT = "rdpm-cert"      # empreinte SHA-1 du certificat RDP, préenregistrée auprès de mstsc
OS_LABELS = (L_OS, L_DISTRO, L_XRDP, L_CERT)
L_APPS = "rdpm-apps"      # logiciels du catalogue installés (masque hexadécimal, voir software/catalog.py)
L_ADMIN = "rdpm-admin"    # « 1 » : la clé d'administration de l'application est posée sur le bureau
SOFTWARE_LABELS = (L_APPS, L_ADMIN)

OP_LABELS = (L_OP, L_OP_TS, L_OP_HOST, L_OP_IMAGE)

# Licence d'évaluation Windows Server, suivie par l'application (Windows ne lui est pas accessible) :
# date d'expiration AAAAMMJJ, prolongations restantes, tâche de prolongation automatique installée.
L_EVAL_EXP = "rdpm-eval-exp"
L_EVAL_REARMS = "rdpm-eval-rearms"
L_EVAL_AUTO = "rdpm-eval-auto"
EVAL_LABELS = (L_EVAL_EXP, L_EVAL_REARMS, L_EVAL_AUTO)
# Labels qui suivent un bureau de snapshot en serveur puis en snapshot (lancement, sauvegarde, duplication).
CARRIED_LABELS = EVAL_LABELS + OS_LABELS + SOFTWARE_LABELS
EVAL_PERIOD_DAYS = 180
EVAL_ALERT_DAYS = 15     # alerte sur la carte et seuil de la prolongation automatique

ROLE_DESKTOP = "desktop"
ROLE_TMP = "tmp"
ROLE_RDP_FIREWALL = "rdp"                  # liste d'accès commune à tous les bureaux du projet
ROLE_DESKTOP_FIREWALL = "rdp-desktop"      # liste d'accès propre à un bureau (s'ajoute à la commune)

OS_WINDOWS = "windows"
OS_LINUX = "linux"

OP_LAUNCHING = "launching"
OP_SAVING = "saving"
OP_CHECKPOINT = "checkpoint"
OP_DISCARDING = "discarding"
OP_DUPLICATING = "duplicating"
OP_BUILDING = "building"

RDP_PORT = "3389"
DEFAULT_RDP_USER = "Administrator"
DEFAULT_FIREWALL_NAME = "RDP-WINDOWS"
MAX_FIREWALL_SOURCES = 20   # adresses par liste d'accès

DEFAULT_SETTINGS = {
    "retention": 2,
    "reminder_hours": 3.0,
    "shutdown_timeout_s": 300,
    "snapshot_timeout_s": 3600,
    "boot_probe_timeout_s": 600,
    "quit_force_countdown_s": 60,
    "confirm_save_close": True,
    "auto_connect": True,
    "appearance": "system",
    # Construction d'un Windows de référence (secondes)
    "build_ssh_timeout_s": 300,
    "build_prepare_timeout_s": 900,
    "build_installer_timeout_s": 2400,
    "build_windows_timeout_s": 2700,
    "build_settle_s": 120,
    "build_shutdown_timeout_s": 600,
    "build_linux_timeout_s": 3600,
    "build_linux_shutdown_timeout_s": 180,
    # Logiciels (construction et installation depuis l'application)
    "apps_install_timeout_s": 3600,
    "admin_ssh_timeout_s": 240,
}

REFRESH_IDLE_S = 30
REFRESH_BUSY_S = 10
PROBE_BOOTING_S = 5
PROBE_READY_S = 30
PUBLIC_IP_REFRESH_S = 300
STATIC_CACHE_TTL_S = 24 * 3600

VOLUME_MIN_GB = 10
VOLUME_MAX_GB = 10240
