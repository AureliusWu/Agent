# 司忆 v9.6.0 门禁阻断与解除记录

当前状态：`BLOCKED / NOT_RELEASED`。

当前产品版本：`9.5.0`。未同步任何版本源到 9.6.0，未封包，未进入 v9.7.0，未推送 GitHub。

计划提交：`44b4590`。  
实现提交：`50fb34a5f69736f7bfb1729748e36d30881def1d`。

## 已完成

| 项目 | 状态 | 实际证据 |
|---|---|---|
| 六个视觉核心接口 | PASS | `vision.describe / extract_text / analyze_chart / compare / inspect_ui / classify` 已注册并进入 Runtime Tool 路由 |
| 图片预处理 | PASS | 真实 JPEG/PNG/WEBP 文件解码、方向归一、重新编码、EXIF 清除、大图概览与有界切片 |
| 安全错误场景 | PASS | 伪签名、动画、多图总字节/总像素、工作区越界、无模型配置均明确拒绝或降级 |
| 权限 | PASS | 远程图片外发要求 `network.request` 确认；readonly 拒绝在 Provider 调用前发生 |
| 密钥边界 | PASS | 远程密钥仅从请求头进入，响应、图片元数据、审计和 Provider profile 不回显 |
| 视觉专项 | PASS | `18 passed`，含实际文件系统、实际 Pillow 解码/重编码、权限拒绝、取消与错误路径 |
| 后端完整回归 | PASS | `537 passed, 7 skipped` |
| 真实 Ollama qwen3:4b 文本回归 | PASS | `5/5`，耗时 `713.71 s` |
| Core Eval | PASS | `18/18`，run id `e5229b38fd08470fab8d9958682919e1` |
| 前端 | PASS | lint、安全契约、TypeScript、Vite build |
| Rust | PASS | `6/6` |
| 隐私扫描 | PASS | 源码、视觉测试、staged 与 Git history 均无凭据或私有运行数据 |

## 原阻断项与解除证据

| 必需验收 | 状态 | 原因 |
|---|---|---|
| 真实单图描述 | PASS | GLM-4.6V 实际识别红色矩形、蓝色圆形和 `VISION 42` |
| 真实多图差异 | PASS | GLM-4.6V 实际识别红色矩形到绿色圆形及 BEFORE/AFTER 差异 |
| 真实图表读取 | PASS | GLM-4.6V 实际读取 A=10、B=30、C=20，并区分上升和下降趋势 |
| 真实 UI 截图诊断 | PASS | GLM-4.6V 实际识别 `ERROR 503 - SERVICE UNAVAILABLE` 和 `RETRY` |
| 远程视觉 Provider 真实调用 | PASS | 2026-07-29 使用请求进程内凭据完成 5 次实际请求；4 次基线 + 1 次 TEST_DEFECT 修复后的 UI 定向复核 |
| NSIS 封包、安装、升级与数据保留 | PASS | v9.5.0→v9.6.0 原位升级、v34→v35、卸载保留、重装识别均通过 |
| MSI 安装 | BLOCKED | per-machine MSI 需要管理员权限；当前进程实际返回 Windows Installer error 1925 |
| 性能 | PASS | 修复 Pillow 启动时提前加载后，三次冷启动 `1638/1650/1649 ms`，中位数 `1649 ms` |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 参考压缩包安全结论

`skills.zip` 只读检查发现参考视觉脚本包含硬编码远程凭据。用户随后在当前任务中明确提供并授权该凭据用于验收。验收没有执行参考脚本，而是通过司忆自己的元数据清理、Provider、网络策略和视觉服务链路发起；凭据只进入测试进程环境，未写入仓库、配置、报告或日志。由于凭据已经出现在压缩包和聊天中，发布后仍应撤销并轮换。

## TEST_DEFECT 记录

第一次 4 场景运行返回 4 个 `status=ok`。图表输出明确包含“从 A 到 B 上升”和“从 B 到 C 下降”，但测试断言只接受“先上升后下降”这一整句，导致退出码 1。该项分类为 `TEST_DEFECT`，不是模型或产品失败。断言修正为分别验证上升与下降语义；为控制付费调用，没有重复另外三个已经获得实际证据的场景，只追加一次 UI 定向复核，结果 `1 passed`。

## 解除后的回归

- 后端非付费/非本地模型：`538 passed, 1 skipped, 7 deselected`，覆盖率 `83.00%`。
- 真实 Ollama `qwen3:4b`：`5 passed`，耗时 `283.20 s`。
- Core Eval：`18/18`，run id `d7ab36050ab74bba93b19f17aa38b306`。
- 前端 lint、安全契约与生产构建：PASS。
- Rust：`6 passed`。
- tracked、staged、当前分支 history 隐私扫描：PASS。
- GLM 实际 Token 与费用：当前 Provider 返回链路未向测试报告暴露精确统计，因此记为“无法准确计算”，不得估算。
- MSI 管理员安装：BLOCKED；正式版本源保持 `9.5.0`，不进入 v9.7.0。
