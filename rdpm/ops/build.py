"""Construction d'un bureau de référence (Windows ou Linux), sans aucun clic.

`BaseBuildOp` porte le déroulement commun : clé SSH et pare-feu temporaires, serveur, installation
(propre à chaque système), attente de RDP, arrêt, snapshot, identifiants, nettoyage. `BuildOp` construit
Windows (ci-dessous) ; `LinuxBuildOp` (build_linux.py) construit un bureau Linux XFCE + xrdp.

Windows :

Serveur Ubuntu temporaire → reinstall.sh (figé, vérifié) prépare un environnement Alpine qui télécharge
l'ISO Microsoft, injecte les pilotes VirtIO et le fichier de réponses (RDP, mot de passe) → nous y
ajoutons nos réglages de premier démarrage (clavier, fuseau, arrêt ACPI…) → Windows s'installe seul →
dès que RDP répond : arrêt propre, snapshot, suppression du serveur.

Le serveur temporaire porte rdpm-role=tmp et rdpm-op=building : s'il survit à un plantage, la bannière
« serveur temporaire orphelin » et le nettoyage existants s'en chargent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .. import fmt, license
from ..build import catalog
from ..build.reinstall_log import parse_reinstall_log, strip_ansi
from ..build.scripts import (
    ALPINE_STATUS, PING, REBOOT, alpine_hook, alpine_set_image_name, render_locale_xml,
    render_postinstall_ps1, ubuntu_prepare,
)
from ..constants import (
    BUILD_DIR, EVAL_ALERT_DAYS, L_DESKTOP, L_FORCED, L_LOC, L_MANAGED, L_OP, L_OP_HOST, L_OP_TS, L_ROLE, L_SRC_SERVER, L_TYPE,
    L_OS, OS_WINDOWS,
    OP_BUILDING, ROLE_TMP,
)
from ..hetzner.errors import OpCancelled, UserError, api_code
from ..hetzner.waiting import wait_actions
from ..labels import desktop_labels, format_description, tmp_server_name
from ..models import FirewallRule
from .base import OpContext, Operation
from .common import delete_server_with_retry, stop_system, wait_snapshot_ready

BUILD_PREFIX = "rdpm-build-"
ALPINE_USER = "administrator"   # reinstall.sh crée cet utilisateur (clé + sudo) dans l'environnement Alpine


@dataclass
class BuildParams:
    edition: str
    language: str
    iso_url: str
    image_name: str
    keyboard: str
    timezone: str
    admin_account: str
    server_type: str
    location: str
    allow_cidr: str
    pin: bool = True
    system_image: str = catalog.SYSTEM_IMAGE


class BaseBuildOp(Operation):
    """Déroulement commun ; les sous-classes fournissent `_install` et quelques textes."""

    kind = "build"
    os_name = OS_WINDOWS
    title = "Création d'un bureau"

    def __init__(self, ctx: OpContext, slug: str, name: str, params) -> None:
        super().__init__(ctx, slug, name)
        self.params = params
        self.password = catalog.generate_password()
        self.server_id: int | None = None
        self.ip: str | None = None
        self.firewall_id: int | None = None
        self.ssh_key_id: int | None = None
        self.image_id: int | None = None
        self.pubkey = ""
        self.ssh_user = "root"
        self.stamp = int(time.time())   # suffixe unique des ressources temporaires (clé, pare-feu)
        self.key_path = BUILD_DIR / f"{slug}.key"
        self.known_hosts = BUILD_DIR / f"{slug}.known_hosts"
        self.server_kept = False
        self._cleanup_error: Exception | None = None

    @property
    def describe(self) -> str:
        return f"{self.params.server_type} · {self.params.location}"

    # --- points d'extension ----------------------------------------------------------------------
    def _install(self) -> None:
        """Installe le système sur le serveur temporaire ; au retour, RDP doit pouvoir répondre."""
        raise NotImplementedError

    def _server_label(self) -> str:
        """Nom du système de départ du serveur temporaire (phases affichées)."""
        return "Ubuntu"

    def _firewall_rules(self, cidr: str) -> list[FirewallRule]:
        return [FirewallRule("in", "tcp", "22", (cidr,), "SSH (construction)"),
                FirewallRule("in", "tcp", "3389", (cidr,), "RDP (construction)")]

    def _rdp_wait(self) -> tuple[float, float, float]:
        """(délai max, stabilisation après la première réponse, durée typique pour la barre) en secondes."""
        return self._cfg("build_windows_timeout_s"), self._cfg("build_settle_s"), 1200

    def _shutdown_timeout(self) -> float:
        return max(self._cfg("shutdown_timeout_s"), self._cfg("build_shutdown_timeout_s"))

    def _after_shutdown(self, forced: bool) -> None:
        """Réaction à un arrêt forcé (sans conséquence pour un Windows neuf)."""

    def _snapshot_labels(self) -> dict[str, str]:
        return {L_OS: self.os_name}

    def _result_extra(self) -> dict:
        return {}

    def _success_message(self, size: float | None) -> str:
        return f"« {self.name} » : bureau de référence créé ({fmt.gb(size)}) — prêt à lancer"

    def _diagnosis_hint(self) -> str:
        return (f"Conservé : SSH avec la clé {self.key_path}, RDP avec le compte {self.params.admin_account} "
                "(mot de passe enregistré).")

    # --- utilitaires -------------------------------------------------------------------------
    def _cfg(self, key: str) -> float:
        return float(self.ctx.config.get(key))

    def _redact(self, text: str) -> str:
        return strip_ansi(text or "").replace(self.password, "***")

    def _tail(self, result, lines: int = 6) -> str:
        text = (result.err.strip() or result.out.strip()) if result else ""
        return self._redact("\n".join(text.splitlines()[-lines:]))

    def _run(self, script: str, timeout: float, shell: str = "sh"):
        return self.ctx.backend.remote.run(self.ip, script, key=self.key_path, known_hosts=self.known_hosts,
                                           timeout=timeout, shell=shell, user=self.ssh_user)

    def _next_stage(self, user: str) -> None:
        """Les clés d'hôte changent entre Ubuntu, Alpine et Windows : on oublie le known_hosts.
        Sur Ubuntu on est root ; dans l'environnement Alpine, reinstall.sh pose la clé sur « administrator »
        (avec sudo sans mot de passe)."""
        self.ssh_user = user
        try:
            self.known_hosts.unlink()
        except OSError:
            pass

    def _labels(self) -> dict[str, str]:
        return {L_MANAGED: "1", L_ROLE: ROLE_TMP, L_DESKTOP: self.slug}

    # --- déroulement -------------------------------------------------------------------------
    def execute(self) -> None:
        p, backend = self.params, self.ctx.backend
        self._preflight()
        self._create_ssh_key()
        self._create_firewall()
        self._create_server()
        self._wait_ssh(f"Connexion SSH à {self._server_label()}…", self._cfg("build_ssh_timeout_s"))
        self._install()
        self._wait_rdp()
        srv = backend.get_server(self.server_id)
        if srv is None:
            raise UserError("Le serveur de construction a disparu", code="build_server_lost")
        # Un système neuf n'a rien à perdre : arrêt forcé d'office s'il traîne au-delà du délai.
        forced = stop_system(self, srv, force_on_timeout=True, timeout_s=self._shutdown_timeout())
        self._after_shutdown(forced)
        self._snapshot(srv, forced)
        self._store_identity()
        self._cleanup(delete_server=True)
        if self._cleanup_error:
            raise self._cleanup_error
        img = backend.get_image(self.image_id)
        size = img.image_size if img else None
        self.result = {"image_id": self.image_id, "admin_account": p.admin_account, "os": self.os_name,
                       "image_size": size, **self._result_extra()}
        self.followup = "build_done"
        self.success_message = self._success_message(size)

    def _store_identity(self) -> None:
        p = self.params
        self.ctx.creds.set_password(self.slug, self.password)
        self.ctx.config.update_prefs(self.slug, display_name=self.name, rdp_user=p.admin_account,
                                     last_type=p.server_type, last_location=p.location)

    def _preflight(self) -> None:
        self.set_phase("Vérification des outils…", cancellable=True)
        remote = self.ctx.backend.remote
        if remote is None or not remote.available():
            raise UserError("Client OpenSSH introuvable sur ce poste",
                            "Paramètres Windows → Applications → Fonctionnalités facultatives → Client OpenSSH.",
                            code="build_no_ssh")
        if self.ctx.register_secret:
            self.ctx.register_secret(self.password)

    def _create_ssh_key(self) -> None:
        backend = self.ctx.backend
        self.set_phase("Clé SSH temporaire…", cancellable=True)
        for key in backend.list_ssh_keys(f"{L_ROLE}={ROLE_TMP}"):   # restes d'une construction interrompue
            if str(key.get("name", "")).startswith(BUILD_PREFIX):
                try:
                    backend.delete_ssh_key(key["id"])
                except Exception:  # noqa: BLE001
                    pass
        self.pubkey = backend.remote.keypair(self.key_path)
        self.ssh_key_id = backend.create_ssh_key(f"{BUILD_PREFIX}{self.slug}-{self.stamp}", self.pubkey,
                                                 self._labels())

    def _create_firewall(self) -> None:
        backend, cidr = self.ctx.backend, self.params.allow_cidr
        self.set_phase(f"Pare-feu temporaire (SSH et RDP depuis {cidr})…", cancellable=True)
        for fw in backend.list_firewalls(f"{L_ROLE}={ROLE_TMP}"):
            if fw.name.startswith(BUILD_PREFIX) and not fw.applied_server_ids:
                try:
                    backend.delete_firewall(fw.id)
                except Exception:  # noqa: BLE001
                    pass
        rules = self._firewall_rules(cidr)
        self.firewall_id = backend.create_firewall(f"{BUILD_PREFIX}{self.slug}-{self.stamp}", self._labels(),
                                                   rules).id

    def _create_server(self) -> None:
        p, backend = self.params, self.ctx.backend
        label = self._server_label()
        self.set_phase(f"Création du serveur {label} ({p.server_type}, {p.location})…", 0, cancellable=True)
        labels = desktop_labels(self.slug, **{L_ROLE: ROLE_TMP, L_OP: OP_BUILDING, L_OP_TS: int(time.time()),
                                              L_OP_HOST: self.ctx.host, L_TYPE: p.server_type, L_LOC: p.location,
                                              L_OS: self.os_name})
        srv, action_ids = backend.create_server(
            name=tmp_server_name(self.slug), server_type=p.server_type, image_id=p.system_image,
            location=p.location, labels=labels, firewall_ids=[self.firewall_id], ssh_key_ids=[self.ssh_key_id])
        self.server_id, self.ip = srv.id, srv.ipv4
        if not self.ip:
            raise UserError("Le serveur de construction n'a pas d'IPv4", code="build_no_ip")
        self.log(f"Serveur temporaire {srv.name} ({self.ip}) créé")
        wait_actions(backend, action_ids, self, timeout_s=900,
                     on_progress=lambda pct: self.set_progress(pct, f"Création du serveur {label}… {pct} %"))

    def _wait_ssh(self, phase: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        self.set_phase(phase, cancellable=True)
        while True:
            r = self._run(PING, 30)
            if r.ok and "RDPM_PONG" in r.out:
                return
            if time.monotonic() > deadline:
                raise UserError("Le serveur de construction ne répond pas en SSH",
                                f"Pare-feu ou réseau ? Détail : {self._tail(r, 2) or 'aucune réponse'}",
                                code="build_ssh_timeout", retryable=True)
            self.sleep(5)

    def _wait_rdp(self) -> None:
        backend = self.ctx.backend
        timeout, settle_s, typical = self._rdp_wait()
        system = "Windows" if self.os_name == OS_WINDOWS else "Le bureau"
        start = time.monotonic()
        deadline = start + timeout
        last_check = 0.0
        settled = False
        while True:
            if backend.rdp_probe(self.ip):
                if settled or settle_s <= 0:
                    self.log(f"{system} répond en RDP")
                    return
                settled = True
                self.set_phase(f"{system} répond — finalisation des réglages…", 96, cancellable=True)
                self.sleep(settle_s)
                continue
            settled = False
            now = time.monotonic()
            if now - last_check > 60:
                last_check = now
                srv = backend.get_server(self.server_id)
                if srv is None:
                    raise UserError("Le serveur de construction a disparu", code="build_server_lost")
                if srv.status == "off":
                    raise UserError("Le serveur s'est éteint pendant l'installation",
                                    "Consulte la console Hetzner (capture d'écran) pour comprendre.",
                                    code="build_install_failed")
            if now > deadline:
                raise UserError(f"{system} ne répond toujours pas en RDP",
                                "L'installation a peut-être échoué : regarde la console Hetzner du serveur.",
                                code="build_windows_timeout" if self.os_name == OS_WINDOWS else "build_rdp_timeout",
                                retryable=True)
            elapsed = now - start
            phase = "Installation de Windows" if self.os_name == OS_WINDOWS else "Démarrage du bureau distant"
            self.set_phase(f"{phase}… {fmt.clock(elapsed)}", min(95, int(elapsed / typical * 100)),
                           cancellable=True)
            self.sleep(20 if self.os_name == OS_WINDOWS else 5)

    def _snapshot(self, srv, forced: bool) -> None:
        backend, p = self.ctx.backend, self.params
        self.set_phase("Snapshot du bureau de référence…", 0)
        labels = desktop_labels(self.slug, **{L_SRC_SERVER: srv.id, L_TYPE: p.server_type, L_LOC: p.location,
                                              L_FORCED: int(forced)})
        labels.update(self._snapshot_labels())
        image_id, action_id = backend.create_snapshot(srv.id, format_description(self.name, datetime.now(timezone.utc)),
                                                      labels)
        self.image_id = image_id
        self.log(f"Snapshot {image_id} en cours de création")
        try:
            wait_snapshot_ready(self, image_id, action_id)
        except UserError as exc:
            if exc.code in ("snapshot_failed", "snapshot_unverified"):
                self.image_id = None
                raise UserError("Le snapshot de référence a échoué",
                                "Relance la création : le serveur temporaire va être supprimé.",
                                code="build_snapshot_failed", retryable=True) from exc
            raise
        if p.pin:
            try:
                wait_actions(backend, [backend.set_image_protection(image_id, True)], self, timeout_s=60)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Snapshot non épinglé : {exc}", "warning")

    # --- nettoyage ---------------------------------------------------------------------------
    def _cleanup(self, delete_server: bool) -> None:
        backend = self.ctx.backend
        self.set_phase("Nettoyage des ressources temporaires…")  # non annulable : rien ne doit rester
        if delete_server and self.server_id:
            try:
                delete_server_with_retry(self, self.server_id)
                self.server_id = None
            except OpCancelled:
                self.log("Fermeture de l'application : suppression du serveur demandée sans attendre", "warning")
            except Exception as exc:  # noqa: BLE001
                self._cleanup_error = exc
                self.log(f"Serveur temporaire NON supprimé : {exc}", "error")
        if self.firewall_id and self.server_id is None:
            for attempt in range(6):
                try:
                    backend.delete_firewall(self.firewall_id)
                    self.firewall_id = None
                    break
                except Exception as exc:  # noqa: BLE001
                    if api_code(exc) not in ("resource_in_use", "conflict", "locked") or attempt == 5:
                        self.log(f"Pare-feu temporaire non supprimé (gratuit) : {exc}", "warning")
                        break
                    try:
                        self.sleep(5)
                    except OpCancelled:
                        break
        if self.ssh_key_id:
            try:
                backend.delete_ssh_key(self.ssh_key_id)
            except Exception as exc:  # noqa: BLE001
                if api_code(exc) != "not_found":
                    self.log(f"Clé SSH temporaire non supprimée : {exc}", "warning")
            self.ssh_key_id = None
        if not self.server_kept:
            backend.remote.forget_keypair(self.key_path)
            self._next_stage("root")

    def on_failure(self) -> None:
        backend = self.ctx.backend
        keep = False
        if (self.outcome == "failed" and not self.ctx.hard_stop.is_set() and self.server_id
                and backend.get_server(self.server_id)):
            try:
                choice = self.ask(
                    "Serveur de construction",
                    "La création a échoué. Supprimer le serveur temporaire (facturé à l'heure) ou le conserver "
                    f"pour diagnostic ? {self._diagnosis_hint()}",
                    [("delete", "Supprimer (recommandé)", "danger"), ("keep", "Conserver pour diagnostic", "default")],
                    default="delete", countdown_s=120)
                keep = choice == "keep"
            except OpCancelled:
                keep = False
        if keep:
            self.server_kept = True
            self.ctx.creds.set_password(self.slug, self.password)
            self.ctx.config.update_prefs(self.slug, display_name=self.name, rdp_user=self.params.admin_account)
            self.log("Serveur conservé (toujours facturé) : supprime-le depuis « Dormants » quand tu auras fini",
                     "warning")
            self._cleanup(delete_server=False)
        else:
            self._cleanup(delete_server=True)


class BuildOp(BaseBuildOp):
    """Windows de référence : reinstall.sh prépare un environnement Alpine qui installe Windows."""

    os_name = OS_WINDOWS
    title = "Création du Windows de référence"

    def __init__(self, ctx: OpContext, slug: str, name: str, params: BuildParams) -> None:
        super().__init__(ctx, slug, name, params)
        self.image_name = params.image_name
        self._prompts_handled = 0

    @property
    def describe(self) -> str:
        ed = catalog.EDITIONS.get(self.params.edition)
        edition = ed.label.split(" (")[0] if ed and not ed.custom else "ISO personnalisée"
        lang = catalog.LANGS.get(self.params.language)
        return f"{edition} · {lang.label if lang else self.params.language} · {self.params.server_type} · " \
               f"{self.params.location}"

    def _edition(self) -> catalog.Edition | None:
        return catalog.EDITIONS.get(self.params.edition)

    def _eval_rearm_days(self) -> int | None:
        """Seuil de la prolongation automatique, uniquement pour une édition d'évaluation Microsoft."""
        ed = self._edition()
        return EVAL_ALERT_DAYS if ed and ed.eval_days else None

    # --- points d'extension ----------------------------------------------------------------------
    def _preflight(self) -> None:
        super()._preflight()
        self.set_phase("Vérification de l'ISO et des outils…", cancellable=True)
        size = self.ctx.backend.remote.url_size(self.params.iso_url)
        if size is None:
            raise UserError("ISO Windows introuvable à cette adresse",
                            "Vérifie l'URL (Microsoft change parfois les liens) ou utilise une ISO personnalisée.",
                            code="build_iso_missing")
        if size and size < catalog.MIN_ISO_BYTES:
            raise UserError(f"Le fichier à cette adresse ne ressemble pas à une ISO Windows ({fmt.gb(size / 1e9)})",
                            code="build_iso_missing")
        self.log(f"ISO : {self.params.iso_url} ({fmt.gb(size / 1e9) if size else 'taille inconnue'})")

    def _firewall_rules(self, cidr: str) -> list[FirewallRule]:
        rules = super()._firewall_rules(cidr)
        rules.insert(1, FirewallRule("in", "tcp", "80", (cidr,), "Journal de l'installeur (construction)"))
        return rules

    def _install(self) -> None:
        self._prepare_installer()
        self._wait_installer()
        self._customize_image()

    def _snapshot_labels(self) -> dict[str, str]:
        labels = super()._snapshot_labels()
        ed = self._edition()
        if ed and ed.eval_days:   # licence d'évaluation activée pendant la construction
            labels.update(license.to_labels(license.new_license(date.today(), ed.eval_rearms, auto=True,
                                                                period_days=ed.eval_days)))
        return labels

    def _result_extra(self) -> dict:
        p = self.params
        return {"edition": p.edition, "language": p.language, "keyboard": p.keyboard, "timezone": p.timezone}

    def _success_message(self, size: float | None) -> str:
        return f"« {self.name} » : Windows de référence créé ({fmt.gb(size)}) — prêt à lancer"

    def _diagnosis_hint(self) -> str:
        return super()._diagnosis_hint() + f" Journal : http://{self.ip}/."

    # --- étapes Windows --------------------------------------------------------------------------
    def _prepare_installer(self) -> None:
        p = self.params
        self.set_phase("Préparation de l'installeur (reinstall.sh, version vérifiée)…", cancellable=True)
        script = ubuntu_prepare(iso_url=p.iso_url, image_name=self.image_name, password=self.password,
                                pubkey=self.pubkey)
        r = self._run(script, self._cfg("build_prepare_timeout_s"), shell="bash")
        if not r.ok or "RDPM_PREPARED" not in r.out:
            raise UserError("Préparation de l'installeur impossible", self._tail(r) or f"code {r.rc}",
                            code="build_prepare_failed")
        self.log("Installeur préparé, redémarrage vers l'environnement d'installation")
        self._run(REBOOT, 30)
        self._next_stage(ALPINE_USER)

    def _wait_installer(self) -> None:
        deadline = time.monotonic() + self._cfg("build_installer_timeout_s")
        self.set_phase("Redémarrage vers l'environnement d'installation…", cancellable=True)
        stalled, last_len = 0, -1
        while True:
            r = self._run(ALPINE_STATUS, 60)
            if r.ok and "RDPM_LOG_BEGIN" in r.out:
                head, _, log_text = r.out.partition("RDPM_LOG_BEGIN\n")
                running = "RDPM_RUNNING" in head
                status = parse_reinstall_log(log_text)
                self.set_progress(status.percent, f"Installation : {status.phase}")
                if status.hold2:   # bannière imprimée après la fin de trans.sh : l'installeur est prêt
                    return
                # Le journal ne bouge plus ? (pgrep n'est pas fiable : le shell parent survit à trans.sh)
                stalled = stalled + 1 if len(log_text) == last_len else 0
                last_len = len(log_text)
                if status.image_prompt:
                    prompts = strip_ansi(log_text).count("Invalid image name:")
                    if prompts > self._prompts_handled:
                        self._prompts_handled = prompts
                        self._answer_image_prompt(status.image_prompt.requested, status.image_prompt.candidates)
                        stalled, last_len = 0, -1
                elif status.error and stalled >= 2:
                    raise UserError("L'installeur Windows a échoué",
                                    f"{status.error} — journal complet : http://{self.ip}/ tant que le serveur "
                                    "existe.", code="build_install_failed")
                elif not running and stalled >= 4:
                    raise UserError("L'installeur s'est arrêté sans message",
                                    f"Journal : http://{self.ip}/ tant que le serveur existe.",
                                    code="build_install_failed")
            if time.monotonic() > deadline:
                raise UserError("L'environnement d'installation ne répond pas dans les temps",
                                f"Journal : http://{self.ip}/ tant que le serveur existe.",
                                code="build_installer_timeout", retryable=True)
            self.sleep(15 if r.ok else 10)

    def _answer_image_prompt(self, requested: str, candidates: list[str]) -> None:
        # Noms WIM des ISO Server : « … SERVERDATACENTER » (avec bureau) / « … SERVERDATACENTERCORE » (sans).
        year = "".join(ch for ch in requested if ch.isdigit())[:4]
        auto = [c for c in candidates if (not year or year in c) and "datacenter" in c.lower()
                and not c.lower().endswith("core") and "desktop experience" not in c.lower()]
        auto = auto or [c for c in candidates if "desktop experience" in c.lower() and (not year or year in c)]
        if len(auto) == 1:
            choice = auto[0]
            self.log(f"Nom d'image « {requested} » inconnu : « {choice} » choisi automatiquement", "warning")
        else:
            options = [(c, c, "default") for c in candidates[:6]] + [("cancel", "Abandonner", "danger")]
            choice = self.ask("Édition Windows à installer",
                              f"« {requested} » n'existe pas dans cette ISO. Choisis l'édition à installer "
                              "(« Desktop Experience » = avec bureau graphique).", options,
                              default=auto[0] if auto else None)
            if choice == "cancel" or choice not in candidates:
                raise OpCancelled("Construction abandonnée : édition Windows introuvable dans l'ISO")
        self.image_name = choice
        r = self._run(alpine_set_image_name(choice), 30)
        if not r.ok:
            raise UserError("Impossible de transmettre le nom d'image à l'installeur", self._tail(r),
                            code="build_install_failed")

    def _customize_image(self) -> None:
        p = self.params
        self.set_phase("Personnalisation de l'image (clavier, fuseau, réglages)…", 95, cancellable=True)
        script = alpine_hook(self.image_name, render_postinstall_ps1(p.timezone, self._eval_rearm_days()),
                             render_locale_xml(p.keyboard, catalog.default_keyboard(p.language)))
        r = self._run(script, 300)
        if not r.ok or "RDPM_HOOKED" not in r.out:
            raise UserError("Personnalisation de l'image impossible", self._tail(r) or f"code {r.rc}",
                            code="build_hook_failed")
        self.log("Réglages injectés ; Windows s'installe maintenant (10 à 25 min)")
        self._run(REBOOT, 30)
        self._next_stage("root")
