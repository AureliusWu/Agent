# 司忆 6.0.0 实施记录

## 目标

6.0.0 将司忆收口为可持续执行、自动调度工具的基础Agent桌面系统。Voice Mode 与 Computer Use 仅保留接口，不进入本版界面和验收。

## 已实现

- SQLite Schema 25 新增 `execution_segments` 与 `workspace_instruction_snapshots`，迁移前继续自动备份。
- 默认 Token、轮数、工具次数和单段超时改为分段压力信号；检查点、确定性压缩和重启恢复保留任务现场。
- DeepSeek 原生 `reasoning_content` 在工具轮次中完整回传；严格解析已暴露工具的 DSML 降级协议，未知工具拒绝执行且协议不外泄。
- ToolScheduler 按 `parallel_safe`、`serial`、`exclusive` 调度，保持结果顺序、失败隔离和取消传播。
- 自动按作用域加载 `AGENTS.md` 与 `AGENTS.override.md`；README 默认非权威，不自动创建或修改项目说明文件。
- Tavily 与 Brave 使用统一 SearchProvider，`web_fetch` 经过 SSRF、重定向、MIME 和大小限制；搜索结果按不可信数据进入上下文。
- 界面只显示基础Agent，隐藏 Profile、编排模式和常驻摘要；增加推理时间线、搜索进度及 `/clear`、`/compact`、`/context`、`/cost`、`/doctor`。

## 验收证据

- 后端全量测试覆盖连续分段、工具调度、项目指令、搜索安全、413 与恢复路径。
- 长任务测试跨越 13 个模型工具轮次、52 次工具调用和 210,000 Token，无 `token_limit` 终止。
- 真实 30 轮无工作区对话通过身份、长期记忆、上下文装配和执行分段检查。
- Tavily 真实健康检查与基础Agent搜索通过，最终引用必须来自实际工具结果；Brave 因管理员选择不绑定付费方式，本版保留实现并按未配置状态验收。
