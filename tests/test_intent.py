from src.intent import detect_intent


def test_explicit_ticket_intent_uses_form():
    result = detect_intent("请帮我提交工单")
    assert result["results"][0]["intent"] == "ticket"
    assert result["results"][0]["reply_mode"] == "ticket_form"


def test_greeting_stays_conversational():
    result = detect_intent("你好，今天怎么样？")
    assert result["results"][0]["reply_mode"] == "conversation"


def test_troubleshooting_uses_rag():
    result = detect_intent("VPN 连接失败，提示认证错误")
    assert result["results"][0]["reply_mode"] == "rag"
