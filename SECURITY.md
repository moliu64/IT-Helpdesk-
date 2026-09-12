# Security Policy

## Reporting a Vulnerability

请不要在公开 Issue 中披露 API Key、个人数据或可直接利用的安全漏洞。请通过 GitHub Security Advisory 的私密报告功能联系维护者，并提供复现步骤、影响范围和建议修复方式。

## Scope

后台管理页面和管理 API 使用 `HELPDESK_ADMIN_USER` / `HELPDESK_ADMIN_PASSWORD` 做 Basic Auth；用户会话使用 `HELPDESK_SESSION_SECRET` 签名的 HttpOnly Cookie。生产环境仍应通过 Cloudflare Access、VPN 或 HTTPS 网关进一步限制后台入口，并设置稳定的随机密钥。`ui/server.py` 默认仅监听 `127.0.0.1`，不应在未配置认证和限流时直接暴露到公网。
