# 06015.top 部署包

本目录是 IT 运维工单智能体的线上部署模板，不包含 API Key、数据库、用户上传文件或本地向量索引。

## Linux + systemd + Nginx（推荐）

```bash
sudo mkdir -p /opt/it-helpdesk
sudo chown "$USER":"$USER" /opt/it-helpdesk
git clone https://github.com/moliu64/IT-Helpdesk-.git /opt/it-helpdesk
cd /opt/it-helpdesk
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp deploy/06015.top/.env.example .env
vi .env
```

复制 `it-helpdesk.service.example` 到 `/etc/systemd/system/it-helpdesk.service`，将其中的用户和路径改为服务器实际值，然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now it-helpdesk
curl http://127.0.0.1:8787/healthz
```

将 `nginx.conf.example` 配置到 Nginx，补充 HTTPS 证书和后台认证文件，再 reload Nginx。

## Docker Compose

```bash
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY
docker compose -f docker-compose.yml up -d --build
curl http://127.0.0.1:8787/healthz
```

线上入口：

- 用户入口：`https://06015.top/`
- 中文标注的后台管理入口：`https://06015.top/backend`
- 兼容后台入口：`https://06015.top/admin`
- 健康检查：`https://06015.top/healthz`

检查时 `06015.top` 尚未解析到可访问服务器，因此这里只提交部署模板，不能代替真实服务器发布。DNS 和服务器权限就绪后按本文件执行即可上线。
