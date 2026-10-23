"""专门用于「把文档导入 RAG 知识库」的智能体工具。

这个工具把完整流程封装成一个可被智能体/脚本调用的动作：

1. 解析 PDF / DOCX / Markdown / TXT 文本；
2. 调用 :mod:`src.rag.knowledge_agent` 把文档拆成一条条独立知识条
   （默认优先 LLM 智能体，失败自动回退规则拆分）；
3. 写入 ``data/knowledge/managed_entries.json`` 并原子替换同名文档；
4. 重建 Chroma 向量索引（可关闭）。

两种用法：

* 智能体函数调用：把 :data:`RAG_IMPORT_TOOL` 作为 tool schema 注册给模型，
  模型返回参数后交给 :func:`call_rag_import_tool` 执行。
* 命令行：

  .. code-block:: powershell

     .venv\\Scripts\\python.exe scripts\\import_rag.py "C:\\path\\doc.pdf" --dry-run
     .venv\\Scripts\\python.exe scripts\\import_rag.py "C:\\path\\doc.pdf" --mode agent
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.llm_client import ROOT
from src.rag.ingest import SUPPORTED, extract_text
from src.rag.knowledge_agent import KnowledgeIngestAgent, SplitError, split_document

TOOL_NAME = "import_document_to_rag"

RAG_IMPORT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "把一份 IT 运维文档（PDF/DOCX/MD/TXT）拆分成独立知识条并导入 RAG 知识库。"
            "文档会被解析、按语义拆成多个知识条（每条含标题、正文、可执行步骤），"
            "同名文档会被整体替换，并自动重建向量索引。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要导入的文档路径（绝对路径或相对仓库根目录的路径）。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "agent", "rule"],
                    "description": (
                        "拆分模式：auto=优先 AI 智能体、失败回退规则；"
                        "agent=只用 AI 智能体（失败报错）；rule=只用规则拆分。默认 auto。"
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "为 true 时只预览拆分结果，不写入知识库、不重建索引。",
                },
                "rebuild_index": {
                    "type": "boolean",
                    "description": "写入后是否立即重建向量索引，默认 true。",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


def _resolve_path(raw: str) -> Path:
    path = Path(str(raw or "").strip())
    if not path.is_absolute():
        path = ROOT / path
    return path


def preview_document(path: str | Path, *, mode: str | None = None,
                     config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Split a document and return the knowledge entries without saving them."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError("仅支持 PDF、DOCX、MD、TXT")
    text = extract_text(path)
    if not text.strip():
        raise ValueError("未能从该文件提取到文本（可能是扫描版 PDF），请改用可复制文本的文件")
    result = split_document(text, title_hint=path.stem, mode=mode, config=config)
    return {"document": path.name, "title": path.stem, "mode": result.mode,
            "warnings": list(result.warnings), "entry_count": len(result.entries),
            "entries": [entry.as_dict() for entry in result.entries]}


def import_document_to_rag(path: str | Path, *, mode: str | None = None,
                           rebuild_index: bool = True,
                           config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Split a document into knowledge entries and persist it to the RAG store."""
    path = Path(path)
    from src.rag import managed_store
    result = managed_store.import_file(path, mode=mode, rebuild=rebuild_index)
    return {"saved": True, "path": str(path), **result}


def call_rag_import_tool(arguments: dict[str, Any],
                         config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute one function-call payload for :data:`RAG_IMPORT_TOOL`."""
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")
    raw_path = str(arguments.get("path") or "").strip()
    if not raw_path:
        raise ValueError("path 不能为空")
    path = _resolve_path(raw_path)
    if not path.is_file():
        raise ValueError(f"文件不存在：{path}")
    mode = arguments.get("mode")
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in {"auto", "agent", "rule"}:
            raise ValueError("mode 必须为 auto、agent 或 rule")
    dry_run = bool(arguments.get("dry_run", False))
    rebuild_index = bool(arguments.get("rebuild_index", True))
    if dry_run:
        return preview_document(path, mode=mode, config=config)
    return import_document_to_rag(path, mode=mode, rebuild_index=rebuild_index, config=config)


def run_knowledge_ingest_agent(path: str | Path, *, mode: str | None = None,
                               config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the intake agent only (no store write), useful in tests/debugging."""
    agent = KnowledgeIngestAgent(config=config, mode=mode)
    path = Path(path)
    result = agent.split(extract_text(path), title_hint=path.stem)
    return {"document": path.name, "mode": result.mode,
            "warnings": list(result.warnings),
            "entries": [entry.as_dict() for entry in result.entries]}


__all__ = [
    "RAG_IMPORT_TOOL",
    "TOOL_NAME",
    "call_rag_import_tool",
    "import_document_to_rag",
    "preview_document",
    "run_knowledge_ingest_agent",
    "SplitError",
]
