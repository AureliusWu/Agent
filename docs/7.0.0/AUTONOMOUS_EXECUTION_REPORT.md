# 7.0.0 持续自治执行报告

状态：核心正确性与规定规模合成压力已通过；长时间耐久门禁待执行。

## 已验证

- Durable Runtime P0：任务租约、Heartbeat、Provider 等待、Managed Process、STOP 传播、ToolReceipt v2、版本令牌与恢复链路。
- Context Compiler v2：结构化任务状态、Decision Ledger、稳定证据引用与多次重编译。
- 规定规模合成压力：1,000 个 ExecutionSegment、10,000 条模型循环记录、50,000 条工具记录、100 个检查点。
- 压力结果：序号 1—1,000 连续；运行中 Segment 为 0；工具 execution_id 50,000 个且全部唯一；Provider 等待后恢复为 RUNNING；检查点可恢复最后工作记忆。

## 命令与结果

```text
siyi/.venv/Scripts/python.exe -m pytest ../tests/backend/test_autonomous_runtime_stress.py -q --no-cov
1 passed in 57.86s
```

该测试使用临时 SQLite 和合成数据，不读取真实对话、记忆、工作区或用户运行目录。

## 尚未满足

- 30 分钟、2 小时、8—12 小时、24 小时和 72 小时耐久档尚未在当前提交执行。
- 在上述发布候选门禁完成前，不得把本报告解释为“无限运行证明”。
