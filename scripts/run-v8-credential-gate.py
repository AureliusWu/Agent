from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "build" / "v8-evidence" / "credential-manager-gate.json"


def run(command: list[str], cwd: Path) -> dict:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return {
        "command": command,
        "return_code": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
        "output_tail": output[-2_000:],
    }


def main() -> int:
    checks = [
        run(
            [
                "cargo",
                "test",
                "provider_secret_round_trips_through_windows_credential_manager",
                "--",
                "--nocapture",
            ],
            ROOT / "desktop" / "src-tauri",
        ),
        run(
            [
                str(ROOT / "siyi" / ".venv" / "Scripts" / "python.exe"),
                "scripts/privacy_scan.py",
                "--scan-path",
                ".",
            ],
            ROOT,
        ),
    ]
    build = json.loads(
        (ROOT / "build" / "generated" / "build-info.json").read_text(
            encoding="utf-8"
        )
    )
    payload = {
        "schema_version": 1,
        "status": (
            "passed"
            if all(check["status"] == "passed" for check in checks)
            else "failed"
        ),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "build_id": build["build_id"],
        "uses_synthetic_credential_only": True,
        "credential_deleted_after_test": checks[0]["status"] == "passed",
        "checks": checks,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": payload["status"], "report": str(REPORT)}))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
