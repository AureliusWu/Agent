from __future__ import annotations

import copy
import importlib.util
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


COLLECTOR = load_module(
    "v14_a23_endurance_evidence", ROOT / "scripts" / "v14-a23-endurance-evidence.py"
)
EVIDENCE = load_module("v14_evidence_for_a23", ROOT / "scripts" / "v14-evidence.py")


def iso(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def event_timeline(base: datetime) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for sequence, action in enumerate(COLLECTOR.build_action_schedule(), 1):
        challenge = f"{sequence:024X}"
        variant = (
            "SIMULATED"
            if action == "device_change"
            else "STT" if action == "model_load_unload" else ""
        )
        issued = sequence * 4.0
        events.append(
            {
                "sequence": sequence,
                "action": action,
                "challenge": challenge,
                "issued_at": iso(base, issued),
                "completed_at": iso(base, issued + 1),
                "elapsed_seconds": 1.0,
                "run_elapsed_seconds": issued + 1,
                "outcome": "PASS",
                "variant": variant or None,
                "confirmation_sha256": COLLECTOR.confirmation_digest(
                    challenge, "PASS", variant
                ),
                "valid": True,
            }
        )
    return events


def resource_samples(base: datetime) -> list[dict[str, object]]:
    samples = []
    devices = [{"identity_sha256": "D" * 64, "status": "OK"}]
    for ordinal in range(121):
        samples.append(
            {
                "recorded_at": iso(base, ordinal * 15),
                "monotonic_seconds": float(ordinal * 15),
                "desktop_alive": True,
                "sidecar_alive": True,
                "api_healthy": True,
                "system_memory": {
                    "total_bytes": 16 * 1024**3,
                    "free_bytes": 8 * 1024**3,
                    "used_bytes": 8 * 1024**3,
                },
                "process": {
                    "process_count": 2,
                    "rss_bytes": 700_000_000 + ordinal * 1000,
                    "private_bytes": 600_000_000 + ordinal * 1000,
                    "handle_count": 300,
                    "processes": [
                        {
                            "pid": 101,
                            "ppid": 1,
                            "name": "司忆.exe",
                            "creation_time": "desktop-created",
                            "executable_sha256": "A" * 64,
                            "rss_bytes": 300_000_000,
                            "private_bytes": 250_000_000,
                            "handle_count": 100,
                        },
                        {
                            "pid": 102,
                            "ppid": 101,
                            "name": "agent-backend.exe",
                            "creation_time": "sidecar-created",
                            "executable_sha256": "B" * 64,
                            "rss_bytes": 400_000_000,
                            "private_bytes": 350_000_000,
                            "handle_count": 200,
                        },
                    ],
                },
                "gpu": {
                    "available": True,
                    "gpu_count": 1,
                    "used_mib": 1000,
                    "owned_used_mib": 0,
                },
                "audio_devices": {
                    "available": True,
                    "count": 1,
                    "devices": devices,
                },
                "filesystem": {
                    "temp": {"file_count": 0, "bytes": 0, "link_count": 0},
                    "cache": {"file_count": 1, "bytes": 10_000, "link_count": 0},
                    "audio_file_count": 1,
                },
            }
        )
    return samples


def passing_raw_report() -> dict[str, object]:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    events = event_timeline(base)
    counts = dict(Counter(str(item["action"]) for item in events))
    database_checks = {name: True for name in EVIDENCE.A23_REQUIRED_DATABASE_CHECKS}
    resource_checks = {name: True for name in EVIDENCE.A23_REQUIRED_RESOURCE_CHECKS}
    source = {
        "source_version": "14.0.0",
        "source_commit": "c" * 40,
        "source_tree_fingerprint": "F" * 64,
        "workspace_clean": False,
    }
    return {
        "schema_version": 1,
        "report_type": COLLECTOR.REPORT_TYPE,
        "producer": COLLECTOR.PRODUCER,
        "target_version": "14.0.0",
        "recorded_at": iso(base, 0),
        "finished_at": iso(base, 1802),
        "duration_seconds": 1802.0,
        "actual_run": True,
        "status": "PASS",
        "source": source,
        "interactive_operator": True,
        "test_owned_runtime": True,
        "candidate": {
            "filename": "司忆.exe",
            "sha256": "A" * 64,
            "identity_verified": True,
            "health": {
                "version": "14.0.0",
                "build_id": "build-current-source",
                "component_build_id": "sidecar-build-current-source",
                "git_commit": source["source_commit"],
                "source_fingerprint": source["source_tree_fingerprint"],
                "workspace_state": "DIRTY",
                "embedded": True,
            },
        },
        "runtime": {"name": "a23-runtime-fixture", "isolated": True, "retained": False},
        "ownership": {
            "verified": True,
            "desktop": {
                "pid": 101,
                "creation_time": "desktop-created",
                "executable_sha256": "A" * 64,
            },
            "sidecar_pid": 102,
            "sidecar_parent_pid": 101,
            "api_port": 49152,
        },
        "events": events,
        "resource_samples": resource_samples(base),
        "sampler_errors": [],
        "database_corroboration": {"passed": True, "checks": database_checks, "deltas": {}},
        "cleanup": {
            "owned_processes_released": True,
            "runtime_removed": True,
            "external_processes_protected": True,
            "external_ollama_before": [],
            "external_ollama_after": [],
            "remaining_owned_pids": [],
            "forced": False,
            "owned_gpu_mib_after": 0,
            "global_gpu_delta_mib": 0,
        },
        "results": {
            "action_counts": counts,
            "resource_assessment": {
                "passed": True,
                "checks": resource_checks,
                "sample_count": 121,
                "rss_growth_bytes": 120_000,
                "handle_growth": 0,
                "cache_growth_bytes": 0,
                "temp_growth_bytes": 0,
            },
            "failure_rate_trend": [
                {
                    "sequence": sequence,
                    "failures": 0,
                    "failure_rate": 0,
                }
                for sequence in range(1, len(events) + 1)
            ],
        },
        "checks": {
            name: {"passed": True}
            for name in COLLECTOR.REQUIRED_CHECKS
        },
    }


def test_execute_flag_is_mandatory() -> None:
    with pytest.raises(SystemExit):
        COLLECTOR.parse_args(["--candidate-exe", "missing.exe", "--output", "raw/a23.json"])


def test_action_schedule_contains_every_plan_minimum() -> None:
    counts = Counter(COLLECTOR.build_action_schedule())

    assert counts == Counter(COLLECTOR.ACTION_REQUIREMENTS)
    assert counts["recording_start_stop"] == 100
    assert counts["stt"] == counts["tts"] == 50


def test_a23_is_runner_attested_and_requires_its_dedicated_collector() -> None:
    policy = EVIDENCE.ATTESTED_CASE_POLICIES["A23"]

    assert policy["command_path"] == "scripts/v14-a23-endurance-evidence.py"
    assert policy["report_type"] == COLLECTOR.REPORT_TYPE
    assert policy["raw_source"] is True
    assert set(policy["required_checks"]) == set(COLLECTOR.REQUIRED_CHECKS)


def test_operator_confirmation_binds_fresh_challenge_and_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    challenge = "AB" * 12
    monotonic = iter([10.0, 12.0])
    wall = iter(["2026-08-12T00:00:10Z", "2026-08-12T00:00:12Z"])
    monkeypatch.setattr(COLLECTOR.secrets, "token_hex", lambda _: challenge.casefold())

    event = COLLECTOR.confirm_action(
        "device_change",
        1,
        1,
        input_fn=lambda _: f"PASS {challenge} SIMULATED",
        monotonic_fn=lambda: next(monotonic),
        now_fn=lambda: next(wall),
    )

    assert event["valid"] is True
    assert event["outcome"] == "PASS"
    assert COLLECTOR.event_is_valid(event) is True


def test_evaluate_report_rejects_short_run_and_cleanup_failure() -> None:
    raw = passing_raw_report()
    assert COLLECTOR.evaluate_report(raw)["status"] == "PASS"

    raw["duration_seconds"] = 1799
    assert COLLECTOR.evaluate_report(raw)["status"] == "FAIL"
    raw["duration_seconds"] = 1802
    raw["cleanup"]["runtime_removed"] = False  # type: ignore[index]
    assert COLLECTOR.evaluate_report(raw)["status"] == "FAIL"


def test_deep_validator_accepts_complete_structured_a23_fixture() -> None:
    EVIDENCE._validate_a23_endurance_live(passing_raw_report(), case_id="A23")


@pytest.mark.parametrize(
    "mutation",
    (
        "short_duration",
        "missing_recording_event",
        "forged_challenge_digest",
        "unembedded_candidate",
        "uncorroborated_database",
        "resource_gap",
        "cleanup_failure",
        "external_process_changed",
    ),
)
def test_deep_validator_rejects_incomplete_or_forge_like_a23(mutation: str) -> None:
    raw = copy.deepcopy(passing_raw_report())
    if mutation == "short_duration":
        raw["duration_seconds"] = 1799
    elif mutation == "missing_recording_event":
        raw["events"] = [
            event for event in raw["events"] if event["action"] != "recording_start_stop"  # type: ignore[index]
        ]
    elif mutation == "forged_challenge_digest":
        raw["events"][0]["confirmation_sha256"] = "0" * 64  # type: ignore[index]
    elif mutation == "unembedded_candidate":
        raw["candidate"]["health"]["embedded"] = False  # type: ignore[index]
    elif mutation == "uncorroborated_database":
        raw["database_corroboration"]["checks"]["stt_completed"] = False  # type: ignore[index]
    elif mutation == "resource_gap":
        raw["resource_samples"][60]["recorded_at"] = raw["resource_samples"][63]["recorded_at"]  # type: ignore[index]
    elif mutation == "cleanup_failure":
        raw["cleanup"]["runtime_removed"] = False  # type: ignore[index]
    elif mutation == "external_process_changed":
        raw["cleanup"]["external_ollama_after"] = [{"pid": 999}]  # type: ignore[index]

    with pytest.raises(EVIDENCE.EvidenceValidationError):
        EVIDENCE._validate_a23_endurance_live(raw, case_id="A23")


def test_immutable_writer_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "a23.json"
    COLLECTOR.write_json_immutable(output, {"status": "FAIL"})

    with pytest.raises(COLLECTOR.EnduranceEvidenceError):
        COLLECTOR.write_json_immutable(output, {"status": "PASS"})
