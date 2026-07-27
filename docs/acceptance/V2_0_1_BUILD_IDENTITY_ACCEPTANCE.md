# 司忆 v2.0.1 构建与连续性验收

状态：**自动验收通过，等待管理员确认**

## 实现范围

- 构建阶段统一生成版本、完整/短提交、分支、时间、Development/Release、CLEAN/DIRTY、源码 SHA-256 指纹、组件构建 ID 与 Schema。
- 同一清单分别嵌入 Tauri、React 和 PyInstaller Sidecar；安装后离线读取，不调用 Git。
- `/api/health` 与 `/api/diagnostics/status` 返回 Sidecar 构建身份；前端集中比较三个组件。
- 侧栏显示 `v版本 · 短哈希 · 构建时间`；“关于司忆”显示完整信息、复制按钮及构建不一致告警。
- 规划器区分只读路径与写入目标，避免把读取源码误判为代码改动；目录创建幂等。

## 自动验收表

| 项目 | 结果 | 证据 |
|---|---|---|
| 未构建时旧制品指纹不变 | 通过 | 旧/新 `司忆.exe` SHA-256 与时间对照 |
| DIRTY 源码内容可区分 | 通过 | `test_build_fingerprint.py` |
| 新提交改变 Git 哈希 | 通过 | 隔离 Git 仓库提交测试 |
| 三组件一致 | 通过 | `test:build-info` consistent case |
| 旧 Sidecar 混用告警 | 通过 | `test:build-info` mismatch case |
| 缺失/掉线不保留旧身份 | 通过 | incomplete case 与 refresh 清空逻辑 |
| Sidecar 内嵌信息/Schema | 通过 | `smoke-sidecar.ps1`, Schema 22/22 |
| 无工作区连续对话 | 通过 | 12 轮真实模型对话 |
| 新会话/真重启/模型切换 | 通过 | 正式 Runtime、持久库、DeepSeek |
| 普通工作区文件链路 | 通过 | 读取、创建、移动、复读均完成 |
| 权限确认 | 通过 | approval token 一次批准前后对照 |
| 司忆源码工作区 | 通过 | 读取版本/Schema并创建复读证明文件 |
| 身份语义判定 | 通过 | 8 条标准逐项通过、无矛盾 |

## 已执行命令

```powershell
.\siyi\.venv\Scripts\python.exe -m pytest siyi/tests -q
cd frontend; npm run lint; npm run test:build-info; npm run build
cd desktop\frontend\src-tauri; cargo test --locked
.\scripts\build-runtime.ps1 -Force
.\scripts\smoke-sidecar.ps1
$env:SIYI_ACCEPTANCE_MODEL_KEY='<temporary-key>'
.\siyi\.venv\Scripts\python.exe .\scripts\acceptance_identity_workspace.py
Remove-Item Env:SIYI_ACCEPTANCE_MODEL_KEY
.\scripts\build-desktop.ps1
```

## 结果摘要

- Python：`273 passed, 1 skipped`，覆盖率 `82.31%`。
- Rust：`4 passed`。
- React：lint、生产构建及构建一致性测试通过。
- 真实运行：失败项 `0`，未残留根目录 Sidecar 进程。
- 完整逐轮输入、输出、环境、工作区证据和 AI 判定见 [V2_0_1_IDENTITY_WORKSPACE_TRANSCRIPT.md](V2_0_1_IDENTITY_WORKSPACE_TRANSCRIPT.md)。

## 剩余风险

- 构建为 DIRTY 时提交哈希代表 HEAD，必须结合源码内容指纹识别制品。
- DeepSeek 服务可用性、限流和模型行为属于外部依赖；验收记录只证明本次真实链路。
- 管理员尚未在正式安装程序中完成最终人工视觉与交互确认。
