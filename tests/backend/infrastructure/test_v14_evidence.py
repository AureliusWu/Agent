from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "v14-evidence.py"
SPEC = importlib.util.spec_from_file_location("v14_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("13.0.0\n", encoding="ascii")
    (root / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    plan = root / "v14-plan.md"
    plan.write_text(
        "# 司忆 v14.0.0 实施计划\n\n"
        "FasterWhisperProvider\n/api/stt/transcribe\n/api/voice/events\nA01–A28\n",
        encoding="utf-8",
    )
    evidence_root = root / "build" / "v1400-evidence"
    evidence_root.mkdir(parents=True)
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "evidence-test@example.invalid"],
        ["git", "config", "user.name", "Evidence Test"],
        ["git", "add", "VERSION", "v14-plan.md", ".gitignore"],
        ["git", "commit", "-m", "test evidence source"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root, plan, evidence_root


def controlled_test_command(root: Path) -> tuple[str, dict[str, object]]:
    path = root / "tests" / "backend" / "test_evidence_contract.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_contract_fixture():\n    assert True\n", encoding="utf-8")
    return (
        "python -m pytest tests/backend/test_evidence_contract.py -q",
        {"kind": "pytest", "paths": ["tests/backend/test_evidence_contract.py"]},
    )


def write_current_execution_report(
    root: Path, evidence_root: Path, *, case_ids: list[str], filename: str = "controlled.json"
) -> tuple[Path, str, str]:
    command, command_contract = controlled_test_command(root)
    recorded_at = "2026-08-03T00:00:00Z"
    source = MODULE.source_identity(root)
    report = evidence_root / filename
    report.write_text(
        json.dumps(
            {
                "schema_version": MODULE.EXECUTION_REPORT_SCHEMA_VERSION,
                "report_type": MODULE.EXECUTION_REPORT_TYPE,
                "producer": MODULE.EXECUTION_REPORT_PRODUCER,
                "target_version": "14.0.0",
                **source,
                "actual_run": True,
                "status": "PASS",
                "case_ids": case_ids,
                "command": command,
                "command_contract": command_contract,
                "recorded_at": recorded_at,
                "execution": {
                    "actual_run": True,
                    "status": "PASS",
                    "exit_code": 0,
                    "timed_out": False,
                },
            }
        ),
        encoding="utf-8",
    )
    return report, command, recorded_at


def write_attested_live_execution_report(
    root: Path,
    evidence_root: Path,
    *,
    case_id: str,
    filename: str = "attested-envelope.json",
    include_attachment: bool = True,
    raw_details: dict[str, object] | None = None,
) -> tuple[Path, str, str, Path, dict[str, object]]:
    """Build a synthetic but cryptographically complete live-report fixture.

    It deliberately does not call a provider.  The test covers only whether
    the evidence generator refuses a forged or post-run-modified attachment.
    """

    policy = MODULE.ATTESTED_CASE_POLICIES[case_id]
    command_path = str(policy["command_path"])
    report_type = str(policy["report_type"])
    controlled_script = root / command_path
    controlled_script.parent.mkdir(parents=True, exist_ok=True)
    controlled_script.write_text("# controlled fixture script\n", encoding="utf-8")
    source = MODULE.source_identity(root)
    raw = evidence_root / "raw" / f"{case_id.lower()}-live.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = {
        "schema_version": 1,
        "report_type": report_type,
        "producer": command_path,
        "target_version": "14.0.0",
        "status": "PASS",
        "actual_run": True,
        "source": source,
    }
    if raw_details:
        raw_payload.update(raw_details)
    raw.write_text(json.dumps(raw_payload, sort_keys=True) + "\n", encoding="utf-8")
    attachment: dict[str, object] = {
        "path": MODULE.repository_relative(root, raw),
        "sha256": MODULE.sha256(raw),
        "bytes": raw.stat().st_size,
        "status": "PASS",
        "actual_run": True,
        "target_version": "14.0.0",
        "source_version": source["source_version"],
        "source_commit": source["source_commit"],
        "source_tree_fingerprint": source["source_tree_fingerprint"],
        "workspace_clean": source["workspace_clean"],
        "source_identity_mode": "raw_report",
        "report_type": report_type,
        "producer": command_path,
    }
    command = f"python {command_path} --output {attachment['path']}"
    recorded_at = "2026-08-03T00:00:00Z"
    envelope: dict[str, object] = {
        "schema_version": MODULE.EXECUTION_REPORT_SCHEMA_VERSION,
        "report_type": MODULE.EXECUTION_REPORT_TYPE,
        "producer": MODULE.EXECUTION_REPORT_PRODUCER,
        "target_version": "14.0.0",
        **source,
        "actual_run": True,
        "status": "PASS",
        "case_ids": [case_id],
        "command": command,
        "command_contract": {"kind": "repository_script", "paths": [command_path]},
        "recorded_at": recorded_at,
        "execution": {
            "actual_run": True,
            "status": "PASS",
            "exit_code": 0,
            "timed_out": False,
        },
    }
    if include_attachment:
        envelope["attested_outputs"] = [attachment]
    report = evidence_root / filename
    report.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    return report, command, recorded_at, raw, attachment


def valid_stt_case_details(case_id: str) -> dict[str, object]:
    policy = MODULE.ATTESTED_CASE_POLICIES[case_id]
    required_checks = policy.get("required_checks", ())
    details: dict[str, object] = {
        "checks": {name: {"passed": True} for name in required_checks},
        "scope": {"download_only": False},
        "results": {},
    }
    if case_id == "A08":
        details["results"] = {
            "accuracy_review": {
                "manual_review_required": True,
                "decision": "NOT_AUTOMATED",
                "categories_exercised": sorted(MODULE.A08_REQUIRED_ACCURACY_CATEGORIES),
                "samples": {
                    "fixed_sample": {
                        "reference_text": "司忆正在检查 README 文件。",
                        # Deliberately differs: the generator must require the
                        # human-review payload, not auto-promote string equality.
                        "observed_text": "私意正在检查 README 文件。",
                        "expected_entities": ["司忆", "README"],
                        "observed_entities": ["README"],
                        "accuracy_review": {
                            "normalized_reference": "司忆正在检查readme文件",
                            "normalized_observed": "私意正在检查readme文件",
                            "character_error_rate": 0.1,
                            "differences": [{"operation": "replace"}],
                        },
                    }
                },
            }
        }
    elif case_id == "A09":
        details.update(
            {
                "scope": {
                    "model_download": "CALLED",
                    "user_confirmation": True,
                    "model_delete": "NOT_CALLED",
                    "download_only": True,
                },
                "model": {
                    "id": "small",
                    "repository": "Systran/faster-whisper-small",
                    "installed_before_run": False,
                    "installed_after_run": True,
                    "download_operation": "CALLED",
                    "user_confirmation": True,
                    "delete_operation": "NOT_CALLED",
                },
                "results": {
                    "model_download": {
                        "preview": {
                            "model": "small",
                            "repo_id": "Systran/faster-whisper-small",
                            "estimated_bytes": 30,
                            "target_directory": "<formal-model-root>/small",
                            "already_installed": False,
                            "available_bytes": 1_000,
                            "required_bytes": 100,
                            "fits": True,
                        },
                        "unconfirmed_request": {
                            "confirmed": False,
                            "http_status": 409,
                            "error_code": "STT_DOWNLOAD_CONFIRMATION_REQUIRED",
                            "download_started": False,
                        },
                        "confirmed_request": {
                            "confirmed": True,
                            "called": True,
                            "http_status": 202,
                            "response": {"status": "DOWNLOADING"},
                        },
                        "final_state": {
                            "model": "small",
                            "status": "INSTALLED",
                            "completed_bytes": 30,
                            "total_bytes": 30,
                            "error": None,
                        },
                        "final_model": {
                            "model": "small",
                            "model_directory": "<formal-model-root>/small",
                            "file_count": 2,
                            "actual_bytes": 30,
                            "files": [
                                {"path": "config.json", "size_bytes": 10, "sha256": "a" * 64},
                                {"path": "model.bin", "size_bytes": 20, "sha256": "b" * 64},
                            ],
                        },
                    },
                    "settings": {
                        "model_id": "small",
                        "device": "cpu",
                        "compute_type": "int8",
                        "gpu_experimental": False,
                    },
                    "load": {
                        "response": {
                            "status": "READY",
                            "device": "cpu",
                            "compute_type": "int8",
                        },
                        "status": {"loaded_model": "small", "worker_pid": 1234},
                    },
                    "unload": {
                        "unload_response": {"status": "UNLOADED"},
                        "status_after": {"loaded_model": None, "worker_pid": None},
                        "resource_release": {"worker_gone": True, "observed_release": True},
                    },
                },
            }
        )
    return details


