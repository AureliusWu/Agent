from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "desktop" / "frontend"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the v8 frontend gate and write durable evidence")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "v8-evidence" / "frontend-gate.json",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    commands = [
        ["npm.cmd", "run", "lint"],
        ["npm.cmd", "run", "build"],
        ["npm.cmd", "run", "test:security"],
        ["npm.cmd", "run", "test:build-info"],
    ]
    results: list[dict[str, object]] = []
    status = "passed"
    for command in commands:
        result = subprocess.run(
            command,
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        combined = (result.stdout + "\n" + result.stderr).strip()
        results.append(
            {
                "command": command,
                "return_code": result.returncode,
                "status": "passed" if result.returncode == 0 else "failed",
                "output_tail": combined[-4_000:],
            }
        )
        if result.returncode != 0:
            status = "failed"
            break

    payload = {
        "schema_version": 1,
        "status": status,
        "recorded_at": utc_now(),
        "working_directory": "desktop/frontend",
        "commands": results,
    }
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "report": str(output)}, ensure_ascii=True))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
