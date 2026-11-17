import asyncio
import time

import httpx
import pytest

from luminext.video import latest_put, post_latest, publish_latest


@pytest.mark.asyncio
async def test_preview_resumes_after_transport_failure():
    pytest.importorskip('cv2')
    import numpy as np
    queue = asyncio.Queue(maxsize=1)
    raw = np.zeros((120, 160, 3), dtype=np.uint8).tobytes()
    telemetry = {'preview_frames': 0, 'preview_pipeline_ms': 0}
    latest_put(queue, (time.monotonic(), raw))
    class Client:
        calls = 0
        async def post(self, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                latest_put(queue, (time.monotonic(), raw))
                raise httpx.ReadError('temporary fixture failure')
            latest_put(queue, None)
            return httpx.Response(200, request=httpx.Request('POST', 'http://localhost'+url))
    client = Client()
    await asyncio.wait_for(publish_latest(queue, client, 'test', 160, 120, telemetry), 2)
    assert client.calls == 2 and telemetry['publish_errors'] == 1
    assert telemetry['preview_frames'] == 1


@pytest.mark.asyncio
async def test_publication_drops_server_failure_but_rejects_invalid_credentials():
    class Client:
        def __init__(self, status): self.status = status
        async def post(self, url, **kwargs):
            return httpx.Response(self.status, request=httpx.Request('POST', 'http://localhost'+url))
    telemetry = {}
    assert await post_latest(Client(503), '/detections', telemetry, json={}) is False
    assert telemetry['publish_errors'] == 1
    assert await post_latest(Client(200), '/detections', telemetry, json={}) is True
    with pytest.raises(httpx.HTTPStatusError):
        await post_latest(Client(401), '/detections', telemetry, json={})


@pytest.mark.asyncio
async def test_detection_publish_hiccup_keeps_same_capture_process(monkeypatch):
    pytest.importorskip('cv2')
    import numpy as np
    from luminext.video import camera_loop
    raw = np.zeros((120, 160, 3), dtype=np.uint8).tobytes()
    spawns = []
    class Reader:
        async def readexactly(self, size):
            await asyncio.sleep(.02)
            return raw
    class Process:
        stdout = Reader()
        returncode = None
        def kill(self): self.returncode = 0
        async def wait(self): return 0
    async def spawn(*args, **kwargs):
        process = Process()
        spawns.append(process)
        return process
    monkeypatch.setattr('asyncio.create_subprocess_exec', spawn)
    recovered = asyncio.Event()
    class Client:
        calls = 0
        async def post(self, url, **kwargs):
            if url.endswith('/detections'):
                self.calls += 1
                if self.calls == 1: raise httpx.ReadError('temporary fixture failure')
                recovered.set()
            return httpx.Response(200, request=httpx.Request('POST', 'http://localhost'+url))
    class Detector:
        device = 'test'
        def infer(self, frame, config): return [], 1
    config = {'id': 'test', 'width': 160, 'height': 120, 'fps': 10, 'live_fps': 10,
              'rtsp_url': 'rtsp://test.invalid/live', 'masks': [], 'threshold': .85}
    task = asyncio.create_task(camera_loop(config, Client(), Detector(), asyncio.Semaphore(1), 'ffmpeg'))
    try:
        await asyncio.wait_for(recovered.wait(), 2)
        assert len(spawns) == 1 and spawns[0].returncode is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
