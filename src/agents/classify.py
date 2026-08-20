"""Helpdesk ticket classification with schema validation and retries."""
from __future__ import annotations
import logging
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from src.agents.prompts import classify_messages
from src.llm_client import LLMClient, load_config

logger = logging.getLogger(__name__)

class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    subcategory: str
    confidence: float = Field(ge=0.0, le=1.0)

class ClassificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[Classification]

def classify_ticket(ticket: dict[str, Any], client: LLMClient | None = None,
                    config: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    settings = config or load_config()
    categories = settings.get("helpdesk", {}).get("categories", [])
    if not categories:
        logger.error("分类失败：config.helpdesk.categories 为空")
        return {"results": []}
    llm = client or LLMClient(settings)
    messages = classify_messages(ticket, categories)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            payload = llm.json_completion(messages, retries=1)
            validated = ClassificationResult.model_validate(payload)
            invalid = [item.category for item in validated.results if item.category not in categories]
            if invalid:
                raise ValueError(f"category 不在配置清单中: {invalid}")
            return validated.model_dump()
        except (ValidationError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            last_error = exc
            logger.warning("分类输出第 %d/3 次校验失败：%s", attempt, exc)
    logger.error("分类失败，返回空 results：%s", last_error)
    return {"results": []}
