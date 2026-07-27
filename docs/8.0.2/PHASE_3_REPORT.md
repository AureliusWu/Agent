# 司忆 v8.0.2 Phase 3 缓存正确性报告

记录时间：2026-07-27T10:27:57Z

## 当前结论

缓存正确性子阶段已完成：TTL 只决定是否尝试复用，真实 `SourceVersion` 决定是否允许复用。外部文件变化、Git checkout、命令生成输出、目录变化、LSP 工作区变化与自身副作用都会使旧缓存失效。

Phase 3 整体仍未完成：50 轮真实任务 A/B、40% 未缓存输入降幅和 Provider usage 逐调用对账尚未执行，因此 EFF-001~008 不判定为整体 PASS。

## 实现边界

- 文件版本绑定路径、size、mtime_ns 和完整 SHA-256。
- 目录版本对范围内文件树以可重现顺序生成指纹。
- Git 读取在工作区指纹外绑定 HEAD。
- 自身成功副作用递增 `workspace_generation`，旧 generation 不可复用。
- 读取执行前后版本不一致时，结果不进入缓存。
- 上下文压缩保留版本账本，复用前仍重新验证真实源。

## 实际运行证据

精确源码提交：`b9e03d4ac4032f7fa61786e7328b1b156bb4bb7c`

开发验证 Build ID：`62dedddf300a6fb76d0be16a`（工作树 `CLEAN`，产品版本仍为 `8.0.1`）

聚焦命令：

```powershell
$env:PYTHONPATH = (Resolve-Path 'siyi').Path
pytest tests/backend/infrastructure/test_efficiency.py tests/backend/runtime/test_task_runner.py -q
```

结果：`42 passed`。

本地全量命令：

```powershell
.\scripts\test.ps1
```

结果：后端 `396 passed, 1 skipped`，覆盖率 `82.24%`；前端 lint、TypeScript/Vite 构建、安全与推理边界测试均通过。

GitHub CI：Run `30257852659`，绑定上述精确提交，结论 `success`，耗时 14m45s。CI 实际执行后端、评测契约、紧凑对抗门禁、前端、Windows 桌面壳与 SBOM。

## 用例状态

`V802-CACHE-001~006` 已绑定上述测试名、提交和 CI Run，可判定 `PASS`。详细映射见 `TEST_MATRIX.json`。

## 未完成

- 50 轮同模型、同任务、同工具集真实 A/B。
- 未缓存输入相对 v8.0.1 基线降低至少 40% 的证明。
- Provider usage 与本地账本逐调用一致性证明。
