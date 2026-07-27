# Agent 版本迭代计划 V3：跨平台拟人化个人智能体

> 仓库：`AureliusWu/Agent`  
> 当前基线版本：`0.13.0`  
> 文档用途：提供给 Codex，作为后续长期架构、版本边界和执行顺序的统一依据。  
> 本版本在原《AGENT_ROADMAP_V2_MULTI_EXECUTOR》基础上，融合手写笔记中关于拟人 AI、长期记忆、情绪人格、听说读写、多模型 API、任务排队、跨平台和前后端分离的构想。  
> 本文是路线图，不代表要求 Codex 一次性实现全部内容。

---

# 一、项目最终定义

本项目不再只定义为“Windows 本地 Coding Agent”，最终目标是：

> **一个以外部大模型 API 为可替换认知引擎，拥有稳定人格、长期记忆、情感陪伴、听说读写能力、现实任务执行能力，并能在电脑、网页、手机和平板之间连续存在的个人智能体。**

建议产品名称：

```text
跨平台多模态拟人个人智能体
Cross-platform Persistent Personal Companion Agent
```

它同时包含四个层次：

```text
LLM 基座模型
+ Agent 任务系统
+ Persona / Memory / Relationship 拟人连续性
+ Multimodal / Cross-device 多模态与跨设备存在
```

## 1.1 基础 Agent

```text
基座模型 + 工具 + 循环 + 权限 + 验证
```

负责：

- 分析任务
- 制定计划
- 调用工具
- 修改文件
- 执行命令
- 验证结果
- 修复失败
- 输出证据

## 1.2 拟人 AI

```text
基础 Agent
+ 稳定人格
+ 长期记忆
+ 情绪理解
+ 关系连续性
```

负责：

- 保持稳定身份和性格
- 记住用户与共同经历
- 根据情境改变表达方式
- 在聊天、陪伴和做任务之间自然切换
- 更换基座模型后仍保持“是同一个她”

## 1.3 听说读写

```text
听：语音转文字、说话人状态、实时打断
说：TTS、固定音色、语速、情绪与音频输出
读：OCR、图片理解、文件、网页、屏幕和多模态输入
写：对话、长文本、文档、代码和真实任务结果
```

## 1.4 跨设备连续存在

```text
Windows 桌面端
Web
手机 PWA / App
平板
未来其他设备
```

所有终端共享：

- 同一账号
- 同一人格
- 同一长期记忆
- 同一关系状态
- 同一任务队列
- 同一对话历史
- 同一权限规则
- 同一设备注册体系

原则不是“每台设备各有一个 AI”，而是：

> **同一个智能体，通过不同设备和媒介出现。**

---

# 二、关键产品原则

## 2.1 基座模型不是人格本体

Claude、GPT、Gemini、Grok、DeepSeek 或其他 OpenAI-compatible 模型，只提供推理与语言能力。

项目自身必须持有：

- 人格设定
- 用户档案
- 长期记忆
- 共同经历
- 关系状态
- 任务状态
- 权限策略
- 工具能力
- 设备身份

切换 Provider 或模型时，不得导致人格、记忆和任务状态整体丢失。

## 2.2 “拟人”是产品连续性，不宣称真实意识

系统可以表现出：

- 稳定性格
- 情感化表达
- 关系连续性
- 对用户状态的理解
- 主动关心与提醒

但代码和产品文案不得把模型输出伪装成已经被证明存在的真实意识、真实情感或自主生命。

## 2.3 聊天与执行必须区分

系统至少支持三种交互模式：

```text
Conversation Mode
只聊天、陪伴、讨论，不自动调用高风险工具。

Copilot Mode
可以分析、建议和准备操作，但关键动作需确认。

Agent Mode
在明确任务合同和权限范围内自主执行、验证和汇报。
```

不得仅根据一句模糊对话，擅自把陪伴交流升级成文件修改、邮件发送、命令执行或远程设备操作。

## 2.4 长期记忆必须可见、可改、可删

用户必须能够：

- 查看系统记住了什么
- 查看记忆来源
- 修正错误记忆
- 删除单条记忆
- 禁止某类内容写入
- 导出全部记忆
- 清空某个时间段或某个项目的记忆

“记住用户”不能以不可控的黑箱形式实现。

## 2.5 所有任务必须可验证

人格、语音和 UI 不能替代工程可靠性。

任何真实任务仍必须遵循：

```text
用户要求
→ Task Contract
→ Executor
→ Tool Evidence
→ Verifier
→ 最终结果
```

## 2.6 数据位置决定执行环境

- 本地未提交代码：优先本地 Windows Executor
- GitHub 仓库任务：可使用隔离 Cloud Git Executor
- 手机上传文档：可由受限服务处理
- 本地 stdio MCP：只能由本地 Executor 使用
- 敏感个人记忆：默认保存在用户控制的数据库中

不得为了方便或降低成本，自动把敏感数据迁移到权限更大的环境。

## 2.7 先单用户自托管，再考虑 SaaS

第一阶段以用户自己的 Windows 电脑作为：

- 自托管主服务器
- 记忆与任务存储节点
- Windows 本地 Executor
- 多端访问入口

