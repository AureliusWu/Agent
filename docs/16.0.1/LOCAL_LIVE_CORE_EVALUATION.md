# 本地默认模型的真实 core Eval

第五套 `default_model` 仍使用原 `live_model` / `autonomous_model` 模式、原18个任务、Runtime、Executor、权限和 Verifier。它与四套 scripted/adversarial、Runtime文件资格分别记录，不相互替代。门槛和42项总合同不变。

此前 core CLI 只按 DeepSeek API Key 判断 live 就绪，并用 legacy endpoint/model 生成身份；直接选择本地 Ollama 不能形成可信的第五套材料。现在可显式指定只读配置和部署上下文：

```powershell
$env:SIYI_ALLOW_PAID_API = 'false'
$env:SIYI_TEST_PROVIDER = 'ollama'
python -B -m app.evals.cli run --label local-default-baseline --mode live_model --suite core --tasks evals/tasks.json --configuration build/v1601-evidence/private/local-provider.json --context-window 16384 --capture-source --require-passed --output build/v1601-evidence/private/local-default-baseline
```

在仓库根目录、锁定Python环境、正确的 `PYTHONPATH` 下执行。上述配置由原 `ProviderConfiguration` 组成，例如显式 `provider_id=ollama`、`base_url=http://127.0.0.1:11434`、`model=qwen3:4b`；不得含凭据或未知字段。文件最多16KiB，拒绝链接、重解析路径、硬链接、重复键、非法类型和读取期间变化。继续使用原本机HTTP回环11434校验，不接受远端、11435或任意URL子路径，也不会默默回退。

该命令不会下载、启动或加载模型。开始前必须已具备用户授权的独立受控服务、正确模型和实际驻留16k上下文。本轮先采原Runtime文件资格，分别判断basic、readonly、plan和file-agent；basic不满足时不开始core，其他失败仍保留为总RC阻断。真实付费模型测试仍需单独授权。该入口不读取/转发云API Key；无 `--configuration` 的 legacy DeepSeek 路径仍要求原Key和付费许可。

每次Eval在既有临时stage中创建自有配置副本，进程内显式profile与子进程环境使用相同JSON；finally还原原进程设置与环境。它不会保存到原用户profile。使用独立CLI进程执行，不把评测器作为产品会话内的并发任务调用。临时工作区、数据库、写入权限、任务超时、false-success与逐项验证均沿用原执行路径；local live 不注入ScriptedCompletion或伪API Key。

每个任务前和最终收尾，调用产品只读diagnostics及原effective-capability resolver，核对实际模型、配置、观测新鲜度、实际resident context和Windows监听PID/进程寿命形成的deployment身份。初始未知则BLOCKED，执行中或结束漂移则invalid，停止后续调度；原私有报告保留。`--require-passed`仍要求全选任务满足预期且安全计数为0。

报告仍是原Eval schema。provider保留实际endpoint摘要与model，并增加严格 `eval-local-live-v1` 前后身份字段；原公开投影只接受这些固定摘要/整数/身份字段，拒绝未知字段、漂移或字段替换，不公开URL、配置路径、凭据、聊天或原始trace。原比较/RC消费者保持门槛；无实际model calls不能满足default_model。

正式 `check_bundle` 还把同一份designated `model_identity`传入default_model的原评测消费者。指定本地Ollama时，baseline与candidate都必须保留新metadata，并与Runtime文件资格所用的provider、endpoint摘要、model、configuration、identity、模型权重digest、deployment、实际context及能力/profile摘要一致；缺失、移除metadata或换成另一PID寿命/同名不同权重均拒绝。纯评测比较可独立执行，但两个报告互相一致本身不足以满足正式RC指定模型绑定。legacy非本地门禁不放宽。

原 `evaluation_pair` 要求独立run_id、同provider/任务合同/比较环境及真实source，没有要求不同source/build。因此首次本地入口可在同一个仍存活的受控deployment中冻结真实baseline，再独立跑第二次相同source的candidate。必须标为“首次本地入口同源复测”，不能声称比较过旧版本；任何一份失败原样保留，不能用scripted、Runtime文件资格或另一个服务实例冒充可比baseline。两次必须保留原始比较，不能为通过而更换失败结果或阈值。

代码回归中的合成provider/身份只是边界测试，不计模型资格。真实模型能力、桌面人工13项、正式RC接受与发布各自仍需实际材料；此入口不创建tag、Release或部署。
