"""Sélection des types de serveur compatibles avec un snapshot (fonctions pures).

Piège : le disk_size d'un snapshot est la taille du disque du serveur source. Lancer sur un type
à plus gros disque puis sauvegarder rendrait le bureau incompatible avec les petits types. On crée
donc le serveur sur un « type de base » (disque = disk_size), puis change_type(upgrade_disk=False).
"""

from __future__ import annotations

from dataclasses import replace

from .models import Offer, ServerTypeInfo


def _usable(st: ServerTypeInfo, location: str, arch: str, min_disk: int) -> bool:
    avail = st.availability.get(location)
    return (st.architecture == arch and st.disk >= min_disk and avail is not None
            and not avail[1] and location in st.prices)


def base_type_for(types: dict[str, ServerTypeInfo], location: str, disk_size: int,
                  arch: str) -> ServerTypeInfo | None:
    candidates = [st for st in types.values()
                  if _usable(st, location, arch, disk_size) and st.disk == disk_size
                  and st.availability[location][0]]
    return min(candidates, key=lambda st: st.prices[location][0], default=None)


def compatible_offers(types: dict[str, ServerTypeInfo], location: str, disk_size: int,
                      arch: str = "x86", include_unavailable: bool = False) -> list[Offer]:
    base = base_type_for(types, location, disk_size, arch)
    offers = []
    for st in types.values():
        if not _usable(st, location, arch, disk_size):
            continue
        available = st.availability[location][0]
        if not available and not include_unavailable:
            continue
        bigger = st.disk > disk_size
        price_h, price_m = st.prices[location]
        offers.append(Offer(
            stype=st, location=location, price_h=price_h, price_m=price_m, available=available,
            base_type=base.name if bigger and base else None,
            disk_grows=bigger and base is None,
        ))
    offers.sort(key=lambda o: (not o.available, o.price_h))
    pick = next((o for o in offers if o.available and o.stype.disk == disk_size), None) or \
        next((o for o in offers if o.available), None)
    return [replace(o, recommended=True) if o is pick else o for o in offers]


def recommended_offer(types: dict[str, ServerTypeInfo], location: str, disk_size: int,
                      arch: str = "x86") -> Offer | None:
    return next((o for o in compatible_offers(types, location, disk_size, arch) if o.recommended), None)


def best_by_location(types: dict[str, ServerTypeInfo], locations: list[str], disk_size: int,
                     arch: str = "x86") -> dict[str, Offer | None]:
    return {loc: recommended_offer(types, loc, disk_size, arch) for loc in locations}


def alternatives(types: dict[str, ServerTypeInfo], locations: list[str], disk_size: int,
                 arch: str, exclude: tuple[str, str], limit: int = 3) -> list[Offer]:
    """Suggestions quand un type est indisponible : autres types ici, puis ailleurs."""
    here = [o for o in compatible_offers(types, exclude[1], disk_size, arch)
            if o.name != exclude[0]]
    elsewhere = [o for loc, o in best_by_location(types, locations, disk_size, arch).items()
                 if o and loc != exclude[1]]
    elsewhere.sort(key=lambda o: o.price_h)
    return (here[:2] + elsewhere)[:limit]


def cheapest_base_anywhere(types: dict[str, ServerTypeInfo], locations: list[str], disk_size: int,
                           arch: str, prefer: str | None = None) -> tuple[str, ServerTypeInfo] | None:
    """Type de serveur temporaire pour copier un snapshot sans changer son disk_size."""
    ordered = ([prefer] if prefer in locations else []) + [l for l in locations if l != prefer]
    best = None
    for loc in ordered:
        st = base_type_for(types, loc, disk_size, arch)
        if st and (best is None or st.prices[loc][0] < best[1].prices[best[0]][0]):
            best = (loc, st)
    return best
