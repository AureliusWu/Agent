# 插件库验收记录

日期：2026-10-03。变更基线：`8fd2eb7bcefd730c440373824bf91ec9752f027b`。本轮交付源码和学习文档，版本仍为 `8.0.1`，未执行安装包发布或付费模型/音频调用。

## 固定验收项

| requirement_id | 类型 | 行为与对应验证 |
| --- | --- | --- |
| `PLG-CODE-001` | Code | 47 项工具只有一个可信所属模块，原有写入版本和风险合同保留；`test_plugin_library.py` |
| `PLG-CODE-002` | Code | 搜索元数据、从可信目录重新加载 schema、上限与重复激活；`test_discovery.py` |
| `PLG-API-001` | API | 目录接口认证、真实配置状态、不存在模块 404；`test_plugin_library.py` |
| `PLG-SEC-001` | Security | 当前任务范围在 Hook 前执行；只读模式、伪造 schema 与未配置服务拒绝；`test_discovery.py` |
| `PLG-SEC-002` | Security | 语音需明确审批，参数绑定防重放，凭据外发拒绝，转写注入处理；`test_voice.py` |
| `PLG-SEC-003` | Security | HTTPS 配置、SSRF、跳转拒绝、流式响应大小、返回类型；`test_speech_protocol.py` |
| `PLG-DB-001` | Database | 发现后的工具集合进入检查点，停止后恢复且只记录一次工具执行；`test_discovery.py` |
| `PLG-CODE-003` | Code | 语音输出跨任务锁、版本变化拒绝、原子写入、撤销及已写副作用恢复；`test_voice.py` |
| `PLG-UI-001` | UI | 模块分类、配置状态、加载/失败/刷新和工具说明已实现；TypeScript、lint、两种生产构建通过；安装后 UI 场景待实测 |
| `PLG-DOC-001` | Document | 配置、来源、代码入口、净室范围、学习顺序和限制；人工核对本文件与另外两份插件文档 |

这些是源码层的聚焦证据。已有 Code/API/UI/Database/Security/Document Verifier 合同继续使用；普通编译和单元测试不替代安装后 UI、真实音频和发布验收。

## 运行检查

- 新增插件与语音用例位于 `tests/backend/plugins/`，全部采用假凭据、模拟供应商或确定性模型返回。
- 已运行插件、上下文、扩展、Kernel、记忆、权限、安全和 Provider 聚焦回归，全部通过。
- 已运行完整后端测试与覆盖率门禁。Linux 容器的 PID 命名空间与挂载的 `/proc` 不一致，8 个子进程/相关 Eval 用例未通过；原始主线在同环境复现同一组 8 个失败。覆盖率高于 70%。未放宽创建指纹、进程终止或审批规则。
- 前端 lint、桌面构建、保留网页构建、凭据边界测试、构建身份测试通过；未增加网页专用功能。
- 核心 18 项、Multi-Agent 3 项、professional 4 项契约已校验并执行。核心命令/Sidecar 用例受上述环境及原有审批循环影响；后两组旧断言要求主线已归一到基础 Agent/单执行链的行为，在基线与候选均未满足。保持严格失败，未调整期望或冒充通过。
- Windows 桌面 Rust、安装/升级/进程清理、真实录音及付费语音供应商未在本机执行。对应提交的 Windows CI 结果以 GitHub Actions 记录为准；CI 成功也不能替代安装包与真实供应商验收。

复现命令：

```powershell
cd siyi
python -m pytest -q --no-cov ../tests/backend/plugins
python -m pytest -q
python -m app.evals.cli validate --suite core
python -m app.evals.cli run --label plugins-core --mode scripted_runtime --suite core --output data/evals --require-passed
python -m app.evals.cli run --label plugins-multi-agent --mode scripted_runtime --tasks ../evals/multi_agent_tasks.json --suite multi_agent --output data/evals --require-passed
python -m app.evals.cli run --label plugins-professional --mode scripted_runtime --tasks ../evals/professional_agent_tasks.json --suite professional_agents --output data/evals --require-passed
cd ../desktop/frontend
npm ci
npm run lint
npm run build:desktop
npm run build
npm run test:security
npm run test:build-info
cd ../..
python scripts/check-release-metadata.py
python scripts/privacy_scan.py --tracked --checks all
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
```

验收应继续区分新增能力回归、现存契约不一致、环境阻塞和真实桌面验证。完整运行报告存放于被忽略的 `data/evals/`，不把数据库、音频或运行期凭据提交到 Git。