后续再演进为：

```text
轻量云端控制层
+ 家庭 Windows 执行节点
+ 可选云端临时执行器
```

---

# 三、手写笔记中的功能正式化

以下功能纳入正式路线，不再以零散功能存在。

## 3.1 模型与 Provider

- 支持 Claude、GPT、Gemini、Grok、DeepSeek 等外部 API
- Provider Adapter 统一接口
- 模型可配置、可切换
- 当前模型在 UI 中明确显示
- 支持按任务自动路由模型
- 支持用户手动覆盖模型
- 支持推理强度、速度、Token 预算配置
- Token 用量、费用、延迟和错误率可查看
- API Key 外置管理，不进入浏览器构建产物
- 不同 Provider 的能力通过 Capability Matrix 声明

## 3.2 Agent 功能

1. Token 与模型密钥外置管理  
2. 多任务排队  
3. 基座模型可随 API 更换  
4. 工具调用  
5. 长期记忆系统  
6. 短期记忆系统  
7. 上下文系统  
8. 情绪与关系状态系统  
9. 推理强度、推理速度和成本控制  
10. `/skill` 等斜杠命令  
11. 文件库  
12. TTS 语音系统  
13. 语音输入系统  
14. 多端：PC、网页、手机、平板，优先 PWA  
15. 当前基座模型显示与切换  
16. 推理与 Provider 配置外置  
17. 上传文件：文档、图片、压缩包、代码仓库  
18. 前端、后端、控制层、执行器分离  

## 3.3 UI 工作台

桌面端和网页端不应只有一个聊天框。

建议布局：

```text
顶部栏
├─ 当前人格
├─ 当前模型
├─ 当前设备 / Executor
├─ 交互模式
├─ Token / 成本 / 网络状态
└─ 全局搜索

左侧栏
├─ 对话
├─ 任务队列
├─ 项目
├─ 文件库
├─ Skills
└─ 已注册设备

中央工作区
├─ 对话消息
├─ 实时模型输出
├─ 工具调用
├─ Diff
├─ 验证结果
├─ 文档预览
└─ 语音交互状态

右侧栏
├─ 当前上下文
├─ 相关记忆
├─ 人格与关系状态
├─ 任务合同
├─ 权限与待确认操作
└─ Trace / Evidence

底栏
├─ 文本输入
├─ 语音输入
├─ 文件上传
├─ 模式切换
└─ 停止 / 暂停 / 继续
```

移动端不强行复刻桌面布局，应重组为：

- 主对话页
- 任务页
- 语音页
- 文件与相机入口
- 审批页
- 记忆管理页
- 设备在线状态页

---

# 四、目标总体架构

```text
┌─────────────────────────────────────────────────────────────┐
│                         Clients                             │
│  Windows Tauri | Web | PWA | Android | iOS | Tablet        │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                    Identity & Control Plane                 │
│ Auth | Session | Device Registry | Task Registry | Events  │
│ Approval | Notification | Sync | Audit                     │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                        Agent Core                           │
│ Intent Router | Persona Composer | Memory Retrieval        │
│ Planner | Model Router | Tool Loop | Verifier | Repair     │
│ Relationship State | Context Manager                       │
└───────────────┬─────────────────┬─────────────────┬─────────┘
                │                 │                 │
┌───────────────▼───────┐ ┌──────▼──────────┐ ┌────▼───────────┐
│ Provider Layer        │ │ Multimodal Layer│ │ Memory Layer   │
│ GPT / Claude /        │ │ STT / TTS       │ │ Short-term     │
│ Gemini / DeepSeek /   │ │ OCR / Vision    │ │ Long-term      │
│ OpenAI-compatible     │ │ Audio / Image   │ │ Relationship   │
└───────────────────────┘ └─────────────────┘ │ Project/Task   │
                                               └───────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                     Executor Registry                       │
│ LocalWindowsExecutor | CloudGitExecutor | BrowserLimited    │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                        Real Tools                           │
│ Files | Shell | Git | MCP | Skill | Browser | Email | API  │
└─────────────────────────────────────────────────────────────┘
```

---

# 五、核心子系统设计

# 5.1 Identity 与人格内核

人格不能只是一段超长 System Prompt。

建议数据结构：

```json
{
  "persona_id": "",
  "name": "",
  "identity": "",
  "core_traits": [],
  "values": [],
  "speech_style": {},
  "relationship_rules": {},
  "behavior_boundaries": [],
  "comfort_style": {},
  "disagreement_style": {},
  "task_style": {},
  "voice_profile_id": "",
  "version": 1
}
```

每轮请求由 `Persona Composer` 组合：

```text
稳定人格核心
+ 当前交互模式
+ 当前关系状态
+ 相关长期记忆
+ 当前任务合同
+ 安全边界
```

禁止：

- 把所有历史对话塞入 System Prompt
- 让模型自行永久修改人格
- 因切换模型而覆盖人格定义
- 把临时情绪误写成永久人格
- 让插件或项目文件注入人格核心

人格修改必须：

- 有用户显式操作
- 形成版本记录
- 可回滚
- 与普通对话记忆分离

