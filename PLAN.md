# IT 运维工单 Helpdesk 智能体 — 项目规划与技术方案（Spec）

> 本文件是交给 Codex 执行的项目规格说明书。
> 目标：做出一个**可演示、可评测、能写进简历**的多智能体垂直应用。

---

## 0. 项目一句话定位

输入一条 IT 运维工单（文本或结构化 JSON），系统用多个**并行 AI Agent**分别完成：

1. 问题分类（账号 / 网络 / 硬件 / 软件 / 权限 / 安全 …）
2. 优先级与 SLA 评估（P1~P4、预计处理时限）
3. 历史工单与知识库检索（RAG，相似解决方案）
4. 路由建议（分派到哪个支持组）

最后汇总生成：**分诊报告 + 给用户的回复草稿 + 给工程师的处理建议 + 自动标签**。

---

## 1. 简历价值（为什么要做这个）

- **多智能体并行 + 汇总**，典型 Agent 编排场景，面试好讲
- 有**可量化评测指标**（分类准确率、优先级判定准确率、端到端处理耗时）
- 贴近真实业务（ITSM / Helpdesk，对应 Jira Service Management、ServiceNow 等工具的 AI 化方向）

---

## 2. 系统架构

```
输入(工单文本/JSON)
   │
   ▼
[工单解析与标准化]  ── 抽取 ticket 字段（标题、描述、渠道、用户）
   │
   ▼
┌────────────── 四路并行 ──────────────┐
│ ① 问题分类   ② 优先级/SLA           │
│ ③ 解决方案检索(RAG)  ④ 路由建议      │
└─────────────────────────────────────┘
   │
   ▼
[汇总 + 交叉校验] ── 生成分诊报告、回复草稿、处理建议、自动标签
   │
   ▼
输出(报告: Markdown + JSON)
```

关键设计点：**四路互相独立、可并行**，最后一步做**交叉校验**（例如"分类=VPN 问题"与"路由=网络组"应互相印证；"优先级=P1"必须与"影响面=全员/业务阻断"匹配，否则降级并标注）。

---

## 3. Agent 分工设计（核心）

每个 Agent 都有明确的**输入契约、输出 JSON Schema**。

> 统一约定：**所有 Agent 的 LLM 输出顶层必须是 JSON 对象，字段名 `results` 承载数组**（`{"results": [...]}`）。
> 原因：`response_format=json_object` 要求顶层是 `{...}`，裸数组会被拒。这是上一版合同项目踩过的坑，必须避免。

### Agent 0 — 工单解析与标准化（`ticket_parser.py`，非 LLM 为主）

- **职责**：从原始文本/JSON 抽取标准工单字段
- **输入**：一段自由文本（邮件、聊天记录、门户表单提交）或结构化 JSON
- **输出 Schema**：
```json
{
  "ticket": {
    "ticket_id": "",
    "requester": "",
    "title": "",
    "description": "",
    "channel": "email|portal|chat|phone",
    "created_at": ""
  }
}
```
- **技术**：正则/启发式抽取常见字段（标题、联系方式、"我"自称等），其余 LLM 兜底补全

### Agent 1 — 问题分类（`agents/classify.py`）

- **职责**：判定工单类别 + 子类别 + 置信度
- **类别清单**（在 config 里可配）：账号与登录、网络连接、硬件设备、软件应用、权限申请、邮件通讯、安全事件、其他
- **输出 Schema**：
```json
{"results": [
  {"category": "网络连接", "subcategory": "VPN", "confidence": 0.92}
]}
```

### Agent 2 — 优先级与 SLA 评估（`agents/priority.py`）

- **职责**：评估优先级、SLA 时限、影响面
- **优先级枚举**：`P1|P2|P3|P4`（P1 最高）；SLA 时限从 config 的 `sla_map` 读取，禁止硬编码
- **判定依据**：影响范围（单人/团队/全员）、是否阻断业务、紧急程度
- **输出 Schema**：
```json
{"results": [
  {
    "priority": "P2",
    "sla_hours": 4,
    "impact_scope": "团队",
    "affected_users": 5,
    "business_blocked": false,
    "reason": "团队共享盘不可访问，影响协作但不阻断核心生产"
  }
]}
```

### Agent 3 — 解决方案检索（`agents/solution_retrieval.py`，RAG）

