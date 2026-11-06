# 司忆 v16.0.3 本地验证

本文件仅记录本版本的实际验证；不继承 v16.0.2 的结论。正式安装、CI 和发行不能由定向测试替代。

- 冻结代码 `a81ea7a040b8358c2cbaa2ad1bc8982cc00aadbf`，制品 `6117d4c5ebed410b669c7a0d`。250 个运行时文件，逐文件比对打包前清单和 ZIP 读回。便携包 93,858,605 字节，完整 SHA256 见证据清单；未替换本机 v16.0.2、未推送或创建 Release。
- 指令作用域定向 11 项，工作区外引导与支持链路 49 项，恢复定向 9 项；这三组有交集，不相加。前端桌面、安全、构建身份、lint、生产构建通过，Rust 17 项通过。Vite 仍有主 JS 块超过 500 kB 的提醒。
- 冻结源码四套隔离脚本评估：核心 18/18、多 Agent 10/10、专业 8/8、对抗 4/4，误报完成、权限及沙箱违规均为 0。所有评估使用脚本模型；不以请求或令牌计数推断真实消费。
- Windows 普通权限后端全量：4,230 通过、29 跳过、1 失败、2 警告，覆盖率 85.01%。唯一失败 `test_local_desktop_launcher.py::test_check_only_valid_runtime_does_not_create_or_launch`，定向重跑 4 过、同 1 失败：当前真实 v16.0.2 仍在运行，原启动器拒绝第二个实例。这两个文件与基线无差异；未关闭用户程序，也未放宽保护或把该项改记为通过。完整测试结果保留在 Git 忽略的 `build/v1603-full-a81ea7a0`。
- 编译 sidecar：离线 TLS 构造 3/3；模拟待办查询、恢复、取消、SQLite 完整性和进程正常退出通过。重启隔离 sidecar 后分别向已取消和已完成任务发恢复请求：均为 409，独立 SQLite 读回确认状态、队列行数和事件行数未变。未做原生桌面点击或安装验收。
- 真实 DeepSeek：生产适配器流式解析通过，只提交 1 个合成请求，HTTP 200，返回 1 个合成工具调用且未执行；输入 298 / 输出 40 Token。按[官方高峰未命中单价](https://api-docs.deepseek.com/quick_start/pricing/)保守估算 0.0001374 USD，预留估算 0.001344 USD，不代表实际账单。无自动重试或重定向。只在隔离进程内读取原模型凭据，不读取搜索密钥，不写入真实用户配置。
- 指令加载同机对照：5 个合成工作区，每侧 3 次预热、20 次计时，交错 100 对样本。加载 p50 2.164 → 5.759 ms、p95 2.999 → 7.322 ms；对应提示词 1,562 → 1,036 字节。严格路径、文件和链接检查带来小幅绝对耗时增加，去掉了重复规则。仅测加载器，不代表启动、HTTP 或模型延迟；同机有测试并行。

## 复跑入口

- 后端新增回归：`tests/backend/workspace/test_workspace_instructions.py`、`tests/backend/runtime/test_workspace_free_steering_v1603.py`、`tests/backend/runtime/test_task_runtime.py`。按 `scripts/test.ps1` 的隔离数据约定运行；请勿把用户数据库或真实凭据交给测试。
- 前端生产 Hook 的合成回归：在 `desktop/frontend` 运行 `npm run test:desktop` 和 `npm run test:security`。Rust：`cargo test --locked --offline`。
- 制品：从冻结提交执行 `scripts/build-desktop.ps1 -CandidateOnly -CandidateDirectory <新目录>`，然后用 `scripts/smoke-provider-connection.py` 和 `scripts/smoke-task-attention.py --expected-version 16.0.3` 验证便携目录的 sidecar。现有用户程序没有参与这些制品冒烟。
