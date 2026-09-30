"""Modèles immuables construits à partir du JSON brut de l'API Hetzner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .constants import (
    L_DESKTOP, L_FORCED, L_MANAGED, L_OP, L_OP_HOST, L_OP_IMAGE, L_OS, L_ROLE, OS_WINDOWS, RDP_PORT,
    ROLE_DESKTOP, ROLE_DESKTOP_FIREWALL, ROLE_RDP_FIREWALL,
)
from .labels import parse_description


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _price(entry: dict, key: str) -> float:
    return float(entry[key]["gross"])


class _Labelled:
    labels: dict[str, str]

    @property
    def managed(self) -> bool:
        return self.labels.get(L_MANAGED) == "1"

    @property
    def slug(self) -> str | None:
        return self.labels.get(L_DESKTOP) if self.managed else None


@dataclass(frozen=True)
class SnapshotInfo(_Labelled):
    id: int
    description: str
    labels: dict[str, str]
    status: str
    created: datetime
    image_size: float | None
    disk_size: int
    protected: bool
    architecture: str
    created_from_id: int | None = None
    created_from_name: str | None = None

    @classmethod
    def from_api(cls, d: dict) -> "SnapshotInfo":
        src = d.get("created_from") or {}
        return cls(
            id=d["id"], description=d.get("description") or "", labels=d.get("labels") or {},
            status=d.get("status", "available"), created=parse_ts(d["created"]),
            image_size=d.get("image_size"), disk_size=int(d.get("disk_size") or 0),
            protected=bool((d.get("protection") or {}).get("delete")),
            architecture=d.get("architecture") or "x86",
            created_from_id=src.get("id"), created_from_name=src.get("name"),
        )

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def forced(self) -> bool:
        return self.labels.get(L_FORCED) == "1"

    @property
    def display_name(self) -> str:
        name, _ = parse_description(self.description)
        return name or self.created_from_name or f"snapshot-{self.id}"

    @property
    def size_gb(self) -> float:
        return self.image_size or 0.0


@dataclass(frozen=True)
class ServerInfo(_Labelled):
    id: int
    name: str
    status: str
    labels: dict[str, str]
    created: datetime
    server_type: str
    cores: int
    memory: float
    cpu_type: str
    disk: int
    location: str
    ipv4: str | None
    ipv4_id: int | None
    firewall_ids: tuple[int, ...]
    volume_ids: tuple[int, ...]
    image_id: int | None
    price_hourly: float
    price_monthly: float
    locked: bool = False

    @classmethod
    def from_api(cls, d: dict) -> "ServerInfo":
        stype = d.get("server_type") or {}
        location = d.get("location") or (d.get("datacenter") or {}).get("location") or {}
        loc_name = location.get("name", "?")
        price = next((p for p in stype.get("prices", []) if p.get("location") == loc_name), None)
        pub = d.get("public_net") or {}
        ipv4 = pub.get("ipv4") or {}
        return cls(
            id=d["id"], name=d["name"], status=d.get("status", "unknown"),
            labels=d.get("labels") or {}, created=parse_ts(d["created"]),
            server_type=stype.get("name", "?"), cores=int(stype.get("cores") or 0),
            memory=float(stype.get("memory") or 0), cpu_type=stype.get("cpu_type", "shared"),
            disk=int(d.get("primary_disk_size") or stype.get("disk") or 0), location=loc_name,
            ipv4=ipv4.get("ip"), ipv4_id=ipv4.get("id"),
            firewall_ids=tuple(f["id"] for f in pub.get("firewalls") or []),
            volume_ids=tuple(d.get("volumes") or []),
            image_id=(d.get("image") or {}).get("id"),
            price_hourly=_price(price, "price_hourly") if price else 0.0,
            price_monthly=_price(price, "price_monthly") if price else 0.0,
            locked=bool(d.get("locked")),
        )

    @property
    def role(self) -> str | None:
        return self.labels.get(L_ROLE, ROLE_DESKTOP) if self.managed else None

    @property
    def op(self) -> str | None:
        return self.labels.get(L_OP)

    @property
    def op_image(self) -> int | None:
        return _int_or_none(self.labels.get(L_OP_IMAGE))

    @property
    def op_host(self) -> str | None:
        return self.labels.get(L_OP_HOST)

    @property
    def spec(self) -> str:
        cpu = "dédié" if self.cpu_type == "dedicated" else "partagé"
        return f"{self.server_type} · {self.location} · {self.cores} vCPU {cpu} · {self.memory:g} Go"


@dataclass(frozen=True)
class VolumeInfo(_Labelled):
    id: int
    name: str
    size: int
    location: str
    server_id: int | None
    labels: dict[str, str]
    created: datetime
    status: str = "available"

    @classmethod
    def from_api(cls, d: dict) -> "VolumeInfo":
        return cls(
            id=d["id"], name=d["name"], size=int(d["size"]),
            location=(d.get("location") or {}).get("name", "?"), server_id=d.get("server"),
            labels=d.get("labels") or {}, created=parse_ts(d["created"]),
            status=d.get("status", "available"),
        )


@dataclass(frozen=True)
class PrimaryIpInfo(_Labelled):
    id: int
    name: str
    ip: str
    type: str
    location: str
    assignee_id: int | None
    auto_delete: bool
    labels: dict[str, str]
    created: datetime

    @classmethod
    def from_api(cls, d: dict) -> "PrimaryIpInfo":
        location = d.get("location") or (d.get("datacenter") or {}).get("location") or {}
        return cls(
            id=d["id"], name=d.get("name") or "", ip=d["ip"], type=d.get("type", "ipv4"),
            location=location.get("name", "?"), assignee_id=d.get("assignee_id"),
            auto_delete=bool(d.get("auto_delete")), labels=d.get("labels") or {},
            created=parse_ts(d["created"]),
        )


@dataclass(frozen=True)
class FirewallRule:
    direction: str
    protocol: str
    port: str | None
    source_ips: tuple[str, ...]
    description: str | None = None

    @classmethod
    def from_api(cls, d: dict) -> "FirewallRule":
        return cls(d["direction"], d["protocol"], d.get("port"), tuple(d.get("source_ips") or ()),
                   d.get("description"))

    def to_api(self) -> dict:
        rule = {"direction": self.direction, "protocol": self.protocol,
                "source_ips": list(self.source_ips), "destination_ips": []}
        if self.port:
            rule["port"] = self.port
        if self.description:
            rule["description"] = self.description
        return rule

    @property
    def is_rdp(self) -> bool:
        return self.direction == "in" and self.port == RDP_PORT and self.protocol in ("tcp", "udp")


@dataclass(frozen=True)
class FirewallInfo(_Labelled):
    id: int
    name: str
    labels: dict[str, str]
    rules: tuple[FirewallRule, ...]
    applied_server_ids: tuple[int, ...]

    @classmethod
    def from_api(cls, d: dict) -> "FirewallInfo":
        servers = tuple(a["server"]["id"] for a in d.get("applied_to") or []
                        if a.get("type") == "server" and a.get("server"))
        return cls(d["id"], d["name"], d.get("labels") or {},
                   tuple(FirewallRule.from_api(r) for r in d.get("rules") or []), servers)

    @property
    def is_rdp_managed(self) -> bool:
        """Liste d'accès commune à tous les bureaux du projet."""
        return self.managed and self.labels.get(L_ROLE) == ROLE_RDP_FIREWALL

    @property
    def is_desktop_firewall(self) -> bool:
        """Liste d'accès propre à un bureau (label rdpm-desktop)."""
        return self.managed and self.labels.get(L_ROLE) == ROLE_DESKTOP_FIREWALL and bool(self.slug)

    @property
    def has_rdp_rules(self) -> bool:
        return any(r.is_rdp for r in self.rules)


