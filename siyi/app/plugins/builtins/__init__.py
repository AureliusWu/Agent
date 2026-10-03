"""Explicit trusted composition; workspace packages never become Python imports."""

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

BUILTIN_PLUGINS = (
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
