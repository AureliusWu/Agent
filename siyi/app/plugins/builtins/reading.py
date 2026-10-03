"""按范围读取、搜索、比较工作区文件."""

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_workspace
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec(
        "list_files",
        "列出工作区目录",
        "low",
        {"path": {"type": "string", "default": "."}},
        selection_order=0,
    ),
    ToolSpec(
        "list_directory",
        "列出工作区目录",
        "low",
        {"path": {"type": "string", "default": "."}},
        selection_order=1,
    ),
    ToolSpec(
        "search_files",
        "搜索工作区文件名与文本内容",
        "low",
        {
            "query": {"type": "string", "maxLength": 1000},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "string", "default": "*"},
            "regex": {"type": "boolean", "default": False},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        ("query",),
        selection_order=2,
    ),
    ToolSpec(
        "search_text",
        "搜索工作区文本内容",
        "low",
        {
            "query": {"type": "string", "maxLength": 1000},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "string", "default": "*"},
            "regex": {"type": "boolean", "default": False},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        ("query",),
        selection_order=3,
    ),
    ToolSpec(
        "read_file",
        "分段读取工作区文本文件",
        "low",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
            "encoding": {
                "type": "string",
                "enum": ["auto", "utf-8", "utf-8-sig", "utf-16", "gb18030"],
            },
        },
        ("path",),
        selection_order=4,
    ),
    ToolSpec(
        "read_file_range",
        "按行范围读取工作区文本文件",
        "low",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
            "encoding": {
                "type": "string",
                "enum": ["auto", "utf-8", "utf-8-sig", "utf-16", "gb18030"],
            },
        },
        ("path", "start_line", "end_line"),
        selection_order=5,
    ),
    ToolSpec(
        "file_metadata",
        "查看文件元数据与编码",
        "low",
        {"path": {"type": "string"}},
        ("path",),
        selection_order=6,
    ),
    ToolSpec(
        "file_info",
        "查看文件元数据与编码",
        "low",
        {"path": {"type": "string"}},
        ("path",),
        selection_order=7,
    ),
    ToolSpec(
        "file_diff",
        "预览写入内容与现有文件的差异",
        "low",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ("path", "content"),
        selection_order=8,
    ),
    ToolSpec(
        "view_diff",
        "预览写入内容与现有文件的差异",
        "low",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ("path", "content"),
        selection_order=9,
    ),
    ToolSpec(
        "compare_files",
        "比较工作区内两个文本文件",
        "low",
        {"left": {"type": "string"}, "right": {"type": "string"}},
        ("left", "right"),
        selection_order=10,
    ),
)

PLUGIN = PluginDefinition(
    id="builtin.reading",
    name="阅读与检索",
    description="按范围读取、搜索、比较工作区文件",
    category="read",
    tools=TOOLS,
    handler=execute_workspace,
)
