"""Fenêtre principale."""

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
from ..models import Desktop, SnapshotInfo
from ..ops.launch import LaunchParams
from ..state import Act
from . import theme as t
from .desktop_card import DesktopCard
from .dialogs.base import confirm, confirm_typed, ask_text, top_window
from .dialogs.launch import LaunchDialog
from .dialogs.manage import (
    AddVolumeDialog, ConflictDialog, CredentialsDialog, DeleteDesktopDialog, DuplicateDialog, FirewallDialog,
    FixedIpDialog, SnapshotsDialog, VolumesDialog, resize_help, volume_help,
)
from .dialogs.misc import AdoptDialog, DormantDialog, ImportChooser, SettingsDialog
from .dialogs.quit import DecisionDialog, QuitDialog, QuitProgressDialog
from .token_bar import TokenBar
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


class MainWindow(ctk.CTk):
    def __init__(self, controller: AppController) -> None:
        super().__init__(fg_color=t.BG)
        self.controller = controller
        controller.ui = self
        self.title(APP_TITLE)
        self.geometry("1220x820")
        self.minsize(720, 540)
        self.protocol("WM_DELETE_WINDOW", self.request_quit)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.toasts = ToastManager(self)
        self.cards: dict[str, DesktopCard] = {}
        self._cols = 0
        self._layout_sig = None
        self._banner_sig = None
        self._import_sig = None
        self._render_pending = False
        self._log_open = False
        self._quitting = False
        self._reflow_sig = None

        self.token_bar = TokenBar(self)
        if controller.mode != "fake":
            self.token_bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(14, 0))
        self._build_header()
        self._build_content()
        self._build_log()
        self._bind_keys()
        self.bind("<Configure>", self._on_window_resize, add="+")
        self.after(100, self._pump)
        self.after(200, self._tick)
        self.set_locked(controller.locked)

    # --- construction ----------------------------------------------------------------------------
    def _build_header(self) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=1, column=0, sticky="ew", padx=24, pady=(14 if self.controller.mode != "fake" else 20, 10))
        head.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w")
        label(titles, "Bureaux Windows", 24, "bold").pack(anchor="w")
        self.subtitle = label(titles, "Chargement de l'inventaire Hetzner…", 12, color=t.MUTED)
        self.subtitle.pack(anchor="w")

        bar = self.head_bar = ctk.CTkFrame(head, fg_color="transparent")
        new = primary_button(bar, "+ Nouveau bureau", lambda: ImportChooser(self), width=150)
        new.pack(side="left", padx=(0, 8))
        Tooltip(new, "Importer un snapshot ou un serveur existant (Ctrl+N)")
        fw = secondary_button(bar, "Pare-feu", lambda: FirewallDialog(self), width=96)
        fw.pack(side="left", padx=(0, 8))
        Tooltip(fw, "Adresses autorisées à se connecter en RDP (Ctrl+F)")
        self.dormant_btn = secondary_button(bar, "Dormants", lambda: DormantDialog(self), width=110)
        self.dormant_btn.pack(side="left", padx=(0, 8))
        Tooltip(self.dormant_btn, "Ressources facturées sans servir à un bureau")
        refresh = secondary_button(bar, "↻", self.controller.refresh_now, width=40)
        refresh.pack(side="left", padx=(0, 8))
        Tooltip(refresh, "Rafraîchir (F5)")
        settings = secondary_button(bar, "⚙", lambda: SettingsDialog(self), width=40)
        settings.pack(side="left")
        Tooltip(settings, "Réglages (Ctrl+,)")
        self.header_buttons = (new, fw, self.dormant_btn, refresh, settings)

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
        self.empty_btn = primary_button(self.empty, "Importer un snapshot", lambda: ImportChooser(self), width=200)
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
            return lambda _e: None if self.controller.locked else fn()
        self.bind("<F5>", unlocked(self.controller.refresh_now))
        self.bind("<Control-n>", unlocked(lambda: ImportChooser(self)))
        self.bind("<Control-f>", unlocked(lambda: FirewallDialog(self)))
        self.bind("<Control-l>", lambda _e: self.toggle_log())
        self.bind("<Control-comma>", unlocked(lambda: SettingsDialog(self)))
        self.bind("<Control-q>", lambda _e: self.request_quit())

    def set_locked(self, locked: bool) -> None:
        """Sans token validé, tout ce qui parle à Hetzner reste désactivé."""
        state = "disabled" if locked else "normal"
        for btn in (*self.header_buttons, self.empty_btn):
            btn.configure(state=state)
        self._render()

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
            self.controller.pump()
        except Exception:  # noqa: BLE001
            log.exception("Erreur de traitement des événements")
        self.after(100, self._pump)

    def _tick(self) -> None:
        try:
            self.controller.tick()
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
        ctrl = self.controller
        self._render_header()
        self._render_banners()
        self.token_bar.refresh()
        views = ctrl.desktop_views() if ctrl.inventory else []
        seen = set()
        for view in views:
            seen.add(view.slug)
            card = self.cards.get(view.slug)
            if card is None:
                card = DesktopCard(self.grid_frame, self.handle_action, self.handle_error_action)
                self.cards[view.slug] = card
            card.update_view(view)
        for slug in list(self.cards):
            if slug not in seen:
                self.cards.pop(slug).destroy()
        self._relayout([v.slug for v in views])
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
        for i, slug in enumerate(order):
            card = self.cards.get(slug)
            if card:
                row, col = divmod(i, cols)
                card.grid(row=row, column=col, sticky="nsew", pady=6,
                          padx=(0 if col == 0 else 6, 0 if col == cols - 1 else 6))

    def _render_header(self) -> None:
        ctrl = self.controller
        costs = ctrl.costs()
        if ctrl.locked:
            self.subtitle.configure(text="Token Hetzner requis pour charger le projet")
        elif not ctrl.inventory:
            self.subtitle.configure(text="Chargement de l'inventaire Hetzner…")
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
        ip = ctrl.public_ip
        allowed = ctrl.ip_is_allowed()
        if ip:
            sub = {True: "Autorisée sur le pare-feu RDP", False: "Non autorisée sur le pare-feu RDP",
                   None: "Pare-feu RDP non géré" if ctrl.inventory else "Pare-feu RDP pas encore lu"}[allowed]
            self.tile_ip.set(ip, sub, {True: "success", False: "warning", None: None}[allowed])
        else:
            self.tile_ip.set("…", "Détection en cours")
        if ctrl.inventory:
            n = len(ctrl.grouping.desktops)
            ago = time.monotonic() - ctrl.last_refresh if ctrl.last_refresh else 0
            refreshed = "à l'instant" if ago < 5 else f"il y a {fmt.duration(ago)}"
            self.subtitle.configure(text=f"Hetzner Cloud · {n} bureau{'x' if n > 1 else ''} · rafraîchi {refreshed}")
        dormant = ctrl.dormant_items()
        text_ = f"Dormants ({len(dormant)})" if dormant else "Dormants"
        if self.dormant_btn.cget("text") != text_:
            self.dormant_btn.configure(text=text_)

    def _render_banners(self) -> None:
        banners = self.controller.banners()
        sig = [(b.key, b.text) for b in banners]
        if sig == self._banner_sig:
            return
        self._banner_sig = sig
        for child in self.banner_area.winfo_children():
            child.destroy()
        for b in banners:
            actions = [(text_, lambda k=key: self.handle_banner(k)) for text_, key in b.actions]
            dismiss = None if b.key in ("mode", "api") else (lambda k=b.key: self._dismiss(k))
            Banner(self.banner_area, b.kind, b.text, actions, dismiss).pack(fill="x", pady=(0, 6))

    def _dismiss(self, key: str) -> None:
        self.controller.dismissed.add(key)
        self._render()

    def _render_empty(self, views) -> None:
        ctrl = self.controller
        if views:
            self.empty.grid_forget()
            return
        self.empty.grid(row=2, column=0, sticky="ew")
        self.empty_btn.pack_forget()
        if ctrl.locked:
            self.empty_bar.stop()
            self.empty_bar.pack_forget()
            self.empty_title.configure(text="En attente du token Hetzner")
            self.empty_sub.configure(text="Saisissez un token API en haut de la fenêtre pour charger vos bureaux.")
            return
        if ctrl.inventory is None:
            if ctrl.refresh_error:
                self.empty_title.configure(text="Hetzner est injoignable")
                self.empty_sub.configure(text=str(ctrl.refresh_error))
                self.empty_bar.pack_forget()
            else:
                self.empty_title.configure(text="Chargement…")
                self.empty_sub.configure(text="Lecture des serveurs, snapshots, volumes et pare-feu du projet.")
                if not self.empty_bar.winfo_ismapped():
                    self.empty_bar.pack(pady=16)
                    self.empty_bar.start()
            return
        self.empty_bar.stop()
        self.empty_bar.pack_forget()
        self.empty_title.configure(text="Aucun bureau géré")
        if ctrl.grouping.unmanaged_snapshots:
            self.empty_sub.configure(text="Tes snapshots Windows existants peuvent devenir des bureaux : "
                                          "l'application les lancera à la demande et les sauvegardera à la fermeture.")
            self.empty_btn.pack(pady=16)
        else:
            self.empty_sub.configure(text="Aucun snapshot Windows trouvé dans ce projet. Installe Windows sur un "
                                          "serveur Hetzner, crée un snapshot, puis importe-le ici.")

    def _render_imports(self) -> None:
        g = self.controller.grouping
        items = [("snapshot", s) for s in g.unmanaged_snapshots] + [("server", s) for s in g.unmanaged_servers]
        sig = [(k, o.id) for k, o in items]
        if sig == self._import_sig:
            return
        self._import_sig = sig
        for child in self.import_frame.winfo_children():
            child.destroy()
        if not items or not self.cards:
            return
        caption(self.import_frame, "À importer").pack(fill="x", pady=(0, 6))
        for kind, obj in items:
            box = card_frame(self.import_frame)
            box.pack(fill="x", pady=4)
            box.grid_columnconfigure(0, weight=1)
            if kind == "snapshot":
                title = f"Snapshot « {obj.description or obj.id} »"
                detail = f"{fmt.gb(obj.image_size)} · disque {obj.disk_size} Go · créé le {fmt.date_long(obj.created)}"
                cmd = lambda s=obj: AdoptDialog(self, snapshot=s)  # noqa: E731
            else:
                title = f"Serveur non géré « {obj.name} »"
                detail = f"{obj.spec} · {fmt.status(obj.status)} · {fmt.eur_h(obj.price_hourly)}"
                cmd = lambda s=obj: AdoptDialog(self, server=s)  # noqa: E731
            label(box, title, 13, "bold").grid(row=0, column=0, sticky="w", padx=16, pady=(10, 0))
            label(box, detail, 12, color=t.MUTED).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 10))
            secondary_button(box, "Importer…", cmd, width=110).grid(row=0, column=1, rowspan=2, padx=16)

    # --- journal et notifications ------------------------------------------------------------------
    def toggle_log(self) -> None:
        self._log_open = not self._log_open
        if self._log_open:
            self.log_box.grid(row=1, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 10))
            self.log_toggle.configure(text="▾ Journal")
        else:
            self.log_box.grid_forget()
            self.log_toggle.configure(text="▸ Journal")

    def on_log(self, level: str, message: str, slug: str | None) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        who = f"[{slug}] " if slug else ""
        line = f"{stamp}  {who}{message}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line, level if level in LEVEL_COLORS else "info")
        if int(self.log_box.index("end-1c").split(".")[0]) > 600:
            self.log_box.delete("1.0", "100.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.log_last.configure(text=f"{stamp}  {who}{message}", text_color=LEVEL_COLORS.get(level, t.MUTED))

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

    def on_ready(self, desktop: Desktop) -> None:
        notify.flash_window(self)
        notify.beep()
        actions = [] if desktop.slug in self.controller.pending_autoconnect else \
            [("Connecter", lambda s=desktop.slug: self._safe(self.controller.connect, s))]
        self.toast(f"« {desktop.name} » est prêt.", "success", actions)

    def on_reminder(self, desktop: Desktop, uptime_h: float, cost: float) -> None:
        notify.flash_window(self)
        sid = desktop.server.id
        self.toast(f"« {desktop.name} » est allumé depuis {fmt.duration(uptime_h * 3600)} (≈ {fmt.eur(cost)}). "
                   "Toujours besoin ?", "warning",
                   [("Sauvegarder & fermer", lambda: self.handle_action(desktop.slug, Act.SAVE_CLOSE)),
                    ("Dans 1 h", lambda: self.controller.snooze_reminder(sid, 1)),
                    ("Ignorer", lambda: self.controller.snooze_reminder(sid, None))], sticky=True)

    def on_followup(self, op) -> None:
        if op.followup == "volume_help":
            volume_help(self)
        elif op.followup == "resize_help":
            resize_help(self)

    # --- actions ------------------------------------------------------------------------------------
    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (UserError, ValueError) as exc:
            self.toast(str(exc), "error")
        return None

    def handle_action(self, slug: str, act: Act) -> None:
        ctrl = self.controller
        d = ctrl.desktop(slug)
        if d is None:
            return
        handlers = {
            Act.LAUNCH: lambda: self.open_launch(d),
            Act.CONNECT: lambda: self._safe(ctrl.connect, slug),
            Act.SAVE_CLOSE: lambda: self._save_close(d),
            Act.CHECKPOINT: lambda: self._checkpoint(d),
            Act.DISCARD: lambda: self._discard(d),
            Act.POWER_ON: lambda: self._safe(ctrl.power, slug, "poweron"),
            Act.REBOOT: lambda: self._reboot(d),
            Act.ADD_VOLUME: lambda: AddVolumeDialog(self, d),
            Act.VOLUMES: lambda: VolumesDialog(self, d),
            Act.FIREWALL: lambda: FirewallDialog(self, d),
            Act.COPY_IP: lambda: self._copy(d.server.ipv4 if d.server else "", "IP copiée"),
            Act.COPY_PASSWORD: lambda: self._copy_password(d),
            Act.HISTORY: lambda: SnapshotsDialog(self, d, lambda snap: self.open_launch(ctrl.desktop(slug), snap)),
            Act.DUPLICATE: lambda: DuplicateDialog(self, d, lambda name: self.open_launch(d, None, new_name=name)),
            Act.RENAME: lambda: self._rename(d),
            Act.FIXED_IP: lambda: FixedIpDialog(self, d),
            Act.CREDENTIALS: lambda: CredentialsDialog(self, d),
            Act.DELETE: lambda: DeleteDesktopDialog(self, d),
            Act.RESUME: lambda: self._safe(ctrl.resume, slug),
            Act.IGNORE_OP: lambda: self._ignore_op(d),
            Act.RESOLVE: lambda: ConflictDialog(self, d),
            Act.CANCEL_OP: lambda: ctrl.cancel_op(slug),
        }
        handler = handlers.get(act)
        if handler:
            handler()

    def handle_error_action(self, slug: str, key: str) -> None:
        ctrl = self.controller
        d = ctrl.desktop(slug)
        if key == "dismiss":
            ctrl.errors.pop(slug, None)
        elif d is None:
            return
        elif key == "relaunch":
            ctrl.errors.pop(slug, None)
            self.open_launch(d, unavailable=ctrl.failed_launch.get(slug))
        elif key == "retry_save":
            self._safe(ctrl.save, slug, True)
        elif key == "retry_delete":
            self._safe(ctrl.discard, slug)
        self._render()

    def handle_banner(self, key: str) -> None:
        ctrl = self.controller
        name, _, arg = key.partition(":")
        if name == "refresh":
            ctrl.refresh_now()
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
                AdoptDialog(self, server=srv)
        elif name == "adopt_fw":
            self._safe(ctrl.adopt_firewall, int(arg))
        elif name == "create_fw":
            if ctrl.public_ip:
                from ..netutil import normalize_cidr
                self._safe(ctrl.create_firewall, normalize_cidr(ctrl.public_ip), "Mon IP")
        elif name == "allow_ip":
            self._safe(ctrl.allow_current_ip)
        elif name == "firewall":
            FirewallDialog(self)
        elif name == "import":
            ImportChooser(self)
        elif name == "dormant":
            DormantDialog(self)

    def open_launch(self, d: Desktop | None, snapshot: SnapshotInfo | None = None, new_name: str | None = None,
                    unavailable=None) -> None:
        ctrl = self.controller
        if d is None:
            return
        if ctrl.static is None:
            self.toast("Catalogue Hetzner en cours de chargement, réessayez dans un instant.", "info")
            return
        if not d.latest:
            self.toast("Ce bureau n'a aucune sauvegarde disponible.", "error")
            return

        def submit(params: LaunchParams) -> None:
            if new_name:
                self._safe(ctrl.duplicate, d.slug, d.latest, new_name, "launch", params)
            else:
                self._safe(ctrl.launch, d.slug, d.name, params)
        LaunchDialog(self, d, submit, snapshot=snapshot, new_name=new_name, unavailable=unavailable)

    def _save_close(self, d: Desktop) -> None:
        ctrl = self.controller
        if ctrl.config.get("confirm_save_close"):
            ok, never = confirm(self, f"Sauvegarder et fermer « {d.name} » ?",
                                "Windows va être arrêté proprement, sauvegardé, puis le serveur supprimé pour ne "
                                "plus rien payer d'autre que le stockage.",
                                "Sauvegarder & fermer", details=[
                                    "Enregistre ton travail et ferme tes applications dans Windows avant.",
                                    "Compte 5 à 20 minutes selon la taille du disque.",
                                    "Ta session RDP va se déconnecter."],
                                checkbox="Ne plus demander")
            if not ok:
                return
            if never:
                ctrl.config.settings["confirm_save_close"] = False
                ctrl.config.save()
        self._safe(ctrl.save, d.slug, True)

    def _checkpoint(self, d: Desktop) -> None:
        ok, _ = confirm(self, f"Sauvegarder « {d.name} » sans le fermer ?",
                        "Windows va s'arrêter le temps du snapshot (5 à 20 min), puis redémarrer automatiquement.",
                        "Sauvegarder", details=["Enregistre ton travail avant : la session RDP va se déconnecter."])
        if ok:
            self._safe(self.controller.save, d.slug, False)

    def _discard(self, d: Desktop) -> None:
        latest = d.latest
        since = f"le {fmt.date_long(latest.created)} ({fmt.ago(latest.created)})" if latest else "jamais"
        kept = [f"La sauvegarde du {fmt.date_long(latest.created)}"] if latest else []
        kept += [f"Volume « {v.name} » ({v.size} Go)" for v in d.volumes]
        if d.fixed_ip:
            kept.append(f"IP fixe {d.fixed_ip.ip}")
        lost = ["Tout ce qui a été fait dans Windows depuis la dernière sauvegarde"]
        if not latest:
            lost = ["TOUT le bureau : il n'a jamais été sauvegardé"]
        if confirm_typed(self, f"Fermer « {d.name} » sans sauvegarder",
                         f"Le serveur sera supprimé immédiatement. Toutes les modifications faites depuis la dernière "
                         f"sauvegarde ({since}) seront définitivement perdues.",
                         d.name, "Supprimer sans sauvegarder", lost=lost, kept=kept):
            self._safe(self.controller.discard, d.slug)

    def _reboot(self, d: Desktop) -> None:
        ok, _ = confirm(self, f"Redémarrer « {d.name} » ?", "Windows va redémarrer (demande propre) : la session "
                        "RDP sera coupée quelques minutes.", "Redémarrer")
        if ok:
            self._safe(self.controller.power, d.slug, "reboot")

    def _ignore_op(self, d: Desktop) -> None:
        ok, _ = confirm(self, "Ignorer l'opération interrompue ?", "Le serveur reste tel quel (et facturé). "
                        "Vous pourrez le sauvegarder ou le fermer normalement ensuite.", "Ignorer")
        if ok:
            self._safe(self.controller.ignore_op, d.slug)

    def _rename(self, d: Desktop) -> None:
        name = ask_text(self, "Renommer le bureau", "Nouveau nom affiché (l'identifiant technique ne change pas).",
                        d.name, "Renommer")
        if name and name != d.name:
            self._safe(self.controller.rename, d.slug, name)

    def _copy(self, text_: str, message: str) -> None:
        if text_:
            self.clipboard_clear()
            self.clipboard_append(text_)
            self.toast(message, "info")

    def _copy_password(self, d: Desktop) -> None:
        pw = self.controller.creds.get_password(d.slug)
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
        ctrl = self.controller
        if top_window(self) is not self:
            top_window(self).lift()
            return
        if ctrl.locked:
            self.hard_exit()
            return
        if ctrl.inventory is None and ctrl.refresh_error is None:
            ok, _ = confirm(self, "Quitter ?", "L'inventaire n'est pas encore chargé : impossible de vérifier "
                            "qu'aucun serveur ne tourne.", "Quitter quand même", danger=True)
            if ok:
                self.hard_exit()
            return
        if not ctrl.billed_servers() and not ctrl.runner.active():
            self.hard_exit()
            return
        choice = QuitDialog(self).show()
        if choice == "force":
            if ctrl.runner.active():
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
            self.controller.shutdown()
        finally:
            try:
                self.destroy()
            except tk.TclError:
                pass
            logging.shutdown()
            os._exit(0)
