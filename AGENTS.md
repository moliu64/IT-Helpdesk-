# AGENTS.md — IT 运维工单 Helpdesk 智能体实施说明（直接交给 Codex 执行）

> 本文件是给 Codex 的执行指令。请严格按顺序实施，每阶段完成前自检验收标准。
> 同目录下的 `PLAN.md` 是完整规格说明；冲突时以本文件为准。
>
> ⚠️ 本项目由"合同审查"改造而来：`src/` 里可能残留 `parser.py`、`agents/risk.py` 等**合同项目旧代码**（已归档到 `legacy_contract_review/`）。**不要复用或 import 这些旧文件**，一律按本文件重新实现。`src/llm_client.py` 是通用封装，可直接复用。

---

## 1. 你的任务

实现一个 **IT 运维工单 Helpdesk 智能体**：输入一条工单（文本或 JSON），输出**分诊报告 + 用户回复草稿 + 工程师处理建议 + 自动标签**（Markdown + JSON）。

四个维度必须能**并行执行**：

1. 问题分类（账号 / 网络 / 硬件 / 软件 / 权限 / 邮件 / 安全 / 其他）
2. 优先级与 SLA 评估（P1~P4）
3. 解决方案检索（RAG：历史工单 + 知识库）
4. 路由建议（分派支持组）

最后汇总：合并四路结果、交叉校验、生成交付物。

---

## 2. 硬性规则（必须遵守，违反即返工）

1. **LLM 输出顶层必须是 JSON 对象**，形如 `{"results": [...]}`，**禁止裸数组**。原因：`response_format=json_object` 只接受 `{...}` 顶层。
2. **所有 LLM 输出必须经过 Pydantic 校验**：校验失败自动重试（最多 3 次），仍失败返回空 `results` 并记录错误，绝不崩溃。
3. **禁止编造解决方案/知识库内容**：`matches` 只能来自 RAG 检索结果；检索不到返回空数组，报告里写"未找到匹配方案，建议人工排查"。
4. **LLM 调用统一走 `src/llm_client.py`**：禁止在 Agent 里直接 `openai`/`requests` 调模型。
5. **保留纯 Python 回退**：最终系统必须能**不依赖 DeepSeek Harness 完整运行**（`python -m src.main`）。DSH 编排是可选加分项，不能成为运行前提。
6. **可配置**：模型名、API key、`teams`（支持组）、`categories`（类别）、`sla_map`（优先级时限）、向量库路径全部走 `config/config.yaml`，禁止硬编码。
7. **输出 JSON Schema 严格按 §5 定义**，字段名、层级、枚举值一字不差。

---

## 3. 技术栈与依赖

- 语言：Python 3.10+
- LLM：DeepSeek API（`deepseek-chat`，复杂推理可换 `deepseek-reasoner`），走 **OpenAI 兼容接口**
- 向量库：`Chroma`（本地）
- Embedding：OpenAI 兼容 embedding API 或 `sentence-transformers`（BGE 系列），config 可切换
- 校验：`pydantic`；配置：`pyyaml`

`requirements.txt`：`chromadb`、`pydantic>=2`、`pyyaml`、`openai`、`sentence-transformers`（不再需要 pymupdf）。

---

## 4. 配置与密钥

`config/config.yaml` 结构：

```yaml
llm:
  base_url: "https://api.deepseek.com/v1"
  api_key_env: "LLM_API_KEY"
  model: "deepseek-chat"
  temperature: 0.1
embedding:
  provider: "openai"          # openai | local
  model: "text-embedding-3-small"
rag:
  index_dir: "data/tickets/index"
helpdesk:
  teams: [网络组, 桌面支持组, 应用组, 安全组, 账号权限组, 其他]
  categories: [账号与登录, 网络连接, 硬件设备, 软件应用, 权限申请, 邮件通讯, 安全事件, 其他]
  sla_map:
    P1: 1
    P2: 4
    P3: 8
    P4: 24
```

**规则**：API key 只能从环境变量读取（`os.environ["LLM_API_KEY"]`），禁止写进代码或配置文件。

---

## 5. 数据契约（各 Agent 输出 Schema，必须严格实现）

> 所有 Agent 输出顶层都是 `{"results": [...]}`。

### 5.1 工单解析（`src/ticket_parser.py`）
```json
{
  "ticket": {
    "ticket_id": "",
    "requester": "",
    "title": "",
    "description": "",
    "channel": "portal",
    "created_at": ""
  }
}
```
`channel` 枚举：`email|portal|chat|phone`。

### 5.2 分类（`src/agents/classify.py`）
```json
{"results": [{"category": "网络连接", "subcategory": "VPN", "confidence": 0.92}]}
```
`category` 必须来自 config 的 `helpdesk.categories`；`confidence` 为 0~1 浮点数。

### 5.3 优先级（`src/agents/priority.py`）
```json
{"results": [
  {
    "priority": "P2",
    "sla_hours": 4,
    "impact_scope": "团队",
    "affected_users": 5,
    "business_blocked": false,
    "reason": "..."
  }
]}
```
`priority` 枚举 `P1|P2|P3|P4`；`sla_hours` 从 config 的 `sla_map` 读取（按 priority 查表，禁止硬编码）。

### 5.4 解决方案检索（`src/agents/solution_retrieval.py`）
```json
{"results": [
  {
    "query": "VPN 连接失败",
    "matches": [
      {"source": "KB-1001", "title": "VPN 常见问题排查", "steps": ["重启客户端"], "relevance": "高"}
    ]
  }
]}
```
`matches` 只能来自检索结果；检索不到返回 `"matches": []`。

### 5.5 路由（`src/agents/routing.py`）
```json
{"results": [{"team": "网络组", "reason": "VPN 属网络基础设施问题"}]}
```
`team` 必须来自 config 的 `helpdesk.teams`。

