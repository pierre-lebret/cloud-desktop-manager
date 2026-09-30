from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rdpm import fmt, netutil, rdp
from rdpm.labels import format_description, is_valid_label_value, merge_labels, parse_description, slugify, unique_slug
from rdpm.models import Desktop, FirewallRule, Inventory, PrimaryIpInfo, ServerInfo, SnapshotInfo, VolumeInfo
from rdpm.offers import base_type_for, cheapest_base_anywhere, compatible_offers, recommended_offer
from rdpm.pricing import billed_hours, billing_minute, compute_costs
from rdpm.retention import snapshots_to_delete
from rdpm.state import DState, derive_state, group_inventory

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def snap(i, days_ago, protected=False, status="available", forced=False, slug="bureau", size=10.0):
    labels = {"rdpm": "1", "rdpm-desktop": slug, "rdpm-forced": "1" if forced else "0"} if slug else {}
    return SnapshotInfo(id=i, description=f"Bureau — 2026-09-{27 - days_ago:02d} 10:00", labels=labels,
                        status=status, created=NOW - timedelta(days=days_ago), image_size=size, disk_size=160,
                        protected=protected, architecture="x86")


def server(i=1, status="running", slug="bureau", op=None, host=None, ipv4="203.0.113.5", **kw):
    labels = {"rdpm": "1", "rdpm-desktop": slug, "rdpm-role": kw.pop("role", "desktop")} if slug else {}
    if op:
        labels["rdpm-op"] = op
    if host:
        labels["rdpm-op-host"] = host
    return ServerInfo(id=i, name=f"rdpm-{slug}", status=status, labels=labels, created=NOW - timedelta(hours=2),
                      server_type="cpx32", cores=4, memory=8, cpu_type="shared", disk=160, location="nbg1",
                      ipv4=ipv4, ipv4_id=77, firewall_ids=(), volume_ids=(), image_id=None,
                      price_hourly=0.0569, price_monthly=35.49)


# --- labels -----------------------------------------------------------------------------
def test_slugify_handles_accents_and_length():
    assert slugify("Bureau Été 2026 !") == "bureau-ete-2026"
    assert slugify("   ") == "bureau"
    assert len(slugify("x" * 100)) == 40
    assert unique_slug("dev", {"dev", "dev-2"}) == "dev-3"


def test_label_validation_and_merge():
    assert is_valid_label_value("abc-1.2_x")
    assert not is_valid_label_value("-abc")
    assert not is_valid_label_value("a" * 64)
    assert merge_labels({"a": "1", "b": "2"}, {"c": 3}, remove=("a",)) == {"b": "2", "c": "3"}
    with pytest.raises(ValueError):
        merge_labels({}, {"k": "bad value"})


def test_description_roundtrip():
    when = datetime(2026, 9, 27, 13, 30, tzinfo=timezone.utc)
    name, parsed = parse_description(format_description("Mon bureau", when))
    assert name == "Mon bureau" and parsed is not None
    assert parse_description("windows-8gb-nbg1-1-1790100869") == ("windows-8gb-nbg1-1-1790100869", None)


# --- offres -------------------------------------------------------------------------------
def test_offers_respect_disk_and_availability(static):
    offers = compatible_offers(static.server_types, "nbg1", 160)
    names = [o.name for o in offers]
    assert "cpx32" in names and "cpx31" not in names and "cx43" not in names
    assert all(o.stype.disk >= 160 for o in offers)
    rec = recommended_offer(static.server_types, "nbg1", 160)
    assert rec.name == "cpx32" and rec.base_type is None
    big = next(o for o in offers if o.name == "cpx42")
    assert big.base_type == "cpx32" and not big.disk_grows


def test_unavailable_offers_can_be_listed(static):
    offers = compatible_offers(static.server_types, "nbg1", 160, include_unavailable=True)
    cx43 = next(o for o in offers if o.name == "cx43")
    assert not cx43.available


