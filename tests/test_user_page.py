"""Contract for the user-facing main page.

The page is served as a static file: the server must not rewrite the HTML at
runtime, so every client feature (chat, ticket modal, online search, identity)
must be self-contained in ``ui/index.html``.
"""

from __future__ import annotations

import http.client
import re
import threading

from ui import server


def _request(http_server, path):
    connection = http.client.HTTPConnection(*http_server.server_address)
    connection.request("GET", path)
    response = connection.getresponse()
    payload = response.read()
    headers = response.headers
    connection.close()
    return response.status, payload, headers


def _run_server(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "helpdesk.db")
    http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    return http_server, thread


def _stop_server(http_server, thread):
    http_server.shutdown()
    thread.join()
    http_server.server_close()


def test_user_page_is_served_as_static_file(monkeypatch, tmp_path):
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        expected = server.INDEX.read_bytes()
        for path in ("/", "/index.html"):
            status, payload, headers = _request(http_server, path)
            assert status == 200
            assert headers.get("Content-Type", "").startswith("text/html")
            # No runtime HTML rewriting: the response is byte-identical.
            assert payload == expected, path
            assert headers.get("WWW-Authenticate") is None
    finally:
        _stop_server(http_server, thread)


def test_user_page_is_public_and_self_contained(monkeypatch, tmp_path):
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        status, payload, _ = _request(http_server, "/")
        assert status == 200
        html = payload.decode("utf-8")
        # The redesigned page must carry its own interactive features.
        for marker in (
            'id="newChat"',
            'id="sessions"',
            'id="messages"',
            'id="composer"',
            'id="input"',
            'id="onlineSearch"',
            'id="ticketModal"',
            'id="openTicket"',
            'class="message',
        ):
            assert marker in html, marker
        # It is the public chat surface, not an admin page.
        assert "管理后台登录" not in html
        assert "helpdesk_admin" not in html
        # No external assets (inline SVG namespaces such as
        # xmlns="http://www.w3.org/2000/svg" are not network requests).
        assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html)
        assert "@import" not in html
    finally:
        _stop_server(http_server, thread)