# 临时 Windows 桌面候选验收

`rc-desktop-candidate.yml` 只采集当前源码的桌面八项实际材料，不发布软件，
不接受正式 RC，也不签人工验收。本文说明入口，不能作为一次真实运行通过的证明。

## 明确选择源码

工作流只有 `workflow_dispatch`，必填 `source_sha`。选择 `main` 后，输入完整
40 位小写 SHA；它必须同时等于 dispatch 的 `github.sha` 和运行时远端 main。
每个 job 在 checkout 或准备依赖前，先核对本仓库、main、工作流文件、manual event
及 GitHub-hosted 环境。PR、fork、其他分支、其他 workflow 或 self-hosted runner 均拒绝。

```powershell
gh workflow run rc-desktop-candidate.yml --ref main -f source_sha=<当前main完整SHA>
```

权限仅 `contents: read`、`actions: read`。GitHub token 只提供给读取本仓库元数据和
固定产物的父 helper；安装器、应用、模型及其他子进程不继承该 token。
checkout 不持久化凭据，三个 job 都在 checkout 前固定 `core.autocrlf=true`，
对齐现有 Windows-only CI / offline 的 CRLF checkout，独立复核同一源码字节和指纹。
本地最终采集也使用显式相同设置的 fresh detached checkout；只有与云候选、离线材料
实际指纹逐一相等才能接受。当前编辑树的 clean 外观不替代原始字节核对。
新入口使用的四个官方 v6 Actions 均固定到独立核验的完整 commit SHA，保留 v6 注释；
未修改其他既有工作流版本。上游 artifact ID 只通过 step 环境变量作为单个参数传给
helper 的整数校验，不把上游输出插入 PowerShell 源码。

## 实际执行范围

1. `Build candidate` 在新的 Windows 2025 VM 上，用原锁定依赖和
   `build-desktop.ps1 -CandidateOnly` 生成当前 CLEAN 源码的真实便携 EXE/Sidecar。
   原 CandidateOnly 本身仍然 `--no-bundle`；helper 在输入字节核验后另用 Tauri
   `bundle` 生成 NSIS/MSI，不经过 tag、正式 Release 或安装执行入口。
   `tauri-bundle-inputs.json` 只在 ignored 候选目录里指定完整已核对的 `_internal`；
   删除旧资源映射再指定这一份输入，避免恢复的源码 `.gitkeep` 混进安装 payload。
   应用版本、标识、原 NSIS/WiX 配置、升级码和安装策略保持原合同。
   构建前核验 VM 上既有可信 PowerShell 7 的实际 major 版本；原 CandidateOnly
   的多行 Python 参数必须由该 shell 正确传递。缺失或版本不符时阻断，
   不回退到已复现参数损坏的 Windows PowerShell 5，也不改原构建检查。
2. 两个独立的新 VM 分别运行 `Installed lifecycle (nsis)` 和
   `Installed lifecycle (msi)`。它们使用原 owned lifecycle 的真实旧版安装、启动、
   升级保数、卸载、重装、再启动和最终卸载；只操作该 VM 的 test-owned fixture。
   旧包固定为本仓库官方 v8.0.1 的明确 asset ID、字节数及 SHA-256，不能替换成
   任意 URL、重打包或合成旧包。
3. 聚合 job 只有在 build 和两种真实安装 job 全成功后运行。它再次核对两种
   public projection、原 installer consumer、实际候选字节、允许的安装版标记变换、
   完整 payload 和原 `validate_matrix()` 的八项 desktop 证据。

历史 onefile Sidecar 的身份读取不是纯标准库操作。每个 lifecycle job 在开始真实
安装前，先在自己的 venv 准备原 requirements.lock 中固定的 PyInstaller 及其六个
依赖，并实际导入原 archive reader。不会在用户机器安装或升级 Python 运行时。

