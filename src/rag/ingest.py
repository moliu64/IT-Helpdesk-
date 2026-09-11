"""Import and chunk knowledge files for the local RAG index."""
from __future__ import annotations

from pathlib import Path
from typing import Any

SUPPORTED = {".md", ".txt", ".pdf", ".docx"}
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
    """Split normalized text with a fixed, configurable character overlap."""
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
    """Convert one supported knowledge file into indexable chunks."""
    text = extract_text(path)
    chunks = chunk_text(text)
    return [{"id": f"FILE-{path.stem}-{i:04d}", "text": chunk,
             "source": path.name, "title": path.stem, "steps": []}
            for i, chunk in enumerate(chunks, 1)]
