import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .rules import RuleError, parse_rule
from .media import MediaService
from .runtime import Runtime
from .store import Store, check_password, now, password_hash, uid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
WEB = Path(__file__).resolve().parent.parent / "web"
SAFE_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=256)


class UserInput(Credentials):
    role: str = "operator"

class Zone(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    name: str = Field(min_length=1, max_length=80)
    points: list[tuple[float, float]] = Field(min_length=3, max_length=50)

    @field_validator("points")
    @classmethod
    def coordinates(cls, points):
        if any(not 0 <= x <= 1 or not 0 <= y <= 1 for x, y in points):
            raise ValueError("Polygon coordinates must be relative values between 0 and 1")
        return points


class CameraInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    area: str = Field(default="Production", min_length=1, max_length=80)
    rtsp_url: str | None = Field(default=None, max_length=3000)
    simulated: bool = False
    enabled: bool = True
    fps: float = Field(default=5, ge=1, le=15)
    width: int = Field(default=640, ge=160, le=1920, multiple_of=2)
    height: int = Field(default=360, ge=120, le=1080, multiple_of=2)
    threshold: float = Field(default=.5, ge=.05, le=1)
    confirm_frames: int = Field(default=2, ge=1, le=100)
    disappear_frames: int = Field(default=10, ge=1, le=500)
    off_delay: float = Field(default=10, ge=0, le=3600)
    min_area: float = Field(default=.001, ge=0, le=1)
    max_area: float = Field(default=1, ge=0, le=1)
    min_ratio: float = Field(default=.1, ge=0, le=10)
    max_ratio: float = Field(default=4, ge=.1, le=20)
    zones: list[Zone] = Field(default_factory=list, max_length=30)
    masks: list[Zone] = Field(default_factory=list, max_length=30)


class DeviceInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    area: str = Field(default="Production", min_length=1, max_length=80)
    host: str = Field(default="", max_length=253, pattern=r"^[a-zA-Z0-9.:-]*$")
    port: int = Field(default=6053, ge=1, le=65535)
    api_key: str | None = Field(default=None, max_length=100)
    simulated: bool = False
    relay_count: int = Field(default=4, ge=1, le=16)


class RuleInput(BaseModel):
    id: str | None = None
    yaml: str = Field(max_length=100_000)
    enabled: bool = True


class RelayInput(BaseModel):
    action: str = Field(pattern=r"^(turn_on|turn_off|toggle)$")


class MappingInput(BaseModel):
    occupancy_sources: list[str] = Field(max_length=100)


class Detection(BaseModel):
    box: tuple[float, float, float, float]
    score: float = Field(ge=0, le=1)

    @field_validator("box")
    @classmethod
    def box_coordinates(cls, box):
        if any(not 0 <= x <= 1 for x in box) or box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("Invalid normalized detection box")
        return box


class DetectionInput(BaseModel):
    detections: list[Detection] = Field(default_factory=list, max_length=100)
    metrics: dict = Field(default_factory=dict)


def create_app(directory=None, mode=None):
    store = Store(directory or os.getenv("LUMINEXT_DATA", "data"))
    mode = mode or os.getenv("LUMINEXT_MODE", "edge")
    if mode not in {"edge", "cloud"}:
        raise ValueError("LUMINEXT_MODE must be edge or cloud")
    rt = Runtime(store, mode)
    media = MediaService(store)
    cookie_name = os.getenv("LUMINEXT_COOKIE_NAME", f"luminext_{mode}_session")
    attempts = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        await rt.start()
        yield
        await rt.stop()
        store.db.close()

    app = FastAPI(title="LumiNext", version="0.1.1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.rt = rt

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", 0)) > 4_000_000:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/"):
            # All browser writes require the non-simple header, blocking cross-origin form requests.
            if not request.url.path.startswith(("/api/worker/", "/api/edge/")):
                if request.headers.get("x-luminext") != "1":
                    return JSONResponse({"detail": "Missing request header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'"})
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def user_from_token(token):
        if not token:
            return None
        row = store.db.execute("SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=? AND s.expires>?", (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()
        return dict(row) if row else None

    async def user(request: Request):
        result = user_from_token(request.cookies.get(cookie_name))
        if not result:
            raise HTTPException(401, "Sign in required")
        return result

    async def admin(u=Depends(user)):
        if u["role"] != "admin":
            raise HTTPException(403, "Administrator permission required")
        return u

    def edge_only():
        if mode != "edge":
            raise HTTPException(400, "Use the local controller for this operation")

    def ensure_id(id):
        if not SAFE_ID.fullmatch(id):
            raise ValueError("Invalid ID")
        return id

    async def worker(request: Request):
        edge_only()
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(given, rt.worker_token()):
            raise HTTPException(401, "Invalid worker credential")

    async def edge(request: Request):
        if mode != "cloud":
            raise HTTPException(404)
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        digest = hashlib.sha256(given.encode()).hexdigest()
        site = next((s for s in store.all("site") if secrets.compare_digest(s["token_hash"], digest)), None)
        if not site:
            raise HTTPException(401, "Invalid site credential")
        return site

    @app.get("/api/auth/status")
    async def auth_status():
        return {"setup_required": store.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0, "mode": mode}

    @app.post("/api/auth/setup")
    async def setup(body: Credentials):
        if store.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(409, "Setup already completed")
        id = uid("user")
        store.db.execute("INSERT INTO users VALUES(?,?,?,?)", (id, body.username, password_hash(body.password), "admin"))
        store.db.commit()
        store.event("account_created", id, {"username": body.username}, body.username)
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login(body: Credentials, request: Request):
        address = request.client.host if request.client else "unknown"
        queue = attempts[address]
        while queue and queue[0] < now() - 300:
            queue.popleft()
        if len(queue) >= 10:
            raise HTTPException(429, "Too many attempts; try again in five minutes")
        queue.append(now())
        row = store.db.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
        if not row or not check_password(body.password, row["password"]):
            raise HTTPException(401, "Invalid username or password")
        queue.clear()
        token = secrets.token_urlsafe(40)
        store.db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["id"], now() + 43200))
        store.db.commit()
        response = JSONResponse({"username": row["username"], "role": row["role"]})
        response.set_cookie(cookie_name, token, httponly=True, samesite="strict", secure=os.getenv("LUMINEXT_SECURE_COOKIES", "0") == "1", max_age=43200)
        return response

    @app.get("/api/auth/me")
    async def me(u=Depends(user)):
        return u

    @app.post("/api/auth/logout")
    async def logout(request: Request, u=Depends(user)):
        token = request.cookies.get(cookie_name, "")
        store.db.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))
        store.db.commit()
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name)
        return response

    @app.get("/api/state")
    async def state(u=Depends(user)):
        return rt.snapshot()

    @app.websocket("/api/ws")
    async def websocket(ws: WebSocket):
        origin = ws.headers.get("origin")
        token = ws.cookies.get(cookie_name)
        if not user_from_token(token) or (origin and urlsplit(origin).netloc != ws.headers.get("host")):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = asyncio.Queue(maxsize=100)
        rt.clients.add(queue)
        try:
            await ws.send_json({"type": "refresh"})
            while user_from_token(token):
                try:
                    message = await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    message = {"type": "ping", "time": now()}
                await ws.send_json(message)
            await ws.close(code=1008)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rt.clients.discard(queue)

    async def save_camera(body, actor):
        id = ensure_id(body.id or uid("cam"))
        previous = store.get("camera", id, {})
        data = body.model_dump()
        if len({z["id"] for z in data["zones"]}) != len(data["zones"]):
            raise ValueError("Zone IDs must be unique")
        if body.rtsp_url and urlsplit(body.rtsp_url).scheme not in {"rtsp", "rtsps"}:
            raise ValueError("Use an rtsp:// or rtsps:// stream")
        if body.min_area > body.max_area or body.min_ratio > body.max_ratio:
            raise ValueError("Minimum filter values must not exceed maximum")
        if len(store.all("camera")) >= 32 and not previous:
            raise ValueError("This installation supports at most 32 cameras")
        data.update(id=id, rtsp_url=store.seal(body.rtsp_url) if body.rtsp_url is not None else previous.get("rtsp_url", ""))
        # Do not allow removing entities still referenced by rules or override mappings.
        new_ids = {f"occupancy.{id}"} | {f"occupancy.{id}_{z['id']}" for z in data["zones"]}
        if any(entity_id in rt.entities and rt.entities[entity_id].get("camera_id") != id for entity_id in new_ids):
            raise ValueError("Camera/zone IDs collide with an existing occupancy entity")
        removed = {e["id"] for e in rt.entities.values() if e.get("camera_id") == id} - new_ids
        assert_unreferenced(removed)
        store.put("camera", data)
        rt.camera_entities(data)
        rt.camera_seen.pop(id, None)
        rt.frames.pop(id, None)
        for e in list(rt.entities.values()):
            if e.get("camera_id") == id:
                rt.samples.pop(e["id"], None)
                await rt.set_state(e["id"], "unknown", available=False)
        store.event("camera_saved", id, {"name": body.name}, actor)
        await rt.broadcast({"type": "refresh"})
        return {"id": id}

    def assert_unreferenced(ids):
        for automation in store.all("automation"):
            if any(f'"{id}"' in json.dumps(automation["rule"]) for id in ids):
                raise ValueError(f"Remove references in automation '{automation['name']}' first")
        for e in rt.entities.values():
            if set(e.get("occupancy_sources", [])) & ids:
                raise ValueError(f"Remove the occupancy mapping on '{e['name']}' first")

    @app.post("/api/cameras")
    async def cameras_save(body: CameraInput, u=Depends(admin)):
        edge_only()
        return await save_camera(body, u["username"])

    @app.delete("/api/cameras/{id}")
    async def camera_delete(id: str, u=Depends(admin)):
        edge_only()
        ids = {e["id"] for e in rt.entities.values() if e.get("camera_id") == id}
        assert_unreferenced(ids)
        for entity in ids:
            rt.entities.pop(entity)
            store.delete("entity", entity)
        store.delete("camera", id)
        rt.frames.pop(id, None)
        store.event("camera_deleted", id, {}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"ok": True}

    @app.post("/api/devices")
    async def device_save(body: DeviceInput, u=Depends(admin)):
        edge_only()
        id = ensure_id(body.id or uid("wall"))
        previous = store.get("device", id, {})
        if previous and previous["simulated"] != body.simulated:
            raise ValueError("Add a new device to change its simulation mode")
        if not body.simulated and not body.host:
            raise ValueError("ESPHome device IP or hostname is required")
        if body.api_key:
            import base64
            try:
                if len(base64.b64decode(body.api_key, validate=True)) != 32:
                    raise ValueError()
            except Exception:
                raise ValueError("ESPHome encryption key must be base64-encoded 32 bytes")
        data = body.model_dump()
        data.update(id=id, api_key=store.seal(body.api_key) if body.api_key is not None else previous.get("api_key", ""))
        store.put("device", data)
        if body.simulated:
            for key in range(1, body.relay_count + 1):
                entity_id = f"switch.{id}_{key}"
                rt.add_entity({"id": entity_id, "type": "switch", "name": f"{body.name} · Relay {key}", "area": body.area, "device_id": id, "key": key, "simulated": True})
                await rt.set_state(entity_id, rt.entities[entity_id].get("state") is True, available=True, source="simulation")
        else:
            rt.start_device(data)
        store.event("device_saved", id, {"name": body.name}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"id": id}

    @app.delete("/api/devices/{id}")
    async def device_delete(id: str, u=Depends(admin)):
        edge_only()
        ids = {e["id"] for e in rt.entities.values() if e.get("device_id") == id}
        assert_unreferenced(ids)
        task = rt.device_tasks.pop(id, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for entity in ids:
            rt.entities.pop(entity)
            store.delete("entity", entity)
        store.delete("device", id)
        store.event("device_deleted", id, {}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"ok": True}

    @app.post("/api/relays/{id}/command")
    async def relay_command(id: str, body: RelayInput, u=Depends(user)):
        edge_only()
        return await rt.command_switch(id, body.action, u["username"], manual=True)

    @app.put("/api/relays/{id}/mapping")
    async def relay_mapping(id: str, body: MappingInput, u=Depends(admin)):
        edge_only()
        entity = rt.entities.get(id)
        if not entity or entity["type"] != "switch":
            raise HTTPException(404)
        if any(s not in rt.entities or rt.entities[s]["type"] != "occupancy_sensor" for s in body.occupancy_sources):
            raise ValueError("Select valid occupancy sources")
        entity["occupancy_sources"] = list(set(body.occupancy_sources))
        store.put("entity", entity)
        store.event("mapping_saved", id, {"sources": entity["occupancy_sources"]}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"ok": True}

    @app.post("/api/automations/validate")
    async def automation_validate(body: RuleInput, u=Depends(admin)):
        edge_only()
        return {"valid": True, "rule": parse_rule(body.yaml, rt.entities)}

    @app.post("/api/automations")
    async def automation_save(body: RuleInput, u=Depends(admin)):
        edge_only()
        rule = parse_rule(body.yaml, rt.entities)
        id = ensure_id(body.id or uid("auto"))
        await rt.engine.cancel(id)
        store.put("automation", {"id": id, "name": rule.get("alias", "Untitled automation"), "yaml": body.yaml, "rule": rule, "enabled": body.enabled, "updated_at": now()})
        store.event("automation_saved", id, {}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"id": id}

    @app.post("/api/automations/{id}/run")
    async def automation_run(id: str, u=Depends(admin)):
        edge_only()
        record = store.get("automation", id)
        if not record:
            raise HTTPException(404)
        await rt.engine.trigger(record, {"manual": u["username"]}, manual=True)
        store.event("automation_manual_run", id, {}, u["username"])
        return {"queued": True}

    @app.delete("/api/automations/{id}")
    async def automation_delete(id: str, u=Depends(admin)):
        edge_only()
        await rt.engine.cancel(id)
        store.delete("automation", id)
        store.event("automation_deleted", id, {}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"ok": True}

    @app.get("/api/history")
    async def history(entity: str | None = None, u=Depends(user)):
        return store.history(entity)

    @app.post("/api/simulation/{id}")
    async def simulate(id: str, body: DetectionInput, u=Depends(admin)):
        edge_only()
        camera = store.get("camera", id)
        if not camera or not camera["simulated"]:
            raise ValueError("This is not a simulated camera")
        if not camera["enabled"]:
            raise ValueError("Camera is disabled")
        # A simulation click represents a stable observation, including debounce/off-delay.
        for e in list(rt.entities.values()):
            if e.get("camera_id") == id:
                sample = rt.samples[e["id"]]
                sample.update(present=camera["confirm_frames"], absent=camera["disappear_frames"], empty_since=now() - camera["off_delay"] - 1)
        await rt.occupancy(camera, [d.model_dump() for d in body.detections], {"device": "SIMULATION", "fps": 0, "inference_ms": 0})
        return {"ok": True}

    @app.post("/api/demo")
    async def demo(u=Depends(admin)):
        edge_only()
        if store.all("camera") or store.all("device"):
            raise ValueError("Demo can only be loaded into an empty installation")
        names = ["Cutting floor", "Sewing line A", "Sewing line B", "Quality control", "Finishing", "Packing", "Fabric store", "Dispatch", "Main entrance", "Office", "Stairwell", "Utility room"]
        for i, name in enumerate(names):
            area = "Production" if i < 6 else "Logistics" if i < 9 else "Administration"
            camera = CameraInput(id=f"cam_{i+1:02}", name=name, area=area, simulated=True, zones=[Zone(id="main", name="Work area", points=[(.1,.1),(.9,.1),(.9,.9),(.1,.9)])])
            await save_camera(camera, u["username"])
        for i in range(36):
            area = "Production" if i < 18 else "Logistics" if i < 27 else "Administration"
            body = DeviceInput(id=f"wall_{i+1:02}", name=f"Wall switch {i+1:02}", area=area, simulated=True)
            await device_save(body, u)
            for key in range(1,5):
                entity = rt.entities[f"switch.wall_{i+1:02}_{key}"]
                entity["occupancy_sources"] = [f"occupancy.cam_{i//3+1:02}"]
                store.put("entity", entity)
        for camera in store.all("camera"):
            for e in list(rt.entities.values()):
                if e.get("camera_id") == camera["id"]:
                    await rt.set_state(e["id"], False, available=True, source="simulation", count=0)
        await rt.broadcast({"type": "refresh"})
        return {"cameras": 12, "devices": 36, "relays": 144, "simulated": True}

    @app.post("/api/settings")
    async def settings(request: Request, u=Depends(admin)):
        body = await request.json()
        name = str(body.get("name", "LumiNext Factory")).strip()[:80]
        timezone = str(body.get("timezone", "Asia/Dhaka"))
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown IANA timezone")
        days = int(body.get("history_days", 30))
        if not 1 <= days <= 365:
            raise ValueError("History retention must be 1..365 days")
        store.put("settings", {"id": "site", "name": name, "timezone": timezone, "history_days": days})
        store.event("settings_saved", "site", {}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"ok": True}

    @app.get("/api/users")
    async def users(u=Depends(admin)):
        return [dict(r) for r in store.db.execute("SELECT id,username,role FROM users")]

    @app.post("/api/users")
    async def user_create(body: UserInput, u=Depends(admin)):
        if body.role not in {"admin", "operator"}:
            raise ValueError("Invalid role")
        if store.db.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone():
            raise ValueError("Username already exists")
        id = uid("user")
        store.db.execute("INSERT INTO users VALUES(?,?,?,?)", (id, body.username, password_hash(body.password), body.role))
        store.db.commit()
        store.event("user_created", id, {"username": body.username, "role": body.role}, u["username"])
        return {"id": id}

    @app.delete("/api/users/{id}")
    async def user_delete(id: str, u=Depends(admin)):
        if u["id"] == id:
            raise ValueError("Cannot delete your own account")
        store.db.execute("DELETE FROM sessions WHERE user_id=?", (id,))
        store.db.execute("DELETE FROM users WHERE id=?", (id,))
        store.db.commit()
        store.event("user_deleted", id, {}, u["username"])
        return {"ok": True}

    @app.get("/api/config/export")
    async def export(u=Depends(admin)):
        edge_only()
        config = {"format": "luminext-site", "version": 1, "settings": rt.settings(), "cameras": rt.snapshot()["cameras"], "devices": rt.snapshot()["devices"], "automations": store.all("automation"), "mappings": {e["id"]: e.get("occupancy_sources", []) for e in rt.entities.values() if e["type"] == "switch"}}
        store.event("config_exported", "site", {"secrets_included": False}, u["username"])
        return Response(json.dumps(config, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="luminext-site.json"'})

    @app.post("/api/config/import")
    async def config_import(request: Request, u=Depends(admin)):
        edge_only()
        if store.all("camera") or store.all("device") or store.all("automation"):
            raise ValueError("Import requires an empty installation; export/back up an existing site first")
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected a site configuration object")
        if body.get("format") != "luminext-site" or body.get("version") != 1:
            raise ValueError("Unsupported site configuration")
        if not all(isinstance(body.get(k, []), list) for k in ("cameras", "devices", "automations")) or not isinstance(body.get("mappings", {}), dict):
            raise ValueError("Invalid configuration collections")
        cameras = [CameraInput.model_validate(c) for c in body.get("cameras", [])]
        devices = [DeviceInput.model_validate(d) for d in body.get("devices", [])]
        if len(cameras) > 32 or len(devices) > 256:
            raise ValueError("Site exceeds supported device limits")
        if len({c.id for c in cameras}) != len(cameras) or len({d.id for d in devices}) != len(devices):
            raise ValueError("Imported device IDs must be unique")
        for camera in cameras:
            if camera.rtsp_url or camera.min_area > camera.max_area or camera.min_ratio > camera.max_ratio:
                raise ValueError("Import camera credentials locally and use valid filter ranges")
            if len({z.id for z in camera.zones}) != len(camera.zones):
                raise ValueError("Zone IDs must be unique")
        for device in devices:
            if device.api_key or (not device.simulated and not device.host):
                raise ValueError("Real devices require a hostname; enter encryption keys locally after import")
        imported_settings = body.get("settings", {})
        if not isinstance(imported_settings, dict):
            raise ValueError("Invalid site settings")
        try:
            ZoneInfo(imported_settings.get("timezone", "Asia/Dhaka"))
        except (ZoneInfoNotFoundError, TypeError):
            raise ValueError("Unknown imported timezone")
        int(imported_settings.get("history_days", 30))
        proposed = {}
        for c in cameras:
            if not c.id:
                raise ValueError("Imported cameras must have IDs")
            ensure_id(c.id)
            if f"occupancy.{c.id}" in proposed:
                raise ValueError("Duplicate occupancy entity ID")
            proposed[f"occupancy.{c.id}"] = {"type": "occupancy_sensor"}
            for z in c.zones:
                if f"occupancy.{c.id}_{z.id}" in proposed:
                    raise ValueError("Duplicate occupancy entity ID")
                proposed[f"occupancy.{c.id}_{z.id}"] = {"type": "occupancy_sensor"}
        for d in devices:
            if not d.id:
                raise ValueError("Imported devices must have IDs")
            ensure_id(d.id)
            if d.simulated:
                for key in range(1, d.relay_count + 1):
                    proposed[f"switch.{d.id}_{key}"] = {"type": "switch"}
        # Real relay keys remain discoverable from exports/mapping keys; never invent hardware keys.
        for id, sources in body.get("mappings", {}).items():
            ensure_id(id)
            if not isinstance(sources, list) or len(sources) > 100:
                raise ValueError("Invalid occupancy source list")
            int(id.rsplit("_", 1)[1])
            if not any(id.startswith(f"switch.{d.id}_") for d in devices):
                raise ValueError("Unknown device in mapping")
            if any(s not in proposed for s in sources):
                raise ValueError("Unknown occupancy mapping")
            proposed[id] = {"type": "switch"}
        imported_rules = []
        for record in body.get("automations", []):
            validated = RuleInput.model_validate(record)
            if not validated.id:
                raise ValueError("Imported automation must have an ID")
            imported_rules.append(validated.model_dump())
        rules = [(a, parse_rule(a["yaml"], proposed)) for a in imported_rules]
        for record, _ in rules:
            ensure_id(record["id"])
        for camera in cameras:
            await save_camera(camera, u["username"])
        for device in devices:
            await device_save(device, u)
        for id, sources in body.get("mappings", {}).items():
            if id not in rt.entities:
                device_id, key = id.removeprefix("switch.").rsplit("_", 1)
                d = store.get("device", device_id)
                rt.add_entity({"id": id, "name": id, "type": "switch", "device_id": device_id, "key": int(key), "area": d["area"], "simulated": d["simulated"]})
            rt.entities[id]["occupancy_sources"] = sources
            store.put("entity", rt.entities[id])
        for record, rule in rules:
            store.put("automation", {"id": ensure_id(record["id"]), "name": rule.get("alias", "Automation"), "yaml": record["yaml"], "rule": rule, "enabled": record.get("enabled", True), "updated_at": now()})
        if body.get("settings"):
            await settings_from_import(body["settings"])
        store.event("config_imported", "site", {}, u["username"])
        await rt.broadcast({"type": "refresh"})
        return {"ok": True, "note": "Device encryption keys and camera URLs must be entered locally"}

    async def settings_from_import(data):
        timezone = data.get("timezone", "Asia/Dhaka")
        ZoneInfo(timezone)
        store.put("settings", {"id": "site", "name": str(data.get("name", "LumiNext Factory"))[:80], "timezone": timezone, "history_days": max(1, min(365, int(data.get("history_days", 30))))})

    @app.post("/api/backup")
    async def backup(u=Depends(admin)):
        path = store.backup()
        store.event("backup_created", "site", {"name": path.name}, u["username"])
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.post("/api/cloud/connect")
    async def cloud_connect(request: Request, u=Depends(admin)):
        edge_only()
        body = await request.json()
        url = str(body.get("url", "")).rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "cloud"}):
            raise ValueError("Cloud URL requires HTTPS (HTTP only for loopback or the Compose cloud service)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Enter the cloud console origin without a path or credentials")
        token = str(body.get("token", ""))
        if len(token) < 32:
            raise ValueError("Enter a site enrollment token from the cloud console")
        store.put("secret", {"id": "cloud", "url": url, "token": store.seal(token)})
        store.event("cloud_configured", "site", {}, u["username"])
        return {"ok": True}

    @app.delete("/api/cloud/connect")
    async def cloud_disconnect(u=Depends(admin)):
        store.delete("secret", "cloud")
        rt.cloud_status = {"connected": False, "message": "Disconnected"}
        return {"ok": True}

    @app.get("/api/cloud/sites")
    async def sites(u=Depends(user)):
        return [{k: v for k, v in s.items() if k not in {"token_hash", "snapshot"}} | {"online": now() - s.get("last_seen", 0) < 15} for s in store.all("site")]

    @app.post("/api/cloud/sites")
    async def site_create(request: Request, u=Depends(admin)):
        if mode != "cloud":
            raise ValueError("Run the cloud console to enroll sites")
        body = await request.json()
        token = secrets.token_urlsafe(40)
        site = {"id": uid("site"), "name": str(body.get("name", "New installation"))[:80], "location": str(body.get("location", ""))[:100], "token_hash": hashlib.sha256(token.encode()).hexdigest(), "last_seen": 0}
        store.put("site", site)
        store.event("site_enrolled", site["id"], {}, u["username"])
        return {"id": site["id"], "token": token}

    @app.get("/api/cloud/sites/{id}/state")
    async def site_state(id: str, u=Depends(user)):
        site = store.get("site", id)
        if not site:
            raise HTTPException(404)
        return {"site": {k: v for k,v in site.items() if k not in {"token_hash", "snapshot"}}, "snapshot": site.get("snapshot"), "online": now() - site.get("last_seen", 0) < 15, "commands": [c for c in store.all("cloud_command") if c["site_id"] == id][-30:]}

    @app.post("/api/cloud/sites/{id}/commands")
    async def site_command(id: str, request: Request, u=Depends(user)):
        site = store.get("site", id)
        if not site or now() - site.get("last_seen", 0) >= 15:
            raise ValueError("Site is offline; no command was queued")
        body = await request.json()
        kind = body.get("kind")
        data = body.get("data", {})
        if kind == "automation":
            if u["role"] != "admin":
                raise HTTPException(403)
            entities = {e["id"]: e for e in site["snapshot"]["entities"]}
            parsed = RuleInput.model_validate(data)
            parse_rule(parsed.yaml, entities)
            data = parsed.model_dump()
        elif kind == "relay":
            RelayInput.model_validate(data)
            if not any(e["id"] == data.get("entity_id") and e["type"] == "switch" for e in site["snapshot"]["entities"]):
                raise ValueError("Unknown site relay")
        else:
            raise ValueError("Supported cloud commands: relay and automation")
        command = {"id": uid("cmd"), "site_id": id, "kind": kind, "data": data, "actor": u["username"], "created": now(), "expires": now() + 30, "status": "pending"}
        store.put("cloud_command", command)
        store.event("remote_command_queued", id, {"command_id": command["id"], "kind": kind}, u["username"])
        return command

    @app.post("/api/edge/sync")
    async def sync(request: Request, site=Depends(edge)):
        body = await request.json()
        snapshot = body.get("snapshot")
        if not isinstance(snapshot, dict) or not all(k in snapshot for k in ("entities", "cameras", "devices", "automations", "health", "settings")):
            raise ValueError("Invalid site snapshot")
        site.update(snapshot=snapshot, last_seen=now())
        store.put("site", site)
        commands = []
        for command in store.all("cloud_command"):
            if command["site_id"] == site["id"] and command["status"] == "pending":
                if command["expires"] < now():
                    command["status"] = "expired"
                    store.put("cloud_command", command)
                else:
                    commands.append(command)
        await rt.broadcast({"type": "cloud_update", "site_id": site["id"]})
        return {"commands": commands[:20]}

    @app.post("/api/edge/ack")
    async def ack(request: Request, site=Depends(edge)):
        body = await request.json()
        command = store.get("cloud_command", body.get("id"))
        if not command or command["site_id"] != site["id"]:
            raise HTTPException(404)
        if body.get("status") not in {"completed", "failed"}:
            raise ValueError("Invalid command status")
        command.update(status=body["status"], result=body.get("result"), error=str(body.get("error", ""))[:1000], finished=now())
        store.put("cloud_command", command)
        store.event("remote_command_result", site["id"], {"command_id": command["id"], "status": command["status"]})
        await rt.broadcast({"type": "cloud_update", "site_id": site["id"]})
        return {"ok": True}

    @app.get("/api/worker/config", dependencies=[Depends(worker)])
    async def worker_config():
        cameras = [{**c, "rtsp_url": store.unseal(c.get("rtsp_url", ""))} for c in store.all("camera") if not c["simulated"] and c["enabled"] and c.get("rtsp_url")]
        if media.origin:
            for camera in cameras:
                # Pass the DB record to ensure() so its URL remains encrypted until used.
                try:
                    restream = await media.ensure(store.get("camera", camera["id"]))
                    if restream:
                        camera["rtsp_url"] = restream
                except ValueError:
                    # Preserve detection during a media-server restart, using the camera directly.
                    pass
        return {"cameras": cameras}

    @app.post("/api/cameras/{id}/webrtc")
    async def webrtc(id: str, request: Request, u=Depends(user)):
        edge_only()
        camera = store.get("camera", id)
        if not camera or camera["simulated"] or not camera["enabled"]:
            raise HTTPException(404)
        body = await request.json()
        sdp = str(body.get("sdp", ""))
        if len(sdp) > 100_000 or not sdp.startswith("v=0"):
            raise ValueError("Expected SDP offer")
        return {"sdp": await media.answer(camera, sdp)}

    @app.post("/api/worker/cameras/{id}/detections", dependencies=[Depends(worker)])
    async def detections(id: str, body: DetectionInput):
        camera = store.get("camera", id)
        if not camera or camera["simulated"] or not camera["enabled"]:
            raise HTTPException(404)
        await rt.occupancy(camera, [d.model_dump() for d in body.detections], body.metrics)
        return {"ok": True}

    @app.post("/api/worker/cameras/{id}/frame", dependencies=[Depends(worker)])
    async def frame(id: str, request: Request):
        camera = store.get("camera", id)
        if not camera or not camera["enabled"] or camera["simulated"]:
            raise HTTPException(404)
        data = await request.body()
        if len(data) > 500_000 or not data.startswith(b"\xff\xd8"):
            raise ValueError("Expected JPEG under 500 KB")
        rt.frames[id] = (now(), data)
        return {"ok": True}

    @app.get("/api/cameras/{id}/snapshot")
    async def snapshot(id: str, u=Depends(user)):
        frame = rt.frames.get(id)
        if not frame or now() - frame[0] > 10:
            raise HTTPException(404, "No live frame available")
        return Response(frame[1], media_type="image/jpeg")

    @app.get("/api/cameras/{id}/live")
    async def live(id: str, request: Request, u=Depends(user)):
        if id not in {c["id"] for c in store.all("camera")}:
            raise HTTPException(404)
        token = request.cookies.get(cookie_name)
        async def stream():
            previous = 0
            while not await request.is_disconnected() and user_from_token(token):
                frame = rt.frames.get(id)
                if frame and frame[0] > previous and now() - frame[0] < 10:
                    previous = frame[0]
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame[1] + b"\r\n"
                await asyncio.sleep(.2)
        return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "mode": mode}

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    return app
