"""Bounded, independent live-preview and inference consumers of one RTSP decode."""
import asyncio
import logging
import sys
import time
from pathlib import Path
import httpx
from .tracking import PersonContinuity

log = logging.getLogger("luminext.detection")


def latest_put(queue, value):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(value)


async def post_latest(client, url, telemetry, **kwargs):
    """Drop a failed publication, preserving decode and tracking across API hiccups."""
    try:
        response = await client.post(url, **kwargs)
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as error:
        if error.response.status_code < 500 and error.response.status_code not in {408, 429}:
            raise
    except httpx.TransportError:
        pass
    telemetry['publish_errors'] = telemetry.get('publish_errors', 0)+1
    failures = telemetry['publish_errors']
    if failures & (failures-1) == 0:
        log.warning('Controller publication interrupted (%s dropped updates); preserving camera capture', failures)
    await asyncio.sleep(.2)
    return False


async def publish_latest(queue, client, id, width, height, telemetry, fps=10):
    import cv2
    import numpy as np
    previous = 0.
    while True:
        item = await queue.get()
        if item is None:
            return
        captured, raw = item
        current = time.monotonic()
        if current-previous < 1/fps:
            continue
        previous = current
        frame = np.frombuffer(raw,dtype=np.uint8).reshape(height,width,3)
        ok,jpeg = await asyncio.to_thread(cv2.imencode,".jpg",frame,[cv2.IMWRITE_JPEG_QUALITY,75])
        if ok:
            if await post_latest(client,f"/api/worker/cameras/{id}/frame",telemetry,content=jpeg.tobytes(),headers={"Content-Type":"image/jpeg"}):
                telemetry["preview_frames"] += 1
                telemetry["preview_pipeline_ms"] = round((time.monotonic()-captured)*1000,1)


async def camera_loop(config, client, detector, semaphore, ffmpeg):
    import cv2
    import numpy as np
    id,width,height = config["id"],config["width"],config["height"]
    live_fps = config.get("live_fps",10)
    hardware = sys.platform.startswith("linux") and Path("/dev/dri/renderD128").exists()
    while True:
        process = reader = publisher = None
        stage = "RTSP startup"
        try:
            command = [ffmpeg,"-hide_banner","-loglevel","error","-nostdin","-rtsp_transport","tcp","-timeout","10000000","-fflags","nobuffer","-flags","low_delay","-analyzeduration","1000000","-probesize","1000000"]
            if hardware:
                command += ["-hwaccel","vaapi","-hwaccel_device","/dev/dri/renderD128","-hwaccel_output_format","vaapi"]
            command += ["-i",config["rtsp_url"],"-an","-sn","-dn"]
            scale = f"scale={width}:{height},fps={max(config['fps'],live_fps)}"
            if hardware:
                scale = "hwdownload,format=nv12,"+scale
            command += ["-vf",scale,"-pix_fmt","bgr24","-f","rawvideo","pipe:1"]
            process = await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=width*height*3*2)
            inference_frames,preview_frames = asyncio.Queue(maxsize=1),asyncio.Queue(maxsize=1)
            telemetry = {"preview_frames":0,"preview_pipeline_ms":0}

            async def capture():
                try:
                    while True:
                        raw = await asyncio.wait_for(process.stdout.readexactly(width*height*3),15)
                        item = (time.monotonic(),raw)
                        latest_put(inference_frames,item)
                        latest_put(preview_frames,item)
                finally:
                    latest_put(inference_frames,None)
                    latest_put(preview_frames,None)

            reader = asyncio.create_task(capture())
            publisher = asyncio.create_task(publish_latest(preview_frames,client,id,width,height,telemetry,live_fps))
            previous = None
            last_scan = 0.
            continuity = PersonContinuity(confirmations=config.get('confirm_frames', 2))
            started,count = time.monotonic(),0
            detections,inference_ms = [],0.
            mask = np.full((height,width),255,dtype=np.uint8)
            for polygon in config.get("masks",[]):
                cv2.fillPoly(mask,[np.array([[int(x*width),int(y*height)] for x,y in polygon["points"]],dtype=np.int32)],0)
            while True:
                stage = "live preview" if publisher.done() else "RTSP capture"
                if publisher.done():
                    await publisher
                    raise RuntimeError("Preview consumer ended")
                item = await asyncio.wait_for(inference_frames.get(),20)
                if item is None:
                    raise RuntimeError("Capture ended")
                captured,raw = item
                current = time.monotonic()
                if current-last_scan < 1/config["fps"]:
                    continue
                last_scan = current
                frame = np.frombuffer(raw,dtype=np.uint8).reshape(height,width,3)
                gray = cv2.GaussianBlur(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),(15,15),0)
                motion = previous is None or cv2.countNonZero(cv2.bitwise_and(cv2.threshold(cv2.absdiff(gray,previous),20,255,cv2.THRESH_BINARY)[1],mask)) > width*height*.001
                previous = gray
                # Every published result must represent a new inference, including
                # stationary people. Replaying one cached miss must not advance
                # the controller's confirmation/disappearance frame counters.
                stage = "inference"
                async with semaphore:
                    while not inference_frames.empty():
                        item = inference_frames.get_nowait()
                        if item is None:
                            raise RuntimeError("Capture ended")
                        captured,raw = item
                        frame = np.frombuffer(raw,dtype=np.uint8).reshape(height,width,3)
                    inference = asyncio.create_task(asyncio.to_thread(detector.infer,frame,config))
                    try:
                        detections,inference_ms = await asyncio.shield(inference)
                    except asyncio.CancelledError:
                        await inference
                        raise
                detections = continuity.update(detections, config["threshold"])
                count += 1
                stage = "publishing detection"
                elapsed = max(.1,time.monotonic()-started)
                await post_latest(client,f"/api/worker/cameras/{id}/detections",telemetry,json={"detections":detections,"metrics":{"fps":round(count/elapsed,2),"inference_count":count,"publish_errors":telemetry.get('publish_errors',0),"detection_pipeline_ms":round((time.monotonic()-captured)*1000,1),"preview_fps":round(telemetry["preview_frames"]/elapsed,2),"preview_pipeline_ms":telemetry["preview_pipeline_ms"],"inference_ms":round(inference_ms,1),"device":detector.device,"model":getattr(detector,"model_name","unknown"),"decode":"VAAPI" if hardware else "software","motion":bool(motion)}})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("Camera %s failed at %s (%s); retrying",id,stage,type(error).__name__)
            if hardware:
                hardware = False
                log.warning("Camera %s using software decode fallback",id)
        finally:
            tasks = [task for task in (reader,publisher) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if process and process.returncode is None:
                process.kill()
                await process.wait()
        await asyncio.sleep(3)
