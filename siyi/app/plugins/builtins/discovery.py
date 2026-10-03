"""The model's small, read-only entry point into the trusted capability catalog."""

from app.plugins.contracts import PluginDefinition
from app.plugins.discovery import execute_discovery
from app.tools.spec import ToolSpec

PLUGIN = PluginDefinition(
    id="builtin.discovery",
    name="能力发现",
    description="按任务查找已配置且允许使用的工具，在后续请求中加载定义",
    category="read",
    handler=execute_discovery,
    tools=(
        ToolSpec(
            "discover_tools",
            "当当前工具不够用时，描述所需能力以查找工具；仅发现任务允许且已配置的内置工具，不授予执行权限",
            "low",
            {
                "query": {"type": "string", "maxLength": 160},
                "limit": {"type": "integer", "minimum": 1, "maximum": 6},
                "category": {
                    "type": "string",
                    "enum": ["hear", "speak", "read", "write", "execute", "memory"],
                },
            },
            ("query",),
            selection_order=-1,
            concurrency_policy="serial",
        ),
    ),
)
