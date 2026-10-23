"""Security contract for the admin cookie-session login.

The admin UI must never answer with a ``WWW-Authenticate`` challenge (the
browser's native Basic-auth dialog is what the old implementation kept
triggering).  Pages redirect to /login, APIs return JSON 401, and a signed
HttpOnly cookie grants access until it expires or the password rotates.
"""

import base64
import http.client
import json
import sqlite3
import threading
import time
from urllib.parse import quote

import pytest

from ui import server


def _request(http_server, method, path, body=None, auth=False, cookie="", headers=None):
    request_headers = dict(headers or {})
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode()
        request_headers["Content-Type"] = "application/json"
    else:
        encoded = None
    if auth:
        token = base64.b64encode(b"admin:secret").decode()
        request_headers["Authorization"] = f"Basic {token}"
    if cookie:
        request_headers["Cookie"] = cookie
    connection = http.client.HTTPConnection(*http_server.server_address)
    connection.request(method, path, encoded, request_headers)
    response = connection.getresponse()
    payload = response.read()
    response_headers = response.headers
    connection.close()
    return response.status, payload, response_headers


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


def _login(http_server, username="admin", password="secret"):
    status, payload, headers = _request(
        http_server, "POST", "/api/admin/login",
        {"username": username, "password": password},
    )
    set_cookie = headers.get("Set-Cookie", "")
    cookie = set_cookie.split(";", 1)[0] if set_cookie else ""
    return status, json.loads(payload), set_cookie, cookie


def _assert_no_challenge(headers):
    assert headers.get("WWW-Authenticate") is None


@pytest.fixture(autouse=True)
def _isolate_login_throttle():
    """The throttle is process-global; keep tests independent of each other."""
    server._LOGIN_FAILURES.clear()
    yield
    server._LOGIN_FAILURES.clear()


