from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
COLLECTOR_PATH = ROOT / "scripts" / "v14-desktop-voice-acceptance-evidence.py"
EVIDENCE_PATH = ROOT / "scripts" / "v14-evidence.py"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


COLLECTOR = load_module("v14_desktop_voice_acceptance", COLLECTOR_PATH)
EVIDENCE = load_module("v14_evidence_for_desktop_voice", EVIDENCE_PATH)


def _event_timeline() -> list[dict[str, object]]:
    base = datetime(2026, 8, 13, tzinfo=UTC)
    events: list[dict[str, object]] = []
    for sequence, (action, case_ids, _instruction, scope) in enumerate(
        COLLECTOR.ACTION_SPECS, 1
    ):
        challenge = f"{sequence:024X}"
        issued = base + timedelta(seconds=sequence * 2)
        completed = issued + timedelta(seconds=1)
        events.append(
            {
                "sequence": sequence,
                "action": action,
                "case_ids": list(case_ids),
                "evidence_scope": scope,
                "formal_candidate": True,
                "real_microphone": scope == "REAL_MICROPHONE",
                "challenge": challenge,
                "issued_at": issued.isoformat().replace("+00:00", "Z"),
                "completed_at": completed.isoformat().replace("+00:00", "Z"),
                "elapsed_seconds": 1.0,
                "outcome": "PASS",
                "confirmation_sha256": COLLECTOR.confirmation_digest(
                    challenge, "PASS"
                ),
                "valid": True,
            }
        )
    return events


