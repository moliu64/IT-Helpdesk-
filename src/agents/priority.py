"""Ticket priority and SLA assessment."""
from __future__ import annotations
import logging
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from src.agents.prompts import priority_messages
from src.llm_client import LLMClient, load_config

logger = logging.getLogger(__name__)

class PriorityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: Literal["P1", "P2", "P3", "P4"]
    sla_hours: int = Field(gt=0)
    impact_scope: str
    affected_users: int = Field(ge=0)
    business_blocked: bool
    reason: str

class PriorityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[PriorityItem]

def assess_priority(ticket: dict[str, Any], client: LLMClient | None = None,
                    config: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    settings = config or load_config()
    sla_map = settings.get("helpdesk", {}).get("sla_map", {})
    if not sla_map:
        logger.error("优先级评估失败：config.helpdesk.sla_map 为空")
        return {"results": []}
    llm = client or LLMClient(settings)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            payload = llm.json_completion(priority_messages(ticket, sla_map), retries=1)
            validated = PriorityResult.model_validate(payload)
            for item in validated.results:
                expected = sla_map.get(item.priority)
                if expected is None or item.sla_hours != expected:
                    raise ValueError(f"{item.priority} 的 SLA 应为配置值 {expected}")
            return validated.model_dump()
        except (ValidationError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            last_error = exc
            logger.warning("优先级输出第 %d/3 次校验失败：%s", attempt, exc)
    logger.error("优先级评估失败，返回空 results：%s", last_error)
    return {"results": []}
