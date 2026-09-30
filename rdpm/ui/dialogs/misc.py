"""Nouveau bureau (par système), importation, ressources dormantes, réglages, aide Windows."""

from __future__ import annotations

import os
import tkinter as tk

import customtkinter as ctk

from ... import __version__, fmt
from ...constants import APP_DIR, DEFAULT_RDP_USER, OS_LINUX, OS_WINDOWS
from ...hetzner.errors import UserError
from ...labels import slugify
from ...models import ServerInfo, SnapshotInfo
from ...offers import recommended_offer
from ...ops.resources import AdoptServerOp, AdoptSnapshotOp, CleanupOp
from .. import theme as t
from ..icons import OS_NAMES, icon, icon_label, os_icon_name
from ..widgets import bind_enabled, caption, ghost_button, label, secondary_button
from .base import Modal, confirm, info
from .build import BuildDialog
from .manage import LiveModal


class ImportChooser(LiveModal):
    """Ce qui peut devenir un bureau géré, par système : une installation neuve, un snapshot ou un serveur existant."""

    def __init__(self, app) -> None:
        super().__init__(app, "Nouveau bureau", width=660)
        self.heading("Nouveau bureau",
                     "Choisis le système, puis installe un bureau neuf ou importe un snapshot ou un serveur existant. "
                     "Pour copier un bureau, utilise « Dupliquer… » sur sa carte.")
        self.os_var = tk.StringVar(value=OS_WINDOWS)
        picker = ctk.CTkFrame(self.body, fg_color="transparent")
        picker.pack(fill="x", pady=(14, 0))
        self.os_buttons = {}
        for os_name in (OS_WINDOWS, OS_LINUX):
            btn = ctk.CTkButton(picker, text=f"  {OS_NAMES[os_name]}", image=icon(picker, os_icon_name(os_name)),
                                compound="left", width=140, height=36, corner_radius=8, font=t.font(13, "bold"),
                                border_width=1, command=lambda o=os_name: self._pick(o))
            btn.pack(side="left", padx=(0, 8))
            self.os_buttons[os_name] = btn
        self._style_picker()
        self.content.pack(fill="x")
        self.buttons(("Fermer", self.cancel, "primary"))

    def _pick(self, os_name: str) -> None:
        self.os_var.set(os_name)
        self._style_picker()
        self._version = None   # redessiner au prochain passage

    def _style_picker(self) -> None:
        for os_name, btn in self.os_buttons.items():
            on = os_name == self.os_var.get()
            btn.configure(fg_color=t.tone("accent")[1] if on else "transparent", hover_color=t.SURFACE_3,
                          border_color=t.ACCENT if on else t.BORDER, text_color=t.TEXT)

    def render(self, parent) -> None:
        if self.os_var.get() == OS_LINUX:
            self._row(parent, "Installer une distribution Linux",
                      "Bureaux Linux à la demande (Ubuntu, Debian…) : bientôt disponible.", None, "Bientôt",
                      os_name=OS_LINUX)
            label(parent, "L'import de snapshots et de serveurs Linux arrivera avec leur prise en charge.", 12,
                  color=t.MUTED, wraplength=600).pack(fill="x", pady=(14, 0))
            return
        g = self.ctrl.grouping
        importing = any(isinstance(op, (AdoptSnapshotOp, AdoptServerOp)) for op in self.ctrl.runner.active())
        self._row(parent, "Créer un Windows de référence",
                  "Installation automatique de Windows Server (évaluation Microsoft) ou d'une ISO à toi sur un "
                  "serveur temporaire, puis snapshot. Langue, clavier et fuseau au choix · ≈ 30–40 min.",
                  self._build, "Créer…")
        if not (g.unmanaged_snapshots or g.unmanaged_servers):
            label(parent, "Rien à importer : tous les snapshots et serveurs du projet sont déjà gérés.", 12,
                  color=t.MUTED, wraplength=600).pack(fill="x", pady=(14, 0))
            return
        caption(parent, "À importer").pack(fill="x", pady=(16, 0))
        if importing:
            label(parent, "Importation en cours…", 12, color=t.ACCENT).pack(fill="x", pady=(4, 0))
        for snap in g.unmanaged_snapshots:
            self._row(parent, f"Snapshot « {snap.description or snap.id} »",
                      f"{fmt.gb(snap.image_size)} · disque {snap.disk_size} Go · créé le {fmt.date_long(snap.created)}"
                      + (" · protégé" if snap.protected else ""),
                      None if importing else lambda s=snap: self._adopt(snapshot=s))
        for srv in g.unmanaged_servers:
            self._row(parent, f"Serveur « {srv.name} »", f"{srv.spec} · {fmt.status(srv.status)}",
                      None if importing else lambda s=srv: self._adopt(server=s))

    def _row(self, parent, title: str, detail: str, action, button: str = "Importer…",
             os_name: str = OS_WINDOWS) -> None:
        box = ctk.CTkFrame(parent, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
        box.pack(fill="x", pady=(10, 0))
        box.grid_columnconfigure(1, weight=1)
        icon_label(box, os_icon_name(os_name)).grid(row=0, column=0, rowspan=2, sticky="n", padx=(12, 0), pady=12)
        label(box, title, 13, "bold", color=t.TEXT if action else t.MUTED).grid(
            row=0, column=1, sticky="w", padx=10, pady=(10, 0))
        label(box, detail, 12, color=t.MUTED, wraplength=440).grid(row=1, column=1, sticky="w", padx=10, pady=(0, 10))
        secondary_button(box, button, action or (lambda: None), width=100,
                         state="normal" if action else "disabled").grid(row=0, column=2, rowspan=2, padx=12)

    def _adopt(self, **kw) -> None:
        self.close(None)
        AdoptDialog(self.app, **kw)

    def _build(self) -> None:
        self.close(None)
        BuildDialog(self.app)


class AdoptDialog(Modal):
    def __init__(self, app, snapshot: SnapshotInfo | None = None, server: ServerInfo | None = None) -> None:
        super().__init__(app, "Importer comme bureau", width=560)
        self.app, self.snapshot, self.server = app, snapshot, server
        ctrl = app.controller
        default = (snapshot.created_from_name if snapshot else server.name) or "Bureau"
        default = default.replace("-", " ").strip().capitalize()
        self.heading("Importer comme bureau",
                     "L'application le gérera : lancement, sauvegardes, fermeture, coûts.")
        if snapshot and ctrl.static:
            offer = recommended_offer(ctrl.static.server_types, "nbg1", snapshot.disk_size, snapshot.architecture)
            hint = f" · le moins cher : {offer.name} à {fmt.eur_h(offer.price_h)}" if offer else ""
            self.text(f"Disque de {snapshot.disk_size} Go : il faut un type avec au moins {snapshot.disk_size} Go"
                      f"{hint}.", t.MUTED, 12)
        self.section("Nom du bureau")
        self.name_var = tk.StringVar(value=default)
        ctk.CTkEntry(self.body, textvariable=self.name_var, height=32, font=t.font(12)).pack(fill="x")
        self.slug_hint = label(self.body, "", 11, color=t.FAINT)
        self.slug_hint.pack(fill="x", pady=(2, 0))
        self.name_var.trace_add("write", lambda *_: self.slug_hint.configure(
            text=f"Identifiant : {slugify(self.name_var.get())}"))
        self.slug_hint.configure(text=f"Identifiant : {slugify(default)}")
        self.section("Compte du bureau (pour la connexion en 1 clic)")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x")
        self.user_var = tk.StringVar(value=DEFAULT_RDP_USER)
        ctk.CTkEntry(row, textvariable=self.user_var, height=32, width=180, font=t.font(12)).pack(side="left")
        self.pw = ctk.CTkEntry(row, height=32, font=t.font(12), placeholder_text="Mot de passe (facultatif)")
        self.pw.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.pw.bind("<Key>", lambda _e: self.after(1, lambda: self.pw.configure(show="•" if self.pw.get() else "")))
        label(self.body, "Stocké dans le Gestionnaire d'identifiants de ce PC, jamais en clair.", 11,
              color=t.MUTED).pack(fill="x", pady=(4, 0))
        self.pin_var = tk.BooleanVar(value=True)
        if snapshot:
            text_ = "Épingler comme « version d'origine » (jamais supprimée par la rétention)"
            if snapshot.protected:
                text_ = "Déjà protégé : restera épinglé comme version d'origine"
            box = ctk.CTkCheckBox(self.body, text=text_, variable=self.pin_var, font=t.font(12))
            box.pack(anchor="w", pady=(12, 0))
            if snapshot.protected:
                box.configure(state="disabled")
        self.error = label(self.body, "", 12, color=t.tone("danger")[0])
        self.error.pack(fill="x", pady=(6, 0))
        ok = self.buttons(("Annuler", self.cancel, "secondary"), ("Importer", self._ok, "primary"))[1]
        bind_enabled(ok, lambda: bool(self.name_var.get().strip()), self.name_var)

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            return
        ctrl = self.app.controller
        try:
            if self.snapshot:
                ctrl.adopt_snapshot(self.snapshot, name, self.user_var.get().strip(), self.pw.get() or None,
                                    self.pin_var.get())
            else:
                ctrl.adopt_server(self.server, name, self.user_var.get().strip(), self.pw.get() or None)
        except UserError as exc:
            self.error.configure(text=str(exc))
            return
        self.close(True)


class DormantDialog(LiveModal):
    def __init__(self, app) -> None:
        super().__init__(app, "Ressources dormantes", width=660)
        self.heading("Ressources dormantes",
                     "Ce qui existe chez le fournisseur sans servir à un bureau : supprime-le ou rattache-le.")
        self.content.pack(fill="x")
        self.buttons(("Fermer", self.cancel, "primary"))

    def render(self, parent) -> None:
        items = self.ctrl.dormant_items()
        st = "disabled" if any(isinstance(op, CleanupOp) for op in self.ctrl.runner.active()) else "normal"
        without_ip = [d for d in self.ctrl.grouping.desktops if not d.fixed_ip]
        if not items:
            label(parent, "Rien à signaler : aucune ressource inutile n'est facturée.", 12,
                  color=t.tone("success")[0]).pack(fill="x", pady=(14, 0))
        total = 0.0
        for item in items:
            total += item.monthly
            box = ctk.CTkFrame(parent, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
            box.pack(fill="x", pady=(10, 0))
            box.grid_columnconfigure(0, weight=1)
            label(box, item.title, 13, "bold", wraplength=420).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
            cost = f" · {fmt.eur_m(item.monthly)}" if item.monthly else ""
            label(box, f"{item.detail}{cost}", 12, color=t.MUTED).grid(
                row=1, column=0, sticky="w", padx=12, pady=(0, 10))
            actions = ctk.CTkFrame(box, fg_color="transparent")
            actions.grid(row=0, column=1, rowspan=2, padx=8)
            if item.kind == "snapshot":
                ghost_button(actions, "Importer…", lambda s=item.resource: AdoptDialog(self.app, snapshot=s),
                             width=10, text_color=t.ACCENT, state=st).pack(side="left")
            if item.kind == "ip" and without_ip:
                ghost_button(actions, "Utiliser comme IP fixe…", lambda i=item.resource: self._use_ip(i), width=10,
                             text_color=t.ACCENT, state=st).pack(side="left")
            ghost_button(actions, "Supprimer…", lambda i=item: self._delete(i), width=10,
                         text_color=t.tone("danger")[0], state=st).pack(side="left")
        if items:
            label(parent, f"Total : {fmt.eur_m(total)}", 12, "bold", color=t.MUTED).pack(fill="x", pady=(10, 0))

    def _use_ip(self, ip) -> None:
        candidates = [d for d in self.ctrl.grouping.desktops if not d.fixed_ip]
        if not candidates:
            return
        dlg = _PickDesktop(self, candidates, f"Associer {ip.ip} ({ip.location}) au bureau :")
        slug = dlg.show()
        if slug:
            self.run(self.ctrl.fixed_ip, slug, "adopt", ip=ip)

    def _delete(self, item) -> None:
        extra = " Ce snapshot est protégé : la protection sera retirée." if item.kind == "snapshot" and \
            item.resource.protected else ""
        saving = f" (−{fmt.eur_m(item.monthly)})" if item.monthly else ""
        ok, _ = confirm(self, "Supprimer ?", f"{item.title} sera définitivement supprimé{saving}.{extra}",
                        "Supprimer", danger=True)
        if ok:
            self.run(self.ctrl.cleanup, item)


class _PickDesktop(Modal):
    def __init__(self, master, desktops, prompt: str) -> None:
        super().__init__(master, "Choisir un bureau", width=440)
        self.heading("Choisir un bureau")
        self.text(prompt, t.MUTED, 12)
        self.map = {d.name: d.slug for d in desktops}
        self.var = tk.StringVar(value=next(iter(self.map)))
        ctk.CTkOptionMenu(self.body, values=list(self.map), variable=self.var, height=32, font=t.font(12)).pack(
            fill="x", pady=(10, 0))
        self.buttons(("Annuler", self.cancel, "secondary"), ("Associer", lambda: self.close(self.map[self.var.get()]),
                                                              "primary"))


class SettingsDialog(Modal):
    CHOICES = {
        "retention": ("Sauvegardes conservées par bureau (hors épinglées)", {"1": 1, "2": 2, "3": 3, "5": 5, "10": 10}),
        "reminder_hours": ("Rappel « toujours besoin ? » après", {"Jamais": 0, "1 h": 1, "2 h": 2, "3 h": 3, "4 h": 4,
                                                                  "6 h": 6, "8 h": 8}),
        "shutdown_timeout_s": ("Attente de l'arrêt du système avant de demander", {"2 min": 120, "5 min": 300,
                                                                                    "10 min": 600, "20 min": 1200}),
        "quit_force_countdown_s": ("En quittant, forcer l'arrêt d'un bureau bloqué après",
                                   {"30 s": 30, "60 s": 60, "2 min": 120, "10 min": 600}),
        "appearance": ("Apparence", {"Système": "system", "Sombre": "dark", "Clair": "light"}),
    }

    def __init__(self, app) -> None:
        super().__init__(app, "Réglages", width=560)
        self.app = app
        cfg = app.controller.config
        self.heading("Réglages")
        self.vars: dict[str, tk.StringVar] = {}
        grid = ctk.CTkFrame(self.body, fg_color="transparent")
        grid.pack(fill="x", pady=(10, 0))
        grid.grid_columnconfigure(0, weight=1)
        for row, (key, (text_, options)) in enumerate(self.CHOICES.items()):
            current = cfg.get(key)
            label_for = next((k for k, v in options.items() if v == current), next(iter(options)))
            var = tk.StringVar(value=label_for)
            self.vars[key] = var
            label(grid, text_, 12).grid(row=row, column=0, sticky="w", pady=5)
            ctk.CTkOptionMenu(grid, values=list(options), variable=var, width=140, height=30, font=t.font(12)).grid(
                row=row, column=1, sticky="e", pady=5)
        self.confirm_var = tk.BooleanVar(value=bool(cfg.get("confirm_save_close")))
        self.auto_var = tk.BooleanVar(value=bool(cfg.get("auto_connect")))
        ctk.CTkSwitch(self.body, text="Demander confirmation avant « Sauvegarder & fermer »",
                      variable=self.confirm_var, font=t.font(12)).pack(anchor="w", pady=(12, 0))
        ctk.CTkSwitch(self.body, text="Connexion automatique par défaut dès que le bureau est prêt",
                      variable=self.auto_var, font=t.font(12)).pack(anchor="w", pady=(8, 0))
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=(16, 0))
        secondary_button(row, "Préparer Windows…", lambda: windows_prep_help(self), width=170).pack(side="left")
        if hasattr(os, "startfile"):   # Windows uniquement
            secondary_button(row, "Ouvrir le dossier de l'application", self._open_dir, width=240).pack(
                side="left", padx=8)
        mode = {"fake": "simulation", "readonly": "lecture seule"}.get(app.controller.mode, "normal")
        label(self.body, f"Version {__version__} · mode {mode} · {APP_DIR}", 11, color=t.FAINT).pack(fill="x",
                                                                                                 pady=(12, 0))
        save = self.buttons(("Annuler", self.cancel, "secondary"), ("Enregistrer", self._save, "primary"))[1]
        initial = self._values()
        bind_enabled(save, lambda: self._values() != initial, *self.vars.values(), self.confirm_var, self.auto_var)

    def _values(self) -> tuple:
        return (tuple(var.get() for var in self.vars.values()), self.confirm_var.get(), self.auto_var.get())

    def _open_dir(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(APP_DIR)  # noqa: S606

    def _save(self) -> None:
        cfg = self.app.controller.config
        for key, var in self.vars.items():
            cfg.settings[key] = self.CHOICES[key][1][var.get()]
        cfg.settings["confirm_save_close"] = self.confirm_var.get()
        cfg.settings["auto_connect"] = self.auto_var.get()
        cfg.save()
        ctk.set_appearance_mode(cfg.get("appearance"))
        self.close(True)


def windows_prep_help(master) -> None:
    info(master, "Préparer Windows pour les sauvegardes", [
        "Quelques réglages à faire une fois dans le bureau Windows pour des sauvegardes rapides, petites et "
        "fiables :",
        "•  Arrêt ACPI : le bouton d'alimentation doit « Arrêter » (Options d'alimentation), sinon "
        "« Sauvegarder & fermer » devra forcer l'arrêt.",
        "•  Désactiver la mise en veille prolongée et le démarrage rapide : le fichier hiberfil.sys (plusieurs "
        "Go) disparaît des snapshots.",
        "•  Définir des heures d'activité Windows Update pour éviter les arrêts interminables.",
        "•  Avant une sauvegarde importante, TRIM du disque : les blocs libérés ne sont plus copiés.",
        "PowerShell (administrateur) :",
    ], code="powercfg /h off\n"
            "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3\n"
            "powercfg /setactive SCHEME_CURRENT\n"
            "reg add \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power\" /v HiberbootEnabled /t REG_DWORD /d 0 /f\n"
            "Optimize-Volume -DriveLetter C -ReTrim -Verbose")
