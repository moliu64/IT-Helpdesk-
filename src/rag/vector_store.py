"""Chroma vector store backed exclusively by a local BGE model."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from src.llm_client import ROOT, load_config

COLLECTION = "helpdesk_solutions"
_LOCK = threading.Lock()
_CACHED_STORE: VectorStore | None = None
_CACHE_LOCK = threading.Lock()

def load_documents(root: Path = ROOT) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted((root / "data" / "knowledge").glob("KB-*.md")):
        text = path.read_text(encoding="utf-8")
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        steps = [line.split(". ", 1)[1] for line in text.splitlines() if line[:1].isdigit() and ". " in line]
        documents.append({"id": path.stem, "text": text, "source": path.stem, "title": title, "steps": steps})
    ticket_file = root / "data" / "tickets" / "historical_tickets.json"
    if ticket_file.exists():
        for ticket in json.loads(ticket_file.read_text(encoding="utf-8")):
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
                documents.extend(documents_from_file(path))
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
        with _LOCK:
            try:
                self._client.delete_collection(COLLECTION)
            except Exception:
                pass
            self._collection = self._client.create_collection(
                COLLECTION, embedding_function=self._embedding, metadata={"hnsw:space": "cosine"}
            )
            ids = [str(item["id"]) for item in documents]
            if len(ids) != len(set(ids)):
                raise ValueError("索引文档 ID 必须唯一")
            self._collection.add(
                ids=ids,
                documents=[item["text"] for item in documents],
                metadatas=[{"source": item["source"], "title": item["title"],
                            "steps_json": json.dumps(item["steps"], ensure_ascii=False)} for item in documents],
            )
            with self._search_cache_lock:
                self._search_cache.clear()
            (self.index_dir / ".ready").write_text(str(len(documents)), encoding="ascii")
        return len(documents)

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        query = " ".join(str(query).split())[:500]
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
            matches.append({"source": metadata["source"], "title": metadata["title"],
                            "steps": json.loads(metadata["steps_json"]), "relevance": relevance})
        with self._search_cache_lock:
            # Bound memory usage if a long-running server receives many unique
            # queries.  Dict insertion order gives a simple FIFO eviction.
            if len(self._search_cache) >= 128:
                self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[cache_key] = matches
        return matches


def get_cached_store(config: dict[str, Any] | None = None) -> VectorStore:
    """Reuse one Chroma/BGE instance so each chat does not reload model weights."""
    global _CACHED_STORE
    if _CACHED_STORE is None:
        with _CACHE_LOCK:
            if _CACHED_STORE is None:
                _CACHED_STORE = VectorStore(config)
    return _CACHED_STORE


def clear_cached_store() -> None:
    global _CACHED_STORE
    with _CACHE_LOCK:
        _CACHED_STORE = None
