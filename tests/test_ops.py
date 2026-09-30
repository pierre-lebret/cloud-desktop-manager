"""Opérations de bout en bout sur le FakeCloud (temps accéléré), sans réseau ni interface."""

from __future__ import annotations

import threading

from rdpm.constants import L_OP
from rdpm.hetzner.fake import BOOT_TO_RDP_S
from rdpm.labels import desktop_labels
from rdpm.ops.duplicate import DuplicateOp
from rdpm.ops.launch import LaunchOp, LaunchParams
from rdpm.ops.resources import AddVolumeOp, AdoptFirewallOp, AdoptSnapshotOp, FirewallOp
from rdpm.ops.save import DiscardOp, ResumeOp, SaveOp

ORIGIN = 434955057
FIREWALL = 11664160


def adopt(h, slug="win"):
    snap = h.backend.get_image(ORIGIN)
    op = h.run(AdoptSnapshotOp(h.ctx, slug, "Windows", snap, pin=True))
    assert op.outcome == "ok", op.error
    return h.backend.get_image(ORIGIN)


def launch(h, slug="win", server_type="cpx32", base_type=None, **kw):
    params = LaunchParams(snapshot_id=ORIGIN, snapshot_disk=160, server_type=server_type, location="nbg1",
                          base_type=base_type, firewall_id=FIREWALL, **kw)
    op = h.run(LaunchOp(h.ctx, slug, "Windows", params))
    assert op.outcome == "ok", op.error
    return h.backend.get_server(op.result["server_id"])


def test_adopt_and_launch(harness):
    h = harness()
    snap = adopt(h)
    assert snap.slug == "win" and snap.protected
    h.creds.set_password("win", "S3cret!")
    srv = launch(h)
    assert srv.status == "running" and srv.slug == "win" and L_OP not in srv.labels
    assert FIREWALL in srv.firewall_ids
    body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/servers")
    assert body["location"] == "nbg1" and "datacenter" not in body
    assert body["public_net"] == {"enable_ipv4": True, "enable_ipv6": False}
    assert h.config.managed_creds == {srv.ipv4: "win"}


def test_disk_lock_keeps_snapshot_disk_size(harness):
    h = harness()
    adopt(h)
    srv = launch(h, server_type="cpx42", base_type="cpx32")
    assert srv.server_type == "cpx42" and srv.disk == 160
    op = h.run(SaveOp(h.ctx, "win", "Windows", srv.id, close=True))
    assert op.outcome == "ok", op.error
    assert h.backend.get_image(op.image_id).disk_size == 160


def test_save_close_deletes_server_and_applies_retention(harness):
    h = harness()
    adopt(h)
    for _ in range(3):
        srv = launch(h)
        op = h.run(SaveOp(h.ctx, "win", "Windows", srv.id, close=True))
        assert op.outcome == "ok", op.error
        assert h.backend.get_server(srv.id) is None
        assert h.config.managed_creds == {}
    snaps = h.backend.list_snapshots("win")
    unpinned = [s for s in snaps if not s.protected]
    assert len(unpinned) == 2 and any(s.id == ORIGIN for s in snaps)
    assert len(h.sessions.read_all()) == 3


def test_failed_snapshot_never_deletes_server(harness):
    h = harness(fail={"snapshot"})
    adopt(h)
    srv = launch(h)
    op = h.run(SaveOp(h.ctx, "win", "Windows", srv.id, close=True))
    assert op.outcome == "failed" and op.error.code == "snapshot_failed"
    remaining = h.backend.get_server(srv.id)
    assert remaining is not None and L_OP not in remaining.labels


def test_shutdown_timeout_asks_then_forces(harness):
    h = harness(fail={"shutdown_timeout"})
    h.config.settings["shutdown_timeout_s"] = 0.05
    adopt(h)
    srv = launch(h)
    h.decisions = ["force"]
    op = h.run(SaveOp(h.ctx, "win", "Windows", srv.id, close=True))
    assert op.outcome == "ok", op.error
    assert op.forced and h.backend.get_image(op.image_id).forced


def test_shutdown_timeout_cancel_keeps_server(harness):
    h = harness(fail={"shutdown_timeout"})
    h.config.settings["shutdown_timeout_s"] = 0.05
    adopt(h)
    srv = launch(h)
    h.decisions = ["cancel"]
    op = h.run(SaveOp(h.ctx, "win", "Windows", srv.id, close=True))
    assert op.outcome == "cancelled"
    assert h.backend.get_server(srv.id).status == "running"


def test_delete_failure_is_reported(harness):
    h = harness(fail={"delete"})
    adopt(h)
    srv = launch(h)
    op = h.run(DiscardOp(h.ctx, "win", "Windows", srv.id))
    assert op.outcome == "failed" and op.error.code == "delete_failed"


def test_discard(harness):
    h = harness()
    adopt(h)
    srv = launch(h)
    op = h.run(DiscardOp(h.ctx, "win", "Windows", srv.id))
    assert op.outcome == "ok" and h.backend.get_server(srv.id) is None


def test_checkpoint_keeps_server_running(harness):
    h = harness()
    adopt(h)
    srv = launch(h)
    op = h.run(SaveOp(h.ctx, "win", "Windows", srv.id, close=False))
    assert op.outcome == "ok", op.error
    after = h.backend.get_server(srv.id)
    assert after.status == "running" and L_OP not in after.labels


