# v16 接受产物生产入口

本文件说明生产端实现，不是 RC、安装、真实模型、人工验收或已上传证明。
实现遵循 release-checklist 的“本地结果不替代远端验收”边界。

当前 installed helper 到公开 artifact 尚未闭环：原始绝对命令、
fixture-scoped inventory 解析及新增 build admission 证据链存在明确
阻断，详见末尾兼容性审计。在修订采集协议并重新验收前，下面的
传输步骤不能被视为当前可完成的真实安装发布流程。

## 本地冻结与导出

先完成原 `RC_GATE_CONTRACT.md` 的全部要求。新入口不增加模型调用、不安装
程序、不创建人工 PASS、不重编候选，也不修改旧收据中的 argv 或来源。

```powershell
.\siyi\.venv\Scripts\python.exe scripts/export-accepted-rc.py export --output build/v1600-evidence/transport/unique-accepted.zip
```

导出先读取固定 `accepted/rc-bundle.json`、`default-model-identity.json`，运行
现有十项总 RC 与 NSIS/MSI 生命周期验证器。必须当前 CLEAN 同源、
`RC_READY_NOT_RELEASED`，否则不导出。随后按原引用递归保留完整附件闭包、
完整 sidecar inventory、实际安装包、上一正式升级包、SBOM 和 notices。
索引继续采用既有 importer 格式与受限布局，最多20,000文件/8 GiB。
创建 fresh ZIP 后重新验证复制的实际字节和完整 RC/源码身份。

拒绝链接/reparse/hardlink、大小写重名、特殊文件、hash不符、缺项、超限、
数据库、env、私有备份/导出、真实或合成音频等材料。原始文本附件含实际
机器绝对路径、PII 或凭据时失败；必须设计新的安全采集，不得脱敏改写
旧 argv/收据以继承资格。失败产物仅保留本地检查，不能上传。
扫描不能证明所有未知格式秘密都不存在；上传前仍须独立审核完整索引。

## 经授权的传输动作（此轮未执行）

GitHub workflow 无权读取本机目录。完成导出、独立隐私复核并得到上传
授权后，可由操作者把该 ZIP 暂存到**本仓库 draft Release 的 asset**。
这是显式传输暂存，不是正式发行；不得把私有 Git bundle、数据库、env、
用户原始证据或录音作为资产上传。记录实际 asset ID 与 exporter 输出的
ZIP SHA-256，再手动从当前 main 运行 `rc-acceptance.yml`。
draft 不等于本地私密保险箱，授权上传即为 GitHub 外部传输。

workflow 只接受显式本仓库 asset ID 和完整 SHA-256，不接受任意URL。
通过 GitHub asset API 读取 own-repo 元数据，下载仅允许 GitHub 固定
资产域重定向，重定向不携带 Authorization/Cookie。精确核验大小/摘要后，
ZIP 只展开到 fresh ignored `rc-input`，拒绝多余未索引文件和路径攻击。
随后运行既有 importer、安装证据验证器和原完整总RC。主分支在开始及
上传前必须仍为 workflow 的精确SHA。上传前再次核验 expanded transport
全部实际文件、隐私、hash与未索引新增文件，并复验完整总RC。
全部成功才上传固定名：

`Siyi-RC-Acceptance-v16.0.0`

该同仓库、同SHA成功run ID 可在后续经授权tag/正式release时作为现有
`rc_evidence_run_id`。release workflow再次独立校验，不重编验收过的包。
本入口不会自动建立tag、触发正式发布、改版本或宣称 RELEASED。
此轮没有创建draft、上传资产、调用远端workflow或生成真实接受产物。

## 已确认未闭环：installed helper 与公开 transport

以下为源码兼容性审计，不是实际安装通过记录。当前生产入口具备拒绝
不合格材料的能力，**不代表新 installed collector 的结果已经能够完整导出**。
本节未修改旧收据、私密路径扫描器、十项总 RC 或安装验收门槛。

### 原消费合同可以接收的部分

`rc_installed_lifecycle.Lifecycle.run()` 成功分支的 `report_type`、
`target_version`、`source/source_after`（含 `build_id`）、`run`、
`checks/results` 与包 `artifacts` 形状对应
`check-release-evidence._validate_one()` 的合同。安装桌面 EXE 使用原有
NSS/MSI SDK 标记精确派生，实际 sidecar 字节和完整 payload 也核对。
`rc_gate.check_bundle()` 的 installer check 通过
`rc_payload_inventory.content_summary()` 比较位置无关内容，允许 installed
inventory 以自有 fixture 为根保存 `install/...` 观测路径。该设计应保留。

这仅确认字段及计算关系，不是测试确认全部成功分支。现有 lifecycle
合成测试没有把真实形状的成功报告完整送入原总 RC 和 transport 链。
CLI 的 `--request` 允许绝对 test-only boundary 下任意 fresh 输出文件；
尚无生产桥保证它同时成为固定 `nsis-installer-smoke.json` /
`msi-installer-smoke.json` 和 bundle `installers` 引用的同源相同字节。
外部私有目录的收据不能直接成为仓库相对附件；不得事后改内部 argv。

