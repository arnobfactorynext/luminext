"""Download the official Open Model Zoo FP16 person detector for an offline install."""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

BASE = "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-detection-retail-0013/FP16/"
GENERAL_SHA384 = {
    "xml": "7746ed6534bb59c9d16e7af4dbcbc768772288d48aca7e081c059eb1924ea956c67f23381a860a0bf0e187d66b7f1955",
    "bin": "2a149bc8c2f02965c59a998d7ca7868e4ba537c223ae492a8a177386f98997d8c15204f1c7fe847a51c660d9d4116d10",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="models")
    parser.add_argument("--model", choices=["person-detection-retail-0013", "person-detection-0202"], default="person-detection-retail-0013")
    args = parser.parse_args()
    root = Path(args.directory)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {}
    base = BASE.replace("person-detection-retail-0013", args.model)
    for suffix in ("xml", "bin"):
        name = f"{args.model}.{suffix}"
        destination = root / name
        request = urllib.request.Request(base + name, headers={"User-Agent": "LumiNext/0.2.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if not data or (suffix == "xml" and b"<net" not in data[:2000]):
            raise RuntimeError("Unexpected model response")
        if args.model == "person-detection-0202" and hashlib.sha384(data).hexdigest() != GENERAL_SHA384[suffix]:
            raise RuntimeError("Official model checksum mismatch")
        destination.write_bytes(data)
        manifest[name] = {"source": base + name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        print(f"Downloaded {name}: {len(data)} bytes")
    manifest_name = "download-manifest.json" if args.model == "person-detection-retail-0013" else f"{args.model}.download-manifest.json"
    (root / manifest_name).write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
