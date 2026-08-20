"""Ticket support-team routing."""
from __future__ import annotations
import logging
from typing import Any
from pydantic import BaseModel, ConfigDict, ValidationError
from src.agents.prompts import routing_messages
from src.llm_client import LLMClient, load_config

logger = logging.getLogger(__name__)

class RoutingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team: str
    reason: str

class RoutingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[RoutingItem]

def recommend_route(ticket: dict[str, Any], classification: dict[str, Any],
                    client: LLMClient | None = None,
                    config: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    settings = config or load_config()
    teams = settings.get("helpdesk", {}).get("teams", [])
    if not teams:
        logger.error("路由失败：config.helpdesk.teams 为空")
        return {"results": []}
    llm = client or LLMClient(settings)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            payload = llm.json_completion(routing_messages(ticket, classification, teams), retries=1)
            validated = RoutingResult.model_validate(payload)
            invalid = [item.team for item in validated.results if item.team not in teams]
            if invalid:
                raise ValueError(f"team 不在配置清单中: {invalid}")
            return validated.model_dump()
        except (ValidationError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            last_error = exc
            logger.warning("路由输出第 %d/3 次校验失败：%s", attempt, exc)
    logger.error("路由失败，返回空 results：%s", last_error)
    return {"results": []}
