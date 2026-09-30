"""Formulaire « Créer un bureau Linux » : même disposition que le Windows de référence (nom, emplacement,
type, épinglage, coût), avec la distribution, le compte et la langue à la place de l'édition Windows."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from ... import netutil
from ...build import linux
from ...constants import OS_LINUX
from ...models import Offer
from ...ops.build_linux import LinuxBuildParams
from .. import theme as t
from ..widgets import label, on_edit
from .build import BuildDialog, _menu


class LinuxBuildDialog(BuildDialog):
    WINDOW_TITLE = "Créer un bureau Linux"
    SUBTITLE = ("Ubuntu ou Debian avec le bureau XFCE, installé et réglé automatiquement sur un serveur temporaire, "
                "puis sauvegardé en snapshot : environ 15 minutes. Connexion RDP en 1 clic, image fluide même en "
                "vidéo (H.264), son et presse-papiers.")
    DEFAULT_NAME = "Bureau Linux"
    SUBMIT_TEXT = "Créer le bureau Linux"
    MIN_DISK = linux.MIN_DISK_GB
    SNAPSHOT_GB = 5
    DURATION_TEXT = "10 à 20 min de construction"
    OS_NAME = OS_LINUX

    # --- points d'extension ------------------------------------------------------------------------
    def _watched_vars(self) -> tuple:
        return self.name_var, self.user_var

    def _after_sections(self) -> None:
        self._on_distro(self.distro_menu.get())

    def _pick_offer(self, available: list[Offer]) -> Offer | None:
        """Le moins cher avec 4 Go de RAM et le plus petit disque : le snapshot pourra démarrer partout."""
        roomy = [o for o in available if o.stype.memory >= linux.RECOMMENDED_RAM_GB]
        small = [o for o in roomy if o.stype.disk <= 40]
        return (small or roomy or available or [None])[0]

    def _credit_text(self) -> str:
        return (f"Paquets officiels de la distribution ; xrdp {linux.XRDP.version} et xorgxrdp "
                f"{linux.XORGXRDP.version} compilés depuis les sources officielles (neutrinolabs), empreintes "
                "SHA-256 vérifiées avant compilation.")

    # --- sections ----------------------------------------------------------------------------------
    def _edition_section(self) -> None:
        """Distribution et compte (à la place de l'édition Windows)."""
        self.section("Distribution", parent=self.left)
        self.distro_labels = {d.label: key for key, d in linux.DISTROS.items()}
        self.distro_menu = _menu(self.left, list(self.distro_labels), self._on_distro)
        self.distro_menu.set(linux.DISTROS[linux.DEFAULT_DISTRO].label)
        self.distro_menu.pack(fill="x")
        self.distro_note = label(self.left, "", 11, color=t.MUTED, wraplength=440)
        self.distro_note.pack(fill="x", pady=(4, 0))

        self.section("Compte", parent=self.left)
        row = ctk.CTkFrame(self.left, fg_color="transparent")
        row.pack(fill="x")
        label(row, "Identifiant", 12).pack(side="left")
        self.user_var = tk.StringVar(value=linux.default_username())
        entry = ctk.CTkEntry(row, textvariable=self.user_var, height=32, width=220, font=t.mono(12))
        entry.pack(side="left", padx=(10, 0))
        on_edit(entry, self._update_state)
        label(self.left, "Mot de passe généré, enregistré dans le Gestionnaire d'identifiants de ce PC (connexion en "
                         "1 clic) ; c'est aussi le mot de passe de « sudo ».", 11, color=t.MUTED,
              wraplength=440).pack(fill="x", pady=(4, 0))

    def _locale_section(self) -> None:
        self.section("Langue et fuseau horaire", parent=self.right)
        grid = ctk.CTkFrame(self.right, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        self.locale_labels = {loc.label: code for code, loc in linux.LOCALES.items()}
        self.tz_labels = {lb: tz for tz, lb in linux.TIMEZONES.items()}
        label(grid, "Langue du bureau", 12).grid(row=0, column=0, sticky="w", pady=4)
        self.locale_menu = _menu(grid, list(self.locale_labels), self._on_locale, width=260)
        self.locale_menu.set(linux.LOCALES[linux.DEFAULT_LOCALE].label)
        self.locale_menu.grid(row=0, column=1, sticky="e", pady=4)
        label(grid, "Fuseau horaire", 12).grid(row=1, column=0, sticky="w", pady=4)
        self.tz_menu = _menu(grid, list(self.tz_labels), lambda _v: setattr(self, "_tz_touched", True), width=260)
        self.tz_menu.grid(row=1, column=1, sticky="e", pady=4)
        self._set_timezone(linux.default_timezone(linux.DEFAULT_LOCALE))
        label(self.right, "Le clavier suit automatiquement celui de ce PC à chaque connexion.", 11,
              color=t.MUTED, wraplength=440).pack(fill="x", pady=(4, 0))

    # --- réactions ---------------------------------------------------------------------------------
    def _on_distro(self, _choice: str) -> None:
        self.distro_note.configure(text=linux.DISTROS[self._distro()].note)

    def _on_locale(self, _choice: str) -> None:
        if not self._tz_touched:
            self._set_timezone(linux.LOCALES[self._locale()].timezone)

    def _set_timezone(self, tz: str) -> None:
        self.tz_menu.set(linux.TIMEZONES.get(tz, linux.TIMEZONES["Europe/Paris"]))

    def _distro(self) -> str:
        return self.distro_labels[self.distro_menu.get()]

    def _locale(self) -> str:
        return self.locale_labels[self.locale_menu.get()]

    # --- validation --------------------------------------------------------------------------------
    def _problem(self) -> str | None:
        if not self.name_var.get().strip():
            return "Donne un nom au bureau."
        problem = linux.username_problem(self.user_var.get().strip())
        if problem:
            return f"Identifiant : {problem}"
        if self.selected_offer() is None:
            return "Choisis un type de serveur disponible."
        if not self.ctrl.public_ip:
            return "En attente de ton IP publique (elle limite l'accès SSH/RDP au serveur temporaire)…"
        return None

    def _submit(self) -> None:
        if self._problem() is not None:
            return
        offer = self.selected_offer()
        params = LinuxBuildParams(
            distro=self._distro(), username=self.user_var.get().strip(), locale=self._locale(),
            timezone=self.tz_labels[self.tz_menu.get()], server_type=offer.name, location=offer.location,
            allow_cidr=netutil.normalize_cidr(self.ctrl.public_ip), pin=self.pin_var.get(),
            apps=tuple(self.picker.requested()))
        if self.app._safe(self.ctrl.build_desktop, self.name_var.get().strip(), params) is not None:
            self.close(params)
