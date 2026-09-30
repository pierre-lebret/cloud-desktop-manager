"""Contrôleur : état de l'application, rafraîchissements, sondes, commandes, fermeture.

Tout ce module s'exécute sur le thread Tk, sauf les tâches soumises aux pools qui ne font que
poster des événements dans la file.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from queue import Empty, SimpleQueue
from typing import Callable

from . import fmt, license, netutil, rdp
from .config import AppConfig, SessionLog
from .constants import (
    MAX_FIREWALL_SOURCES, OS_LINUX, L_LOC, L_OP_TS, L_TYPE, OP_BUILDING, OP_CHECKPOINT, OP_DISCARDING, OP_LAUNCHING, OP_SAVING,
    PROBE_BOOTING_S, PROBE_READY_S, PUBLIC_IP_REFRESH_S, REFRESH_BUSY_S, REFRESH_IDLE_S, STATIC_CACHE_PATH,
)
from .events import (
    InventoryLoaded, LogEvent, NeedDecision, OpFinished, OpProgress, ProbeResult, PublicIpResult,
    RefreshRequest, StaticLoaded, ToastEvent,
)
from .hetzner.errors import UserError, to_user_error
from .labels import host_label, slugify, unique_slug
from .models import Desktop, FirewallInfo, Inventory, PrimaryIpInfo, ServerInfo, SnapshotInfo, StaticData, VolumeInfo
from .offers import best_by_location
from .projects import ProjectSpec
from .ops.base import OpContext, Operation
from .ops.build import BuildOp, BuildParams
from .ops.duplicate import DuplicateOp
from .ops.launch import LaunchOp, LaunchParams
from .ops.resources import (
    AddVolumeOp, AdoptFirewallOp, AdoptServerOp, AdoptSnapshotOp, ApplyFirewallOp, CleanupOp,
    ClearOpLabelsOp, CreateFirewallOp, DeleteDesktopOp, DesktopFirewallOp, FirewallOp, FixedIpOp, PowerOp,
    RenameOp, EvalAutoOp, SnapshotActionOp, VolumeActionOp,
)
from .ops.runner import BusyError, OperationRunner
from .ops.save import DiscardOp, ResumeOp, SaveOp
from .pricing import CostSummary, billing_minute, compute_costs, server_burn, session_cost
from .rdp import CredentialStore
from .state import (
    SERVER_STATES, STATE_STYLE, Act, DState, Grouping, allowed_actions, derive_state, group_inventory,
    primary_action,
)

log = logging.getLogger(__name__)

TRANSITIONAL = {DState.BOOTING, DState.STOPPING, DState.SNAPSHOT_PENDING, DState.LAUNCHING, DState.SAVING,
                DState.CHECKPOINTING, DState.DISCARDING, DState.DUPLICATING, DState.BUILDING, DState.BUSY,
                DState.PENDING}

OP_NAMES_FR = {OP_SAVING: "sauvegarde", OP_CHECKPOINT: "sauvegarde", OP_DISCARDING: "fermeture",
               OP_LAUNCHING: "lancement", "duplicating": "duplication", OP_BUILDING: "construction"}
PLACEHOLDER_KINDS = ("launch", "duplicate", "build")
LAUNCH_GRACE_S = 180   # délai max pour voir apparaître dans l'inventaire un serveur tout juste créé   # opérations qui font naître une carte sans snapshot


@dataclass
class ProbeInfo:
    ok: bool = False
    running_since: float | None = None
    next_at: float = 0.0
    inflight: bool = False


@dataclass
class DesktopView:
    slug: str
    name: str
    state: DState
    state_label: str
    color: str
    spec: str
    ip: str | None = None
    fixed_ip: str | None = None
    uptime: str | None = None
    burn: str | None = None
    backup: str = ""
    volumes: list[str] = field(default_factory=list)
    license: str | None = None      # ligne « Licence » (évaluation Windows Server)
    note: str | None = None
    op_phase: str | None = None
    op_progress: float | None = None
    op_elapsed: str | None = None
    op_cancellable: bool = False
    error: UserError | None = None
    actions: list[Act] = field(default_factory=list)
    primary: Act | None = None
    primary_disabled: bool = False
    primary_hint: str | None = None   # pourquoi le bouton principal est grisé (infobulle)
    os: str = "windows"
    secondary: Act | None = None
    key: str = ""            # « <projet>/<slug> » : identifiant unique dans l'interface
    project: str = ""


@dataclass
class Banner:
    key: str
    kind: str
    text: str
    actions: list[tuple[str, str]] = field(default_factory=list)  # (libellé, clé d'action)


@dataclass
class DormantItem:
    key: str
    kind: str          # ip / volume / snapshot / server
    title: str
    detail: str
    monthly: float
    resource: object


@dataclass
class QuitState:
    slugs: list[str] = field(default_factory=list)
    attempted: dict[str, Operation] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)


@dataclass
class QuitRow:
    slug: str
    name: str
    status: str        # running / done / failed / skipped
    detail: str
    progress: float | None = None
    error: UserError | None = None


class AppController:
    """Un projet chez un fournisseur cloud (une clé API). Plusieurs contrôleurs cohabitent, rassemblés par ProjectHub."""

    def __init__(self, backend, config: AppConfig, creds: CredentialStore, sessions: SessionLog,
                 mode: str = "live", project=None, on_secret: Callable[[str], None] | None = None) -> None:
        self.backend, self.config, self.creds, self.sessions = backend, config, creds, sessions
        self.mode = mode
        self.project = project or ProjectSpec("local", "Projet", "", "fake")
        self.token_invalid = False   # clé refusée par le fournisseur : plus aucun appel pour ce projet
        self.host = host_label()
        self.queue: SimpleQueue = SimpleQueue()
        self.hard_stop = threading.Event()
        self.runner = OperationRunner(8)
        self._bg = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bg")
        self.ctx = OpContext(backend, config, creds, sessions, self.queue.put, self.host, self.hard_stop,
                             lambda: self.static, register_secret=on_secret)
        self.ui = None

        self.static: StaticData | None = None
        self.inventory: Inventory | None = None
        self.grouping = Grouping()
        self.inventory_version = 0
        self.refresh_error: UserError | None = None
        self.last_refresh: float | None = None
        self._refresh_inflight = False
        self._refresh_again = False
        self._next_refresh = 0.0
        self._invalidated_at = 0.0
        self._last_refresh_started = -1.0
        self._static_inflight = False
        self._next_static = 0.0

        self.public_ip: str | None = None
        self._ip_inflight = False
        self._next_ip = 0.0

        self.probes: dict[int, ProbeInfo] = {}
        self.errors: dict[str, UserError] = {}
        self.pending_autoconnect: set[str] = set()
        # Serveur créé mais pas encore dans l'inventaire : la carte reste « Création… » (pas de 2e lancement).
        self.launched: dict[str, tuple[int, float]] = {}
        self.failed_launch: dict[str, tuple[str, str]] = {}
        self.reminder_at: dict[int, float] = {}
        self.dismissed: set[str] = set()
        self._creds_swept = False
        self._pw_known: dict[str, bool] = {}
        self._month_sessions = sessions.month_total()
        self._load_static_cache()

    # --- boucle ------------------------------------------------------------------------------
    def pump(self) -> None:
        changed = False
        for _ in range(300):
            try:
                event = self.queue.get_nowait()
            except Empty:
                break
            changed |= self._handle(event)
        if changed and self.ui:
            self.ui.on_model_changed()

    def tick(self) -> None:
        now = time.monotonic()
        if not self._ip_inflight and now >= self._next_ip:  # hors fournisseur : possible sans token
            self._ip_inflight = True
            self._next_ip = now + PUBLIC_IP_REFRESH_S
            self._bg.submit(self._task_public_ip)
        if self.token_invalid:
            return
        if self.static is None or now >= self._next_static:
            self._load_static()
        if not self._refresh_inflight and now >= self._next_refresh:
            self.refresh_now()
        self._schedule_probes(now)
        self._check_reminders()

    def refresh_now(self) -> None:
        if self._refresh_inflight:
            self._refresh_again = True
            return
        self._refresh_inflight = True
        self._bg.submit(self._task_refresh, time.monotonic())

    def _task_refresh(self, started: float) -> None:
        try:
            self.queue.put(InventoryLoaded(self.backend.fetch_inventory(), None, started))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(InventoryLoaded(None, exc, started))

    def _task_static(self) -> None:
        try:
            raw = self.backend.fetch_static_raw()
            if self.mode == "live":
                STATIC_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                STATIC_CACHE_PATH.write_text(json.dumps(raw), encoding="utf-8")
            self.queue.put(StaticLoaded(StaticData.from_raw(raw)))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(StaticLoaded(None, exc))

    def _task_public_ip(self) -> None:
        self.queue.put(PublicIpResult(self.backend.public_ip()))

    def _task_probe(self, server_id: int, ip: str) -> None:
        self.queue.put(ProbeResult(server_id, self.backend.rdp_probe(ip)))

    def _load_static_cache(self) -> None:
        if self.mode != "live":
            return
        try:
            self.static = StaticData.from_raw(json.loads(STATIC_CACHE_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            self.static = None

    def _load_static(self) -> None:
        if self._static_inflight:
            return
        self._static_inflight = True
        self._next_static = time.monotonic() + 3600
        self._bg.submit(self._task_static)

    # --- événements --------------------------------------------------------------------------
    def _handle(self, ev) -> bool:
        if isinstance(ev, InventoryLoaded):
            return self._on_inventory(ev)
        if isinstance(ev, StaticLoaded):
            self._static_inflight = False
            if ev.static:
                self.static = ev.static
            else:
                self._next_static = time.monotonic() + 60
                self._log("warning", f"Catalogue {self.project.provider.short} indisponible : {to_user_error(ev.error)}")
            return True
        if isinstance(ev, ProbeResult):
            return self._on_probe(ev)
        if isinstance(ev, PublicIpResult):
            self._ip_inflight = False
            if ev.ip is None:
                self._next_ip = time.monotonic() + 60
            changed = ev.ip != self.public_ip
            self.public_ip = ev.ip or self.public_ip
            return changed
        if isinstance(ev, OpProgress):
            return True
        if isinstance(ev, OpFinished):
            self._on_op_finished(ev.op)
            return True
        if isinstance(ev, NeedDecision):
            if self.ui:
                self.ui.on_decision(ev)
            else:
                ev.future.set_result(ev.default)
            return False
        if isinstance(ev, LogEvent):
            if self.ui:
                self.ui.on_log(ev.level, ev.message, ev.slug)
            return False
        if isinstance(ev, ToastEvent):
            if self.ui:
                self.ui.toast(ev.message, ev.kind)
            return False
        if isinstance(ev, RefreshRequest):
            self.refresh_now()
        return False

    def _on_inventory(self, ev: InventoryLoaded) -> bool:
        self._refresh_inflight = False
        if ev.error is not None:
            self._next_refresh = time.monotonic() + REFRESH_IDLE_S
            first = self.refresh_error is None
            self.refresh_error = to_user_error(ev.error)
            if first:
                self._log("error", f"Rafraîchissement impossible : {self.refresh_error}")
            if self.refresh_error.code == "unauthorized":
                self._token_rejected()
            return True
        if ev.started < self._invalidated_at:
            self._refresh_again = True
        else:
            self.refresh_error = None
            self.inventory = ev.inventory
            self.inventory_version += 1
            known = {s.id for s in ev.inventory.servers}
            self.launched = {slug: (sid, t) for slug, (sid, t) in self.launched.items()
                             if sid not in known and time.monotonic() - t < LAUNCH_GRACE_S}
            self.last_refresh = time.monotonic()
            self._last_refresh_started = ev.started
            self._regroup()
            self._sync_probes()
            if not self._creds_swept:
                self._sweep_creds()
            busy = bool(self.runner.active()) or any(
                self.state_of(d) in TRANSITIONAL for d in self.grouping.desktops)
            self._next_refresh = time.monotonic() + (REFRESH_BUSY_S if busy else REFRESH_IDLE_S)
        if self._refresh_again:
            self._refresh_again = False
            self.refresh_now()
        return True

    def _on_probe(self, ev: ProbeResult) -> bool:
        info = self.probes.get(ev.server_id)
        if info is None:
            return False
        info.inflight = False
        was_ok, info.ok = info.ok, ev.ok
        info.next_at = time.monotonic() + (PROBE_READY_S if ev.ok else PROBE_BOOTING_S)
        if ev.ok and not was_ok:
            desktop = next((d for d in self.grouping.desktops if d.server and d.server.id == ev.server_id), None)
            if desktop:
                self._log("info", "Le bureau est prêt : connexion RDP possible", desktop.slug)
                if self.ui:
                    self.ui.on_ready(desktop)
                if desktop.slug in self.pending_autoconnect:
                    self.pending_autoconnect.discard(desktop.slug)
                    self.connect(desktop.slug)
        return was_ok != ev.ok

    def _on_op_finished(self, op: Operation) -> None:
        self._invalidated_at = time.monotonic()
        if op.slug:
            if op.outcome == "failed":
                self.errors[op.slug] = op.error
            elif op.outcome == "ok":
                self.errors.pop(op.slug, None)
        if op.outcome == "failed" and op.error and op.error.code == "unauthorized":
            self._token_rejected()
        if op.outcome == "ok":
            if op.success_message and self.ui:
                self.ui.toast(op.success_message, "success")
            if op.kind == "launch" and op.result.get("auto_connect"):
                self.pending_autoconnect.add(op.slug)
            if op.kind == "launch" and op.result.get("server_id"):
                self.launched[op.slug] = (op.result["server_id"], time.monotonic())
            if op.kind in ("launch", "checkpoint") or isinstance(op, PowerOp):
                sid = op.result.get("server_id") or getattr(op, "server_id", None)
                if sid:
                    self.probes[sid] = ProbeInfo(running_since=time.monotonic())
            if op.kind in ("save", "discard"):
                self._month_sessions = self.sessions.month_total()
            if op.followup and self.ui:
                self.ui.on_followup(op)
        elif op.outcome == "failed":
            if isinstance(op, LaunchOp):
                self.failed_launch[op.slug] = (op.params.base_type or op.params.server_type, op.params.location)
            if self.ui:
                self.ui.toast(f"{op.title} « {op.name} » : {op.error.message}", "error")
        elif op.outcome == "cancelled" and self.ui:
            self.ui.toast(f"{op.title} « {op.name} » annulée", "warning")
        self._regroup()
        self.refresh_now()

    def _sync_probes(self) -> None:
        running = {s.id: s for s in self.inventory.servers if s.status == "running" and s.managed}
        for sid in list(self.probes):
            if sid not in running:
                del self.probes[sid]
        for sid in running:
            self.probes.setdefault(sid, ProbeInfo(running_since=time.monotonic()))

    def _schedule_probes(self, now: float) -> None:
        for d in self.grouping.desktops:
            srv = d.server
            if not srv or not srv.ipv4 or srv.status != "running" or self.runner.for_slug(d.slug):
                continue
            info = self.probes.setdefault(srv.id, ProbeInfo(running_since=now))
            if not info.inflight and now >= info.next_at:
                info.inflight = True
                self._bg.submit(self._task_probe, srv.id, srv.ipv4)

    def _sweep_creds(self) -> None:
        """Supprime les identifiants TERMSRV d'IP qui ne nous appartiennent plus."""
        self._creds_swept = True
        ours = {s.ipv4 for s in self.inventory.servers if s.managed and s.ipv4}
        ours |= {d.fixed_ip.ip for d in self.grouping.desktops if d.fixed_ip}
        for ip in list(self.config.managed_creds):
            if ip not in ours:
                rdp.delete_termsrv_cred(ip)
                self.config.remove_cred(ip)
                self._log("info", f"Identifiant RDP périmé supprimé ({ip})")

    def _check_reminders(self) -> None:
        hours = float(self.config.get("reminder_hours") or 0)
        if hours <= 0 or not self.ui:
            return
        now = datetime.now(timezone.utc)
        for d in self.grouping.desktops:
            srv = d.server
            if not srv or self.runner.for_slug(d.slug):
                continue
            uptime_h = (now - srv.created).total_seconds() / 3600
            if uptime_h >= self.reminder_at.get(srv.id, hours):
                self.reminder_at[srv.id] = math.inf
                cost = session_cost(srv, self.static.pricing, now) if self.static else 0.0
                self.ui.on_reminder(d, uptime_h, cost)

    def snooze_reminder(self, server_id: int, hours: float | None) -> None:
        if hours is None:
            self.reminder_at[server_id] = math.inf
        else:
            srv = self.inventory.server(server_id) if self.inventory else None
            if srv:
                uptime_h = (datetime.now(timezone.utc) - srv.created).total_seconds() / 3600
                self.reminder_at[server_id] = uptime_h + hours

    def _regroup(self) -> None:
        if self.inventory is None:
            return
        placeholders = [op.slug for op in self.runner.active() if op.slug and op.kind in PLACEHOLDER_KINDS]
        names = {op.slug: op.name for op in self.runner.active() if op.slug}

        def name_for(slug: str) -> str | None:
            prefs = self.config.desktops.get(slug)
            return (prefs.display_name if prefs else None) or names.get(slug)
        self.grouping = group_inventory(self.inventory, name_for, placeholders)
        self._pw_known = {d.slug: bool(self.creds.get_password(d.slug)) for d in self.grouping.desktops}

    def _log(self, level: str, message: str, slug: str | None = None) -> None:
        getattr(log, "error" if level == "error" else "warning" if level == "warning" else "info")(message)
        if self.ui:
            self.ui.on_log(level, message, slug)

    # --- vues ----------------------------------------------------------------------------------
    def desktop(self, slug: str) -> Desktop | None:
        return self.grouping.desktop(slug)

    def launch_pending(self, slug: str) -> bool:
        info = self.launched.get(slug)
        return bool(info and time.monotonic() - info[1] < LAUNCH_GRACE_S)

    def state_of(self, d: Desktop) -> DState:
        op = self.runner.for_slug(d.slug)
        if not op and d.server is None and self.launch_pending(d.slug):
            return DState.LAUNCHING
        probe = self.probes.get(d.server.id) if d.server else None
        running_for = time.monotonic() - probe.running_since if probe and probe.running_since else None
        return derive_state(d, op.kind if op else None, probe.ok if probe else None, running_for, self.host,
                            float(self.config.get("boot_probe_timeout_s")))

    def desktop_views(self) -> list[DesktopView]:
        now = datetime.now(timezone.utc)
        views = []
        for d in self.grouping.desktops:
            state = self.state_of(d)
            label, color = STATE_STYLE[state]
            op = self.runner.for_slug(d.slug)
            srv = d.server
            view = DesktopView(slug=d.slug, name=d.name, state=state, state_label=label, color=color,
                               spec=self._spec(d), error=self.errors.get(d.slug), key=self.key(d.slug),
                               project=self.project.name, os=d.os)
            if srv:
                view.ip = srv.ipv4
                if self.static:
                    cost = session_cost(srv, self.static.pricing, now)
                    elapsed = (now - srv.created).total_seconds()
                    view.uptime = (f"Allumé depuis {fmt.duration(elapsed)} · {fmt.eur(cost)} · "
                                   f"heure entamée {billing_minute(srv.created, now)}/60 min")
                    view.burn = fmt.eur_h(server_burn(srv, self.static.pricing))
            if d.fixed_ip:
                view.fixed_ip = d.fixed_ip.ip
            view.backup = self._backup_line(d, now)
            view.volumes = [f"{v.name} · {v.size} Go" + ("" if v.server_id else " (détaché)") for v in d.volumes]
            lic = self.eval_license(d)
            if lic:
                view.license = license.summary(lic, now.astimezone().date())
            view.note = self._note(d, state) or (license.alert(lic, now.astimezone().date()) if lic else None)
            if op:
                view.op_phase = op.phase
                view.op_progress = op.progress / 100 if op.progress is not None else None
                view.op_elapsed = fmt.clock(op.elapsed)
                view.op_cancellable = op.cancellable
            view.actions = allowed_actions(state)
            if Act.CANCEL_OP in view.actions and not view.op_cancellable:
                view.actions.remove(Act.CANCEL_OP)
            if not self._pw_known.get(d.slug) and Act.COPY_PASSWORD in view.actions:
                view.actions.remove(Act.COPY_PASSWORD)
            if not lic and Act.LICENSE in view.actions:
                view.actions.remove(Act.LICENSE)
            view.primary = primary_action(state)
            if state == DState.LAUNCHING:   # bouton « Lancer » grisé tant que le serveur démarre
                view.primary, view.primary_disabled = Act.LAUNCH, True
                view.primary_hint = "Serveur en cours de création"
            elif view.primary == Act.LAUNCH and not d.latest:
                view.primary_disabled, view.primary_hint = True, "Aucune sauvegarde disponible pour ce bureau"
            elif view.primary == Act.LAUNCH and self.static is None:
                view.primary_disabled, view.primary_hint = True, "Catalogue du fournisseur en cours de chargement…"
            elif view.primary == Act.CONNECT and state == DState.BOOTING:
                view.primary_disabled = True
                view.primary_hint = "Le système démarre : « Connecter » s'active dès que RDP répond"
            if srv and state in SERVER_STATES and view.primary != Act.SAVE_CLOSE:
                view.secondary = Act.SAVE_CLOSE
            views.append(view)
        return views

    def _spec(self, d: Desktop) -> str:
        if d.server:
            return f"{d.server.spec} · disque {d.server.disk} Go"
        op = self.runner.for_slug(d.slug)
        if isinstance(op, BuildOp):
            return f"Installation : {op.describe}"
        prefs = self.config.desktops.get(d.slug)
        latest = d.latest or (d.snapshots[0] if d.snapshots else None)
        stype = (prefs.last_type if prefs else None) or (latest.labels.get(L_TYPE) if latest else None)
        loc = (prefs.last_location if prefs else None) or (latest.labels.get(L_LOC) if latest else None)
        parts = [f"Dernier lancement : {stype} · {loc}" if stype and loc else "Jamais lancé ici"]
        if latest:
            parts.append(f"disque {latest.disk_size} Go")
        return " · ".join(parts)

    def _backup_line(self, d: Desktop, now: datetime) -> str:
        latest = d.latest
        if not latest:
            pending = d.pending_snapshot
            if pending:
                return "Première sauvegarde en cours…"
            if isinstance(self.runner.for_slug(d.slug), BuildOp):
                return "Le snapshot de référence sera créé à la fin de l'installation"
            return "Aucune sauvegarde : fermer sans sauvegarder supprimera tout"
        n = sum(1 for s in d.snapshots if s.available)
        extra = f" · {n} versions" if n > 1 else ""
        if d.pinned_count:
            extra += f" ({d.pinned_count} épinglée{'s' if d.pinned_count > 1 else ''})"
        forced = " · arrêt forcé" if latest.forced else ""
        return (f"Dernière sauvegarde {fmt.ago(latest.created, now)} ({fmt.date_short(latest.created)}) · "
                f"{fmt.gb(latest.image_size)}{extra}{forced}")

    def eval_license(self, d: Desktop) -> license.EvalLicense | None:
        """Licence d'évaluation suivie : celle du serveur en cours, sinon celle de la dernière sauvegarde."""
        source = d.server if d.server else d.latest
        return license.from_labels(source.labels) if source else None

    def _note(self, d: Desktop, state: DState) -> str | None:
        srv = d.server
        os_label = "Linux" if d.os == OS_LINUX else "Windows"
        if state in (DState.INTERRUPTED, DState.REMOTE_OP) and srv:
            ts = srv.labels.get(L_OP_TS)
            when = fmt.ago(datetime.fromtimestamp(int(ts), timezone.utc)) if ts and ts.isdigit() else ""
            what = OP_NAMES_FR.get(srv.op, srv.op)
            where = f" sur le poste « {srv.op_host} »" if state == DState.REMOTE_OP else ""
            return f"La {what} a été interrompue{where} {when}. Reprends-la pour éviter des frais inutiles."
        if state == DState.CONFLICT:
            return f"{1 + len(d.extra_servers)} serveurs existent pour ce bureau (tous facturés)."
        if state == DState.UNREACHABLE:
            ip_ok = self.ip_is_allowed_for(d)
            if ip_ok is False:
                return "Ton IP publique n'est pas autorisée sur le pare-feu de ce serveur."
            return f"{os_label} ne répond pas sur le port RDP (mises à jour ? plantage ?). Essaie « Redémarrer »."
        if state == DState.OFF_BILLED:
            return "Un serveur éteint reste facturé : sauvegarde-le et ferme-le, ou redémarre-le."
        if state == DState.BOOTING and srv and srv.firewall_ids and self.ip_is_allowed_for(d) is False:
            return "Ton IP publique n'est pas autorisée sur le pare-feu : RDP restera inaccessible."
        if srv and not srv.firewall_ids and state in SERVER_STATES:
            return "Aucun pare-feu : le port RDP est ouvert à tout Internet."
        if srv and state in SERVER_STATES and self.unapplied_firewalls(d):
            return "Des accès RDP de ce bureau ne sont pas appliqués à son serveur (menu ⋯ → Accès RDP…)."
        return None

    def costs(self) -> CostSummary | None:
        if not self.inventory or not self.static:
            return None
        return compute_costs(self.inventory, self.static.pricing, self._month_sessions)

    # --- pare-feu et IP ---------------------------------------------------------------------------
    # Deux listes d'accès RDP : la commune (tous les bureaux du projet) et, au besoin, une propre à chaque
    # bureau. Hetzner additionne les règles des pare-feux appliqués à un serveur.
    def rdp_firewall(self) -> FirewallInfo | None:
        """Liste d'accès commune à tous les bureaux du projet."""
        return self.inventory.rdp_firewall if self.inventory else None

    def firewalls_of(self, d: Desktop) -> list[FirewallInfo]:
        """Pare-feux qui filtrent le bureau : ceux appliqués à son serveur, sinon ceux qui le seront au lancement."""
        if not self.inventory:
            return []
        if d.server:
            return [f for f in self.inventory.firewalls if f.id in d.server.firewall_ids]
        return [f for f in (self.rdp_firewall(), d.firewall) if f]

    def unapplied_firewalls(self, d: Desktop) -> list[FirewallInfo]:
        """Listes d'accès gérées du bureau pas encore appliquées à son serveur."""
        if not d.server:
            return []
        return [f for f in (self.rdp_firewall(), d.firewall) if f and f.id not in d.server.firewall_ids]

    def rdp_sources_of(self, slug: str | None) -> list[tuple[str, str]]:
        """Adresses d'une liste d'accès : la commune (slug None) ou celle d'un bureau."""
        if slug is None:
            fw = self.rdp_firewall()
        else:
            d = self.desktop(slug)
            fw = d.firewall if d else None
        return netutil.rdp_sources(fw.rules) if fw else []

    def ip_is_allowed_for(self, d: Desktop) -> bool | None:
        fws = self.firewalls_of(d)
        if not self.public_ip or not fws:
            return None
        return any(netutil.ip_allowed(self.public_ip, [c for c, _ in netutil.rdp_sources(f.rules)]) for f in fws)

    def ip_access(self) -> tuple[int, int] | None:
        """(bureaux joignables depuis ton IP, bureaux filtrés par un pare-feu) ; None si rien à évaluer.

        Sans bureau filtré, la liste commune compte pour une unité."""
        if not self.public_ip or not self.inventory:
            return None
        states = [self.ip_is_allowed_for(d) for d in self.grouping.desktops]
        states = [s for s in states if s is not None]
        if states:
            return sum(states), len(states)
        fw = self.rdp_firewall()
        if fw is None:
            return None
        return int(netutil.ip_allowed(self.public_ip, [c for c, _ in netutil.rdp_sources(fw.rules)])), 1

    def ip_is_allowed(self) -> bool | None:
        access = self.ip_access()
        if access is None:
            return None
        ok, total = access
        return ok == total

    def rdp_source_problem(self, slug: str | None, cidr: str) -> str | None:
        """Pourquoi `cidr` ne peut pas être ajouté à la liste visée (doublon, limite), sinon None."""
        if slug is None and self.rdp_firewall() is None and self.grouping.adoptable_firewalls:
            return f"Importe d'abord le pare-feu « {self.grouping.adoptable_firewalls[0].name} » (liste commune)"
        own = self.rdp_sources_of(slug)
        shared = self.rdp_sources_of(None)
        found = netutil.source_conflict(cidr, [c for c, _ in own])
        if found:
            return (f"{cidr} est déjà autorisée dans cette liste" if found == cidr else
                    f"{cidr} est déjà couverte par {found} dans cette liste")
        if slug is not None:
            found = netutil.source_conflict(cidr, [c for c, _ in shared])
            if found:
                return (f"{cidr} est déjà autorisée pour tous les bureaux" if found == cidr else
                        f"{cidr} est déjà couverte par {found}, autorisée pour tous les bureaux")
        else:
            names = [d.name for d in self.grouping.desktops if d.firewall and netutil.source_conflict(
                cidr, [c for c, _ in netutil.rdp_sources(d.firewall.rules)])]
            if names:
                where = ", ".join(f"« {n} »" for n in names)
                return f"{cidr} est déjà autorisée pour {where} : retire-la d'abord de la liste de ce bureau"
        if len(own) >= MAX_FIREWALL_SOURCES:
            return f"Limite de {MAX_FIREWALL_SOURCES} adresses atteinte : retire les anciennes"
        return None

    def firewall_busy(self) -> bool:
        return any(isinstance(op, (FirewallOp, DesktopFirewallOp, CreateFirewallOp, AdoptFirewallOp,
                                   ApplyFirewallOp)) for op in self.runner.active())

    # --- bandeaux et dormants --------------------------------------------------------------------
    def banners(self) -> list[Banner]:
        out: list[Banner] = []
        if self.mode == "fake":
            out.append(Banner("mode", "info", "Mode simulation : aucun appel au fournisseur, temps accéléré."))
        elif self.mode == "readonly":
            out.append(Banner("mode", "info", "Mode lecture seule : aucune modification ne sera envoyée au fournisseur."))
        if self.token_invalid:
            out.append(Banner("api", "error", f"Clé API refusée par {self.project.provider.short} (révoquée ou supprimée) : ce projet n'est "
                              "plus interrogé.", [("Que faire ?", "token_invalid")]))
        elif self.refresh_error:
            out.append(Banner("api", "error", f"{self.project.provider.short} injoignable : {self.refresh_error}",
                              [("Réessayer", "refresh")]))
        if self.inventory is None:
            return out
        g = self.grouping
        for d in g.desktops:
            if self.state_of(d) in (DState.INTERRUPTED, DState.REMOTE_OP):
                out.append(Banner(f"resume:{d.slug}", "warning",
                                  f"Opération interrompue sur « {d.name} » : le serveur est toujours facturé.",
                                  [("Reprendre", f"resume:{d.slug}")]))
        for srv in g.tmp_servers:
            if not self.runner.active():
                out.append(Banner(f"tmp:{srv.id}", "warning",
                                  f"Serveur temporaire orphelin « {srv.name} » (facturé).",
                                  [("Supprimer", f"cleanup_server:{srv.id}")]))
        for srv in g.unmanaged_servers:
            out.append(Banner(f"unmanaged:{srv.id}", "warning",
                              f"Serveur non géré « {srv.name} » ({fmt.status(srv.status)}, {fmt.eur_h(srv.price_hourly)}) : "
                              "il ne sera pas fermé par cette application.",
                              [("Importer…", f"adopt_server:{srv.id}")]))
        fw = self.rdp_firewall()
        if fw is None and g.adoptable_firewalls:
            f = g.adoptable_firewalls[0]
            out.append(Banner(f"adopt_fw:{f.id}", "info",
                              f"Pare-feu « {f.name} » détecté : laisse l'application le gérer "
                              "(ajout d'UDP 3389 pour un RDP plus fluide).", [("Importer", f"adopt_fw:{f.id}")]))
        elif fw is None and not any(d.firewall for d in g.desktops):
            out.append(Banner("no_fw", "warning", "Aucun pare-feu RDP : tes bureaux seraient ouverts à tout Internet.",
                              [("Créer avec mon IP", "create_fw")] if self.public_ip else [("Gérer…", "firewall")]))
        elif (access := self.ip_access()) and access[0] == 0:
            actions = [("Autoriser pour tous les bureaux", "allow_ip")] if fw else []
            out.append(Banner(f"ip:{self.public_ip}", "warning",
                              f"Ton IP publique ({self.public_ip}) n'est autorisée en RDP sur aucun bureau.",
                              actions + [("Gérer…", "firewall")]))
        if g.unmanaged_snapshots:
            n = len(g.unmanaged_snapshots)
            out.append(Banner("import", "info",
                              f"{n} snapshot{'s' if n > 1 else ''} existant{'s' if n > 1 else ''} à importer comme bureau.",
                              [("Importer…", "import")]))
        today = date.today()
        for d in g.desktops:
            lic = self.eval_license(d)
            if lic and license.needs_action(lic, today):
                left = lic.days_left(today)
                when = f"expirée depuis {-left} j" if left < 0 else f"expire dans {left} j"
                out.append(Banner(f"eval:{d.slug}:{lic.expires:%Y%m%d}", "warning",
                                  f"Licence d'évaluation de « {d.name} » : {when}.", [("Que faire ?", f"license:{d.slug}")]))
        dormant = sum(item.monthly for item in self.dormant_items() if item.kind in ("ip", "volume"))
        if dormant > 0:
            out.append(Banner("dormant", "info", f"Ressources dormantes : {fmt.eur_m(dormant)} facturés pour rien.",
                              [("Voir…", "dormant")]))
        return [b for b in out if b.key not in self.dismissed]

    def dormant_items(self) -> list[DormantItem]:
        if not self.inventory or not self.static:
            return []
        p, g, items = self.static.pricing, self.grouping, []
        for ip in g.unassigned_ips:
            items.append(DormantItem(f"ip:{ip.id}", "ip", f"IP {ip.ip} ({ip.location})",
                                     "Non assignée, facturée en permanence", p.ipv4_m(ip.location), ip))
        for vol in g.orphan_volumes:
            items.append(DormantItem(f"vol:{vol.id}", "volume", f"Volume « {vol.name} » · {vol.size} Go ({vol.location})",
                                     "Détaché, n'appartient à aucun bureau", vol.size * p.volume_gb_month, vol))
        for snap in g.unmanaged_snapshots:
            items.append(DormantItem(f"snap:{snap.id}", "snapshot", f"Snapshot « {snap.description or snap.id} » · {fmt.gb(snap.image_size)}",
                                     "Non importé" + (" · protégé" if snap.protected else ""),
                                     snap.size_gb * p.image_gb_month, snap))
        for fw in g.orphan_firewalls:
            items.append(DormantItem(f"fw:{fw.id}", "firewall", f"Liste d'accès « {fw.name} »",
                                     "Reste d'un bureau supprimé (gratuit)", 0.0, fw))
        for srv in g.tmp_servers:
            if not self.runner.active():
                what = "installation Windows" if srv.op == OP_BUILDING else "duplication"
                items.append(DormantItem(f"srv:{srv.id}", "server", f"Serveur temporaire « {srv.name} »",
                                         f"Reste d'une {what} interrompue · {fmt.eur_h(srv.price_hourly)}",
                                         srv.price_monthly, srv))
        return items

    # --- commandes --------------------------------------------------------------------------------
    def _submit(self, op: Operation) -> Operation:
        try:
            self.runner.submit(op)
        except BusyError as exc:
            raise UserError(exc.message) from None
        if op.slug:
            self.errors.pop(op.slug, None)
        self._regroup()
        if self.ui:
            self.ui.on_model_changed()
        return op

    def _require(self, slug: str) -> Desktop:
        d = self.desktop(slug)
        if d is None:
            raise UserError("Bureau introuvable (rafraîchis la liste)")
        return d

    def launch(self, slug: str, name: str, params: LaunchParams) -> Operation:
        d = self.desktop(slug)
        if self.runner.for_slug(slug) or self.launch_pending(slug) or (d and d.server):
            raise UserError(f"« {name} » est déjà lancé ou en cours de lancement")
        return self._submit(LaunchOp(self.ctx, slug, name, params))

    def save(self, slug: str, close: bool, quit_mode: bool = False) -> Operation:
        d = self._require(slug)
        if not d.server:
            raise UserError("Ce bureau n'a pas de serveur en cours")
        return self._submit(SaveOp(self.ctx, slug, d.name, d.server.id, close,
                                   d.fixed_ip.id if d.fixed_ip else None, quit_mode))

    def discard(self, slug: str) -> Operation:
        d = self._require(slug)
        if not d.server:
            raise UserError("Ce bureau n'a pas de serveur en cours")
        return self._submit(DiscardOp(self.ctx, slug, d.name, d.server.id, d.fixed_ip.id if d.fixed_ip else None))

    def resume(self, slug: str) -> Operation:
        d = self._require(slug)
        return self._submit(ResumeOp(self.ctx, slug, d.name, d.server, d.fixed_ip.id if d.fixed_ip else None))

    def ignore_op(self, slug: str) -> Operation:
        d = self._require(slug)
        return self._submit(ClearOpLabelsOp(self.ctx, slug, d.name, d.server.id))

    def power(self, slug: str, action: str) -> Operation:
        d = self._require(slug)
        return self._submit(PowerOp(self.ctx, slug, d.name, d.server.id, action))

    def cancel_op(self, slug: str) -> bool:
        op = self.runner.for_slug(slug)
        return bool(op and op.request_cancel())

    def duplicate(self, slug: str, snapshot: SnapshotInfo, new_name: str, mode: str,
                  params: LaunchParams | None = None) -> Operation:
        new_slug = unique_slug(slugify(new_name), self.known_slugs())
        if mode == "launch":
            self.config.copy_prefs(slug, new_slug, new_name)
            self.creds.copy_password(slug, new_slug)
            return self._submit(LaunchOp(self.ctx, new_slug, new_name, params))
        return self._submit(DuplicateOp(self.ctx, slug, snapshot, new_slug, new_name))

    def build_windows(self, name: str, params: BuildParams) -> Operation:
        """Crée un bureau à partir de rien : installation Windows sans surveillance puis snapshot."""
        if self.mode == "readonly":
            raise UserError("Mode lecture seule : impossible de créer un serveur")
        slug = unique_slug(slugify(name), self.known_slugs())
        return self._submit(BuildOp(self.ctx, slug, name, params))

    def add_volume(self, slug: str, size: int, volume_name: str) -> Operation:
        d = self._require(slug)
        return self._submit(AddVolumeOp(self.ctx, slug, d.name, d.server.id, size, volume_name))

    def volume_action(self, slug: str | None, volume: VolumeInfo, action: str, size: int | None = None) -> Operation:
        d = self.desktop(slug) if slug else None
        server_id = d.server.id if d and d.server else None
        return self._submit(VolumeActionOp(self.ctx, slug, d.name if d else volume.name, volume, action,
                                           server_id, size))

    def firewall_change(self, firewall_id: int, add: list[tuple[str, str]] = (), remove: list[str] = ()) -> Operation:
        return self._submit(FirewallOp(self.ctx, firewall_id, add, remove))

    def add_rdp_source(self, slug: str | None, raw: str, description: str) -> Operation:
        """Autorise une adresse sur la liste commune (slug None) ou sur celle d'un bureau (créée au besoin)."""
        cidr = netutil.normalize_cidr(raw)
        problem = self.rdp_source_problem(slug, cidr)
        if problem:
            raise UserError(problem)
        desc = (description or "").strip()[:60] or "Accès"
        if slug is None:
            fw = self.rdp_firewall()
            if fw is None:
                return self.create_firewall(cidr, desc)
            return self._submit(FirewallOp(self.ctx, fw.id, add=[(cidr, desc)]))
        d = self._require(slug)
        if d.firewall:
            return self._submit(FirewallOp(self.ctx, d.firewall.id, add=[(cidr, desc)], slug=slug, name=d.name))
        if any(isinstance(op, DesktopFirewallOp) and op.slug == slug for op in self.runner.active()):
            raise UserError("La liste d'accès de ce bureau est en cours de création : patiente un instant")
        return self._submit(DesktopFirewallOp(self.ctx, slug, d.name, cidr, desc, d.server.id if d.server else None))

    def remove_rdp_source(self, slug: str | None, cidr: str) -> Operation:
        if slug is None:
            fw = self.rdp_firewall()
            if not fw:
                raise UserError("Aucune liste d'accès commune")
            return self.firewall_change(fw.id, remove=[cidr])
        d = self._require(slug)
        if not d.firewall:
            raise UserError("Ce bureau n'a pas de liste d'accès propre")
        return self._submit(FirewallOp(self.ctx, d.firewall.id, remove=[cidr], slug=slug, name=d.name))

    def allow_current_ip(self, slug: str | None = None, description: str = "Mon IP") -> Operation:
        if not self.public_ip:
            raise UserError("IP publique inconnue")
        return self.add_rdp_source(slug, self.public_ip, description)

    def create_firewall(self, cidr: str, description: str) -> Operation:
        server_ids = [d.server.id for d in self.grouping.desktops if d.server and not d.server.firewall_ids]
        return self._submit(CreateFirewallOp(self.ctx, cidr, description, server_ids))

    def adopt_firewall(self, firewall_id: int) -> Operation:
        fw = self.inventory.firewall(firewall_id)
        server_ids = [d.server.id for d in self.grouping.desktops if d.server and not d.server.firewall_ids]
        return self._submit(AdoptFirewallOp(self.ctx, fw, server_ids))

    def apply_firewall(self, slug: str) -> Operation:
        d = self._require(slug)
        missing = self.unapplied_firewalls(d)
        if not d.server or not missing:
            raise UserError("Rien à appliquer : les listes d'accès du bureau sont déjà en place")
        return self._submit(ApplyFirewallOp(self.ctx, slug, d.name, [f.id for f in missing], d.server.id))

    def fixed_ip(self, slug: str, mode: str, location: str | None = None, ip: PrimaryIpInfo | None = None) -> Operation:
        d = self._require(slug)
        return self._submit(FixedIpOp(self.ctx, slug, d.name, mode, location, ip))

    def adopt_snapshot(self, snapshot: SnapshotInfo, name: str, rdp_user: str, password: str | None,
                       pin: bool) -> Operation:
        slug = unique_slug(slugify(name), self.known_slugs())
        self._store_identity(slug, name, rdp_user, password)
        return self._submit(AdoptSnapshotOp(self.ctx, slug, name, snapshot, pin))

    def adopt_server(self, server: ServerInfo, name: str, rdp_user: str, password: str | None) -> Operation:
        slug = unique_slug(slugify(name), self.known_slugs())
        self._store_identity(slug, name, rdp_user, password)
        return self._submit(AdoptServerOp(self.ctx, slug, name, server))

    def _store_identity(self, slug: str, name: str, rdp_user: str, password: str | None) -> None:
        self.config.update_prefs(slug, display_name=name, rdp_user=rdp_user or "Administrator")
        if password:
            self.creds.set_password(slug, password)

    def rename(self, slug: str, new_name: str) -> Operation:
        d = self._require(slug)
        return self._submit(RenameOp(self.ctx, slug, new_name, list(d.snapshots)))

    def snapshot_action(self, slug: str, snapshot: SnapshotInfo, action: str) -> Operation:
        d = self._require(slug)
        return self._submit(SnapshotActionOp(self.ctx, slug, d.name, snapshot, action))

    def delete_desktop(self, slug: str, delete_volumes: bool, delete_ip: bool) -> Operation:
        d = self._require(slug)
        if d.server:
            raise UserError("Ferme d'abord le serveur de ce bureau")
        volumes = [v for v in d.volumes if v.server_id is None] if delete_volumes else []
        return self._submit(DeleteDesktopOp(self.ctx, slug, d.name, list(d.snapshots), volumes,
                                            d.fixed_ip if delete_ip else None, forget_password=True,
                                            firewall=d.firewall))

    def cleanup(self, item: DormantItem) -> Operation:
        return self._submit(CleanupOp(self.ctx, item.kind, item.resource, item.title))

    def mark_eval_auto(self, slug: str) -> Operation:
        """La tâche de prolongation a été installée à la main dans la session en cours."""
        d = self._require(slug)
        if not d.server:
            raise UserError("Lance d'abord le bureau : la tâche s'installe dans Windows")
        return self._submit(EvalAutoOp(self.ctx, slug, d.name, d.server.id))

    def set_credentials(self, slug: str, user: str, password: str | None, forget: bool = False) -> None:
        self.config.update_prefs(slug, rdp_user=user or "Administrator")
        if forget:
            self.creds.delete_password(slug)
            self._pw_known[slug] = False
        elif password:
            self.creds.set_password(slug, password)
            self._pw_known[slug] = True
            d = self.desktop(slug)
            if d and d.server and d.server.ipv4 and rdp.write_termsrv_cred(d.server.ipv4, user, password):
                self.config.add_cred(d.server.ipv4, slug)

    def connect(self, slug: str) -> None:
        d = self._require(slug)
        srv = d.server
        if not srv or not srv.ipv4:
            raise UserError("Ce bureau n'a pas d'IP publique")
        user = self.config.prefs(slug).rdp_user
        password = self.creds.get_password(slug)
        if password and rdp.write_termsrv_cred(srv.ipv4, user, password):
            self.config.add_cred(srv.ipv4, slug)
        rdp.launch_mstsc(rdp.write_rdp_file(slug, srv.ipv4, user))
        self._log("info", f"Connexion RDP à {srv.ipv4} ({user})", slug)
        if not password and self.ui:
            self.ui.toast("Astuce : enregistre le mot de passe (Identifiants RDP…) pour la connexion en 1 clic.", "info")

    def known_slugs(self) -> set[str]:
        slugs = {d.slug for d in self.grouping.desktops} | set(self.config.desktops)
        slugs |= {op.slug for op in self.runner.active() if op.slug}
        return slugs

    def location_offers(self, snapshot: SnapshotInfo) -> dict:
        if not self.static:
            return {}
        return best_by_location(self.static.server_types, list(self.static.locations), snapshot.disk_size,
                                snapshot.architecture)

    # --- fermeture de l'application --------------------------------------------------------------
    def billed_servers(self) -> list[ServerInfo]:
        return list(self.inventory.servers) if self.inventory else []

    def quit_candidates(self) -> list[str]:
        return [d.slug for d in self.grouping.desktops if d.server or self.runner.for_slug(d.slug)]

    def quit_step(self, qs: QuitState) -> tuple[list[QuitRow], bool]:
        """Fait avancer « Tout sauvegarder et quitter » ; appelé périodiquement par la fenêtre.

        Renvoie les lignes à afficher et vrai quand on peut quitter : tout est fermé ET l'inventaire
        a été relu après la dernière opération (un serveur tout juste créé ne peut pas passer inaperçu).
        """
        for slug in self.quit_candidates():
            if slug not in qs.slugs:
                qs.slugs.append(slug)
        rows, all_done = [], True
        for slug in qs.slugs:
            d = self.desktop(slug)
            name = d.name if d else slug
            if slug in qs.skipped:
                rows.append(QuitRow(slug, name, "skipped", "Laissé allumé (toujours facturé)"))
                continue
            op = self.runner.for_slug(slug)
            if op:
                rows.append(QuitRow(slug, name, "running", op.phase,
                                    op.progress / 100 if op.progress is not None else None))
                all_done = False
                continue
            mine = qs.attempted.get(slug)
            if mine is not None:
                if mine.outcome is None:
                    rows.append(QuitRow(slug, name, "running", mine.phase))
                    all_done = False
                    continue
                if mine.outcome != "ok":
                    err = mine.error or UserError("Opération annulée")
                    rows.append(QuitRow(slug, name, "failed", err.message, error=err))
                    all_done = False
                    continue
                gone = d is None or d.server is None or d.server.id == getattr(mine, "server_id", None)
                if mine.closes_server and gone:
                    rows.append(QuitRow(slug, name, "done", "Sauvegardé, serveur supprimé"
                                        if mine.kind == "save" else "Serveur supprimé"))
                    continue
                del qs.attempted[slug]
            if d is None or d.server is None:
                rows.append(QuitRow(slug, name, "done", "Aucun serveur"))
                continue
            state = self.state_of(d)
            try:
                if state == DState.CONFLICT:
                    raise UserError("Plusieurs serveurs pour ce bureau : résous le conflit")
                if state in (DState.INTERRUPTED, DState.REMOTE_OP):
                    op = self.resume(slug)
                else:
                    op = self.save(slug, close=True, quit_mode=True)
            except UserError as exc:
                rows.append(QuitRow(slug, name, "failed", exc.message, error=exc))
                all_done = False
                continue
            qs.attempted[slug] = op
            rows.append(QuitRow(slug, name, "running", op.phase))
            all_done = False
        fresh = self._last_refresh_started > self._invalidated_at
        if all_done and not fresh:
            if not self._refresh_inflight:
                self.refresh_now()
            all_done = False
        return rows, all_done

    def quit_retry(self, qs: QuitState, slug: str) -> None:
        qs.attempted.pop(slug, None)
        self.errors.pop(slug, None)

    def quit_discard(self, qs: QuitState, slug: str) -> None:
        qs.attempted[slug] = self.discard(slug)

    # --- projet ---------------------------------------------------------------------------------------
    def key(self, slug: str) -> str:
        """Identifiant d'un bureau dans l'interface, unique entre projets."""
        return f"{self.project.id}/{slug}"

    def _token_rejected(self) -> None:
        """Clé révoquée ou supprimée : on arrête d'interroger le fournisseur et l'interface propose de l'oublier."""
        if self.token_invalid:
            return
        self.token_invalid = True
        self._log("error", f"Clé API du projet « {self.project.name} » refusée par {self.project.provider.short}")
        if self.ui:
            self.ui.on_token_invalid()

    def shutdown(self) -> None:
        self.hard_stop.set()
        self.runner.shutdown()
        self._bg.shutdown(wait=False, cancel_futures=True)
        try:
            self.config.save()
        except OSError:
            log.exception("Sauvegarde de la configuration impossible")
