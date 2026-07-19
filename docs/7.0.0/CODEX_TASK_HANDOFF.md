# 司忆 7.0.0 Codex 任务交接

更新日期：2026-07-19
仓库：`<repository-root>`
分支：`codex/v2.0.1`
当前版本仍为 `6.0.0`，未达到全部验收前不得升级版本号。

## 当前目标

先完成隐私与 Git 历史审计，再按《7.0.0最新修改方案_v2.md》分阶段实施。任何提交都不得包含真实记忆、对话、数据库、密钥、用户素材或运行日志；全部验收通过后才提交、推送并升级到 `7.0.0`。

## 已完成

1. 完成仓库、历史和远程制品隐私清理；远程分支与标签已重写，Actions 制品已删除。
2. 增加 `scripts/privacy_scan.py`、CI/发布隐私门禁和强化后的 `.gitignore`。
3. 生产数据迁至 `%LOCALAPPDATA%\AureliusWu\Agent`，开发数据迁至 `Agent-Dev`，迁移前备份并校验 SQLite。
4. 已提交：
   - `be28fbf security: add repository privacy guardrails`
   - `2d3bb8d docs: record 7.0.0 privacy baseline`
   - `61142a1 feat: isolate production and development runtime data`
5. P0 本地未提交实现：按 `task_id` 的文件锁与续租、文件版本令牌、ToolReceipt v2、验证失败指纹追踪、AdminActionGrant、Schema 26、系统本地时区、移除用户可见暂停、任务租约与心跳骨架。

## 当前验证状态

- 隐私扫描（工作区、跟踪文件、Git 历史）：此前已通过。
- 文件锁、沙箱、验证、任务运行等阶段测试：此前分别通过。
- 移除暂停并修复 Kernel 契约后：`44 passed`。
- 新增 `tests/siyi/test_task_leases.py` 后，测试尚未完成，因 Codex 客户端当前任务历史损坏而中断。

## 下一步顺序

1. 先运行任务租约测试并修复失败：
   `siyi\.venv\Scripts\python.exe -m pytest tests/siyi/test_task_leases.py -q --no-cov`
2. 完成 P0：Provider 临时限流/额度耗尽分类与 `WAITING_PROVIDER`、Managed Process Supervisor、停止传播和孤儿进程回收。
3. 运行 P0 定向测试、后端全量测试、前端 lint/build；检查 `git diff` 后提交 P0。
4. 实施 Context Compiler v2、自动标题、源码布局迁移。
5. 执行压力、故障注入、长时间运行、升级、正式构建及隐私门禁。
6. 全部门禁通过后统一升级 `VERSION`、Python、npm、Cargo 到 `7.0.0`，再提交并推送。

## 重要约束

- 不覆盖当前未提交改动，不使用 `git reset --hard` 或 `git checkout --`。
- 不读取或提交本地真实运行数据；测试只能使用临时目录与合成数据。
- `paused` 仅保留历史兼容；新任务只提供停止、可恢复中断和等待确认。
- Schema 26 尚在本阶段持续补齐，正式运行生产数据库前先完成迁移定义与测试。
- 当前 Codex 任务发生 `Custom tool call output is missing`，会导致“ChatGPT 已意外停止”。请在新任务继续，不要恢复本任务的长工具调用。

## 新任务开场提示

> 请阅读 `<repository-root>/docs/7.0.0/CODEX_TASK_HANDOFF.md` 和用户提供的 `7.0.0最新修改方案_v2.md`，检查当前 Git diff 后从“任务租约测试”继续。保留全部未提交改动，按文档顺序实施；不得提交私人运行数据。
