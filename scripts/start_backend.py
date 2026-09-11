"""Start the Helpdesk backend monitor UI."""
from __future__ import annotations

import argparse
import os
import runpy
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Helpdesk backend monitor")
    parser.add_argument("--host", default=os.getenv("HELPDESK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HELPDESK_BACKEND_PORT", "8788")))
    parser.add_argument("--no-browser", action="store_true", help="Do not open the monitor in a browser")
    args = parser.parse_args()
    os.chdir(ROOT)
    os.environ["HELPDESK_HOST"] = args.host
    os.environ["HELPDESK_PORT"] = str(args.port)
    os.environ["HELPDESK_UI_MODE"] = "backend"
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    url = f"http://127.0.0.1:{args.port}/backend"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Backend monitor: {url}")
    # server.py serves both surfaces; /backend is the dedicated monitor entry.
    sys.argv = [str(ROOT / "ui" / "server.py"), str(args.port), args.host]
    runpy.run_path(str(ROOT / "ui" / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
