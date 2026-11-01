import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from luminext.app import create_app
from luminext.rules import RuleError, parse_rule
from luminext.runtime import Runtime
from luminext.store import Store, now

HEADERS = {"X-LumiNext": "1"}
PASSWORD = "test-only-long-password"


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, "edge")
    with TestClient(app, headers=HEADERS) as c:
        c.post("/api/auth/setup", json={"username": "installer", "password": PASSWORD}).raise_for_status()
        c.post("/api/auth/login", json={"username": "installer", "password": PASSWORD}).raise_for_status()
        yield c


def add_simulated(client):
    client.post("/api/cameras", json={"id": "cam_a", "name": "Camera A", "simulated": True, "confirm_frames": 1, "disappear_frames": 1, "off_delay": 0}).raise_for_status()
    client.post("/api/devices", json={"id": "wall_a", "name": "Wall A", "simulated": True}).raise_for_status()
    client.put("/api/relays/switch.wall_a_1/mapping", json={"occupancy_sources": ["occupancy.cam_a"]}).raise_for_status()


def test_auth_roles_csrf_and_revocation(client):
    add_simulated(client)
    assert client.post("/api/users", json={"username": "operator", "password": PASSWORD, "role": "operator"}).status_code == 200
    user_id = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "operator")
    admin_cookie = client.cookies.get("luminext_edge_session")
    client.post("/api/auth/logout", json={})
    assert client.get("/api/state").status_code == 401
    client.post("/api/auth/login", json={"username": "operator", "password": PASSWORD})
    operator_cookie = client.cookies.get("luminext_edge_session")
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/cameras", json={"name": "Forbidden"}).status_code == 403
    assert client.get("/api/config/export").status_code == 403
    assert client.get("/api/worker/config").status_code == 401
    assert client.post("/api/relays/switch.wall_a_1/command", json={"action": "turn_on"}).status_code == 200
    assert client.post("/api/relays/switch.wall_a_1/command", json={"action": "toggle"}, headers={"Origin": "https://evil.example"}).status_code == 403
    client.post("/api/auth/login", json={"username": "installer", "password": PASSWORD})
    client.delete(f"/api/users/{user_id}").raise_for_status()
    client.cookies.set("luminext_edge_session", operator_cookie)
    assert client.get("/api/state").status_code == 401


def test_setup_and_missing_csrf_header(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        assert c.post("/api/auth/setup", json={"username": "admin", "password": PASSWORD}).status_code == 403
        assert c.post("/api/auth/setup", headers=HEADERS, json={"username": "admin", "password": PASSWORD}).status_code == 200
        assert c.post("/api/auth/setup", headers=HEADERS, json={"username": "other", "password": PASSWORD}).status_code == 409


def test_secrets_encrypted_and_not_in_snapshot_or_export(client):
    url = "rtsp://private:do-not-leak@192.168.1.10:554/stream"
    key = "A" * 43 + "="
    client.post("/api/cameras", json={"id": "real", "name": "Real", "rtsp_url": url}).raise_for_status()
    rt = client.app.state.rt
    stored = rt.store.get("camera", "real")
    assert url not in json.dumps(stored)
    assert rt.store.unseal(stored["rtsp_url"]) == url
    assert url not in client.get("/api/state").text
    assert url not in client.get("/api/config/export").text
    assert client.get("/api/worker/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/worker/config", headers={"Authorization": "Bearer " + rt.worker_token()})
    assert response.json()["cameras"][0]["rtsp_url"] == url


def test_demo_is_explicit_and_idempotently_rejected(client):
    assert not client.get("/api/state").json()["cameras"]
    assert client.post("/api/demo", json={}).json()["relays"] == 144
    state = client.get("/api/state").json()
    assert len(state["cameras"]) == 12 and len(state["devices"]) == 36
    assert all(c["simulated"] for c in state["cameras"])
    assert not state["automations"]
    assert client.post("/api/demo", json={}).status_code == 422


def test_rule_validation_unknown_entities_services_and_templates(client):
    add_simulated(client)
    valid = 'alias: Test\ntrigger:\n  - platform: state\n    entity_id: occupancy.cam_a\n    to: "on"\naction:\n  - service: switch.turn_on\n    target:\n      entity_id: switch.wall_a_1\n'
    assert client.post("/api/automations/validate", json={"yaml": valid}).status_code == 200
    for bad in [valid.replace("occupancy.cam_a", "occupancy.missing"), valid.replace("switch.turn_on", "shell_command.run"), valid+"variables: {}\n", "!!python/object/apply:os.system ['echo forbidden']", "123: invalid\n", valid.replace('to: "on"', 'to: "on"\n    for: 30')]:
        assert client.post("/api/automations", json={"yaml": bad}).status_code == 422
    client.post("/api/automations", json={"yaml": valid}).raise_for_status()
    assert client.delete("/api/cameras/cam_a").status_code == 422


def test_invalid_import_does_not_partially_write_configuration(client):
    invalid = {"format": "luminext-site", "version": 1, "cameras": [{"id": "new", "name": "New", "simulated": True}], "devices": [], "automations": [{"id": "bad", "yaml": "trigger: []\naction: []"}]}
    assert client.post("/api/config/import", json=invalid).status_code == 422
    assert client.get("/api/state").json()["cameras"] == []


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path)
    rt = Runtime(store)
    for id in ("a", "b"):
        camera = {"id": id, "name": id, "area": "Test", "simulated": True, "zones": [], "confirm_frames": 1, "disappear_frames": 1, "off_delay": 0}
        store.put("camera", camera)
        rt.camera_entities(camera)
    rt.add_entity({"id": "switch.r", "name": "Relay", "type": "switch", "device_id": "d", "simulated": True, "available": True, "state": True, "occupancy_sources": ["occupancy.a", "occupancy.b"]})
    yield rt
    store.db.close()


