"""Plusieurs projets Hetzner (une clé API chacun), clé refusée, double lancement, confirmation CONFIRM."""

from __future__ import annotations

from hcloud import APIException

from rdpm.config import AppConfig, SessionLog
from rdpm.controller import AppController
from rdpm.hetzner.fake import FakeCloud
from rdpm.hetzner.service import HetznerService
from rdpm.hub import ProjectHub
from rdpm.ops.launch import LaunchParams
from rdpm.projects import ProjectSpec, ProjectStore, ScopedConfig, ScopedCreds, project_id
from rdpm.rdp import MemoryCredentialStore
from rdpm.state import Act, DState
from rdpm.ui.dialogs.base import is_confirm_word
from tests.test_controller import spin

T1, T2, T3 = "a" * 64, "b" * 64, "c" * 64


# --- clés et cloisonnement ------------------------------------------------------------------------
def test_store_env_first_then_keyring_and_legacy_migration(tmp_path):
    config, creds = AppConfig(path=tmp_path / "c.json"), MemoryCredentialStore()
    creds.set_token(T3)                               # ancienne clé unique mémorisée
    store = ProjectStore(config, creds)
    store.remember(ProjectSpec(project_id(T2), "Client", T2, "session"))
    specs = store.load(T1, ".env")
    assert [(s.token, s.source) for s in specs] == [(T1, ".env"), (T2, "keyring"), (T3, "keyring")]
    assert specs[0].name == "Projet principal" and not specs[0].removable and specs[1].removable
    assert creds.get_token() is None and creds.get_token(project_id(T3)) == T3   # migrée
    assert store.namespace(project_id(T1)) == "" and store.namespace(project_id(T2)) == f"{project_id(T2)}:"
    store.rename(specs[0], "Perso")
    store.forget(project_id(T2))
    again = ProjectStore(config, creds).load(T1, ".env")
    assert [s.name for s in again] == ["Perso", "Projet mémorisé"] and creds.get_token(project_id(T2)) is None


def test_env_key_always_wins_even_if_same_key_is_memorized(tmp_path):
    config, creds = AppConfig(path=tmp_path / "c.json"), MemoryCredentialStore()
    store = ProjectStore(config, creds)
    store.remember(ProjectSpec(project_id(T1), "Mémorisée", T1, "session"))
    specs = store.load(T1, ".env")
    assert len(specs) == 1 and specs[0].source == ".env" and specs[0].name == "Mémorisée"


def test_scoped_config_and_creds_isolate_projects(tmp_path):
    config, creds = AppConfig(path=tmp_path / "c.json"), MemoryCredentialStore()
    main, other = ScopedConfig(config, ""), ScopedConfig(config, "p2:")
    main.update_prefs("win", display_name="Perso")
    other.update_prefs("win", display_name="Client")
    assert main.prefs("win").display_name == "Perso" and other.prefs("win").display_name == "Client"
    assert set(main.desktops) == {"win"} and set(other.desktops) == {"win"}
    main.add_cred("1.1.1.1", "win")
    other.add_cred("2.2.2.2", "win")
    assert main.managed_creds == {"1.1.1.1": "win"} and other.managed_creds == {"2.2.2.2": "win"}
    other.remove_cred("1.1.1.1")                       # jamais les identifiants d'un autre projet
    assert "1.1.1.1" in config.managed_creds
    c1, c2 = ScopedCreds(creds, ""), ScopedCreds(creds, "p2:")
    c1.set_password("win", "one")
    c2.set_password("win", "two")
    assert (c1.get_password("win"), c2.get_password("win")) == ("one", "two")


# --- hub ----------------------------------------------------------------------------------------------
def make_hub(tmp_path, seeds=("demo", "account")):
    config, creds = AppConfig(path=tmp_path / "c.json"), MemoryCredentialStore()
    sessions = SessionLog(tmp_path / "s.jsonl")
    fakes = {}

    def factory(spec: ProjectSpec) -> AppController:
        fake = fakes[spec.id] = FakeCloud(speed=2000.0, seed=seeds[len(fakes) % len(seeds)])
        backend = HetznerService(fake, probe_fn=fake.probe, public_ip_fn=fake.public_ip, remote=fake.remote)
        ns = "" if spec.id == "p1" else f"{spec.id}:"
        return AppController(backend, ScopedConfig(config, ns), ScopedCreds(creds, ns), sessions, mode="fake",
                             project=spec)
    hub = ProjectHub(factory, lambda token: None, mode="fake")
    hub.add(ProjectSpec("p1", "Perso", T1, ".env"))
    hub.add(ProjectSpec("p2", "Client", T2, "keyring"))
    return hub, fakes


