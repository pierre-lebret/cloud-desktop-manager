"""Fenêtre principale : bureaux de tous les projets (une clé API d'un fournisseur cloud par projet)."""

from __future__ import annotations

import logging
import os
import time
import tkinter as tk
from datetime import datetime

import customtkinter as ctk

from .. import fmt, notify
from ..constants import APP_TITLE
from ..controller import AppController
from ..hetzner.errors import UserError
from ..hub import ProjectHub
from ..models import Desktop, SnapshotInfo
from ..ops.launch import LaunchParams
from ..projects import ProjectStore
from ..state import Act
from . import theme as t
from .desktop_card import DesktopCard
from .icons import OS_NAMES, set_window_icon
from .dialogs.base import ask_text, confirm, confirm_typed, info, top_window
from .dialogs.build import BuildDialog, BuildDoneDialog, LicenseDialog
from .dialogs.launch import LaunchDialog
from .dialogs.manage import (
    AddVolumeDialog, ConflictDialog, CredentialsDialog, DeleteDesktopDialog, DuplicateDialog, FirewallDialog,
    FixedIpDialog, SnapshotsDialog, VolumesDialog, resize_help, volume_help,
)
from .dialogs.misc import AdoptDialog, DormantDialog, ImportChooser, SettingsDialog
from .dialogs.quit import DecisionDialog, QuitDialog, QuitProgressDialog
from .token_bar import ProjectsBar
from .widgets import (
    Banner, ToastManager, Tooltip, caption, card_frame, ghost_button, label, logical, primary_button,
    secondary_button,
)

log = logging.getLogger(__name__)

LEVEL_COLORS = {"info": t.MUTED, "warning": t.tone("warning")[0], "error": t.tone("danger")[0],
                "success": t.tone("success")[0]}


class StatTile(ctk.CTkFrame):
    def __init__(self, master, title: str) -> None:
        super().__init__(master, fg_color=t.SURFACE, corner_radius=t.RADIUS, border_width=1, border_color=t.BORDER)
        caption(self, title).pack(fill="x", padx=16, pady=(12, 0))
        self.value = label(self, "—", 20, "bold")
        self.value.pack(fill="x", padx=16)
        self.sub = label(self, "", 11, color=t.MUTED)
        self.sub.pack(fill="x", padx=16, pady=(0, 12))
        self._state = None
        self.bind("<Configure>", lambda e: self.sub.configure(wraplength=max(80, int(logical(self, e.width)) - 32)))

    def set(self, value: str, sub: str, kind: str | None = None) -> None:
        if self._state == (value, sub, kind):
            return
        self._state = (value, sub, kind)
        self.value.configure(text=value, text_color=t.tone(kind)[0] if kind else t.TEXT)
        self.sub.configure(text=sub)


class ProjectScope:
    """La fenêtre vue par un projet : c'est l'« app » que reçoivent les dialogues (app.controller = ce projet).

    Relaie aussi les rappels du contrôleur vers la fenêtre en y ajoutant le projet concerné."""

    def __init__(self, window: "MainWindow", controller: AppController) -> None:
        self.tk_root, self.controller = window, controller

    def __getattr__(self, name):
        return getattr(self.tk_root, name)

    # rappels du contrôleur
    def on_model_changed(self) -> None:
        self.tk_root.on_model_changed()

    def on_log(self, level: str, message: str, slug: str | None) -> None:
        who = slug if not self.tk_root.hub.multi else \
            (f"{self.controller.project.name}/{slug}" if slug else self.controller.project.name)
        self.tk_root.on_log(level, message, who)

    def on_decision(self, event) -> None:
        self.tk_root.on_decision(event)

    def on_ready(self, desktop: Desktop) -> None:
        self.tk_root.on_ready(self, desktop)

    def on_reminder(self, desktop: Desktop, uptime_h: float, cost: float) -> None:
        self.tk_root.on_reminder(self, desktop, uptime_h, cost)

    def on_followup(self, op) -> None:
        self.tk_root.on_followup(self, op)

    def on_token_invalid(self) -> None:
        self.tk_root.after_idle(lambda: self.tk_root.on_token_invalid(self))

    # actions demandées par les dialogues (slug du projet)
    def handle_action(self, slug: str, act: Act) -> None:
        self.tk_root.handle_action(self.controller.key(slug), act)

    def open_launch(self, d: Desktop | None, *args, **kwargs) -> None:
        self.tk_root.open_launch(self, d, *args, **kwargs)


