# v9.3.0 性能回归报告

结论：`PASS`。

Skill 启动发现只读取不超过 64 KiB 的 Manifest 前缀，完整正文仅在触发后加载；单 Skill 最多注入 12,000 字符，总量受 `max_skill_context_chars` 限制。

预提交封包冷启动样本为 `1642 / 1647 / 1653 ms`，中位数 `1647 ms`，低于 `1903 ms` 门槛。正式发布提交后的 clean 封包三样本写入 `build/v930-evidence/performance-gate-final.json`，并以该文件作为最终证据。
