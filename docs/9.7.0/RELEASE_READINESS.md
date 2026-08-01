# 司忆 v9.7.0 本地发布就绪报告

## 结论

- 评估状态：`BLOCKED / NOT_RELEASED`
- 当前版本：`9.6.0`
- 目标版本：`9.7.0`
- 分发状态：仅本地修改，未推送 GitHub
- 24 小时耐久：按用户要求 `NOT_APPLICABLE`

统一 Artifact Engine 的实现、真实文件验证和冻结包冒烟已经完成，但当前单基础 Agent 架构与仓库保留的 Professional/Multi-Agent 发布矩阵互相冲突。发布规则禁止在必需矩阵存在 FAIL/BLOCKED 时更新版本源，因此本轮不修改 `VERSION`。

## 已完成并有实际运行证据

- Artifact Engine 专项：Markdown、DOCX、PDF、PPTX 的创建、编辑、合并、提取、渲染、验证、安全边界与二进制下载。
- Python 3.12.13：发布环境已真实重建，原 Python 3.14 环境保存在本机 AgentBuildCache 中。
- 后端全量：`587 passed / 8 skipped`，覆盖率 `82.85%`。
- Core Eval：`18/18 PASS`，无 false success、权限越界或沙箱越界。
- Ollama `qwen3:4b`：`5/5 PASS`，未调用 DeepSeek。
- 前端：安全契约、桌面生产构建、lint 均通过。
- Rust：fmt、clippy `-D warnings`、测试 `6/6 PASS`。
- 冻结 sidecar：PyInstaller 内容清单通过；打包 EXE 真实完成 DOCX/PPTX 重开、PDF 提取与 PDF→PNG 渲染。
- 独立查看器：官方 LibreOffice `26.2.5.2`，MSI SHA-256 `f15ba07bfcb0186986cf3171063506f5d207c11f8cc051ba0d135209e9e915f9`；隔离配置真实打开 DOCX/PPTX 并分别导出 PDF，DOCX 1 页、PPTX 2 页。
- 隐私与凭据：源码、历史、工作树、负向样本和 Windows Credential Manager 门禁通过。
- SBOM 与许可证：运行时/构建期 Python 依赖分域，生成第三方许可证清单并包含 PDFium BUILD_LICENSES。

本机证据位于忽略目录 `build/v970-evidence/`。这些文件与安装包不进入 Git。

## 当前阻塞

### 1. Professional Eval 与当前产品架构冲突

产品当前只暴露 `general` 基础 Agent，旧 ID `coding`、`data`、`documents`、`file_organizer` 会按既定兼容规则映射到 `general`。因此 Professional Eval 的四项规则仍要求旧专业 Profile 出现在任务 Trace 时，实际结果为 `0/4`，且被正确标记为 false success。

不能通过篡改 Trace、把 `general` 伪装成旧 Profile 或降低规则来获得 PASS。需要产品决策：

1. 保留单基础 Agent 架构，并正式退役/替换旧 Professional Eval；或
2. 恢复可执行的专业 Agent Profile，再运行原矩阵。

### 2. Multi-Agent Eval 与当前禁用状态冲突

当前诊断明确返回 `multi_agent_enabled=false`，旧 Multi-Agent Eval 仍要求 planner/verifier/explorer 子 Agent，实际为 `0/3`。v9.7.0 实施计划未新增多 Agent 能力，不应为通过旧矩阵而偷偷重新启用。

## 尚未执行的最终发布步骤

以下步骤只在上述架构决策完成且相应矩阵通过后执行：

- 统一更新全部版本源到 `9.7.0`；
- 从干净提交生成最终 Build ID；
- 构建 v9.7.0 NSIS/MSI；
- 使用 v9.6.0 安装包执行升级、卸载保留数据与重装识别；
- 与 v9.6.0 clean sidecar 做三组同机配对性能门禁；
- 执行 NSIS 总大小不超过 55 MiB、相对增长不超过 25 MiB 的门禁；
- 生成最终 SHA-256、SBOM、许可证和本地发布报告。

在这些步骤完成前，不得声称 v9.7.0 已发布。
