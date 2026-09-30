"""Fermeture de l'application avec des serveurs encore facturés."""

from __future__ import annotations

from datetime import datetime, timezone

import customtkinter as ctk

from ... import fmt
from ...pricing import server_burn, session_cost
from .. import theme as t
from ..widgets import caption, ghost_button, label
from .base import Modal, confirm, confirm_typed

STATUS_STYLE = {"running": ("●", "info"), "done": ("✓", "success"), "failed": ("✕", "danger"),
                "skipped": ("!", "warning")}


class QuitDialog(Modal):
    """Choix : Annuler / Quitter quand même / Tout sauvegarder et quitter."""

    def __init__(self, app) -> None:
        super().__init__(app, "Des serveurs sont encore allumés", width=720)
        hub = app.hub
        servers = hub.billed_servers()
        ops = [op for _, op in hub.active_ops()]
        now = datetime.now(timezone.utc)
        n = len(servers)
        self.heading(f"{n} serveur{'s' if n > 1 else ''} toujours facturé{'s' if n > 1 else ''}" if n
                     else "Des opérations sont en cours",
                     "Un serveur est facturé tant qu'il existe, même éteint et même si l'application est fermée.")
        if servers:
            table = ctk.CTkFrame(self.body, fg_color=t.SURFACE, corner_radius=10, border_width=1,
                                 border_color=t.BORDER)
            table.pack(fill="x", pady=(14, 0))
            heads = ["Bureau", "Type · lieu", "État", "Allumé depuis", "€/h", "Session"]
            for col, text_ in enumerate(heads):
                caption(table, text_).grid(row=0, column=col, sticky="w", padx=10, pady=(10, 4))
            total = 0.0
            for row, (ctrl, srv) in enumerate(servers, 1):
                pricing = ctrl.static.pricing if ctrl.static else None
                d = next((d for d in ctrl.grouping.desktops if d.server and d.server.id == srv.id), None)
                name = d.name if d else f"{srv.name} (non géré)"
                if hub.multi:
                    name = f"{name} · {ctrl.project.name}"
                burn = server_burn(srv, pricing) if pricing else srv.price_hourly
                total += burn
                cells = [name, f"{srv.server_type} · {srv.location}", fmt.status(srv.status),
                         fmt.duration((now - srv.created).total_seconds()), fmt.eur_h(burn),
                         fmt.eur(session_cost(srv, pricing, now)) if pricing else "—"]
                for col, text_ in enumerate(cells):
                    label(table, text_, 12, "bold" if col == 0 else "normal",
                          color=t.TEXT if col == 0 else t.MUTED).grid(row=row, column=col, sticky="w", padx=10,
                                                                     pady=3)
            label(table, f"Total : {fmt.eur_h(total)} · ≈ {fmt.eur(total * 24)} par jour si rien n'est fait", 12,
                  "bold", color=t.tone("warning")[0]).grid(row=len(servers) + 1, column=0, columnspan=6,
                                                          sticky="w", padx=10, pady=(6, 10))
            unmanaged = [s for _, s in servers if not s.managed]
            if unmanaged:
                self.notice("Les serveurs non gérés ne seront pas touchés et resteront allumés.", "warning")
        if ops:
            self.section("Opérations en cours")
            for op in ops:
                label(self.body, f"•  {op.title} « {op.name} » — {op.phase}", 12, color=t.MUTED).pack(fill="x")
        self.buttons(("Annuler", self.cancel, "secondary"),
                     ("Quitter quand même", lambda: self.close("force"), "danger_outline"),
                     ("Tout sauvegarder et quitter", lambda: self.close("save_all"), "primary"))


