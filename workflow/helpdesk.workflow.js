// ============================================================
// DSH workflow 脚本主体：IT Helpdesk 工单智能分诊（多智能体编排版）
//
// 这是与 src/main.py（ThreadPoolExecutor 并行版）等价的 Harness 编排实现，
// 用 DSH workflow 原生的 parallel() + subagent fan-out 编排四路 Agent。
//
// 使用方式（在 DeepSeek Harness 的 workflow 工具中）：
//   meta:
//     name: "helpdesk-triage"
//     description: "IT 运维工单四路并行分诊：分类/优先级/解决方案/路由"
//     phases:
//       - title: "解析工单"
//       - title: "三路并行审查"
//       - title: "路由建议"
//       - title: "汇总报告"
//   args:
//     ticket: "标题：VPN 无法连接\n描述：今天开始连接公司 VPN 超时，无法访问内部系统。"
//   script: 本文件内容
//
// 注意：本编排版由 subagent 直接完成解析/分类/优先级/路由，
//       解决方案由 subagent 读取 data/knowledge 与 data/tickets 语料后检索；
//       生产级 RAG（Chroma+BGE）在 Python 版 src/rag/vector_store.py 中实现。
// ============================================================

const ticketText = (args && args.ticket) || "";
if (!ticketText) {
  throw new Error("缺少 args.ticket，请传入工单文本");
}

// ---------- 1. 解析工单 ----------
phase("解析工单");
const parsed = await agent(
  `你是 IT Helpdesk 工单解析器。把下面的工单文本解析成标准 JSON 对象（只输出 JSON，不要解释、不要 Markdown）。
channel 只能是 email/portal/chat/phone 之一；缺失字段用空字符串。

工单文本：
${ticketText}`,
  {
    label: "ticket-parser",
    schema: {
      type: "object",
      properties: {
        ticket: {
          type: "object",
          properties: {
            ticket_id: { type: "string" },
            requester: { type: "string" },
            title: { type: "string" },
            description: { type: "string" },
            channel: { type: "string", enum: ["email", "portal", "chat", "phone"] },
            created_at: { type: "string" }
          },
          required: ["title", "description"]
        }
      },
      required: ["ticket"]
    }
  }
);
const ticket = parsed.ticket;

// ---------- 2. 三路并行审查（分类 / 优先级 / 解决方案检索） ----------
phase("三路并行审查");
const [classification, priority, solutions] = await parallel([
  () => agent(
    `你是 IT Helpdesk 工单分类器。category 只能从以下列表选择：账号与登录、网络连接、硬件设备、软件应用、权限申请、邮件通讯、安全事件、其他。
只输出 JSON 对象，严格格式 {"results":[{"category":"...","subcategory":"...","confidence":0.0}]}，confidence 在 0~1 之间。

工单：${JSON.stringify(ticket)}`,
    {
      label: "classify",
      schema: {
        type: "object",
        properties: {
          results: {
            type: "array",
            items: {
              type: "object",
              properties: {
                category: { type: "string" },
                subcategory: { type: "string" },
                confidence: { type: "number" }
              },
              required: ["category", "subcategory", "confidence"]
            }
          }
        },
        required: ["results"]
      }
    }
  ),
  () => agent(
    `你是 IT Helpdesk 优先级评估器。严格按以下标准判定 priority（P1 最高）：
- P1：全员或大范围受影响，或核心业务被阻断（如整层断网、勒索软件、生产系统宕机）
- P2：影响一个团队/多人，或安全事件（账号异常登录、病毒、钓鱼），或关键任务受阻、有明确截止时间
- P3：单人或少数用户受影响，常规故障，存在临时绕行方案
- P4：咨询类、低影响、无时间压力，可排队处理
只输出 JSON 对象，严格格式 {"results":[{"priority":"P1|P2|P3|P4","sla_hours":数字,"impact_scope":"单人|团队|全员","affected_users":整数,"business_blocked":布尔值,"reason":"..."}]}。

工单：${JSON.stringify(ticket)}`,
    {
      label: "priority",
      schema: {
        type: "object",
        properties: {
          results: {
            type: "array",
            items: {
              type: "object",
              properties: {
                priority: { type: "string", enum: ["P1", "P2", "P3", "P4"] },
                sla_hours: { type: "number" },
                impact_scope: { type: "string", enum: ["单人", "团队", "全员"] },
                affected_users: { type: "number" },
                business_blocked: { type: "boolean" },
                reason: { type: "string" }
              },
              required: ["priority", "sla_hours", "impact_scope", "affected_users", "business_blocked", "reason"]
            }
          }
        },
        required: ["results"]
      }
    }
  ),
  () => agent(
    `你是 IT Helpdesk 解决方案检索员。先读取项目里的知识库文件（data/knowledge/KB-*.md）和历史工单（data/tickets/historical_tickets.json），从中找出与下面工单最相关的解决方案（最多 3 条）。
只输出 JSON 对象，严格格式 {"results":[{"query":"...","matches":[{"source":"KB-0001 或 HIST-0001","title":"...","steps":["...","..."],"relevance":"高|中|低"}]}]}。
如果语料里没有匹配方案，matches 返回空数组 []，禁止编造。

工单：${JSON.stringify(ticket)}`,
    {
      label: "solution-retrieval",
      schema: {
        type: "object",
        properties: {
          results: {
            type: "array",
            items: {
              type: "object",
              properties: {
                query: { type: "string" },
                matches: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: {
                      source: { type: "string" },
                      title: { type: "string" },
                      steps: { type: "array", items: { type: "string" } },
                      relevance: { type: "string", enum: ["高", "中", "低"] }
                    },
                    required: ["source", "title", "steps", "relevance"]
                  }
                }
              },
              required: ["query", "matches"]
            }
          }
        },
        required: ["results"]
      }
    }
  )
]);

// ---------- 3. 路由（依赖分类结果，串行） ----------
phase("路由建议");
const routing = await agent(
  `你是 IT Helpdesk 路由器。team 只能从以下列表选择：网络组、桌面支持组、应用组、安全组、账号权限组、其他。
只输出 JSON 对象，严格格式 {"results":[{"team":"...","reason":"..."}]}。

工单：${JSON.stringify(ticket)}
分类结果：${JSON.stringify(classification)}`,
  {
    label: "routing",
    schema: {
      type: "object",
      properties: {
        results: {
          type: "array",
          items: {
            type: "object",
            properties: {
              team: { type: "string" },
              reason: { type: "string" }
            },
            required: ["team", "reason"]
          }
        }
      },
      required: ["results"]
    }
  }
);

// ---------- 4. 汇总报告（交叉校验 + 回复草稿） ----------
phase("汇总报告");
const report = await agent(
  `你是 IT Helpdesk 汇总器。基于四路结果做交叉校验（分类与路由一致性；P1 是否满足"全员/业务阻断"条件，不满足则降级），
并生成分诊报告。只输出 JSON 对象，字段包含：
- triage: {classification, priority, sla_hours, routing}
- solutions: 解决方案结果
- conflicts: 字符串数组（交叉校验发现的不一致）
- tags: 字符串数组（如 ["#网络连接", "#VPN", "#P3"]）
- user_reply_draft: 给用户的回复草稿
- engineer_advice: 给工程师的处理建议

分类：${JSON.stringify(classification)}
优先级：${JSON.stringify(priority)}
解决方案：${JSON.stringify(solutions)}
路由：${JSON.stringify(routing)}`,
  { label: "reducer" }
);

return { ticket, classification, priority, solutions, routing, report };
