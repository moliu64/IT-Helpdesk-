from types import SimpleNamespace
from src.agents.classify import ClassificationResult, classify_ticket
from src.llm_client import LLMClient, load_config

class FakeCompletions:
    def __init__(self, contents):
        self.contents = iter(contents)
        self.calls = 0
    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(self.contents)))])

def fake_client(contents):
    completions = FakeCompletions(contents)
    api = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LLMClient(client=api), completions

def test_classification_retries_validation_and_uses_config_category():
    client, calls = fake_client([
        '{"results":[{"category":"不存在","subcategory":"VPN","confidence":0.9}]}',
        '{"results":[{"category":"网络连接","subcategory":"VPN","confidence":1.5}]}',
        '{"results":[{"category":"网络连接","subcategory":"VPN","confidence":0.92}]}',
    ])
    result = classify_ticket({"title": "VPN 无法连接", "description": "连接超时"}, client=client)
    assert ClassificationResult.model_validate(result)
    assert result == {"results": [{"category": "网络连接", "subcategory": "VPN", "confidence": 0.92}]}
    assert result["results"][0]["category"] in load_config()["helpdesk"]["categories"]
    assert calls.calls == 3

def test_classification_returns_empty_after_three_failures():
    client, calls = fake_client(['[]', '[]', '[]'])
    assert classify_ticket({}, client=client) == {"results": []}
    assert calls.calls == 3