class MainWindow(ctk.CTk):
    def __init__(self, hub: ProjectHub, store: ProjectStore | None = None, on_secret=None) -> None:
        super().__init__(fg_color=t.BG)
        self.hub, self.store, self.on_secret = hub, store, on_secret
        hub.ui = self
        self._scopes: dict[str, ProjectScope] = {}
        for ctrl in hub.controllers:
            self.attach(ctrl)
        self.title(APP_TITLE)
        set_window_icon(self)
        self.geometry("1220x820")
        self.minsize(720, 540)
        self.protocol("WM_DELETE_WINDOW", self.request_quit)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.toasts = ToastManager(self)
        self.cards: dict[str, DesktopCard] = {}
        self._layout_sig = None
        self._banner_sig = None
        self._import_sig = None
        self._render_pending = False
        self._log_open = False
        self._quitting = False
        self._reflow_sig = None

        self.projects_bar = ProjectsBar(self)
        self.projects_bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(14, 0))
        self._build_header()
        self._build_content()
        self._build_log()
        self._bind_keys()
        self.bind("<Configure>", self._on_window_resize, add="+")
        self.after(100, self._pump)
        self.after(200, self._tick)
        self.set_locked(hub.locked)

    # --- projets ------------------------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self.hub.mode

    def attach(self, ctrl: AppController) -> None:
        scope = self._scopes[ctrl.project.id] = ProjectScope(self, ctrl)
        ctrl.ui = scope

    def scope(self, ctrl: AppController) -> ProjectScope:
        if ctrl.project.id not in self._scopes:
            self.attach(ctrl)
        return self._scopes[ctrl.project.id]

    def with_project(self, anchor, action, title: str = "Pour quel projet ?") -> None:
        """Exécute `action(scope)` sur le projet choisi (menu si plusieurs projets)."""
        choices = self.hub.active
        if not choices:
            self.toast("Aucun projet utilisable : ajoute une clé API.", "warning")
            return
        if len(choices) == 1:
            action(self.scope(choices[0]))
            return
        menu = tk.Menu(self, tearoff=0, font=("Segoe UI", 10), bg=t.pick(t.SURFACE), fg=t.pick(t.TEXT),
                       activebackground=t.pick(t.SURFACE_3), activeforeground=t.pick(t.TEXT), bd=0, relief="flat")
        menu.add_command(label=f"  {title}  ", state="disabled")
        menu.add_separator()
        for ctrl in choices:
            menu.add_command(label=f"  {ctrl.project.name}  ", command=lambda c=ctrl: action(self.scope(c)))
        x = anchor.winfo_rootx() if anchor else self.winfo_pointerx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 2 if anchor else self.winfo_pointery()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def add_project(self, spec) -> None:
        if self.on_secret:
            self.on_secret(spec.token)
        self.hub.add(spec)
        self.set_locked(self.hub.locked)

    def remove_project(self, ctrl: AppController, forget: bool) -> bool:
        billed = ctrl.billed_servers()
        ops = ctrl.runner.active()
        if billed or ops:
            ok, _ = confirm(self, f"Retirer le projet « {ctrl.project.name} » ?",
                            f"{len(billed)} serveur(s) de ce projet existent encore et {len(ops)} opération(s) sont en "
                            "cours : ils resteront facturés et ne seront plus visibles ici.", "Retirer quand même",
                            danger=True)
            if not ok:
                return False
        self.hub.remove(ctrl.project.id)
        self._scopes.pop(ctrl.project.id, None)
        if forget and self.store:
            self.store.forget(ctrl.project.id)
        self.toast(f"Projet « {ctrl.project.name} » retiré" + (" et clé supprimée du Gestionnaire" if forget else ""),
                   "info")
        self.set_locked(self.hub.locked)
        return True

    def on_token_invalid(self, scope: ProjectScope) -> None:
        """Clé refusée par le fournisseur (au lancement, à la saisie ou pendant une action)."""
        ctrl = scope.controller
        spec = ctrl.project
        if ctrl not in self.hub.controllers:
            return
        head = (f"{spec.provider.short} refuse la clé API du projet « {spec.name} » ({spec.masked}) : elle a été "
                "révoquée ou supprimée. Ce projet n'est plus interrogé.")
        if spec.source == "keyring":
            ok, _ = confirm(self, "Clé API refusée", head + "\n\nSupprimer cette clé du Gestionnaire d'identifiants ?",
                            "Supprimer la clé", danger=True)
            if ok:
                self.remove_project(ctrl, forget=True)
        elif spec.source in ("env", ".env"):
            prov = spec.provider
            where = "le fichier .env" if spec.source == ".env" else f"la variable d'environnement {prov.token_env}"
            info(self, "Clé API refusée", [head, f"Cette clé vient de {where} : remplace-la par une clé valide "
                                                 f"({prov.token_help}) puis relance l'application."])
        else:
            ok, _ = confirm(self, "Clé API refusée", head + "\n\nRetirer ce projet de la session ?", "Retirer",
                            danger=True)
            if ok:
                self.remove_project(ctrl, forget=False)
        self._render()

    # --- construction ----------------------------------------------------------------------------
    def _build_header(self) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=1, column=0, sticky="ew", padx=24, pady=(14, 10))
        head.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")
        label(titles, "Bureaux", 24, "bold").pack(anchor="w")
        self.subtitle = label(titles, "Chargement de l'inventaire…", 12, color=t.MUTED)
        self.subtitle.pack(anchor="w")

        bar = self.head_bar = ctk.CTkFrame(head, fg_color="transparent")
        new = primary_button(bar, "+ Nouveau bureau", lambda: self.with_project(new, ImportChooser), width=150)
        new.pack(side="left", padx=(0, 8))
        Tooltip(new, "Installer un système, importer un snapshot ou un serveur existant (Ctrl+N)")
        fw = secondary_button(bar, "Accès RDP", lambda: self.with_project(fw, FirewallDialog), width=106)
        fw.pack(side="left", padx=(0, 8))
        Tooltip(fw, "Adresses autorisées à se connecter : pour tous les bureaux ou bureau par bureau (Ctrl+F)")
        self.dormant_btn = secondary_button(bar, "Dormants", lambda: self.with_project(self.dormant_btn, DormantDialog),
                                            width=110)
        self.dormant_btn.pack(side="left", padx=(0, 8))
        Tooltip(self.dormant_btn, "Ressources facturées sans servir à un bureau")
        refresh = secondary_button(bar, "↻", self.hub.refresh_now, width=40)
        refresh.pack(side="left", padx=(0, 8))
        Tooltip(refresh, "Rafraîchir (F5)")
        settings = secondary_button(bar, "⚙", self._open_settings, width=40)
        settings.pack(side="left")
        Tooltip(settings, "Réglages (Ctrl+,)")
        self.header_buttons = (new, fw, self.dormant_btn, refresh, settings)   # grisés sans aucune clé API
        self.inventory_buttons = (new, fw)                              # inutiles tant que rien n'est chargé

        stats = self.stats = ctk.CTkFrame(head, fg_color="transparent")
        stats.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.tile_running = StatTile(stats, "En cours")
        self.tile_idle = StatTile(stats, "Au repos")
        self.tile_month = StatTile(stats, "Ce mois (estimation)")
        self.tile_ip = StatTile(stats, "Ton IP publique")
        self.tiles = (self.tile_running, self.tile_idle, self.tile_month, self.tile_ip)
        self._reflow()

    def _build_content(self) -> None:
        # Marge gauche identique à l'en-tête (24) ; la barre de défilement occupe la marge droite.
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self.scroll.grid(row=2, column=0, sticky="nsew", padx=(24, 6), pady=(6, 0))
        self.scroll.grid_columnconfigure(0, weight=1)
        # Bandeaux dans la zone défilante : sur petit écran ils ne mangent plus la place des cartes.
        self.banner_area = ctk.CTkFrame(self.scroll, fg_color="transparent", height=1)
        self.banner_area.grid(row=0, column=0, sticky="ew")
        self.grid_frame = ctk.CTkFrame(self.scroll, fg_color="transparent", height=1)
        self.grid_frame.grid(row=1, column=0, sticky="ew")
        self.empty = ctk.CTkFrame(self.scroll, fg_color="transparent", height=1)
        self.empty_title = label(self.empty, "", 18, "bold", anchor="center", justify="center")
        self.empty_title.pack(pady=(60, 4))
        self.empty_sub = label(self.empty, "", 13, color=t.MUTED, anchor="center", justify="center", wraplength=560)
        self.empty_sub.pack()
        self.empty_bar = ctk.CTkProgressBar(self.empty, mode="indeterminate", width=260, progress_color=t.ACCENT)
        self.empty_btn = primary_button(self.empty, "Importer un snapshot…",
                                        lambda: self.with_project(self.empty_btn, ImportChooser), width=200)
        self.empty_build_btn = primary_button(self.empty, "Créer un Windows de référence…",
                                              lambda: self.with_project(self.empty_build_btn, BuildDialog), width=260)
        self.import_frame = ctk.CTkFrame(self.scroll, fg_color="transparent", height=1)
        self.import_frame.grid(row=3, column=0, sticky="ew", pady=(18, 10))
        self.scroll.bind("<Configure>", lambda _e: self._relayout())

    def _build_log(self) -> None:
        wrap = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=0, border_width=0)
        wrap.grid(row=3, column=0, sticky="ew")
        wrap.grid_columnconfigure(1, weight=1)
        self.log_toggle = ghost_button(wrap, "▸ Journal", self.toggle_log, width=90, font=t.font(12, "bold"))
        self.log_toggle.grid(row=0, column=0, padx=(14, 4), pady=4)
        self.log_last = label(wrap, "", 12, color=t.MUTED)
        self.log_last.grid(row=0, column=1, sticky="ew")
        ghost_button(wrap, "Copier", self._copy_log, width=60).grid(row=0, column=2, padx=(4, 14))
        self.log_box = ctk.CTkTextbox(wrap, height=160, font=t.mono(11), fg_color=t.BG, wrap="word")
        self.log_box.configure(state="disabled")
        for level, color in LEVEL_COLORS.items():
            self.log_box.tag_config(level, foreground=t.pick(color))

    def _bind_keys(self) -> None:
        def unlocked(fn):
            return lambda _e: None if self.hub.locked else fn()
        self.bind("<F5>", unlocked(self.hub.refresh_now))
        self.bind("<Control-n>", unlocked(lambda: self.hub.loaded and self.with_project(None, ImportChooser)))
        self.bind("<Control-f>", unlocked(lambda: self.hub.loaded and self.with_project(None, FirewallDialog)))
        self.bind("<Control-l>", lambda _e: self.toggle_log())
        self.bind("<Control-comma>", unlocked(self._open_settings))
        self.bind("<Control-q>", lambda _e: self.request_quit())

    def _open_settings(self) -> None:
        if self.hub.controllers:
            SettingsDialog(self.scope(self.hub.controllers[0]))

    def set_locked(self, locked: bool) -> None:
        """Sans aucune clé API, tout ce qui parle au fournisseur reste désactivé."""
        self._locked = locked
        state = "disabled" if locked else "normal"
        for btn in (*self.header_buttons, self.empty_btn, self.empty_build_btn):
            btn.configure(state=state)
        self._buttons_sig = None
        self._render()

    def _render_buttons(self) -> None:
        """Boutons d'en-tête grisés quand ils n'ont rien à faire (inventaire absent, aucun dormant…)."""
        hub = self.hub
        usable = not hub.locked and bool(hub.active)
        loaded = usable and any(c.inventory for c in hub.active)
        sig = (usable, loaded, hub.dormant_count() > 0)
        if sig == getattr(self, "_buttons_sig", None):
            return
        self._buttons_sig = sig
        for btn in self.inventory_buttons:
            btn.configure(state="normal" if loaded else "disabled")
        self.dormant_btn.configure(state="normal" if loaded and sig[2] else "disabled")

    # --- mise en page adaptative ----------------------------------------------------------------
    def _on_window_resize(self, event) -> None:
        if event.widget is self:
            self._reflow()

    def _reflow(self) -> None:
        """En-tête et tuiles selon la largeur logique : boutons sous le titre, puis tuiles en 2 × 2."""
        width = logical(self, self.winfo_width()) if self.winfo_width() > 1 else 1220
        sig = (width < 900, width < 1000)
        if sig == self._reflow_sig:
            return
        self._reflow_sig = sig
        narrow_head, narrow_stats = sig
        if narrow_head:
            self.head_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
        else:
            self.head_bar.grid(row=0, column=1, columnspan=1, sticky="e", pady=0)
        cols = 2 if narrow_stats else 4
        for i in range(4):
            self.stats.grid_columnconfigure(i, weight=1 if i < cols else 0, uniform="stats" if i < cols else "")
        for i, tile in enumerate(self.tiles):
            row, col = divmod(i, cols)
            tile.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0 if col == cols - 1 else 6),
                      pady=(0 if row == 0 else 12, 0))

    # --- boucle ---------------------------------------------------------------------------------
    def _pump(self) -> None:
        try:
            self.hub.pump()
        except Exception:  # noqa: BLE001
            log.exception("Erreur de traitement des événements")
        self.after(100, self._pump)

    def _tick(self) -> None:
        try:
            self.hub.tick()
            self._render()
        except Exception:  # noqa: BLE001
            log.exception("Erreur de rafraîchissement de l'interface")
        self.after(1000, self._tick)

    def on_model_changed(self) -> None:
        if not self._render_pending:
            self._render_pending = True
            self.after_idle(self._render)

    def _render(self) -> None:
        self._render_pending = False
        self._render_header()
        self._render_buttons()
        self._render_banners()
        self.projects_bar.refresh()
        views = self.hub.desktop_views()
        seen = set()
        for view in views:
            seen.add(view.key)
            card = self.cards.get(view.key)
            if card is None:
                card = DesktopCard(self.grid_frame, self.handle_action, self.handle_error_action)
                self.cards[view.key] = card
            card.update_view(view)
        for key in list(self.cards):
            if key not in seen:
                self.cards.pop(key).destroy()
        self._relayout([v.key for v in views])
        self._render_empty(views)
        self._render_imports()

    def _relayout(self, order: list[str] | None = None) -> None:
        width = self.scroll.winfo_width()
        if width > 1:
            avail = logical(self.scroll, width)  # CARD_WIDTH est en unités logiques, winfo_width en pixels
            cols = max(1, min(4, int((avail + 12) // (t.CARD_WIDTH + 12))))
            self.empty_sub.configure(wraplength=max(240, min(560, int(avail) - 40)))
        else:
            cols = 2
        order = order if order is not None else list(self.cards)
        sig = (cols, tuple(order))
        if sig == self._layout_sig:
            return
        self._layout_sig = sig
        for i in range(4):
            self.grid_frame.grid_columnconfigure(i, weight=1 if i < cols else 0, uniform="cards" if i < cols else "")
        for i, key in enumerate(order):
            card = self.cards.get(key)
            if card:
                row, col = divmod(i, cols)
                card.grid(row=row, column=col, sticky="nsew", pady=6,
                          padx=(0 if col == 0 else 6, 0 if col == cols - 1 else 6))

    def _render_header(self) -> None:
        hub = self.hub
        costs = hub.costs()
        if hub.locked:
            self.subtitle.configure(text="Clé API requise pour charger un projet")
        elif not hub.loaded:
            self.subtitle.configure(text="Chargement de l'inventaire…")
        if not costs:
            for tile in (self.tile_running, self.tile_idle, self.tile_month):
                tile.set("—", "En attente de l'inventaire")
        else:
            n = costs.running_count
            self.tile_running.set(fmt.eur_h(costs.running_h) if n else "0 €/h",
                                  f"{n} serveur{'s' if n > 1 else ''} facturé{'s' if n > 1 else ''}" if n
                                  else "Aucun serveur : rien ne tourne", "warning" if n else None)
            self.tile_idle.set(fmt.eur_m(costs.idle_month),
                               f"Sauvegardes et volumes {fmt.eur(costs.storage_month)}"
                               + (f" · IP fixes {fmt.eur(costs.fixed_ips_month)}" if costs.fixed_ips_month else ""))
            self.tile_month.set(fmt.eur(costs.month_estimate), "Sessions + stockage au prorata")
        ip = hub.public_ip
        access = hub.ip_access()
        if ip and access:
            ok, total = access
            if ok == total:
                sub, kind = ("Autorisée en RDP" if total == 1 else f"Autorisée en RDP sur les {total} bureaux"), "success"
            elif ok == 0:
                sub, kind = "Autorisée en RDP sur aucun bureau", "warning"
            else:
                sub, kind = f"Autorisée en RDP sur {ok} bureau{'x' if ok > 1 else ''} sur {total}", "info"
            self.tile_ip.set(ip, sub, kind)
        elif ip:
            loaded = any(c.inventory for c in hub.controllers)
            self.tile_ip.set(ip, "Aucun pare-feu RDP géré" if loaded else "Accès RDP pas encore lus")
        else:
            self.tile_ip.set("…", "Détection en cours")
        if hub.loaded and not hub.locked:
            n = hub.desktop_count()
            stamp = hub.last_refresh()
            ago = time.monotonic() - stamp if stamp else 0
            refreshed = "à l'instant" if ago < 5 else f"il y a {fmt.duration(ago)}"
            projects = f"{len(hub.controllers)} projets · " if hub.multi else ""
            self.subtitle.configure(text=f"{projects}{n} bureau{'x' if n > 1 else ''} · rafraîchi {refreshed}")
        count = hub.dormant_count()
        text_ = f"Dormants ({count})" if count else "Dormants"
        if self.dormant_btn.cget("text") != text_:
            self.dormant_btn.configure(text=text_)

    def _render_banners(self) -> None:
        banners = self.hub.banners()
        sig = [(b.key, b.text) for b in banners]
        if sig == self._banner_sig:
            return
        self._banner_sig = sig
        for child in self.banner_area.winfo_children():
            child.destroy()
        for b in banners:
            actions = [(text_, lambda k=key: self.handle_banner(k)) for text_, key in b.actions]
            inner = b.key.split("|", 1)[-1]
            dismiss = None if inner in ("mode", "api") else (lambda k=b.key: self._dismiss(k))
            Banner(self.banner_area, b.kind, b.text, actions, dismiss).pack(fill="x", pady=(0, 6))

    def _dismiss(self, key: str) -> None:
        self.hub.dismiss(key)
        self._render()

    def _render_empty(self, views) -> None:
        hub = self.hub
        if views:
            self.empty.grid_forget()
            return
        self.empty.grid(row=2, column=0, sticky="ew")
        self.empty_btn.pack_forget()
        self.empty_build_btn.pack_forget()
        if hub.locked:
            self.empty_bar.stop()
            self.empty_bar.pack_forget()
            self.empty_title.configure(text="Aucune clé API")
            self.empty_sub.configure(text="Ajoute la clé API d'un projet en haut de la fenêtre pour charger tes "
                                          "bureaux.")
            return
        if not hub.loaded:
            self.empty_title.configure(text="Chargement…")
            self.empty_sub.configure(text="Lecture des serveurs, snapshots, volumes et pare-feu des projets.")
            if not self.empty_bar.winfo_ismapped():
                self.empty_bar.pack(pady=16)
                self.empty_bar.start()
            return
        self.empty_bar.stop()
        self.empty_bar.pack_forget()
        failing = [c for c in hub.controllers if c.inventory is None and c.refresh_error]
        if failing and len(failing) == len(hub.controllers):
            self.empty_title.configure(text=f"{failing[0].project.provider.short} est injoignable")
            self.empty_sub.configure(text=str(failing[0].refresh_error))
            return
        self.empty_title.configure(text="Aucun bureau géré")
        if any(c.grouping.unmanaged_snapshots for c in hub.active if c.inventory):
            self.empty_sub.configure(text="Tes snapshots existants peuvent devenir des bureaux : l'application les "
                                          "lancera à la demande et les sauvegardera à la fermeture. Tu peux aussi "
                                          "installer un système neuf (+ Nouveau bureau).")
            self.empty_btn.pack(pady=16)
        else:
            self.empty_sub.configure(text="Aucun snapshot à importer. L'application peut installer Windows toute "
                                          "seule sur un serveur temporaire (≈ 30–40 min) et en faire ton premier "
                                          "bureau. Linux arrivera bientôt.")
            self.empty_build_btn.pack(pady=16)

    def _render_imports(self) -> None:
        items = []
        for ctrl in self.hub.active:
            g = ctrl.grouping
            items += [(ctrl, "snapshot", s) for s in g.unmanaged_snapshots]
            items += [(ctrl, "server", s) for s in g.unmanaged_servers]
        sig = [(c.project.id, k, o.id) for c, k, o in items]
        if sig == self._import_sig:
            return
        self._import_sig = sig
        for child in self.import_frame.winfo_children():
            child.destroy()
        if not items or not self.cards:
            return
        caption(self.import_frame, "À importer").pack(fill="x", pady=(0, 6))
        for ctrl, kind, obj in items:
            scope = self.scope(ctrl)
            box = card_frame(self.import_frame)
            box.pack(fill="x", pady=4)
            box.grid_columnconfigure(0, weight=1)
            where = f"{ctrl.project.name} · " if self.hub.multi else ""
            if kind == "snapshot":
                title = f"Snapshot « {obj.description or obj.id} »"
                detail = (f"{where}{fmt.gb(obj.image_size)} · disque {obj.disk_size} Go · "
                          f"créé le {fmt.date_long(obj.created)}")
                cmd = lambda s=obj, sc=scope: AdoptDialog(sc, snapshot=s)  # noqa: E731
            else:
                title = f"Serveur non géré « {obj.name} »"
                detail = f"{where}{obj.spec} · {fmt.status(obj.status)} · {fmt.eur_h(obj.price_hourly)}"
                cmd = lambda s=obj, sc=scope: AdoptDialog(sc, server=s)  # noqa: E731
            label(box, title, 13, "bold").grid(row=0, column=0, sticky="w", padx=16, pady=(10, 0))
            label(box, detail, 12, color=t.MUTED).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 10))
            importing = any(type(op).__name__ in ("AdoptSnapshotOp", "AdoptServerOp") for op in ctrl.runner.active())
            secondary_button(box, "Importer…", cmd, width=110, state="disabled" if importing else "normal").grid(
                row=0, column=1, rowspan=2, padx=16)

    # --- journal et notifications ------------------------------------------------------------------
    def toggle_log(self) -> None:
        self._log_open = not self._log_open
        if self._log_open:
            self.log_box.grid(row=1, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 10))
            self.log_toggle.configure(text="▾ Journal")
        else:
            self.log_box.grid_forget()
            self.log_toggle.configure(text="▸ Journal")

    def on_log(self, level: str, message: str, who: str | None) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{who}] " if who else ""
        line = f"{stamp}  {prefix}{message}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line, level if level in LEVEL_COLORS else "info")
        if int(self.log_box.index("end-1c").split(".")[0]) > 600:
            self.log_box.delete("1.0", "100.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.log_last.configure(text=f"{stamp}  {prefix}{message}", text_color=LEVEL_COLORS.get(level, t.MUTED))

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log_box.get("1.0", "end"))
        self.toast("Journal copié", "info")

    def toast(self, message: str, kind: str = "info", actions=(), sticky: bool = False) -> None:
        self.toasts.show(message, kind, list(actions), 0 if sticky else None)

    def on_decision(self, event) -> None:
        notify.flash_window(self)
        notify.beep("warning")
        DecisionDialog(self, event)

    def on_ready(self, scope: ProjectScope, desktop: Desktop) -> None:
        notify.flash_window(self)
        notify.beep()
        ctrl = scope.controller
        actions = [] if desktop.slug in ctrl.pending_autoconnect else \
            [("Connecter", lambda s=desktop.slug: self._safe(ctrl.connect, s))]
        self.toast(f"« {desktop.name} » est prêt.", "success", actions)

    def on_reminder(self, scope: ProjectScope, desktop: Desktop, uptime_h: float, cost: float) -> None:
        notify.flash_window(self)
        ctrl = scope.controller
        sid = desktop.server.id
        self.toast(f"« {desktop.name} » est allumé depuis {fmt.duration(uptime_h * 3600)} (≈ {fmt.eur(cost)}). "
                   "Toujours besoin ?", "warning",
                   [("Sauvegarder & fermer", lambda: self.handle_action(ctrl.key(desktop.slug), Act.SAVE_CLOSE)),
                    ("Dans 1 h", lambda: ctrl.snooze_reminder(sid, 1)),
                    ("Ignorer", lambda: ctrl.snooze_reminder(sid, None))], sticky=True)

    def on_followup(self, scope: ProjectScope, op) -> None:
        d = scope.controller.desktop(op.slug) if op.slug else None
        os_name = op.result.get("os") or (d.os if d else "windows")
        if op.followup == "volume_help":
            volume_help(self, os_name, op.result.get("volume_id"))
        elif op.followup == "resize_help":
            resize_help(self, os_name, op.result.get("volume_id"))
        elif op.followup == "build_done":
            BuildDoneDialog(scope, op)

    # --- actions ------------------------------------------------------------------------------------
    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (UserError, ValueError) as exc:
            self.toast(str(exc), "error")
        return None

    def handle_action(self, key: str, act: Act) -> None:
        ctrl, slug = self.hub.split(key)
        d = ctrl.desktop(slug) if ctrl else None
        if d is None:
            return
        sc = self.scope(ctrl)
        handlers = {
            Act.LAUNCH: lambda: self.open_launch(sc, d),
            Act.CONNECT: lambda: self._safe(ctrl.connect, slug),
            Act.SAVE_CLOSE: lambda: self._save_close(sc, d),
            Act.CHECKPOINT: lambda: self._checkpoint(sc, d),
            Act.DISCARD: lambda: self._discard(sc, d),
            Act.POWER_ON: lambda: self._safe(ctrl.power, slug, "poweron"),
            Act.REBOOT: lambda: self._reboot(sc, d),
            Act.ADD_VOLUME: lambda: AddVolumeDialog(sc, d),
            Act.VOLUMES: lambda: VolumesDialog(sc, d),
            Act.FIREWALL: lambda: FirewallDialog(sc, d),
            Act.COPY_IP: lambda: self._copy(d.server.ipv4 if d.server else "", "IP copiée"),
            Act.COPY_PASSWORD: lambda: self._copy_password(sc, d),
            Act.HISTORY: lambda: SnapshotsDialog(sc, d, lambda snap: self.open_launch(sc, ctrl.desktop(slug), snap)),
            Act.DUPLICATE: lambda: DuplicateDialog(sc, d, lambda name: self.open_launch(sc, d, None, new_name=name)),
            Act.RENAME: lambda: self._rename(sc, d),
            Act.FIXED_IP: lambda: FixedIpDialog(sc, d),
            Act.CREDENTIALS: lambda: CredentialsDialog(sc, d),
            Act.LICENSE: lambda: LicenseDialog(sc, slug),
            Act.DELETE: lambda: DeleteDesktopDialog(sc, d),
            Act.RESUME: lambda: self._safe(ctrl.resume, slug),
            Act.IGNORE_OP: lambda: self._ignore_op(sc, d),
            Act.RESOLVE: lambda: ConflictDialog(sc, d),
            Act.CANCEL_OP: lambda: ctrl.cancel_op(slug),
        }
        handler = handlers.get(act)
        if handler:
            handler()

    def handle_error_action(self, key: str, err_key: str) -> None:
        ctrl, slug = self.hub.split(key)
        if ctrl is None:
            return
        d = ctrl.desktop(slug)
        if err_key == "dismiss":
            ctrl.errors.pop(slug, None)
        elif d is None:
            return
        elif err_key == "relaunch":
            ctrl.errors.pop(slug, None)
            self.open_launch(self.scope(ctrl), d, unavailable=ctrl.failed_launch.get(slug))
        elif err_key == "retry_save":
            self._safe(ctrl.save, slug, True)
        elif err_key == "retry_delete":
            self._safe(ctrl.discard, slug)
        self._render()

    def handle_banner(self, key: str) -> None:
        pid, _, inner = key.partition("|")
        ctrl = self.hub.get(pid)
        if ctrl is None:
            return
        sc = self.scope(ctrl)
        name, _, arg = inner.partition(":")
        if name == "refresh":
            ctrl.refresh_now()
        elif name == "token_invalid":
            self.on_token_invalid(sc)
        elif name == "resume":
            self._safe(ctrl.resume, arg)
        elif name == "cleanup_server":
            item = next((i for i in ctrl.dormant_items() if i.key == f"srv:{arg}"), None)
            if item and confirm(self, "Supprimer le serveur temporaire ?", f"{item.title} sera supprimé.",
                                "Supprimer", danger=True)[0]:
                self._safe(ctrl.cleanup, item)
        elif name == "adopt_server":
            srv = ctrl.inventory.server(int(arg)) if ctrl.inventory else None
            if srv:
                AdoptDialog(sc, server=srv)
        elif name == "adopt_fw":
            self._safe(ctrl.adopt_firewall, int(arg))
        elif name == "create_fw":
            if ctrl.public_ip:
                from ..netutil import normalize_cidr
                self._safe(ctrl.create_firewall, normalize_cidr(ctrl.public_ip), "Mon IP")
        elif name == "allow_ip":
            self._safe(ctrl.allow_current_ip)
        elif name == "firewall":
            FirewallDialog(sc)
        elif name == "import":
            ImportChooser(sc)
        elif name == "dormant":
            DormantDialog(sc)
        elif name == "license":
            LicenseDialog(sc, arg)

    def open_launch(self, sc: ProjectScope, d: Desktop | None, snapshot: SnapshotInfo | None = None,
                    new_name: str | None = None, unavailable=None) -> None:
        ctrl = sc.controller
        if d is None:
            return
        if ctrl.static is None:
            self.toast("Catalogue du fournisseur en cours de chargement, réessaie dans un instant.", "info")
            return
        if not d.latest:
            self.toast("Ce bureau n'a aucune sauvegarde disponible.", "error")
            return
        if not new_name and (ctrl.runner.for_slug(d.slug) or ctrl.launch_pending(d.slug) or d.server):
            self.toast(f"« {d.name} » est déjà lancé ou en cours de lancement.", "info")
            return

        def submit(params: LaunchParams) -> None:
            if new_name:
                self._safe(ctrl.duplicate, d.slug, d.latest, new_name, "launch", params)
            else:
                self._safe(ctrl.launch, d.slug, d.name, params)
        LaunchDialog(sc, d, submit, snapshot=snapshot, new_name=new_name, unavailable=unavailable)

    def _save_close(self, sc: ProjectScope, d: Desktop) -> None:
        ctrl = sc.controller
        os_name = OS_NAMES.get(d.os, "Le système")
        if ctrl.config.get("confirm_save_close"):
            ok, never = confirm(self, f"Sauvegarder et fermer « {d.name} » ?",
                                f"{os_name} va être arrêté proprement, sauvegardé, puis le serveur supprimé pour "
                                "ne plus rien payer d'autre que le stockage.",
                                "Sauvegarder & fermer", details=[
                                    "Enregistre ton travail et ferme tes applications avant.",
                                    "Compte 5 à 20 minutes selon la taille du disque.",
                                    "Ta session RDP va se déconnecter."],
                                checkbox="Ne plus demander")
            if not ok:
                return
            if never:
                ctrl.config.settings["confirm_save_close"] = False
                ctrl.config.save()
        self._safe(ctrl.save, d.slug, True)

    def _checkpoint(self, sc: ProjectScope, d: Desktop) -> None:
        ok, _ = confirm(self, f"Sauvegarder « {d.name} » sans le fermer ?",
                        f"{OS_NAMES.get(d.os, 'Le système')} va s'arrêter le temps du snapshot (5 à 20 min), puis "
                        "redémarrer automatiquement.",
                        "Sauvegarder", details=["Enregistre ton travail avant : la session RDP va se déconnecter."])
        if ok:
            self._safe(sc.controller.save, d.slug, False)

    def _discard(self, sc: ProjectScope, d: Desktop) -> None:
        latest = d.latest
        since = f"le {fmt.date_long(latest.created)} ({fmt.ago(latest.created)})" if latest else "jamais"
        kept = [f"La sauvegarde du {fmt.date_long(latest.created)}"] if latest else []
        kept += [f"Volume « {v.name} » ({v.size} Go)" for v in d.volumes]
        if d.fixed_ip:
            kept.append(f"IP fixe {d.fixed_ip.ip}")
        lost = ["Tout ce qui a été fait dans le bureau depuis la dernière sauvegarde"]
        if not latest:
            lost = ["TOUT le bureau : il n'a jamais été sauvegardé"]
        if confirm_typed(self, f"Fermer « {d.name} » sans sauvegarder",
                         f"Le serveur sera supprimé immédiatement. Toutes les modifications faites depuis la dernière "
                         f"sauvegarde ({since}) seront définitivement perdues.",
                         d.name, "Supprimer sans sauvegarder", lost=lost, kept=kept):
            self._safe(sc.controller.discard, d.slug)

    def _reboot(self, sc: ProjectScope, d: Desktop) -> None:
        ok, _ = confirm(self, f"Redémarrer « {d.name} » ?", f"{OS_NAMES.get(d.os, 'Le système')} va redémarrer "
                        "(demande propre) : la session RDP sera coupée quelques minutes.", "Redémarrer")
        if ok:
            self._safe(sc.controller.power, d.slug, "reboot")

    def _ignore_op(self, sc: ProjectScope, d: Desktop) -> None:
        ok, _ = confirm(self, "Ignorer l'opération interrompue ?", "Le serveur reste tel quel (et facturé). "
                        "Tu pourras le sauvegarder ou le fermer normalement ensuite.", "Ignorer")
        if ok:
            self._safe(sc.controller.ignore_op, d.slug)

    def _rename(self, sc: ProjectScope, d: Desktop) -> None:
        name = ask_text(self, "Renommer le bureau", "Nouveau nom affiché (l'identifiant technique ne change pas).",
                        d.name, "Renommer", allow_unchanged=False)
        if name and name != d.name:
            self._safe(sc.controller.rename, d.slug, name)

    def _copy(self, text_: str, message: str) -> None:
        if text_:
            self.clipboard_clear()
            self.clipboard_append(text_)
            self.toast(message, "info")

    def _copy_password(self, sc: ProjectScope, d: Desktop) -> None:
        pw = sc.controller.creds.get_password(d.slug)
        if not pw:
            return
        self.clipboard_clear()
        self.clipboard_append(pw)
        self.toast("Mot de passe copié (effacé du presse-papiers dans 30 s)", "info")

        def clear() -> None:
            try:
                if self.clipboard_get() == pw:
                    self.clipboard_clear()
            except tk.TclError:
                pass
        self.after(30_000, clear)

    # --- fermeture ----------------------------------------------------------------------------------
    def request_quit(self) -> None:
        if self._quitting:
            return
        hub = self.hub
        if top_window(self) is not self:
            top_window(self).lift()
            return
        if hub.locked:
            self.hard_exit()
            return
        if not hub.loaded:
            ok, _ = confirm(self, "Quitter ?", "L'inventaire n'est pas encore chargé : impossible de vérifier "
                            "qu'aucun serveur ne tourne.", "Quitter quand même", danger=True)
            if ok:
                self.hard_exit()
            return
        if not hub.billed_servers() and not hub.active_ops():
            self.hard_exit()
            return
        choice = QuitDialog(self).show()
        if choice == "force":
            if hub.active_ops():
                ok, _ = confirm(self, "Des opérations sont en cours", "Elles seront interrompues : les serveurs "
                                "concernés ne seront pas supprimés et resteront facturés. Au prochain démarrage, "
                                "l'application proposera de reprendre.", "Quitter quand même", danger=True)
                if not ok:
                    return
            self.hard_exit()
        elif choice == "save_all":
            QuitProgressDialog(self)

    def hard_exit(self) -> None:
        self._quitting = True
        try:
            self.hub.shutdown()
        finally:
            try:
                self.destroy()
            except tk.TclError:
                pass
            logging.shutdown()
            os._exit(0)
