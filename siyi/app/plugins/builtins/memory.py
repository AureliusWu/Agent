"""按任务策略读取、记录和忘记工作区记忆."""

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_memory
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec(
        "list_workspace_memories",
        "列出当前工作区的工程记忆",
        "low",
        {
            "category": {
                "type": "string",
                "enum": [
                    "architecture",
                    "build_command",
                    "test_command",
                    "coding_convention",
                    "decision",
                    "known_issue",
                    "successful_fix",
                    "failed_approach",
                    "user_constraint",
                ],
            }
        },
        selection_order=39,
    ),
    ToolSpec(
        "remember_workspace",
        "保存或更新当前工作区的分类工程记忆",
        "medium",
        {
            "key": {"type": "string", "maxLength": 80},
            "content": {"type": "string", "maxLength": 4000},
            "kind": {"type": "string", "enum": ["project", "experience"]},
            "category": {
                "type": "string",
                "enum": [
                    "architecture",
                    "build_command",
                    "test_command",
                    "coding_convention",
                    "decision",
                    "known_issue",
                    "successful_fix",
                    "failed_approach",
                    "user_constraint",
                ],
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "applicable_version": {"type": "string", "maxLength": 100},
        },
        ("key", "content"),
        selection_order=40,
    ),
    ToolSpec(
        "forget_workspace_memory",
        "删除当前工作区的一条工程记忆",
        "high",
        {"key": {"type": "string", "maxLength": 80}},
        ("key",),
        selection_order=41,
    ),
)

PLUGIN = PluginDefinition(
    id="builtin.memory",
    name="工程记忆",
    description="按任务策略读取、记录和忘记工作区记忆",
    category="memory",
    tools=TOOLS,
    handler=execute_memory,
)
