from __future__ import annotations

import re
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Iterable

from .contracts import PluginCall, PluginDefinition
from app.tools.outcomes import RuntimeToolOutcome
from app.tools.spec import ToolSpec


class PluginLibrary:
    """Immutable, explicit composition of source-controlled capabilities."""

    def __init__(self, plugins: Iterable[PluginDefinition]) -> None:
        definitions: dict[str, PluginDefinition] = {}
        owners: dict[str, PluginDefinition] = {}
        specs: dict[str, ToolSpec] = {}
        for plugin in plugins:
            if not re.fullmatch(r"builtin\.[a-z][a-z0-9_]*", plugin.id):
                raise ValueError(f"Invalid trusted plugin ID: {plugin.id}")
            if plugin.id in definitions:
                raise ValueError(f"Duplicate plugin ID: {plugin.id}")
            if not plugin.tools or not callable(plugin.handler):
                raise ValueError(f"Plugin requires tools and a handler: {plugin.id}")
            definitions[plugin.id] = plugin
            for spec in plugin.tools:
                if spec.name in specs:
                    raise ValueError(f"Duplicate plugin tool: {spec.name}")
                if spec.plugin_id and spec.plugin_id != plugin.id:
                    raise ValueError(f"Tool owner mismatch: {spec.name}")
                owners[spec.name] = plugin
                specs[spec.name] = replace(spec, plugin_id=plugin.id)
        self.plugins = MappingProxyType(definitions)
        self._owners = MappingProxyType(owners)
        self._specs = MappingProxyType(specs)

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        # Preserve the established selection order and model-context limits.
        return tuple(sorted(self._specs.values(), key=lambda spec: spec.selection_order))

    def owner(self, tool: str) -> PluginDefinition | None:
        return self._owners.get(tool)

    def configured(self, tool: str) -> bool:
        owner = self.owner(tool)
        return owner is not None and (owner.configured is None or owner.configured())

    def catalog(self, workspace: str = "") -> list[dict[str, Any]]:
        result = []
        for plugin in self.plugins.values():
            configured = plugin.configured is None or plugin.configured()
            if not configured:
                status, reason = "unconfigured", plugin.configuration_hint
            elif plugin.requires_workspace and not workspace.strip():
                status, reason = "requires_workspace", "请主动选择工作区"
            elif plugin.configured is not None:
                status, reason = "configured", "已配置；服务能力需通过实际调用验证"
            else:
                status, reason = "available", "使用现有受控执行器"
            result.append(
                {
                    "id": plugin.id,
                    "name": plugin.name,
                    "description": plugin.description,
                    "category": plugin.category,
                    "version": plugin.version,
                    "source": "builtin",
                    "status": status,
                    "reason": reason,
                    "requires_workspace": plugin.requires_workspace,
                    "tools": [
                        {
                            **self._specs[spec.name].catalog(),
                            "status": (
                                "unconfigured"
                                if plugin.tool_configuration
                                and not plugin.tool_configuration(spec.name)
                                else status
                            ),
                        }
                        for spec in plugin.tools
                    ],
                }
            )
        return result

    async def execute(self, call: PluginCall) -> RuntimeToolOutcome:
        from app.tools.registry import ToolValidationError, validate_arguments

        plugin = self.owner(call.name)
        spec = self._specs.get(call.name)
        try:
            validate_arguments(call.name, call.arguments)
        except ToolValidationError as exc:
            return RuntimeToolOutcome(
                {
                    "success": False,
                    "status": "error",
                    "error_code": "invalid_arguments",
                    "error_message": str(exc),
                },
                False,
                spec.risk if spec else "critical",
                "builtin",
            )
        if plugin is None or spec is None:
            raise RuntimeError("Validated tool has no trusted plugin owner")
        if not self.configured(call.name):
            return RuntimeToolOutcome(
                {
                    "success": False,
                    "status": "unavailable",
                    "error_code": "plugin_unconfigured",
                    "error_message": plugin.configuration_hint,
                },
                False,
                spec.risk,
                f"plugin:{plugin.id}",
            )
        return await plugin.handler(call)


from .builtins import BUILTIN_PLUGINS

PLUGIN_LIBRARY = PluginLibrary(BUILTIN_PLUGINS)
