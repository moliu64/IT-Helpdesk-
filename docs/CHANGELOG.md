# 变更记录

本文件记录项目从合同审查改造为 IT 运维工单智能体之后的主要变更。README 只保留最近更新摘要，完整历史在此维护。

## 2026-10-23：RAG 文档入库智能体（知识条语义拆分）

- 修复 RAG 文档导入“直接按 900 字切块”的问题：上传 PDF/DOCX/MD/TXT 后不再生成文件名 + 半截话的 `FILE-...` 碎片，而是由新增的「文档入库智能体」按文档结构拆成一条条自包含知识条，每条包含具体标题、完整正文（现象/排查与解决）和可执行步骤。
- 新增 `src/rag/knowledge_agent.py`：LLM 智能体拆分（Prompt 位于 `src/agents/prompts.py` 的 `KNOWLEDGE_SPLIT_SYSTEM`）、Pydantic 校验与重试、长文档分窗、去重、AI 结果与规则结构的覆盖校验；支持 `auto`（默认，AI 优先/规则兜底）、`agent`、`rule` 三种模式，配置在 `rag.ingest`，可用 `HELPDESK_RAG_INGEST_MODE` 覆盖。
- 新增专用导入工具 `src/rag/import_tool.py`（`import_document_to_rag` function-calling schema、预览、落库）与命令行 `scripts/import_rag.py`（`--dry-run` / `--mode` / `--no-rebuild` / `--store` / `--index-dir` / `--json`）。
- 接入现有链路：`managed_store.import_file()` 先拆分再原子替换同名文档；`POST /api/rag/import` 新增返回 `mode`/`warnings` 并保持原有字段兼容；`/rag` 页面上传 Toast 区分“AI 智能拆分 / 规则拆分 / AI 不可用已回退”；索引文本改为“标题 + 正文”，按故障名（如“Too many open files”“crontab 导致磁盘爆满”）检索的 Top-1 命中明显提升。
- 种子导入同样改为结构拆分（`documents_from_file`），无结构长文本按句子边界拆到 `max_entry_chars`；PDF 物理换行合并，`steps` 从显式列表或“排查与解决”段落抽取。
- 验证：`pytest tests -q` **82 passed**（新增拆分器/导入工具测试），Ruff、compileall 通过；用真实文档 `新建 Microsoft Word 文档.docx`（10 条编号故障）与 `新建 Microsoft Word 文档 (2).pdf`（10 条编号故障、2 页）在真实 LLM + 生产知识库验证：两篇各拆成 10 条知识条，PDF 原 3 个碎片被替换；HTTP `/api/rag/library`、`/api/rag/entries`、`/api/rag/search` 均验证通过；真实索引 108 条知识，`crontab 导致磁盘爆满`、`文件系统变为只读` 等查询 Top-1 命中且相关度为“高”。
- 文档：新增 `docs/RAG_INGEST_AGENT.md`（完整 Prompt、工具 Schema、兜底/覆盖策略与验证记录），同步 README、ARCHITECTURE。

## 2026-10-23：磨砂玻璃 UI 与静默单实例控制面板

- 四个 Web 界面统一升级为磨砂玻璃（glassmorphism）视觉：环境渐变背景 + 多层模糊色块，侧栏/顶栏/卡片/输入框/弹窗/表格等表面使用半透明白 + `backdrop-filter` 模糊 + 柔和高光与阴影；按钮增加 hover 上浮、按下回弹、主按钮扫光等微动画，并支持 `prefers-reduced-motion` 降级。仅改动 CSS 与装饰元素，未改变任何页面 ID、JS 逻辑与既有功能。
- 控制面板新增静默启动器 `scripts/launch_panel.vbs`，`项目控制面板.bat` 改用 `wscript` 启动，不再创建任何可见/隐藏的控制台终端窗口；面板脚本增加单实例互斥锁，重复双击只会把已有面板恢复到前台，不再叠加多个 PowerShell 面板窗口。
- 面板生命周期明确为“启动后可关闭、项目后台常驻”：关闭面板不会停止服务，再次打开只查看状态，只有点击「停止项目」才会停止项目；关闭面板时不会中断仍在后台执行的启动/停止流程。
- 兼容性：`.ps1` 保持 UTF-8 BOM，`.bat`/`.vbs` 使用 CRLF；面板脚本通过 PowerShell AST 语法检查。
- 验证：面板端到端 16/16 通过（单实例、无终端窗口、关闭面板服务不断、启动中关闭面板仍能后台完成启动）；玻璃 UI 模拟检查 19/19、隔离环境真实接口 15/15 通过，`node --check` 4/4，全量测试通过。

