"""Licence d'évaluation : suivi par labels, prolongation prévue au lancement, vues et scripts Windows."""

from __future__ import annotations

from datetime import date, timedelta

from rdpm import license
from rdpm.build import catalog
from rdpm.build.scripts import render_eval_rearm_ps1, render_postinstall_ps1
from rdpm.constants import L_EVAL_AUTO, L_EVAL_EXP, L_EVAL_REARMS
from rdpm.ops.build import BuildOp
from rdpm.ops.duplicate import DuplicateOp
from rdpm.ops.save import SaveOp
from rdpm.state import Act
from tests.test_build import params as build_params
from tests.test_ops import ORIGIN, adopt, launch

TODAY = date(2026, 9, 29)


def lic(days: int, rearms: int | None = 5, auto: bool = True) -> license.EvalLicense:
    return license.EvalLicense(TODAY + timedelta(days=days), rearms, auto)


def labels_for(days: int, rearms: int = 5, auto: bool = True) -> dict[str, str]:
    return license.to_labels(license.EvalLicense(date.today() + timedelta(days=days), rearms, auto))


# --- fonctions pures ---------------------------------------------------------------------------------
def test_labels_roundtrip_and_invalid():
    one = lic(180)
    assert license.from_labels(license.to_labels(one)) == one
    assert license.to_labels(one) == {L_EVAL_EXP: "20270328", L_EVAL_AUTO: "1", L_EVAL_REARMS: "5"}
    assert license.from_labels({}) is None
    assert license.from_labels({L_EVAL_EXP: "2027-03-28"}) is None
    assert license.from_labels({L_EVAL_EXP: "20271340"}) is None
    assert license.from_labels({L_EVAL_EXP: "20270328"}).rearms is None


def test_at_boot_follows_the_task_rule():
    extended, done = license.at_boot(lic(10), TODAY)
    assert done and extended.expires == TODAY + timedelta(days=180) and extended.rearms == 4
    expired, done = license.at_boot(lic(-30, rearms=1), TODAY)
    assert done and expired.rearms == 0
    for untouched in (lic(15), lic(10, auto=False), lic(10, rearms=0)):
        assert license.at_boot(untouched, TODAY) == (untouched, False)


def test_summary_alert_and_action():
    assert license.summary(lic(42), TODAY) == ("Évaluation : 42 jours restants (jusqu'au 10/11/2026) · "
                                              "prolongation automatique (5 restantes)")
    assert "manuelle" in license.summary(lic(42, auto=False), TODAY)
    assert "expirée depuis 3 jours" in license.summary(lic(-3), TODAY)
    assert "plus de prolongation" in license.summary(lic(42, rearms=0), TODAY)
    assert license.alert(lic(15), TODAY) is None
    assert "prolongée automatiquement" in license.alert(lic(14), TODAY)
    assert "slmgr /rearm" in license.alert(lic(3, auto=False), TODAY)
    assert "Plus aucune prolongation" in license.alert(lic(3, rearms=0), TODAY)
    assert "s'éteindra seul" in license.alert(lic(-1, auto=False), TODAY)
    assert not license.needs_action(lic(3), TODAY)
    assert license.needs_action(lic(3, auto=False), TODAY) and license.needs_action(lic(3, rearms=0), TODAY)


# --- scripts Windows ------------------------------------------------------------------------------------
def test_postinstall_installs_task_only_for_evaluation():
    with_task = render_postinstall_ps1("Romance Standard Time", 15)
    assert "Register-ScheduledTask -TaskName 'rdpm-eval-rearm'" in with_task and with_task.isascii()
    assert "-AtStartup" in with_task and "S-1-5-18" in with_task
    assert "rdpm-eval-rearm" not in render_postinstall_ps1("Romance Standard Time", None)
    script = render_eval_rearm_ps1(15)
    assert "param([int]$Threshold = 15)" in script and "/rearm" in script and "/ato" in script
    assert "Restart-Computer -Force" in script and "TotalDays -lt 2" in script   # garde-fou anti-boucle
    assert "RemainingWindowsReArmCount" in script and "GracePeriodRemaining" in script


