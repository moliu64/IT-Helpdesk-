from pathlib import Path

import pytest

from src.rag.ingest import CHUNK_SIZE, SUPPORTED, chunk_text, documents_from_file


def test_chunk_text_uses_fifteen_percent_overlap() -> None:
    chunks = chunk_text("a" * (CHUNK_SIZE * 2))

    assert len(chunks) == 3
    overlap = int(CHUNK_SIZE * 0.15)
    assert chunks[0][-overlap:] == chunks[1][:overlap]


def test_chunk_text_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        chunk_text("text", chunk_size=0)
    with pytest.raises(ValueError):
        chunk_text("text", overlap_ratio=1)


def test_markdown_file_becomes_rag_documents(tmp_path: Path) -> None:
    knowledge = tmp_path / "vpn.md"
    knowledge.write_text("# VPN\n\n请重启客户端后重试。", encoding="utf-8")

    documents = documents_from_file(knowledge)

    assert ".md" in SUPPORTED
    assert documents[0]["source"] == "vpn.md"
    assert "重启客户端" in documents[0]["text"]
