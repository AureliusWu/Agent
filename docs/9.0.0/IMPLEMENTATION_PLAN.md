# 司忆 v9.0.0 本地模型测试体系实施计划

## 发布原则

用户指定本次为大版本升级，最终版本为 `v9.0.0`。实施期间所有版本源保持
`8.0.3`；仅在实现、测试、隐私安全门禁、桌面构建和本地启动验证完成后一次性同步。
本次不推送 GitHub，不默认调用 DeepSeek 付费 API。

## 阶段与门禁

### Phase 1：架构审计

- 输出差距矩阵、依赖点和不可破坏边界。
- 门禁：审计能够解释每一处 Runtime Provider 直接依赖。

### Phase 2：统一 Provider 层

- 新增 `LLMProvider`、统一请求/响应/工具调用/能力/健康契约。
- 新增失败分类：`MODEL_FAILURE`、`PROTOCOL_FAILURE`、`RUNTIME_FAILURE`、
  `TOOL_FAILURE`、`VERIFICATION_FAILURE`、`ENVIRONMENT_FAILURE`。
- 新增 Provider Registry，并以兼容入口迁移 Runtime。
- 门禁：现有 Provider 单测和 Runtime 代表性测试通过。

### Phase 3：Mock Provider 与 A 层测试

- 实现正常回复、流式、工具调用及错误/超时/取消/协议异常等 17 类确定性场景。
- 为每类实际执行用例并输出机器可读结果。
- 门禁：A 层全部 PASS，无网络、无真实密钥、无付费调用。

### Phase 4：Ollama 与 B 层测试

- 提供检测、安装、拉取、测试脚本；模型仅 `qwen3:4b`。
- 完成健康、普通对话、流式、多轮原生工具调用、取消和错误分类验证。
- 门禁：若环境具备条件则全部实际运行；环境原因必须标记 BLOCKED，不能伪造 PASS。

### Phase 5：付费 C 层硬门禁

- 仅当 `SIYI_TEST_PROVIDER=deepseek` 且
  `SIYI_ALLOW_PAID_API=true` 时允许真实 DeepSeek 请求。
- 默认执行只验证门禁有效，不消耗 token。
- 门禁：未经显式授权时结果必须为 BLOCKED/NOT_RUN，不得判 PASS。

### Phase 6：桌面配置与诊断

- 增加 Provider、模型、端点、超时、输出上限、流式/工具开关及健康诊断。
- 密钥继续由 Credential Manager 管理。
- 门禁：前端 lint/build 通过，UI 不暴露密钥或原始凭据端点。

### Phase 7：发布收口

- 运行后端、Eval、隐私/安全、前端、Rust、Sidecar、桌面构建及隔离启动验证。
- 输出测试矩阵、成本、已知限制、回滚和变更报告。
- 所有必需门禁通过后同步 `VERSION`、Python、npm、Cargo、Tauri 和锁文件到
  `9.0.0`，重建本地安装包并验证界面版本。
- 不推送远端。

## 回滚

代码回滚点为基线提交 `6f1e27a`。运行配置采用独立 JSON 文件并使用原子替换；
删除该配置即可回到 DeepSeek 默认行为。Ollama 模型与应用安装不纳入 Git，
可独立卸载，不影响司忆用户数据。

