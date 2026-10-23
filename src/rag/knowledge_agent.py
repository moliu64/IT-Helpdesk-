"""RAG 文档入库智能体：把一篇文档拆成一条条自包含的知识条。

背景
----
旧流程用固定字符窗口（900 字）切片，一篇文档会变成若干段“半截话”：
一个故障的“现象”和“排查”可能被切到两个条目里，标题也只是文件名，导致向量
检索召回的是碎片，而不是可独立回答问题的知识条。

本模块提供两种拆分策略：

``agent``
    调用 LLM（``src.agents.prompts.KNOWLEDGE_SPLIT_SYSTEM``），让模型按文档
    语义拆成知识条，并用 Pydantic 校验模型输出；模型不可用时由调用方决定
    是否回退。
``rule``
    不依赖模型的确定性兜底：识别 Markdown/中文/编号标题，按编号小节拆条，
    合并 PDF 换行，补全标题并抽取步骤。

``auto``（默认）
    先试 ``agent``；失败时回退 ``rule``，并在 ``SplitResult.warnings`` 中说明。
    即使模型调用成功，也会用规则拆分结果做覆盖校验，把模型漏掉的编号小节
    补回来，避免入库时丢内容。

配置（``config/config.yaml``）：:

    rag:
      ingest:
        mode: "auto"          # auto | agent | rule
        max_entry_chars: 1200 # 单条正文超过该长度时继续拆分
        window_chars: 6000    # 单次发给模型的最大字符数
        max_entries: 200      # 单篇文档最多知识条数

环境变量 ``HELPDESK_RAG_INGEST_MODE`` 可覆盖 ``mode``（测试/部署时使用）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.agents.prompts import knowledge_split_messages
from src.llm_client import LLMClient, load_config

logger = logging.getLogger(__name__)

SPLIT_MODES = {"auto", "agent", "rule"}
DEFAULT_MODE = "auto"
MODE_ENV = "HELPDESK_RAG_INGEST_MODE"
DEFAULT_MAX_ENTRY_CHARS = 1200
DEFAULT_WINDOW_CHARS = 6000
DEFAULT_MAX_ENTRIES = 200

# 文档结构识别
_HEADING_MARKDOWN = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_HEADING_NUMBERED = re.compile(r"^(\d{1,3})[.、．)）]\s*(.+?)\s*$")
_HEADING_CN = re.compile(
    r"^(?:第[一二三四五六七八九十百零]+[章节部分篇]"
    r"|[一二三四五六七八九十百]+[、.．]"
    r"|[（(][一二三四五六七八九十百]+[)）])\s*(.+?)\s*$"
)
_LABEL_PREFIX = re.compile(
    r"^(现象|问题|故障|原因|根因|排查与解决|排查|分析|解决|处理|步骤|方法|操作"
    r"|注意|说明|影响|风险|建议|验证|结果|结论)\s*[:：]"
)
_STEP_LINE = re.compile(
    r"^(?:步骤\s*[一二三四五六七八九十\d]*\s*[:：]?\s*|[-*•·]\s*|[①-⑳]\s*|\d{1,2}[.、)）]\s*)"
)
_CAUSE_ONLY = re.compile(r"^(通常|一般|可能|由于|因为|往往|多数|主要(?:原因)?|常见原因|主要是)")
_MEANINGLESS_TITLE = re.compile(r"^(问题|故障|说明|其他|概述|知识条|文档|小节|内容|示例)\s*\d*$")
_STEP_LABELS = {"排查与解决", "排查", "解决", "处理", "步骤", "方法", "操作"}

_SPLIT_SENTENCE = re.compile(r"(?<=[。；;！!？?\n])")


class SplitError(RuntimeError):
    """Raised when the requested splitting strategy cannot produce entries."""


@dataclass
class KnowledgeEntry:
    """A single, self-contained knowledge entry ready for the managed store."""

    title: str
    text: str
    steps: list[str] = field(default_factory=list)
    # True when the entry comes from a detected document section heading.  The
    # flag is internal (never persisted) and drives the agent-coverage guard.
    structural: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "text": self.text, "steps": list(self.steps)}


@dataclass
class SplitResult:
    """Result of one document split."""

    entries: list[KnowledgeEntry]
    mode: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "warnings": list(self.warnings),
                "entries": [entry.as_dict() for entry in self.entries]}


class _AgentEntry(BaseModel):
    """One entry as returned by the model (validated before it can be saved)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1)
    steps: list[str] = Field(default_factory=list)

    @field_validator("title", "text", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None and str(item).strip()]
        return []


