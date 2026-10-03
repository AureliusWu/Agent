"""Text-to-speech with version-bound, reversible workspace output."""

from app.plugins.contracts import PluginDefinition
from app.plugins.voice import execute_voice, synthesis_configured
from app.tools.spec import ToolSpec

PLUGIN = PluginDefinition(
    id="builtin.speaking",
    name="说 · 语音合成",
    description="把文字合成为音频文件，使用独立的默认声线",
    category="speak",
    handler=execute_voice,
    configured=synthesis_configured,
    configuration_hint="需要启用语音服务并配置地址、后端密钥、合成模型和默认声线",
    tools=(
        ToolSpec(
            "synthesize_speech",
            "通过已配置的语音服务合成语音并原子保存至工作区；调用前读取目标版本，输出可撤销",
            "critical",
            {
                "text": {"type": "string", "maxLength": 4000},
                "path": {"type": "string"},
                "format": {"type": "string", "enum": ["mp3", "wav"]},
                "expected_version_token": {"type": "string"},
                "message_id": {"type": "string", "maxLength": 128},
            },
            ("text", "path", "expected_version_token"),
            timeout_seconds=90,
            concurrency_policy="exclusive",
            interruptibility="cancel",
            selection_order=1001,
        ),
    ),
)