@pytest.mark.asyncio
async def test_override_waits_for_all_sources_and_offline_is_not_clear(runtime):
    rt = runtime
    await rt.command_switch("switch.r", "turn_off", "operator", manual=True)
    assert rt.entities["switch.r"]["override"]
    skipped = await rt.command_switch("switch.r", "turn_on", "automation")
    assert skipped["result"] == "skipped_manual_override"
    await rt.occupancy(rt.store.get("camera", "a"), [])
    assert rt.entities["switch.r"]["override"]  # b has never been observed
    await rt.set_state("occupancy.b", False, available=False)
    await rt.release_overrides()
    assert rt.entities["switch.r"]["override"]
    await rt.occupancy(rt.store.get("camera", "b"), [{"box": [.2,.2,.4,.8], "score": .99}])
    assert rt.entities["switch.r"]["override"]
    await rt.occupancy(rt.store.get("camera", "b"), [])
    assert not rt.entities["switch.r"]["override"]


@pytest.mark.asyncio
async def test_off_rule_runs_after_override_release(runtime):
    rt = runtime
    await rt.occupancy(rt.store.get("camera", "a"), [{"box": [.2,.2,.4,.8], "score": .99}])
    await rt.occupancy(rt.store.get("camera", "b"), [])
    text = 'trigger:\n  - platform: state\n    entity_id: occupancy.a\n    to: "off"\naction:\n  - service: switch.turn_off\n    target:\n      entity_id: switch.r\n'
    rt.store.put("automation", {"id": "auto", "name": "Off", "enabled": True, "rule": parse_rule(text, rt.entities), "yaml": text})
    await rt.command_switch("switch.r", "turn_on", "operator", manual=True)
    await rt.occupancy(rt.store.get("camera", "a"), [])
    await asyncio.gather(*rt.engine.running["auto"])
    assert rt.entities["switch.r"]["state"] is False
    assert not rt.entities["switch.r"]["override"]
    assert rt.store.history("auto")[0]["body"]["result"] == "completed"


@pytest.mark.asyncio
async def test_physical_changes_create_override_but_initial_sync_does_not(runtime):
    rt = runtime
    rt.add_entity({"id": "switch.real_7", "name": "Real", "type": "switch", "device_id": "real", "key": 7})
    await rt.device_state("real", 7, True)
    assert not rt.entities["switch.real_7"].get("override")
    await rt.device_state("real", 7, False)
    assert rt.entities["switch.real_7"]["override"]


@pytest.mark.asyncio
async def test_real_switch_state_waits_for_confirmation(runtime):
    rt = runtime
    rt.add_entity({"id": "switch.real_8", "name": "Real", "type": "switch", "device_id": "real", "key": 8, "available": True, "state": False})

    class FakeESPHome:
        def switch_command(self, key, value):
            assert rt.entities["switch.real_8"]["state"] is False  # No optimistic update.
            asyncio.create_task(rt.device_state("real", key, value))

    rt.drivers["real"] = FakeESPHome()
    result = await rt.command_switch("switch.real_8", "turn_on", "automation")
    assert result["result"] == "confirmed"
    assert rt.entities["switch.real_8"]["state"] is True
    assert not rt.entities["switch.real_8"].get("override")


@pytest.mark.asyncio
async def test_stale_camera_becomes_unknown_and_does_not_release_override(runtime):
    rt = runtime
    camera = rt.store.get("camera", "a")
    camera["simulated"] = False
    rt.store.put("camera", camera)
    await rt.set_state("occupancy.a", True, available=True)
    await rt.set_state("occupancy.b", False, available=True)
    await rt.command_switch("switch.r", "turn_off", "operator", manual=True)
    rt.camera_seen["a"] = now() - 20
    task = asyncio.create_task(rt.maintenance())
    await asyncio.sleep(.03)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert rt.entities["occupancy.a"]["state"] == "unknown"
    assert not rt.entities["occupancy.a"]["available"]
    assert rt.entities["switch.r"]["override"]


