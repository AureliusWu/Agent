# 司忆 v9.7.0 统一文档与产物引擎实施计划

## 状态

- 目标版本：`9.7.0`
- 当前基线：`9.6.0`
- 基线提交：`64883ae7973361e1ed44c64527c312b954b5b630`
- 当前状态：`PLANNED / NOT_RELEASED`
- 分发范围：仅本地；不推送 GitHub
- 版本规则：所有必需门禁通过前，`VERSION` 和用户可见版本继续保持 `9.6.0`

## 1. 目标与边界

本版本建立统一 Artifact Engine，使 Markdown、DOCX、PDF、PPTX 共享同一套内容模型、工作区边界、权限、错误报告、验证、渲染和导出流程，并提供以下 Agent Core 工具：

```text
artifact.markdown.create
artifact.docx.create
artifact.docx.edit
artifact.pdf.create
artifact.pdf.merge
artifact.pdf.extract
artifact.pptx.create
artifact.pptx.edit
artifact.render
artifact.validate
```

本版本不实现云端文档、多人协作、Office 宏、旧版 `.doc`/`.ppt`、第三方 Skill 市场或运行时依赖下载。用户已明确排除 24 小时耐久测试，该项记为 `NOT_APPLICABLE`，不得写成 `PASS`。

## 2. 现状审计

- `siyi/app/artifacts/store.py` 目前只提供任务结果字节存储与读取，不是文档生成引擎。
- 当前工具注册表没有 `artifact.*` 工具，API 和 Runtime 也没有统一产物执行入口。
- 当前后端没有 DOCX、PPTX、PDF 创建、编辑、合并、提取或渲染依赖。
- 当前 `DocumentVerifier` 只检查文件存在且非空，无法阻止损坏文档被判为通过，且文档后缀尚未覆盖 `.pptx`。
- 现有 Artifact API 会把任意字节强制按 UTF-8 解码，不能作为 Office/PDF 二进制下载链路。
- 当前机器未发现 Word、PowerPoint 或 LibreOffice；最终“真实打开/打印”必须补充固定版本、固定哈希、隔离运行的独立查看器证据，否则该门禁保持 `BLOCKED`。
- `skills.zip` 仅作为能力清单、公开格式和公开依赖的参考来源；不得复制其中的专有提示词、脚本、Schema 或实现，也不得把其中的 `node_modules` 带入项目。

## 3. Clean-room 架构

计划新增以下职责边界：

```text
siyi/app/artifacts/
├── errors.py       # 统一错误码与去敏错误报告
├── model.py        # 格式无关的文档、段落、表格、图片、分页与幻灯片模型
├── markdown.py     # Markdown 解析与创建
├── ooxml.py        # DOCX/PPTX 共用的 ZIP/XML 安全、关系、验证与元数据层
├── docx.py         # DOCX 创建、局部修改和提取适配器
├── pdf.py          # PDF 创建、合并、提取和验证适配器
├── pptx.py         # PPTX 创建、局部修改和提取适配器
├── render.py       # 统一页面/幻灯片渲染
├── validation.py   # 独立格式解析、结构检查和 Verifier 桥接
└── service.py      # 工具级编排、工作区路径、原子写入、回执与审计
```

统一内容模型负责中文字体、标题、正文、列表、分页、表格、图片和幻灯片。DOCX 与 PPTX 必须共用 `ooxml.py` 的包安全、关系检查、XML 验证和内嵌引擎元数据，不得形成两套彼此无关的底层包处理逻辑。

## 4. 公开依赖策略

候选依赖仅使用公开、可锁定、可审计的 Python 包：

- `python-docx`：DOCX 对象模型；
- `python-pptx`：PPTX 对象模型；
- `pypdf`：PDF 合并、读取和文本提取；
- `reportlab`：PDF 生成与中文排版；
- `pypdfium2`：PDF 页面渲染；
- 已存在的 `Pillow`：图片解码、尺寸与渲染。

依赖必须写入 `siyi/pyproject.toml`、`siyi/uv.lock` 和 `siyi/requirements.lock`，构建时锁定安装。运行时不得执行 `pip install`、`npm install` 或下载字体/模型。文档依赖采用懒加载，避免扩大普通聊天和 Sidecar 冷启动路径。

## 5. 安全、权限与数据边界

- 所有输入、输出和模板路径必须经过现有 `workspace_root` / `safe_path` 规范化；禁止 UNC、设备路径、越界、符号链接逃逸和绝对路径伪造。
- 创建和编辑属于写操作，纳入现有权限确认、审计、任务归属和可恢复备份；创建默认拒绝覆盖，编辑必须绑定文件版本令牌。
- 提取和验证属于只读操作；渲染若写出文件则按写操作处理。
- 产物写工具必须同步进入运行时 mutation 集合、工作区文件锁、side-effect checkpoint、崩溃恢复和受限返工边界，不能只在 Registry 中注册。
- Skill 权限推导必须把产物创建、编辑、合并和渲染明确归为 `filesystem.write` / `artifacts.write`，不得沿用未知工具默认只读的行为。
- OOXML/PDF 输入设置文件大小、条目数、展开总量、压缩比、页数、图片像素和输出数量上限，阻断 ZIP bomb、畸形 XML、宏和外部关系。
- 不执行文档内嵌脚本、宏、外部链接或对象；错误报告不得包含文件正文、凭据或完整机器路径。
- Skill 只组合 `artifact.*` 工作流，不携带文档引擎、依赖目录或默认 API Key。
- 产物内容只写入用户选择的工作区或现有任务 Artifact Store，不进入长期记忆。

