# Windows 安装、升级与恢复

## 安装

个人安装优先使用 `司忆_<version>_x64-setup.exe`（NSIS）；集中部署可使用 `.msi`。安装包内含前端和本地 FastAPI sidecar，不需要单独安装 Python 或 Node.js。

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

1. 正常退出司忆；未完成任务会保存为可恢复状态。
2. 运行新版 NSIS 或 MSI 安装包覆盖安装。
3. 启动后在“审计”中确认版本和数据库状态正常。

从 `v1.0.0` 升级时，NSIS 会先调用旧 `Agent` 卸载器，再安装 `司忆`，并清理旧 `Agent.exe`、快捷方式和卸载项。运行数据位于独立的 `%LOCALAPPDATA%\AureliusWu\Agent`，不会随旧程序卸载而删除。MSI 固定复用 v1.0.0 的 UpgradeCode，并使用简体中文安装数据库代码页，避免改名后形成第二个 MSI 产品。

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

该流程校验统一版本和锁文件，构建 sidecar、NSIS 与 MSI，并生成 `dist/release/agent-sbom.cdx.json`。v2 候选版会下载已接受的 v1.0.0 NSIS：先安装旧版，再覆盖候选版，然后使用隔离数据目录启动桌面程序，验证旧主程序清理、schema 迁移、迁移前备份、日志、正常退出、sidecar 清理、原地覆盖安装、卸载和数据保留。测试目录始终位于系统临时目录，不会访问真实 `%LOCALAPPDATA%\AureliusWu\Agent`。
