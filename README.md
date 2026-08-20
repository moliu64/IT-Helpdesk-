# IT 运维工单 Helpdesk 智能体

输入一条 IT 工单文本或 JSON，系统将工单标准化后交给四路审查 Agent：问题分类、优先级与 SLA、历史工单/知识库 RAG 检索、支持组路由；最后生成分诊报告、用户回复草稿、工程师处理建议和自动标签。

## 架构

```mermaid
flowchart TD
    A[工单文本/JSON] --> B[工单解析与标准化]
    B --> C1[问题分类]
    B --> C2[优先级与 SLA]
    B --> C3[解决方案 RAG]
    B --> C4[路由建议]
    C1 --> D[汇总与交叉校验]
    C2 --> D
    C3 --> D
    C4 --> D
    D --> E[report.json + report.md]
```

四路 Agent 的 JSON 输出都要求顶层为 `{"results": [...]}`，并经过 Pydantic 校验和最多三次重试。汇总层会检查分类-路由冲突，并对不满足影响面条件的 P1 自动降级。

## 快速开始

PowerShell：

```powershell
$env:LLM_API_KEY="你的 DeepSeek API Key"
$env:HF_HUB_OFFLINE="1"
$env:TRANSFORMERS_OFFLINE="1"
python -m pip install -r requirements.txt
python scripts/build_index.py
python -m src.main --input data/raw/sample_ticket.txt
```

Linux/macOS：

```bash
export LLM_API_KEY="你的 DeepSeek API Key"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
python -m pip install -r requirements.txt
python scripts/build_index.py
python -m src.main --input data/raw/sample_ticket.txt
```

报告输出到 `outputs/<ticket_id>/report.json` 和 `outputs/<ticket_id>/report.md`。API key 只从 `LLM_API_KEY` 环境变量读取，不写入代码或配置文件。

## RAG 与数据

RAG 使用本地缓存的 BGE 中文模型 `BAAI/bge-small-zh-v1.5` 和 Chroma。配置中的 `embedding.provider` 为 `local`，不调用 OpenAI embedding；运行建库或检索前应设置两个离线变量，避免访问 huggingface.co：

```powershell
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"
```

当前语料包含 24 篇 KB 和 64 条历史工单，历史工单 8 个类别各 8 条，resolution 是针对具体问题的 3~5 步操作步骤。

## 效果示例

对 `data/raw/sample_ticket.txt`（"VPN 无法连接"）的真实报告片段：

```text
## 分诊结论
- 分类：网络连接 / VPN连接问题（置信度 0.95）
- 优先级：P3，SLA：8 小时
- 路由：网络组

## 相似解决方案
- 来源：HIST-0009；标题：VPN 连接超时；相关度：高；步骤：确认客户端能访问 VPN 网关；校准系统时间并清理 VPN 缓存；切换到备用网关重试；导出客户端日志和错误时间交网络组
- 来源：KB-0001；标题：VPN 客户端连接超时；相关度：高；步骤：确认本地网络可访问互联网；校准系统时间并重新登录；切换网络后重试；仍失败时收集客户端日志交网络组

## 自动标签
#网络连接 #VPN连接问题 #P3
```

## Phase 7 评测

评测集位于 `data/annotated/helpdesk_eval.json`，共 32 条与历史工单不重复的工单，category 和 priority 均有 gold 标注。运行：

```powershell
$env:LLM_API_KEY="你的 DeepSeek API Key"
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"
python scripts/evaluate.py
```

脚本计算分类准确率、优先级准确率和 RAG Top-3 命中率，并写入 `outputs/eval_result.json`。当前 32 条评测集的实测结果：

| 指标 | 结果 |
|---|---|
| 分类准确率 | 87.5%（28/32，其余 4 条均为"其他"类被判为具体类别） |
| 优先级准确率 | 65.6%（21/32；11 条误判全部为相邻一级，±1 级内 100%，无 P1 漏判） |
| RAG Top-3 命中率 | 100%（检索覆盖率，语料覆盖全部 8 个类别） |

## 为什么使用多 Agent

分类、SLA、检索和路由职责单一，可以并行提速并独立调优；汇总层再做交叉校验，尤其能降低 P1 误判和分类/路由不一致带来的分派错误。解决方案只展示 RAG 实际检索结果，检索不到时明确提示人工排查，不编造知识库内容。

## 测试

```powershell
python -m pytest tests -q
```

