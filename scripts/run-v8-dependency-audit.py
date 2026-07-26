from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(name: str, command: list[str], cwd: Path) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stdout = result.stdout.strip()
    try:
        report: object = json.loads(stdout)
    except json.JSONDecodeError:
        report = {"output_tail": (stdout + "\n" + result.stderr.strip())[-8_000:]}
    return {
        "name": name,
        "command": command,
        "return_code": result.returncode,
        "status": "passed" if result.returncode == 0 else "failed",
        "report": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit v8 Python, npm, and Rust locked dependencies")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "v8-evidence" / "dependency-audit.json",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    cargo_audit = ROOT / "build" / "tools" / "cargo-audit" / "bin" / "cargo-audit.exe"
    rust_audit = (
        run(
            "rust",
            [
                str(cargo_audit),
                "audit",
                "--json",
                "--target-os",
                "windows",
                "--target-arch",
                "x86_64",
            ],
            ROOT / "desktop" / "src-tauri",
        )
        if cargo_audit.is_file()
        else {
            "name": "rust",
            "command": ["cargo-audit", "audit", "--json"],
            "return_code": None,
            "status": "blocked",
            "report": {"reason": "cargo-audit is unavailable"},
        }
    )
    if rust_audit["status"] == "passed":
        tree = subprocess.run(
            ["cargo", "tree", "--locked", "--target", "x86_64-pc-windows-msvc"],
            cwd=ROOT / "desktop" / "src-tauri",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        report = rust_audit["report"]
        warning_packages: set[str] = set()
        if isinstance(report, dict):
            warnings = report.get("warnings")
            if isinstance(warnings, dict):
                for kind, items in warnings.items():
                    if kind not in {"unsound", "yanked"}:
                        continue
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, dict):
                            package = item.get("package")
                            if isinstance(package, dict) and package.get("name"):
                                warning_packages.add(str(package["name"]))
        reachable = sorted(
            package
            for package in warning_packages
            if any(
                line.lstrip(" │├└─").startswith(f"{package} v")
                for line in tree.stdout.splitlines()
            )
        )
        rust_audit["windows_target_reachability"] = {
            "command": ["cargo", "tree", "--locked", "--target", "x86_64-pc-windows-msvc"],
            "return_code": tree.returncode,
            "warning_packages": sorted(warning_packages),
            "reachable_warning_packages": reachable,
            "status": "passed" if tree.returncode == 0 and not reachable else "failed",
        }
        if tree.returncode != 0 or reachable:
            rust_audit["status"] = "failed"

    audits = [
        run(
            "python",
            ["uvx", "pip-audit", "-r", "requirements.lock", "--format", "json"],
            ROOT / "siyi",
        ),
        run(
            "npm-production",
            [
                "npm.cmd",
                "audit",
                "--omit=dev",
                "--json",
                "--registry=https://registry.npmjs.org/",
            ],
            ROOT / "desktop" / "frontend",
        ),
        rust_audit,
    ]
    status = "passed" if all(item["status"] == "passed" for item in audits) else "failed"
    payload = {
        "schema_version": 1,
        "status": status,
        "recorded_at": utc_now(),
        "audits": audits,
    }
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "report": str(output)}, ensure_ascii=True))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