def valid_a09_verified_receipt_details(
    root: Path,
    evidence_root: Path,
) -> tuple[dict[str, object], Path, Path]:
    controlled_script = root / "scripts" / "v14-stt-live-evidence.py"
    controlled_script.parent.mkdir(parents=True, exist_ok=True)
    controlled_script.write_text("# controlled fixture script\n", encoding="utf-8")
    source = MODULE.source_identity(root)
    prior_details = valid_stt_case_details("A09")
    prior_payload = {
        "schema_version": 1,
        "report_type": "v14_stt_live_evidence",
        "producer": "scripts/v14-stt-live-evidence.py",
        "target_version": "14.0.0",
        "status": "PASS",
        "actual_run": True,
        "source": source,
        **prior_details,
    }
    prior_raw = evidence_root / "raw" / "prior-actual-a09.json"
    prior_raw.parent.mkdir(parents=True, exist_ok=True)
    prior_raw.write_text(json.dumps(prior_payload, sort_keys=True) + "\n", encoding="utf-8")
    prior_attachment = {
        "path": MODULE.repository_relative(root, prior_raw),
        "sha256": MODULE.sha256(prior_raw),
        "bytes": prior_raw.stat().st_size,
        "status": "PASS",
        "actual_run": True,
        "target_version": "14.0.0",
        "report_type": "v14_stt_live_evidence",
        "producer": "scripts/v14-stt-live-evidence.py",
        "source_identity_mode": "raw_report",
        **source,
    }
    prior_envelope_payload = {
        "schema_version": MODULE.EXECUTION_REPORT_SCHEMA_VERSION,
        "report_type": MODULE.EXECUTION_REPORT_TYPE,
        "producer": MODULE.EXECUTION_REPORT_PRODUCER,
        "target_version": "14.0.0",
        "status": "PASS",
        "actual_run": True,
        "case_ids": ["A09"],
        "command": "python scripts/v14-stt-live-evidence.py --download-small-confirmed --download-only",
        "command_contract": {
            "kind": "repository_script",
            "paths": ["scripts/v14-stt-live-evidence.py"],
        },
        "execution": {
            "actual_run": True,
            "status": "PASS",
            "exit_code": 0,
            "timed_out": False,
        },
        "attested_outputs": [prior_attachment],
        **source,
    }
    prior_envelope = evidence_root / "executions" / "prior-actual-a09.json"
    prior_envelope.parent.mkdir(parents=True, exist_ok=True)
    prior_envelope.write_text(
        json.dumps(prior_envelope_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    prior_results = prior_details["results"]
    assert isinstance(prior_results, dict)
    prior_download = prior_results["model_download"]
    assert isinstance(prior_download, dict)
    prior_manifest = prior_download["final_model"]
    assert isinstance(prior_manifest, dict)
    current_details: dict[str, object] = {
        "checks": {
            **{
                name: {"passed": True}
                for name in MODULE.ATTESTED_CASE_POLICIES["A09"]["required_checks"]
            },
            "current_small_manifest_matches_download_receipt": {"passed": True},
        },
        "scope": {
            "model_download": "VERIFIED_PRIOR_ACTUAL",
            "user_confirmation": False,
            "prior_user_confirmation_verified": True,
            "model_delete": "NOT_CALLED",
            "download_only": True,
        },
        "model": {
            "id": "small",
            "installed_before_run": True,
            "installed_after_run": True,
            "download_operation": "VERIFIED_PRIOR_ACTUAL",
            "user_confirmation": False,
            "prior_user_confirmation_verified": True,
            "delete_operation": "NOT_CALLED",
        },
        "results": {
            "download_receipt_verification": {
                "mode": "VERIFIED_PRIOR_ACTUAL",
                "prior_raw": {
                    "path": MODULE.repository_relative(root, prior_raw),
                    "sha256": MODULE.sha256(prior_raw),
                    "bytes": prior_raw.stat().st_size,
                },
                "prior_envelope": {
                    "path": MODULE.repository_relative(root, prior_envelope),
                    "sha256": MODULE.sha256(prior_envelope),
                    "bytes": prior_envelope.stat().st_size,
                },
                "prior_source": source,
                "runner_binding_verified": True,
                "formal_confirmation_flow_verified": True,
                "prior_user_confirmation_verified": True,
                "model_manifest_exact_match": True,
                "expected_model_manifest": prior_manifest,
                "current_model_manifest": prior_manifest,
            },
            "settings": {
                "model_id": "small",
                "device": "cpu",
                "compute_type": "int8",
                "gpu_experimental": False,
            },
            "load": {
                "response": {"status": "READY", "device": "cpu", "compute_type": "int8"},
                "status": {"loaded_model": "small", "worker_pid": 5678},
            },
            "unload": {
                "unload_response": {"status": "UNLOADED"},
                "status_after": {"loaded_model": None, "worker_pid": None},
                "resource_release": {
                    "worker_gone": True,
                    "observed_release": True,
                    "stt_rss_before_bytes": 4096,
                    "stt_rss_after_bytes": None,
                },
            },
        },
    }
    return current_details, prior_raw, prior_envelope


def live_pass_ledger_entry(
    report: Path, command: str, recorded_at: str, attachment: dict[str, object]
) -> dict[str, object]:
    return {
        "path": f"build/v1400-evidence/{report.name}",
        "kind": "automated",
        "actual_run": True,
        "outcome": "PASS",
        "command": command,
        "recorded_at": recorded_at,
        "attested_outputs": [attachment["path"]],
    }


def write_a08_manual_review(
    root: Path,
    evidence_root: Path,
    raw: Path,
    *,
    decision: str = "PASS_WITH_WARNING",
    filename: str = "a08-manual-review.json",
) -> tuple[Path, dict[str, object], dict[str, object]]:
    raw_payload = json.loads(raw.read_text(encoding="utf-8"))
    raw_review = raw_payload["results"]["accuracy_review"]
    procedure = (
        "Manually compare every A08 reference and observed transcript and record "
        "bounded release claims"
    )
    recorded_at = "2026-08-03T00:05:00Z"
    source = MODULE.source_identity(root)
    report = evidence_root / "reviews" / filename
    report.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": MODULE.A08_MANUAL_REVIEW_SCHEMA_VERSION,
        "report_type": MODULE.A08_MANUAL_REVIEW_REPORT_TYPE,
        "producer": MODULE.A08_MANUAL_REVIEW_PRODUCER,
        "target_version": "14.0.0",
        "case_id": "A08",
        "actual_review": True,
        "review_mode": "manual_semantic_inspection",
        "automated_decision": False,
        "decision": decision,
        "procedure": procedure,
        "recorded_at": recorded_at,
        "reviewer": {
            "identity": "release-reviewer",
            "role": "release acceptance reviewer",
        },
        "source": source,
        "source_raw": {
            "path": MODULE.repository_relative(root, raw),
            "sha256": MODULE.sha256(raw),
            "bytes": raw.stat().st_size,
        },
        "review": {
            "categories_reviewed": raw_review["categories_exercised"],
            "sample_reviews": {
                name: {
                    "assessment": "WARNING",
                    "reference_text_reviewed": True,
                    "observed_text_reviewed": True,
                    "notes": (
                        "The transcript is usable for the narrow local-Chinese claim, "
                        "with disclosed errors."
                    ),
                }
                for name in raw_review["samples"]
            },
        },
        "acceptance": {
            "local_chinese_actual_inference": "PASS",
            "microphone_capture": "NOT_RUN",
            "input_audio_scope": "SYNTHETIC_NON_MICROPHONE",
        },
        "warning_boundaries": {
            "terminology": {
                "assessment": "WARNING",
                "observations": ["A proper noun differs from its reference."],
                "release_claim": "No terminology-accuracy PASS is claimed.",
            },
            "mixed_language": {
                "assessment": "WARNING",
                "observations": ["Only part of the mixed-language entities survived."],
                "release_claim": "No mixed-language-accuracy PASS is claimed.",
            },
            "hallucination_repetition": {
                "assessment": "PASS",
                "observations": ["No unprompted phrase was identified in this fixture."],
                "release_claim": "This assessment applies only to the reviewed fixed corpus.",
            },
        },
    }
    report.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    entry = {
        "path": MODULE.repository_relative(root, report),
        "kind": "manual",
        "actual_run": True,
        "outcome": "PASS",
        "command": procedure,
        "recorded_at": recorded_at,
    }
    return report, entry, payload


def valid_a02_case_details() -> dict[str, object]:
    required_checks = MODULE.ATTESTED_CASE_POLICIES["A02"]["required_checks"]
    return {
        "target": {
            "platform": "windows",
            "os_name": "nt",
            "sys_platform": "win32",
            "provider": "windows",
            "provider_maturity": "stable",
            "melotts_maturity": "experimental",
            "enabled_zh_cn_voice_count": 1,
        },
        "scope": {
            "production_manager": "app.tts.manager.TTSManager",
            "tts_provider": "windows",
            "fixed_text_classification": "PUBLIC_NON_SENSITIVE",
            "tts_playback": "NOT_RUN",
            "microphone_capture": "NOT_RUN",
            "ollama_actions": "NONE",
            "melotts_model_actions": "NONE",
            "network_tts_calls": "NONE",
        },
        "checks": {name: {"passed": True} for name in required_checks},
        "results": {
            "providers": {
                "windows": {
                    "provider": "windows",
                    "status": "ok",
                    "device": "cpu",
                    "version": "SAPI.SpVoice",
                    "maturity": "stable",
                },
                "melotts": {
                    "provider": "melotts",
                    "status": "unavailable",
                    "availability": "not_installed",
                    "device": "cpu",
                    "version": "optional-local",
                    "maturity": "experimental",
                    "release_gate": False,
                },
            },
            "synthesis": {
                "provider": "windows",
                "status": "READY",
                "cached": False,
                "duration_ms": 850,
                "sample_rate": 24000,
                "synthesis_ms": 75.5,
                "fixed_text_sha256": "A" * 64,
                "text_character_count": 30,
                "sensitive": False,
            },
            "wav": {
                "container": "RIFF/WAVE",
                "encoding": "PCM",
                "channels": 1,
                "sample_width_bytes": 2,
                "sample_rate": 24000,
                "frame_count": 20_400,
                "duration_ms": 850,
                "size_bytes": 40_844,
                "pcm_rms": 1024,
                "sha256": "B" * 64,
            },
            "lifecycle": {
                "initial_status": "IDLE",
                "temporary_wavs_before": [],
                "request_status_after_synthesis": "READY",
                "manager_status_after_synthesis": "IDLE",
                "queue_length_after_synthesis": 0,
                "manager_status_after_shutdown": "IDLE",
                "temporary_wavs_after_shutdown": [],
            },
        },
        "cleanup": {
            "manager_shutdown_called": True,
            "manager_shutdown_error": None,
            "manager_status_after_shutdown": "IDLE",
            "queue_length_after_shutdown": 0,
            "active_synthesis_after_shutdown": [],
            "temporary_wavs_after_shutdown": [],
        },
    }


def valid_a20_case_details() -> dict[str, object]:
    owner_hash = "a" * 64
    pid = 4567
    identity = {
        "pid": pid,
        "creation_date": "20260812123000.000000+480",
        "executable_name": "ollama.exe",
        "command_sha256": "B" * 64,
    }
    service = {
        "status": "MANAGED_RUNNING",
        "mode": "managed",
        "api_healthy": True,
        "owner_sha256": owner_hash,
        "managed_pid": pid,
        "listener_pid": pid,
        "managed_port": 11435,
    }
    fingerprint = {
        "mode": "non_mutating_api_intent",
        "regular_file_count": 3,
        "regular_file_bytes": 1024,
        "whole_tree_sha256": "c" * 64,
        "links_followed": False,
    }
    hardware_snapshot = {
        "ollama_pid": pid,
        "system_total_bytes": 16 * 1024**3,
        "gpu_total_bytes": 6 * 1024**3,
    }
    resource = {
        "snapshot": hardware_snapshot,
        "ollama_listener_pid": pid,
        "admission": {},
    }
    required_checks = MODULE.ATTESTED_CASE_POLICIES["A20"]["required_checks"]
    return {
        "checks": {name: {"passed": True} for name in required_checks},
        "scope": {
            "ollama_url": "http://127.0.0.1:11435",
            "external_11434_policy": "non_mutating_intent_not_used_for_preload_or_unload",
            "test_owned_service_owner_sha256": owner_hash,
            "qwen_model": "qwen3:4b",
            "model_store_policy": "non_mutating_api_intent_full_tree_verified",
            "stt_model": "small",
            "stt_model_source": "app.stt.schemas.DEFAULT_STT_MODEL_ID",
            "stt_model_fallback": "DISALLOWED",
            "stt_device": "cpu",
            "stt_compute_type": "int8",
            "tts_provider": "windows",
            "model_download": "NOT_CALLED",
            "model_delete": "NOT_CALLED",
            "microphone_capture": "NOT_RUN",
            "tts_playback": "NOT_RUN",
            "desktop_renderer": "NOT_RUN",
            "endurance": "NOT_RUN",
        },
        "runtime": {
            "database_isolated": True,
            "logs_isolated": True,
            "temporary_audio_isolated": True,
            "formal_model_link": {"kind": "junction", "copy_performed": False},
        },
        "stt_model": {
            "id": "small",
            "installed_before_run": True,
            "file_count": 3,
            "size_bytes": 1024,
            "download_operation": "NOT_CALLED",
            "delete_operation": "NOT_CALLED",
        },
        "results": {
            "ollama_model_store_before": fingerprint,
            "ollama_service_before": {
                "mode": "test_owned_managed",
                "owner_sha256": owner_hash,
                "service": service,
                "identity": identity,
            },
            "preflight": {
                "ok": True,
                "action": "preflight",
                "running": [],
                "installed": [{"name": "qwen3:4b"}],
                "resources": {
                    "snapshot": hardware_snapshot,
                    "admission": {
                        "model_preload": {"allowed": True},
                        "stt_cpu": {"allowed": True},
                        "voice": {"allowed": True},
                    },
                },
            },
            "resources_before_preload": resource,
            "qwen_preload": {
                "ok": True,
                "action": "preload_qwen",
                "loaded": {"status": "LOADED", "model": "qwen3:4b"},
                "running_after": [{"name": "qwen3:4b"}],
            },
            "resources_with_qwen": resource,
            "stt_with_qwen": {
                "settings": {
                    "model_id": "small", "device": "cpu", "compute_type": "int8",
                    "gpu_experimental": False,
                },
                "load": {"status": "READY", "model": "small"},
                "status": {"loaded_model": "small", "worker_pid": 9876},
                "transcription": {
                    "provider": "faster_whisper", "model": "small", "text": "fixture",
                },
                "resources": resource,
            },
            "tts_with_qwen_and_stt": {
                "settings": {
                    "provider": "windows", "fallback_provider": "windows",
                    "allow_fallback": False, "cache_enabled": False,
                },
                "provider": "windows",
                "duration_ms": 100,
                "audio": {
                    "http_status": 200, "bytes": 2048, "frames": 100,
                    "sample_rate": 24000, "channels": 1,
                },
                "playback": {
                    "queued_status": "QUEUED", "started_status": "PLAYING",
                    "completed_status": "COMPLETED", "queue_after": 0,
                    "status_after": "IDLE", "active_synthesis_after": 0,
                },
                "resources": resource,
            },
            "tts_temp_residual_after_lifecycle": [],
        },
        "cleanup": {
            "test_owned_ollama_start_attempted": True,
            "qwen_loaded_by_script": True,
            "stt_unload": {"status": "UNLOADED", "model": "small"},
            "qwen_unload": {
                "ok": True,
                "action": "unload_owned_qwen",
                "unloaded": {"status": "UNLOADED", "model": "qwen3:4b"},
                "running_before": [{"name": "qwen3:4b"}],
                "running_after": [],
            },
            "sidecar": {"tree_cleanup": {"tree_terminated": True}},
            "tts_temp_residual_after_lifecycle": [],
            "tts_temp_residual_after_sidecar_stop": [],
            "formal_models_link_removed": True,
            "test_owned_ollama_stop": {
                "ok": True,
                "action": "stop_test_owned_service",
                "service": {
                    "stopped": True,
                    "status": "INSTALLED_STOPPED",
                    "api_healthy": False,
                    "mode": None,
                    "managed_pid": None,
                    "listener_pid": None,
                    "managed_port": None,
                    "managed_started_at": None,
                    "owner_sha256": None,
                    "managed_state_unowned": False,
                },
            },
            "ollama_processes_after_stop": [],
            "ollama_model_store_after": fingerprint,
        },
    }


def valid_a26_case_details() -> dict[str, object]:
    owner_hash = "a" * 64
    identity = {
        "pid": 4567,
        "creation_date": "20260812123000.000000+480",
        "executable_name": "ollama.exe",
        "command_sha256": "B" * 64,
    }
    service = {
        "status": "MANAGED_RUNNING",
        "mode": "managed",
        "api_healthy": True,
        "owner_sha256": owner_hash,
        "managed_pid": 4567,
        "listener_pid": 4567,
        "managed_port": 11435,
        "managed_state_unowned": False,
    }
    stopped_service = {
        "stopped": True,
        "status": "INSTALLED_STOPPED",
        "api_healthy": False,
        "mode": None,
        "managed_pid": None,
        "listener_pid": None,
        "managed_port": None,
        "managed_started_at": None,
        "owner_sha256": None,
        "managed_state_unowned": False,
    }
    model_store = {
        "mode": "non_mutating_api_intent",
        "regular_file_count": 2,
        "regular_file_bytes": 100,
        "whole_tree_sha256": "c" * 64,
        "links_followed": False,
    }
    required_checks = MODULE.ATTESTED_CASE_POLICIES["A26"]["required_checks"]
    return {
        "target": {
            "platform": "windows",
            "os_name": "nt",
            "sys_platform": "win32",
            "service_mode": "test_owned_managed",
            "base_url": "http://127.0.0.1:11435",
            "port": 11435,
            "model": "qwen3:4b",
            "keep_alive": "5m",
            "owner_sha256": owner_hash,
        },
        "scope": {
            "external_11434_policy": "PROTECTED_NOT_TOUCHED",
            "test_owned_11435_only": True,
            "model_store": "NON_MUTATING_API_INTENT_FULL_TREE_VERIFIED",
            "model_download": "NOT_CALLED",
            "model_delete": "NOT_CALLED",
            "chat_prompt": "NOT_SENT",
            "paid_provider_calls": "NONE",
            "stt_actions": "NONE",
            "tts_actions": "NONE",
            "microphone_capture": "NOT_RUN",
        },
        "checks": {name: {"passed": True} for name in required_checks},
        "results": {
            "ollama_processes_before": [],
            "test_owned_port_before": {"port": 11435, "listening": False},
            "model_store_before": model_store,
            "service_start": {
                "controller": {
                    "ok": True,
                    "action": "start_test_owned_service",
                    "service": service,
                },
                "process_identity": identity,
            },
            "preflight": {
                "ok": True,
                "action": "preflight",
                "service": service,
                "running": [],
                "installed": [{"name": "qwen3:4b", "loaded": False}],
            },
            "preload": {
                "ok": True,
                "action": "preload_qwen",
                "service": service,
                "loaded": {
                    "status": "LOADED",
                    "model": "qwen3:4b",
                    "keep_alive": "5m",
                    "load_ms": 123.5,
                },
                "running_after": [{"name": "qwen3:4b"}],
            },
            "process_identity_after_preload": identity,
        },
        "cleanup": {
            "processes_before_unload": [identity],
            "unload": {
                "ok": True,
                "action": "unload_owned_qwen",
                "service": service,
                "unloaded": {
                    "status": "UNLOADED",
                    "model": "qwen3:4b",
                    "resource_release_observed": {
                        "ollama_rss_delta_bytes": 1024,
                        "gpu_free_delta_bytes": 0,
                    },
                },
                "running_before": [{"name": "qwen3:4b"}],
                "running_after": [],
            },
            "processes_before_stop": [identity],
            "only_test_owned_before_stop": True,
            "service_stop": {
                "ok": True,
                "action": "stop_test_owned_service",
                "service": stopped_service,
            },
            "model_store_after": model_store,
            "ollama_processes_after_stop": [],
            "test_owned_port_after_stop": {"port": 11435, "listening": False},
        },
        "test_summary": {
            "framework": "structured_production_lifecycle_probes",
            "collected": len(MODULE.A26_REQUIRED_STRUCTURED_TESTS),
            "executed": len(MODULE.A26_REQUIRED_STRUCTURED_TESTS),
            "passed": len(MODULE.A26_REQUIRED_STRUCTURED_TESTS),
            "failed": 0,
            "skipped": 0,
            "tests": [
                {"name": name, "status": "PASS"}
                for name in MODULE.A26_REQUIRED_STRUCTURED_TESTS
            ],
        },
    }


def test_default_ledger_covers_exactly_the_v14_acceptance_matrix() -> None:
    ledger = MODULE.default_ledger()

    assert ledger["target_version"] == "14.0.0"
    assert [case["id"] for case in ledger["cases"]] == [f"A{number:02d}" for number in range(1, 29)]
    assert {case["status"] for case in ledger["cases"]} == {"NOT_RUN"}


def test_target_v14_evidence_can_be_generated_before_version_sync(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    ledger = MODULE.default_ledger()

    documents = MODULE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=evidence_root,
        expected_plan_sha256=MODULE.sha256(plan),
    )

    matrix = json.loads((evidence_root / "TEST_MATRIX.json").read_text(encoding="utf-8"))
    manifest = json.loads((evidence_root / "EVIDENCE_MANIFEST.json").read_text(encoding="utf-8"))
    assert matrix["target_version"] == "14.0.0"
    assert matrix["source_version"] == "13.0.0"
    assert isinstance(matrix["source_tree_fingerprint"], str)
    assert matrix["summary"]["not_run"] == 28
    assert documents["status"]["release_status"] == "BLOCKED"
    assert documents["status"]["distribution_status"] == "NOT_READY"
    assert documents["status"]["release_metadata_check"]["status"] == "BLOCKED_PRE_VERSION_SYNC"
    assert manifest["evidence_root"] == "build/v1400-evidence"
    feedback = (evidence_root / "IMPLEMENTATION_FEEDBACK.md").read_text(encoding="utf-8")
    for heading in (
        "## Git 状态",
        "## 版本与 Schema",
        "## 发布门禁处理",
        "## TTS 正式化",
        "## 麦克风与录音",
        "## STT Provider",
        "## STT 模型",
        "## STT API",
        "## Voice Session",
        "## 前端交互",
        "## Agent 联动",
        "## 停止与打断",
        "## 资源",
        "## 隐私",
        "## 性能",
        "## 30 分钟耐久",
        "## 故障注入",
        "## NSIS",
        "## MSI",
        "## 已知问题",
        "## 技术债务",
        "## 后续候选",
        "## 用户决策",
        "## 机器可读摘要",
    ):
        assert heading in feedback
    assert "SQLite Schema=`42`" in feedback
    for filename in MODULE.GENERATED_DOCUMENT_FILENAMES:
        assert (root / "docs" / "14.0.0" / filename).read_bytes() == (
            evidence_root / filename
        ).read_bytes()


def test_pass_is_rejected_without_current_execution_artifact(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    static_source = root / "tests" / "static_reference.py"
    static_source.parent.mkdir()
    static_source.write_text("# source is not runtime evidence\n", encoding="utf-8")
    ledger = MODULE.default_ledger()
    ledger["cases"][0] = {
        "id": "A01",
        "status": "PASS",
        "evidence": [
            {
                "path": "tests/static_reference.py",
                "kind": "automated",
                "actual_run": True,
                "outcome": "PASS",
                "command": "pytest tests/static_reference.py",
                "recorded_at": "2026-08-03T00:00:00Z",
            }
        ],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match="must be collected under"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_pass_requires_a_target_version_execution_report(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report = evidence_root / "a01-baseline.json"
    command, command_contract = controlled_test_command(root)
    recorded_at = "2026-08-03T00:00:00Z"
    source = MODULE.source_identity(root)
    report.write_text(
        json.dumps(
            {
                "schema_version": MODULE.EXECUTION_REPORT_SCHEMA_VERSION,
                "report_type": MODULE.EXECUTION_REPORT_TYPE,
                "producer": MODULE.EXECUTION_REPORT_PRODUCER,
                "target_version": "14.0.0",
                **source,
                "actual_run": True,
                "status": "PASS",
                "case_ids": ["A01"],
                "command": command,
                "command_contract": command_contract,
                "recorded_at": recorded_at,
                "execution": {
                    "actual_run": True,
                    "status": "PASS",
                    "exit_code": 0,
                    "timed_out": False,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][0] = {
        "id": "A01",
        "status": "PASS",
        "result": "已执行受控基线核验。",
        "evidence": [
            {
                "path": "build/v1400-evidence/a01-baseline.json",
                "kind": "automated",
                "actual_run": True,
                "outcome": "PASS",
                "command": command,
                "recorded_at": recorded_at,
            }
        ],
    }

    documents = MODULE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=evidence_root,
        expected_plan_sha256=MODULE.sha256(plan),
    )

    assert documents["matrix"]["cases"][0]["status"] == "PASS"
    assert documents["matrix"]["cases"][0]["evidence"][0]["sha256"] == MODULE.sha256(report)


def test_live_pass_requires_runner_bound_raw_attachment(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id="A20", include_attachment=False
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][19] = {
        "id": "A20",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match="missing its runner-bound attested_outputs"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_live_pass_rejects_post_run_raw_report_replacement(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, raw, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id="A24"
    )
    raw.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "v14_a24_stt_privacy_live_evidence",
                "producer": "scripts/v14-stt-privacy-live-evidence.py",
                "target_version": "14.0.0",
                "status": "PASS",
                "actual_run": True,
                "source": MODULE.source_identity(root),
                "note": "tampered after runner completion",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][23] = {
        "id": "A24",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match="SHA-256/bytes"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_live_pass_requires_the_case_specific_raw_report_and_command(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, raw, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id="A20"
    )
    raw_payload = json.loads(raw.read_text(encoding="utf-8"))
    raw_payload["report_type"] = "v14_stt_live_evidence"
    raw.write_text(json.dumps(raw_payload, sort_keys=True) + "\n", encoding="utf-8")
    attachment["report_type"] = "v14_stt_live_evidence"
    attachment["sha256"] = MODULE.sha256(raw)
    attachment["bytes"] = raw.stat().st_size
    envelope = json.loads(report.read_text(encoding="utf-8"))
    envelope["attested_outputs"] = [attachment]
    report.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    ledger = MODULE.default_ledger()
    ledger["cases"][19] = {
        "id": "A20",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match="report_type=v14_resource_live_evidence"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_a20_accepts_complete_owned_resource_and_cleanup_evidence(tmp_path: Path) -> None:
    details = valid_a20_case_details()

    MODULE._validate_case_specific_attested_payload(
        details,
        case_id="A20",
        policy=MODULE.ATTESTED_CASE_POLICIES["A20"],
        repository_root=tmp_path,
        evidence_root=tmp_path,
    )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("manifest_only", "full model-store tree fingerprint"),
        ("wrong_stt_model", "small-STT proof"),
        ("wrong_hardware", "idle preflight/admission proof"),
        ("start_not_attempted", "cleanup/store settlement"),
        ("store_changed", "cleanup/store settlement"),
    ],
)
def test_a20_rejects_shallow_or_unsettled_resource_claims(
    tmp_path: Path, mutation: str, error: str
) -> None:
    details = valid_a20_case_details()
    results = details["results"]
    cleanup = details["cleanup"]
    assert isinstance(results, dict) and isinstance(cleanup, dict)
    if mutation == "manifest_only":
        store = results["ollama_model_store_before"]
        assert isinstance(store, dict)
        store["mode"] = "read_only_dependency"
    elif mutation == "wrong_stt_model":
        stt = results["stt_with_qwen"]
        assert isinstance(stt, dict) and isinstance(stt["transcription"], dict)
        stt["transcription"]["model"] = "base"
    elif mutation == "wrong_hardware":
        preflight = results["preflight"]
        assert isinstance(preflight, dict) and isinstance(preflight["resources"], dict)
        snapshot = preflight["resources"]["snapshot"]
        assert isinstance(snapshot, dict)
        snapshot["gpu_total_bytes"] = 4 * 1024**3
    elif mutation == "start_not_attempted":
        cleanup["test_owned_ollama_start_attempted"] = False
    else:
        after = dict(cleanup["ollama_model_store_after"])
        after["whole_tree_sha256"] = "d" * 64
        cleanup["ollama_model_store_after"] = after

    with pytest.raises(MODULE.EvidenceValidationError, match=error):
        MODULE._validate_case_specific_attested_payload(
            details,
            case_id="A20",
            policy=MODULE.ATTESTED_CASE_POLICIES["A20"],
            repository_root=tmp_path,
            evidence_root=tmp_path,
        )


def test_a02_pass_requires_and_accepts_source_bound_real_windows_tts_report(
    tmp_path: Path,
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id="A02",
        raw_details=valid_a02_case_details(),
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][1] = {
        "id": "A02",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    documents = MODULE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=evidence_root,
        expected_plan_sha256=MODULE.sha256(plan),
    )

    assert documents["matrix"]["cases"][1]["status"] == "PASS"


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("melotts_stable", "provider maturity/status"),
        ("melotts_unavailable_without_state", "provider maturity/status"),
        ("residual_audio", "manager lifecycle"),
        ("silent_pcm", "RIFF/PCM"),
    ],
)
def test_a02_rejects_metadata_only_or_incomplete_live_claims(
    tmp_path: Path, mutation: str, error: str
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    details = valid_a02_case_details()
    results = details["results"]
    assert isinstance(results, dict)
    if mutation == "melotts_stable":
        providers = results["providers"]
        assert isinstance(providers, dict) and isinstance(providers["melotts"], dict)
        providers["melotts"]["maturity"] = "stable"
    elif mutation == "melotts_unavailable_without_state":
        providers = results["providers"]
        assert isinstance(providers, dict) and isinstance(providers["melotts"], dict)
        providers["melotts"]["availability"] = "ready"
    elif mutation == "residual_audio":
        lifecycle = results["lifecycle"]
        assert isinstance(lifecycle, dict)
        lifecycle["temporary_wavs_after_shutdown"] = ["residual.wav"]
    else:
        wav = results["wav"]
        assert isinstance(wav, dict)
        wav["pcm_rms"] = 0
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id="A02", raw_details=details
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][1] = {
        "id": "A02",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match=error):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_a26_accepts_only_complete_structured_test_owned_ollama_evidence(
    tmp_path: Path,
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id="A26",
        raw_details=valid_a26_case_details(),
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][25] = {
        "id": "A26",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    documents = MODULE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=evidence_root,
        expected_plan_sha256=MODULE.sha256(plan),
    )

    assert documents["matrix"]["cases"][25]["status"] == "PASS"


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("all_skipped", "all structured Ollama probes"),
        ("no_release", "resource release"),
        ("pid_changed", "process identity changed"),
        ("extra_process_before_stop", "external-process protection"),
        ("links_followed", "non-mutating full-tree model-store fingerprint"),
        ("model_store_changed", "model-store protection"),
    ],
)
def test_a26_rejects_all_skip_or_unbound_lifecycle_claims(
    tmp_path: Path, mutation: str, error: str
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    details = valid_a26_case_details()
    if mutation == "all_skipped":
        summary = details["test_summary"]
        assert isinstance(summary, dict)
        summary.update({"executed": 0, "passed": 0, "skipped": 5})
        tests = summary["tests"]
        assert isinstance(tests, list)
        for item in tests:
            assert isinstance(item, dict)
            item["status"] = "SKIPPED"
    elif mutation == "no_release":
        cleanup = details["cleanup"]
        assert isinstance(cleanup, dict)
        unload = cleanup["unload"]
        assert isinstance(unload, dict)
        unloaded = unload["unloaded"]
        assert isinstance(unloaded, dict)
        unloaded["resource_release_observed"] = {
            "ollama_rss_delta_bytes": 0,
            "gpu_free_delta_bytes": 0,
        }
    elif mutation == "extra_process_before_stop":
        cleanup = details["cleanup"]
        assert isinstance(cleanup, dict)
        cleanup["only_test_owned_before_stop"] = False
    elif mutation == "links_followed":
        results = details["results"]
        assert isinstance(results, dict)
        model_store = results["model_store_before"]
        assert isinstance(model_store, dict)
        model_store["links_followed"] = True
    elif mutation == "model_store_changed":
        cleanup = details["cleanup"]
        assert isinstance(cleanup, dict)
        changed = dict(cleanup["model_store_after"])
        changed["whole_tree_sha256"] = "d" * 64
        cleanup["model_store_after"] = changed
    else:
        results = details["results"]
        assert isinstance(results, dict)
        changed = dict(results["process_identity_after_preload"])
        changed["pid"] = 9999
        results["process_identity_after_preload"] = changed
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id="A26", raw_details=details
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][25] = {
        "id": "A26",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match=error):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


@pytest.mark.parametrize("case_id", ["A08", "A09", "A10", "A11", "A21"])
def test_stt_live_pass_requires_and_accepts_case_specific_checks(
    tmp_path: Path, case_id: str
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, raw, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id=case_id,
        raw_details=valid_stt_case_details(case_id),
    )
    evidence = [live_pass_ledger_entry(report, command, recorded_at, attachment)]
    if case_id == "A08":
        _, manual_entry, _ = write_a08_manual_review(root, evidence_root, raw)
        evidence.append(manual_entry)
    ledger = MODULE.default_ledger()
    ledger["cases"][int(case_id[1:]) - 1] = {
        "id": case_id,
        "status": "PASS",
        "evidence": evidence,
    }

    documents = MODULE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=evidence_root,
        expected_plan_sha256=MODULE.sha256(plan),
    )

    normalized_case = documents["matrix"]["cases"][int(case_id[1:]) - 1]
    assert normalized_case["status"] == "PASS"
    if case_id == "A08":
        manual = next(item for item in normalized_case["evidence"] if item["kind"] == "manual")
        assert manual["decision"] == "PASS_WITH_WARNING"
        assert manual["source_raw"]["path"] == attachment["path"]


@pytest.mark.parametrize("case_id", ["A08", "A09", "A10", "A11", "A21"])
def test_stt_live_pass_rejects_a_missing_case_specific_check(
    tmp_path: Path, case_id: str
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    details = valid_stt_case_details(case_id)
    checks = details["checks"]
    assert isinstance(checks, dict)
    missing_check = str(MODULE.ATTESTED_CASE_POLICIES[case_id]["required_checks"][0])
    checks.pop(missing_check)
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id=case_id, raw_details=details
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][int(case_id[1:]) - 1] = {
        "id": case_id,
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(
        MODULE.EvidenceValidationError,
        match=rf"missing required check {missing_check}",
    ):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_a08_live_pass_requires_human_reviewable_fixed_corpus_data(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    details = valid_stt_case_details("A08")
    results = details["results"]
    assert isinstance(results, dict)
    review = results["accuracy_review"]
    assert isinstance(review, dict)
    review["categories_exercised"] = ["ordinary_chinese"]
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id="A08", raw_details=details
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][7] = {
        "id": "A08",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match="missing fixed-corpus categories"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_a08_automatic_raw_cannot_promote_pass_without_independent_manual_review(
    tmp_path: Path,
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id="A08",
        raw_details=valid_stt_case_details("A08"),
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][7] = {
        "id": "A08",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(
        MODULE.EvidenceValidationError,
        match="independent structured manual accuracy review evidence",
    ):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("source_identity", "does not match the current source identity"),
        ("raw_sha", "no longer matches its SHA-256/bytes"),
        ("sample_coverage", "must cover every source_raw reference/observed sample"),
        ("warning_boundary", "must explicitly bound terminology"),
        ("microphone_claim", "must not claim microphone capture"),
        ("automated_decision", "manual-review declaration"),
        ("warning_decision_without_warning", "requires at least one explicit warning boundary"),
    ],
)
def test_a08_manual_review_rejects_unbound_or_incomplete_claims(
    tmp_path: Path, mutation: str, error: str
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, raw, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id="A08",
        raw_details=valid_stt_case_details("A08"),
    )
    review_path, manual_entry, payload = write_a08_manual_review(root, evidence_root, raw)
    if mutation == "source_identity":
        payload["source"]["source_tree_fingerprint"] = "F" * 64
    elif mutation == "raw_sha":
        payload["source_raw"]["sha256"] = "F" * 64
    elif mutation == "sample_coverage":
        payload["review"]["sample_reviews"].pop("fixed_sample")
    elif mutation == "warning_boundary":
        payload["warning_boundaries"].pop("terminology")
    elif mutation == "microphone_claim":
        payload["acceptance"]["microphone_capture"] = "PASS"
    elif mutation == "automated_decision":
        payload["automated_decision"] = True
    else:
        for sample in payload["review"]["sample_reviews"].values():
            sample["assessment"] = "PASS"
        for boundary in payload["warning_boundaries"].values():
            boundary["assessment"] = "PASS"
    review_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    ledger = MODULE.default_ledger()
    ledger["cases"][7] = {
        "id": "A08",
        "status": "PASS",
        "evidence": [
            live_pass_ledger_entry(report, command, recorded_at, attachment),
            manual_entry,
        ],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match=error):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_a08_manual_review_must_reference_the_runner_attested_raw_file(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report, command, recorded_at, raw, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id="A08",
        raw_details=valid_stt_case_details("A08"),
    )
    copied_raw = raw.with_name("a08-unattested-copy.json")
    copied_raw.write_bytes(raw.read_bytes())
    _, manual_entry, _ = write_a08_manual_review(root, evidence_root, copied_raw)
    ledger = MODULE.default_ledger()
    ledger["cases"][7] = {
        "id": "A08",
        "status": "PASS",
        "evidence": [
            live_pass_ledger_entry(report, command, recorded_at, attachment),
            manual_entry,
        ],
    }

    with pytest.raises(
        MODULE.EvidenceValidationError,
        match="source_raw is not the runner-attested STT raw evidence",
    ):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


@pytest.mark.parametrize("failure_kind", ["confirmation_refusal", "integrity_manifest"])
def test_a09_live_pass_rejects_incomplete_formal_download_proof(
    tmp_path: Path, failure_kind: str
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    details = valid_stt_case_details("A09")
    results = details["results"]
    assert isinstance(results, dict)
    download = results["model_download"]
    assert isinstance(download, dict)
    if failure_kind == "confirmation_refusal":
        rejected = download["unconfirmed_request"]
        assert isinstance(rejected, dict)
        rejected["http_status"] = 200
    else:
        manifest = download["final_model"]
        assert isinstance(manifest, dict)
        files = manifest["files"]
        assert isinstance(files, list) and isinstance(files[0], dict)
        files[0]["sha256"] = "not-a-sha256"
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root, evidence_root, case_id="A09", raw_details=details
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][8] = {
        "id": "A09",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    expected = (
        "confirmed=false refusal"
        if failure_kind == "confirmation_refusal"
        else "manifest file is invalid"
    )
    with pytest.raises(MODULE.EvidenceValidationError, match=expected):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_a09_accepts_current_source_verification_of_prior_actual_download(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    details, _, _ = valid_a09_verified_receipt_details(root, evidence_root)
    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id="A09",
        filename="current-a09-envelope.json",
        raw_details=details,
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][8] = {
        "id": "A09",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    documents = MODULE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=evidence_root,
        expected_plan_sha256=MODULE.sha256(plan),
    )

    assert documents["matrix"]["cases"][8]["status"] == "PASS"


@pytest.mark.parametrize(
    "tamper_kind,expected",
    [
        ("prior_raw_hash", "receipt file hash/size binding changed"),
        ("runner_binding", "prior A09 envelope no longer binds"),
        ("manifest", "current installed small-model manifest does not exactly match"),
        ("rss", "unload/RSS release was not proven"),
    ],
)
def test_a09_verified_receipt_rejects_tampering(
    tmp_path: Path, tamper_kind: str, expected: str
) -> None:
    root, plan, evidence_root = repository(tmp_path)
    details, prior_raw, prior_envelope = valid_a09_verified_receipt_details(root, evidence_root)
    results = details["results"]
    assert isinstance(results, dict)
    receipt = results["download_receipt_verification"]
    assert isinstance(receipt, dict)
    if tamper_kind == "prior_raw_hash":
        prior_raw_meta = receipt["prior_raw"]
        assert isinstance(prior_raw_meta, dict)
        prior_raw_meta["sha256"] = "0" * 64
    elif tamper_kind == "runner_binding":
        envelope_payload = json.loads(prior_envelope.read_text(encoding="utf-8"))
        envelope_payload["attested_outputs"][0]["sha256"] = "0" * 64
        prior_envelope.write_text(
            json.dumps(envelope_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        prior_envelope_meta = receipt["prior_envelope"]
        assert isinstance(prior_envelope_meta, dict)
        prior_envelope_meta["sha256"] = MODULE.sha256(prior_envelope)
        prior_envelope_meta["bytes"] = prior_envelope.stat().st_size
    elif tamper_kind == "manifest":
        manifest = receipt["current_model_manifest"]
        assert isinstance(manifest, dict)
        files = manifest["files"]
        assert isinstance(files, list) and isinstance(files[0], dict)
        files[0]["sha256"] = "0" * 64
    else:
        unload = results["unload"]
        assert isinstance(unload, dict)
        release = unload["resource_release"]
        assert isinstance(release, dict)
        release["stt_rss_before_bytes"] = None

    report, command, recorded_at, _, attachment = write_attested_live_execution_report(
        root,
        evidence_root,
        case_id="A09",
        filename=f"current-a09-{tamper_kind}.json",
        raw_details=details,
    )
    ledger = MODULE.default_ledger()
    ledger["cases"][8] = {
        "id": "A09",
        "status": "PASS",
        "evidence": [live_pass_ledger_entry(report, command, recorded_at, attachment)],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match=expected):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_source_version_13_can_never_be_release_ready_for_v14() -> None:
    cases = [
        {"id": case_id, "status": "PASS", "evidence": [{"actual_run": True, "outcome": "PASS", "sha256": "A" * 64}]}
        for case_id, _ in MODULE.CASE_CATALOG
    ]
    status, _ = MODULE.release_state(
        target_version="14.0.0",
        source={"source_version": "13.0.0", "workspace_clean": True},
        matrix_summary={"fail": 0, "blocked": 0, "not_run": 0, "skipped": 0},
        redlines={"security": "PASS", "privacy": "PASS"},
        requested={"requested_status": "READY_FOR_RELEASE", "reason": "synthetic all-pass"},
        cases=cases,
        not_applicable_decisions={},
    )
    assert status == "BLOCKED"


def test_pass_rejects_a_json_placeholder_without_execution_identity(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report = evidence_root / "a01-placeholder.json"
    report.write_text('{"status":"PASS","recorded_at":"2026-08-03T00:00:00Z"}\n', encoding="utf-8")
    ledger = MODULE.default_ledger()
    ledger["cases"][0] = {
        "id": "A01",
        "status": "PASS",
        "evidence": [
            {
                "path": "build/v1400-evidence/a01-placeholder.json",
                "kind": "automated",
                "actual_run": True,
                "outcome": "PASS",
                "command": "python scripts/example-baseline.py",
                "recorded_at": "2026-08-03T00:00:00Z",
            }
        ],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match="schema_version"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_pass_rejects_non_runner_or_unsuccessful_execution_envelope(tmp_path: Path) -> None:
    root, _, evidence_root = repository(tmp_path)
    report = evidence_root / "a01-strict-envelope.json"
    command = "python scripts/example-baseline.py"
    recorded_at = "2026-08-03T00:00:00Z"
    source = MODULE.source_identity(root)
    payload = {
        "schema_version": MODULE.EXECUTION_REPORT_SCHEMA_VERSION,
        "report_type": MODULE.EXECUTION_REPORT_TYPE,
        "producer": MODULE.EXECUTION_REPORT_PRODUCER,
        "target_version": "14.0.0",
        **source,
        "actual_run": True,
        "status": "PASS",
        "case_ids": ["A01"],
        "command": command,
        "recorded_at": recorded_at,
        "execution": {
            "actual_run": True,
            "status": "PASS",
            "exit_code": 0,
            "timed_out": False,
        },
    }

    payload["producer"] = "handwritten-placeholder"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MODULE.EvidenceValidationError, match="produced by"):
        MODULE.validate_pass_report(
            report,
            target_version="14.0.0",
            case_id="A01",
            command=command,
            recorded_at=recorded_at,
            source=source,
        )

    payload["producer"] = MODULE.EXECUTION_REPORT_PRODUCER
    payload["execution"]["exit_code"] = 1
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MODULE.EvidenceValidationError, match="exit_code=0"):
        MODULE.validate_pass_report(
            report,
            target_version="14.0.0",
            case_id="A01",
            command=command,
            recorded_at=recorded_at,
            source=source,
        )


def test_pass_rejects_an_envelope_for_a_different_dirty_source_tree(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)
    report = evidence_root / "a01-stale-tree.json"
    command = "python scripts/example-baseline.py"
    recorded_at = "2026-08-03T00:00:00Z"
    source = MODULE.source_identity(root)
    report.write_text(
        json.dumps(
            {
                "schema_version": MODULE.EXECUTION_REPORT_SCHEMA_VERSION,
                "report_type": MODULE.EXECUTION_REPORT_TYPE,
                "producer": MODULE.EXECUTION_REPORT_PRODUCER,
                "target_version": "14.0.0",
                **source,
                "actual_run": True,
                "status": "PASS",
                "case_ids": ["A01"],
                "command": command,
                "recorded_at": recorded_at,
                "execution": {
                    "actual_run": True,
                    "status": "PASS",
                    "exit_code": 0,
                    "timed_out": False,
                },
            }
        ),
        encoding="utf-8",
    )
    changed_source = root / "siyi" / "app.py"
    changed_source.parent.mkdir()
    changed_source.write_text("# changed after the test run\n", encoding="utf-8")
    ledger = MODULE.default_ledger()
    ledger["cases"][0] = {
        "id": "A01",
        "status": "PASS",
        "evidence": [
            {
                "path": "build/v1400-evidence/a01-stale-tree.json",
                "kind": "automated",
                "actual_run": True,
                "outcome": "PASS",
                "command": command,
                "recorded_at": recorded_at,
            }
        ],
    }

    with pytest.raises(MODULE.EvidenceValidationError, match="source_tree_fingerprint"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=evidence_root,
            expected_plan_sha256=MODULE.sha256(plan),
        )


def test_default_generator_rejects_a_marker_matching_but_unlocked_plan(tmp_path: Path) -> None:
    root, plan, evidence_root = repository(tmp_path)

    with pytest.raises(MODULE.EvidenceValidationError, match="locked v14 implementation plan"):
        MODULE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=MODULE.default_ledger(),
            output_root=evidence_root,
        )


def test_not_applicable_needs_a_tracked_formal_decision_and_current_case_evidence(
    tmp_path: Path,
) -> None:
    root, _, evidence_root = repository(tmp_path)
    decision_path = root / "docs" / "14.0.0" / "decisions" / "optional-model.json"
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "document_type": "v14_formal_product_decision",
                "target_version": "14.0.0",
                "case_id": "A09",
                "decision_id": "PD-V14-OPTIONAL_MODEL",
                "disposition": "NOT_APPLICABLE",
                "approved_by": "product-owner",
                "approved_at": "2026-08-03T00:00:00Z",
                "reason": "model download needs explicit user authorization",
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "docs/14.0.0/decisions/optional-model.json"],
        cwd=root,
        check=True,
    )
    report, command, recorded_at = write_current_execution_report(
        root, evidence_root, case_ids=["A09"], filename="a09-decision.json"
    )
    cases = MODULE.normalize_cases(
        {
            "cases": [
                {
                    "id": case_id,
                    "status": "NOT_APPLICABLE" if case_id == "A09" else "NOT_RUN",
                    "reason": "formal optional-product decision" if case_id == "A09" else "not run",
                    "evidence": [],
                }
                for case_id, _ in MODULE.CASE_CATALOG
            ]
        },
        repository_root=root,
        evidence_root=evidence_root,
        target_version="14.0.0",
        source=MODULE.source_identity(root),
    )
    decisions = MODULE.normalize_not_applicable_decisions(
        {
            "A09": {
                "decision_id": "PD-V14-OPTIONAL_MODEL",
                "document": "docs/14.0.0/decisions/optional-model.json",
                "evidence": [
                    {
                        "path": "build/v1400-evidence/a09-decision.json",
                        "kind": "automated",
                        "actual_run": True,
                        "outcome": "PASS",
                        "command": command,
                        "recorded_at": recorded_at,
                    }
                ],
            }
        },
        repository_root=root,
        evidence_root=evidence_root,
        target_version="14.0.0",
        source=MODULE.source_identity(root),
        cases=cases,
    )
    assert decisions["A09"]["decision_id"] == "PD-V14-OPTIONAL_MODEL"
    status, _ = MODULE.release_state(
        target_version="14.0.0",
        source={"source_version": "14.0.0", "workspace_clean": True},
        matrix_summary={"fail": 0, "blocked": 0, "not_run": 0, "skipped": 0},
        redlines={"security": "PASS", "privacy": "PASS"},
        requested={"requested_status": "READY_FOR_RELEASE", "reason": "ready"},
        cases=cases,
        not_applicable_decisions={},
    )
    assert status == "BLOCKED"


def test_green_redlines_and_machine_claims_need_bound_pass_case_evidence() -> None:
    cases = [
        {
            "id": case_id,
            "status": "PASS" if case_id in {"A15", "A22", "A24"} else "NOT_RUN",
            "evidence": ([{"actual_run": True, "outcome": "PASS", "sha256": "A" * 64}]
                         if case_id in {"A15", "A22", "A24"} else []),
        }
        for case_id, _ in MODULE.CASE_CATALOG
    ]
    with pytest.raises(MODULE.EvidenceValidationError, match="requires current case evidence"):
        MODULE.normalize_redline_evidence(
            {},
            redlines={"security": "PASS", "privacy": "NOT_RUN"},
            cases=cases,
        )
    assert MODULE.normalize_redline_evidence(
        {"security": ["A15"]},
        redlines={"security": "PASS", "privacy": "NOT_RUN"},
        cases=cases,
    )["security"] == ["A15"]

    ledger = MODULE.default_ledger()
    ledger["machine_summary"] = {"performance": {"sidecar_median_ms": 1234}}
    with pytest.raises(MODULE.EvidenceValidationError, match="needs case evidence"):
        MODULE.normalize_machine_summary_evidence(ledger, target_version="14.0.0", cases=cases)
    ledger["machine_summary_evidence"] = {"performance.sidecar_median_ms": ["A22"]}
    assert MODULE.normalize_machine_summary_evidence(
        ledger, target_version="14.0.0", cases=cases
    ) == {"performance.sidecar_median_ms": ["A22"]}


def test_template_file_stays_identical_to_the_conservative_default() -> None:
    template_path = SCRIPT.parent / "templates" / "v14-evidence-input.template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))

    assert template == MODULE.default_ledger()
