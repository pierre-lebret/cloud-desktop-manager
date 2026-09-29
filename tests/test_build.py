"""Création d'un Windows de référence : lecture du journal, scripts générés, et parcours complet simulé."""

from __future__ import annotations

import threading
import time

from rdpm.build import catalog
from rdpm.build.reinstall_log import parse_reinstall_log, strip_ansi
from rdpm.build.scripts import (
    alpine_hook, render_locale_xml, render_postinstall_ps1, tag_of, ubuntu_prepare,
)
from rdpm.constants import L_OP, L_ROLE, OP_BUILDING, ROLE_TMP
from rdpm.hetzner.fake import BUILD_TIMELINE, IMAGE_NAMES
from rdpm.ops.build import BuildOp, BuildParams
from rdpm.remote import ssh_argv

DE = "Windows Server 2025 SERVERDATACENTER"


def params(**kw) -> BuildParams:
    base = dict(edition="server2025", language="fr-fr", iso_url=catalog.iso_url("server2025", "fr-fr"),
                image_name=DE, keyboard="040c:0000040c", timezone="Romance Standard Time",
                admin_account="Administrateur", server_type="cpx22", location="nbg1",
                allow_cidr="198.51.100.23/32")
    base.update(kw)
    return BuildParams(**base)


def leftovers(h) -> dict:
    return {"servers": list(h.fake.servers), "ssh_keys": list(h.fake.ssh_keys),
            "firewalls": [f["name"] for f in h.fake.firewalls.values() if f["name"].startswith("rdpm-build-")]}


# --- unités -------------------------------------------------------------------------------------
def test_parse_reinstall_log_progress_and_hold():
    log = "".join("\n".join(lines) + "\n" for _, lines in BUILD_TIMELINE)
    st = parse_reinstall_log(log)
    assert st.hold2 and st.percent == 95 and st.error is None and st.image_prompt is None
    partial = "".join("\n".join(lines) + "\n" for _, lines in BUILD_TIMELINE[:3])
    st = parse_reinstall_log(partial)
    assert 10 < st.percent < 60 and "45 %" in st.phase and not st.hold2
    assert "\x1b" not in strip_ansi(log)


def test_parse_reinstall_log_error_and_image_prompt():
    log = ("\x1b[32m***** PROCESS WINDOWS ISO *****\x1b[0m\n\x1b[31m***** ERROR *****\x1b[0m\n"
           "\x1b[31mcan't find boot.wim\x1b[0m\n")
    assert parse_reinstall_log(log).error == "can't find boot.wim"
    prompt = ("***** ERROR *****\nInvalid image name: Windows 2025 Foo\n"
              "Choose a correct image name by one of follow command in ssh to continue:\n"
              + "".join(f"  echo '{n}' >/image-name\n" for n in IMAGE_NAMES))
    st = parse_reinstall_log(prompt)
    assert st.error is None and st.image_prompt.requested == "Windows 2025 Foo"
    assert st.image_prompt.candidates == IMAGE_NAMES


def test_generate_password_is_strong_and_shell_safe():
    for _ in range(50):
        pw = catalog.generate_password()
        assert len(pw) == 16 and catalog.password_is_safe(pw)
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw) and any(c in "!+-_.=" for c in pw)
        assert "'" not in pw and '"' not in pw and "$" not in pw


def test_rendered_files():
    xml = render_locale_xml("040c:0000040c", "0409:00000409")
    assert 'Action="add" ID="040c:0000040c" Default="true"' in xml and 'remove" ID="0409:00000409"' in xml
    assert "remove" not in render_locale_xml("040c:0000040c", "040C:0000040C")
    ps1 = render_postinstall_ps1("Romance Standard Time")
    assert 'tzutil.exe /s "Romance Standard Time"' in ps1 and "PBUTTONACTION 3" in ps1
    assert "HiberbootEnabled" in ps1 and "intl.cpl" in ps1 and "Restart-Computer" not in ps1
    assert "shutdown.exe" not in ps1.lower() and "stop-computer" not in ps1.lower() and ps1.isascii()
    assert "shutdownwithoutlogon" in ps1   # sinon Windows Server ignore l'arrêt ACPI à l'écran de verrouillage
    script = ubuntu_prepare(iso_url="https://x/y.iso", image_name=DE, password="Ab1!cd'e", pubkey="ssh-ed25519 AAAA")
    assert tag_of(script) == "ubuntu-prepare" and catalog.REINSTALL_COMMIT in script
    assert catalog.REINSTALL_SHA256 in script and "--hold 2" in script and "configs/ssh_keys" in script
    assert "--ssh-key" not in script and "'Ab1!cd'\"'\"'e'" in script
    assert "--username administrator" in script   # sinon reinstall.sh pose la question et échoue sans terminal
    hook = alpine_hook(DE, ps1, xml)
    assert "wimmountrw" in hook and "SetupComplete.cmd" in hook and DE.lower() in hook
    assert "rdpm-postinstall.ps1" in hook and "wimunmount --commit" in hook


