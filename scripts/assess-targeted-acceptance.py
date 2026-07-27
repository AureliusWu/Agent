from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "siyi"))

from app.evals.full_function import assess_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Assess the 180-case targeted acceptance manifest")
    parser.add_argument("--gates", type=Path, required=True)
    parser.add_argument("--scenarios", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args()
    gates = json.loads(args.gates.read_text(encoding="utf-8-sig"))
    scenarios = json.loads(args.scenarios.read_text(encoding="utf-8-sig")) if args.scenarios else {}
    report = assess_manifest(ROOT / "evals" / "full_function_manifest_v2.json", gates, scenarios)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "release_ready": report["release_ready"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if report["release_ready"] or not args.require_release_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
