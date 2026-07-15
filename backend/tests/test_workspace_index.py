from __future__ import annotations

from pathlib import Path

from app.sandbox import execute_tool
from app.workspace_index import (
    build_workspace_index,
    find_definition,
    find_references,
    find_related_tests,
    find_symbol,
    get_call_chain,
    get_repo_map,
    inspect_diagnostics,
    list_module_dependencies,
)


def _workspace(root: Path) -> Path:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "web").mkdir()
    (root / "native").mkdir()
    (root / "src" / "service.py").write_text(
        "from pathlib import Path\n\n"
        "class Worker:\n"
        "    def run(self, value):\n"
        "        return calculate(value)\n\n"
        "def calculate(value):\n"
        "    return Path(str(value))\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_service.py").write_text(
        "from src.service import calculate\n\n"
        "def test_calculate():\n"
        "    assert calculate(1)\n",
        encoding="utf-8",
    )
    (root / "web" / "client.ts").write_text(
        "import { request } from './http'\n"
        "export interface ApiResult { ok: boolean }\n"
        "export const loadData = async () => request()\n",
        encoding="utf-8",
    )
    (root / "native" / "lib.rs").write_text(
        "use std::path::Path;\n"
        "pub struct Runner;\n"
        "impl Runner { pub fn run() { execute(); } }\n"
        "fn execute() {}\n",
        encoding="utf-8",
    )
    return root


def test_indexes_python_typescript_and_rust(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    index, cache_hit = build_workspace_index(root)

    assert cache_hit is False
    assert {item.language for item in index.files} == {"python", "typescript", "rust"}
    assert {item.name for item in index.symbols} >= {"Worker", "run", "calculate", "ApiResult", "loadData", "Runner", "execute"}
    assert any(item.target == "pathlib" for item in index.dependencies)
    assert any(item.target == "./http" for item in index.dependencies)
    assert any(item.target == "std::path::Path" for item in index.dependencies)

    cached, second_cache_hit = build_workspace_index(root)
    assert second_cache_hit is True
    assert cached is index


def test_source_fingerprint_invalidates_cache_after_nested_edit(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first, _ = build_workspace_index(root)
    source = root / "src" / "service.py"
    source.write_text(source.read_text(encoding="utf-8") + "\ndef added():\n    return 1\n", encoding="utf-8")

    second, cache_hit = build_workspace_index(root)

    assert cache_hit is False
    assert second.fingerprint != first.fingerprint
    assert any(item.name == "added" for item in second.symbols)


def test_symbol_reference_dependency_and_related_test_queries(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    assert find_symbol(root, "calc")["matches"][0]["name"] == "calculate"
    assert find_definition(root, "calculate")["total"] == 1
    assert {item["path"] for item in find_references(root, "calculate")["matches"]} == {
        "src/service.py",
        "tests/test_service.py",
    }
    assert list_module_dependencies(root, "web/client.ts")["dependencies"][0]["target"] == "./http"
    assert find_related_tests(root, path="src/service.py")["tests"][0]["path"] == "tests/test_service.py"
    assert get_call_chain(root, "calculate")["edges"][0]["caller"] == "run"


def test_repo_map_diagnostics_and_ignored_directories(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    ignored = root / "node_modules" / "package"
    ignored.mkdir(parents=True)
    (ignored / "ignored.ts").write_text("export function hidden() {}\n", encoding="utf-8")

    repo_map = get_repo_map(root)
    diagnostics = inspect_diagnostics(root)

    assert repo_map["totals"]["files"] == 5
    assert repo_map["languages"] == {"python": 3, "rust": 1, "typescript": 1}
    assert diagnostics["total"] == 1
    assert diagnostics["diagnostics"][0]["path"] == "src/broken.py"
    assert find_definition(root, "hidden")["total"] == 0


def test_workspace_index_tools_run_through_sandbox(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    repo_map = execute_tool(str(root), "full", "get_repo_map", {})
    definition = execute_tool(str(root), "full", "find_definition", {"symbol": "calculate"})

    assert repo_map["success"] is True
    assert repo_map["data"]["totals"]["files"] == 4
    assert definition["success"] is True
    assert definition["data"]["matches"][0]["path"] == "src/service.py"
