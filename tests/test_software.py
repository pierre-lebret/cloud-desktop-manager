"""Logiciels prêts à l'emploi : catalogue, outils générés, construction avec logiciels, installation plus tard."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import threading
import time

import pytest

from rdpm.build.linux_scripts import render_install_script
from rdpm.build.scripts import render_postinstall_ps1, tag_of
from rdpm.constants import L_ADMIN, L_APPS, OS_LINUX, OS_WINDOWS
from rdpm.ops.build import BuildOp
from rdpm.ops.build_linux import LinuxBuildOp
from rdpm.ops.launch import LaunchOp, LaunchParams
from rdpm.ops.save import SaveOp
from rdpm.ops.software import ADMIN_FW_PREFIX, SoftwareOp
from rdpm.remote import powershell_command, ssh_argv
from rdpm.software import catalog, linux as apps_linux, windows as apps_windows
from rdpm.software.progress import parse_app_marks, parse_inventory
from tests.test_build import params as win_params
from tests.test_build_linux import params as linux_params


# --- catalogue --------------------------------------------------------------------------------------
def test_catalog_keys_bits_and_recipes_are_consistent():
    keys = [a.key for a in catalog.APPS]
    bits = [a.bit for a in catalog.APPS]
    assert len(set(keys)) == len(keys) and len(set(bits)) == len(bits)
    assert max(bits) < 63 * 4   # le masque hexadécimal tient dans un label (63 caractères)
    for app in catalog.APPS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", app.key), app.key
        assert app.category in catalog.CATEGORIES and app.blurb and app.name
        assert set(app.requires) <= set(keys), app.key
        for os_name in (OS_LINUX, OS_WINDOWS):
            recipe = app.recipe(os_name)
            if isinstance(recipe, str):
                assert len(recipe) > 20, (app.key, os_name)   # raison affichée, grisée
            else:
                assert recipe.install.strip() and recipe.check.strip(), (app.key, os_name)
    assert catalog.APPS_BY_KEY["docker"].reason(OS_WINDOWS)
    assert catalog.APPS_BY_KEY["terminal"].reason(OS_LINUX)


def test_install_order_puts_dependencies_first():
    order = catalog.install_order()
    assert sorted(order) == sorted(a.key for a in catalog.APPS)
    for app in catalog.APPS:
        for dep in app.requires:
            assert order.index(dep) < order.index(app.key), (dep, app.key)


def test_resolve_adds_dependencies_and_skips_unavailable():
    assert catalog.resolve(["openclaw"], OS_LINUX) == ["node", "openclaw"]
    assert catalog.resolve(["codex", "docker"], OS_WINDOWS) == ["node", "git", "codex"]
    assert catalog.resolve(["inconnu"], OS_LINUX) == []
    added = catalog.added_by_dependencies(["openclaw", "codex"], OS_LINUX)
    assert added == {"node": ["OpenClaw", "Codex CLI"], "git": ["Codex CLI"]}
    minutes, gb = catalog.estimate(["openclaw"], OS_LINUX)
    assert minutes > 0 and gb > 0
    assert catalog.estimate([], OS_WINDOWS) == (0, 0)
    for os_name in (OS_LINUX, OS_WINDOWS):
        assert catalog.recommended(os_name) and all(catalog.APPS_BY_KEY[k].available(os_name)
                                                    for k in catalog.recommended(os_name))


def test_label_encoding_round_trip():
    keys = {"node", "codex", "chrome", "antigravity"}
    value = catalog.encode(keys)
    assert re.fullmatch(r"[0-9a-f]+", value) and len(value) <= 63
    assert catalog.decode(value) == keys
    assert catalog.encode([]) == "" and catalog.decode("") == set() and catalog.decode("zz") == set()
    assert catalog.decode(catalog.encode(a.key for a in catalog.APPS)) == set(catalog.APPS_BY_KEY)


def test_progress_parsing():
    marks = ["RDPM-APP start 1/3 node Node.js LTS", "RDPM-APP ok node", "RDPM-APP start 2/3 git Git",
             "RDPM-APP fail git (code 1)", "RDPM-APP start 3/3 codex Codex CLI"]
    p = parse_app_marks(marks)
    assert p.current == (3, 3, "codex") and p.ok == ["node"] and p.failed == ["git"] and not p.done
    assert p.label == "3/3 : Codex CLI" and p.percent() == 66
    p = parse_app_marks(marks + ["RDPM-APP ok codex (déjà installé)", "RDPM-APPS-DONE ok=node codex fail=git"])
    assert p.done and p.ok == ["node", "codex"]
    assert parse_inventory("bruit\nRDPM-APPS-STATUS node codex inconnu\n") == {"node", "codex"}
    assert parse_inventory("RDPM-APPS-STATUS \n") == set() and parse_inventory("rien") is None


# --- outils générés ----------------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash absent")
def test_linux_tool_and_scripts_are_valid_bash(tmp_path):
    scripts = {"tool": apps_linux.render_tool(), "start": apps_linux.apps_start(["codex", "openclaw"]),
               "update": apps_linux.apps_start([], "update"), "status": apps_linux.APPS_STATUS,
               "inventory": apps_linux.apps_inventory(),
               "install": render_install_script(distro="ubuntu-26.04", username="pierre", locale="fr",
                                                timezone="Europe/Paris", apps=["codex"])}
    for name, text in scripts.items():
        f = tmp_path / f"{name}.sh"
        f.write_text(text, encoding="utf-8")
        r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, (name, r.stderr)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash absent")
def test_linux_tool_lists_and_expands_without_root(tmp_path):
    """L'outil s'exécute vraiment : liste du catalogue et ajout des dépendances (sans rien installer)."""
    tool = tmp_path / "rdpm-apps"
    text = apps_linux.render_tool().replace(apps_linux.CONF_PATH, str(tmp_path / "absent.conf"))
    tool.write_text(text, encoding="utf-8")
    r = subprocess.run(["bash", str(tool), "list"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    rows = [line.split("|") for line in r.stdout.splitlines()]
    assert [row[0] for row in rows] == [a.key for a in catalog.for_os(OS_LINUX)]   # ordre d'affichage
    assert all(row[4] in ("installé", "-") for row in rows)
    probe = text.replace('case "${1:-}" in', 'if [ "${1:-}" = expand ]; then shift; expand "$@"; exit; fi\ncase "${1:-}" in')
    tool.write_text(probe, encoding="utf-8")
    r = subprocess.run(["bash", str(tool), "expand", "openclaw", "codex"], capture_output=True, text=True, timeout=60)
    assert r.stdout.split() == catalog.resolve(["openclaw", "codex"], OS_LINUX)


def test_generated_tools_define_each_function_once():
    """Une aide qui porterait le nom d'une recette (install_node…) la remplacerait : récursion infinie."""
    bash_defs = re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{", apps_linux.render_tool(), re.M)
    assert len(bash_defs) == len(set(bash_defs)), sorted({d for d in bash_defs if bash_defs.count(d) > 1})
    # Chaque aide appelée par une recette existe (setup_node, npm_global, apt_repo…).
    tool = apps_linux.render_tool()
    for app in catalog.for_os(OS_LINUX):
        for body in (app.linux.install, app.linux.check, app.linux.update):
            for word in re.findall(r"^\s*([a-z_]+)\b", body, re.M):
                if "_" in word or word in ("fetch", "have"):
                    assert re.search(rf"^{word}\(\) \{{", tool, re.M), (app.key, word)
    ps_defs = [d.lower() for d in re.findall(r"^function ([A-Za-z-_]+)", apps_windows.render_tool(), re.M)]
    assert len(ps_defs) == len(set(ps_defs)), sorted({d for d in ps_defs if ps_defs.count(d) > 1})


def test_windows_tool_is_parsable_and_secret_free(tmp_path):
    tool = apps_windows.render_tool()
    start = apps_windows.apps_start(["vscode"], "S3cr'et!")
    assert "S3cr''et!" in start and "S3cr" not in tool
    assert "-Keys vscode" in start and tag_of(start) == "win-apps-start"
    post = render_postinstall_ps1("Romance Standard Time", 15, "ssh-ed25519 AAAA cloud-desktop-manager-admin")
    assert post.isascii() and "administrators_authorized_keys" in post and "PasswordAuthentication no" in post
    assert post.index("OpenSSH") < post.index("rdpm-postinstall.done")
    if shutil.which("pwsh") is None:
        pytest.skip("pwsh absent : analyse syntaxique PowerShell ignorée")
    files = {"tool": tool, "start": start, "status": apps_windows.APPS_STATUS, "post": post,
             "inventory": apps_windows.apps_inventory()}
    for name, text in files.items():
        (tmp_path / f"{name}.ps1").write_text(text, encoding="utf-8")
    check = ("$bad = 0; foreach ($f in Get-ChildItem '" + str(tmp_path) + "' -Filter *.ps1) { $e = $null; "
             "[void][Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$e); "
             "if ($e) { $bad++; Write-Output \"$($f.Name): $e\" } }; exit $bad")
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", check], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_powershell_remote_command_reads_script_from_stdin():
    argv = ssh_argv("203.0.113.5", tmp := __import__("pathlib").Path("k"), tmp, "powershell", user="Administrateur")
    assert argv[-1] == powershell_command() and argv[-2] == "Administrateur@203.0.113.5"
    runner = base64.b64decode(argv[-1].split()[-1]).decode("utf-16-le")
    assert "OpenStandardInput" in runner and "-File $f" in runner and "exit $r" in runner


# --- construction avec logiciels -----------------------------------------------------------------------
def _spy(h, name: str) -> list[str]:
    seen: list[str] = []
    orig = getattr(h.fake.remote, name)

    def spy(srv, b, script):
        seen.append(script)
        return orig(srv, b, script)
    setattr(h.fake.remote, name, spy)
    return seen


def test_linux_build_installs_apps_and_admin_key(harness):
    h = harness()
    finalize = _spy(h, "_do_linux_finalize")
    op = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", linux_params(apps=("openclaw", "vscode"))))
    assert op.outcome == "ok", op.error
    snap = h.backend.get_image(op.image_id)
    assert catalog.decode(snap.labels[L_APPS]) == {"node", "openclaw", "vscode"} and snap.labels[L_ADMIN] == "1"
    assert op.result["apps_ok"] == ["node", "openclaw", "vscode"] and op.result["apps_failed"] == []
    assert h.fake.remote.admin_public_key in finalize[0]


def test_linux_build_survives_a_failed_app(harness):
    h = harness(fail={"apps_one_fails"})
    op = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", linux_params(apps=("codex",))))
    assert op.outcome == "ok", op.error
    assert op.result["apps_failed"] == ["codex"] and "codex" not in catalog.decode(
        h.backend.get_image(op.image_id).labels.get(L_APPS))


def test_windows_build_installs_apps_over_admin_ssh(harness):
    h = harness()
    hooks = _spy(h, "_do_alpine_hook")
    op = h.run(BuildOp(h.ctx, "ref", "Ref", win_params(apps=("vscode", "codex"))))
    assert op.outcome == "ok", op.error
    snap = h.backend.get_image(op.image_id)
    assert catalog.decode(snap.labels[L_APPS]) == {"vscode", "node", "git", "codex"}
    assert snap.labels[L_ADMIN] == "1" and op.result["admin_ok"]
    assert h.fake.remote.admin_public_key in hooks[0]
    start = next(s for t, s in h.fake.remote.scripts if t == "win-apps-start")
    assert op.password in start
    logs = " ".join(getattr(e, "message", "") for e in h.events)
    assert op.password not in logs


def test_windows_build_without_admin_access_still_succeeds(harness):
    h = harness(fail={"admin_ssh_refused"})
    h.config.settings.update(admin_ssh_timeout_s=0.2, build_settle_s=0)
    op = h.run(BuildOp(h.ctx, "ref", "Ref", win_params(apps=("vscode",))))
    assert op.outcome == "ok", op.error
    labels = h.backend.get_image(op.image_id).labels
    assert L_ADMIN not in labels and L_APPS not in labels
    assert op.result["apps_failed"] == ["vscode"] and not op.result["admin_ok"]


# --- installation depuis l'application ------------------------------------------------------------------
def _launch(h, build, user):
    launch = h.run(LaunchOp(h.ctx, build.slug, build.name, LaunchParams(
        snapshot_id=build.image_id, snapshot_disk=80, server_type="cpx22", location="nbg1", rdp_user=user)))
    assert launch.outcome == "ok", launch.error
    return h.backend.get_server(launch.result["server_id"])


def _admin_firewalls(h) -> list[str]:
    return [f["name"] for f in h.fake.firewalls.values() if f["name"].startswith(ADMIN_FW_PREFIX)]


def test_install_later_on_linux_updates_label_and_closes_ssh(harness):
    h = harness()
    build = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", linux_params(apps=("git",))))
    srv = _launch(h, build, "pierre")
    assert catalog.decode(srv.labels[L_APPS]) == {"git"} and srv.labels[L_ADMIN] == "1"
    op = h.run(SoftwareOp(h.ctx, "perso", "Perso", srv.id, srv.ipv4, OS_LINUX, ["codex", "git"]))
    assert op.outcome == "ok", op.error
    assert op.result["ok"] == ["node", "codex"] and op.result["failed"] == []
    srv = h.backend.get_server(srv.id)
    assert catalog.decode(srv.labels[L_APPS]) == {"git", "node", "codex"}
    assert _admin_firewalls(h) == []   # retiré du serveur puis supprimé
    fw = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/firewalls" and b["name"].startswith(
        ADMIN_FW_PREFIX))
    assert [(r["port"], r["source_ips"]) for r in fw["rules"]] == [("22", ["198.51.100.23/32"])]
    tags = [t for t, sid in h.fake.remote.calls if t != "ping" and sid == srv.id]
    assert tags[:2] == ["apps-inventory", "apps-start"] and tags[-1] == "apps-inventory"
    save = h.run(SaveOp(h.ctx, "perso", "Perso", srv.id, close=True))
    assert catalog.decode(h.backend.get_image(save.image_id).labels[L_APPS]) == {"git", "node", "codex"}


