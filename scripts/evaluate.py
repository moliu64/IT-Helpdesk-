"""Evaluate classification, priority, and local-RAG top-3 hit rate."""
from __future__ import annotations
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.classify import classify_ticket
from src.agents.priority import assess_priority
from src.agents.solution_retrieval import retrieve_solutions
from src.ticket_parser import parse_ticket

def evaluate() -> dict:
    output = ROOT / "outputs" / "eval_result.json"
    if not os.getenv("LLM_API_KEY"):
        result = {"status": "not_run", "reason": "LLM_API_KEY 未设置；未运行真实模型评测，未生成虚构指标。"}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result["reason"])
        return result
    records = json.loads((ROOT / "data" / "annotated" / "helpdesk_eval.json").read_text(encoding="utf-8"))
    category_hits = priority_hits = rag_hits = 0
    details = []
    for record in records:
        ticket = parse_ticket({"ticket": {k: v for k, v in record.items() if k != "gold"}})["ticket"]
        category_result = classify_ticket(ticket).get("results", [])
        priority_result = assess_priority(ticket).get("results", [])
        solution_result = retrieve_solutions(ticket).get("results", [])
        predicted_category = category_result[0].get("category") if category_result else None
        predicted_priority = priority_result[0].get("priority") if priority_result else None
        matches = solution_result[0].get("matches", []) if solution_result else []
        # Gold category is considered a hit when a retrieved KB title contains a category signal.
        signals = {"账号与登录": ["账号", "登录", "MFA", "密码"], "网络连接": ["网络", "VPN", "DNS", "网"], "硬件设备": ["电脑", "显示", "键盘", "设备"], "软件应用": ["Office", "浏览器", "客户端", "会议"], "权限申请": ["权限", "仓库", "目录", "管理员"], "邮件通讯": ["邮件", "邮箱", "附件"], "安全事件": ["安全", "钓鱼", "登录", "杀毒"], "其他": ["会议室", "资产", "采购", "工位"]}
        rag_hit = any(any(signal.lower() in (match.get("title", "") + " " + " ".join(match.get("steps", []))).lower() for signal in signals[record["gold"]["category"]]) for match in matches)
        category_hits += predicted_category == record["gold"]["category"]
        priority_hits += predicted_priority == record["gold"]["priority"]
        rag_hits += rag_hit
        details.append({"ticket_id": record["ticket_id"], "predicted_category": predicted_category, "gold_category": record["gold"]["category"], "predicted_priority": predicted_priority, "gold_priority": record["gold"]["priority"], "rag_top3_hit": rag_hit})
    total = len(records)
    result = {"status": "completed", "total": total, "category_accuracy": category_hits / total, "priority_accuracy": priority_hits / total, "rag_top3_hit_rate": rag_hits / total, "details": details}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"分类准确率: {result['category_accuracy']:.2%}\n优先级准确率: {result['priority_accuracy']:.2%}\nRAG Top-3 命中率: {result['rag_top3_hit_rate']:.2%}")
    return result

if __name__ == "__main__":
    evaluate()