class _AgentPayload(BaseModel):
    """Top-level object contract: ``{"results": [...]}`` (never a bare array)."""

    model_config = ConfigDict(extra="ignore")

    results: list[_AgentEntry]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = config
    if settings is None:
        try:
            settings = load_config()
        except (OSError, ValueError) as exc:  # missing/broken config: safe defaults
            logger.warning("读取 RAG 入库配置失败，使用默认值：%s", exc)
            settings = {}
    ingest = ((settings.get("rag") or {}).get("ingest") or {}) if isinstance(settings, dict) else {}

    def _int(name: str, default: int) -> int:
        try:
            return max(1, int(ingest.get(name, default)))
        except (TypeError, ValueError):
            return default

    mode = str(os.getenv(MODE_ENV, ingest.get("mode", DEFAULT_MODE)) or DEFAULT_MODE).strip().lower()
    if mode not in SPLIT_MODES:
        logger.warning("未知的知识条拆分模式 %r，已按 auto 处理", mode)
        mode = DEFAULT_MODE
    return {
        "mode": mode,
        "max_entry_chars": _int("max_entry_chars", DEFAULT_MAX_ENTRY_CHARS),
        "window_chars": _int("window_chars", DEFAULT_WINDOW_CHARS),
        "max_entries": _int("max_entries", DEFAULT_MAX_ENTRIES),
    }


# ---------------------------------------------------------------------------
# Text normalization helpers
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_cjk(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
            or 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF)


def _join_text(left: str, right: str) -> str:
    """Join wrapped lines without inserting spaces inside Chinese text.

    A space is kept between an ASCII word and a Chinese character (e.g.
    ``修复完成后reboot`` -> ``修复完成后 reboot``), which is how PDF wraps
    usually split mixed-language sentences.
    """
    if not left:
        return right
    if not right:
        return left
    left_cjk, right_cjk = _is_cjk(left[-1]), _is_cjk(right[0])
    if left_cjk and right_cjk:
        return left + right
    if (left[-1].isascii() and left[-1].isalnum()) or (right[0].isascii() and right[0].isalnum()):
        return f"{left} {right}"
    return left + right


def _normalize_key(text: str) -> str:
    """Whitespace/punctuation-insensitive key used for dedupe and coverage."""
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _paragraphs(text: str) -> list[str]:
    """Merge physical lines into logical paragraphs.

    PDF text wraps a sentence across several physical lines; DOCX often puts
    “现象/排查与解决” inside one paragraph separated by ``\\n``.  Both cases
    are normalized to one paragraph per logical block so headings and labels
    can be recognized reliably.
    """
    paragraphs: list[str] = []
    current = ""
    for raw_line in _normalize_text(text).split("\n"):
        line = raw_line.strip()
        if not line:
            if current:
                paragraphs.append(current)
                current = ""
            continue
        starts_block = bool(
            _LABEL_PREFIX.match(line)
            or _STEP_LINE.match(line)
            or _HEADING_MARKDOWN.match(line)
            or _HEADING_CN.match(line)
        )
        if starts_block and current:
            paragraphs.append(current)
            current = ""
        current = _join_text(current, line) if current else line
    if current:
        paragraphs.append(current)
    return paragraphs


def _heading_title(paragraphs: list[str], index: int) -> str | None:
    """Return a section title when ``paragraphs[index]`` is a section heading."""
    line = paragraphs[index]
    if not line:
        return None
    markdown = _HEADING_MARKDOWN.match(line)
    if markdown:
        return markdown.group(1).strip()
    chinese = _HEADING_CN.match(line)
    if chinese:
        return chinese.group(1).strip()
    numbered = _HEADING_NUMBERED.match(line)
    if numbered:
        # A numbered line counts as a section heading only when the next
        # paragraph is a symptom/diagnosis label (this is what troubleshooting
        # documents look like).  Otherwise it is a step in a procedure and must
        # stay inside the current entry.
        following = paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
        if _LABEL_PREFIX.match(following):
            return numbered.group(2).strip()
    return None


def _clean_title(raw: str, fallback: str = "") -> str:
    title = str(raw or "").strip()
    title = re.sub(r"^#{1,6}\s*", "", title)
    title = re.sub(r"^\d{1,3}[.、．)）]\s*", "", title)
    title = re.sub(r"^[（(]?[一二三四五六七八九十百]+[)）、.．]\s*", "", title)
    title = title.strip(" 　:：-—")
    if not title or _MEANINGLESS_TITLE.match(title):
        title = str(fallback or "").strip()
        title = re.sub(r"^#{1,6}\s*", "", title).strip(" 　:：-—")
    return (title or "知识条")[:80]