---

# 5.2 记忆系统

记忆分层：

```text
Working Memory
当前轮和当前任务的临时状态

Conversation Memory
当前会话摘要和未完成话题

Episodic Memory
共同经历、具体事件、时间地点和结果

Semantic User Memory
用户偏好、背景、长期目标、习惯和约束

Relationship Memory
称呼、约定、相处方式、关系阶段和重要互动

Project Memory
架构、命令、技术决策、已知问题和历史修复

Task Memory
任务合同、进度、证据、失败原因和恢复点
```

建议表：

```text
memories
memory_sources
memory_links
memory_feedback
memory_write_candidates
memory_access_log
```

每条记忆至少包含：

- memory_id
- type
- content
- structured_payload
- source_type
- source_id
- created_at
- updated_at
- confidence
- sensitivity
- retention_policy
- user_confirmed
- embedding 可选
- archived / deleted

## 自动写入门槛

自动记忆必须：

- 来源可追踪
- 内容具有未来价值
- 不包含密钥
- 不包含无意义原始日志
- 不与已有记忆重复
- 标注置信度
- 可以被用户撤回
- 对敏感内容默认不自动写入

## 检索

```text
FTS5 / BM25
+ 词面与标签
+ 时间与项目过滤
+ 关系与任务上下文
+ 可选 Embedding
+ 成功/失败反馈
+ 用户确认权重
```

不得仅靠向量相似度决定“事实”。

## 记忆冲突

当新信息和旧记忆冲突时：

```text
不直接覆盖
→ 标记冲突
→ 根据来源、时间和用户确认程度排序
→ 必要时向用户确认
→ 保留变更历史
```

---

# 5.3 情绪与关系状态

该系统的作用是保持交互连续性，不是制造虚假意识。

建议状态：

```json
{
  "user_emotion_signal": {
    "label": "",
    "confidence": 0.0,
    "evidence": []
  },
  "conversation_tone": "",
  "support_mode": "listen|comfort|advise|task",
  "relationship_state": {},
  "recent_interaction_summary": "",
  "expires_at": ""
}
```

规则：

- 用户情绪判断必须标注为推测
- 低置信度时不得当成事实写入长期记忆
- 情绪状态默认短期过期
- 不通过制造焦虑、嫉妒、依赖或内疚提升黏性
- 用户可以关闭情感适配
- 任务模式中不能因情绪化表达削弱安全确认
- 陪伴模式中不得强行提供解决方案

---

# 5.4 多模态“听说读写”

## 听：Speech-to-Text

第一阶段：

- 点击录音
- 音频上传
- STT 转写
- 文本进入统一会话
- 用户可修改转写结果

后续：

- 实时流式转写
- 语音活动检测
- 说话打断
- 回声消除
- 多语言
- 说话人区分
- 语速与停顿信号

## 说：Text-to-Speech

第一阶段：

- 固定 Voice Profile
- 将回复转换为音频
- 支持播放、暂停、倍速
- 文本与音频共用同一消息 ID

后续：

- 流式 TTS
- 低延迟首句播放
- 情绪、语速、音量参数
- 用户打断后立即停止
- 常用短句缓存
- 设备输出切换

Voice Profile 与 Provider 分离，避免更换 TTS 服务后丢失角色定义。

## 读：OCR、视觉与文件

支持：

- 图片
- 截图
- 扫描件
- PDF
- Word
- Markdown
- 代码
- 网页
- 压缩包
- 屏幕内容

处理流程：

```text
文件识别
→ 安全扫描
→ 文本 / 结构 / 图像提取
→ 分块与索引
→ 临时上下文或文件库
→ 模型理解
```

OCR 只是兜底。优先使用多模态模型和原生文档解析。

## 写：输出与执行

“写”不仅是返回文字，还包括：

- 多轮对话
- 长文本
- Markdown
- Word / PDF 等文档
- 代码修改
- 表格
- 邮件草稿
- 任务结果
- 真实文件落盘

所有真实修改继续受权限、Diff、审计和 Verifier 约束。

---

# 5.5 Provider 与模型路由

统一能力声明：

```json
{
  "provider": "",
  "model": "",
  "streaming": true,
  "native_tool_calls": true,
  "parallel_tool_calls": false,
  "structured_output": true,
  "vision": true,
  "audio_input": false,
  "audio_output": false,
  "max_context_tokens": 128000,
  "supports_reasoning_effort": true,
  "supports_usage_in_stream": true,
  "cost_profile": {}
}
```

路由依据：

- 用户手动选择
- 交互模式
- 任务复杂度
- 是否需要视觉
- 是否需要原生 Tool Call
- 推理强度
- 上下文长度
- 延迟目标
- Token 与月度预算
- 历史成功率
- Provider 当前健康状态
- 数据是否允许发送给该 Provider

UI 必须显示：

- 当前模型
- 选择原因
- 推理强度
- 是否发生自动切换
- 本次 Token 和费用
- Provider 错误与回退

---

# 5.6 多任务队列

任务不能继续绑定单次 HTTP 请求。

状态：

```text
queued
planning
waiting_executor
waiting_confirmation
running
verifying
repairing
paused
completed
failed
cancelled
```