@dataclass(frozen=True)
class ServerTypeInfo:
    name: str
    description: str
    cores: int
    memory: float
    disk: int
    cpu_type: str
    architecture: str
    prices: dict[str, tuple[float, float]]
    availability: dict[str, tuple[bool, bool]]  # location -> (available, deprecated)

    @classmethod
    def from_api(cls, d: dict) -> "ServerTypeInfo":
        prices = {p["location"]: (_price(p, "price_hourly"), _price(p, "price_monthly"))
                  for p in d.get("prices") or []}
        availability = {}
        for loc in d.get("locations") or []:
            dep = loc.get("deprecation")
            deprecated = bool(dep) and parse_ts(dep.get("unavailable_after")) is not None and \
                parse_ts(dep["unavailable_after"]) <= datetime.now(timezone.utc)
            availability[loc["name"]] = (bool(loc.get("available", True)), deprecated)
        return cls(d["name"], d.get("description") or d["name"], int(d["cores"]), float(d["memory"]),
                   int(d["disk"]), d.get("cpu_type", "shared"), d.get("architecture", "x86"),
                   prices, availability)

    @property
    def cpu_label(self) -> str:
        return "dédié" if self.cpu_type == "dedicated" else "partagé"


@dataclass(frozen=True)
class LocationInfo:
    name: str
    city: str
    country: str
    network_zone: str

    @classmethod
    def from_api(cls, d: dict) -> "LocationInfo":
        return cls(d["name"], d.get("city") or d["name"], d.get("country") or "", d.get("network_zone") or "")


