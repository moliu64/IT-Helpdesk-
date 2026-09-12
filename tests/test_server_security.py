import base64
import http.client
import json
import sqlite3
import threading

from ui import server


def _request(http_server, method, path, body=None, auth=False, cookie=""):
    headers = {}
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    else:
        encoded = None
    if auth:
        token = base64.b64encode(b"admin:secret").decode()
        headers["Authorization"] = f"Basic {token}"
    if cookie:
        headers["Cookie"] = cookie
    connection = http.client.HTTPConnection(*http_server.server_address)
    connection.request(method, path, encoded, headers)
    response = connection.getresponse()
    payload = response.read()
    cookies = response.getheader("Set-Cookie", "")
    connection.close()
    return response.status, payload, cookies


def _run_server(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "helpdesk.db")
    http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    return http_server, thread


def test_healthz_does_not_depend_on_audit_database(monkeypatch, tmp_path):
    http_server, thread = _run_server(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "record_access", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    try:
        status, payload, _ = _request(http_server, "GET", "/healthz")
        assert status == 200
        assert json.loads(payload) == {"status": "ok"}
    finally:
        http_server.shutdown()
        thread.join()
        http_server.server_close()


def test_admin_routes_require_basic_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        assert _request(http_server, "GET", "/backend")[0] == 401
        assert _request(http_server, "GET", "/backend", auth=True)[0] == 200
        assert _request(http_server, "GET", "/api/rag/status")[0] == 401
        assert _request(http_server, "GET", "/api/rag/status", auth=True)[0] == 200
    finally:
        http_server.shutdown()
        thread.join()
        http_server.server_close()


def test_invalid_rag_upload_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(server, "RAG_IMPORT_PATH", tmp_path / "imports")
    http_server, thread = _run_server(monkeypatch, tmp_path / "db")
    try:
        status, payload, _ = _request(
            http_server, "POST", "/api/rag/import",
            {"name": "bad.txt", "content_base64": "not-base64"}, auth=True,
        )
        assert status == 400
        assert "Base64" in json.loads(payload)["error"]
        assert not list((tmp_path / "imports").glob("*"))
    finally:
        http_server.shutdown()
        thread.join()
        http_server.server_close()


def test_internal_post_error_is_not_reported_as_bad_request(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(server, "get_cached_store", lambda: (_ for _ in ()).throw(RuntimeError("backend failed")))
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        status, payload, _ = _request(http_server, "POST", "/api/rag/search", {"query": "VPN"}, auth=True)
        assert status == 500
        assert json.loads(payload) == {"error": "server internal error"}
    finally:
        http_server.shutdown()
        thread.join()
        http_server.server_close()
