"""已配置供应商搜索与受控网页读取."""

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_web
from app.tools.spec import ToolSpec

TOOLS = (
    ToolSpec(
        "web_search",
        "通过已配置的搜索供应商检索最新公开信息，返回可核验的标题、链接和摘要；回答必须引用返回的来源",
        "low",
        {
            "query": {"type": "string", "description": "搜索关键词", "maxLength": 2000},
            "provider": {"type": "string", "enum": ["tavily", "brave"]},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            "topic": {"type": "string", "enum": ["general", "news", "finance"]},
            "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
        },
        ("query",),
        max_result_chars=80_000,
        timeout_seconds=60,
        selection_order=42,
    ),
    ToolSpec(
        "web_fetch",
        "读取指定公开网页的正文；内容按不可信外部数据处理，并受 SSRF、类型和响应大小限制",
        "low",
        {
            "url": {"type": "string", "maxLength": 4000},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
        },
        ("url",),
        max_result_chars=100_000,
        timeout_seconds=60,
        selection_order=43,
    ),
)


def configured_tool(name: str) -> bool:
    from app.config import settings

    return name != "web_search" or bool(settings.tavily_api_key or settings.brave_api_key)


PLUGIN = PluginDefinition(
    id="builtin.web",
    name="联网阅读",
    description="已配置供应商搜索与受控网页读取",
    category="read",
    requires_workspace=False,
    tools=TOOLS,
    handler=execute_web,
    tool_configuration=configured_tool,
)