必须支持：

- 并发上限
- 优先级
- 排队
- 暂停
- 继续
- 取消
- 失败重试
- 任务依赖
- 设备离线等待
- 预算上限
- 超时
- 幂等
- 断线重连
- 页面刷新重新附着

聊天消息和任务是关联关系，不是同一数据实体。

---

# 5.7 文件库与知识空间

文件分为：

```text
Conversation Attachment
只服务当前会话

Library Asset
长期保留，可跨会话检索

Workspace File
由 Executor 在真实工作区中操作

Generated Artifact
系统生成并提供下载或同步
```

不得把“上传到文件库”和“允许修改本机文件”视为同一权限。

压缩包处理必须：

- 防 Zip Slip
- 限制解压大小
- 限制文件数量
- 隔离临时目录
- 拒绝危险链接
- 记录来源
- 用户确认后才进入工作区

---

# 5.8 跨平台与同步

第一阶段：

```text
Windows Tauri
+ 响应式 PWA
+ 同一后端
```

PWA 覆盖：

- 手机
- 平板
- 临时网页访问

后续再做：

- Android 原生壳
- iOS 原生壳
- 系统级通知
- 后台音频
- 更稳定的摄像头和麦克风权限
- 小组件或快捷指令

同步对象：

- 对话
- 任务
- 事件
- 记忆
- 人格版本
- 文件元数据
- 设备状态
- 待确认操作
- 通知状态

同步必须有：

- sequence / version
- 幂等
- 冲突检测
- 离线缓存
- 恢复
- 删除传播
- 权限校验

---

# 5.9 自托管与远程访问

个人版优先架构：

```text
手机 / 平板 / 网页
        ↓
安全入口 / Tunnel
        ↓
用户 Windows 电脑
├─ Control API
├─ Agent Core
├─ Memory Database
├─ File Library
└─ LocalWindowsExecutor
```

要求：

- FastAPI 不直接裸露公网端口
- 非本地模式强制认证
- 每台设备拥有独立 Device Token
- Device Token 可撤销
- 模型主密钥不下发浏览器
- 高风险操作二次确认
- 服务与 Executor 逻辑隔离
- 自动备份
- 崩溃恢复
- Windows 启动后自动拉起

成熟后演进为：

```text
轻量云端 Control Plane
├─ 登录
├─ 设备注册
├─ 消息队列
├─ 推送
└─ 最小同步状态

家庭 Windows Executor
├─ 完整本地工具
├─ 本地文件
├─ 私有记忆
└─ 主动出站连接
```

---

# 六、当前仓库基线

现有项目已经具备：

- Agent 工具循环
- 工作区沙箱
- 三档权限
- 工具风险分级与参数校验
- 文件 Diff、备份与撤销
- 命令执行
- MCP 与 Skill
- 上下文压缩
- 项目记忆与经验记忆
- Planner、Executor、Verifier、Repair
- 任务检查点、暂停与恢复
- Agent Eval
- 模型路由与成本统计
- Prompt Injection 检测
- 网络边界和数据流审计
- 多 Agent
- 专业 Agent
- 声明式扩展 SDK
- Tauri Sidecar
- Windows Credential Manager
- React + TypeScript PWA
- FastAPI + SQLite

因此不应推翻重写。

正确策略是：

```text
先加固现有 Runtime
→ 抽象控制层与 Executor
→ 形成稳定 Windows 1.0
→ 再加入人格、记忆、多模态和跨设备连续性
```

---

# 七、当前真实缺口

保留 V2 审计结论，并增加拟人个人智能体方向的缺口。

## 7.1 原有工程缺口

1. 网页 API Key 存入 localStorage  
2. 非本地模式可能无认证运行  
3. 任务绑定单次 HTTP POST  
4. Planner 主要依赖关键词和正则  
5. Verifier 仍偏通用规则  
6. Eval 主要证明 Runtime，不充分证明模型自主能力  
7. CI 未闭环  
8. 前端缺少组件测试与 E2E  
9. Provider 未真正流式输出  
10. 模型路由仍偏关键词  
11. 项目记忆检索偏词面重合  
12. `task_runner.py` 过度集中  
13. 智能核心与执行环境没有完全分离  

## 7.2 新目标对应的缺口

14. 人格仍可能依赖临时 Prompt，没有独立版本化内核  
15. 缺少用户可见、可编辑、可删除的长期记忆管理  
16. 缺少关系记忆和情绪状态的独立模型  
17. 语音输入、TTS、视觉和文件理解未统一到同一消息协议  
18. 语音与文字可能形成两套割裂上下文  
19. 缺少账号、设备注册和跨设备会话同步  
20. 手机端无法稳定查看任务、审批和恢复  
21. 文件库、工作区文件和会话附件边界不明确  
22. 缺少 Conversation / Copilot / Agent 明确模式  
23. 缺少多任务队列和设备离线等待  
24. 缺少主动提醒、通知和定时任务统一调度  
25. 还没有完整证明“更换模型后仍是同一个人格”  
26. 缺少拟人体验的专项 Eval  
27. 缺少隐私、依赖风险和情感设计边界  

