"""Liste à cocher des logiciels prêts à l'emploi (formulaires de création et dialogue « Logiciels… »)."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from ..software import catalog
from . import theme as t
from .widgets import caption, ghost_button, label

BADGE_TONES = {catalog.BADGE_CPU: "warning", catalog.BADGE_HEAVY: "warning", catalog.BADGE_PREVIEW: "info"}


def summary_text(keys, os_name: str) -> str:
    keys = list(keys)
    if not keys:
        return "Aucun logiciel"
    minutes, gb = catalog.estimate(keys, os_name)
    n = len(keys)
    return f"{n} logiciel{'s' if n > 1 else ''} · + ~{max(1, round(minutes))} min · + ~{gb:.1f} Go".replace(".", ",")


class SoftwarePicker(ctk.CTkFrame):
    """Cases par catégorie, préréglages, dépendances ajoutées automatiquement, estimation.

    `installed` : déjà présents (cochés et grisés). `selected()` rend ce qu'il reste à installer, dépendances
    comprises, dans l'ordre d'installation."""

    def __init__(self, master, os_name: str, *, installed=(), preset: str = "recommended", chosen=None,
                 on_change: Callable[[], None] | None = None, height: int = 280, wraplength: int = 380) -> None:
        super().__init__(master, fg_color="transparent")
        self.os_name, self.on_change, self.wraplength = os_name, on_change, wraplength
        self.installed = set(installed)
        self.user: set[str] = set()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.boxes: dict[str, ctk.CTkCheckBox] = {}
        self.notes: dict[str, ctk.CTkLabel] = {}

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x")
        self.summary = label(top, "", 12, "bold")
        self.summary.pack(side="left")
        for text, name in (("Aucun", "none"), ("Tout", "all"), ("Recommandé", "recommended")):
            ghost_button(top, text, lambda n=name: self.apply_preset(n), width=10,
                         text_color=t.ACCENT).pack(side="right")
        self.list = ctk.CTkScrollableFrame(self, height=height, fg_color=t.SURFACE, corner_radius=10,
                                           border_width=1, border_color=t.BORDER)
        self.list.pack(fill="both", expand=True, pady=(4, 0))
        self.list.grid_columnconfigure(0, weight=1)
        self.deps = label(self, "", 11, color=t.MUTED, wraplength=wraplength + 40)
        self.deps.pack(fill="x", pady=(4, 0))
        self._build_rows()
        if chosen is None:
            self.apply_preset(preset, notify=False)
        else:   # choix conservé d'un rendu à l'autre
            self.user = set(chosen) - self.installed
            self._refresh(notify=False)

    # --- construction ------------------------------------------------------------------------------
    def _build_rows(self) -> None:
        row = 0
        for category in catalog.CATEGORIES:
            apps = [a for a in catalog.APPS if a.category == category]
            if not apps:
                continue
            caption(self.list, category).grid(row=row, column=0, sticky="w", padx=10, pady=(10 if row else 6, 2))
            row += 1
            for app in apps:
                self._row(row, app)
                row += 1

    def _row(self, row: int, app: catalog.App) -> None:
        frame = ctk.CTkFrame(self.list, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", padx=(8, 6), pady=2)
        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.pack(fill="x")
        var = tk.BooleanVar(value=False)
        box = ctk.CTkCheckBox(head, text=app.name, variable=var, font=t.font(12, "bold"), checkbox_width=18,
                              checkbox_height=18, text_color_disabled=t.FAINT,
                              command=lambda k=app.key: self._toggled(k))
        box.pack(side="left")
        reason = app.reason(self.os_name)
        badges = ["installé"] if app.key in self.installed else list(app.badges)
        for badge in badges if not reason else []:
            strong, soft = t.tone("success" if badge == "installé" else BADGE_TONES.get(badge, "muted"))
            ctk.CTkLabel(head, text=badge.upper(), font=t.font(9, "bold"), fg_color=soft, text_color=strong,
                         corner_radius=6, height=18, padx=6).pack(side="left", padx=(6, 0))
        blurb = label(frame, reason or app.blurb, 11, color=t.FAINT if reason else t.MUTED,
                      wraplength=self.wraplength)
        blurb.pack(fill="x", padx=(26, 0))
        note = label(frame, "", 11, color=t.ACCENT, wraplength=self.wraplength)
        self.vars[app.key], self.boxes[app.key], self.notes[app.key] = var, box, note
        if reason:
            box.configure(state="disabled")
        elif app.key not in self.installed:
            blurb.bind("<Button-1>", lambda _e, k=app.key: self._click(k))

    # --- état ---------------------------------------------------------------------------------------
    def apply_preset(self, name: str, notify: bool = True) -> None:
        available = {a.key for a in catalog.for_os(self.os_name)}
        if name == "all":
            self.user = available - self.installed
        elif name == "recommended":
            self.user = set(catalog.recommended(self.os_name)) - self.installed
        else:
            self.user = set()
        self._refresh(notify)

    def _click(self, key: str) -> None:
        if self.boxes[key].cget("state") == "disabled":
            return
        self.vars[key].set(not self.vars[key].get())
        self._toggled(key)

    def _toggled(self, key: str) -> None:
        if self.vars[key].get():
            self.user.add(key)
        else:
            self.user.discard(key)
        self._refresh()

    def _refresh(self, notify: bool = True) -> None:
        effective = set(catalog.resolve(self.user | self.installed, self.os_name))
        added = catalog.added_by_dependencies(self.user, self.os_name)
        for app in catalog.APPS:
            key, var, box, note = app.key, self.vars[app.key], self.boxes[app.key], self.notes[app.key]
            if not app.available(self.os_name):
                var.set(False)
                continue
            if key in self.installed:
                var.set(True)
                box.configure(state="disabled")
                continue
            auto = key in effective and key not in self.user
            var.set(key in effective)
            box.configure(state="disabled" if auto else "normal")
            if auto and key in added:
                note.configure(text=f"Ajouté : requis par {', '.join(added[key])}")
                note.pack(fill="x", padx=(26, 0))
            else:
                note.pack_forget()
        todo = self.selected()
        self.summary.configure(text=summary_text(todo, self.os_name))
        extra = [k for k in added if k not in self.installed]
        self.deps.configure(text=(f"Dépendances ajoutées automatiquement : {', '.join(catalog.names(extra))}."
                                  if extra else "Versions les plus récentes, depuis les sources officielles. "
                                                "Tu te connectes à tes comptes au premier lancement."))
        if notify and self.on_change:
            self.on_change()

    def selected(self) -> list[str]:
        """Logiciels à installer (dépendances comprises, déjà installés exclus), dans l'ordre d'installation."""
        return [k for k in catalog.resolve(self.user | self.installed, self.os_name) if k not in self.installed]

    def requested(self) -> list[str]:
        """Choix de l'utilisateur (sans les dépendances ajoutées), pour la construction."""
        return [k for k in catalog.install_order() if k in self.user]
