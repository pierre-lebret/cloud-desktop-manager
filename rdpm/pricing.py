"""Calculs de coûts (fonctions pures)."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Inventory, Pricing, ServerInfo


def billed_hours(created: datetime, now: datetime) -> int:
    """Hetzner facture chaque heure entamée."""
    elapsed = max(0.0, (now - created).total_seconds())
    return max(1, math.ceil(elapsed / 3600))


def billing_minute(created: datetime, now: datetime) -> int:
    elapsed = max(0.0, (now - created).total_seconds())
    return int((elapsed % 3600) // 60)


def server_burn(server: ServerInfo, pricing: Pricing) -> float:
    ip = pricing.ipv4_h(server.location) if server.ipv4 else 0.0
    return server.price_hourly + ip


def session_cost(server: ServerInfo, pricing: Pricing, now: datetime) -> float:
    hours = billed_hours(server.created, now)
    return min(hours * server.price_hourly, server.price_monthly or math.inf) + \
        (hours * pricing.ipv4_h(server.location) if server.ipv4 else 0.0)


@dataclass(frozen=True)
class CostSummary:
    running_h: float          # €/h des serveurs existants (allumés ou éteints) + leurs IPv4
    running_count: int
    storage_month: float      # snapshots + volumes
    fixed_ips_month: float    # IP primaires conservées (non supprimées avec le serveur)
    idle_month: float         # ce que coûte le compte quand tout est archivé
    month_estimate: float


def compute_costs(inv: Inventory, pricing: Pricing, sessions_this_month: float,
                  now: datetime | None = None) -> CostSummary:
    now = now or datetime.now(timezone.utc)
    running_h = sum(server_burn(s, pricing) for s in inv.servers)
    storage = sum(s.size_gb for s in inv.snapshots) * pricing.image_gb_month
    storage += sum(v.size for v in inv.volumes) * pricing.volume_gb_month
    fixed_ips = sum(pricing.ipv4_m(ip.location) for ip in inv.primary_ips if not ip.auto_delete)
    idle = storage + fixed_ips
    days = calendar.monthrange(now.year, now.month)[1]
    month_fraction = (now.day - 1 + now.hour / 24) / days
    running_sessions = sum(session_cost(s, pricing, now) for s in inv.servers)
    estimate = sessions_this_month + running_sessions + idle * month_fraction
    return CostSummary(running_h, len(inv.servers), storage, fixed_ips, idle, estimate)
