# Security Policy

## Reporting a Vulnerability

请不要在公开 Issue 中披露 API Key、个人数据或可直接利用的安全漏洞。请通过 GitHub Security Advisory 的私密报告功能联系维护者，并提供复现步骤、影响范围和建议修复方式。

## Scope

当前版本是本地演示项目，不提供生产级身份认证、授权、审计或网络边界保护。`ui/server.py` 默认仅监听 `127.0.0.1`，不应直接暴露到公网。
