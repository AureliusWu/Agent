# 司忆 v9.0.0 本地模型测试体系：现状审计

审计基线：`6f1e27a`（分支 `local/v9.0.0`）

## 结论

现有代码已经具备 OpenAI-compatible HTTP、流式输出、原生工具调用、取消传播、
Provider 健康检查与能力观测等基础设施，但调用入口仍集中在一个 DeepSeek 导向的
`provider.py` 中。当前缺少统一 Provider 对象、确定性的 Mock Provider、受限的
Ollama Provider、付费测试硬门禁，以及可逐层归因的本地模型测试体系。

因此本次升级采用兼容式重构：保留已验证的底层传输和 DeepSeek 行为，通过 Registry
把 Runtime 迁移到统一接口；不重写 Executor、权限、安全、审计或持久化边界。

## 差距矩阵

| 能力 | 当前状态 | v9.0.0 目标 | 主要风险 | 处理方式 |
|---|---|---|---|---|
| 统一 Provider 接口 | 缺失，Runtime 直接导入函数 | `LLMProvider` 完整契约 | 大范围回归 | 兼容适配器 + Registry |
| DeepSeek | 已有且可用 | 行为保持兼容 | 误耗付费 token | 付费验收双环境变量门禁 |
| Mock Provider | 缺失 | 17 类确定性场景 | 假阳性 | 每个 PASS 绑定实际 pytest 证据 |
| Ollama | 缺失 | 仅本机回环、仅 `qwen3:4b` | SSRF、模型漂移 | 固定 host/port/model 白名单 |
| 错误分类 | Provider 私有错误码 | 六类统一失败分类 | 归因混乱 | 集中映射并保留原错误码 |
| 分层测试 | 通用单测/Eval | A Mock、B 本地模型、C 付费验收 | 默认调用付费 API | 脚本级和代码级双门禁 |
| Provider 配置 | DeepSeek 配置散落 | 本地 JSON 配置 + Credential Manager | 泄密、测试污染 | 不持久化密钥，测试使用隔离目录 |
| UI 诊断 | DeepSeek 专用面板 | Provider 选择、能力与健康状态 | 误导用户 | 明示来源、模型、状态和失败类别 |
| 发布证据 | v8.0.3 基线 | v9.0.0 本地构建与报告 | 版本先行 | 所有门禁前保持 8.0.3 |

## 当前直接依赖点

- `runtime/runner.py`
- `runtime/task_runtime.py`
- `context/service.py`
- `artifacts/title_jobs.py`
- `kernel/adapters.py`
- `kernel/services.py`
- `api/routes/system.py`

这些入口将迁移到 Provider Registry；底层 HTTP 实现继续留在
`providers/provider.py`，避免同时改变传输协议与 Runtime 编排。

## 不可破坏边界

1. 默认测试不得访问任何付费模型。
2. Ollama 只允许 `http://127.0.0.1:11434`、`http://localhost:11434`
   或等价 IPv6 回环地址，不允许远程 Ollama。
3. 本地模型固定 `qwen3:4b`，不得隐式下载或回退其他模型。
4. API Key 仍只通过环境变量/Windows Credential Manager 提供，不写入配置、日志、
   数据库或诊断包。
5. Provider 必须继续经过数据流记录、网络策略、取消、审计和模型运行持久化。
6. 本次只做 Windows PC 桌面端；不恢复已剥离的 Web/PWA/移动端代码。

