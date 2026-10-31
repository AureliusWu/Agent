# v16 正式发布阻断核对 · 2026-10-31

结论：**BLOCKED，尚无正式 RC 接受产物。** 本记录不修改任何历史测量、人工记录、标签或正式验收门槛。

## 已核实的版本和运行

- 当前 main：`2de15b383b30245dc1e8543bd4323e7c0f37f707`，`VERSION=16.0.1`。15 项版本来源检查通过；对应 CI `41170126480` 成功。
- 旧发布运行 `40872127032` 绑定 `v16.0.0` / `52be7c058587a79b75e85c0ff7a8fa61223fbf0d`；在读取缺失的 `rc_evidence_run_id` 时失败，没有发布安装包。
- `Accept immutable v16 RC transport` 工作流当前显示 0 次运行，未产生可提供给 Release 的成功 RC run ID。公开 Release 列表当前最新仍是 `v8.0.1`。
- `docs/16.0.1/RELEASE_STATUS.json` 明确为 `LOCAL_USER_TEST_DELIVERED / PARTIAL / BLOCKED`，模型评测为 `NOT_RUN`，`EVIDENCE_MANIFEST.json` 明确 `rc_eligible=false`。本地补丁交付收据绑定 `57d54cdf55ed9ed6f9e126593afb9fba8536ea7c`，不冒充当前 main 的正式 RC。

## 缺失与不兼容

1. 未找到同 SHA 接受 bundle、模型身份、完整 NSIS/MSI 生命周期及正式元数据快照。实际运行 `scripts/rc_gate.py` 读取固定接受路径后返回 `BLOCKED` / 缺少 `rc-bundle.json`。
2. 当前产品已是 `16.0.1`，旧 RC 合同与 exporter/importer/lifecycle/acceptance workflow 仍固定 `16.0.0`、`build/v1600-evidence` 和 artifact 后缀 `v16.0.0`；release workflow 却按当前版本计算 `build/v1601-evidence`、要求 `Siyi-RC-Acceptance-v16.0.1`。直接填旧 run ID 或只改一个工作流字符串均不能解决。
3. 真实默认模型对照评测、麦克风、完整桌面操作、安装升级卸载与同机性能基线没有当前正式 PASS。`CURRENT_DELIVERY.json` 的历史豁免仅适用于自用测试基线，明确 `formal_release_accepted=false`，不能扩大为稳定版批准。真实付费模型验证仍没有新增预算授权。

## 续接次序

先将完整 RC 生产/传输/校验合同适配到选定的当前 `16.0.1`，保留原 42 项要求、证据来源和失败，不重写历史 `16.0.0`。再冻结精确源码 S，在 Windows 本机实际采集完整离线与已授权现场验收，生成同 S 的干净候选、完整证据及不可变 transport ZIP。按现有 exporter 隐私规则审阅索引后，以本仓库 asset ID 与实际 SHA-256 运行 main-only acceptance。

取得同 S 成功 RC artifact/run ID 后，创建新的 `v16.0.1` 标签并将该 ID 传给正式 Release，复核公开包字节。已存在的 `v16.0.0` 标签保留；main/源码改变后，旧测量和候选不重标到新源码。

本次云端代码工作区没有原 Windows 本机候选、模型、麦克风和完整接受目录，不能生产所缺现场证明。本次只核对来源、现有 CI、门禁及版本检查，不创建虚假 PASS，也不触发没有材料的失败重试。
