# v16.0.1 RC 合同适配 · 2026-10-31

本次解决当前产品 `VERSION=16.0.1` 与仍固定在 `16.0.0` 的 RC 采集、传输和接受工具不兼容。基于源码 `0f36b85d873ce24f348b0d324cdd988cec88f5c2`，将当前 RC 目标统一为 `16.0.1`，生成目录统一为 `build/v1601-evidence`，接受产物统一为 `Siyi-RC-Acceptance-v16.0.1`，与现有正式 Release 工作流一致。

适配覆盖总门禁、安装与升级生命周期、桌面/性能/测试证据采集、exporter/importer、接受快照与 main-only acceptance 工作流。原有 42 项要求、`V160-*` 要求 ID、同 SHA 干净源码、完整字节闭包、隐私过滤和实际运行要求均保留。历史 `docs/16.0.0`、原始证据、旧标签和本地用户数据目录/任务身份均未迁移。

回归新增了旧目标版本与旧证据路径拒绝用例：即使为旧目标填写当前源码或当前目录，也不能获得接受。仅测试夹具使用合成数据，不能作为真实 RC 证据。

## 已执行验证

- 使用锁定 Python 依赖和 `npm ci`，执行 release 工具测试及 release metadata 测试：**1017 passed, 19 skipped**。本地运行环境为 Linux，已安装 PowerShell 执行可跨平台的脚本合同；Windows 专用或真实安装场景跳过，不计为验收 PASS。
- `scripts/check-release-metadata.py`：15 项版本来源一致，目标 `16.0.1`。
- `scripts/privacy_scan.py --tracked --checks all`：通过。
- `scripts/rc_gate.py --print-contract`：当前目标 `16.0.1`，保留 21 项 automated、8 项 desktop、13 项 manual 要求。
- 远端当前提交的完整 Windows CI 是独立验证，结果以对应 Actions run 为准。

## 正式发布仍为 BLOCKED

本次未生成实际接受 bundle、未运行付费模型、未操作真实麦克风，也未创建发布标签或正式 Release。`RELEASE_STATUS.json`、`EVIDENCE_MANIFEST.json`、`TEST_MATRIX.json`、`MODEL_BENCHMARK.json` 中的历史状态保持原样。原始阻断核对见 `RELEASE_BLOCKERS_20261031.md`；其中版本不兼容已由本次代码消除，其余现场证据缺口仍成立。

下一步先冻结本次适配之后的精确源码 S，在 Windows 本机实际完成原有全套门禁和同机基线，按当前 `v1601` 目录生成同 S 的完整证据。通过 exporter 后，将本仓库不可变资产的 ID 与实际 SHA-256 传入 main-only acceptance。只有同 S 成功生成 `Siyi-RC-Acceptance-v16.0.1` 后，才可将该 run ID 交给新的 `v16.0.1` 正式发布；main 变化后须重新采集，不重标旧证据。

## 2026-11-05 Runtime 目标补齐

实际本地资格运行暴露了上述适配遗漏：Runtime 文件资格 producer/validator 的 `TARGET_VERSION` 仍为 `16.0.0`，而应用和正式 RC consumer 为 `16.0.1`。原始报告保留；不能把旧目标改写成当前目标，或让正式 consumer 接受旧目标。

当前 Runtime 目标直接绑定现有 `app.__version__`，reader 在每个能力层都严格核对 `app_version`。版本元数据检查增加第 16 个来源，同时静态核验 Runtime 的版本绑定及实际应用版本，不导入或运行模型模块。旧 `16.0.0` 报告即使填写当前 app/source 仍会被拒绝。`runtime-file-v1`、30 个样本、原有质量/资源/身份/来源守卫和 42 项 RC 要求均保留；测试中的合成证据仅验证工具链，不构成模型资格或正式 RC。

该修复后的真实资格必须在新的精确源码及新的自有模型实例上重新采集，随后按原 basic 依赖判定能否运行默认模型 core。旧运行的 basic/只读/计划层历史结果及文件层失败不能迁移成新源码的接受材料。
