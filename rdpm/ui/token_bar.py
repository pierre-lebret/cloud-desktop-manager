"""Barre « Projets » en haut de la fenêtre : une ligne par clé API chargée (fournisseur, nom, état), et l'ajout
d'une clé.

Sans aucune clé, le formulaire est affiché et tout le reste est bloqué.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from ..hetzner.errors import to_user_error
from ..projects import SOURCE_LABELS, ProjectSpec, project_id
from ..providers import DEFAULT_PROVIDER, PROVIDERS, provider
from . import theme as t
from .dialogs.base import ask_text, confirm
from .icons import icon_label
from .widgets import Tooltip, bind_enabled, ghost_button, label, logical, primary_button, secondary_button


class ProjectsBar(ctk.CTkFrame):
    def __init__(self, app) -> None:
        super().__init__(app, fg_color="transparent")
        self.app, self.hub = app, app.hub
        self.grid_columnconfigure(0, weight=1)
        self._adding = False
        self._future = None
        self._sig = None
        self.rows = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=8, border_width=1, border_color=t.BORDER)
        self.rows.grid_columnconfigure(3, weight=1)
        self._build_form()

    # --- formulaire d'ajout ----------------------------------------------------------------------
    def _build_form(self) -> None:
        self.form = ctk.CTkFrame(self, fg_color=t.SURFACE, corner_radius=t.RADIUS, border_width=1,
                                 border_color=t.ACCENT)
        self.form.grid_columnconfigure(1, weight=1)
        self.form_title = label(self.form, "Clé API", 15, "bold")
        self.form_title.grid(row=0, column=0, columnspan=6, sticky="w", padx=18, pady=(14, 0))
        self.help = label(self.form, "", 12, color=t.MUTED, wraplength=900)
        self.help.grid(row=1, column=0, columnspan=6, sticky="ew", padx=18, pady=(2, 10))

        # Fournisseur du projet : Hetzner Cloud seulement pour l'instant.
        self.provider_names = {p.name: p.id for p in PROVIDERS.values()}
        self.provider_var = tk.StringVar(value=provider(DEFAULT_PROVIDER).name)
        prov = ctk.CTkFrame(self.form, fg_color="transparent")
        prov.grid(row=2, column=0, columnspan=6, sticky="w", padx=18, pady=(0, 8))
        label(prov, "Fournisseur", 12, color=t.MUTED).pack(side="left", padx=(0, 8))
        ctk.CTkOptionMenu(prov, values=list(self.provider_names), variable=self.provider_var, width=180, height=30,
                          font=t.font(12), command=lambda _v: self._sig_reset()).pack(side="left")
        label(prov, "D'autres fournisseurs arriveront plus tard.", 11, color=t.FAINT).pack(side="left", padx=(10, 0))

        self.name_var = tk.StringVar()
        ctk.CTkEntry(self.form, textvariable=self.name_var, height=34, width=180, font=t.font(12),
                     placeholder_text="Nom du projet").grid(row=3, column=0, padx=(18, 8))
        self.var = tk.StringVar()
        self.entry = ctk.CTkEntry(self.form, textvariable=self.var, show="•", height=34, font=t.mono(12))
        self.entry.grid(row=3, column=1, sticky="ew", padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.bind("<Escape>", lambda _e: self._cancel())
        self.show_btn = ghost_button(self.form, "Afficher", self._toggle_show, width=70)
        self.show_btn.grid(row=3, column=2, padx=(0, 8))
        self.cancel_btn = secondary_button(self.form, "Annuler", self._cancel, width=90)
        self.cancel_btn.grid(row=3, column=3, padx=(0, 8))
        self.ok_btn = primary_button(self.form, "Ajouter", self._submit, width=100)
        self.ok_btn.grid(row=3, column=4, padx=(0, 18))
        self._update_ok = bind_enabled(self.ok_btn, lambda: len("".join(self.var.get().split())) >=
                                       self._provider().token_min_length and not self._future, self.var)

        self.remember = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.form, text="Mémoriser dans le Gestionnaire d'identifiants Windows (rechargée au "
                                        "prochain démarrage)", variable=self.remember, font=t.font(12),
                        checkbox_width=18, checkbox_height=18).grid(row=4, column=0, columnspan=5, sticky="w",
                                                                    padx=18, pady=(10, 14))
        self.status = label(self.form, "", 12, color=t.MUTED)
        self.status.grid(row=5, column=0, columnspan=5, sticky="ew", padx=18, pady=(0, 12))
        self.status.grid_remove()
        self.form.bind("<Configure>", self._on_resize)

    def _provider(self):
        return provider(self.provider_names.get(self.provider_var.get()))

    def _sig_reset(self) -> None:
        self._sig = None
        self.refresh()

    def _on_resize(self, event) -> None:
        self.help.configure(wraplength=max(240, int(logical(self, event.width)) - 40))

    # --- état ------------------------------------------------------------------------------------
    def _project_state(self, ctrl) -> tuple[str, str]:
        if ctrl.token_invalid:
            return "clé refusée", "danger"
        if ctrl.inventory is None:
            return ("injoignable", "warning") if ctrl.refresh_error else ("chargement…", None)
        n = len(ctrl.grouping.desktops)
        return f"{n} bureau{'x' if n > 1 else ''}", "success"

    def refresh(self) -> None:
        hub = self.hub
        form = hub.locked or self._adding
        sig = (form, hub.locked, tuple((c.project.id, c.project.name, c.project.source, self._project_state(c))
                                       for c in hub.controllers))
        if sig == self._sig:
            return
        self._sig = sig
        for child in self.rows.winfo_children():
            child.destroy()
        for row, ctrl in enumerate(hub.controllers):
            self._project_row(row, ctrl)
        add = ghost_button(self.rows, "+ Ajouter une clé API", self._add, width=160,
                           state="disabled" if form else "normal")
        add.grid(row=len(hub.controllers), column=0, columnspan=6, sticky="w", padx=8, pady=(0, 6))
        if hub.controllers:
            self.rows.grid(row=0, column=0, sticky="ew")
        else:
            self.rows.grid_remove()
        if form:
            self.form.grid(row=1, column=0, sticky="ew", pady=(8 if hub.controllers else 0, 0))
            prov = self._provider()
            if hub.locked:
                self.form_title.configure(text="Clé API requise")
                self.help.configure(text=f"Aucune clé trouvée ({prov.token_env} dans le fichier .env ou clé "
                                         f"mémorisée). Crée une clé {prov.name} dans la {prov.token_help}. "
                                         "L'application reste bloquée jusqu'à sa validation.")
                self.cancel_btn.grid_remove()
            else:
                self.form_title.configure(text="Ajouter un projet")
                self.help.configure(text=f"Une clé API donne accès à un projet chez le fournisseur ({prov.name} : "
                                         f"{prov.token_help}). Ses bureaux s'affichent avec ceux des autres projets ; "
                                         "la clé du fichier .env reste toujours chargée.")
                self.cancel_btn.grid()
            if not self.name_var.get():
                self.name_var.set(f"Projet {len(hub.controllers) + 1}")
            self.after(50, self.entry.focus_set)
        else:
            self.form.grid_remove()

    def _project_row(self, row: int, ctrl) -> None:
        spec: ProjectSpec = ctrl.project
        pad = (6, 0) if row == 0 else (0, 0)
        prov = spec.provider
        badge = icon_label(self.rows, prov.icon)
        badge.grid(row=row, column=0, padx=(14, 8), pady=pad, sticky="w")
        Tooltip(badge, f"Fournisseur : {prov.name}")
        label(self.rows, spec.name, 12, "bold").grid(row=row, column=1, sticky="w", pady=pad)
        detail = f"{prov.short} · {spec.masked} · {SOURCE_LABELS.get(spec.source, spec.source)}"
        ctk.CTkLabel(self.rows, text=detail, font=t.mono(11), text_color=t.MUTED, anchor="w").grid(
            row=row, column=2, sticky="w", padx=(10, 0), pady=pad)
        text_, kind = self._project_state(ctrl)
        label(self.rows, f"· {text_}", 12, color=t.tone(kind)[0] if kind else t.MUTED).grid(
            row=row, column=3, sticky="w", padx=(8, 0), pady=pad)
        ghost_button(self.rows, "Renommer", lambda c=ctrl: self._rename(c), width=76).grid(
            row=row, column=4, padx=(0, 2), pady=pad)
        if spec.removable:
            ghost_button(self.rows, "Retirer", lambda c=ctrl: self._remove(c), width=66,
                         text_color=t.tone("danger")[0]).grid(row=row, column=5, padx=(0, 8), pady=pad)
        else:
            label(self.rows, "toujours chargée", 11, color=t.FAINT).grid(row=row, column=5, padx=(0, 12), pady=pad)

    def _set_status(self, text_: str, kind: str | None = None) -> None:
        self.status.configure(text=text_, text_color=t.tone(kind)[0] if kind else t.MUTED)
        if text_:
            self.status.grid()
        else:
            self.status.grid_remove()

    # --- actions ---------------------------------------------------------------------------------
    def _add(self) -> None:
        self._adding = True
        self._sig = None
        self.refresh()

    def _cancel(self) -> None:
        if self.hub.locked or self._future:
            return
        self._adding = False
        self.var.set("")
        self.name_var.set("")
        self._set_status("")
        self._sig = None
        self.refresh()

    def _toggle_show(self) -> None:
        hidden = self.entry.cget("show") == "•"
        self.entry.configure(show="" if hidden else "•")
        self.show_btn.configure(text="Masquer" if hidden else "Afficher")

    def _rename(self, ctrl) -> None:
        name = ask_text(self.app, "Renommer le projet", "Nom affiché pour ce projet.", ctrl.project.name,
                        "Renommer", allow_unchanged=False)
        if name and name.strip() and name.strip() != ctrl.project.name:
            if self.app.store:
                self.app.store.rename(ctrl.project, name.strip())
            else:
                ctrl.project.name = name.strip()
            self._sig = None
            self.app.on_model_changed()

    def _remove(self, ctrl) -> None:
        spec = ctrl.project
        forget = spec.source == "keyring"
        ok, _ = confirm(self.app, f"Retirer le projet « {spec.name} » ?",
                        f"Ses bureaux disparaîtront de l'application (ils restent chez {spec.provider.short})."
                        + (" La clé sera supprimée du Gestionnaire d'identifiants." if forget else ""),
                        "Retirer", danger=True)
        if ok and self.app.remove_project(ctrl, forget=forget):
            self._sig = None
            self.refresh()

    def _submit(self) -> None:
        if self._future:
            return
        token = "".join(self.var.get().split())
        name = self.name_var.get().strip() or f"Projet {len(self.hub.controllers) + 1}"
        prov = self._provider()
        if len(token) < prov.token_min_length:
            self._set_status(f"Clé incomplète : au moins {prov.token_min_length} caractères.", "danger")
            return
        if self.hub.find_token(token):
            self._set_status("Cette clé est déjà chargée.", "warning")
            return
        self._set_status(f"Vérification auprès de {prov.short}…")
        for w in (self.entry, self.ok_btn, self.cancel_btn):
            w.configure(state="disabled")
        self._future = self.hub.check_token(token)
        self.after(100, self._poll, token, name)

    def _poll(self, token: str, name: str) -> None:
        if not self._future.done():
            self.after(100, self._poll, token, name)
            return
        future, self._future = self._future, None
        for w in (self.entry, self.cancel_btn):
            w.configure(state="normal")
        self._update_ok()
        exc = future.exception()
        if exc is not None:
            err = to_user_error(exc)
            self._set_status(str(err), "danger")
            self._offer_forget_rejected(token, err)
            return
        spec = ProjectSpec(project_id(token), name, token, "session", self._provider().id)
        if self.remember.get() and self.app.store:
            try:
                self.app.store.remember(spec)
            except Exception as exc:  # noqa: BLE001
                self.app.toast(f"Clé non mémorisée dans le Gestionnaire d'identifiants : {exc}", "warning")
        self.app.add_project(spec)
        self.var.set("")
        self.name_var.set("")
        self._set_status("")
        self._adding = False
        self._sig = None
        self.refresh()

    def _offer_forget_rejected(self, token: str, err) -> None:
        """Clé refusée à la saisie : si elle est encore mémorisée, proposer de la supprimer du Gestionnaire."""
        store = self.app.store
        if err.code not in ("unauthorized", "forbidden") or not store:
            return
        pid = project_id(token)
        if not any(p.get("id") == pid for p in store._saved()):
            return
        ok, _ = confirm(self.app, "Clé API refusée", f"{self._provider().short} refuse cette clé, mais elle est encore "
                        "mémorisée dans le Gestionnaire d'identifiants.\n\nSupprimer la clé du Gestionnaire ?",
                        "Supprimer la clé",
                        danger=True)
        if ok:
            ctrl = self.hub.get(pid)
            if ctrl:
                self.app.remove_project(ctrl, forget=True)
            else:
                store.forget(pid)
            self.app.toast("Clé supprimée du Gestionnaire d'identifiants", "info")