## 2026-10-22：RAG 知识库管理页与知识条增删改迁

- 新增独立的 RAG 知识库管理页 `/rag`（后台点击「管理 RAG 知识库」在新标签页打开）：左侧按文档分组（知识库 KB / 历史工单 / 导入文件 / 手工），右侧按文档展示可编辑的知识条；支持单条新增、编辑、删除、移动（可移动到已有或新建文档）、启用/停用，跨文档搜索、上传文件、重建索引与统计。
- 引入持久化的知识条存储 `data/knowledge/managed_entries.json`（`HELPDESK_RAG_STORE` 可覆盖；已加入 `.gitignore`）：首次运行自动把现有 KB、历史工单和导入文件灌入，之后成为 Chroma 索引的唯一数据源；每次修改会自动重建索引。
- 修复上传导入：`POST /api/rag/import` 现在解析文件、切片、写入知识库并**自动重建索引**（响应返回 `document/chunks/indexed`），同名文件重复上传会替换旧知识条且不会 ID 冲突；扫描版 PDF 等提取不到文本时返回 400 友好提示，不再静默失败。
- 新增管理 API：`/api/rag/library`、`/api/rag/entries`（增删改移）、`/api/rag/documents/delete`；`/api/rag/rebuild` 与 `DELETE /api/rag/files` 改为走知识条存储；`/api/rag/status` 增加 `managed_entries` 和 `library_ready`。旧接口保持兼容。
- 索引隔离：新增 `HELPDESK_RAG_INDEX_DIR` / `HELPDESK_RAG_SOURCE_ROOT`，方便测试环境使用独立索引与语料，避免污染生产数据。
- 验证：全量测试 `65 passed`（新增 19 条知识库管理测试）；新页面模拟运行 33/33、隔离环境真实接口运行 24/24 通过；后台与用户端页面回归通过。

## 2026-10-22：控制面板终端窗口/关闭卡死修复与启动脚本健壮性

- 控制面板改为无控制台窗口运行：`.bat` 以 `-WindowStyle Hidden` 启动，面板脚本再通过 Win32 `ShowWindow` 隐藏宿主控制台，双击后不再残留一直闪烁的终端窗口。
- “停止项目”改为后台异步执行：旧实现用 `-Wait` 同步等待，`stop.ps1` 一旦卡在 WMI/网络查询上会冻结整个面板、导致无法关窗；现在面板保持可操作，60 秒超时自动结束停止进程，关闭面板也永远不阻塞且不会停止项目。
- 面板增加启动/停止/打开网站异常处理与 UI 线程未处理异常捕获（异常显示在面板日志里，不再弹出阻塞对话框）；重复点击启动/停止有状态保护。
- `stop.ps1`：CIM 查询增加超时；兜底清理只针对本仓库、且命令行包含本项目端口的 python/cloudflared，不再误杀仓库内其他测试进程；端口被非本项目进程占用时只提示不杀。
- `项目控制面板.bat` 改为 CRLF 换行并用隐藏方式启动，修复 LF 换行导致 cmd 拆行、启动失败的问题。
- `ui/server.py` 访问日志改为实时 flush，`output/online-app.log` 能立即看到请求与慢请求记录。
- 验证：PowerShell AST 语法检查通过、pytest `46 passed`、GUI 端到端测试 PASS（停止 → 面板自动启动 → 状态“运行中” → 全程无可见终端窗口 → 关闭面板 → 服务保持运行）。

## 2026-10-22：后台登录改为 Cookie 会话，修复流程图为空与库内文档无法查看

