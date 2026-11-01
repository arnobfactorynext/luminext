import argparse
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="LumiNext controller / cloud console")
    parser.add_argument("--mode", choices=["edge", "cloud"], default=os.getenv("LUMINEXT_MODE", "edge"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=os.getenv("LUMINEXT_DATA", "data"))
    args = parser.parse_args()
    os.environ["LUMINEXT_MODE"] = args.mode
    os.environ["LUMINEXT_DATA"] = args.data
    if args.mode == "edge":
        os.environ.setdefault("LUMINEXT_AUTOSTART_DETECTION", "1")
        local_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        os.environ.setdefault("LUMINEXT_API", f"http://{local_host}:{args.port}")
    uvicorn.run("luminext.app:create_app", factory=True, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
