# v9.5.0 性能回归报告

结论：`PASS`。

源码路径未引入无界队列扫描；运行状态查询与 checkpoint 查询均有界。

预提交封包冷启动样本为 `1684 / 1654 / 1667 ms`，中位数 `1667 ms`，低于 `1903 ms` 门槛。正式发布提交后的 clean 封包三样本写入 `build/v950-evidence/performance-gate-final.json`。
