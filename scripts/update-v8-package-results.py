from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "8.0.0" / "TEST_RESULTS.json"


def evidence(command: str, artifact: str, report: dict, build_id: str) -> dict[str, str]:
    return {
        "command": command,
        "artifact": artifact,
        "recorded_at": str(report["recorded_at"]),
        "build_id": build_id,
    }


def main() -> int:
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    build = json.loads((ROOT / "build" / "generated" / "build-info.json").read_text(encoding="utf-8"))
    build_id = str(build["build_id"])
    privacy = json.loads((ROOT / "build" / "v8-evidence" / "privacy-gate.json").read_text(encoding="utf-8"))
    nsis = json.loads((ROOT / "build" / "v8-evidence" / "nsis-lifecycle.json").read_text(encoding="utf-8-sig"))
    performance = json.loads((ROOT / "build" / "v8-evidence" / "performance-gate.json").read_text(encoding="utf-8"))
    credential = json.loads((ROOT / "build" / "v8-evidence" / "credential-manager-gate.json").read_text(encoding="utf-8"))
    if (
        privacy["status"] != "passed"
        or nsis["status"] != "ok"
        or performance["status"] != "passed"
        or credential["status"] != "passed"
    ):
        raise RuntimeError("package, privacy, performance, or credential evidence is not passed")

    privacy_evidence = evidence(
        "python scripts/run-v8-privacy-gate.py",
        "build/v8-evidence/privacy-gate.json",
        privacy,
        build_id,
    )
    nsis_evidence = evidence(
        "powershell scripts/smoke-installer.ps1 -PreviousInstaller <v6-nsis>",
        "build/v8-evidence/nsis-lifecycle.json",
        nsis,
        build_id,
    )
    performance_evidence = evidence(
        "python scripts/run-v8-performance-gate.py",
        "build/v8-evidence/performance-gate.json",
        performance,
        build_id,
    )
    credential_evidence = evidence(
        "python scripts/run-v8-credential-gate.py",
        "build/v8-evidence/credential-manager-gate.json",
        credential,
        str(credential["build_id"]),
    )
    updates = {
        "DESK-005": [nsis_evidence],
        "DATA-001": [privacy_evidence, nsis_evidence],
        "DATA-003": [privacy_evidence],
        "DATA-004": [privacy_evidence],
        "DATA-006": [privacy_evidence],
        "DATA-008": [credential_evidence, privacy_evidence],
        "DATA-009": [nsis_evidence],
        "DATA-010": [nsis_evidence],
        "DATA-011": [nsis_evidence],
        "DATA-012": [nsis_evidence],
        "DATA-018": [privacy_evidence],
        "KOKORO-019": [privacy_evidence],
        "REL-002": [performance_evidence],
    }
    for case_id, records in updates.items():
        results[case_id] = {"status": "PASS", "evidence": records}
    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(updates)} package/privacy mappings in {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
