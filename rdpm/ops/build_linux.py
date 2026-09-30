"""Construction d'un bureau Linux : XFCE + xrdp compilé avec H.264, depuis une image Ubuntu ou Debian.

Serveur temporaire (image de la distribution, clé SSH éphémère, pare-feu SSH/RDP limité à ton IP) →
`linux-prepare` crée le compte et lance l'installation détachée (unité systemd) → suivi du journal par
jalons → `linux-finalize` : session XFCE de test, empreinte du certificat RDP, nettoyage (la clé SSH
temporaire est retirée) → RDP répond → arrêt ACPI → snapshot étiqueté rdpm-os=linux.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .. import fmt
from ..build import linux
from ..build.linux_scripts import INSTALL_STEPS, LINUX_STATUS, linux_finalize, linux_prepare, render_install_script
from ..constants import L_CERT, L_DISTRO, L_OS, L_XRDP, OS_LINUX
from ..hetzner.errors import UserError
from ..labels import is_valid_label_value
from ..software import catalog as software_catalog
from ..software.progress import parse_app_marks, parse_status
from .base import OpContext
from .build import BaseBuildOp



@dataclass
class LinuxBuildParams:
    distro: str
    username: str
    locale: str
    timezone: str
    server_type: str
    location: str
    allow_cidr: str
    pin: bool = True
    apps: tuple[str, ...] = ()

    @property
    def system_image(self) -> str:
        return linux.DISTROS[self.distro].image

    @property
    def admin_account(self) -> str:
        return self.username


class LinuxBuildOp(BaseBuildOp):
    os_name = OS_LINUX
    title = "Création du bureau Linux"

    def __init__(self, ctx: OpContext, slug: str, name: str, params: LinuxBuildParams) -> None:
        super().__init__(ctx, slug, name, params)
        self.cert: str | None = None
        self.xrdp_version: str | None = None
        self.session_ok: bool | None = None
        self.forced_shutdown = False

    @property
    def distro(self) -> linux.Distro:
        return linux.DISTROS[self.params.distro]

    @property
    def describe(self) -> str:
        loc = linux.LOCALES.get(self.params.locale)
        distro = self.distro.label.split(" (")[0]
        return f"{distro} · XFCE · {loc.label if loc else self.params.locale} · {self.params.server_type} · " \
               f"{self.params.location}"

    # --- points d'extension ----------------------------------------------------------------------
    def _server_label(self) -> str:
        return self.distro.label.split(" (")[0]

    def _install(self) -> None:
        self._prepare()
        self._follow_install()
        self._finalize()

    def _rdp_wait(self) -> tuple[float, float, float]:
        return 600, 0, 60   # xrdp tourne déjà à la fin de l'installation

    def _shutdown_timeout(self) -> float:
        return self._cfg("build_linux_shutdown_timeout_s")

    def _after_shutdown(self, forced: bool) -> None:
        self.forced_shutdown = forced
        if forced:
            self.log("Le système ne s'est pas éteint au signal d'arrêt ACPI : les prochains « Sauvegarder & "
                     "fermer » risquent d'être forcés", "warning")

    def _snapshot_labels(self) -> dict[str, str]:
        labels = super()._snapshot_labels()
        labels.update({L_OS: OS_LINUX, L_DISTRO: self.params.distro})
        if self.xrdp_version and is_valid_label_value(self.xrdp_version):
            labels[L_XRDP] = self.xrdp_version
        if self.cert:
            labels[L_CERT] = self.cert
        return labels

    def _result_extra(self) -> dict:
        p = self.params
        return {"distro": p.distro, "locale": p.locale, "timezone": p.timezone, "session_ok": self.session_ok,
                "xrdp": self.xrdp_version, "forced_shutdown": self.forced_shutdown}

    def _success_message(self, size: float | None) -> str:
        return f"« {self.name} » : bureau Linux créé ({fmt.gb(size)}) — prêt à lancer"

    def _diagnosis_hint(self) -> str:
        return (f"Conservé : SSH root avec la clé {self.key_path} (journal /var/log/rdpm-install.log, "
                f"« journalctl -u rdpm-install »), RDP avec le compte {self.params.username} "
                "(mot de passe enregistré).")

    def _apps(self) -> list[str]:
        return software_catalog.resolve(self.params.apps, OS_LINUX)

    # --- étapes Linux --------------------------------------------------------------------------------
    def _prepare(self) -> None:
        p = self.params
        self.set_phase("Préparation : compte utilisateur, lancement de l'installation…", cancellable=True)
        install = render_install_script(distro=p.distro, username=p.username, locale=p.locale,
                                        timezone=p.timezone, apps=self._apps())
        script = linux_prepare(username=p.username, password=self.password, install_script=install)
        r = self._run(script, self._cfg("build_prepare_timeout_s"), shell="bash")
        if not r.ok or "RDPM_STARTED" not in r.out:
            raise UserError("Préparation du serveur impossible", self._tail(r) or f"code {r.rc}",
                            code="build_prepare_failed")
        self.log(f"Installation lancée sur {self._server_label()} (XFCE, xrdp compilé avec H.264)")

    def _follow_install(self) -> None:
        start = time.monotonic()
        deadline = start + self._cfg("build_linux_timeout_s")
        stopped = unreachable = 0
        last_step = None
        while True:
            r = self._run(LINUX_STATUS, 60, shell="bash")
            elapsed = fmt.clock(time.monotonic() - start)
            if r.ok and "RDPM_MARKS_BEGIN" in r.out:
                unreachable = 0
                status = parse_status(r.out)
                apps = parse_app_marks(status.marks)
                if status.done:
                    self._record_apps(apps)
                    self.set_progress(100, f"Installation terminée · {elapsed}")
                    self.log(f"Installation terminée en {elapsed}")
                    return
                failure = status.failure
                if failure:
                    raise UserError(f"L'installation a échoué : {failure}", self._redact(status.tail[-600:]),
                                    code="build_install_failed")
                step = status.step
                if step:
                    i, total, label = step
                    total = total or INSTALL_STEPS
                    done_part = 0.0
                    if i == total and apps.current:   # étape « Logiciels » : un jalon par logiciel
                        label = f"Logiciels {apps.label}"
                        step = (i, total, label)
                        done_part = (apps.percent() or 0) / 100
                    if step != last_step:
                        self.log(f"Installation {i}/{total} : {label}")
                        last_step = step
                    self.set_progress(int((i - 1 + done_part) / total * 100),
                                      f"Installation {i}/{total} : {label} · {elapsed}")
                else:
                    self.set_phase(f"Installation : démarrage… · {elapsed}", cancellable=True)
                # Unité arrêtée sans « terminé » : tuée (mémoire), arrêt ou redémarrage du serveur.
                stopped = stopped + 1 if status.unit in ("inactive", "failed") else 0
                if stopped >= 2:
                    raise UserError("L'installation s'est arrêtée avant la fin",
                                    self._redact(status.tail[-600:]) or "journal vide",
                                    code="build_install_failed")
            else:
                unreachable += 1
                if unreachable >= 6:
                    srv = self.ctx.backend.get_server(self.server_id)
                    if srv is None:
                        raise UserError("Le serveur de construction a disparu", code="build_server_lost")
                    raise UserError("Le serveur de construction ne répond plus en SSH", self._tail(r),
                                    code="build_ssh_timeout", retryable=True)
            if time.monotonic() > deadline:
                raise UserError("L'installation n'est pas terminée dans les temps",
                                "Serveur trop lent ou dépôts indisponibles ? Relance la création.",
                                code="build_installer_timeout", retryable=True)
            self.sleep(10)

    def _record_apps(self, apps) -> None:
        wanted = self._apps()
        self.apps_ok = [k for k in wanted if k in apps.ok]
        self.apps_failed = [k for k in wanted if k not in apps.ok]
        if self.apps_failed:
            self.log(f"Logiciels non installés : {', '.join(software_catalog.names(self.apps_failed))} (réessaie "
                     "depuis « Logiciels… » une fois le bureau lancé)", "warning")
        elif wanted:
            self.log(f"Logiciels installés : {', '.join(software_catalog.names(self.apps_ok))}")

    def _finalize(self) -> None:
        self.set_phase("Test d'une session XFCE et nettoyage…", 98, cancellable=True)
        r = self._run(linux_finalize(self.params.username, self.admin_pubkey), 300, shell="bash")
        if not r.ok or "RDPM_FINALIZED" not in r.out:
            raise UserError("Finalisation du bureau impossible", self._tail(r) or f"code {r.rc}",
                            code="build_finalize_failed")
        for line in r.out.splitlines():
            key, _, value = line.partition(" ")
            if key == "RDPM_SESSION":
                self.session_ok = value.strip() == "1"
            elif key == "RDPM_CERT" and re.fullmatch(r"[0-9A-Fa-f]{40}", value.strip()):
                self.cert = value.strip().upper()
            elif key == "RDPM_XRDP" and value.strip():
                self.xrdp_version = value.strip()
            elif key == "RDPM_ADMIN":
                self.admin_ok = value.strip() == "1"
        if self.session_ok:
            self.log("Session XFCE de test ouverte avec succès")
        else:
            detail = [line for line in r.out.splitlines() if line.strip() and not line.startswith("RDPM_")]
            self.log("La session XFCE de test n'a pas pu être vérifiée : essaie une connexion après le "
                     f"lancement. {self._redact(' / '.join(detail[-4:]))}", "warning")
