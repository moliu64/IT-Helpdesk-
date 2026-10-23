"""Extract and split knowledge files for the local RAG index.

``extract_text`` turns a supported file into plain text; ``documents_from_file``
runs the deterministic structure-aware splitter so imported files seed the
managed store as individual knowledge entries (not fixed 900-character chunks).
Live uploads go through :mod:`src.rag.knowledge_agent` instead, which prefers
the LLM agent and falls back to the same rule splitter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.rag.knowledge_agent import split_text_rule

SUPPORTED = {".md", ".txt", ".pdf", ".docx"}
# Kept for the legacy fixed-window helper (used by external scripts/tests).
CHUNK_SIZE = 900
CHUNK_OVERLAP_RATIO = 0.15


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if suffix == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    raise ValueError(f"不支持的文件格式: {suffix}")


def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap_ratio: float = CHUNK_OVERLAP_RATIO,
) -> list[str]:
    """Split normalized text with a fixed, configurable character overlap.

    This remains the low-level legacy helper; knowledge-base imports use
    :func:`documents_from_file`, which splits on document structure instead.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not 0 <= overlap_ratio < 1:
        raise ValueError("overlap_ratio 必须在 [0, 1) 内")

    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        return []

    overlap = int(chunk_size * overlap_ratio)
    step = chunk_size - overlap
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += step
    return chunks


def documents_from_file(path: Path) -> list[dict[str, Any]]:
    """Convert one supported knowledge file into indexable knowledge entries.

    The returned dicts keep the legacy document contract (``id``/``text``/
    ``source``/``title``/``steps``) but each item now represents one semantic
    knowledge entry instead of a fixed-size text window.
    """
    text = extract_text(path)
    entries = split_text_rule(text, title_hint=path.stem)
    return [{"id": f"FILE-{path.stem}-{index:04d}", "text": entry.text,
             "source": path.name, "title": entry.title, "steps": list(entry.steps)}
            for index, entry in enumerate(entries, 1)]
