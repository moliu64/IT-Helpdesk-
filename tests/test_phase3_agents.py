from types import SimpleNamespace
from src.agents.priority import PriorityResult, assess_priority
from src.agents.routing import RoutingResult, recommend_route
from src.agents.solution_retrieval import SolutionResult, retrieve_solutions
from src.llm_client import LLMClient, load_config

class FakeCompletions:
    def __init__(self, contents): self.contents, self.calls = iter(contents), 0
    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(self.contents)))])

def fake_client(contents):
    completions = FakeCompletions(contents)
    api = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LLMClient(client=api), completions

def test_priority_retries_and_uses_config_sla():
    client, calls = fake_client([
        '{"results":[{"priority":"P2","sla_hours":99,"impact_scope":"团队","affected_users":5,"business_blocked":false,"reason":"共享盘不可用"}]}',
        '{"results":[{"priority":"P2","sla_hours":4,"impact_scope":"团队","affected_users":5,"business_blocked":false,"reason":"共享盘不可用"}]}',
    ])
    result = assess_priority({}, client=client)
    assert PriorityResult.model_validate(result)
    assert result["results"][0]["sla_hours"] == load_config()["helpdesk"]["sla_map"]["P2"]
    assert calls.calls == 2

def test_routing_retries_and_uses_config_team():
    client, calls = fake_client([
        '{"results":[{"team":"未知组","reason":"VPN"}]}',
        '{"results":[{"team":"网络组","reason":"VPN 属于网络连接"}]}',
    ])
    result = recommend_route({}, {"results": []}, client=client)
    assert RoutingResult.model_validate(result)
    assert result["results"][0]["team"] in load_config()["helpdesk"]["teams"]
    assert calls.calls == 2

def test_solution_placeholder_is_valid_and_empty():
    result = retrieve_solutions({})
    assert SolutionResult.model_validate(result)
    assert result == {"results": []}
