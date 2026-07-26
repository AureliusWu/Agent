# v8.0.1 阶段 1 报告

状态：`IN PROGRESS`

## 已完成

- 上下文固定前缀改为稳定顺序：安全边界、固定身份、Agent 配置。
- 时间、情绪、记忆、检查点和任务增量位于稳定前缀之后。
- 每次组装记录固定前缀 SHA-256 和估算 Token。
- TokenBudget 支持 DeepSeek 的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。
- TokenBudget 支持 OpenAI 风格 `prompt_tokens_details.cached_tokens`。
- 按阶段记录缓存输入、未缓存输入、缓存写入、输出和总 Token。
- SQLite Schema 升级到 v29，`model_runs` 持久化三类缓存 Token。
- 任务聚合与检查点保存三类缓存 Token，恢复后账本不会归零。
- 每次模型调用保存当时的输入/输出价格快照。
- `/api/usage/summary` 聚合缓存 Token 和总缓存命中率。
- 桌面用量页显示缓存命中率、命中 Token 和未缓存 Token。
- 编译状态升级到 v3，显式保存 FACT、INFERENCE、CONSTRAINT、DECISION、OPEN_QUESTION、NEXT_ACTION 与 EVIDENCE_REF。
- 相同来源和内容不得在压缩时从 INFERENCE 提升为 FACT。
- 同一上下文分段内重复读取未变化文件时只注入 SHA-256 引用；上下文压缩后清空引用缓存。

## 实际验证

```text
python -m pytest tests/backend/context/test_context_assembler.py tests/backend/infrastructure/test_efficiency.py -q
→ 15 passed

python -m pytest tests/backend/providers/test_provider.py \
  tests/backend/api/test_api.py::test_recent_tasks_reports_model_cost_by_phase \
  tests/backend/api/test_api.py::test_usage_summary_reports_provider_cache_tokens \
  tests/backend/integration/test_database.py -q
→ 22 passed

npm run build
→ TypeScript 和 Vite 生产构建通过

python scripts/run-v8-backend-gate.py --output build/v8.0.1-evidence/phase-1-backend-final.json
→ 387 passed、1 skipped、0 failed
```

## 本阶段修改文件

- `siyi/app/context/assembler.py`
- `siyi/app/context/compiler.py`
- `siyi/app/efficiency.py`
- `siyi/app/database.py`
- `siyi/app/api/routes/system.py`
- `siyi/app/providers/provider.py`
- `siyi/app/runtime/runner.py`
- `siyi/app/kernel/adapters.py`
- `desktop/frontend/src/components/providers/UsagePanel.tsx`
- `desktop/frontend/src/types.ts`
- `tests/backend/context/test_context_assembler.py`
- `tests/backend/context/test_context_compiler.py`
- `tests/backend/infrastructure/test_efficiency.py`
- `tests/backend/providers/test_provider.py`
- `tests/backend/api/test_api.py`
- `tests/backend/runtime/test_task_runner.py`
- `docs/CURRENT_ARCHITECTURE.md`

## 剩余差距

- 尚未建立文件内容版本指纹与“未变化文件不重复完整注入”的任务级证据。
- 工具长输出已有压缩，重复读取已有内容哈希引用；跨任务 Artifact 去重尚未形成统一账本。
- 当前只证明账本字段与 Provider 返回一致，尚未运行 50 轮真实任务，因此 EFF-007、EFF-008 不能判定 PASS。
- 上下文关键类型 FACT / INFERENCE 等还需要在编译器中显式编码并验证压缩前后类型不变。
