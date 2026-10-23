"""Contract for the RAG knowledge-entry splitting agent and its rule fallback."""
from __future__ import annotations

import pytest

from src.rag.knowledge_agent import (
    KnowledgeEntry,
    SplitError,
    split_document,
    split_text_rule,
)

DOCX_SAMPLE = """1. 服务器/网站无法访问
现象：无法通过网络访问服务器或网站。
排查与解决：检查服务器是否开机、网络是否连通（ping）；确认 Web 服务端口（如 80）是否监听（netstat -ntulp | grep 80）；检查防火墙规则是否放行。
2. 数据库连接失败
现象：应用程序报错无法连接到数据库。
排查与解决：确认数据库服务进程是否正在运行；核对连接字符串（IP、端口、用户名、密码）是否正确。
"""

PDF_SAMPLE = """11. 文件系统变为只读（Read-only）
现象：无法写入文件，提示“Read-only file system”。
排查与解决：通常由文件系统损坏或 /etc/fstab 配置错误引起。首先尝试
重启看是否恢复；若无效，需进入单用户/救援模式，对相应分区执行 fsck 修复。
12. 已删除文件仍占用空间
现象：df -h 显示磁盘满，但 du 统计并不大。
排查与解决：通常有进程持有已删除文件的句柄，使用 lsof | grep deleted 查找。
"""


class FakeClient:
    """Minimal LLM client double: returns a preset payload or raises."""

    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def json_completion(self, messages, retries=3):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.payload


def test_rule_split_one_entry_per_numbered_section():
    entries = split_text_rule(DOCX_SAMPLE)

    assert [entry.title for entry in entries] == ["服务器/网站无法访问", "数据库连接失败"]
    assert all("现象：" in entry.text for entry in entries)
    assert entries[0].steps
    assert "检查服务器是否开机" in entries[0].steps[0]
    assert entries[0].structural is True


def test_rule_split_joins_pdf_wrapped_lines():
    entries = split_text_rule(PDF_SAMPLE)

    assert [entry.title for entry in entries] == ["文件系统变为只读（Read-only）", "已删除文件仍占用空间"]
    assert "首先尝试重启看是否恢复" in entries[0].text
    assert "重启看是否恢复；若无效" in entries[0].text
    assert entries[0].text.count("\n") >= 1


def test_rule_split_does_not_treat_step_lists_as_sections():
    text = "1. 重启 VPN 客户端\n2. 检查账号权限\n3. 重试连接"
    entries = split_text_rule(text)

    assert len(entries) == 1
    assert entries[0].steps == ["重启 VPN 客户端", "检查账号权限", "重试连接"]


def test_rule_split_markdown_headings():
    text = "# VPN 连接失败\n\n现象：客户端一直提示超时。\n排查与解决：重启客户端并检查网络。\n\n# 邮箱无法登录\n\n现象：提示密码错误。"
    entries = split_text_rule(text, title_hint="运维手册")

    assert [entry.title for entry in entries] == ["VPN 连接失败", "邮箱无法登录"]
    assert "重启客户端" in entries[0].text


def test_rule_split_long_unstructured_text_is_chunked():
    text = "这是一个没有标题的故障说明。" * 40
    entries = split_text_rule(text, title_hint="故障说明", max_entry_chars=120)

    assert len(entries) > 1
    assert all(len(entry.text) <= 120 for entry in entries)
    base = entries[0].title.rsplit("（", 1)[0]
    assert "故障说明" in base
    assert all(entry.title.startswith(base) for entry in entries)


def test_agent_split_uses_validated_json():
    first, second = DOCX_SAMPLE.split("2. 数据库连接失败", 1)
    client = FakeClient({"results": [
        {"title": "服务器/网站无法访问排查", "text": first.strip(),
         "steps": ["检查服务器是否开机", "确认端口是否监听"]},
        {"title": "数据库连接失败排查", "text": f"2. 数据库连接失败{second}".strip(),
         "steps": ["确认数据库进程正在运行"]},
    ]})

    result = split_document(DOCX_SAMPLE, title_hint="运维手册", mode="agent", client=client)

    assert result.mode == "agent"
    assert result.warnings == []
    assert len(result.entries) == 2
    assert result.entries[0].title == "服务器/网站无法访问排查"
    assert client.calls and "文档入库智能体" in client.calls[0][0]["content"]


def test_agent_split_retries_invalid_payload_then_falls_back():
    client = FakeClient({"not_results": []})

    result = split_document(DOCX_SAMPLE, mode="auto", client=client)

    assert result.mode == "rule"
    assert result.warnings and "回退" in result.warnings[0]
    assert [entry.title for entry in result.entries] == ["服务器/网站无法访问", "数据库连接失败"]


def test_agent_mode_surfaces_failure_instead_of_falling_back():
    client = FakeClient(error=RuntimeError("LLM_API_KEY 未设置"))

    with pytest.raises(SplitError):
        split_document(DOCX_SAMPLE, mode="agent", client=client)


def test_agent_output_is_checked_against_structural_sections():
    # The model only returns the first section: the coverage guard must add
    # the missing numbered section back so no knowledge is silently lost.
    client = FakeClient({"results": [
        {"title": "服务器无法访问", "text": DOCX_SAMPLE.split("2. 数据库连接失败")[0],
         "steps": ["检查服务器是否开机"]},
    ]})

    result = split_document(DOCX_SAMPLE, mode="agent", client=client)

    assert result.mode == "agent"
    assert result.warnings and "补回" in result.warnings[0]
    assert any("数据库连接失败" in entry.title for entry in result.entries)


def test_agent_entries_are_deduped_and_cleaned():
    duplicated = {"results": [
        {"title": "VPN 连接失败", "text": "现象：连接超时。排查与解决：重启客户端。", "steps": ["重启客户端"]},
        {"title": "VPN 连接失败", "text": "现象：连接超时。排查与解决：重启客户端。", "steps": None},
    ]}
    result = split_document("现象：连接超时。排查与解决：重启客户端。", mode="agent",
                            client=FakeClient(duplicated))

    assert len(result.entries) == 1
    assert result.entries[0].steps == ["重启客户端"]


def test_empty_text_returns_no_entries():
    assert split_text_rule("   \n\n ") == []
    assert split_document("   ").entries == []


def test_knowledge_entry_serializes_without_internal_flag():
    entry = KnowledgeEntry(title="标题", text="正文", steps=["一步"], structural=True)
    assert entry.as_dict() == {"title": "标题", "text": "正文", "steps": ["一步"]}
