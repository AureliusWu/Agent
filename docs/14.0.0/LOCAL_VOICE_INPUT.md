# v14 本地语音输入与隐私说明

## 使用方式

在桌面端的对话输入框中可以使用麦克风按钮：单击开始/再次单击结束，或按住“按住说话”并松开结束；`Esc` 会取消当前录音。录音期间会显示持续时长、输入音量和当前麦克风。转写完成后，文本先回到普通输入框，用户可以修改后再发送；勾选“自动发送”时才会自动进入现有任务队列。

第一次使用会由 Windows/WebView2 请求麦克风权限。权限被拒绝、设备断开或被其他程序占用时，录音会停止并显示可见错误，不会在后台继续录音。

## 本地模型与资源管理

在“本地模型与 Ollama”面板的“本地语音输入（STT）”区域可查看 Faster Whisper 运行环境、CPU/GPU 模式、模型和工作进程状态。

- 默认使用 CPU `int8`，不会抢占 GTX 1660 Super 的主要显存。
- 新安装默认推荐 `small`，用于提高中文与专名识别；`base` 继续作为速度优先选项。已有用户明确保存的模型选择不会被升级流程覆盖，模型本体不随安装包附带。
- 下载前会显示来源、预计体积、可用磁盘和目标目录，必须在确认框中确认；下载完成不会自动加载或启用。
- 模型可以显式加载、空闲后自动卸载、手动卸载或删除。删除会再次要求确认。
- CUDA 仅在用户主动启用“GPU 实验性模式”后可选。

默认模型目录为 `%LOCALAPPDATA%\AureliusWu\Agent\voice\models`（开发运行时为 `Agent-Dev`）。模型下载是可选操作；没有本地模型时，语音输入会明确提示模型缺失，而不是改用云端或付费服务。

## 音频和转写隐私

录音路径固定为：

```text
WebView2 getUserMedia
→ MediaRecorder
→ Web Audio 转为 16 kHz、单声道、16-bit PCM WAV
→ 本机 FastAPI /api/stt/transcribe
→ 本机 Faster Whisper
```

系统只接受受控的 WAV 上传，不接受任意本地路径、远程 URL 或任意输出目录。临时录音位于 `%LOCALAPPDATA%\AureliusWu\Agent\voice\tmp\<session>`，转写完成、取消、失败、关闭语音或程序退出时会删除。

这是有意的格式边界：桌面端已把麦克风数据规范化为 16 kHz、单声道、16-bit PCM WAV，安装包使用内置 WAV/NumPy 解码后将波形直接交给 Faster Whisper。安装包不携带 PyAV/FFmpeg 的通用音视频解码器；任何非规范 WAV 都会以 `STT_INVALID_AUDIO` 失败，不会回退到其他编解码器、远程服务或模拟转写。

语音会话、STT 请求和事件记录只保存状态、时长、Provider/模型和 SHA-256；不保存完整录音，也不额外保存一份 STT 转写原文。转写结果在用户确认发送，或在结果通过可靠性门槛并启用“自动发送”后，才作为普通用户消息进入现有会话、任务队列和权限链路。这条已发送的用户消息按普通会话的持久化规则保存。诊断包默认不包含录音和转写文本。

麦克风原始音频只在本机 WebView2、FastAPI 和 Faster Whisper 之间处理：不会上传到云端 STT，也不会发送给 Ollama 或任何模型 Provider。在用户确认或满足可靠性门槛的自动发送条件前，转写文本同样不会进入 Agent。发送后，转写文本才作为与手工输入相同的普通用户消息，按当前选中的 Provider 路由；若当前 Provider 是 Ollama，Ollama 收到的仅是这条已经发送的转写文本，绝不会收到原始音频或未发送的转写草稿。语音输入不会自动触发模型下载或付费调用，也不会因为“不要确认”等话语绕过现有工具权限和危险操作确认。

当前目标机固定合成语料验证中，`small` 的 CPU `int8` 性能满足门槛，但仍可能把“司忆”“夏目心”及部分中英混合术语误写。默认未启用全局热词表：虽然完整词表能提高实体命中，但真实测试出现过未说出的提示词泄漏，因此宁可保留可见的术语警告，也不以潜在幻觉换取表面准确率。

## 与 TTS 和停止操作的关系

语音输入使用半双工策略：开始录音前会停止当前 TTS 播放并清空尚未播放的队列；录音期间后端拒绝新的 TTS 播放请求。全局“停止”会共同停止录音、转写、Agent、TTS 和播放，并清理待发送的语音草稿。

## 本地 API

所有接口仅绑定本机并复用桌面端 API 令牌。主要接口为：

```text
GET  /api/stt/health
GET  /api/stt/settings
PUT  /api/stt/settings
GET  /api/stt/models
GET  /api/stt/models/{id}/download-preview
POST /api/stt/models/download
POST /api/stt/models/download/cancel
POST /api/stt/models/load
POST /api/stt/models/unload
DELETE /api/stt/models/{id}?confirmed=true
POST /api/stt/sessions
POST /api/stt/transcribe
POST /api/stt/sessions/{id}/cancel
GET  /api/voice/events?voice_session_id={id}
POST /api/voice/stop
```

常见错误码包括 `STT_MODEL_MISSING`、`STT_DOWNLOAD_CONFIRMATION_REQUIRED`、`STT_PERMISSION_DENIED`、`RECORDING_TOO_SHORT`、`STT_INVALID_AUDIO`、`STT_NO_SPEECH`、`STT_RESOURCE_LIMIT` 和 `STT_ALREADY_CANCELLED`。这些错误均应在界面中可见，不应静默改用远程服务。
