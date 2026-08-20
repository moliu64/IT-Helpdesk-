# AGENTS.md — 合同审查 Agent 实施说明（直接交给 Codex 执行）

> 本文件是给 Codex 的执行指令。请严格按顺序实施，每阶段完成前自检验收标准。
> 同目录下的 `PLAN.md` 是完整规格说明，需要背景/理由时参考；冲突时以本文件为准。

---

## 1. 你的任务

实现一个**合同智能审查**系统：输入一份合同 PDF，输出一份结构化审查报告（Markdown + JSON）。

审查包含四个维度，且这四个维度要能**并行执行**：

1. 风险条款识别
2. 合规性审查
3. 参考判例检索（RAG）
4. 必备条款完备性检查

最后有一个汇总步骤，合并四路结果、去重、交叉校验、按风险等级排序。

---

## 2. 硬性规则（必须遵守，违反即返工）

1. **LLM 输出必须校验**：所有调用 LLM 的 Agent，其输出必须经过 Pydantic 模型校验；校验失败要自动重试（最多 3 次），仍失败则返回空列表并记录错误，绝不崩溃。
2. **禁止编造法条和判例**：`legal_basis`（法条）只能来自内置的合法条清单或 RAG 检索结果；检索不到时必须写 `"依据待人工核实"`，绝不能凭空编法条号。
3. **LLM 调用统一走一个封装**：所有模型调用只允许通过 `src/llm_client.py`，禁止在 Agent 代码里直接 `openai`/`requests` 调模型。目的是换模型/换 key 只改配置。
4. **保留纯 Python 回退**：最终系统必须能**不依赖 DeepSeek Harness 也能完整运行**（`python -m src.main`）。DSH 编排是加分项，做成可选入口，不能成为运行前提。
5. **可配置**：模型名、API key、compliance_scope（行业）、向量库路径全部走 `config/config.yaml`，禁止硬编码。
6. **输出 JSON Schema 严格按 §5 定义**，字段名、层级、枚举值一字不差（汇总阶段依赖这些契约）。

---

## 3. 技术栈与依赖

- 语言：Python 3.10+
- LLM：DeepSeek API（`deepseek-chat`，复杂推理可换 `deepseek-reasoner`），走 **OpenAI 兼容接口**
- PDF 解析：`PyMuPDF`（首选）或 `pdfplumber`
- 向量库：`Chroma`（本地、简单）
- Embedding：OpenAI 兼容 embedding API，或 `sentence-transformers`（BGE 系列），二者选一并在配置里可切换
- 校验：`pydantic`
- 配置：`pyyaml`

`requirements.txt` 至少包含：`pymupdf`、`chromadb`、`pydantic`、`pyyaml`、`openai`（或 `httpx`）、`sentence-transformers`（如选本地 embedding）。

---

## 4. 配置与密钥

`config/config.yaml` 结构：

```yaml
llm:
  base_url: "https://api.deepseek.com/v1"   # OpenAI 兼容
  api_key_env: "LLM_API_KEY"                # 从环境变量读 key，不落盘
  model: "deepseek-chat"
  temperature: 0.1
embedding:
  provider: "openai"        # openai | local
  model: "text-embedding-3-small"   # 或本地 bge 模型名
rag:
  index_dir: "data/cases/index"
review:
  compliance_scope: "general"   # general | labor | lease | sales
```

**规则**：API key 只能从环境变量读取（如 `os.environ["LLM_API_KEY"]`），禁止写进代码或配置文件。README 里说明如何设置环境变量。

---

## 5. 数据契约（各 Agent 输出 JSON Schema，必须严格实现）

### 5.1 解析输出（`src/parser.py`）
```json
{
  "contract_meta": {"title": "", "parties": [], "date": ""},
  "clauses": [
    {"clause_id": "C01", "heading": "违约责任", "content": "..."}
  ]
}
```
`clause_id` 用 `C01`、`C02`…顺序编号。

