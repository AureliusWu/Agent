# 7.0.0 耐久测试报告

状态：运行器已验证；正式时长门禁待执行。

## 运行器

`scripts/run-autonomous-soak.py` 重复执行发布级合成压力测试，并写出只包含计数、状态、耗时、输出尾部和 SHA-256 的 JSON 报告。默认时长为 1,800 秒。

示例：

```text
siyi/.venv/Scripts/python.exe scripts/run-autonomous-soak.py --duration-seconds 1800 --output build/soak/pr-30m.json
siyi/.venv/Scripts/python.exe scripts/run-autonomous-soak.py --duration-seconds 86400 --output build/soak/rc-24h.json
```

## 当前证据

- 冒烟：请求 1 秒，至少完整执行 1 轮发布级压力，结果通过。
- 单轮负载：1,000 Segment / 10,000 模型循环 / 50,000 工具记录 / 100 检查点。
- 报告位置：`build/soak/smoke.json`（构建目录，不提交 Git）。

## 待执行矩阵

| 档位 | 时长 | 当前状态 |
|---|---:|---|
| PR | 30 分钟 | 待执行 |
| 集成 | 2 小时 | 待执行 |
| Nightly | 8—12 小时 | 待执行 |
| 发布候选 | 24 小时 | 待执行 |
| 重大 Runtime | 72 小时 | 待执行 |

未完成 24 小时发布候选档前，不允许升级为 7.0.0。
