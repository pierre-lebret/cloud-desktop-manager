from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "HetznerRDP"
APP_TITLE = "Hetzner RDP Manager"

APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
LOG_DIR = APP_DIR / "logs"
RDP_DIR = APP_DIR / "rdp"
CONFIG_PATH = APP_DIR / "config.json"
SESSIONS_PATH = APP_DIR / "sessions.jsonl"
STATIC_CACHE_PATH = APP_DIR / "static_cache.json"

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

OP_LABELS = (L_OP, L_OP_TS, L_OP_HOST, L_OP_IMAGE)

ROLE_DESKTOP = "desktop"
ROLE_TMP = "tmp"
ROLE_RDP_FIREWALL = "rdp"

OP_LAUNCHING = "launching"
OP_SAVING = "saving"
OP_CHECKPOINT = "checkpoint"
OP_DISCARDING = "discarding"
OP_DUPLICATING = "duplicating"

RDP_PORT = "3389"
DEFAULT_RDP_USER = "Administrator"
DEFAULT_FIREWALL_NAME = "RDP-WINDOWS"
MAX_FIREWALL_SOURCES = 20

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
}

REFRESH_IDLE_S = 30
REFRESH_BUSY_S = 10
PROBE_BOOTING_S = 5
PROBE_READY_S = 30
PUBLIC_IP_REFRESH_S = 300
STATIC_CACHE_TTL_S = 24 * 3600

VOLUME_MIN_GB = 10
VOLUME_MAX_GB = 10240
