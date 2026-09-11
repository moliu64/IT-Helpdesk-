import sqlite3

from ui.server import compact_history, retrieval_context


def _rows(*items: tuple[str, str]) -> list[sqlite3.Row]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE messages (role TEXT, content TEXT)")
    connection.executemany("INSERT INTO messages VALUES (?, ?)", items)
    # The production query returns newest first.
    return connection.execute("SELECT role, content FROM messages ORDER BY rowid DESC").fetchall()


def test_compact_history_keeps_latest_three_turns_in_order() -> None:
    rows = _rows(
        ("user", "第一轮"),
        ("assistant", "第一轮答复"),
        ("user", "第二轮"),
        ("assistant", "第二轮答复"),
        ("user", "第三轮"),
        ("assistant", "第三轮答复"),
        ("user", "第四轮"),
        ("assistant", "第四轮答复"),
    )

    assert compact_history(rows) == [
        {"role": "user", "content": "第二轮"},
        {"role": "assistant", "content": "第二轮答复"},
        {"role": "user", "content": "第三轮"},
        {"role": "assistant", "content": "第三轮答复"},
        {"role": "user", "content": "第四轮"},
        {"role": "assistant", "content": "第四轮答复"},
    ]


def test_retrieval_context_prefers_current_message_and_excludes_assistant() -> None:
    history = [
        {"role": "user", "content": "VPN 连接超时"},
        {"role": "assistant", "content": "不要把这段回答拿去检索"},
    ]

    context = retrieval_context("还是不行", history)

    assert "VPN 连接超时" in context
    assert "还是不行" in context
    assert "不要把这段回答拿去检索" not in context
