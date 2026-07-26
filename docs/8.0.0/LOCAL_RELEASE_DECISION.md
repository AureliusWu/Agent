# 司忆 v8.0.0 本地发布决定

日期：2026-07-26

## 决定

司忆本地版本统一更新为 `v8.0.0`。本次决定仅覆盖本地源码、离线验证与本地安装包，不包含 GitHub 推送或云端发布。

用户已明确取消 24 小时耐久测试，并要求在基本能力完成后更新到 `v8.0.0`。因此 `LONG-005` 保持 `NOT_RUN`，作为已知豁免项，不得解释为 `PASS`。

## 发布依据

- 220 项测试矩阵保留实际状态：141 PASS、2 FAIL、21 BLOCKED、56 NOT_RUN。
- 后端完整离线回归已有 376 PASS、1 SKIPPED 的实际证据。
- EvoPolicyGym 固定官方 commit `34474a604e821ab4201697f345d60b0b43912495`，Toy 与 CartPole 已真实运行。
- Windows NSIS 安装、启动、升级、卸载生命周期已有通过证据。
- 前端、隐私、性能、凭据存储等关键离线门禁已有运行证据。

## 已知限制与豁免

- `LONG-005`：24 小时耐久测试未执行，用户明确豁免。
- `EVO-006`、`EVO-008`：真实运行结果未满足测试集规定的迭代反馈判据，维持 FAIL。
- MSI 生命周期：非管理员环境受 Windows Installer 权限限制，维持 BLOCKED；NSIS 路径已验证。
- 桌面 UI 自动化控制通道不可用，相关项目不得判定 PASS。
- Codex Security 全量扫描连接曾中断；现有隐私与依赖审计结果不能替代完整安全扫描。
- 为节省 DeepSeek Token，用户发出限制后不再运行任何付费模型测试。

## 发布边界

- 本地版本：`v8.0.0`
- 本地 Build ID：`9647780a340319f8a5670aa4`
- 本地构建：已生成 NSIS 与 MSI 安装包
- GitHub / 云端：未更新
- 测试矩阵：不因版本发布而篡改原始判定

## v8.0.0 最终离线复核

- 版本元数据：8.0.0，全部正式版本源一致。
- 后端：376 PASS、1 SKIPPED、0 FAIL。
- 前端：类型检查、单元测试、生产构建通过。
- Rust：5 PASS、0 FAIL。
- 性能：独占环境下三次冷启动门禁通过。
- 隐私扫描：通过。
- Windows Credential Manager：真实合成凭据写入、读取、删除验证通过。
- NSIS 生命周期：v7→v8 原地升级、Schema v27→v28、数据保留、卸载、重装均通过。
- 程序文件版本：ProductVersion 与 FileVersion 均为 8.0.0。

安装包：

- `desktop/src-tauri/target/release/bundle/nsis/司忆_8.0.0_x64-setup.exe`
- `desktop/src-tauri/target/release/bundle/msi/司忆_8.0.0_x64_zh-CN.msi`

## 回滚

如本地 v8.0.0 出现阻断问题，可卸载 v8.0.0 后安装保留的 v7.0.0 NSIS 安装包。用户数据目录不得随卸载或回滚删除。
