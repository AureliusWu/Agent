from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "siyi"))

from app.evals.real_scenarios import validate_catalog, validate_run_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate v8.0.1 real-scenario definitions and evidence")
    parser.add_argument("--catalog", type=Path, default=ROOT / "real-scenarios" / "catalog.json")
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args()
    errors = validate_catalog(args.catalog.resolve())
    if args.run_root:
        errors.extend(validate_run_root(args.run_root.resolve()))
    result = {
        "status": "passed" if not errors else "failed",
        "catalog": str(args.catalog.resolve()),
        "run_root": str(args.run_root.resolve()) if args.run_root else None,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
