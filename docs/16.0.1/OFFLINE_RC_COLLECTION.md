# Windows 离线 RC 证据采集

`Collect Windows offline RC evidence` 为当前干净源码运行原有完整测试入口，采集 19 项可离线执行的自动化检查及 core、multi_agent、professional_agents、adversarial 四组完整脚本 Eval。

Windows 本机已按锁文件准备 Python 3.12 虚拟环境和前端依赖后，从仓库根目录执行：

```powershell
.\siyi\.venv\Scripts\python.exe scripts/collect-rc-offline.py
.\siyi\.venv\Scripts\python.exe scripts/collect-rc-offline.py --execute
```

第一条只显示采集范围。第二条实际执行离线检查，不安装软件、不下载模型、不调用付费 API、不访问麦克风、不创建标签或 Release。首次提交采集流程后自动运行；后续可从 Actions 手动运行当前选定源码。

采集器使用既有 `record-rc-check.py`、逐项专用验证器、原始后端 JUnit/collection/coverage/execution 和 Eval 公开投影。每阶段绑定同一源码；DIRTY、失败、缺项、原始结果被修改或隐私扫描拒绝时停止。每次创建新目录，旧失败材料保留；不导入或改写历史报告。

受控 pytest 在最初收集参数化用例时使用 `opaque-parameter-sha256-v1` 标识，避免把安全测试中的模拟路径、手机号或密钥形状写入公开用例名。标识在执行和原始 JUnit 生成前产生；函数名、参数、断言、用例数量、失败/跳过结果和专用验证均保留，不脱敏或改写已有原始收据。

只将明确列出的 JSON/XML 证据通过原 exporter 隐私校验后放入 `build/v1601-evidence/offline-download/<run>/`。工作流只上传该目录，不递归上传 pytest 临时数据、数据库、原始 Eval trace、原始日志或环境文件。可下载产物名称为 `Siyi-RC-Offline-v16.0.1-<source SHA>`。

这份离线采集包始终 `rc_eligible=false`、`formal_release=false`，不能作为 Release 的 `rc_evidence_run_id`。原 42 项合同完整保留：余下 2 项模型/同机性能检查、8 项桌面与安装检查、13 项实际操作检查，以及可比 Eval 基线、真实默认模型、模型资格、候选三组件/完整 payload 和正式快照仍须实际提供。只有完整材料经过原 `rc-acceptance.yml` 成功接受后，才能进入正式发布。
