# 7.0.0 AppData 迁移报告

状态：实现和自动化测试通过。

- 生产根：系统解析的 `%LOCALAPPDATA%/AureliusWu/Agent`。
- 开发根：系统解析的 `%LOCALAPPDATA%/AureliusWu/Agent-Dev`。
- 可由测试专用 `AGENT_DATA_ROOT` 覆盖，避免接触真实运行数据。
- 数据库、日志、备份、素材、Artifact、缓存、崩溃、状态、临时文件和 Kokoro 私有产物均位于运行根。
- 旧源码数据库迁移采用先备份、SHA-256 和 SQLite 完整性校验，再原子落位。

实现提交：`61142a1 feat: isolate production and development runtime data`。

自动化覆盖运行根隔离、旧数据库迁移、备份位置与失败恢复。未在本次任务读取任何真实 AppData 文件内容。
