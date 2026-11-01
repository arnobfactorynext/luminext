"""Supervise the separate detection process for native desktop installations."""
import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


class DetectionSupervisor:
    def __init__(self, runtime):
        self.rt = runtime
        self.process = None
        self.log = None
        self.enabled = os.getenv("LUMINEXT_AUTOSTART_DETECTION", "0") == "1"

    async def run(self):
        try:
            while True:
                try:
                    await self.check()
                except Exception:
                    self.rt.detection_status = {"status": "blocked", "message": "Camera worker could not start; check the detection installation and data/detection.log"}
                await asyncio.sleep(2)
        finally:
            await self.stop()

    async def check(self):
        if not self.enabled:
            self.rt.detection_status = {"status": "external", "message": "Detection is managed as a separate service"}
            return
        if self.process and self.process.returncode is None:
            self.rt.detection_status = {"status": "running", "message": "Camera worker is running"}
            return
        needed = any(not c["simulated"] and c["enabled"] and c.get("rtsp_url") for c in self.rt.store.all("camera"))
        if not needed:
            self.rt.detection_status = {"status": "idle", "message": "Waiting for an enabled camera with an RTSP URL"}
            return
        missing = [m for m in ("openvino", "cv2", "numpy") if importlib.util.find_spec(m) is None]
        model = Path(os.getenv("LUMINEXT_MODEL", "models/person-detection-retail-0013.xml")).resolve()
        if missing or not model.is_file():
            message = "Missing detection packages: " + ", ".join(missing) if missing else "Person detection model is missing; run tools/download_model.py"
            self.rt.detection_status = {"status": "blocked", "message": message}
            return
        from .worker import ffmpeg_path
        try:
            ffmpeg = ffmpeg_path(os.getenv("LUMINEXT_FFMPEG"))
        except ValueError as error:
            self.rt.detection_status = {"status": "blocked", "message": str(error)}
            return
        if self.log:
            self.log.close()
        log_path = self.rt.store.directory / "detection.log"
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.write_text("")
        self.log = log_path.open("ab", buffering=0)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        command = [sys.executable, "-m", "luminext.worker", "--data", str(self.rt.store.directory.resolve()), "--api", os.getenv("LUMINEXT_API", "http://127.0.0.1:8000"), "--model", str(model), "--device", os.getenv("LUMINEXT_INFERENCE_DEVICE", "GPU"), "--ffmpeg", ffmpeg]
        self.process = await asyncio.create_subprocess_exec(*command, cwd=Path(__file__).resolve().parent.parent, stdout=self.log, stderr=self.log, creationflags=flags)
        self.rt.detection_status = {"status": "starting", "message": "Starting camera worker; first frames may take a few seconds"}

    async def stop(self):
        if self.process and self.process.returncode is None:
            if sys.platform == "win32":
                cleanup = await asyncio.create_subprocess_exec("taskkill", "/PID", str(self.process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                await cleanup.wait()
            else:
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.log:
            self.log.close()
            self.log = None
