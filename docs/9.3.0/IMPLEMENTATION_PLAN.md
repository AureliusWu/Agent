# 司忆 v9.3.0 实施计划

## 目标

把现有按需读取 `SKILL.md` 的能力收敛为 Skill Runtime 1.0：有严格 Manifest、依赖与冲突检查、启停与版本状态、触发证据、Token 预算和权限边界。

## 现状审计

- 当前启动/任务路径已经先扫描名称与描述，命中后才读取完整 `SKILL.md`，并限制 Skill 数量和字符预算。
- `skill_settings` 已支持按路径启停；`skill_runs` 已保存任务、名称、路径和内容字符数。
- Extension Runtime 已支持包版本、启用、停用、卸载和回滚，可复用其生命周期，不另建平行安装器。
- Skill 内容已作为不可信数据处理并记录数据流，但缺少严格 Manifest、依赖图、冲突判定、负触发、权限声明和 Token 统计。

## 实施

1. 定义并验证完整 Skill Manifest：
   `name/version/description/type/entrypoint/requires_tools/requires_skills/permissions/platforms/risk/license/trigger_examples/negative_trigger_examples`。
2. 保持摘要优先、命中后加载正文；正向触发加分，负向触发直接排除。
3. 对依赖做拓扑解析；缺失依赖、循环依赖、同名多版本冲突均 fail closed。
4. 在加载前验证工具与权限声明；Skill 只能请求 Agent Core 已登记能力，不能直接获得文件、网络或进程句柄。
5. 触发记录增加版本、来源、字符数、估算 Token、匹配原因和依赖链；失败不写入主会话消息。
6. 内置 release-checklist、data-reliability-audit、performance-regression-check、deploy-smoke-test、daily-report 和 ui-design。
7. `ui-design` 合并设计能力，支持 `existing-product`、`greenfield-product`、`strict-design-system` 三种模式。
8. 内置 Skill 只随应用版本更新；第三方 Skill 不自动安装或运行。

## 验收

- 六个内置 Skill Manifest 全字段通过；
- 正向/负向触发、摘要懒加载、Token 上限；
- 依赖排序、缺依赖、循环、同名版本冲突；
- 启用、停用、Extension 卸载/回滚兼容；
- 未声明工具、越权权限和直接执行意图拒绝；
- 触发失败不污染 messages；
- 专项、全量、前端、Rust、Eval、隐私、性能、桌面安装包全部通过后才更新 v9.3.0。

## 边界

- `skills.zip` 只作能力与字段参考，不直接安装、不执行其中脚本；
- 不复制其中专有文档实现；
- 不调用付费 API，不推送 GitHub，不执行 24 小时耐久。
