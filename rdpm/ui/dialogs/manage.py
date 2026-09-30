"""Dialogues de gestion : accès RDP (pare-feu), volumes, sauvegardes, IP fixe, identifiants, duplication, suppression."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from ... import fmt, netutil
from ...constants import OS_LINUX, OS_WINDOWS, VOLUME_MAX_GB, VOLUME_MIN_GB
from ...hetzner.errors import UserError
from ...labels import slugify
from ...models import Desktop
from ...software import catalog as software_catalog
from ...software.windows import render_admin_access_ps1
from .. import theme as t
from ..software import SoftwarePicker
from ..widgets import bind_enabled, caption, on_edit, ghost_button, label, primary_button, secondary_button
from .base import CONFIRM_WORD, Modal, confirm, confirm_typed, info, is_confirm_word


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
        busy = tuple(id(op) for op in self.ctrl.runner.active())
        version = (self.ctrl.inventory_version, busy, self.ctrl.public_ip, self.version_extra())
        if version != self._version:
            self._version = version
            for child in self.content.winfo_children():
                child.destroy()
            self.render(self.content)
            self.after_render()
            self.refit()
        self.after(500, self._poll)

    def render(self, parent) -> None:
        raise NotImplementedError

    def version_extra(self):
        """État supplémentaire qui, s'il change, redessine la fenêtre (en plus de l'inventaire)."""
        return None

    def after_render(self) -> None:
        """Mise à jour des parties fixes de la fenêtre (hors `content`) après un nouveau rendu."""

    @staticmethod
    def enabled_state(enabled: bool) -> str:
        return "normal" if enabled else "disabled"

    def run(self, fn: Callable, *args, **kwargs) -> bool:
        try:
            fn(*args, **kwargs)
        except (UserError, ValueError) as exc:
            self.app.toast(str(exc), "error")
            return False
        return True


# --- accès RDP (pare-feu) -----------------------------------------------------------------------------
SHARED_SCOPE = "Tous les bureaux"
DESKTOP_SCOPE = "Ce bureau"


class FirewallDialog(LiveModal):
    """Accès RDP : liste commune à tous les bureaux du projet + liste propre à chaque bureau.

    Ouverte depuis une carte : la liste du bureau et la liste commune. Depuis l'en-tête : toutes les listes."""

    def __init__(self, app, desktop: Desktop | None = None) -> None:
        self.desktop_slug = desktop.slug if desktop else None
        title = f"Accès RDP — {desktop.name}" if desktop else "Accès RDP"
        super().__init__(app, title, width=700)
        self.heading("Accès RDP",
                     "Seules les adresses listées peuvent joindre tes bureaux sur le port RDP (3389, TCP et UDP). "
                     "Chaque bureau accepte les adresses de la liste commune et celles de sa propre liste.")
        self.content.pack(fill="x")
        self.scope_var = tk.StringVar(value=DESKTOP_SCOPE if desktop else SHARED_SCOPE)
        self.scopes: dict[str, str | None] = {}
        self._build_form()
        self.buttons(("Fermer", self.cancel, "primary"))

    # --- listes ---------------------------------------------------------------------------------
    def render(self, parent) -> None:
        d = self.ctrl.desktop(self.desktop_slug) if self.desktop_slug else None
        busy = self.ctrl.firewall_busy()
        if self.desktop_slug and d is None:
            label(parent, "Ce bureau n'existe plus.", 12, color=t.MUTED).pack(fill="x", pady=(12, 0))
            return
        if d is not None:
            self._server_status(parent, d, busy)
            self._block(parent, f"Ce bureau uniquement ({d.name})", d.slug, busy)
            self._block(parent, "Tous les bureaux du projet", None, busy)
        else:
            desktops = self.ctrl.grouping.desktops
            holder = parent
            if len(desktops) > 2:
                holder = ctk.CTkScrollableFrame(parent, fg_color="transparent", height=380)
                holder.pack(fill="x")
            self._block(holder, "Tous les bureaux du projet", None, busy)
            for desk in desktops:
                self._block(holder, f"{desk.name} uniquement", desk.slug, busy)
        if busy:
            label(parent, "Mise à jour des accès…", 12, color=t.ACCENT).pack(fill="x", pady=(8, 0))

    def _server_status(self, parent, d: Desktop, busy: bool) -> None:
        missing = self.ctrl.unapplied_firewalls(d)
        if not (d.server and missing):
            return
        names = ", ".join(f"« {f.name} »" for f in missing)
        box = self.notice(f"Pas encore appliqué au serveur de ce bureau : {names}.", "warning", parent=parent)
        secondary_button(box, "Appliquer maintenant", lambda: self.run(self.ctrl.apply_firewall, d.slug),
                         width=180, state=self.enabled_state(not busy)).pack(anchor="w", padx=12, pady=(0, 8))

    def _block(self, parent, title: str, slug: str | None, busy: bool) -> None:
        ip = self.ctrl.public_ip
        caption(parent, title).pack(fill="x", pady=(14, 4))
        table = ctk.CTkFrame(parent, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
        table.pack(fill="x")
        table.grid_columnconfigure(1, weight=1)
        if slug is None and self.ctrl.rdp_firewall() is None:
            self._no_shared(table, busy)
            return
        sources = self.ctrl.rdp_sources_of(slug)
        if not sources:
            empty = ("Aucune adresse propre : seules celles de la liste commune s'appliquent." if slug else
                     "Aucune adresse commune : seuls les accès propres à chaque bureau s'appliquent.")
            label(table, empty, 12, color=t.MUTED, wraplength=600).grid(row=0, column=0, columnspan=3, sticky="w",
                                                                         padx=12, pady=10)
        for row, (cidr, desc) in enumerate(sources):
            mine = bool(ip) and netutil.ip_allowed(ip, [cidr])
            ctk.CTkLabel(table, text=cidr, font=t.mono(12), text_color=t.TEXT, anchor="w").grid(
                row=row, column=0, sticky="w", padx=12, pady=5)
            label(table, (desc or "—") + ("   · ton IP actuelle" if mine else ""), 12,
                  color=t.tone("success")[0] if mine else t.MUTED).grid(row=row, column=1, sticky="w")
            ghost_button(table, "Retirer", lambda c=cidr, m=mine: self._remove(slug, c, m), width=70,
                         text_color=t.tone("danger")[0], state=self.enabled_state(not busy)).grid(row=row, column=2, padx=8)

    def _no_shared(self, table, busy: bool) -> None:
        ip = self.ctrl.public_ip
        label(table, "Pas encore de liste commune.", 12, color=t.tone("warning")[0]).grid(
            row=0, column=0, sticky="w", padx=12, pady=10)
        adoptable = self.ctrl.grouping.adoptable_firewalls
        if adoptable:
            primary_button(table, f"Importer « {adoptable[0].name} »",
                           lambda: self.run(self.ctrl.adopt_firewall, adoptable[0].id), width=220,
                           state=self.enabled_state(not busy)).grid(row=0, column=2, padx=8, pady=6)
        elif ip:
            primary_button(table, f"Créer avec mon IP ({ip})",
                           lambda: self.run(self.ctrl.create_firewall, netutil.normalize_cidr(ip), "Mon IP"),
                           width=220, state=self.enabled_state(not busy)).grid(row=0, column=2, padx=8, pady=6)

    # --- ajout ----------------------------------------------------------------------------------
    def _build_form(self) -> None:
        form = ctk.CTkFrame(self.body, fg_color="transparent")
        form.pack(fill="x", pady=(18, 0))
        head = ctk.CTkFrame(form, fg_color="transparent")
        head.pack(fill="x")
        caption(head, "Ajouter une adresse pour").pack(side="left")
        self.scope_holder = ctk.CTkFrame(head, fg_color="transparent")
        self.scope_holder.pack(side="left", padx=(10, 0))
        self._scope_sig = None

        self.mine_btn = secondary_button(form, "Autoriser mon IP actuelle", self._add_mine, width=300)
        self.mine_btn.pack(anchor="w", pady=(10, 0))
        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        self.cidr_entry = ctk.CTkEntry(row, height=32, width=290, font=t.mono(12),
                                       placeholder_text="IP ou réseau (ex. 203.0.113.7 ou 10.0.0.0/24)")
        self.cidr_entry.pack(side="left")
        self.cidr_entry.bind("<Return>", lambda _e: self._add())
        on_edit(self.cidr_entry, self._update_form)
        self.desc_entry = ctk.CTkEntry(row, placeholder_text="Description (ex. Maison)", height=32, width=170,
                                       font=t.font(12))
        self.desc_entry.pack(side="left", padx=8)
        self.add_btn = secondary_button(row, "Ajouter", self._add, width=90)
        self.add_btn.pack(side="left")
        self.hint = label(form, "", 12, color=t.tone("danger")[0], wraplength=640)
        self.hint.pack(fill="x", pady=(4, 0))
        self.scope_var.trace_add("write", lambda *_: self._update_form())

    def _render_scopes(self) -> None:
        if self.desktop_slug:
            d = self.ctrl.desktop(self.desktop_slug)
            self.scopes = {DESKTOP_SCOPE: self.desktop_slug, SHARED_SCOPE: None} if d else {SHARED_SCOPE: None}
        else:
            self.scopes = {SHARED_SCOPE: None}
            for d in self.ctrl.grouping.desktops:
                self.scopes[d.name if d.name not in self.scopes else f"{d.name} ({d.slug})"] = d.slug
        sig = tuple(self.scopes)
        if sig == self._scope_sig:
            return
        self._scope_sig = sig
        for child in self.scope_holder.winfo_children():
            child.destroy()
        if self.scope_var.get() not in self.scopes:
            self.scope_var.set(next(iter(self.scopes)))
        if self.desktop_slug:
            ctk.CTkSegmentedButton(self.scope_holder, values=list(self.scopes), variable=self.scope_var,
                                   font=t.font(12), height=28, selected_color=t.ACCENT,
                                   selected_hover_color=t.ACCENT_HOVER).pack(side="left")
        else:
            ctk.CTkOptionMenu(self.scope_holder, values=list(self.scopes), variable=self.scope_var, width=220,
                              height=28, font=t.font(12), fg_color=t.SURFACE_2, button_color=t.SURFACE_3,
                              button_hover_color=t.BORDER, text_color=t.TEXT,
                              dropdown_font=t.font(12)).pack(side="left")

    def _scope(self) -> str | None:
        return self.scopes.get(self.scope_var.get())

    def _problem(self, raw: str) -> str | None:
        """Raison d'un refus, "" si le champ est vide, None si l'adresse peut être ajoutée."""
        if not raw.strip():
            return ""
        try:
            cidr = netutil.normalize_cidr(raw)
        except ValueError as exc:
            return str(exc)
        return self.ctrl.rdp_source_problem(self._scope(), cidr)

    def after_render(self) -> None:
        self._render_scopes()
        self._update_form()

    def _update_form(self) -> None:
        if not self.winfo_exists() or not self.scopes:
            return
        busy = self.ctrl.firewall_busy()
        problem = self._problem(self.cidr_entry.get())
        self.add_btn.configure(state=self.enabled_state(problem is None and not busy))
        ip = self.ctrl.public_ip
        mine = self._problem(ip) if ip else None
        if not ip:
            self.mine_btn.configure(text="Ton IP publique n'est pas encore connue", state="disabled")
        elif mine and "déjà" in mine:
            self.mine_btn.configure(text=f"✓  Ton IP ({ip}) est déjà autorisée ici", state="disabled")
        else:
            self.mine_btn.configure(text=f"Autoriser mon IP actuelle ({ip})",
                                    state=self.enabled_state(mine is None and not busy))
        # Motif affiché : celui de l'adresse saisie, sinon pourquoi « mon IP » est indisponible.
        reason = problem or (mine if mine and "déjà" not in mine else "")
        self.hint.configure(text=reason)

    def _add_mine(self) -> None:
        if self.ctrl.public_ip:
            self.run(self.ctrl.allow_current_ip, self._scope(), self.desc_entry.get().strip() or "Mon IP")

    def _add(self) -> None:
        raw = self.cidr_entry.get()
        if self._problem(raw) is not None or self.ctrl.firewall_busy():
            return
        if self.run(self.ctrl.add_rdp_source, self._scope(), raw, self.desc_entry.get()):
            self.cidr_entry.delete(0, "end")
            self._update_form()

    def _remove(self, slug: str | None, cidr: str, mine: bool) -> None:
        where = "ce bureau" if slug else "tous les bureaux du projet"
        message = f"L'adresse {cidr} ne pourra plus se connecter en RDP à {where}."
        if mine:
            message += "\n\nC'est ton IP actuelle : tu risques de perdre l'accès depuis ce poste."
        ok, _ = confirm(self, "Retirer l'adresse ?", message, "Retirer", danger=mine)
        if ok:
            self.run(self.ctrl.remove_rdp_source, slug, cidr)


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
                                    command=lambda v: self.size_var.set(str(int(v))))  # saisie : jusqu'au max
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
        ok = self.buttons(("Annuler", self.cancel, "secondary"), ("Créer et attacher", self._ok, "primary"))[1]
        bind_enabled(ok, lambda: self._size() is not None and bool(slugify(self.name_var.get(), 63).strip()) and
                     bool(self.name_var.get().strip()), self.size_var, self.name_var)
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
        if size <= 1000 and int(self.slider.get()) != size:
            self.slider.set(size)
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
        st = self.enabled_state(not self.ctrl.runner.for_slug(self.slug))
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
            ghost_button(actions, "Agrandir…", lambda v=vol: self._resize(v), width=80, state=st).pack(side="left")
            if vol.server_id:
                ghost_button(actions, "Détacher", lambda v=vol: self._detach(v), width=70, state=st).pack(side="left")
            elif d.server and d.server.location == vol.location:
                ghost_button(actions, "Attacher", lambda v=vol: self.run(self.ctrl.volume_action, self.slug, v,
                                                                         "attach"), width=70, state=st).pack(side="left")
            ghost_button(actions, "Supprimer…", lambda v=vol: self._delete(v), width=80,
                         text_color=t.tone("danger")[0], state=st).pack(side="left")
        if d.server and d.server.status in ("running", "off"):
            secondary_button(parent, "+ Ajouter un volume", lambda: AddVolumeDialog(self.app, d),
                             width=180, state=st).pack(anchor="w", pady=(14, 0))
        elif not d.server:
            label(parent, "Lance le bureau pour lui ajouter un volume.", 12, color=t.MUTED).pack(fill="x", pady=(12, 0))

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
        ok, _ = confirm(self, "Détacher le volume ?", f"« {vol.name} » sera retiré du système (ferme d'abord les "
                        "fichiers qui s'y trouvent). Il reste facturé et sera rattaché au prochain lancement.",
                        "Détacher")
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
        busy = bool(self.ctrl.runner.for_slug(self.slug))
        archived = d.server is None and not busy
        st = self.enabled_state(not busy)
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
                                                     "unpin" if s.protected else "pin"), width=10,
                             state=st).pack(side="left")
                ghost_button(actions, "Supprimer…", lambda s=snap: self._delete(s, len(available)), width=10,
                             text_color=t.tone("danger")[0], state=st).pack(side="left")
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
                                     d.name, "Supprimer", lost=["Tout le contenu du bureau"]):
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
        static = self.ctrl.static
        loc = self.ctrl.config.prefs(self.slug).last_location or next(iter(static.locations), "nbg1")
        self.heading("IP fixe",
                     "Une IP fixe garde la même adresse d'un lancement à l'autre (pratique pour un fichier .rdp "
                     "enregistré ou un accès filtré ailleurs). Elle est facturée en permanence "
                     f"({fmt.eur_m(static.pricing.ipv4_m(loc))}) et impose l'emplacement du bureau.")
        self.content.pack(fill="x")
        self.city_to_loc = {f"{static.city(l)} ({l})": l for l in static.locations}
        self.loc_var = tk.StringVar(value=next((c for c, l in self.city_to_loc.items() if l == loc),
                                               next(iter(self.city_to_loc), "")))
        # Choix mémorisé entre deux rendus ; rien n'est réservé tant que « Activer » n'est pas cliqué.
        self.choice = tk.StringVar(value="adopt" if self.ctrl.grouping.unassigned_ips else "new")
        self.free_var = tk.StringVar()
        self.buttons(("Fermer", self.cancel, "primary"))

    def render(self, parent) -> None:
        d = self.ctrl.desktop(self.slug)
        if d is None:
            return
        static = self.ctrl.static
        busy = bool(self.ctrl.runner.for_slug(self.slug))
        if d.fixed_ip:
            ip = d.fixed_ip
            label(parent, f"{ip.ip}  ·  {static.city(ip.location)} · {fmt.eur_m(static.pricing.ipv4_m(ip.location))}",
                  14, "bold").pack(fill="x", pady=(14, 0))
            if d.server:
                label(parent, "Ferme le bureau pour pouvoir supprimer son IP fixe.", 12, color=t.MUTED).pack(
                    fill="x", pady=(6, 0))
            else:
                ghost_button(parent, "Supprimer l'IP fixe…", lambda: self._release(d), width=10,
                             text_color=t.tone("danger")[0], state=self.enabled_state(not busy)).pack(anchor="w", pady=(8, 0))
            return
        if busy:
            label(parent, "Réservation de l'IP fixe en cours…", 12, color=t.ACCENT).pack(fill="x", pady=(14, 0))
            return
        if d.server:
            self.notice("Elle sera utilisée au prochain lancement (Hetzner exige un serveur éteint pour changer "
                        "d'IP).", "info", parent=parent)
        ctk.CTkRadioButton(parent, text="Réserver une nouvelle IP à", variable=self.choice, value="new",
                           font=t.font(12)).pack(anchor="w", pady=(14, 0))
        ctk.CTkOptionMenu(parent, values=list(self.city_to_loc), variable=self.loc_var, width=220, height=30,
                          font=t.font(12)).pack(anchor="w", padx=(28, 0), pady=(4, 0))
        free = self.ctrl.grouping.unassigned_ips
        self.free_map = {f"{ip.ip} ({static.city(ip.location)})": ip for ip in free}
        if free:
            if self.free_var.get() not in self.free_map:
                self.free_var.set(next(iter(self.free_map)))
            ctk.CTkRadioButton(parent, text="Réutiliser une IP déjà réservée et inutilisée (aucun coût en plus)",
                               variable=self.choice, value="adopt", font=t.font(12)).pack(anchor="w", pady=(12, 0))
            ctk.CTkOptionMenu(parent, values=list(self.free_map), variable=self.free_var, width=260, height=30,
                              font=t.font(12)).pack(anchor="w", padx=(28, 0), pady=(4, 0))
        elif self.choice.get() == "adopt":
            self.choice.set("new")
        primary_button(parent, "Activer l'IP fixe", self._enable_fixed_ip, width=180).pack(anchor="w", pady=(16, 0))

    def _enable_fixed_ip(self) -> None:
        """Appelée uniquement par le bouton « Activer l'IP fixe »."""
        if self.ctrl.runner.for_slug(self.slug):
            return
        if self.choice.get() == "adopt" and self.free_var.get() in getattr(self, "free_map", {}):
            self.run(self.ctrl.fixed_ip, self.slug, "adopt", ip=self.free_map[self.free_var.get()])
        elif self.loc_var.get() in self.city_to_loc:
            self.run(self.ctrl.fixed_ip, self.slug, "create", location=self.city_to_loc[self.loc_var.get()])

    def _release(self, d: Desktop) -> None:
        price = fmt.eur_m(self.ctrl.static.pricing.ipv4_m(d.fixed_ip.location))
        ok, _ = confirm(self, "Supprimer l'IP fixe ?", f"L'adresse {d.fixed_ip.ip} sera rendue au fournisseur "
                        f"(économie de {price}). Les prochains lancements utiliseront une IP dynamique.", "Supprimer",
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
        self.heading("Identifiants de connexion",
                     "Compte du bureau distant, stocké dans le Gestionnaire d'identifiants de ce PC, jamais en "
                     "clair. Il permet la connexion en 1 clic.")
        self.section("Utilisateur")
        self.user_var = tk.StringVar(value=ctrl.config.prefs(desktop.slug).rdp_user)
        ctk.CTkEntry(self.body, textvariable=self.user_var, height=32, font=t.font(12)).pack(fill="x")
        self.section("Mot de passe")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x")
        self.pw = ctk.CTkEntry(row, height=32, font=t.font(12),
                               placeholder_text="Enregistré ✓ (laisser vide pour le garder)" if has_pw
                               else "Mot de passe du compte")
        self.pw.pack(side="left", fill="x", expand=True)
        self.show_var = tk.BooleanVar(value=False)
        self.pw.bind("<Key>", lambda _e: self.after(1, self._toggle_show))
        ctk.CTkCheckBox(row, text="Afficher", variable=self.show_var, font=t.font(11), width=80,
                        command=self._toggle_show).pack(
            side="left", padx=(8, 0))
        specs = [("Annuler", self.cancel, "secondary"), ("Enregistrer", self._save, "primary")]
        if has_pw:
            specs.insert(0, ("Oublier le mot de passe", self._forget, "secondary"))
        save = self.buttons(*specs)[-1]
        initial_user = self.user_var.get()
        # Rien à enregistrer tant que ni l'utilisateur ni le mot de passe n'ont changé.
        update = bind_enabled(save, lambda: bool(self.user_var.get().strip()) and
                              (self.user_var.get().strip() != initial_user or bool(self.pw.get())), self.user_var)
        on_edit(self.pw, update)

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
        label(self.body, "La copie ne reprend pas les accès RDP propres à ce bureau : seule la liste commune "
                         "s'applique jusqu'à ce que tu en ajoutes.", 11, color=t.MUTED, wraplength=480).pack(
            fill="x", pady=(10, 0))
        ok = self.buttons(("Annuler", self.cancel, "secondary"), ("Continuer", self._ok, "primary"))[1]
        bind_enabled(ok, lambda: bool(self.name_var.get().strip()), self.name_var)

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
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
        self.heading("Supprimer le bureau", "Action définitive : le bureau ne pourra plus être relancé.")
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
        if desktop.firewall:
            label(self.body, f"✕  Accès RDP propres à ce bureau (liste « {desktop.firewall.name} »)", 12,
                  color=t.tone("danger")[0]).pack(fill="x", pady=(8, 0))
        self.section(f"Tape {CONFIRM_WORD} pour confirmer")
        self.var = tk.StringVar()
        entry = ctk.CTkEntry(self.body, textvariable=self.var, height=32, font=t.font(12),
                             placeholder_text=CONFIRM_WORD)
        entry.pack(fill="x")
        self.ok = self.buttons(("Annuler", self.cancel, "secondary"), ("Supprimer définitivement", self._ok, "danger"))[1]
        self.ok.configure(state="disabled")
        self.var.trace_add("write", lambda *_: self.ok.configure(
            state="normal" if is_confirm_word(self.var.get()) else "disabled"))
        self.after(80, entry.focus_set)

    def _ok(self) -> None:
        if not is_confirm_word(self.var.get()):
            return
        try:
            self.app.controller.delete_desktop(self.desktop.slug, self.vol_var.get(), self.ip_var.get())
        except UserError as exc:
            self.app.toast(str(exc), "error")
            return
        self.close(True)


# --- conflit --------------------------------------------------------------------------------------
class ConflictDialog(LiveModal):
    def __init__(self, app, desktop: Desktop) -> None:
        self.slug = desktop.slug
        super().__init__(app, f"Conflit — {desktop.name}", width=600)
        self.heading("Plusieurs serveurs pour un même bureau",
                     "Cela arrive après une double exécution (deux postes). Garde celui qui contient ton travail "
                     "et supprime les autres (sans sauvegarde).")
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
                             text_color=t.tone("danger")[0],
                             state=self.enabled_state(not self.ctrl.runner.active())).pack(side="right", padx=8)

    def _delete(self, srv) -> None:
        if confirm_typed(self, "Supprimer ce serveur", f"Le serveur {srv.name} et tout ce qu'il contient depuis sa "
                         "création seront perdus.", srv.name, "Supprimer"):
            from ...controller import DormantItem
            self.run(self.ctrl.cleanup, DormantItem(f"srv:{srv.id}", "server", srv.name, "", 0.0, srv))


