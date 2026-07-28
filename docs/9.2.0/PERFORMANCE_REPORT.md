# v9.2.0 性能回归报告

结论：`PASS`。

最终 clean 封包运行三次独立 sidecar 冷启动；门槛保持 `1903 ms`。版本、Build ID、组件 Build ID、Schema 33、loopback 与 Release 状态必须一致。确切样本保存在 `build/v920-evidence/performance-gate-final.json`，以避免生成证据改变被测构建指纹。

批量文件操作硬限制为 50 项，目录删除默认最多检查 1000 项、调用方最多可提高到 5000 项；读取限制 2 MB，单次文本写入限制 5 MB，防止一次工具调用造成无界内存或备份压力。
