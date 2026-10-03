# 司忆能力插件库

云端最初在 `8.0.1` 开发线实现插件库；2026-10-03 与本地 `16.0.0` 开发候选整合，未正式发布。扩展页新增能力插件库，按听、说、读、写、执行、记忆展示真实配置状态。保留 v16 的 63 项工具及其成熟执行链，新增语音转写、语音合成、能力发现，共 13 个模块、66 项工具。

## 模块与入口

| 模块 ID | 能力 | 工具数 | 实现文件 |
| --- | --- | ---: | --- |
| `builtin.reading` | 文件阅读、搜索、比较 | 11 | `siyi/app/plugins/builtins/reading.py` |
| `builtin.writing` | 文件创建、修改、复制、移动、删除及事务 | 11 | `siyi/app/plugins/builtins/writing.py` / 组合根补充 |
| `builtin.code` | 工作区索引与 LSP | 9 | `siyi/app/plugins/builtins/code.py` |
| `builtin.history` | 变更记录、撤销与安全快照、批量撤销 | 7 | `siyi/app/plugins/builtins/history.py` / 组合根补充 |
| `builtin.worktrees` | 受管 Git 工作树 | 3 | `siyi/app/plugins/builtins/worktrees.py` |
| `builtin.process` | 受控命令执行 | 1 | `siyi/app/plugins/builtins/process.py` |
| `builtin.memory` | 项目记忆查询与显式变更 | 3 | `siyi/app/plugins/builtins/memory.py` |
| `builtin.web` | 已配置供应商搜索、网页读取 | 2 | `siyi/app/plugins/builtins/web.py` |
| `builtin.hearing` | 音频文件转写 | 1 | `siyi/app/plugins/builtins/hearing.py` |
| `builtin.speaking` | 文字合成为音频文件 | 1 | `siyi/app/plugins/builtins/speaking.py` |
| `builtin.discovery` | 按任务查找能力 | 1 | `siyi/app/plugins/builtins/discovery.py` |
| `builtin.artifacts` | 受控文档生成、编辑、渲染与验证 | 10 | `siyi/app/plugins/builtins/__init__.py` / `siyi/app/artifacts/` |
| `builtin.vision` | 本地与远程视觉分析 | 6 | `siyi/app/plugins/builtins/__init__.py` / `siyi/app/vision/` |

`PluginDefinition` 声明模块元数据、工具契约、异步处理器和配置条件；`PluginCall` 携带工作区、任务、权限函数与任务能力范围。组合根 `builtins/__init__.py` 显式列出可信模块。`PluginLibrary` 拒绝重复 ID、重复工具和所属模块冲突，提供只读的注册映射。

原有 `tools/registry.py` 保留兼容导出，统一 `ToolSpec` 在 `tools/spec.py`；参数、风险、版本、回滚、验证、中断策略和历史选择次序保持 v16 合同。运行时仍通过注册的 Executor 调用工具。原有 63 项工具继续使用成熟执行链，不使用旧云端 handler 替代文件事务、扩展审批或 MCP；仅新增三项能力进入插件分派。统一结果类型在 `tools/outcomes.py`，包含完整 v16 工具回执与模块来源。

## 能力发现与加载

初始模型请求沿用有界语义选择，包含小型 `discover_tools` 入口。模型可用 `{"query":"find_symbol"}` 查找当前请求缺少的能力，也可按能力类别过滤。搜索在本地确定性执行，每次最多返回 6 项元数据，不调用额外模型或搜索服务。

搜索结果同时受当前任务能力范围、权限模式和配置状态限制。运行时根据返回的名称重新从可信目录取定义，忽略结果携带的外部 schema，在下一次模型请求中加载工具。任务最多保留 24 个活动工具，继续使用现有 Token 预检和上下文预算。

发现不授予操作权限：写入和语音仍走各自审批。Executor 在 Hook 和处理器之前拦截任务范围外的调用。运行时保存 `selected_tool_names` 到原有检查点，并记录 `plugins.tools_activated` 事件；恢复时重新与当前允许的工具交集，未配置或被限制的能力不会重新加载。已配置的语音不自动进行付费探测。

内置能力、第三方声明式扩展、Skills 和 MCP 保留各自信任合同。第三方扩展继续使用原 SDK 的安装、校验、升级、禁用和回滚；工作区文件不会被动态导入为 Python 插件。远程 MCP 的发现和会话生命周期继续由既有实现管理。

## 听与说的配置和使用

