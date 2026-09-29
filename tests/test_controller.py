"""Contrôleur sans interface : chargement, vues et parcours « Tout sauvegarder et quitter »."""

from __future__ import annotations

import time

from rdpm.config import AppConfig, SessionLog
from rdpm.controller import AppController, QuitState
from rdpm.hetzner.fake import FakeCloud
from rdpm.hetzner.service import HetznerService
from rdpm.rdp import MemoryCredentialStore
from rdpm.state import DState

def make_controller(tmp_path, fail=()):
    fake = FakeCloud(speed=2000.0, fail=set(fail), seed="demo")
    backend = HetznerService(fake, probe_fn=fake.probe, public_ip_fn=fake.public_ip)
    ctrl = AppController(backend, AppConfig(path=tmp_path / "c.json"), MemoryCredentialStore(),
                         SessionLog(tmp_path / "s.jsonl"), mode="fake")
    return fake, ctrl

def spin(ctrl, until, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctrl.tick()
        ctrl.pump()
        result = until()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("délai dépassé")

def test_initial_load_and_views(tmp_path):
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static and ctrl.public_ip)
    views = {v.slug: v for v in ctrl.desktop_views()}
    assert set(views) == {"dev-perso", "trading"}
    assert views["dev-perso"].state == DState.ARCHIVED
    assert "20 Go" in views["dev-perso"].volumes[0]
    spin(ctrl, lambda: ctrl.desktop_views() and
         {v.slug: v for v in ctrl.desktop_views()}["trading"].state == DState.READY)
    keys = {b.key.split(":")[0] for b in ctrl.banners()}
    assert {"mode", "adopt_fw", "import", "dormant"} <= keys
    assert any(item.kind == "ip" for item in ctrl.dormant_items())

def test_save_all_and_quit(tmp_path):
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    qs = QuitState()
    rows_done = spin(ctrl, lambda: ctrl.quit_step(qs)[1])
    assert rows_done
    assert not [s for s in fake.servers.values() if s["labels"].get("rdpm") == "1"]
    trading = ctrl.backend.list_snapshots("trading")
    assert len(trading) == 2

def test_quit_reports_failure_and_keeps_server(tmp_path):
    fake, ctrl = make_controller(tmp_path, fail={"snapshot"})
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    qs = QuitState()

    def failed():
        rows, done = ctrl.quit_step(qs)
        assert not done
        return any(r.status == "failed" for r in rows)
    spin(ctrl, failed)
    assert fake.servers, "le serveur doit rester en place après un snapshot raté"
    qs.skipped.add("trading")
    assert spin(ctrl, lambda: ctrl.quit_step(qs)[1])

class _NoTransport:
    """Backend de contrôleur verrouillé : aucun appel Hetzner ne doit partir tant que le token manque."""

    def __init__(self, fake):
        self.inner = HetznerService(fake, probe_fn=fake.probe, public_ip_fn=fake.public_ip)
        self.transport = None
        self.calls = 0

    def __getattr__(self, name):
        if name in ("fetch_inventory", "fetch_static_raw"):
            self.calls += 1
        return getattr(self.inner, name)

    def set_token(self, token):
        self.transport = self.inner.transport
        self.token = token

    @staticmethod
    def verify_token(token):
        return None


def test_locked_until_token_applied(tmp_path):
    fake = FakeCloud(speed=2000.0, seed="demo")
    backend = _NoTransport(fake)
    secrets = []
    creds = MemoryCredentialStore()
    ctrl = AppController(backend, AppConfig(path=tmp_path / "c.json"), creds, SessionLog(tmp_path / "s.jsonl"),
                         mode="live", on_secret=secrets.append)
    assert ctrl.locked
    spin(ctrl, lambda: ctrl.public_ip)  # l'IP publique ne dépend pas de Hetzner
    assert backend.calls == 0 and ctrl.inventory is None

    ctrl.check_token("x" * 64).result(5)
    ctrl.apply_token("x" * 64, "session", remember=True)
    assert not ctrl.locked and secrets == ["x" * 64]
    assert creds.get_token() == "x" * 64 and ctrl.token_source == "keyring"
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    assert backend.calls >= 2

    ctrl.forget_token()
    assert creds.get_token() is None and ctrl.token_source == "session"


def test_token_change_resets_project_and_drops_stale_refresh(tmp_path):
    from rdpm.events import InventoryLoaded
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    stale = ctrl.inventory
    ctrl.errors["trading"] = object()
    ctrl.backend.set_token = lambda token: None  # le FakeCloud reste le transport
    ctrl.apply_token("y" * 64, "session", remember=False)
    assert ctrl.inventory is None and not ctrl.errors and not ctrl.grouping.desktops
    ctrl.queue.put(InventoryLoaded(stale, None, 0.0, token_gen=0))  # réponse obtenue avec l'ancien token
    ctrl.pump()
    assert ctrl.inventory is None
    spin(ctrl, lambda: ctrl.inventory)
