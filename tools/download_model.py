"""Download the official Open Model Zoo FP16 person detector for an offline install."""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

BASE = "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-detection-retail-0013/FP16/"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="models")
    args = parser.parse_args()
    root = Path(args.directory)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for suffix in ("xml", "bin"):
        name = f"person-detection-retail-0013.{suffix}"
        destination = root / name
        request = urllib.request.Request(BASE + name, headers={"User-Agent": "LumiNext/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if not data or (suffix == "xml" and b"<net" not in data[:2000]):
            raise RuntimeError("Unexpected model response")
        destination.write_bytes(data)
        manifest[name] = {"source": BASE + name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        print(f"Downloaded {name}: {len(data)} bytes")
    (root / "download-manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
