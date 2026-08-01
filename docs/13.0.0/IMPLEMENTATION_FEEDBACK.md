# 司忆 v13.0.0 实施反馈

## 基本信息与最终状态

- 目标版本：v13.0.0
- 实施分支：`local/v13.0.0`
- 安装包绑定源提交：`0929fff369f7a45a06c1d6925e976cdc3092077f`
- 实施状态：`COMPLETE`
- 测试状态：`BLOCKED`
- 发布状态：`BLOCKED`
- GitHub：`NOT_PUSHED`
- 阻塞原因：MSI 是 per-machine 安装，当前非管理员进程收到 Windows Installer 错误 1925（退出码 1603），因此没有把 MSI 文件存在或静态检查当作安装通过。

## 原始目标与逐项完成情况

本次目标是把司忆从通用单 Agent 执行升级为可理解复杂任务、构建依赖图和预算、由内部专业角色协作且保持唯一身份的 v13.0.0 Windows PC 桌面版本。阶段 A 至 G 已完成并取得实际运行证据；阶段 H 的 NSIS 链路完成，MSI 管理员安装被环境权限阻塞。验收矩阵为 20 项：19 `PASS`、0 `FAIL`、1 `BLOCKED`、0 `NOT_RUN`，执行覆盖率 100%。

## 用户可感知变化

- 任务入口不再把请求的编排模式强制降级为 `single/general`，复杂任务可自动进入多 Agent 编排。
- 审计面板可查看任务依赖图、专业角色、性能轨迹、Provider 策略和验收证据。
- 内置专业配置真实区分代码、数据、文档和文件整理能力，不再全部映射到通用配置。
- 本地模型能力不足时会显式降级或阻塞，不会静默切换到付费 Provider。

## 架构、复杂任务与内部协作

- 新增 `task_intelligence.py`，负责任务分类、需求与验收条件提取、复杂度判断、依赖 DAG 校验和有界预算。
- 新增 `professional_orchestration.py`，定义 Planner、Executor、Reviewer、Verifier、Recovery Coordinator 的能力边界与结构化消息。
- Reviewer 会拒绝计划外 Receipt；Verifier 保持独立完成判定；Recovery 仅允许有限重试、修复、回滚或阻塞。
- Schema 升至 v38，持久化任务需求、验收条件、依赖、角色消息、预算、性能轨迹、Provider 策略、评测与发布产物。
- 所有内部角色共享司忆的身份、权限、状态机和证据链，不创建第二人格，也不绕过 Executor。

## Provider 与本地模型

- Provider 策略要求显式授权付费回退，并记录本地能力为 `FULL`、`PARTIAL` 或 `UNSUPPORTED`。
- Ollama `qwen3:4b` 使用真实本地服务运行 15 个用例，15/15 通过，耗时 390.87 秒；未调用 DeepSeek 或 GLM 付费接口。
- Core 18/18、Adversarial 4/4、Multi-Agent 10/10、Professional 8/8 全部通过。

## 性能与耐久

- 确定性性能门禁全部通过：100 文件扫描 2.896 ms、100 文件移动 1921.514 ms、1,000 文件快照 3344.045 ms、恢复 6759.370 ms、100 次回滚 1824.230 ms、10 MiB 原子写入 67.907 ms、1,000 文件冲突扫描 298.535 ms、进程树停止 133.567 ms。
- 打包 Sidecar 三次样本为 1657/1660/1655 ms，中位数 1657 ms；v12 基线中位数 2649 ms，变化 -37.45%，且最差值未超过预算 1903 ms。
- 30 分钟耐久测试实际运行 1851.578 秒，共 29 个完整周期；每周期包含 1,000 execution segments、10,000 model loops、50,000 tool calls、100 checkpoints，29/29 通过。

## 测试、数据可靠性与安全

- 最终全量回归：607 `PASS`、18 项明确 `SKIPPED`、0 `FAIL`，覆盖率 83.42%；前端 lint、生产构建和安全检查通过。
- v37 到 v38 的迁移、预迁移备份、旧任务与 Receipt 保留以及一致性检查均通过；测试全程使用隔离数据目录，未触碰真实用户数据库。
- 隐私门禁通过；未提交 `.env`、密钥、数据库、日志、真实工作区内容或生成证据。
- 数据可信度结论：`CONDITIONALLY TRUSTED`。迁移、Receipt 和任务状态路径有回归证据；MSI 管理员安装后的真实迁移链路尚未验证，因此不能给出完整发布级 `TRUSTED`。

## 安装验证与构建产物

- NSIS：安装、启动、v12 升级、Schema 迁移、卸载、重装、数据保留与 Sidecar 清理通过。
- MSI：安装包构建和哈希完成，但实际安装因缺少管理员权限被阻塞；不得判定为通过。
- NSIS：`desktop/src-tauri/target/release/bundle/nsis/司忆_13.0.0_x64-setup.exe`，SHA-256 `1A7CFA1CF29F67730C68BC1FD880D5AFCE9714AEB679FF30F34895A78B2DF3EB`。
- MSI：`desktop/src-tauri/target/release/bundle/msi/司忆_13.0.0_x64_zh-CN.msi`，SHA-256 `C068A7E0F5D625BF7E42B6B6A2BB747225EA1221E9E509F6B40F1C018C9B7B77`。
- SBOM：`dist/release/agent-sbom.cdx.json`；第三方通知：`dist/release/THIRD_PARTY_NOTICES.txt`。

## 兼容性、Git 与已知问题

- Windows PC 桌面、Tauri 与打包 FastAPI Sidecar 是唯一交付路径；未重新引入网页端或非 PC 端代码。
- 版本源、npm、Cargo、Tauri 与锁文件均为 13.0.0；安装包内版本、Schema v38、构建标识和多 Agent 默认开关一致。
- 源代码提交为 `5c3bbc5` 与 `0929fff`；最终证据文档另行提交。未推送 GitHub。
- 唯一发布级已知问题：MSI 管理员安装门禁 `BLOCKED`。这不是兼容性警告，而是禁止完整发布的硬门禁。

## 技术债务、后续候选与用户决策

- 部分既有构建脚本仍把性能与包体报告写入历史命名目录 `build/v970-evidence`，不影响结果真实性，但后续应参数化版本目录。
- 后续可继续治理大型 Runtime 模块、性能历史趋势和更多本地模型覆盖；这些不应绕过当前 MSI 阻塞。
- 用户需在管理员 PowerShell 中执行 `scripts/smoke-msi.ps1 -Output build/v130-evidence/msi-smoke-admin.json`，完成安装、启动、升级、卸载和重装；通过后再更新 A20 与发布状态。回滚点为源提交前的 v12.0.0 提交 `cd74d7d`，安装链路回滚可使用已验证的 v12 安装包与迁移备份。

## 机器可读证据

- 验收矩阵：`docs/13.0.0/TEST_MATRIX.json`
- 发布状态：`docs/13.0.0/RELEASE_STATUS.json`
- 证据清单：`docs/13.0.0/EVIDENCE_MANIFEST.json`
- 全量回归：`build/v130-evidence/full-regression-v13-final.log`
- 耐久测试：`build/v130-evidence/durability-30m.json`
- 性能：`build/v130-evidence/performance/performance.json` 与 `build/v970-evidence/performance-gate.json`
- MSI 阻塞：`build/v130-evidence/msi-smoke.json` 与 `build/v130-evidence/msi-install.log`
