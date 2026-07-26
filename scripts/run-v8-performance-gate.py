from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run packaged sidecar performance and identity gates")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "v8-evidence" / "performance-gate.json",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "build" / "generated" / "build-info.json").read_text(encoding="utf-8"))

    samples: list[dict[str, object]] = []
    for _ in range(3):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-File",
                str(ROOT / "scripts" / "smoke-sidecar.ps1"),
            ],
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
        samples.append(sample)

    readiness = [int(item["readiness_ms"]) for item in samples if "readiness_ms" in item]
    median_ms = int(statistics.median(readiness)) if len(readiness) == 3 else None
    threshold_ms = int(BASELINE_READINESS_MS * (1 + MAX_REGRESSION_RATIO))
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
    status = (
        "passed"
        if median_ms is not None
        and median_ms <= threshold_ms
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