def test_install_later_on_windows_uses_password_and_powershell(harness):
    h = harness()
    build = h.run(BuildOp(h.ctx, "ref", "Ref", win_params()))
    srv = _launch(h, build, "Administrateur")
    op = h.run(SoftwareOp(h.ctx, "ref", "Ref", srv.id, srv.ipv4, OS_WINDOWS, ["vscode"],
                          admin_user="Administrateur"))
    assert op.outcome == "ok", op.error
    assert op.result["ok"] == ["vscode"]
    assert {"win-apps-inventory", "win-apps-start", "win-apps-cleanup"} <= {t for t, _ in h.fake.remote.calls}
    assert _admin_firewalls(h) == []
    h.creds.delete_password("ref")
    op = h.run(SoftwareOp(h.ctx, "ref", "Ref", srv.id, srv.ipv4, OS_WINDOWS, ["chrome"], admin_user="Administrateur"))
    assert op.outcome == "failed" and op.error.code == "apps_no_password"


def test_install_later_without_admin_access_explains_and_cleans(harness):
    h = harness()
    build = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", linux_params()))
    srv = _launch(h, build, "pierre")
    h.backend.update_server_labels(srv.id, remove=(L_ADMIN,))
    h.config.settings.update(admin_ssh_timeout_s=0.2)
    op = h.run(SoftwareOp(h.ctx, "perso", "Perso", srv.id, srv.ipv4, OS_LINUX, ["codex"]))
    assert op.outcome == "failed" and op.error.code == "apps_no_admin"
    assert _admin_firewalls(h) == []


