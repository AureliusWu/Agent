from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "8.0.0" / "TEST_RESULTS.json"
REPORT = ROOT / "build" / "v8-evidence" / "live-chat-gate.json"

CASE_TO_CHECK = {
    "CHAT-001": "identity_and_stream",
    "CHAT-002": "identity_and_stream",
    "CHAT-004": "prompt_injection",
    "CHAT-012": "cancel_without_residual",
    "KOKORO-001": "identity_and_stream",
    "KOKORO-003": "identity_and_stream",
    "KOKORO-004": "identity_and_stream",
    "KOKORO-006": "new_conversation_continuity",
    "KOKORO-007": "restart_continuity",
    "DATA-007": "logs_exclude_full_model_content",
}


def main() -> int:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    if report.get("status") not in {"passed", "completed_with_failures"}:
        raise RuntimeError("live chat gate did not complete")
    checks = report.get("checks") or {}
    results = json.loads(RESULTS.read_text(encoding="utf-8-sig"))
    for unsupported in ("CHAT-003", "KOKORO-002"):
        if results.get(unsupported, {}).get("status") == "PASS":
            results[unsupported] = {
                "status": "NOT_RUN",
                "reason": (
                    "The live gate did not execute every required assertion for "
                    "this case; it must not remain PASS."
                ),
            }
    for case_id, check_name in CASE_TO_CHECK.items():
        check = checks.get(check_name) or {}
        if check.get("passed") is not True:
            results[case_id] = {
                "status": "FAIL",
                "reason": (
                    f"Actual live-model check did not satisfy the required "
                    f"assertions: {check_name}."
                ),
                "evidence": [
                    {
                        "command": (
                            "powershell -Command "
                            "\"$env:SIYI_EVO_MODEL_API_KEY="
                            "[Environment]::GetEnvironmentVariable("
                            "'SIYI_EVO_MODEL_API_KEY','User'); "
                            "python scripts/run-v8-live-chat-gate.py\""
                        ),
                        "artifact": "build/v8-evidence/live-chat-gate.json",
                        "recorded_at": report["recorded_at"],
                        "build_id": report["build_id"],
                    }
                ],
            }
            continue
        results[case_id] = {
            "status": "PASS",
            "evidence": [
                {
                    "command": (
                        "powershell -Command "
                        "\"$env:SIYI_EVO_MODEL_API_KEY="
                        "[Environment]::GetEnvironmentVariable("
                        "'SIYI_EVO_MODEL_API_KEY','User'); "
                        "python scripts/run-v8-live-chat-gate.py\""
                    ),
                    "artifact": "build/v8-evidence/live-chat-gate.json",
                    "recorded_at": report["recorded_at"],
                    "build_id": report["build_id"],
                }
            ],
        }
    RESULTS.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Recorded {len(CASE_TO_CHECK)} live-chat mappings in {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
