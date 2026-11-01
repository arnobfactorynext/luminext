"""Independent RTSP / inference service. No frames or recordings are written to disk."""
import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from .runtime import inside

log = logging.getLogger("luminext.detection")


def ffmpeg_path(configured=None):
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        raise ValueError("Configured FFmpeg executable was not found")
    resolved = shutil.which("ffmpeg")
    if resolved:
        return resolved
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise ValueError("FFmpeg is missing. Install FFmpeg or the Windows detection requirements.") from None


def read_token(directory):
    root = Path(directory)
    cipher = Fernet((root / "encryption.key").read_bytes())
    db = sqlite3.connect(f"file:{(root / 'luminext.db').as_posix()}?mode=ro", uri=True)
    try:
        record = db.execute("SELECT body FROM records WHERE kind='secret' AND id='worker'").fetchone()
        return cipher.decrypt(json.loads(record[0])["token"].encode()).decode()
    finally:
        db.close()


class Detector:
    def __init__(self, model, device, kind, label, layout, color, scale, mean):
        import cv2
        import numpy as np
        import openvino as ov
        self.cv2, self.np = cv2, np
        self.kind, self.label, self.layout, self.color, self.scale, self.mean = kind, label, layout, color, scale, np.array(mean)
        core = ov.Core()
        try:
            self.compiled = core.compile_model(model, device, {"PERFORMANCE_HINT": "LATENCY"})
            self.device = device
        except Exception:
            if device == "CPU":
                raise
            log.warning("Requested inference device unavailable; using CPU. Hardware performance is not verified.")
            self.compiled = core.compile_model(model, "CPU", {"PERFORMANCE_HINT": "LATENCY"})
            self.device = "CPU (fallback)"
        self.input = self.compiled.input(0)
        shape = list(self.input.shape)
        if len(shape) != 4:
            raise ValueError("Model input must be a static four-dimensional image tensor")
        self.height, self.width = (shape[2], shape[3]) if layout == "NCHW" else (shape[1], shape[2])
        self.output = self.compiled.output(0)
        self.request = self.compiled.create_infer_request()

    def infer(self, frame, config):
        cv2, np = self.cv2, self.np
        image = cv2.resize(frame, (self.width, self.height))
        if self.color == "RGB":
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = (image.astype(np.float32) - self.mean) * self.scale
        if self.layout == "NCHW":
            image = image.transpose(2, 0, 1)
        input_type = self.input.element_type.to_dtype()
        tensor = np.expand_dims(image, 0).astype(input_type)
        started = time.perf_counter()
        result = self.request.infer({self.input: tensor})[self.output]
        elapsed = (time.perf_counter() - started) * 1000
        detections = []
        if self.kind == "ssd":
            for row in result.reshape(-1, 7):
                if row[0] < 0:
                    break
                if int(row[1]) == self.label and row[2] >= config["threshold"]:
                    detections.append({"score": float(row[2]), "box": [float(x) for x in row[3:7]]})
        else:
            # YOLOv8/11 raw export: [1, 4 + classes, candidates], no objectness channel.
            rows = np.squeeze(result)
            if rows.shape[0] < rows.shape[1]:
                rows = rows.T
            boxes, scores = [], []
            for row in rows:
                classes = row[4:]
                if int(np.argmax(classes)) != self.label or classes[self.label] < config["threshold"]:
                    continue
                cx, cy, w, h = row[:4]
                boxes.append([float(cx-w/2), float(cy-h/2), float(w), float(h)])
                scores.append(float(classes[self.label]))
            for idx in np.array(cv2.dnn.NMSBoxes(boxes, scores, config["threshold"], .45)).flatten():
                x,y,w,h = boxes[int(idx)]
                detections.append({"score": scores[int(idx)], "box": [x/self.width, y/self.height, (x+w)/self.width, (y+h)/self.height]})
        filtered = []
        for d in detections:
            d["box"] = [max(0., min(1., x)) for x in d["box"]]
            x1,y1,x2,y2 = d["box"]
            w,h = x2-x1,y2-y1
            if w <= 0 or h <= 0 or not config["min_area"] <= w*h <= config["max_area"] or not config["min_ratio"] <= w/h <= config["max_ratio"]:
                continue
            if any(inside(((x1+x2)/2, y2), m["points"]) for m in config.get("masks", [])):
                continue
            filtered.append(d)
        return filtered, elapsed


