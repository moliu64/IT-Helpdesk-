"""命令行入口：把文档拆成知识条并导入 RAG 知识库。

示例::

    # 只看拆分结果，不落库（推荐先验证拆分质量）
    .venv\\Scripts\\python.exe scripts\\import_rag.py "C:\\Users\\me\\Desktop\\运维手册.pdf" --dry-run

    # 用 AI 智能体拆分并导入、重建索引
    .venv\\Scripts\\python.exe scripts\\import_rag.py "C:\\Users\\me\\Desktop\\运维手册.pdf" --mode agent

    # 只用规则拆分（不调用模型），或写入后不重建索引
    .venv\\Scripts\\python.exe scripts\\import_rag.py notes.docx --mode rule --no-rebuild

``--store`` / ``--index-dir`` 可配合 ``HELPDESK_RAG_SOURCE_ROOT`` 在隔离环境
中验证，避免污染生产知识库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rag.import_tool import import_document_to_rag, preview_document  # noqa: E402
from src.rag.knowledge_agent import SPLIT_MODES  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 PDF/DOCX/MD/TXT 文档拆分成知识条并导入 RAG 知识库。",
    )
    parser.add_argument("files", nargs="+", help="一个或多个文档路径")
    parser.add_argument("--mode", choices=sorted(SPLIT_MODES), default=None,
                        help="拆分模式：auto（默认，AI 优先/规则兜底）、agent、rule")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览拆分结果，不写入知识库、不重建索引")
    parser.add_argument("--no-rebuild", action="store_true", help="写入后不重建向量索引")
    parser.add_argument("--store", default=None,
                        help="覆盖知识条存储文件（等价 HELPDESK_RAG_STORE）")
    parser.add_argument("--index-dir", default=None,
                        help="覆盖 Chroma 索引目录（等价 HELPDESK_RAG_INDEX_DIR）")
    parser.add_argument("--source-root", default=None,
                        help="种子语料根目录（等价 HELPDESK_RAG_SOURCE_ROOT）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果（便于脚本消费）")
    return parser


def _apply_env(args: argparse.Namespace) -> None:
    for env_name, value in (
        ("HELPDESK_RAG_STORE", args.store),
        ("HELPDESK_RAG_INDEX_DIR", args.index_dir),
        ("HELPDESK_RAG_SOURCE_ROOT", args.source_root),
    ):
        if value:
            os.environ[env_name] = str(value)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _apply_env(args)
    reports: list[dict] = []
    failures = 0
    for raw in args.files:
        path = Path(raw)
        try:
            if not path.is_file():
                raise ValueError(f"文件不存在：{path}")
            if args.dry_run:
                report = preview_document(path, mode=args.mode)
                if not args.json:
                    print(f"[预览] {path.name}：{report['entry_count']} 条知识条 "
                          f"(mode={report['mode']})")
            else:
                report = import_document_to_rag(path, mode=args.mode,
                                                rebuild_index=not args.no_rebuild)
                if not args.json:
                    print(f"[导入] {path.name}：{report['chunks']} 条知识条 "
                          f"(mode={report['mode']}, indexed={report['indexed']}, "
                          f"replaced={report['replaced']})")
            reports.append(report)
        except Exception as exc:  # noqa: BLE001 - CLI must report all failures
            failures += 1
            message = f"{path}: {exc}"
            reports.append({"document": path.name, "error": str(exc)})
            if not args.json:
                print(f"[失败] {message}", file=sys.stderr)
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
