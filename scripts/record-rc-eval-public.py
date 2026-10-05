"""Capture a fresh public projection of an unchanged private Eval v1 report.

Never executes an Eval, model, microphone, installer, build or publication. The
original measurement source remains original; this collector has separate
provenance. A successful projection is not actual-run attestation or RC success.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def module(name: str):
    specification = importlib.util.spec_from_file_location("rc_eval_capture_" + name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    if specification is None or specification.loader is None:
        raise ValueError("required projector is unavailable")
    value = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = value
    specification.loader.exec_module(value)
    return value


def current_source_identity() -> dict:
    return module("generate_build_info")._release_source_identity(ROOT)


def projector_identity() -> dict:
    before = current_source_identity()
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "source": before,
              "module_sha256": hashlib.sha256(Path(__file__).with_name("rc_eval_public.py").read_bytes()).hexdigest(),
              "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    result["source_after"] = current_source_identity()
    if result["source_after"] != before:
        raise ValueError("projector source changed")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="Existing private original; never modified")
    parser.add_argument("--output", required=True, help="Fresh repository-relative .public.json under accepted/evals")
    args = parser.parse_args(argv)
    try:
        relative = Path(args.output)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("output must be canonical and repository relative")
        public = module("rc_eval_public")
        identity = projector_identity()
        result = public.write_public_report(ROOT, ROOT / relative, args.report, projector=identity)
        # Identity capture is separate from and cannot relabel the measurement.
        if current_source_identity() != identity["source_after"]:
            raise ValueError("projector source changed during projection; output retained")
        print(json.dumps({"status": "PUBLIC_EVAL_PROJECTED_NOT_RC_ACCEPTED", "path": relative.as_posix(),
                          "measurement_run_id": result["measurement"]["run_id"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, ImportError, RecursionError, AttributeError):
        # Private filenames, endpoints and malformed values must not reach logs.
        print(json.dumps({"status": "BLOCKED", "detail": "Unsafe, malformed, changed or existing Eval projection; private original untouched"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
