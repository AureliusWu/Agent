# 司忆 v9.0.1 本地发布报告

## 结论

v9.0.1 已完成实现、真实 Ollama 验收、完整回归、桌面封包和隔离安装烟测，发布状态为 `READY / LOCAL_ONLY`。实现代码绑定提交 `5516eab3eb9fd435889f710a979f1c9907ff33c0`；最终封包同时嵌入本目录的就绪状态与证据清单。

## 实际验证

| 门禁 | 状态 | 证据 |
|---|---|---|
| 后端完整回归 | PASS | `461 passed, 7 skipped` |
| 覆盖率 | PASS | `82.67%`，高于 70% 门槛 |
| 真实 qwen3:4b | PASS | `5 passed in 284.50s`，实际调用 31 次 |
| Ollama 错误场景 | PASS | 缺模型、停服务、端口冲突均真实触发并正确分类，随后恢复服务 |
| 桌面可见验收 | PASS | v9.0.1、Ollama v0.32.5、qwen3:4b 2.5 GB、重试 2、首次加载提示均可见 |
| Core Eval | PASS | `18/18`，run id `b90cc6ce4f0b45adbcd2f1443605fdb7` |
| Rust | PASS | `5/5` |
| 前端 | PASS | lint、TypeScript、Vite desktop build |
| 隐私 | PASS | tracked + history 均无禁入私密数据 |
| 性能 | PASS | 冷启动中位数 1692 ms，门槛 1903 ms |
| 安装与迁移 | PASS | NSIS、MSI、Schema 32→33 备份、卸载保留、重装识别 |
| DeepSeek 付费 | NOT_RUN | 用户要求节省 Token，付费调用为 0 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 已知边界

`qwen3:4b` 在人为要求严格短标记、同时触发较长内部推理的提示上可能耗尽输出预算；这不是协议解析或私有推理泄漏。日常中文问答、连续对话、流式输出和工具调用均以自然任务真实验证通过。应用不会自动下载模型或隐式切换到云端。

## 数据与回滚

Schema 33 的 `first_token_ms` 可空：未观测到首 Token 时保持 `null`，不会伪造为 0。迁移前自动建立数据库备份，安装烟测已证明原数据、卸载后数据和重装识别均保持。Provider 配置可通过删除测试数据目录下的 `state/provider-settings.json` 回到默认 DeepSeek；此操作不会卸载用户自行安装的 Ollama 或模型。

## 分发

本次仅更新本地源码、可执行文件和安装包；没有提交到 GitHub，也没有远程分发。
