"""Connexion RDP en 1 clic : mot de passe dans le Gestionnaire d'identifiants Windows.

- Mot de passe du bureau : keyring (service cloud-desktop-manager, compte « desktop:<slug> »).
- Au lancement : identifiant générique TERMSRV/<ip> écrit via CredWriteW (pas de mot de passe
  sur une ligne de commande), lu automatiquement par mstsc.
- À la fermeture du serveur : l'identifiant TERMSRV/<ip> est supprimé, car Hetzner peut réattribuer
  l'IP à quelqu'un d'autre.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .constants import KEYRING_SERVICE, KEYRING_TOKEN_USER, OS_LINUX, OS_WINDOWS, RDP_DIR

log = logging.getLogger(__name__)

# Mode simulation (--fake, tests) : aucun identifiant Windows écrit, mstsc non lancé.
SIMULATE = False

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CREATE_NO_WINDOW = 0x08000000


class CredentialStore:
    """Mots de passe dans le Gestionnaire d'identifiants Windows (keyring)."""

    @staticmethod
    def _user(slug: str) -> str:
        return f"desktop:{slug}"

    def get_password(self, slug: str) -> str | None:
        try:
            return keyring.get_password(KEYRING_SERVICE, self._user(slug))
        except KeyringError as exc:
            log.warning("Lecture du mot de passe impossible : %s", exc)
            return None

    def set_password(self, slug: str, password: str) -> None:
        keyring.set_password(KEYRING_SERVICE, self._user(slug), password)

    def delete_password(self, slug: str) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, self._user(slug))
        except (PasswordDeleteError, KeyringError):
            pass

    def copy_password(self, src: str, dst: str) -> None:
        pw = self.get_password(src)
        if pw:
            self.set_password(dst, pw)

    # Clés API : « api-token » (ancienne clé unique) ou « api-token:<id> » (une par projet).
    @staticmethod
    def _token_user(key_id: str | None) -> str:
        return f"{KEYRING_TOKEN_USER}:{key_id}" if key_id else KEYRING_TOKEN_USER

    def get_token(self, key_id: str | None = None) -> str | None:
        try:
            return keyring.get_password(KEYRING_SERVICE, self._token_user(key_id))
        except KeyringError:
            return None

    def set_token(self, token: str, key_id: str | None = None) -> None:
        keyring.set_password(KEYRING_SERVICE, self._token_user(key_id), token)

    def delete_token(self, key_id: str | None = None) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, self._token_user(key_id))
        except (PasswordDeleteError, KeyringError):
            pass


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _target(host: str) -> str:
    return f"TERMSRV/{host}"