class QuitProgressDialog(Modal):
    """Sauvegarde de tous les bureaux, puis fermeture automatique si tout a réussi."""

    def __init__(self, app) -> None:
        super().__init__(app, "Sauvegarde avant fermeture", width=640)
        self.app, self.hub = app, app.hub
        self.states = self.hub.new_quit_states()
        self.protocol("WM_DELETE_WINDOW", self._quit_now)
        self.unbind("<Escape>")
        self.heading("Sauvegarde de tous les bureaux",
                     "Chaque bureau est arrêté proprement, sauvegardé puis supprimé. L'application se fermera "
                     "toute seule une fois terminé.")
        self.rows_frame = ctk.CTkFrame(self.body, fg_color="transparent", height=1)
        self.rows_frame.pack(fill="x", pady=(14, 0))
        self.status = label(self.body, "", 12, "bold", color=t.MUTED)
        self.status.pack(fill="x", pady=(12, 0))
        self.buttons(("Revenir à l'application", self._back, "secondary"),
                     ("Quitter maintenant", self._quit_now, "danger_outline"))
        self._signature = None
        self._done_at = None
        self.after(200, self._step)

    def _step(self) -> None:
        if not self.winfo_exists():
            return
        rows, done = self.hub.quit_step(self.states)
        signature = [(r.slug, r.status, r.detail, round(r.progress or 0, 2), r.error.message if r.error else None)
                     for r in rows]
        if signature != self._signature:
            self._signature = signature
            self._render(rows)
        if done:
            skipped = sum(len(qs.skipped) for qs in self.states.values())
            self.status.configure(text="Tout est sauvegardé — 0,00 €/h. Fermeture…" if not skipped else
                                  f"Terminé ({skipped} bureau{'x' if skipped > 1 else ''} laissé"
                                  f"{'s' if skipped > 1 else ''} allumé{'s' if skipped > 1 else ''}). Fermeture…",
                                  text_color=t.tone("success")[0])
            self.after(1500, self.app.hard_exit)
            return
        failed = sum(1 for r in rows if r.status == "failed")
        self.status.configure(text=f"{failed} échec(s) : choisis quoi faire pour chaque bureau." if failed
                              else "Sauvegarde en cours… ne coupe pas Internet.",
                              text_color=t.tone("danger")[0] if failed else t.MUTED)
        self.after(500, self._step)

    def _render(self, rows) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        if not rows:
            label(self.rows_frame, "Vérification de l'inventaire…", 12, color=t.MUTED).pack(fill="x")
        for r in rows:
            icon, kind = STATUS_STYLE.get(r.status, ("●", "muted"))
            box = ctk.CTkFrame(self.rows_frame, fg_color=t.SURFACE, corner_radius=10, border_width=1,
                               border_color=t.tone(kind)[0] if r.status == "failed" else t.BORDER)
            box.pack(fill="x", pady=4)
            box.grid_columnconfigure(1, weight=1)
            label(box, icon, 14, "bold", color=t.tone(kind)[0]).grid(row=0, column=0, rowspan=2, padx=(12, 8),
                                                                      pady=8)
            label(box, r.name, 13, "bold").grid(row=0, column=1, sticky="w", pady=(8, 0))
            label(box, r.detail, 12, color=t.MUTED, wraplength=460).grid(row=1, column=1, sticky="w", pady=(0, 8))
            if r.status == "running":
                bar = ctk.CTkProgressBar(box, height=6, width=140, progress_color=t.ACCENT,
                                         mode="determinate" if r.progress is not None else "indeterminate")
                bar.grid(row=0, column=2, rowspan=2, padx=12)
                if r.progress is not None:
                    bar.set(r.progress)
                else:
                    bar.start()
            elif r.status == "failed":
                actions = ctk.CTkFrame(box, fg_color="transparent")
                actions.grid(row=2, column=1, columnspan=2, sticky="w", pady=(0, 8))
                for text_, cmd in (("Réessayer", lambda s=r.slug: self._retry(s)),
                                   ("Fermer sans sauvegarder…", lambda s=r.slug, n=r.name: self._discard(s, n)),
                                   ("Laisser allumé", lambda s=r.slug: self._skip(s))):
                    ghost_button(actions, text_, cmd, width=10, text_color=t.tone("danger")[0],
                                 font=t.font(12, "bold")).pack(side="left", padx=2)
        self.refit()

    def _retry(self, key: str) -> None:
        self.hub.quit_retry(self.states, key)
        self._signature = None

    def _discard(self, key: str, name: str) -> None:
        if confirm_typed(self, "Fermer sans sauvegarder", "Toutes les modifications depuis la dernière sauvegarde "
                         "de ce bureau seront définitivement perdues.", name, "Supprimer le serveur"):
            self.hub.quit_discard(self.states, key)
            self._signature = None

    def _skip(self, key: str) -> None:
        self.hub.quit_skip(self.states, key)
        self._signature = None

    def _back(self) -> None:
        self.close(None)

    def _quit_now(self) -> None:
        ok, _ = confirm(self, "Arrêter d'attendre ?", "Les opérations en cours s'arrêteront et les serveurs non "
                        "encore supprimés resteront facturés. Au prochain démarrage, l'application proposera de "
                        "reprendre.", "Quitter maintenant", danger=True)
        if ok:
            self.app.hard_exit()


class DecisionDialog(Modal):
    """Question posée par une opération en cours (avec compte à rebours éventuel)."""

    def __init__(self, app, event) -> None:
        super().__init__(app, event.title, width=540)
        self.event = event
        self.heading(event.title)
        self.text(event.message, size=12)
        self.countdown = label(self.body, "", 12, "bold", color=t.tone("warning")[0])
        self.countdown.pack(fill="x", pady=(10, 0))
        specs = []
        for key, text_, style in event.options:
            specs.append((text_, lambda k=key: self._choose(k), style if style in ("primary", "danger") else "secondary"))
        self.buttons(*specs)
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose(event.default))
        self.unbind("<Escape>")
        self.remaining = event.countdown_s
        if self.remaining:
            self._tick()

    def _tick(self) -> None:
        if not self.winfo_exists() or self.event.future.done():
            return
        if self.remaining <= 0:
            self._choose(self.event.default)
            return
        default = next((txt for k, txt, _ in self.event.options if k == self.event.default), "")
        self.countdown.configure(text=f"Choix automatique « {default} » dans {self.remaining} s")
        self.remaining -= 1
        self.after(1000, self._tick)

    def _choose(self, key) -> None:
        if not self.event.future.done():
            self.event.future.set_result(key if key is not None else self.event.options[0][0])
        self.close(key)
