from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete v8 backend gate")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "v8-evidence" / "backend-gate.json",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/backend",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    collection = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/backend",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    nodeids = [
        line.strip()
        for line in collection.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    ]
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
    summary_match = re.search(
        r"(?P<passed>\d+) passed(?:, (?P<skipped>\d+) skipped)?(?:, (?P<warnings>\d+) warnings?)?",
        combined,
    )
    payload = {
        "schema_version": 1,
        "status": "passed" if result.returncode == 0 else "failed",
        "recorded_at": utc_now(),
        "command": ["python", "-m", "pytest", "tests/backend", "-q", "-p", "no:cacheprovider"],
        "return_code": result.returncode,
        "collection_return_code": collection.returncode,
        "collected_nodeids": nodeids,
        "summary": {
            "passed": int(summary_match.group("passed")) if summary_match else None,
            "skipped": int(summary_match.group("skipped") or 0) if summary_match else None,
            "warnings": int(summary_match.group("warnings") or 0) if summary_match else None,
        },
        "output_tail": combined[-8_000:],
    }
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "report": str(output)}, ensure_ascii=True))
    if collection.returncode != 0:
        return collection.returncode
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
