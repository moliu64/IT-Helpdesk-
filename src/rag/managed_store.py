"""Persistent, editable knowledge store that feeds the local RAG index.

The managed store is the source of truth for ``/api/rag/*``: file-based KB
articles, historical tickets and imported uploads are parsed once to seed it,
after which administrators can add/edit/delete/move individual entries.  Every
mutation persists the store atomically and rebuilds the Chroma index.

Store file: ``HELPDESK_RAG_STORE`` (absolute or ROOT-relative), default
``data/knowledge/managed_entries.json``.  Schema::

    {"version": 1, "updated_at": iso,
     "documents": {"<document>": {
         "title": str, "kind": "kb|history|import|manual",
         "entries": [{"id": str, "title": str, "text": str, "steps": [str],
                      "enabled": bool, "updated_at": iso}]}}}
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.llm_client import ROOT

logger = logging.getLogger(__name__)

STORE_VERSION = 1
DEFAULT_STORE_RELATIVE = "data/knowledge/managed_entries.json"
STORE_ENV = "HELPDESK_RAG_STORE"
SOURCE_ROOT_ENV = "HELPDESK_RAG_SOURCE_ROOT"
ENTRY_KINDS = {"kb", "history", "import", "manual"}
EMPTY_TEXT_MESSAGE = "未能从该文件提取到文本（可能是扫描版 PDF），请改用可复制文本的文件"

# One process-wide re-entrant lock protects the read-modify-write cycle; the
# vector-store rebuild happens outside it so slow embeddings never block reads.
_LOCK = threading.RLock()
# path -> (st_mtime_ns, st_size, parsed store) so chat/status reads stay cheap.
_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


class StoreError(ValueError):
    """Base class for user-facing managed-store validation errors."""


class EmptyTextError(StoreError):
    """Raised when an uploaded file yields no extractable text."""


class NotFoundError(StoreError):
    """Raised when a requested entry or document does not exist."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _resolve(raw: str | None, default_relative: str) -> Path:
    candidate = Path((raw or "").strip() or default_relative)
    return candidate if candidate.is_absolute() else ROOT / candidate


def store_path() -> Path:
    """Absolute path of the managed JSON store (env-overridable for tests)."""
    return _resolve(os.getenv(STORE_ENV), DEFAULT_STORE_RELATIVE)


def source_root() -> Path:
    """Root whose data/ tree is parsed when seeding or using the legacy loader."""
    raw = os.getenv(SOURCE_ROOT_ENV, "").strip()
    return Path(raw) if raw else ROOT


# ---------------------------------------------------------------------------
# Source parsing (legacy file-only loader)
# ---------------------------------------------------------------------------

def _parse_sources(root: Path) -> list[dict[str, Any]]:
    """Parse KB markdown, historical tickets and imported files under ``root``."""
    root = Path(root)
    documents: list[dict[str, Any]] = []
    for path in sorted((root / "data" / "knowledge").glob("KB-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            logger.warning("跳过无法读取的知识库文件 %s: %s", path, exc)
            continue
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        steps = [line.split(". ", 1)[1] for line in text.splitlines() if line[:1].isdigit() and ". " in line]
        documents.append({"id": path.stem, "text": text, "source": path.stem,
                          "title": title, "steps": steps, "kind": "kb"})
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
                              "title": ticket["title"], "steps": resolution_steps, "kind": "history"})
    # Imported files use the same document contract as built-in KB articles.
    from src.rag.ingest import SUPPORTED, documents_from_file
    imports = root / "data" / "knowledge" / "imports"
    if imports.exists():
        for path in sorted(imports.iterdir()):
            if path.is_file() and path.suffix.lower() in SUPPORTED:
                try:
                    chunks = documents_from_file(path)
                except (OSError, UnicodeError, ValueError, TypeError) as exc:
                    logger.warning("跳过无法解析的导入文件 %s: %s", path, exc)
                    continue
                for chunk in chunks:
                    chunk["kind"] = "import"
                    chunk["doc_title"] = path.stem
                    documents.append(chunk)
    return documents


def load_source_documents(root: Path = ROOT) -> list[dict[str, Any]]:
    """Legacy file-only loader used for custom roots and first-time seeding."""
    return [
        {"id": item["id"], "text": item["text"], "source": item["source"],
         "title": item["title"], "steps": item["steps"]}
        for item in _parse_sources(Path(root))
    ]