def spin_hub(hub, until, timeout=20.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hub.tick()
        hub.pump()
        result = until()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("délai dépassé")


def test_hub_aggregates_projects(tmp_path):
    hub, fakes = make_hub(tmp_path)
    spin_hub(hub, lambda: hub.loaded and all(c.static for c in hub.controllers))
    views = hub.desktop_views()
    assert {v.key for v in views} == {"p1/dev-perso", "p1/trading"}   # le projet « account » n'a pas de bureau
    assert all(v.spec.startswith("Perso · ") for v in views)
    ctrl, slug = hub.split("p1/trading")
    assert ctrl.project.name == "Perso" and slug == "trading"
    banners = hub.banners()
    assert sum(1 for b in banners if b.key.endswith("|mode")) == 1
    assert any(b.key.startswith("p2|") and b.text.startswith("Client — ") for b in banners)
    costs = hub.costs()
    parts = [c.costs() for c in hub.controllers]
    assert abs(costs.idle_month - sum(p.idle_month for p in parts)) < 1e-9
    assert costs.running_count == sum(p.running_count for p in parts)
    assert len(hub.billed_servers()) == 1
    states = hub.new_quit_states()
    rows = spin_hub(hub, lambda: hub.quit_step(states)[1] or None, timeout=40) or hub.quit_step(states)[0]
    assert not [s for f in fakes.values() for s in f.servers.values() if s["labels"].get("rdpm") == "1"]


def test_hub_remove_project(tmp_path):
    hub, _ = make_hub(tmp_path)
    hub.remove("p2")
    assert [c.project.id for c in hub.controllers] == ["p1"] and not hub.multi


# --- clé refusée ------------------------------------------------------------------------------------------
class _Rejecting:
    def request(self, *a, **k):
        raise APIException(code="unauthorized", message="unable to authenticate", details=None)


class _Ui:
    def __init__(self):
        self.invalid = 0

    def on_token_invalid(self):
        self.invalid += 1

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_rejected_key_stops_polling_and_warns_once(tmp_path):
    backend = HetznerService(_Rejecting(), probe_fn=lambda ip: False, public_ip_fn=lambda: "198.51.100.23")
    ctrl = AppController(backend, AppConfig(path=tmp_path / "c.json"), MemoryCredentialStore(),
                         SessionLog(tmp_path / "s.jsonl"), mode="live",
                         project=ProjectSpec("px", "Révoquée", T1, "keyring"))
    ui = ctrl.ui = _Ui()
    spin(ctrl, lambda: ctrl.token_invalid)
    assert ui.invalid == 1 and ctrl.refresh_error.code == "unauthorized"
    ctrl.refresh_error = None
    ctrl.tick()
    ctrl.pump()
    assert ui.invalid == 1 and ctrl.refresh_error is None   # plus aucun appel


# --- double lancement -------------------------------------------------------------------------------------
def test_launch_is_locked_until_the_new_server_is_visible(tmp_path):
    from tests.test_controller import make_controller
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    snap = ctrl.desktop("dev-perso").latest
    params = LaunchParams(snapshot_id=snap.id, snapshot_disk=snap.disk_size, server_type="cpx32", location="nbg1")
    ctrl.launch("dev-perso", "Dev perso", params)
    try:
        ctrl.launch("dev-perso", "Dev perso", params)
        raise AssertionError("deuxième lancement accepté")
    except Exception as exc:  # noqa: BLE001
        assert "déjà lancé" in str(exc)
    spin(ctrl, lambda: not ctrl.runner.active())
    ctrl.inventory = ctrl.inventory   # inventaire pas encore relu : le serveur n'y est pas
    ctrl._regroup()
    view = next(v for v in ctrl.desktop_views() if v.slug == "dev-perso")
    if ctrl.desktop("dev-perso").server is None:
        assert view.state == DState.LAUNCHING and view.primary == Act.LAUNCH and view.primary_disabled
    spin(ctrl, lambda: ctrl.desktop("dev-perso").server is not None)
    assert not ctrl.launch_pending("dev-perso")
    assert len([s for s in fake.servers.values() if s["labels"].get("rdpm-desktop") == "dev-perso"]) == 1


def test_confirm_word_is_case_insensitive():
    assert all(is_confirm_word(w) for w in ("CONFIRM", "confirm", "cOnFirm", "  Confirm "))
    assert not any(is_confirm_word(w) for w in ("", "confirme", "Dev perso", "CONFIRM!"))
