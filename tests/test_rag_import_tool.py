"""Contract for the dedicated RAG import tool (schema, preview, persistence)."""
from __future__ import annotations

import json
import re

import pytest

from src.rag import managed_store
from src.rag.import_tool import (
    RAG_IMPORT_TOOL,
    TOOL_NAME,
    call_rag_import_tool,
    import_document_to_rag,
    preview_document,
)

ENTRY_ID = re.compile(r"^FILE-[0-9a-f]{12}-\d{4}$")
GUIDE = """1. VPN 连接失败
现象：客户端提示连接超时。
排查与解决：重启 VPN 客户端；检查账号权限是否正常。
2. 邮箱无法登录
现象：网页提示密码错误。
排查与解决：在自助门户重置密码；确认邮箱账号未被锁定。
"""


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HELPDESK_RAG_STORE", str(tmp_path / "managed.json"))
    monkeypatch.setenv("HELPDESK_RAG_SOURCE_ROOT", str(tmp_path / "source"))
    monkeypatch.setenv("HELPDESK_RAG_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("HELPDESK_RAG_INGEST_MODE", "rule")
    monkeypatch.setattr(managed_store, "_CACHE", {})

    class Sync:
        calls = 0

        @classmethod
        def fake_sync(cls):
            cls.calls += 1
            return {"indexed": True, "count": len(managed_store.load_documents())}

    monkeypatch.setattr(managed_store, "sync_index", Sync.fake_sync)
    return {"tmp": tmp_path, "sync": Sync}


def test_tool_schema_is_a_function_call():
    assert RAG_IMPORT_TOOL["type"] == "function"
    function = RAG_IMPORT_TOOL["function"]
    assert function["name"] == TOOL_NAME == "import_document_to_rag"
    assert function["parameters"]["required"] == ["path"]
    assert set(function["parameters"]["properties"]) == {"path", "mode", "dry_run", "rebuild_index"}


def test_preview_document_returns_entries_without_writing(store_env, tmp_path):
    source = tmp_path / "guide.md"
    source.write_text(GUIDE, encoding="utf-8")

    preview = preview_document(source, mode="rule")

    assert preview["mode"] == "rule"
    assert preview["entry_count"] == 2
    assert [entry["title"] for entry in preview["entries"]] == ["VPN 连接失败", "邮箱无法登录"]
    assert not (store_env["tmp"] / "managed.json").exists(), "预览不能写入知识库"


def test_import_document_to_rag_persists_entries_and_rebuilds(store_env, tmp_path):
    source = tmp_path / "guide.md"
    source.write_text(GUIDE, encoding="utf-8")

    result = import_document_to_rag(source, mode="rule")

    assert result["saved"] is True
    assert result["chunks"] == 2
    assert result["replaced"] == 0
    assert result["mode"] == "rule"
    assert result["indexed"] is True
    assert store_env["sync"].calls == 1
    entries = managed_store.list_entries(document="guide.md")
    assert {entry["title"] for entry in entries} == {"VPN 连接失败", "邮箱无法登录"}
    assert all(ENTRY_ID.match(entry["id"]) for entry in entries)
    assert all(entry["steps"] for entry in entries)
    stored = json.loads((store_env["tmp"] / "managed.json").read_text(encoding="utf-8"))
    assert stored["documents"]["guide.md"]["kind"] == "import"


def test_import_document_to_rag_can_skip_index_rebuild(store_env, tmp_path):
    source = tmp_path / "guide.md"
    source.write_text(GUIDE, encoding="utf-8")

    result = import_document_to_rag(source, mode="rule", rebuild_index=False)

    assert result["chunks"] == 2
    assert result["indexed"] is False
    assert store_env["sync"].calls == 0


def test_call_tool_supports_dry_run_and_validation(store_env, tmp_path):
    source = tmp_path / "guide.md"
    source.write_text(GUIDE, encoding="utf-8")

    preview = call_rag_import_tool({"path": str(source), "mode": "rule", "dry_run": True})
    assert preview["entry_count"] == 2
    assert not (store_env["tmp"] / "managed.json").exists()

    with pytest.raises(ValueError, match="文件不存在"):
        call_rag_import_tool({"path": str(tmp_path / "missing.md")})
    with pytest.raises(ValueError, match="mode"):
        call_rag_import_tool({"path": str(source), "mode": "bogus"})