def volume_help(app, os_name: str = OS_WINDOWS, volume_id: int | None = None) -> None:
    if os_name == OS_LINUX:
        dev = f"/dev/disk/by-id/scsi-0HC_Volume_{volume_id or '<id>'}"
        info(app, "Utiliser le volume dans Linux", [
            "Le volume est attaché et déjà formaté (ext4).",
            "Pour l'utiliser tout de suite : dans le gestionnaire de fichiers (Thunar), clique sur le volume dans "
            "le panneau de gauche, il est monté sans mot de passe.",
            "Pour le retrouver automatiquement à chaque lancement dans /mnt/volume, une fois dans un terminal :",
        ], code="sudo mkdir -p /mnt/volume\n"
                f"echo '{dev} /mnt/volume ext4 discard,nofail,defaults 0 0' | sudo tee -a /etc/fstab\n"
                "sudo mount -a && sudo chown \"$USER:\" /mnt/volume")
        return
    info(app, "Initialiser le volume dans Windows", [
        "Le volume est attaché. Windows le voit comme un nouveau disque vierge :",
        "1. Clic droit sur Démarrer → Gestion des disques.",
        "2. Le disque « Hors connexion » : clic droit → En ligne, puis Initialiser (GPT).",
        "3. Clic droit sur l'espace non alloué → Nouveau volume simple → NTFS.",
        "Ou en PowerShell (administrateur) :",
    ], code="Get-Disk | Where-Object PartitionStyle -eq 'RAW' | Initialize-Disk -PartitionStyle GPT -PassThru |\n"
            "  New-Partition -AssignDriveLetter -UseMaximumSize | Format-Volume -FileSystem NTFS -Confirm:$false")


