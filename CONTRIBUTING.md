# Contributing

感谢参与项目贡献。较小、聚焦的 Pull Request 更容易审查和合并。

## 开发环境

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

复制 `.env.example` 为 `.env` 并设置 `LLM_API_KEY`。不要在代码、测试数据、Issue 或提交记录中写入真实密钥。

## 提交流程

1. 从 `main` 创建功能分支，并在提交信息中清楚描述变更。
2. 新功能补充测试；涉及数据契约时同步更新文档和测试样例。
3. 运行 `python -m pytest tests -q`，确认本地测试通过。
4. 运行 `python -m ruff check src scripts tests ui`，确认无新增 lint 错误。
5. 提交 Pull Request，说明背景、实现方式、验证结果和已知限制。

## 代码约定

- Agent 输出必须符合 Pydantic 数据契约，顶层使用 `{"results": [...]}`。
- 解决方案必须来自 RAG 检索结果，不得由模型凭空生成。
- 配置项放在 `config/config.yaml`，不要硬编码类别、团队或 SLA。
- 不提交 `.env`、`outputs/`、`ui/helpdesk.db` 或 `data/tickets/index/`。
