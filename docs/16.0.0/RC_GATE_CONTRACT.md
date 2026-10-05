# v16 RC 证据合同

本文件说明实现后的消费合同，不是验收记录。没有创建或补填任何人工 PASS。

## 三种入口，各自负责什么

- `scripts/test.ps1`：统一的全量 Python ≥80%、前端 lint/build/security/desktop（含语音和文件工作台）/build-info、Rust 本地检查。CI 使用相同阈值及真实入口，另有变异测试证明竞态/身份错误会使入口失败。
- `scripts/release-gate.ps1`：单份 Eval 与明确基线的比较，只证明该评测层。`Mode`、`Baseline` 必填；suite/case 来自源码预定合同，不来自报告自称的清单。
- `scripts/rc_gate.py`：只读的 **pre-tag RC 总聚合**，不执行模型、麦克风、安装或发行。通过状态只叫 `RC_READY_NOT_RELEASED`，不叫 RELEASED。缺项、未知、旧来源与不相符层级均阻断。

## 采集与核对

1. `python scripts/rc_gate.py --print-contract` 输出固定的 `V160-*` requirement ID。RC 中暂不要求 `git_tag_consistency`，避免“必须先有 tag 才能冻结候选”的循环；正式发布仍由既有 `check-release-metadata.py --release` 检查 tag 和发布状态。
2. 自动化采集使用 `python scripts/record-rc-check.py --gate frontend_desktop_tests --output executions/unique-desktop.json`。参数只选择已有离线检查，不接受任意命令；混合后端/安全矩阵会运行既有完整 `test.ps1`。后端固定清理主机的 PYTEST/COVERAGE/Python 优化及插件环境，显式加载锁定插件、固定整个 tests/backend 与 rootdir。原始 JUnit、预筛选 collection、coverage JSON、execution receipt 四份附件均绑定 SHA-256；exit 0 不代替逐 case、所有 app 源文件 ≥80% 与四个 Windows native commit probe 的精确名称且不得 skip。输出不可覆盖，源码前后指纹不同则 FAIL。DIRTY 源码可以做开发验证，不能成为 RC。
3. 四份完整确定性 Eval（core、multi_agent、professional_agents、adversarial）及默认模型真实 core Eval 分别提供 candidate、baseline。运行时增加 `--capture-source --require-passed`，将源码执行前后身份写入报告；不会自动开启 live。真实云模型必须另有预算授权。没有可比旧基线时应先建立基线，不能复制当前报告、改变 run_id 冒充旧版本或把缺失解释成“无回归”。
4. Runtime 文件模型资格使用 `app.evals.local_model_benchmark.runtime_file_cli`，`runtime-file-v1`、v16、每必选项至少三次且 `local_live`。scripted 验证 Runtime 实现，不授予模型资格。`--capture-source` 为仓库模式可选项，正式 RC 必须带上。模型身份文件是选定默认本地模型的有效公开能力快照，不含密钥，不自动下载或加载模型。
5. 人工记录必须由真实操作后生成：`kind=manual`、operator/operator_attested、对应 build_id、具体 requirement ID 与源码绑定。单元测试不允许转换为人工证据。安装证据继续复用现有 NSIS/MSI 完整校验器，必须包括实际上一版本升级、隔离数据库、保数、卸载/重装与进程退出，不用空 EXE 代替。
6. 组装 `build/v1600-evidence/rc-bundle.json` 后，执行 `python scripts/rc_gate.py --bundle build/v1600-evidence/rc-bundle.json --model-identity build/v1600-evidence/default-model-identity.json`。生成报告后的任何源码/附件变化均须重新核对。

### Eval 的公开投影

原始 Eval 报告可含本机路径、任务文本和 trace，须保持在本地且不改写。
`scripts/record-rc-eval-public.py --report <原始报告> --output build/v1600-evidence/accepted/evals/<新名称>.public.json`
另存严格白名单的 `rc_eval_public_evidence / eval-public-v1`。它原样保留测量源码前后身份、
任务定义 hash、逐 case 结果、rule/boolean、模型/端点摘要、运行环境、指标及失败；投影器自己的源码与生成时间另列。
`private_origin` 的字节数和 SHA-256 只是原件承诺，不是公开附件引用、数字签名或执行证明。
旧 Eval 没有的 `actual_run` 或执行 argv 不得补造；投影成功不叫测量成功或 RC 通过。
总门禁消费相同事实并继续执行原比较合同，包括 15% 平均任务耗时门槛；不得重新绑定旧测量到新提交。
导出、接收和材料化使用同一严格 typed 校验及既有隐私扫描，隐藏附件、未知字段与原件绝对路径仍拒绝。

