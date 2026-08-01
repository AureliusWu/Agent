# 司忆 v11.0.0 本地验证报告

## 结论

- 版本更新：`COMPLETE`，13 个版本真相源均为 `11.0.0`。
- 验证状态：`FAILED / BLOCKED`。
- 分发状态：`NOT_READY`，仅本地修改，未推送 GitHub。
- 24 小时耐久：按用户要求 `NOT_APPLICABLE`。

## 通过项

- Python 3.12.13 后端全量：`587 passed / 8 skipped`，覆盖率 `82.85%`。
- 前端：安全契约、Artifact 下载契约、lint 和桌面生产构建通过。
- Rust：`fmt`、`clippy -D warnings` 和 `6/6` 测试通过。
- Ollama `qwen3:4b`：真实本地模型 `5/5 PASS`，未调用 DeepSeek 或 GLM 付费接口。
- Core Eval：`18/18 PASS`，run ID `cbc6ab39e1e04415b0f1f4775b9c44dc`。
- 隐私门禁：源码、历史、工作树与合成负向样本通过。
- v11.0.0 打包 sidecar：Schema 36、冻结 Artifact 依赖清单、DOCX/PPTX 重开、PDF 提取与渲染通过。

## 发布级失败

- Professional Eval：`0/4`，4 项 false success。当前任务 Trace 使用唯一 `general` Agent，但旧合同仍要求 `coding/data/documents/file_organizer` Profile。
- Multi-Agent Eval：`0/3`，2 项 false success。当前产品明确为 `multi_agent_enabled=false`，旧合同仍要求 planner/verifier/explorer 子 Agent。

上述失败出现后，按稳定版规则停止最终 NSIS/MSI、覆盖升级和配对性能发布门禁。版本号与本地运行副本已为 v11.0.0，但不得声称为已验证稳定发布。