- **职责**：根据工单内容构造 query，从**历史工单库 + 知识库**检索相似解决方案；LLM 负责构造 query 与汇总去重，检索由向量库完成
- **输出 Schema**：
```json
{"results": [
  {
    "query": "VPN 连接失败",
    "matches": [
      {"source": "KB-1001", "title": "VPN 常见问题排查", "steps": ["重启客户端", "检查账号锁定"], "relevance": "高"}
    ]
  }
]}
```
- **规则**：`matches` 必须来自检索结果，检索不到时返回空数组并在报告里写"未找到匹配方案，建议人工排查"，**禁止编造解决方案**

### Agent 4 — 路由建议（`agents/routing.py`）

- **职责**：建议分派到哪个支持组
- **路由目标**（config 可配）：网络组、桌面支持组、应用组、安全组、账号权限组、其他
- **输出 Schema**：
```json
{"results": [
  {"team": "网络组", "reason": "VPN 连接问题属于网络基础设施，网络组有对应权限和工具"}
]}
```

### Agent 5 — 汇总与回复草稿（`report.py`，Reducer）

- **职责**：合并四路结果、交叉校验、生成最终交付物
- **交叉校验规则**：
  1. 分类与路由一致性：分类为网络类但路由到应用组 → 需在报告中标注冲突
  2. 优先级与影响面匹配：`P1` 必须对应"全员或业务阻断"，否则降级并注明
  3. 多 Agent 对同一事实有不同判断时，取置信度/依据更明确者并标注差异
- **输出**：
  - `outputs/<ticket_id>/report.json`：机器可读完整结果
  - `outputs/<ticket_id>/report.md`：给人看的报告，结构：
    - 工单基本信息
    - 分诊结论（分类 + 优先级/SLA + 路由）
    - 相似解决方案（来源 + 步骤 + 相关度）
    - 给用户的回复草稿（安抚 + 预计时限 + 可自助执行的排查步骤）
    - 给工程师的处理建议（内部，含风险提示）
    - 自动标签（`#网络 #VPN #P2` 形式）

---

## 4. 技术选型

| 组件 | 选型 | 说明 |
|---|---|---|
| LLM | DeepSeek API（`deepseek-chat`，复杂推理可换 `deepseek-reasoner`） | OpenAI 兼容接口，代码里可换 provider |
| 编排 | DeepSeek Harness（DSH）`workflow`（`parallel()` + 汇总），或纯 Python 回退 | 见 Phase 6 |
| 向量库 | Chroma（本地、简单） | 历史工单 + 知识库检索 |
| Embedding | OpenAI 兼容 embedding API 或 `sentence-transformers`（BGE 系列） | config 里可切换 |
| 结构化输出 | LLM 的 JSON 输出 + Pydantic 校验 | 保证 schema 稳定 |
| 语言 | Python 3.10+ | |

**原则**：LLM 调用统一封装成 `llm_client`（OpenAI 兼容），换模型/换 key 只改配置。

---

## 5. 目录结构

```
it-helpdesk-agent/
├── README.md
├── requirements.txt
├── config/
│   └── config.yaml            # LLM、embedding、rag、helpdesk(teams/categories/sla_map)
├── data/
│   ├── raw/                   # 原始工单样例（txt/json）
│   ├── knowledge/             # 知识库文章（KB，用于 RAG）
│   ├── tickets/               # 历史工单库（RAG 语料）+ index/
│   └── annotated/             # 评测集 gold 答案
├── src/
│   ├── llm_client.py          # 统一 LLM 调用封装（可复用）
│   ├── ticket_parser.py       # 工单解析与标准化
│   ├── main.py                # 命令行入口
│   ├── report.py              # 汇总 + 交叉校验 + 回复草稿
│   ├── agents/
│   │   ├── classify.py        # Agent 1
│   │   ├── priority.py        # Agent 2
│   │   ├── solution_retrieval.py  # Agent 3（含 RAG）
│   │   ├── routing.py         # Agent 4
│   │   └── prompts.py         # 所有 prompt 集中管理
│   └── rag/
│       └── vector_store.py    # 建库 + 检索
├── workflow/
│   └── helpdesk.workflow.js   # DSH 编排（Phase 6，可选）
├── scripts/
│   ├── build_index.py         # 建历史工单/知识库向量索引
│   └── evaluate.py            # 评测脚本
├── outputs/                   # 生成结果，.gitignore
└── tests/
```

---

## 6. 分阶段实施步骤（Codex 执行）