- 后台认证从 HTTP Basic 改为 `/login` 登录页 + HMAC 签名的 HttpOnly `helpdesk_admin` Cookie（默认 30 天，可用 `HELPDESK_ADMIN_SESSION_DAYS` 调整，改密码即失效）。响应中不再出现 `WWW-Authenticate`，从根本上消除浏览器反复弹出的“登录以访问此站点”原生认证框。
- 后台 LangGraph 流程图重绘：真实度量节点尺寸后绘制圆角正交连线和箭头，状态色（待运行/运行中/已完成/异常）、图例、实时时间戳，桌面与 390px 移动端均自适应。
- 修复“查看库内文档”一直停在“加载中…”：统一 `apiJson` 请求封装，401 自动跳转登录，失败展示错误与“重试”，成功显示“共 N 篇”文档卡片。
- 新增管理员专用 `GET /api/backend/trace`，后台浏览器（无聊天身份 Cookie）也能加载运行事件，流程图不再全部显示“待运行”。
- 用户端主页面整体重做（参考 DeepSeek Harness 的浅色极简风格）：272px 侧边栏（新对话 / 最近对话 / 身份 / 管理后台入口）、56px 标题栏、居中聊天流、底部圆角输入框；用户气泡、助手回复、运行轨迹折叠面板、输入中动画、错误重试、在线搜索开关、提交工单弹窗（含分诊结果卡片）全部内置在 `ui/index.html` 中，移动端自动切换为抽屉式侧栏。
- 服务端不再对 `ui/index.html` 做运行时字符串注入，`/` 直接返回静态页面；新增 `tests/test_user_page.py` 覆盖静态返回与页面契约。
- RAG 性能优化：启动时预热本地 BGE/Chroma（生产启动脚本设置 `HELPDESK_RAG_PREWARM=1`，也支持 `background` / `0`），后台与对话的首次检索从约 5.8 秒降到约 30 毫秒；`/api/rag/status` 增加 `search_ready` 字段；LLM 客户端复用同一个 OpenAI/httpx 连接，减少多次模型调用的 TLS 握手开销；RAG 检索、重建和对话的慢请求会写入运行日志便于排查。
- 安全回归测试 40 → `42 passed`；Playwright 真浏览器端到端：后台 11/11、用户主页面 18/18 通过。
- RAG/性能相关测试补充后，当时全量测试为 `46 passed`。

## 2026-09-12：全项目 Review 与线上稳定性优化

- 修复 Windows 中文启动入口乱码，以及项目路径含空格时 Cloudflare Tunnel 配置路径被截断的问题。
- 后台页面和管理 API 增加 Basic Auth；用户历史会话改用服务端签名的 HttpOnly Cookie 隔离（后台登录后续在 2026-10-22 改为 Cookie 会话）。
- 修复健康检查依赖审计数据库、RAG 状态文件损坏、非法上传和内部异常错误码不准确等问题。
- RAG 重建增加文档校验、临时 collection 和原子 ready 标记，避免异常输入破坏可用索引。
- LLM 客户端增加超时并关闭 SDK 隐式重试；工单解析不再把长文本误判为 Windows 文件路径。
- 统一用户端与后台端为同一个服务进程；此前多个启动/部署入口统一收敛为 Windows 唯一入口 `项目控制面板.bat`，避免用户误选入口导致端口和数据库状态分裂。
- 修复 Windows PowerShell/cmd 中文乱码，部署脚本统一使用 UTF-8，并在服务健康检查成功后再连接 Tunnel。
- 增加安全回归测试，当时验证结果为 `26 passed`、Ruff 通过、Python 编译检查通过。
- 修复并验证 `https://060115.top/`、`https://060115.top/healthz`；线上后台未配置管理员凭据时默认拒绝访问。

## 2026-09-15：后台常驻启动与中文控制面板

- 新增 `项目控制面板.bat/.ps1`，提供“一键启动项目”“停止项目”“打开网站”三个中文标注入口。
- 源站与 Cloudflare Tunnel 改为独立隐藏进程并记录实际服务 PID；兼容 Windows 虚拟环境 Python 启动器与实际监听进程 PID 不一致的情况。关闭 CMD 或控制面板不会停止服务，必须手动点击“停止项目”或执行 `stop.ps1`。
- 修复控制面板 PowerShell 中文引号语法问题，并将控制面板标准输出与错误输出拆分到不同日志文件。
- 兼容 Windows PowerShell 5 的 Windows Forms 构造方式，避免控制面板在系统 PowerShell 中启动失败。
- `stop.ps1` 支持从 `.env` 读取自定义端口；本次验证包含 PowerShell 语法检查、测试、Ruff、compileall 和 `git diff --check`。

## 2026-10-12：统一 Windows 启动与部署入口

- 删除重复的 `一键部署上线`、`一键启动`、`start` 和独立停止入口文件。
- 仅保留 `项目控制面板.bat` 作为用户可见的 Windows 启动/部署开关；控制面板内部继续提供“一键启动项目”“停止项目”“打开网站”。
- README 与中文部署说明同步改为 Windows 唯一入口说明。

## 维护约定

每次修改代码、配置、启动脚本或部署文件后，必须同步更新本文件与 README 中的相关说明（变更原因、影响范围、配置要求、启动方式、验证结果）。提交 GitHub 前至少执行：

```bash
python -m pytest tests -q
python -m ruff check src scripts tests ui
python -m compileall -q src scripts ui
git diff --check
```

不得将 `.env`、API Key、Cloudflare 凭据、SQLite 数据库、用户上传文件和本地向量索引提交到 GitHub。
