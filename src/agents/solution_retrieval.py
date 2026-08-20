"""Retrieve solutions exclusively from the local RAG index."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class SolutionMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    title: str
    steps: list[str]
    relevance: Literal["高", "中", "低"]

class SolutionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    matches: list[SolutionMatch]

class SolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[SolutionItem] = Field(default_factory=list)

def retrieve_solutions(ticket: dict[str, Any], store: Any = None, top_k: int = 3) -> dict[str, list]:
    query = " ".join(part for part in (ticket.get("title", ""), ticket.get("description", "")) if part).strip()
    if not query:
        return SolutionResult(results=[]).model_dump()
    if store is None:
        from src.rag.vector_store import VectorStore
        try:
            from src.llm_client import ROOT, load_config
            index = Path(load_config()["rag"]["index_dir"])
            index = index if index.is_absolute() else ROOT / index
            if not (index / ".ready").is_file():
                return SolutionResult(results=[]).model_dump()
            store = VectorStore()
        except Exception:
            return SolutionResult(results=[]).model_dump()
    try:
        matches = store.search(query, top_k=top_k)
        return SolutionResult(results=[SolutionItem(query=query, matches=matches)]).model_dump()
    except Exception:
        return SolutionResult(results=[]).model_dump()