## Bundle 结构

每个文件引用都是 `{ "path": "仓库内相对路径", "sha256": "实际文件 SHA-256" }`。拒绝路径逃逸、符号链接/reparse 和内容哈希不符，JSON 读取上限 32 MiB。

顶层字段：

- `schema_version: 1`、`protocol_version: "siyi-rc-v1"`、`target_version: "16.0.0"`。
- `source`、`source_after`：均为现有 `generate_build_info._release_source_identity` 的完整结果，包含版本、提交、工作区 clean 和 source_tree_fingerprint。以实际源码为准，不手改 DIRTY 为 CLEAN。
- `component_manifests`：react、tauri、sidecar 三份真实构建身份引用。逐字段匹配同一 Release manifest，包括 schema 和 component_build_ids。
- `component_observations`：react、tauri、sidecar 各自 `rc_component_observation` 封套引用。生产桌面启动 collector 读取真实应用收据，分别来自 React 原始注入值、Tauri invoke 和带进程认证的 Sidecar diagnostics。每份观察同时绑定实际两个 EXE 的哈希及原始桌面收据；三份旁置 manifest 不满足此项。
- `binaries`：desktop、sidecar 的实际 EXE 引用；安装包另外由 installer validator 对目标 bundle 实物核对。
- `sidecar_payload`：`rc_sidecar_payload_inventory` 引用，完整列出 onedir `_internal` 的每个文件相对路径、字节数、SHA-256，限制 20,000 个目录/文件节点、4 GiB、120 秒。既校验附件哈希，也重新核对实际完整目录；EXE 没变但 `_internal` 变了，原验收仍失效。`payload_content_sha256` 是按相对 `_internal` 文件名排序的 `{path,bytes,sha256}` 列表以 `sort_keys=True,separators=(',',':'),ensure_ascii=True` JSON 编码后的 SHA-256，不包含机器位置。
- `matrix`：TEST_MATRIX 引用；schema≥5，补充 target/protocol/source/source_after。每个 gate 的 requirement_id 必须与 `--print-contract` 相同。
- `evaluations`：core、multi_agent、professional_agents、adversarial、default_model，每项含 candidate/baseline 两个报告引用。完整案例、任务定义 hash、mode/layer、模型/端点和可比较环境都必须相符。
- `runtime_file_qualification`：实际模型 Runtime 资格报告引用；当前有效配置/模型 digest 不同即失效。
- `installers`：nsis、msi 两份真实隔离生命周期证据引用。

## 单项证据封套

TEST_MATRIX 的 `evidence[]` 保留 kind/actual_run/outcome；再增加 `report` 文件引用。引用内容使用 `report_type: rc_check_evidence`，含 source/source_after、target_version、kind、actual_run、status，以及 `checks: { "准确的 V160 requirement_id": true }`。自动化还需要实际 argv 数组、cwd、exit_code=0、timed_out=false；无关 `exit(0)` 不被接受。desktop/manual 必须绑定 build_id、`binary_sha256: {desktop, sidecar}` 两个实际 EXE 的小写 SHA-256，以及 `sidecar_payload_sha256` 完整内容摘要，manual 必须真实操作者确认。相同源码重新构建但二进制或内部 payload 字节变化时，不自动沿用旧人工记录。附件名称不是证明，封套不是数字签名；它负责防止意外错绑和层级混用，不能抵御有权限改写所有源文件和证据的攻击者。

启动性能门禁要求 `measurement_object=desktop`、`measurement_protocol=desktop-render-ready-v1`，同机、同启动路径、相同缓存状态、基线与候选各至少五次正有限值、不同 build/run_id。每次都引用唯一原始观察附件与唯一 acceptance nonce，严格比对实际 collector argv，拒绝把脚本名放在 `-Command` 参数里冒充执行。只计算真实采样中位数；回退超过 20% 阻断，超过 10% 没有解释也阻断。局部 1 GiB metadata 或 Sidecar /health 时间不满足桌面启动性能。

