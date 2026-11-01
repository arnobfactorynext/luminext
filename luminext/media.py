"""Private go2rtc adapter. Public clients only use authenticated LumiNext signaling."""
import os

import httpx


class MediaService:
    def __init__(self, store):
        self.store = store
        self.origin = os.getenv("LUMINEXT_MEDIA_API", "").rstrip("/")
        self.rtsp = os.getenv("LUMINEXT_MEDIA_RTSP", "rtsp://streaming:8554").rstrip("/")
        self.registered = {}

    async def ensure(self, camera):
        if not self.origin:
            return None
        url = self.store.unseal(camera.get("rtsp_url", ""))
        if not url:
            return None
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                if self.registered.get(camera["id"]) == url:
                    check = await client.get(self.origin + "/api/streams", params={"src": camera["id"]})
                    if check.status_code == 200:
                        return self.rtsp + "/" + camera["id"]
                response = await client.put(self.origin + "/api/streams", params={"name": camera["id"], "src": url})
                response.raise_for_status()
                self.registered[camera["id"]] = url
                return self.rtsp + "/" + camera["id"]
        except Exception:
            # Responses can echo credentials: return a fixed diagnostic only.
            raise ValueError("Private streaming service is unavailable") from None

    async def answer(self, camera, sdp):
        if not self.origin:
            raise ValueError("WebRTC service is not configured; use the live JPEG fallback")
        await self.ensure(camera)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(self.origin + "/api/webrtc", params={"src": camera["id"]}, content=sdp, headers={"Content-Type": "application/sdp"})
                response.raise_for_status()
                if not response.text.startswith("v=0"):
                    raise ValueError()
                return response.text
        except Exception:
            raise ValueError("WebRTC negotiation failed; the stream or browser codec may be unsupported") from None
