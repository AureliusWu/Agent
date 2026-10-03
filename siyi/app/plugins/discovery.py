"""Bounded capability lookup and task-local tool activation, without paid search."""

from __future__ import annotations

import re
from typing import Any

from .contracts import PluginCall
from app.tools.outcomes import RuntimeToolOutcome

MAX_ACTIVE_TOOLS = 24


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9_.]+", lowered))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", lowered):
        terms.add(phrase)
        terms.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return terms


async def execute_discovery(call: PluginCall) -> RuntimeToolOutcome:
    from .registry import PLUGIN_LIBRARY

    query = str(call.arguments["query"]).strip()
    if not query:
        return RuntimeToolOutcome(
            {
                "success": False,
                "status": "error",
                "error_code": "invalid_arguments",
                "error_message": "请说明要查找的能力",
            },
            False,
            "low",
            "plugin:builtin.discovery",
        )
    query_terms = _terms(query)
    scope = set(call.available_tool_names) if call.available_tool_names is not None else None
    category = str(call.arguments.get("category") or "")
    candidates: list[tuple[int, dict[str, Any]]] = []
    for plugin in PLUGIN_LIBRARY.catalog(call.workspace):
        if category and plugin["category"] != category:
            continue
        for tool in plugin["tools"]:
            name = tool["name"]
            session_configured = name == "web_search" and bool(
                call.search_credentials.get("tavily") or call.search_credentials.get("brave")
            )
            if name == "discover_tools" or (
                tool["status"] not in {"available", "configured"} and not session_configured
            ):
                continue
            if scope is not None and name not in scope:
                continue
            score = len(
                query_terms
                & _terms(f"{name} {tool['description']} {plugin['name']} {plugin['description']}")
            )
            score += 100 if name.lower() == query.lower() else 0
            if score:
                candidates.append(
                    (
                        score,
                        {
                            "name": name,
                            "description": tool["description"],
                            "plugin_id": plugin["id"],
                            "category": plugin["category"],
                            "risk_level": tool["risk_level"],
                        },
                    )
                )
    candidates.sort(key=lambda item: (-item[0], item[1]["name"]))
    limit = min(6, max(1, int(call.arguments.get("limit") or 4)))
    found = [item for _, item in candidates[:limit]]
    return RuntimeToolOutcome(
        {
            "success": True,
            "status": "ok",
            "tools": found,
            "tool_names": [item["name"] for item in found],
            "notice": "任务运行时将在下一轮加载允许的工具定义；发现能力不代表授权执行。未配置的服务不会加载。",
        },
        False,
        "low",
        "plugin:builtin.discovery",
    )


def activate_discovered_tools(
    loaded: list[dict[str, Any]],
    available: list[dict[str, Any]],
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Re-resolve names against trusted, profile-filtered schemas, never output schemas."""
    if not result.get("success"):
        return list(loaded), []
    names = result.get("tool_names")
    if not isinstance(names, list):
        return list(loaded), []
    from .registry import PLUGIN_LIBRARY

    trusted = {item["function"]["name"]: item for item in available}
    active = list(loaded)
    seen = {item["function"]["name"] for item in active}
    added = []
    for name in names[:6]:
        if not isinstance(name, str) or name in seen or name not in trusted:
            continue
        if not PLUGIN_LIBRARY.configured(name) or len(active) >= MAX_ACTIVE_TOOLS:
            continue
        active.append(trusted[name])
        seen.add(name)
        added.append(name)
    return active, added
