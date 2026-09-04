# 司忆 v15.0.0 实施反馈

## 当前结论

v15.0.0 已完成候选源码、Windows 桌面构建、安装器生命周期和大部分人工验收，但**尚未达到正式发布条件**。当前证据绑定源码提交：

`2f67faa3852b753bb3d79bb83507ddf8800adac2`

31 项必需门禁的真实状态是：

- 28 项 `PASS`
- 1 项 `FAIL`：`manual.deepseek`
- 2 项 `BLOCKED`：`manual.voice_basic`、`automated.git_tag_consistency`

因此 `release_status` 必须保持 `BLOCKED`，不得标记为 `READY` 或 `RELEASED`。

## 已实现范围

- 统一 Permission Execution Contract，并让 readonly 对所有副作用 fail closed。
- 覆盖活动任务状态、租约、Provider 等待、预算、截止时间与受管进程身份的崩溃恢复。
- 加固 Windows 路径、symlink/junction/reparse point、UNC、设备路径与越界访问。
- 将命令与外部 MCP 快照改为显式 operation checkpoint，区分可恢复路径与 receipt-only 边界。
- 完成 file_batch v2 的预检、绑定授权、事务执行和回滚语义。
- 引入 ProviderDescriptor、本地已安装模型发现与选择，并新增本地模型 Benchmark v1。
- 完成 MCP JSON-RPC、会话、发现、撤销、凭据绑定与类型化失败契约。
- 将 Memory 统一到 user/workspace/conversation/task 权威记录，同时保留 Schema 42 历史升级和兼容读取。
- 在 characterization tests 保护下局部拆分 Runner 与 database.py，未重写现有 Agent/SQLite。
- 增强桌面对话隔离、Sidecar endpoint epoch、进程身份与 Files UI 操作反馈。
- 让版本、源码提交、安装包、升级基线、构建指纹与发布证据能够由同一流水线校验。

## 自动化验证

Automated 为 `13/14 PASS`，唯一未通过项是最终标签一致性。

- 后端：`1367 passed, 31 skipped, 2 warnings`。
- 覆盖率：`82.28%`，高于 80% 门槛。
- 前端 lint：PASS。
- 前端生产构建：PASS，2020 个模块；最大 JS chunk 为 519.09 kB，保留 Vite 体积警告。
- 前端 security：PASS，扫描 96 个源码文件与 7 个构建文件，语音/TTS/权限相关静态契约全部通过。
- 前端 desktop reliability：PASS。
- Rust：`11 passed`，另有一条 MSVC 链接器库创建警告。
- Core Eval：`18/18 PASS`。
- Professional Eval：`8/8 PASS`。
- Multi-Agent Eval：`10/10 PASS`。
- Adversarial Eval：`4/4 PASS`。
- 版本一致性：15 个版本/证据源一致为 `15.0.0`。
- 正式工作区 tag 预检：BLOCKED；当前没有最终 `v15.0.0` tag，且保留了五个用户所有的未跟踪交接/v14 文件。

### 自动化重跑中暴露的问题

第一次在干净候选克隆运行完整套件时，4 个 v14 路径保护测试因 `.gitignore` 排除的历史目录不存在而失败。补充两个明确标为“仅用于路径语义、无 v14 运行时、不可充当发布证据”的非空 sentinel 后，定点测试 `7/7 PASS`，后端全量通过。

随后前端 security 在该克隆上发现行尾敏感断言：全局 `core.autocrlf=true` 把 `LocalAiPanel.tsx` 转成 CRLF，而测试硬编码搜索 `\n\n`。处理器实际存在，Git blob 与正式工作区均为 LF；相同 blob 在规范 LF 表示下 security 全部通过。该测试可移植性问题保留为警告，未修改产品源码或弱化断言。

两次失败日志与最终定点通过日志均保留，未隐藏失败历史。

## 桌面与安装器验证

Desktop 为 `8/8 PASS`。

- Tauri 正式产物及冻结支持文件：PASS。
- NSIS：16/16 检查通过。
- MSI：管理员真实运行，17/17 检查通过。
- 全新安装、启动、Sidecar 健康：PASS。
- v14.0.0 → v15.0.0 升级：PASS；Schema 44 → 45、迁移前备份及测试数据保留均通过。
- 卸载：PASS；程序文件移除，测试数据和模型按策略保留，重装可识别。
- 性能：候选中位数 1667 ms，v14 基线 1654 ms，回归 0.79%，PASS。

