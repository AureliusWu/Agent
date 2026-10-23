"""Real temporary files prove internal backups stay out of read-only discovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.permissions import PermissionDecision
from app.sandbox import execute_tool


_QUERY = "recovery-guard-needle"
_PRIVATE_MARKER = "PRIVATE_BACKUP_CONTENT_MUST_NOT_BE_DISCOVERED"
_BACKUP_NAMES = (".agent-backups", ".AGENT-BACKUPS", ".AgEnT-BaCkUpS")


def _allow(**_kwargs: Any) -> PermissionDecision:
    return PermissionDecision(allowed=True, confirmed=False)


def _discover(workspace: Path, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return execute_tool(
        str(workspace), "readonly", tool, arguments, permission_fn=_allow,
    )


@pytest.mark.parametrize("tool", ["search_files", "search_text"])
@pytest.mark.parametrize("backup_name", _BACKUP_NAMES)
@pytest.mark.parametrize("parent_name", [".", "sources"])
def test_root_search_never_exposes_mixed_case_backup_names_or_content(
    tmp_path: Path, tool: str, backup_name: str, parent_name: str,
) -> None:
    public = tmp_path / f"{_QUERY}-public.txt"
    public.write_text(f"{_QUERY}: ordinary workspace content", encoding="utf-8")
    backup = tmp_path / parent_name / backup_name / "owned-change"
    backup.mkdir(parents=True)
    private = backup / f"{_QUERY}-private.txt"
    private_bytes = f"{_QUERY}: {_PRIVATE_MARKER}".encode("utf-8")
    private.write_bytes(private_bytes)

    result = _discover(tmp_path, tool, {"query": _QUERY, "path": "."})

    assert result["success"] is True
    assert result["truncated"] is False
    assert {match["path"] for match in result["matches"]} == {public.name}
    assert any(match["line"] == 1 for match in result["matches"])
    serialized = json.dumps(result, ensure_ascii=False)
    assert _PRIVATE_MARKER not in serialized
    assert backup_name not in serialized
    assert private.read_bytes() == private_bytes


@pytest.mark.parametrize("tool", ["search_files", "search_text"])
@pytest.mark.parametrize("backup_name", _BACKUP_NAMES)
def test_search_from_ordinary_subdirectory_still_prunes_mixed_case_backups(
    tmp_path: Path, tool: str, backup_name: str,
) -> None:
    parent = tmp_path / "sources"
    parent.mkdir()
    public = parent / f"{_QUERY}-public.txt"
    public.write_text(_QUERY, encoding="utf-8")
    backup = parent / backup_name
    backup.mkdir()
    private = backup / f"{_QUERY}-private.txt"
    private.write_text(f"{_QUERY}: {_PRIVATE_MARKER}", encoding="utf-8")

    result = _discover(tmp_path, tool, {"query": _QUERY, "path": "sources"})

    assert result["success"] is True
    assert {match["path"] for match in result["matches"]} == {
        str(public.relative_to(tmp_path)),
    }
    assert _PRIVATE_MARKER not in json.dumps(result, ensure_ascii=False)
    assert private.read_text(encoding="utf-8") == f"{_QUERY}: {_PRIVATE_MARKER}"


@pytest.mark.parametrize("tool", ["list_files", "list_directory"])
@pytest.mark.parametrize("backup_name", _BACKUP_NAMES)
@pytest.mark.parametrize("parent_name", [".", "sources"])
def test_directory_listing_hides_mixed_case_backup_directory(
    tmp_path: Path, tool: str, backup_name: str, parent_name: str,
) -> None:
    parent = tmp_path / parent_name
    parent.mkdir(exist_ok=True)
    backup = parent / backup_name
    backup.mkdir()
    private = backup / "private.txt"
    private.write_text(_PRIVATE_MARKER, encoding="utf-8")
    (parent / "visible").mkdir()
    (parent / "visible.txt").write_text("ordinary workspace content", encoding="utf-8")

    result = _discover(tmp_path, tool, {"path": parent_name})

    assert result["success"] is True
    assert {item["name"] for item in result["items"]} == {"visible", "visible.txt"}
    assert backup_name not in json.dumps(result, ensure_ascii=False)
    assert private.read_text(encoding="utf-8") == _PRIVATE_MARKER


@pytest.mark.parametrize("tool", ["search_files", "search_text"])
@pytest.mark.parametrize("directory_name", [
    ".GiT", "NoDe_MoDuLeS", "DiSt", "BuIlD", "TaRgEt", "__PyCaChE__", ".VeNv", "VeNv",
])
def test_search_keeps_existing_ignored_directory_set_case_insensitive(
    tmp_path: Path, tool: str, directory_name: str,
) -> None:
    ignored = tmp_path / directory_name
    ignored.mkdir()
    private = ignored / "generated.txt"
    private.write_text(_QUERY, encoding="utf-8")
    public = tmp_path / "ordinary.txt"
    public.write_text(_QUERY, encoding="utf-8")

    result = _discover(tmp_path, tool, {"query": _QUERY})

    assert result["success"] is True
    assert {match["path"] for match in result["matches"]} == {public.name}
    assert private.read_text(encoding="utf-8") == _QUERY


@pytest.mark.parametrize("tool", ["search_files", "search_text"])
@pytest.mark.parametrize("directory_name", [".agent-backups-old", ".agent-backup"])
def test_search_does_not_hide_similar_but_nonreserved_directory_names(
    tmp_path: Path, tool: str, directory_name: str,
) -> None:
    ordinary = tmp_path / directory_name
    ordinary.mkdir()
    public = ordinary / "ordinary.txt"
    public.write_text(_QUERY, encoding="utf-8")

    result = _discover(tmp_path, tool, {"query": _QUERY})

    assert result["success"] is True
    assert {match["path"] for match in result["matches"]} == {
        str(public.relative_to(tmp_path)),
    }


@pytest.mark.parametrize("tool", ["list_files", "list_directory"])
def test_listing_keeps_nonreserved_directories_visible(tmp_path: Path, tool: str) -> None:
    names = {".agent-backups-old", ".agent-backup", ".GiT", "BuIlD", "ordinary"}
    for name in names:
        (tmp_path / name).mkdir()

    result = _discover(tmp_path, tool, {"path": "."})

    assert result["success"] is True
    assert {item["name"] for item in result["items"]} == names
