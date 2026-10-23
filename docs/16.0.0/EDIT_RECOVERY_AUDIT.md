# 编辑恢复实现审计

## 范围与结论边界

- 本轮修复本机编辑前置失败的恢复判定，不是任意工具失败自动清除机制。
- 本文记录实现与待核验证项，不代表完整全量、覆盖率或正式发布验收通过。
- 不改变版本号、原任务定义、30 秒分段限制、模型调用或付费验证协议。
- 不读取凭据，不调用真实模型，不执行安装、管理员或界面操作。

## Typed 错误合同

- `siyi/app/sandbox.py` 新增 `EditMatchPreconditionError`。
- 仅内置 `replace_text` 的匹配次数不符产生 `edit_match_precondition`。
- 产生位置已完成权限、工作区路径、文件类型和版本检查，尚未创建备份或写入。
- 错误保留原失败状态及 `retryable=False`，不增加自动重试。
- `data.version_before` 保存执行器当时核验的真实文件版本。
- 嵌套 `edit_precondition` 固定包含 schema_version、kind、规范相对路径、预期与实际次数。
- kind 为 `replace_text_match_count`，side_effect 为 `not_started`。
- 不返回顶层 path、change_id 或 version_after，失败收据的 changed_files 保持为空。
- 权限、网络、沙箱、版本冲突、IO 和未知错误不得通过消息文本匹配升级为该类型。

## 恢复证据链

- 入口为 `runtime/verification.py`；具体证明在 `runtime/edit_recovery.py`。
- 只为本任务的新 typed 失败建立恢复关联，不迁移或改写历史 generic tool_error。
- 工具运行行必须属于当前任务、具有有效运行编号与 execution_id，且来源严格为 builtin。
- 不信任工具结果内自称的 source、plugin 标签或外部工具伪造的 typed code。
- v2 收据的 task、tool、tool_call、状态、参数表示及执行时间必须与运行行一致。
- 另核对 `tool_receipts` 持久化收据；这验证同一执行链的持久化一致性，不是两个独立密码学来源。
- MCP、extension、未知来源、伪造或缺失收据均不能取得恢复资格。
- 原失败必须证明无已记录副作用，并且目标属于任务声明的文件范围。
- 后续仅考虑同目标、时间顺序正确、非 dry-run、非截断的成功 write_file 或 replace_text。
- 后续写入必须具有批准状态、真实 change_id、前后版本及绑定任务／调用的备份 manifest。
- manifest 前态、typed 前态和成功写入前态必须形成一致版本链。
- manifest 后态、收据后态、文件验证结果及当前实际文件哈希必须一致。
- 后续相交写入、撤销、外部工具或不明确操作使恢复保持未解决。

## 脱敏表示与可信前态

- 不修改全局 `sanitize_details`，不还原或记录被脱敏的参数内容。
- 持久化输入及收据的参数表示仍按既有脱敏规则核对。
- token 命名参数的固定脱敏表示不能独立证明文件版本。
- 只有完整 host／任务／收据证明成立，且真实 data、receipt、manifest 版本一致时才使用该链。
- 不从错误消息、模型叙述、任意占位符或“后面有成功”推断恢复。

## 后续验证命令的关联

- 必须有写入之后真实成功的内置 run_command，退出码明确为整数零。
- 命令需与项目登记的验证命令或受限 Python 检查结构及对应专业验证域关联。
- 明确指定但不覆盖目标的 affected_paths 不得充当目标验证。
- Python 检查使用有界 AST 分析识别目标读取／导入及断言关联，不执行 AST。
- AST 只能证明可观察关联，不是任意代码正确性、完整语义或无恶意行为的数学证明。
- 动态或歧义结构不能靠关键词命中放行；无关成功命令不能清除失败。
- 已记录的 checker／依赖脚本后续修改会使相关证明失效。
- 未记录的外部 checker 改动无法由当前收据协议重建；不得声称已有历史内容快照。

