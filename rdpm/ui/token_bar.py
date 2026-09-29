"""Barre « Token Hetzner » en haut de la fenêtre : saisie (bloquante sans token) ou rappel compact."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from ..hetzner.errors import to_user_error
from . import theme as t
from .dialogs.base import confirm
from .widgets import caption, ghost_button, label, logical, primary_button, secondary_button

SOURCES = {
    "env": "variable d'environnement",
    ".env": "fichier .env",
    "keyring": "Gestionnaire d'identifiants",
    "session": "saisi pour cette session",
}
MIN_LENGTH = 32


class TokenBar(ctk.CTkFrame):
    def __init__(self, app) -> None:
        super().__init__(app, fg_color="transparent")
        self.app = app
        self.ctrl = app.controller
        self.grid_columnconfigure(0, weight=1)
        self._editing = False
        self._future = None
        self._sig = None
        self._build_compact()
        self._build_form()

    # --- construction ----------------------------------------------------------------------------
    def _build_compact(self) -> None:
        self.compact = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=8, border_width=1,
                                    border_color=t.BORDER)
        self.compact.grid_columnconfigure(3, weight=1)
        caption(self.compact, "Token Hetzner").grid(row=0, column=0, padx=(14, 10), pady=6)
        self.masked = ctk.CTkLabel(self.compact, text="", font=t.mono(12), text_color=t.TEXT, anchor="w")
        self.masked.grid(row=0, column=1, sticky="w")
        self.source = label(self.compact, "", 12, color=t.MUTED)
        self.source.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.forget_btn = ghost_button(self.compact, "Oublier", self._forget, width=70)
        self.forget_btn.grid(row=0, column=4, padx=(0, 4))
        ghost_button(self.compact, "Changer", self._change, width=70).grid(row=0, column=5, padx=(0, 8))

    def _build_form(self) -> None:
        self.form = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS, border_width=1,
                                 border_color=t.ACCENT)
        self.form.grid_columnconfigure(0, weight=1)
        label(self.form, "Token API Hetzner", 15, "bold").grid(row=0, column=0, columnspan=5, sticky="w",
                                                               padx=18, pady=(14, 0))
        self.help = label(self.form, "", 12, color=t.MUTED, wraplength=900)
        self.help.grid(row=1, column=0, columnspan=5, sticky="ew", padx=18, pady=(2, 10))

        self.var = tk.StringVar()
        self.entry = ctk.CTkEntry(self.form, textvariable=self.var, show="•", height=34, font=t.mono(12))
        self.entry.grid(row=2, column=0, sticky="ew", padx=(18, 8))
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.bind("<Escape>", lambda _e: self._cancel())
        self.show_btn = ghost_button(self.form, "Afficher", self._toggle_show, width=70)
        self.show_btn.grid(row=2, column=1, padx=(0, 8))
        self.cancel_btn = secondary_button(self.form, "Annuler", self._cancel, width=90)
        self.cancel_btn.grid(row=2, column=2, padx=(0, 8))
        self.ok_btn = primary_button(self.form, "Valider", self._submit, width=100)
        self.ok_btn.grid(row=2, column=3, padx=(0, 18))

        self.remember = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.form, text="Mémoriser dans le Gestionnaire d'identifiants Windows",
                        variable=self.remember, font=t.font(12), checkbox_width=18, checkbox_height=18).grid(
            row=3, column=0, columnspan=4, sticky="w", padx=18, pady=(10, 14))
        self.status = label(self.form, "", 12, color=t.MUTED)
        self.status.grid(row=4, column=0, columnspan=4, sticky="ew", padx=18, pady=(0, 12))
        self.status.grid_remove()
        self.form.bind("<Configure>", self._on_resize)

    # --- état ------------------------------------------------------------------------------------
    def refresh(self) -> None:
        ctrl = self.ctrl
        form = ctrl.locked or self._editing
        transport = ctrl.backend.transport
        token = getattr(transport, "token", "") or ""
        sig = (form, ctrl.locked, token[-4:], ctrl.token_source)
        if sig == self._sig:
            return
        self._sig = sig
        if form:
            self.compact.grid_remove()
            self.form.grid(row=0, column=0, sticky="ew")
            if ctrl.locked:
                self.help.configure(text="Aucun token trouvé (variable HETZNER_TOKEN, fichier .env ou Gestionnaire "
                                         "d'identifiants). Créez-en un en « Lecture & écriture » dans la console "
                                         "Hetzner → votre projet → Sécurité → Tokens API. L'application reste "
                                         "bloquée jusqu'à sa validation.")
                self.cancel_btn.grid_remove()
            else:
                self.help.configure(text="Le nouveau token remplace l'actuel pour cette session : l'inventaire du "
                                         "projet correspondant est rechargé.")
                self.cancel_btn.grid()
            self.after(50, self.entry.focus_set)
        else:
            self.form.grid_remove()
            self.compact.grid(row=0, column=0, sticky="ew")
            self.masked.configure(text=f"••••{token[-4:]}")
            self.source.configure(text=f"· {SOURCES.get(ctrl.token_source, 'saisi')}")
            if ctrl.token_source == "keyring":
                self.forget_btn.grid()
            else:
                self.forget_btn.grid_remove()

    def _on_resize(self, event) -> None:
        self.help.configure(wraplength=max(240, int(logical(self, event.width)) - 40))

    def _set_status(self, text_: str, kind: str | None = None) -> None:
        self.status.configure(text=text_, text_color=t.tone(kind)[0] if kind else t.MUTED)
        if text_:
            self.status.grid()
        else:
            self.status.grid_remove()

    # --- actions ---------------------------------------------------------------------------------
    def _toggle_show(self) -> None:
        hidden = self.entry.cget("show") == "•"
        self.entry.configure(show="" if hidden else "•")
        self.show_btn.configure(text="Masquer" if hidden else "Afficher")

    def _change(self) -> None:
        if self.ctrl.runner.active():
            self.app.toast("Impossible de changer de token pendant une opération en cours.", "warning")
            return
        self._editing = True
        self.refresh()

    def _cancel(self) -> None:
        if self.ctrl.locked or self._future:
            return
        self._editing = False
        self.var.set("")
        self._set_status("")
        self.refresh()

    def _forget(self) -> None:
        self.ctrl.forget_token()
        self.app.toast("Token retiré du Gestionnaire d'identifiants : il reste chargé pour cette session.", "info")
        self.refresh()

    def _submit(self) -> None:
        if self._future:
            return
        token = "".join(self.var.get().split())
        if len(token) < MIN_LENGTH:
            self._set_status("Token incomplet : un token Hetzner fait 64 caractères.", "danger")
            return
        self._set_status("Vérification auprès de Hetzner…")
        for w in (self.entry, self.ok_btn, self.cancel_btn):
            w.configure(state="disabled")
        self._future = self.ctrl.check_token(token)
        self.after(100, self._poll, token)

    def _poll(self, token: str) -> None:
        if not self._future.done():
            self.after(100, self._poll, token)
            return
        future, self._future = self._future, None
        for w in (self.entry, self.ok_btn, self.cancel_btn):
            w.configure(state="normal")
        exc = future.exception()
        if exc is not None:
            self._set_status(str(to_user_error(exc)), "danger")
            return
        ctrl = self.ctrl
        if not ctrl.locked:
            if ctrl.runner.active():
                self._set_status("Une opération a démarré entre-temps : réessayez quand elle sera terminée.",
                                 "warning")
                return
            billed = ctrl.billed_servers()
            if billed and not confirm(self.app, "Changer de token ?",
                                      f"{len(billed)} serveur(s) du projet actuel tournent encore : ils resteront "
                                      "facturés et ne seront plus visibles dans l'application tant que vous "
                                      "n'aurez pas remis ce token.", "Changer quand même", danger=True)[0]:
                return
        ctrl.apply_token(token, "session", self.remember.get())
        self.var.set("")
        self.remember.set(False)
        self._set_status("")
        self._editing = False
        self.app.set_locked(False)
        self.refresh()
