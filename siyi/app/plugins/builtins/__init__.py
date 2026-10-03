"""Explicit trusted composition; workspace packages never become Python imports."""

from dataclasses import replace

from app.plugins.contracts import PluginDefinition
from app.plugins.handlers import execute_legacy_builtin
from app.tools.spec import V16_SPECS

from . import (
    reading,
    writing,
    code,
    history,
    worktrees,
    process,
    memory,
    web,
    hearing,
    speaking,
    discovery,
)

_DECLARED_PLUGINS = (
    reading.PLUGIN,
    writing.PLUGIN,
    code.PLUGIN,
    history.PLUGIN,
    worktrees.PLUGIN,
    process.PLUGIN,
    memory.PLUGIN,
    web.PLUGIN,
    hearing.PLUGIN,
    speaking.PLUGIN,
    discovery.PLUGIN,
)

# Cloud v8 modules describe ownership; v16 remains authoritative for existing
# schemas, risk, version checks and stable selection order.  Existing execution
# stays on the mature runtime/file-operation path rather than the old adapters.
_canonical = {spec.name: spec for spec in V16_SPECS}
_claimed = {spec.name for plugin in _DECLARED_PLUGINS for spec in plugin.tools}
_additional = {plugin.id: [] for plugin in _DECLARED_PLUGINS}
_artifact_specs = []
_vision_specs = []
for _spec in V16_SPECS:
    if _spec.name in _claimed:
        continue
    if _spec.name.startswith("artifact."):
        _artifact_specs.append(_spec)
    elif _spec.name.startswith("vision."):
        _vision_specs.append(_spec)
    elif _spec.name in {"undo_file_batch"}:
        _additional["builtin.history"].append(_spec)
    elif _spec.name in {"delete_directory", "file_batch"}:
        _additional["builtin.writing"].append(_spec)
    else:
        raise ValueError(f"Unassigned v16 tool: {_spec.name}")

BUILTIN_PLUGINS = tuple(
    replace(
        plugin,
        tools=tuple(_canonical.get(spec.name, spec) for spec in plugin.tools)
        + tuple(_additional[plugin.id]),
        handler=(
            plugin.handler
            if plugin.id in {"builtin.hearing", "builtin.speaking", "builtin.discovery"}
            else execute_legacy_builtin
        ),
    )
    for plugin in _DECLARED_PLUGINS
) + (
    PluginDefinition(
        id="builtin.artifacts", name="文档产物", description="沿用受控文档生成、编辑、渲染与验证",
        category="write", tools=tuple(_artifact_specs), handler=execute_legacy_builtin,
    ),
    PluginDefinition(
        id="builtin.vision", name="视觉分析", description="沿用独立视觉权限和供应商边界",
        category="read", tools=tuple(_vision_specs), handler=execute_legacy_builtin,
    ),
)