def test_install_later_cancel_stops_remote_install_and_cleans(harness):
    h = harness(speed=200.0)
    build = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", linux_params()))
    srv = _launch(h, build, "pierre")
    op = SoftwareOp(h.ctx, "perso", "Perso", srv.id, srv.ipv4, OS_LINUX, ["rust", "go", "java"])
    t = threading.Thread(target=op.run)
    t.start()
    deadline = time.monotonic() + 20
    while not op.phase.startswith("Logiciels") and time.monotonic() < deadline:
        time.sleep(0.005)
    assert op.request_cancel()
    t.join(20)
    assert op.outcome == "cancelled"
    assert "apps-stop" in [tag for tag, _ in h.fake.remote.calls] and _admin_firewalls(h) == []


def test_update_mode_and_stale_firewall_cleanup(harness):
    h = harness()
    build = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", linux_params(apps=("node",))))
    srv = _launch(h, build, "pierre")
    from rdpm.models import FirewallRule
    stale = h.backend.create_firewall(f"{ADMIN_FW_PREFIX}perso-1", {"rdpm": "1", "rdpm-role": "tmp"},
                                      [FirewallRule("in", "tcp", "22", ("198.51.100.23/32",), "vieux")])
    h.backend.apply_firewall(stale.id, [srv.id])
    op = h.run(SoftwareOp(h.ctx, "perso", "Perso", srv.id, srv.ipv4, OS_LINUX, mode="update"))
    assert op.outcome == "ok", op.error
    assert "mis à jour" in op.success_message and _admin_firewalls(h) == []