def _seed_documents(root: Path) -> dict[str, dict[str, Any]]:
    """Group parsed sources into the managed document/entry structure."""
    documents: dict[str, dict[str, Any]] = {}
    salts: dict[str, str] = {}
    for item in _parse_sources(root):
        source = str(item["source"])
        kind = str(item["kind"])
        updated = _now()
        if kind in {"kb", "history"}:
            documents[source] = {
                "title": str(item["title"]),
                "kind": kind,
                "entries": [{
                    "id": str(item["id"]),
                    "title": str(item["title"]),
                    "text": str(item["text"]),
                    "steps": [str(step) for step in item["steps"]],
                    "enabled": True,
                    "updated_at": updated,
                }],
            }
            continue
        document = documents.setdefault(source, {"title": str(item.get("doc_title") or source),
                                                 "kind": "import", "entries": []})
        index = len(document["entries"]) + 1
        salt = salts.setdefault(source, uuid4().hex[:8])
        document["entries"].append(_import_entry(source, index, str(item["text"]), salt))
    return documents


def _import_entry(source: str, index: int, text: str, salt: str) -> dict[str, Any]:
    """Build an import chunk entry with an id that cannot collide on re-upload."""
    digest = hashlib.sha1(f"{source}:{index}:{salt}".encode()).hexdigest()[:12]
    return {"id": f"FILE-{digest}-{index:04d}", "title": source, "text": text,
            "steps": [], "enabled": True, "updated_at": _now()}


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------

def _empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "updated_at": _now(), "documents": {}}


def _normalize_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    entry_id = str(raw.get("id") or "").strip()
    text = str(raw.get("text") or "")
    if not entry_id or not text.strip():
        return None
    steps = raw.get("steps") or []
    if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
        steps = []
    return {"id": entry_id, "title": str(raw.get("title") or entry_id), "text": text,
            "steps": list(steps), "enabled": bool(raw.get("enabled", True)),
            "updated_at": str(raw.get("updated_at") or _now())}