## 6. 统一工具契约

- 创建接口接受 Markdown 或结构化 blocks，并返回格式、相对路径、字节数、SHA256、页/幻灯片数、验证结果和警告。
- 编辑接口采用受限的精确替换/追加操作，不接受任意 XML，不允许宏或关系注入。
- PDF 合并必须保持输入顺序并独立验证每个输入与最终输出。
- PDF 提取返回有页码边界的文本和明确的空页/扫描件状态，不伪造 OCR 结果。
- `artifact.render` 对 Engine 创建的 DOCX/PPTX 使用内嵌格式无关模型，对 PDF 使用锁定渲染器；不具备可靠渲染条件时返回稳定错误码，不以代码存在性判定成功。
- `artifact.validate` 返回统一的 `PASS / FAIL`、格式、结构检查、可打开性、页数/幻灯片数、风险和错误列表。
- 二进制交付优先使用受控工作区路径；若登记到任务 Artifact Store，则必须提供保留媒体类型和安全文件名的原始字节下载接口，不得经过 UTF-8 文本解码。

## 7. 实施阶段

1. 固化工具 Schema、错误码、内容模型、资源上限和安全策略。
2. 实现 Markdown 与共享 OOXML 包层。
3. 实现 DOCX 创建、精确局部修改、验证和模型回读。
4. 实现 PDF 创建、合并、提取、验证和页面渲染。
5. 实现 PPTX 创建、精确局部修改、验证和模型回读。
6. 接入工具注册表、Runtime、API、权限、审计、回执、文件锁、恢复、返工边界和任务 Artifact Store。
7. 更新 DocumentVerifier、documents 专业 Profile 与内置日报 Skill，使其只编排统一 Core 工具，不复制引擎。
8. 增加真实产物专项测试、独立解析器打开测试、渲染检查、错误与安全测试。
9. 在固定版本、固定哈希的隔离查看器中真实打开并导出打印表示；无法建立该环境时明确标记 `BLOCKED`。
10. 运行全量回归、Core Eval、Professional Eval、真实 qwen3:4b 合约、前端、Rust、隐私、性能与桌面安装门禁。
11. 所有必需门禁通过后，统一更新版本源到 `9.7.0`，从干净提交构建并生成本地发布证据。

## 8. 验收矩阵

### 功能专项

- Markdown 创建并转换为可打开的 DOCX；
- DOCX 创建与局部修改；
- PDF 创建、合并、逐页提取和渲染；
- PPTX 创建与局部修改；
- 中文字体、分页、列表、表格和图片正常；
- DOCX/PPTX 的共享 OOXML 安全验证生效；
- 统一错误码与去敏报告生效；
- 权限拒绝、确认、版本冲突和路径越界均有真实测试。

### 真实产物

- 使用独立解析器重新打开 DOCX、PDF、PPTX；
- 对 PDF 页面以及 Engine 创建的 DOCX/PPTX 预览执行真实像素渲染；
- 在隔离的独立查看器中真实打开 DOCX/PPTX，并导出 PDF 作为可打印证据；
- 检查页数、幻灯片数、中文文本、表格、图片、文件签名与 ZIP/XML 完整性；
- 产物可重复打开，且无宏、外部关系或损坏条目。

### 回归与发布门禁

- Artifact Engine 专项全部通过；
- 后端全量回归通过，覆盖率不低于 70%；
- Core Eval 18/18 通过；
- Professional Eval 全部通过；
- 真实 Ollama `qwen3:4b` 合约通过，不调用 DeepSeek；
- 前端 lint、安全契约和生产构建通过；
- Rust 测试通过；
- 跟踪文件、暂存区、提交历史和发布输入隐私扫描通过；
- 最终 clean build 的 NSIS/MSI、升级、卸载保留数据和重装识别通过；
- 以 v9.6.0 clean build 为同机性能基线，Sidecar 启动回归不超过 15%；
- 安装包不得包含 `node_modules` 或重复文档引擎；NSIS 相比 v9.6.0 增量不超过 25 MB，且总大小不超过 55 MB，否则阻断并审计原因；
- SBOM、依赖许可证、SHA256、版本源、Build ID、源码提交和工作区状态一致。

## 9. 发布判定

以下任一情况存在时，保持 `9.6.0`，并将 v9.7.0 标记为 `BLOCKED / NOT_RELEASED`：

- 必需用例为 `FAIL`、`BLOCKED` 或 `NOT_RUN`；
- 真实产物未由独立解析器和隔离查看器打开、未导出打印表示或未渲染；
- 安全、隐私、路径、宏或外部关系红线未通过；
- 安装包体积或性能超出门槛且没有完成原因审计；
- 最终安装、升级、卸载或重装验证未通过；
- 版本源、提交、Build ID 或证据不一致。

仅在全部必需门禁通过后，本地发布 `9.7.0`；未经用户明确授权，不推送 GitHub。
