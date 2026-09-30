"""Dialogue « Lancer » : version, emplacement, type (avec prix), réseau, pare-feu, volumes."""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from ... import fmt, netutil
from ...constants import L_LOC
from ...models import Desktop, Offer, SnapshotInfo
from ...offers import alternatives, compatible_offers
from ...ops.launch import LaunchParams
from .. import theme as t
from ..widgets import label, notice
from .base import Modal

LOCATION_ORDER = ["nbg1", "fsn1", "hel1", "ash", "hil", "sin"]
CITY_FR = {"Nuremberg": "Nuremberg", "Falkenstein": "Falkenstein", "Helsinki": "Helsinki",
           "Ashburn, VA": "Ashburn (US)", "Hillsboro, OR": "Hillsboro (US)", "Singapore": "Singapour"}


def city_label(static, loc: str) -> str:
    city = static.city(loc)
    return CITY_FR.get(city, city)


def ordered_locations(static) -> list[str]:
    return [loc for loc in LOCATION_ORDER if loc in static.locations] + \
        [loc for loc in static.locations if loc not in LOCATION_ORDER]


class OfferList(ctk.CTkScrollableFrame):
    """Liste de types de serveur (radio + prix), partagée par « Lancer » et « Créer un Windows »."""

    def __init__(self, master, type_var: tk.StringVar, on_select: Callable[[], None], height: int = 230) -> None:
        super().__init__(master, height=height, fg_color=t.SURFACE, corner_radius=10, border_width=1,
                         border_color=t.BORDER)
        self.grid_columnconfigure(1, weight=1)
        self.type_var, self.on_select = type_var, on_select

    def render(self, offers: list[Offer], empty_text: str | None = None) -> None:
        for child in self.winfo_children():
            child.destroy()
        if empty_text:
            label(self, empty_text, 12, color=t.tone("warning")[0], wraplength=500).grid(
                row=0, column=0, columnspan=4, padx=12, pady=12)
        for row, offer in enumerate(offers, start=1):
            self._row(row, offer)

    def _row(self, row: int, offer: Offer) -> None:
        st = offer.stype
        state = "normal" if offer.available else "disabled"
        color = t.TEXT if offer.available else t.FAINT
        radio = ctk.CTkRadioButton(self, text="", variable=self.type_var, value=offer.name,
                                   command=self.on_select, state=state, width=24, radiobutton_width=18,
                                   radiobutton_height=18)
        radio.grid(row=row, column=0, padx=(10, 2), pady=6, sticky="w")
        name = ctk.CTkFrame(self, fg_color="transparent")
        name.grid(row=row, column=1, sticky="w", pady=6)
        label(name, st.name, 13, "bold", color=color).pack(side="left")
        if offer.recommended:
            ctk.CTkLabel(name, text="RECOMMANDÉ", font=t.font(9, "bold"), fg_color=t.tone("success")[1],
                         text_color=t.tone("success")[0], corner_radius=6, height=18, padx=6).pack(side="left", padx=8)
        if not offer.available:
            ctk.CTkLabel(name, text="INDISPONIBLE", font=t.font(9, "bold"), fg_color=t.tone("muted")[1],
                         text_color=t.tone("muted")[0], corner_radius=6, height=18, padx=6).pack(side="left", padx=8)
        label(name, f"  {st.cores} vCPU {st.cpu_label} · {st.memory:g} Go · {st.disk} Go", 12,
              color=t.MUTED).pack(side="left")
        label(self, fmt.eur_h(offer.price_h), 12, "bold", color=color).grid(row=row, column=2, padx=8, sticky="e")
        label(self, f"≈ {fmt.eur(offer.price_h * 30)} / 30 h", 11, color=t.MUTED).grid(
            row=row, column=3, padx=(4, 12), sticky="e")
        if offer.available:
            for widget in (name, *name.winfo_children()):
                widget.bind("<Button-1>", lambda _e, n=offer.name: (self.type_var.set(n), self.on_select()))