冻结产物：

- NSIS SHA-256：`F6E0AC0D80C1AE372EE8B4A65177B7093DEDA20ADD8F5E5665309D2581A4AABD`
- MSI SHA-256：`96F6CE976FC85B55AD36C6972F8B2EFB09AE409055036B8BBACE235396C968B6`
- `司忆.exe` SHA-256：`F6AE48C2DED6C69FED554A8D60995EBF1F9006F190DB7C1AF60CE2AEDC6BA901`
- `agent-backend.exe` SHA-256：`26A1F680B5FEF8B5CF695550472EC875B06E6779A3AB5174407B09B823073F6E`

产物尚未执行正式上传/发布；本地候选可用不等于已发行。

## 人工桌面验收

Manual 为 `7/9 PASS`。

已通过：

- Chat：真实 Ollama `qwen2.5:1.5b` 对话完成。
- 文件：创建、编辑、移动、删除、Undo 恢复完成，恢复哈希与删除前一致。
- readonly：写入、Undo、上传和计划等副作用入口正确禁用。
- ask：高风险文件操作出现明确执行确认，并记录 approved receipt。
- Ollama：环回健康、模型调用与对话时间线完成。
- MCP：真实 loopback HTTP discovery、critical once 授权、tool call、receipt、禁用后撤销与 session DELETE 均通过。
- Memory：隔离数据根写入、检索和标记命中通过。

未通过：

1. `manual.deepseek = FAIL`

   已按授权执行一次最小付费真实请求。`deepseek-v4-flash` 返回 `reasoning_content` 且 `finish_reason=length`，但可见内容为空，应用最终报 `empty_after_reasoning`。本次调用没有被算作 PASS；未获得新的明确授权前不得执行第二次付费请求。

2. `manual.voice_basic = BLOCKED`

   small 模型已存在于正式模型目录，共 15 个文件、486,217,555 bytes，树哈希为 `C35ADDEB6A60350B06D430FC840CB23D2A0345BF951A9EFC3AF3B9547F6B43B6`。在 1 GiB 自动化专用 RAM 下限下，真实本地 CPU/int8 small 模型加载及 14 项合成语音检查通过；但该证据明确标注 `eligible_for_manual_voice_basic=false`。真实麦克风采集和桌面 renderer 流程均未运行，不能用合成 WAV 代替。

## 本地模型边界

`local_model_benchmark_basic` 所要求的 Basic 套件为 `3/3 PASS`，所以该单项门禁合法通过。完整 19 项 Benchmark 仍只有 `10/19`，状态为 `failed` 且 `release_gate_eligible=false`；Tool、Reasoning、Safety 能力仍有明显差距，不得把 Basic 通过改写成“完整本地模型能力已通过”。

## 已知技术边界

- `run_command.affected_paths` 依赖调用方如实声明；空数组只产生 receipt-only 检查点。
- 外部 MCP 的工作区外副作用无法由本地文件快照回滚，只保留收据和审计。
- Runner 的 workspace-free 流程已拆出，但 workspace `_run_chat` 仍然较大；本版不做重写。
- Memory 仍保留旧表兼容层，当前是单向镜像和精确内容去重，不是语义去重。
- 前端 security 的静态源码切片对 CRLF 不可移植，后续应改为行尾无关解析，并补 Windows `core.autocrlf=true` 回归测试。
- 完整本地模型 Benchmark、DeepSeek 可见输出和真实麦克风桌面流仍未达到发布要求。

## 剩余发布动作

只有在以下事项完成后，才可重新评估 `RELEASED`：

1. 修正/确认 DeepSeek 可见输出路径，并在**新的明确付费授权**下重新执行最小真实请求。
2. 在**操作时麦克风确认**下，由用户说出固定非敏感短句，完成真实桌面录音、转写、renderer 回填/发送与清理验证。
3. 让 31 项门禁全部为 PASS，再创建指向最终 evidence-only HEAD 的正式 `v15.0.0` tag。
4. 在干净正式发布 checkout 运行 `python scripts/check-release-metadata.py --release`。
5. 上传/推送已冻结产物，并对最终分发物做一次哈希及安装后复核。

本轮没有删除用户未跟踪文件，没有把临时候选标签当作正式标签，也没有执行第二次付费 DeepSeek 调用或未经确认的麦克风采集。