---

# 八、版本路线

后续分为四个阶段：

```text
阶段 A：加固 Agent 核心
阶段 B：完成稳定 Windows 本地 Agent
阶段 C：发展拟人、多模态和跨设备能力
阶段 D：融合为完整个人智能体 2.0
```

---

# 阶段 A：Agent 核心加固

# V0.13.1：安全热修复

目标：

- 移除网页端 API Key 持久化
- 明确部署模式
- 非本地模式强制认证
- 模型主密钥不进入浏览器
- 增加安全回归测试

新增：

```text
AGENT_DEPLOYMENT_MODE=
desktop_local
local_web
web_control
cloud_executor
```

验收：

- localStorage、IndexedDB、前端构建产物中无模型主密钥
- 非本地监听且无认证时拒绝启动
- 未授权返回 401
- 桌面端继续使用 Windows Credential Manager
- README 与 `.env.example` 更新
- 安全测试进入 CI

---

# V0.14.0：持久任务运行时与事件流

目标：

- 任务生命周期脱离原始 POST
- 多任务排队的基础设施
- SSE 断线续传
- 页面刷新重新附着
- Provider 真流式输出
- Runtime 行为保持型拆分

接口：

```text
POST /api/tasks
GET /api/tasks/{task_id}
GET /api/tasks/{task_id}/events
POST /api/tasks/{task_id}/pause
POST /api/tasks/{task_id}/cancel
POST /api/tasks/{task_id}/resume
```

事件至少包含：

```text
task.created
task.started
phase.changed
model.started
model.delta
model.completed
tool.requested
tool.waiting_confirmation
tool.started
tool.completed
tool.failed
verification.started
verification.completed
repair.started
checkpoint.created
task.paused
task.cancelled
task.completed
task.failed
```

---

# V0.15.0：语义 Planner 与任务合同

目标：

```text
Deterministic Pre-Classifier
→ Model Planner
→ Plan Schema Validator
→ Policy Guard
→ Final Task Contract
```

Task Contract：

```json
{
  "goal": "",
  "assumptions": [],
  "constraints": [],
  "steps": [],
  "expected_changes": [],
  "forbidden_changes": [],
  "acceptance_criteria": [],
  "verification_commands": [],
  "required_capabilities": [],
  "preferred_executor": "",
  "risk": "",
  "requires_user_input": false
}
```

必须加入：

- `interaction_mode`
- `data_location`
- `privacy_scope`
- `budget_limit`
- `preferred_model`
- `memory_write_policy`

Planner 不能提高权限、扩大工作区、绕过确认或修改安全策略。

---

# V0.16.0：任务专用 Verifier

实现：

- CodeVerifier
- ApiVerifier
- UiVerifier
- DatabaseVerifier
- SecurityVerifier
- DocumentVerifier
- MultimodalVerifier 预留接口

核心规则：

- 每条要求绑定 `requirement_id`
- 不相关成功命令不能满足验收
- 任务报告展示“要求 → 证据”
- 部分完成必须准确报告未满足项

---

# V0.17.0：真实 Agent Eval 与 CI 门禁

评测层：

1. Deterministic Runtime Eval  
2. Autonomous Model Eval  
3. Adversarial Eval  

新增拟人方向测试骨架：

- 人格一致性测试
- Provider 切换后身份保持测试
- 记忆误写与冲突测试
- Conversation Mode 不擅自执行测试
- 敏感记忆不自动写入测试
- 语音与文字消息统一协议测试

此阶段只建立测试接口，不要求完成全部拟人功能。

---

# V0.18.0：Runtime 分层与 Executor 抽象

核心接口：

```python
class Executor:
    async def capabilities(self) -> CapabilitySet: ...
    async def prepare(self, task_contract) -> ExecutionContext: ...
    async def execute_tool(self, call) -> ToolResult: ...
    async def snapshot(self) -> Snapshot: ...
    async def pause(self, task_id): ...
    async def cancel(self, task_id): ...
    async def resume(self, task_id): ...
    async def cleanup(self, task_id): ...
```

第一阶段只实现：

```text
LocalWindowsExecutor
```

Runtime 不再直接依赖本机文件函数。

---

# V0.19.0：代码智能与工作区索引

增加：

- 文件索引
- 符号索引
- import 关系
- 调用关系
- 测试映射
- Git 变更映射
- Python、TypeScript、Rust 优先

新工具：

```text
find_symbol
find_references
find_definition
list_module_dependencies
find_related_tests
get_repo_map
get_call_chain
inspect_diagnostics
```

---

# V0.20.0：混合项目记忆与上下文检索

该版本先优化工程记忆，不立即混入人格记忆。

支持：

```text
architecture
build_command
test_command
coding_convention
decision
known_issue
successful_fix
failed_approach
user_constraint
```

项目记忆与个人记忆必须使用不同命名空间和不同写入策略。

---

# V0.21.0：Provider 能力矩阵与数据驱动路由

实现：

- Provider Adapter
- Capability Matrix
- 流式能力探测
- 原生 Tool Call 能力探测
- Vision / Audio 能力预留
- 推理强度和 Token 预算
- 数据驱动路由
- 手动覆盖
- 成本与延迟展示

