# Agent

当前版本：`0.2.1`。

面向个人使用的通用 Agent：React/TypeScript 响应式 PWA、FastAPI + SQLite 后端，以及 Tauri 2 Windows 桌面壳。

## 当前能力

- OpenAI-compatible Provider，默认兼容 DeepSeek `deepseek-chat`，支持真实连接与延迟检查
- 持久化多轮对话、最多 12 轮工具调用、上下文用量估算与自动/手动压缩
- 用户选择的工作区沙箱，拒绝路径越界
- 只读、需确认、自动执行三档权限
- 文件上传、搜索、分段读取、创建、修改、移动、删除与操作审计
- 受控命令执行：固定工作区、无 shell、超时限制，并遵守三档权限模式
- 兼容 `.agent/skills/*/SKILL.md` 与 `.codex/skills/*/SKILL.md`，相关 Skill 会进入模型上下文
- MCP 工具自动发现并进入模型工具目录；网页端支持远程 HTTP/SSE，桌面端可启用 stdio
- 每次工具调用持久化记录输入、结果、状态和耗时边界
- 桌面 API Key 存入 Windows Credential Manager；网页端使用后端环境变量
- Windows 安装包内置 FastAPI sidecar，数据写入 `%LOCALAPPDATA%\AureliusWu\Agent`

## 目录

- `frontend/`：React PWA 与 `src-tauri/` Windows 桌面壳
- `backend/`：FastAPI、SQLite、模型代理、沙箱、Skill/MCP
- `scripts/`：Windows 开发与测试脚本

## 本地开发

```powershell
cd D:\AI项目\Agent
copy backend\.env.example backend\.env
# 在 backend\.env 配置 AGENT_DEEPSEEK_API_KEY；不要提交该文件
.\scripts\dev.ps1
```

浏览器打开 `http://localhost:5173`，API 文档位于 `http://127.0.0.1:8000/docs`。

## 测试与构建

```powershell
.\scripts\test.ps1
cd frontend
npm run build
```

## Windows 桌面端

Tauri 2 使用 Rust、Cargo 与 Microsoft C++ Build Tools。当前开发机已安装并通过 `cargo check`。运行：

```powershell
cd frontend
npm run tauri dev
npm run tauri build
```

桌面模式应让本机后端使用 `AGENT_ALLOW_LOCAL_MCP=true`。仓库已配置 Tauri 与 Windows Credential Manager 凭据命令。

## 安全边界

工作区必须是已存在目录。后端会对所有目标调用 `resolve()` 并确认其仍位于工作区内。命令执行不经过 shell，当前目录必须位于工作区，输出和执行时间均有限制。只读模式拒绝写入、命令和 MCP；需确认模式不会在批准前执行。`.env`、数据库、密钥和构建产物均不得提交。

## 后续能力边界

本轮优先补齐了单 Agent 的可靠执行闭环。下一阶段再引入流式输出与中止、后台任务、细粒度永久允许/拒绝规则、多 Provider 凭据与自动故障转移、子 Agent 和 Git 工作树隔离。这些能力需要独立的任务调度与策略层，不应直接塞进当前同步聊天循环。
