"""Hetzner RDP Manager — lance, sauvegarde et ferme des bureaux Windows à la demande.

Usage :
    python app.py                 comptes Hetzner réels : la clé HETZNER_TOKEN du .env (toujours chargée) plus
                                  les clés mémorisées dans le Gestionnaire d'identifiants (un projet par clé)
    python app.py --readonly      lit le vrai compte sans jamais rien modifier
    python app.py --fake          simulation complète, sans appel à Hetzner (temps accéléré)
    python app.py --fake --fake-fail=snapshot,shutdown_timeout   injecte des pannes
    python app.py --fake --fake-projects=2                      deux projets simulés
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import dotenv_values

from rdpm import rdp
from rdpm.config import AppConfig, SessionLog
from rdpm.constants import APP_NAME, CONFIG_PATH, LOG_DIR, SESSIONS_PATH

HERE = Path(__file__).resolve().parent


class RedactFilter(logging.Filter):
    """Retire le token et les mots de passe connus des journaux."""

    secrets: list[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            message = record.getMessage()
            for secret in self.secrets:
                if secret and secret in message:
                    message = message.replace(secret, "***")
                    record.msg, record.args = message, ()
        return True


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="hcloud")


def env_token() -> tuple[str | None, str | None]:
    """Clé du fichier .env (prioritaire), sinon de la variable d'environnement HETZNER_TOKEN."""
    for source, token in ((".env", dotenv_values(HERE / ".env").get("HETZNER_TOKEN")),
                          ("env", os.environ.get("HETZNER_TOKEN"))):
        if (token or "").strip():
            return token.strip(), source
    return None, None


def resolve_token(creds: rdp.CredentialStore) -> tuple[str | None, str | None]:
    """Clé principale : .env / variable d'environnement, sinon la première clé mémorisée (scripts, tests)."""
    token, source = env_token()
    if token:
        return token, source
    legacy = creds.get_token()
    return (legacy, "keyring") if legacy else (None, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gestionnaire de bureaux Windows Hetzner")
    parser.add_argument("--fake", action="store_true", help="simulation sans appel à Hetzner")
    parser.add_argument("--fake-speed", type=float, default=8.0, help="accélération du temps en simulation")
    parser.add_argument("--fake-fail", default="", help="pannes simulées, séparées par des virgules")
    parser.add_argument("--fake-seed", default="demo", choices=("demo", "account"),
                        help="données de départ de la simulation")
    parser.add_argument("--fake-projects", type=int, default=1, help="nombre de projets simulés")
    parser.add_argument("--readonly", action="store_true", help="lecture seule sur le vrai compte")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("app")

    import customtkinter as ctk

    from rdpm import notify
    from rdpm.controller import AppController
    from rdpm.hetzner.service import HcloudTransport, HetznerService
    from rdpm.hub import ProjectHub
    from rdpm.projects import ProjectSpec, ProjectStore, ScopedConfig, ScopedCreds, project_id
    from rdpm.remote import SshRemote

    if args.fake:
        from rdpm.hetzner.fake import FakeCloud
        rdp.SIMULATE = True
        creds: rdp.CredentialStore = rdp.MemoryCredentialStore()
        sim_dir = LOG_DIR.parent / "simulation"
        config = AppConfig.load(sim_dir / "config.json")
        sessions = SessionLog(sim_dir / "sessions.jsonl")
        mode = "fake"
        store = None
        fail = {f for f in args.fake_fail.split(",") if f}
        seeds = ["demo", "account"]
        specs = [ProjectSpec(f"sim{i + 1}", "Simulation" if i == 0 else f"Simulation {i + 1}", f"fake-token-{i + 1}",
                             "fake") for i in range(max(1, args.fake_projects))]
        primary = specs[0].id

        def make_backend(spec: ProjectSpec) -> HetznerService:
            seed = args.fake_seed if spec.id == primary else seeds[(int(spec.id[3:]) - 1) % 2] \
                if spec.id.startswith("sim") else "account"
            fake = FakeCloud(speed=args.fake_speed, fail=fail, seed=seed)
            return HetznerService(fake, probe_fn=fake.probe, public_ip_fn=fake.public_ip, remote=fake.remote)

        def verify(token: str) -> None:
            if len(token) < 32:
                raise ValueError("clé trop courte")

        def namespace(pid: str) -> str:
            return "" if pid == primary else f"{pid}:"
    else:
        mutex = notify.single_instance(APP_NAME)
        if mutex is None:
            import tkinter.messagebox as mb
            mb.showwarning(APP_NAME, "L'application est déjà ouverte.")
            return 1
        creds = rdp.CredentialStore()
        config = AppConfig.load(CONFIG_PATH)
        sessions = SessionLog(SESSIONS_PATH)
        mode = "readonly" if args.readonly else "live"
        store = ProjectStore(config, creds)
        # La clé du .env est toujours chargée en premier ; les autres viennent du Gestionnaire d'identifiants.
        specs = store.load(*env_token())
        for spec in specs:
            RedactFilter.secrets.append(spec.token)

        def make_backend(spec: ProjectSpec) -> HetznerService:
            return HetznerService(HcloudTransport(spec.token), readonly=args.readonly, remote=SshRemote())

        verify = HetznerService.verify_token
        namespace = store.namespace

    ctk.set_appearance_mode(config.get("appearance"))
    ctk.set_default_color_theme("blue")
    log.info("Démarrage (mode %s, %d projet(s))", mode, len(specs))

    def make_controller(spec: ProjectSpec) -> AppController:
        if mode == "fake" and spec.source != "fake":   # clé ajoutée en simulation : nouveau projet simulé
            spec.id = project_id(spec.token)
        ns = namespace(spec.id)
        return AppController(make_backend(spec), ScopedConfig(config, ns), ScopedCreds(creds, ns), sessions,
                             mode=mode, project=spec, on_secret=RedactFilter.secrets.append)

    from rdpm.ui.main_window import MainWindow
    hub = ProjectHub(make_controller, verify, mode=mode)
    for spec in specs:
        hub.add(spec)
    window = MainWindow(hub, store, on_secret=RedactFilter.secrets.append)
    try:
        window.mainloop()
    finally:
        hub.shutdown()
        logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
