from unittest.mock import AsyncMock

import pytest

from luminext.runtime import Runtime
from luminext.store import Store
from luminext.supervisor import DetectionSupervisor
from luminext.worker import ffmpeg_path


@pytest.mark.asyncio
async def test_supervisor_waits_for_real_camera_and_reports_missing_model(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMINEXT_AUTOSTART_DETECTION", "1")
    monkeypatch.setenv("LUMINEXT_MODEL", str(tmp_path / "missing.xml"))
    store = Store(tmp_path)
    try:
        rt = Runtime(store)
        supervisor = DetectionSupervisor(rt)
        await supervisor.check()
        assert rt.detection_status["status"] == "idle"
        store.put("camera", {"id": "real", "simulated": False, "enabled": True, "rtsp_url": store.seal("rtsp://example.invalid/live")})
        await supervisor.check()
        assert rt.detection_status["status"] == "blocked"
        assert not supervisor.process
    finally:
        store.db.close()


@pytest.mark.asyncio
async def test_supervisor_launches_separate_worker_and_does_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMINEXT_AUTOSTART_DETECTION", "1")
    model = tmp_path / "model.xml"
    model.write_text("test-only")
    monkeypatch.setenv("LUMINEXT_MODEL", str(model))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("luminext.worker.ffmpeg_path", lambda configured: "test-ffmpeg")
    process = type("Process", (), {"returncode": None})()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    store = Store(tmp_path)
    try:
        rt = Runtime(store)
        store.put("camera", {"id": "real", "simulated": False, "enabled": True, "rtsp_url": store.seal("rtsp://example.invalid/live")})
        supervisor = DetectionSupervisor(rt)
        await supervisor.check()
        await supervisor.check()
        assert spawn.await_count == 1
        assert "luminext.worker" in spawn.call_args.args
        assert rt.detection_status["status"] == "running"
        supervisor.log.close()
    finally:
        store.db.close()


def test_explicit_missing_ffmpeg_has_clear_error(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda path: None)
    with pytest.raises(ValueError, match="FFmpeg executable"):
        ffmpeg_path("a-missing-executable")
