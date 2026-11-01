import asyncio
import logging
import secrets
from collections import defaultdict

import httpx
import psutil

from .rules import RuleEngine
from .store import now, uid

log = logging.getLogger("luminext")


def inside(point, polygon):
    x, y = point
    hit = False
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[i - 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
    return hit


class Runtime:
    def __init__(self, store, mode="edge"):
        self.store, self.mode = store, mode
        self.entities = {e["id"]: e for e in store.all("entity")}
        self.clients = set()
        self.frames = {}
        self.samples = defaultdict(lambda: {"present": 0, "absent": 0, "empty_since": None})
        self.camera_seen = {}
        self.metrics = {}
        self.drivers = {}
        self.device_tasks = {}
        self.locks = defaultdict(asyncio.Lock)
        self.expected = {}
        self.tasks = []
        self.started = now()
        self.detection_status = {"status": "not_started", "message": "Camera worker has not started"}
        self.engine = RuleEngine(self)
        self.cloud_status = {"connected": False, "message": "Not connected"}
        if not store.get("settings", "site"):
            store.put("settings", {"id": "site", "name": "LumiNext Factory", "timezone": "Asia/Dhaka", "history_days": 30})
        if not store.get("secret", "worker"):
            store.put("secret", {"id": "worker", "token": store.seal(secrets.token_urlsafe(40))})
        # Stale persisted occupancy and relay states are never treated as freshly observed.
        for entity in self.entities.values():
            if not entity.get("simulated"):
                entity.update(available=False, state="unknown", observed_at=None)

    def settings(self):
        return self.store.get("settings", "site")

    def worker_token(self):
        return self.store.unseal(self.store.get("secret", "worker")["token"])

    async def start(self):
        self.tasks = [asyncio.create_task(self.maintenance())]
        if self.mode == "edge":
            from .supervisor import DetectionSupervisor
            self.supervisor = DetectionSupervisor(self)
            self.tasks.append(asyncio.create_task(self.supervisor.run()))
            self.tasks.append(asyncio.create_task(self.cloud_loop()))
            for device in self.store.all("device"):
                self.start_device(device)

    async def stop(self):
        tasks = self.tasks + list(self.device_tasks.values()) + [t for ts in self.engine.running.values() for t in ts]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast(self, message):
        for queue in list(self.clients):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    async def set_state(self, id, state, emit_rule=True, **attributes):
        entity = self.entities[id]
        old = entity.get("state")
        old_available = entity.get("available")
        old_count = entity.get("count")
        entity.update(attributes)
        entity["state"] = state
        entity["observed_at"] = now()
        if old != state:
            entity["last_changed"] = now()
            self.store.event("state", id, {"from": old, "to": state, "source": attributes.get("source", "device")})
        if old != state or old_available != entity.get("available"):
            self.store.put("entity", entity)
        if old != state or old_available != entity.get("available") or old_count != entity.get("count"):
            await self.broadcast({"type": "entity", "entity": entity})
        if old != state and emit_rule:
            await self.engine.on_state(id, old, state)

    def add_entity(self, entity):
        existing = self.entities.get(entity["id"], {})
        self.entities[entity["id"]] = {"state": "unknown", "available": False, "last_changed": now(), **existing, **entity}
        self.store.put("entity", self.entities[entity["id"]])

    def camera_entities(self, camera):
        self.add_entity({"id": f"occupancy.{camera['id']}", "name": camera["name"], "type": "occupancy_sensor", "camera_id": camera["id"], "area": camera["area"], "simulated": camera["simulated"]})
        active = {f"occupancy.{camera['id']}"}
        for zone in camera.get("zones", []):
            id = f"occupancy.{camera['id']}_{zone['id']}"
            active.add(id)
            self.add_entity({"id": id, "name": f"{camera['name']} · {zone['name']}", "type": "occupancy_sensor", "camera_id": camera["id"], "zone_id": zone["id"], "area": camera["area"], "simulated": camera["simulated"]})
        for id, e in list(self.entities.items()):
            if e.get("camera_id") == camera["id"] and id not in active:
                self.store.delete("entity", id)
                del self.entities[id]

    async def occupancy(self, camera, detections, metrics=None):
        self.camera_seen[camera["id"]] = now()
        self.metrics[camera["id"]] = {**(metrics or {}), "received_at": now()}
        boxes = [d for d in detections if d.get("score", 0) >= camera.get("threshold", .5)]
        changes = []
        for entity in list(self.entities.values()):
            if entity.get("camera_id") != camera["id"]:
                continue
            zone = next((z for z in camera.get("zones", []) if z["id"] == entity.get("zone_id")), None)
            selected = boxes if not zone else [d for d in boxes if inside(((d["box"][0] + d["box"][2]) / 2, d["box"][3]), zone["points"])]
            present = bool(selected)
            sample = self.samples[entity["id"]]
            sample["present"] = sample["present"] + 1 if present else 0
            sample["absent"] = 0 if present else sample["absent"] + 1
            sample["empty_since"] = None if present else (sample["empty_since"] or now())
            state = entity["state"]
            if present and sample["present"] >= camera.get("confirm_frames", 2):
                state = True
            elif not present and sample["absent"] >= camera.get("disappear_frames", 10) and now() - sample["empty_since"] >= camera.get("off_delay", 10):
                state = False
            old = entity["state"]
            await self.set_state(entity["id"], state, emit_rule=False, available=True, count=len(selected), source="simulation" if camera["simulated"] else "detection")
            if old != state:
                changes.append((entity["id"], old, state))
        await self.release_overrides()
        for id, old, new in changes:
            await self.engine.on_state(id, old, new)

    async def release_overrides(self):
        for entity in list(self.entities.values()):
            if not entity.get("override"):
                continue
            sources = entity.get("occupancy_sources", [])
            required_sources = set(sources)
            for source in sources:
                mapped = self.entities.get(source, {})
                if mapped.get("camera_id"):
                    required_sources.add(f"occupancy.{mapped['camera_id']}")
            # Empty source lists, offline cameras and stale/unknown states cannot clear an override.
            if sources and all(s in self.entities and self.entities[s].get("available") and self.entities[s]["state"] is False for s in required_sources):
                entity["override"] = False
                entity.pop("override_since", None)
                self.store.put("entity", entity)
                self.store.event("override_released", entity["id"], {"reason": "all_mapped_occupancy_clear"})
                await self.broadcast({"type": "entity", "entity": entity})

    async def command_switch(self, id, action, actor, manual=False):
        if action not in {"turn_on", "turn_off", "toggle"}:
            raise ValueError("Unsupported relay action")
        entity = self.entities.get(id)
        if not entity or entity["type"] != "switch":
            raise ValueError("Unknown relay")
        async with self.locks[id]:
            if entity.get("override") and not manual:
                return {"result": "skipped_manual_override"}
            if not entity.get("available"):
                raise ValueError("Device is offline; command was not sent")
            if action == "toggle" and not isinstance(entity.get("state"), bool):
                raise ValueError("Relay state is unknown")
            target = not entity["state"] if action == "toggle" else action == "turn_on"
            if manual:
                entity.update(override=True, override_since=now())
                self.store.put("entity", entity)
            self.store.event("relay_command", id, {"target": target, "manual": manual}, actor)
            if entity.get("simulated"):
                await self.set_state(id, target, source="manual" if manual else "automation")
            else:
                client = self.drivers.get(entity["device_id"])
                if not client:
                    raise ValueError("ESPHome is disconnected")
                # Wait for an actual state subscription update. Never paint an optimistic success.
                event = asyncio.Event()
                self.expected[id] = {"state": target, "event": event, "expires": now() + 5}
                try:
                    client.switch_command(entity["key"], target)
                    await asyncio.wait_for(event.wait(), 5)
                except asyncio.TimeoutError as e:
                    raise ValueError("No confirmed state received from ESPHome within 5 seconds") from e
                finally:
                    self.expected.pop(id, None)
            await self.broadcast({"type": "entity", "entity": entity})
            return {"result": "confirmed", "state": target}

    async def device_state(self, device_id, key, state):
        id = f"switch.{device_id}_{key}"
        if id not in self.entities:
            return
        entity = self.entities[id]
        expected = self.expected.get(id)
        initial = not entity.get("available") or entity.get("state") == "unknown"
        if expected and state == expected["state"]:
            expected["event"].set()
        elif not initial and entity["state"] != state:
            entity.update(override=True, override_since=now())
            self.store.event("physical_override", id, {"state": state})
        await self.set_state(id, state, available=True, source="esphome")

    def start_device(self, device):
        if device.get("simulated") or self.mode != "edge":
            return
        old = self.device_tasks.get(device["id"])
        if old:
            old.cancel()
        self.device_tasks[device["id"]] = asyncio.create_task(self.device_loop(device))

    async def device_loop(self, device):
        from aioesphomeapi import APIClient, SwitchInfo, SwitchState

        while True:
            client = None
            pending = set()
            disconnected = asyncio.Event()
            try:
                client = APIClient(device["host"], device.get("port", 6053), password=None, noise_psk=self.store.unseal(device.get("api_key", "")) or None)
                async def on_stop(expected):
                    disconnected.set()
                await client.connect(on_stop=on_stop, login=True, log_errors=False)
                infos, _ = await client.list_entities_services()
                allowed = set(device.get("relay_keys", []))
                for info in infos:
                    if not isinstance(info, SwitchInfo):
                        continue
                    # Factory reset is exposed as a switch in this firmware; never register it as a load.
                    if info.entity_category or "reset" in (info.name + info.object_id).lower():
                        continue
                    if allowed and info.key not in allowed:
                        continue
                    self.add_entity({"id": f"switch.{device['id']}_{info.key}", "name": f"{device['name']} · {info.name}", "type": "switch", "device_id": device["id"], "key": info.key, "area": device["area"], "simulated": False})
                self.drivers[device["id"]] = client

                def receive(state):
                    if isinstance(state, SwitchState):
                        task = asyncio.create_task(self.device_state(device["id"], state.key, state.state))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                client.subscribe_states(receive)
                self.store.event("device_online", device["id"], {})
                await self.broadcast({"type": "refresh"})
                await disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log connection exceptions: they can contain addresses or credentials.
                log.warning("ESPHome connection failed for device %s; retrying", device["id"])
            finally:
                if self.drivers.get(device["id"]) is client:
                    self.drivers.pop(device["id"], None)
                for entity in list(self.entities.values()):
                    if entity.get("device_id") == device["id"]:
                        await self.set_state(entity["id"], "unknown", available=False)
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(5)

    async def maintenance(self):
        last_backup = 0
        while True:
            try:
                if self.mode == "edge":
                    for camera in self.store.all("camera"):
                        if camera["simulated"]:
                            continue
                        if now() - self.camera_seen.get(camera["id"], 0) > max(10, 3 / camera.get("fps", 5)):
                            for e in list(self.entities.values()):
                                if e.get("camera_id") == camera["id"] and e.get("available"):
                                    self.samples.pop(e["id"], None)
                                    await self.set_state(e["id"], "unknown", available=False, count=None)
                    await self.engine.tick()
                if now() - last_backup > 86400:
                    self.store.backup()
                    self.store.prune(self.settings().get("history_days", 30))
                    last_backup = now()
            except Exception:
                log.exception("Maintenance failed")
            await asyncio.sleep(.5)

    def snapshot(self):
        cameras = [{k: v for k, v in c.items() if k != "rtsp_url"} | {"has_stream": bool(c.get("rtsp_url")), "metrics": self.metrics.get(c["id"], {})} for c in self.store.all("camera")]
        for camera in cameras:
            entity = self.entities.get(f"occupancy.{camera['id']}", {})
            if camera["simulated"]:
                message = "Simulated camera"
            elif not camera["enabled"]:
                message = "Camera is disabled"
            elif not camera["has_stream"]:
                message = "RTSP URL has not been configured"
            elif entity.get("available"):
                message = "Receiving live frames and detection results"
            elif self.detection_status["status"] in {"blocked", "idle", "not_started"}:
                message = self.detection_status["message"]
            else:
                message = "No fresh detection frames yet. Check the worker status, RTSP credentials and network reachability."
            camera["connection_message"] = message
        devices = [{k: v for k, v in d.items() if k != "api_key"} | {"has_key": bool(d.get("api_key"))} for d in self.store.all("device")]
        return {"mode": self.mode, "settings": self.settings(), "cameras": cameras, "devices": devices, "entities": list(self.entities.values()), "automations": self.store.all("automation"), "events": self.store.history(limit=80), "health": {"cpu_percent": psutil.cpu_percent(), "memory_percent": psutil.virtual_memory().percent, "memory_used_gb": round(psutil.virtual_memory().used / 1024**3, 1), "uptime_seconds": int(now() - self.started), "detection_connected": any(now() - t < 10 for t in self.camera_seen.values()), "detection_service": self.detection_status, "cloud": self.cloud_status, "inference_device": next((m.get("device") for m in self.metrics.values() if m.get("device")), "Not connected"), "version": "0.1.1"}}

    async def cloud_loop(self):
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                config = self.store.get("secret", "cloud")
                if config:
                    try:
                        headers = {"Authorization": f"Bearer {self.store.unseal(config['token'])}"}
                        # Credentials stay on the edge. The cloud sees the sanitized snapshot only.
                        r = await client.post(config["url"] + "/api/edge/sync", headers=headers, json={"snapshot": self.snapshot()})
                        r.raise_for_status()
                        self.cloud_status = {"connected": True, "message": "Connected", "last_sync": now()}
                        for command in r.json().get("commands", []):
                            completed = self.store.get("command_result", command["id"])
                            if not completed:
                                self.store.put("command_result", {"id": command["id"], "status": "failed", "error": "Execution interrupted; not retried automatically"})
                                try:
                                    result = await self.remote_command(command)
                                    completed = {"id": command["id"], "status": "completed", "result": result}
                                except Exception as e:
                                    completed = {"id": command["id"], "status": "failed", "error": str(e)}
                                self.store.put("command_result", completed)
                            ack = await client.post(config["url"] + "/api/edge/ack", headers=headers, json=completed)
                            ack.raise_for_status()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.cloud_status = {"connected": False, "message": "Cloud unavailable; local control remains active"}
                await asyncio.sleep(2)

    async def remote_command(self, command):
        if now() > command["expires"]:
            raise ValueError("Command expired")
        kind, data = command["kind"], command["data"]
        if kind == "relay":
            return await self.command_switch(data["entity_id"], data["action"], "cloud:" + command["actor"], manual=True)
        if kind == "automation":
            from .rules import parse_rule
            rule = parse_rule(data["yaml"], self.entities)
            id = data.get("id") or uid("auto")
            await self.engine.cancel(id)
            self.store.put("automation", {"id": id, "name": rule.get("alias", "Untitled automation"), "yaml": data["yaml"], "rule": rule, "enabled": bool(data.get("enabled", True)), "updated_at": now()})
            self.store.event("automation_saved", id, {}, "cloud:" + command["actor"])
            await self.broadcast({"type": "refresh"})
            return {"id": id}
        raise ValueError("Unsupported cloud command")
