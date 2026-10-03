"""原子创建、修改、复制、移动与删除."""

from dataclasses import replace
from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_workspace
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec(
        "create_file",
        "仅在目标不存在时原子创建文件",
        "medium",
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "encoding": {"type": "string", "enum": ["utf-8", "utf-8-sig", "utf-16", "gb18030"]},
        },
        ("path", "content"),
        selection_order=26,
    ),
    ToolSpec(
        "write_file",
        "原子写入文件并生成可撤销备份",
        "medium",
        {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "encoding": {
                "type": "string",
                "enum": ["auto", "utf-8", "utf-8-sig", "utf-16", "gb18030"],
            },
        },
        ("path", "content"),
        selection_order=27,
    ),
    ToolSpec(
        "replace_text",
        "精确替换文件文本并生成 diff",
        "medium",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "expected_count": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        ("path", "old_text", "new_text"),
        selection_order=28,
    ),
    ToolSpec(
        "apply_patch",
        "应用单文件 unified diff",
        "medium",
        {"path": {"type": "string"}, "patch": {"type": "string"}},
        ("path", "patch"),
        selection_order=29,
    ),
    ToolSpec(
        "copy_file",
        "复制工作区内文件",
        "medium",
        {"source": {"type": "string"}, "destination": {"type": "string"}},
        ("source", "destination"),
        selection_order=30,
    ),
    ToolSpec(
        "move_file",
        "移动或重命名工作区内文件",
        "medium",
        {"source": {"type": "string"}, "destination": {"type": "string"}},
        ("source", "destination"),
        selection_order=31,
    ),
    ToolSpec(
        "rename_file",
        "重命名工作区内文件",
        "medium",
        {"source": {"type": "string"}, "destination": {"type": "string"}},
        ("source", "destination"),
        selection_order=32,
    ),
    ToolSpec(
        "create_directory",
        "创建工作区目录",
        "medium",
        {"path": {"type": "string"}},
        ("path",),
        selection_order=33,
    ),
    ToolSpec(
        "delete_file",
        "删除单个文件并生成可撤销备份",
        "high",
        {"path": {"type": "string"}},
        ("path",),
        selection_order=34,
    ),
)

_VERSION_FIELDS = {
    "write_file": ["expected_version_token"],
    "replace_text": ["expected_version_token"],
    "apply_patch": ["expected_version_token"],
    "copy_file": ["expected_version_token", "expected_destination_version_token"],
    "move_file": ["expected_version_token", "expected_destination_version_token"],
    "rename_file": ["expected_version_token", "expected_destination_version_token"],
    "delete_file": ["expected_version_token"],
}

TOOLS = tuple(
    (
        replace(
            spec,
            properties={
                **spec.properties,
                **{name: {"type": "string"} for name in _VERSION_FIELDS[spec.name]},
            },
            required=(*spec.required, *_VERSION_FIELDS[spec.name]),
        )
        if spec.name in _VERSION_FIELDS
        else spec
    )
    for spec in TOOLS
)

PLUGIN = PluginDefinition(
    id="builtin.writing",
    name="文件写入",
    description="原子创建、修改、复制、移动与删除",
    category="write",
    tools=TOOLS,
    handler=execute_workspace,
)
