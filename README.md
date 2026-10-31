# 司忆

<!-- project-badges:start -->
[![Version](https://img.shields.io/github/v/tag/AureliusWu/Agent?sort=semver&style=flat-square&label=version)](https://github.com/AureliusWu/Agent/tags) [![Build](https://github.com/AureliusWu/Agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/AureliusWu/Agent/actions/workflows/ci.yml) [![Last commit](https://img.shields.io/github/last-commit/AureliusWu/Agent?style=flat-square)](https://github.com/AureliusWu/Agent/commits/main)
<!-- project-badges:end -->

当前版本：`16.0.1`。当前为 Windows PC 桌面端用户测试基线；最新正式 GitHub Release 仍为 `8.0.1`。

司忆是一个面向本地文件与工作区任务的桌面 Agent，重点是任务拆解、受控工具执行、持久任务、验证与可恢复文件操作。

## 核心能力

- Windows 桌面端：Tauri 2 + React + FastAPI sidecar。
- 持久任务队列、跨会话恢复、任务 steering、取消与检查点。
- 文件读写、批量改名 / 移动 / 替换、撤销、快照与冲突保护。
- 多模型 / OpenAI-compatible Provider、本地模型、MCP 与能力插件。
- ask / agent / full 权限模式，关键命令、外部调用和敏感操作继续确认。
- 独立 Verifier、验收要求、审计日志与安全边界，避免“执行成功”直接等同于“任务完成”。

## 开发与验证

Windows PowerShell：

```powershell
.\scripts\dev.ps1
.\scripts\test.ps1
.\scripts\eval.ps1
.\scripts\smoke-sidecar.ps1
.\scripts\build-desktop.ps1
```

## AI / 维护者入口

修改前必须先读 [AGENTS.md](AGENTS.md)。运行时权限、Executor、任务队列、Verifier、上下文、记忆、MCP、扩展、安全和发布约束均以该文件为准。

- `当前版本：x.y.z` 是作品集同步会读取的机器入口，请保留该格式。
- 版本事实源：`VERSION`。
- 当前交付状态：`docs/16.0.1/CURRENT_DELIVERY.json`。
- 当前发布说明：`docs/16.0.1/RELEASE_NOTES.md`。
- 历史版本、实验、审计和验收继续留在 `docs/`，不要回填进 README。
- 不使用真实凭据做自动测试，不把 Mock / 单测 / 浏览器模拟冒充真实模型、实体设备或正式发行验收。

README 只维护产品定位、当前交付边界和最小开发入口。