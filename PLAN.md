# 合同审查 Agent — 项目规划与技术方案（Spec）

> 本文件是交给 Codex 执行的项目规格说明书。
> 目标：做出一个**可演示、可评测、能写进简历**的多智能体垂直应用。

---

## 0. 项目一句话定位

上传一份合同 PDF，系统用多个**并行 AI Agent**分别完成：

1. 风险条款识别
2. 合规性审查
3. 参考判例检索
4. 必备条款完备性检查

最后汇总生成一份**结构化审查报告**，包含：条款定位、风险等级、问题描述、修改建议、法条/判例依据。

---

## 1. 简历价值（为什么要做这个）

- **多智能体并行 + 汇总交叉校验**，是典型的 Agent 编排场景，面试好讲
- 有**可量化评测指标**（风险条款识别的准确率 / 召回率）
- 有**真实痛点**（合同审查费时、依赖律师经验）和公开数据

---

## 2. 系统架构

```
输入(合同 PDF)
   │
   ▼
[解析与条款切分]  ── PDF → 文本 → 结构化条款列表
   │
   ▼
┌────────────── 四路并行审查 ──────────────┐
│ ① 风险条款识别   ② 合规性审查            │
│ ③ 参考判例检索   ④ 必备条款完备性检查     │
└─────────────────────────────────────────┘
   │
   ▼
[汇总 + 交叉校验] ── 去重、冲突消解、按风险等级排序
   │
   ▼
输出(结构化审查报告: Markdown + JSON)
```

关键设计点：**四路审查互相独立、可并行**，最后一步做**交叉校验**（例如"风险 Agent 认为违约金过高"与"合规 Agent 认为格式条款无效"同时命中同一条款时，置信度更高、要合并呈现）。

---

## 3. Agent 分工设计（核心）

每个 Agent 都有明确的**输入契约、输出 JSON Schema**，这是多 Agent 能并行且能汇总的前提。

### Agent 0 — 文档解析与条款切分（Parser，非 LLM 为主）

- **职责**：PDF → 纯文本；识别合同结构（标题、当事人、条款编号、章节），切分为条款列表
- **技术**：`PyMuPDF` 或 `pdfplumber` 抽文本；用正则/启发式识别条款边界（如"第一条""第1条""1." 等编号模式）；可选加一次 LLM 辅助修正边界
- **输出 Schema**：
```json
{
  "contract_meta": {"title": "", "parties": [], "date": ""},
  "clauses": [
    {"clause_id": "C01", "heading": "违约责任", "content": "..."}
  ]
}
```

### Agent 1 — 风险条款审查（核心 Agent）

- **职责**：逐条/按章节识别对委托方不利的条款
- **识别类型（风险清单）**：违约责任不对等、赔偿限额过低、解除权不对等、违约金过高、管辖/仲裁条款不利、担保责任、知识产权归属、保密义务过重、不可抗力、自动续约、竞业限制、单方变更权、验收标准模糊
- **输出 Schema**：
```json
[
  {
    "clause_id": "C01",
    "risk_type": "违约金过高",
    "risk_level": "high|medium|low",
    "description": "约定违约金为合同总额的 50%，明显过高",
    "suggested_revision": "建议调整为不超过实际损失的 30%",
    "legal_basis": "《民法典》第585条"
  }
]
```

### Agent 2 — 合规性审查

- **职责**：检查是否违反《民法典》合同编、格式条款规则，及可配置的行业规范（劳动/租赁/买卖等，用一个 `compliance_scope` 参数切换）
- **输出 Schema**：
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

### Agent 3 — 参考判例检索（RAG）

- **职责**：基于前面识别出的风险点，从判例库检索相似合同纠纷案例，作为报告佐证
- **技术**：向量库 + embedding 检索；检索 query 由风险点自动构造
- **输出 Schema**：
```json
[
  {
    "risk_point": "违约金过高",
    "related_cases": [
      {"case_title": "…诉…合同纠纷案", "court": "…", "key_holding": "…", "relevance": "高"}
    ]
  }
]
```