> 每阶段都有**明确交付物和验收标准**，按顺序做，先跑通单链路再扩展并行。

### Phase 0 — 环境与骨架
- 任务：目录结构、`requirements.txt`、`config/config.yaml`、`llm_client.py`
- 交付物：可运行的最小工程骨架
- 验收：`llm_client` 能调用一次 LLM 并拿到 `{"results": [...]}` 形式的 JSON

### Phase 1 — 工单解析与标准化
- 任务：`ticket_parser.py`，从文本抽取标准工单字段
- 交付物：给定一段工单文本，输出 §3 Agent 0 的结构化对象
- 验收：对 10 条样例文本，字段抽取（标题/描述/渠道）人工抽查正确率 ≥ 80%

### Phase 2 — 单链路跑通（先只做分类）
- 任务：`agents/prompts.py` + `agents/classify.py` + `main.py`
- 流程：工单文本 → 解析 → 分类 → 打印简单结论
- 验收：一条样例工单能稳定输出正确类别 + 置信度，且通过 Pydantic 校验

### Phase 3 — 扩展到四路 Agent
- 任务：实现 `priority.py`、`routing.py`；`solution_retrieval.py` 先返回空占位
- 验收：四路都能独立运行、输出符合 §3 schema、内容合理无幻觉

### Phase 4 — 解决方案检索 RAG
- 任务：`rag/vector_store.py` + `scripts/build_index.py`
- 语料：**自建 20~30 篇知识库 KB + 50~80 条历史工单样例**（覆盖各类别，数据自构造）
- 验收：给定 5 个问题，检索 Top-3 中至少 1 条相关

### Phase 5 — 汇总报告与交叉校验
- 任务：`report.py` 实现去重、冲突消解、生成 `report.md` + `report.json` + 回复草稿 + 标签
- 验收：分类/路由不一致能被标注；P1 与影响面不匹配会被降级；报告结构符合 §3 Agent 5

### Phase 6 — DSH 编排（可选加分项）
- 任务：用 DSH `workflow` 的 `parallel()` 编排四路，barrier 后汇总
- 前置：`ticket_parser` 和 RAG 建索引作为编排外步骤
- 验收：DSH 能并行跑四路并出汇总报告；`python -m src.main` 纯 Python 版本仍可完整运行

### Phase 7 — 评测
- 任务：准备 30~50 条工单 + 人工标注 gold；`scripts/evaluate.py` 计算指标
- 指标：分类准确率、优先级判定准确率、RAG Top-3 命中率、端到端处理耗时
- 验收：输出至少一组可写进简历的数字

### Phase 8 — README 与 Demo
- 任务：README（架构图 + 快速开始 + 效果对比）；录 2~3 分钟 demo
- 验收：陌生环境按 README 10 分钟内能跑通

---

## 7. 数据准备

- **知识库**：自写 20~30 篇常见 IT 问题解决方案（VPN、密码重置、共享盘、打印机、邮箱等）
- **历史工单**：自构造 50~80 条工单样例（覆盖全类别 + 不同优先级）
- **评测集**：30~50 条带人工标注（类别、优先级为必标项）
- 数据可用脚本批量生成初稿，再人工修正，保证质量

---

## 8. 风险与注意事项

1. **LLM JSON 稳定性**：顶层必须是 `{"results":[...]}` 对象，Pydantic 校验 + 重试 + 兜底，否则汇总会崩
2. **禁止编造解决方案**：`matches` 只能来自检索结果，检索不到明确标注"建议人工排查"
3. **优先级判定要保守**：拿不准时宁低勿高（P1 误报代价大），交叉校验可降级
4. **评测集标注成本**：优先标"分类"和"优先级"两项，其余可选做

---

## 9. 简历呈现模板（成品后直接套用）

> 基于多智能体编排实现 IT 工单智能分诊系统：工单解析标准化后，由**问题分类 / 优先级与 SLA / 解决方案检索(RAG) / 路由建议**四个 Agent 并行处理，汇总层做交叉校验与回复草稿生成，输出分诊报告 + 用户回复 + 工程师处理建议 + 自动标签。
> 在 X 条标注工单上分类准确率达到 0.9x，优先级判定准确率 0.8x，单条工单从录入到出分诊报告耗时 N 秒。

面试必被问："为什么用多 Agent 而不是一次 prompt？"——答案：**并行提速、职责单一便于独立调优、交叉校验降低误判（尤其 P1 优先级）**。
