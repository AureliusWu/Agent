# v9.1.0 性能回归报告

结论：`PASS`。

最终清洁封包使用三次独立 sidecar 冷启动，门槛为 v9.0.0 基线 `1655 ms` 的 115%，即 `1903 ms`。三次启动必须同时满足版本、Build ID、组件 Build ID、Schema 33、loopback 和 Release 构建一致。确切样本和中位数保存在忽略提交的 `build/v910-evidence/performance-gate-final.json`，避免性能证据反向改变被测构建指纹。

前端 lint、安全契约、TypeScript 与 Vite desktop build 均通过；未发现统一能力展示造成的显著构建体积或启动时间回归。