def test_base_type_and_tmp_server_choice(static):
    assert base_type_for(static.server_types, "nbg1", 160, "x86").name == "cpx32"
    loc, st = cheapest_base_anywhere(static.server_types, list(static.locations), 160, "x86", prefer="nbg1")
    assert st.disk == 160


# --- rétention ----------------------------------------------------------------------------
def test_retention_keeps_n_and_pinned():
    snaps = [snap(1, 0), snap(2, 1), snap(3, 2), snap(4, 3, protected=True), snap(5, 4)]
    doomed = {s.id for s in snapshots_to_delete(snaps, keep=2, protect_ids={1})}
    assert doomed == {3, 5}


def test_retention_forced_keeps_last_clean():
    snaps = [snap(1, 0, forced=True), snap(2, 1, forced=True), snap(3, 2), snap(4, 3)]
    doomed = {s.id for s in snapshots_to_delete(snaps, keep=2, protect_ids={1}, new_was_forced=True)}
    assert doomed == {4}


def test_retention_ignores_creating_and_never_deletes_last():
    snaps = [snap(1, 0, status="creating"), snap(2, 1)]
    assert snapshots_to_delete(snaps, keep=1) == []


# --- états --------------------------------------------------------------------------------
@pytest.mark.parametrize("srv,op,probe,running_for,expected", [
    (None, None, None, None, DState.ARCHIVED),
    (server(), None, True, 30, DState.READY),
    (server(), None, False, 30, DState.BOOTING),
    (server(), None, False, 9999, DState.UNREACHABLE),
    (server(status="off"), None, None, None, DState.OFF_BILLED),
    (server(status="stopping"), None, None, None, DState.STOPPING),
    (server(op="saving", host="testhost"), None, None, None, DState.INTERRUPTED),
    (server(op="saving", host="autre-pc"), None, None, None, DState.REMOTE_OP),
    (server(), "save", None, None, DState.SAVING),
    (server(), "discard", None, None, DState.DISCARDING),
])
def test_derive_state(srv, op, probe, running_for, expected):
    d = Desktop(slug="bureau", name="Bureau", snapshots=[snap(1, 1)], server=srv)
    assert derive_state(d, op, probe, running_for, "testhost", 600) == expected


def test_grouping_and_conflict():
    ip_orphan = PrimaryIpInfo(9, "ip", "178.105.239.115", "ipv4", "nbg1", None, False, {}, NOW)
    vol = VolumeInfo(5, "data", 20, "nbg1", None, {"rdpm": "1", "rdpm-desktop": "bureau"}, NOW)
    inv = Inventory(servers=(server(1), server(2), server(3, slug=None)),
                    snapshots=(snap(1, 1), snap(2, 2, slug=None)), volumes=(vol,), primary_ips=(ip_orphan,))
    g = group_inventory(inv, lambda s: None)
    d = g.desktop("bureau")
    assert d.server.id == 1 and [s.id for s in d.extra_servers] == [2]
    assert derive_state(d, None, True, 1, "h", 600) == DState.CONFLICT
    assert [s.id for s in g.unmanaged_snapshots] == [2]
    assert [s.id for s in g.unmanaged_servers] == [3]
    assert [i.ip for i in g.unassigned_ips] == ["178.105.239.115"]
    assert d.volumes == [vol] and d.location_lock() == ("nbg1", ["volume « data »"])


# --- coûts --------------------------------------------------------------------------------
def test_billing_rounds_up_started_hours(static):
    start = NOW - timedelta(minutes=61)
    assert billed_hours(start, NOW) == 2 and billing_minute(start, NOW) == 1
    inv = Inventory(servers=(server(),), snapshots=(snap(1, 1, size=12.7),))
    costs = compute_costs(inv, static.pricing, 0.0, NOW)
    assert costs.running_h == pytest.approx(0.0569 + static.pricing.ipv4_h("nbg1"))
    assert costs.storage_month == pytest.approx(12.7 * static.pricing.image_gb_month)


