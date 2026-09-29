"""Dialogues « Créer un Windows de référence » : formulaire de construction et récapitulatif final."""

from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from datetime import date

import customtkinter as ctk

from ... import fmt, license, netutil
from ...build import catalog
from ...build.scripts import render_eval_task_install
from ...constants import EVAL_ALERT_DAYS
from ...labels import slugify
from ...models import Offer
from ...offers import compatible_offers
from ...ops.build import BuildParams
from ...state import Act
from .. import theme as t
from ..widgets import label, notice
from .base import Modal
from .launch import OfferList, city_label, ordered_locations

MIN_DISK_GB = 40
RECOMMENDED_DISK_GB, RECOMMENDED_RAM_GB = 80, 4
SNAPSHOT_GB_ESTIMATE = 15


def _menu(master, values: list[str], command=None, width: int | None = None) -> ctk.CTkOptionMenu:
    kw = {"width": width} if width else {}
    return ctk.CTkOptionMenu(master, values=values, command=command, height=32, font=t.font(12),
                             fg_color=t.SURFACE_2, button_color=t.SURFACE_3, button_hover_color=t.BORDER,
                             text_color=t.TEXT, dropdown_font=t.font(12), **kw)


class BuildDialog(Modal):
    def __init__(self, app) -> None:
        self.app, self.ctrl = app, app.controller
        self.static = self.ctrl.static
        super().__init__(app, "Créer un Windows de référence", width=1000)
        if self.static is None:
            self.heading("Créer un Windows de référence")
            self.notice("Catalogue Hetzner en cours de chargement : réessayez dans un instant.", "info")
            self.buttons(("Fermer", self.cancel, "secondary"))
            return
        self.heading("Créer un Windows de référence",
                     "Windows est installé automatiquement sur un serveur temporaire (aucun clic dans l'installeur), "
                     "puis sauvegardé en snapshot : environ 30 à 40 minutes.")
        self.locations = ordered_locations(self.static)
        self.city_to_loc = {city_label(self.static, loc): loc for loc in self.locations}
        prefs_loc = next((p.last_location for p in self.ctrl.config.desktops.values() if p.last_location), None)
        self.loc_var = tk.StringVar(value=prefs_loc if prefs_loc in self.locations else self.locations[0])
        self.type_var = tk.StringVar(value="")
        self.pin_var = tk.BooleanVar(value=True)
        self.name_var = tk.StringVar(value="Windows de référence")
        self.offers: list[Offer] = []
        self._kb_touched = self._tz_touched = False
        self._local_tz = catalog.local_timezone()
        self._iso_check_seq = 0

        cols = ctk.CTkFrame(self.body, fg_color="transparent")
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=1, uniform="build")
        cols.grid_columnconfigure(1, weight=1, uniform="build")
        self.left = ctk.CTkFrame(cols, fg_color="transparent")
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.right = ctk.CTkFrame(cols, fg_color="transparent")
        self.right.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
        self._identity_section()
        self._edition_section()
        self._options_section()
        self._location_section()
        self._type_section()
        self._locale_section()

        left = ctk.CTkFrame(self.footer, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        self.cost = label(left, "", 14, "bold")
        self.cost.pack(anchor="w")
        self.cost_sub = label(left, "", 11, color=t.MUTED, wraplength=600)
        self.cost_sub.pack(anchor="w")
        self.error = label(left, "", 12, color=t.tone("danger")[0], wraplength=600)
        self.error.pack(anchor="w")
        self.create_btn = self.buttons(("Annuler", self.cancel, "secondary"),
                                       ("Créer le Windows", self._submit, "primary"))[1]
        self._on_edition(self.edition_menu.get())
        self._refresh_offers()

    # --- sections --------------------------------------------------------------------------------
    def _identity_section(self) -> None:
        self.section("Nom du bureau", pady=(10, 6), parent=self.left)
        ctk.CTkEntry(self.left, textvariable=self.name_var, height=32, font=t.font(12)).pack(fill="x")
        self.slug_hint = label(self.left, "", 11, color=t.FAINT)
        self.slug_hint.pack(fill="x", pady=(2, 0))
        self.name_var.trace_add("write", lambda *_: self.slug_hint.configure(
            text=f"Identifiant : {slugify(self.name_var.get())}"))
        self.slug_hint.configure(text=f"Identifiant : {slugify(self.name_var.get())}")

    def _edition_section(self) -> None:
        self.section("Édition Windows", parent=self.left)
        self.edition_labels = {ed.label: key for key, ed in catalog.EDITIONS.items()}
        self.edition_menu = _menu(self.left, list(self.edition_labels), self._on_edition)
        self.edition_menu.set(catalog.EDITIONS[catalog.DEFAULT_EDITION].label)
        self.edition_menu.pack(fill="x")
        self.edition_note = label(self.left, "", 11, color=t.MUTED, wraplength=440)
        self.edition_note.pack(fill="x", pady=(4, 0))
        self.custom = ctk.CTkFrame(self.left, fg_color="transparent")
        self.url_var, self.image_var, self.admin_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        label(self.custom, "URL directe de l'ISO (http/https)", 11, color=t.MUTED).pack(anchor="w", pady=(6, 0))
        row = ctk.CTkFrame(self.custom, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkEntry(row, textvariable=self.url_var, height=30, font=t.font(12)).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Vérifier", command=self._check_iso, width=80, height=30, fg_color="transparent",
                      hover_color=t.SURFACE_3, border_width=1, border_color=t.BORDER, text_color=t.TEXT,
                      font=t.font(12)).pack(side="left", padx=(6, 0))
        self.iso_status = label(self.custom, "", 11, color=t.MUTED)
        self.iso_status.pack(fill="x")
        label(self.custom, "Nom exact de l'image dans l'ISO (DISM /Get-WimInfo), ex. « Windows 11 Pro »", 11,
              color=t.MUTED).pack(anchor="w", pady=(4, 0))
        ctk.CTkEntry(self.custom, textvariable=self.image_var, height=30, font=t.font(12)).pack(fill="x")
        label(self.custom, "Compte administrateur intégré (nom localisé)", 11, color=t.MUTED).pack(anchor="w",
                                                                                                  pady=(4, 0))
        ctk.CTkEntry(self.custom, textvariable=self.admin_var, height=30, font=t.font(12), width=220).pack(anchor="w")

    def _locale_section(self) -> None:
        self.section("Langue, clavier, fuseau horaire", parent=self.right)
        grid = ctk.CTkFrame(self.right, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        self.lang_labels = {lg.label: code for code, lg in catalog.LANGS.items()}
        self.kb_labels = {lb: kid for kid, lb in catalog.KEYBOARDS.items()}
        self.tz_labels = {lb: tz for tz, lb in catalog.TIMEZONES.items()}
        if self._local_tz and self._local_tz not in catalog.TIMEZONES:
            self.tz_labels[f"{self._local_tz} (ce poste)"] = self._local_tz
        label(grid, "Langue de Windows", 12).grid(row=0, column=0, sticky="w", pady=4)
        self.lang_menu = _menu(grid, list(self.lang_labels), self._on_language, width=260)
        self.lang_menu.set(catalog.LANGS[catalog.DEFAULT_LANG].label)
        self.lang_menu.grid(row=0, column=1, sticky="e", pady=4)
        label(grid, "Clavier", 12).grid(row=1, column=0, sticky="w", pady=4)
        self.kb_menu = _menu(grid, list(self.kb_labels), lambda _v: setattr(self, "_kb_touched", True), width=260)
        self.kb_menu.grid(row=1, column=1, sticky="e", pady=4)
        label(grid, "Fuseau horaire", 12).grid(row=2, column=0, sticky="w", pady=4)
        self.tz_menu = _menu(grid, list(self.tz_labels), lambda _v: setattr(self, "_tz_touched", True), width=260)
        self.tz_menu.grid(row=2, column=1, sticky="e", pady=4)
        self._apply_language_defaults(catalog.DEFAULT_LANG)
        label(self.right, "La langue est celle de l'ISO Microsoft ; le clavier et le fuseau sont appliqués au premier "
                          "démarrage, y compris sur l'écran de connexion.", 11, color=t.MUTED,
              wraplength=440).pack(fill="x", pady=(4, 0))

    def _location_section(self) -> None:
        self.section("Emplacement", pady=(10, 6), parent=self.right)
        seg = ctk.CTkSegmentedButton(self.right, values=list(self.city_to_loc),
                                     command=lambda city: self._on_location(self.city_to_loc[city]),
                                     font=t.font(12), height=32, selected_color=t.ACCENT,
                                     selected_hover_color=t.ACCENT_HOVER)
        seg.set(city_label(self.static, self.loc_var.get()))
        seg.pack(fill="x")

    def _type_section(self) -> None:
        self.section("Type de serveur (et disque du futur bureau)", parent=self.right)
        self.offer_frame = OfferList(self.right, self.type_var, self._on_offer, height=170)
        self.offer_frame.pack(fill="x")
        self.disk_note = label(self.right, "", 11, color=t.MUTED, wraplength=440)
        self.disk_note.pack(fill="x", pady=(6, 0))

    def _options_section(self) -> None:
        self.section("Options", parent=self.left)
        ctk.CTkCheckBox(self.left, text="Épingler le snapshot comme « version d'origine » (jamais supprimé "
                                        "par la rétention)", variable=self.pin_var, font=t.font(12)).pack(anchor="w")
        ip = self.ctrl.public_ip
        if ip:
            label(self.left, f"✓  SSH et RDP limités à ton IP {ip} pendant la construction (pare-feu temporaire, "
                             "clé SSH éphémère).", 12, color=t.tone("success")[0], wraplength=440).pack(
                anchor="w", pady=(8, 0))
        else:
            notice(self.left, "IP publique inconnue : la construction attend de la connaître pour limiter "
                              "l'accès SSH/RDP au serveur temporaire.", "warning", 420).pack(fill="x", pady=(8, 0))
        label(self.left, "Installation via reinstall.sh (GPLv3, github.com/bin456789/reinstall), version figée "
                         f"au commit {catalog.REINSTALL_COMMIT[:7]} et vérifiée par SHA-256 avant exécution.", 11,
              color=t.FAINT, wraplength=440).pack(fill="x", pady=(10, 0))

    # --- réactions ---------------------------------------------------------------------------------
    def _edition(self) -> catalog.Edition:
        return catalog.EDITIONS[self.edition_labels[self.edition_menu.get()]]

    def _language(self) -> str:
        return self.lang_labels[self.lang_menu.get()]

    def _on_edition(self, _choice: str) -> None:
        ed = self._edition()
        if ed.custom:
            self.custom.pack(fill="x", pady=(4, 0), after=self.edition_note)
            self.edition_note.configure(text="Tu fournis l'ISO (lien direct vers un fichier contenant install.wim) "
                                             "et sa licence. Windows 10/11 : les prérequis TPM/CPU sont contournés.")
            if not self.admin_var.get():
                self.admin_var.set(catalog.admin_account(self._language()))
        else:
            self.custom.pack_forget()
            self.edition_note.configure(text=f"ISO officielle Microsoft, licence d'évaluation {ed.eval_days} jours "
                                             "(prolongeable avec slmgr /rearm), édition Datacenter avec bureau.")
        self.refit()

    def _apply_language_defaults(self, code: str) -> None:
        if not self._kb_touched:
            self.kb_menu.set(catalog.KEYBOARDS[catalog.default_keyboard(code)])
        if not self._tz_touched:
            tz = self._local_tz if self._local_tz else catalog.default_timezone(code)
            self.tz_menu.set(next((lb for lb, v in self.tz_labels.items() if v == tz),
                                  catalog.TIMEZONES[catalog.default_timezone(code)]))

    def _on_language(self, _choice: str) -> None:
        code = self._language()
        self._apply_language_defaults(code)
        if self._edition().custom and self.admin_var.get() in {lg.admin_account for lg in catalog.LANGS.values()}:
            self.admin_var.set(catalog.admin_account(code))

    def _on_location(self, loc: str) -> None:
        self.loc_var.set(loc)
        self._refresh_offers()

    def _refresh_offers(self) -> None:
        loc = self.loc_var.get()
        offers = compatible_offers(self.static.server_types, loc, MIN_DISK_GB, "x86")
        available = [o for o in offers if o.available]
        pick = next((o for o in available if o.stype.memory >= RECOMMENDED_RAM_GB
                     and o.stype.disk >= RECOMMENDED_DISK_GB), available[0] if available else None)
        self.offers = [replace(o, recommended=o is pick, base_type=None, disk_grows=False) for o in offers]
        if self.type_var.get() not in {o.name for o in available}:
            self.type_var.set(pick.name if pick else "")
        self.offer_frame.render(self.offers, None if available else
                                f"Aucun type disponible à {city_label(self.static, loc)} en ce moment.")
        self._on_offer()

    def selected_offer(self) -> Offer | None:
        return next((o for o in self.offers if o.name == self.type_var.get() and o.available), None)

    def _on_offer(self) -> None:
        offer = self.selected_offer()
        if offer is None:
            self.cost.configure(text="—")
            self.cost_sub.configure(text="")
            self.disk_note.configure(text="")
            self.create_btn.configure(state="disabled")
            return
        disk = offer.stype.disk
        self.disk_note.configure(text=f"Le snapshot aura un disque de {disk} Go : le bureau ne pourra démarrer que "
                                      f"sur des types à {disk} Go ou plus. Plus il est petit, plus tu gardes le "
                                      "choix des types les moins chers.")
        pricing = self.static.pricing
        hour = offer.price_h + pricing.ipv4_h(offer.location)
        storage = SNAPSHOT_GB_ESTIMATE * pricing.image_gb_month
        self.cost.configure(text=f"≈ {fmt.eur(hour)} pour la construction  ·  puis ≈ {fmt.eur_m(storage)} de stockage")
        self.cost_sub.configure(text=f"Serveur {fmt.eur_h(offer.price_h)} + IPv4 {fmt.eur_h(pricing.ipv4_h(offer.location))}, "
                                     "1 h facturée (30 à 40 min de construction) · snapshot d'environ "
                                     f"{SNAPSHOT_GB_ESTIMATE} Go à {fmt.eur(pricing.image_gb_month, 4)}/Go/mois")
        self.create_btn.configure(state="normal")

    def _check_iso(self) -> None:
        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            self.iso_status.configure(text="URL invalide (http:// ou https:// attendu)", text_color=t.tone("danger")[0])
            return
        self._iso_check_seq += 1
        seq = self._iso_check_seq
        self.iso_status.configure(text="Vérification…", text_color=t.MUTED)
        remote = self.ctrl.backend.remote

        def work() -> None:
            size = remote.url_size(url) if remote else None
            self.after(0, lambda: done(size))

        def done(size) -> None:
            if seq != self._iso_check_seq or not self.winfo_exists():
                return
            if size is None:
                self.iso_status.configure(text="Introuvable : l'URL ne répond pas 200.", text_color=t.tone("danger")[0])
            elif size and size < catalog.MIN_ISO_BYTES:
                self.iso_status.configure(text=f"Fichier trop petit pour une ISO Windows ({fmt.gb(size / 1e9)}).",
                                          text_color=t.tone("warning")[0])
            else:
                self.iso_status.configure(text=f"OK · {fmt.gb(size / 1e9) if size else 'taille inconnue'}",
                                          text_color=t.tone("success")[0])
        self.ctrl._bg.submit(work)

    # --- validation --------------------------------------------------------------------------------
    def _submit(self) -> None:
        name = self.name_var.get().strip()
        offer = self.selected_offer()
        ed, lang = self._edition(), self._language()
        ip = self.ctrl.public_ip
        if not name:
            return self.error.configure(text="Nom requis.")
        if offer is None:
            return self.error.configure(text="Choisis un type de serveur disponible.")
        if not ip:
            return self.error.configure(text="IP publique inconnue : réessaie dans un instant (elle sert à limiter "
                                             "l'accès SSH/RDP au serveur temporaire).")
        if ed.custom:
            url, image, admin = self.url_var.get().strip(), self.image_var.get().strip(), self.admin_var.get().strip()
            if not url.lower().startswith(("http://", "https://")):
                return self.error.configure(text="URL de l'ISO invalide.")
            if not image:
                return self.error.configure(text="Nom d'image requis (ex. « Windows 11 Pro »).")
            if not admin:
                return self.error.configure(text="Compte administrateur requis.")
        else:
            url, image, admin = catalog.iso_url(ed.key, lang), ed.image_name, catalog.admin_account(lang)
        params = BuildParams(
            edition=ed.key, language=lang, iso_url=url, image_name=image,
            keyboard=self.kb_labels[self.kb_menu.get()], timezone=self.tz_labels[self.tz_menu.get()],
            admin_account=admin, server_type=offer.name, location=offer.location,
            allow_cidr=netutil.normalize_cidr(ip), pin=self.pin_var.get())
        if self.app._safe(self.ctrl.build_windows, name, params) is not None:
            self.close(params)


class BuildDoneDialog(Modal):
    """Récapitulatif après une construction réussie : identifiants, réglages appliqués, licence."""

    def __init__(self, app, op) -> None:
        self.app, self.ctrl, self.op = app, app.controller, op
        super().__init__(app, "Windows de référence prêt", width=560)
        res = op.result
        ed = catalog.EDITIONS.get(res.get("edition"))
        self.heading(f"« {op.name} » est prêt",
                     f"Snapshot de {fmt.gb(res.get('image_size'))} créé" + (" et épinglé" if op.params.pin else "")
                     + ". Le serveur temporaire a été supprimé : seul le stockage est facturé.")
        self.section("Compte Windows")
        self.text(f"Utilisateur : {res.get('admin_account')} · mot de passe généré et enregistré dans le Gestionnaire "
                  "d'identifiants (connexion RDP en 1 clic).", t.TEXT, 12)
        self.section("Réglages déjà appliqués")
        kb = catalog.KEYBOARDS.get(res.get("keyboard"), res.get("keyboard"))
        tz = catalog.TIMEZONES.get(res.get("timezone"), res.get("timezone"))
        for line in (f"Clavier {kb} · fuseau {tz}", "Bureau à distance activé, pilotes VirtIO installés",
                     "Arrêt propre (bouton d'alimentation = arrêter), veille prolongée et démarrage rapide désactivés",
                     "Gestionnaire de serveur silencieux au démarrage"):
            label(self.body, f"•  {line}", 12, color=t.MUTED, wraplength=500).pack(fill="x", padx=(6, 0), pady=(2, 0))
        if ed and not ed.custom:
            self.notice(f"Licence d'évaluation Microsoft : {ed.eval_days} jours, prolongée automatiquement au "
                        f"démarrage quand il reste moins de {EVAL_ALERT_DAYS} jours. Le compte à rebours est "
                        "affiché sur la carte du bureau.", "info", pady=(12, 0))
        else:
            self.notice("ISO personnalisée : pense à activer Windows avec ta licence.", "info", pady=(12, 0))
        self.launch_btn = self.buttons(("Fermer", self.cancel, "secondary"),
                                       ("Copier le mot de passe", self._copy, "secondary"),
                                       ("Lancer maintenant", self._launch, "primary"))[2]
        self._poll()

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        d = self.ctrl.desktop(self.op.slug)
        self.launch_btn.configure(state="normal" if d and d.latest else "disabled")
        self.after(500, self._poll)

    def _copy(self) -> None:
        self.app.handle_action(self.op.slug, Act.COPY_PASSWORD)

    def _launch(self) -> None:
        d = self.ctrl.desktop(self.op.slug)
        self.close(True)
        if d:
            self.app.open_launch(d)


class LicenseDialog(Modal):
    """État de la licence d'évaluation d'un bureau, et installation manuelle de la prolongation automatique
    pour les bureaux créés avant cette fonction (ou importés)."""

    def __init__(self, app, slug: str) -> None:
        self.app, self.ctrl, self.slug = app, app.controller, slug
        d = self.ctrl.desktop(slug)
        lic = self.ctrl.eval_license(d) if d else None
        super().__init__(app, "Licence d'évaluation", width=640)
        name = d.name if d else slug
        self.heading(f"Licence d'évaluation — « {name} »")
        if lic is None:
            self.text("Aucune licence d'évaluation suivie pour ce bureau.", t.MUTED, 12)
            self.buttons(("Fermer", self.cancel, "primary"))
            return
        today = date.today()
        self.text(license.summary(lic, today), t.TEXT, 12)
        note = license.alert(lic, today)
        if note:
            self.notice(note, "warning", pady=(10, 0))
        self.text("À l'expiration, rien n'est effacé, mais Windows s'éteint seul au bout d'une heure d'utilisation. "
                  "« slmgr /rearm » redonne 180 jours, un nombre limité de fois. La date affichée est suivie par "
                  "l'application (Windows ne lui est pas accessible) : « slmgr /dli » donne la valeur exacte.",
                  t.MUTED, 11, pady=(10, 0))
        if lic.auto:
            self.text(f"Prolongation automatique installée : au démarrage, s'il reste moins de {EVAL_ALERT_DAYS} "
                      "jours, Windows se prolonge puis redémarre avant ta connexion. Journal : "
                      r"C:\ProgramData\rdpm\eval-rearm.log", t.MUTED, 11, pady=(8, 0))
            self.buttons(("Fermer", self.cancel, "primary"))
            return
        self.section("Installer la prolongation automatique")
        running = bool(d and d.server)
        self.text(("1. Dans la session du bureau, ouvre PowerShell en administrateur et colle ce bloc.\n"
                   "2. Clique ensuite sur « C'est installé » : l'information sera enregistrée à la prochaine "
                   "sauvegarde.") if running else
                  "Lance d'abord le bureau : la tâche s'installe dans Windows (PowerShell administrateur).",
                  t.TEXT, 12)
        code = render_eval_task_install(EVAL_ALERT_DAYS)
        box = ctk.CTkTextbox(self.body, height=180, font=t.mono(11), fg_color=t.SURFACE_2, wrap="none")
        box.insert("1.0", code)
        box.configure(state="disabled")
        box.pack(fill="x", pady=(8, 0))
        _, copy_btn, done_btn = self.buttons(("Fermer", self.cancel, "secondary"),
                                             ("Copier le bloc", lambda: self._copy(code), "secondary"),
                                             ("C'est installé", self._done, "primary"))
        if not running:
            done_btn.configure(state="disabled")

    def _copy(self, code: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(code)
        self.app.toast("Bloc PowerShell copié", "info")

    def _done(self) -> None:
        if self.app._safe(self.ctrl.mark_eval_auto, self.slug) is not None:
            self.close(True)
