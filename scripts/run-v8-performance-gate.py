from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE_READINESS_MS = 1655
MAX_REGRESSION_RATIO = 0.15


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_smoke(binary: Path | None = None) -> dict[str, object]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-File",
        str(ROOT / "scripts" / "smoke-sidecar.ps1"),
    ]
    if binary is not None:
        command.extend(["-Binary", str(binary)])
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        sample = json.loads(result.stdout)
    except json.JSONDecodeError:
        sample = {"status": "failed", "output_tail": (result.stdout + result.stderr)[-4_000:]}
    sample["return_code"] = result.returncode
    return sample


def performance_decision(
    readiness: list[int],
    baseline_readiness: list[int],
    *,
    require_paired_baseline: bool = False,
) -> dict[str, object]:
    median_ms = int(statistics.median(readiness)) if len(readiness) == 3 else None
    absolute_limit_ms = int(BASELINE_READINESS_MS * (1 + MAX_REGRESSION_RATIO))
    absolute_pass = median_ms is not None and median_ms <= absolute_limit_ms
    paired_median_ms = (
        int(statistics.median(baseline_readiness))
        if len(baseline_readiness) == 3
        else None
    )
    paired_limit_ms = (
        int(paired_median_ms * (1 + MAX_REGRESSION_RATIO))
        if paired_median_ms is not None
        else None
    )
    paired_pass = (
        median_ms is not None
        and paired_limit_ms is not None
        and median_ms <= paired_limit_ms
    )
    return {
        "median_readiness_ms": median_ms,
        "absolute_limit_ms": absolute_limit_ms,
        "absolute_pass": absolute_pass,
        "paired_baseline_median_ms": paired_median_ms,
        "paired_limit_ms": paired_limit_ms,
        "paired_pass": paired_pass,
        "passed": paired_pass if require_paired_baseline else absolute_pass or paired_pass,
        "require_paired_baseline": require_paired_baseline,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run packaged sidecar performance and identity gates")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "v8-evidence" / "performance-gate.json",
    )
    parser.add_argument(
        "--baseline-binary",
        type=Path,
        help="Optional clean binary from the immediately previous release for paired same-host measurements.",
    )
    parser.add_argument(
        "--require-paired-baseline",
        action="store_true",
        help="Require three valid same-host baseline samples and the paired <=15% regression gate.",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "build" / "generated" / "build-info.json").read_text(encoding="utf-8"))

    baseline_binary = args.baseline_binary.resolve() if args.baseline_binary else None
    if baseline_binary is not None and not baseline_binary.is_file():
        raise FileNotFoundError(f"Baseline binary does not exist: {baseline_binary}")

    samples: list[dict[str, object]] = []
    baseline_samples: list[dict[str, object]] = []
    for index in range(3):
        # Alternate ordering so cache, antivirus and host-load drift do not
        # systematically favor either release.
        if baseline_binary is not None and index % 2 == 0:
            baseline_samples.append(run_smoke(baseline_binary))
            samples.append(run_smoke())
        else:
            samples.append(run_smoke())
            if baseline_binary is not None:
                baseline_samples.append(run_smoke(baseline_binary))

    readiness = [int(item["readiness_ms"]) for item in samples if "readiness_ms" in item]
    baseline_readiness = [
        int(item["readiness_ms"])
        for item in baseline_samples
        if "readiness_ms" in item
    ]
    decision = performance_decision(
        readiness,
        baseline_readiness,
        require_paired_baseline=args.require_paired_baseline,
    )
    median_ms = decision["median_readiness_ms"]
    threshold_ms = decision["absolute_limit_ms"]
    build_id = str(manifest["build_id"])
    frontend_assets = list((ROOT / "desktop" / "frontend" / "dist" / "assets").glob("*.js"))
    desktop_binary = ROOT / "desktop" / "src-tauri" / "target" / "release" / "司忆.exe"
    frontend_embedded = any(build_id.encode() in path.read_bytes() for path in frontend_assets)
    desktop_embedded = desktop_binary.is_file() and build_id.encode() in desktop_binary.read_bytes()
    sidecar_consistent = all(
        item.get("return_code") == 0
        and item.get("status") == "ok"
        and item.get("build_id") == build_id
        for item in samples
    )
    component_ids = manifest.get("component_build_ids") or {}
    component_ids_consistent = component_ids == {
        "tauri": f"tauri-{build_id}",
        "react": f"react-{build_id}",
        "sidecar": f"sidecar-{build_id}",
    }
    baseline_consistent = (
        baseline_binary is not None
        and len(baseline_samples) == 3
        and all(
            item.get("return_code") == 0
            and item.get("status") == "ok"
            and item.get("workspace_state") == "CLEAN"
            for item in baseline_samples
        )
        and len({str(item.get("build_id") or "") for item in baseline_samples}) == 1
        and str(baseline_samples[0].get("version") or "") != str(manifest["product_version"])
    )
    performance_pass = (
        baseline_consistent and bool(decision["paired_pass"])
        if args.require_paired_baseline
        else bool(decision["absolute_pass"])
        or (baseline_consistent and bool(decision["paired_pass"]))
    )
    status = (
        "passed"
        if performance_pass
        and frontend_embedded
        and desktop_embedded
        and sidecar_consistent
        and component_ids_consistent
        else "failed"
    )
    payload = {
        "schema_version": 1,
        "status": status,
        "recorded_at": utc_now(),
        "command": "powershell -File scripts/smoke-sidecar.ps1 (3 cold starts)",
        "build_id": build_id,
        "samples": samples,
        "readiness_ms": readiness,
        "median_readiness_ms": median_ms,
        "baseline_readiness_ms": BASELINE_READINESS_MS,
        "maximum_readiness_ms": threshold_ms,
        "absolute_threshold_pass": decision["absolute_pass"],
        "require_paired_baseline": args.require_paired_baseline,
        "paired_baseline": {
            "provided": baseline_binary is not None,
            "binary_sha256": (
                hashlib.sha256(baseline_binary.read_bytes()).hexdigest()
                if baseline_binary is not None
                else None
            ),
            "consistent": baseline_consistent,
            "samples": baseline_samples,
            "readiness_ms": baseline_readiness,
            "median_readiness_ms": decision["paired_baseline_median_ms"],
            "maximum_readiness_ms": decision["paired_limit_ms"],
            "regression_ratio": (
                round((int(median_ms) / int(decision["paired_baseline_median_ms"])) - 1, 4)
                if median_ms is not None and decision["paired_baseline_median_ms"]
                else None
            ),
            "pass": baseline_consistent and bool(decision["paired_pass"]),
        },
        "identity": {
            "frontend_embedded": frontend_embedded,
            "desktop_embedded": desktop_embedded,
            "sidecar_consistent": sidecar_consistent,
            "component_ids_consistent": component_ids_consistent,
        },
    }
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "report": str(output)}, ensure_ascii=True))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
