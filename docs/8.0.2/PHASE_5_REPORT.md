# 司忆 v8.0.2 Phase 5 记忆搜索与斜杠指令报告

记录时间：2026-07-27

## 当前结论

Phase 5 的产品实现已落地，精确源码提交为 `01c22534c86f25622e9e3e4223b4ecbcc1d6a232`，对应 GitHub CI run `30265632742` 已成功。新增 18 项 MEMSEARCH/SLASH 门禁中，14 项具有实际运行证据并标记 `PASS`；`V802-MEMSEARCH-005`、`V802-MEMSEARCH-008`、`V802-SLASH-008`、`V802-SLASH-010` 仍为 `NOT_RUN`。因此 Phase 5 证据尚未完全闭合，产品版本仍为 `8.0.1`，整体发布状态仍为 `NOT_READY`。

## 实现边界

- Schema 升至 v32，长期记忆使用 SQLite FTS5，并由插入、更新、删除触发器事务性同步；迁移沿用迁移前备份和失败自动恢复。
- `MemorySearchService` 保持 `personal_long_term` 与工程记忆命名空间隔离，支持标题、正文、标签、类型、状态、来源、确认/锁定、有效期、重要性、可信度、排序、分页和不透明游标。
- 排名返回关键词、FTS、既有长期记忆召回权重、重要性、可信度和时效性分解，不引入外部向量数据库。
- 普通搜索默认排除敏感记忆；去敏或完整结果必须使用一次性、会话绑定、载荷绑定的管理员授权。敏感查询使用 POST，请求日志只记录路径。
- 长期记忆页提供搜索、类型/状态/敏感模式/排序和清除条件；全局搜索将工程记忆与个人长期记忆分组，结果点击后携带查询、定位并高亮真实记录。
- 后端 Command Registry 是运行时唯一目录，覆盖 `/help`、`/clear`、`/compact`、`/context`、`/cost`、`/doctor`、`/memory`、`/search`、`/stop`，并统一校验参数、对话/工作区、运行、待确认、恢复和风险状态。
- 斜杠输入先于模型、NEXT 和 STEER 路由；本地指令结果使用独立“本地指令”系统卡，不伪装成夏目心回答，也不写入消息历史或记忆候选。
- 指令审计只保存指令名和状态，明确不记录参数。

## UI 验收发现并修复的问题

隔离浏览器实际执行首次发现：指令审计返回 `204` 空响应，而前端通用 API 客户端仍解析 JSON，导致 `/context` 已成功却显示失败。修复后审计接口返回明确 JSON 确认，系统卡只在实际动作和审计均成功后出现。

第二次发现：记忆创建授权按浏览器提交的 `1` 生成哈希，Pydantic 验证后变成 `1.0`，语义相同但旧哈希不同，真实 UI 创建被 403 拒绝。现已对授权载荷做稳定数值规范化，并按客户端实际提交字段绑定授权；字段和值仍严格校验。

## 本地实际运行证据

全量命令：

```powershell
.\scripts\test.ps1
```

结果：后端 `416 passed, 1 skipped`，覆盖率 `82.59%`；前端 lint、TypeScript/Vite 构建、安全检查、推理边界、指令菜单、零模型路由、请求竞态和高亮测试均通过。

桌面命令：

```powershell
cargo test
```

结果：`5 passed`，包含 Windows Credential Manager 往返测试。

隐私命令：

```powershell
.\siyi\.venv\Scripts\python.exe scripts/privacy_scan.py --staged
.\siyi\.venv\Scripts\python.exe scripts/privacy_scan.py --tracked --history
```

结果：均通过，无禁止的私有数据模式。

前端最终产物：主 JS gzip `133.34 KB`；Phase 4 基线为 `130.13 KB`，增长约 `2.47%`，低于 10% 性能警戒线。

## 隔离 UI 与数据库证据

测试使用 `AGENT_DATA_ROOT=<isolated-temp>/siyi-v802-phase5-current`，未读取真实用户 AppData。实际观察：

- 输入 `/` 显示九项受控指令，空闲时 `/stop` 显示禁用原因。
- 输入 `/co` 只显示 `/compact`、`/context`、`/cost`；方向键和 Tab 将选择补全为 `/context`。
- `/context` 产生独立“本地指令”系统卡。
- `/memory semantic` 命中隔离数据库中的真实长期记忆。
- `/search memory-search-proof` 打开全局搜索，保留关键词，显示“个人长期记忆”分组并高亮命中词。
- 点击结果进入长期记忆页；目标 `data-memory-id` 唯一，边界为 `top=351.2`、`bottom=475.2`、视口高度 `720`，证明目标已滚动到可视区。
- 普通搜索 `sensitive-ui-keyword` 不返回已写入的敏感记忆。
- 执行 `/context` 和 `/memory` 后，SQLite 观测为 `model_runs=0`、`messages=0`、`conversation_queue_items=0`、`memory_candidates=0`；指令审计详情仅为 `{"arguments_recorded": false}`。

浏览器验收截图已在本次 Codex 任务中实际生成并展示；没有把截图、合成数据库或日志加入 Git。测试服务已经关闭。临时目录删除命令被本机命令策略拒绝，未绕过策略；该目录仅含可重建的合成数据库与测试日志，不含真实用户数据。

## CI 与用例状态

首个功能提交 `98caa4ccd38cfe0f7e6ec14abd9a436f0d002a18` 对应 CI run `30264508393` 成功。隔离 UI 随后发现上述两个产品缺陷，因此旧 run 不作为最终 Phase 5 证据。

最终 GitHub CI run `30265632742` 精确绑定 `01c22534c86f25622e9e3e4223b4ecbcc1d6a232`，结论 `success`。CI 实际执行版本元数据、隐私/密钥/PII、完整后端、评测契约、紧凑对抗门禁、前端、Windows 桌面壳和 SBOM。

已转为 `PASS`：

- `V802-MEMSEARCH-001~004`、`006~007`
- `V802-SLASH-001~007`、`009`

仍为 `NOT_RUN`：

- `V802-MEMSEARCH-005`：纯竞态门和组件接入已有测试，但尚未用可控延迟对真实组件请求做反序完成注入。
- `V802-MEMSEARCH-008`：无结果 UI 已实际观察，搜索 API 故障状态尚未做受控注入，因此整项不判 PASS。
- `V802-SLASH-008`：`/stop` 已绑定真实控制面，但尚未在实际运行任务中从 Composer 执行并核对 UI、进程和 SQLite。
- `V802-SLASH-010`：修复了实际审计响应故障，但尚未完成通用受控失败注入矩阵，不能由单一修复推导整项通过。

## 发布影响

- Phase 5 未全闭环，后续桌面 Harness 必须补齐四项 `NOT_RUN`。
- 18 个真实场景、真实搜索/对话、50 轮真实 Token A/B、2 小时长任务、Evo 二次候选和安装生命周期仍未完成。
- `Multi-Agent 0/3` 与 `Professional Agent 0/4` 的旧评测契约冲突仍是 `FAIL`，没有被本阶段覆盖或改写。
- 当前仍为 `PARTIAL / NOT_READY / NOT_DISTRIBUTED`，不得更新为 v8.0.2、打 Tag 或创建 Release。
