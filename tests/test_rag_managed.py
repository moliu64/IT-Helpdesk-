"""Contract for the managed RAG knowledge store and its admin CRUD APIs."""

from __future__ import annotations

import base64
import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from src.rag import managed_store, vector_store
from ui import server

IMPORT_ID = re.compile(r"^FILE-[0-9a-f]{12}-\d{4}$")


def _make_source(root: Path) -> Path:
    knowledge = root / "data" / "knowledge"
    imports = knowledge / "imports"
    tickets = root / "data" / "tickets"
    knowledge.mkdir(parents=True)
    imports.mkdir()
    tickets.mkdir(parents=True)
    (knowledge / "KB-0001.md").write_text(
        "# VPN 连接失败\n\n1. 重启 VPN 客户端\n2. 检查账号权限\n", encoding="utf-8")
    (knowledge / "KB-0002.md").write_text(
        "# 邮件无法发送\n\n1. 检查 SMTP 配置\n", encoding="utf-8")
    (tickets / "historical_tickets.json").write_text(json.dumps([
        {"ticket_id": "HIST-1", "title": "网络中断", "description": "交换机重启", "resolution": "1. 重启交换机"},
        {"ticket_id": "HIST-BAD", "title": "缺少字段"},
        None,
    ], ensure_ascii=False), encoding="utf-8")
    (imports / "note.md").write_text("# 导入说明\n\n重置密码请访问自助门户。", encoding="utf-8")
    return root


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated store/source/index plus a fake index rebuild (no Chroma/model)."""
    source = _make_source(tmp_path / "source")
    store_file = tmp_path / "managed.json"
    monkeypatch.setenv("HELPDESK_RAG_STORE", str(store_file))
    monkeypatch.setenv("HELPDESK_RAG_SOURCE_ROOT", str(source))
    monkeypatch.setenv("HELPDESK_RAG_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(managed_store, "_CACHE", {})
    sync_calls: list[int] = []

    def fake_sync():
        count = len(managed_store.load_documents())
        sync_calls.append(count)
        return {"indexed": True, "count": count}

    monkeypatch.setattr(managed_store, "sync_index", fake_sync)
    return {"tmp": tmp_path, "source": source, "store": store_file, "calls": sync_calls}


# ---------------------------------------------------------------------------
# Seeding / store layout
# ---------------------------------------------------------------------------

def test_seed_creates_expected_documents_and_is_idempotent(env):
    documents = managed_store.load_documents()
    assert len(documents) == 4
    assert {item["source"] for item in documents} == {"KB-0001", "KB-0002", "HIST-1", "note.md"}

    raw = json.loads(env["store"].read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert set(raw["documents"]) == {"KB-0001", "KB-0002", "HIST-1", "note.md"}
    assert raw["documents"]["note.md"]["kind"] == "import"
    assert raw["documents"]["KB-0001"]["kind"] == "kb"
    assert raw["documents"]["HIST-1"]["kind"] == "history"

    mtime = env["store"].stat().st_mtime_ns
    managed_store.ensure_seeded()
    assert managed_store.load_documents()
    assert env["store"].stat().st_mtime_ns == mtime, "seeding must not rewrite an existing store"
    ids = [item["id"] for item in managed_store.load_documents()]
    assert len(ids) == len(set(ids)) == 4


def test_imported_entries_use_hashed_ids(env):
    managed_store.ensure_seeded()
    entries = managed_store.list_entries(document="note.md")
    assert len(entries) == 1
    assert IMPORT_ID.match(entries[0]["id"]), entries[0]["id"]

    by_name = {doc["name"]: doc for doc in managed_store.list_documents()}
    assert by_name["note.md"]["kind"] == "import"
    assert by_name["note.md"]["entry_count"] == 1
    assert by_name["KB-0001"]["kind"] == "kb"
    assert by_name["HIST-1"]["kind"] == "history"
    assert managed_store.stats() == {"documents": 4, "entries": 4, "enabled": 4}


def test_corrupt_store_is_reseeded(env):
    env["store"].write_text("{ this is not json", encoding="utf-8")
    documents = managed_store.load_documents()
    assert len(documents) == 4
    raw = json.loads(env["store"].read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["documents"]) == 4


# ---------------------------------------------------------------------------
# Entry CRUD
# ---------------------------------------------------------------------------

def test_create_update_delete_entry(env):
    created = managed_store.create_entry(
        document="manual-1", title="打印机卡纸", text="打开后盖取出纸张",
        steps=["取出纸张", "重启打印服务"],
    )
    entry = created["entry"]
    assert created["indexed"] is True
    assert env["calls"], "mutations must trigger an index rebuild"
    assert entry["document"] == "manual-1"
    assert entry["enabled"] is True
    assert created["stats"] == {"documents": 5, "entries": 5, "enabled": 5}

    updated = managed_store.update_entry(entry["id"], title="打印机卡纸处理",
                                         text="清空队列后重启打印服务", enabled=False)
    assert updated["indexed"] is True
    assert updated["entry"]["title"] == "打印机卡纸处理"
    assert updated["entry"]["enabled"] is False
    assert all(item["id"] != entry["id"] for item in managed_store.load_documents()), "disabled entries are not indexed"

    deleted = managed_store.delete_entry(entry["id"])
    assert deleted["deleted"] is True
    assert deleted["stats"]["entries"] == 4
    assert "manual-1" not in {doc["name"] for doc in managed_store.list_documents()}
    with pytest.raises(managed_store.NotFoundError):
        managed_store.delete_entry(entry["id"])


def test_entry_validation_errors(env):
    with pytest.raises(managed_store.StoreError):
        managed_store.create_entry(document="", text="x")
    with pytest.raises(managed_store.StoreError):
        managed_store.create_entry(document="doc", text="   ")
    with pytest.raises(managed_store.StoreError):
        managed_store.create_entry(document="doc", text="x", steps="not-a-list")
    with pytest.raises(managed_store.NotFoundError):
        managed_store.update_entry("missing-id", text="x")
    created = managed_store.create_entry(document="doc", text="keep me")
    with pytest.raises(managed_store.StoreError):
        managed_store.update_entry(created["entry"]["id"], text="  ")


def test_move_entry_creates_target_document(env):
    created = managed_store.create_entry(document="old-doc", title="待移动", text="move me")
    entry_id = created["entry"]["id"]
    moved = managed_store.move_entry(entry_id, "new-doc")
    assert moved["indexed"] is True
    assert moved["entry"]["document"] == "new-doc"
    assert moved["entry"]["id"] == entry_id

    by_name = {doc["name"]: doc for doc in managed_store.list_documents()}
    assert "old-doc" not in by_name, "empty source documents are dropped"
    assert by_name["new-doc"]["kind"] == "manual"
    assert by_name["new-doc"]["entry_count"] == 1

    # Same-document move is a no-op and does not rebuild the index.
    calls_before = len(env["calls"])
    again = managed_store.move_entry(entry_id, "new-doc")
    assert again["indexed"] is False
    assert len(env["calls"]) == calls_before
    with pytest.raises(managed_store.NotFoundError):
        managed_store.move_entry("missing-id", "somewhere")


def test_remove_document(env):
    managed_store.ensure_seeded()
    result = managed_store.remove_document("KB-0001")
    assert result["deleted"] is True
    assert result["removed"] == 1
    assert result["stats"]["documents"] == 3
    assert "KB-0001" not in {doc["name"] for doc in managed_store.list_documents()}
    with pytest.raises(managed_store.NotFoundError):
        managed_store.remove_document("KB-0001")


def test_list_entries_filters_and_newest_first(env, monkeypatch):
    managed_store.create_entry(document="man", title="Alpha VPN", text="vpn 掉线了")
    managed_store.create_entry(document="man", title="Beta 打印机", text="无法打印")

    assert len(managed_store.list_entries(document="man")) == 2
    assert all(entry["document"] == "man" for entry in managed_store.list_entries(document="man"))
    hits = managed_store.list_entries(query="vpn")
    assert any(entry["title"] == "Alpha VPN" for entry in hits)
    assert all("vpn" in f"{entry['title']} {entry['text']} {entry['document']}".lower() for entry in hits)

    # Force distinct timestamps to assert newest-first ordering deterministically.
    clock = {"value": 0}

    def fake_now():
        clock["value"] += 1
        return f"2026-01-01T00:00:{clock['value']:02d}"

    monkeypatch.setattr(managed_store, "_now", fake_now)
    first = managed_store.create_entry(document="ordered", title="older", text="one")["entry"]
    second = managed_store.create_entry(document="ordered", title="newer", text="two")["entry"]
    ordered = managed_store.list_entries(document="ordered")
    assert [entry["id"] for entry in ordered] == [second["id"], first["id"]]


# ---------------------------------------------------------------------------
# File import / removal
# ---------------------------------------------------------------------------

def test_import_file_creates_and_replaces_same_filename(env):
    upload = env["tmp"] / "upload.md"
    upload.write_text("# 新知识\n\n" + "这是一个很长的导入测试内容。" * 120, encoding="utf-8")
    first = managed_store.import_file(upload)
    assert first["document"] == "upload.md"
    assert first["chunks"] >= 2
    assert first["indexed"] is True
    first_entries = managed_store.list_entries(document="upload.md")
    assert len(first_entries) == first["chunks"]
    assert all(IMPORT_ID.match(entry["id"]) for entry in first_entries)
    first_ids = {entry["id"] for entry in first_entries}

    upload.write_text("# 新知识\n\n短内容", encoding="utf-8")
    second = managed_store.import_file(upload)
    assert second["replaced"] == first["chunks"]
    assert second["chunks"] == 1
    second_entries = managed_store.list_entries(document="upload.md")
    assert len(second_entries) == 1
    assert {entry["id"] for entry in second_entries}.isdisjoint(first_ids)

    all_ids = [entry["id"] for entry in managed_store.list_entries()]
    assert len(all_ids) == len(set(all_ids)), "re-upload must not duplicate entry ids"

    removed = managed_store.remove_file(upload)
    assert removed["removed"] == 1
    assert managed_store.list_entries(document="upload.md") == []


def test_empty_text_upload_is_rejected_without_touching_store(env):
    empty = env["tmp"] / "empty.md"
    empty.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(managed_store.EmptyTextError) as excinfo:
        managed_store.import_file(empty)
    assert managed_store.EMPTY_TEXT_MESSAGE in str(excinfo.value)
    assert "empty.md" not in {doc["name"] for doc in managed_store.list_documents()}
    assert managed_store.stats()["entries"] == 4


# ---------------------------------------------------------------------------
# Legacy file-only loader
# ---------------------------------------------------------------------------

def test_legacy_load_documents_for_custom_root(tmp_path):
    legacy = tmp_path / "legacy"
    knowledge = legacy / "data" / "knowledge"
    imports = knowledge / "imports"
    tickets = legacy / "data" / "tickets"
    knowledge.mkdir(parents=True)
    imports.mkdir()
    tickets.mkdir(parents=True)
    (knowledge / "KB-0009.md").write_text("# 旧知识\n\n1. 做点什么\n", encoding="utf-8")
    (imports / "legacy.md").write_text("# 旧导入\n\n内容", encoding="utf-8")
    (tickets / "historical_tickets.json").write_text(
        '[{"ticket_id":"HIST-1","title":"ok","description":"desc","resolution":"fix"},'
        '{"ticket_id":"HIST-2","title":"missing"}, null]', encoding="utf-8")

    documents = vector_store.load_documents(legacy)
    assert [item["source"] for item in documents] == ["KB-0009", "HIST-1", "legacy.md"]
    assert set(documents[0]) == {"id", "text", "source", "title", "steps"}
    # Legacy import chunk ids keep the old deterministic form.
    assert documents[2]["id"] == "FILE-legacy-0001"
    assert not (legacy / "data" / "knowledge" / "managed_entries.json").exists()
    assert managed_store.load_documents(legacy) == documents


# ---------------------------------------------------------------------------
# HTTP APIs
# ---------------------------------------------------------------------------

@pytest.fixture
def api(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "db" / "helpdesk.db")
    monkeypatch.setattr(server, "RAG_IMPORT_PATH", tmp_path / "imports")
    http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, auth=True):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            token = base64.b64encode(b"admin:secret").decode()
            headers["Authorization"] = f"Basic {token}"
        connection = http.client.HTTPConnection(*http_server.server_address)
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        payload = response.read()
        response_headers = response.headers
        connection.close()
        try:
            parsed = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            parsed = payload
        return response.status, parsed, response_headers

    yield request
    http_server.shutdown()
    thread.join()
    http_server.server_close()


def test_rag_page_and_library_require_admin(api):
    status, _, headers = api("GET", "/rag", auth=False)
    assert status == 302
    assert headers.get("Location") == "/login?next=/rag"
    assert headers.get("WWW-Authenticate") is None

    status, _, headers = api("GET", "/rag.html", auth=False)
    assert status == 302
    assert headers.get("Location") == "/login?next=/rag.html"

    status, body, headers = api("GET", "/api/rag/library", auth=False)
    assert status == 401
    assert headers.get("WWW-Authenticate") is None
    assert body["error"] == "登录已过期，请重新登录"

    status, body, headers = api("POST", "/api/rag/entries/create",
                                {"document": "x", "text": "y"}, auth=False)
    assert status == 401
    assert headers.get("WWW-Authenticate") is None


def test_rag_page_served_when_deployed(api):
    status, body, headers = api("GET", "/rag")
    if server.RAG_PAGE.is_file():
        assert status == 200
        assert headers.get("Content-Type", "").startswith("text/html")
    else:
        assert status == 404
        assert "尚未部署" in body["error"]


def test_library_and_status_over_http(api):
    status, body, _ = api("GET", "/api/rag/library")
    assert status == 200
    names = {doc["name"] for doc in body["documents"]}
    assert {"KB-0001", "KB-0002", "HIST-1", "note.md"} <= names
    assert body["stats"]["entries"] >= 4
    for doc in body["documents"]:
        assert {"name", "title", "kind", "entry_count", "enabled_count", "updated_at"} <= set(doc)

    status, status_body, _ = api("GET", "/api/rag/status")
    assert status == 200
    assert status_body["library_ready"] is True
    assert status_body["managed_entries"] == status_body["source_documents"]
    assert "ready" in status_body


def test_entry_crud_over_http(api):
    status, body, _ = api("POST", "/api/rag/entries/create", {
        "document": "manual-http", "title": "HTTP 条目",
        "text": "通过 HTTP 创建的知识条目", "steps": ["第一步"],
    })
    assert status == 200
    entry = body["entry"]
    assert body["indexed"] is True
    assert entry["document"] == "manual-http"
    assert entry["steps"] == ["第一步"]

    status, body, _ = api("GET", "/api/rag/entries?document=manual-http")
    assert status == 200
    assert [item["id"] for item in body["entries"]] == [entry["id"]]

    status, body, _ = api("POST", "/api/rag/entries/update",
                          {"id": entry["id"], "title": "更新标题", "enabled": False})
    assert status == 200
    assert body["entry"]["title"] == "更新标题"
    assert body["entry"]["enabled"] is False

    status, body, _ = api("POST", "/api/rag/entries/move",
                          {"id": entry["id"], "target_document": "moved-http"})
    assert status == 200
    assert body["entry"]["document"] == "moved-http"

    status, body, _ = api("GET", "/api/rag/entries?q=HTTP")
    assert status == 200
    assert any(item["id"] == entry["id"] for item in body["entries"])

    status, body, _ = api("POST", "/api/rag/documents/delete", {"document": "moved-http"})
    assert status == 200
    assert body["deleted"] is True
    status, body, _ = api("GET", "/api/rag/library")
    assert "moved-http" not in {doc["name"] for doc in body["documents"]}

    status, body, _ = api("POST", "/api/rag/entries/delete", {"id": "does-not-exist"})
    assert status == 404
    assert body["error"] == "条目不存在"


def test_import_and_delete_file_over_http(api):
    payload = base64.b64encode("# 门户重置\n\n访问自助门户重置密码".encode()).decode()
    status, body, _ = api("POST", "/api/rag/import", {"name": "http-note.md", "content_base64": payload})
    assert status == 200
    assert body["saved"] is True
    assert body["document"] == "http-note.md"
    assert body["chunks"] == 1
    assert body["indexed"] is True
    assert (server.RAG_IMPORT_PATH / "http-note.md").is_file()
    assert len(managed_store.list_entries(document="http-note.md")) == 1

    # Re-uploading the same filename replaces the managed entries.
    replacement = base64.b64encode("# 门户重置\n\n新的口令策略".encode()).decode()
    status, body, _ = api("POST", "/api/rag/import", {"name": "http-note.md", "content_base64": replacement})
    assert status == 200
    assert body["replaced"] == 1
    assert len(managed_store.list_entries(document="http-note.md")) == 1

    # Empty extraction is rejected and the broken file must not be kept.
    empty = base64.b64encode(b"   \n").decode()
    status, body, _ = api("POST", "/api/rag/import", {"name": "empty-http.md", "content_base64": empty})
    assert status == 400
    assert "未能从该文件提取到文本" in body["error"]
    assert not (server.RAG_IMPORT_PATH / "empty-http.md").exists()

    status, body, _ = api("DELETE", "/api/rag/files?name=http-note.md")
    assert status == 200
    assert body["deleted"] is True
    assert body["removed"] == 1
    assert managed_store.list_entries(document="http-note.md") == []
    assert not (server.RAG_IMPORT_PATH / "http-note.md").exists()

    status, _, _ = api("DELETE", "/api/rag/files?name=http-note.md")
    assert status == 404


def test_documents_backcompat_shape(api):
    status, body, _ = api("GET", "/api/rag/documents")
    assert status == 200
    assert isinstance(body, list) and body
    first = body[0]
    for key in ("id", "source", "document", "title", "steps", "preview", "enabled"):
        assert key in first
    assert first["document"] == first["source"]


def test_rebuild_over_http(api):
    status, body, _ = api("POST", "/api/rag/rebuild", {})
    assert status == 200
    assert body["count"] == len(managed_store.load_documents())
    assert body["indexed"] is True
    assert body["stats"]["entries"] >= 4


def test_rebuild_rejects_empty_library(api, tmp_path, monkeypatch):
    empty_source = tmp_path / "empty-source"
    (empty_source / "data" / "knowledge").mkdir(parents=True)
    (empty_source / "data" / "tickets").mkdir(parents=True)
    monkeypatch.setenv("HELPDESK_RAG_SOURCE_ROOT", str(empty_source))
    monkeypatch.setenv("HELPDESK_RAG_STORE", str(tmp_path / "empty-store.json"))
    monkeypatch.setattr(managed_store, "_CACHE", {})

    status, body, _ = api("POST", "/api/rag/rebuild", {})
    assert status == 400
    assert body["error"] == "知识库为空，无法重建索引"