def _valid_report() -> dict[str, object]:
    source = {
        "source_version": "14.0.0",
        "source_commit": "a" * 40,
        "source_tree_fingerprint": "B" * 64,
        "workspace_clean": True,
    }
    devices = {
        "count": 1,
        "devices": [{"identity_sha256": "C" * 64, "status": "OK"}],
    }
    database = {
        "permission": {"requested": 1, "denied": 1, "recording_started": 2},
        "device": {
            "selection_persisted": True,
            "selected_identity_sha256": "D" * 64,
        },
        "recording": {
            "start_events": 2,
            "stop_events": 1,
            "cancelled_sessions": 1,
            "wav_16k_mono_pcm": 1,
            "completed_stt": 1,
        },
        "events": {
            "standard_order_sessions": 1,
            "tts_started": 1,
            "tts_stopped": 1,
            "agent_cancelled": 1,
            "stt_cancelled": 1,
            "half_duplex_order": True,
        },
        "messages": {
            "edited_message_count": 1,
            "privacy_message_count": 1,
            "cancel_message_count": 0,
            "dangerous_task_count": 1,
            "voice_bound_manual": 1,
            "voice_bound_auto": 1,
        },
        "permissions": {
            "critical_confirmation_required": 1,
            "confirmed_critical_success": 0,
            "waiting_confirmation_tasks": 1,
            "dangerous_task_count": 1,
            "dangerous_critical_confirmation_required": 1,
            "dangerous_waiting_confirmation_tasks": 1,
            "dangerous_confirmed_critical_success": 0,
        },
        "stop": {
            "cancelled_voice": 1,
            "cancelled_stt": 1,
            "cancelled_tts": 1,
            "cancelled_tasks": 1,
            "active_voice": 0,
            "active_stt": 0,
            "active_tts": 0,
        },
        "offline": {
            "providers": ["ollama"],
            "models": ["qwen3:4b"],
            "successful_ollama_runs": 1,
            "completed_tts": 1,
        },
    }
    empty_scan = {
        "file_count": 0,
        "bytes_scanned": 0,
        "aggregate_sha256": "E" * 64,
        "marker_matches": 0,
    }
    def stop_stage(event: str) -> dict[str, object]:
        return {
            "audit": {
                "receipt_count": 2,
                "first_target_count": 1,
                "second_target_count": 0,
                "first_settled": True,
                "second_settled": True,
                "second_idempotent_no_active_target": True,
                "metadata_only": True,
            },
            "machine": {
                "stt_cancelled": 1 if event == "stt_cancelled" else 0,
                "agent_cancelled": 1 if event == "agent_cancelled" else 0,
                "tts_stopped": 1 if event == "tts_stopped" else 0,
                "voice_terminal": 1,
                "active_voice": 0,
                "active_stt": 0,
                "active_tts": 0,
                "active_tasks": 0,
                "active_queue": 0,
                "temporary_audio_files": 0,
            },
        }
    return {
        "schema_version": 1,
        "report_type": COLLECTOR.REPORT_TYPE,
        "producer": COLLECTOR.PRODUCER,
        "target_version": "14.0.0",
        "actual_run": True,
        "status": "PASS",
        "source": source,
        "interactive_operator": True,
        "input_contract": {
            "microphone": "REAL_WINDOWS_DEVICE_ONLY",
            "audio_file_upload": "FORBIDDEN",
            "synthetic_speech": "FORBIDDEN",
            "fixed_test_text_sensitive": False,
        },
        "candidate": {
            "filename": "司忆.exe",
            "sha256": "F" * 64,
            "identity_verified": True,
            "health": {
                "version": "14.0.0",
                "embedded": True,
                "workspace_state": "CLEAN",
                "git_commit": source["source_commit"],
                "source_fingerprint": source["source_tree_fingerprint"],
            },
        },
        "runtime": {"name": "desktop-voice-runtime-test", "isolated": True, "retained": False},
        "ownership": {
            "verified": True,
            "desktop_pid": 100,
            "sidecar_pid": 101,
            "sidecar_parent_pid": 100,
            "api_port": 51001,
        },
        "operator_events": _event_timeline(),
        "results": {
            "devices": {
                "before": devices,
                "disconnected": {"count": 0, "devices": []},
                "after": copy.deepcopy(devices),
            },
            "network": {
                "before": {
                    "physical_count": 1,
                    "up_count": 1,
                    "adapters": [{"identity_sha256": "1" * 64, "status": "UP"}],
                },
                "offline": {
                    "physical_count": 1,
                    "up_count": 0,
                    "adapters": [{"identity_sha256": "1" * 64, "status": "DISCONNECTED"}],
                },
                "connections": {
                    "owned_pid_count": 2,
                    "established_count": 1,
                    "loopback_count": 1,
                    "public_connections": [],
                },
                "provider": {
                    "provider_id": "ollama",
                    "base_url": "http://127.0.0.1:11434",
                    "model": "qwen3:4b",
                },
                "after": {
                    "physical_count": 1,
                    "up_count": 1,
                    "adapters": [{"identity_sha256": "1" * 64, "status": "UP"}],
                },
            },
            "dangerous_confirmation": {
                "before_reject": {
                    "task_count": 1,
                    "waiting_confirmation_count": 1,
                    "cancelled_count": 0,
                    "active_count": 1,
                    "critical_confirmation_required_count": 1,
                    "critical_executed_count": 0,
                },
                "after_reject": {
                    "task_count": 1,
                    "waiting_confirmation_count": 0,
                    "cancelled_count": 1,
                    "active_count": 0,
                    "critical_confirmation_required_count": 1,
                    "critical_executed_count": 0,
                },
            },
            "global_stop": {
                "global_stop_recording": stop_stage("stt_cancelled"),
                "global_stop_stt": stop_stage("stt_cancelled"),
                "global_stop_agent": stop_stage("agent_cancelled"),
                "global_stop_tts": stop_stage("tts_stopped"),
            },
            "database": database,
            "privacy": {
                "runtime_scans": {
                    name: copy.deepcopy(empty_scan)
                    for name in ("logs", "crash", "temp")
                },
                "diagnostics": {
                    "bundle_count": 1,
                    "entry_count": 2,
                    "bytes_scanned": 200,
                    "aggregate_sha256": "2" * 64,
                    "marker_matches": 0,
                },
                "database": {
                    "forbidden_storage_matches": {
                        "voice_sessions": 0,
                        "voice_events": 0,
                        "stt_requests": 0,
                        "tts_requests": 0,
                        "audit_logs": 0,
                        "data_flows": 0,
                    },
                    "all_forbidden_zero": True,
                    "allowed_conversation_storage": "messages_and_agent_task_follow_existing_conversation_rules",
                },
                "temporary_audio_files_after": 0,
            },
        },
        "cleanup": {
            "desktop_stopped": True,
            "forced": False,
            "model_junction_removed": True,
            "owned_processes_released": True,
            "runtime_removed": True,
            "external_processes_protected": True,
        },
    }


