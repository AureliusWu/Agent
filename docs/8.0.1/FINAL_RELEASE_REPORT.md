# 司忆 v8.0.1 发布报告

日期：2026-07-26
发布结论：`RELEASED WITH WARNINGS`（以 GitHub Release 工作流成功为最终生效条件）

## 发布范围

本次发布包含从 v7.0.0 以来累计完成的 v8.0.0/v8.0.1 工作，包括目录架构收敛、夏目心头像恢复、Runtime 能力增强、上下文与 Token 成本治理、Schema v29、真实 EvoPolicyGym 接入及 Windows 发布链路完善。

管理员已通过指令 `v8.0.1 发布` 授权带警告发布。该授权不改变测试事实，未执行、失败或受阻用例仍保持原状态。

## 本地质量门禁

- 版本源一致性：PASS，7 个版本源均为 `8.0.1`。
- 后端：PASS，387 passed、1 skipped，覆盖率 82.08%。
- 前端：PASS，oxlint、TypeScript、Vite production build 与源码安全扫描通过。
- Rust：PASS，5 passed。
- 隐私：PASS，tracked、history、全工作区和合成密钥负例均通过。
- 凭据：PASS，仅使用合成凭据，Windows Credential Manager 往返验证通过并已清理。
- 依赖：PASS，Python/npm/Rust 未发现适用于 Windows 目标的已知可达漏洞；Rust 非 Windows 依赖存在 informational 警告，不构成当前 Windows 发布阻断。
- 性能：PASS，独占冷启动样本 1656/1652/1628 ms，中位数 1652 ms；门限 1903 ms，基线 1655 ms。
- 数据可靠性：`CONDITIONALLY TRUSTED`。隔离 AppData、数据库迁移备份、升级/卸载/重装数据保留均实际通过；MSI 管理员权限生命周期未在本机完整执行。

## Windows 安装包

| 安装包 | 大小 | SHA-256 |
|---|---:|---|
| `司忆_8.0.1_x64-setup.exe` | 32,663,183 bytes | `3301AAE0333A573915F7CE4F6FFBA86550C730EA7A2644047070997614CE464F` |
| `司忆_8.0.1_x64_zh-CN.msi` | 33,865,728 bytes | `5111655D95798AA4FC4492DBD6968A6C15599D3C1E2690703285BD85BC1779D9` |

NSIS 实际生命周期验证结果：

- v8.0.0 → v8.0.1 原位升级成功；
- Schema v28 → v29 迁移成功，并生成迁移前备份；
- 升级保留用户数据；
- 卸载保留用户数据；
- 重装成功识别原数据；
- 桌面进程和 Sidecar 均正常启动/停止；
- 测试使用隔离数据库与隔离日志，不接触真实用户数据。

## 220 项矩阵原始状态

该矩阵作为 v8.0.0 能力与回归基线保留，不因本次发布授权改写：

| 状态 | 数量 |
|---|---:|
| PASS | 141 |
| FAIL | 2 |
| BLOCKED | 21 |
| NOT_RUN | 56 |
| 合计 | 220 |

其中 P0 为 120 PASS、14 BLOCKED、41 NOT_RUN；P1 为 21 PASS、2 FAIL、7 BLOCKED、15 NOT_RUN。

## 已知警告

- 24 小时耐久测试按管理员要求未执行。
- 18 个真实场景未全部执行。
- 220 项矩阵并非全 PASS。
- EVO-006、EVO-008 的反馈后第二候选闭环仍为 FAIL。
- MSI per-machine 管理员权限生命周期未在本机完成；NSIS 路径已完整验证。
- 真实 Provider 的 50 轮 Token 降本目标尚无完整实测证据。

## 回滚

若 GitHub Release 或安装后验证失败：

1. 停止分发 v8.0.1；
2. 保留用户 AppData，不删除用户数据库；
3. 使用已发布的 v7.0.0 安装包回滚，或使用本地已验证的 v8.0.0 NSIS 安装包进行应急恢复；
4. 根据迁移前备份恢复数据库；
5. 修复后重新执行隐私、依赖、性能、安装生命周期与云端资产验证。