# --- opérations -------------------------------------------------------------------------------------------
def test_build_starts_tracking(harness):
    h = harness()
    op = h.run(BuildOp(h.ctx, "ref", "Ref", build_params()))
    assert op.outcome == "ok", op.error
    snap = h.backend.list_snapshots("ref")[0]
    tracked = license.from_labels(snap.labels)
    assert tracked.auto and tracked.rearms == catalog.EDITIONS["server2025"].eval_rearms
    assert tracked.expires == date.today() + timedelta(days=180)
    custom = harness()
    op = custom.run(BuildOp(custom.ctx, "iso", "ISO", build_params(edition="custom", iso_url="https://x/w.iso",
                                                                   image_name="Windows 11 Pro")))
    assert op.outcome == "ok", op.error
    assert license.from_labels(custom.backend.list_snapshots("iso")[0].labels) is None


def test_launch_predicts_rearm_then_save_and_duplicate_carry_labels(harness):
    h = harness()
    adopt(h)
    h.fake.images[ORIGIN]["labels"].update(labels_for(days=5, rearms=3))
    srv = launch(h)
    on_server = license.from_labels(srv.labels)
    assert on_server.expires == date.today() + timedelta(days=180) and on_server.rearms == 2
    assert any("prolongée automatiquement" in getattr(e, "message", "") for e in h.events)
    op = h.run(SaveOp(h.ctx, "win", "Windows", srv.id, close=True))
    assert op.outcome == "ok", op.error
    saved = h.backend.get_image(op.image_id)
    assert license.from_labels(saved.labels) == on_server
    dup = h.run(DuplicateOp(h.ctx, "win", saved, "copie", "Copie"))
    assert dup.outcome == "ok", dup.error
    assert license.from_labels(h.backend.list_snapshots("copie")[0].labels) == on_server


def test_launch_without_tracking_adds_nothing(harness):
    h = harness()
    adopt(h)
    srv = launch(h)
    assert license.from_labels(srv.labels) is None and L_EVAL_EXP not in srv.labels


def test_controller_view_banner_and_manual_task(tmp_path):
    from tests.test_controller import make_controller, spin
    fake, ctrl = make_controller(tmp_path)
    spin(ctrl, lambda: ctrl.inventory and ctrl.static)
    dev = next(i for i in fake.images.values() if i["labels"].get("rdpm-desktop") == "dev-perso" and
               not i["protection"]["delete"])
    dev["labels"].update(labels_for(days=9, rearms=5, auto=False))
    ctrl.refresh_now()
    view = spin(ctrl, lambda: next((v for v in ctrl.desktop_views() if v.slug == "dev-perso" and v.license), None))
    assert "9 jours restants" in view.license and "manuelle" in view.license
    assert "slmgr /rearm" in view.note and Act.LICENSE in view.actions
    assert any(b.key.startswith("eval:dev-perso:") for b in ctrl.banners())
    trading = next(v for v in ctrl.desktop_views() if v.slug == "trading")
    assert trading.license is None and Act.LICENSE not in trading.actions

    # bureau en cours sans suivi → la tâche installée à la main est notée sur le serveur
    srv = next(s for s in fake.servers.values() if s["labels"].get("rdpm-desktop") == "trading")
    srv["labels"].update(labels_for(days=9, rearms=5, auto=False))
    ctrl.refresh_now()
    spin(ctrl, lambda: next((v for v in ctrl.desktop_views() if v.slug == "trading" and v.license), None))
    ctrl.mark_eval_auto("trading")
    spin(ctrl, lambda: srv["labels"].get(L_EVAL_AUTO) == "1" and not ctrl.runner.active())
    ctrl.refresh_now()
    view = spin(ctrl, lambda: next((v for v in ctrl.desktop_views()
                                    if v.slug == "trading" and v.license and "automatique" in v.license), None))
    assert "prolongée automatiquement" in view.note
    assert not any(b.key.startswith("eval:trading:") for b in ctrl.banners())