class LaunchDialog(Modal):
    def __init__(self, app, desktop: Desktop, on_submit: Callable[[LaunchParams], None],
                 snapshot: SnapshotInfo | None = None, new_name: str | None = None,
                 unavailable: tuple[str, str] | None = None) -> None:
        self.app, self.ctrl, self.desktop = app, app.controller, desktop
        self.static = self.ctrl.static
        self.on_submit, self.new_name = on_submit, new_name
        title = f"Lancer la copie « {new_name} »" if new_name else f"Lancer « {desktop.name} »"
        super().__init__(app, title, width=1000)
        self.snapshots = [s for s in desktop.snapshots if s.available]
        self.snapshot = snapshot or desktop.latest
        self.prefs = self.ctrl.config.prefs(desktop.slug)
        self.fixed = None if new_name else desktop.fixed_ip
        lock, self.lock_reasons = (None, []) if new_name else desktop.location_lock()
        self.locations = ordered_locations(self.static)
        self.city_to_loc = {self._city(loc): loc for loc in self.locations}
        default_loc = lock or self.prefs.last_location or (self.snapshot.labels.get(L_LOC) if self.snapshot else None)
        self.loc_var = tk.StringVar(value=default_loc if default_loc in self.locations else self.locations[0])
        self.type_var = tk.StringVar(value=self.prefs.last_type or "")
        self.show_unavailable = tk.BooleanVar(value=False)
        self.grow_var = tk.BooleanVar(value=False)
        self.ack_var = tk.BooleanVar(value=False)
        self.fixed_var = tk.BooleanVar(value=self.fixed is not None)
        auto = self.prefs.auto_connect if self.prefs.auto_connect is not None else self.ctrl.config.get("auto_connect")
        self.auto_var = tk.BooleanVar(value=bool(auto))
        self.allow_var = tk.BooleanVar(value=True)
        self.volume_vars: dict[int, tk.BooleanVar] = {}
        self.offers: list[Offer] = []

        self.heading(title, "Le serveur est créé depuis la sauvegarde choisie et facturé à l'heure tant qu'il existe.")
        if unavailable:
            self._unavailable_notice(unavailable)
        cols = ctk.CTkFrame(self.body, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=3, uniform="launch")
        cols.grid_columnconfigure(1, weight=2, uniform="launch")
        self.left = ctk.CTkFrame(cols, fg_color="transparent")
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.right = ctk.CTkFrame(cols, fg_color="transparent")
        self.right.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
        self._version_section()
        self._location_section(lock)
        self._type_section()
        self._network_section()
        self._volume_section()
        self.section("Options", parent=self.right)
        ctk.CTkCheckBox(self.right, text="Connexion automatique dès que le bureau est prêt",
                        variable=self.auto_var, font=t.font(12)).pack(anchor="w")
        if not self.ctrl.creds.get_password(desktop.slug) and not new_name:
            label(self.right, "Aucun mot de passe enregistré : il sera demandé à la connexion "
                              "(menu ⋯ → Identifiants RDP… pour la connexion en 1 clic).", 11, color=t.MUTED,
                  wraplength=340).pack(fill="x", pady=(4, 0))

        left = ctk.CTkFrame(self.footer, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        self.cost = label(left, "", 14, "bold")
        self.cost.pack(anchor="w")
        self.cost_sub = label(left, "", 11, color=t.MUTED)
        self.cost_sub.pack(anchor="w")
        self.launch_btn = self.buttons(("Annuler", self.cancel, "secondary"), ("Lancer", self._submit, "primary"))[1]
        self.bind("<Return>", lambda _e: self._submit())
        self._refresh_offers()

    # --- sections ------------------------------------------------------------------------------
    def _city(self, loc: str) -> str:
        return city_label(self.static, loc)

    def _unavailable_notice(self, unavailable: tuple[str, str]) -> None:
        stype, loc = unavailable
        alts = alternatives(self.static.server_types, self.locations, self.snapshot.disk_size,
                            self.snapshot.architecture, unavailable) if self.snapshot else []
        text = f"{stype} est momentanément indisponible à {self._city(loc)}."
        if alts:
            text += " Suggestions : " + ", ".join(f"{o.name} à {self._city(o.location)} ({fmt.eur_h(o.price_h)})"
                                                  for o in alts)
            first = alts[0]
            self.loc_var.set(first.location)
            self.type_var.set(first.name)
        self.notice(text, "warning", pady=(12, 0))

    def _version_section(self) -> None:
        self.section("Version", pady=(10, 6), parent=self.left)
        if not self.snapshots:
            self.notice("Aucune sauvegarde disponible.", "danger", parent=self.left)
            return
        labels = []
        for i, snap in enumerate(self.snapshots):
            tag = " · dernière" if i == 0 else ""
            tag += " · épinglée" if snap.protected else ""
            tag += " · arrêt forcé" if snap.forced else ""
            labels.append(f"{fmt.date_long(snap.created)} · {fmt.gb(snap.image_size)}{tag}")
        self.version_map = dict(zip(labels, self.snapshots))
        current = next(k for k, v in self.version_map.items() if v.id == self.snapshot.id) \
            if self.snapshot in self.snapshots else labels[0]
        self.snapshot = self.version_map[current]
        menu = ctk.CTkOptionMenu(self.left, values=labels, command=self._on_version, height=32, font=t.font(12),
                                 fg_color=t.SURFACE_2, button_color=t.SURFACE_3, button_hover_color=t.BORDER,
                                 text_color=t.TEXT, dropdown_font=t.font(12))
        menu.set(current)
        menu.pack(fill="x")

    def _on_version(self, choice: str) -> None:
        self.snapshot = self.version_map[choice]
        self._refresh_offers()

    def _location_section(self, lock: str | None) -> None:
        self.section("Emplacement", parent=self.left)
        seg = ctk.CTkSegmentedButton(self.left, values=[self._city(l) for l in self.locations],
                                     command=lambda city: self._on_location(self.city_to_loc[city]),
                                     font=t.font(12), height=32, selected_color=t.ACCENT,
                                     selected_hover_color=t.ACCENT_HOVER)
        seg.set(self._city(self.loc_var.get()))
        seg.pack(fill="x")
        self.seg = seg
        if lock:
            seg.configure(state="disabled")
            self.notice(f"Emplacement imposé ({self._city(lock)}) : {', '.join(self.lock_reasons)} "
                        "y sont rattachés.", "info", pady=(8, 0), parent=self.left, wraplength=520)
        self.loc_hint = label(self.left, "", 11, color=t.MUTED)
        self.loc_hint.pack(fill="x", pady=(4, 0))

    def _on_location(self, loc: str) -> None:
        self.loc_var.set(loc)
        self._refresh_offers()

    def _type_section(self) -> None:
        head = ctk.CTkFrame(self.left, fg_color="transparent")
        head.pack(fill="x", pady=(16, 6))
        label(head, "TYPE DE SERVEUR", 10, "bold", color=t.FAINT).pack(side="left")
        ctk.CTkCheckBox(head, text="Afficher les indisponibles", variable=self.show_unavailable,
                        command=self._refresh_offers, font=t.font(11), checkbox_width=16,
                        checkbox_height=16).pack(side="right")
        self.offer_frame = OfferList(self.left, self.type_var, self._on_offer)
        self.offer_frame.pack(fill="x")
        self.disk_info = ctk.CTkFrame(self.left, fg_color="transparent", height=1)
        self.disk_info.pack(fill="x", pady=(6, 0))

    def _network_section(self) -> None:
        self.section("Réseau et accès", pady=(10, 6), parent=self.right)
        ip_box = ctk.CTkFrame(self.right, fg_color="transparent")
        ip_box.pack(fill="x")
        ctk.CTkRadioButton(ip_box, text="IP dynamique (change à chaque\nlancement)",
                           variable=self.fixed_var, value=False, font=t.font(12)).pack(anchor="w")
        if self.fixed:
            ctk.CTkRadioButton(ip_box, text=f"IP fixe {self.fixed.ip} ({self._city(self.fixed.location)})",
                               variable=self.fixed_var, value=True, font=t.font(12)).pack(anchor="w", pady=(4, 0))

        # Accès RDP : liste commune (ou pare-feu à importer) + liste propre au bureau (pas pour une copie).
        fw = self.ctrl.rdp_firewall() or next(iter(self.ctrl.grouping.adoptable_firewalls), None)
        own = None if self.new_name else self.desktop.firewall
        self.firewall, self.own_firewall = fw, own
        ip = self.ctrl.public_ip
        self.fw_mode = "none"
        self.scope_var = tk.StringVar(value="Ce bureau" if own or not fw else "Tous les bureaux")
        box = ctk.CTkFrame(self.right, fg_color="transparent")
        box.pack(fill="x", pady=(10, 0))
        fws = [f for f in (fw, own) if f]
        if fws:
            sources = [c for f in fws for c, _ in netutil.rdp_sources(f.rules)]
            allowed = netutil.ip_allowed(ip, sources) if ip else None
            if allowed:
                self.fw_mode = "ok"
                label(box, f"✓  Accès RDP : ton IP {ip} est autorisée", 12,
                      color=t.tone("success")[0], wraplength=340).pack(anchor="w")
            elif ip:
                self.fw_mode = "allow"
                ctk.CTkCheckBox(box, text=f"Autoriser mon IP {ip} avant le lancement", variable=self.allow_var,
                                font=t.font(12)).pack(anchor="w")
                if fw:
                    ctk.CTkSegmentedButton(box, values=["Ce bureau", "Tous les bureaux"], variable=self.scope_var,
                                           font=t.font(11), height=26, selected_color=t.ACCENT,
                                           selected_hover_color=t.ACCENT_HOVER).pack(anchor="w", padx=(28, 0),
                                                                                     pady=(4, 0))
                label(box, "Sinon, le bureau sera injoignable depuis ce poste.", 11, color=t.MUTED).pack(
                    anchor="w", pady=(2, 0))
            else:
                label(box, "Accès RDP filtrés · ton IP publique est inconnue, vérifie l'accès après le lancement.",
                      12, color=t.tone("warning")[0], wraplength=340).pack(anchor="w")
        elif ip:
            self.fw_mode = "create"
            ctk.CTkCheckBox(box, text=f"Créer un pare-feu RDP limité\nà mon IP {ip} (recommandé)",
                            variable=self.allow_var, font=t.font(12)).pack(anchor="w")
            label(box, "Sans pare-feu, le port RDP est exposé à tout Internet.", 11,
                  color=t.tone("warning")[0]).pack(anchor="w")
        else:
            notice(box, "Aucun pare-feu et IP publique inconnue : le port RDP sera ouvert à tout Internet.",
                   "warning", 320).pack(fill="x")

    def _volume_section(self) -> None:
        detached = [v for v in self.desktop.volumes if v.server_id is None] if not self.new_name else []
        if not detached:
            return
        self.section("Volumes à rattacher", parent=self.right)
        self.volume_boxes = []
        for vol in detached:
            var = tk.BooleanVar(value=True)
            self.volume_vars[vol.id] = var
            box = ctk.CTkCheckBox(self.right, text=f"{vol.name} · {vol.size} Go ({self._city(vol.location)})",
                                  variable=var, font=t.font(12))
            box.pack(anchor="w", pady=(0, 4))
            self.volume_boxes.append((vol, box))

    # --- offres --------------------------------------------------------------------------------
    def _refresh_offers(self) -> None:
        for child in self.disk_info.winfo_children():
            child.destroy()
        if not self.snapshot:
            self.offer_frame.render([])
            self.cost.configure(text="—")
            self.launch_btn.configure(state="disabled")
            return
        loc = self.loc_var.get()
        self.offers = compatible_offers(self.static.server_types, loc, self.snapshot.disk_size,
                                        self.snapshot.architecture, self.show_unavailable.get())
        available = [o for o in self.offers if o.available]
        empty = None if available else (f"Aucun type compatible (disque ≥ {self.snapshot.disk_size} Go) disponible "
                                        f"à {self._city(loc)} en ce moment. Essaie un autre emplacement.")
        if self.type_var.get() not in {o.name for o in available}:
            rec = next((o for o in available if o.recommended), available[0] if available else None)
            self.type_var.set(rec.name if rec else "")
        self.offer_frame.render(self.offers, empty)
        best = next((o for o in available if o.recommended), None)
        self.loc_hint.configure(text=f"Le moins cher ici : {best.name} à {fmt.eur_h(best.price_h)} · disque du "
                                     f"bureau : {self.snapshot.disk_size} Go" if best else "")
        for vol, box in getattr(self, "volume_boxes", []):
            ok = vol.location == loc
            box.configure(state="normal" if ok else "disabled")
            if not ok:
                self.volume_vars[vol.id].set(False)
        self._on_offer()

    def selected_offer(self) -> Offer | None:
        return next((o for o in self.offers if o.name == self.type_var.get() and o.available), None)

    def _on_offer(self) -> None:
        for child in self.disk_info.winfo_children():
            child.destroy()
        offer = self.selected_offer()
        if offer is None:
            self.cost.configure(text="—")
            self.cost_sub.configure(text="")
            self.launch_btn.configure(state="disabled")
            return
        disk = self.snapshot.disk_size
        if offer.base_type:
            label(self.disk_info, f"Disque conservé à {disk} Go : le bureau pourra toujours repartir sur "
                                  f"{offer.base_type}, moins cher.", 11, color=t.MUTED, wraplength=540).pack(anchor="w")
            ctk.CTkCheckBox(self.disk_info, text=f"Agrandir le disque à {offer.stype.disk} Go (irréversible)",
                            variable=self.grow_var, font=t.font(11), checkbox_width=16,
                            checkbox_height=16).pack(anchor="w", pady=(2, 0))
        elif offer.disk_grows:
            notice(self.disk_info, f"Aucun type à {disk} Go n'est disponible ici : le disque passera à "
                                   f"{offer.stype.disk} Go et ce bureau ne pourra plus démarrer sur un type plus "
                                   "petit.", "warning", 520).pack(fill="x")
            ctk.CTkCheckBox(self.disk_info, text="J'ai compris", variable=self.ack_var, command=self._on_offer_ack,
                            font=t.font(11), checkbox_width=16, checkbox_height=16).pack(anchor="w", pady=(4, 0))
        ip_h = self.static.pricing.ipv4_h(offer.location)
        total = offer.price_h + ip_h
        self.cost.configure(text=f"{fmt.eur_h(total)}  ·  ≈ {fmt.eur(total * 30)} pour 30 h")
        self.cost_sub.configure(text=f"Serveur {fmt.eur_h(offer.price_h)} + IPv4 {fmt.eur_h(ip_h)} · "
                                     "facturé à l'heure entamée, même éteint")
        self._on_offer_ack()

    def _on_offer_ack(self) -> None:
        offer = self.selected_offer()
        ok = offer is not None and (not offer.disk_grows or self.ack_var.get())
        self.launch_btn.configure(state="normal" if ok else "disabled")

    # --- validation ----------------------------------------------------------------------------
    def _submit(self) -> None:
        offer = self.selected_offer()
        if offer is None or (offer.disk_grows and not self.ack_var.get()):
            return
        ip = self.ctrl.public_ip
        cidr = netutil.normalize_cidr(ip) if ip else None
        fw, own = self.firewall, self.own_firewall
        scope = "desktop" if self.scope_var.get() == "Ce bureau" or not fw else "shared"
        params = LaunchParams(
            snapshot_id=self.snapshot.id, snapshot_disk=self.snapshot.disk_size, server_type=offer.name,
            location=offer.location,
            base_type=None if self.grow_var.get() else offer.base_type,
            firewall_id=fw.id if fw else None,
            desktop_firewall_id=own.id if own else None,
            allow_scope=scope,
            create_firewall_cidr=cidr if self.fw_mode == "create" and self.allow_var.get() else None,
            allow_cidr=cidr if self.fw_mode == "allow" and self.allow_var.get() else None,
            allow_description="Mon IP",
            volume_ids=[vid for vid, var in self.volume_vars.items() if var.get()],
            primary_ip_id=self.fixed.id if self.fixed and self.fixed_var.get() else None,
            rdp_user=self.prefs.rdp_user, auto_connect=self.auto_var.get())
        self.ctrl.config.update_prefs(self.desktop.slug, auto_connect=self.auto_var.get())
        self.close(params)
        self.on_submit(params)