def test_ssh_argv_is_non_interactive(tmp_path):
    argv = ssh_argv("203.0.113.5", tmp_path / "k", tmp_path / "kh", "bash", exe="ssh")
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined and "PasswordAuthentication=no" in joined and "root@203.0.113.5" in argv
    assert argv[-1].startswith("sh -c") and "bash" in argv[-1] and "</dev/null" in argv[-1] and "sudo" not in argv[-1]
    adm = ssh_argv("203.0.113.5", tmp_path / "k", tmp_path / "kh", "sh", exe="ssh", user="administrator")
    assert "administrator@203.0.113.5" in adm and "sudo -n sh" in adm[-1]


# --- parcours simulé ------------------------------------------------------------------------------
def test_build_success(harness):
    h = harness()
    op = h.run(BuildOp(h.ctx, "ref", "Windows de référence", params()))
    assert op.outcome == "ok", op.error
    snaps = h.backend.list_snapshots("ref")
    assert len(snaps) == 1 and snaps[0].protected and snaps[0].disk_size == 80
    assert snaps[0].labels["rdpm-type"] == "cpx22" and snaps[0].display_name == "Windows de référence"
    assert leftovers(h) == {"servers": [], "ssh_keys": [], "firewalls": []}
    assert h.creds.get_password("ref") == op.password and len(op.password) == 16
    prefs = h.config.prefs("ref")
    assert prefs.rdp_user == "Administrateur" and prefs.last_type == "cpx22" and prefs.display_name == "Windows de référence"
    methods = [(m, u.split("/")[1]) for m, u, _ in h.fake.requests if m in ("POST", "DELETE")]
    assert methods.index(("POST", "ssh_keys")) < methods.index(("POST", "servers"))
    assert methods.index(("DELETE", "servers")) < methods.index(("DELETE", "firewalls"))
    body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/servers")
    assert body["image"] == "ubuntu-24.04" and body["ssh_keys"] and body["labels"][L_OP] == OP_BUILDING
    assert body["labels"][L_ROLE] == ROLE_TMP
    fw_body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/firewalls")
    assert {r["port"] for r in fw_body["rules"]} == {"22", "80", "3389"}
    assert all(r["source_ips"] == ["198.51.100.23/32"] for r in fw_body["rules"])
    tags = [t for t, _ in h.fake.remote.calls]
    assert tags[:3] == ["ping", "ubuntu-prepare", "reboot"] and tags[-2:] == ["alpine-hook", "reboot"]
    assert op.followup == "build_done" and op.result["admin_account"] == "Administrateur"


def test_build_records_password_and_hook_in_fake(harness):
    h = harness()
    seen = {}
    orig = h.fake.remote._do_alpine_hook

    def spy(srv, b, script):
        seen["image_name"], seen["password"], seen["hook"] = b["image_name"], b["password"], script
        return orig(srv, b, script)
    h.fake.remote._do_alpine_hook = spy
    op = h.run(BuildOp(h.ctx, "ref", "Ref", params()))
    assert op.outcome == "ok", op.error
    assert seen["image_name"] == DE and seen["password"] == op.password
    assert "rdpm-postinstall.ps1" in seen["hook"] and 'ID="040c:0000040c"' in seen["hook"]
    assert 'tzutil.exe /s "Romance Standard Time"' in seen["hook"]


def test_build_cancel_during_installer_cleans_everything(harness):
    h = harness()
    op = BuildOp(h.ctx, "ref", "Ref", params())
    t = threading.Thread(target=op.run)
    t.start()
    deadline = time.monotonic() + 20
    while not op.phase.startswith("Installation :") and time.monotonic() < deadline:
        time.sleep(0.005)
    assert op.request_cancel()
    t.join(20)
    assert op.outcome == "cancelled"
    assert leftovers(h) == {"servers": [], "ssh_keys": [], "firewalls": []}
    assert not h.backend.list_snapshots("ref") and h.creds.get_password("ref") is None


