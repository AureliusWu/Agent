# 司忆 v9.4.0 实施计划：权限、密钥与沙箱

## 基线与边界

- 基线为本地 v9.3.0 发布提交 `49d098a7095f918ce9d0de979bf57cdc8326ba2b`。
- 仅 Windows PC；不推送 GitHub，不下载新模型，不调用 DeepSeek 付费接口。
- 使用隔离数据库、临时工作区和合成密钥；不读取或提交用户真实凭据。
- `skills.zip` 仅作设计参考，不安装、不执行、不复制其私有实现。
- 用户已明确排除 24 小时耐久测试，矩阵标为 `NOT_APPLICABLE`。

## 实施内容

1. 建立 Permission Broker v2，将工具映射到规范权限：
   `filesystem.read/write/delete`、`process.execute`、`network.request`、
   `secret.read`、`clipboard.read/write`、`camera.read`、`microphone.read`、
   `connector.access`、`skill.install/modify`。
2. 支持自动、本次、当前任务、当前工作区、始终允许和拒绝；持久授权必须可列出与撤销，critical 仍只能单次确认。
3. 增加普通、开发者、管理员安全域；开发/管理 Skill 不向普通域暴露，管理员模式不能绕过 critical 确认、沙箱和网络策略。
4. Skill 使用自身 Manifest 权限集，安装到隔离区，完成 Manifest 校验和第三方脚本静态扫描后才激活。
5. 禁止 Skill 与运行时触发 `pip/npm install`，强化危险命令、命令白名单、工作区路径和域名白名单策略。
6. 增加 Secret Manager 契约：桌面 Credential Manager 保存值，后端只接受 opaque 引用；日志、审计、错误与诊断继续自动脱敏。
7. 提供权限授权/拒绝/撤销、运行域和安全策略的 API 与审计状态。

## 专项测试

- 五级授权和拒绝优先级、授权范围绑定、过期与撤销。
- critical 权限不可持久化，管理员模式不可越权。
- Skill 独立权限、普通域隐藏开发 Skill、安装隔离与脚本扫描。
- 危险命令、包管理安装、目录越界、域名白名单和 Secret 引用测试。
- 密钥、路径、请求参数、命令输出、日志与诊断脱敏。

## 发布门禁

- v9.4 专项、后端全量、真实 qwen3:4b、Core Eval、前端、Rust、隐私、封包、安装/卸载数据保留和性能全部通过。
- 任一必需门禁 FAIL/BLOCKED/NOT_RUN 时不更新到 v9.4.0。
- 最终生成机器矩阵、证据清单、数据可靠性审计、性能报告和本地发布报告，并绑定精确实现提交。