### 5.2 风险识别输出（`src/agents/risk.py`）
```json
[
  {
    "clause_id": "C01",
    "risk_type": "违约金过高",
    "risk_level": "high",
    "description": "约定违约金为合同总额的 50%，明显过高",
    "suggested_revision": "建议调整为不超过实际损失的 30%",
    "legal_basis": "《民法典》第585条"
  }
]
```
`risk_level` 枚举：`high | medium | low`。
`risk_type` 覆盖清单（至少支持）：违约责任不对等、赔偿限额过低、解除权不对等、违约金过高、管辖/仲裁条款不利、担保责任、知识产权归属、保密义务过重、不可抗力、自动续约、竞业限制、单方变更权、验收标准模糊。

### 5.3 合规审查输出（`src/agents/compliance.py`）
```json
[
  {
    "clause_id": "C01",
    "compliance_issue": "格式条款加重对方责任",
    "legal_basis": "《民法典》第497条",
    "suggested_revision": "..."
  }
]
```

### 5.4 判例检索输出（`src/agents/case_retrieval.py`）
```json
[
  {
    "risk_point": "违约金过高",
    "related_cases": [
      {"case_title": "...诉...合同纠纷案", "court": "...", "key_holding": "...", "relevance": "高"}
    ]
  }
]
```

### 5.5 完备性检查输出（`src/agents/completeness.py`）
```json
[
  {"missing_clause": "争议解决方式", "importance": "high", "suggestion": "..."}
]
```
必备条款清单：当事人信息、标的、数量、质量、价款/报酬、履行期限/地点/方式、违约责任、争议解决方式。

### 5.6 汇总报告输出（`src/report.py`）
- `outputs/<合同名>/report.json`：完整机器可读结果（合并去重后的所有问题 + 元数据）
- `outputs/<合同名>/report.md`：给人看的报告，结构：
  - 合同基本信息
  - 风险等级总览（高 X 条 / 中 Y 条 / 低 Z 条）
  - 问题列表（按 `risk_level` 高→低排序，每条含：条款位置、类型、等级、问题描述、修改建议、法条/判例依据）
  - 合规性意见
  - 必备条款完备性
  - 综合结论与谈判建议

**交叉校验规则**：同一 `clause_id` 被多个 Agent 命中时，合并为一条并标注"多维度命中"；内容冲突时以法律依据更明确者为准，并在报告中注明差异。

---

## 6. 目录结构（严格按此创建）

```
contract-review-agent/
├── README.md
├── requirements.txt
├── config/config.yaml
├── data/
│   ├── raw/               # 原始合同 PDF（放 2~3 份样例）
│   ├── annotated/         # 评测集 gold 答案
│   └── cases/             # 判例库原始文本 + index/
├── src/
│   ├── llm_client.py
│   ├── parser.py
│   ├── main.py
│   ├── report.py
│   ├── agents/
│   │   ├── risk.py
│   │   ├── compliance.py
│   │   ├── case_retrieval.py
│   │   ├── completeness.py
│   │   └── prompts.py     # 所有 prompt 集中在此，禁止散落在各 agent 文件里
│   └── rag/
│       └── vector_store.py
├── workflow/
│   └── review.workflow.js # DSH 编排（Phase 6 才做，可选）
├── scripts/
│   ├── build_case_index.py
│   └── evaluate.py
├── outputs/               # 生成结果，.gitignore
└── tests/
```

---

## 7. 实施顺序（严格按 Phase 0 → 8，禁止跳步）

> 每个 Phase 先做、自测验收、再进下一个。不要一次性写完所有文件再跑。

### Phase 0 — 环境与骨架
- 创建目录结构、`requirements.txt`、`config/config.yaml`
- 实现 `src/llm_client.py`：OpenAI 兼容调用 + 强制 JSON 输出 + 失败重试（最多 3 次）
- **验收**：单独跑一个测试调用能返回合法 JSON

