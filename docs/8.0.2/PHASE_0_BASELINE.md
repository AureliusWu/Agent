# 司忆 v8.0.2 Phase 0 基线

记录时间：2026-07-27（Asia/Shanghai）

## 冻结对象

| 项目 | 值 |
|---|---|
| 旧分支 | `codex/v2.0.1` |
| 旧远程提交 | `c31d1e5b75567664f83f9f53a42c3143ca82e5d2` |
| 旧树 | `f7a3b8303a5b7946dd56a82b99bb1adc7cf8756f` |
| 旧版本 | `8.0.1` |
| 旧 Schema | `29` |
| 旧发布标签 | `v8.0.1` |
| main 起点 | `290cfa6ce32c6a29e2eadb3cd137bf844ce910ef` |
| main 树 | `699f1ca5f797604077e317b52bb4c0df20ce33f5` |
| 净化分支 | `clean/v8.0.2` |

旧分支相对 `origin/main` 为：领先 14 个提交、落后 6 个提交。旧分支只作为审计参考，不 merge、不 rebase、不继续开发。

## 旧测试事实

旧 220 项矩阵保持只读：

| 状态 | 数量 |
|---|---:|
| PASS | 141 |
| FAIL | 2 |
| BLOCKED | 21 |
| NOT_RUN | 56 |

这些状态只作为 `baseline_status`，不得自动成为 v8.0.2 的 `current_status`。

## 隐私净化

- `origin/main` 的可达历史不包含 `natsume-kokoro.png`。
- 旧 v8 分支的可达历史包含该私人 PNG Blob，不能直接合并。
- 本机工作区中的图片已按 SHA-256 保存在 AppData `Siyi/resources/user-assets/`，不进入净化分支。
- 净化源码使用 `kokoro-placeholder.svg`。
- 删除隐私扫描器中的私人 PNG 大文件白名单。
- v8.0.1 本地发布证据已移出仓库，归档到 AppData，不作为新分支测试证据。

## 当前发布状态

```json
{
  "implementation_status": "MIGRATING",
  "test_status": "NOT_READY",
  "distribution_status": "NOT_DISTRIBUTED"
}
```

Phase 0 完成前不得更新版本为 8.0.2，不得创建 Release，不得把旧 PASS 解释为新分支 PASS。
