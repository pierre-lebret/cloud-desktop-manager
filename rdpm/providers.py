"""Fournisseurs cloud : ce que l'interface a besoin d'en dire (nom, icône, aide pour la clé API).

Seul Hetzner Cloud est pris en charge aujourd'hui ; les textes génériques de l'application passent par
ce catalogue plutôt que de nommer Hetzner en dur, pour accueillir d'autres fournisseurs.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PROVIDER = "hetzner"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str            # « Hetzner Cloud »
    short: str           # « Hetzner »
    icon: str            # icône de rdpm/ui/assets
    token_env: str       # variable d'environnement / clé du .env
    token_help: str      # où créer une clé API
    token_min_length: int = 32


PROVIDERS: dict[str, Provider] = {
    "hetzner": Provider(
        id="hetzner", name="Hetzner Cloud", short="Hetzner", icon="provider-hetzner", token_env="HETZNER_TOKEN",
        token_help="console Hetzner → ton projet → Sécurité → Tokens API, en « Lecture & écriture »"),
}


def provider(provider_id: str | None) -> Provider:
    return PROVIDERS.get(provider_id or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])
