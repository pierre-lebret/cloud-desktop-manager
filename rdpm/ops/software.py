"""Logiciels sur un bureau lancé : installation, mise à jour ou inventaire depuis l'application.

Déroulement : pare-feu **temporaire** (SSH, depuis ton IP publique seulement) appliqué au serveur → connexion
avec la clé d'administration de l'application (root sous Linux ; compte administrateur et PowerShell sous
Windows) → dépôt de l'outil à jour et inventaire → installation détachée (unité systemd / tâche planifiée),
suivie par ses jalons → label `rdpm-apps` mis à jour → pare-feu retiré et supprimé, quoi qu'il arrive.

Les ajouts vivent sur le disque du serveur : « Sauvegarder & fermer » les conserve dans le snapshot.
"""

from __future__ import annotations

import time

from .. import fmt, netutil
from ..constants import ADMIN_KEY_PATH, BUILD_DIR, L_ADMIN, L_APPS, L_MANAGED, L_ROLE, OS_WINDOWS, ROLE_TMP
from ..hetzner.errors import OpCancelled, UserError, api_code
from ..hetzner.waiting import wait_actions
from ..labels import desktop_labels
from ..models import FirewallRule
from ..software import catalog, linux as apps_linux, windows as apps_windows
from ..software.progress import AppsProgress, parse_app_marks, parse_inventory, parse_status
from .base import OpContext, Operation

ADMIN_FW_PREFIX = "rdpm-admin-"
PING = "# rdpm:ping\necho RDPM_PONG\n"   # valide en sh comme en PowerShell


def follow_apps(op: Operation, run_status, *, timeout_s: float, prefix: str = "Logiciels") -> AppsProgress:
    """Suit une installation détachée par ses jalons « RDPM-APP » jusqu'à « RDPM-APPS-DONE »."""
    start = time.monotonic()
    deadline = start + timeout_s
    stopped = unreachable = 0
    last = None
    while True:
        r = run_status()
        elapsed = fmt.clock(time.monotonic() - start)
        if r.ok and "RDPM_MARKS_BEGIN" in r.out:
            unreachable = 0
            status = parse_status(r.out)
            prog = parse_app_marks(status.marks)
            if prog.done:
                return prog
            if prog.current != last:
                last = prog.current
                if prog.current:
                    op.log(f"{prefix} {prog.label}")
            op.set_phase(f"{prefix} {prog.label} · {elapsed}", prog.percent(), cancellable=True)
            stopped = stopped + 1 if status.unit in ("inactive", "failed", "unknown") else 0
            if stopped >= 3:
                prog.failed += [k for k in _pending(prog) if k not in prog.failed]
                op.log(f"L'installation des logiciels s'est arrêtée avant la fin : {status.tail[-300:]}", "warning")
                return prog
        else:
            unreachable += 1
            if unreachable >= 6:
                raise UserError("Le bureau ne répond plus en SSH pendant l'installation des logiciels",
                                (r.err or r.out or "").strip()[-300:], code="apps_ssh_lost", retryable=True)
        if time.monotonic() > deadline:
            raise UserError("L'installation des logiciels n'est pas terminée dans les temps",
                            "Elle continue peut-être sur le bureau : rouvre « Logiciels… » plus tard.",
                            code="apps_timeout", retryable=True)
        op.sleep(10)


def _pending(prog: AppsProgress) -> list[str]:
    return [prog.current[2]] if prog.current and prog.current[2] not in prog.ok else []


