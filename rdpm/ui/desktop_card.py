"""Carte d'un bureau : état, coûts, sauvegardes, progression et actions contextuelles."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from ..controller import DesktopView
from ..state import ACT_LABELS, Act
from . import theme as t
from .icons import OS_NAMES, icon, os_icon_name
from .widgets import Pill, Tooltip, caption, ghost_button, label, logical, primary_button, secondary_button

MENU_GROUPS = [
    [Act.CONNECT, Act.COPY_IP, Act.COPY_PASSWORD],
    [Act.CHECKPOINT, Act.SAVE_CLOSE, Act.RESUME],
    [Act.ADD_VOLUME, Act.VOLUMES, Act.FIREWALL, Act.FIXED_IP],
    [Act.HISTORY, Act.DUPLICATE, Act.RENAME, Act.CREDENTIALS, Act.LICENSE],
    [Act.POWER_ON, Act.REBOOT],
    [Act.IGNORE_OP, Act.DISCARD, Act.DELETE],
]
DANGER_ACTS = {Act.DISCARD, Act.DELETE}
_UNSET = object()

ERROR_ACTIONS = {
    "resource_unavailable": ("Choisir un autre type…", "relaunch"),
    "snapshot_failed": ("Réessayer la sauvegarde", "retry_save"),
    "snapshot_unverified": ("Réessayer la sauvegarde", "retry_save"),
    "delete_failed": ("Réessayer la suppression", "retry_delete"),
}


class _Row:
    """Ligne « LIBELLÉ  valeur » masquable."""

    def __init__(self, master, row: int, title: str) -> None:
        self.cap = caption(master, title, width=86)
        self.value = ctk.CTkFrame(master, fg_color="transparent")
        self.cap.grid(row=row, column=0, sticky="nw", padx=(18, 6), pady=(3, 0))
        self.value.grid(row=row, column=1, sticky="ew", padx=(0, 18), pady=(3, 0))
        self.visible = True

    def show(self, visible: bool) -> None:
        if visible == self.visible:
            return
        self.visible = visible
        for w in (self.cap, self.value):
            w.grid() if visible else w.grid_remove()


class DesktopCard(ctk.CTkFrame):
    def __init__(self, master, on_action: Callable[[str, Act], None],
                 on_error_action: Callable[[str, str], None]) -> None:
        super().__init__(master, fg_color=t.SURFACE, corner_radius=t.RADIUS, border_width=1, border_color=t.BORDER)
        self.on_action, self.on_error_action = on_action, on_error_action
        self.view: DesktopView | None = None
        self._last: dict[str, object] = {}
        self.grid_columnconfigure(1, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(16, 0))
        head.grid_columnconfigure(1, weight=1)
        self.os_icon = ctk.CTkLabel(head, text="", width=16, height=16)
        self.os_icon.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.os_tip = Tooltip(self.os_icon, "")
        self.name = label(head, "", 16, "bold")
        self.name.grid(row=0, column=1, sticky="w")
        self.pill = Pill(head)
        self.pill.grid(row=0, column=2, sticky="e")
        self.spec = label(self, "", 12, color=t.MUTED)
        self.spec.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(2, 10))

        self.ip_row = _Row(self, 2, "Adresse")
        self.ip = ctk.CTkLabel(self.ip_row.value, text="", font=t.mono(12), text_color=t.TEXT, anchor="w")
        self.ip.pack(side="left")
        self.copy_ip = ghost_button(self.ip_row.value, "Copier", lambda: self._act(Act.COPY_IP), width=50, height=22)
        self.copy_ip.pack(side="left", padx=(6, 0))
        self.fixed_badge = ctk.CTkLabel(self.ip_row.value, text="IP FIXE", font=t.font(10, "bold"),
                                        fg_color=t.tone("accent")[1], text_color=t.tone("accent")[0],
                                        corner_radius=6, height=18, padx=6)

        self.session_row = _Row(self, 3, "Session")
        self.session = label(self.session_row.value, "", 12)
        self.session.pack(fill="x")
        self.backup_row = _Row(self, 4, "Sauvegarde")
        self.backup = label(self.backup_row.value, "", 12)
        self.backup.pack(fill="x")
        self.volume_row = _Row(self, 5, "Volumes")
        self.volumes = label(self.volume_row.value, "", 12)
        self.volumes.pack(fill="x")
        self.license_row = _Row(self, 6, "Licence")
        self.license = label(self.license_row.value, "", 12)
        self.license.pack(fill="x")

        self.note_box = ctk.CTkFrame(self, fg_color=t.tone("warning")[1], corner_radius=8)
        self.note = ctk.CTkLabel(self.note_box, text="", font=t.font(12), text_color=t.tone("warning")[0],
                                 justify="left", anchor="w")
        self.note.pack(fill="x", padx=10, pady=7)
        self.note_box.grid(row=7, column=0, columnspan=2, sticky="ew", padx=18, pady=(10, 0))

        self.progress_box = ctk.CTkFrame(self, fg_color=t.SURFACE_2, corner_radius=8)
        self.progress_box.grid_columnconfigure(0, weight=1)
        self.phase = label(self.progress_box, "", 12, "bold")
        self.phase.grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        self.elapsed = label(self.progress_box, "", 11, color=t.MUTED)
        self.elapsed.grid(row=0, column=1, sticky="e", padx=12, pady=(8, 2))
        self.bar = ctk.CTkProgressBar(self.progress_box, height=6, progress_color=t.ACCENT)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 10))
        self.cancel_btn = ghost_button(self.progress_box, "Annuler", lambda: self._act(Act.CANCEL_OP), width=70,
                                       text_color=t.tone("danger")[0])
        self.progress_box.grid(row=8, column=0, columnspan=2, sticky="ew", padx=18, pady=(10, 0))
        self._bar_mode = None

        self.error_box = ctk.CTkFrame(self, fg_color=t.tone("danger")[1], corner_radius=8)
        self.error_box.grid_columnconfigure(0, weight=1)
        self.error_msg = ctk.CTkLabel(self.error_box, text="", font=t.font(12, "bold"),
                                      text_color=t.tone("danger")[0], justify="left", anchor="w")
        self.error_msg.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        self.error_hint = ctk.CTkLabel(self.error_box, text="", font=t.font(12), text_color=t.tone("danger")[0],
                                       justify="left", anchor="w")
        self.error_hint.grid(row=1, column=0, sticky="ew", padx=10)
        self.error_actions = ctk.CTkFrame(self.error_box, fg_color="transparent", height=1)
        self.error_actions.grid(row=2, column=0, sticky="w", padx=6, pady=(4, 8))
        self.error_box.grid(row=9, column=0, columnspan=2, sticky="ew", padx=18, pady=(10, 0))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=10, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 16))
        actions.grid_columnconfigure(2, weight=1)
        self.primary = primary_button(actions, "", lambda: self._act(self.view.primary), width=130)
        self.primary_tip = Tooltip(self.primary, "")
        self.secondary = secondary_button(actions, "", lambda: self._act(self.view.secondary), width=170)
        self.more = secondary_button(actions, "⋯", self._open_menu, width=40)
        self.more.grid(row=0, column=3, sticky="e")
        Tooltip(self.more, "Plus d'actions")
        self._primary_act = self._secondary_act = None
        self.bind("<Configure>", self._on_resize)

    # --- mise à jour ------------------------------------------------------------------------
    def _set(self, key: str, value, apply: Callable) -> None:
        if self._last.get(key, _UNSET) != value:
            self._last[key] = value
            apply(value)

    def update_view(self, v: DesktopView) -> None:
        self.view = v
        self._set("name", v.name, lambda x: self.name.configure(text=x))
        self._set("os", v.os, self._apply_os)
        self.pill.set(v.state_label, v.color)
        self._set("border", v.color, lambda c: self.configure(
            border_color=t.tone(c)[0] if c in ("warning", "danger") else t.BORDER))
        self._set("spec", v.spec, lambda x: self.spec.configure(text=x))

        self.ip_row.show(bool(v.ip))
        self._set("ip", v.ip, lambda x: self.ip.configure(text=x or ""))
        self._set("fixed", v.fixed_ip, lambda x: self.fixed_badge.pack(side="left", padx=(8, 0)) if x
                  else self.fixed_badge.pack_forget())
        self.session_row.show(bool(v.uptime))
        self._set("uptime", (v.uptime, v.burn), lambda x: self.session.configure(
            text=f"{x[0]}\n{x[1]} en ce moment" if x[0] else ""))
        self._set("backup", v.backup, lambda x: self.backup.configure(text=x))
        self.volume_row.show(bool(v.volumes))
        self._set("volumes", tuple(v.volumes), lambda x: self.volumes.configure(text="\n".join(x)))
        self.license_row.show(bool(v.license))
        self._set("license", v.license, lambda x: self.license.configure(text=x or ""))

        self._set("note", v.note, self._apply_note)
        self._apply_progress(v)
        self._set("error", (v.error.message, v.error.hint, v.error.code) if v.error else None, self._apply_error)
        self._apply_buttons(v)

    def _apply_os(self, os_name: str) -> None:
        self.os_icon.configure(image=icon(self.os_icon, os_icon_name(os_name)))
        self.os_tip.text = OS_NAMES.get(os_name, os_name)

    def _apply_note(self, note: str | None) -> None:
        if note:
            self.note.configure(text=note)
            self.note_box.grid()
        else:
            self.note_box.grid_remove()

    def _apply_progress(self, v: DesktopView) -> None:
        if not v.op_phase:
            self._set("progress_visible", False, lambda _x: self.progress_box.grid_remove())
            if self._bar_mode == "indeterminate":
                self.bar.stop()
            self._bar_mode = None
            return
        self._set("progress_visible", True, lambda _x: self.progress_box.grid())
        self._set("phase", v.op_phase, lambda x: self.phase.configure(text=x))
        self.elapsed.configure(text=v.op_elapsed or "")
        mode = "determinate" if v.op_progress is not None else "indeterminate"
        if mode != self._bar_mode:
            if self._bar_mode == "indeterminate":
                self.bar.stop()
            self.bar.configure(mode=mode)
            if mode == "indeterminate":
                self.bar.start()
            self._bar_mode = mode
        if v.op_progress is not None:
            self.bar.set(v.op_progress)
        self._set("cancel", v.op_cancellable, lambda c: self.cancel_btn.grid(row=0, column=2, padx=(0, 6))
                  if c else self.cancel_btn.grid_remove())

    def _apply_error(self, err) -> None:
        for child in self.error_actions.winfo_children():
            child.destroy()
        if not err:
            self.error_box.grid_remove()
            return
        message, hint, code = err
        self.error_msg.configure(text=message)
        self.error_hint.configure(text=hint or "")
        if hint:
            self.error_hint.grid()
        else:
            self.error_hint.grid_remove()
        specs = [ERROR_ACTIONS[code]] if code in ERROR_ACTIONS else []
        specs.append(("Masquer", "dismiss"))
        for text_, key in specs:
            ghost_button(self.error_actions, text_, lambda k=key: self.on_error_action(self.view.key, k),
                         width=10, text_color=t.tone("danger")[0], font=t.font(12, "bold")).pack(side="left",
                                                                                              padx=2)
        self.error_box.grid()

    def _apply_buttons(self, v: DesktopView) -> None:
        self._set("primary_disabled", v.primary_disabled,
                  lambda off: self.primary.configure(state="disabled" if off else "normal"))
        self.primary_tip.text = v.primary_hint or ""
        if v.primary != self._primary_act:
            self._primary_act = v.primary
            if v.primary:
                self.primary.configure(text=ACT_LABELS[v.primary])
                self.primary.grid(row=0, column=0, sticky="w", padx=(0, 8))
            else:
                self.primary.grid_remove()
        if v.secondary != self._secondary_act:
            self._secondary_act = v.secondary
            if v.secondary:
                self.secondary.configure(text=ACT_LABELS[v.secondary])
                self.secondary.grid(row=0, column=1, sticky="w")
            else:
                self.secondary.grid_remove()
        menu_items = [a for a in v.actions if a not in (v.primary, v.secondary, Act.CANCEL_OP)]
        self._set("more", bool(menu_items), lambda has: self.more.configure(state="normal" if has else "disabled"))

    def _on_resize(self, event) -> None:
        # wraplength est en unités logiques (CTk le multiplie par l'échelle DPI), event.width en pixels.
        width = int(logical(self, event.width))
        if self._last.get("wrap") != width:
            self._last["wrap"] = width
            for lbl in (self.session, self.backup, self.volumes, self.license):
                lbl.configure(wraplength=max(160, width - 150))
            for lbl in (self.spec, self.note, self.error_msg, self.error_hint):
                lbl.configure(wraplength=max(160, width - 70))

    # --- actions ----------------------------------------------------------------------------
    def _act(self, act: Act | None) -> None:
        if act and self.view:
            self.on_action(self.view.key, act)

    def _open_menu(self) -> None:
        if not self.view:
            return
        v = self.view
        available = [a for a in v.actions if a not in (v.primary, v.secondary, Act.CANCEL_OP)]
        menu = tk.Menu(self, tearoff=0, font=("Segoe UI", 10), bg=t.pick(t.SURFACE), fg=t.pick(t.TEXT),
                       activebackground=t.pick(t.SURFACE_3), activeforeground=t.pick(t.TEXT), bd=0,
                       relief="flat")
        first = True
        for group in MENU_GROUPS:
            items = [a for a in group if a in available]
            if not items:
                continue
            if not first:
                menu.add_separator()
            first = False
            for act in items:
                kwargs = {"foreground": t.pick(t.DANGER)} if act in DANGER_ACTS else {}
                menu.add_command(label=f"  {ACT_LABELS[act]}  ", command=lambda a=act: self._act(a), **kwargs)
        try:
            menu.tk_popup(self.more.winfo_rootx(), self.more.winfo_rooty() + self.more.winfo_height() + 2)
        finally:
            menu.grab_release()