### Phase 1 — 文档解析与条款切分
- 实现 `src/parser.py`：PDF → 文本 → 条款列表
- 条款边界识别：先处理常见编号（"第一条/第1条/1./（一）"），其余 LLM 兜底
- **验收**：对样例 PDF，人工抽查 10 条，边界正确率 ≥ 90%

### Phase 2 — 单链路跑通（只做风险识别）
- 实现 `src/agents/prompts.py` + `src/agents/risk.py` + `src/main.py`
- `main.py` 流程：PDF → 条款 → 风险识别 → 打印简单文本结果
- **验收**：一份样例合同稳定输出 ≥ 3 条合理风险点，且通过 Pydantic 校验

### Phase 3 — 扩展到四路 Agent
- 实现 `compliance.py`、`completeness.py`；`case_retrieval.py` 先返回空列表占位
- **验收**：四路都能独立运行、输出符合 §5 schema、内容合理无幻觉

### Phase 4 — 判例检索 RAG
- 实现 `src/rag/vector_store.py` + `scripts/build_case_index.py`
- 内置 30~50 条**脱敏判例摘要**（可用公开文书摘要，标注仅用于学习研究）
- `case_retrieval.py` 接入：由风险点构造 query → 检索 → 返回相关判例
- **验收**：给定 5 个风险点，Top-3 中至少 1 条相关

### Phase 5 — 汇总报告与交叉校验
- 实现 `src/report.py`：去重、冲突消解、按等级排序、生成 `report.md` + `report.json`
- **验收**：同一条款被多 Agent 命中能合并；报告按高→低排序；markdown 结构符合 §5.6

### Phase 6 — DSH 编排（可选加分项）
- 用 DSH `workflow` 把四路审查用 `parallel()` 编排，barrier 后汇总
- 前置：`parser` 和 `rag` 建索引作为编排外的步骤
- **验收**：DSH 能并行跑四路并出汇总报告；同时 `python -m src.main` 纯 Python 版本仍可完整运行

### Phase 7 — 评测
- 准备 20~30 份合同 + 人工标注 gold 答案（`data/annotated/`）
- `scripts/evaluate.py` 计算风险识别 **Precision / Recall / F1** 和风险等级准确率
- **验收**：输出一组可写进简历的数字（如 `F1 = 0.7x`）

### Phase 8 — README 与 Demo
- README：架构图（文本/mermaid）、快速开始（设置 key → 安装 → 跑样例）、效果对比表
- 录 2~3 分钟 demo 视频脚本（说明输入、四路并行、输出报告）
- **验收**：陌生环境按 README 10 分钟内能跑通

---

## 8. 完成定义（Definition of Done）

满足以下全部才算完成：

- [ ] `python -m src.main --input 样例合同.pdf` 能端到端产出 `report.md` + `report.json`
- [ ] 四路审查可并行执行（Python 多线程/进程或 DSH `parallel()`）
- [ ] 所有 LLM 输出经过 Pydantic 校验 + 重试，无崩溃路径
- [ ] 无编造法条/判例（未检索到即标注"依据待人工核实"）
- [ ] 有评测脚本和至少一组指标数字
- [ ] README 可让陌生人在 10 分钟内跑通

---

## 9. 注意事项（实现时容易踩的坑）

1. 中文合同编号格式多样，条款切分是隐藏难点——先用启发式，再 LLM 兜底。
2. DeepSeek 的 JSON 模式若不稳定，用 `response_format` + 手动 `json.loads` 双保险，解析失败走重试。
3. 并行四路时注意线程安全：`llm_client` 和向量库访问要能并发调用（Chroma 用锁或每线程独立 client）。
4. `config.yaml` 里不要放真实 key；示例文件写占位符，真实 key 走环境变量。
5. `outputs/` 和向量索引目录加入 `.gitignore`。
