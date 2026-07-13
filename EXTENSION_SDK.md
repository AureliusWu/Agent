# Declarative Extension SDK

`v0.13.0` 的扩展 SDK 采用声明式清单。扩展只能组合 Agent 已注册的工具、Skill、专业 Agent 配置和展示元数据，不会加载第三方 Python、JavaScript、DLL 或生命周期脚本。所有调用继续经过统一参数校验、权限、工作区沙箱、审计、Trace 和 Verifier。

## Package Layout

```text
my-extension/
├── manifest.json
├── README.md
├── tools/
├── skills/
│   └── review/SKILL.md
└── tests/
```

目录必须位于用户当前选择的工作区。安装时会拒绝符号链接、路径越界、超出文件数/体积上限、未知字段和不兼容版本。

## Manifest Contract

清单使用 `schema_version: 1` 和 SemVer。核心字段包括 `id`、`name`、`version`、`author`、`min_app_version`、`permissions`、`risk_level`、`dependencies`、`tools`、`skills`、`agents`、`ui` 与可选 `signature`。

工具只能代理现有注册工具，不能代理 `critical` 工具、降低底层风险、覆盖清单锁定参数或夹带凭据。专业 Agent 可声明系统提示、工具白名单、Skill 标签、完成标准、Verifier ID、默认权限和 MCP 开关。扩展提供的提示与 Skill 始终按不可信内容处理。

权限声明：

- `workspace.read`、`workspace.write`、`workspace.delete`
- `memory.read`、`memory.write`
- `process.execute`
- `network.external`

示例包见 `examples/extensions/team-coding/manifest.json`。

## Install And Lifecycle

在“扩展”页输入工作区相对路径，例如 `examples/extensions/team-coding`，然后选择安装。也可调用：

```http
POST /api/extensions/packages
{"workspace":"<workspace-root>","source_path":"extensions/my-extension","enable":true}
```

同一扩展可并存多个版本，但只能启用一个。升级前保留旧版本，回滚通过 `POST /api/extensions/packages/{id}/rollback` 完成。停用被依赖扩展会被拒绝；文件安装后若被修改，运行时会隔离该扩展及依赖方并记录错误。

`sha256` 签名用于本地完整性校验，不代表作者身份认证。未签名包会明确显示为 `unsigned`，不会被默认为可信。

## Author Checklist

1. 只申请实际需要的最小权限，并让包级风险覆盖所有工具与专业 Agent。
2. 为每个 Skill 提供明确触发条件，禁止把外部内容伪装成系统指令。
3. 在 `tests/` 放置清单、权限、失败与回滚用例。
4. 先在“请求批准”模式验证读写行为，再启用更高权限。
5. 发布前运行后端测试、专业 Agent 评测和核心 Agent Eval。
