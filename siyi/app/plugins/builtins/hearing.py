"""Audio-file transcription with explicit outbound approval."""

from app.plugins.contracts import PluginDefinition
from app.plugins.voice import execute_voice, transcription_configured
from app.tools.spec import ToolSpec

PLUGIN = PluginDefinition(
    id="builtin.hearing",
    name="听 · 语音识别",
    description="将工作区音频转写为文字，保留来源与消息关联",
    category="hear",
    handler=execute_voice,
    configured=transcription_configured,
    configuration_hint="需要启用语音服务并配置地址、后端密钥和转写模型",
    tools=(
        ToolSpec(
            "transcribe_audio",
            "将工作区音频发往已配置的语音服务进行转写；外发音频需要明确批准",
            "critical",
            {
                "path": {"type": "string"},
                "language": {"type": "string", "maxLength": 12},
                "message_id": {"type": "string", "maxLength": 128},
            },
            ("path",),
            timeout_seconds=90,
            concurrency_policy="exclusive",
            interruptibility="cancel",
            idempotent=False,
            selection_order=1000,
        ),
    ),
)
