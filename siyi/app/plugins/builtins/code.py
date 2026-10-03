"""代码索引、符号导航、依赖与语言服务器."""

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_code
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec(
        "get_repo_map",
        "获取工作区代码地图、语言构成、索引统计和 Git 变更",
        "low",
        {},
        selection_order=11,
    ),
    ToolSpec(
        "find_symbol",
        "按名称查找 Python、TypeScript、JavaScript 或 Rust 符号",
        "low",
        {
            "query": {"type": "string", "maxLength": 200},
            "exact": {"type": "boolean", "default": False},
            "kind": {"type": "string", "maxLength": 40},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        ("query",),
        selection_order=12,
    ),
    ToolSpec(
        "find_definition",
        "查找符号定义位置",
        "low",
        {
            "symbol": {"type": "string", "maxLength": 200},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ("symbol",),
        selection_order=13,
    ),
    ToolSpec(
        "find_references",
        "查找符号调用和读取位置",
        "low",
        {
            "symbol": {"type": "string", "maxLength": 200},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        ("symbol",),
        selection_order=14,
    ),
    ToolSpec(
        "list_module_dependencies",
        "查看工作区模块导入依赖",
        "low",
        {
            "path": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        selection_order=15,
    ),
    ToolSpec(
        "find_related_tests",
        "根据源文件或符号查找相关测试",
        "low",
        {
            "path": {"type": "string"},
            "symbol": {"type": "string", "maxLength": 200},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        selection_order=16,
    ),
    ToolSpec(
        "get_call_chain",
        "向上追踪符号调用链",
        "low",
        {
            "symbol": {"type": "string", "maxLength": 200},
            "depth": {"type": "integer", "minimum": 1, "maximum": 8},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        ("symbol",),
        selection_order=17,
    ),
    ToolSpec(
        "inspect_diagnostics",
        "查看代码索引发现的解析诊断",
        "low",
        {
            "path": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        selection_order=18,
    ),
    ToolSpec(
        "lsp_query",
        "通过语言服务器查询定义、引用或文档符号；无服务器时安全降级到工作区索引",
        "low",
        {
            "path": {"type": "string"},
            "operation": {"type": "string", "enum": ["definition", "references", "symbols"]},
            "line": {"type": "integer", "minimum": 0},
            "character": {"type": "integer", "minimum": 0},
            "symbol": {"type": "string", "maxLength": 200},
        },
        ("path", "operation"),
        selection_order=19,
    ),
)

PLUGIN = PluginDefinition(
    id="builtin.code",
    name="代码理解",
    description="代码索引、符号导航、依赖与语言服务器",
    category="read",
    tools=TOOLS,
    handler=execute_code,
)
