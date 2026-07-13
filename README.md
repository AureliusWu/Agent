# Agent

当前版本：`0.4.0`。

面向个人使用的通用 Agent：React/TypeScript 响应式 PWA、FastAPI + SQLite 后端，以及 Tauri 2 Windows 桌面壳。

## 当前能力

- OpenAI-compatible Provider，默认兼容 DeepSeek `deepseek-chat`，支持真实连接与延迟检查
- 持久化多轮对话、最多 12 轮模型循环、上下文用量估算与自动/手动压缩
- 用户选择的工作区沙箱，拒绝路径越界
- Codex 式三档权限：请求批准、替我审批、完全访问权限
- 工具注册表、严格参数 Schema、low/medium/high/critical 风险分级
- 文件上传、搜索、分段读取、编码/元数据、diff、创建、复制、修改、移动、删除
- 原子写入、修改备份和最近一次文件操作撤销；禁止默认递归删除目录
- 受控命令执行：固定工作区、无 shell、超时限制，并遵守三档权限模式
- 兼容 `.agent/skills/*/SKILL.md` 与 `.codex/skills/*/SKILL.md`，相关 Skill 会进入模型上下文
- MCP 工具自动发现并进入模型工具目录；网页端支持远程 HTTP/SSE，桌面端可启用 stdio
- 显式“停止”按钮；同时中断浏览器请求和后端模型/MCP 协程，无需等待当前模型调用返回
- 任务状态、5 分钟总超时、重复调用/连续失败/循环上限终止
- 每次工具调用记录任务、来源、风险、确认状态、输入输出、结果和耗时
- 对话重命名/删除，MCP 与 Skill 启用/停用，MCP 连接测试接口
- Windows 单实例运行，重复启动时聚焦已有窗口
- 桌面 API Key 存入 Windows Credential Manager；网页端使用后端环境变量
- Windows 安装包内置 FastAPI sidecar，数据写入 `%LOCALAPPDATA%\AureliusWu\Agent`

## 目录

- `frontend/src/components/`：聊天、侧栏、文件、扩展和审计界面
- `frontend/src/hooks/`：聊天任务与取消状态；`frontend/src/styles/`：组件级样式
- `frontend/src-tauri/`：Windows 桌面壳
- `backend/app/routes/`：按领域拆分的 FastAPI 路由
- `backend/app/task_runner.py`：Agent 循环、任务状态与即时取消
- `backend/app/`：SQLite、模型代理、沙箱、Skill/MCP 和工具注册表
- `scripts/`：Windows 开发与测试脚本

## 本地开发

```powershell
cd D:\AI项目\Agent
.\scripts\dev.ps1
```

首次运行会自动从 `backend/.env.example` 创建 `backend/.env`。网页模式在该文件配置 `AGENT_DEEPSEEK_API_KEY`；桌面模式可在扩展页保存到 Windows 凭据管理器。

浏览器打开 `http://localhost:5173`，API 文档位于 `http://127.0.0.1:8000/docs`。

## 测试与构建

```powershell
.\scripts\test.ps1
.\scripts\clean.ps1   # 清理可重新生成的构建产物
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

工作区必须是已存在目录。后端会规范化目标路径并验证解析后的真实路径仍位于工作区内。命令不经过 shell，当前目录必须位于工作区，输出和执行时间均有限制；交互式 shell、提权、格式化和系统控制命令被禁止。`完全访问权限` 只代表当前工作区文件权限，不代表整台电脑或系统权限。`.env`、数据库、密钥和构建产物均不得提交。

## 后续能力边界

`v0.4.0` 完成即时中断与前后端模块化。尚未宣称完成的高级能力包括：逐 Token 流式传输、后台/并行任务、多 Provider 自动故障转移、子 Agent、远程工作区和扩展市场。