def test_healthz_does_not_depend_on_audit_database(monkeypatch, tmp_path):
    http_server, thread = _run_server(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "record_access", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    try:
        status, payload, headers = _request(http_server, "GET", "/healthz")
        assert status == 200
        assert json.loads(payload) == {"status": "ok"}
        _assert_no_challenge(headers)
    finally:
        _stop_server(http_server, thread)


def test_login_page_is_offline_and_self_contained(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        for path in ("/login", "/login.html"):
            status, payload, headers = _request(http_server, "GET", path)
            assert status == 200
            _assert_no_challenge(headers)
            assert headers.get("Content-Type", "").startswith("text/html")
            html = payload.decode("utf-8")
            assert "管理后台登录" in html
            assert 'id="username"' in html and 'id="password"' in html
            assert "autofocus" in html
            # No external assets: the page must work on an offline LAN.
            assert "http://" not in html and "https://" not in html
    finally:
        _stop_server(http_server, thread)


def test_unauthenticated_admin_page_redirects_to_login(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        for route in ("/backend", "/backend.html", "/admin", "/admin.html"):
            status, _, headers = _request(http_server, "GET", route)
            assert status == 302
            location = headers.get("Location", "")
            assert location == f"/login?next={route}"
            _assert_no_challenge(headers)
    finally:
        _stop_server(http_server, thread)


def test_api_without_session_returns_json_401_without_challenge(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        cases = [
            ("GET", "/api/rag/status", None),
            ("GET", "/api/rag/documents", None),
            ("GET", "/api/backend/runs", None),
            ("GET", "/api/backend/trace?user_id=u&session_id=s", None),
            ("POST", "/api/rag/search", {"query": "VPN"}),
            ("DELETE", "/api/rag/files?name=x.txt", None),
        ]
        for method, path, body in cases:
            status, payload, headers = _request(http_server, method, path, body)
            assert status == 401, (method, path)
            data = json.loads(payload)
            assert data["error"] == "登录已过期，请重新登录"
            assert data["login"] == "/login?next=/backend"
            _assert_no_challenge(headers)
    finally:
        _stop_server(http_server, thread)


def test_login_grants_admin_pages_and_apis(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        status, data, set_cookie, cookie = _login(http_server)
        assert status == 200
        assert data == {"ok": True, "redirect": "/backend"}
        assert cookie.startswith("helpdesk_admin=")
        for attribute in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age="):
            assert attribute in set_cookie

        status, payload, headers = _request(http_server, "GET", "/backend", cookie=cookie)
        assert status == 200
        assert headers.get("Content-Type", "").startswith("text/html")

        status, payload, headers = _request(http_server, "GET", "/api/rag/status", cookie=cookie)
        assert status == 200
        assert "ready" in json.loads(payload)

        status, payload, headers = _request(http_server, "GET", "/api/rag/documents", cookie=cookie)
        assert status == 200
        assert isinstance(json.loads(payload), list)

        status, payload, headers = _request(http_server, "GET", "/api/backend/runs", cookie=cookie)
        assert status == 200
        assert isinstance(json.loads(payload), list)

        # Already authenticated visitors skip the form.
        status, _, headers = _request(http_server, "GET", "/login?next=/backend", cookie=cookie)
        assert status == 302
        assert headers.get("Location") == "/backend"
    finally:
        _stop_server(http_server, thread)


def test_login_rejects_wrong_password_without_challenge(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        status, payload, headers = _request(
            http_server, "POST", "/api/admin/login",
            {"username": "admin", "password": "wrong"},
        )
        assert status == 401
        assert headers.get("Set-Cookie") is None
        assert json.loads(payload)["error"]
        _assert_no_challenge(headers)
    finally:
        _stop_server(http_server, thread)


def test_tampered_admin_cookie_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        _, _, _, cookie = _login(http_server)
        name, _, token = cookie.partition("=")
        encoded, issued, signature = token.split(".")
        flipped = ("A" if signature[-1] != "A" else "B")
        tampered = f"{name}={encoded}.{issued}.{signature[:-1]}{flipped}"
        status, _, headers = _request(http_server, "GET", "/api/rag/status", cookie=tampered)
        assert status == 401
        _assert_no_challenge(headers)
        status, _, _ = _request(http_server, "GET", "/backend", cookie=tampered)
        assert status == 302

        # Also reject a token whose username was rewritten.
        other_user = base64.urlsafe_b64encode(b"root").decode().rstrip("=")
        forged = f"{name}={other_user}.{issued}.{signature}"
        assert _request(http_server, "GET", "/api/rag/status", cookie=forged)[0] == 401
    finally:
        _stop_server(http_server, thread)


def test_expired_or_future_admin_cookie_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        now = int(time.time())
        expired = server._admin_token("admin", "secret", issued=now - 31 * 86400)
        future = server._admin_token("admin", "secret", issued=now + 3600)
        for token in (expired, future):
            status, _, headers = _request(http_server, "GET", "/api/rag/status",
                                          cookie=f"helpdesk_admin={token}")
            assert status == 401
            _assert_no_challenge(headers)
    finally:
        _stop_server(http_server, thread)


def test_login_redirect_next_only_allows_same_origin_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        _, _, _, cookie = _login(http_server)
        unsafe_targets = (
            "//evil.example/backend",
            "https://evil.example/backend",
            "/\\evil.example",
            "\\\\evil.example",
            "javascript:alert(1)",
        )
        for target in unsafe_targets:
            status, _, headers = _request(
                http_server, "GET", f"/login?next={quote(target, safe='')}", cookie=cookie,
            )
            assert status == 302
            assert headers.get("Location") == "/backend", target

        status, _, headers = _request(
            http_server, "GET", f"/login?next={quote('/api/rag/status', safe='')}", cookie=cookie,
        )
        assert status == 302
        assert headers.get("Location") == "/api/rag/status"

        # Missing next falls back to the admin page.
        assert server._safe_next(None) == "/backend"
        assert server._safe_next("") == "/backend"
        assert server._safe_next("/login") == "/login"
    finally:
        _stop_server(http_server, thread)


def test_secure_cookie_flag_from_env_and_forwarded_proto(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        # Env unset: plain HTTP -> no Secure, HTTPS proxy header -> Secure.
        monkeypatch.delenv("HELPDESK_COOKIE_SECURE", raising=False)
        _, _, set_cookie, _ = _login(http_server)
        assert "; Secure" not in set_cookie
        status, payload, headers = _request(
            http_server, "POST", "/api/admin/login",
            {"username": "admin", "password": "secret"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert status == 200
        assert "; Secure" in headers.get("Set-Cookie", "")

        # Explicit HELPDESK_COOKIE_SECURE=1 always marks the cookie Secure.
        monkeypatch.setenv("HELPDESK_COOKIE_SECURE", "1")
        _, _, set_cookie, _ = _login(http_server)
        assert "; Secure" in set_cookie
    finally:
        _stop_server(http_server, thread)


def test_logout_clears_the_admin_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        # Logout is idempotent and needs no session: it only clears the cookie.
        status, payload, headers = _request(http_server, "POST", "/api/admin/logout", {})
        assert status == 200
        assert json.loads(payload) == {"ok": True}
        _, _, _, cookie = _login(http_server)
        status, payload, headers = _request(http_server, "POST", "/api/admin/logout", {})
        assert status == 200
        assert json.loads(payload) == {"ok": True}
        set_cookie = headers.get("Set-Cookie", "")
        assert set_cookie.startswith("helpdesk_admin=")
        assert "Max-Age=0" in set_cookie
        cleared = set_cookie.split(";", 1)[0]
        assert _request(http_server, "GET", "/backend", cookie=cleared)[0] == 302
    finally:
        _stop_server(http_server, thread)


def test_basic_auth_fallback_still_works_without_challenge(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        status, _, headers = _request(http_server, "GET", "/backend", auth=True)
        assert status == 200
        _assert_no_challenge(headers)
        status, payload, headers = _request(http_server, "GET", "/api/rag/status", auth=True)
        assert status == 200
        assert "ready" in json.loads(payload)
        _assert_no_challenge(headers)

        # Wrong Basic credentials still yield a plain JSON 401, not a challenge.
        bad = base64.b64encode(b"admin:wrong").decode()
        status, payload, headers = _request(
            http_server, "GET", "/api/rag/status",
            headers={"Authorization": f"Basic {bad}"},
        )
        assert status == 401
        _assert_no_challenge(headers)
    finally:
        _stop_server(http_server, thread)


def test_unconfigured_credentials_returns_503(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        status, payload, headers = _request(
            http_server, "POST", "/api/admin/login",
            {"username": "admin", "password": "secret"},
        )
        assert status == 503
        _assert_no_challenge(headers)
        assert "HELPDESK_ADMIN_USER" in json.loads(payload)["error"]
    finally:
        _stop_server(http_server, thread)


def test_failed_login_throttle_returns_429_with_retry_after(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        for _ in range(server.LOGIN_MAX_FAILURES):
            status, _, headers = _request(
                http_server, "POST", "/api/admin/login",
                {"username": "admin", "password": "wrong"},
            )
            assert status == 401
            _assert_no_challenge(headers)
        status, payload, headers = _request(
            http_server, "POST", "/api/admin/login",
            {"username": "admin", "password": "wrong"},
        )
        assert status == 429
        assert headers.get("Retry-After") == "60"
        _assert_no_challenge(headers)
        assert json.loads(payload)["error"]
    finally:
        _stop_server(http_server, thread)


def test_successful_login_resets_the_failure_counter(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        for _ in range(server.LOGIN_MAX_FAILURES - 1):
            assert _login(http_server, password="wrong")[0] == 401
        assert _login(http_server)[0] == 200
        assert _login(http_server, password="wrong")[0] == 401
    finally:
        _stop_server(http_server, thread)


def test_admin_trace_endpoint_returns_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    http_server, thread = _run_server(monkeypatch, tmp_path)
    try:
        connection = server.db()
        connection.execute(
            "INSERT INTO agent_traces(user_id,session_id,node,status,timestamp,result_count,error) VALUES(?,?,?,?,?,?,?)",
            ("u-1", "s-1", "parse", "completed", "2026-01-01T00:00:00", 0, None),
        )
        connection.execute(
            "INSERT INTO agent_traces(user_id,session_id,node,status,timestamp,result_count,error) VALUES(?,?,?,?,?,?,?)",
            ("u-1", "s-1", "aggregate", "completed", "2026-01-01T00:00:01", 1, None),
        )
        connection.commit()
        connection.close()

        _, _, _, cookie = _login(http_server)
        status, payload, headers = _request(
            http_server, "GET", "/api/backend/trace?user_id=u-1&session_id=s-1", cookie=cookie,
        )
        assert status == 200
        rows = json.loads(payload)
        assert [row["node"] for row in rows] == ["parse", "aggregate"]
        assert set(rows[0]) == {"node", "status", "timestamp", "result_count", "error"}
        _assert_no_challenge(headers)

        # The user-facing /api/trace keeps its own identity-cookie contract.
        status, _, headers = _request(
            http_server, "GET", "/api/trace?user_id=u-1&session_id=s-1", cookie=cookie,
        )
        assert status == 401
        _assert_no_challenge(headers)
    finally:
        _stop_server(http_server, thread)


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
        _stop_server(http_server, thread)


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
        _stop_server(http_server, thread)