@pytest.mark.asyncio
async def test_zone_bottom_centre_and_debounce(runtime):
    rt = runtime
    camera = rt.store.get("camera", "a")
    camera.update(confirm_frames=2, disappear_frames=2, zones=[{"id": "zone", "name": "Zone", "points": [[.4,.5],[.8,.5],[.8,1],[.4,1]]}])
    rt.camera_entities(camera)
    detection = {"box": [.3,.2,.7,.8], "score": .9}
    await rt.occupancy(camera, [detection])
    assert rt.entities["occupancy.a"]["state"] == "unknown"
    await rt.occupancy(camera, [detection])
    assert rt.entities["occupancy.a_zone"]["state"] is True
    await rt.occupancy(camera, [])
    assert rt.entities["occupancy.a_zone"]["state"] is True
    await rt.occupancy(camera, [])
    assert rt.entities["occupancy.a_zone"]["state"] is False


@pytest.mark.asyncio
async def test_zone_mapping_override_requires_entire_camera_view_empty(runtime):
    rt = runtime
    camera = rt.store.get("camera", "a")
    camera["zones"] = [{"id": "zone", "name": "Zone", "points": [[.1,.1],[.3,.1],[.3,.9],[.1,.9]]}]
    rt.camera_entities(camera)
    rt.entities["switch.r"]["occupancy_sources"] = ["occupancy.a_zone"]
    await rt.command_switch("switch.r", "turn_off", "operator", manual=True)
    # The zone is empty, but another part of the camera still sees a person.
    await rt.occupancy(camera, [{"box": [.5,.2,.7,.8], "score": .99}])
    assert rt.entities["occupancy.a_zone"]["state"] is False
    assert rt.entities["switch.r"]["override"] is True
    await rt.occupancy(camera, [])
    assert rt.entities["switch.r"]["override"] is False


@pytest.mark.asyncio
async def test_restart_and_queued_modes(runtime):
    rt = runtime
    text = 'mode: restart\ntrigger:\n  - platform: manual\naction:\n  - delay: 0.03\n  - service: switch.turn_off\n    target:\n      entity_id: switch.r\n'
    record = {"id": "r", "enabled": True, "rule": parse_rule(text, rt.entities)}
    await rt.engine.trigger(record, {"test": 1})
    await asyncio.sleep(.005)
    await rt.engine.trigger(record, {"test": 2})
    await asyncio.gather(*rt.engine.running["r"])
    results = [e["body"]["result"] for e in rt.store.history("r")]
    assert "cancelled" in results and "completed" in results
    record["id"] = "q"
    record["rule"]["mode"] = "queued"
    await rt.engine.trigger(record, {"test": 1})
    await rt.engine.trigger(record, {"test": 2})
    await asyncio.gather(*rt.engine.running["q"])
    traces = rt.store.history("q")
    assert len(traces) == 2 and all(e["body"]["result"] == "completed" for e in traces)


def test_config_export_import_and_persistent_override(client, tmp_path):
    add_simulated(client)
    client.post("/api/relays/switch.wall_a_1/command", json={"action": "turn_on"}).raise_for_status()
    exported = client.get("/api/config/export").json()
    with TestClient(create_app(tmp_path / "other"), headers=HEADERS) as other:
        other.post("/api/auth/setup", json={"username": "installer", "password": PASSWORD})
        other.post("/api/auth/login", json={"username": "installer", "password": PASSWORD})
        other.post("/api/config/import", json=exported).raise_for_status()
        assert len(other.get("/api/state").json()["entities"]) == 5
    persisted = client.app.state.rt.store.get("entity", "switch.wall_a_1")
    assert persisted["override"] is True


def test_cloud_enrollment_ack_site_isolation_and_stale_control(tmp_path):
    with TestClient(create_app(tmp_path, "cloud"), headers=HEADERS) as c:
        c.post("/api/auth/setup", json={"username": "installer", "password": PASSWORD})
        c.post("/api/auth/login", json={"username": "installer", "password": PASSWORD})
        a = c.post("/api/cloud/sites", json={"name": "A"}).json()
        b = c.post("/api/cloud/sites", json={"name": "B"}).json()
        snapshot = {"entities": [{"id": "switch.x", "type": "switch"}], "cameras": [], "devices": [], "automations": [], "health": {}, "settings": {}}
        ha = {"Authorization": "Bearer " + a["token"]}
        hb = {"Authorization": "Bearer " + b["token"]}
        assert c.post(f"/api/cloud/sites/{a['id']}/commands", json={"kind": "relay", "data": {"entity_id": "switch.x", "action": "toggle"}}).status_code == 422
        assert c.post("/api/edge/sync", json={"snapshot": snapshot}, headers=ha).status_code == 200
        command = c.post(f"/api/cloud/sites/{a['id']}/commands", json={"kind": "relay", "data": {"entity_id": "switch.x", "action": "turn_on"}}).json()
        assert command["status"] == "pending"
        assert len(c.post("/api/edge/sync", json={"snapshot": snapshot}, headers=ha).json()["commands"]) == 1
        assert not c.post("/api/edge/sync", json={"snapshot": snapshot}, headers=hb).json()["commands"]
        assert c.post("/api/edge/ack", json={"id": command["id"], "status": "completed"}, headers=hb).status_code == 404
        assert c.post("/api/edge/ack", json={"id": command["id"], "status": "completed"}, headers=ha).status_code == 200
        assert not c.post("/api/edge/sync", json={"snapshot": snapshot}, headers=ha).json()["commands"]
        assert a["token"] not in c.get("/api/cloud/sites").text
