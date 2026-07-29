# v9.6.0 性能回归报告

当前结论：`PASS`。

第一次候选包冷启动为 `9063 / 3309 / 2733 ms`，中位数 `3309 ms`，超过 `1903 ms` 门槛。根因是 Pillow 和图片格式插件在所有 Sidecar 启动时由视觉 API 与 Runtime Tool 顶层导入。

改为只在真实视觉请求/工具执行时延迟加载图像栈后，预发布重新封包的冷启动为 `1638 / 1650 / 1649 ms`，中位数 `1649 ms`，通过门槛。发布提交后的 clean-tree Build ID 与最终三次样本以 `build/generated/build-info.json` 和 `build/v960-evidence/performance-gate-final.json` 为准。

视觉预处理对单图字节、总字节、单图像素、总像素、图片数量和切片数量均有硬上限，不存在无界图片展开。
