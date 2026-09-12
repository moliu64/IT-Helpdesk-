"""Chroma vector store backed exclusively by a local BGE model."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.llm_client import ROOT, load_config

logger = logging.getLogger(__name__)

COLLECTION = "helpdesk_solutions"
_LOCK = threading.Lock()
_CACHED_STORES: dict[tuple[str, str, str], VectorStore] = {}
_CACHE_LOCK = threading.Lock()

def load_documents(root: Path = ROOT) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted((root / "data" / "knowledge").glob("KB-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            logger.warning("跳过无法读取的知识库文件 %s: %s", path, exc)
            continue
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        steps = [line.split(". ", 1)[1] for line in text.splitlines() if line[:1].isdigit() and ". " in line]
        documents.append({"id": path.stem, "text": text, "source": path.stem, "title": title, "steps": steps})
    ticket_file = root / "data" / "tickets" / "historical_tickets.json"
    if ticket_file.exists():
        try:
            history = json.loads(ticket_file.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                raise ValueError("历史工单必须是数组")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("历史工单文件不可用，跳过：%s", exc)
            history = []
        for ticket in history:
            if not isinstance(ticket, dict):
                logger.warning("跳过非对象历史工单记录")
                continue
            required = ("ticket_id", "title", "description", "resolution")
            if any(not isinstance(ticket.get(field), str) or not ticket[field].strip() for field in required):
                logger.warning("跳过字段不完整的历史工单：%r", ticket.get("ticket_id"))
                continue
            text = f"{ticket['title']}\n{ticket['description']}\n解决方案：{ticket['resolution']}"
            resolution_steps = [
                line.split(". ", 1)[1].strip()
                for line in ticket["resolution"].splitlines()
                if line[:1].isdigit() and ". " in line
            ] or [ticket["resolution"]]
            documents.append({"id": ticket["ticket_id"], "text": text, "source": ticket["ticket_id"],
                              "title": ticket["title"], "steps": resolution_steps})
    # Imported files use the same document contract as built-in KB articles.
    from src.rag.ingest import SUPPORTED, documents_from_file
    imports = root / "data" / "knowledge" / "imports"
    if imports.exists():
        for path in sorted(imports.iterdir()):
            if path.is_file() and path.suffix.lower() in SUPPORTED:
                try:
                    documents.extend(documents_from_file(path))
                except (OSError, UnicodeError, ValueError, TypeError) as exc:
                    logger.warning("跳过无法解析的导入文件 %s: %s", path, exc)
    return documents

class LocalBGEEmbeddingFunction:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_name)
    def __call__(self, input: list[str]) -> list[list[float]]:
        return self.model.encode(input, normalize_embeddings=True).tolist()
    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self(input)
    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return self(input)
    def name(self) -> str:
        return "local-bge"

def _settings(config: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path]:
    settings = config or load_config()
    embedding = settings.get("embedding", {})
    if embedding.get("provider") != "local":
        raise ValueError("本项目 RAG 仅允许 embedding.provider=local")
    index = Path(settings["rag"]["index_dir"])
    return settings, index if index.is_absolute() else ROOT / index

class VectorStore:
    def __init__(self, config: dict[str, Any] | None = None):
        import chromadb
        settings, index_dir = _settings(config)
        index_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(index_dir))
        self.index_dir = index_dir
        self._embedding = LocalBGEEmbeddingFunction(settings["embedding"]["model"])
        # Query embeddings are the dominant per-request cost.  Keep a small
        # process-local cache for repeated questions (including UI retries),
        # while still rebuilding/clearing it whenever the index changes.
        self._search_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._search_cache_lock = threading.Lock()
        self._collection = self._client.get_or_create_collection(
            COLLECTION, embedding_function=self._embedding, metadata={"hnsw:space": "cosine"}
        )

    def rebuild(self, documents: list[dict[str, Any]]) -> int:
        if not documents:
            raise ValueError("没有可索引的文档")
        ids: list[str] = []
        for item in documents:
            if not isinstance(item, dict):
                raise ValueError("索引文档必须是对象")
            if not all(isinstance(item.get(field), str) and item[field].strip()
                       for field in ("id", "text", "source", "title")):
                raise ValueError("索引文档缺少必填字段")
            if not isinstance(item.get("steps", []), list) or not all(isinstance(step, str) for step in item["steps"]):
                raise ValueError("索引文档 steps 必须是字符串数组")
            ids.append(item["id"])
        if len(ids) != len(set(ids)):
            # Validate before deleting the current collection. A bad import
            # must never destroy a known-good index.
            raise ValueError("索引文档 ID 必须唯一")
        with _LOCK:
            ready = self.index_dir / ".ready"
            staged_name = f"{COLLECTION}_build_{uuid4().hex[:12]}"
            staged = self._client.create_collection(
                staged_name, embedding_function=self._embedding, metadata={"hnsw:space": "cosine"}
            )
            try:
                staged.add(
                    ids=ids,
                    documents=[item["text"] for item in documents],
                    metadatas=[{"source": item["source"], "title": item["title"],
                                "steps_json": json.dumps(item["steps"], ensure_ascii=False)} for item in documents],
                )
            except Exception:
                self._client.delete_collection(staged_name)
                raise

            # The expensive/fragile embedding write is complete before the
            # old collection is touched. Chroma supports renaming a collection,
            # which gives us a small atomic swap window for readers.
            try:
                self._client.delete_collection(COLLECTION)
            except Exception:
                pass
            try:
                staged.modify(name=COLLECTION)
                self._collection = self._client.get_collection(COLLECTION, embedding_function=self._embedding)
                with self._search_cache_lock:
                    self._search_cache.clear()
                marker = self.index_dir / ".ready.tmp"
                marker.write_text(str(len(documents)), encoding="ascii")
                marker.replace(ready)
            except Exception:
                try:
                    self._client.delete_collection(staged_name)
                except Exception:
                    pass
                ready.unlink(missing_ok=True)
                raise
        return len(documents)

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        query = " ".join(str(query).split())[:500]
        if not query:
            return []
        top_k = max(1, min(int(top_k), 10))
        cache_key = (query, top_k)
        with self._search_cache_lock:
            cached = self._search_cache.get(cache_key)
            if cached is not None:
                return [dict(item, steps=list(item.get("steps", []))) for item in cached]
        with _LOCK:
            result = self._collection.query(query_texts=[query], n_results=top_k,
                                            include=["metadatas", "distances"])
        matches = []
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for metadata, distance in zip(metadatas, distances, strict=False):
            relevance = "高" if distance <= 0.3 else "中" if distance <= 0.6 else "低"
            try:
                steps = json.loads(metadata["steps_json"])
                if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
                    raise ValueError("steps_json 不是字符串数组")
                matches.append({"source": str(metadata["source"]), "title": str(metadata["title"]),
                                "steps": steps, "relevance": relevance})
            except (KeyError, TypeError, json.JSONDecodeError, ValueError):
                logger.warning("跳过格式损坏的 RAG 元数据")
        with self._search_cache_lock:
            # Bound memory usage if a long-running server receives many unique
            # queries.  Dict insertion order gives a simple FIFO eviction.
            if len(self._search_cache) >= 128:
                self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[cache_key] = matches
        return matches


def get_cached_store(config: dict[str, Any] | None = None) -> VectorStore:
    """Reuse one Chroma/BGE instance so each chat does not reload model weights."""
    settings, index = _settings(config)
    embedding = settings.get("embedding", {})
    key = (str(index), str(embedding.get("provider", "")), str(embedding.get("model", "")))
    with _CACHE_LOCK:
        if key not in _CACHED_STORES:
            _CACHED_STORES[key] = VectorStore(settings)
        return _CACHED_STORES[key]


def clear_cached_store() -> None:
    with _CACHE_LOCK:
        _CACHED_STORES.clear()
