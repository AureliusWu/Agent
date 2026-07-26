# v8.0.1 阶段 0 报告

状态：`PARTIAL`

## 已完成

- 冻结 v8.0.0 脏工作树基线，记录提交、Diff 哈希、Status 哈希、Build ID 与矩阵统计。
- 建立 18 个真实场景目录与需求映射。
- 建立统一 `scenario.json` Evidence Schema。
- 建立场景定义和实际运行目录验证器。
- PASS 必须具有成功命令、时区时间、Build ID、源码指纹和存在的证据文件。
- BLOCKED 必须具有明确阻塞原因。
- 建立隔离桌面测试环境，默认移除模型和搜索凭据；真实模型模式必须显式开启。
- Windows Computer Use 已真实发现并操作隔离启动的司忆窗口。
- 自动化读取到 v8.0.0、核心在线、夏目心头像和 Build 信息，进入搜索页后正常关闭。
- 关闭后主程序与隔离 Sidecar 数量均为 0。

## 实际验证

```text
python scripts/validate-real-scenarios.py
→ passed

python -m pytest tests/backend/evals/test_real_scenario_evidence.py tests/backend/evals/test_scenario_environment.py -q
→ 5 passed

Windows Computer Use:
启动隔离候选 → 捕获 UI 树 → 点击“搜索” → 捕获搜索页 → 关闭
→ passed
```

桌面控制证据：`v8.0.1-release-evidence/phase-0/desktop-control-smoke.json`

## 本阶段修改文件

- `.gitignore`
- `docs/8.0.1/BASELINE.json`
- `docs/8.0.1/GAP_ANALYSIS.md`
- `docs/8.0.1/IMPLEMENTATION_PLAN.md`
- `docs/8.0.1/PHASE_0_REPORT.md`
- `real-scenarios/README.md`
- `real-scenarios/catalog.json`
- `real-scenarios/evidence.schema.json`
- `siyi/app/evals/real_scenarios.py`
- `siyi/app/evals/scenario_environment.py`
- `scripts/validate-real-scenarios.py`
- `tests/backend/evals/test_real_scenario_evidence.py`
- `tests/backend/evals/test_scenario_environment.py`

## 关键 Diff 摘要

- 新增严格的真实场景证据契约，阻止“目录存在即 PASS”和“静态审查即 PASS”。
- 新增隔离 AppData 环境构造器，默认剥离 DeepSeek、Tavily 和 Evo 模型凭据。
- 新增 18 个真实场景与既有测试 ID 的显式映射。
- 新增本地发布证据目录忽略规则，防止截图、日志、数据库和模型输出进入 Git。

## 未完成与风险

- 还没有把 Computer Use 事件自动写入每个场景的 `events.jsonl`。
- 截图/视频的安全持久化和清理策略尚未完成。
- Mock Provider 尚未接入真实桌面进程。
- DESK/UI 各项尚未逐项运行，因此矩阵状态保持不变。
- v8.0.1 的 24 小时门禁没有获得单独豁免；当前不得跳过或发布。
- 版本号仍为 8.0.0，符合方案要求。