class MemoryCredentialStore(CredentialStore):
    """Variante en mémoire pour le mode simulation et les tests."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get_password(self, slug: str) -> str | None:
        return self._data.get(self._user(slug))

    def set_password(self, slug: str, password: str) -> None:
        self._data[self._user(slug)] = password

    def delete_password(self, slug: str) -> None:
        self._data.pop(self._user(slug), None)

    def get_token(self, key_id: str | None = None) -> str | None:
        return self._data.get(self._token_user(key_id))

    def set_token(self, token: str, key_id: str | None = None) -> None:
        self._data[self._token_user(key_id)] = token

    def delete_token(self, key_id: str | None = None) -> None:
        self._data.pop(self._token_user(key_id), None)


def write_termsrv_cred(host: str, user: str, password: str) -> bool:
    if SIMULATE:
        log.info("[simulation] identifiant TERMSRV/%s écrit", host)
        return True
    if sys.platform != "win32":
        return False
    blob = password.encode("utf-16-le")
    buf = (ctypes.c_ubyte * max(1, len(blob))).from_buffer_copy(blob or b"\0")
    cred = _CREDENTIALW()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = _target(host)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        if advapi32.CredWriteW(ctypes.byref(cred), 0):
            return True
        log.warning("CredWriteW a échoué (code %s), repli sur cmdkey", ctypes.get_last_error())
    except OSError as exc:
        log.warning("CredWriteW indisponible (%s), repli sur cmdkey", exc)
    result = subprocess.run(["cmdkey", f"/generic:{_target(host)}", f"/user:{user}", f"/pass:{password}"],
                            capture_output=True, creationflags=CREATE_NO_WINDOW)
    return result.returncode == 0


def delete_termsrv_cred(host: str) -> bool:
    if SIMULATE:
        log.info("[simulation] identifiant TERMSRV/%s supprimé", host)
        return True
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.advapi32.CredDeleteW(_target(host), CRED_TYPE_GENERIC, 0))
    except OSError:
        result = subprocess.run(["cmdkey", f"/delete:{_target(host)}"], capture_output=True,
                                creationflags=CREATE_NO_WINDOW)
        return result.returncode == 0


def build_rdp_file(host: str, user: str, os_name: str = OS_WINDOWS) -> str:
    linux = os_name == OS_LINUX
    lines = [
        f"full address:s:{host}",
        f"username:s:{user}",
        "prompt for credentials:i:0",
        "authentication level:i:2",
        # xrdp n'a pas d'authentification NLA : sans CredSSP, mstsc transmet l'identifiant enregistré
        # dans la connexion et xrdp ouvre la session directement.
        f"enablecredsspsupport:i:{0 if linux else 1}",
        "screen mode id:i:2",
        "use multimon:i:0",
        "session bpp:i:32",
        "connection type:i:7",
        "networkautodetect:i:1",
        "bandwidthautodetect:i:1",
        "autoreconnection enabled:i:1",
        "redirectclipboard:i:1",
        "audiomode:i:0",
        "displayconnectionbar:i:1",
    ]
    if linux:
        lines.append("dynamic resolution:i:1")   # xrdp redimensionne la session avec la fenêtre
    return "\r\n".join(lines) + "\r\n"


def rdp_file_path(slug: str) -> Path:
    return RDP_DIR / f"{slug}.rdp"


def write_rdp_file(slug: str, host: str, user: str, os_name: str = OS_WINDOWS) -> Path:
    path = rdp_file_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_rdp_file(host, user, os_name), encoding="utf-8")
    return path


# --- certificat du serveur : comme « Ne plus me demander pour ce PC » dans mstsc ------------------------
_SERVERS_KEY = r"Software\Microsoft\Terminal Server Client\Servers"


def trust_certificate(host: str, sha1_hex: str | None, user: str | None = None) -> bool:
    """Préenregistre l'empreinte du certificat RDP du bureau pour cette IP : pas d'alerte à la connexion.
    L'empreinte vient du snapshot (label), relevée pendant la construction."""
    try:
        digest = bytes.fromhex(sha1_hex or "")
    except ValueError:
        return False
    if len(digest) != 20:
        return False
    if SIMULATE:
        log.info("[simulation] certificat RDP approuvé pour %s", host)
        return True
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{_SERVERS_KEY}\\{host}") as key:
            winreg.SetValueEx(key, "CertHash", 0, winreg.REG_BINARY, digest)
            if user:
                winreg.SetValueEx(key, "UsernameHint", 0, winreg.REG_SZ, user)
        return True
    except OSError as exc:
        log.warning("Certificat RDP non préenregistré pour %s : %s", host, exc)
        return False


def forget_certificate(host: str) -> None:
    """Oublie le certificat approuvé pour une IP qui n'est plus à nous (Hetzner peut la réattribuer)."""
    if SIMULATE or sys.platform != "win32":
        return
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{_SERVERS_KEY}\\{host}")
    except OSError:
        pass


def delete_rdp_file(slug: str) -> None:
    rdp_file_path(slug).unlink(missing_ok=True)


def launch_mstsc(path: Path) -> None:
    if SIMULATE:
        log.info("[simulation] mstsc %s", path)
        return
    subprocess.Popen(["mstsc", str(path)], close_fds=True)
