"""Heuristic parsing and normalization for Helpdesk tickets."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Ticket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticket_id: str = ""
    requester: str = ""
    title: str = ""
    description: str = ""
    channel: Literal["email", "portal", "chat", "phone"] = "portal"
    created_at: str = ""


class ParsedTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: Ticket


FIELD_PATTERN = re.compile(
    r"^(?P<key>工单号|ticket[_ ]?id|申请人|报障人|requester|标题|主题|title|描述|问题描述|description|渠道|channel|创建时间|created_at)\s*[:：]\s*(?P<value>.*)$",
    re.IGNORECASE,
)
CHANNEL_ALIASES = {
    "email": "email", "邮件": "email", "邮箱": "email",
    "portal": "portal", "门户": "portal", "工单系统": "portal",
    "chat": "chat", "聊天": "chat", "即时消息": "chat", "微信": "chat",
    "phone": "phone", "电话": "phone", "来电": "phone",
}
KEY_MAP = {
    "工单号": "ticket_id", "ticketid": "ticket_id", "ticket_id": "ticket_id", "ticket id": "ticket_id",
    "申请人": "requester", "报障人": "requester", "requester": "requester",
    "标题": "title", "主题": "title", "title": "title",
    "描述": "description", "问题描述": "description", "description": "description",
    "渠道": "channel", "channel": "channel",
    "创建时间": "created_at", "created_at": "created_at",
}


def _channel(value: str, text: str) -> str:
    candidate = value.strip().lower()
    for alias, normalized in CHANNEL_ALIASES.items():
        if alias in candidate:
            return normalized
    for alias, normalized in CHANNEL_ALIASES.items():
        if alias in text.lower():
            return normalized
    return "portal"


def _from_text(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    free_lines: list[str] = []
    active_description = False
    for source_line in raw.splitlines():
        line = source_line.strip()
        if not line:
            continue
        match = FIELD_PATTERN.match(line)
        if match:
            key = KEY_MAP[match.group("key").lower().replace(" ", "_")]
            fields[key] = match.group("value").strip()
            active_description = key == "description"
        elif active_description:
            fields["description"] = (fields.get("description", "") + "\n" + line).strip()
        else:
            free_lines.append(line)

    if not fields.get("title"):
        fields["title"] = free_lines[0][:120] if free_lines else "未命名工单"
    if not fields.get("description"):
        remainder = free_lines[1:] if free_lines else []
        fields["description"] = "\n".join(remainder) or fields["title"]
    fields["channel"] = _channel(fields.get("channel", ""), raw)
    return fields


def parse_ticket(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    source_path: Path | None = None
    if isinstance(source, dict):
        raw_data = source.get("ticket", source)
    else:
        path = Path(source)
        source_path = path if path.is_file() else None
        raw = path.read_text(encoding="utf-8-sig") if path.is_file() else str(source)
        if path.suffix.lower() == ".json" and path.is_file():
            loaded = json.loads(raw)
            raw_data = loaded.get("ticket", loaded)
        else:
            raw_data = _from_text(raw)

    data = dict(raw_data)
    if not data.get("ticket_id") and source_path is not None:
        data["ticket_id"] = source_path.stem
    data["channel"] = _channel(str(data.get("channel", "")), str(data))
    parsed = ParsedTicket(ticket=Ticket.model_validate(data))
    return parsed.model_dump()
