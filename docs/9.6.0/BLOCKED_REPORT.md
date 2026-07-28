# 司忆 v9.6.0 门禁阻断报告

状态：`BLOCKED / NOT_RELEASED`。

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

## 阻断项

| 必需验收 | 状态 | 原因 |
|---|---|---|
| 真实单图描述 | BLOCKED | 当前仅安装 `qwen3:4b` 文本模型，没有已授权视觉 Provider |
| 真实多图差异 | BLOCKED | 同上 |
| 真实图表读取 | BLOCKED | 同上 |
| 真实 UI 截图诊断 | BLOCKED | 同上 |
| 远程视觉 Provider 真实调用 | NOT_RUN | 未获请求级视觉 API Key 与 Provider URL/model 配置，且不得默认调用付费 API |
| 桌面封包、安装与性能 | NOT_RUN | 发布门禁已被必需视觉真实场景阻断，不生成伪 9.6.0 包 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 参考压缩包安全结论

`skills.zip` 只读检查发现参考视觉脚本包含硬编码远程凭据。该脚本未安装、未执行、未复制，凭据未写入仓库或运行配置。该凭据已暴露在压缩包源码中，建议由凭据所有者立即撤销并轮换；不得把它用于 v9.6.0 验收。

## 解除阻断所需输入

二选一：

1. 提供一个已授权的 OpenAI-compatible 视觉 Provider 的 base URL、model，并在测试请求时以 `X-Vision-Model-Api-Key` 临时传入密钥；密钥不落盘。
2. 明确授权安装一个指定的 Ollama 视觉模型。安装前需确认模型名称、固定版本/摘要、预计下载量和磁盘占用；司忆不会自行选择或下载大模型。

解除阻断后必须真实执行单图、多图、图表和 UI 四类场景，再重跑回归、隐私、封包、安装迁移与性能门禁；全部通过后才可发布 v9.6.0。
