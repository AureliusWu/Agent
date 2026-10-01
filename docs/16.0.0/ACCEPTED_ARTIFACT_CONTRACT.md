# v16 冻结产物与正式 workflow 输入合同

本文件是实现合同，不是正式验收或已上传记录。当前工作区 DIRTY，缺真实模型/设备/安装证据，不能生成一个已接受发行包。

## 不同阶段

- `build-desktop.ps1 -CandidateOnly` 生成独立本地开发候选，不覆盖根目录便携程序，不安装/UAC、不下载模型或调用麦克风。
- `rc_gate.py` 验证 CLEAN 同源候选、真实逐项证据、实际组件、EXE **及完整 `_internal`**、模型资格、配对性能和安装。通过只表示 `RC_READY_NOT_RELEASED`。
- `release.yml` 的 v16 路径使用已验收不可变候选，不再构建新包。相同 commit/build_id 不代表相同二进制字节；嵌入的构建时间也可能使重新构建产生不同 hash。
- 旧版 workflow 构建路径保留用于历史兼容，不允许其代替 v16 总门禁。

## 输入和布局

通过 `workflow_dispatch` 的 `rc_evidence_run_id` 或显式仓库变量 `SIYI_RC_EVIDENCE_RUN_ID` 选择接受证据 run。必须是本仓库、当前 GitHub release commit、已完成且成功的 run；artifact 固定名 `Siyi-RC-Acceptance-v16.0.0`。没有输入或过期输入则阻断，不默认寻找最近一个。

artifact 解压到忽略目录 `build/v1600-evidence/rc-input`，**不直接解压覆盖仓库**。其根包含 `artifact-index.json` 及保留仓库相对布局的文件。索引为：

```json
{"schema_version":1,"target_version":"16.0.0","source_commit":"实际40位commit","accepted_candidate":true,"files":[{"path":"仓库相对路径","bytes":123,"sha256":"实际小写64位sha256"}]}
```

布尔值不是授权或真实性证明；`import-accepted-rc.py` 只核对受限物料，随后总 RC 和旧安装校验器独立复验每份真实证据，最后仍需用户授权的正式 tag/发布。

允许材料化的目录/文件仅为：

- `desktop/src-tauri/target/release/`：已验收的 EXE、`_internal`、NSIS/MSI，不能自行重编。
- `build/v1600-evidence/accepted/`：所有封套及附件，必须包含 `rc-bundle.json`、`default-model-identity.json`。
- `build/v1600-evidence/nsis-installer-smoke.json` 与 `msi-installer-smoke.json`：完整实际安装生命周期记录。
- `build/generated/build-info.json`、`dist/release/agent-sbom.cdx.json` 与 `THIRD_PARTY_NOTICES.txt`。
- `build/upgrade-baseline/` 中实际上一正式 NSIS/MSI 文件，以便验证上一版本及原始 hash。

索引最多20,000文件、总量8 GiB、索引8 MiB。拒绝绝对路径、`..`、Windows drive/ADS、非规范路径、大小不符、重复大小写路径、特殊文件、任何 reparse/符号链接及 hash 不符。已有文件只有完全相同字节可复用，绝不覆盖不同产物或写源码、用户配置/数据库。导入失败保留本次中间产物，不自动清理。

证据在**采集时**选择 `accepted/` 子路径；内部引用及执行 argv 仍保持原始正确路径。不要移动报告后随意改 argv 冒充实际命令，不要把同一 report 改 run_id 当性能基线。接收包必须连同每个 hash 引用的原始附件一起保留，不能只上传 summary。

## 安装器绑定

NSIS/MSI collectors 在实际安装目录读取 desktop/sidecar EXE SHA256、完整 `_internal` inventory 与位置无关内容摘要。完成全部安装、启动、升级、卸载、重装后再记录 `source_after`。sidecar 和 `_internal` 必须与接受候选逐字节相同。

桌面 EXE 唯一允许的派生是当前 tauri-utils 2.9.3 `platform.rs` 的 SDK 类型标记：接受的便携 EXE 必须只有一个 `__TAURI_BUNDLE_TYPE_VAR_UNK`，NSIS/MSI 分别精确变为同长 `__TAURI_BUNDLE_TYPE_VAR_NSS` / `__TAURI_BUNDLE_TYPE_VAR_MSI`。门禁从已核验的完整便携 EXE 字节计算派生 SHA256，不忽略区域、不相信安装器自报 hash，也不修改任何 EXE。实际 MSI 只读解包已验证此变化恰为 3 字节；这不是安装生命周期验收。

desktop/manual 封套默认 `artifact_variant: "portable"`；实际验收安装版时必须明确 `"nsis"` 或 `"msi"` 并绑定对应完整派生 EXE hash。其他字节变化、签名重写、新构建、依赖替换、重复/缺失标记或旧缺字段记录均不能通过；未来签名流程必须增加独立候选字节验收，不能扩大此例外。

## 当前未验证

没有远端运行、artifact 上传、正式 clean RC 或 installed 验收。这份合同不会把旧 v15 便携、scripted 模型或开发候选转换为发行证据。