def _derive_title(text: str, fallback: str = "") -> str:
    """Derive a readable title from the first sentence of an unstructured block."""
    first = _SPLIT_SENTENCE.split(str(text or "").strip(), maxsplit=1)[0]
    first = re.sub(r"^(现象|问题|故障|原因|根因)\s*[:：]\s*", "", first.strip())
    first = first.strip(" 　，,、；;：:。！？!?")
    if len(first) > 40:
        first = first[:40].rstrip()
    return _clean_title(first, fallback or "知识条")


def _clean_step(step: str) -> str:
    cleaned = _STEP_LINE.sub("", str(step or ""), count=1).strip()
    return cleaned.strip(" 　。；;，,")


def _clean_steps(steps: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for step in steps:
        value = _clean_step(step)
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned[:30]


def _extract_steps(paragraphs: list[str]) -> list[str]:
    """Extract executable steps from explicit lists or a diagnosis paragraph."""
    explicit: list[str] = []
    for paragraph in paragraphs:
        if _LABEL_PREFIX.match(paragraph):
            continue
        if _STEP_LINE.match(paragraph):
            explicit.append(paragraph)
    if explicit:
        return _clean_steps(explicit)

    for paragraph in paragraphs:
        label = _LABEL_PREFIX.match(paragraph)
        if not label or label.group(1) not in _STEP_LABELS:
            continue
        content = paragraph[label.end():].strip()
        candidates = [item.strip(" 　。；;，,") for item in _SPLIT_SENTENCE.split(content)]
        steps = [item for item in candidates
                 if item and not _CAUSE_ONLY.match(item)]
        if steps:
            return _clean_steps(steps)
    return []


def _split_sentences(text: str, limit: int) -> list[str]:
    """Split text into sentence-ish pieces, hard-splitting anything too long."""
    pieces: list[str] = []
    for sentence in _SPLIT_SENTENCE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        for start in range(0, len(sentence), limit):
            piece = sentence[start:start + limit].strip()
            if piece:
                pieces.append(piece)
    return pieces


def _split_text_chunks(text: str, max_chars: int) -> list[str]:
    """Split a long body into chunks of at most ``max_chars`` characters."""
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    segments: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            segments.append(paragraph)
        else:
            segments.extend(_split_sentences(paragraph, max_chars))

    chunks: list[str] = []
    current = ""
    for segment in segments:
        candidate = f"{current}\n{segment}" if current else segment
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = segment
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


def _dedupe(entries: list[KnowledgeEntry]) -> list[KnowledgeEntry]:
    seen: set[str] = set()
    unique: list[KnowledgeEntry] = []
    for entry in entries:
        key = hashlib.sha1(_normalize_key(entry.text).encode("utf-8")).hexdigest()
        if not _normalize_key(entry.text) or key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


# ---------------------------------------------------------------------------
# Rule-based splitter (offline fallback)
# ---------------------------------------------------------------------------

def split_text_rule(text: str, *, title_hint: str = "",
                    max_entry_chars: int = DEFAULT_MAX_ENTRY_CHARS,
                    max_entries: int = DEFAULT_MAX_ENTRIES) -> list[KnowledgeEntry]:
    """Structure-aware, model-free splitting used for fallback/coverage."""
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return []

    heading_positions: list[int] = []
    heading_titles: dict[int, str] = {}
    for index in range(len(paragraphs)):
        title = _heading_title(paragraphs, index)
        if title:
            heading_positions.append(index)
            heading_titles[index] = title

    if not heading_positions:
        return _unstructured_entries(paragraphs, title_hint, max_entry_chars, max_entries)

    entries: list[KnowledgeEntry] = []
    intro = paragraphs[:heading_positions[0]]
    carry = ""
    if intro:
        intro_text = "\n".join(intro).strip()
        if len(_normalize_key(intro_text)) >= 80:
            intro_title = f"{title_hint}概述" if title_hint else "文档概述"
            entries.extend(_entries_from_body(intro_text, intro_title, intro, max_entry_chars))
        else:
            carry = intro_text

    for position, heading_index in enumerate(heading_positions):
        end = heading_positions[position + 1] if position + 1 < len(heading_positions) else len(paragraphs)
        body = paragraphs[heading_index + 1:end]
        if carry:
            body = [carry, *body]
            carry = ""
        body_text = "\n".join(body).strip()
        if not body_text:
            continue
        title = _clean_title(heading_titles[heading_index], title_hint)
        entries.extend(_entries_from_body(body_text, title, body, max_entry_chars,
                                          structural=True))
        if len(entries) >= max_entries:
            break
    return _dedupe(entries)[:max_entries]


def _entries_from_body(body_text: str, title: str, paragraphs: list[str],
                       max_entry_chars: int, *, structural: bool = False) -> list[KnowledgeEntry]:
    steps = _extract_steps(paragraphs)
    chunks = _split_text_chunks(body_text, max_entry_chars)
    entries: list[KnowledgeEntry] = []
    for index, chunk in enumerate(chunks, 1):
        chunk_title = title if len(chunks) == 1 else f"{title}（{index}/{len(chunks)}）"
        entries.append(KnowledgeEntry(title=chunk_title, text=chunk,
                                      steps=steps if index == 1 else [],
                                      structural=structural))
    return entries


def _unstructured_entries(paragraphs: list[str], title_hint: str,
                          max_entry_chars: int, max_entries: int) -> list[KnowledgeEntry]:
    body = "\n".join(paragraphs).strip()
    if not body:
        return []
    steps = _extract_steps(paragraphs)
    chunks = _split_text_chunks(body, max_entry_chars)
    base_title = _derive_title(chunks[0] if chunks else body, title_hint)
    entries: list[KnowledgeEntry] = []
    for index, chunk in enumerate(chunks, 1):
        title = base_title if len(chunks) == 1 else f"{base_title}（{index}/{len(chunks)}）"
        entries.append(KnowledgeEntry(title=title, text=chunk,
                                      steps=steps if index == 1 else []))
    return entries[:max_entries]


# ---------------------------------------------------------------------------
# Agent-based splitter (LLM)
# ---------------------------------------------------------------------------

def _windows(paragraphs: list[str], window_chars: int) -> list[list[str]]:
    windows: list[list[str]] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        length = len(paragraph) + 1
        if current and size + length > window_chars:
            windows.append(current)
            current, size = [], 0
        if length > window_chars:
            if current:
                windows.append(current)
                current, size = [], 0
            windows.extend([[piece] for piece in _split_text_chunks(paragraph, window_chars)])
            continue
        current.append(paragraph)
        size += length
    if current:
        windows.append(current)
    if len(windows) > 1 and len("\n".join(windows[-1])) < 200:
        windows[-2].extend(windows.pop())
    return windows or [paragraphs]


def _validate_payload(raw: Any) -> _AgentPayload:
    if not isinstance(raw, dict):
        raise ValueError("LLM JSON 顶层必须是对象")
    return _AgentPayload.model_validate(raw)


def _request_payload(client: LLMClient, messages: list[dict[str, str]]) -> _AgentPayload:
    """Call the model and validate its JSON, retrying once with an error hint."""
    errors: list[str] = []
    for attempt in range(2):
        try:
            raw = client.json_completion(messages, retries=1)
            return _validate_payload(raw)
        except (RuntimeError, ValidationError, ValueError) as exc:
            errors.append(str(exc))
            logger.warning("知识条智能体输出无效（第 %d 次）：%s", attempt + 1, exc)
            if attempt == 0:
                messages = [
                    *messages,
                    {"role": "user", "content": (
                        "上一次输出未通过校验：" + str(exc)[:300]
                        + "。请严格按系统要求重新输出一个 JSON 对象，顶层为 results 数组，"
                          "每项包含 title/text/steps。"
                    )},
                ]
    raise SplitError("知识条智能体调用失败：" + "；".join(errors[-2:]))


def _clean_agent_entry(entry: _AgentEntry, title_hint: str) -> KnowledgeEntry | None:
    text = re.sub(r"^\s*<document>|</document>\s*$", "", entry.text.strip()).strip()
    if not text:
        return None
    title = _clean_title(entry.title, _derive_title(text, title_hint))
    if _MEANINGLESS_TITLE.match(title) or title == title_hint:
        title = _derive_title(text, title_hint or "知识条")
    # Drop an accidental duplicated title line at the beginning of the body.
    if text.startswith(title) and len(text) > len(title):
        remainder = text[len(title):].lstrip(" 　:：-—\n")
        if remainder:
            text = remainder
    steps = _clean_steps(entry.steps)
    return KnowledgeEntry(title=title[:80], text=text, steps=steps)


def _agent_split(text: str, title_hint: str, settings: dict[str, Any],
                 client: LLMClient) -> list[KnowledgeEntry]:
    windows = _windows(_paragraphs(text), settings["window_chars"])
    entries: list[KnowledgeEntry] = []
    total = len(windows)
    for index, window in enumerate(windows, 1):
        messages = knowledge_split_messages(title_hint, "\n".join(window), index, total)
        payload = _request_payload(client, messages)
        for raw_entry in payload.results:
            entry = _clean_agent_entry(raw_entry, title_hint)
            if entry is not None:
                entries.append(entry)
        if len(entries) >= settings["max_entries"]:
            break
    if not entries:
        raise SplitError("模型未返回可用的知识条")
    return _dedupe(entries)[:settings["max_entries"]]


# ---------------------------------------------------------------------------
# Coverage guard / public API
# ---------------------------------------------------------------------------

def _coverage_missing(agent_entries: list[KnowledgeEntry],
                      rule_entries: list[KnowledgeEntry]) -> list[KnowledgeEntry]:
    """Return structural rule entries the agent failed to cover.

    A rule entry counts as covered when its body snippet or title appears in
    the agent output after whitespace/punctuation normalization.  This catches
    model truncation / skipped sections without duplicating content when the
    model merely rewrote a heading.
    """
    structural = [entry for entry in rule_entries if entry.structural]
    if not structural:
        return []
    haystack = _normalize_key("\n".join(f"{entry.title}\n{entry.text}" for entry in agent_entries))
    missing: list[KnowledgeEntry] = []
    for entry in structural:
        body_key = _normalize_key(entry.text)[:80]
        title_key = _normalize_key(entry.title)
        if (body_key and body_key in haystack) or (title_key and title_key in haystack):
            continue
        missing.append(entry)
    return missing


def split_document(text: str, *, title_hint: str = "", mode: str | None = None,
                   client: LLMClient | None = None,
                   config: dict[str, Any] | None = None) -> SplitResult:
    """Split one document into knowledge entries.

    ``mode`` overrides config/env.  ``auto`` never raises for model failures;
    ``agent`` surfaces them as :class:`SplitError` so batch/CLI callers can
    decide whether to retry or fail.
    """
    normalized = _normalize_text(text)
    if not normalized:
        return SplitResult(entries=[], mode="rule", warnings=[])

    settings = _settings(config)
    selected = str(mode or settings["mode"]).strip().lower()
    if selected not in SPLIT_MODES:
        raise ValueError(f"未知的知识条拆分模式：{selected}")

    if selected == "rule":
        entries = split_text_rule(normalized, title_hint=title_hint,
                                  max_entry_chars=settings["max_entry_chars"],
                                  max_entries=settings["max_entries"])
        return SplitResult(entries=entries, mode="rule", warnings=[])

    try:
        agent_client = client or LLMClient(config)
        agent_entries = _agent_split(normalized, title_hint, settings, agent_client)
        rule_entries = split_text_rule(normalized, title_hint=title_hint,
                                       max_entry_chars=settings["max_entry_chars"],
                                       max_entries=settings["max_entries"])
        missing = _coverage_missing(agent_entries, rule_entries)
        warnings: list[str] = []
        entries = list(agent_entries)
        if missing:
            warnings.append(f"AI 拆分遗漏 {len(missing)} 个结构化小节，已按规则补回")
            entries.extend(missing)
        return SplitResult(entries=_dedupe(entries)[:settings["max_entries"]],
                           mode="agent", warnings=warnings)
    except Exception as exc:
        if selected == "agent":
            raise SplitError(f"知识条智能体拆分失败：{exc}") from exc
        logger.warning("知识条智能体拆分失败，已回退规则拆分：%s", exc)
        entries = split_text_rule(normalized, title_hint=title_hint,
                                  max_entry_chars=settings["max_entry_chars"],
                                  max_entries=settings["max_entries"])
        reason = str(exc).strip()
        if len(reason) > 300:
            reason = reason[:300] + "…"
        return SplitResult(entries=entries, mode="rule",
                           warnings=[f"AI 拆分失败，已回退规则拆分：{reason}"])


class KnowledgeIngestAgent:
    """The RAG document-intake agent (prompt + validation + rule fallback)."""

    def __init__(self, *, client: LLMClient | None = None,
                 config: dict[str, Any] | None = None, mode: str | None = None):
        self._client = client
        self._config = config
        self._mode = mode

    def split(self, text: str, *, title_hint: str = "") -> SplitResult:
        return split_document(text, title_hint=title_hint, mode=self._mode,
                              client=self._client, config=self._config)


__all__ = [
    "DEFAULT_MAX_ENTRY_CHARS",
    "KnowledgeEntry",
    "KnowledgeIngestAgent",
    "MODE_ENV",
    "SPLIT_MODES",
    "SplitError",
    "SplitResult",
    "split_document",
    "split_text_rule",
]
