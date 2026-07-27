from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _toml(path: str) -> dict:
    with (ROOT / path).open("rb") as handle:
        return tomllib.load(handle)


def _python_version() -> str:
    tree = ast.parse((ROOT / "siyi/app/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            return str(ast.literal_eval(node.value))
    raise RuntimeError("siyi/app/__init__.py does not define __version__")


def collected_versions() -> dict[str, str]:
    cargo_lock = _toml("desktop/src-tauri/Cargo.lock")
    app_lock = next(item for item in cargo_lock["package"] if item["name"] == "app")
    return {
        "backend package": str(_toml("siyi/pyproject.toml")["project"]["version"]),
        "backend runtime": _python_version(),
        "frontend package": str(_json("desktop/frontend/package.json")["version"]),
        "frontend lock": str(_json("desktop/frontend/package-lock.json")["packages"][""]["version"]),
        "Tauri config": str(_json("desktop/src-tauri/tauri.conf.json")["version"]),
        "Cargo package": str(_toml("desktop/src-tauri/Cargo.toml")["package"]["version"]),
        "Cargo lock": str(app_lock["version"]),
    }


def main() -> int:
    expected = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    versions = collected_versions()
    mismatches = {name: value for name, value in versions.items() if value != expected}
    if mismatches:
        print(f"Release metadata does not match VERSION={expected}:", file=sys.stderr)
        for name, value in mismatches.items():
            print(f"- {name}: {value}", file=sys.stderr)
        return 1
    print(f"Release metadata is consistent: {expected} ({len(versions)} sources checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
