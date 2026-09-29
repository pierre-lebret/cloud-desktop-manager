"""FakeCloud : émulation en mémoire de l'API REST Hetzner, utilisée comme transport.

Sert au mode --fake (UI sans frais, temps accéléré) et aux tests. Les pannes injectables
(`fail`) permettent d'exercer tous les chemins d'erreur :
  snapshot, shutdown_timeout, resource_unavailable, delete, probe, change_type.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hcloud import APIException

SEED_PATH = Path(__file__).with_name("static_seed.json")

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
        self.actions: dict[int, dict] = {}
        self.boot_at: dict[int, float] = {}
        self.os_shutdown_pending: set[int] = set()
        self.public_ip_value = "198.51.100.23"
        self.requests: list[tuple[str, str, dict | None]] = []
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
                 "primary_ips": self.primary_ips, "firewalls": self.firewalls}.get(root)
        key = {"servers": "server", "images": "image", "volumes": "volume",
               "primary_ips": "primary_ip", "firewalls": "firewall"}.get(root)
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
                 "primary_ips": self.primary_ips, "firewalls": self.firewalls}[root]
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
        image = self.images.get(int(body["image"]))
        if image is None:
            raise _err("invalid_input", "image not found")
        if image["disk_size"] > stype["disk"]:
            raise _err("invalid_input", "image disk is bigger than server type disk")
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
        srv = self._server_json(sid, name, stype, location, body.get("labels") or {}, image["id"],
                                "initializing", _now(), stype["disk"], ipv4, fw_ids, vol_ids)
        self.servers[sid] = srv
        for fid in fw_ids:
            self.firewalls[fid]["applied_to"].append({"type": "server", "server": {"id": sid}})
        start = body.get("start_after_create", True)

        def created() -> None:
            srv["status"] = "running" if start else "off"
            if start:
                self.boot_at[sid] = self.sim()
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
               "location": copy.deepcopy(self.locations[location]), "format": None,
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
