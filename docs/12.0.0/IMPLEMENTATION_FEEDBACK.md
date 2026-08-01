# 司忆 v12.0.0 实施反馈

## 结论

本地 v12.0.0 已完成实施、版本同步、真实场景验收和 Windows 本地打包，状态为 `RELEASED_WITH_WARNINGS`。核心验收矩阵 A01–A20 均有实际运行证据并标记 PASS；本次未推送 GitHub。最终清洁产物绑定代码提交 `249571a30148b5b775799738fe8e302bafd19ea7`。

## 已实施

- SQLite Schema 升至 37，新增任务状态转移、Tool Receipt、文件事务和回滚记录。
- 非法状态转换被拒绝，终态不可重开，`COMPLETED` 只能由验证器提交。
- 文件写入使用版本令牌、原子替换、批量预检与整批回滚；删除默认进入 Agent 可恢复区。
- 停止链路先持久化 `CANCEL_REQUESTED`，后端确认进程树退出后才标记取消；修复 Windows 已退出进程因句柄未释放被误判存活的问题。
- Provider 共用生成、工具、结构化输出和 usage 归一化契约，任务运行时保留恢复、验证与有限修复状态。

## 实际验证

- 综合回归：599 PASS / 8 SKIP，覆盖率 82.88%；8 项 SKIP 皆有显式外部环境条件。
- 本地模型：Ollama 0.32.5 + `qwen3:4b` 真实运行 5/5 PASS，耗时 233.40 s。
- 评测：core 18/18 PASS，adversarial 4/4 PASS；multi-agent 0/3、professional 0/4，两者为已知非核心问题，未记 PASS。
- 真实场景：文件分类冲突、Markdown 批改与哈希恢复、安全删除、代码修复、进程树停止、崩溃恢复共 6/6 PASS。
- 工程门禁：前端 lint/build/security PASS，Rust fmt/clippy/6 tests PASS，隐私扫描与负向提交钩子 PASS，13 个版本源一致。
- NSIS：安装、v9.6→v12 升级、Schema 36→37 迁移、卸载保留数据、重装、启动和 sidecar 退出全部 PASS。MSI 安装冒烟因当前进程无管理员权限被 Windows Installer 1925 阻塞，标记 BLOCKED。

## 性能与已知警告

实测：100 文件扫描 2.770 ms，移动 1892.259 ms，1000 文件快照 3181.913 ms，快照恢复 6220.223 ms，100 移动回滚 1711.620 ms，10 MiB 原子写入 72.088 ms，1000 文件冲突令牌扫描 280.256 ms，进程树停止 118.571 ms。干净打包 sidecar 三次中位启动 2661 ms，相对 v9.6 同机基线 1661 ms 回退 60.2%；功能与身份一致性均通过，但该项按计划作为已知性能警告。未测量项均保留在 `build/v120-evidence/performance.json` 的 `not_measured` 字段，没有估算值。

## 产物

- NSIS：`司忆_12.0.0_x64-setup.exe`，37,822,884 bytes，SHA-256 `73c440611817307ad406c8cc3a5ab4d06738f3ef5c7e93466954b14a5ca4892e`。
- MSI：`司忆_12.0.0_x64_zh-CN.msi`，39,002,112 bytes，SHA-256 `75fd1e66a969e262de28a820907bf0cc869689cf526f251c885ca248856aa133`。
- 便携主程序：`司忆.exe`，后端：`agent-backend.exe`；SBOM 与第三方通知已生成。