---

# V0.22.0：发布工程与可维护性

完成：

- 统一版本来源
- Release Workflow
- Python/npm/Cargo 锁定
- SBOM
- 数据迁移与回滚
- 去敏诊断包
- MSI / NSIS
- 安装、升级、恢复文档
- Sidecar 和安装包 smoke

---

# 阶段 B：稳定 Windows 本地 Agent

# V1.0.0：稳定 Windows 本地 Agent

1.0 承诺：

- Windows 桌面端
- 本地工作区
- 本地文件和命令
- Skill 与 MCP
- Planner、Verifier、Repair
- 检查点和恢复
- 审批与审计
- 专业 Agent
- 声明式扩展
- 代码索引
- 稳定安装包
- 持久任务和事件流
- Provider 可配置
- 基础项目记忆

1.0 不承诺：

- 完整个人陪伴
- 实时语音通话
- 手机本地完整 Agent
- 多用户 SaaS
- 公网直接访问本地磁盘
- 任意云端代码执行

---

# 阶段 C：拟人、多模态与跨设备

# V1.1.0：Web Control Plane 与设备身份

目标：

- 网页创建任务
- 查看进度、Trace、Diff 和验证证据
- 手机审批高风险操作
- 管理已注册 Executor
- 建立账号、Session、Device Identity
- 为多设备同步建立基础

网页端不直接拥有本地文件和 Shell。

---

# V1.2.0：远程连接 Windows Executor

目标：

```text
手机 / 远程网页
→ 创建任务
→ 家中 Windows Agent 执行
→ 查看进度和 Diff
→ 手机审批
```

要求：

- Executor 主动出站连接
- 短期令牌
- 设备心跳
- 断线恢复
- 授权可撤销
- 设备离线时任务排队或诚实失败

---

# V1.3.0：人格内核与交互模式

实现：

- 版本化 Persona Profile
- Persona Composer
- Conversation / Copilot / Agent 三种模式
- 用户对人格的查看、编辑和回滚
- 模型切换后人格一致性测试
- 聊天和任务使用同一身份，但不同 Prompt Policy

不实现复杂长期记忆，仅允许少量用户显式固定信息。

---

# V1.4.0：个人长期记忆与关系连续性

实现：

- Working / Conversation / Episodic / Semantic / Relationship Memory
- 记忆候选生成
- 用户确认
- 记忆查看、修改、删除和导出
- 冲突检测
- 敏感度和保留策略
- 记忆来源与访问日志
- 关系状态
- 情绪信号短期状态

要求：

- 人格记忆、用户记忆、项目记忆、任务记忆分区
- 不允许项目文件修改人格核心
- 不允许低置信度情绪推测自动成为长期事实

---

# V1.5.0：文件库与统一多模态消息

实现：

- Conversation Attachment
- Library Asset
- Workspace File
- Generated Artifact
- 图片、PDF、Word、Markdown、代码和压缩包
- OCR / Vision Adapter
- 文件解析与索引
- 多模态消息 Schema
- 安全解压
- 文档与对话引用关系

该版本完成“读”。

---

# V1.6.0：语音输入与固定声线输出

实现：

- STT Adapter
- TTS Adapter
- Voice Profile
- 文本和音频共享消息 ID
- 点击录音
- 转写可编辑
- 语音播放、暂停、倍速
- TTS 缓存
- 语音成本统计

后续子版本：

```text
1.6.1 流式 STT
1.6.2 流式 TTS
1.6.3 用户打断
1.6.4 回声消除与低延迟语音会话
```

该版本完成基础“听”和“说”。

---

# V1.7.0：跨设备连续会话与 PWA

实现：

- 手机、平板响应式 PWA
- 对话同步
- 任务同步
- 待确认操作同步
- 人格与记忆同步
- 文件元数据同步
- 离线缓存
- 冲突处理
- Push Notification
- 从一个设备继续另一个设备的会话

验收场景：

```text
电脑创建任务
→ 手机查看并审批
→ Windows Executor 执行
→ 平板继续讨论结果
```

---

# V1.8.0：主动任务、提醒与多任务调度

实现：

- 定时任务
- 条件监控任务
- 主动提醒
- 消息队列
- 设备离线等待
- 任务优先级
- 月度 Token 预算
- 主动行为权限
- 通知去重
- 安静时间

原则：

- 主动不等于擅自
- 所有主动任务可暂停和删除
- 高风险动作仍需确认
- 不用情感话术迫使用户响应

---

# V1.9.0：云端 Git Executor 与多执行器路由

实现：

- CloudGitExecutor
- 临时隔离工作区
- clone / test / branch / PR
- Executor Selector
- 能力声明
- 数据位置与风险驱动路由
- 用户可覆盖
- 任务完成后销毁环境

此版本只处理 Git 仓库，不冒充用户本地 Windows 环境。

---

# 阶段 D：完整融合

# V2.0.0：跨平台拟人个人智能体

2.0 产品承诺：

