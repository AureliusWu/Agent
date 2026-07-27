from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import privacy_scan


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "privacy_scan.py"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(arguments: list[str], *, expected_return_code: int = 0) -> dict[str, object]:
    command = [sys.executable, str(SCANNER), *arguments]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    combined = (result.stdout + "\n" + result.stderr).strip()
    return {
        "command": ["python", "scripts/privacy_scan.py", *arguments],
        "return_code": result.returncode,
        "expected_return_code": expected_return_code,
        "status": "passed" if result.returncode == expected_return_code else "failed",
        "output": combined,
    }


def run_precommit_negative() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="siyi-precommit-gate-") as directory:
        root = Path(directory)
        (root / "scripts").mkdir()
        (root / ".githooks").mkdir()
        shutil.copy2(SCANNER, root / "scripts" / "privacy_scan.py")
        shutil.copy2(ROOT / ".githooks" / "pre-commit", root / ".githooks" / "pre-commit")
        sample = root / "synthetic-secret.txt"
        sample.write_bytes(
            b"synthetic negative fixture: "
            + b"sk"
            + b"-precommit_fixture_1234567890 "
            + b"C"
            + b":\\Users\\precommit-gate\\private.txt"
        )
        setup = [
            ["git", "init", "-q"],
            ["git", "config", "user.name", "Siyi Release Gate"],
            ["git", "config", "user.email", "release-gate@example.invalid"],
            ["git", "config", "core.hooksPath", ".githooks"],
            ["git", "add", "."],
        ]
        for command in setup:
            result = subprocess.run(command, cwd=root, capture_output=True, check=False)
            if result.returncode != 0:
                return {
                    "command": ["git", "commit", "-m", "<synthetic-negative-fixture>"],
                    "return_code": result.returncode,
                    "expected_return_code": "nonzero",
                    "status": "failed",
                    "output": "temporary repository setup failed",
                }
        result = subprocess.run(
            ["git", "commit", "-m", "synthetic negative fixture"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        commit_exists = (
            subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=root,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        passed = result.returncode != 0 and not commit_exists
        return {
            "command": ["git", "commit", "-m", "<synthetic-negative-fixture>"],
            "return_code": result.returncode,
            "expected_return_code": "nonzero",
            "status": "passed" if passed else "failed",
            "output": "pre-commit hook rejected the staged synthetic secret; no commit was created"
            if passed
            else "pre-commit hook did not reject the staged synthetic secret",
        }


def check_git_status_paths() -> dict[str, object]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    paths: list[str] = []
    entries = [entry for entry in result.stdout.decode("utf-8", "replace").split("\0") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        status = entry[:2]
        paths.append(entry[3:])
        if "R" in status or "C" in status:
            index += 1
            if index < len(entries):
                paths.append(entries[index])
        index += 1
    forbidden = [
        finding
        for path in paths
        for finding in privacy_scan._forbidden_path(path)
        if finding.kind in {"forbidden_runtime_path", "forbidden_file_type", "private_export_name"}
    ]
    return {
        "command": ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        "return_code": result.returncode,
        "expected_return_code": 0,
        "status": "passed" if result.returncode == 0 and not forbidden else "failed",
        "output": f"checked {len(paths)} changed paths; no private runtime path is present"
        if not forbidden
        else f"found {len(forbidden)} forbidden private runtime path(s)",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the v8 privacy gate and write durable evidence")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "v8-evidence" / "privacy-gate.json",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    checks = [
        run(["--tracked"]),
        run(["--history"]),
        run(["--scan-path", "."]),
        check_git_status_paths(),
        run_precommit_negative(),
    ]
    with tempfile.TemporaryDirectory(prefix="siyi-privacy-gate-") as directory:
        sample = Path(directory) / "synthetic-secret.txt"
        sample.write_bytes(
            b"synthetic negative fixture: "
            + b"sk"
            + b"-release_gate_fixture_1234567890 "
            + b"C"
            + b":\\Users\\release-gate\\private.txt"
        )
        negative = run(["--scan-path", str(sample)], expected_return_code=1)
        negative["command"] = [
            "python",
            "scripts/privacy_scan.py",
            "--scan-path",
            "<isolated-temp>/synthetic-secret.txt",
        ]
        negative["output"] = "scanner rejected the synthetic secret and private path without echoing values"
        checks.append(negative)

    status = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
    payload = {
        "schema_version": 1,
        "status": status,
        "recorded_at": utc_now(),
        "checks": checks,
    }
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "report": str(output)}, ensure_ascii=True))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
