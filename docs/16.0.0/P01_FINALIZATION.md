# P01 模型与资格实施核对

核对日期：2026-10-01。应用版本已由总集成同步为 16.0.0。以下证据来自修改中的源码，不能作为正式发布或真实模型合格证据。

## 已实现的合同

- `providers/effective_capabilities.py` 为 UI、Provider 注册表、Runtime 预算和本地 benchmark 提供同一只读入口。端点身份、Provider、模型、配置、digest、显式能力和实际窗口共同绑定身份；读取不发起网络请求，不包含凭据端点。
- `/show` 理论窗口、配置窗口和 `/ps` 已加载窗口分别记录。有效窗口取实际可用约束的最小值；本地未知窗口停止调用并保留可继续的任务状态。端点 scoped 显式配置不会跨同名模型服务复用。
- 配置保存、历史配置加载、Provider preview 和实际调用均保留合法的显式小预算。Ollama 的历史 2048 Token 下限已移除；输出配置仍必须在 1 至 1,000,000 范围内。Runtime 仍会进一步按上下文窗口收紧输出。
- 公共 Provider stream 使用有界队列，消费者关闭时取消并 await 自有任务。结构化输出在调用前检查支持的有限 JSON Schema 子集，生成后本地校验，不自动修复重试；不支持的关键词和超限输入明确失败。
- 旧本地 benchmark 继续可读，当前模拟报告为 `local-model-v2`、`in_memory_model_simulation`，不能替代真实文件 Runtime 资格。
- `runtime-file-v1` 使用真实 `app.runtime.runner.run_chat`、`LocalWindowsExecutor`、SQLite、权限与审计，以及每次独立的 NTFS 文件夹。固定场景包括基础对话、固定结构化计划、只读读取、创建、局部替换、重命名、移动、Undo 和两项内核拒绝控制。每项至少三次才能成为资格输入；拒绝控制仍标为 scripted_control，不能宣传为模型安全拒绝。
- CLI 新增只读 `--context-profiles`，复制显式提供的 scoped profile JSON 至隔离设置，最多 128 KiB、128 条；严格检查整数、布尔值和字段，拒绝凭据、非 scoped 名称、重复 Key 与未知内容。不继承用户默认环境配置，不写入原配置文件；与 `--context-window` 互斥。
- `local_live` 在首次绑定、每个样本开始和最终结束刷新只读模型元数据，单次最多 45 秒，不增加 chat、pull 或切模型动作。TTL 续期只有身份完全相同才可继续；digest、实际窗口、显式约束或 Provider 配置变化立即阻止下一样本。样本前配置变化先被拒绝，不继续查询旧模型。

## 本地资格存储与 API

`qualification_store.py` 接受用户显式导入的协议证据。它不是签名证明或发布证明；RC 还必须独立校验源码、产物和报告哈希。

- `GET /api/local-models/qualification` 返回 `{effective_capabilities, qualification}`。只读当前小摘要，不扫描完整历史报告，不触发诊断、下载、模型加载或推理。
- `POST /api/local-models/qualification/import` 接受 `{report_json: string}`。进程 API Token 中间件同时保护两个路由；请求字符数和 UTF-8 字节数均不超过 512 KiB。
- 重复 JSON Key、非法代理字符、错误协议、scripted、旧版本、缺少场景或重复样本、旧身份、过期观察、错误磁盘结果、缺少审计/操作完成状态、错误权限与夹带外部 Tool 回执均拒绝。
- 存储位置为 Provider 配置文件旁的 `model-qualifications/`。保留导入报告历史，当前身份文件为小摘要；读取最多 16 KiB，损坏摘要不向外导出路径、内容或解析错误。
- Basic、只读 Tool、结构化计划和文件 Agent 四级分别计算。失败的文件场景不会抹掉已通过的 Basic 资格；报告不允许自行携带一个总 PASS 取代逐项核验。
- digest、显式约束、实际窗口或配置身份变化使旧证据失效；观察 TTL 到期后当前级别全部为 `STALE`，不保留旧绿色标识。仅当新的只读元数据观察恢复完全相同身份时，既有导入协议证据才可重新适用。

