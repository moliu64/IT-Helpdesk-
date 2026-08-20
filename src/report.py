"""Reducer for Helpdesk triage results and human-readable reports."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from src.llm_client import ROOT, load_config

def _first(payload: dict[str, Any]) -> dict[str, Any]:
    values = payload.get("results", []) if isinstance(payload, dict) else []
    return values[0] if values else {}

def _category_team_conflict(category: str, team: str) -> str | None:
    pairs = {"网络连接": "网络组", "硬件设备": "桌面支持组", "软件应用": "应用组", "安全事件": "安全组", "账号与登录": "账号权限组", "权限申请": "账号权限组", "邮件通讯": "应用组"}
    expected = pairs.get(category)
    return f"分类为“{category}”，但路由为“{team}”（通常建议“{expected}”）" if expected and team != expected else None

def build_report(ticket: dict[str, Any], classification: dict, priority: dict,
                 solutions: dict, routing: dict, output_root: Path | None = None) -> dict[str, Any]:
    cls, pri, route = _first(classification), _first(priority), _first(routing)
    config = load_config()
    sla_map = config.get("helpdesk", {}).get("sla_map", {})
    conflicts: list[str] = []
    effective_priority = pri.get("priority", "")
    if effective_priority == "P1" and pri.get("impact_scope") not in {"全员", "业务阻断"} and not pri.get("business_blocked", False):
        effective_priority = "P2"
        conflicts.append("原判定为 P1，但影响面非全员且未阻断业务，已降级为 P2")
    team_conflict = _category_team_conflict(cls.get("category", ""), route.get("team", ""))
    if team_conflict: conflicts.append(team_conflict)
    sla_hours = sla_map.get(effective_priority, pri.get("sla_hours", ""))
    solutions_results = solutions.get("results", [])
    matches = solutions_results[0].get("matches", []) if solutions_results else []
    tags = []
    if cls.get("category"): tags.append("#" + cls["category"].replace("与", ""))
    if cls.get("subcategory"): tags.append("#" + cls["subcategory"].replace(" ", ""))
    if effective_priority: tags.append("#" + effective_priority)
    report = {
        "ticket": ticket, "triage": {"classification": cls, "priority": effective_priority,
        "sla_hours": sla_hours, "routing": route}, "solutions": solutions_results,
        "conflicts": conflicts, "tags": tags,
        "user_reply_draft": (f"您好，我们已收到您的工单“{ticket.get('title', '')}”。当前初步分类为{cls.get('category', '待确认')}，预计在 {sla_hours} 小时内响应。" + ("您可以先按匹配方案中的步骤排查。" if matches else "暂未找到匹配方案，建议人工排查。")),
        "engineer_advice": "请核对工单描述、复现步骤、错误信息和影响范围；" + ("优先参考检索到的知识库步骤。" if matches else "当前无检索命中，需人工建立排查路径。"),
    }
    target = output_root or ROOT / "outputs"
    folder = target / ticket.get("ticket_id", "UNASSIGNED")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Helpdesk 工单报告：{ticket.get('ticket_id', '')}", "", "## 工单基本信息", f"- 标题：{ticket.get('title', '')}", f"- 申请人：{ticket.get('requester', '')}", f"- 渠道：{ticket.get('channel', '')}", f"- 描述：{ticket.get('description', '')}", "", "## 分诊结论", f"- 分类：{cls.get('category', '待确认')} / {cls.get('subcategory', '')}（置信度 {cls.get('confidence', '')}）", f"- 优先级：{effective_priority}，SLA：{sla_hours} 小时", f"- 路由：{route.get('team', '待分派')}", "", "## 相似解决方案"]
    if matches:
        for match in matches: lines.append(f"- 来源：{match['source']}；标题：{match['title']}；相关度：{match['relevance']}；步骤：" + "；".join(match["steps"]))
    else: lines.append("- 未找到匹配方案，建议人工排查")
    lines += ["", "## 给用户的回复草稿", report["user_reply_draft"], "", "## 给工程师的处理建议", report["engineer_advice"], "", "## 自动标签", " ".join(tags)]
    if conflicts: lines += ["", "## 交叉校验与差异", *[f"- {item}" for item in conflicts]]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
