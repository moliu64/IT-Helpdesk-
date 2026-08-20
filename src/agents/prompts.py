"""Central prompt definitions for Helpdesk agents."""
from __future__ import annotations
import json

CLASSIFY_SYSTEM = """你是 IT Helpdesk 工单分类器。只能从提供的 categories 中选择 category。
仅返回 JSON 对象，严格格式为 {"results":[{"category":"...","subcategory":"...","confidence":0.0}]}。
confidence 必须在 0 到 1 之间，不要输出解释或 Markdown。"""

PRIORITY_SYSTEM = """你是 IT Helpdesk 优先级评估器。严格按以下标准判定 priority（P1 最高）：
- P1：全员或大范围受影响，或核心业务被阻断（如整层断网、勒索软件、生产系统宕机）
- P2：影响一个团队/多人，或安全事件（账号异常登录、病毒、钓鱼），或关键任务受阻、有明确截止时间
- P3：单人或少数用户受影响，常规故障，存在临时绕行方案
- P4：咨询类、低影响、无时间压力，可排队处理

impact_scope 必须与 priority 一致：P1 对应全员，P2 对应团队，P3/P4 对应单人。
仅返回 JSON 对象，格式为
{"results":[{"priority":"P1|P2|P3|P4","sla_hours":数字,"impact_scope":"单人|团队|全员","affected_users":整数,"business_blocked":布尔值,"reason":"..."}]}。
sla_hours 必须使用提供的 sla_map。"""

ROUTING_SYSTEM = """你是 IT Helpdesk 路由器。只能从提供的 teams 中选择 team。
仅返回 JSON 对象，格式为 {"results":[{"team":"...","reason":"..."}]}。"""

def classify_messages(ticket: dict, categories: list[str]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": json.dumps({"ticket": ticket, "categories": categories}, ensure_ascii=False)},
    ]

def priority_messages(ticket: dict, sla_map: dict[str, int]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PRIORITY_SYSTEM},
        {"role": "user", "content": json.dumps({"ticket": ticket, "sla_map": sla_map}, ensure_ascii=False)},
    ]

def routing_messages(ticket: dict, classification: dict, teams: list[str]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": ROUTING_SYSTEM},
        {"role": "user", "content": json.dumps({"ticket": ticket, "classification": classification, "teams": teams}, ensure_ascii=False)},
    ]
