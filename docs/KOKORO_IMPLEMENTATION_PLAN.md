# Kokoro v2.0.1 Implementation Plan

## Stage 0 - Baseline

记录 Git、架构、需求矩阵、迁移策略和测试基线。保留现有未提交工作，不覆盖用户交接文档。

## Stage 1 - Model Window And Context Budget

建立模型能力注册和安全回退；按完整序列化请求估算 Token；保留输出空间；在阈值前自动执行分层、可追踪压缩，失败时确定性裁剪。保持运行时轮数、工具次数、超时和全局安全上限。

## Stage 2 - Identity Kernel

增加固定 Agent ID、版本化身份存储、只读默认身份、管理员确认更新和回滚。每次模型调用强制注入身份。Identity Guard 只识别错误自我声明，最多修复一次，不改写技术讨论、代码或引用。

## Stage 3-4 - Long-Term Memory

通过迁移引入统一长期记忆和候选模型，复用现有个人/项目记忆接口。实现来源、类型、置信度、重要性、锁定、确认、敏感、软删除、有效期、替代历史、冲突和混合检索。模型只能提交候选。

## Stage 5 - Emotion And Relationship

持久化 Trait、Mood、Emotion 和关系状态。后端校验事件、限幅并按时间衰减；状态只能影响表达建议，不能改变事实、安全和权限。

## Stage 6-7 - Context And Continuity

统一组装平台、身份、状态、长期记忆、响应规则、会话和任务层；记录本轮记忆引用。提供脱敏调试摘要。增加手动巩固、确定性去重归档、删除墓碑以及有来源的连续性摘要。

## Stage 8 - Data And Desktop UI

实现带 manifest/checksum 的完整本地备份包、导入前安全备份、哈希/版本校验和失败回滚。补齐记忆管理、状态、身份和备份桌面入口，保持现有 UI 架构，不恢复网页端开发。

## Stage 9 - Verification And Version

依次运行全量后端、前端、Cargo、Tauri、sidecar、迁移、备份、安装升级和实际桌面冒烟；审查 diff 和敏感信息。全部通过后才统一更新 `VERSION`、npm、Cargo、Tauri 和文档到 `2.0.1`。

每阶段结束都执行 `git status --short`、`git diff --stat` 和对应测试。本任务不推送、不发布。
