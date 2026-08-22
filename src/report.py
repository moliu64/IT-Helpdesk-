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
    category = cls.get("category", "待确认")
    subcategory = cls.get("subcategory", "")
    title = ticket.get("title", "该工单")
    description = ticket.get("description", "未提供")
    user_steps = []
    for match in matches[:2]:
        user_steps.extend(match.get("steps", [])[:3])
    if user_steps:
        user_action = "建议您按以下顺序尝试：" + "；".join(user_steps) + "。完成后请回复操作结果、错误提示和发生时间。"
    else:
        user_action = "目前知识库没有找到与该问题直接匹配的方案，请不要反复修改系统配置；请补充截图、错误码、发生时间、影响设备或账号信息，等待工程师人工排查。"
    engineer_points = [
        f"1. 复现确认：围绕“{title}”复现问题，记录首次出现时间、频率、完整错误信息和影响范围。",
        f"2. 输入核对：核对申请人、渠道、工单描述及已尝试操作，避免重复执行已经失败的步骤。当前描述：{description}",
        f"3. 分诊依据：类别为“{category} / {subcategory}”，建议路由“{route.get('team', '待分派')}”，优先级“{effective_priority}”，SLA 为 {sla_hours} 小时。",
    ]
    if matches:
        engineer_points.append("4. 方案验证：优先按已检索方案逐项验证，并记录每一步的输入、结果和时间；不得跳过日志留存。命中来源：" + "、".join(m.get("source", "") for m in matches) + "。")
    else:
        engineer_points.append("4. 无检索命中：从账号状态、网络连通性、客户端/设备状态和服务端日志四个方向建立排查路径；当前没有可直接复用的知识库步骤。")
    engineer_points.append("5. 升级条件：若影响范围扩大、业务阻断、出现安全告警或超过 SLA 未恢复，请在升级记录中附上复现步骤、日志位置、错误码和已排除项。")
    tags = []
    if cls.get("category"): tags.append("#" + cls["category"].replace("与", ""))
    if cls.get("subcategory"): tags.append("#" + cls["subcategory"].replace(" ", ""))
    if effective_priority: tags.append("#" + effective_priority)
    report = {
        "ticket": ticket, "triage": {"classification": cls, "priority": effective_priority,
        "sla_hours": sla_hours, "routing": route}, "solutions": solutions_results,
        "conflicts": conflicts, "tags": tags,
        "user_reply_draft": (f"您好，我们已收到您的工单“{title}”。初步判断为“{category} / {subcategory}”，当前优先级为 {effective_priority}，预计在 {sla_hours} 小时内响应。{user_action}"),
        "engineer_advice": "\n".join(engineer_points),
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