真实采集入口为 `record-rc-desktop-startup.py --desktop <仓库相对EXE> --sidecar <仓库相对EXE> --output build/v1600-evidence/<新文件>.json --cache-state warm --startup-path portable-desktop`。collector 为每次启动创建唯一隔离数据目录与 exclusive ownership marker，把 nonce 交给原生生产桥，核对 receipt 的 desktop/sidecar PID、实际 Windows ExecutablePath 和父进程关系。外部 `readiness_ms` 从 Popen 到完整收据可读；原生内部计时单独记 `runtime_readiness_ms`。生产桥收据在 React 提交并经过两个帧调度、Tauri invoke 与 authenticated Sidecar diagnostics 成功后记录，不能推导像素或用户点击验收。退出只给自己创建进程的窗口发送 WM_CLOSE，并要求其 Sidecar 退出。

`record-rc-performance.py --measurement-object desktop --baseline <旧桌面EXE> --candidate <候选桌面EXE> --output build/v1600-evidence/<新文件>.json --startup-path portable-desktop --cache-state warm` 执行每组一次预热及五次独立启动，每个 EXE 的同目录必须含实际 `agent-backend.exe` 与 `_internal`。输出同时提供候选三组件 manifest/观察与 payload 引用。无生产桥的旧版本不能假装提供 render-ready 基线。`--measurement-object sidecar --startup-path frozen-sidecar` 单独记录子组件时间，明确 `desktop_startup_qualified=false`。目前不实现受控 cold-cache，不能把 warm 记录改称冷启动。

新桌面性能封套采用 `command_protocol=python-script-argv-v1`：`command` 是实际 `sys.argv`，
不是完整原生 argv。须从仓库根目录用锁定 CPython 3.12 直接执行相对脚本路径，且不加 `-B`、`-c` 等 Python 启动选项。
Windows venv 可重写原生 argv[0]，因此完整 `sys.orig_argv` 只记录摘要，同时记录实际 launcher/runtime 可执行文件 hash 和版本，
不公开本机安装路径，不把摘要称为签名。前后解释器及 argv 身份须相同；各样本使用真正传入子进程的解释器 basename、
相对脚本参数及显式 `executable` 绑定。该 typed 协议只适用于这一个桌面性能采集入口，不能满足其他自动化门禁；
旧原生 argv 收据仍按原合同核验，不能事后删路径伪装成新收据。

开发候选仅 `record-rc-desktop-startup.py --development-candidate` 可以接受 DIRTY Release 构建，输出 `DEVELOPMENT_PASS`、`rc_eligible=false`，保留实际 DIRTY；总 RC 不接受该状态，也不把它转换成 CLEAN。该参数不授予麦克风、模型调用或安装验收。采集时短暂正常显示自己创建的测试桌面窗口，使 WebView 能调度实际渲染帧；隐藏窗口会暂停帧调度，不能作为 render-ready 证据。只关闭该测试窗口，不关闭其他应用。

NSIS/MSI 原始证据新增实际 installed `binary_sha256:{desktop,sidecar}`、`sidecar_payload_sha256`、完整 `installed_sidecar_payload` 及 `source_after=source`。安装版仅接受从已验收便携字节派生的固定 Tauri SDK 三字节类型标记；详见 ACCEPTED_ARTIFACT_CONTRACT。desktop/manual 封套须明确安装版 `artifact_variant`，默认仍严格绑定便携 EXE。其他字节或内部 payload 变化均不能继承旧人工证明。

退出采集通过原生 `main` WebviewWindow 取得实际 HWND，核验窗口仍存在且属于保留的 Popen PID，然后仅向该 HWND 发 WM_CLOSE；不枚举/关闭框架辅助窗口或其他应用。隔离数据库在所属进程树停止后才清理，避免验收清理造成数据库被重新创建。

## 尚需在冻结时提供的材料

当前没有 clean v16 RC、可比完整真实模型基线、真实麦克风及最终安装包，因此总门禁应保持 BLOCKED。不要生成“看起来完整”的成功 bundle。候选冻结、真实授权验收与最终发行仍按实施方案的 M4/M5 分开进行。
