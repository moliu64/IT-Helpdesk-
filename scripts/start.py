"""Cross-platform application launcher for local and hosted deployments."""
from __future__ import annotations

import argparse
import os
import runpy
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _open_browser_when_ready(base_url: str) -> None:
    """Open both surfaces only after the HTTP server accepts connections."""
    health_url = f"{base_url}/healthz"
    for _ in range(30):
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    webbrowser.open_new_tab(base_url)
    webbrowser.open_new_tab(f"{base_url}/backend")


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Helpdesk web application")
    default_host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    parser.add_argument("--host", default=os.getenv("HELPDESK_HOST", default_host))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", os.getenv("HELPDESK_PORT", "8787"))))
    parser.add_argument("--open-browser", action="store_true", help="Open user and backend pages after startup")
    args = parser.parse_args()
    os.chdir(ROOT)
    os.environ["HELPDESK_HOST"] = args.host
    os.environ["HELPDESK_PORT"] = str(args.port)
    # The BGE model is expected to be pre-cached.  Default to offline mode so
    # a normal one-click launch never hangs while trying to reach HuggingFace.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    if args.open_browser:
        browser_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        base_url = f"http://{browser_host}:{args.port}"
        threading.Thread(target=_open_browser_when_ready, args=(base_url,), daemon=True).start()
    sys.argv = [str(ROOT / "ui" / "server.py"), str(args.port), args.host]
    runpy.run_path(str(ROOT / "ui" / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