def test_fmt():
    assert fmt.eur(1234.5) == "1 234,50 €"
    assert fmt.eur_h(0.0577) == "0,0577 €/h"
    assert fmt.duration(3720) == "1 h 02"
    assert fmt.gb(12.718) == "12,7 Go"


# --- réseau / RDP -------------------------------------------------------------------------
def test_cidr_normalization():
    assert netutil.normalize_cidr(" 1.2.3.4 ") == "1.2.3.4/32"
    assert netutil.normalize_cidr("10.0.0.0/24") == "10.0.0.0/24"
    for bad in ("0.0.0.0/0", "abc", "1.0.0.0/8"):
        with pytest.raises(ValueError):
            netutil.normalize_cidr(bad)
    assert netutil.ip_allowed("176.137.192.109", ["176.137.192.109/32"])
    assert not netutil.ip_allowed("176.137.192.110", ["176.137.192.109/32"])


def test_firewall_rule_helpers():
    rules = [FirewallRule("in", "tcp", "3389", ("1.1.1.1/32",), "Maison"),
             FirewallRule("in", "tcp", "22", ("0.0.0.0/0",), "ssh")]
    rules = netutil.with_udp_normalized(rules)
    assert any(r.protocol == "udp" and "1.1.1.1/32" in r.source_ips for r in rules)
    rules = netutil.with_source_added(rules, "2.2.2.2/32", "Bureau")
    assert netutil.rdp_sources(rules) == [("1.1.1.1/32", "Maison"), ("2.2.2.2/32", "Bureau")]
    rules = netutil.with_source_removed(rules, "1.1.1.1/32")
    assert [c for c, _ in netutil.rdp_sources(rules)] == ["2.2.2.2/32"]
    assert any(r.port == "22" for r in rules)


def test_rdp_file():
    content = rdp.build_rdp_file("203.0.113.5", "Administrator")
    assert "full address:s:203.0.113.5" in content
    assert "username:s:Administrator" in content
    assert "prompt for credentials:i:0" in content


def test_source_conflict_detects_duplicates_and_coverage():
    assert netutil.source_conflict("1.2.3.4/32", ["1.2.3.4/32"]) == "1.2.3.4/32"
    assert netutil.source_conflict("1.2.3.4/32", ["9.9.9.9/32", "1.2.3.0/24"]) == "1.2.3.0/24"
    assert netutil.source_conflict("1.2.3.0/24", ["1.2.3.4/32"]) is None   # plus large : pas un doublon
    assert netutil.source_conflict("2001:db8::1/128", ["1.2.3.0/24"]) is None
    assert netutil.source_conflict("1.2.3.5/32", ["1.2.3.4/32", "n'importe quoi"]) is None


def firewall(i, role, slug=None, cidrs=("1.1.1.1/32",)):
    from rdpm.models import FirewallInfo
    labels = {"rdpm": "1", "rdpm-role": role}
    if slug:
        labels["rdpm-desktop"] = slug
    return FirewallInfo(i, f"fw-{i}", labels, (FirewallRule("in", "tcp", "3389", tuple(cidrs)),), ())


def test_grouping_attaches_desktop_firewalls():
    shared = firewall(1, "rdp")
    own = firewall(2, "rdp-desktop", "bureau")
    orphan = firewall(3, "rdp-desktop", "disparu")
    inv = Inventory(snapshots=(snap(1, 1),), firewalls=(shared, own, orphan))
    g = group_inventory(inv, lambda s: None)
    assert inv.rdp_firewall == shared
    assert g.desktops[0].firewall == own
    assert g.orphan_firewalls == [orphan]
    assert [d.slug for d in g.desktops] == ["bureau"]   # un pare-feu ne crée pas de bureau


def test_desktop_os_defaults_to_windows():
    d = Desktop("b", "B", snapshots=[snap(1, 1)])
    assert d.os == "windows"
    linux = snap(2, 0)
    linux.labels["rdpm-os"] = "linux"
    assert Desktop("b", "B", snapshots=[linux, snap(1, 1)]).os == "linux"
