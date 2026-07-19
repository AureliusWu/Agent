# 7.0.0 最终测试报告

状态：**尚未达到发布条件**。

## 当前通过

- 发布元数据一致：6.0.0（按约束尚未升级）。
- 后端：347 passed，1 skipped。
- 前端：lint、build、security、build-info 通过。
- Rust：4 passed。
- 核心 Eval：18/18。
- 规定规模压力：1/1。
- 故障矩阵：34/34。
- 隐私扫描：工作区、跟踪文件、暂存区通过。
- Release runtime：桌面 EXE 与 sidecar 构建通过；sidecar smoke 通过，启动耗时 1,655 ms，Schema 28 与 Kernel 1.2 一致。
- SBOM：生成 1,077 个锁定组件；SBOM 全量隐私扫描及两个 EXE 密钥扫描通过。

## 发布前仍需通过

- 至少 30 分钟、2 小时、Nightly 与 24 小时发布候选耐久测试。
- 正式安装包 bundle 构建与安装 smoke（无 bundle 的 Release runtime 已通过）。
- 6.0.0 → 候选 Schema 升级、失败回滚和重新安装数据保留演练。
- 发布包隐私扫描和 SBOM。
- Windows 休眠/唤醒及工作区 ACL 人工故障注入。

上述门禁未完成，因此不得更新版本号、推送或声明 7.0.0 完成。
