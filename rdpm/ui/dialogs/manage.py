"""Dialogues de gestion : pare-feu, volumes, sauvegardes, IP fixe, identifiants, duplication, suppression."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from ... import fmt, netutil
from ...constants import MAX_FIREWALL_SOURCES, VOLUME_MAX_GB, VOLUME_MIN_GB
from ...hetzner.errors import UserError
from ...labels import slugify
from ...models import Desktop
from .. import theme as t
from ..widgets import caption, ghost_button, label, primary_button, secondary_button
from .base import Modal, confirm, confirm_typed, info


class LiveModal(Modal):
    """Modale qui se redessine quand l'inventaire change (après chaque opération)."""

    def __init__(self, app, title: str, width: int = 600) -> None:
        super().__init__(app, title, width=width)
        self.app, self.ctrl = app, app.controller
        self._version = None
        self.content = ctk.CTkFrame(self.body, fg_color="transparent", height=1)
        self.after(10, self._poll)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        busy = bool(self.ctrl.runner.active())
        version = (self.ctrl.inventory_version, busy)
        if version != self._version:
            self._version = version
            for child in self.content.winfo_children():
                child.destroy()
            self.render(self.content)
            self.refit()
        self.after(500, self._poll)

    def render(self, parent) -> None:
        raise NotImplementedError

    def run(self, fn: Callable, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except (UserError, ValueError) as exc:
            self.app.toast(str(exc), "error")


# --- pare-feu -------------------------------------------------------------------------------------
class FirewallDialog(LiveModal):
    def __init__(self, app, desktop: Desktop | None = None) -> None:
        self.desktop_slug = desktop.slug if desktop else None
        super().__init__(app, "Pare-feu RDP", width=640)
        self.heading("Adresses autorisées en RDP",
                     "Seules ces adresses peuvent joindre tes bureaux sur le port 3389 (TCP et UDP).")
        self.content.pack(fill="x")
        self.desc_var = tk.StringVar(value="Maison")
        self.cidr_entry = None
        self.error = None
        self.buttons(("Fermer", self.cancel, "primary"))

    def _firewall(self):
        d = self.ctrl.desktop(self.desktop_slug) if self.desktop_slug else None
        fws = self.ctrl.firewalls_of(d) if d else []
        return fws[0] if fws else self.ctrl.rdp_firewall()

    def render(self, parent) -> None:
        fw = self._firewall()
        ip = self.ctrl.public_ip
        d = self.ctrl.desktop(self.desktop_slug) if self.desktop_slug else None
        if fw is None:
            box = ctk.CTkFrame(parent, fg_color="transparent")
            box.pack(fill="x", pady=(14, 0))
            label(box, "Aucun pare-feu RDP géré.", 13, "bold").pack(anchor="w")
            adoptable = self.ctrl.grouping.adoptable_firewalls
            if adoptable:
                primary_button(box, f"Adopter « {adoptable[0].name} »",
                               lambda: self.run(self.ctrl.adopt_firewall, adoptable[0].id), width=220).pack(
                    anchor="w", pady=(8, 0))
            elif ip:
                primary_button(box, f"Créer un pare-feu limité à {ip}",
                               lambda: self.run(self.ctrl.create_firewall, netutil.normalize_cidr(ip), "Mon IP"),
                               width=260).pack(anchor="w", pady=(8, 0))
            return
        sub = f"Pare-feu « {fw.name} »"
        if d and d.server and fw.id not in d.server.firewall_ids:
            sub += " — non appliqué à ce serveur"
        label(parent, sub, 12, color=t.MUTED).pack(fill="x", pady=(10, 6))
        if d and d.server and not d.server.firewall_ids:
            secondary_button(parent, "Appliquer à ce serveur", lambda: self.run(self.ctrl.apply_firewall, d.slug),
                             width=200).pack(anchor="w", pady=(0, 8))
        table = ctk.CTkFrame(parent, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
        table.pack(fill="x")
        table.grid_columnconfigure(1, weight=1)
        sources = netutil.rdp_sources(fw.rules)
        if not sources:
            label(table, "Aucune adresse autorisée : personne ne peut se connecter.", 12,
                  color=t.tone("warning")[0]).grid(row=0, column=0, padx=12, pady=12)
        for row, (cidr, desc) in enumerate(sources):
            mine = bool(ip) and netutil.ip_allowed(ip, [cidr])
            ctk.CTkLabel(table, text=cidr, font=t.mono(12), text_color=t.TEXT, anchor="w").grid(
                row=row, column=0, sticky="w", padx=12, pady=6)
            label(table, (desc or "—") + ("   · ton IP actuelle" if mine else ""), 12,
                  color=t.tone("success")[0] if mine else t.MUTED).grid(row=row, column=1, sticky="w")
            ghost_button(table, "Retirer", lambda c=cidr, m=mine: self._remove(fw.id, c, m), width=70,
                         text_color=t.tone("danger")[0]).grid(row=row, column=2, padx=8)
        busy = any(op.title == "Pare-feu" for op in self.ctrl.runner.active())
        if busy:
            label(parent, "Mise à jour du pare-feu…", 12, color=t.ACCENT).pack(fill="x", pady=(8, 0))

        add = ctk.CTkFrame(parent, fg_color="transparent")
        add.pack(fill="x", pady=(16, 0))
        caption(add, "Ajouter une adresse").pack(anchor="w")
        if ip and not any(netutil.ip_allowed(ip, [c]) for c, _ in sources):
            primary_button(add, f"Autoriser mon IP actuelle ({ip})",
                           lambda: self._add(fw.id, ip, self.desc_var.get() or "Mon IP"), width=280).pack(
                anchor="w", pady=(6, 8))
        row = ctk.CTkFrame(add, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))
        self.cidr_entry = ctk.CTkEntry(row, placeholder_text="IP ou réseau (ex. 203.0.113.7 ou 10.0.0.0/24)",
                                       height=32, width=290, font=t.mono(12))
        self.cidr_entry.pack(side="left")
        ctk.CTkEntry(row, textvariable=self.desc_var, placeholder_text="Description", height=32, width=150,
                     font=t.font(12)).pack(side="left", padx=8)
        secondary_button(row, "Ajouter", lambda: self._add(fw.id, self.cidr_entry.get(), self.desc_var.get()),
                         width=90).pack(side="left")
        self.error = label(add, "", 12, color=t.tone("danger")[0])
        self.error.pack(fill="x", pady=(4, 0))
        if len(sources) >= MAX_FIREWALL_SOURCES:
            self.error.configure(text=f"Limite de {MAX_FIREWALL_SOURCES} adresses atteinte : retire les anciennes.")

    def _add(self, fw_id: int, raw: str, desc: str) -> None:
        try:
            cidr = netutil.normalize_cidr(raw)
        except ValueError as exc:
            if self.error is not None:
                self.error.configure(text=str(exc))
            return
        if self.error is not None:
            self.error.configure(text="")
        self.run(self.ctrl.firewall_change, fw_id, add=[(cidr, (desc or "").strip()[:60] or "Accès")])
        if self.cidr_entry is not None and self.cidr_entry.winfo_exists():
            self.cidr_entry.delete(0, "end")

    def _remove(self, fw_id: int, cidr: str, mine: bool) -> None:
        message = f"L'adresse {cidr} ne pourra plus se connecter en RDP."
        if mine:
            message += "\n\nC'est ton IP actuelle : tu perdras l'accès depuis ce poste."
        ok, _ = confirm(self, "Retirer l'adresse ?", message, "Retirer", danger=mine)
        if ok:
            self.run(self.ctrl.firewall_change, fw_id, remove=[cidr])


# --- volumes --------------------------------------------------------------------------------------
class AddVolumeDialog(Modal):
    def __init__(self, app, desktop: Desktop) -> None:
        super().__init__(app, "Ajouter un volume", width=520)
        self.app, self.desktop = app, desktop
        pricing = app.controller.static.pricing
        self.price = pricing.volume_gb_month
        existing = {v.name for v in app.controller.inventory.volumes} if app.controller.inventory else set()
        base = f"{desktop.slug}-data"
        name = base
        i = 2
        while name in existing:
            name, i = f"{base}-{i}", i + 1
        self.heading("Ajouter un volume",
                     f"Disque supplémentaire attaché à chaud à « {desktop.name} » ({desktop.server.location}).")
        self.section("Nom")
        self.name_var = tk.StringVar(value=name)
        ctk.CTkEntry(self.body, textvariable=self.name_var, height=32, font=t.font(12)).pack(fill="x")
        self.section("Taille")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x")
        self.size_var = tk.StringVar(value="50")
        self.slider = ctk.CTkSlider(row, from_=VOLUME_MIN_GB, to=1000, number_of_steps=99,
                                    command=lambda v: self.size_var.set(str(int(v))))
        self.slider.set(50)
        self.slider.pack(side="left", fill="x", expand=True)
        ctk.CTkEntry(row, textvariable=self.size_var, width=80, height=32, font=t.font(12)).pack(side="left", padx=8)
        label(row, "Go", 12).pack(side="left")
        self.cost = label(self.body, "", 13, "bold")
        self.cost.pack(fill="x", pady=(10, 0))
        self.error = label(self.body, "", 12, color=t.tone("danger")[0])
        self.error.pack(fill="x")
        self.notice("Le volume reste facturé quand le bureau est archivé (il n'est pas inclus dans le snapshot) "
                    "et sera rattaché automatiquement au prochain lancement.", "info")
        self.size_var.trace_add("write", lambda *_: self._update())
        self.buttons(("Annuler", self.cancel, "secondary"), ("Créer et attacher", self._ok, "primary"))
        self._update()

    def _size(self) -> int | None:
        try:
            size = int(self.size_var.get())
        except ValueError:
            return None
        return size if VOLUME_MIN_GB <= size <= VOLUME_MAX_GB else None

    def _update(self) -> None:
        size = self._size()
        if size is None:
            self.cost.configure(text=f"Taille entre {VOLUME_MIN_GB} et {VOLUME_MAX_GB} Go")
            return
        self.cost.configure(text=f"{fmt.eur_m(size * self.price)}  ·  {fmt.eur(self.price, 4)} par Go et par mois")

    def _ok(self) -> None:
        size, name = self._size(), slugify(self.name_var.get(), 63)
        if size is None:
            self.error.configure(text=f"Taille invalide ({VOLUME_MIN_GB} à {VOLUME_MAX_GB} Go)")
            return
        try:
            self.app.controller.add_volume(self.desktop.slug, size, name)
        except UserError as exc:
            self.error.configure(text=str(exc))
            return
        self.close(True)


class VolumesDialog(LiveModal):
    def __init__(self, app, desktop: Desktop) -> None:
        self.slug = desktop.slug
        super().__init__(app, f"Volumes — {desktop.name}", width=640)
        self.heading("Volumes", "Disques supplémentaires facturés en permanence, rattachés à chaque lancement.")
        self.content.pack(fill="x")
        self.buttons(("Fermer", self.cancel, "primary"))

    def render(self, parent) -> None:
        d = self.ctrl.desktop(self.slug)
        if d is None:
            return
        price = self.ctrl.static.pricing.volume_gb_month if self.ctrl.static else 0.0
        if not d.volumes:
            label(parent, "Aucun volume.", 12, color=t.MUTED).pack(fill="x", pady=(12, 0))
        for vol in d.volumes:
            box = ctk.CTkFrame(parent, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
            box.pack(fill="x", pady=(10, 0))
            box.grid_columnconfigure(0, weight=1)
            label(box, f"{vol.name} · {vol.size} Go", 13, "bold").grid(row=0, column=0, sticky="w", padx=12,
                                                                     pady=(10, 0))
            state = "attaché" if vol.server_id else "détaché"
            label(box, f"{state} · {vol.location} · {fmt.eur_m(vol.size * price)}", 12, color=t.MUTED).grid(
                row=1, column=0, sticky="w", padx=12, pady=(0, 10))
            actions = ctk.CTkFrame(box, fg_color="transparent")
            actions.grid(row=0, column=1, rowspan=2, padx=8)
            ghost_button(actions, "Agrandir…", lambda v=vol: self._resize(v), width=80).pack(side="left")
            if vol.server_id:
                ghost_button(actions, "Détacher", lambda v=vol: self._detach(v), width=70).pack(side="left")
            elif d.server and d.server.location == vol.location:
                ghost_button(actions, "Attacher", lambda v=vol: self.run(self.ctrl.volume_action, self.slug, v,
                                                                         "attach"), width=70).pack(side="left")
            ghost_button(actions, "Supprimer…", lambda v=vol: self._delete(v), width=80,
                         text_color=t.tone("danger")[0]).pack(side="left")
        if d.server and d.server.status in ("running", "off"):
            secondary_button(parent, "+ Ajouter un volume", lambda: AddVolumeDialog(self.app, d),
                             width=180).pack(anchor="w", pady=(14, 0))

    def _resize(self, vol) -> None:
        def check(value: str) -> str | None:
            try:
                size = int(value)
            except ValueError:
                return "Nombre entier attendu"
            if size <= vol.size or size > VOLUME_MAX_GB:
                return f"Entre {vol.size + 1} et {VOLUME_MAX_GB} Go (un volume ne peut qu'augmenter)"
            return None
        from .base import ask_text
        value = ask_text(self, "Agrandir le volume", f"Nouvelle taille de « {vol.name} » (actuellement {vol.size} Go). "
                         "Irréversible.", str(vol.size + 10), "Agrandir", validate=check)
        if value:
            self.run(self.ctrl.volume_action, self.slug, vol, "resize", int(value))

    def _detach(self, vol) -> None:
        ok, _ = confirm(self, "Détacher le volume ?", f"« {vol.name} » sera retiré de Windows (fermez les fichiers "
                        "qui s'y trouvent). Il reste facturé et sera rattaché au prochain lancement.", "Détacher")
        if ok:
            self.run(self.ctrl.volume_action, self.slug, vol, "detach")

    def _delete(self, vol) -> None:
        if confirm_typed(self, "Supprimer le volume", f"Toutes les données du volume « {vol.name} » ({vol.size} Go) "
                         "seront définitivement effacées.", vol.name, "Supprimer le volume",
                         lost=[f"{vol.size} Go de données"]):
            self.run(self.ctrl.volume_action, self.slug, vol, "delete")


# --- historique des sauvegardes --------------------------------------------------------------------
class SnapshotsDialog(LiveModal):
    def __init__(self, app, desktop: Desktop, on_launch: Callable) -> None:
        self.slug, self.on_launch = desktop.slug, on_launch
        super().__init__(app, f"Sauvegardes — {desktop.name}", width=700)
        keep = app.controller.config.get("retention")
        self.heading("Historique des sauvegardes",
                     f"Les {keep} plus récentes sont conservées automatiquement ; les versions épinglées ne sont "
                     "jamais supprimées par la rétention.")
        self.content.pack(fill="x")
        self.buttons(("Fermer", self.cancel, "primary"))

    def render(self, parent) -> None:
        d = self.ctrl.desktop(self.slug)
        if d is None:
            return
        price = self.ctrl.static.pricing.image_gb_month if self.ctrl.static else 0.0
        archived = d.server is None and not self.ctrl.runner.for_slug(self.slug)
        latest = d.latest
        available = [s for s in d.snapshots if s.available]
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent", height=min(360, 84 * max(1, len(d.snapshots))))
        scroll.pack(fill="x", pady=(10, 0))
        for snap in d.snapshots:
            box = ctk.CTkFrame(scroll, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
            box.pack(fill="x", pady=4)
            box.grid_columnconfigure(0, weight=1)
            title = ctk.CTkFrame(box, fg_color="transparent")
            title.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
            label(title, fmt.date_long(snap.created), 13, "bold").pack(side="left")
            badges = []
            if latest and snap.id == latest.id:
                badges.append(("DERNIÈRE", "success"))
            if snap.protected:
                badges.append(("ÉPINGLÉE", "accent"))
            if snap.forced:
                badges.append(("ARRÊT FORCÉ", "warning"))
            if not snap.available:
                badges.append(("EN COURS", "info"))
            for text_, kind in badges:
                ctk.CTkLabel(title, text=text_, font=t.font(9, "bold"), fg_color=t.tone(kind)[1],
                             text_color=t.tone(kind)[0], corner_radius=6, height=18, padx=6).pack(side="left", padx=(8, 0))
            label(box, f"{fmt.gb(snap.image_size)} · disque {snap.disk_size} Go · {fmt.eur_m(snap.size_gb * price)}"
                       f" · {snap.display_name}", 12, color=t.MUTED).grid(row=1, column=0, sticky="w", padx=12,
                                                                        pady=(0, 10))
            actions = ctk.CTkFrame(box, fg_color="transparent")
            actions.grid(row=0, column=1, rowspan=2, padx=8)
            if snap.available and archived:
                ghost_button(actions, "Lancer cette version", lambda s=snap: (self.close(None), self.on_launch(s)),
                             width=10, text_color=t.ACCENT).pack(side="left")
            if snap.available:
                ghost_button(actions, "Désépingler" if snap.protected else "Épingler",
                             lambda s=snap: self.run(self.ctrl.snapshot_action, self.slug, s,
                                                     "unpin" if s.protected else "pin"), width=10).pack(side="left")
                ghost_button(actions, "Supprimer…", lambda s=snap: self._delete(s, len(available)), width=10,
                             text_color=t.tone("danger")[0]).pack(side="left")
        total = sum(s.size_gb for s in d.snapshots)
        label(parent, f"Total : {fmt.gb(total)} · {fmt.eur_m(total * price)}", 12, "bold", color=t.MUTED).pack(
            fill="x", pady=(10, 0))

    def _delete(self, snap, count: int) -> None:
        name = f"sauvegarde du {fmt.date_long(snap.created)}"
        if count <= 1:
            d = self.ctrl.desktop(self.slug)
            if d and d.server is None:
                if not confirm_typed(self, "Supprimer la dernière sauvegarde",
                                     "C'est la seule sauvegarde de ce bureau : il sera définitivement perdu.",
                                     d.name, "Supprimer", lost=["Tout le contenu du bureau Windows"]):
                    return
                self.run(self.ctrl.snapshot_action, self.slug, snap, "delete")
                return
        extra = " Elle est épinglée." if snap.protected else ""
        ok, _ = confirm(self, "Supprimer la sauvegarde ?", f"La {name} ({fmt.gb(snap.image_size)}) sera "
                        f"définitivement supprimée.{extra}", "Supprimer", danger=True)
        if ok:
            self.run(self.ctrl.snapshot_action, self.slug, snap, "delete")


# --- IP fixe --------------------------------------------------------------------------------------
class FixedIpDialog(LiveModal):
    def __init__(self, app, desktop: Desktop) -> None:
        self.slug = desktop.slug
        super().__init__(app, f"IP fixe — {desktop.name}", width=580)
        self.heading("IP fixe",
                     "Une IP fixe garde la même adresse d'un lancement à l'autre (pratique pour un .rdp enregistré "
                     "ou un accès filtré ailleurs). Elle coûte 0,50 €/mois et impose son emplacement.")
        self.content.pack(fill="x")
        self.choice = tk.StringVar(value="new")
        self.buttons(("Fermer", self.cancel, "secondary"))

    def render(self, parent) -> None:
        d = self.ctrl.desktop(self.slug)
        if d is None:
            return
        static = self.ctrl.static
        if d.fixed_ip:
            ip = d.fixed_ip
            label(parent, f"{ip.ip}  ·  {static.city(ip.location)} · {fmt.eur_m(static.pricing.ipv4_m(ip.location))}",
                  14, "bold").pack(fill="x", pady=(14, 0))
            if d.server:
                label(parent, "Fermez le bureau pour pouvoir supprimer son IP fixe.", 12, color=t.MUTED).pack(
                    fill="x", pady=(6, 0))
            else:
                ghost_button(parent, "Supprimer l'IP fixe…", lambda: self._release(d), width=10,
                             text_color=t.tone("danger")[0]).pack(anchor="w", pady=(8, 0))
            return
        if d.server:
            self.notice("Elle sera utilisée au prochain lancement (Hetzner exige un serveur éteint pour changer "
                        "d'IP).", "info")
        loc_default = self.ctrl.config.prefs(self.slug).last_location or "nbg1"
        self.loc_var = tk.StringVar(value=loc_default)
        ctk.CTkRadioButton(parent, text="Réserver une nouvelle IP à", variable=self.choice, value="new",
                           font=t.font(12)).pack(anchor="w", pady=(14, 0))
        ctk.CTkOptionMenu(parent, values=list(static.locations), variable=self.loc_var, width=160, height=30,
                          font=t.font(12)).pack(anchor="w", padx=(28, 0), pady=(4, 0))
        free = self.ctrl.grouping.unassigned_ips
        self.free_map = {f"{ip.ip} ({static.city(ip.location)})": ip for ip in free}
        if free:
            self.free_var = tk.StringVar(value=next(iter(self.free_map)))
            ctk.CTkRadioButton(parent, text="Réutiliser une IP existante non assignée (gratuit en plus)",
                               variable=self.choice, value="adopt", font=t.font(12)).pack(anchor="w", pady=(12, 0))
            ctk.CTkOptionMenu(parent, values=list(self.free_map), variable=self.free_var, width=260, height=30,
                              font=t.font(12)).pack(anchor="w", padx=(28, 0), pady=(4, 0))
            self.choice.set("adopt")
        primary_button(parent, "Activer l'IP fixe", self._activate, width=180).pack(anchor="w", pady=(16, 0))

    def _activate(self) -> None:
        if self.choice.get() == "adopt":
            ip = self.free_map[self.free_var.get()]
            self.run(self.ctrl.fixed_ip, self.slug, "adopt", ip=ip)
        else:
            self.run(self.ctrl.fixed_ip, self.slug, "create", location=self.loc_var.get())

    def _release(self, d: Desktop) -> None:
        ok, _ = confirm(self, "Supprimer l'IP fixe ?", f"L'adresse {d.fixed_ip.ip} sera rendue à Hetzner "
                        "(−0,50 €/mois). Les prochains lancements utiliseront une IP dynamique.", "Supprimer",
                        danger=True)
        if ok:
            self.run(self.ctrl.fixed_ip, self.slug, "release", ip=d.fixed_ip)


# --- identifiants ---------------------------------------------------------------------------------
class CredentialsDialog(Modal):
    def __init__(self, app, desktop: Desktop) -> None:
        super().__init__(app, f"Identifiants RDP — {desktop.name}", width=500)
        self.app, self.slug = app, desktop.slug
        ctrl = app.controller
        has_pw = bool(ctrl.creds.get_password(desktop.slug))
        self.heading("Identifiants Windows",
                     "Stockés dans le Gestionnaire d'identifiants Windows, jamais en clair. Ils permettent la "
                     "connexion en 1 clic.")
        self.section("Utilisateur")
        self.user_var = tk.StringVar(value=ctrl.config.prefs(desktop.slug).rdp_user)
        ctk.CTkEntry(self.body, textvariable=self.user_var, height=32, font=t.font(12)).pack(fill="x")
        self.section("Mot de passe")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x")
        self.pw = ctk.CTkEntry(row, height=32, font=t.font(12),
                               placeholder_text="Enregistré ✓ (laisser vide pour le garder)" if has_pw
                               else "Mot de passe du compte Windows")
        self.pw.pack(side="left", fill="x", expand=True)
        self.show_var = tk.BooleanVar(value=False)
        self.pw.bind("<Key>", lambda _e: self.after(1, self._toggle_show))
        ctk.CTkCheckBox(row, text="Afficher", variable=self.show_var, font=t.font(11), width=80,
                        command=self._toggle_show).pack(
            side="left", padx=(8, 0))
        specs = [("Annuler", self.cancel, "secondary"), ("Enregistrer", self._save, "primary")]
        if has_pw:
            specs.insert(0, ("Oublier le mot de passe", self._forget, "secondary"))
        self.buttons(*specs)

    def _toggle_show(self) -> None:
        self.pw.configure(show="" if self.show_var.get() or not self.pw.get() else "•")

    def _save(self) -> None:
        self.app.controller.set_credentials(self.slug, self.user_var.get().strip(), self.pw.get() or None)
        self.app.toast("Identifiants enregistrés", "success")
        self.close(True)

    def _forget(self) -> None:
        self.app.controller.set_credentials(self.slug, self.user_var.get().strip(), None, forget=True)
        self.app.toast("Mot de passe oublié", "info")
        self.close(True)


# --- duplication ----------------------------------------------------------------------------------
class DuplicateDialog(Modal):
    def __init__(self, app, desktop: Desktop, on_launch_copy: Callable[[str], None]) -> None:
        super().__init__(app, f"Dupliquer « {desktop.name} »", width=560)
        self.app, self.desktop, self.on_launch_copy = app, desktop, on_launch_copy
        ctrl = app.controller
        snap = desktop.latest
        self.heading("Dupliquer le bureau", f"À partir de la sauvegarde du {fmt.date_long(snap.created)}.")
        self.section("Nom de la copie")
        self.name_var = tk.StringVar(value=f"{desktop.name} (copie)")
        ctk.CTkEntry(self.body, textvariable=self.name_var, height=32, font=t.font(12)).pack(fill="x")
        self.mode = tk.StringVar(value="launch")
        self.section("Mode")
        ctk.CTkRadioButton(self.body, text="Dupliquer et lancer (recommandé, sans surcoût)", variable=self.mode,
                           value="launch", font=t.font(12)).pack(anchor="w")
        label(self.body, "La copie démarre tout de suite ; sa première sauvegarde crée son propre snapshot.", 11,
              color=t.MUTED, wraplength=480).pack(fill="x", padx=(28, 0))
        pricing = ctrl.static.pricing
        month = snap.size_gb * pricing.image_gb_month
        ctk.CTkRadioButton(self.body, text="Copie archivée (sans l'ouvrir)", variable=self.mode, value="archive",
                           font=t.font(12)).pack(anchor="w", pady=(10, 0))
        label(self.body, f"Serveur temporaire jamais démarré, puis supprimé : ≈ 1 h de serveur facturée + "
                         f"{fmt.eur_m(month)} de stockage · 10 à 20 min.", 11, color=t.MUTED,
              wraplength=480).pack(fill="x", padx=(28, 0))
        self.error = label(self.body, "", 12, color=t.tone("danger")[0])
        self.error.pack(fill="x", pady=(6, 0))
        self.buttons(("Annuler", self.cancel, "secondary"), ("Continuer", self._ok, "primary"))

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            self.error.configure(text="Nom requis")
            return
        self.close(True)
        if self.mode.get() == "launch":
            self.on_launch_copy(name)
        else:
            try:
                self.app.controller.duplicate(self.desktop.slug, self.desktop.latest, name, "archive")
            except UserError as exc:
                self.app.toast(str(exc), "error")


# --- suppression d'un bureau -------------------------------------------------------------------------
class DeleteDesktopDialog(Modal):
    def __init__(self, app, desktop: Desktop) -> None:
        super().__init__(app, f"Supprimer « {desktop.name} »", width=560)
        self.app, self.desktop = app, desktop
        n = len(desktop.snapshots)
        self.heading("Supprimer le bureau", "Action définitive : le bureau Windows ne pourra plus être relancé.")
        lost = [f"{n} sauvegarde{'s' if n > 1 else ''} ({fmt.gb(desktop.snapshot_gb)})"]
        if desktop.pinned_count:
            lost.append(f"dont {desktop.pinned_count} épinglée(s)")
        for line in lost:
            label(self.body, f"✕  {line}", 12, color=t.tone("danger")[0]).pack(fill="x", pady=(6, 0))
        self.vol_var = tk.BooleanVar(value=False)
        self.ip_var = tk.BooleanVar(value=True)
        if desktop.volumes:
            ctk.CTkCheckBox(self.body, text=f"Supprimer aussi {len(desktop.volumes)} volume(s) et leurs données",
                            variable=self.vol_var, font=t.font(12)).pack(anchor="w", pady=(12, 0))
        if desktop.fixed_ip:
            ctk.CTkCheckBox(self.body, text=f"Rendre l'IP fixe {desktop.fixed_ip.ip}", variable=self.ip_var,
                            font=t.font(12)).pack(anchor="w", pady=(8, 0))
        self.section(f"Tapez « {desktop.name} » pour confirmer")
        self.var = tk.StringVar()
        entry = ctk.CTkEntry(self.body, textvariable=self.var, height=32, font=t.font(12))
        entry.pack(fill="x")
        self.ok = self.buttons(("Annuler", self.cancel, "secondary"), ("Supprimer définitivement", self._ok, "danger"))[1]
        self.ok.configure(state="disabled")
        self.var.trace_add("write", lambda *_: self.ok.configure(
            state="normal" if self.var.get().strip() == desktop.name else "disabled"))
        self.after(80, entry.focus_set)

    def _ok(self) -> None:
        if self.var.get().strip() != self.desktop.name:
            return
        try:
            self.app.controller.delete_desktop(self.desktop.slug, self.vol_var.get(), self.ip_var.get())
        except UserError as exc:
            self.app.toast(str(exc), "error")
        self.close(True)


# --- conflit --------------------------------------------------------------------------------------
class ConflictDialog(LiveModal):
    def __init__(self, app, desktop: Desktop) -> None:
        self.slug = desktop.slug
        super().__init__(app, f"Conflit — {desktop.name}", width=600)
        self.heading("Plusieurs serveurs pour un même bureau",
                     "Cela arrive après une double exécution (deux postes). Gardez celui qui contient votre travail "
                     "et supprimez les autres (sans sauvegarde).")
        self.content.pack(fill="x")
        self.buttons(("Fermer", self.cancel, "primary"))

    def render(self, parent) -> None:
        d = self.ctrl.desktop(self.slug)
        if d is None or not d.server:
            label(parent, "Conflit résolu.", 12, color=t.tone("success")[0]).pack(fill="x", pady=10)
            return
        for srv in [d.server, *d.extra_servers]:
            box = ctk.CTkFrame(parent, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
            box.pack(fill="x", pady=(10, 0))
            label(box, f"{srv.name} · {srv.server_type} · {srv.location} · {fmt.status(srv.status)}", 13, "bold").pack(
                side="left", padx=12, pady=10)
            label(box, f"créé {fmt.ago(srv.created)}", 12, color=t.MUTED).pack(side="left")
            if d.extra_servers:
                ghost_button(box, "Supprimer…", lambda s=srv: self._delete(s), width=10,
                             text_color=t.tone("danger")[0]).pack(side="right", padx=8)

    def _delete(self, srv) -> None:
        if confirm_typed(self, "Supprimer ce serveur", f"Le serveur {srv.name} et tout ce qu'il contient depuis sa "
                         "création seront perdus.", srv.name, "Supprimer"):
            from ...controller import DormantItem
            self.run(self.ctrl.cleanup, DormantItem(f"srv:{srv.id}", "server", srv.name, "", 0.0, srv))


def volume_help(app) -> None:
    info(app, "Initialiser le volume dans Windows", [
        "Le volume est attaché. Windows le voit comme un nouveau disque vierge :",
        "1. Clic droit sur Démarrer → Gestion des disques.",
        "2. Le disque « Hors connexion » : clic droit → En ligne, puis Initialiser (GPT).",
        "3. Clic droit sur l'espace non alloué → Nouveau volume simple → NTFS.",
        "Ou en PowerShell (administrateur) :",
    ], code="Get-Disk | Where-Object PartitionStyle -eq 'RAW' | Initialize-Disk -PartitionStyle GPT -PassThru |\n"
            "  New-Partition -AssignDriveLetter -UseMaximumSize | Format-Volume -FileSystem NTFS -Confirm:$false")


def resize_help(app) -> None:
    info(app, "Étendre le volume dans Windows", [
        "Le disque est plus grand, mais Windows n'utilise pas encore l'espace ajouté :",
        "Gestion des disques → clic droit sur le volume → Étendre le volume. Ou en PowerShell :",
    ], code="$p = Get-Partition -DriveLetter D\n"
            "Resize-Partition -DriveLetter D -Size (Get-PartitionSupportedSize -DriveLetter D).SizeMax")
