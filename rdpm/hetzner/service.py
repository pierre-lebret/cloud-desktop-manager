"""Accès à l'API Hetzner Cloud.

Le transport est un hcloud.Client par thread (sa session HTTP n'est pas partagée entre threads,
timeout réseau explicite) ou le FakeCloud en mémoire. On passe par client.request() pour garder
la main sur les payloads et parser le JSON brut dans nos modèles.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable

from hcloud import APIException, Client

from .. import __version__, netutil
from ..constants import APP_NAME, L_DESKTOP, L_MANAGED
from ..labels import merge_labels
from ..models import (
    ActionState, FirewallInfo, FirewallRule, Inventory, PrimaryIpInfo, ServerInfo, SnapshotInfo,
    StaticData, VolumeInfo,
)
from .errors import ReadOnlyError, UserError, to_user_error

log = logging.getLogger(__name__)


class HcloudTransport:
    def __init__(self, token: str) -> None:
        self.token = token
        self._local = threading.local()

    def request(self, method: str, url: str, **kwargs) -> dict:
        client = getattr(self._local, "client", None)
        if client is None:
            client = Client(token=self.token, application_name=APP_NAME,
                            application_version=__version__, timeout=(5, 30))
            self._local.client = client
        return client.request(method, url, **kwargs)


def _action_ids(resp: dict) -> list[int]:
    ids = []
    if resp.get("action"):
        ids.append(resp["action"]["id"])
    ids.extend(a["id"] for a in resp.get("next_actions") or [])
    ids.extend(a["id"] for a in resp.get("actions") or [])
    return ids


class HetznerService:
    firewall_lock = threading.Lock()  # set_rules remplace toute la liste : écritures sérialisées

    def __init__(self, transport, *, readonly: bool = False,
                 probe_fn: Callable[[str], bool] = netutil.rdp_probe,
                 public_ip_fn: Callable[[], str | None] = netutil.detect_public_ip,
                 remote=None) -> None:
        self.transport = transport
        self.readonly = readonly
        self._probe = probe_fn
        self._public_ip = public_ip_fn
        self.remote = remote  # exécution de scripts SSH (construction d'un Windows de référence)

    # --- token -----------------------------------------------------------------------------
    def set_token(self, token: str) -> None:
        """Remplace le transport sur place : les OpContext qui référencent ce service suivent."""
        self.transport = HcloudTransport(token)

    @staticmethod
    def verify_token(token: str) -> None:
        """Lecture minimale avec ce token ; lève une UserError s'il est refusé ou si Hetzner est injoignable."""
        try:
            HcloudTransport(token).request("GET", "/servers", params={"per_page": 1})
        except Exception as exc:  # noqa: BLE001
            err = to_user_error(exc)
            if err.code in ("unauthorized", "forbidden"):
                raise UserError("Token refusé par Hetzner",
                                "Vérifie qu'il est complet et n'a pas été révoqué (console Hetzner → "
                                "ton projet → Sécurité → Tokens API).", err.code) from exc
            raise err from exc

    # --- transport -----------------------------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        return self.transport.request("GET", path, params=params or {})

    def _write(self, method: str, path: str, what: str, body: dict | None = None) -> dict:
        if self.readonly:
            log.info("[lecture seule] %s %s ignoré (%s)", method, path, what)
            raise ReadOnlyError(what)
        kwargs = {"json": body} if body is not None else {}
        return self.transport.request(method, path, **kwargs) or {}

    def _get_all(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        items, page = [], 1
        while True:
            resp = self._get(path, {**(params or {}), "page": page, "per_page": 50})
            items.extend(resp.get(key) or [])
            nxt = ((resp.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not nxt:
                return items
            page = nxt

    def _get_or_none(self, path: str, key: str) -> dict | None:
        try:
            return self._get(path).get(key)
        except APIException as exc:
            if str(exc.code) == "not_found":
                return None
            raise

    # --- lecture ---------------------------------------------------------------------------
    def fetch_static_raw(self) -> dict:
        return {"server_types": self._get_all("/server_types", "server_types"),
                "locations": self._get_all("/locations", "locations"),
                "pricing": self._get("/pricing")["pricing"]}

    def fetch_static(self) -> StaticData:
        return StaticData.from_raw(self.fetch_static_raw())

    def fetch_inventory(self) -> Inventory:
        return Inventory(
            servers=tuple(ServerInfo.from_api(d) for d in self._get_all("/servers", "servers")),
            snapshots=tuple(SnapshotInfo.from_api(d) for d in
                            self._get_all("/images", "images", {"type": "snapshot"})),
            volumes=tuple(VolumeInfo.from_api(d) for d in self._get_all("/volumes", "volumes")),
            primary_ips=tuple(PrimaryIpInfo.from_api(d) for d in
                              self._get_all("/primary_ips", "primary_ips")),
            firewalls=tuple(FirewallInfo.from_api(d) for d in self._get_all("/firewalls", "firewalls")),
            fetched_at=datetime.now(timezone.utc),
        )

    def get_server(self, server_id: int) -> ServerInfo | None:
        d = self._get_or_none(f"/servers/{server_id}", "server")
        return ServerInfo.from_api(d) if d else None

    def get_image(self, image_id: int) -> SnapshotInfo | None:
        d = self._get_or_none(f"/images/{image_id}", "image")
        return SnapshotInfo.from_api(d) if d else None

    def list_snapshots(self, slug: str) -> list[SnapshotInfo]:
        selector = f"{L_MANAGED}=1,{L_DESKTOP}={slug}"
        return [SnapshotInfo.from_api(d) for d in
                self._get_all("/images", "images", {"type": "snapshot", "label_selector": selector})]

    def get_firewall(self, firewall_id: int) -> FirewallInfo:
        return FirewallInfo.from_api(self._get(f"/firewalls/{firewall_id}")["firewall"])

    def get_actions(self, ids: list[int]) -> dict[int, ActionState]:
        try:
            resp = self._get("/actions", {"id": list(ids)})
            return {a["id"]: ActionState.from_api(a) for a in resp.get("actions") or []}
        except APIException:
            result = {}
            for aid in ids:
                result[aid] = ActionState.from_api(self._get(f"/actions/{aid}")["action"])
            return result

    def rdp_probe(self, ip: str) -> bool:
        return self._probe(ip)

    def public_ip(self) -> str | None:
        return self._public_ip()

    # --- serveurs --------------------------------------------------------------------------
    def create_server(self, *, name: str, server_type: str, image_id: int | str, location: str,
                      labels: dict[str, str], firewall_ids: Iterable[int] = (),
                      volume_ids: Iterable[int] = (), primary_ip_id: int | None = None,
                      start: bool = True, enable_ipv4: bool = True,
                      enable_ipv6: bool = False, ssh_key_ids: Iterable[int] = ()) -> tuple[ServerInfo, list[int]]:
        """`image_id` accepte aussi le nom d'une image système Hetzner (ex. « ubuntu-24.04 »)."""
        public_net: dict = {"enable_ipv4": enable_ipv4, "enable_ipv6": enable_ipv6}
        if primary_ip_id:
            public_net["ipv4"] = primary_ip_id
        body = {"name": name, "server_type": server_type, "image": image_id, "location": location,
                "start_after_create": start, "labels": labels, "public_net": public_net,
                "firewalls": [{"firewall": fid} for fid in firewall_ids]}
        volume_ids = list(volume_ids)
        if volume_ids:
            body["volumes"] = volume_ids
            body["automount"] = False
        ssh_key_ids = list(ssh_key_ids)
        if ssh_key_ids:
            body["ssh_keys"] = ssh_key_ids
        resp = self._write("POST", "/servers", f"créer le serveur {name}", body)
        return ServerInfo.from_api(resp["server"]), _action_ids(resp)

    def server_action(self, server_id: int, action: str, **payload) -> int:
        resp = self._write("POST", f"/servers/{server_id}/actions/{action}", action, payload or None)
        return resp["action"]["id"]

    def change_type(self, server_id: int, server_type: str, upgrade_disk: bool) -> int:
        return self.server_action(server_id, "change_type", server_type=server_type,
                                  upgrade_disk=upgrade_disk)

    def create_snapshot(self, server_id: int, description: str,
                        labels: dict[str, str]) -> tuple[int, int]:
        resp = self._write("POST", f"/servers/{server_id}/actions/create_image", "créer un snapshot",
                           {"description": description, "type": "snapshot", "labels": labels})
        return resp["image"]["id"], resp["action"]["id"]

    def delete_server(self, server_id: int) -> int | None:
        resp = self._write("DELETE", f"/servers/{server_id}", "supprimer le serveur")
        return (resp.get("action") or {}).get("id")

    def update_server_labels(self, server_id: int, updates: dict | None = None,
                             remove: Iterable[str] = ()) -> ServerInfo | None:
        current = self.get_server(server_id)
        if current is None:
            return None
        labels = merge_labels(current.labels, updates, tuple(remove))
        resp = self._write("PUT", f"/servers/{server_id}", "modifier les labels", {"labels": labels})
        return ServerInfo.from_api(resp["server"])

    # --- snapshots -------------------------------------------------------------------------
    def update_image(self, image_id: int, description: str | None = None,
                     updates: dict | None = None, remove: Iterable[str] = ()) -> SnapshotInfo:
        body: dict = {}
        if updates or remove:
            current = self.get_image(image_id)
            body["labels"] = merge_labels(current.labels if current else {}, updates, tuple(remove))
        if description is not None:
            body["description"] = description
        resp = self._write("PUT", f"/images/{image_id}", "modifier le snapshot", body)
        return SnapshotInfo.from_api(resp["image"])

    def set_image_protection(self, image_id: int, protected: bool) -> int:
        resp = self._write("POST", f"/images/{image_id}/actions/change_protection",
                           "changer la protection", {"delete": protected})
        return resp["action"]["id"]

    def delete_image(self, image_id: int) -> None:
        self._write("DELETE", f"/images/{image_id}", "supprimer le snapshot")

    # --- volumes ---------------------------------------------------------------------------
    def create_volume(self, size: int, name: str, labels: dict[str, str],
                      server_id: int, fs_format: str | None = None) -> tuple[VolumeInfo, list[int]]:
        # server= OU location= (jamais les deux) ; format (ext4, xfs) : bureau Linux uniquement.
        body = {"size": size, "name": name, "labels": labels, "server": server_id, "automount": False}
        if fs_format:
            body["format"] = fs_format
        resp = self._write("POST", "/volumes", f"créer le volume {name}", body)
        return VolumeInfo.from_api(resp["volume"]), _action_ids(resp)

    def attach_volume(self, volume_id: int, server_id: int) -> int:
        resp = self._write("POST", f"/volumes/{volume_id}/actions/attach", "attacher le volume",
                           {"server": server_id, "automount": False})
        return resp["action"]["id"]

    def detach_volume(self, volume_id: int) -> int:
        resp = self._write("POST", f"/volumes/{volume_id}/actions/detach", "détacher le volume", {})
        return resp["action"]["id"]

    def resize_volume(self, volume_id: int, size: int) -> int:
        resp = self._write("POST", f"/volumes/{volume_id}/actions/resize", "agrandir le volume",
                           {"size": size})
        return resp["action"]["id"]

    def delete_volume(self, volume_id: int) -> None:
        self._write("DELETE", f"/volumes/{volume_id}", "supprimer le volume")

    def update_volume_labels(self, volume: VolumeInfo, updates: dict | None = None,
                             remove: Iterable[str] = ()) -> None:
        self._write("PUT", f"/volumes/{volume.id}", "modifier le volume",
                    {"labels": merge_labels(volume.labels, updates, tuple(remove))})

    # --- pare-feu --------------------------------------------------------------------------
    def create_firewall(self, name: str, labels: dict[str, str],
                        rules: list[FirewallRule]) -> FirewallInfo:
        resp = self._write("POST", "/firewalls", f"créer le pare-feu {name}",
                           {"name": name, "labels": labels, "rules": [r.to_api() for r in rules]})
        return FirewallInfo.from_api(resp["firewall"])

    def update_firewall_labels(self, firewall_id: int, updates: dict | None = None,
                               remove: Iterable[str] = ()) -> None:
        current = self.get_firewall(firewall_id)
        self._write("PUT", f"/firewalls/{firewall_id}", "modifier le pare-feu",
                    {"labels": merge_labels(current.labels, updates, tuple(remove))})

    def modify_firewall_rules(self, firewall_id: int,
                              mutate: Callable[[list[FirewallRule]], list[FirewallRule]]) -> list[int]:
        """Relit les règles sous verrou, applique la modification, réécrit la liste complète."""
        with self.firewall_lock:
            current = self.get_firewall(firewall_id)
            rules = mutate(list(current.rules))
            if list(rules) == list(current.rules):
                return []
            resp = self._write("POST", f"/firewalls/{firewall_id}/actions/set_rules",
                               "modifier les règles du pare-feu", {"rules": [r.to_api() for r in rules]})
            return _action_ids(resp)

    def apply_firewall(self, firewall_id: int, server_ids: list[int]) -> list[int]:
        resp = self._write("POST", f"/firewalls/{firewall_id}/actions/apply_to_resources",
                           "appliquer le pare-feu",
                           {"apply_to": [{"type": "server", "server": {"id": sid}} for sid in server_ids]})
        return _action_ids(resp)

    def remove_firewall(self, firewall_id: int, server_ids: list[int]) -> list[int]:
        resp = self._write("POST", f"/firewalls/{firewall_id}/actions/remove_from_resources",
                           "retirer le pare-feu",
                           {"remove_from": [{"type": "server", "server": {"id": sid}} for sid in server_ids]})
        return _action_ids(resp)

    def list_firewalls(self, label_selector: str) -> list[FirewallInfo]:
        return [FirewallInfo.from_api(d) for d in
                self._get_all("/firewalls", "firewalls", {"label_selector": label_selector})]

    def delete_firewall(self, firewall_id: int) -> None:
        self._write("DELETE", f"/firewalls/{firewall_id}", "supprimer le pare-feu")

    # --- clés SSH (serveur de construction) ---------------------------------------------------
    def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> int:
        resp = self._write("POST", "/ssh_keys", f"enregistrer la clé SSH {name}",
                           {"name": name, "public_key": public_key, "labels": labels})
        return resp["ssh_key"]["id"]

    def list_ssh_keys(self, label_selector: str) -> list[dict]:
        return self._get_all("/ssh_keys", "ssh_keys", {"label_selector": label_selector})

    def delete_ssh_key(self, key_id: int) -> None:
        self._write("DELETE", f"/ssh_keys/{key_id}", "supprimer la clé SSH")

    # --- IP primaires ----------------------------------------------------------------------
    def create_primary_ip(self, name: str, location: str, labels: dict[str, str]) -> PrimaryIpInfo:
        resp = self._write("POST", "/primary_ips", f"créer l'IP {name}",
                           {"name": name, "type": "ipv4", "location": location,
                            "assignee_type": "server", "auto_delete": False, "labels": labels})
        return PrimaryIpInfo.from_api(resp["primary_ip"])

    def update_primary_ip(self, ip: PrimaryIpInfo, updates: dict | None = None,
                          remove: Iterable[str] = (), name: str | None = None) -> None:
        body: dict = {"labels": merge_labels(ip.labels, updates, tuple(remove)), "auto_delete": False}
        if name:
            body["name"] = name
        self._write("PUT", f"/primary_ips/{ip.id}", "modifier l'IP", body)

    def delete_primary_ip(self, ip_id: int) -> None:
        self._write("DELETE", f"/primary_ips/{ip_id}", "supprimer l'IP")
