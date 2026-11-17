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


def decode_ssd(outputs, label, threshold, width, height):
    """OMZ DetectionOutput and boxes/labels SSD formats have different contracts."""
    import numpy as np
    boxes = next((x for x in outputs if x.ndim >= 2 and x.shape[-1] == 5), None)
    if boxes is not None:
        rows = boxes.reshape(-1, 5)
        labels = next((x.reshape(-1) for x in outputs if x is not boxes and x.size == len(rows)), None)
        if labels is None:
            raise ValueError('SSD boxes output requires a matching labels output')
        return [{'score': float(row[4]), 'box': [float(row[0]/width), float(row[1]/height), float(row[2]/width), float(row[3]/height)]}
                for row, kind in zip(rows, labels) if int(kind) == label and row[4] >= threshold]
    output = next((x for x in outputs if x.ndim >= 2 and x.shape[-1] == 7), None)
    if output is None:
        raise ValueError('Unsupported SSD output shape')
    detections = []
    for row in output.reshape(-1, 7):
        if row[0] < 0:
            break
        if int(row[1]) == label and row[2] >= threshold:
            detections.append({'score': float(row[2]), 'box': [float(x) for x in row[3:7]]})
    return detections


class Detector:
    def __init__(self, model, device, kind, label, layout, color, scale, mean):
        import cv2
        import numpy as np
        import openvino as ov
        self.cv2, self.np = cv2, np
        self.model_name = Path(model).stem
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
        outputs = self.request.infer({self.input: tensor})
        result = outputs[self.output]
        elapsed = (time.perf_counter() - started) * 1000
        detections = []
        from .tracking import retention_threshold
        # Keep lower-score candidates only for spatial continuation downstream.
        threshold = retention_threshold(config["threshold"])
        if self.kind == "ssd":
            detections = decode_ssd(list(outputs.values()), self.label, threshold, self.width, self.height)
        else:
            # YOLOv8/11 raw export: [1, 4 + classes, candidates], no objectness channel.
            rows = np.squeeze(result)
            if rows.shape[0] < rows.shape[1]:
                rows = rows.T
            boxes, scores = [], []
            for row in rows:
                classes = row[4:]
                if int(np.argmax(classes)) != self.label or classes[self.label] < threshold:
                    continue
                cx, cy, w, h = row[:4]
                boxes.append([float(cx-w/2), float(cy-h/2), float(w), float(h)])
                scores.append(float(classes[self.label]))
            for idx in np.array(cv2.dnn.NMSBoxes(boxes, scores, threshold, .45)).flatten():
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


from .video import camera_loop


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
