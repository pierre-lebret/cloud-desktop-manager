"""Projets (une clé API d'un fournisseur cloud = un projet ; seul Hetzner Cloud aujourd'hui) : sources des clés et cloisonnement des données locales.

- La clé du .env (ou de la variable HETZNER_TOKEN) est toujours chargée, en premier.
- Les autres viennent du Gestionnaire d'identifiants (« api-token:<id> ») ; leurs noms sont dans config.json.
- Préférences, mots de passe et identifiants RDP d'un projet sont rangés sous le préfixe « <id>: ». Le projet
  principal (le premier jamais chargé, normalement celui du .env) garde les clés sans préfixe : les données
  créées avant le multi-projets restent valables.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import AppConfig, DesktopPrefs
from .providers import DEFAULT_PROVIDER, Provider, provider
from .rdp import CredentialStore

SOURCE_LABELS = {"env": "variable d'environnement", ".env": "fichier .env", "keyring": "Gestionnaire d'identifiants",
                 "session": "cette session", "fake": "simulation"}


def project_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:10]


@dataclass
class ProjectSpec:
    id: str
    name: str
    token: str
    source: str   # env / .env / keyring / session / fake
    provider_id: str = DEFAULT_PROVIDER

    @property
    def provider(self) -> Provider:
        return provider(self.provider_id)

    @property
    def masked(self) -> str:
        return f"••••{self.token[-4:]}"

    @property
    def removable(self) -> bool:
        """La clé du .env ne se retire pas depuis l'application (elle est rechargée à chaque démarrage)."""
        return self.source not in ("env", ".env")


class ProjectStore:
    """Clés connues et leurs noms. Ne parle jamais à Hetzner."""

    def __init__(self, config: AppConfig, creds: CredentialStore) -> None:
        self.config, self.creds = config, creds

    def _saved(self) -> list[dict]:
        return list(self.config.settings.get("projects") or [])

    def _write(self, items: list[dict]) -> None:
        self.config.settings["projects"] = items
        self.config.save()

    def load(self, env_token: str | None, env_source: str | None) -> list[ProjectSpec]:
        specs: list[ProjectSpec] = []
        names = {p["id"]: p.get("name") for p in self._saved()}
        if env_token:
            pid = project_id(env_token)
            specs.append(ProjectSpec(pid, names.get(pid) or "Projet principal", env_token, env_source or ".env"))
        legacy = self.creds.get_token()   # ancienne clé unique mémorisée : devient un projet nommé
        if legacy:
            pid = project_id(legacy)
            if not any(p["id"] == pid for p in self._saved()):
                self.creds.set_token(legacy, pid)
                self._write(self._saved() + [{"id": pid, "name": "Projet mémorisé"}])
            self.creds.delete_token()
        for item in self._saved():
            if any(s.id == item["id"] for s in specs):
                continue
            token = self.creds.get_token(item["id"])
            if token:
                specs.append(ProjectSpec(item["id"], item.get("name") or f"Projet {item['id'][:4]}", token, "keyring",
                                         item.get("provider") or DEFAULT_PROVIDER))
        if specs and not self.config.settings.get("primary_project"):
            self.config.settings["primary_project"] = specs[0].id
            self.config.save()
        return specs

    def primary_id(self) -> str | None:
        return self.config.settings.get("primary_project")

    def remember(self, spec: ProjectSpec) -> None:
        self.creds.set_token(spec.token, spec.id)
        items = [p for p in self._saved() if p["id"] != spec.id] + [{"id": spec.id, "name": spec.name,
                                                                        "provider": spec.provider_id}]
        self._write(items)
        spec.source = "keyring"

    def forget(self, pid: str) -> None:
        self.creds.delete_token(pid)
        self._write([p for p in self._saved() if p["id"] != pid])

    def rename(self, spec: ProjectSpec, name: str) -> None:
        spec.name = name
        items = self._saved()
        for item in items:
            if item["id"] == spec.id:
                item["name"] = name
                break
        else:
            items.append({"id": spec.id, "name": name})   # la clé du .env garde aussi son nom
        self._write(items)

    def namespace(self, pid: str) -> str:
        return "" if pid == self.primary_id() else f"{pid}:"


# --- cloisonnement des données locales ------------------------------------------------------------
def _owns(key: str, ns: str) -> bool:
    return key.startswith(ns) if ns else ":" not in key


class ScopedConfig:
    """AppConfig vu par un projet : préférences et identifiants RDP préfixés, réglages communs."""

    def __init__(self, config: AppConfig, ns: str) -> None:
        self._cfg, self.ns = config, ns

    @property
    def settings(self) -> dict:
        return self._cfg.settings

    @property
    def path(self):
        return self._cfg.path

    def get(self, key: str):
        return self._cfg.get(key)

    def save(self) -> None:
        self._cfg.save()

    @property
    def desktops(self) -> dict[str, DesktopPrefs]:
        n = len(self.ns)
        return {k[n:]: v for k, v in self._cfg.desktops.items() if _owns(k, self.ns)}

    def prefs(self, slug: str) -> DesktopPrefs:
        return self._cfg.prefs(self.ns + slug)

    def update_prefs(self, slug: str, **changes) -> None:
        self._cfg.update_prefs(self.ns + slug, **changes)

    def copy_prefs(self, src: str, dst: str, display_name: str) -> None:
        self._cfg.copy_prefs(self.ns + src, self.ns + dst, display_name)

    def forget_desktop(self, slug: str) -> None:
        self._cfg.forget_desktop(self.ns + slug)

    @property
    def managed_creds(self) -> dict[str, str]:
        n = len(self.ns)
        return {ip: slug[n:] for ip, slug in self._cfg.managed_creds.items() if _owns(slug, self.ns)}

    def add_cred(self, ip: str, slug: str) -> None:
        self._cfg.add_cred(ip, self.ns + slug)

    def remove_cred(self, ip: str) -> None:
        if _owns(self._cfg.managed_creds.get(ip, self.ns), self.ns):
            self._cfg.remove_cred(ip)


class ScopedCreds:
    """Mots de passe des bureaux d'un projet, préfixés comme les préférences."""

    def __init__(self, creds: CredentialStore, ns: str) -> None:
        self._creds, self.ns = creds, ns

    def get_password(self, slug: str) -> str | None:
        return self._creds.get_password(self.ns + slug)

    def set_password(self, slug: str, password: str) -> None:
        self._creds.set_password(self.ns + slug, password)

    def delete_password(self, slug: str) -> None:
        self._creds.delete_password(self.ns + slug)

    def copy_password(self, src: str, dst: str) -> None:
        self._creds.copy_password(self.ns + src, self.ns + dst)