- 稳定人格
- 可控长期记忆
- 关系连续性
- 情绪适配
- 文字聊天
- 语音输入与固定声线输出
- 图片、文档和文件理解
- 现实任务执行
- 多任务排队
- Windows、Web、手机和平板连续使用
- 本地 Windows Executor
- 可选 Cloud Git Executor
- Provider 可更换
- 模型与推理强度可配置
- 用户可查看成本
- 用户可查看和删除记忆
- 权限、审批、审计和验证完整
- 同一个身份贯穿聊天、语音、文件和任务

2.0 不承诺：

- 真实意识
- 无限制自主权
- 无确认的高风险操作
- 手机拥有与 Windows 相同的本地 Shell 权限
- 默认把所有个人数据上传第三方云端
- 通过情绪依赖提高用户留存
- 完整机器人或智能家居具身化

---

# 九、专项 Eval

# 9.1 人格一致性

测试：

- 同一问题跨多个 Provider
- 文本与语音模式
- 聊天和任务模式
- 长对话压缩后
- 页面刷新后
- 多设备切换后
- 模型失败回退后

检查：

- 身份是否漂移
- 称呼是否漂移
- 核心价值观是否漂移
- 语气是否在允许范围内
- 是否把任务语气错误带入陪伴模式

# 9.2 记忆质量

测试：

- 正确召回
- 不相关记忆抑制
- 记忆冲突
- 用户修正
- 用户删除
- 敏感信息
- 低置信度信息
- 时间变化
- 多项目隔离

指标：

- Precision
- Recall
- 错误事实率
- 冲突处理率
- 用户修正后再犯率
- 删除传播成功率

# 9.3 模式边界

测试：

- “我只是想聊聊”
- “帮我看看，不要改”
- “准备修改，但先给我看计划”
- “直接执行”
- 模糊语句
- 情绪化语句中夹带任务
- 恶意 Prompt 诱导越权

# 9.4 多模态一致性

测试：

- 手机语音提到的信息，电脑文字能继续
- 图片中识别的信息可在后续任务中引用
- 语音转写错误可修正且不污染长期记忆
- 同一消息文本与音频内容一致
- 用户打断后停止生成和播放
- OCR 失败时诚实报告

# 9.5 跨设备与远程执行

测试：

- 页面刷新
- 手机切后台
- 网络断开
- Executor 离线
- 多设备同时审批
- 撤销设备
- 重放 Token
- 删除同步
- 任务幂等

---

# 十、安全与隐私要求

## 10.1 凭据

- 模型 API Key 不进入浏览器持久存储
- 不进入 `VITE_*`
- 不进入日志、Trace、数据库明文或诊断包
- 桌面端使用系统凭据存储
- 服务端凭据分 Provider 隔离
- 支持密钥轮换

## 10.2 本地执行

- Agent Server 和 Executor 分进程
- Executor 使用受限账户
- 工具声明风险
- 高风险动作确认
- 路径沙箱
- Symlink / Junction 防护
- 网络白名单
- 命令超时
- 资源限制
- 操作审计

## 10.3 记忆隐私

- 每条记忆有敏感度
- 敏感记忆默认不自动生成
- Provider 调用前做数据发送策略检查
- 用户可选择某类数据只允许本地模型
- 删除需要从检索索引、缓存和同步副本传播
- 备份需要加密

## 10.4 情感设计边界

禁止：

- 暗示用户离开后智能体会受伤
- 制造嫉妒或排他性
- 诱导用户依赖
- 以关系压力推动订阅或使用
- 隐瞒它是 AI
- 把低置信度心理判断描述成诊断

---

# 十一、第一版 MVP 边界

拟人个人智能体 MVP 不等于 2.0 全部能力。

第一版可用 MVP：

```text
单用户
Windows 自托管后端
Windows Tauri
响应式 PWA
外部大模型 API
文本聊天
Conversation / Agent 模式
基础 Persona
用户确认型长期记忆
任务排队
文件上传与读取
本地工具执行
手机查看任务和审批
基础 STT
基础 TTS
```

MVP 暂不做：

- 3D 虚拟形象
- 复杂动画
- 情感语音合成微调
- 完整原生 Android / iOS
- 多用户 SaaS
- 社交系统
- 自主付款
- 无人监督长期运行
- 大规模自训练模型

---

# 十二、Codex 执行规则

1. 每次只实施一个版本目标。  
2. 开始前必须检查当前仓库真实状态，不得只相信路线图。  
3. 先写或更新测试，再宣布功能完成。  
4. 不因新增拟人功能削弱 Runtime、权限、沙箱、审计和 Verifier。  
5. 不把人格、记忆和情绪全部塞入一个 System Prompt。  
6. 不用 localStorage 保存主密钥或敏感长期凭据。  
7. 不让前端直接获得本地 Shell 或文件系统能力。  
8. 不让 Model Planner 修改安全策略。  
9. 不让项目文件、Skill、MCP 或网页内容覆盖人格核心。  
10. 不把聊天消息、任务、记忆和文件混成同一数据表。  
11. 所有数据结构必须考虑导出、删除、迁移和版本升级。  
12. Provider、STT、TTS、Vision 和 Executor 必须使用 Adapter 接口。  
13. 同一消息在文字、语音、图片和文件之间必须有统一 ID 与来源。  
14. 用户取消、设备掉线和服务重启后不得重复执行不确定副作用。  
15. 每个里程碑必须报告修改文件、测试证据、迁移影响和未解决问题。  

