"""Plan or explicitly run the retained, source-bound Windows installer lifecycle.

Default planning is not installer acceptance. --execute never elevates the
caller and never invokes legacy smoke/preparation dispatchers. Failed execution
retains owned data, installer logs and state for operator recovery.

Historical manifests and newer no-bootstrap build audits are optional. Official
previous packages remain hash-bound to their original download receipt; missing
historical fields are observed during the owned run, never manufactured. Real
execution writes the unmodified private report before its fresh public projection.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from rc_installed_lifecycle import Lifecycle, SafetyError, WindowsAdapter, load_module, prepare, read_json, require

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="Bounded JSON with exact artifact path/hash pairs and a fresh owned output")
    parser.add_argument("--execute", action="store_true", help="Explicitly perform real installation; never requests UAC or mutates preexisting products")
    args = parser.parse_args(argv)
    adapter = runner = None
    try:
        request = read_json(Path(args.request).resolve())
        output = Path(request.get("output", ""))
        require(output.is_absolute(), "absolute owned output required before readonly probes")
        boundary = Path(request.get("test_boundary", ""))
        require(boundary.is_absolute() and boundary.is_dir() and output.resolve().is_relative_to(boundary.resolve()), "explicit test-only boundary required")
        probe_directory = boundary / ("installer-preflight-" + str(uuid.uuid4()))
        adapter = WindowsAdapter(ROOT, probe_directory)
        source = load_module(ROOT, "generate_build_info")._release_source_identity(ROOT)
        plan = prepare(ROOT, request, adapter, source)
        if not args.execute:
            print(json.dumps({"status": "PLANNED_NOT_EXECUTED", "actual_run": False, "rc_eligible": False,
                "kind": plan.current.kind, "source": plan.source, "candidate_sha256": plan.current.artifact.sha256,
                "previous_sha256": plan.previous.artifact.sha256, "installer_side_effects": False,
                "manual_acceptance": "NOT_RECORDED", "probes_retained": str(probe_directory),
                "public_output": str(plan.public_output), "previous_manifest_available": bool(plan.previous_manifest)}, ensure_ascii=False))
            return 0
        runner = Lifecycle(plan, adapter)
        report = runner.run()
        print(json.dumps({"status": report["status"], "actual_run": report["actual_run"], "rc_eligible": report["rc_eligible"],
                          "output": str(plan.output), "fixture_retained": True,
                          "state": report.get("state"), "private_evidence_retained": True}, ensure_ascii=False))
        return 0 if report["status"] == "PASS" else 1
    except (SafetyError, OSError, ValueError, KeyError, TypeError) as exc:
        # Do not include environment values, observed registrations, credentials,
        # raw API errors, package contents, or user profile paths in the console.
        actual = getattr(adapter, "mutation_started", False) is True
        print(json.dumps({"status": "BLOCKED", "actual_run": actual, "rc_eligible": False,
                          "state": "UNCERTAIN_OPERATOR_RECOVERY" if actual else "PREFLIGHT_BLOCKED",
                          "fixture_retained": runner is not None,
                          "fixture": str(runner.fixture.root) if runner is not None else None,
                          "failure_type": type(exc).__name__,
                          "detail": str(exc) if isinstance(exc, SafetyError) else "Preflight failed; private evidence retained."}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