### 当前必然失败的两个公开导出点

1. `WindowsAdapter.owned_command()` 返回真实 `command`，
   `Lifecycle.run()` 将其保存在 `checks[stage].result`。
   `WindowsAdapter.install()` 的 MSI argv 包含实际 msiexec、包路径、
   `INSTALLDIR` 和 `/L*v` 私有日志路径；NSIS argv 包含实际包/卸载器及
   `/D`、`_?=` 安装目录。因此即使生命周期全部执行通过，报告原始
   JSON 仍含机器绝对路径，`export-accepted-rc.public_content()` 会拒绝。
   exporter 不会删除、替换或伪装这些实际命令。
2. `WindowsAdapter.launch()` 调用 `inventory(fixture.root, ...)`，
   得到 `binary.path=install/agent-backend.exe` 及
   `entries[].path=install/_internal/...`；这些是 fixture-scoped 观测，
   不是仓库附件。报告顶层 `installed_sidecar_payload` 和各 launch check
   目前都内嵌这份 inventory。exporter 的通用 `references()` 遍历任何
   `path+sha256` 对，继而由 importer 的 `allowed_path()` 拒绝
   `install/...`。原总 RC 的内容摘要可以接受它，不代表通用 transport
   已理解该观测的作用域。不可用“忽略所有 path 引用”解决。

只读合成协议探针确认以上差异：fixture-scoped inventory 能计算有效
内容摘要，但其引用被公开路径合同拒绝；合成绝对命令被隐私校验拒绝。
探针没有创建安装报告、候选、人工 PASS 或真实 RC 证明。

### 私有日志、工具链与新 admission 证据链

`prepare()` / `validate_build_audit()` 已要求候选及上一包的真实
`no-bootstrap-build-v1` audit、effective config、build manifest、
execution、非空 raw build log 和 toolchain artifact；还要求上一正式包
GitHub asset digest 的 receipt。它们进入 `Plan.inputs` 并在执行前复核。
但当前成功报告只嵌入包 metadata 和 manifest，没有 hash 引用这些 audit、
官方包 receipt、private dispatcher observation、实际 render receipt 或
逐阶段 owner/event 文件。**因此当前闭包不会自动纳入这些 private
log/toolchain 文件，但远端也无法重验这批新增 admission 事实。**

当前源码检索只找到新 audit 的消费端，未找到对应的真实生产入口；不能
假设旧构建日志已经符合新协议，更不能手工补一个 `actual_run: true`。
如后续加入这些引用，还需正面处理受限布局和隐私，而非仅加字段：

- helper 接受较宽的 `build/candidates/`、`build/generated/` 和普通
  `build/v1600-evidence/` 输入，但 importer 只允许既有明确路径。
- 公开 evidence 附件必须在采集时选择 `accepted/`；升级 baseline
  下仅允许实际 `.exe/.msi`，不允许额外 manifest/audit JSON。
- `.log` 被 exporter 明确拒绝；build raw output 可能含机器路径、
  工具安装位置和用户信息。改后缀也不会绕过原始文本内容扫描。
- owner/registry/shortcut/probe 和合成 DB、备份、env、模型 sentinel
  属于本地 retained fixture；不得把整个 `.run` 目录打包上传。
- `credential_manager_isolation` 明确仍是同 Windows 用户未隔离；
  不能把隔离数据目录宣称为独立 Credential Manager 验收。

### 最小下一步方向（尚未实施）

先定义**新的双流采集协议**：private raw argv/log/owner/probes 保持真实
原样、本地留存；public receipt 在真实采集时独立产生，只表达明确的
阶段、原包与源码绑定、实际 Job cleanup/exit、component observation 和
内容摘要，不携带也不冒充被改写的 actual argv。公开文件布局和固定
installer 入口应在采集前选定；不能把旧私有 receipt 脱敏后继承资格。

installed inventory 应显式标明 fixture-scoped 观测语义。经原
`content_summary()` 与接受候选完整 payload 确认内容一致后，exporter
才能仅对这一**经过验证的、类型明确的观测节点**不继续解析其 scoped
path 为仓库文件；inline 观测字节应保留。其他普通 `path+sha256` 引用、
payload 真正文件闭包、路径保护和原十项总门禁继续严格验证。

先补 helper-shaped 合成 integration：原 installer consumer → typed
closure/export → import → 原远端 RC consumer，覆盖私密命令拒绝、
scoped inventory 语义、遗漏/篡改/普通路径逃逸和固定 smoke 路径。
合成夹具必须保持非真实资格，不能制造真实 RC PASS。再补真实
no-bootstrap provenance 生产与公开消费闭环，重新完成授权采集；
没有完成以上步骤前，不应尝试真实上传或把本入口称为端到端已验收。
