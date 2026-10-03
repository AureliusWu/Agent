"""Read-only catalog for trusted capability modules."""

from fastapi import APIRouter, HTTPException

from app.plugins.registry import PLUGIN_LIBRARY

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


@router.get("")
def plugin_library(workspace: str = "") -> dict:
    plugins = PLUGIN_LIBRARY.catalog(workspace)
    return {"schema_version": 1, "plugins": plugins, "tool_count": len(PLUGIN_LIBRARY.tool_specs())}


@router.get("/{plugin_id}")
def plugin_details(plugin_id: str, workspace: str = "") -> dict:
    for plugin in PLUGIN_LIBRARY.catalog(workspace):
        if plugin["id"] == plugin_id:
            return plugin
    raise HTTPException(404, "插件不存在")
