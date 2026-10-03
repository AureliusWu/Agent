# 从 DeepSeek Harness 与 Codex 学习插件架构

检索日期：2026-10-03。DeepSeek Harness 参考快照为 `master` 的 `da00f7f5358f2949383b35c14f548bc20187d80c`（提交时间 2026-10-02 23:44:05 UTC）。Codex 官方更新页当时最新条目为 2026-10-01 的 CLI `0.160.0`。这些是本轮的公开文档快照，后续更新应重新比较。

## 净室实现范围

本轮仅阅读外部 README、架构说明、能力图、工具执行流程和官方协议文档；没有读取或搬运外部实现源码、私有提示词，也没有引入 Cordis、DeepSeek Harness 或 Codex 源码依赖。需求、类型、搜索算法、处理器和测试在司忆代码库中独立编写。这里实现的是可解释、可测试的同类架构模式，产品和包格式兼容性需要另外验证。

| 参考 | 公开设计要点 | 司忆的取舍与落地 |
| --- | --- | --- |
| DeepSeek Harness 架构 | 插件组成服务，接口、实现、使用方分开 | `PluginDefinition` + `PluginCall` + 显式组合根；先模块化能力层，保留可信 Executor 和安全内核 |
| DeepSeek Harness 能力图 | 文件、网络、语音等能力通过统一接口替换实现 | `SpeechAdapter` 定义合同，HTTP 供应商实现合同，听说插件消费合同；未来可追加本地识别适配器 |
| DeepSeek Harness 工具流水线 | 统一执行链；不可绕过的拒绝规则、审批和最终结果观察 | 范围检查在 Hook 之前，处理器复用原审批与沙箱，结束后统一模块来源和回执 |
| DeepSeek Harness Cordis 说明 | 依赖显式声明，注册和资源释放可回收 | 可信模块静态组合、服务调用独立；HTTP 会话及文件锁在成功、异常和取消路径清理，热重载待独立设计 |
| OpenAI 工具搜索 | 根据当前状态查找并延迟加载工具定义 | 用普通 function tool 实现本地 `discover_tools`，兼容当前 DeepSeek Provider；下一轮加载可信定义，活动工具上限 24 |
| ChatGPT/Codex 插件打包 | 当前推荐根目录 `plugin.json` 的可移植包，兼容 `.codex-plugin/plugin.json` | 现阶段统一司忆内置能力元数据与现有声明式 SDK；跨产品清单导入、MCP/Skill 聚合和兼容校验作为后续独立层 |
| Codex CLI 0.160.0 | 恢复保留权限和 Provider 设置；不确定提交先处理再重发 | 司忆沿用已有 Provider 绑定、审批与副作用检查点；本轮补上发现后的工具选择恢复与能力重新收窄 |

工具搜索在 OpenAI 文档中包含专用 API 协议；司忆本轮没有声称实现该协议，也没有把 Provider 未证实支持的字段发给 DeepSeek。普通函数接口能先验证“少量初始定义、查找后再加载”的收益。

## 最值得学习的三件事

**先划分接口，再写实现。** 从 `providers/speech.py` 的请求/结果类型和 `SpeechAdapter` 开始，随后阅读 HTTP 适配器和 `plugins/voice.py`。前者负责服务协议，后者负责任务审批、数据来源与文件落盘。替换供应商时可以把测试限定在协议层。

**把模块元数据变成唯一声明。** 阅读 `plugins/contracts.py`、一个小模块（如 `builtins/memory.py`）和 `plugins/registry.py`。API、UI、模型 schema 和工具分派使用同一目录；新增能力时，重复声明和所属冲突在组合阶段失败。避免在 UI 和 Runner 分别维护一份能力清单。

**把“找得到”与“允许执行”分别验证。** 阅读 `plugins/discovery.py`、`runtime/executor.py` 和 `tests/backend/plugins/test_discovery.py`。搜索结果只建议名称；真正加载时重新取可信 schema，真正执行时检查任务范围及原权限。测试故意提交伪造 schema、范围外工具、未配置语音和只读写入请求，验证隔离边界。

之后阅读 `test_voice.py` 的版本变化、跨任务锁、取消和撤销用例。它们展示一个外部调用为何要先验证目标、在等待后再验证目标，以及为何“服务已经返回”仍不能直接宣称写入成功。

## 司忆已有基础与后续顺序

已有的 SQLite 队列、SSE 事件恢复、版本绑定写入、独立 Verifier、上下文预算、Hook、MCP 会话和受管 Worktree 可以直接承载新模块。继续沿这些入口扩展，能保留当前 Windows 桌面交付和身份/记忆连续性。

| 后续顺序 | 目标 | 验收前提 |
| --- | --- | --- |
| 1 | 真实音频供应商联调、桌面录音和播放 | 明确的供应商配置；真实音频、取消、审批和安装后流程证据 |
| 2 | 可移植插件清单的受控导入 | 格式校验、内容哈希、可信来源、权限声明；密钥只使用专用绑定 |
| 3 | 任务范围的服务依赖与资源生命周期 | 可重复装卸、逆序清理、取消释放；安全规则具有不可降级地位 |
| 4 | 更大工具目录下的检索效果与 Token 比较 | 固定任务和对照请求，测量正确检索率、上下文大小及增加的轮次 |

完整的任意代码插件热加载、全核心可替换、远程持久终端和新协作团队系统会扩大信任与部署范围，需按独立里程碑设计。本轮优先保证能力层规整、目录真实、执行可恢复。

## 公开参考

- [DeepSeek Harness 架构](https://github.com/deepseek-ai/deepseek-harness/blob/da00f7f5358f2949383b35c14f548bc20187d80c/docs/architecture.md)
- [能力接口与服务图](https://github.com/deepseek-ai/deepseek-harness/blob/da00f7f5358f2949383b35c14f548bc20187d80c/docs/capability-seams.md)
- [工具执行流水线](https://github.com/deepseek-ai/deepseek-harness/blob/da00f7f5358f2949383b35c14f548bc20187d80c/docs/tool-execution-pipeline.md)
- [Cordis 公开说明](https://github.com/deepseek-ai/deepseek-harness/blob/da00f7f5358f2949383b35c14f548bc20187d80c/docs/cordis-primer.md)
- [Codex 最新更新](https://learn.chatgpt.com/docs/changelog)
- [OpenAI 工具搜索](https://developers.openai.com/api/docs/guides/tools-tool-search)
- [ChatGPT/Codex 插件打包](https://developers.openai.com/plugins/build/plugins)
- [音频文件转写协议](https://developers.openai.com/api/docs/guides/speech-to-text)
- [语音合成协议](https://developers.openai.com/api/docs/guides/text-to-speech)
