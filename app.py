"""Hetzner RDP Manager — lance, sauvegarde et ferme des bureaux Windows à la demande.

Usage :
    python app.py                 compte Hetzner réel (token : HETZNER_TOKEN, .env, Gestionnaire d'identifiants,
                                  sinon saisi dans la barre en haut de la fenêtre)
    python app.py --readonly      lit le vrai compte sans jamais rien modifier
    python app.py --fake          simulation complète, sans appel à Hetzner (temps accéléré)
    python app.py --fake --fake-fail=snapshot,shutdown_timeout   injecte des pannes
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


def resolve_token(creds: rdp.CredentialStore) -> tuple[str | None, str | None]:
    """Token et sa provenance : variable d'environnement, .env, puis Gestionnaire d'identifiants."""
    for source, token in (("env", os.environ.get("HETZNER_TOKEN")),
                          (".env", dotenv_values(HERE / ".env").get("HETZNER_TOKEN"))):
        if (token or "").strip():
            return token.strip(), source
    token = creds.get_token()
    return (token, "keyring") if token else (None, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gestionnaire de bureaux Windows Hetzner")
    parser.add_argument("--fake", action="store_true", help="simulation sans appel à Hetzner")
    parser.add_argument("--fake-speed", type=float, default=8.0, help="accélération du temps en simulation")
    parser.add_argument("--fake-fail", default="", help="pannes simulées, séparées par des virgules")
    parser.add_argument("--fake-seed", default="demo", choices=("demo", "account"),
                        help="données de départ de la simulation")
    parser.add_argument("--readonly", action="store_true", help="lecture seule sur le vrai compte")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("app")

    import customtkinter as ctk

    from rdpm import notify
    from rdpm.controller import AppController
    from rdpm.hetzner.service import HcloudTransport, HetznerService

    if args.fake:
        from rdpm.hetzner.fake import FakeCloud
        rdp.SIMULATE = True
        fake = FakeCloud(speed=args.fake_speed, fail={f for f in args.fake_fail.split(",") if f},
                         seed=args.fake_seed)
        backend = HetznerService(fake, probe_fn=fake.probe, public_ip_fn=fake.public_ip)
        creds: rdp.CredentialStore = rdp.MemoryCredentialStore()
        sim_dir = LOG_DIR.parent / "simulation"
        config = AppConfig.load(sim_dir / "config.json")
        sessions = SessionLog(sim_dir / "sessions.jsonl")
        mode = "fake"
        token_source = None
    else:
        mutex = notify.single_instance(APP_NAME)
        if mutex is None:
            import tkinter.messagebox as mb
            mb.showwarning(APP_NAME, "L'application est déjà ouverte.")
            return 1
        creds = rdp.CredentialStore()
        token, token_source = resolve_token(creds)
        config = AppConfig.load(CONFIG_PATH)
        if token:
            RedactFilter.secrets.append(token)
        # Sans token, la fenêtre s'ouvre verrouillée sur la barre de saisie du token.
        backend = HetznerService(HcloudTransport(token) if token else None, readonly=args.readonly)
        sessions = SessionLog(SESSIONS_PATH)
        mode = "readonly" if args.readonly else "live"

    ctk.set_appearance_mode(config.get("appearance"))
    ctk.set_default_color_theme("blue")
    log.info("Démarrage (mode %s)", mode)

    from rdpm.ui.main_window import MainWindow
    controller = AppController(backend, config, creds, sessions, mode=mode, token_source=token_source,
                               on_secret=RedactFilter.secrets.append)
    window = MainWindow(controller)
    try:
        window.mainloop()
    finally:
        controller.shutdown()
        logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
