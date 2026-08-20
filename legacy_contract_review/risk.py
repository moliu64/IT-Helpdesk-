from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from src.agents.prompts import risk_prompt
from src.llm_client import LLMClient

LEGAL_BASIS = {"《民法典》第585条", "《民法典》第497条", "《民法典》第584条", "《民法典》第590条"}
RISK_TYPES = {"违约责任不对等", "赔偿限额过低", "解除权不对等", "违约金过高", "管辖/仲裁条款不利", "担保责任", "知识产权归属", "保密义务过重", "不可抗力", "自动续约", "竞业限制", "单方变更权", "验收标准模糊"}

class RiskItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    clause_id: str
    risk_type: str
    risk_level: str = Field(pattern="^(high|medium|low)$")
    description: str
    suggested_revision: str
    legal_basis: str

def _validate(items) -> list[dict]:
    out = []
    for raw in items if isinstance(items, list) else []:
        item = RiskItem.model_validate(raw)
        if item.risk_type not in RISK_TYPES: item.risk_type = "其他合同风险"
        if item.legal_basis not in LEGAL_BASIS: item.legal_basis = "依据待人工核实"
        out.append(item.model_dump())
    return out

def _heuristic(clauses):
    out=[]
    for c in clauses:
        t=c["content"]
        checks=[("违约金过高", "high", "违约金比例可能明显过高", "建议结合实际损失并设置合理上限", "《民法典》第585条", ["违约金","50%"]), ("管辖/仲裁条款不利", "medium", "争议解决地点或方式可能对一方不利", "协商选择公平的管辖或仲裁机构", "依据待人工核实", ["仲裁","管辖"]), ("验收标准模糊", "medium", "验收标准缺乏明确客观指标", "补充可量化的验收标准和期限", "依据待人工核实", ["验收"])]
        for typ,lvl,desc,sug,basis,keys in checks:
            if any(k in t for k in keys): out.append(RiskItem(clause_id=c["clause_id"],risk_type=typ,risk_level=lvl,description=desc,suggested_revision=sug,legal_basis=basis).model_dump())
    return out

def review_risks(clauses, client: LLMClient | None = None) -> list[dict]:
    try: return _validate((client or LLMClient()).json_completion(risk_prompt(clauses)))
    except Exception: return _heuristic(clauses)
