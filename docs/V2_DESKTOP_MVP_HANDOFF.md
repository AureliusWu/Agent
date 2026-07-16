# 司忆 v2.0.0 Desktop MVP 收口记录

更新时间：2026-07-16（Asia/Shanghai）

## 结论

交接清单已全部执行，`v2.0.0` 达到 Windows 桌面端 MVP 发布标准。当前交付入口是仓库根目录的 `司忆.exe`，运行时必须保留同目录 `agent-backend.exe`。网页端代码继续保留，但不属于本版本验收范围。

## 完成项

- 修复首次启动永久白屏：桌面端不再在 WebView 初始化期间清空浏览数据；仅异步移除旧 PWA Service Worker 与 CacheStorage，并保留本地状态。
- 实测状态栏、首次工作区选择、三档权限、单实例、正常退出与 sidecar 自动释放。
- 使用 Windows 凭据管理器中的已保存密钥完成 DeepSeek 连接检查，`deepseek-v4-flash` 响应约 `1130 ms`，密钥未写入日志或仓库。
- 修复旧数据库部分唯一索引与 `ON CONFLICT(execution_id)` 的兼容问题，并增加旧库幂等写入回归测试。
- 生成 NSIS、简体中文 MSI 与 CycloneDX SBOM；完成 `v1.0.0 -> v2.0.0` 覆盖安装、schema 14 到 15 迁移、迁移前备份、启动退出、sidecar 清理、卸载后数据保留。
- 增加旧版 NSIS 卸载钩子；升级后不再残留 `Agent.exe`、旧快捷方式或旧卸载项。MSI 继续使用 v1.0.0 的 UpgradeCode。

## 质量证据

- 完整测试：`238 passed, 1 skipped`，覆盖率 `83.41%`。
- 前端：lint、TypeScript、生产构建、桌面构建和凭据扫描通过。
- Rust：`4 passed, 0 failed`。
- Agent Eval：核心 `18/18`、Multi-Agent `3/3`、专业 Agent `4/4`、对抗任务 `4/4`；虚假完成、权限违规、沙箱违规、无关文件修改均为 `0`。
- 发布门禁：相对 v1.0 核心基线通过，无回归项。
- sidecar 三次启动：`2698 / 1545 / 2519 ms`，中位数 `2519 ms`，最慢 `2698 ms`。
- 本地 API p95：健康 `17.76 ms`、对话列表 `19.61 ms`、任务创建 `407.47 ms`。
- 构建体积：JS `391.40 kB`（gzip `121.39 kB`）、CSS `39.40 kB`（gzip `7.70 kB`）、角色 PNG `2,048.70 kB`。PNG 已是优化过的 `1200 x 1600` RGBA 资源，继续无损重压没有收益。

## 保留边界

- `%LOCALAPPDATA%\AureliusWu\Agent` 继续作为内部数据目录，以兼容旧数据库和日志。
- “完全访问权限”仍只允许用户主动选择的工作区，不代表整台电脑或系统权限。
- 网页端、移动端、云同步、多人协作、最终 UI V2、角色动画和主题系统继续暂缓。

## 发布与回滚

发布提交使用 `release: ship 司忆 desktop MVP v2.0.0`，标签使用 `v2.0.0`。标签工作流必须产出 NSIS、MSI 和 SBOM。若发布后需要回滚，使用 `git revert <v2提交>`，不要改写 `main` 历史。
