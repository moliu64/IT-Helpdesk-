import json
import sqlite3

from ui import server


def test_ticket_schema_migrates_and_defaults_to_unresolved(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "helpdesk.db")
    connection = server.db()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)").fetchall()}
    assert {"status", "resolution_note", "status_updated_at"} <= columns
    connection.execute(
        "INSERT INTO conversations(user_id,ticket_id,title,created_at,report_json) VALUES(?,?,?,?,?)",
        ("u-1", "T-1", "VPN 失败", "2026-01-01T00:00:00", json.dumps({"ticket": {"description": "超时"}, "triage": {}})),
    )
    connection.commit()
    row = connection.execute("SELECT * FROM conversations WHERE ticket_id='T-1'").fetchone()
    connection.close()
    ticket = server.ticket_row(row)
    assert ticket["status"] == "unresolved"
    assert ticket["resolution_note"] == ""


def test_ticket_row_flattens_report_fields() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE conversations (
        user_id TEXT, ticket_id TEXT, title TEXT, created_at TEXT, report_json TEXT,
        status TEXT, resolution_note TEXT, status_updated_at TEXT
    )""")
    connection.execute(
        "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?)",
        ("u-1", "T-2", "打印机", "now", json.dumps({
            "ticket": {"requester": "张三", "description": "无法打印"},
            "triage": {"classification": {"category": "硬件设备", "subcategory": "打印机"}, "priority": "P3", "sla_hours": 8, "routing": {"team": "桌面支持组"}},
        }, ensure_ascii=False), "resolved", "更换驱动后恢复", "later"),
    )
    row = connection.execute("SELECT * FROM conversations").fetchone()
    assert server.ticket_row(row)["priority"] == "P3"
    assert server.ticket_row(row)["team"] == "桌面支持组"
    assert server.ticket_row(row)["status"] == "resolved"
