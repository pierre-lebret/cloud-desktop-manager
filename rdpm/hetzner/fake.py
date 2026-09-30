"""FakeCloud : émulation en mémoire de l'API REST Hetzner, utilisée comme transport.

Sert au mode --fake (UI sans frais, temps accéléré) et aux tests. Les pannes injectables
(`fail`) permettent d'exercer tous les chemins d'erreur :
  snapshot, shutdown_timeout, resource_unavailable, delete, probe, change_type,
  build_ssh_timeout, build_prepare, build_image_name, build_install_error, build_rdp_never,
  linux_install_error, linux_unit_killed, linux_rdp_never, apps_one_fails, apps_unit_killed,
  admin_ssh_refused.

`FakeRemote` simule le SSH du serveur de construction (Ubuntu → Alpine → Windows) pour BuildOp, et l'accès
d'administration d'un bureau (clé de l'application, port 22 ouvert à ton IP) pour les logiciels.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hcloud import APIException

from ..build.scripts import tag_of
from ..constants import ADMIN_KEY_PATH, L_ADMIN, L_APPS
from ..remote import RemoteResult
from ..software import catalog as apps_catalog

SEED_PATH = Path(__file__).with_name("static_seed.json")
SYSTEM_IMAGES = {
    "ubuntu-24.04": {"id": 161547269, "name": "ubuntu-24.04", "os_flavor": "ubuntu"},
    "ubuntu-22.04": {"id": 67794396, "name": "ubuntu-22.04", "os_flavor": "ubuntu"},
    "debian-12": {"id": 114690387, "name": "debian-12", "os_flavor": "debian"},
    "ubuntu-26.04": {"id": 288447615, "name": "ubuntu-26.04", "os_flavor": "ubuntu"},
    "debian-13": {"id": 247051392, "name": "debian-13", "os_flavor": "debian"},
}
BUILD_BOOT_S = 20          # secondes simulées avant que SSH réponde après un (re)démarrage
BUILD_WINDOWS_S = 300      # durée simulée de l'installation Windows avant que RDP réponde
IMAGE_NAMES = ["Windows Server 2025 SERVERSTANDARDCORE", "Windows Server 2025 SERVERSTANDARD",
               "Windows Server 2025 SERVERDATACENTERCORE", "Windows Server 2025 SERVERDATACENTER"]

DURATIONS = {  # secondes simulées
    "create_server": 40, "start_server": 15, "shutdown_server": 2, "os_shutdown": 30,
    "stop_server": 5, "change_server_type": 25, "delete_server": 8, "reboot_server": 20,
    "create_volume": 6, "attach_volume": 5, "detach_volume": 5, "resize_volume": 5,
    "set_firewall_rules": 2, "change_protection": 1, "apply_firewall": 2,
}
BOOT_TO_RDP_S = 45


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _err(code: str, message: str) -> APIException:
    return APIException(code=code, message=message, details=None)


def _match(labels: dict, selector: str | None) -> bool:
    if not selector:
        return True
    for term in selector.split(","):
        term = term.strip()
        if "!=" in term:
            k, v = term.split("!=", 1)
            if labels.get(k) == v:
                return False
        elif "=" in term:
            k, v = term.split("=", 1)
            if labels.get(k) != v:
                return False
        elif term.startswith("!"):
            if term[1:] in labels:
                return False
        elif term not in labels:
            return False
    return True


class FakeCloud:
    def __init__(self, speed: float = 8.0, fail: set[str] | None = None, seed: str = "demo") -> None:
        self.speed = speed
        self.fail = set(fail or ())
        self._lock = threading.RLock()
        self._t0 = time.monotonic()
        self._ids = itertools.count(900_000)
        self._timeline: list[tuple[float, callable]] = []
        static = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        self.server_types = {d["name"]: d for d in static["server_types"]}
        self.locations = {d["name"]: d for d in static["locations"]}
        self.pricing = static["pricing"]
        self.servers: dict[int, dict] = {}
        self.images: dict[int, dict] = {}
        self.volumes: dict[int, dict] = {}
        self.primary_ips: dict[int, dict] = {}
        self.firewalls: dict[int, dict] = {}
        self.ssh_keys: dict[int, dict] = {}
        self.actions: dict[int, dict] = {}
        self.boot_at: dict[int, float] = {}
        self.os_shutdown_pending: set[int] = set()
        self.public_ip_value = "198.51.100.23"
        self.requests: list[tuple[str, str, dict | None]] = []
        self.builds: dict[int, dict] = {}   # serveur de construction → état simulé (voir FakeRemote)
        self.admin: dict[int, dict] = {}    # bureau → logiciels installés et installation en cours
        self.remote = FakeRemote(self)
        self._seed_account()
        if seed == "demo":
            self._seed_demo()

    # --- horloge ---------------------------------------------------------------------------
    def sim(self) -> float:
        return (time.monotonic() - self._t0) * self.speed

    def _at(self, delay: float, fn) -> None:
        self._timeline.append((self.sim() + delay, fn))

    def _tick(self) -> None:
        now = self.sim()
        due = [item for item in self._timeline if item[0] <= now]
        self._timeline = [item for item in self._timeline if item[0] > now]
        for _, fn in sorted(due, key=lambda item: item[0]):
            fn()

    def _action(self, command: str, resources: list[tuple[int, str]], duration: float | None = None,
                on_done=None, fail_code: str | None = None) -> dict:
        aid = next(self._ids)
        duration = DURATIONS.get(command, 3) if duration is None else duration
        action = {"id": aid, "command": command, "status": "running", "progress": 0,
                  "started": _iso(_now()), "finished": None, "error": None,
                  "resources": [{"id": rid, "type": rtype} for rid, rtype in resources],
                  "_start": self.sim(), "_duration": max(duration, 0.1)}
        self.actions[aid] = action

        def finish() -> None:
            action["progress"] = 100
            action["finished"] = _iso(_now())
            if fail_code:
                action["status"] = "error"
                action["error"] = {"code": fail_code, "message": f"{command} failed (simulé)"}
            else:
                action["status"] = "success"
                if on_done:
                    on_done()
        self._at(duration, finish)
        return action

    @staticmethod
    def _public(action: dict) -> dict:
        return {k: v for k, v in action.items() if not k.startswith("_")}

    # --- API transport ---------------------------------------------------------------------
    def request(self, method: str, url: str, params: dict | None = None, json: dict | None = None,
                **_kwargs) -> dict:
        with self._lock:
            self.requests.append((method, url, copy.deepcopy(json)))
            self._tick()
            for action in self.actions.values():
                if action["status"] == "running":
                    elapsed = self.sim() - action["_start"]
                    action["progress"] = min(99, int(100 * elapsed / action["_duration"]))
            parts = [p for p in url.split("?")[0].strip("/").split("/") if p]
            return copy.deepcopy(self._route(method, parts, params or {}, json or {}))

    def probe(self, ip: str) -> bool:
        if "probe" in self.fail:
            return False
        with self._lock:
            self._tick()
            for srv in self.servers.values():
                pub = (srv["public_net"]["ipv4"] or {}).get("ip")
                if pub == ip and srv["status"] == "running":
                    return self.sim() - self.boot_at.get(srv["id"], self.sim()) >= BOOT_TO_RDP_S
        return False

    def public_ip(self) -> str:
        return self.public_ip_value

    def _route(self, method: str, parts: list[str], params: dict, body: dict) -> dict:
        root = parts[0]
        if method == "GET" and len(parts) == 1:
            return self._list(root, params)
        if root == "actions" and len(parts) == 2:
            return {"action": self._public(self._find(self.actions, parts[1], "action"))}
        store = {"servers": self.servers, "images": self.images, "volumes": self.volumes,
                 "primary_ips": self.primary_ips, "firewalls": self.firewalls,
                 "ssh_keys": self.ssh_keys}.get(root)
        key = {"servers": "server", "images": "image", "volumes": "volume",
               "primary_ips": "primary_ip", "firewalls": "firewall", "ssh_keys": "ssh_key"}.get(root)
        if method == "POST" and len(parts) == 1:
            return getattr(self, f"_create_{key}")(body)
        obj = self._find(store, parts[1], key)
        if len(parts) == 2:
            if method == "GET":
                return {key: obj}
            if method == "PUT":
                for field in ("labels", "description", "name", "auto_delete"):
                    if field in body:
                        obj[field] = body[field]
                return {key: obj}
            if method == "DELETE":
                return getattr(self, f"_delete_{key}")(obj)
        if len(parts) == 4 and parts[2] == "actions":
            return getattr(self, f"_{key}_{parts[3]}")(obj, body)
        raise _err("not_found", f"route inconnue {method} /{'/'.join(parts)}")

    def _find(self, store: dict, raw_id: str, key: str) -> dict:
        try:
            return store[int(raw_id)]
        except (KeyError, ValueError):
            raise _err("not_found", f"{key} {raw_id} not found") from None

    def _list(self, root: str, params: dict) -> dict:
        page_meta = {"pagination": {"page": 1, "per_page": 50, "next_page": None, "last_page": 1}}
        if root == "server_types":
            return {"server_types": list(self.server_types.values()), "meta": page_meta}
        if root == "locations":
            return {"locations": list(self.locations.values()), "meta": page_meta}
        if root == "pricing":
            return {"pricing": self.pricing}
        if root == "actions":
            ids = params.get("id") or []
            ids = ids if isinstance(ids, list) else [ids]
            return {"actions": [self._public(self.actions[int(i)]) for i in ids if int(i) in self.actions]}
        store = {"servers": self.servers, "images": self.images, "volumes": self.volumes,
                 "primary_ips": self.primary_ips, "firewalls": self.firewalls,
                 "ssh_keys": self.ssh_keys}[root]
        items = [o for o in store.values() if _match(o.get("labels") or {}, params.get("label_selector"))]
        if root == "images" and params.get("type"):
            items = [o for o in items if o["type"] == params["type"]]
        return {root: items, "meta": page_meta}

    # --- serveurs --------------------------------------------------------------------------
    def _new_primary_ip(self, location: str, auto_delete: bool, labels: dict | None = None,
                        name: str | None = None) -> dict:
        ipid = next(self._ids)
        n = len(self.primary_ips) + 10
        ip = {"id": ipid, "name": name or f"primary_ip-{ipid}", "ip": f"203.0.113.{n % 250}",
              "type": "ipv4", "assignee_id": None, "assignee_type": "server",
              "auto_delete": auto_delete, "labels": labels or {}, "created": _iso(_now()),
              "location": copy.deepcopy(self.locations[location]), "protection": {"delete": False},
              "blocked": False, "dns_ptr": []}
        self.primary_ips[ipid] = ip
        return ip

    def _server_json(self, sid: int, name: str, stype: dict, location: str, labels: dict,
                     image_id: int | None, status: str, created: datetime, disk: int,
                     ipv4: dict | None, firewall_ids: list[int], volume_ids: list[int]) -> dict:
        return {"id": sid, "name": name, "status": status, "created": _iso(created),
                "server_type": copy.deepcopy(stype), "location": copy.deepcopy(self.locations[location]),
                "labels": labels, "image": {"id": image_id} if image_id else None,
                "primary_disk_size": disk, "locked": False, "volumes": volume_ids,
                "public_net": {"ipv4": {"id": ipv4["id"], "ip": ipv4["ip"]} if ipv4 else None,
                               "ipv6": None, "floating_ips": [],
                               "firewalls": [{"id": f, "status": "applied"} for f in firewall_ids]}}

    def _create_server(self, body: dict) -> dict:
        name = body["name"]
        if any(s["name"] == name for s in self.servers.values()):
            raise _err("uniqueness_error", "server name is already used")
        stype = self.server_types.get(body["server_type"])
        location = body.get("location")
        if stype is None or location not in self.locations:
            raise _err("invalid_input", "invalid server_type or location")
        if "resource_unavailable" in self.fail and body["server_type"] == "cpx32":
            raise _err("resource_unavailable", "server type cpx32 unavailable (simulé)")
        avail = next((l for l in stype.get("locations", []) if l["name"] == location), None)
        if not avail or not avail.get("available", True):
            raise _err("resource_unavailable", f"{stype['name']} unavailable in {location}")
        system = None
        raw_image = body["image"]
        if isinstance(raw_image, str) and not raw_image.isdigit():
            system = SYSTEM_IMAGES.get(raw_image)
            if system is None:
                raise _err("invalid_input", f"image {raw_image!r} not found")
            image = None
        else:
            image = self.images.get(int(raw_image))
            if image is None:
                raise _err("invalid_input", "image not found")
            if image["disk_size"] > stype["disk"]:
                raise _err("invalid_input", "image disk is bigger than server type disk")
        for kid in body.get("ssh_keys") or []:
            self._find(self.ssh_keys, kid, "ssh_key")
        net = body.get("public_net") or {}
        ipv4 = None
        if net.get("enable_ipv4", True):
            if net.get("ipv4"):
                ipv4 = self._find(self.primary_ips, net["ipv4"], "primary_ip")
                if ipv4["assignee_id"]:
                    raise _err("invalid_input", "primary ip already assigned")
                if ipv4["location"]["name"] != location:
                    raise _err("invalid_input", "primary ip is in another location")
            else:
                ipv4 = self._new_primary_ip(location, auto_delete=True)
        sid = next(self._ids)
        if ipv4:
            ipv4["assignee_id"] = sid
        fw_ids = [f["firewall"] for f in body.get("firewalls") or []]
        vol_ids = list(body.get("volumes") or [])
        for vid in vol_ids:
            vol = self._find(self.volumes, vid, "volume")
            if vol["location"]["name"] != location:
                raise _err("invalid_input", "volume is in another location")
            vol["server"] = sid
        srv = self._server_json(sid, name, stype, location, body.get("labels") or {},
                                image["id"] if image else None, "initializing", _now(), stype["disk"], ipv4,
                                fw_ids, vol_ids)
        self.servers[sid] = srv
        for fid in fw_ids:
            self.firewalls[fid]["applied_to"].append({"type": "server", "server": {"id": sid}})
        start = body.get("start_after_create", True)
        if system:
            # Serveur Linux : SSH répond (clé enregistrée) mais RDP jamais tant que Windows n'est pas installé.
            self.builds[sid] = {"stage": "ubuntu" if body.get("ssh_keys") else "nokey", "log": "",
                                "boot": None, "idle": False, "next": 0, "prompt": None,
                                "image_name": None, "password": None, "hooked": None}

        def created() -> None:
            srv["status"] = "running" if start else "off"
            if start:
                self.boot_at[sid] = float("inf") if system else self.sim()
                if system:
                    self.builds[sid]["boot"] = self.sim()
        action = self._action("create_server", [(sid, "server")], on_done=created)
        return {"server": copy.deepcopy(srv), "action": self._public(action), "next_actions": [],
                "root_password": None}

    def _delete_server(self, srv: dict) -> dict:
        if "delete" in self.fail:
            raise _err("locked", "server is locked (simulé)")
        sid = srv["id"]
        srv["status"] = "deleting"

        def gone() -> None:
            self.servers.pop(sid, None)
            self.builds.pop(sid, None)
            for vol in self.volumes.values():
                if vol["server"] == sid:
                    vol["server"] = None
            for ip in list(self.primary_ips.values()):
                if ip["assignee_id"] == sid:
                    ip["assignee_id"] = None
                    if ip["auto_delete"]:
                        self.primary_ips.pop(ip["id"], None)
            for fw in self.firewalls.values():
                fw["applied_to"] = [a for a in fw["applied_to"] if a["server"]["id"] != sid]
        return {"action": self._public(self._action("delete_server", [(sid, "server")], on_done=gone))}

    def _server_shutdown(self, srv: dict, body: dict) -> dict:
        sid = srv["id"]
        if srv["status"] == "running" and "shutdown_timeout" not in self.fail:
            srv["status"] = "stopping"

            def off() -> None:
                if srv["status"] == "stopping":
                    srv["status"] = "off"
            self._at(DURATIONS["os_shutdown"], off)
        return {"action": self._public(self._action("shutdown_server", [(sid, "server")]))}

    def _server_poweroff(self, srv: dict, body: dict) -> dict:
        def off() -> None:
            srv["status"] = "off"
        return {"action": self._public(self._action("stop_server", [(srv["id"], "server")], on_done=off))}

    def _server_poweron(self, srv: dict, body: dict) -> dict:
        srv["status"] = "starting"

        def on() -> None:
            srv["status"] = "running"
            self.boot_at[srv["id"]] = self.sim()
        return {"action": self._public(self._action("start_server", [(srv["id"], "server")], on_done=on))}

    def _server_reboot(self, srv: dict, body: dict) -> dict:
        self.boot_at[srv["id"]] = self.sim()
        return {"action": self._public(self._action("reboot_server", [(srv["id"], "server")]))}

    _server_reset = _server_reboot

    def _server_change_type(self, srv: dict, body: dict) -> dict:
        if srv["status"] != "off":
            raise _err("server_not_stopped", "server must be stopped")
        if "change_type" in self.fail:
            raise _err("resource_unavailable", "server type unavailable (simulé)")
        new = self.server_types[body["server_type"]]
        if new["disk"] < srv["primary_disk_size"]:
            raise _err("invalid_input", "disk of new type too small")

        def done() -> None:
            srv["server_type"] = copy.deepcopy(new)
            if body.get("upgrade_disk"):
                srv["primary_disk_size"] = new["disk"]
        return {"action": self._public(self._action("change_server_type", [(srv["id"], "server")],
                                                    on_done=done))}

    def _server_create_image(self, srv: dict, body: dict) -> dict:
        iid = next(self._ids)
        base = self.images.get((srv.get("image") or {}).get("id"))
        size = round((base or {}).get("image_size") or 12.7, 2) + 0.35
        image = {"id": iid, "type": "snapshot", "status": "creating", "name": None,
                 "description": body.get("description") or "", "labels": body.get("labels") or {},
                 "created": _iso(_now()), "image_size": None, "disk_size": srv["primary_disk_size"],
                 "architecture": "x86", "protection": {"delete": False}, "os_flavor": "ubuntu",
                 "created_from": {"id": srv["id"], "name": srv["name"]}, "deprecated": None}
        self.images[iid] = image
        failing = "snapshot" in self.fail

        def done() -> None:
            if failing:
                self.images.pop(iid, None)
            else:
                image["status"] = "available"
                image["image_size"] = size
        action = self._action("create_image", [(srv["id"], "server"), (iid, "image")],
                              duration=60 + 12 * size, on_done=done,
                              fail_code="image_create_failed" if failing else None)
        if failing:
            self._at(60 + 12 * size, done)
        return {"image": copy.deepcopy(image), "action": self._public(action)}

    # --- images ----------------------------------------------------------------------------
    def _delete_image(self, image: dict) -> dict:
        if image["protection"]["delete"]:
            raise _err("protected", "image is protected")
        self.images.pop(image["id"], None)
        return {}

    def _image_change_protection(self, image: dict, body: dict) -> dict:
        image["protection"]["delete"] = bool(body.get("delete"))
        return {"action": self._public(self._action("change_protection", [(image["id"], "image")]))}

    # --- volumes ---------------------------------------------------------------------------
    def _create_volume(self, body: dict) -> dict:
        if any(v["name"] == body["name"] for v in self.volumes.values()):
            raise _err("uniqueness_error", "volume name is already used")
        if body.get("server") and body.get("location"):
            raise _err("invalid_input", "only one of server or location")
        srv = self._find(self.servers, body["server"], "server") if body.get("server") else None
        location = srv["location"]["name"] if srv else body["location"]
        vid = next(self._ids)
        vol = {"id": vid, "name": body["name"], "size": int(body["size"]), "server": srv["id"] if srv else None,
               "labels": body.get("labels") or {}, "created": _iso(_now()), "status": "available",
               "location": copy.deepcopy(self.locations[location]), "format": body.get("format"),
               "linux_device": f"/dev/disk/by-id/scsi-0HC_Volume_{vid}", "protection": {"delete": False}}
        self.volumes[vid] = vol
        if srv:
            srv["volumes"].append(vid)
        action = self._action("create_volume", [(vid, "volume")])
        return {"volume": copy.deepcopy(vol), "action": self._public(action), "next_actions": []}

    def _delete_volume(self, vol: dict) -> dict:
        if vol["server"]:
            raise _err("invalid_input", "volume is attached")
        self.volumes.pop(vol["id"], None)
        return {}

    def _volume_attach(self, vol: dict, body: dict) -> dict:
        srv = self._find(self.servers, body["server"], "server")
        if srv["location"]["name"] != vol["location"]["name"]:
            raise _err("invalid_input", "volume is in another location")
        vol["server"] = srv["id"]
        srv["volumes"].append(vol["id"])
        return {"action": self._public(self._action("attach_volume", [(vol["id"], "volume")]))}

    def _volume_detach(self, vol: dict, body: dict) -> dict:
        srv = self.servers.get(vol["server"])
        if srv and vol["id"] in srv["volumes"]:
            srv["volumes"].remove(vol["id"])
        vol["server"] = None
        return {"action": self._public(self._action("detach_volume", [(vol["id"], "volume")]))}

    def _volume_resize(self, vol: dict, body: dict) -> dict:
        if int(body["size"]) <= vol["size"]:
            raise _err("invalid_input", "volume can only grow")
        vol["size"] = int(body["size"])
        return {"action": self._public(self._action("resize_volume", [(vol["id"], "volume")]))}

    # --- pare-feu --------------------------------------------------------------------------
    def _create_firewall(self, body: dict) -> dict:
        fid = next(self._ids)
        fw = {"id": fid, "name": body["name"], "labels": body.get("labels") or {},
              "rules": body.get("rules") or [], "applied_to": [], "created": _iso(_now())}
        self.firewalls[fid] = fw
        return {"firewall": copy.deepcopy(fw), "actions": []}

    def _firewall_set_rules(self, fw: dict, body: dict) -> dict:
        fw["rules"] = [{**r, "destination_ips": r.get("destination_ips", [])} for r in body["rules"]]
        return {"actions": [self._public(self._action("set_firewall_rules", [(fw["id"], "firewall")]))]}

    def _delete_firewall(self, fw: dict) -> dict:
        if fw["applied_to"]:
            raise _err("resource_in_use", "firewall is still applied to resources")
        self.firewalls.pop(fw["id"], None)
        return {}

    # --- clés SSH --------------------------------------------------------------------------
    def _create_ssh_key(self, body: dict) -> dict:
        if any(k["name"] == body["name"] or k["public_key"] == body["public_key"] for k in self.ssh_keys.values()):
            raise _err("uniqueness_error", "ssh key name or public key is already used")
        kid = next(self._ids)
        key = {"id": kid, "name": body["name"], "public_key": body["public_key"], "fingerprint": f"fp:{kid}",
               "labels": body.get("labels") or {}, "created": _iso(_now())}
        self.ssh_keys[kid] = key
        return {"ssh_key": copy.deepcopy(key)}

    def _delete_ssh_key(self, key: dict) -> dict:
        self.ssh_keys.pop(key["id"], None)
        return {}

    def _firewall_apply_to_resources(self, fw: dict, body: dict) -> dict:
        actions = []
        for target in body.get("apply_to") or []:
            sid = target["server"]["id"]
            srv = self._find(self.servers, sid, "server")
            if all(a["server"]["id"] != sid for a in fw["applied_to"]):
                fw["applied_to"].append({"type": "server", "server": {"id": sid}})
                srv["public_net"]["firewalls"].append({"id": fw["id"], "status": "applied"})
            actions.append(self._public(self._action("apply_firewall", [(fw["id"], "firewall")])))
        return {"actions": actions}

    def _firewall_remove_from_resources(self, fw: dict, body: dict) -> dict:
        actions = []
        for target in body.get("remove_from") or []:
            sid = target["server"]["id"]
            srv = self._find(self.servers, sid, "server")
            fw["applied_to"] = [a for a in fw["applied_to"] if a["server"]["id"] != sid]
            srv["public_net"]["firewalls"] = [f for f in srv["public_net"]["firewalls"] if f["id"] != fw["id"]]
            actions.append(self._public(self._action("remove_firewall", [(fw["id"], "firewall")])))
        return {"actions": actions}

    # --- IP primaires ----------------------------------------------------------------------
    def _create_primary_ip(self, body: dict) -> dict:
        ip = self._new_primary_ip(body["location"], bool(body.get("auto_delete")), body.get("labels"),
                                  body.get("name"))
        return {"primary_ip": copy.deepcopy(ip), "action": None}

    def _delete_primary_ip(self, ip: dict) -> dict:
        if ip["assignee_id"]:
            raise _err("invalid_input", "primary ip is assigned")
        self.primary_ips.pop(ip["id"], None)
        return {}

    def _primary_ip_unassign(self, ip: dict, body: dict) -> dict:
        ip["assignee_id"] = None
        return {"action": self._public(self._action("unassign_primary_ip", [(ip["id"], "primary_ip")]))}

    # --- données initiales -----------------------------------------------------------------
    def _seed_account(self) -> None:
        """Réplique de l'état réel du compte au moment de la conception."""
        self.images[434955057] = {
            "id": 434955057, "type": "snapshot", "status": "available", "name": None,
            "description": "windows-8gb-nbg1-1-1790100869", "labels": {},
            "created": "2026-09-22T18:14:46Z", "image_size": 12.718, "disk_size": 160,
            "architecture": "x86", "protection": {"delete": True}, "os_flavor": "ubuntu",
            "created_from": {"id": 166978566, "name": "windows-8gb-nbg1-1"}, "deprecated": None}
        self.firewalls[11664160] = {
            "id": 11664160, "name": "RDP-WINDOWS", "labels": {}, "created": "2026-09-22T17:46:38Z",
            "rules": [{"description": "RDP Windows", "direction": "in", "port": "3389",
                       "protocol": "tcp", "destination_ips": [], "source_ips": ["176.137.192.109/32"]}],
            "applied_to": []}
        ip = self._new_primary_ip("nbg1", auto_delete=False, name="primary_ip-150987804")
        ip["ip"] = "178.105.239.115"

    def _seed_demo(self) -> None:
        now = _now()
        lab = {"rdpm": "1", "rdpm-desktop": "dev-perso"}
        for days, size, pinned in ((12, 18.4, True), (2, 21.1, False)):
            iid = next(self._ids)
            created = now - timedelta(days=days)
            self.images[iid] = {
                "id": iid, "type": "snapshot", "status": "available", "name": None,
                "description": f"Dev perso — {created.astimezone():%Y-%m-%d %H:%M}",
                "labels": {**lab, "rdpm-type": "cpx32", "rdpm-loc": "nbg1"},
                "created": _iso(created), "image_size": size, "disk_size": 160, "architecture": "x86",
                "protection": {"delete": pinned}, "os_flavor": "ubuntu",
                "created_from": {"id": 1, "name": "rdpm-dev-perso"}, "deprecated": None}
        fid = next(self._ids)
        self.firewalls[fid] = {
            "id": fid, "name": "rdpm-dev-perso-rdp", "labels": {**lab, "rdpm-role": "rdp-desktop"},
            "created": _iso(now - timedelta(days=12)), "applied_to": [],
            "rules": [{"description": "Bureau", "direction": "in", "port": "3389", "protocol": proto,
                       "destination_ips": [], "source_ips": ["198.51.100.7/32"]} for proto in ("tcp", "udp")]}
        vid = next(self._ids)
        self.volumes[vid] = {"id": vid, "name": "dev-perso-data", "size": 20, "server": None,
                             "labels": dict(lab), "created": _iso(now - timedelta(days=12)),
                             "status": "available", "location": copy.deepcopy(self.locations["nbg1"]),
                             "format": None, "protection": {"delete": False}}

        tlab = {"rdpm": "1", "rdpm-desktop": "trading"}
        snap_id = next(self._ids)
        self.images[snap_id] = {
            "id": snap_id, "type": "snapshot", "status": "available", "name": None,
            "description": f"Trading — {(now - timedelta(days=5)).astimezone():%Y-%m-%d %H:%M}",
            "labels": {**tlab, "rdpm-type": "cpx42", "rdpm-loc": "fsn1"},
            "created": _iso(now - timedelta(days=5)), "image_size": 14.9, "disk_size": 160,
            "architecture": "x86", "protection": {"delete": False}, "os_flavor": "ubuntu",
            "created_from": {"id": 2, "name": "rdpm-trading"}, "deprecated": None}
        sid = next(self._ids)
        ipv4 = self._new_primary_ip("fsn1", auto_delete=True)
        ipv4["assignee_id"] = sid
        self.servers[sid] = self._server_json(
            sid, "rdpm-trading", self.server_types["cpx42"], "fsn1",
            {**tlab, "rdpm-role": "desktop", "rdpm-image": str(snap_id)}, snap_id, "running",
            now - timedelta(hours=1, minutes=40), 160, ipv4, [11664160], [])
        self.firewalls[11664160]["applied_to"].append({"type": "server", "server": {"id": sid}})
        self.boot_at[sid] = -10_000


