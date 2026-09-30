"""Bureau Linux : catalogue, scripts générés, construction simulée, suivi du système du bureau."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import pytest

from rdpm import rdp
from rdpm.build import linux
from rdpm.build.linux_scripts import (
    LINUX_STATUS, linux_finalize, linux_prepare, render_build_xrdp, render_install_script,
)
from rdpm.build.scripts import tag_of
from rdpm.constants import L_CERT, L_DISTRO, L_OP, L_OS, L_ROLE, L_XRDP, OP_BUILDING, OS_LINUX, ROLE_TMP
from rdpm.hetzner.fake import FAKE_CERT
from rdpm.ops.build_linux import LinuxBuildOp, LinuxBuildParams, parse_status
from rdpm.ops.duplicate import DuplicateOp
from rdpm.ops.launch import LaunchOp, LaunchParams
from rdpm.ops.save import SaveOp


def params(**kw) -> LinuxBuildParams:
    base = dict(distro="ubuntu-26.04", username="pierre", locale="fr", timezone="Europe/Paris",
                server_type="cpx22", location="nbg1", allow_cidr="198.51.100.23/32")
    base.update(kw)
    return LinuxBuildParams(**base)


def leftovers(h) -> dict:
    return {"servers": list(h.fake.servers), "ssh_keys": list(h.fake.ssh_keys),
            "firewalls": [f["name"] for f in h.fake.firewalls.values() if f["name"].startswith("rdpm-build-")]}


# --- catalogue --------------------------------------------------------------------------------------
def test_username_rules():
    assert linux.username_problem("pierre") is None
    assert linux.username_problem("jean-marc_2") is None
    for bad in ("", "Pierre", "2pierre", "pierre dupont", "é", "a" * 33):
        assert linux.username_problem(bad), bad
    for reserved in ("root", "sudo", "xrdp", "systemd-network", "audio"):
        assert "réservé" in linux.username_problem(reserved)
    assert linux.username_problem(linux.default_username()) is None


def test_catalog_is_consistent():
    assert linux.DEFAULT_DISTRO in linux.DISTROS and linux.DEFAULT_LOCALE in linux.LOCALES
    assert all(loc.timezone in linux.TIMEZONES for loc in linux.LOCALES.values())
    assert set(linux.WIN_TZ_TO_IANA.values()) <= set(linux.TIMEZONES)
    for src in (linux.XRDP, linux.XORGXRDP):
        assert src.url.startswith("https://github.com/neutrinolabs/") and len(src.sha256) == 64
    assert len(linux.PIPEWIRE_XRDP.commit) == 40


# --- scripts ------------------------------------------------------------------------------------------
def test_install_script_is_pinned_and_secret_free():
    script = render_install_script(distro="debian-13", username="pierre", locale="de", timezone="Europe/Berlin")
    assert script.startswith("#!/bin/bash\n# rdpm:linux-install")
    build = render_build_xrdp()
    for src in (linux.XRDP, linux.XORGXRDP):
        assert src.sha256 in build and src.url in build
    assert "sha256sum -c" in build and linux.PIPEWIRE_XRDP.commit in build
    assert "--enable-x264" in build and "param=$xorg" in build and "security_layer=tls" in build
    assert "FAMILY='debian'" in script and "LOCALE='de_DE.UTF-8'" in script and "TZ_NAME='Europe/Berlin'" in script
    assert "xfce4-power-manager" not in script.split("PKGS=(")[1].split(")")[0]
    assert "HandlePowerKey=poweroff" in script and linux.MOZILLA_KEY_FINGERPRINT in script
    assert "chpasswd" not in script   # le mot de passe ne passe que par linux-prepare


def test_prepare_carries_password_only_through_stdin_script():
    install = render_install_script(distro="ubuntu-26.04", username="pierre", locale="fr", timezone="Europe/Paris")
    script = linux_prepare(username="pierre", password="Abc-123xyz", install_script=install)
    assert tag_of(script) == "linux-prepare"
    assert "'Abc-123xyz' | chpasswd" in script
    assert "systemd-run --unit=rdpm-install" in script and "mask --runtime" in script
    assert "Abc-123xyz" not in install


@pytest.mark.skipif(not shutil.which("bash"), reason="bash absent")
def test_scripts_are_valid_bash(tmp_path):
    install = render_install_script(distro="ubuntu-26.04", username="pierre", locale="fr", timezone="Europe/Paris")
    scripts = {"install": install, "build": render_build_xrdp(), "status": LINUX_STATUS,
               "finalize": linux_finalize("pierre"),
               "prepare": linux_prepare(username="pierre", password="Abc-123xyz", install_script=install)}
    for name, text in scripts.items():
        path = tmp_path / f"{name}.sh"
        path.write_text(text, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stderr)


def test_parse_status():
    out = ("RDPM_UNIT active success\nRDPM_MARKS_BEGIN\nRDPM-STEP 1/6 Mise à jour du système\n"
           "RDPM-STEP 4/6 Compilation\nRDPM_TAIL_BEGIN\nmake[1]: ...\n")
    st = parse_status(out)
    assert st.unit == "active" and st.step == (4, 6, "Compilation") and not st.done and st.failure is None
    st = parse_status("RDPM_UNIT failed exit-code\nRDPM_MARKS_BEGIN\nRDPM-STEP 4/6 X\nRDPM-FAILED X (code 2)\n"
                      "RDPM_TAIL_BEGIN\nerreur\n")
    assert st.failure == "X (code 2)" and st.tail == "erreur"


def test_rdp_file_for_linux_disables_nla_and_follows_window_size():
    linux_file = rdp.build_rdp_file("203.0.113.5", "pierre", OS_LINUX)
    windows_file = rdp.build_rdp_file("203.0.113.5", "Administrateur")
    assert "enablecredsspsupport:i:0" in linux_file and "dynamic resolution:i:1" in linux_file
    assert "enablecredsspsupport:i:1" in windows_file and "dynamic resolution" not in windows_file


# --- construction simulée -------------------------------------------------------------------------------
def test_linux_build_success(harness):
    h = harness()
    op = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", params()))
    assert op.outcome == "ok", op.error
    snap = h.backend.get_image(op.image_id)
    assert snap.protected and snap.labels[L_OS] == OS_LINUX and snap.labels[L_DISTRO] == "ubuntu-26.04"
    assert snap.labels[L_CERT] == FAKE_CERT and snap.labels[L_XRDP] == "0.10.6.1"
    assert h.creds.get_password("perso") == op.password
    assert h.config.prefs("perso").rdp_user == "pierre"
    assert leftovers(h) == {"servers": [], "ssh_keys": [], "firewalls": []}
    body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/servers")
    assert body["image"] == "ubuntu-26.04" and body["ssh_keys"]
    assert body["labels"][L_ROLE] == ROLE_TMP and body["labels"][L_OP] == OP_BUILDING
    assert body["labels"][L_OS] == OS_LINUX
    fw = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/firewalls")
    assert sorted(r["port"] for r in fw["rules"]) == ["22", "3389"]
    assert [t for t, _ in h.fake.remote.calls if t != "ping"][:1] == ["linux-prepare"]
    assert {"linux-status", "linux-finalize"} <= {t for t, _ in h.fake.remote.calls}
    assert op.followup == "build_done" and op.result["os"] == OS_LINUX and op.result["session_ok"]


def test_linux_build_on_debian_uses_debian_image(harness):
    h = harness()
    op = h.run(LinuxBuildOp(h.ctx, "deb", "Deb", params(distro="debian-13")))
    assert op.outcome == "ok", op.error
    body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/servers")
    assert body["image"] == "debian-13"
    assert h.backend.get_image(op.image_id).labels[L_DISTRO] == "debian-13"


def test_linux_build_password_never_logged(harness):
    h = harness()
    op = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", params()))
    assert op.outcome == "ok"
    logs = " ".join(getattr(e, "message", "") for e in h.events)
    assert op.password not in logs


def test_linux_build_cancel_cleans_everything(harness):
    h = harness()
    op = LinuxBuildOp(h.ctx, "perso", "Perso", params())
    t = threading.Thread(target=op.run)
    t.start()
    deadline = time.monotonic() + 20
    while not op.phase.startswith("Installation") and time.monotonic() < deadline:
        time.sleep(0.005)
    assert op.request_cancel()
    t.join(20)
    assert op.outcome == "cancelled"
    assert leftovers(h) == {"servers": [], "ssh_keys": [], "firewalls": []}
    assert not h.backend.list_snapshots("perso") and h.creds.get_password("perso") is None


def test_linux_build_faults_clean_up(harness):
    cases = {"build_prepare": "build_prepare_failed", "linux_install_error": "build_install_failed",
             "linux_unit_killed": "build_install_failed", "linux_rdp_never": "build_rdp_timeout",
             "build_ssh_timeout": "build_ssh_timeout"}
    for fault, code in cases.items():
        h = harness(fail={fault})
        h.config.settings.update(build_ssh_timeout_s=0.3)
        op = LinuxBuildOp(h.ctx, "perso", "Perso", params())
        op._rdp_wait = lambda: (0.3, 0, 60)
        h.run(op)
        assert op.outcome == "failed" and op.error.code == code, (fault, op.error)
        assert leftovers(h) == {"servers": [], "ssh_keys": [], "firewalls": []}, fault
        assert not h.backend.list_snapshots("perso")
    h = harness(fail={"linux_install_error"})
    op = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", params()))
    assert "Compilation d'xrdp" in op.error.message and "x264" in op.error.hint


# --- le système suit le bureau ------------------------------------------------------------------------
def test_os_label_follows_launch_save_and_duplicate(harness):
    h = harness()
    build = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", params(pin=False)))
    assert build.outcome == "ok", build.error
    launch = h.run(LaunchOp(h.ctx, "perso", "Perso", LaunchParams(
        snapshot_id=build.image_id, snapshot_disk=80, server_type="cpx22", location="nbg1", rdp_user="pierre")))
    assert launch.outcome == "ok", launch.error
    srv = h.backend.get_server(launch.result["server_id"])
    assert srv.labels[L_OS] == OS_LINUX and srv.labels[L_CERT] == FAKE_CERT and launch.os_name == OS_LINUX
    assert "enablecredsspsupport:i:0" in rdp.rdp_file_path("perso").read_text(encoding="utf-8")
    save = h.run(SaveOp(h.ctx, "perso", "Perso", srv.id, close=True))
    assert save.outcome == "ok", save.error
    saved = h.backend.get_image(save.image_id)
    assert saved.labels[L_OS] == OS_LINUX and saved.labels[L_DISTRO] == "ubuntu-26.04"
    dup = h.run(DuplicateOp(h.ctx, "perso", saved, "copie", "Copie"))
    assert dup.outcome == "ok", dup.error
    copy = h.backend.list_snapshots("copie")[0]
    assert copy.labels[L_OS] == OS_LINUX and copy.labels[L_CERT] == FAKE_CERT


def test_controller_linux_build_card(tmp_path):
    from rdpm.state import Act, DState
    from tests.test_controller import make_controller, spin
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    ctrl.build_desktop("Mon Linux", params())
    view = spin(ctrl, lambda: next((v for v in ctrl.desktop_views() if v.slug == "mon-linux"), None))
    assert view.state == DState.BUILDING and view.os == OS_LINUX and "XFCE" in view.spec
    view = spin(ctrl, lambda: next((v for v in ctrl.desktop_views()
                                    if v.slug == "mon-linux" and v.state == DState.ARCHIVED), None), timeout=40)
    assert view.os == OS_LINUX and Act.LAUNCH in view.actions
    assert ctrl.desktop("mon-linux").os == OS_LINUX


def test_linux_volume_is_formatted_ext4(harness):
    from rdpm.ops.resources import AddVolumeOp
    h = harness()
    build = h.run(LinuxBuildOp(h.ctx, "perso", "Perso", params()))
    launch = h.run(LaunchOp(h.ctx, "perso", "Perso", LaunchParams(
        snapshot_id=build.image_id, snapshot_disk=80, server_type="cpx22", location="nbg1", rdp_user="pierre")))
    op = h.run(AddVolumeOp(h.ctx, "perso", "Perso", launch.result["server_id"], 10, "perso-data", OS_LINUX))
    assert op.outcome == "ok", op.error
    body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/volumes")
    assert body["format"] == "ext4" and op.result == {"volume_id": op.result["volume_id"], "os": OS_LINUX}
