from __future__ import annotations
import argparse
from src.parser import parse_pdf
from src.agents.risk import review_risks

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input', required=True); args=ap.parse_args()
    parsed=parse_pdf(args.input); risks=review_risks(parsed['clauses'])
    print(f"合同：{parsed['contract_meta']['title']}\n识别到 {len(risks)} 条风险")
    for r in risks: print(f"[{r['risk_level']}] {r['clause_id']} {r['risk_type']}：{r['description']}；建议：{r['suggested_revision']}；依据：{r['legal_basis']}")
    return risks
if __name__ == '__main__': main()
