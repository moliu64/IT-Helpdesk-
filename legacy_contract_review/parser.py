"""PDF text extraction and heuristic contract clause segmentation."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any
import fitz
from pydantic import BaseModel, Field

class ContractMeta(BaseModel):
    title: str = ""
    parties: list[str] = Field(default_factory=list)
    date: str = ""

class Clause(BaseModel):
    clause_id: str
    heading: str
    content: str

class ParsedContract(BaseModel):
    contract_meta: ContractMeta
    clauses: list[Clause]

NUMBER = r"(?:第\s*[一二三四五六七八九十百千万零〇0-9]+\s*条|[0-9]+[.、．]|（[一二三四五六七八九十百千万零〇0-9]+）)"
HEADER_RE = re.compile(rf"^\s*({NUMBER})\s*(.*)$", re.MULTILINE)

def extract_text(pdf_path: str | Path) -> str:
    doc = fitz.open(str(pdf_path))
    return "\n".join(page.get_text() for page in doc)

def _meta(text: str) -> ContractMeta:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    title = lines[0] if lines and len(lines[0]) < 80 else ""
    dates = re.findall(r"(?:20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}日?)", text)
    parties = []
    for line in lines[:30]:
        if any(k in line for k in ("甲方", "乙方", "丙方")) and (":" in line or "：" in line):
            parties.append(line)
    return ContractMeta(title=title, parties=parties, date=dates[0] if dates else "")

def parse_contract(pdf_path: str | Path) -> ParsedContract:
    text = extract_text(pdf_path)
    matches = list(HEADER_RE.finditer(text))
    clauses: list[Clause] = []
    for i, match in enumerate(matches):
        content = text[match.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()
        heading_tail = match.group(2).strip()
        first_line, *rest = content.splitlines()
        heading = heading_tail or (first_line.strip()[:40] if first_line else "")
        clauses.append(Clause(clause_id=f"C{i + 1:02d}", heading=heading, content=content))
    if not clauses and text.strip():
        clauses.append(Clause(clause_id="C01", heading="全文", content=text.strip()))
    return ParsedContract(contract_meta=_meta(text), clauses=clauses)

def parse_pdf(pdf_path: str | Path) -> dict[str, Any]:
    return parse_contract(pdf_path).model_dump()
