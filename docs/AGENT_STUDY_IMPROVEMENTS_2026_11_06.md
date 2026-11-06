# Agent 多轮学习改进记录

日期：2026 年 11 月 6 日。基线提交 `585d3b94ae424aa562ca3073effc178073a12b83`，版本 `17.0.1`。这是本地源码改进，不是安装包、发布或真实模型验收。

## 学习如何变成改动

1. **核对参考机制。** 从本地固定快照确认 DSH 的输出快照/校验、MemoryConstellations 的逐条预算、CC Switch 的同目录独占暂存。对照现有司忆实现，保留队列、Executor、权限、回执与数据库结构，不引入参考项目运行依赖。
2. **故障驱动实现。** 检验首条超预算、配置写入各阶段失败、工具产生副作用后返回畸形数据。工具输出失败不能直接等同于工具未执行：不向模型继续发送此失败以尝试新调用，已开始的副作用记为 `uncertain`。
3. **交叉审查后补强。** 自定义 Schema 不能取消基础结果契约；命令声称成功却携带非零退出码时拒绝成功。并行读取的契约错误透传，已启动同批任务取消并收拢。配置并发测试按 Windows 真实替换语义验收：冲突可显式失败，但旧文件或已提交文件必须完整、暂存不互相覆盖。

## 当前实现与边界

| 方向 | 这轮改动 | 未扩大范围 |
| --- | --- | --- |
| 工具结果 | `tools/output_contract.py` 给 `read_file`、`read_file_range`、`create_file`、`write_file`、`run_command` 启用主 Executor 输出契约。声明在 Hook 前快照，输出是有限的 JSON 对象，生成宿主回执时覆盖工具自带的 receipt。拒绝基础字段错误、非 JSON 值、成功与退出码矛盾。 | 并非全工具、MCP、扩展和子 Agent 的完整数据 Schema；工具有独立 2 MiB 上限，Provider 原 1 MiB 约束不变。 |
| 未知副作用 | `runtime/runner.py` 专用契约中断分支区分派发前声明错误、派发后输出错误；对后者不伪造成功回执或自动继续下一轮。默认恢复沿既有账本对账，不盲目重放。 | 不改变权限策略，不增加通用远程事务或外部操作自动撤销。 |
| 记忆预算 | `memory/service.py` 按最终上下文计数，包含信任边界标题、来源和分隔符。首条没有豁免，超长记录整条跳过，不截断约束；未注入记录不增加使用次数。 | 不改命名空间或数据库；来源修订/hash 尚未接入。非正字符预算返回空，保留条目数量的旧 clamp。 |
| 配置保存 | `providers/configuration.py` 先验证，独占创建同目录暂存文件，写入、flush/fsync、关闭句柄后替换。失败仅清理本次暂存，不扫旧文件。 | 不迁移密钥，不承诺配置与 Windows Credential Manager 的跨存储原子事务，也不宣称断电后目录项一定持久。 |

## 验证方式

在隔离的 `AGENT_DATA_ROOT`、测试专用数据库和不存在的 `AGENT_ENV_FILE` 下运行；云凭据留空。pytest 选择 kernel、plugins、memory、context、providers、工具注册及任务/恢复相关测试，明确排除 `paid_model` 和 `local_model`。未开启全仓覆盖率门禁。Scripted Eval 使用既有任务合同、模拟模型、隔离工作区，三套均使用 `--require-passed`。

新增回归重点：`test_tool_output_contracts.py`、`test_memory_budget.py`、`test_provider_configuration_persistence.py`。其中包含已写入后输出不合规的故障注入，核对持久 `uncertain`、无成功回执、显式恢复不重复执行，以及异常后不遗留在途并行读取。

最终合并回归：**1096 通过，16 项真实模型用例未选中，1 条既有 Starlette/httpx 弃用警告**，耗时 226.12 秒。新增工具契约用例 35 项包含在内。JUnit 位于 `build/study-verify-79a61411097e4770959b884fcc3de9aa/junit.xml`。早期运行结果保留，不混作最终验证。

脚本评测结果：核心 18/18，多 Agent 10/10，专业 Agent 8/8。`evals/tasks.json` 合同校验通过。生成报告保留在忽略目录 `data/evals/study-20261106-a525c8b556734b7b8911446ca9856f52/`，不提交运行证据或测试数据库。

没有安装、推送、版本变更、真实模型调用、现用数据/密钥读取。下轮如需继续，优先选择记忆来源修订或配置＋凭据跨存储失败恢复之一，先定义故障合同再动实现。
