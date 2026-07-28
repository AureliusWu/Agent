# v9.4.0 性能回归报告

结论：`PASS`。

Permission Broker 使用带索引的精确策略查询，deny/allow 决策不扫描文件；命令与 Skill 静态策略为有界正则集合。

预提交封包冷启动样本为 `1654 / 1661 / 1648 ms`，中位数 `1654 ms`，低于 `1903 ms` 门槛。正式发布提交后的 clean 封包三样本写入 `build/v940-evidence/performance-gate-final.json`。
