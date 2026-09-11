"""Intent detection for the conversational Helpdesk entry point."""
from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.llm_client import LLMClient

logger = logging.getLogger(__name__)
IntentName = Literal["greeting", "ticket", "troubleshooting", "follow_up", "general"]


class IntentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: IntentName
    confidence: float = Field(ge=0, le=1)
    reply_mode: Literal["conversation", "rag", "ticket_form"]


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[IntentItem]


def _rule_intent(message: str) -> IntentItem | None:
    text = message.strip().lower()
    if not text:
        return None
    if any(word in text for word in ("提交工单", "创建工单", "报修", "转人工", "开工单")):
        return IntentItem(intent="ticket", confidence=0.99, reply_mode="ticket_form")
    if any(word in text for word in ("你好", "您好", "嗨", "hello", "hi", "谢谢", "感谢")):
        return IntentItem(intent="greeting", confidence=0.98, reply_mode="conversation")
    if any(word in text for word in ("还是不行", "仍然", "依旧", "继续报错", "刚才")):
        return IntentItem(intent="follow_up", confidence=0.9, reply_mode="rag")
    if any(word in text for word in ("报错", "无法", "不能", "失败", "连接", "密码", "网络", "vpn", "邮箱", "打印")):
        return IntentItem(intent="troubleshooting", confidence=0.88, reply_mode="rag")
    return None


def detect_intent(message: str, client: LLMClient | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return a validated top-level ``results`` object without crashing."""
    rule = _rule_intent(message)
    if rule:
        return IntentResult(results=[rule]).model_dump()
    llm = client or LLMClient()
    messages = [
        {"role": "system", "content": """你是 Helpdesk 对话意图识别器。只输出 JSON 对象，不要输出 Markdown。
顶层必须是 {\"results\":[{\"intent\":...,\"confidence\":0到1,\"reply_mode\":...}]}。
intent 只能是 greeting、ticket、troubleshooting、follow_up、general；reply_mode 只能是 conversation、rag、ticket_form。
只有用户明确要求提交/创建/报修/转人工时才使用 ticket_form；故障排查使用 rag；问候、闲聊和一般咨询使用 conversation。"""},
        {"role": "user", "content": message},
    ]
    for _ in range(3):
        try:
            return IntentResult.model_validate(llm.json_completion(messages, retries=1)).model_dump()
        except (ValidationError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("意图识别校验失败：%s", exc)
    return IntentResult(results=[IntentItem(intent="general", confidence=0.3, reply_mode="conversation")]).model_dump()