### 5.6 汇总报告（`src/report.py`）
- `outputs/<ticket_id>/report.json`：机器可读完整结果
- `outputs/<ticket_id>/report.md`，结构：
  - 工单基本信息
  - 分诊结论（分类 + 优先级/SLA + 路由）
  - 相似解决方案（来源 + 步骤 + 相关度）
  - 给用户的回复草稿（安抚 + 预计时限 + 自助排查步骤）
  - 给工程师的处理建议（内部）
  - 自动标签（`#网络 #VPN #P2`）

**交叉校验规则**：
1. 分类与路由不一致 → 报告里标注冲突
2. `P1` 但影响面非"全员"且非"业务阻断" → 降级并注明原因
3. 同一事实多 Agent 判断不一致 → 取依据更明确者并标注差异

---

## 6. 目录结构（严格按此创建）

```
├── README.md
├── requirements.txt
├── config/config.yaml
├── data/
│   ├── raw/               # 原始工单样例（txt/json）
│   ├── knowledge/         # 知识库 KB
│   ├── tickets/           # 历史工单库 + index/
│   └── annotated/         # 评测集 gold
├── src/
│   ├── llm_client.py      # 复用现有，无需重写
│   ├── ticket_parser.py
│   ├── main.py
│   ├── report.py
│   ├── agents/
│   │   ├── classify.py
│   │   ├── priority.py
│   │   ├── solution_retrieval.py
│   │   ├── routing.py
│   │   └── prompts.py     # 所有 prompt 集中在此
│   └── rag/
│       └── vector_store.py
├── workflow/helpdesk.workflow.js
├── scripts/build_index.py
├── scripts/evaluate.py
├── outputs/
└── tests/
```

**注意**：`legacy_contract_review/` 目录是归档的旧合同代码，**忽略它、不要 import、不要修改**。

---

## 7. 实施顺序（严格按 Phase 0 → 8，禁止跳步）

> 每个 Phase 先做、自测验收、再进下一个。不要一次性写完所有文件再跑。

### Phase 0 — 环境与骨架
- 复用 `src/llm_client.py`；确认 `config/config.yaml` 为 §4 结构；确认 `requirements.txt`
- 若 `llm_client.py` 缺失或不适配，按 §4 重写
- **验收**：单独跑一个调用，能返回 `{"results":[...]}` 合法 JSON

### Phase 1 — 工单解析与标准化
- 实现 `src/ticket_parser.py`：文本 → §5.1 结构化对象
- 字段抽取：标题、描述、渠道用启发式，其余 LLM 兜底
- **验收**：10 条样例文本，标题/描述/渠道抽取正确率 ≥ 80%

### Phase 2 — 单链路跑通（只做分类）
- 实现 `src/agents/prompts.py` + `classify.py` + `src/main.py`
- 流程：文本 → 解析 → 分类 → 打印结论
- **验收**：一条样例工单稳定输出正确 `category` + `confidence`，通过 Pydantic 校验

### Phase 3 — 扩展到四路 Agent
- 实现 `priority.py`、`routing.py`；`solution_retrieval.py` 先返回 `{"results":[]}` 占位
- **验收**：四路都能独立运行、输出符合 §5 schema、内容合理无幻觉

### Phase 4 — 解决方案检索 RAG
- 实现 `src/rag/vector_store.py` + `scripts/build_index.py`
- **自建语料**：20~30 篇知识库 KB + 50~80 条历史工单（覆盖全类别，先自构造再人工修）
- **验收**：给定 5 个问题，检索 Top-3 中至少 1 条相关

### Phase 5 — 汇总报告与交叉校验
- 实现 `src/report.py`：去重、冲突消解、生成 `report.md` + `report.json` + 回复草稿 + 标签
- **验收**：分类/路由冲突能标注；P1 与影响面不匹配会被降级；报告结构符合 §5.6

### Phase 6 — DSH 编排（可选加分项）
- 用 DSH `workflow` 的 `parallel()` 编排四路，barrier 后汇总
- **验收**：DSH 能并行跑四路出报告；`python -m src.main` 纯 Python 版本仍可用

### Phase 7 — 评测
- 30~50 条工单 + 人工标注；`scripts/evaluate.py` 计算：分类准确率、优先级准确率、RAG Top-3 命中率、端到端耗时
- **验收**：输出至少一组可写进简历的数字

### Phase 8 — README 与 Demo
- README：架构图 + 快速开始 + 效果对比表；录 2~3 分钟 demo
- **验收**：陌生环境按 README 10 分钟内能跑通

---

## 8. 完成定义（Definition of Done）

- [ ] `python -m src.main --input 样例工单.txt` 端到端产出 `report.md` + `report.json`
- [ ] 四路可并行执行（Python 多线程/进程 或 DSH `parallel()`）
- [ ] 所有 LLM 输出顶层为 `{"results":[...]}`，经 Pydantic 校验 + 重试，无崩溃路径
- [ ] 无编造解决方案（检索不到即标注"建议人工排查"）
- [ ] 分类/优先级/路由/团队 全部从 config 读取，无硬编码
- [ ] 有评测脚本和至少一组指标数字
- [ ] README 可让陌生人在 10 分钟内跑通

---

## 9. 注意事项（易踩坑）

1. **JSON 顶层必须是对象**（`{"results":[...]}`），不要重蹈合同项目裸数组被 `json_object` 拒绝的覆辙。
2. **优先级宁可保守**：P1 误报代价大，交叉校验里做好降级。
3. **并发线程安全**：四路并行时 `llm_client` 和 Chroma 要能并发（锁或每线程独立 client）。
4. **`legacy_contract_review/` 只归档不引用**。
5. `outputs/` 和向量索引目录加入 `.gitignore`。
