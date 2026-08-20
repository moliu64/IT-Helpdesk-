"""Chroma vector store backed exclusively by a local BGE model."""
from __future__ import annotations
import json
import threading
from pathlib import Path
from typing import Any
from src.llm_client import ROOT, load_config

COLLECTION = "helpdesk_solutions"
_LOCK = threading.Lock()

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
        self._collection = self._client.get_or_create_collection(
            COLLECTION, embedding_function=self._embedding, metadata={"hnsw:space": "cosine"}
        )

    def rebuild(self, documents: list[dict[str, Any]]) -> int:
        with _LOCK:
            try:
                self._client.delete_collection(COLLECTION)
            except Exception:
                pass
            self._collection = self._client.create_collection(
                COLLECTION, embedding_function=self._embedding, metadata={"hnsw:space": "cosine"}
            )
            self._collection.add(
                ids=[item["id"] for item in documents],
                documents=[item["text"] for item in documents],
                metadatas=[{"source": item["source"], "title": item["title"],
                            "steps_json": json.dumps(item["steps"], ensure_ascii=False)} for item in documents],
            )
            (self.index_dir / ".ready").write_text(str(len(documents)), encoding="ascii")
        return len(documents)

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        with _LOCK:
            result = self._collection.query(query_texts=[query], n_results=top_k,
                                            include=["metadatas", "distances"])
        matches = []
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for metadata, distance in zip(metadatas, distances):
            relevance = "高" if distance <= 0.3 else "中" if distance <= 0.6 else "低"
            matches.append({"source": metadata["source"], "title": metadata["title"],
                            "steps": json.loads(metadata["steps_json"]), "relevance": relevance})
        return matches