### Agent 4 — 必备条款完备性检查

- **职责**：检查合同必备条款是否缺失
- **必备清单**：当事人信息、标的、数量、质量、价款/报酬、履行期限/地点/方式、违约责任、争议解决方式
- **输出 Schema**：
```json
[
  {"missing_clause": "争议解决方式", "importance": "high", "suggestion": "…"}
]
```

### Agent 5 — 汇总报告（Reducer）

- **职责**：合并四路结果，去重、冲突消解、按风险等级排序，生成报告
- **交叉校验逻辑**：同一 `clause_id` 被多个 Agent 命中时合并为一条并提高置信度；冲突时以法律依据更明确者为准并标注
- **输出**：
  - `report.md`（给人看的 Markdown 报告）
  - `report.json`（机器可读的完整结果，便于评测和二次处理）

---

## 4. 技术选型

| 组件 | 选型 | 说明 |
|---|---|---|
| LLM | DeepSeek API（`deepseek-chat`，复杂推理可用 `deepseek-reasoner`） | 走 OpenAI 兼容接口，代码里做成可换 provider |
| 编排 | **DeepSeek Harness（DSH）** 的 `workflow`（`parallel()` + 汇总），或纯 Python 回退 | 见 §6 Phase 6 |
| PDF 解析 | PyMuPDF / pdfplumber | |
| 向量库 | Chroma（本地、简单）或 FAISS | 判例检索用 |
| Embedding | OpenAI 兼容 embedding API 或 `sentence-transformers`（BGE 系列） | |
| 结构化输出 | LLM 的 JSON 输出 + Pydantic 校验 | 保证 schema 稳定 |
| 语言 | Python 3.10+ | |

**原则**：LLM 调用统一封装成一个 `llm_client`（OpenAI 兼容），这样换模型/换 key 只改配置。

---

## 5. 目录结构

```
contract-review-agent/
├── README.md                 # 架构图 + 快速开始 + 效果对比
├── requirements.txt
├── config/
│   └── config.yaml           # LLM key、模型名、向量库路径、compliance_scope
├── data/
│   ├── raw/                  # 原始合同 PDF
│   ├── annotated/            # 人工标注的评测集（gold 答案）
│   └── cases/                # 判例库（文本/JSON）
├── src/
│   ├── llm_client.py         # 统一 LLM 调用封装（OpenAI 兼容 + JSON 输出）
│   ├── parser.py             # PDF 解析 + 条款切分
│   ├── agents/
│   │   ├── risk.py           # Agent 1
│   │   ├── compliance.py     # Agent 2
│   │   ├── case_retrieval.py # Agent 3（含 RAG）
│   │   ├── completeness.py   # Agent 4
│   │   └── prompts.py        # 所有 prompt 模板集中管理
│   ├── rag/
│   │   └── vector_store.py   # 建库 + 检索
│   ├── report.py             # Agent 5：汇总 + 交叉校验 + 报告生成
│   └── main.py               # 单合同审查入口（命令行）
├── workflow/
│   └── review.workflow.js    # DSH workflow 编排脚本（可选，见 Phase 6）
├── scripts/
│   ├── build_case_index.py   # 建判例向量库
│   └── evaluate.py           # 评测脚本
├── outputs/                  # 生成的报告（不入库）
└── tests/
```

---

## 6. 分阶段实施步骤（Codex 执行）

> 每阶段都有**明确交付物和验收标准**，按顺序做，先跑通单链路再扩展并行。

### Phase 0 — 环境与骨架
- **任务**：建目录、`requirements.txt`、`config.yaml`、`llm_client.py`（能返回 JSON）
- **交付物**：可运行的最小工程骨架
- **验收**：`llm_client` 能调用一次 LLM 并拿到 JSON 结果

### Phase 1 — 文档解析与条款切分
- **任务**：`parser.py` 解析 PDF → 切分条款
- **交付物**：给定一份合同 PDF，输出结构化条款列表
- **验收**：条款切分后，人工抽查 10 条，边界正确率 ≥ 90%

