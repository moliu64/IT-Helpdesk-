"""Helpdesk CLI: 工单输入 -> 标准化 -> 三路并行审查 -> 路由 -> 汇总报告."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from src.agents.classify import classify_ticket
from src.agents.priority import assess_priority
from src.agents.routing import recommend_route
from src.agents.solution_retrieval import retrieve_solutions
from src.report import build_report
from src.ticket_parser import parse_ticket

def run(input_source: str) -> dict:
    parsed = parse_ticket(input_source)
    ticket = parsed["ticket"]
    # 分类 / 优先级 / 解决方案检索 三路互相独立，并行执行
    with ThreadPoolExecutor(max_workers=3) as pool:
        classification_f = pool.submit(classify_ticket, ticket)
        priority_f = pool.submit(assess_priority, ticket)
        solutions_f = pool.submit(retrieve_solutions, ticket)
        classification = classification_f.result()
        priority = priority_f.result()
        solutions = solutions_f.result()
    # 路由依赖分类结果，放在分类之后串行执行
    routing = recommend_route(ticket, classification)
    print(f"工单：{ticket['title']}")
    if classification["results"]:
        result = classification["results"][0]
        print(f"分类：{result['category']} / {result['subcategory']}（置信度 {result['confidence']:.2f}）")
    else:
        print("分类失败，已返回空 results，请检查 API 配置或日志。")
    return build_report(ticket, classification, priority, solutions, routing)

def main() -> None:
    parser = argparse.ArgumentParser(description="IT Helpdesk 工单智能分诊")
    parser.add_argument("--input", required=True, help="工单 txt/json 文件路径")
    args = parser.parse_args()
    run(args.input)

if __name__ == "__main__":
    main()
