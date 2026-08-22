from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from uuid import uuid4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.classify import classify_ticket
from src.agents.priority import assess_priority
from src.agents.routing import recommend_route
from src.agents.solution_retrieval import retrieve_solutions
from src.report import build_report
from src.ticket_parser import parse_ticket

INDEX = Path(__file__).with_name("index.html")
DB_PATH = Path(__file__).with_name("helpdesk.db")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        ticket_id TEXT NOT NULL,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        report_json TEXT NOT NULL,
        UNIQUE(user_id, ticket_id)
    )""")
    conn.commit()
    return conn


def review_ticket(payload: dict) -> dict:
    user_id = str(payload.get("user_id", "")).strip()
    if not user_id:
        raise ValueError("请先填写用户标识")
    fields = payload.get("ticket") or {}
    if not fields.get("title") or not fields.get("description"):
        raise ValueError("标题和描述不能为空")
    fields = dict(fields)
    original_ticket_id = fields.get("ticket_id", "").strip()
    base_id = original_ticket_id or "T"
    # Every submission is a new isolated conversation, even when fields are edited or
    # the user reuses the same visible ticket number.
    fields["ticket_id"] = f"{base_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}"
    fields["source_ticket_id"] = original_ticket_id
    parsed = parse_ticket({"ticket": fields})
    ticket = parsed["ticket"]
    classification = classify_ticket(ticket)
    priority = assess_priority(ticket)
    routing = recommend_route(ticket, classification)
    solutions = retrieve_solutions(ticket)
    report = build_report(ticket, classification, priority, solutions, routing)
    conn = db()
    conn.execute("INSERT OR REPLACE INTO conversations(user_id,ticket_id,title,created_at,report_json) VALUES(?,?,?,?,?)",
                 (user_id, ticket["ticket_id"], ticket["title"], datetime.now().isoformat(timespec="seconds"), json.dumps(report, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return report


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        from urllib.parse import parse_qs, urlparse
        if self.path.startswith("/api/history"):
            query = parse_qs(urlparse(self.path).query)
            user_id = query.get("user_id", [""])[0].strip()
            keyword = query.get("q", [""])[0].strip()
            conn = db()
            rows = conn.execute("SELECT ticket_id,title,created_at FROM conversations WHERE user_id=? AND (title LIKE ? OR ticket_id LIKE ?) ORDER BY id DESC",
                                (user_id, f"%{keyword}%", f"%{keyword}%")).fetchall()
            conn.close()
            self._send(200, json.dumps([dict(row) for row in rows], ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/conversation"):
            query = parse_qs(urlparse(self.path).query)
            user_id, ticket_id = query.get("user_id", [""])[0], query.get("ticket_id", [""])[0]
            conn = db()
            row = conn.execute("SELECT report_json FROM conversations WHERE user_id=? AND ticket_id=?", (user_id, ticket_id)).fetchone()
            conn.close()
            if not row:
                self._send(404, b"{}", "application/json; charset=utf-8")
            else:
                self._send(200, row["report_json"].encode(), "application/json; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path != "/api/review":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size))
            result = review_ticket(payload)
            self._send(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except Exception as exc:
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
            self._send(400, body, "application/json; charset=utf-8")

    def log_message(self, format: str, *args) -> None:
        print(f"[ui] {self.address_string()} - {format % args}")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Helpdesk UI running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