def test_collector_requires_explicit_execute_and_safe_output(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        COLLECTOR.parse_args(
            ["--candidate-exe", "candidate.exe", "--stt-model-directory", "model", "--output", "report.json"]
        )

    outside = tmp_path / "report.json"
    with pytest.raises(COLLECTOR.DesktopAcceptanceError, match="must stay under"):
        COLLECTOR.resolve_output(str(outside))


def test_confirmation_is_fresh_and_privacy_marker_must_be_spoken(monkeypatch: pytest.MonkeyPatch) -> None:
    challenge = "A" * 24
    prompt_seen: list[str] = []
    ticks = iter((10.0, 11.0))
    stamps = iter(("2026-08-13T00:00:00Z", "2026-08-13T00:00:01Z"))
    monkeypatch.setattr(COLLECTOR.secrets, "token_hex", lambda _size: challenge.lower())

    def input_fn(prompt: str) -> str:
        prompt_seen.append(prompt)
        return f"PASS {challenge}"

    privacy_spec = next(spec for spec in COLLECTOR.ACTION_SPECS if spec[0] == "privacy_probe")
    event = COLLECTOR.confirm_action(
        privacy_spec,
        1,
        1,
        privacy_marker="司忆隐私探针123456号",
        cancel_marker="cancel",
        input_fn=input_fn,
        monotonic_fn=lambda: next(ticks),
        now_fn=lambda: next(stamps),
    )

    assert event["valid"] is True
    assert event["real_microphone"] is True
    assert "逐字说出测试标记" in prompt_seen[0]
    assert "司忆隐私探针123456号" in prompt_seen[0]
    assert "PASS AAAAAAAAAAAAAAAAAAAAAAAA" not in json.dumps(event)


def test_collector_never_bypasses_candidate_api_auth_and_projects_provider_state(tmp_path: Path) -> None:
    source = COLLECTOR_PATH.read_text(encoding="utf-8")
    assert "urllib.request" not in source
    assert "def api_json" not in source
    runtime = tmp_path / "isolated-runtime"
    state = runtime / "data" / "state"
    state.mkdir(parents=True)
    (state / "provider-settings.json").write_text(
        json.dumps(
            {
                "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen3:4b",
                "unexpected_secret": "must-not-appear",
            }
        ),
        encoding="utf-8",
    )

    result = COLLECTOR.provider_configuration_observation(runtime)

    assert result == {
        "provider_id": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen3:4b",
    }
    assert "must-not-appear" not in json.dumps(result)


def test_global_stop_audit_projection_refuses_extra_or_content_fields() -> None:
    details = {
        "schema_version": 1,
        "scope": "all",
        "requested_session": False,
        "sessions_targeted": 1,
        "sessions_cancelled": 1,
        "target_count": 1,
        "active_count": 0,
        "queue_active_count": 0,
        "unresolved_count": 0,
        "settled": True,
        "idempotent_no_active_target": False,
    }
    result = COLLECTOR._stop_audit_projection(
        {"status": "CANCELLED", "details": json.dumps(details)}
    )

    assert result["target_count"] == 1
    assert "prompt" not in json.dumps(result)
    details["prompt"] = "must-not-be-audit-data"
    with pytest.raises(COLLECTOR.DesktopAcceptanceError):
        COLLECTOR._stop_audit_projection(
            {"status": "CANCELLED", "details": json.dumps(details)}
        )


def test_collector_evaluation_passes_only_the_complete_bound_contract() -> None:
    report = _valid_report()
    assessment = COLLECTOR.evaluate_report(report)

    assert assessment["status"] == "PASS"
    assert all(item["passed"] is True for item in assessment["checks"].values())

    forged = copy.deepcopy(report)
    forged["operator_events"][0]["evidence_scope"] = "SYNTHETIC"
    forged["operator_events"][0]["real_microphone"] = False
    assert COLLECTOR.evaluate_report(forged)["status"] == "FAIL"


@pytest.mark.parametrize("case_id", sorted(COLLECTOR.REQUIRED_CHECKS))
def test_v14_policy_accepts_the_complete_desktop_voice_contract(case_id: str) -> None:
    EVIDENCE._validate_desktop_voice_live(_valid_report(), case_id=case_id)


@pytest.mark.parametrize(
    ("case_id", "mutation"),
    (
        ("A04", "synthetic_scope"),
        ("A04", "wrong_case_binding"),
        ("A12", "missing_action"),
        ("A12", "candidate_mismatch"),
        ("A12", "forced_cleanup"),
        ("A15", "danger_before_reject_missing"),
        ("A17", "global_stop_only_one_receipt"),
        ("A19", "public_connection"),
        ("A24", "privacy_marker_leak"),
    ),
)
def test_v14_policy_rejects_forge_like_or_incomplete_evidence(
    case_id: str, mutation: str
) -> None:
    report = _valid_report()
    if mutation == "synthetic_scope":
        report["operator_events"][0]["evidence_scope"] = "SYNTHETIC"
        report["operator_events"][0]["real_microphone"] = False
    elif mutation == "wrong_case_binding":
        report["operator_events"][0]["case_ids"] = ["A99"]
    elif mutation == "missing_action":
        del report["operator_events"][6]
    elif mutation == "candidate_mismatch":
        report["candidate"]["health"]["git_commit"] = "f" * 40
    elif mutation == "forced_cleanup":
        report["cleanup"]["forced"] = True
    elif mutation == "danger_before_reject_missing":
        report["results"]["dangerous_confirmation"]["before_reject"]["waiting_confirmation_count"] = 0
    elif mutation == "global_stop_only_one_receipt":
        report["results"]["global_stop"]["global_stop_tts"]["audit"]["receipt_count"] = 1
    elif mutation == "public_connection":
        report["results"]["network"]["connections"]["public_connections"] = [
            {"address_sha256": "3" * 64, "port": 443}
        ]
    elif mutation == "privacy_marker_leak":
        report["results"]["privacy"]["runtime_scans"]["logs"]["marker_matches"] = 1

    with pytest.raises(EVIDENCE.EvidenceValidationError):
        EVIDENCE._validate_desktop_voice_live(report, case_id=case_id)


def _create_probe_database(path: Path, privacy_marker: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE voice_sessions(
          voice_session_id TEXT, state TEXT, audio_format TEXT, audio_duration_ms INTEGER,
          auto_send INTEGER, message_id INTEGER, task_id TEXT, error_code TEXT
        );
        CREATE TABLE voice_event_records(
          id INTEGER PRIMARY KEY, voice_session_id TEXT, event TEXT, payload_json TEXT
        );
        CREATE TABLE stt_requests(
          status TEXT, audio_duration_ms INTEGER, result_text_hash TEXT, error_code TEXT
        );
        CREATE TABLE tts_requests(status TEXT, error_code TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, task_id TEXT);
        CREATE TABLE agent_tasks(id TEXT, status TEXT, prompt TEXT);
        CREATE TABLE tool_runs(
          id INTEGER PRIMARY KEY, task_id TEXT, risk TEXT, confirmed INTEGER, tool TEXT, status TEXT
        );
        CREATE TABLE model_runs(
          id INTEGER PRIMARY KEY, task_id TEXT, provider TEXT, model TEXT, success INTEGER
        );
        CREATE TABLE microphone_settings(
          singleton INTEGER PRIMARY KEY, selected_device_id TEXT, selected_device_label TEXT
        );
        CREATE TABLE audit_logs(details TEXT);
        CREATE TABLE data_flow_events(fields TEXT, reason TEXT);
        """
    )
    db.executemany(
        "INSERT INTO voice_sessions VALUES(?,?,?,?,?,?,?,?)",
        (
            ("voice-manual", "COMPLETED", "wav-16k-mono-pcm", 1000, 0, 1, "task-offline", None),
            ("voice-auto", "COMPLETED", "wav-16k-mono-pcm", 900, 1, 2, "task-auto", None),
            ("voice-cancel", "CANCELLED", None, 0, 0, None, None, "cancelled"),
        ),
    )
    events = (
        "TTS_STARTED",
        "TTS_STOPPED",
        "MIC_PERMISSION",
        "RECORDING_STARTED",
        "RECORDING_STOPPED",
        "AUDIO_READY",
        "STT_STARTED",
        "STT_COMPLETED",
        "MESSAGE_READY",
    )
    for event_id, event in enumerate(events, 1):
        payload = '{"status":"DENIED"}' if event == "MIC_PERMISSION" else "{}"
        db.execute(
            "INSERT INTO voice_event_records VALUES(?,?,?,?)",
            (event_id, "voice-manual", event, payload),
        )
    db.execute(
        "INSERT INTO voice_event_records VALUES(?,?,?,?)",
        (20, "voice-auto", "RECORDING_STARTED", "{}"),
    )
    db.executemany(
        "INSERT INTO stt_requests VALUES(?,?,?,?)",
        (("COMPLETED", 1000, "4" * 64, None), ("CANCELLED", 0, None, "cancelled")),
    )
    db.executemany(
        "INSERT INTO tts_requests VALUES(?,?)",
        (("COMPLETED", None), ("CANCELLED", "cancelled")),
    )
    db.executemany(
        "INSERT INTO messages VALUES(?,?,?,?)",
        (
            (1, "user", COLLECTOR.SAFE_EDITED_MESSAGE, "task-offline"),
            (2, "user", privacy_marker, "task-auto"),
        ),
    )
    db.executemany(
        "INSERT INTO agent_tasks VALUES(?,?,?)",
        (
            ("task-danger", "waiting_confirmation", COLLECTOR.DANGEROUS_MESSAGE),
            ("task-offline", "completed", "offline"),
            ("task-auto", "completed", privacy_marker),
        ),
    )
    db.execute(
        "INSERT INTO tool_runs VALUES(?,?,?,?,?,?)",
        (1, "task-danger", "critical", 0, "delete_file", "confirmation_required"),
    )
    db.execute(
        "INSERT INTO model_runs VALUES(?,?,?,?,?)",
        (1, "task-offline", "ollama", "qwen3:4b", 1),
    )
    db.execute("INSERT INTO microphone_settings VALUES(1,'device-1','Microphone')")
    db.commit()
    return db


def test_database_corrobation_uses_metadata_and_binds_dangerous_task(tmp_path: Path) -> None:
    privacy_marker = "司忆隐私探针123456号"
    database = tmp_path / "agent.db"
    db = _create_probe_database(database, privacy_marker)
    db.close()
    before = {name: 0 for name in (
        "voice_sessions", "voice_event_records", "stt_requests", "tts_requests",
        "messages", "agent_tasks", "tool_runs", "model_runs"
    )}

    result = COLLECTOR.collect_database_results(
        database, before, privacy_marker, "cancel-marker"
    )

    assert result["events"]["standard_order_sessions"] == 1
    assert result["events"]["half_duplex_order"] is True
    assert result["messages"]["edited_message_count"] == 1
    assert result["messages"]["privacy_message_count"] == 1
    assert result["messages"]["voice_bound_manual"] == 1
    assert result["messages"]["voice_bound_auto"] == 1
    assert result["permissions"]["dangerous_task_count"] == 1
    assert result["permissions"]["dangerous_critical_confirmation_required"] == 1
    assert result["permissions"]["dangerous_waiting_confirmation_tasks"] == 1
    assert result["permissions"]["dangerous_confirmed_critical_success"] == 0
    dangerous = COLLECTOR.dangerous_confirmation_observation(database)
    assert dangerous["waiting_confirmation_count"] == 1
    assert dangerous["critical_executed_count"] == 0
    assert COLLECTOR.DANGEROUS_MESSAGE not in json.dumps(dangerous)

    privacy = COLLECTOR.privacy_database_observation(database, privacy_marker)
    assert privacy["all_forbidden_zero"] is True


def test_privacy_scan_detects_utf8_and_utf16_markers(tmp_path: Path) -> None:
    marker = "司忆隐私探针123456号"
    (tmp_path / "utf8.log").write_bytes(marker.encode("utf-8"))
    (tmp_path / "utf16.log").write_bytes(marker.encode("utf-16-le"))

    result = COLLECTOR.scan_files(tmp_path, marker)

    assert result["marker_matches"] == 2
    assert result["file_count"] == 2
    assert result["bytes_scanned"] > 0
    assert len(result["aggregate_sha256"]) == 64


def test_collector_functions_stay_within_review_threshold() -> None:
    tree = ast.parse(COLLECTOR_PATH.read_text(encoding="utf-8"), filename=str(COLLECTOR_PATH))
    oversized = {
        node.name: node.end_lineno - node.lineno + 1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno is not None
        and node.end_lineno - node.lineno + 1 > 120
    }

    assert oversized == {}
