# 司忆 7.0.0 发布交接（2026-07-22）

## 一句话状态

本地实现与本地发布门禁已经完成；GitHub Release 云端发布尚未确认完成，第二次流水线在停止轮询前仍为 `in_progress`。

## Git 状态

- 仓库：当前 Git 工作树根目录
- 分支：`codex/v2.0.1`
- 最新提交：`03084c2 ci: isolate installer schema probe`
- 版本提交：`ed403cd release: ship Siyi 7.0.0`
- 分支已推送到 `origin/codex/v2.0.1`。
- `v7.0.0` 标签已更新并推送到最新提交 `03084c2`。
- 本交接文件是停止后新增的本地文件，尚未提交或推送。

## 本地已完成

- 7 个版本源统一为 `7.0.0`，元数据检查通过。
- 后端测试：349 passed，1 skipped。
- 规定规模压力测试：1 passed（约 60 秒）；按用户要求不再执行 30 分钟至 24 小时长耐久门禁。
- 故障矩阵：34 passed。
- 数据可靠性定向测试：4 passed，覆盖迁移前备份、失败恢复、旧布局迁移与 canonical 备份目录。
- 前端：lint、build、security、build-info 全部通过。
- 前端体积：主 JS 416.45 kB，CSS 45.97 kB，与基线一致。
- Rust：4 passed。
- 核心 Eval：18/18；stable release gate 通过。
- sidecar 三次性能样本：1649、1638、1647 ms，中位数 1647 ms；最终 v7 bundle smoke 为 1745 ms。
- NSIS 与 MSI 的 v7.0.0 正式 bundle 构建通过。
- 安装后的桌面程序、隔离数据库、日志与 sidecar 启停通过。
- GitHub Release v0.22.0 安装包到 v7.0.0 的覆盖升级通过。
- Schema 27 → 28、迁移前备份、哨兵数据保留、原地升级保数、卸载保数均通过。
- Git 跟踪内容、完整历史与暂存区隐私扫描通过。
- SBOM 已生成，包含 1,077 个锁定组件（生成文件保持忽略）。

## 本轮关键修复

1. `scripts/Import-MsvcEnvironment.ps1`
   - Visual Studio 输出 `Path=` 时原脚本大小写敏感，只识别 `PATH=`；已改为忽略大小写。
2. `scripts/smoke-installer.ps1`
   - 升级夹具由旧根路径 `agent.db` 改为真实 v7 路径 `data/agent.db`。
   - 安装冷启动等待由 25 秒调整为 45 秒。
   - Schema 探测显式使用隔离的 `desktop_local`，避免 GitHub runner 注入空 `AGENT_DEPLOYMENT_MODE` 导致 Pydantic 校验失败，并在探测后恢复原环境。
3. `scripts/upgrade-database-fixture.py`
   - 按 v7 数据布局从根 `backups` 目录查找迁移备份。
4. `siyi/app/database.py`
   - 修复 Windows 上 `os.kill(pid, 0)` 会终止被探测进程的问题，改用只读 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`。
5. `.github/workflows/release.yml`
   - 升级基线从不存在的 v1.0.0 Release 改为已验证的 v0.22.0 Release。

## 云端发布状态

第一次 run：

- Run ID：`29900391269`
- 提交：`ed403cd`
- 结论：`failure`
- 已通过：源码验证、旧版基线下载、v7 bundle 构建、云端 sidecar smoke。
- 失败原因：runner 中 `AGENT_DEPLOYMENT_MODE` 是空字符串，安装 smoke 在导入配置读取 Schema 常量时失败。
- 上述原因已由提交 `03084c2` 修复，并在本地用空环境变量场景完整复验通过。

第二次 run：

- Run ID：`29901830179`
- 提交：`03084c2`
- URL：`https://github.com/AureliusWu/Agent/actions/runs/29901830179`
- 停止轮询前状态：`in_progress`
- 已确认通过：环境准备、隐私/元数据预检、锁定依赖安装、全量源码验证、v0.22.0 升级基线下载。
- 停止时正在执行：`Build and smoke Windows packages`（fresh Windows runner 的 Rust/Tauri 首次编译通常约 10 分钟）。
- GitHub API 在本轮多次出现 TLS handshake timeout / unexpected EOF；这是状态查询链路抖动，不等于流水线失败。
- 停止时 GitHub Release 资产尚未出现，因此不能宣称云端发布完成。

## 下次继续

先检查第二次 run：

```powershell
cd <repository-root>
gh run view 29901830179 --json status,conclusion,jobs,url
```

若 run 成功，再核对 Release 和三个资产：

```powershell
gh release view v7.0.0 --json tagName,name,isDraft,isPrerelease,assets,url,publishedAt
```

预期资产：

- `Siyi_7.0.0_x64-setup.exe`
- `Siyi_7.0.0_x64_zh-CN.msi`
- `agent-sbom.cdx.json`

最后检查本地状态。本交接文件尚未提交；如需保留到仓库，先复核后单独提交并推送：

```powershell
git status --short
git diff --check
```

如果第二次 run 失败，使用以下命令拉取失败日志，只修复明确失败点后再移动标签并重跑：

```powershell
gh run view 29901830179 --log-failed
```