# --- SSH simulé du serveur de construction ------------------------------------------------------
_G, _R, _P = "\x1b[32m", "\x1b[31m", "\x1b[0m"
# (secondes simulées après le démarrage d'Alpine, lignes ajoutées à /reinstall.log)
BUILD_TIMELINE: list[tuple[int, list[str]]] = [
    (5, [f"{_G}***** PROCESS WINDOWS ISO *****{_P}"]),
    (10, ["[#a1b2c3 0.9GiB/5.6GiB(16%) CN:16 DL:60MiB ETA:1m20s]"]),
    (25, ["[#a1b2c3 2.5GiB/5.6GiB(45%) CN:16 DL:62MiB ETA:50s]"]),
    (40, ["[#a1b2c3 4.5GiB/5.6GiB(80%) CN:16 DL:61MiB ETA:18s]"]),
    (60, [f"{_G}***** IMAGES COUNT: 4 *****{_P}", *IMAGE_NAMES, ""]),
    (70, [f"{_G}***** SELECTED IMAGE INFO *****{_P}"]),
    (75, [f"{_G}***** ADD DRIVERS *****{_P}", f"{_G}***** ADD DRIVERS: GENERIC VIRTIO *****{_P}"]),
    (85, [f"{_G}***** MOUNT BOOT.WIM *****{_P}"]),
    (88, [f"{_G}***** AUTOUNATTEND.XML *****{_P}"]),
    (95, [f"{_G}***** UNMOUNT BOOT.WIM *****{_P}", f"{_G}***** BOOT.WIM SIZE *****{_P}"]),
    (100, [f"{_G}***** MOUNT INSTALL.WIM *****{_P}"]),
    (110, [f"{_G}***** UNMOUNT INSTALL.WIM *****{_P}"]),
    (120, [f"{_G}***** HOLD 2 *****{_P}"]),
]
_STEP_IMAGES, _STEP_INSTALL_WIM = 4, 10