class SoftwareOp(Operation):
    """mode : « install » (clés), « update » (tout ce qui est installé) ou « inventory » (lecture seule)."""

    kind = "software"
    title = "Logiciels"
    blocks_desktop = True

    def __init__(self, ctx: OpContext, slug: str, name: str, server_id: int, ip: str, os_name: str,
                 keys: list[str] | tuple[str, ...] = (), mode: str = "install", admin_user: str = "root") -> None:
        super().__init__(ctx, slug, name)
        self.server_id, self.ip, self.os_name, self.mode = server_id, ip, os_name, mode
        self.keys = catalog.resolve(keys, os_name) if mode == "install" else []
        self.user = admin_user if os_name == OS_WINDOWS else "root"
        self.shell = "powershell" if os_name == OS_WINDOWS else "bash"
        self.firewall_id: int | None = None
        self.stamp = int(time.time())
        self.known_hosts = BUILD_DIR / f"{slug}.admin.known_hosts"
        self.installed: set[str] = set()
        self.started_remote = False

    @property
    def windows(self) -> bool:
        return self.os_name == OS_WINDOWS

    # --- utilitaires --------------------------------------------------------------------------------
    def _run(self, script: str, timeout: float):
        return self.ctx.backend.remote.run(self.ip, script, key=ADMIN_KEY_PATH, known_hosts=self.known_hosts,
                                           timeout=timeout, shell=self.shell, user=self.user)

    def _tail(self, r, lines: int = 4) -> str:
        text = (r.err.strip() or r.out.strip()) if r else ""
        return "\n".join(text.splitlines()[-lines:])

    def _cfg(self, key: str) -> float:
        return float(self.ctx.config.get(key))

    # --- déroulement ---------------------------------------------------------------------------------
    def execute(self) -> None:
        remote = self.ctx.backend.remote
        self.set_phase("Vérification des outils…", cancellable=True)
        if remote is None or not remote.available():
            raise UserError("Client OpenSSH introuvable sur ce poste",
                            "Paramètres Windows → Applications → Fonctionnalités facultatives → Client OpenSSH.",
                            code="apps_no_ssh")
        if not ADMIN_KEY_PATH.exists() and not getattr(remote, "simulated", False):
            raise UserError("Clé d'administration absente de ce PC",
                            "Ce bureau a été créé depuis un autre poste (ou avant cette version) : active l'accès "
                            "depuis « Logiciels… ».", code="apps_no_admin")
        password = None
        if self.windows and self.mode != "inventory":
            password = self.ctx.creds.get_password(self.slug)
            if not password:
                raise UserError("Mot de passe Windows inconnu de ce PC",
                                "Renseigne-le dans « Identifiants RDP… » : il sert à lancer l'installation sous "
                                "ton compte.", code="apps_no_password")
            if self.ctx.register_secret:
                self.ctx.register_secret(password)
        try:
            self._open_ssh()
            self._wait_ssh()
            self._inventory()
            if self.mode == "inventory":
                self.success_message = (f"« {self.name} » : {len(self.installed)} logiciel(s) du catalogue "
                                        "installé(s)")
                return
            todo = [k for k in self.keys if k not in self.installed] if self.mode == "install" else []
            if self.mode == "install" and not todo:
                self.success_message = f"« {self.name} » : ces logiciels sont déjà installés"
                self.result = {"ok": [], "failed": [], "installed": sorted(self.installed)}
                return
            prog = self._install(todo, password)
            self._inventory(final=True)
            ok = [k for k in prog.ok if k in todo] if self.mode == "install" else prog.ok
            failed = [k for k in (todo if self.mode == "install" else prog.failed) if k not in self.installed]
            self.result = {"ok": ok, "failed": failed, "installed": sorted(self.installed), "mode": self.mode}
            if self.mode == "update":
                self.success_message = f"« {self.name} » : logiciels mis à jour"
            elif failed:
                self.success_message = (f"« {self.name} » : {len(todo) - len(failed)}/{len(todo)} installé(s) ; "
                                        f"échec : {', '.join(catalog.names(failed))}")
            else:
                self.success_message = f"« {self.name} » : {', '.join(catalog.names(todo))} installé(s)"
            if failed:
                self.log(f"Non installé(s) : {', '.join(catalog.names(failed))} — journal sur le bureau : "
                         + (apps_windows.APPS_LOG if self.windows else apps_linux.APPS_LOG), "warning")
        finally:
            self._close_ssh()

    def _open_ssh(self) -> None:
        backend = self.ctx.backend
        ip = backend.public_ip()
        if not ip:
            raise UserError("IP publique de ce PC inconnue", "Vérifie la connexion internet et réessaie.",
                            code="apps_no_ip", retryable=True)
        cidr = netutil.normalize_cidr(ip)
        self.set_phase(f"Ouverture temporaire de SSH pour {cidr}…", cancellable=True)
        self._remove_leftovers()
        labels = {L_MANAGED: "1", L_ROLE: ROLE_TMP, **desktop_labels(self.slug)}
        labels[L_ROLE] = ROLE_TMP
        fw = backend.create_firewall(f"{ADMIN_FW_PREFIX}{self.slug}-{self.stamp}", labels,
                                     [FirewallRule("in", "tcp", "22", (cidr,), "SSH (logiciels, temporaire)")])
        self.firewall_id = fw.id
        wait_actions(backend, backend.apply_firewall(fw.id, [self.server_id]), self, timeout_s=120)

    def _remove_leftovers(self) -> None:
        """Pare-feu SSH d'une opération interrompue : retiré du serveur et supprimé."""
        backend = self.ctx.backend
        for fw in backend.list_firewalls(f"{L_ROLE}={ROLE_TMP}"):
            if not fw.name.startswith(f"{ADMIN_FW_PREFIX}{self.slug}-"):
                continue
            try:
                if fw.applied_server_ids:
                    wait_actions(backend, backend.remove_firewall(fw.id, list(fw.applied_server_ids)), self,
                                 timeout_s=120)
                backend.delete_firewall(fw.id)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Ancien pare-feu SSH temporaire non supprimé : {exc}", "warning")

    def _wait_ssh(self) -> None:
        self.set_phase("Connexion d'administration au bureau…", cancellable=True)
        deadline = time.monotonic() + self._cfg("admin_ssh_timeout_s")
        while True:
            r = self._run(PING, 30)
            if r.ok and "RDPM_PONG" in r.out:
                return
            refused = "Permission denied" in (r.err or "")
            if refused or time.monotonic() > deadline:
                raise UserError("Accès d'administration refusé par le bureau" if refused else
                                "Le bureau ne répond pas en SSH",
                                "Active l'accès depuis « Logiciels… » (commande à coller une fois dans le bureau)."
                                + (f" Détail : {self._tail(r, 2)}" if self._tail(r, 2) else ""),
                                code="apps_no_admin", retryable=not refused)
            self.sleep(6)

    def _inventory(self, final: bool = False) -> None:
        self.set_phase("Lecture des logiciels installés…", cancellable=not final)
        script = apps_windows.apps_inventory() if self.windows else apps_linux.apps_inventory()
        r = self._run(script, 300)
        found = parse_inventory(r.out) if r.ok else None
        if found is None:
            if final:
                self.log(f"Inventaire des logiciels impossible : {self._tail(r)}", "warning")
                return
            raise UserError("Lecture des logiciels du bureau impossible", self._tail(r) or f"code {r.rc}",
                            code="apps_inventory_failed", retryable=True)
        self.installed = found
        try:
            self.ctx.backend.update_server_labels(self.server_id, {L_APPS: catalog.encode(found) or "0",
                                                                   L_ADMIN: "1"})
        except Exception as exc:  # noqa: BLE001 - l'information sera relue à la prochaine opération
            self.log(f"Label des logiciels non mis à jour : {exc}", "warning")

    def _install(self, todo: list[str], password: str | None) -> AppsProgress:
        what = "Mise à jour des logiciels" if self.mode == "update" else "Installation des logiciels"
        self.set_phase(f"{what} : démarrage…", 0, cancellable=True)
        if self.windows:
            script = apps_windows.apps_start(todo, password or "", self.mode)
        else:
            script = apps_linux.apps_start(todo, self.mode)
        r = self._run(script, 300)
        if r.ok and "RDPM_BUSY" in r.out:
            raise UserError("Une installation de logiciels est déjà en cours sur ce bureau",
                            "Attends sa fin (ou regarde le terminal ouvert par le raccourci « Logiciels »).",
                            code="apps_busy")
        if not r.ok or "RDPM_STARTED" not in r.out:
            raise UserError("Lancement de l'installation impossible",
                            self._tail(r).replace(password, "***") if password else self._tail(r),
                            code="apps_start_failed", retryable=True)
        self.started_remote = True
        if self.mode == "install":
            self.log(f"Installation lancée : {', '.join(catalog.names(todo))}")
        status = apps_windows.APPS_STATUS if self.windows else apps_linux.APPS_STATUS
        prefix = "Mise à jour" if self.mode == "update" else "Logiciels"
        prog = follow_apps(self, lambda: self._run(status, 60), timeout_s=self._cfg("apps_install_timeout_s"),
                           prefix=prefix)
        self.started_remote = False
        return prog

    def _close_ssh(self) -> None:
        """Toujours exécuté : tâche Windows (qui garde le mot de passe) retirée, pare-feu SSH retiré."""
        backend = self.ctx.backend
        self.set_phase("Fermeture de l'accès SSH temporaire…")   # non annulable : rien ne doit rester ouvert
        if self.started_remote:   # annulation pendant l'installation : on l'arrête sur le bureau
            try:
                self._run(apps_windows.APPS_STOP if self.windows else apps_linux.APPS_STOP, 60)
            except Exception:  # noqa: BLE001
                pass
        elif self.windows and self.firewall_id:
            try:
                self._run(apps_windows.APPS_CLEANUP, 60)
            except Exception:  # noqa: BLE001
                pass
        if not self.firewall_id:
            return
        try:
            ids = backend.remove_firewall(self.firewall_id, [self.server_id])
            deadline = time.monotonic() + 120
            wait_actions(backend, ids, _NoCancel(self, deadline), timeout_s=120)
        except Exception as exc:  # noqa: BLE001
            if api_code(exc) not in ("not_found",):
                self.log(f"Pare-feu SSH temporaire non retiré : {exc}", "warning")
        for attempt in range(6):
            try:
                backend.delete_firewall(self.firewall_id)
                self.firewall_id = None
                return
            except Exception as exc:  # noqa: BLE001
                if api_code(exc) not in ("resource_in_use", "conflict", "locked") or attempt == 5:
                    self.log(f"Pare-feu SSH temporaire non supprimé (il n'est plus appliqué) : {exc}", "warning")
                    return
                time.sleep(2)


class _NoCancel:
    """Attente pendant le nettoyage : ignore l'annulation (mais pas la fermeture de l'application)."""

    def __init__(self, op: Operation, deadline: float) -> None:
        self.op, self.deadline = op, deadline

    def check(self) -> None:
        if self.op.ctx.hard_stop.is_set() and time.monotonic() > self.deadline:
            raise OpCancelled("Application fermée")

    def sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, 1.0))