# --- contrôleur ------------------------------------------------------------------------------------------
def test_controller_software_state_actions_and_stale_firewall_sweep(tmp_path):
    from rdpm.models import FirewallRule
    from rdpm.state import Act, DState
    from tests.test_controller import make_controller, spin
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    dev = ctrl.desktop("dev-perso")
    assert ctrl.installed_apps(dev) >= {"git", "vscode"} and ctrl.admin_access(dev)
    view = next(v for v in ctrl.desktop_views() if v.slug == "dev-perso")
    assert Act.SOFTWARE in view.actions   # archivé : consultation
    trading = ctrl.desktop("trading")
    assert not ctrl.admin_access(trading) and ctrl.installed_apps(trading) == set()
    with pytest.raises(Exception):
        ctrl.software("dev-perso", ["codex"])   # pas lancé
    # Pare-feu SSH resté d'une opération interrompue : retiré et supprimé au rafraîchissement suivant.
    srv = trading.server
    fw = ctrl.backend.create_firewall(f"{ADMIN_FW_PREFIX}trading-1", {"rdpm": "1", "rdpm-role": "tmp",
                                                                      "rdpm-desktop": "trading"},
                                      [FirewallRule("in", "tcp", "22", ("198.51.100.23/32",), "vieux")])
    ctrl.backend.apply_firewall(fw.id, [srv.id])
    ctrl.refresh_now()
    spin(ctrl, lambda: fw.id not in fake.firewalls, timeout=30)
    # Bureau lancé avec accès : installation, état « Logiciels en cours… », connexion toujours possible.
    fake.servers[srv.id]["labels"]["rdpm-admin"] = "1"
    ctrl.refresh_now()
    spin(ctrl, lambda: ctrl.admin_access(ctrl.desktop("trading")))
    ctrl.creds.set_password("trading", "Demo-Pass-123")
    ctrl.software("trading", ["codex"])
    view = spin(ctrl, lambda: next((v for v in ctrl.desktop_views()
                                    if v.slug == "trading" and v.state == DState.INSTALLING), None))
    assert view.primary == Act.CONNECT and Act.SAVE_CLOSE not in view.actions
    spin(ctrl, lambda: not ctrl.runner.for_slug("trading"), timeout=40)
    ctrl.refresh_now()
    spin(ctrl, lambda: "codex" in ctrl.installed_apps(ctrl.desktop("trading")), timeout=20)