def test_resume_after_crash_mid_save(harness):
    h = harness()
    adopt(h)
    srv = launch(h)
    # Simule un crash : snapshot créé, serveur encore étiqueté « saving ».
    h.backend.server_action(srv.id, "poweroff")
    image_id, _ = h.backend.create_snapshot(srv.id, "Windows — 2026-09-27 10:00", desktop_labels("win"))
    h.backend.update_server_labels(srv.id, {"rdpm-op": "saving", "rdpm-op-image": image_id})
    op = h.run(ResumeOp(h.ctx, "win", "Windows", h.backend.get_server(srv.id)))
    assert op.outcome == "ok", op.error
    assert h.backend.get_server(srv.id) is None and h.backend.get_image(image_id).available


def test_duplicate_archived_copy_cleans_tmp_server(harness):
    h = harness()
    snap = adopt(h)
    h.creds.set_password("win", "pw")
    op = h.run(DuplicateOp(h.ctx, "win", snap, "win-copie", "Windows copie"))
    assert op.outcome == "ok", op.error
    assert not h.fake.servers, "le serveur temporaire doit être supprimé"
    copies = h.backend.list_snapshots("win-copie")
    assert len(copies) == 1 and copies[0].disk_size == 160
    assert h.creds.get_password("win-copie") == "pw"
    body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/servers")
    assert body["public_net"]["enable_ipv4"] is False and body["start_after_create"] is False


def test_add_volume_uses_server_only(harness):
    h = harness()
    adopt(h)
    srv = launch(h)
    op = h.run(AddVolumeOp(h.ctx, "win", "Windows", srv.id, 10, "win-data"))
    assert op.outcome == "ok", op.error
    body = next(b for m, u, b in h.fake.requests if m == "POST" and u == "/volumes")
    assert body["server"] == srv.id and "location" not in body and "format" not in body


def test_concurrent_firewall_adds_keep_both(harness):
    h = harness()
    ops = [FirewallOp(h.ctx, FIREWALL, add=[(f"198.51.100.{i}/32", f"poste {i}")]) for i in range(1, 6)]
    threads = [threading.Thread(target=op.run) for op in ops]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(op.outcome == "ok" for op in ops)
    sources = {ip for r in h.backend.get_firewall(FIREWALL).rules for ip in r.source_ips}
    assert {f"198.51.100.{i}/32" for i in range(1, 6)} <= sources


def test_adopt_firewall_adds_udp(harness):
    h = harness()
    fw = h.backend.get_firewall(FIREWALL)
    op = h.run(AdoptFirewallOp(h.ctx, fw))
    assert op.outcome == "ok", op.error
    fw = h.backend.get_firewall(FIREWALL)
    assert fw.is_rdp_managed and {r.protocol for r in fw.rules} == {"tcp", "udp"}


def test_resource_unavailable_is_translated(harness):
    h = harness(fail={"resource_unavailable"})
    adopt(h)
    params = LaunchParams(snapshot_id=ORIGIN, snapshot_disk=160, server_type="cpx32", location="nbg1")
    op = h.run(LaunchOp(h.ctx, "win", "Windows", params))
    assert op.outcome == "failed" and op.error.code == "resource_unavailable"
    assert not h.fake.servers


def test_probe_waits_for_boot(harness):
    h = harness(speed=1.0)
    adopt(h)
    h.fake.speed = 2000.0
    srv = launch(h)
    h.fake.boot_at[srv.id] = h.fake.sim()
    assert not h.backend.rdp_probe(srv.ipv4)
    h.fake.boot_at[srv.id] = h.fake.sim() - BOOT_TO_RDP_S - 1
    assert h.backend.rdp_probe(srv.ipv4)


def test_desktop_firewall_created_and_applied(harness):
    from rdpm.ops.resources import DesktopFirewallOp
    h = harness()
    adopt(h)
    srv = launch(h)
    op = h.run(DesktopFirewallOp(h.ctx, "win", "Windows", "198.51.100.9/32", "Poste", srv.id))
    assert op.outcome == "ok", op.error
    fw = h.backend.get_firewall(op.result["firewall_id"])
    assert fw.is_desktop_firewall and fw.slug == "win" and not fw.is_rdp_managed
    assert {r.protocol for r in fw.rules} == {"tcp", "udp"}
    assert set(h.backend.get_server(srv.id).firewall_ids) == {FIREWALL, fw.id}


def test_launch_applies_shared_and_desktop_lists(harness):
    h = harness()
    adopt(h)
    srv = launch(h, allow_cidr="198.51.100.10/32", allow_scope="desktop")
    own = next(f for f in h.backend.fetch_inventory().firewalls if f.is_desktop_firewall)
    assert own.slug == "win" and "198.51.100.10/32" in {c for r in own.rules for c in r.source_ips}
    assert set(srv.firewall_ids) == {FIREWALL, own.id}
    shared = h.backend.get_firewall(FIREWALL)
    assert "198.51.100.10/32" not in {c for r in shared.rules for c in r.source_ips}


def test_delete_desktop_removes_its_firewall(harness):
    from rdpm.ops.resources import DeleteDesktopOp, DesktopFirewallOp
    h = harness()
    adopt(h)
    op = h.run(DesktopFirewallOp(h.ctx, "win", "Windows", "198.51.100.9/32", "Poste"))
    fw = h.backend.get_firewall(op.result["firewall_id"])
    op = h.run(DeleteDesktopOp(h.ctx, "win", "Windows", [], [], None, forget_password=False, firewall=fw))
    assert op.outcome == "ok", op.error
    assert fw.id not in h.fake.firewalls
