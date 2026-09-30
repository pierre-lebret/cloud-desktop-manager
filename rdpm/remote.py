"""Exécution de scripts sur un serveur via le client OpenSSH de Windows (ssh.exe, sans dépendance).

Le script est envoyé sur l'entrée standard, jamais sur la ligne de commande : ni mot de passe ni
clé ne transitent par la liste des processus. Côté serveur, il est copié dans un fichier temporaire
puis exécuté avec stdin fermé, pour qu'aucune commande du script ne consomme la suite du script.
"""

from __future__ import annotations

import base64
import getpass
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
_SYSTEM_SSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH"


@dataclass(frozen=True)
class RemoteResult:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Remote(Protocol):
    """Ce qu'une construction demande au réseau : clé SSH, taille d'une URL, exécution de scripts."""

    def available(self) -> bool: ...
    def keypair(self, path: Path) -> str: ...
    def admin_keypair(self, path: Path) -> str: ...
    def forget_keypair(self, path: Path) -> None: ...
    def url_size(self, url: str) -> int | None: ...
    def run(self, host: str, script: str, *, key: Path, known_hosts: Path, timeout: float,
            shell: str = "sh", user: str = "root") -> RemoteResult: ...


def ssh_exe(name: str = "ssh") -> str | None:
    candidate = _SYSTEM_SSH / f"{name}.exe"
    if candidate.exists():
        return str(candidate)
    return shutil.which(name)


def ssh_available() -> bool:
    return bool(ssh_exe("ssh") and ssh_exe("ssh-keygen"))


def _flags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


# Windows (OpenSSH, interpréteur cmd.exe) : PowerShell lit le script sur stdin, l'écrit dans un .ps1
# temporaire (UTF-8 avec BOM, lu correctement par PowerShell 5), l'exécute et renvoie son code de sortie.
_PS_RUNNER = (
    "$ErrorActionPreference='Stop';"
    "$ms=New-Object IO.MemoryStream;[Console]::OpenStandardInput().CopyTo($ms);"
    "$f=Join-Path $env:TEMP ('rdpm-'+[guid]::NewGuid().ToString('N')+'.ps1');"
    "[IO.File]::WriteAllText($f,[Text.Encoding]::UTF8.GetString($ms.ToArray()),(New-Object Text.UTF8Encoding $true));"
    "$r=1;try{& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $f;$r=$LASTEXITCODE}"
    "finally{Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue};exit $r"
)


def powershell_command() -> str:
    encoded = base64.b64encode(_PS_RUNNER.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def remote_command(shell: str, user: str = "root") -> str:
    """Commande distante : lit tout le script sur stdin, l'exécute (via sudo si l'utilisateur n'est pas
    root) avec stdin fermé, puis nettoie. `shell="powershell"` : serveur Windows (OpenSSH)."""
    if shell == "powershell":
        return powershell_command()
    runner = f"{shell}" if user == "root" else f"sudo -n {shell}"
    return (f"sh -c 'f=$(mktemp) && cat >\"$f\" && {runner} \"$f\" </dev/null; r=$?; "
            f"rm -f \"$f\"; exit $r'")


def ssh_argv(host: str, key: Path, known_hosts: Path, shell: str, exe: str = "ssh",
             user: str = "root") -> list[str]:
    return [exe, "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", f'UserKnownHostsFile="{known_hosts}"',
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "LogLevel=ERROR", f"{user}@{host}", remote_command(shell, user)]


class SshRemote:
    def available(self) -> bool:
        return ssh_available()

    def keypair(self, path: Path) -> str:
        return generate_keypair(path)

    def admin_keypair(self, path: Path) -> str:
        return ensure_keypair(path, ADMIN_KEY_COMMENT)

    def forget_keypair(self, path: Path) -> None:
        remove_keypair(path)

    def url_size(self, url: str) -> int | None:
        """Taille annoncée par un HEAD (None si l'URL ne répond pas 200)."""
        import requests
        try:
            resp = requests.head(url, allow_redirects=True, timeout=20)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        try:
            return int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            return 0

    def run(self, host: str, script: str, *, key: Path, known_hosts: Path, timeout: float,
            shell: str = "sh", user: str = "root") -> RemoteResult:
        exe = ssh_exe("ssh")
        if not exe:
            return RemoteResult(-2, "", "ssh.exe introuvable")
        argv = ssh_argv(host, key, known_hosts, shell, exe, user)
        try:
            proc = subprocess.run(argv, input=script.encode("utf-8"), capture_output=True, timeout=timeout,
                                  creationflags=_flags())
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode("utf-8", "replace")
            return RemoteResult(-1, out, f"délai dépassé ({timeout:.0f} s)")
        return RemoteResult(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                            proc.stderr.decode("utf-8", "replace"))


ADMIN_KEY_COMMENT = "cloud-desktop-manager-admin"


def ensure_keypair(path: Path, comment: str) -> str:
    """Clé durable (accès d'administration des bureaux) : créée au premier besoin puis réutilisée."""
    pub = path.with_suffix(path.suffix + ".pub")
    if path.exists() and pub.exists():
        text = pub.read_text(encoding="utf-8").strip()
        if text:
            return text
    return generate_keypair(path, comment)


def generate_keypair(path: Path, comment: str = "rdpm-build") -> str:
    """Crée une clé ed25519 sans phrase de passe ; renvoie la clé publique (une ligne)."""
    exe = ssh_exe("ssh-keygen")
    if not exe:
        raise RuntimeError("ssh-keygen.exe introuvable")
    path.parent.mkdir(parents=True, exist_ok=True)
    for p in (path, path.with_suffix(path.suffix + ".pub")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    subprocess.run([exe, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                   check=True, capture_output=True, creationflags=_flags())
    restrict_key_acl(path)
    return path.with_suffix(path.suffix + ".pub").read_text(encoding="utf-8").strip()


def restrict_key_acl(path: Path) -> None:
    """ssh.exe refuse une clé lisible par d'autres comptes : on ne garde que l'utilisateur courant."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
                       capture_output=True, creationflags=_flags(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.warning("ACL de la clé SSH non restreinte : %s", exc)


def remove_keypair(path: Path) -> None:
    for p in (path, path.with_suffix(path.suffix + ".pub")):
        try:
            p.unlink()
        except OSError:
            pass
