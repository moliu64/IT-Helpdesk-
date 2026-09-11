"""Optional LangGraph orchestration for the Helpdesk agents.

The module is deliberately independent from the web UI.  It exposes a small
runner that returns the same report payload as ``src.main.run`` and emits
trace events suitable for a monitoring panel or websocket stream.  LangGraph
is optional; when it is not installed, a threaded Python implementation is
used so the project remains runnable with the documented dependencies.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, TypedDict

from src.agents.classify import classify_ticket
from src.agents.priority import assess_priority
from src.agents.routing import recommend_route
from src.agents.solution_retrieval import retrieve_solutions
from src.report import build_report

TraceSink = Callable[[dict[str, Any]], None]


class GraphState(TypedDict, total=False):
    ticket: dict[str, Any]
    classification: dict[str, Any]
    priority: dict[str, Any]
    solutions: dict[str, Any]
    routing: dict[str, Any]
    report: dict[str, Any]
    trace: list[dict[str, Any]]


def _event(state: GraphState, node: str, status: str,
           sink: TraceSink | None = None, **extra: Any) -> None:
    event = {
        "node": node,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    state.setdefault("trace", []).append(event)
    if sink:
        try:
            sink(event)
        except Exception:
            # Monitoring must never break a ticket run.
            pass


def _call_node(name: str, fn: Callable[[], dict[str, Any]], state: GraphState,
               sink: TraceSink | None) -> dict[str, Any]:
    _event(state, name, "running", sink)
    try:
        result = fn()
        _event(state, name, "completed", sink,
               result_count=len(result.get("results", [])))
        return result
    except Exception as exc:  # defensive boundary around every agent
        _event(state, name, "error", sink, error=str(exc))
        return {"results": []}


def _python_run(ticket: dict[str, Any], sink: TraceSink | None = None) -> GraphState:
    state: GraphState = {"ticket": ticket, "trace": []}
    _event(state, "parse", "completed", sink)
    # These three agents have no dependencies and should remain concurrent.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "classify": pool.submit(_call_node, "classify",
                                     lambda: classify_ticket(ticket), state, sink),
            "priority": pool.submit(_call_node, "priority",
                                     lambda: assess_priority(ticket), state, sink),
            "solution_retrieval": pool.submit(_call_node, "solution_retrieval",
                                               lambda: retrieve_solutions(ticket), state, sink),
        }
        state["classification"] = futures["classify"].result()
        state["priority"] = futures["priority"].result()
        state["solutions"] = futures["solution_retrieval"].result()
    state["routing"] = _call_node(
        "routing", lambda: recommend_route(ticket, state["classification"]), state, sink
    )
    _event(state, "aggregate", "running", sink)
    state["report"] = build_report(
        ticket, state["classification"], state["priority"],
        state["solutions"], state["routing"]
    )
    _event(state, "aggregate", "completed", sink)
    return state


def _langgraph_run(ticket: dict[str, Any], sink: TraceSink | None = None) -> GraphState:
    """Run a StateGraph when LangGraph is installed.

    The independent nodes are connected in parallel from START.  Routing is
    intentionally downstream of classification, while aggregation waits for
    all four outputs through its incoming edges.
    """
    from langgraph.graph import END, START, StateGraph  # optional dependency

    initial: GraphState = {"ticket": ticket, "trace": []}

    def node(trace_name: str, result_key: str, fn: Callable[[GraphState], dict[str, Any]]):
        def wrapped(state: GraphState) -> dict[str, Any]:
            _event(state, trace_name, "running", sink)
            try:
                result = fn(state)
                _event(state, trace_name, "completed", sink,
                       result_count=len(result.get("results", [])))
                return {result_key: result}
            except Exception as exc:
                _event(state, trace_name, "error", sink, error=str(exc))
                return {result_key: {"results": []}}
        return wrapped

    graph = StateGraph(GraphState)
    graph.add_node("classify", node("classify", "classification", lambda s: classify_ticket(s["ticket"])))
    graph.add_node("priority", node("priority", "priority", lambda s: assess_priority(s["ticket"])))
    graph.add_node("solution_retrieval", node("solution_retrieval", "solutions", lambda s: retrieve_solutions(s["ticket"])))
    graph.add_node("routing", node("routing", "routing", lambda s: recommend_route(s["ticket"], s.get("classification", {"results": []}))))

    def aggregate(state: GraphState) -> dict[str, Any]:
        _event(state, "aggregate", "running", sink)
        try:
            report = build_report(state["ticket"], state.get("classification", {"results": []}),
                                  state.get("priority", {"results": []}),
                                  state.get("solutions", {"results": []}),
                                  state.get("routing", {"results": []}))
            _event(state, "aggregate", "completed", sink)
            return {"report": report}
        except Exception as exc:
            _event(state, "aggregate", "error", sink, error=str(exc))
            return {"report": {}}

    graph.add_node("aggregate", aggregate)
    _event(initial, "parse", "completed", sink)
    graph.add_edge(START, "classify")
    graph.add_edge(START, "priority")
    graph.add_edge(START, "solution_retrieval")
    graph.add_edge("classify", "routing")
    # A multi-source edge is a barrier: aggregation runs once only after all
    # independent branches and the classification-dependent route complete.
    graph.add_edge(["priority", "solution_retrieval", "routing"], "aggregate")
    graph.add_edge("aggregate", END)
    return graph.compile().invoke(initial)


def run_agent_graph(ticket: dict[str, Any], sink: TraceSink | None = None,
                    prefer_langgraph: bool = True) -> GraphState:
    """Execute triage and return state including ``trace`` and ``report``.

    Set ``prefer_langgraph=False`` to force the deterministic Python fallback.
    """
    if prefer_langgraph:
        try:
            import langgraph  # type: ignore[import-not-found]  # noqa: F401
            return _langgraph_run(ticket, sink)
        except (ImportError, ModuleNotFoundError):
            pass
        except Exception as exc:
            # A graph/version error should not take down the service.
            fallback = _python_run(ticket, sink)
            _event(fallback, "graph", "fallback", sink, error=str(exc))
            return fallback
    return _python_run(ticket, sink)


__all__ = ["GraphState", "run_agent_graph"]
