# Security Policy

## Reporting a Vulnerability

请不要在公开 Issue 中披露 API Key、个人数据或可直接利用的安全漏洞。请通过 GitHub Security Advisory 的私密报告功能联系维护者，并提供复现步骤、影响范围和建议修复方式。

## Scope

后台登录使用 `HELPDESK_ADMIN_USER` / `HELPDESK_ADMIN_PASSWORD` 在 `/login` 表单校验，成功后下发由 `HELPDESK_SESSION_SECRET` 与管理员账号共同派生的 HttpOnly、SameSite=Lax `helpdesk_admin` 签名 Cookie（默认 30 天，`HELPDESK_ADMIN_SESSION_DAYS` 可调；修改密码会立即使旧 Cookie 失效），响应中不再发送 `WWW-Authenticate`，因此不会触发浏览器原生 Basic 认证弹窗。用户会话仍使用独立签名的 HttpOnly Cookie。生产环境仍应通过 Cloudflare Access、VPN 或 HTTPS 网关进一步限制后台入口，并设置稳定的随机密钥。`ui/server.py` 默认仅监听 `127.0.0.1`，不应在未配置认证和限流时直接暴露到公网。
