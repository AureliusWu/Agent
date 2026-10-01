# v16 独立便携候选构建入口

2026-10-01，当前 `VERSION=16.0.0`。首个独立候选实际运行了 Python 依赖核验、PyInstaller 与冻结依赖检查；Tauri 的 beforeBuild 因 Windows 嵌套命令将输出路径引号作为文件名而失败，未生成桌面候选。失败中间产物保留于 `build/candidates/v16.0.0-dev-20261001-rc1`，日志为 `build/v1600-evidence/candidate-build-v16-rc1.log`。

```powershell
.\scripts\build-desktop.ps1 -CandidateOnly
# 或为本次构建指定一个尚不存在的目录：
.\scripts\build-desktop.ps1 -CandidateOnly -CandidateDirectory 'build\candidates\v16.0.0-rc-local-20261001'
```

`CandidateOnly` 沿用现有 sidecar 冻结、冻结依赖盘点、锁定 build-info 与 Tauri 编译步骤。候选目录必须是 `build/candidates` 下的新目录，拒绝已存在结果、越界路径和 reparse 祖先。默认名称包含版本、时间及随机后缀；失败保留本次中间结果。

本模式不执行 pip/npm 安装、安装器打包及安装验收、应用启动或任何模型/麦克风验收。它读取 `requirements.lock` 对照所有适用已装 Python 包版本，执行 `pip check`，检查已存在的 PyInstaller/Tauri 工具。Cargo 使用 `--locked --offline`，不会自动获取缺失 crate；依赖缺失时明确失败。

输出包括 `sidecar-dist`、PyInstaller work/spec、`frontend-dist`、`build-info.json`、`tauri-candidate.json`、`evidence/frozen-artifacts.json`、`portable/司忆.exe`、`portable/agent-backend.exe`、`portable/_internal` 和 `candidate-build.json`，均在本次独立候选目录。源码内 Tauri binaries 仍作为已有构建输入暂存；原 tracked `_internal/.gitkeep` 在成功返回或失败的 finally 中恢复。根目录便携三件套不作为本模式的写入目标。

为控制资源，编译继续复用固定 `desktop/src-tauri/target` 的生成缓存并使用单个 Cargo job。锁定新 manifest 后，仅 `cargo clean --package app --release` 刷新本项目 crate 的缓存，保留依赖缓存，使 build.rs 重新读取本次 manifest，避免复用旧 build_id。所有候选环境变量在外层 finally 恢复。

候选前端输出通过 `SIYI_CANDIDATE_FRONTEND_DIST` 传递，beforeBuild 保持普通 npm 命令，避免 Windows 的多层参数引号。Vite 仅接受 `build/candidates/<candidate>/frontend-dist` 的绝对路径并拒绝链接祖先；正常前端构建输出不改变。后续实际构建和运行结果以 `IMPLEMENTATION_FEEDBACK.md`、`TEST_MATRIX.json` 及原始证据为准。

Tauri 的 `frontendDist` 独立使用相对于 `desktop/src-tauri` 的正斜杠目录路径。当前 SDK 的 `FrontendDist` 为 URL-first untagged enum，带盘符的 Windows 绝对路径会被解为 URL，导致前端不嵌入、候选桌面未正常加载。Rust 回归测试通过实际 SDK 类型核验此差异；rc2/rc4/rc5 的启动失败证据保留，不能当作可用桌面候选。

`candidate-build.json` 仅表示候选已构建并复制。它记录产物哈希与 build/source identity，明确 `NOT_RELEASED`、runtime smoke `NOT_RUN`、installed acceptance `NOT_RUN`；不得用于代替真实运行、安装升级或人工验收门禁。

## 离线验证

- PowerShell Parser：PASS。
- `pytest tests/backend/release/test_candidate_build_contract_v160.py --no-cov -q -p no:cacheprovider`：18 passed，13.62 秒；原始结果 `build/v1600-evidence/candidate-build-contract-final.log`。
- 真实 Windows junction 祖先拒绝、已有结果保留、运行目标路径计算、候选与正式发布选项互斥、无安装依赖分支、Cargo 锁定离线参数/自身 crate 刷新顺序、候选提前返回、成功/失败时 `.gitkeep` 清理、早期失败环境恢复均有离线合同检查。测试只执行抽取的 production 函数/AST 块，不运行整个构建脚本。
- 实际只读执行现有 Python 精确锁定依赖检查及 `pip check`：PASS；原始结果 `build/v1600-evidence/candidate-existing-dependencies.log`。无安装升级。

构建入口首轮离线验证时的 SHA-256（路径引号修正前，不能作为最终候选源码绑定）：

| 文件 | SHA-256 |
| --- | --- |
| scripts/build-desktop.ps1 | `E74ABABC7971BB4F295518A5BE8AF69170FA2B517568AE7116DBBE64ABB43337` |
| tests/backend/release/test_candidate_build_contract_v160.py | `7031E6B3690AAE68FE505545946686CF1C289869BD13096A617E53D39C4BD430` |
| build/v1600-evidence/candidate-build-contract-final.log | `D9DCF33A7E5A37F55BA3894B78B21E8BEC2A78BA201A4407173DB16D3B4AE7B6` |
| build/v1600-evidence/candidate-existing-dependencies.log | `0E8EB025D788B9DF7FF604032F11B6C97ED9BB6482C2E14FE9AAFC8D57C3BC03` |

根目录旧便携文件本阶段仍与 B00 基线一致：`司忆.exe` 为 `5B53922F50149F2094CE6885DA0BBCF3869658A5926D1932410C80B563EE4308`，`agent-backend.exe` 为 `6689BD51BC0FA102B1138B0A0BBACC8B7C17D8A2BECC7F8DB34E8BF9882E15A1`，`_internal/build-info.json` 为 `6E3B4B4B0F8896811C0756B316FD7B6ABA135434C65681631CDF7E6983B5614D`。

首轮失败不代表候选运行或安装验收通过；后续在新的独立目录重新构建，不覆盖失败目录，也不替换根目录 v15 便携程序。

## 真实候选验证及安装类型标记

rc7 已通过实际 React/Tauri/Sidecar render-ready 采集和正常退出，一次预热及五次重复启动均成功。每次使用独立数据目录、WebView2 profile、nonce 与原生 main HWND；没有使用用户数据库。实际 NSIS/MSI 也已生成，但未安装，旧根目录便携程序未变。

只读解包 MSI 确认 desktop 相对便携版只变化 SDK 类型标记的三个字节，sidecar 完全相同；bundler 完成后恢复编译缓存 EXE。因此安装验收必须使用 ACCEPTED_ARTIFACT_CONTRACT 的精确类型派生 SHA256，而不能把安装版与未知类型便携 EXE 的 hash 直接等同。此修正及测试改变了源码指纹，rc7 仍仅是其已记录源码的证据；下一候选需要重新构建/启动，不能改写 rc7 收据或伪称当前 CLEAN。
