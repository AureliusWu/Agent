from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


MAX_TOTAL_BYTES = 55 * 1024 * 1024
MAX_INCREASE_BYTES = 25 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def size_decision(candidate_bytes: int, baseline_bytes: int) -> dict[str, object]:
    increase_bytes = candidate_bytes - baseline_bytes
    total_pass = candidate_bytes <= MAX_TOTAL_BYTES
    increase_pass = increase_bytes <= MAX_INCREASE_BYTES
    return {
        "candidate_bytes": candidate_bytes,
        "baseline_bytes": baseline_bytes,
        "increase_bytes": increase_bytes,
        "maximum_total_bytes": MAX_TOTAL_BYTES,
        "maximum_increase_bytes": MAX_INCREASE_BYTES,
        "total_pass": total_pass,
        "increase_pass": increase_pass,
        "passed": total_pass and increase_pass,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the NSIS package size budget")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    candidate = args.candidate.resolve(strict=True)
    baseline = args.baseline.resolve(strict=True)
    decision = size_decision(candidate.stat().st_size, baseline.stat().st_size)
    payload = {
        "schema_version": 1,
        "status": "passed" if decision["passed"] else "failed",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "candidate": {"path": str(candidate), "sha256": sha256(candidate)},
        "baseline": {"path": str(baseline), "sha256": sha256(baseline)},
        **decision,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "report": str(output)}))
    return 0 if decision["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