def resize_help(app, os_name: str = OS_WINDOWS, volume_id: int | None = None) -> None:
    if os_name == OS_LINUX:
        info(app, "Étendre le volume dans Linux", [
            "Le disque est plus grand ; le système de fichiers s'agrandit à chaud (volume monté ou non) :",
        ], code=f"sudo resize2fs /dev/disk/by-id/scsi-0HC_Volume_{volume_id or '<id>'}")
        return
    info(app, "Étendre le volume dans Windows", [
        "Le disque est plus grand, mais Windows n'utilise pas encore l'espace ajouté :",
        "Gestion des disques → clic droit sur le volume → Étendre le volume. Ou en PowerShell :",
    ], code="$p = Get-Partition -DriveLetter D\n"
            "Resize-Partition -DriveLetter D -Size (Get-PartitionSupportedSize -DriveLetter D).SizeMax")


# --- logiciels --------------------------------------------------------------------------------------
class SoftwareDialog(LiveModal):
    """Logiciels du catalogue sur un bureau : ce qui est installé, et installation ou mise à jour depuis
    l'application (bureau lancé, accès d'administration actif)."""

    def __init__(self, app, desktop: Desktop) -> None:
        self.slug, self.os_name = desktop.slug, desktop.os
        super().__init__(app, f"Logiciels — {desktop.name}", width=700)
        self.picker: SoftwarePicker | None = None
        self.chosen: set[str] = set()   # cases cochées, conservées quand la fenêtre se redessine
        self.heading("Logiciels prêts à l'emploi",
                     "IA et développement, installés depuis leurs sources officielles dans leur dernière version. "
                     "Tu te connectes à tes comptes (Claude, ChatGPT, GitHub…) au premier lancement de chaque "
                     "logiciel : aucune clé n'est enregistrée par l'application.")
        self.content.pack(fill="both", expand=True)
        self.close_btn, self.update_btn, self.install_btn = self.buttons(
            ("Fermer", self.cancel, "secondary"), ("Tout mettre à jour", self._update, "secondary"),
            ("Installer", self._install, "primary"))

    # --- rendu ----------------------------------------------------------------------------------
    def render(self, parent) -> None:
        d = self.ctrl.desktop(self.slug)
        self.picker = None
        if d is None:
            return
        op = self.ctrl.runner.for_slug(self.slug)
        installed = self.ctrl.installed_apps(d)
        if not d.server:
            self._render_archived(parent, installed)
            return
        if op is not None and op.kind == "software":
            self.text(f"{op.phase}", t.ACCENT, 13, parent=parent, pady=(12, 0))
            self.text("Tu peux continuer à travailler dans le bureau pendant l'installation. Le résultat s'affichera "
                      "ici et dans une notification.", t.MUTED, 12, parent=parent, pady=(4, 0))
            return
        if not self.ctrl.admin_access(d) or not self.ctrl.admin_key_on_this_pc():
            self._render_enable(parent, d)
            return
        if self.os_name == OS_WINDOWS and not self.ctrl.password_known(self.slug):
            self.notice("Le mot de passe Windows de ce bureau n'est pas enregistré sur ce PC : il sert à lancer "
                        "l'installation sous ton compte. Renseigne-le dans « Identifiants RDP… ».", "warning",
                        parent=parent)
            secondary_button(parent, "Identifiants RDP…", lambda: CredentialsDialog(self.app, d),
                             width=170).pack(anchor="w", pady=(8, 0))
            return
        self.picker = SoftwarePicker(parent, self.os_name, installed=installed, chosen=self.chosen,
                                     on_change=self._on_pick, height=330, wraplength=560)
        self.picker.pack(fill="both", expand=True, pady=(12, 0))
        if op is not None:
            self.notice(f"Opération en cours sur ce bureau ({op.title.lower()}) : attends sa fin.", "warning",
                        parent=parent)
        else:
            self.text("Les ajouts vivent sur le disque du serveur : « Sauvegarder & fermer » les conserve dans la "
                      "sauvegarde. L'application ouvre SSH à ton IP le temps de l'installation seulement.", t.MUTED,
                      11, parent=parent, pady=(8, 0))

    def version_extra(self):
        op = self.ctrl.runner.for_slug(self.slug)
        return (self.os_name == OS_WINDOWS and self.ctrl.password_known(self.slug),
                op.phase if op is not None and op.kind == "software" else None)

    def _render_archived(self, parent, installed: set[str]) -> None:
        names = software_catalog.names(k for k in software_catalog.install_order() if k in installed)
        self.text("Dans la dernière sauvegarde : " + (", ".join(names) if names else "aucun logiciel du catalogue "
                  "(ou bureau créé avant cette fonction)."), t.TEXT, 12, parent=parent, pady=(14, 0))
        self.notice("Lance le bureau pour en installer d'autres : ils s'ajoutent pendant que tu travailles.", "info",
                    parent=parent)

    def _render_enable(self, parent, d: Desktop) -> None:
        linux_os = self.os_name == OS_LINUX
        why = ("ce bureau a été créé avant cette fonction ou depuis un autre PC" if self.ctrl.admin_access(d) is False
               else "la clé d'administration de ce bureau n'est pas sur ce PC")
        self.notice(f"L'application n'a pas encore accès à ce bureau pour y installer des logiciels ({why}).",
                    "warning", parent=parent)
        steps = ("1. Dans le bureau, ouvre un terminal et colle cette commande (mot de passe « sudo » demandé).\n"
                 if linux_os else
                 "1. Dans le bureau, ouvre PowerShell en administrateur et colle ce bloc (il active OpenSSH, "
                 "connexion par clé seulement).\n")
        self.text(steps + "2. Clique sur « C'est fait, vérifier » : l'application teste l'accès puis lit les logiciels "
                  "installés.\nLe port SSH reste fermé en dehors des installations lancées d'ici.", t.TEXT, 12,
                  parent=parent)
        try:
            code = self._enable_code()
        except UserError as exc:
            self.notice(str(exc), "danger", parent=parent)
            return
        box = ctk.CTkTextbox(parent, height=110 if linux_os else 200, font=t.mono(11), fg_color=t.SURFACE_2,
                             wrap="char" if linux_os else "none")
        box.insert("1.0", code)
        box.configure(state="disabled")
        box.pack(fill="x", pady=(8, 0))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        secondary_button(row, "Copier", lambda: self._copy(code), width=100).pack(side="left")
        primary_button(row, "C'est fait, vérifier", self._verify, width=170).pack(side="left", padx=(8, 0))

    def _enable_code(self) -> str:
        pubkey = self.ctrl.admin_public_key()
        if self.os_name == OS_LINUX:
            return ("sudo sh -c 'install -d -m 0700 /root/.ssh && echo \"" + pubkey + "\" >> "
                    "/root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys' && echo OK")
        return render_admin_access_ps1(pubkey)

    def _on_pick(self) -> None:
        self.chosen = set(self.picker.user) if self.picker else set()
        self.after_render()

    def after_render(self) -> None:
        d = self.ctrl.desktop(self.slug)
        busy = bool(self.ctrl.runner.for_slug(self.slug))
        # Installer / Tout mettre à jour : seulement quand la liste est proposée.
        for btn in (self.install_btn, self.update_btn):
            btn.pack_forget()
        if self.picker is not None:
            self.install_btn.pack(side="right", padx=(8, 0), before=self.close_btn)
            self.update_btn.pack(side="right", padx=(8, 0), before=self.close_btn)
        todo = self.picker.selected() if self.picker else []
        self.install_btn.configure(text=f"Installer ({len(todo)})" if todo else "Installer",
                                   state=self.enabled_state(bool(todo) and not busy))
        self.update_btn.configure(state=self.enabled_state(self.picker is not None and not busy and bool(
            d and self.ctrl.installed_apps(d))))

    # --- actions --------------------------------------------------------------------------------
    def _install(self) -> None:
        if self.picker and self.picker.selected():
            if self.run(self.ctrl.software, self.slug, self.picker.selected(), "install"):
                self.chosen = set()

    def _update(self) -> None:
        self.run(self.ctrl.software, self.slug, (), "update")

    def _verify(self) -> None:
        self.run(self.ctrl.software, self.slug, (), "inventory")

    def _copy(self, code: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(code)
        self.app.toast("Commande copiée", "info")