语音使用独立的 `SpeechAdapter` 协议，当前 HTTP 实现对接 OpenAI-compatible 音频端点。文字模型的 API Key 与模型名称不会自动借给语音服务。配置仅由后端环境变量持有，样例见 `siyi/.env.example`：

| 配置 | 含义 |
| --- | --- |
| `AGENT_SPEECH_ENABLED` | 显式启用，默认 `false` |
| `AGENT_SPEECH_BASE_URL` | 公网 HTTPS 基础地址，如 `https://api.openai.com/v1` |
| `AGENT_SPEECH_API_KEY` | 后端语音凭据 |
| `AGENT_SPEECH_TRANSCRIPTION_MODEL` | 服务实际支持的转写模型 |
| `AGENT_SPEECH_SYNTHESIS_MODEL` | 服务实际支持的合成模型 |
| `AGENT_SPEECH_VOICE` | 服务实际支持的默认声线 |
| `AGENT_SPEECH_TIMEOUT_SECONDS` | 请求超时，默认 60 秒，上限 60 秒 |
| `AGENT_SPEECH_MAX_AUDIO_BYTES` | 音频大小上限，默认 20 MB，上限 20 MB |

读取配置的后端进程需要重新启动。配置齐全后，扩展页显示“已配置 · 待实测”。模型只能在配置满足时发现和选择对应工具。实际服务支持、音质、费用和响应格式由明确批准的一次真实调用确认。

`transcribe_audio` 读取已选工作区内的 WAV、MP3、FLAC、M4A/MP4、OGG 或 WebM 文件。参数包含 `path`，可选 `language` 与 `message_id`。支持扩展名和文件头预检；请求使用通用文件名，保留文字与音频来源关联。转写作为不可信资料进入工具结果，沿用去敏和注入检测，长期记忆不自动保存转写内容。

`synthesize_speech` 接收不超过 4,000 字符的 `text`、工作区目标 `path`、`expected_version_token`，可选 `format`（MP3/WAV）和 `message_id`。先通过 `file_metadata` 读取目标版本，关联有效任务，再批准调用。文字中的凭据会阻止外发。调用前锁定目标并检查版本、保存安全快照，得到有效音频后再次检查版本并原子写入。结果包含相对路径、版本、变更 ID 和 `ai_generated: true`，可以用既有撤销工具恢复。

两种语音工具在所有权限模式中均需要明确批准；批准绑定会话、任务、工作区和参数。网络共用现有公网 HTTPS、元数据拒绝、域名限制和附件拒绝策略，语音请求禁止跳转，响应按流检查大小。本地模型 `local_only` 模式禁止这两项远程语音能力。合成输出的有效上限为语音上限与全局 `AGENT_NETWORK_MAX_RESPONSE_BYTES` 中较小者。此插件完成文件式语音工具；不替换 v16 已有麦克风、STT/TTS、播放器和 voice session 代码。实际设备、实时语音、声线与费用仍应按对应 v16 门禁单独验收，不能因有代码而宣称完成。

## API 和扩展开发

`GET /api/plugins?workspace=...` 返回 `schema_version: 1`、模块列表与工具总数；`GET /api/plugins/{plugin_id}` 返回详情。接口沿用应用认证，响应不包含密钥或凭据地址。状态区分可用、已配置待验证、未配置、需要工作区；联网模块还说明单项工具是否配置。任务临时搜索凭据可用于任务内发现，不写入公共目录。

手动执行工具的原有 `POST /api/tools/execute` 现在也通过 Executor，仍校验会话、工作区、任务和审批。语音合成必须关联有效任务，保证锁、撤销和恢复可追溯。

添加可信模块时按以下顺序实现：

1. 在 `plugins/builtins/` 声明唯一模块 ID、工具 schema、风险与并发合同。
2. 实现异步处理器。外部服务先定义 Protocol，再实现供应商适配器；文件、命令和网络继续复用受控入口。
3. 在 `BUILTIN_PLUGINS` 显式加入模块。注册表、模型工具定义与目录 API 从同一份声明生成。
4. 为审批拒绝、取消、失败、范围隔离与恢复编写聚焦测试；涉及副作用时接入锁、回执、快照和恢复分类。
5. 运行验收文档中的相关检查，确认新能力与无关流程的证据分别成立。

公开架构参考、学习路径和后续取舍见 [HARNESS_LEARNING.md](HARNESS_LEARNING.md)，验证状态见 [PLUGIN_LIBRARY_ACCEPTANCE.md](PLUGIN_LIBRARY_ACCEPTANCE.md)。