## 原失败与任务结论

- 保留原失败运行行、错误指纹及审计记录，只新增逐失败恢复关联。
- 不改变其他验收条件，不由恢复模块直接写任务完成或重写独立 Verifier 结论。
- 缺失、过期、冲突或不完整的证据一律保留 unresolved。
- 预览冻结源码 345 的程序不因本轮源码编辑自动更新。
- 345 的 F3、付费或其他历史证据不能改名复用于新源码冻结与验收。

## 备份路径与只读发现保护

- `safe_path` 保留原 raw、UNC、设备路径、盘符相对路径及越界检查。
- 新增规范路径首段 casefold 检查，关闭绝对路径及内部 `..` 直达 `.agent-backups` 的绕过。
- 隔离临时文件测试曾实际复现：混合大小写备份目录可被根搜索和列表发现，修复前 36 项失败、16 项通过。
- 搜索的目录剪枝、文件过滤，以及目录列表的备份排除现统一按 casefold 比较；保留原 ignored 集合，不扩张目录禁用范围。
- 新增 52 项真实临时文件回归，覆盖根／子目录搜索、内容与名称隐藏、原 ignored 目录和相似非保留名称。
- 内部历史读取及撤销仍走既有受控恢复接口；这些检查不等于句柄级 TOCTOU 或任意外部程序访问隔离。

## 验证索引与待填结果

| 项目 | 位置 | 最终结果 |
| --- | --- | --- |
| Typed、来源与直接路径回归 | `tests/backend/tools/test_edit_precondition_v160.py` | 已纳入 Root 定向回归；分项数未单列 |
| 完整恢复证据及拒绝反例 | `tests/backend/runtime/test_cross_tool_recovery.py` | 已纳入 Root 定向回归；分项数未单列 |
| 备份搜索／列表隔离 | `tests/backend/tools/test_backup_search_guard_v160.py` | 已纳入以下最终十文件回归 |
| 十文件定向汇总 | 本轮实际报告 | 300 passed、3 skipped（Windows 符号链接权限）、1 Starlette 弃用警告；35.08 秒 |
| 发布元数据与隐私扫描 | 本轮实际报告 | 15 源版本一致；tracked 与新模块隐私扫描通过；非正式发布结论 |
| 前一阶段四组单侧功能 Eval | 原任务集及 `--capture-source` | 18＋10＋8＋4 项满足原规则；运行前后 dirty 指纹均为 `95B6C82BC4C665AC0C1A9A09D49C93923874C8451B9CF82A82A442ED921FC1A7` |
| 本轮新冻结的四组配对 Eval | 原任务集、原基线与原比较门槛 | 待独立采集，不把前一阶段结果复用为新源码／性能／正式门禁 |

前一阶段的 40 项不等于 40 个任务均以 completed 结束：原安全规则允许预期阻断或部分完成；虚假完成反例随后发生脚本耗尽错误，仍满足原“不声称完成”规则。报告中的脚本模型调用及自动批准不是真实付费请求或人工验收。Sidecar 父进程退出也不替代正式 Native Job 全进程树清理证据。后续三处备份发现过滤修复发生在该次单侧评测之后，必须以新冻结源码重新采集对应门禁。

- `test_sandbox.py`：`test_safe_path_rejects_workspace_escape`、`test_unc_and_drive_relative_paths_are_rejected`。
- `test_sandbox.py`：`test_write_rejects_stale_version_token_without_overwriting`、`test_replace_text_preserves_crlf_and_returns_diff`。
- `test_sandbox.py`：`test_apply_patch_rejects_stale_content_and_applies_exact_hunk`、`test_failed_atomic_replace_keeps_original_file`。
- `test_file_recovery_v160.py`：`test_verified_terminal_state_includes_successful_undo_and_detects_later_drift`。
- symlink／junction 能力测试应单独记录实际执行或跳过；本静态说明不能替代其运行证据。