每个 VM 先实测 native 可见 window station、input desktop、非 service session、
管理员能力、WebView2、安装命名空间和进程状态。实际应用还必须生成原
`desktop-render-ready-v1` 三组件收据、PID/路径/父子关系及正常退出证明。
环境没有可用桌面或真实 receipt 时失败；不把 headless、`/health` 或构建完成当桌面通过。

八项为 `tauri_build`、`nsis`、`msi`、`install`、`launch`、`sidecar_health`、
`upgrade`、`uninstall`。它们属于真实自动化 desktop 层；没有把这些结果写进
13 项 manual，也没有设置人工 `operator_attested=true`。

## 固定产物来源与公开边界

跨 job 不使用按名字自动选择、只警告 ZIP 摘要不符的下载路径。父 helper 先分页读取
完整元数据，核对独立 run-attempt API、同仓库/main/精确 SHA、工作流路径、事件、
唯一生产 job、job 的成功终态和 artifact 创建时间窗，再按固定 artifact ID 下载。
实际 ZIP 的长度和 SHA-256 必须硬匹配元数据后才展开；原有文件数、大小、路径、链接、
大小写重名、字节闭包及隐私检查继续执行。跨域下载不携带 Authorization/Cookie。

公开候选名包含 run ID、attempt、SHA：

`Siyi-Desktop-Candidate-<run>-<attempt>-<sha>-payload|nsis|msi`

只有通过检查的完整候选闭包、typed 安装 public projection 和 desktop checks 被放入
`RUNNER_TEMP/candidate-public`、`nsis-public`、`msi-public`。
私有安装请求、原始日志、测试数据库、profile、owner 状态和实际命令不递归上传。
产物保留七天，旧失败不删除；任一步失败不会上传一个看起来完整的成功子集。

helper 启动后发生失败时，三个 job 只可通过 `if: failure()` 上传固定的
`RUNNER_TEMP/desktop-failure-public/failure.json`。该投影仅含审过的固定阶段、
门禁代码、允许的异常类别、真实子进程 exit/timeout/log 摘要、host 布尔/有界计数和
实际生命周期阶段，不包含原异常文字、日志、argv、路径、URL、profile 或数据库。
helper 未启动或投影自身失败时允许没有此文件；不补造摘要，不把原 job 失败改成成功。
这个小摘要用于定位阶段和门禁，不能替代私有原始证据，也不作为不完整成功材料。

原生命周期会在内部捕获 `SafetyError`。观察型子类只调用 `super().transition`，
在捕获后原样抛出同一异常，保留原 BLOCKED、不确定态、所有 guard 和清理逻辑。
只有异常确为该类、且消息精确匹配当前受审 `rc_installed_lifecycle.py` 中
`require(..., literal_string)` 时，公开 `lifecycle_guard` 才记录固定阶段和该常量的
`SAFETY_<SHA-256>` 代码；未知或动态值一律 `UNKNOWN`，不输出原文。
原安装 dispatcher 也只经 `super()` 观察；独立的 `last_mutation` 保留最后一次真实
安装/卸载调用的 exit、timeout 和 Job 清理布尔/计数，不被后续只读 probe 的 exit=0
覆盖。诊断自身异常不改写执行结果，原始记录只留私有目录。这个改进不追改旧失败，
也不能把旧版安装阶段的失败描述成已完成安装验收。

聚合产物 `Siyi-Desktop-Subset-<run>-<attempt>-<sha>` 明确为
`DESKTOP_8_OBSERVED_NOT_RC_READY`、`formal_rc_accepted=false`、`published=false`。
原总矩阵仍报告缺失的 automated/manual 要求；只有精确已知的其他类别缺项允许在这个
desktop 子集说明中出现，任何 desktop 或未知校验错误仍失败。

这个自定义候选传输索引不能充当原 accepted-RC 索引。13 项真实人工签署、默认模型
真实 Eval/资格、可比基线与性能、完整总 RC、隐私与正式发布流程均保持原门槛。
工作流不会录制麦克风、下载或调用模型、使用收费 API、创建 tag/Release 或部署。
