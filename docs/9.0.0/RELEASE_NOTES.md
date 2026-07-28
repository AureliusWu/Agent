# 司忆 v9.0.0 本地版本说明

## 主要更新

- 新增统一 `LLMProvider` 与 Provider Registry，保留 DeepSeek 兼容行为。
- 新增 17 类确定性 Mock Provider 场景和 A 层无网络回归。
- 新增仅允许本机 `qwen3:4b` 的 Ollama Provider、安全边界和 B 层真实测试。
- 新增 DeepSeek 付费测试双开关硬门禁，默认测试不会消耗 token。
- 新增六类统一失败归因、Provider 健康检查和能力声明。
- 桌面设置支持 DeepSeek、Ollama、Mock 切换，以及超时、流式和工具调用配置。
- 新增 Ollama 检测/安装/拉取与 A/B/C 三层测试脚本。

## 验证状态

Mock、全量后端、Core Eval、Adversarial Eval、前端、Rust 和隐私门禁均已通过。
真实 Ollama 测试因当前环境无法完成 1.56 GB 官方安装包下载而标记 BLOCKED；
DeepSeek 付费验收按用户节省 token 的要求标记 NOT_RUN。两者都没有伪造 PASS。

本版本仅在本地更新和构建，不推送 GitHub。

