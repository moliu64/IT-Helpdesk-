"""Performance-related contracts: RAG warmup, status field, shared LLM client."""

from __future__ import annotations

import base64
import http.client
import json
import threading

import pytest

from src import llm_client
from ui import server


@pytest.fixture(autouse=True)
def _reset_warmup_flag():
    server._RAG_WARMED.clear()
    yield
    server._RAG_WARMED.clear()


def test_warm_rag_index_marks_ready(monkeypatch):
    class FakeStore:
        def __init__(self):
            self.queries = []

        def search(self, query, top_k=3):
            self.queries.append(query)
            return []

    store = FakeStore()
    monkeypatch.setattr(server, "get_cached_store", lambda: store)
    server._warm_rag_index()
    assert server._RAG_WARMED.is_set()
    # The warmup performs one tiny query so torch kernels are initialised.
    assert store.queries


def test_warm_rag_index_swallows_missing_model(monkeypatch):
    def explode():
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(server, "get_cached_store", explode)
    server._warm_rag_index()  # must not raise
    assert not server._RAG_WARMED.is_set()


def test_rag_status_reports_search_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("HELPDESK_ADMIN_USER", "admin")
    monkeypatch.setenv("HELPDESK_ADMIN_PASSWORD", "secret")
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "helpdesk.db")
    http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        server._RAG_WARMED.set()
        token = base64.b64encode(b"admin:secret").decode()
        connection = http.client.HTTPConnection(*http_server.server_address)
        connection.request("GET", "/api/rag/status", headers={"Authorization": f"Basic {token}"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        assert response.status == 200
        assert payload["search_ready"] is True
        assert "ready" in payload
    finally:
        http_server.shutdown()
        thread.join()
        http_server.server_close()


def test_llm_client_reuses_shared_http_client(monkeypatch):
    created = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(llm_client, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(llm_client, "_CLIENTS", {})
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    first = llm_client.LLMClient()
    second = llm_client.LLMClient()

    assert first._client is second._client
    assert len(created) == 1
    assert created[0]["max_retries"] == 0