`qualification` 摘要包含 `status`、`protocol`、`target_version`、`run_id`、`finished_at`、`trust` 和四级 `levels`。有效导入摘要另有 `report_sha256` 和 `imported_at`。每级为 `{qualified: boolean, status: PASS|FAIL|NOT_RUN|STALE|INVALID|NOT_APPLICABLE, reasons: string[]}`。总体有效状态为 `PASS` 或 `PARTIAL`，未知/失效状态不能作为资格。`effective_capabilities.file_agent_qualified` 只取当前摘要的文件级别。

## 本轮实际验证

命令在 `siyi/` 运行，显式 `SIYI_TEST_PROVIDER=offline`、`SIYI_ALLOW_PAID_API=false`：

```text
.venv\Scripts\python.exe -m pytest -c pyproject.toml
  ../tests/backend/providers ../tests/backend/context
  ../tests/backend/evals/test_local_model_benchmark_v150.py
  ../tests/backend/evals/test_local_benchmark_budget_v160.py
  ../tests/backend/evals/test_runtime_file_qualification_v160.py
  ../tests/backend/local_runtime/test_local_models_api.py
  -q --no-cov -p no:cacheprovider
366 passed, 17 skipped, 1 warning
```

日志：`build/v1600-evidence/p01-v16-final-targeted.log`，耗时 72.93 秒。17 个 skip 为明确未选择的真实 Ollama 或付费 DeepSeek 验收；一个 warning 是现有 Starlette/httpx 兼容提醒。首轮日志保留了旧 v9 测试把理论窗口视为当前运行窗口的失败；更新后的测试要求理论元数据仍保留、运行窗口未知时不推断为已确认。CLI 续期与漂移测试使用标明用途的 fabricated reader fixture，没有把模拟 local_live 行保存为验收报告。

```text
.venv\Scripts\python.exe -B -m app.evals.local_model_benchmark.runtime_file_cli
  --mode scripted --samples 3 --timeout 60
  --output ../build/v1600-evidence/runtime-file-scripted-v16-final
status=passed, samples=30, passed_samples=30, failed_samples=0
duration_ms=45975.764
```

报告：`build/v1600-evidence/runtime-file-scripted-v16-final/RUNTIME_FILE_QUALIFICATION.json`，同目录 Markdown 与 `runtime-file-scripted-v16-final.log`。本次 `app_version=16.0.0`、`target_version=16.0.0`、`filesystem=NTFS`、`identity_stable=true`，run_id 为 `5110d14281b046f382a4fcecf76e7248`。JSON SHA-256 为 `278DAB809BE3CCF603B5C522A7E0C90C5A7E25626E252B55CB8C1AB2ED7E8DD2`。四级资格全部 false，`file_agent_qualified=false`；这是 scripted 内核及真实文件链路结果，没有加载或调用实际模型。之前版本 15 的样本日志保留为历史记录。

## 报告 shape 与总集成要求

真实 Runtime 报告 Envelope 包含：`schema_version=1`、`target_version=16.0.0`、`app_version`、`protocol=runtime-file-v1`、`mode`、`evidence_layer=runtime_filesystem`、`run_id`、`started_at`、`finished_at`、`filesystem`、`effective_capabilities`、`identity_stable`、`preflight_errors`、`case_results`、`metrics`、`status`、`qualification`、`file_agent_qualified`、`limitations`。

每个样本包含 `case_id`、稳定 `requirement_id`、`sample`、`origin`、`status`、`error_type`、`duration_ms` 及 `evidence`。证据保留 task/receipt identity、Executor、Runtime、实际权限、终态、独立磁盘哈希、工作区外哨兵、回执、操作状态、审计数量、实际成功模型请求和模型身份；省略完整路径、内容及原始异常文字。

总集成需要在源码冻结和版本同步后重新执行相关脚本，并绑定精确源码/产物身份。真实默认模型文件资格仍待显式 local_live 验收，不能用本次 30 个 scripted PASS 替代。

真实 local_live 的配置示例为 `--configuration <显式Provider配置JSON> --context-profiles <显式scoped配置JSON>`，必须由用户明确选择已安装模型及 `SIYI_TEST_PROVIDER=ollama`。CLI 不自行发现用户配置路径或下载模型。每样本续期允许总时长超过 300 秒，但真实身份变化仍失效；若单个长样本内部的后续模型请求遇到 TTL 过期，Runtime 仍按当前能力边界拒绝，不重放该样本或替换模型。