@dataclass(frozen=True)
class Pricing:
    currency: str
    image_gb_month: float
    volume_gb_month: float
    ipv4_hourly: dict[str, float]
    ipv4_monthly: dict[str, float]

    @classmethod
    def from_api(cls, d: dict) -> "Pricing":
        hourly, monthly = {}, {}
        for entry in d.get("primary_ips") or []:
            if entry.get("type") != "ipv4":
                continue
            for p in entry.get("prices") or []:
                loc = p.get("location")
                hourly[loc] = _price(p, "price_hourly")
                monthly[loc] = _price(p, "price_monthly")
        return cls(d.get("currency", "EUR"), float(d["image"]["price_per_gb_month"]["gross"]),
                   float(d["volume"]["price_per_gb_month"]["gross"]), hourly, monthly)

    def ipv4_h(self, location: str) -> float:
        return self.ipv4_hourly.get(location, 0.0008)

    def ipv4_m(self, location: str) -> float:
        return self.ipv4_monthly.get(location, 0.50)


@dataclass(frozen=True)
class StaticData:
    locations: dict[str, LocationInfo]
    server_types: dict[str, ServerTypeInfo]
    pricing: Pricing

    @classmethod
    def from_api(cls, server_types: list[dict], locations: list[dict], pricing: dict) -> "StaticData":
        return cls({d["name"]: LocationInfo.from_api(d) for d in locations},
                   {d["name"]: ServerTypeInfo.from_api(d) for d in server_types},
                   Pricing.from_api(pricing))

    @classmethod
    def from_raw(cls, raw: dict) -> "StaticData":
        return cls.from_api(raw["server_types"], raw["locations"], raw["pricing"])

    def city(self, location: str) -> str:
        info = self.locations.get(location)
        return info.city if info else location


@dataclass(frozen=True)
class ActionState:
    id: int
    status: str
    progress: int
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_api(cls, d: dict) -> "ActionState":
        err = d.get("error") or {}
        return cls(d["id"], d["status"], int(d.get("progress") or 0), err.get("code"), err.get("message"))


@dataclass(frozen=True)
class Inventory:
    servers: tuple[ServerInfo, ...] = ()
    snapshots: tuple[SnapshotInfo, ...] = ()
    volumes: tuple[VolumeInfo, ...] = ()
    primary_ips: tuple[PrimaryIpInfo, ...] = ()
    firewalls: tuple[FirewallInfo, ...] = ()
    fetched_at: datetime | None = None

    def server(self, server_id: int) -> ServerInfo | None:
        return next((s for s in self.servers if s.id == server_id), None)

    def firewall(self, firewall_id: int) -> FirewallInfo | None:
        return next((f for f in self.firewalls if f.id == firewall_id), None)

    @property
    def rdp_firewall(self) -> FirewallInfo | None:
        return next((f for f in self.firewalls if f.is_rdp_managed), None)


@dataclass(frozen=True)
class Offer:
    stype: ServerTypeInfo
    location: str
    price_h: float
    price_m: float
    available: bool
    base_type: str | None = None   # type de création quand le disque est conservé
    disk_grows: bool = False       # disque agrandi (irréversible) faute de type de base
    recommended: bool = False

    @property
    def name(self) -> str:
        return self.stype.name


@dataclass
class Desktop:
    slug: str
    name: str
    snapshots: list[SnapshotInfo] = field(default_factory=list)  # du plus récent au plus ancien
    server: ServerInfo | None = None
    extra_servers: list[ServerInfo] = field(default_factory=list)
    volumes: list[VolumeInfo] = field(default_factory=list)
    fixed_ip: PrimaryIpInfo | None = None
    firewall: FirewallInfo | None = None      # liste d'accès propre au bureau (en plus de la commune)
    placeholder: bool = False

    @property
    def latest(self) -> SnapshotInfo | None:
        return next((s for s in self.snapshots if s.available), None)

    @property
    def pending_snapshot(self) -> SnapshotInfo | None:
        return next((s for s in self.snapshots if s.status == "creating"), None)

    @property
    def snapshot_gb(self) -> float:
        return sum(s.size_gb for s in self.snapshots)

    @property
    def os(self) -> str:
        """Système du bureau : label du serveur en cours, sinon du snapshot le plus récent qui le porte ;
        Windows par défaut (bureaux créés avant la prise en charge de Linux)."""
        if self.server and self.server.labels.get(L_OS):
            return self.server.labels[L_OS]
        return next((s.labels[L_OS] for s in self.snapshots if s.labels.get(L_OS)), OS_WINDOWS)

    @property
    def pinned_count(self) -> int:
        return sum(1 for s in self.snapshots if s.protected)

    def location_lock(self) -> tuple[str | None, list[str]]:
        """Emplacement imposé par les volumes détachés ou l'IP fixe, avec les raisons."""
        reasons: dict[str, list[str]] = {}
        for vol in self.volumes:
            if vol.server_id is None:
                reasons.setdefault(vol.location, []).append(f"volume « {vol.name} »")
        if self.fixed_ip:
            reasons.setdefault(self.fixed_ip.location, []).append(f"IP fixe {self.fixed_ip.ip}")
        if not reasons:
            return None, []
        loc = max(reasons, key=lambda k: len(reasons[k]))
        return loc, reasons[loc]
