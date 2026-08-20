RISK_SYSTEM = """你是合同风险审查专家。仅输出 JSON 数组，每项字段为 clause_id,risk_type,risk_level,description,suggested_revision,legal_basis。legal_basis 只能使用给定法条清单，否则写依据待人工核实。"""

def risk_prompt(clauses: list[dict]) -> list[dict[str, str]]:
    import json
    return [{"role": "system", "content": RISK_SYSTEM}, {"role": "user", "content": json.dumps({"clauses": clauses}, ensure_ascii=False)}]
