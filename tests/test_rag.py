from src.agents.solution_retrieval import SolutionResult, retrieve_solutions
from src.rag.vector_store import load_documents


class FakeStore:
    def search(self, query, top_k=3):
        return [{"source": "KB-0001", "title": "VPN 客户端连接超时",
                 "steps": ["确认本地网络可访问互联网"], "relevance": "高"}]

def test_corpus_size_and_distribution():
    docs = load_documents()
    assert len([item for item in docs if item["source"].startswith("KB-")]) == 24
    assert len([item for item in docs if item["source"].startswith("HIST-")]) == 64

def test_retrieval_output_uses_store_matches():
    result = retrieve_solutions({"title": "VPN 连接失败", "description": "提示超时"}, store=FakeStore())
    assert SolutionResult.model_validate(result)
    assert result["results"][0]["matches"][0]["source"] == "KB-0001"

def test_no_query_returns_empty_results():
    assert retrieve_solutions({}, store=FakeStore()) == {"results": []}


def test_malformed_history_record_is_skipped(tmp_path):
    knowledge = tmp_path / "data" / "knowledge"
    tickets = tmp_path / "data" / "tickets"
    knowledge.mkdir(parents=True)
    tickets.mkdir(parents=True)
    (tickets / "historical_tickets.json").write_text(
        '[{"ticket_id":"HIST-1","title":"ok","description":"desc","resolution":"fix"},'
        '{"ticket_id":"HIST-2","title":"missing"}, null]', encoding="utf-8"
    )
    docs = load_documents(tmp_path)
    assert [item["source"] for item in docs] == ["HIST-1"]
