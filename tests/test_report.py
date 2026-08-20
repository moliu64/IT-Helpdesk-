import json
from pathlib import Path
from src.report import build_report

def test_report_conflicts_and_outputs(tmp_path: Path):
    ticket = {"ticket_id": "T-001", "requester": "张三", "title": "VPN 连接失败", "description": "全员无法连接", "channel": "portal", "created_at": ""}
    result = build_report(ticket,
        {"results": [{"category": "网络连接", "subcategory": "VPN", "confidence": .9}]},
        {"results": [{"priority": "P1", "sla_hours": 1, "impact_scope": "团队", "affected_users": 5, "business_blocked": False, "reason": "多人受影响"}]},
        {"results": [{"query": "VPN", "matches": [{"source": "KB-0001", "title": "VPN 排查", "steps": ["检查网络"], "relevance": "高"}]}]},
        {"results": [{"team": "应用组", "reason": "错误路由"}]}, output_root=tmp_path)
    assert result["triage"]["priority"] == "P2"
    assert any("降级" in item for item in result["conflicts"])
    assert any("冲突" not in item for item in result["conflicts"])
    assert (tmp_path / "T-001" / "report.json").exists()
    markdown = (tmp_path / "T-001" / "report.md").read_text(encoding="utf-8")
    assert "相似解决方案" in markdown and "自动标签" in markdown and "#P2" in markdown
