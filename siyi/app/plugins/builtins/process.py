"""工作区内运行程序，支持取消和安全快照."""

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_process
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec(
        "run_command",
        "在工作区运行具体程序，不使用 shell",
        "critical",
        {
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string", "default": "."},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
        },
        ("command",),
        selection_order=38,
    ),
)

PLUGIN = PluginDefinition(
    id="builtin.process",
    name="命令执行",
    description="工作区内运行程序，支持取消和安全快照",
    category="execute",
    tools=TOOLS,
    handler=execute_process,
)
