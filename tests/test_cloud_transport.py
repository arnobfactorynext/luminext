"""Exercise outbound sync and acknowledged remote control across real HTTP servers."""
import contextlib
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def until(fn, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    raise AssertionError("Timed out waiting for controller/cloud convergence")


def test_real_http_cloud_sync_remote_confirmation_and_local_independence(tmp_path):
    root = Path(__file__).resolve().parent.parent
    ports = [free_port(), free_port()]
    processes = []
    try:
        for mode, port in zip(["edge", "cloud"], ports):
            processes.append(subprocess.Popen([sys.executable, "-m", "luminext", "--mode", mode, "--port", str(port), "--data", str(tmp_path/mode)], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        with contextlib.ExitStack() as stack:
            edge, cloud = [stack.enter_context(httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-LumiNext": "1"}, timeout=5)) for port in ports]
            for c in (edge, cloud):
                until(lambda: c.get("/healthz").status_code == 200)
                credentials = {"username": "qa_installer", "password": "temporary-test-only-password"}
                c.post("/api/auth/setup", json=credentials).raise_for_status()
                c.post("/api/auth/login", json=credentials).raise_for_status()
            edge.post("/api/devices", json={"id": "wall", "name": "Test wall", "simulated": True}).raise_for_status()
            enrollment = cloud.post("/api/cloud/sites", json={"name": "Test factory"}).json()
            site_id = enrollment["id"]
            edge.post("/api/cloud/connect", json={"url": str(cloud.base_url).rstrip("/"), "token": enrollment["token"]}).raise_for_status()
            until(lambda: cloud.get("/api/cloud/sites").json()[0]["online"])
            command = cloud.post(f"/api/cloud/sites/{site_id}/commands", json={"kind": "relay", "data": {"entity_id": "switch.wall_1", "action": "turn_on"}})
            command.raise_for_status()
            command_id = command.json()["id"]
            def confirmed():
                state = cloud.get(f"/api/cloud/sites/{site_id}/state").json()
                return next((c for c in state["commands"] if c["id"] == command_id and c["status"] == "completed"), None)
            assert until(confirmed)["result"]["state"] is True
            assert next(e for e in edge.get("/api/state").json()["entities"] if e["id"] == "switch.wall_1")["state"] is True
            processes[1].terminate()
            processes[1].wait(timeout=10)
            response = edge.post("/api/relays/switch.wall_1/command", json={"action": "turn_off"})
            assert response.status_code == 200 and response.json()["state"] is False
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