# Installation d'un bureau Linux (secondes simulées après linux-prepare, jalons du journal).
LINUX_TIMELINE: list[tuple[int, str]] = [
    (5, "RDPM-STEP 1/7 Mise à jour du système"),
    (40, "RDPM-STEP 2/7 Bureau XFCE, son et Firefox"),
    (100, "RDPM-STEP 3/7 Langue, clavier et fuseau horaire"),
    (110, "RDPM-STEP 4/7 Compilation d'xrdp avec H.264 (quelques minutes)"),
    (230, "RDPM-STEP 5/7 Configuration du bureau distant"),
    (240, "RDPM-STEP 6/7 Vérifications"),
    (245, "RDPM-STEP 7/7 Logiciels"),
]
APP_SECONDS = 20        # durée simulée de l'installation d'un logiciel
ADMIN_BOOT_S = 30       # OpenSSH répond ce délai (simulé) après que Windows répond en RDP
LINUX_STAGES = ("linux-installing", "linux-done")
FAKE_CERT = "3A1F9C0D5E7B2468ACE013579BDF2468ACE01357"


class FakeRemote:
    """Machine à états d'un serveur de construction : ubuntu → prepared → rebooting → alpine → hold →
    hooked → windows. Reconnaît les scripts à leur étiquette `# rdpm:<tag>`."""

    simulated = True

    def __init__(self, cloud: FakeCloud) -> None:
        self.cloud = cloud
        self.calls: list[tuple[str, int]] = []
        self.public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE rdpm-build"
        self.admin_public_key = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIADMINADMINADMINADMINADMINADMINADMINADMINAD "
                                 "cloud-desktop-manager-admin")
        self.scripts: list[tuple[str, str]] = []   # (étiquette, script) de l'accès d'administration

    # --- clés et réseau (rien n'est écrit sur le disque) ------------------------------------
    def available(self) -> bool:
        return True

    def keypair(self, path: Path) -> str:
        return self.public_key

    def admin_keypair(self, path: Path) -> str:
        return self.admin_public_key

    def forget_keypair(self, path: Path) -> None:
        pass

    def url_size(self, url: str) -> int | None:
        if "build_iso_missing" in self.cloud.fail:
            return None
        return 6_014_152_704

    # --- exécution -------------------------------------------------------------------------
    def run(self, host: str, script: str, *, key=None, known_hosts=None, timeout: float = 60.0,
            shell: str = "sh", user: str = "root") -> RemoteResult:
        tag = tag_of(script)
        with self.cloud._lock:
            self.cloud._tick()
            srv = next((s for s in self.cloud.servers.values()
                        if (s["public_net"]["ipv4"] or {}).get("ip") == host), None)
            if srv is None or srv["status"] != "running":
                return RemoteResult(255, "", f"ssh: connect to host {host}: No route to host")
            if key is not None and Path(key).name == ADMIN_KEY_PATH.name:
                return self._admin_run(srv, tag, script, shell, user, host)
            b = self.cloud.builds.get(srv["id"])
            if (b is None or b["stage"] in ("nokey", "rebooting", "windows") or b["boot"] is None
                    or "build_ssh_timeout" in self.cloud.fail
                    or self.cloud.sim() - b["boot"] < BUILD_BOOT_S):
                return RemoteResult(255, "", f"ssh: connect to host {host}: Connection refused")
            # Ubuntu : root ; Alpine (reinstall.sh) : la clé est posée sur l'utilisateur « administrator ».
            # Bureau Linux : root, jusqu'à la finalisation qui retire la clé temporaire.
            expected = "root" if b["stage"] in ("ubuntu", "prepared", *LINUX_STAGES) else "administrator"
            if user != expected or b["stage"] == "linux-finalized":
                return RemoteResult(255, "", f"{user}@{host}: Permission denied (publickey).")
            self.calls.append((tag, srv["id"]))
            handler = getattr(self, f"_do_{tag.replace('-', '_')}", None)
            if handler is None:
                return RemoteResult(127, "", f"script inconnu : {tag}")
            return handler(srv, b, script)

    # --- accès d'administration (clé de l'application) ---------------------------------------------
    def _admin_state(self, srv: dict) -> dict | None:
        """Bureau joignable avec la clé d'administration ? Windows en construction (post-install avec la
        clé, RDP depuis ADMIN_BOOT_S) ou bureau lancé (label rdpm-admin=1 et port 22 ouvert à ton IP)."""
        sid = srv["id"]
        b = self.cloud.builds.get(sid)
        if b is not None:
            if b["stage"] != "windows" or self.admin_public_key not in (b.get("hooked") or ""):
                return None
            if self.cloud.sim() - self.cloud.boot_at.get(sid, float("inf")) < BOOT_TO_RDP_S + ADMIN_BOOT_S:
                return None
            return self.cloud.admin.setdefault(sid, {"installed": set(), "marks": [], "unit": "unknown",
                                                     "os": "windows"})
        if srv["labels"].get(L_ADMIN) != "1" or not self._port22_open(srv):
            return None
        if self.cloud.sim() - self.cloud.boot_at.get(sid, float("inf")) < BOOT_TO_RDP_S:
            return None
        return self.cloud.admin.setdefault(sid, {"installed": apps_catalog.decode(srv["labels"].get(L_APPS)),
                                                 "marks": [], "unit": "unknown",
                                                 "os": srv["labels"].get("rdpm-os", "windows")})

    def _port22_open(self, srv: dict) -> bool:
        me = self.cloud.public_ip_value
        for ref in srv["public_net"]["firewalls"]:
            fw = self.cloud.firewalls.get(ref["id"])
            for rule in (fw or {}).get("rules", []):
                if rule.get("port") == "22" and any(src.split("/")[0] == me for src in rule.get("source_ips", [])):
                    return True
        return False

    def _admin_run(self, srv: dict, tag: str, script: str, shell: str, user: str, host: str) -> RemoteResult:
        st = self._admin_state(srv)
        if st is None:
            return RemoteResult(255, "", f"ssh: connect to host {host}: Connection timed out")
        windows = st["os"] == "windows"
        if "admin_ssh_refused" in self.cloud.fail or (windows and (shell != "powershell" or user == "root")) \
                or (not windows and user != "root"):
            return RemoteResult(255, "", f"{user}@{host}: Permission denied (publickey).")
        self.calls.append((tag, srv["id"]))
        self.scripts.append((tag, script))
        name = tag.replace("win-", "").replace("-", "_")
        handler = getattr(self, f"_admin_{name}", None)
        if handler is None:
            return RemoteResult(127, "", f"script inconnu : {tag}")
        return handler(srv, st, script)

    def _admin_ping(self, srv: dict, st: dict, script: str) -> RemoteResult:
        return RemoteResult(0, "RDPM_PONG\n", "")

    def _admin_postinstall_status(self, srv: dict, st: dict, script: str) -> RemoteResult:
        return RemoteResult(0, "RDPM_POSTINSTALL_DONE\n", "")

    def _admin_apps_inventory(self, srv: dict, st: dict, script: str) -> RemoteResult:
        keys = [k for k in apps_catalog.install_order() if k in st["installed"]]
        return RemoteResult(0, "RDPM-APPS-STATUS " + " ".join(keys) + "\n", "")

    def _admin_apps_start(self, srv: dict, st: dict, script: str) -> RemoteResult:
        if st["unit"] == "active":
            return RemoteResult(0, "RDPM_BUSY\n", "")
        m = re.search(r"-Mode (\w+)(?: -Keys ([\w,]+))?", script) if st["os"] == "windows" else \
            re.search(r"rdpm-apps (install|update) ?([^\n]*)$", script, re.M)
        mode = m.group(1) if m else "install"
        raw = (m.group(2) or "") if m else ""
        keys = [k.strip("'") for k in re.split(r"[,\s]+", raw) if k.strip("'")]
        st.update(unit="active", marks=[], mode=mode, keys=keys)
        sid = srv["id"]
        if mode == "update":
            self.cloud._at(APP_SECONDS, lambda: self._admin_mark(sid, "RDPM-APPS-DONE update", done=True))
            return RemoteResult(0, "RDPM_STARTED\n", "")
        keys = apps_catalog.resolve(keys, st["os"])
        for i, key in enumerate(keys):
            self.cloud._at(APP_SECONDS * i + 1, lambda i=i, k=key, n=len(keys): self._admin_mark(
                sid, f"RDPM-APP start {i + 1}/{n} {k} {apps_catalog.APPS_BY_KEY[k].name}"))
            self.cloud._at(APP_SECONDS * (i + 1), lambda k=key: self._admin_finish(sid, k))
        self.cloud._at(APP_SECONDS * len(keys) + 2, lambda: self._admin_mark(sid, "RDPM-APPS-DONE", done=True))
        return RemoteResult(0, "RDPM_STARTED\n", "")

    def _admin_finish(self, sid: int, key: str) -> None:
        st = self.cloud.admin.get(sid)
        if st is None or st["unit"] != "active":
            return
        if "apps_unit_killed" in self.cloud.fail:
            st["unit"] = "inactive"
            return
        failing = "apps_one_fails" in self.cloud.fail and key == apps_catalog.resolve(st["keys"], st["os"])[-1]
        if failing:
            st["marks"].append(f"RDPM-APP fail {key} (code 1)")
        else:
            st["installed"].add(key)
            st["marks"].append(f"RDPM-APP ok {key}")

    def _admin_mark(self, sid: int, mark: str, done: bool = False) -> None:
        st = self.cloud.admin.get(sid)
        if st is None or st["unit"] != "active":
            return
        st["marks"].append(mark)
        if done:
            st["unit"] = "inactive"

    def _admin_apps_status(self, srv: dict, st: dict, script: str) -> RemoteResult:
        marks = "\n".join(st["marks"])
        return RemoteResult(0, f"RDPM_UNIT {st['unit']}\nRDPM_MARKS_BEGIN\n{marks}\nRDPM_TAIL_BEGIN\n"
                               f"{st['marks'][-1] if st['marks'] else ''}\n", "")

    def _admin_apps_stop(self, srv: dict, st: dict, script: str) -> RemoteResult:
        st["unit"] = "inactive"
        return RemoteResult(0, "RDPM_STOPPED\n", "")

    def _admin_apps_cleanup(self, srv: dict, st: dict, script: str) -> RemoteResult:
        return RemoteResult(0, "RDPM_CLEANED\n", "")

    def _do_ping(self, srv: dict, b: dict, script: str) -> RemoteResult:
        return RemoteResult(0, "RDPM_PONG\n", "")

    def _do_ubuntu_prepare(self, srv: dict, b: dict, script: str) -> RemoteResult:
        if b["stage"] != "ubuntu":
            return RemoteResult(1, "", "reinstall.sh: already prepared")
        if "build_prepare" in self.cloud.fail:
            return RemoteResult(1, "", "sha256sum: WARNING: 1 computed checksum did NOT match")
        for key, pattern in (("image_name", r"--image-name '([^']*)'"), ("password", r"--password '([^']*)'"),
                             ("iso", r"--iso '([^']*)'"), ("pubkey", r"printf '%s\\n' '([^']*)' > configs/ssh_keys")):
            m = re.search(pattern, script)
            b[key] = m.group(1) if m else None
        b["stage"] = "prepared"
        return RemoteResult(0, "Warning: Windows install prepared (simulé)\nRDPM_PREPARED\n", "")

    def _do_reboot(self, srv: dict, b: dict, script: str) -> RemoteResult:
        sid = srv["id"]
        if b["stage"] == "prepared":
            b["stage"], b["boot"] = "rebooting", self.cloud.sim()

            def alpine() -> None:
                if self.cloud.builds.get(sid) is b:
                    b["stage"], b["log"], b["next"] = "alpine", f"{_G}***** RDPM ALPINE *****{_P}\n", 0
                    self.cloud._at(BUILD_TIMELINE[0][0], lambda: self._step(sid, 0))
            self.cloud._at(BUILD_BOOT_S, alpine)
        elif b["stage"] == "hooked":
            b["stage"] = "windows"
            self.cloud.boot_at[sid] = float("inf") if "build_rdp_never" in self.cloud.fail \
                else self.cloud.sim() + BUILD_WINDOWS_S
        else:
            return RemoteResult(1, "", "rien à redémarrer dans cet état")
        return RemoteResult(0, "RDPM_REBOOTING\n", "")

    def _step(self, sid: int, idx: int) -> None:
        b = self.cloud.builds.get(sid)
        if b is None or b["stage"] != "alpine" or b["next"] != idx:
            return
        delay, lines = BUILD_TIMELINE[idx]
        b["log"] += "\n".join(lines) + "\n"
        b["next"] = idx + 1
        if idx == _STEP_IMAGES and "build_image_name" in self.cloud.fail and not b.get("prompt_done"):
            requested = b.get("image_name") or "?"
            b["log"] += (f"{_R}***** ERROR *****{_P}\n{_R}Invalid image name: {requested}{_P}\n"
                         "Choose a correct image name by one of follow command in ssh to continue:\n"
                         + "".join(f"  echo '{n}' >/image-name\n" for n in IMAGE_NAMES))
            b["prompt"] = list(IMAGE_NAMES)
            return
        if idx == _STEP_INSTALL_WIM and "build_install_error" in self.cloud.fail:
            b["log"] += f"{_R}***** ERROR *****{_P}\n{_R}can't find install.wim{_P}\n"
            b["idle"] = True
            return
        if idx + 1 < len(BUILD_TIMELINE):
            self.cloud._at(BUILD_TIMELINE[idx + 1][0] - delay, lambda: self._step(sid, idx + 1))
        else:
            b["stage"], b["idle"] = "hold", True

    def _do_alpine_status(self, srv: dict, b: dict, script: str) -> RemoteResult:
        if b["stage"] not in ("alpine", "hold"):
            return RemoteResult(3, "", "")
        state = "RDPM_IDLE" if b["idle"] else "RDPM_RUNNING"
        return RemoteResult(0, f"{state}\nRDPM_LOG_BEGIN\n{b['log']}", "")

    def _do_alpine_set_image_name(self, srv: dict, b: dict, script: str) -> RemoteResult:
        m = re.search(r"printf '%s\\n' '([^']*)' > /image-name", script)
        name = m.group(1) if m else ""
        if b.get("prompt") and name in b["prompt"]:
            b["image_name"], b["prompt"], b["prompt_done"] = name, None, True
            sid, nxt = srv["id"], b["next"]
            self.cloud._at(2, lambda: self._step(sid, nxt))
        return RemoteResult(0, "RDPM_IMAGE_SET\n", "")

    # --- bureau Linux --------------------------------------------------------------------------
    def _do_linux_prepare(self, srv: dict, b: dict, script: str) -> RemoteResult:
        if b["stage"] in LINUX_STAGES:   # rejoué après une coupure : l'installation continue
            return RemoteResult(0, "RDPM_STARTED\n", "")
        if b["stage"] != "ubuntu":
            return RemoteResult(1, "", "état inattendu")
        if "build_prepare" in self.cloud.fail:
            return RemoteResult(1, "", "useradd: group 'sudo' does not exist")
        user = re.search(r"^USERNAME='([^']*)'", script, re.M)
        pw = re.search(r"""printf '%s:%s\\n' "\$USERNAME" '([^']*)' \| chpasswd""", script)
        b.update(stage="linux-installing", username=user.group(1) if user else None,
                 password=pw.group(1) if pw else None, marks=[], unit="active", result="success",
                 install_script=script)
        sid = srv["id"]
        for delay, mark in LINUX_TIMELINE:
            self.cloud._at(delay, lambda m=mark: self._linux_mark(sid, m))
        keys = self._install_apps_of(script)
        t0 = LINUX_TIMELINE[-1][0]
        for i, key in enumerate(keys):
            name = apps_catalog.APPS_BY_KEY[key].name
            self.cloud._at(t0 + APP_SECONDS * i + 1,
                           lambda m=f"RDPM-APP start {i + 1}/{len(keys)} {key} {name}": self._linux_mark(sid, m))
            fails = "apps_one_fails" in self.cloud.fail and i == len(keys) - 1
            self.cloud._at(t0 + APP_SECONDS * (i + 1),
                           lambda m=f"RDPM-APP {'fail' if fails else 'ok'} {key}": self._linux_mark(sid, m))
        self.cloud._at(t0 + APP_SECONDS * len(keys) + 2, lambda: self._linux_mark(sid, "RDPM-DONE"))
        return RemoteResult(0, "RDPM_STARTED\n", "")

    @staticmethod
    def _install_apps_of(prepare_script: str) -> list[str]:
        """Logiciels demandés au script d'installation (transmis en base64 dans linux-prepare)."""
        import base64
        m = re.search(r"^echo (\S+) \| base64 -d > /root/rdpm-install\.sh$", prepare_script, re.M)
        if not m:
            return []
        install = base64.b64decode(m.group(1)).decode("utf-8")
        line = re.search(r"^rdpm-apps install (.*?) \|\| true$", install, re.M)
        return [k.strip("'") for k in (line.group(1).split() if line else [])]

    def _linux_mark(self, sid: int, mark: str) -> None:
        b = self.cloud.builds.get(sid)
        if b is None or b["stage"] != "linux-installing" or b["unit"] != "active":
            return
        if mark.startswith("RDPM-STEP 4/") and "linux_install_error" in self.cloud.fail:
            b["marks"] += [mark, "RDPM-FAILED Compilation d'xrdp avec H.264 (code 2, ligne 203)"]
            b["tail"] = "configure: error: x264 library not found (simulé)"
            b["unit"], b["result"] = "failed", "exit-code"
            return
        if mark.startswith("RDPM-STEP 3/") and "linux_unit_killed" in self.cloud.fail:
            b["marks"].append(mark)
            b["unit"], b["result"] = "inactive", "oom-kill"
            return
        b["marks"].append(mark)
        if mark == "RDPM-DONE":
            b["stage"], b["unit"] = "linux-done", "inactive"

    def _do_linux_status(self, srv: dict, b: dict, script: str) -> RemoteResult:
        if b["stage"] not in LINUX_STAGES:
            return RemoteResult(1, "", "pas d'installation en cours")
        marks = "\n".join(b["marks"])
        tail = b.get("tail") or (b["marks"][-1] if b["marks"] else "")
        return RemoteResult(0, f"RDPM_UNIT {b['unit']} {b['result']}\nRDPM_MARKS_BEGIN\n{marks}\n"
                               f"RDPM_TAIL_BEGIN\n{tail}\n", "")

    def _do_linux_finalize(self, srv: dict, b: dict, script: str) -> RemoteResult:
        if b["stage"] != "linux-done":
            return RemoteResult(1, "", "installation non terminée")
        b["stage"], b["finalize_script"] = "linux-finalized", script
        self.cloud.boot_at[srv["id"]] = float("inf") if "linux_rdp_never" in self.cloud.fail \
            else self.cloud.sim() - BOOT_TO_RDP_S
        admin = "RDPM_ADMIN 1\n" if self.admin_public_key in script else ""
        return RemoteResult(0, f"RDPM_SESSION 1\nRDPM_CERT {FAKE_CERT}\nRDPM_XRDP 0.10.6.1\n{admin}"
                               "RDPM_FINALIZED\n", "")

    def _do_alpine_hook(self, srv: dict, b: dict, script: str) -> RemoteResult:
        if b["stage"] != "hold":
            return RemoteResult(1, "", "RDPM_ERR l'installeur n'est pas en attente")
        b["stage"], b["hooked"] = "hooked", script
        return RemoteResult(0, "RDPM_HOOKED\n", "")
