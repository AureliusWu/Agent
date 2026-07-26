from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST = ROOT / "tests" / "backend" / "runtime" / "test_autonomous_runtime_stress.py"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic durable-runtime stress test as a timed soak")
    parser.add_argument("--duration-seconds", type=int, default=1_800)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "soak" / "latest.json")
    parser.add_argument("--test", type=Path, default=DEFAULT_TEST)
    args = parser.parse_args()
    if args.duration_seconds <= 0:
        parser.error("--duration-seconds must be positive")

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    started = time.monotonic()
    deadline = started + args.duration_seconds
    iterations: list[dict[str, object]] = []
    status = "passed"

    while not iterations or time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if iterations and remaining < float(iterations[-1]["duration_seconds"]):
            break
        iteration = len(iterations) + 1
        basetemp = output.parent / f"pytest-{iteration:04d}"
        command = [
            sys.executable,
            "-m",
            "pytest",
            str(args.test.resolve()),
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "--basetemp",
            str(basetemp),
        ]
        iteration_started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT / "siyi", capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        duration = round(time.monotonic() - iteration_started, 3)
        combined = (result.stdout + "\n" + result.stderr).strip()
        iterations.append(
            {
                "iteration": iteration,
                "status": "passed" if result.returncode == 0 else "failed",
                "return_code": result.returncode,
                "duration_seconds": duration,
                "output_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
                "output_tail": combined[-1_000:],
            }
        )
        if result.returncode != 0:
            status = "failed"
            break

    payload = {
        "schema_version": 1,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "requested_duration_seconds": args.duration_seconds,
        "actual_duration_seconds": round(time.monotonic() - started, 3),
        "iterations": iterations,
        "synthetic_load_per_iteration": {
            "execution_segments": 1_000,
            "model_loops": 10_000,
            "tool_calls": 50_000,
            "checkpoints": 100,
        },
    }
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "iterations": len(iterations), "report": str(output)}, ensure_ascii=True))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