def _normalize_store(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    raw_documents = raw.get("documents")
    if not isinstance(raw_documents, dict):
        return None
    documents: dict[str, dict[str, Any]] = {}
    for name, document in raw_documents.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(document, dict):
            return None
        raw_entries = document.get("entries")
        if not isinstance(raw_entries, list):
            return None
        entries = []
        for raw_entry in raw_entries:
            entry = _normalize_entry(raw_entry)
            if entry is None:
                return None
            entries.append(entry)
        kind = str(document.get("kind") or "manual")
        documents[name] = {"title": str(document.get("title") or name),
                           "kind": kind if kind in ENTRY_KINDS else "manual",
                           "entries": entries}
    return {"version": STORE_VERSION, "updated_at": str(raw.get("updated_at") or _now()),
            "documents": documents}


def _remember(path: Path, data: dict[str, Any]) -> None:
    try:
        stat = path.stat()
    except OSError:
        _CACHE.pop(str(path), None)
        return
    _CACHE[str(path)] = (stat.st_mtime_ns, stat.st_size, data)


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = STORE_VERSION
    data["updated_at"] = _now()
    temporary = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _save(data: dict[str, Any]) -> None:
    path = store_path()
    _write(path, data)
    _remember(path, data)


def _load() -> dict[str, Any]:
    """Return the parsed store, seeding or repairing it when necessary."""
    path = store_path()
    with _LOCK:
        try:
            stat = path.stat()
        except OSError:
            return _seed(path)
        cached = _CACHE.get(str(path))
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning("RAG 管理库损坏，将从源文件重新生成：%s", exc)
            return _seed(path)
        data = _normalize_store(raw)
        if data is None:
            logger.warning("RAG 管理库格式无效，将从源文件重新生成")
            return _seed(path)
        _remember(path, data)
        return data


def _seed(path: Path) -> dict[str, Any]:
    data = _empty_store()
    data["documents"] = _seed_documents(source_root())
    _write(path, data)
    _remember(path, data)
    logger.info("RAG 管理库已从源文件初始化：%d 个文档 / %d 个条目",
                len(data["documents"]), _stats(data)["entries"])
    return data


def ensure_seeded() -> None:
    """Create the store from source files on first access (idempotent)."""
    _load()


# ---------------------------------------------------------------------------
# Index synchronisation
# ---------------------------------------------------------------------------

def sync_index() -> dict[str, Any]:
    """Rebuild Chroma from the managed store; never raise into a CRUD call."""
    try:
        from src.rag.vector_store import get_cached_store
        from src.rag.vector_store import load_documents as vector_load_documents
        documents = vector_load_documents()
        if not documents:
            return {"indexed": False, "count": 0, "error": "知识库为空，无法重建索引"}
        count = get_cached_store().rebuild(documents)
        return {"indexed": True, "count": count}
    except Exception as exc:  # embedding model/index unavailable: keep data usable
        logger.warning("RAG 索引重建失败（数据已保存）：%s", exc)
        return {"indexed": False, "count": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------

def load_documents(root: Path = ROOT) -> list[dict[str, Any]]:
    """Flatten enabled managed entries into indexable documents.

    ``root`` other than the project root keeps the legacy file-only behaviour
    (tests/test_rag.py relies on this for isolated corpora).
    """
    if Path(root).resolve() != ROOT.resolve():
        return load_source_documents(Path(root))
    data = _load()
    flattened: list[dict[str, Any]] = []
    for document, doc in data["documents"].items():
        for entry in doc["entries"]:
            if not entry.get("enabled", True):
                continue
            flattened.append({"id": entry["id"], "text": entry["text"], "source": document,
                              "title": entry["title"], "steps": list(entry["steps"])})
    return flattened


def _stats(data: dict[str, Any]) -> dict[str, int]:
    entries = [entry for doc in data["documents"].values() for entry in doc["entries"]]
    return {"documents": len(data["documents"]), "entries": len(entries),
            "enabled": sum(1 for entry in entries if entry.get("enabled", True))}


def stats() -> dict[str, int]:
    return _stats(_load())


def list_documents() -> list[dict[str, Any]]:
    data = _load()
    documents = []
    for name in sorted(data["documents"]):
        doc = data["documents"][name]
        entries = doc["entries"]
        documents.append({
            "name": name,
            "title": doc["title"],
            "kind": doc["kind"],
            "entry_count": len(entries),
            "enabled_count": sum(1 for entry in entries if entry.get("enabled", True)),
            "updated_at": max((entry["updated_at"] for entry in entries), default=""),
        })
    return documents


def _entry_out(document: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {"id": entry["id"], "document": document, "title": entry["title"],
            "text": entry["text"], "steps": list(entry["steps"]),
            "enabled": bool(entry.get("enabled", True)), "updated_at": entry["updated_at"]}


def list_entries(document: str | None = None, query: str = "") -> list[dict[str, Any]]:
    """Return entries newest-first, optionally filtered by document/keyword."""
    data = _load()
    document_filter = str(document or "").strip()
    needle = str(query or "").strip().lower()
    entries = []
    for name, doc in data["documents"].items():
        if document_filter and name != document_filter:
            continue
        for entry in doc["entries"]:
            if needle:
                haystack = f"{name} {entry['title']} {entry['text']}".lower()
                if needle not in haystack:
                    continue
            entries.append(_entry_out(name, entry))
    entries.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    return entries[:500]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _find_entry(data: dict[str, Any], entry_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    for name, doc in data["documents"].items():
        for entry in doc["entries"]:
            if entry["id"] == entry_id:
                return name, doc, entry
    raise NotFoundError("条目不存在")


def _clean_steps(steps: Any) -> list[str]:
    if steps is None:
        return []
    if not isinstance(steps, (list, tuple)) or not all(isinstance(step, str) for step in steps):
        raise StoreError("steps 必须是字符串数组")
    return [step.strip() for step in steps if step.strip()]


def create_entry(document: str, title: str = "", text: str = "",
                 steps: Any = None, enabled: bool = True) -> dict[str, Any]:
    document = str(document or "").strip()
    if not document:
        raise StoreError("文档名不能为空")
    body = str(text or "").strip()
    if not body:
        raise StoreError("条目内容不能为空")
    cleaned_steps = _clean_steps(steps)
    with _LOCK:
        data = _load()
        doc = data["documents"].get(document)
        if doc is None:
            doc = {"title": document, "kind": "manual", "entries": []}
            data["documents"][document] = doc
        entry_id = f"MAN-{uuid4().hex[:12]}"
        existing_ids = {item["id"] for item_doc in data["documents"].values() for item in item_doc["entries"]}
        while entry_id in existing_ids:
            entry_id = f"MAN-{uuid4().hex[:12]}"
        entry = {"id": entry_id, "title": str(title or "").strip() or body.splitlines()[0][:60],
                 "text": body, "steps": cleaned_steps, "enabled": bool(enabled), "updated_at": _now()}
        doc["entries"].append(entry)
        _save(data)
        result_entry = _entry_out(document, entry)
        result_stats = _stats(data)
    sync = sync_index()
    return {"entry": result_entry, "stats": result_stats, "indexed": bool(sync.get("indexed"))}


def update_entry(entry_id: str, title: str | None = None, text: str | None = None,
                 steps: Any = None, enabled: bool | None = None) -> dict[str, Any]:
    entry_id = str(entry_id or "").strip()
    if not entry_id:
        raise StoreError("条目 id 不能为空")
    with _LOCK:
        data = _load()
        document, _doc, entry = _find_entry(data, entry_id)
        if title is not None:
            cleaned_title = str(title).strip()
            if cleaned_title:
                entry["title"] = cleaned_title
        if text is not None:
            body = str(text).strip()
            if not body:
                raise StoreError("条目内容不能为空")
            entry["text"] = body
        if steps is not None:
            entry["steps"] = _clean_steps(steps)
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        entry["updated_at"] = _now()
        _save(data)
        result_entry = _entry_out(document, entry)
        result_stats = _stats(data)
    sync = sync_index()
    return {"entry": result_entry, "stats": result_stats, "indexed": bool(sync.get("indexed"))}


def delete_entry(entry_id: str) -> dict[str, Any]:
    entry_id = str(entry_id or "").strip()
    if not entry_id:
        raise StoreError("条目 id 不能为空")
    with _LOCK:
        data = _load()
        document, doc, entry = _find_entry(data, entry_id)
        doc["entries"] = [item for item in doc["entries"] if item["id"] != entry_id]
        if not doc["entries"]:
            data["documents"].pop(document, None)
        _save(data)
        result_stats = _stats(data)
    sync = sync_index()
    return {"deleted": True, "document": document, "stats": result_stats,
            "indexed": bool(sync.get("indexed"))}


def move_entry(entry_id: str, target_document: str) -> dict[str, Any]:
    entry_id = str(entry_id or "").strip()
    target = str(target_document or "").strip()
    if not entry_id:
        raise StoreError("条目 id 不能为空")
    if not target:
        raise StoreError("目标文档不能为空")
    with _LOCK:
        data = _load()
        document, doc, entry = _find_entry(data, entry_id)
        if document == target:
            return {"entry": _entry_out(document, entry), "stats": _stats(data), "indexed": False}
        doc["entries"] = [item for item in doc["entries"] if item["id"] != entry_id]
        if not doc["entries"]:
            data["documents"].pop(document, None)
        target_doc = data["documents"].get(target)
        if target_doc is None:
            target_doc = {"title": target, "kind": "manual", "entries": []}
            data["documents"][target] = target_doc
        entry["updated_at"] = _now()
        target_doc["entries"].append(entry)
        _save(data)
        result_entry = _entry_out(target, entry)
        result_stats = _stats(data)
    sync = sync_index()
    return {"entry": result_entry, "stats": result_stats, "indexed": bool(sync.get("indexed"))}


def remove_document(document: str) -> dict[str, Any]:
    document = str(document or "").strip()
    if not document:
        raise StoreError("文档名不能为空")
    with _LOCK:
        data = _load()
        if document not in data["documents"]:
            raise NotFoundError("文档不存在")
        removed = len(data["documents"].pop(document)["entries"])
        _save(data)
        result_stats = _stats(data)
    sync = sync_index()
    return {"deleted": True, "document": document, "removed": removed,
            "stats": result_stats, "indexed": bool(sync.get("indexed"))}


def import_file(path: Path) -> dict[str, Any]:
    """Parse one uploaded file into managed entries, replacing same-name ones.

    Raises ``EmptyTextError`` before touching the store when no text can be
    extracted, so a broken upload never replaces a working document.
    """
    path = Path(path)
    from src.rag.ingest import SUPPORTED, documents_from_file
    if path.suffix.lower() not in SUPPORTED:
        raise StoreError("仅支持 PDF、DOCX、MD、TXT")
    try:
        chunks = documents_from_file(path)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise StoreError(f"无法解析该文件：{exc}") from exc
    chunks = [chunk for chunk in chunks if str(chunk.get("text", "")).strip()]
    if not chunks:
        raise EmptyTextError(EMPTY_TEXT_MESSAGE)
    document = path.name
    with _LOCK:
        data = _load()
        replaced = len(data["documents"].get(document, {}).get("entries", []))
        salt = uuid4().hex[:8]
        entries = [_import_entry(document, index, str(chunk["text"]), salt)
                   for index, chunk in enumerate(chunks, 1)]
        data["documents"][document] = {"title": path.stem, "kind": "import", "entries": entries}
        _save(data)
        result_stats = _stats(data)
    sync = sync_index()
    return {"document": document, "chunks": len(entries), "replaced": replaced,
            "stats": result_stats, "indexed": bool(sync.get("indexed"))}


def remove_file(path: Path) -> dict[str, Any]:
    """Drop the managed entries that came from ``path`` and rebuild the index."""
    document = Path(path).name
    with _LOCK:
        data = _load()
        removed = len(data["documents"].pop(document, {}).get("entries", []))
        if removed:
            _save(data)
        result_stats = _stats(data)
    if not removed:
        return {"document": document, "removed": 0, "stats": result_stats, "indexed": False}
    sync = sync_index()
    return {"document": document, "removed": removed, "stats": result_stats,
            "indexed": bool(sync.get("indexed"))}
