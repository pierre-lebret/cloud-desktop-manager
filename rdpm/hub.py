"""ProjectHub : plusieurs projets Hetzner (une clé API chacun) affichés ensemble.

Chaque projet a son AppController (inventaire, opérations, bandeaux) ; le hub les rassemble pour la fenêtre :
cartes de tous les projets, coûts additionnés, bandeaux préfixés, « Tout sauvegarder et quitter » global.
Un bureau est désigné dans l'interface par sa clé « <projet>/<slug> ».
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Callable

from .controller import AppController, Banner, DesktopView, QuitRow, QuitState
from .models import ServerInfo
from .ops.base import Operation
from .pricing import CostSummary
from .projects import ProjectSpec


class ProjectHub:
    def __init__(self, factory: Callable[[ProjectSpec], AppController], verify: Callable[[str], None],
                 mode: str = "live") -> None:
        self.factory, self.verify, self.mode = factory, verify, mode
        self.controllers: list[AppController] = []
        self.ui = None   # fenêtre principale : fournit à chaque contrôleur sa vue « projet »
        self._bg = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub")

    # --- projets ----------------------------------------------------------------------------------
    def add(self, spec: ProjectSpec) -> AppController:
        ctrl = self.factory(spec)
        self.controllers.append(ctrl)
        if self.ui:
            self.ui.attach(ctrl)
        return ctrl

    def remove(self, pid: str) -> None:
        ctrl = self.get(pid)
        if ctrl:
            self.controllers.remove(ctrl)
            ctrl.shutdown()

    def get(self, pid: str) -> AppController | None:
        return next((c for c in self.controllers if c.project.id == pid), None)

    def split(self, key: str) -> tuple[AppController | None, str]:
        pid, _, slug = key.partition("/")
        return self.get(pid), slug

    def find_token(self, token: str) -> AppController | None:
        return next((c for c in self.controllers if c.project.token == token), None)

    def check_token(self, token: str) -> Future:
        return self._bg.submit(self.verify, token)

    @property
    def locked(self) -> bool:
        return not self.controllers

    @property
    def multi(self) -> bool:
        return len(self.controllers) > 1

    @property
    def active(self) -> list[AppController]:
        return [c for c in self.controllers if not c.token_invalid]

    # --- boucle -------------------------------------------------------------------------------------
    def pump(self) -> None:
        for ctrl in list(self.controllers):
            ctrl.pump()

    def tick(self) -> None:
        for ctrl in list(self.controllers):
            ctrl.tick()

    def refresh_now(self) -> None:
        for ctrl in self.active:
            ctrl.refresh_now()

    def shutdown(self) -> None:
        for ctrl in self.controllers:
            ctrl.shutdown()
        self._bg.shutdown(wait=False, cancel_futures=True)

    # --- vues agrégées -----------------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        """Chaque projet valide a été lu au moins une fois (ou a répondu par une erreur)."""
        return all(c.inventory is not None or c.refresh_error is not None for c in self.active)

    def desktop_views(self) -> list[DesktopView]:
        views = []
        for ctrl in self.controllers:
            if not ctrl.inventory:
                continue
            for v in ctrl.desktop_views():
                views.append(replace(v, spec=f"{ctrl.project.name} · {v.spec}") if self.multi else v)
        return views

    def desktop_count(self) -> int:
        return sum(len(c.grouping.desktops) for c in self.controllers if c.inventory)

    def costs(self) -> CostSummary | None:
        parts = [(c, c.costs()) for c in self.active]
        parts = [(c, s) for c, s in parts if s]
        if not parts:
            return None
        # Les sessions terminées du mois (fichier local commun) sont comptées dans chaque estimation.
        shared = parts[0][0]._month_sessions
        return CostSummary(
            running_h=sum(s.running_h for _, s in parts), running_count=sum(s.running_count for _, s in parts),
            storage_month=sum(s.storage_month for _, s in parts),
            fixed_ips_month=sum(s.fixed_ips_month for _, s in parts), idle_month=sum(s.idle_month for _, s in parts),
            month_estimate=sum(s.month_estimate for _, s in parts) - shared * (len(parts) - 1))

    @property
    def public_ip(self) -> str | None:
        return next((c.public_ip for c in self.controllers if c.public_ip), None)

    def ip_is_allowed(self) -> bool | None:
        states = [c.ip_is_allowed() for c in self.active if c.inventory]
        if any(s is False for s in states):
            return False
        return True if states and all(s is True for s in states) else None

    def last_refresh(self) -> float | None:
        stamps = [c.last_refresh for c in self.active if c.last_refresh]
        return min(stamps) if stamps else None

    def dormant_count(self) -> int:
        return sum(len(c.dormant_items()) for c in self.active if c.inventory)

    def banners(self) -> list[Banner]:
        out = []
        for ctrl in self.controllers:
            for b in ctrl.banners():
                pid = ctrl.project.id
                text = f"{ctrl.project.name} — {b.text}" if self.multi and b.key != "mode" else b.text
                out.append(Banner(f"{pid}|{b.key}", b.kind, text, [(t, f"{pid}|{k}") for t, k in b.actions]))
        # un seul bandeau « mode simulation / lecture seule »
        seen, unique = set(), []
        for b in out:
            inner = b.key.split("|", 1)[1]
            if inner == "mode":
                if "mode" in seen:
                    continue
                seen.add("mode")
            unique.append(b)
        return unique

    def dismiss(self, key: str) -> None:
        pid, _, inner = key.partition("|")
        ctrl = self.get(pid)
        if ctrl:
            ctrl.dismissed.add(inner)

    # --- fermeture ------------------------------------------------------------------------------------
    def billed_servers(self) -> list[tuple[AppController, ServerInfo]]:
        return [(c, s) for c in self.controllers for s in c.billed_servers()]

    def active_ops(self) -> list[tuple[AppController, Operation]]:
        return [(c, op) for c in self.controllers for op in c.runner.active()]

    def new_quit_states(self) -> dict[str, QuitState]:
        return {c.project.id: QuitState() for c in self.active}

    def quit_step(self, states: dict[str, QuitState]) -> tuple[list[QuitRow], bool]:
        rows, done = [], True
        for ctrl in self.active:
            qs = states.setdefault(ctrl.project.id, QuitState())
            sub_rows, sub_done = ctrl.quit_step(qs)
            done &= sub_done
            for r in sub_rows:
                name = f"{r.name} ({ctrl.project.name})" if self.multi else r.name
                rows.append(replace(r, slug=ctrl.key(r.slug), name=name))
        return rows, done

    def quit_retry(self, states: dict[str, QuitState], key: str) -> None:
        ctrl, slug = self.split(key)
        if ctrl:
            ctrl.quit_retry(states[ctrl.project.id], slug)

    def quit_discard(self, states: dict[str, QuitState], key: str) -> None:
        ctrl, slug = self.split(key)
        if ctrl:
            ctrl.quit_discard(states[ctrl.project.id], slug)

    def quit_skip(self, states: dict[str, QuitState], key: str) -> None:
        ctrl, slug = self.split(key)
        if ctrl:
            states[ctrl.project.id].skipped.add(slug)
