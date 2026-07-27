# 司忆 v8.0.2 测试矩阵

- 目标版本：`8.0.2`
- 当前源码版本：`8.0.1`
- 绑定基线提交：`e2f52e3589b1c8cb930e5be9f287c67adb385e31`
- 发布门禁：`NOT READY`

旧 220 项矩阵只读保留在 `docs/8.0.0/TEST_MATRIX.md`。其 `141 PASS / 2 FAIL / 21 BLOCKED / 56 NOT_RUN` 仅是基线事实，不继承为 v8.0.2 的当前 PASS；后续重跑时逐项写入新证据。

本版本新增 44 项强制测试，机器可读状态与证据见 `TEST_MATRIX.json`。Phase 0 CI 是迁移门禁证据，但发生在后续 Phase 1 修改之前，因此不冒充当前提交的用例 PASS。全部新增用例当前均为 `NOT_RUN`，不得按代码存在性或推断判定 PASS。
