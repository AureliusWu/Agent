# 司忆 v16.0.3 本地验证

本文件仅记录本版本的实际验证；不继承 v16.0.2 的结论。正式安装、CI 和发行不能由定向测试替代。

- 冻结代码 `a81ea7a040b8358c2cbaa2ad1bc8982cc00aadbf`，制品 `6117d4c5ebed410b669c7a0d`。250 个运行时文件，逐文件比对打包前清单和 ZIP 读回。便携包 93,858,605 字节，完整 SHA256 见证据清单；本机已通过原启动入口使用该版，不视作正式 Release。
- 指令作用域定向 11 项，工作区外引导与支持链路 49 项，恢复定向 9 项；这三组有交集，不相加。前端桌面、安全、构建身份、lint、生产构建通过，Rust 17 项通过。Vite 仍有主 JS 块超过 500 kB 的提醒。
- 冻结源码四套隔离脚本评估：核心 18/18、多 Agent 10/10、专业 8/8、对抗 4/4，误报完成、权限及沙箱违规均为 0。所有评估使用脚本模型；不以请求或令牌计数推断真实消费。
- Windows 普通权限后端原全量：4,230 通过、29 跳过、1 失败、2 警告，覆盖率 85.01%。唯一失败 `test_local_desktop_launcher.py::test_check_only_valid_runtime_does_not_create_or_launch`，v16.0.2 运行中触发单实例保护。用户授权正常退出后，未改变启动器或用例，隔离补测 **5/5 通过**；不改写原全量记录。原始结果保留在 Git 忽略的 `build/v1603-full-a81ea7a0`。
- 编译 sidecar：离线 TLS 构造 3/3；模拟待办查询、恢复、取消、SQLite 完整性和进程正常退出通过。重启隔离 sidecar 后分别向已取消和已完成任务发恢复请求：均为 409，独立 SQLite 读回确认状态、队列行数和事件行数未变。
- 隔离 Windows 实机：React、Tauri 和 sidecar 身份一致；新建会话收取 Mock 回复、从人工待办打开并继续指定的中断任务、正常退出。SQLite 检查通过，消息只保存一次，目标任务只恢复一次；没有真实模型调用。一次原生就绪为 2,542 ms，不作为冷启动性能基准。
- 本机便携替换：先校验并备份旧 v16.0.2 运行时和用户库，校验新制品全部文件；新版通过 `CheckOnly` 和原静默入口启动，已验证窗口渲染及配套 loopback 核心身份。原 SQLite schema 46 和完整性检查通过；会话、消息、记忆、模型调用记录及原配置保持一致。未访问或修改 Windows 凭据。回退使用归档中自带的 v16.0.2 `prior-runtime/scripts/start-local-desktop.ps1`，退出 v16.0.3 后显式指定原数据目录。MSI/NSIS 生命周期与正式 RC 不属于此次验收。
- 真实 DeepSeek：生产适配器流式解析通过，只提交 1 个合成请求，HTTP 200，返回 1 个合成工具调用且未执行；输入 298 / 输出 40 Token。按[官方高峰未命中单价](https://api-docs.deepseek.com/quick_start/pricing/)保守估算 0.0001374 USD，预留估算 0.001344 USD，不代表实际账单。无自动重试或重定向。只在隔离进程内读取原模型凭据，不读取搜索密钥，不写入真实用户配置。
- 指令加载同机对照：5 个合成工作区，每侧 3 次预热、20 次计时，交错 100 对样本。加载 p50 2.164 → 5.759 ms、p95 2.999 → 7.322 ms；对应提示词 1,562 → 1,036 字节。严格路径、文件和链接检查带来小幅绝对耗时增加，去掉了重复规则。仅测加载器，不代表启动、HTTP 或模型延迟；同机有测试并行。

## 复跑入口

- 后端新增回归：`tests/backend/workspace/test_workspace_instructions.py`、`tests/backend/runtime/test_workspace_free_steering_v1603.py`、`tests/backend/runtime/test_task_runtime.py`。按 `scripts/test.ps1` 的隔离数据约定运行；请勿把用户数据库或真实凭据交给测试。
- 前端生产 Hook 的合成回归：在 `desktop/frontend` 运行 `npm run test:desktop` 和 `npm run test:security`。Rust：`cargo test --locked --offline`。
- 制品：从冻结提交执行 `scripts/build-desktop.ps1 -CandidateOnly -CandidateDirectory <新目录>`，然后用 `scripts/smoke-provider-connection.py` 和 `scripts/smoke-task-attention.py --expected-version 16.0.3` 验证便携目录的 sidecar。现有用户程序没有参与这些制品冒烟。
