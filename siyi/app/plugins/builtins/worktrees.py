"""管理工作区内的受控 Git 工作树."""

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_workspace
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec("list_worktrees", "列出当前 Git 仓库的受控工作树", "low", {}, selection_order=20),
    ToolSpec(
        "create_worktree",
        "在工作区 .agent/worktrees 中创建隔离的 Git 工作树",
        "high",
        {
            "name": {"type": "string", "maxLength": 80},
            "ref": {"type": "string", "maxLength": 200},
            "branch": {"type": "string", "maxLength": 200},
        },
        ("name",),
        selection_order=21,
    ),
    ToolSpec(
        "remove_worktree",
        "移除由司忆管理的隔离 Git 工作树",
        "critical",
        {
            "name": {"type": "string", "maxLength": 80},
            "force": {"type": "boolean", "default": False},
        },
        ("name",),
        selection_order=22,
    ),
)

PLUGIN = PluginDefinition(
    id="builtin.worktrees",
    name="隔离工作树",
    description="管理工作区内的受控 Git 工作树",
    category="execute",
    tools=TOOLS,
    handler=execute_workspace,
)