def test_build_faults_clean_up(harness):
    cases = {"build_iso_missing": "build_iso_missing", "build_ssh_timeout": "build_ssh_timeout",
             "build_prepare": "build_prepare_failed", "build_install_error": "build_install_failed",
             "build_rdp_never": "build_windows_timeout"}
    for fault, code in cases.items():
        h = harness(fail={fault})
        h.config.settings.update(build_ssh_timeout_s=0.3, build_windows_timeout_s=0.3)
        op = h.run(BuildOp(h.ctx, "ref", "Ref", params()))
        assert op.outcome == "failed" and op.error.code == code, (fault, op.error)
        assert leftovers(h) == {"servers": [], "ssh_keys": [], "firewalls": []}, fault
        assert not h.backend.list_snapshots("ref")
    h = harness(fail={"build_iso_missing"})
    h.run(BuildOp(h.ctx, "ref", "Ref", params()))
    assert not [r for r in h.fake.requests if r[0] == "POST"]


def test_build_image_prompt_is_answered_automatically(harness):
    h = harness(fail={"build_image_name"})
    op = h.run(BuildOp(h.ctx, "ref", "Ref", params(image_name="Windows Server 2025 Foo")))
    assert op.outcome == "ok", op.error
    assert op.image_name == "Windows Server 2025 SERVERDATACENTER"
    assert any(t == "alpine-set-image-name" for t, _ in h.fake.remote.calls)
    assert not [e for e in h.events if type(e).__name__ == "NeedDecision"]


def test_build_image_prompt_asks_when_ambiguous(harness):
    h = harness()
    h.decisions = ["Windows 11 Pro"]
    op = BuildOp(h.ctx, "ref", "Ref", params(image_name="Windows 11 Foo"))
    op.ip = "203.0.113.10"
    op._run = lambda script, timeout, shell="sh": type("R", (), {"ok": True, "out": "", "err": ""})()
    op._answer_image_prompt("Windows 11 Foo", ["Windows 11 Home", "Windows 11 Pro", "Windows 11 Education"])
    assert op.image_name == "Windows 11 Pro"
    assert [e for e in h.events if type(e).__name__ == "NeedDecision"]


def test_ssh_users_per_stage(harness):
    h = harness()
    op = h.run(BuildOp(h.ctx, "ref", "Ref", params()))
    assert op.outcome == "ok", op.error
    assert op.ssh_user == "root"


def test_build_image_prompt_auto_pick(harness):
    h = harness()
    op = BuildOp(h.ctx, "ref", "Ref", params(image_name="Windows Server 2022 Foo"))
    op.ip = "203.0.113.10"
    calls = []
    op._run = lambda script, timeout, shell="sh": calls.append(script) or type("R", (), {"ok": True, "out": "", "err": ""})()
    op._answer_image_prompt("Windows Server 2022 Foo", ["Windows Server 2022 Standard Evaluation",
                                                        "Windows Server 2022 Datacenter Evaluation (Desktop Experience)"])
    assert op.image_name == "Windows Server 2022 Datacenter Evaluation (Desktop Experience)"
    assert calls and "/image-name" in calls[0]
    assert not [e for e in h.events if type(e).__name__ == "NeedDecision"]


def test_build_snapshot_failure_deletes_server(harness):
    h = harness(fail={"snapshot"})
    op = h.run(BuildOp(h.ctx, "ref", "Ref", params()))
    assert op.outcome == "failed" and op.error.code == "build_snapshot_failed"
    assert leftovers(h) == {"servers": [], "ssh_keys": [], "firewalls": []}
    assert not [i for i in h.fake.images.values() if i["labels"].get("rdpm-desktop") == "ref"]


def test_build_delete_failure_keep_for_diagnosis(harness):
    h = harness(fail={"delete"})
    h.decisions = ["keep"]
    op = h.run(BuildOp(h.ctx, "ref", "Ref", params()))
    assert op.outcome == "failed" and op.error.code == "delete_failed"
    srv = next(iter(h.fake.servers.values()))
    assert srv["labels"][L_ROLE] == ROLE_TMP and srv["labels"][L_OP] == OP_BUILDING
    assert h.creds.get_password("ref") == op.password
    assert not h.fake.ssh_keys and [f for f in h.fake.firewalls.values() if f["name"].startswith("rdpm-build-")]


def test_controller_build_placeholder_and_result(tmp_path):
    from rdpm.state import Act, DState
    from tests.test_controller import make_controller, spin
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    ctrl.build_windows("Mon Windows", params())
    view = spin(ctrl, lambda: next((v for v in ctrl.desktop_views() if v.slug == "mon-windows"), None))
    assert view.state == DState.BUILDING and "Installation" in view.spec
    assert not [b for b in ctrl.banners() if b.key.startswith("tmp:")]
    view = spin(ctrl, lambda: next((v for v in ctrl.desktop_views()
                                    if v.slug == "mon-windows" and v.state == DState.ARCHIVED), None), timeout=40)
    assert view.name == "Mon Windows" and Act.LAUNCH in view.actions
    assert not fake.builds and not fake.ssh_keys