### Phase 2 — 单链路跑通（先只做风险识别）
- **任务**：`agents/risk.py` + `prompts.py`，输入条款列表，输出风险条目 JSON
- **交付物**：`main.py` 能跑"PDF → 条款 → 风险识别 → 简单文本报告"
- **验收**：对一份样例合同，能稳定输出 3 条以上合理风险点，JSON schema 校验通过

### Phase 3 — 扩展到四路审查 Agent
- **任务**：实现 `compliance.py`、`completeness.py`（`case_retrieval.py` 先给占位/空结果）
- **交付物**：四路 Agent 都能独立运行、各自输出符合 schema
- **验收**：四路输出都能通过 Pydantic 校验，且内容可读、无幻觉乱编

### Phase 4 — 判例检索 RAG
- **任务**：`rag/vector_store.py` + `scripts/build_case_index.py`；准备 30~50 条公开判例摘要入库；`case_retrieval.py` 接入检索
- **交付物**：能根据风险点返回相关判例
- **验收**：给定 5 个风险点，检索结果 Top-3 中有 1 条以上相关

### Phase 5 — 汇总报告与交叉校验
- **任务**：`report.py` 实现去重、冲突消解、按风险等级排序；输出 `report.md` + `report.json`
- **交付物**：完整审查报告
- **验收**：同一 clause 被多 Agent 命中时能合并；报告按 高→中→低 排序；格式规范

### Phase 6 — DSH 编排（体现 Harness 能力）
- **任务**：把四路审查用 DSH `workflow` 的 `parallel()` 编排，`barrier` 后汇总
  - 推荐：DSH workflow 做编排层，`parser` 和 `rag` 作为前置步骤
  - 同时保留 Phase 5 的纯 Python 版本作为回退（保证不依赖 DSH 也能跑）
- **交付物**：`workflow/review.workflow.js`
- **验收**：DSH workflow 能并行跑四路审查并产出汇总报告；纯 Python 版本仍可用

### Phase 7 — 评测
- **任务**：准备 20~30 份合同 + 人工标注 gold 答案；`scripts/evaluate.py` 计算指标
- **指标**：风险条款识别 **Precision / Recall / F1**；风险等级判定准确率
- **验收**：跑出至少一组可写进简历的数字（如 "F1 = 0.7x"）

### Phase 8 — Demo 与文档
- **任务**：README（架构图 + 快速开始 + 效果对比表）；录 2~3 分钟 demo 视频
- **交付物**：完整可交付项目
- **验收**：一个陌生环境按 README 能 10 分钟内跑起来

---

## 7. 数据准备（注意合规）

- **合同范本**：公开合同模板（政府/行业协会发布的标准合同范本、公开模板库）
- **判例**：中国裁判文书网公开文书（**仅限学习研究用途，注意脱敏与版权**；可用摘要而非全文）
- **起步策略**：先用手工构造 3~5 份样例合同跑通全链路，再扩充到评测集

---

## 8. 风险与注意事项

1. **LLM JSON 稳定性**：一定要加 Pydantic 校验 + 失败重试 + schema 兜底，否则汇总阶段会崩
2. **幻觉**：判例/法条必须来自检索库或明确标注"仅供参考"，不要凭空编法条号
3. **条款切分是隐藏难点**：中文合同编号格式多样（"第一条 / 第1条 / 1."），先处理常见格式，其余交给 LLM 兜底
4. **评测集标注成本**：先标风险识别这一项（最重要），其他维度可选做

---

## 9. 简历呈现模板（成品后直接套用）

> 基于多智能体编排实现合同智能审查系统：PDF 解析与条款切分后，由**风险识别 / 合规审查 / 判例检索 / 完备性检查**四个 Agent 并行审查，汇总层做交叉校验与风险分级，输出带条款定位与法条依据的结构化报告。
> 在 X 份标注合同上，风险条款识别 F1 达到 0.7x，单份合同审查耗时从 N 分钟降至 M 分钟。

面试必被问："为什么用多 Agent 而不是一次 prompt？"——提前准备答案：**并行提速、职责单一便于独立调优、交叉校验降低幻觉**。
