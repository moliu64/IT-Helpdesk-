"""Cross-platform application launcher for local and hosted deployments."""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Helpdesk web application")
    default_host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    parser.add_argument("--host", default=os.getenv("HELPDESK_HOST", default_host))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", os.getenv("HELPDESK_PORT", "8787"))))
    args = parser.parse_args()
    os.chdir(ROOT)
    os.environ["HELPDESK_HOST"] = args.host
    os.environ["HELPDESK_PORT"] = str(args.port)
    sys.argv = [str(ROOT / "ui" / "server.py"), str(args.port), args.host]
    runpy.run_path(str(ROOT / "ui" / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
