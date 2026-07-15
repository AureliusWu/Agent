# Windows 安装、升级与恢复

## 安装

个人安装优先使用 `Agent_<version>_x64-setup.exe`（NSIS）；集中部署可使用 `.msi`。安装包内含前端和本地 FastAPI sidecar，不需要单独安装 Python 或 Node.js。

首次启动后，运行数据固定写入：

```text
%LOCALAPPDATA%\AureliusWu\Agent
├── agent.db
├── backups\
├── diagnostics\
├── extensions\
└── logs\agent.log
```

模型密钥仍保存在 Windows 凭据管理器，不写入上述目录、安装包或诊断包。

## 升级

1. 正常退出 Agent，确认任务不再执行。
2. 运行新版 NSIS 或 MSI 安装包覆盖安装。
3. 启动后在“审计”中确认版本和数据库状态正常。

数据库 schema 需要升级时，应用会先在 `backups` 创建 `pre-migration-v<old>-to-v<new>-*.db`。迁移失败会自动恢复原库并终止启动，不会带着半迁移数据继续运行。

## 恢复

应用内手动备份使用 `agent-*.db`，升级前备份使用 `pre-migration-*.db`。恢复前务必退出 Agent。

1. 将当前 `agent.db` 复制到安全位置。
2. 从 `backups` 选择目标文件并复制为 `agent.db`。
3. 重新启动 Agent；应用会检查完整性并补齐必要迁移。

也可通过本地受令牌保护的 `POST /api/database/restore/{name}` 恢复。恢复操作会先额外保存当前数据库，以便反向撤销。

## 诊断

`POST /api/diagnostics/export` 会生成去敏 ZIP，包含版本、运行边界、数据库健康、最近审计摘要和截断日志。它不包含数据库正文、工作区文件、`.env` 或凭据。提交故障材料前仍应人工快速检查压缩包内容。

## 发布验证

维护者运行：

```powershell
.\scripts\build-desktop.ps1
```

该流程校验统一版本和锁文件，构建 sidecar、NSIS 与 MSI，执行 sidecar/安装/卸载冒烟，并生成 `dist/release/agent-sbom.cdx.json`。