async def camera_loop(config, client, detector, semaphore, ffmpeg):
    import cv2
    import numpy as np
    id, width, height = config["id"], config["width"], config["height"]
    hardware = sys.platform.startswith("linux") and Path("/dev/dri/renderD128").exists()
    while True:
        process = None
        reader = None
        try:
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp", "-timeout", "10000000"]
            if hardware:
                command += ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"]
            command += ["-i", config["rtsp_url"], "-an", "-sn", "-dn"]
            scale = f"scale={width}:{height},fps={config['fps']}"
            if hardware:
                scale = "hwdownload,format=nv12," + scale
            command += ["-vf", scale, "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
            # Suppress FFmpeg stderr because a malformed URL can echo a credential.
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=width*height*3*2)
            frames = asyncio.Queue(maxsize=1)

            async def capture_latest():
                try:
                    while True:
                        raw = await asyncio.wait_for(process.stdout.readexactly(width*height*3), 15)
                        if frames.full():
                            frames.get_nowait()
                        frames.put_nowait(raw)
                finally:
                    if frames.full():
                        frames.get_nowait()
                    frames.put_nowait(None)
            reader = asyncio.create_task(capture_latest())
            previous = None
            last_infer, last_frame, started, count = 0., 0., time.monotonic(), 0
            detections, inference_ms = [], 0.
            motion_mask = np.full((height, width), 255, dtype=np.uint8)
            for mask in config.get("masks", []):
                points = np.array([[int(x*width), int(y*height)] for x,y in mask["points"]], dtype=np.int32)
                cv2.fillPoly(motion_mask, [points], 0)
            while True:
                raw = await asyncio.wait_for(frames.get(), 20)
                if raw is None:
                    raise RuntimeError("RTSP capture ended")
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
                gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (15,15), 0)
                motion = previous is None or cv2.countNonZero(cv2.bitwise_and(cv2.threshold(cv2.absdiff(gray, previous), 20,255,cv2.THRESH_BINARY)[1], motion_mask)) > width*height*.001
                previous = gray
                current = time.monotonic()
                # Periodic refresh preserves stationary people. Gated frames reuse the last inference.
                if motion or current-last_infer >= 1:
                    async with semaphore:
                        # Refresh the frame after waiting for shared inference capacity.
                        while not frames.empty():
                            latest = frames.get_nowait()
                            if latest is None:
                                raise RuntimeError("RTSP capture ended")
                            frame = np.frombuffer(latest, dtype=np.uint8).reshape(height, width, 3)
                        inference = asyncio.create_task(asyncio.to_thread(detector.infer, frame, config))
                        try:
                            detections, inference_ms = await asyncio.shield(inference)
                        except asyncio.CancelledError:
                            # A Python thread cannot be cancelled. Keep the shared request locked
                            # until it finishes before a replacement camera task can use it.
                            await inference
                            raise
                    last_infer = current
                count += 1
                r = await client.post(f"/api/worker/cameras/{id}/detections", json={"detections": detections, "metrics": {"fps": round(count / max(.1,current-started),2), "inference_ms": round(inference_ms,1), "device": detector.device, "decode": "VAAPI" if hardware else "software", "motion": bool(motion)}})
                r.raise_for_status()
                if current - last_frame >= .5:
                    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok:
                        r = await client.post(f"/api/worker/cameras/{id}/frame", content=jpeg.tobytes(), headers={"Content-Type": "image/jpeg"})
                        r.raise_for_status()
                    last_frame = current
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Camera %s capture/inference unavailable; retrying (credentials omitted)", id)
            if hardware:
                hardware = False
                log.warning("Camera %s using software decode fallback", id)
        finally:
            if reader:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if process and process.returncode is None:
                process.kill()
                await process.wait()
        await asyncio.sleep(3)


async def run(args):
    args.ffmpeg = ffmpeg_path(args.ffmpeg)
    if not Path(args.model).is_file():
        raise SystemExit("Model file missing. Install an SSD or YOLOv8/11 OpenVINO/ONNX model and set --model. No empty occupancy will be emitted.")
    detector = Detector(args.model, args.device, args.model_type, args.person_label, args.layout, args.color, args.scale, args.mean)
    token = os.getenv("LUMINEXT_WORKER_TOKEN") or read_token(args.data)
    tasks, configs = {}, {}
    semaphore = asyncio.Semaphore(1)
    async with httpx.AsyncClient(base_url=args.api, headers={"Authorization": f"Bearer {token}"}, timeout=15) as client:
        try:
            while True:
                try:
                    r = await client.get("/api/worker/config")
                    r.raise_for_status()
                    cameras = {c["id"]: c for c in r.json()["cameras"]}
                    for id in set(tasks) | set(cameras):
                        if id not in cameras or configs.get(id) != cameras[id] or (id in tasks and tasks[id].done()):
                            task = tasks.pop(id, None)
                            if task:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                            configs.pop(id, None)
                        if id in cameras and id not in tasks:
                            configs[id] = cameras[id]
                            tasks[id] = asyncio.create_task(camera_loop(cameras[id], client, detector, semaphore, args.ffmpeg))
                except Exception:
                    log.warning("Controller configuration unavailable; retrying")
                await asyncio.sleep(5)
        finally:
            for t in tasks.values():
                t.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="LumiNext isolated detection worker")
    parser.add_argument("--api", default=os.getenv("LUMINEXT_API", "http://127.0.0.1:8000"))
    parser.add_argument("--data", default=os.getenv("LUMINEXT_DATA", "data"))
    parser.add_argument("--model", default=os.getenv("LUMINEXT_MODEL", "models/person-detection-retail-0013.xml"))
    parser.add_argument("--model-type", choices=["ssd", "yolo"], default="ssd")
    parser.add_argument("--device", default="GPU")
    parser.add_argument("--person-label", type=int, default=1)
    parser.add_argument("--layout", choices=["NCHW", "NHWC"], default="NCHW")
    parser.add_argument("--color", choices=["BGR", "RGB"], default="BGR")
    parser.add_argument("--scale", type=float, default=1.)
    parser.add_argument("--mean", nargs=3, type=float, default=[0.,0.,0.])
    parser.add_argument("--ffmpeg", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
