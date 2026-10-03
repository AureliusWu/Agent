"""变更记录、快照预览和受控撤销."""

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_workspace
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec(
        "list_file_changes",
        "查看可撤销的文件变更",
        "low",
        {"task_id": {"type": "string"}},
        selection_order=23,
    ),
    ToolSpec(
        "list_security_snapshots",
        "列出当前工作区的高风险操作安全快照",
        "low",
        {"task_id": {"type": "string"}},
        selection_order=24,
    ),
    ToolSpec(
        "preview_security_snapshot",
        "预览恢复安全快照会改变的文件",
        "low",
        {"snapshot_id": {"type": "string", "maxLength": 32}},
        ("snapshot_id",),
        selection_order=25,
    ),
    ToolSpec(
        "undo_file_change",
        "撤销指定或最近一次文件变更",
        "high",
        {"change_id": {"type": "string"}},
        (),
        selection_order=35,
    ),
    ToolSpec(
        "undo_task_changes",
        "按相反顺序撤销指定任务的全部文件变更",
        "high",
        {"task_id": {"type": "string"}},
        ("task_id",),
        selection_order=36,
    ),
    ToolSpec(
        "restore_security_snapshot",
        "恢复命令执行前的工作区与 Git 状态",
        "critical",
        {"snapshot_id": {"type": "string", "maxLength": 32}},
        ("snapshot_id",),
        selection_order=37,
    ),
)

PLUGIN = PluginDefinition(
    id="builtin.history",
    name="变更与恢复",
    description="变更记录、快照预览和受控撤销",
    category="write",
    tools=TOOLS,
    handler=execute_workspace,
)