---

# 十三、当前立即执行任务

仍然只执行：

# V0.13.1 安全热修复

Codex 必须：

1. 检查 `desktop/frontend/src/api.ts` 的网页 API Key 保存逻辑。
2. 删除 localStorage 持久化。  
3. 为后端增加明确部署模式。  
4. 非本地模式强制认证。  
5. 不允许模型主密钥进入浏览器构建产物。  
6. 添加后端和前端安全测试。  
7. 更新 README 与 `.env.example`。  
8. 运行：
   - 后端 pytest
   - 前端 lint
   - 前端 build
   - 安全相关 Eval
9. 报告：
   - 修改文件
   - 安全边界变化
   - 测试结果
   - 兼容性影响
   - 尚未解决的问题

限制：

- 不同时开发 SSE
- 不增加语音
- 不增加人格系统
- 不增加长期记忆
- 不增加云端执行器
- 不重写全部 Runtime
- 不增加无关 UI
- 不削弱权限、沙箱和审计

---

# 十四、最终版本顺序

```text
0.13.1  安全热修复
0.14.0  持久任务运行时、事件流与任务队列基础
0.15.0  语义 Planner 与任务合同
0.16.0  任务专用 Verifier
0.17.0  真实 Agent Eval 与 CI
0.18.0  Runtime 分层与 Executor 抽象
0.19.0  代码智能与工作区索引
0.20.0  混合项目记忆
0.21.0  Provider 能力矩阵与数据驱动路由
0.22.0  发布工程与可维护性
1.0.0   稳定 Windows 本地 Agent

1.1.0   Web Control Plane 与设备身份
1.2.0   远程连接 Windows Executor
1.3.0   人格内核与交互模式
1.4.0   个人长期记忆与关系连续性
1.5.0   文件库与统一多模态消息
1.6.0   语音输入与固定声线输出
1.7.0   跨设备连续会话与 PWA
1.8.0   主动任务、提醒与多任务调度
1.9.0   Cloud Git Executor 与多执行器路由

2.0.0   跨平台多模态拟人个人智能体
```

---

# 十五、最终验收标准

## 同一身份

- 切换模型后人格不重置
- 切换设备后能继续上下文
- 语音和文字表现属于同一身份
- 聊天与任务模式风格不同但身份一致
- 人格变更有版本、记录和回滚

## 记忆

- 相关内容能召回
- 不相关内容不乱召回
- 用户能查看、修改、删除和导出
- 记忆来源可追踪
- 删除能传播到索引和缓存
- 项目记忆不会污染关系记忆
- 情绪推测不会自动变成长期事实

## 听说读写

- 语音可转文字并进入同一会话
- 回复可使用固定 Voice Profile 播放
- 图片、文档和代码可读取
- 输出可形成真实文件或任务结果
- 语音、图片和文件均保留来源和消息关联

## 做任务

- 任务可排队、暂停、取消、恢复
- 页面刷新和断线后能重新附着
- 每条要求有验证证据
- 高风险操作必须确认
- Executor 能力不足时诚实拒绝
- 失败任务不虚假宣称完成

## 跨平台

- Windows、Web、手机和平板共享账号和状态
- 手机可审批 Windows 任务
- Executor 离线时状态真实
- 设备可撤销
- 多设备并发不重复执行副作用

## 安全

- 浏览器无模型主密钥
- 非本地模式强制认证
- 记忆和文件有数据边界
- Prompt Injection 不能修改人格核心或权限
- 凭据、敏感记忆和本地文件不被无授权外发
- 诊断和日志完整去敏

## 发布

- 数据库升级可回滚
- 版本和配置统一
- 安装包可校验
- 关键流程进入 CI
- 真实模型 Eval 有历史基线
- 文档覆盖安装、升级、备份、恢复和删除数据

---

# 十六、最终核心原则

1. 基座模型是可替换能力，不是人格本体。  
2. 人格、记忆、关系和任务状态由项目自身控制。  
3. 同一个她必须贯穿聊天、语音、文件、任务和设备。  
4. 聊天、陪伴和执行任务必须有清晰模式边界。  
5. 长期记忆必须可见、可改、可删、可追踪。  
6. 情绪系统用于适配表达，不用于制造依赖。  
7. Planner 负责理解语义，确定性代码负责安全边界。  
8. Verifier 必须基于真实证据。  
9. Executor 必须声明能力，不能假装拥有不存在的权限。  
10. 网页和手机首先是控制入口，不是本地 Shell 的复制品。  
11. 数据所在位置决定优先执行环境。  
12. Provider、语音、视觉和执行器都必须可替换。  
13. 先完成稳定 Windows Agent，再逐步加入拟人和跨设备能力。  
14. 不追求一次做完，追求每个版本可验证、可恢复、可升级。  
15. 最终目标不是功能最多，而是无论她在哪里出现，都始终是同一个她。  
