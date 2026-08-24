from __future__ import annotations

"""Generate and validate the v14.0.0 release-evidence summaries.

The tool intentionally does not execute tests, download models, change VERSION, or
promote a release.  It only turns a reviewer-maintained evidence ledger into the
four v14 machine-readable/human-readable deliverables after checking that a PASS
has a current, target-version execution artifact behind it.

Typical pre-version-sync use:

    python scripts/v14-evidence.py --target-version 14.0.0 --plan <plan.md> --init
    # Fill build/v1400-evidence/V14_EVIDENCE_INPUT.json with real run evidence.
    python scripts/v14-evidence.py --target-version 14.0.0 --plan <plan.md> \
        --input build/v1400-evidence/V14_EVIDENCE_INPUT.json

The target version may be 14.0.0 while VERSION is still 13.0.0.  That is
represented explicitly as pre-version-sync evidence and can never be emitted as a
release-ready result.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "14.0.0"
# The user-provided implementation plan is part of the release contract.  A
# similarly named document with the same headings is not an interchangeable
# plan for this release.
V14_PLAN_SHA256 = "6BE5204C439A9FB3611BB6D6B6F93FB543EFADCC690940AE45DA36C73B8B2F28"
VALID_STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "SKIPPED", "NOT_RUN", "NOT_APPLICABLE"})
ACTUAL_EVIDENCE_KINDS = frozenset({"automated", "e2e", "manual"})
RELEASE_DECISIONS = frozenset({"BLOCKED", "FAILED", "READY_FOR_RELEASE"})
REDLINE_STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "NOT_RUN"})
EXECUTION_REPORT_SCHEMA_VERSION = 5
EXECUTION_REPORT_TYPE = "v14_execution"
EXECUTION_REPORT_PRODUCER = "v14-evidence-runner"
GENERATED_DOCUMENT_FILENAMES = (
    "TEST_MATRIX.json",
    "RELEASE_STATUS.json",
    "EVIDENCE_MANIFEST.json",
    "IMPLEMENTATION_FEEDBACK.md",
)
CONTROLLED_COMMAND_KINDS = frozenset({"pytest", "repository_script", "cargo_test", "npm_script"})
# Live/performance PASS claims need a raw report created in the same command
# and cryptographically bound by the v14 runner.  A bare successful process
# exit is not sufficient for these measured runtime claims.
ATTESTED_CASE_POLICIES: dict[str, dict[str, object]] = {
    "A02": {
        "report_type": "v14_windows_tts_live_evidence",
        "command_path": "scripts/v14-windows-tts-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "production_tts_manager_path",
            "manager_initially_idle",
            "windows_tts_stable",
            "melotts_experimental_non_blocking",
            "enabled_zh_cn_voice_available",
            "fixed_text_is_non_sensitive_chinese",
            "manager_synthesizes_real_non_sensitive_chinese_wav",
            "riff_wave_pcm_nonempty_duration",
            "audio_stays_in_isolated_temporary_root",
            "manager_request_lifecycle_persisted",
            "synthesis_does_not_fake_playback",
            "manager_shutdown_and_temporary_audio_cleanup",
        ),
    },
    "A04": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "explicit_execute_and_interactive_operator",
            "candidate_release_identity",
            "real_microphone_only",
            "permission_denied_observed",
            "permission_granted_observed",
        ),
    },
    "A05": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "explicit_execute_and_interactive_operator",
            "candidate_release_identity",
            "real_device_inventory",
            "device_selection_persisted",
            "device_disconnect_observed",
            "device_busy_observed",
            "device_restored",
        ),
    },
    "A06": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "real_microphone_only",
            "recording_start_stop_persisted",
            "recording_cancelled",
            "recording_format_verified",
        ),
    },
    "A08": {
        "report_type": "v14_stt_live_evidence",
        "command_path": "scripts/v14-stt-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "real_cpu_int8_model_load",
            "fixed_accuracy_corpus_category_coverage",
            "synthetic_chinese_and_mixed_transcripts_nonempty",
        ),
    },
    "A09": {
        "report_type": "v14_stt_live_evidence",
        "command_path": "scripts/v14-stt-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "small_download_target_absent_before_run",
            "confirmed_small_download_via_formal_http_api",
            "cpu_int8_configuration",
            "isolated_sidecar_detects_formal_model",
            "real_cpu_int8_model_load",
            "real_explicit_model_unload",
            "stt_unload_memory_release_observation",
        ),
    },
    "A10": {
        "report_type": "v14_stt_live_evidence",
        "command_path": "scripts/v14-stt-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "cpu_int8_configuration",
            "real_cpu_int8_model_load",
        ),
    },
    "A11": {
        "report_type": "v14_stt_live_evidence",
        "command_path": "scripts/v14-stt-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "synthetic_chinese_and_mixed_transcripts_nonempty",
            "blank_audio_rejected_without_message",
            "real_stt_cancellation",
        ),
    },
    "A12": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": ("candidate_release_identity", "voice_event_order_verified"),
    },
    "A13": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "edit_before_send_verified",
            "edit_cancel_did_not_send",
        ),
    },
    "A14": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "manual_send_ordinary_message",
            "auto_send_ordinary_message",
        ),
    },
    "A15": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "dangerous_voice_requires_confirmation",
            "dangerous_tool_not_executed",
        ),
    },
    "A16": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "tts_interrupt_before_recording",
            "tts_queue_settled",
        ),
    },
    "A17": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "global_stop_all_stages_observed",
            "global_stop_terminal_state",
            "global_stop_idempotent",
        ),
    },
    "A18": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "half_duplex_order_verified",
            "no_uncontrolled_overlap_observed",
        ),
    },
    "A19": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "physical_network_disconnected",
            "ollama_loopback_only",
            "no_external_owned_connections",
            "offline_voice_agent_tts_completed",
            "network_state_restored",
        ),
    },
    "A20": {
        "report_type": "v14_resource_live_evidence",
        "command_path": "scripts/v14-resource-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "no_preexisting_ollama_processes",
            "final_default_stt_model_is_small",
            "preexisting_formal_stt_small",
            "only_test_owned_ollama_after_start",
            "test_owned_ollama_idle_before_preload",
            "preexisting_qwen3_4b",
            "target_hardware_16gb_6gb_observed",
            "fresh_resource_admission_before_work",
            "isolated_source_sidecar_ready",
            "qwen_preloaded_with_idle_runtime_guard",
            "only_test_owned_ollama_after_preload",
            "cpu_int8_small_stt_resident_with_qwen",
            "real_cpu_small_stt_transcription_with_qwen",
            "real_windows_tts_with_qwen_and_stt",
            "test_owned_tts_lifecycle_converged",
            "only_test_owned_ollama_before_success",
        ),
    },
    "A21": {
        "report_type": "v14_stt_live_evidence",
        "command_path": "scripts/v14-stt-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "real_cpu_int8_model_load",
            "five_second_hot_transcription_budget",
            "fifteen_second_real_time_factor_budget",
            "real_explicit_model_unload",
            "stt_unload_memory_release_observation",
        ),
    },
    "A22": {"report_type": "v14_sidecar_performance", "command_path": "scripts/v14-candidate-runtime-smoke.ps1", "raw_source": False},
    "A23": {
        "report_type": "v14_a23_endurance_live_evidence",
        "command_path": "scripts/v14-a23-endurance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "explicit_execute_and_interactive_operator",
            "fresh_test_owned_runtime",
            "candidate_release_identity",
            "desktop_and_sidecar_pid_ownership",
            "continuous_api_health",
            "minimum_30_minute_duration",
            "operator_challenges_valid",
            "required_action_counts",
            "database_action_corroboration",
            "ram_vram_handle_process_trends_collected",
            "audio_device_trend_collected_and_restored",
            "temporary_and_cache_growth_bounded",
            "zero_failure_rate",
            "owned_processes_released",
            "test_owned_runtime_removed",
            "external_processes_protected",
        ),
    },
    "A24": {
        "report_type": "v14_desktop_voice_acceptance_evidence",
        "command_path": "scripts/v14-desktop-voice-acceptance-evidence.py",
        "raw_source": True,
        "required_checks": (
            "candidate_release_identity",
            "real_microphone_only",
            "formal_runtime_privacy_scan",
            "diagnostic_bundle_privacy_scan",
            "voice_metadata_only_persistence",
            "temporary_audio_removed",
        ),
    },
    "A26": {
        "report_type": "v14_ollama_live_evidence",
        "command_path": "scripts/v14-ollama-live-evidence.py",
        "raw_source": True,
        "required_checks": (
            "no_preexisting_ollama_processes",
            "test_owned_port_free_before_start",
            "non_mutating_model_store_full_tree_fingerprinted",
            "test_owned_service_identity_bound",
            "only_test_owned_ollama_after_start",
            "only_test_owned_ollama_before_stop",
            "test_owned_runtime_idle_before_preload",
            "qwen3_4b_installed",
            "qwen3_4b_preloaded",
            "only_test_owned_ollama_after_preload",
            "qwen3_4b_unloaded",
            "qwen3_4b_resource_release_observed",
            "test_owned_service_stopped",
            "model_store_unchanged",
            "no_ollama_process_after_stop",
            "test_owned_port_released",
        ),
    },
    "A27": {
        "report_type": "v14_nsis_installer_live_evidence",
        "command_path": "scripts/smoke-installer.ps1",
        "raw_source": True,
        "required_checks": (
            "non_administrator_execution",
            "candidate_version_matches_target",
            "candidate_nsis_present",
            "candidate_build_identity_matches_source",
            "previous_installer_supplied",
            "previous_build_identity_matches_artifact",
            "previous_version_upgrade",
            "isolated_desktop_started",
            "sidecar_stopped",
            "schema_migrated",
            "migration_backup_created",
            "in_place_upgrade_preserved_data",
            "uninstall_preserved_user_data",
            "reinstall_started",
            "reinstall_recognized_user_data",
            "final_uninstall_completed",
        ),
    },
    "A28": {
        "report_type": "v14_msi_installer_live_evidence",
        "command_path": "scripts/smoke-msi.ps1",
        "raw_source": True,
        "required_checks": (
            "administrator_execution",
            "candidate_version_matches_target",
            "candidate_msi_present",
            "candidate_build_identity_matches_source",
            "previous_installer_supplied",
            "previous_build_identity_matches_artifact",
            "fresh_install",
            "installed_desktop_started",
            "microphone_permission_grant",
            "microphone_permission_denial",
            "local_stt_transcription",
            "windows_tts_playback",
            "ollama_detection",
            "previous_version_upgrade",
            "schema_migrated",
            "migration_backup_created",
            "uninstall_preserved_user_data",
            "uninstall_preserved_models",
            "package_files_completely_removed",
            "reinstall_started",
            "reinstall_recognized_user_data",
            "final_uninstall_completed",
        ),
    },
}
A08_REQUIRED_ACCURACY_CATEGORIES = frozenset(
    {
        "ordinary_chinese",
        "numbers_dates_percent",
        "english_abbreviation",
        "mixed_language",
        "siyi",
        "natsume",
        "file_name",
        "technical_terms",
        "pause",
        "background_noise",
    }
)
A08_MANUAL_REVIEW_SCHEMA_VERSION = 1
A08_MANUAL_REVIEW_REPORT_TYPE = "v14_a08_manual_accuracy_review"
A08_MANUAL_REVIEW_PRODUCER = "manual-review"
A08_MANUAL_REVIEW_DECISIONS = frozenset({"PASS", "PASS_WITH_WARNING"})
A08_MANUAL_REVIEW_BOUNDARIES = frozenset(
    {"terminology", "mixed_language", "hallucination_repetition"}
)
A08_MANUAL_REVIEW_ASSESSMENTS = frozenset({"PASS", "WARNING", "NOT_CLAIMED"})
A26_REQUIRED_STRUCTURED_TESTS = (
    "external_process_and_port_preflight",
    "test_owned_service_identity",
    "qwen3_4b_preload_and_ps",
    "qwen3_4b_unload_and_resource_release",
    "test_owned_service_stop_and_external_protection",
)
A23_ACTION_REQUIREMENTS: dict[str, int] = {
    "recording_start_stop": 100,
    "stt": 50,
    "tts": 50,
    "cancel": 20,
    "tts_interrupt": 20,
    "device_change": 10,
    "model_load_unload": 10,
    "agent_stop": 10,
    "cache_roundtrip": 5,
}
A23_MINIMUM_DURATION_SECONDS = 30 * 60
A23_MINIMUM_RESOURCE_SAMPLES = 60
A23_REQUIRED_RESOURCE_CHECKS = frozenset(
    {
        "rss_growth_bounded",
        "handle_growth_bounded",
        "cache_growth_bounded",
        "temp_growth_bounded",
        "gpu_collected",
        "audio_devices_collected",
        "audio_devices_restored",
        "no_sample_liveness_failure",
        "owned_processes_released",
        "owned_gpu_released",
        "global_gpu_returned",
    }
)
A23_REQUIRED_DATABASE_CHECKS = frozenset(
    {
        "recording_started",
        "recording_stopped",
        "stt_completed",
        "tts_created",
        "cancelled",
        "tts_interrupted",
        "model_loaded",
        "model_unloaded",
        "agent_cancelled",
        "cache_reused",
    }
)
DESKTOP_VOICE_CASE_IDS = frozenset(
    {"A04", "A05", "A06", "A12", "A13", "A14", "A15", "A16", "A17", "A18", "A19", "A24"}
)
DESKTOP_VOICE_ACTION_CASES: dict[str, frozenset[str]] = {
    "A04": frozenset({"permission_denied", "permission_granted"}),
    "A05": frozenset({"device_selected", "device_disconnected", "device_restored", "device_busy"}),
    "A06": frozenset({"record_start_stop", "record_cancel"}),
    "A12": frozenset({"record_start_stop"}),
    "A13": frozenset({"edit_cancel", "edit_send"}),
    "A14": frozenset({"edit_send", "auto_send"}),
    "A15": frozenset({"dangerous_confirmation_visible", "dangerous_confirmation_rejected"}),
    "A16": frozenset({"tts_interrupt"}),
    "A17": frozenset(
        {
            "record_cancel",
            "global_stop_recording",
            "global_stop_stt",
            "global_stop_agent",
            "global_stop_tts",
        }
    ),
    "A18": frozenset({"tts_interrupt", "half_duplex"}),
    "A19": frozenset({"offline_e2e", "network_restored"}),
    "A24": frozenset({"privacy_probe", "diagnostics_export"}),
}
DESKTOP_VOICE_ACTION_CONTRACT: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("permission_denied", ("A04",), "REAL_MICROPHONE"),
    ("permission_granted", ("A04",), "REAL_MICROPHONE"),
    ("device_selected", ("A05",), "REAL_MICROPHONE"),
    ("device_disconnected", ("A05",), "REAL_MICROPHONE"),
    ("device_restored", ("A05",), "REAL_MICROPHONE"),
    ("device_busy", ("A05",), "REAL_MICROPHONE"),
    ("record_start_stop", ("A06", "A12"), "REAL_MICROPHONE"),
    ("record_cancel", ("A06", "A17"), "REAL_MICROPHONE"),
    ("edit_cancel", ("A13",), "REAL_MICROPHONE"),
    ("edit_send", ("A13", "A14"), "REAL_MICROPHONE"),
    ("auto_send", ("A14",), "REAL_MICROPHONE"),
    ("dangerous_confirmation_visible", ("A15",), "REAL_MICROPHONE"),
    ("dangerous_confirmation_rejected", ("A15",), "REAL_MICROPHONE"),
    ("tts_interrupt", ("A16", "A18"), "REAL_MICROPHONE"),
    ("global_stop_recording", ("A17",), "REAL_MICROPHONE"),
    ("global_stop_stt", ("A17",), "REAL_MICROPHONE"),
    ("global_stop_agent", ("A17",), "REAL_MICROPHONE"),
    ("global_stop_tts", ("A17",), "REAL_MICROPHONE"),
    ("half_duplex", ("A18",), "REAL_MICROPHONE"),
    ("offline_e2e", ("A19",), "REAL_MICROPHONE"),
    ("network_restored", ("A19",), "ENVIRONMENT_CONTROL"),
    ("privacy_probe", ("A24",), "REAL_MICROPHONE"),
    ("diagnostics_export", ("A24",), "FORMAL_DESKTOP_UI"),
)
REDLINE_CASE_BINDINGS: dict[str, frozenset[str]] = {
    "security": frozenset({"A15", "A17"}),
    "privacy": frozenset({"A07", "A24"}),
}
MACHINE_SUMMARY_CASE_BINDINGS: dict[str, frozenset[str]] = {
    "stt.provider": frozenset({"A08"}),
    "stt.model": frozenset({"A09"}),
    "stt.device": frozenset({"A10", "A20"}),
    "stt.compute_type": frozenset({"A10", "A20"}),
    "stt.offline": frozenset({"A19"}),
    "stt.microphone_capture": frozenset({"A04", "A06"}),
    "stt.transcription": frozenset({"A08", "A11"}),
    "stt.cancel": frozenset({"A11", "A17"}),
    "stt.model_load": frozenset({"A09"}),
    "stt.model_unload": frozenset({"A09"}),
    "voice.push_to_talk": frozenset({"A06", "A12"}),
    "voice.edit_before_send": frozenset({"A13"}),
    "voice.auto_send": frozenset({"A14"}),
    "voice.tts_interrupt": frozenset({"A16"}),
    "voice.global_stop": frozenset({"A17"}),
    "voice.voice_session": frozenset({"A12", "A14"}),
    "voice.privacy": frozenset({"A24"}),
    "performance.sidecar_median_ms": frozenset({"A22"}),
    "performance.sidecar_p95_ms": frozenset({"A22"}),
    "performance.recording_start_ms": frozenset({"A06", "A21"}),
    "performance.stt_model_load_ms": frozenset({"A21"}),
    "performance.stt_5s_audio_ms": frozenset({"A21"}),
    "performance.stt_rtf": frozenset({"A21"}),
    "performance.global_stop_ms": frozenset({"A17"}),
    "artifacts.nsis_path": frozenset({"A27"}),
    "artifacts.msi_path": frozenset({"A28"}),
    "artifacts.portable_exe_path": frozenset({"A27"}),
}

CASE_CATALOG: tuple[tuple[str, str], ...] = (
    ("A01", "基线核验"),
    ("A02", "TTS 正式化"),
    ("A03", "证据目录"),
    ("A04", "麦克风权限"),
    ("A05", "设备管理"),
    ("A06", "录音"),
    ("A07", "临时音频"),
    ("A08", "STT Provider"),
    ("A09", "STT 模型管理"),
    ("A10", "CPU 默认策略"),
    ("A11", "STT HTTP API"),
    ("A12", "Voice Events"),
    ("A13", "转写编辑"),
    ("A14", "消息接入"),
    ("A15", "权限安全"),
    ("A16", "TTS 打断"),
    ("A17", "全局停止"),
    ("A18", "半双工"),
    ("A19", "离线运行"),
    ("A20", "资源协调"),
    ("A21", "STT 性能"),
    ("A22", "Sidecar 性能"),
    ("A23", "30 分钟耐久"),
    ("A24", "隐私"),
    ("A25", "专业任务回归"),
    ("A26", "Ollama 回归"),
    ("A27", "NSIS"),
    ("A28", "MSI"),
)


class EvidenceValidationError(ValueError):
    """Raised when an evidence ledger would overstate what actually ran."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def normalize_version(value: object) -> str:
    version = str(value or "").strip()
    if version.startswith("v") and len(version) > 1 and version[1].isdigit():
        version = version[1:]
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise EvidenceValidationError(f"unsafe version: {value!r}")
    return version


def compact_version(version: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z]", "", version)
    if not compact:
        raise EvidenceValidationError(f"version has no compact form: {version!r}")
    return compact


def repository_relative(repository_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError as exc:
        raise EvidenceValidationError(f"path escapes repository root: {path}") from exc


def resolve_repository_path(repository_root: Path, value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceValidationError(f"{field} must be a non-empty repository-relative path")
    candidate = Path(value)
    if candidate.is_absolute():
        raise EvidenceValidationError(f"{field} must not be an absolute path: {value}")
    resolved = (repository_root / candidate).resolve()
    try:
        resolved.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise EvidenceValidationError(f"{field} escapes repository root: {value}") from exc
    return resolved


def expected_evidence_root(repository_root: Path, target_version: str) -> Path:
    return repository_root / "build" / f"v{compact_version(target_version)}-evidence"


def git_output(repository_root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def git_bytes(repository_root: Path, *arguments: str) -> bytes | None:
    """Return raw Git output so the source fingerprint is byte-accurate."""

    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout


def git_value(repository_root: Path, *arguments: str) -> str | None:
    value = git_output(repository_root, *arguments)
    return value or None


def is_generated_evidence_path(relative_text: str) -> bool:
    normalized = relative_text.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    evidence_prefix = f"build/v{compact_version(TARGET_VERSION)}-evidence"
    generated_documents = {
        f"docs/{TARGET_VERSION}/{filename}" for filename in GENERATED_DOCUMENT_FILENAMES
    }
    return (
        normalized in generated_documents
        or normalized == evidence_prefix
        or normalized.startswith(evidence_prefix + "/")
    )


def source_workspace_clean(repository_root: Path) -> bool | None:
    """Ignore generated evidence when determining whether source is clean."""

    status = git_bytes(repository_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status is None:
        return None
    entries = [entry for entry in status.split(b"\0") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            raise EvidenceValidationError("Git returned an invalid porcelain status entry")
        status_code = entry[:2]
        paths = [entry[3:]]
        if status_code[:1] in {b"R", b"C"} or status_code[1:2] in {b"R", b"C"}:
            if index >= len(entries):
                raise EvidenceValidationError("Git returned an incomplete rename/copy status entry")
            paths.append(entries[index])
            index += 1
        for raw_path in paths:
            try:
                relative_text = raw_path.decode("utf-8", errors="surrogateescape")
            except UnicodeDecodeError as exc:
                raise EvidenceValidationError("Git returned a non-decodable source path") from exc
            if not is_generated_evidence_path(relative_text):
                return False
    return True


def source_tree_fingerprint(repository_root: Path) -> str | None:
    """Hash the executable source tree, including untracked source changes.

    Git HEAD alone cannot identify v14 work collected before the version bump:
    the worktree is deliberately dirty during that period.  The generated
    evidence directory and generated feedback copy are excluded so producing
    evidence does not make its own source identity change.
    """

    tracked = git_bytes(repository_root, "ls-files", "-z")
    untracked = git_bytes(repository_root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or untracked is None:
        return None

    root = repository_root.resolve()
    digest = hashlib.sha256()
    digest.update(b"siyi-v14-source-tree-fingerprint-v1\0")

    paths = sorted({path for path in tracked.split(b"\0") + untracked.split(b"\0") if path})
    for raw_path in paths:
        try:
            relative_text = raw_path.decode("utf-8", errors="surrogateescape")
        except UnicodeDecodeError as exc:
            raise EvidenceValidationError("Git returned a non-decodable source path") from exc
        if is_generated_evidence_path(relative_text):
            continue

        candidate = root / relative_text
        try:
            candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise EvidenceValidationError(f"source fingerprint path escapes repository root: {relative_text}") from exc

        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        if candidate.is_symlink():
            payload = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
            kind = b"L"
        elif candidate.is_file():
            payload = candidate.read_bytes()
            kind = b"F"
        elif not candidate.exists():
            # A tracked deletion is still part of the source identity.
            payload = b""
            kind = b"D"
        else:
            raise EvidenceValidationError(f"unsupported source entry in fingerprint: {relative_text}")
        digest.update(kind)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest().upper()


def source_identity(repository_root: Path) -> dict[str, object]:
    version_path = repository_root / "VERSION"
    if not version_path.is_file():
        raise EvidenceValidationError(f"VERSION is missing: {version_path}")
    source_version = normalize_version(version_path.read_text(encoding="ascii").strip())
    return {
        "source_version": source_version,
        "source_commit": git_value(repository_root, "rev-parse", "HEAD"),
        "workspace_clean": source_workspace_clean(repository_root),
        "source_tree_fingerprint": source_tree_fingerprint(repository_root),
    }


def plan_identity(
    repository_root: Path,
    plan: Path,
    *,
    expected_sha256: str = V14_PLAN_SHA256,
) -> dict[str, object]:
    if not plan.is_file():
        raise EvidenceValidationError(f"plan file is missing: {plan}")
    plan_text = plan.read_text(encoding="utf-8-sig")
    required_markers = (
        "v14.0.0",
        "FasterWhisperProvider",
        "/api/stt/transcribe",
        "/api/voice/events",
        "A01",
        "A28",
    )
    missing_markers = [marker for marker in required_markers if marker not in plan_text]
    if missing_markers:
        raise EvidenceValidationError(
            "plan is not the required v14 evidence plan; missing markers: "
            + ", ".join(missing_markers)
        )
    plan_sha256 = sha256(plan)
    if plan_sha256 != expected_sha256:
        raise EvidenceValidationError(
            "plan SHA-256 does not match the locked v14 implementation plan: "
            f"expected {expected_sha256}, got {plan_sha256}"
        )
    try:
        relative = repository_relative(repository_root, plan)
    except EvidenceValidationError:
        # The plan is an input attachment.  Persist only its basename so release
        # evidence never records a user home directory or an absolute path.
        relative = plan.name
    return {
        "path": relative,
        "sha256": plan_sha256,
        "line_count": len(plan_text.splitlines()),
    }


def default_ledger(target_version: str = TARGET_VERSION) -> dict[str, object]:
    """Return the conservative evidence input template: no execution is claimed."""

    return {
        "schema_version": 1,
        "target_version": target_version,
        "release_decision": {
            "requested_status": "BLOCKED",
            "reason": "尚未完成 v14 的全部真实执行、安装和发布门禁。",
        },
        "redlines": {
            "security": "NOT_RUN",
            "privacy": "NOT_RUN",
        },
        "remote_pushed": False,
        "machine_summary": {
            "stt": {
                "provider": "",
                "model": "",
                "device": "",
                "compute_type": "",
                "offline": False,
                "microphone_capture": "NOT_RUN",
                "transcription": "NOT_RUN",
                "cancel": "NOT_RUN",
                "model_load": "NOT_RUN",
                "model_unload": "NOT_RUN",
            },
            "voice": {
                "push_to_talk": "NOT_RUN",
                "edit_before_send": "NOT_RUN",
                "auto_send": "NOT_RUN",
                "tts_interrupt": "NOT_RUN",
                "global_stop": "NOT_RUN",
                "voice_session": "NOT_RUN",
                "privacy": "NOT_RUN",
            },
            "performance": {
                "sidecar_median_ms": None,
                "sidecar_p95_ms": None,
                "recording_start_ms": None,
                "stt_model_load_ms": None,
                "stt_5s_audio_ms": None,
                "stt_rtf": None,
                "global_stop_ms": None,
                "not_measured": [
                    "sidecar_median_ms",
                    "sidecar_p95_ms",
                    "recording_start_ms",
                    "stt_model_load_ms",
                    "stt_5s_audio_ms",
                    "stt_rtf",
                    "global_stop_ms",
                ],
            },
            "artifacts": {
                "nsis_path": "",
                "msi_path": "",
                "portable_exe_path": "",
            },
        },
        "narrative": {},
        "cases": [
            {
                "id": case_id,
                "status": "NOT_RUN",
                "reason": "尚未采集该项 v14 的实际执行证据。",
                "evidence": [],
                "source_references": [],
            }
            for case_id, _ in CASE_CATALOG
        ],
    }


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceValidationError(f"JSON object required: {path}")
    return payload


def parse_recorded_at(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceValidationError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceValidationError(f"{field} is not ISO-8601: {value!r}") from exc
    return value


def validate_command_contract(
    payload: dict[str, object], *, repository_root: Path, case_id: str
) -> None:
    """Reject a PASS envelope created by an arbitrary ``exit 0`` command.

    The runner deliberately remains able to record every explicit command (a
    failure envelope is often useful).  A command becomes release evidence only
    when the runner could identify a repository-owned test or release script.
    This is intentionally a small, auditable contract rather than a second test
    runner: it prevents ``python -c`` / shell snippets from being promoted to a
    release PASS without blocking legitimate pytest, packaging, Cargo, or npm
    commands.
    """

    contract = payload.get("command_contract")
    if not isinstance(contract, dict):
        raise EvidenceValidationError(f"{case_id}: PASS evidence report needs a command_contract")
    kind = contract.get("kind")
    if kind not in CONTROLLED_COMMAND_KINDS:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence command_contract.kind must be one of "
            f"{sorted(CONTROLLED_COMMAND_KINDS)}"
        )
    raw_paths = contract.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths or not all(
        isinstance(value, str) and value.strip() for value in raw_paths
    ):
        raise EvidenceValidationError(f"{case_id}: PASS evidence command_contract.paths must be non-empty")

    command = payload.get("command")
    if not isinstance(command, str):
        raise EvidenceValidationError(f"{case_id}: PASS evidence report command must be a string")
    normalized_command = command.replace("\\", "/").lower()
    normalized_paths: list[str] = []
    for value in raw_paths:
        path = resolve_repository_path(
            repository_root, value, field=f"{case_id}.command_contract.paths"
        )
        if not path.exists():
            raise EvidenceValidationError(
                f"{case_id}: PASS evidence command_contract path does not exist: {value}"
            )
        relative = repository_relative(repository_root, path)
        command_reference = relative.lower()
        if kind == "npm_script" and relative == "desktop/frontend/package.json":
            command_reference = "desktop/frontend"
        if command_reference not in normalized_command:
            raise EvidenceValidationError(
                f"{case_id}: PASS evidence command does not reference its controlled path: {relative}"
            )
        normalized_paths.append(relative)

    if kind == "pytest":
        if "pytest" not in normalized_command or not all(
            relative.startswith("tests/") for relative in normalized_paths
        ):
            raise EvidenceValidationError(
                f"{case_id}: pytest evidence must run repository tests under tests/"
            )
    elif kind == "repository_script":
        if not all(relative.startswith("scripts/") for relative in normalized_paths):
            raise EvidenceValidationError(
                f"{case_id}: repository_script evidence must reference scripts/"
            )
    elif kind == "cargo_test":
        if "cargo" not in normalized_command or "test" not in normalized_command or normalized_paths != [
            "desktop/src-tauri/Cargo.toml"
        ]:
            raise EvidenceValidationError(
                f"{case_id}: cargo_test evidence must bind desktop/src-tauri/Cargo.toml"
            )
    elif kind == "npm_script":
        if "npm" not in normalized_command or "run" not in normalized_command or normalized_paths != [
            "desktop/frontend/package.json"
        ]:
            raise EvidenceValidationError(
                f"{case_id}: npm_script evidence must bind desktop/frontend/package.json"
            )


def repository_root_for_execution_report(path: Path) -> Path:
    evidence_directory_name = f"v{compact_version(TARGET_VERSION)}-evidence"
    for ancestor in (path.parent, *path.parents):
        if ancestor.name == evidence_directory_name and ancestor.parent.name == "build":
            return ancestor.parent.parent
    raise EvidenceValidationError(
        f"PASS evidence report is not located below build/{evidence_directory_name}: {path}"
    )


def validate_pass_report(
    path: Path,
    *,
    target_version: str,
    case_id: str,
    command: str,
    recorded_at: str,
    source: dict[str, object],
    repository_root: Path | None = None,
) -> dict[str, object]:
    """Ensure a claimed PASS is tied to a v14 execution-report envelope.

    A JSON file merely containing ``{"status": "PASS"}`` is not reliable
    release evidence: it could be a copied historical result, a source-review
    note, or a hand-written placeholder.  A v14 evidence artifact must identify
    the target release, the acceptance case it exercised, the actual command or
    manual procedure, and the time it was recorded.  This verifies the evidence
    *binding*; the tool deliberately does not infer that a command ran.
    """

    if path.suffix.lower() != ".json":
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence must be a JSON v14 execution report: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report is unreadable JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise EvidenceValidationError(f"{case_id}: PASS evidence report must be a JSON object")
    if payload.get("schema_version") != EXECUTION_REPORT_SCHEMA_VERSION:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report must use schema_version="
            f"{EXECUTION_REPORT_SCHEMA_VERSION}"
        )
    if payload.get("report_type") != EXECUTION_REPORT_TYPE:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report must set report_type={EXECUTION_REPORT_TYPE!r}"
        )
    if payload.get("producer") != EXECUTION_REPORT_PRODUCER:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report must be produced by {EXECUTION_REPORT_PRODUCER!r}"
        )
    if payload.get("target_version") != target_version:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report target_version must be {target_version!r}"
        )
    if payload.get("actual_run") is not True or payload.get("status") != "PASS":
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report must set actual_run=true and status=PASS"
        )
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        raise EvidenceValidationError(f"{case_id}: PASS evidence report needs an execution object")
    if (
        execution.get("actual_run") is not True
        or execution.get("status") != "PASS"
        or execution.get("exit_code") != 0
        or execution.get("timed_out") is not False
    ):
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence execution must set actual_run=true, status=PASS, "
            "exit_code=0, and timed_out=false"
        )
    report_case_ids = payload.get("case_ids")
    if (
        not isinstance(report_case_ids, list)
        or not all(isinstance(value, str) and value in {case for case, _ in CASE_CATALOG} for value in report_case_ids)
        or case_id not in report_case_ids
    ):
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report case_ids must include the acceptance case"
        )
    report_command = payload.get("command")
    if not isinstance(report_command, str) or report_command.strip() != command:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report command must exactly match the ledger command"
        )
    report_recorded_at = parse_recorded_at(
        payload.get("recorded_at"), field=f"{case_id}.report.recorded_at"
    )
    if report_recorded_at != recorded_at:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence report recorded_at must exactly match the ledger"
        )
    for field in ("source_version", "source_commit", "source_tree_fingerprint"):
        expected = source.get(field)
        if not isinstance(expected, str) or not expected.strip():
            raise EvidenceValidationError(
                f"{case_id}: current source identity lacks a usable {field}; PASS cannot be bound"
            )
        if payload.get(field) != expected:
            raise EvidenceValidationError(
                f"{case_id}: PASS evidence {field} does not match the current source identity"
            )
    expected_clean = source.get("workspace_clean")
    if not isinstance(expected_clean, bool):
        raise EvidenceValidationError(
            f"{case_id}: current source identity lacks a usable workspace_clean flag; PASS cannot be bound"
        )
    if payload.get("workspace_clean") is not expected_clean:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence workspace_clean does not match the current source identity"
        )
    validate_command_contract(
        payload,
        repository_root=(repository_root or repository_root_for_execution_report(path)),
        case_id=case_id,
    )
    return payload


def _attested_path(
    *,
    repository_root: Path,
    evidence_root: Path,
    case_id: str,
    value: object,
) -> tuple[Path, str]:
    """Resolve a raw attachment without accepting links or external paths."""

    path = resolve_repository_path(
        repository_root, value, field=f"{case_id}.evidence.attested_outputs"
    )
    try:
        path.relative_to(evidence_root.resolve())
    except ValueError as exc:
        raise EvidenceValidationError(
            f"{case_id}: attested raw output must be collected under "
            f"{repository_relative(repository_root, evidence_root)}"
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise EvidenceValidationError(
            f"{case_id}: attested raw output must be an existing regular file"
        )
    return path, repository_relative(repository_root, path)


def _raw_object(
    container: dict[str, object], key: str, *, case_id: str, field: str
) -> dict[str, object]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"{case_id}: attested raw output must contain object {field}")
    return value


def _validate_required_attested_checks(
    raw_payload: dict[str, object], *, case_id: str, required_checks: object
) -> None:
    if not isinstance(required_checks, tuple) or not all(
        isinstance(value, str) and value for value in required_checks
    ):
        raise EvidenceValidationError(f"{case_id}: internal attestation policy has invalid required_checks")
    checks = raw_payload.get("checks")
    if not isinstance(checks, dict):
        raise EvidenceValidationError(f"{case_id}: attested raw output is missing required checks")
    for name in required_checks:
        result = checks.get(name)
        if not isinstance(result, dict):
            raise EvidenceValidationError(
                f"{case_id}: attested raw output is missing required check {name}"
            )
        if result.get("passed") is not True:
            raise EvidenceValidationError(
                f"{case_id}: required attested check {name} did not pass"
            )


def _validate_a08_accuracy_review(raw_payload: dict[str, object], *, case_id: str) -> None:
    """Require reviewable fixed-corpus data without deciding transcription accuracy."""

    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    review = _raw_object(results, "accuracy_review", case_id=case_id, field="results.accuracy_review")
    if review.get("manual_review_required") is not True or review.get("decision") != "NOT_AUTOMATED":
        raise EvidenceValidationError(
            f"{case_id}: accuracy evidence must remain explicitly subject to human review"
        )
    categories = review.get("categories_exercised")
    if not isinstance(categories, list) or not all(isinstance(value, str) for value in categories):
        raise EvidenceValidationError(
            f"{case_id}: accuracy evidence is missing fixed-corpus category coverage"
        )
    missing = sorted(A08_REQUIRED_ACCURACY_CATEGORIES.difference(categories))
    if missing:
        raise EvidenceValidationError(
            f"{case_id}: accuracy evidence is missing fixed-corpus categories: {', '.join(missing)}"
        )
    samples = review.get("samples")
    if not isinstance(samples, dict) or not samples:
        raise EvidenceValidationError(f"{case_id}: accuracy evidence must contain reviewable samples")
    for sample_name, sample_value in samples.items():
        if not isinstance(sample_name, str) or not sample_name or not isinstance(sample_value, dict):
            raise EvidenceValidationError(f"{case_id}: accuracy evidence contains an invalid sample")
        reference = sample_value.get("reference_text")
        observed = sample_value.get("observed_text")
        accuracy = sample_value.get("accuracy_review")
        expected_entities = sample_value.get("expected_entities")
        observed_entities = sample_value.get("observed_entities")
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or not isinstance(observed, str)
            or not isinstance(accuracy, dict)
            or not isinstance(expected_entities, list)
            or not isinstance(observed_entities, list)
        ):
            raise EvidenceValidationError(
                f"{case_id}: accuracy sample {sample_name} lacks reference/transcript/diff/entity data"
            )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_a09_download_scope_and_model(
    raw_payload: dict[str, object], *, case_id: str
) -> None:
    scope = _raw_object(raw_payload, "scope", case_id=case_id, field="scope")
    if (
        scope.get("model_download") != "CALLED"
        or scope.get("user_confirmation") is not True
        or scope.get("download_only") is not True
        or scope.get("model_delete") != "NOT_CALLED"
    ):
        raise EvidenceValidationError(
            f"{case_id}: raw evidence must be an explicit download-only small-model run"
        )

    model = _raw_object(raw_payload, "model", case_id=case_id, field="model")
    if (
        model.get("id") != "small"
        or model.get("installed_before_run") is not False
        or model.get("installed_after_run") is not True
        or model.get("download_operation") != "CALLED"
        or model.get("user_confirmation") is not True
        or model.get("delete_operation") != "NOT_CALLED"
    ):
        raise EvidenceValidationError(
            f"{case_id}: raw evidence does not identify the newly installed managed small model"
        )


def _validate_a09_download_request(
    download: dict[str, object], *, case_id: str
) -> object:
    """Validate preview/confirmation/final-state fields and return completed bytes."""

    preview = _raw_object(download, "preview", case_id=case_id, field="results.model_download.preview")
    if (
        preview.get("model") != "small"
        or preview.get("repo_id") != "Systran/faster-whisper-small"
        or preview.get("already_installed") is not False
        or preview.get("fits") is not True
        or not _positive_int(preview.get("estimated_bytes"))
        or not _positive_int(preview.get("required_bytes"))
        or not _positive_int(preview.get("available_bytes"))
        or int(preview["available_bytes"]) < int(preview["required_bytes"])
        or not isinstance(preview.get("target_directory"), str)
        or not str(preview["target_directory"]).strip()
    ):
        raise EvidenceValidationError(f"{case_id}: formal small-model preview evidence is incomplete")

    rejected = _raw_object(
        download,
        "unconfirmed_request",
        case_id=case_id,
        field="results.model_download.unconfirmed_request",
    )
    if (
        rejected.get("confirmed") is not False
        or rejected.get("http_status") != 409
        or rejected.get("error_code") != "STT_DOWNLOAD_CONFIRMATION_REQUIRED"
        or rejected.get("download_started") is not False
    ):
        raise EvidenceValidationError(
            f"{case_id}: formal API confirmed=false refusal was not proven"
        )

    confirmed = _raw_object(
        download,
        "confirmed_request",
        case_id=case_id,
        field="results.model_download.confirmed_request",
    )
    if (
        confirmed.get("confirmed") is not True
        or confirmed.get("called") is not True
        or confirmed.get("http_status") != 202
        or not isinstance(confirmed.get("response"), dict)
    ):
        raise EvidenceValidationError(
            f"{case_id}: formal API confirmed=true download was not proven"
        )

    final_state = _raw_object(
        download, "final_state", case_id=case_id, field="results.model_download.final_state"
    )
    completed = final_state.get("completed_bytes")
    total = final_state.get("total_bytes")
    if (
        final_state.get("model") != "small"
        or final_state.get("status") != "INSTALLED"
        or not _positive_int(completed)
        or completed != total
        or final_state.get("error") is not None
    ):
        raise EvidenceValidationError(f"{case_id}: small-model download did not finish as INSTALLED")
    return completed


def _validate_a09_download_manifest(
    download: dict[str, object], *, completed: object, case_id: str
) -> None:
    manifest = _raw_object(
        download, "final_model", case_id=case_id, field="results.model_download.final_model"
    )
    files = manifest.get("files")
    if (
        manifest.get("model") != "small"
        or not _positive_int(manifest.get("file_count"))
        or manifest.get("file_count") != (len(files) if isinstance(files, list) else None)
        or manifest.get("actual_bytes") != completed
        or not isinstance(files, list)
        or not files
    ):
        raise EvidenceValidationError(f"{case_id}: downloaded small-model integrity manifest is incomplete")
    file_paths: set[str] = set()
    total_manifest_bytes = 0
    for item in files:
        if not isinstance(item, dict):
            raise EvidenceValidationError(f"{case_id}: downloaded small-model manifest contains an invalid file")
        path = item.get("path")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path in file_paths
            or not _positive_int(size)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
        ):
            raise EvidenceValidationError(f"{case_id}: downloaded small-model manifest file is invalid")
        file_paths.add(path)
        total_manifest_bytes += int(size)
    if "config.json" not in file_paths or total_manifest_bytes != manifest.get("actual_bytes"):
        raise EvidenceValidationError(
            f"{case_id}: downloaded small-model manifest does not prove complete hashed files"
        )


def _validate_a09_download_runtime(
    results: dict[str, object], *, case_id: str
) -> None:
    settings = _raw_object(results, "settings", case_id=case_id, field="results.settings")
    load = _raw_object(results, "load", case_id=case_id, field="results.load")
    load_response = _raw_object(load, "response", case_id=case_id, field="results.load.response")
    load_status = _raw_object(load, "status", case_id=case_id, field="results.load.status")
    if (
        settings.get("model_id") != "small"
        or settings.get("device") != "cpu"
        or settings.get("compute_type") != "int8"
        or settings.get("gpu_experimental") is not False
        or load_response.get("status") != "READY"
        or load_response.get("device") != "cpu"
        or load_response.get("compute_type") != "int8"
        or load_status.get("loaded_model") != "small"
        or not _positive_int(load_status.get("worker_pid"))
    ):
        raise EvidenceValidationError(f"{case_id}: real CPU/int8 small-model load was not proven")

    unload = _raw_object(results, "unload", case_id=case_id, field="results.unload")
    unload_response = _raw_object(
        unload, "unload_response", case_id=case_id, field="results.unload.unload_response"
    )
    unload_status = _raw_object(
        unload, "status_after", case_id=case_id, field="results.unload.status_after"
    )
    release = _raw_object(
        unload,
        "resource_release",
        case_id=case_id,
        field="results.unload.resource_release",
    )
    if (
        unload_response.get("status") != "UNLOADED"
        or unload_status.get("loaded_model") is not None
        or unload_status.get("worker_pid") is not None
        or release.get("worker_gone") is not True
        or release.get("observed_release") is not True
    ):
        raise EvidenceValidationError(f"{case_id}: explicit small-model unload/release was not proven")


def _validate_a09_download_flow(raw_payload: dict[str, object], *, case_id: str) -> None:
    """Validate the actual confirmed-small API flow and final model integrity."""

    _validate_a09_download_scope_and_model(raw_payload, case_id=case_id)
    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    download = _raw_object(
        results, "model_download", case_id=case_id, field="results.model_download"
    )
    completed = _validate_a09_download_request(download, case_id=case_id)
    _validate_a09_download_manifest(download, completed=completed, case_id=case_id)
    _validate_a09_download_runtime(results, case_id=case_id)


def _a09_normalized_manifest(
    payload: object,
    *,
    case_id: str,
    field: str,
    expected_total: object | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise EvidenceValidationError(f"{case_id}: {field} must be an object")
    files = payload.get("files")
    if (
        payload.get("model") != "small"
        or not _positive_int(payload.get("file_count"))
        or not isinstance(files, list)
        or payload.get("file_count") != len(files)
        or not _positive_int(payload.get("actual_bytes"))
        or (expected_total is not None and payload.get("actual_bytes") != expected_total)
    ):
        raise EvidenceValidationError(f"{case_id}: {field} is incomplete")
    normalized_files: list[dict[str, object]] = []
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise EvidenceValidationError(f"{case_id}: {field} contains an invalid file")
        relative = item.get("path")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen
            or not _positive_int(size)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
        ):
            raise EvidenceValidationError(f"{case_id}: {field} contains an unsafe file entry")
        seen.add(relative)
        total += int(size)
        normalized_files.append(
            {"path": relative, "size_bytes": int(size), "sha256": digest.upper()}
        )
    if "config.json" not in seen or total != payload.get("actual_bytes"):
        raise EvidenceValidationError(f"{case_id}: {field} bytes do not balance")
    normalized_files.sort(key=lambda item: str(item["path"]).casefold())
    return {
        "model": "small",
        "file_count": len(normalized_files),
        "actual_bytes": total,
        "files": normalized_files,
    }


def _validate_a09_current_small_load_unload(
    raw_payload: dict[str, object], *, case_id: str
) -> None:
    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    settings = _raw_object(results, "settings", case_id=case_id, field="results.settings")
    load = _raw_object(results, "load", case_id=case_id, field="results.load")
    load_response = _raw_object(load, "response", case_id=case_id, field="results.load.response")
    load_status = _raw_object(load, "status", case_id=case_id, field="results.load.status")
    if (
        settings.get("model_id") != "small"
        or settings.get("device") != "cpu"
        or settings.get("compute_type") != "int8"
        or settings.get("gpu_experimental") is not False
        or load_response.get("status") != "READY"
        or load_response.get("device") != "cpu"
        or load_response.get("compute_type") != "int8"
        or load_status.get("loaded_model") != "small"
        or not _positive_int(load_status.get("worker_pid"))
    ):
        raise EvidenceValidationError(f"{case_id}: current-source CPU/int8 small-model load was not proven")
    unload = _raw_object(results, "unload", case_id=case_id, field="results.unload")
    unload_response = _raw_object(
        unload, "unload_response", case_id=case_id, field="results.unload.unload_response"
    )
    unload_status = _raw_object(
        unload, "status_after", case_id=case_id, field="results.unload.status_after"
    )
    release = _raw_object(
        unload, "resource_release", case_id=case_id, field="results.unload.resource_release"
    )
    if (
        unload_response.get("status") != "UNLOADED"
        or unload_status.get("loaded_model") is not None
        or unload_status.get("worker_pid") is not None
        or release.get("worker_gone") is not True
        or release.get("observed_release") is not True
        or not _positive_int(release.get("stt_rss_before_bytes"))
        or release.get("stt_rss_after_bytes") is not None
    ):
        raise EvidenceValidationError(
            f"{case_id}: current-source explicit small-model unload/RSS release was not proven"
        )


def _a09_prior_receipt_metadata(
    raw_payload: dict[str, object], *, case_id: str
) -> dict[str, object]:
    scope = _raw_object(raw_payload, "scope", case_id=case_id, field="scope")
    if (
        scope.get("model_download") != "VERIFIED_PRIOR_ACTUAL"
        or scope.get("download_only") is not True
        or scope.get("model_delete") != "NOT_CALLED"
        or scope.get("user_confirmation") is not False
        or scope.get("prior_user_confirmation_verified") is not True
    ):
        raise EvidenceValidationError(
            f"{case_id}: receipt mode must disclose that no current download or confirmation occurred"
        )
    model = _raw_object(raw_payload, "model", case_id=case_id, field="model")
    if (
        model.get("id") != "small"
        or model.get("installed_before_run") is not True
        or model.get("installed_after_run") is not True
        or model.get("download_operation") != "VERIFIED_PRIOR_ACTUAL"
        or model.get("user_confirmation") is not False
        or model.get("prior_user_confirmation_verified") is not True
        or model.get("delete_operation") != "NOT_CALLED"
    ):
        raise EvidenceValidationError(f"{case_id}: receipt mode does not identify the preserved small model")

    checks = _raw_object(raw_payload, "checks", case_id=case_id, field="checks")
    manifest_check = checks.get("current_small_manifest_matches_download_receipt")
    if not isinstance(manifest_check, dict) or manifest_check.get("passed") is not True:
        raise EvidenceValidationError(f"{case_id}: current installed model manifest was not verified")

    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    receipt = _raw_object(
        results,
        "download_receipt_verification",
        case_id=case_id,
        field="results.download_receipt_verification",
    )
    if (
        receipt.get("mode") != "VERIFIED_PRIOR_ACTUAL"
        or receipt.get("runner_binding_verified") is not True
        or receipt.get("formal_confirmation_flow_verified") is not True
        or receipt.get("prior_user_confirmation_verified") is not True
        or receipt.get("model_manifest_exact_match") is not True
    ):
        raise EvidenceValidationError(f"{case_id}: prior actual-download receipt verification is incomplete")
    return receipt


def _read_a09_prior_receipt_files(
    receipt: dict[str, object],
    *,
    case_id: str,
    repository_root: Path,
    evidence_root: Path,
) -> tuple[dict[str, object], dict[str, object], Path, str, str, bytes, bytes]:
    """Resolve, read and hash-bind the historical raw report and runner envelope."""

    prior_raw_meta = _raw_object(
        receipt, "prior_raw", case_id=case_id, field="results.download_receipt_verification.prior_raw"
    )
    prior_envelope_meta = _raw_object(
        receipt,
        "prior_envelope",
        case_id=case_id,
        field="results.download_receipt_verification.prior_envelope",
    )
    prior_raw_path, prior_raw_relative = _attested_path(
        repository_root=repository_root,
        evidence_root=evidence_root,
        case_id=case_id,
        value=prior_raw_meta.get("path"),
    )
    prior_envelope_path, prior_envelope_relative = _attested_path(
        repository_root=repository_root,
        evidence_root=evidence_root,
        case_id=case_id,
        value=prior_envelope_meta.get("path"),
    )
    try:
        prior_raw_bytes = prior_raw_path.read_bytes()
        prior_raw = json.loads(prior_raw_bytes.decode("utf-8-sig"))
        prior_envelope_bytes = prior_envelope_path.read_bytes()
        prior_envelope = json.loads(prior_envelope_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(f"{case_id}: prior A09 receipt files are unreadable") from exc
    if not isinstance(prior_raw, dict) or not isinstance(prior_envelope, dict):
        raise EvidenceValidationError(f"{case_id}: prior A09 receipt files must contain JSON objects")
    if (
        prior_raw_meta.get("path") != prior_raw_relative
        or prior_raw_meta.get("sha256") != sha256(prior_raw_path)
        or prior_raw_meta.get("bytes") != len(prior_raw_bytes)
        or prior_envelope_meta.get("path") != prior_envelope_relative
        or prior_envelope_meta.get("sha256") != sha256(prior_envelope_path)
        or prior_envelope_meta.get("bytes") != len(prior_envelope_bytes)
    ):
        raise EvidenceValidationError(f"{case_id}: prior A09 receipt file hash/size binding changed")
    return (
        prior_raw,
        prior_envelope,
        prior_raw_path,
        prior_raw_relative,
        prior_envelope_relative,
        prior_raw_bytes,
        prior_envelope_bytes,
    )


def _validate_a09_prior_envelope(
    prior_envelope: dict[str, object], *, case_id: str
) -> None:
    """Require the historical receipt to be a controlled A09 runner PASS."""

    execution = prior_envelope.get("execution")
    contract = prior_envelope.get("command_contract")
    command = prior_envelope.get("command")
    if (
        prior_envelope.get("schema_version") != EXECUTION_REPORT_SCHEMA_VERSION
        or prior_envelope.get("report_type") != EXECUTION_REPORT_TYPE
        or prior_envelope.get("producer") != EXECUTION_REPORT_PRODUCER
        or prior_envelope.get("target_version") != "14.0.0"
        or prior_envelope.get("status") != "PASS"
        or prior_envelope.get("actual_run") is not True
        or prior_envelope.get("case_ids") != ["A09"]
        or not isinstance(execution, dict)
        or execution.get("actual_run") is not True
        or execution.get("status") != "PASS"
        or execution.get("exit_code") != 0
        or execution.get("timed_out") is not False
        or not isinstance(contract, dict)
        or contract.get("kind") != "repository_script"
        or contract.get("paths") != ["scripts/v14-stt-live-evidence.py"]
        or not isinstance(command, str)
        or "--download-small-confirmed" not in command.split()
        or "--download-only" not in command.split()
    ):
        raise EvidenceValidationError(f"{case_id}: prior A09 runner envelope is not a controlled PASS")


def _validate_a09_prior_source_attachment(
    *,
    receipt: dict[str, object],
    prior_raw: dict[str, object],
    prior_envelope: dict[str, object],
    prior_raw_path: Path,
    prior_raw_relative: str,
    prior_raw_bytes: bytes,
    case_id: str,
) -> None:
    """Verify source identity and the envelope-to-raw attachment binding."""

    prior_source = prior_raw.get("source")
    receipt_source = receipt.get("prior_source")
    source_fields = (
        "source_version",
        "source_commit",
        "source_tree_fingerprint",
        "workspace_clean",
    )
    if not isinstance(prior_source, dict) or not isinstance(receipt_source, dict) or any(
        prior_source.get(field) != prior_envelope.get(field)
        or prior_source.get(field) != receipt_source.get(field)
        for field in source_fields
    ):
        raise EvidenceValidationError(f"{case_id}: prior A09 source identity binding changed")
    attachments = prior_envelope.get("attested_outputs")
    if not isinstance(attachments, list) or len(attachments) != 1 or not isinstance(attachments[0], dict):
        raise EvidenceValidationError(f"{case_id}: prior A09 envelope must bind one raw report")
    attachment = attachments[0]
    if (
        attachment.get("path") != prior_raw_relative
        or attachment.get("sha256") != sha256(prior_raw_path)
        or attachment.get("bytes") != len(prior_raw_bytes)
        or attachment.get("status") != prior_raw.get("status")
        or attachment.get("actual_run") != prior_raw.get("actual_run")
        or attachment.get("target_version") != prior_raw.get("target_version")
        or attachment.get("report_type") != prior_raw.get("report_type")
        or attachment.get("producer") != prior_raw.get("producer")
        or attachment.get("source_identity_mode") != "raw_report"
        or any(attachment.get(field) != prior_source.get(field) for field in source_fields)
    ):
        raise EvidenceValidationError(f"{case_id}: prior A09 envelope no longer binds its raw report")


def _validate_a09_prior_manifest_match(
    *, receipt: dict[str, object], prior_raw: dict[str, object], case_id: str
) -> None:
    """Re-run the original flow validator and compare all three model manifests."""

    # This reuses the strict direct-download validator.  Therefore receipt mode
    # cannot weaken any original confirmation, manifest, load or unload gate.
    _validate_required_attested_checks(
        prior_raw,
        case_id=case_id,
        required_checks=ATTESTED_CASE_POLICIES["A09"]["required_checks"],
    )
    _validate_a09_download_flow(prior_raw, case_id=case_id)
    prior_download = _raw_object(
        _raw_object(prior_raw, "results", case_id=case_id, field="prior.results"),
        "model_download",
        case_id=case_id,
        field="prior.results.model_download",
    )
    prior_final_state = _raw_object(
        prior_download,
        "final_state",
        case_id=case_id,
        field="prior.results.model_download.final_state",
    )
    expected_manifest = _a09_normalized_manifest(
        receipt.get("expected_model_manifest"),
        case_id=case_id,
        field="receipt.expected_model_manifest",
        expected_total=prior_final_state.get("completed_bytes"),
    )
    prior_manifest = _a09_normalized_manifest(
        prior_download.get("final_model"),
        case_id=case_id,
        field="prior.results.model_download.final_model",
        expected_total=prior_final_state.get("completed_bytes"),
    )
    current_manifest = _a09_normalized_manifest(
        receipt.get("current_model_manifest"),
        case_id=case_id,
        field="receipt.current_model_manifest",
        expected_total=prior_final_state.get("completed_bytes"),
    )
    if expected_manifest != prior_manifest or current_manifest != prior_manifest:
        raise EvidenceValidationError(
            f"{case_id}: current installed small-model manifest does not exactly match the prior download"
        )


def _validate_a09_verified_prior_actual(
    raw_payload: dict[str, object],
    *,
    case_id: str,
    repository_root: Path,
    evidence_root: Path,
) -> None:
    """Deep-check a current-source verifier over a prior actual A09 download."""

    receipt = _a09_prior_receipt_metadata(raw_payload, case_id=case_id)
    (
        prior_raw,
        prior_envelope,
        prior_raw_path,
        prior_raw_relative,
        _prior_envelope_relative,
        prior_raw_bytes,
        _prior_envelope_bytes,
    ) = _read_a09_prior_receipt_files(
        receipt,
        case_id=case_id,
        repository_root=repository_root,
        evidence_root=evidence_root,
    )
    _validate_a09_prior_envelope(prior_envelope, case_id=case_id)
    _validate_a09_prior_source_attachment(
        receipt=receipt,
        prior_raw=prior_raw,
        prior_envelope=prior_envelope,
        prior_raw_path=prior_raw_path,
        prior_raw_relative=prior_raw_relative,
        prior_raw_bytes=prior_raw_bytes,
        case_id=case_id,
    )
    _validate_a09_prior_manifest_match(
        receipt=receipt, prior_raw=prior_raw, case_id=case_id
    )
    _validate_a09_current_small_load_unload(raw_payload, case_id=case_id)


def _validate_a02_windows_tts_live(
    raw_payload: dict[str, object], *, case_id: str
) -> None:
    """Reject exit-zero, all-skip, and metadata-only A02 release claims."""

    target = _raw_object(raw_payload, "target", case_id=case_id, field="target")
    if (
        target.get("platform") != "windows"
        or target.get("os_name") != "nt"
        or target.get("sys_platform") != "win32"
        or target.get("provider") != "windows"
        or target.get("provider_maturity") != "stable"
        or target.get("melotts_maturity") != "experimental"
        or not _positive_int(target.get("enabled_zh_cn_voice_count"))
    ):
        raise EvidenceValidationError(
            f"{case_id}: raw evidence is not bound to real Windows Stable TTS with an enabled zh-CN voice"
        )

    scope = _raw_object(raw_payload, "scope", case_id=case_id, field="scope")
    if (
        scope.get("production_manager") != "app.tts.manager.TTSManager"
        or scope.get("tts_provider") != "windows"
        or scope.get("fixed_text_classification") != "PUBLIC_NON_SENSITIVE"
        or scope.get("tts_playback") != "NOT_RUN"
        or scope.get("microphone_capture") != "NOT_RUN"
        or scope.get("ollama_actions") != "NONE"
        or scope.get("melotts_model_actions") != "NONE"
        or scope.get("network_tts_calls") != "NONE"
    ):
        raise EvidenceValidationError(f"{case_id}: raw evidence crossed the bounded TTS-only scope")

    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    providers = _raw_object(results, "providers", case_id=case_id, field="results.providers")
    windows = _raw_object(
        providers, "windows", case_id=case_id, field="results.providers.windows"
    )
    melotts = _raw_object(
        providers, "melotts", case_id=case_id, field="results.providers.melotts"
    )
    melotts_status = melotts.get("status")
    melotts_availability = melotts.get("availability")
    melotts_availability_valid = (
        melotts_status == "ok"
        and melotts_availability == "ready"
    ) or (
        melotts_status == "unavailable"
        and melotts_availability in {"not_installed", "model_not_confirmed", "unavailable"}
    )
    if (
        windows.get("provider") != "windows"
        or windows.get("status") != "ok"
        or windows.get("device") != "cpu"
        or windows.get("version") != "SAPI.SpVoice"
        or windows.get("maturity") != "stable"
        or melotts.get("provider") != "melotts"
        or melotts.get("device") != "cpu"
        or melotts.get("version") != "optional-local"
        or melotts.get("maturity") != "experimental"
        or melotts.get("release_gate") is not False
        or not melotts_availability_valid
    ):
        raise EvidenceValidationError(
            f"{case_id}: provider maturity/status evidence does not match the production contract"
        )

    synthesis = _raw_object(
        results, "synthesis", case_id=case_id, field="results.synthesis"
    )
    fixed_text_digest = synthesis.get("fixed_text_sha256")
    if (
        synthesis.get("provider") != "windows"
        or synthesis.get("status") != "READY"
        or synthesis.get("cached") is not False
        or synthesis.get("sensitive") is not False
        or not _positive_int(synthesis.get("duration_ms"))
        or not _positive_int(synthesis.get("sample_rate"))
        or not isinstance(synthesis.get("synthesis_ms"), (int, float))
        or isinstance(synthesis.get("synthesis_ms"), bool)
        or float(synthesis["synthesis_ms"]) <= 0
        or not _positive_int(synthesis.get("text_character_count"))
        or not isinstance(fixed_text_digest, str)
        or re.fullmatch(r"[0-9A-F]{64}", fixed_text_digest) is None
    ):
        raise EvidenceValidationError(f"{case_id}: real non-cache Windows synthesis was not proven")

    wav = _raw_object(results, "wav", case_id=case_id, field="results.wav")
    wav_digest = wav.get("sha256")
    if (
        wav.get("container") != "RIFF/WAVE"
        or wav.get("encoding") != "PCM"
        or not _positive_int(wav.get("channels"))
        or wav.get("sample_width_bytes") not in {1, 2, 3, 4}
        or not _positive_int(wav.get("sample_rate"))
        or not _positive_int(wav.get("frame_count"))
        or not _positive_int(wav.get("duration_ms"))
        or not isinstance(wav.get("size_bytes"), int)
        or isinstance(wav.get("size_bytes"), bool)
        or int(wav["size_bytes"]) <= 44
        or not _positive_int(wav.get("pcm_rms"))
        or not isinstance(wav_digest, str)
        or re.fullmatch(r"[0-9A-F]{64}", wav_digest) is None
    ):
        raise EvidenceValidationError(f"{case_id}: RIFF/PCM duration and non-silent frames were not proven")

    lifecycle = _raw_object(
        results, "lifecycle", case_id=case_id, field="results.lifecycle"
    )
    if (
        lifecycle.get("initial_status") != "IDLE"
        or lifecycle.get("temporary_wavs_before") != []
        or lifecycle.get("request_status_after_synthesis") != "READY"
        or lifecycle.get("manager_status_after_synthesis") != "IDLE"
        or lifecycle.get("queue_length_after_synthesis") != 0
        or lifecycle.get("manager_status_after_shutdown") != "IDLE"
        or lifecycle.get("temporary_wavs_after_shutdown") != []
    ):
        raise EvidenceValidationError(f"{case_id}: manager lifecycle did not converge cleanly")
    cleanup = _raw_object(raw_payload, "cleanup", case_id=case_id, field="cleanup")
    if (
        cleanup.get("manager_shutdown_called") is not True
        or cleanup.get("manager_shutdown_error") is not None
        or cleanup.get("manager_status_after_shutdown") != "IDLE"
        or cleanup.get("queue_length_after_shutdown") != 0
        or cleanup.get("active_synthesis_after_shutdown") != []
        or cleanup.get("temporary_wavs_after_shutdown") != []
    ):
        raise EvidenceValidationError(f"{case_id}: TTS temporary audio or manager state remained after shutdown")


def _a26_model_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            str(item.get("name") or item.get("model") or "").strip()
            for item in value
            if isinstance(item, dict) and str(item.get("name") or item.get("model") or "").strip()
        }
    )


def _a26_identity(value: object, *, case_id: str, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"{case_id}: {field} must be an object")
    digest = value.get("command_sha256")
    if (
        not _positive_int(value.get("pid"))
        or not isinstance(value.get("creation_date"), str)
        or not str(value["creation_date"]).strip()
        or str(value.get("executable_name") or "").casefold() != "ollama.exe"
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9A-F]{64}", digest) is None
    ):
        raise EvidenceValidationError(f"{case_id}: {field} is not a complete Ollama process identity")
    return value


def _validate_a20_scope_runtime_model(
    raw_payload: dict[str, object], *, case_id: str
) -> str:
    scope = _raw_object(raw_payload, "scope", case_id=case_id, field="scope")
    owner_hash = scope.get("test_owned_service_owner_sha256")
    if (
        scope.get("ollama_url") != "http://127.0.0.1:11435"
        or scope.get("external_11434_policy")
        != "non_mutating_intent_not_used_for_preload_or_unload"
        or scope.get("model_store_policy")
        != "non_mutating_api_intent_full_tree_verified"
        or scope.get("qwen_model") != "qwen3:4b"
        or scope.get("stt_model") != "small"
        or scope.get("stt_model_source") != "app.stt.schemas.DEFAULT_STT_MODEL_ID"
        or scope.get("stt_model_fallback") != "DISALLOWED"
        or scope.get("stt_device") != "cpu"
        or scope.get("stt_compute_type") != "int8"
        or scope.get("tts_provider") != "windows"
        or scope.get("model_download") != "NOT_CALLED"
        or scope.get("model_delete") != "NOT_CALLED"
        or any(scope.get(name) != "NOT_RUN" for name in (
            "microphone_capture", "tts_playback", "desktop_renderer", "endurance"
        ))
        or not isinstance(owner_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", owner_hash) is None
    ):
        raise EvidenceValidationError(f"{case_id}: resource run scope/ownership is incomplete")

    runtime = _raw_object(raw_payload, "runtime", case_id=case_id, field="runtime")
    if any(runtime.get(name) is not True for name in (
        "database_isolated", "logs_isolated", "temporary_audio_isolated"
    )):
        raise EvidenceValidationError(f"{case_id}: resource run was not isolated")
    model_link = _raw_object(
        runtime, "formal_model_link", case_id=case_id, field="runtime.formal_model_link"
    )
    if model_link.get("copy_performed") is not False or not str(model_link.get("kind") or ""):
        raise EvidenceValidationError(f"{case_id}: formal STT model link contract is incomplete")

    stt_model = _raw_object(raw_payload, "stt_model", case_id=case_id, field="stt_model")
    if (
        stt_model.get("id") != "small"
        or stt_model.get("installed_before_run") is not True
        or not _positive_int(stt_model.get("file_count"))
        or not _positive_int(stt_model.get("size_bytes"))
        or stt_model.get("download_operation") != "NOT_CALLED"
        or stt_model.get("delete_operation") != "NOT_CALLED"
    ):
        raise EvidenceValidationError(f"{case_id}: pre-existing default small model was not proven")

    return str(owner_hash)


def _validate_a20_store_and_service(
    results: dict[str, object], *, case_id: str, owner_hash: str
) -> tuple[dict[str, object], int]:
    store_before = _raw_object(
        results, "ollama_model_store_before", case_id=case_id,
        field="results.ollama_model_store_before",
    )
    store_digest = store_before.get("whole_tree_sha256")
    if (
        store_before.get("mode") != "non_mutating_api_intent"
        or store_before.get("links_followed") is not False
        or not _positive_int(store_before.get("regular_file_count"))
        or not _positive_int(store_before.get("regular_file_bytes"))
        or not isinstance(store_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", store_digest) is None
    ):
        raise EvidenceValidationError(f"{case_id}: full model-store tree fingerprint is incomplete")

    service_before = _raw_object(
        results, "ollama_service_before", case_id=case_id,
        field="results.ollama_service_before",
    )
    service = _raw_object(
        service_before, "service", case_id=case_id,
        field="results.ollama_service_before.service",
    )
    identity = _a26_identity(
        service_before.get("identity"), case_id=case_id,
        field="results.ollama_service_before.identity",
    )
    owned_pid = identity["pid"]
    if (
        service_before.get("mode") != "test_owned_managed"
        or service_before.get("owner_sha256") != owner_hash
        or service.get("status") != "MANAGED_RUNNING"
        or service.get("mode") != "managed"
        or service.get("api_healthy") is not True
        or service.get("owner_sha256") != owner_hash
        or service.get("managed_port") != 11435
        or service.get("managed_pid") != owned_pid
        or service.get("listener_pid") != owned_pid
    ):
        raise EvidenceValidationError(f"{case_id}: test-owned Ollama binding is incomplete")

    return store_before, int(owned_pid)


def _validate_a20_resource_binding(
    value: object, *, case_id: str, field: str, owned_pid: int
) -> None:
    resource = value if isinstance(value, dict) else {}
    resource_snapshot = resource.get("snapshot")
    if (
        not isinstance(resource_snapshot, dict)
        or resource.get("ollama_listener_pid") != owned_pid
        or resource_snapshot.get("ollama_pid") != owned_pid
    ):
        raise EvidenceValidationError(f"{case_id}: {field} is not bound to owned Ollama")


def _validate_a20_preload(
    results: dict[str, object], *, case_id: str, owned_pid: int
) -> None:
    preflight = _raw_object(results, "preflight", case_id=case_id, field="results.preflight")
    resources = _raw_object(preflight, "resources", case_id=case_id, field="results.preflight.resources")
    admission = _raw_object(resources, "admission", case_id=case_id, field="results.preflight.resources.admission")
    snapshot = _raw_object(resources, "snapshot", case_id=case_id, field="results.preflight.resources.snapshot")
    if (
        preflight.get("ok") is not True
        or preflight.get("action") != "preflight"
        or _a26_model_names(preflight.get("running")) != []
        or "qwen3:4b" not in _a26_model_names(preflight.get("installed"))
        or snapshot.get("ollama_pid") != owned_pid
        or not isinstance(snapshot.get("system_total_bytes"), int)
        or isinstance(snapshot.get("system_total_bytes"), bool)
        or int(snapshot["system_total_bytes"]) < 15 * 1024**3
        or not isinstance(snapshot.get("gpu_total_bytes"), int)
        or isinstance(snapshot.get("gpu_total_bytes"), bool)
        or int(snapshot["gpu_total_bytes"]) < 6 * 1024**3
        or any(
            not isinstance(admission.get(name), dict)
            or admission[name].get("allowed") is not True
            for name in ("model_preload", "stt_cpu", "voice")
        )
    ):
        raise EvidenceValidationError(f"{case_id}: idle preflight/admission proof is incomplete")

    preload = _raw_object(results, "qwen_preload", case_id=case_id, field="results.qwen_preload")
    loaded = _raw_object(preload, "loaded", case_id=case_id, field="results.qwen_preload.loaded")
    if (
        preload.get("ok") is not True
        or preload.get("action") != "preload_qwen"
        or loaded.get("status") != "LOADED"
        or loaded.get("model") != "qwen3:4b"
        or _a26_model_names(preload.get("running_after")) != ["qwen3:4b"]
    ):
        raise EvidenceValidationError(f"{case_id}: qwen preload was not proven")

    _validate_a20_resource_binding(
        results.get("resources_before_preload"),
        case_id=case_id,
        field="resources_before_preload",
        owned_pid=owned_pid,
    )
    _validate_a20_resource_binding(
        results.get("resources_with_qwen"),
        case_id=case_id,
        field="resources_with_qwen",
        owned_pid=owned_pid,
    )


def _validate_a20_stt(
    results: dict[str, object], *, case_id: str, owned_pid: int
) -> None:
    stt = _raw_object(results, "stt_with_qwen", case_id=case_id, field="results.stt_with_qwen")
    settings = _raw_object(stt, "settings", case_id=case_id, field="results.stt_with_qwen.settings")
    load = _raw_object(stt, "load", case_id=case_id, field="results.stt_with_qwen.load")
    status = _raw_object(stt, "status", case_id=case_id, field="results.stt_with_qwen.status")
    transcription = _raw_object(
        stt, "transcription", case_id=case_id, field="results.stt_with_qwen.transcription"
    )
    if (
        settings.get("model_id") != "small"
        or settings.get("device") != "cpu"
        or settings.get("compute_type") != "int8"
        or settings.get("gpu_experimental") is not False
        or load.get("status") != "READY"
        or load.get("model") != "small"
        or status.get("loaded_model") != "small"
        or not _positive_int(status.get("worker_pid"))
        or transcription.get("provider") != "faster_whisper"
        or transcription.get("model") != "small"
        or not str(transcription.get("text") or "").strip()
    ):
        raise EvidenceValidationError(f"{case_id}: real CPU int8 small-STT proof is incomplete")
    _validate_a20_resource_binding(
        stt.get("resources"), case_id=case_id, field="stt resources", owned_pid=owned_pid
    )


def _validate_a20_tts(
    results: dict[str, object], *, case_id: str, owned_pid: int
) -> None:
    tts = _raw_object(
        results, "tts_with_qwen_and_stt", case_id=case_id,
        field="results.tts_with_qwen_and_stt",
    )
    tts_settings = _raw_object(tts, "settings", case_id=case_id, field="results.tts.settings")
    audio = _raw_object(tts, "audio", case_id=case_id, field="results.tts.audio")
    playback = _raw_object(tts, "playback", case_id=case_id, field="results.tts.playback")
    if (
        tts_settings.get("provider") != "windows"
        or tts_settings.get("fallback_provider") != "windows"
        or tts_settings.get("allow_fallback") is not False
        or tts_settings.get("cache_enabled") is not False
        or tts.get("provider") != "windows"
        or not _positive_int(tts.get("duration_ms"))
        or audio.get("http_status") != 200
        or not isinstance(audio.get("bytes"), int)
        or isinstance(audio.get("bytes"), bool)
        or int(audio["bytes"]) <= 46
        or not _positive_int(audio.get("frames"))
        or not _positive_int(audio.get("sample_rate"))
        or not _positive_int(audio.get("channels"))
        or playback.get("queued_status") != "QUEUED"
        or playback.get("started_status") != "PLAYING"
        or playback.get("completed_status") != "COMPLETED"
        or playback.get("queue_after") != 0
        or playback.get("status_after") != "IDLE"
        or playback.get("active_synthesis_after") != 0
        or results.get("tts_temp_residual_after_lifecycle") != []
    ):
        raise EvidenceValidationError(f"{case_id}: real Windows TTS lifecycle proof is incomplete")
    _validate_a20_resource_binding(
        tts.get("resources"), case_id=case_id, field="TTS resources", owned_pid=owned_pid
    )


def _validate_a20_cleanup(
    raw_payload: dict[str, object], *, case_id: str, store_before: dict[str, object]
) -> None:
    cleanup = _raw_object(raw_payload, "cleanup", case_id=case_id, field="cleanup")
    stt_unload = _raw_object(cleanup, "stt_unload", case_id=case_id, field="cleanup.stt_unload")
    qwen_unload = _raw_object(cleanup, "qwen_unload", case_id=case_id, field="cleanup.qwen_unload")
    qwen_unloaded = _raw_object(
        qwen_unload, "unloaded", case_id=case_id, field="cleanup.qwen_unload.unloaded"
    )
    sidecar = _raw_object(cleanup, "sidecar", case_id=case_id, field="cleanup.sidecar")
    tree_cleanup = _raw_object(sidecar, "tree_cleanup", case_id=case_id, field="cleanup.sidecar.tree_cleanup")
    stop = _raw_object(
        cleanup, "test_owned_ollama_stop", case_id=case_id,
        field="cleanup.test_owned_ollama_stop",
    )
    stopped_service = _raw_object(stop, "service", case_id=case_id, field="cleanup.test_owned_ollama_stop.service")
    cleanup_errors = [name for name in cleanup if name.endswith("_error")]
    if (
        cleanup.get("test_owned_ollama_start_attempted") is not True
        or cleanup.get("qwen_loaded_by_script") is not True
        or stt_unload.get("status") != "UNLOADED"
        or stt_unload.get("model") != "small"
        or qwen_unload.get("ok") is not True
        or qwen_unload.get("action") != "unload_owned_qwen"
        or qwen_unloaded.get("status") != "UNLOADED"
        or qwen_unloaded.get("model") != "qwen3:4b"
        or _a26_model_names(qwen_unload.get("running_before")) != ["qwen3:4b"]
        or _a26_model_names(qwen_unload.get("running_after")) != []
        or tree_cleanup.get("tree_terminated") is not True
        or cleanup.get("tts_temp_residual_after_lifecycle") != []
        or cleanup.get("tts_temp_residual_after_sidecar_stop") != []
        or cleanup.get("formal_models_link_removed") is not True
        or stop.get("ok") is not True
        or stop.get("action") != "stop_test_owned_service"
        or stopped_service.get("stopped") is not True
        or stopped_service.get("status") != "INSTALLED_STOPPED"
        or stopped_service.get("api_healthy") is not False
        or any(stopped_service.get(name) is not None for name in (
            "mode", "managed_pid", "listener_pid", "managed_port",
            "managed_started_at", "owner_sha256"
        ))
        or stopped_service.get("managed_state_unowned") is not False
        or cleanup.get("ollama_processes_after_stop") != []
        or cleanup.get("ollama_model_store_after") != store_before
        or cleanup_errors
    ):
        raise EvidenceValidationError(f"{case_id}: owned resource cleanup/store settlement is incomplete")


def _validate_a20_resource_live(raw_payload: dict[str, object], *, case_id: str) -> None:
    """Require A20's complete owned-service/resource/cleanup contract."""

    owner_hash = _validate_a20_scope_runtime_model(raw_payload, case_id=case_id)
    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    store_before, owned_pid = _validate_a20_store_and_service(
        results, case_id=case_id, owner_hash=owner_hash
    )
    _validate_a20_preload(results, case_id=case_id, owned_pid=owned_pid)
    _validate_a20_stt(results, case_id=case_id, owned_pid=owned_pid)
    _validate_a20_tts(results, case_id=case_id, owned_pid=owned_pid)
    _validate_a20_cleanup(raw_payload, case_id=case_id, store_before=store_before)


def _validate_a26_target_scope_summary(
    raw_payload: dict[str, object], *, case_id: str
) -> str:
    target = _raw_object(raw_payload, "target", case_id=case_id, field="target")
    owner_hash = target.get("owner_sha256")
    if (
        target.get("platform") != "windows"
        or target.get("os_name") != "nt"
        or target.get("sys_platform") != "win32"
        or target.get("service_mode") != "test_owned_managed"
        or target.get("base_url") != "http://127.0.0.1:11435"
        or target.get("port") != 11435
        or target.get("model") != "qwen3:4b"
        or target.get("keep_alive") != "5m"
        or not isinstance(owner_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", owner_hash) is None
    ):
        raise EvidenceValidationError(f"{case_id}: raw evidence is not bound to test-owned 11435 qwen3:4b")
    scope = _raw_object(raw_payload, "scope", case_id=case_id, field="scope")
    if (
        scope.get("external_11434_policy") != "PROTECTED_NOT_TOUCHED"
        or scope.get("test_owned_11435_only") is not True
        or scope.get("model_store") != "NON_MUTATING_API_INTENT_FULL_TREE_VERIFIED"
        or scope.get("model_download") != "NOT_CALLED"
        or scope.get("model_delete") != "NOT_CALLED"
        or scope.get("chat_prompt") != "NOT_SENT"
        or scope.get("paid_provider_calls") != "NONE"
        or scope.get("stt_actions") != "NONE"
        or scope.get("tts_actions") != "NONE"
        or scope.get("microphone_capture") != "NOT_RUN"
    ):
        raise EvidenceValidationError(f"{case_id}: raw evidence crossed its protected Ollama-only scope")

    summary = _raw_object(raw_payload, "test_summary", case_id=case_id, field="test_summary")
    tests = summary.get("tests")
    if (
        summary.get("framework") != "structured_production_lifecycle_probes"
        or summary.get("collected") != len(A26_REQUIRED_STRUCTURED_TESTS)
        or summary.get("executed") != len(A26_REQUIRED_STRUCTURED_TESTS)
        or summary.get("passed") != len(A26_REQUIRED_STRUCTURED_TESTS)
        or summary.get("failed") != 0
        or summary.get("skipped") != 0
        or not isinstance(tests, list)
        or len(tests) != len(A26_REQUIRED_STRUCTURED_TESTS)
        or [item.get("name") for item in tests if isinstance(item, dict)]
        != list(A26_REQUIRED_STRUCTURED_TESTS)
        or any(not isinstance(item, dict) or item.get("status") != "PASS" for item in tests)
    ):
        raise EvidenceValidationError(f"{case_id}: all structured Ollama probes must execute and pass with zero skips")
    assert isinstance(owner_hash, str)
    return owner_hash


def _validate_a26_preconditions(
    raw_payload: dict[str, object], *, case_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    if results.get("ollama_processes_before") != []:
        raise EvidenceValidationError(f"{case_id}: a pre-existing Ollama process was present")
    port_before = _raw_object(
        results, "test_owned_port_before", case_id=case_id, field="results.test_owned_port_before"
    )
    if port_before.get("port") != 11435 or port_before.get("listening") is not False:
        raise EvidenceValidationError(f"{case_id}: test-owned port was occupied before execution")
    model_store_before = _raw_object(
        results, "model_store_before", case_id=case_id, field="results.model_store_before"
    )
    store_digest = model_store_before.get("whole_tree_sha256")
    if (
        model_store_before.get("mode") != "non_mutating_api_intent"
        or model_store_before.get("links_followed") is not False
        or not _positive_int(model_store_before.get("regular_file_count"))
        or not _positive_int(model_store_before.get("regular_file_bytes"))
        or not isinstance(store_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", store_digest) is None
    ):
        raise EvidenceValidationError(
            f"{case_id}: non-mutating full-tree model-store fingerprint was not proven"
        )
    return results, model_store_before


def _validate_a26_start_and_preload(
    results: dict[str, object], *, owner_hash: str, case_id: str
) -> dict[str, object]:
    service_start = _raw_object(
        results, "service_start", case_id=case_id, field="results.service_start"
    )
    start_controller = _raw_object(
        service_start, "controller", case_id=case_id, field="results.service_start.controller"
    )
    service = _raw_object(
        start_controller,
        "service",
        case_id=case_id,
        field="results.service_start.controller.service",
    )
    start_identity = _a26_identity(
        service_start.get("process_identity"),
        case_id=case_id,
        field="results.service_start.process_identity",
    )
    if (
        start_controller.get("ok") is not True
        or start_controller.get("action") != "start_test_owned_service"
        or service.get("status") != "MANAGED_RUNNING"
        or service.get("mode") != "managed"
        or service.get("api_healthy") is not True
        or service.get("owner_sha256") != owner_hash
        or service.get("managed_pid") != start_identity.get("pid")
        or service.get("listener_pid") != start_identity.get("pid")
        or service.get("managed_port") != 11435
        or service.get("managed_state_unowned") is not False
    ):
        raise EvidenceValidationError(f"{case_id}: test-owned service PID/owner/port identity was not proven")

    preflight = _raw_object(results, "preflight", case_id=case_id, field="results.preflight")
    if (
        preflight.get("ok") is not True
        or preflight.get("action") != "preflight"
        or _a26_model_names(preflight.get("running")) != []
        or "qwen3:4b" not in _a26_model_names(preflight.get("installed"))
    ):
        raise EvidenceValidationError(f"{case_id}: idle preflight or installed qwen3:4b was not proven")
    preload = _raw_object(results, "preload", case_id=case_id, field="results.preload")
    loaded = _raw_object(preload, "loaded", case_id=case_id, field="results.preload.loaded")
    if (
        preload.get("ok") is not True
        or preload.get("action") != "preload_qwen"
        or loaded.get("status") != "LOADED"
        or loaded.get("model") != "qwen3:4b"
        or loaded.get("keep_alive") != "5m"
        or not isinstance(loaded.get("load_ms"), (int, float))
        or isinstance(loaded.get("load_ms"), bool)
        or float(loaded["load_ms"]) <= 0
        or _a26_model_names(preload.get("running_after")) != ["qwen3:4b"]
    ):
        raise EvidenceValidationError(f"{case_id}: production qwen3:4b preload and /api/ps were not proven")
    preload_identity = _a26_identity(
        results.get("process_identity_after_preload"),
        case_id=case_id,
        field="results.process_identity_after_preload",
    )
    if preload_identity != start_identity:
        raise EvidenceValidationError(f"{case_id}: Ollama process identity changed during preload")
    return start_identity


def _validate_a26_cleanup(
    raw_payload: dict[str, object],
    *,
    model_store_before: dict[str, object],
    start_identity: dict[str, object],
    case_id: str,
) -> None:
    cleanup = _raw_object(raw_payload, "cleanup", case_id=case_id, field="cleanup")
    before_unload = cleanup.get("processes_before_unload")
    before_stop = cleanup.get("processes_before_stop")
    if (
        not isinstance(before_unload, list)
        or len(before_unload) != 1
        or _a26_identity(before_unload[0], case_id=case_id, field="cleanup.processes_before_unload[0]") != start_identity
        or not isinstance(before_stop, list)
        or len(before_stop) != 1
        or _a26_identity(before_stop[0], case_id=case_id, field="cleanup.processes_before_stop[0]") != start_identity
        or cleanup.get("only_test_owned_before_stop") is not True
    ):
        raise EvidenceValidationError(f"{case_id}: external-process protection was lost during cleanup")
    unload = _raw_object(cleanup, "unload", case_id=case_id, field="cleanup.unload")
    unloaded = _raw_object(unload, "unloaded", case_id=case_id, field="cleanup.unload.unloaded")
    release = unloaded.get("resource_release_observed")
    release_ok = isinstance(release, dict) and any(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in release.values()
    )
    if (
        unload.get("ok") is not True
        or unload.get("action") != "unload_owned_qwen"
        or unloaded.get("status") != "UNLOADED"
        or unloaded.get("model") != "qwen3:4b"
        or "qwen3:4b" not in _a26_model_names(unload.get("running_before"))
        or "qwen3:4b" in _a26_model_names(unload.get("running_after"))
        or not release_ok
    ):
        raise EvidenceValidationError(f"{case_id}: unload, /api/ps convergence, or resource release was not proven")
    stopped = _raw_object(
        cleanup, "service_stop", case_id=case_id, field="cleanup.service_stop"
    )
    stopped_service = _raw_object(
        stopped, "service", case_id=case_id, field="cleanup.service_stop.service"
    )
    if (
        stopped.get("ok") is not True
        or stopped.get("action") != "stop_test_owned_service"
        or stopped_service.get("stopped") is not True
        or stopped_service.get("status") != "INSTALLED_STOPPED"
        or stopped_service.get("api_healthy") is not False
        or stopped_service.get("mode") is not None
        or stopped_service.get("managed_pid") is not None
        or stopped_service.get("listener_pid") is not None
        or stopped_service.get("managed_port") is not None
        or stopped_service.get("owner_sha256") is not None
        or stopped_service.get("managed_state_unowned") is not False
        or cleanup.get("model_store_after") != model_store_before
        or cleanup.get("ollama_processes_after_stop") != []
    ):
        raise EvidenceValidationError(f"{case_id}: test-owned service cleanup or model-store protection failed")
    port_after = _raw_object(
        cleanup,
        "test_owned_port_after_stop",
        case_id=case_id,
        field="cleanup.test_owned_port_after_stop",
    )
    if port_after.get("port") != 11435 or port_after.get("listening") is not False:
        raise EvidenceValidationError(f"{case_id}: test-owned port remained occupied after cleanup")


def _validate_a26_ollama_live(raw_payload: dict[str, object], *, case_id: str) -> None:
    """Require real structured probes; pytest exit-zero/all-skip is insufficient."""

    owner_hash = _validate_a26_target_scope_summary(raw_payload, case_id=case_id)
    results, model_store_before = _validate_a26_preconditions(raw_payload, case_id=case_id)
    start_identity = _validate_a26_start_and_preload(
        results, owner_hash=owner_hash, case_id=case_id
    )
    _validate_a26_cleanup(
        raw_payload,
        model_store_before=model_store_before,
        start_identity=start_identity,
        case_id=case_id,
    )


def _desktop_voice_number(
    value: object, *, case_id: str, field: str, minimum: float = 0
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or float(value) < minimum
    ):
        raise EvidenceValidationError(
            f"{case_id}: desktop voice {field} must be a number >= {minimum}"
        )
    return float(value)


def _validate_desktop_voice_candidate(
    raw_payload: dict[str, object], *, case_id: str
) -> None:
    candidate = _raw_object(raw_payload, "candidate", case_id=case_id, field="candidate")
    health = _raw_object(candidate, "health", case_id=case_id, field="candidate.health")
    source = _raw_object(raw_payload, "source", case_id=case_id, field="source")
    runtime = _raw_object(raw_payload, "runtime", case_id=case_id, field="runtime")
    ownership = _raw_object(raw_payload, "ownership", case_id=case_id, field="ownership")
    digest = candidate.get("sha256")
    if (
        raw_payload.get("interactive_operator") is not True
        or source.get("source_version") != TARGET_VERSION
        or source.get("workspace_clean") is not True
        or candidate.get("filename") != "司忆.exe"
        or candidate.get("identity_verified") is not True
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9A-F]{64}", digest) is None
        or health.get("version") != TARGET_VERSION
        or health.get("embedded") is not True
        or health.get("workspace_state") != "CLEAN"
        or health.get("git_commit") != source.get("source_commit")
        or health.get("source_fingerprint") != source.get("source_tree_fingerprint")
        or runtime.get("isolated") is not True
        or runtime.get("retained") is not False
        or ownership.get("verified") is not True
        or not _positive_int(ownership.get("desktop_pid"))
        or not _positive_int(ownership.get("sidecar_pid"))
        or ownership.get("sidecar_parent_pid") != ownership.get("desktop_pid")
        or not _positive_int(ownership.get("api_port"))
    ):
        raise EvidenceValidationError(
            f"{case_id}: desktop voice candidate/source/PID identity is incomplete"
        )


def _validate_desktop_voice_events(
    raw_payload: dict[str, object], *, case_id: str
) -> None:
    events = raw_payload.get("operator_events")
    required_actions = DESKTOP_VOICE_ACTION_CASES[case_id]
    if not isinstance(events, list) or len(events) != len(DESKTOP_VOICE_ACTION_CONTRACT):
        raise EvidenceValidationError(f"{case_id}: desktop voice operator events are missing")
    actions: set[str] = set()
    challenges: set[str] = set()
    previous_completed: datetime | None = None
    for sequence, event_value in enumerate(events, 1):
        if not isinstance(event_value, dict):
            raise EvidenceValidationError(f"{case_id}: desktop voice event must be an object")
        expected_action, expected_case_ids, expected_scope = DESKTOP_VOICE_ACTION_CONTRACT[
            sequence - 1
        ]
        action = event_value.get("action")
        challenge = event_value.get("challenge")
        outcome = event_value.get("outcome")
        elapsed = _desktop_voice_number(
            event_value.get("elapsed_seconds"),
            case_id=case_id,
            field="event.elapsed_seconds",
            minimum=0.25,
        )
        if (
            event_value.get("sequence") != sequence
            or action != expected_action
            or event_value.get("case_ids") != list(expected_case_ids)
            or event_value.get("evidence_scope") != expected_scope
            or event_value.get("real_microphone")
            is not (expected_scope == "REAL_MICROPHONE")
            or event_value.get("valid") is not True
            or outcome != "PASS"
            or event_value.get("formal_candidate") is not True
            or not isinstance(challenge, str)
            or re.fullmatch(r"[0-9A-F]{24}", challenge) is None
            or challenge in challenges
            or elapsed > 600
            or event_value.get("confirmation_sha256")
            != hashlib.sha256(f"PASS {challenge}".encode("ascii")).hexdigest().upper()
        ):
            raise EvidenceValidationError(f"{case_id}: desktop voice event is forge-like")
        try:
            issued = datetime.fromisoformat(str(event_value.get("issued_at")).replace("Z", "+00:00"))
            completed = datetime.fromisoformat(str(event_value.get("completed_at")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceValidationError(f"{case_id}: desktop voice event time is invalid") from exc
        if (
            issued.tzinfo is None
            or completed.tzinfo is None
            or completed < issued
            or (previous_completed is not None and issued < previous_completed)
            or abs((completed - issued).total_seconds() - elapsed) > 10
        ):
            raise EvidenceValidationError(f"{case_id}: desktop voice event time is inconsistent")
        actions.add(action)
        challenges.add(challenge)
        previous_completed = completed
    if not required_actions.issubset(actions):
        raise EvidenceValidationError(
            f"{case_id}: desktop voice evidence misses required case actions"
        )


def _desktop_voice_results(
    raw_payload: dict[str, object], *, case_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    database = _raw_object(results, "database", case_id=case_id, field="results.database")
    return results, database


def _validate_desktop_voice_dangerous_confirmation(
    results: dict[str, object], *, case_id: str
) -> None:
    proof = _raw_object(
        results,
        "dangerous_confirmation",
        case_id=case_id,
        field="results.dangerous_confirmation",
    )
    expected = {
        "task_count",
        "waiting_confirmation_count",
        "cancelled_count",
        "active_count",
        "critical_confirmation_required_count",
        "critical_executed_count",
    }
    phases: dict[str, dict[str, object]] = {}
    for name in ("before_reject", "after_reject"):
        phase = _raw_object(proof, name, case_id=case_id, field=f"results.dangerous_confirmation.{name}")
        if set(phase) != expected or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in phase.values()
        ):
            raise EvidenceValidationError(
                f"{case_id}: dangerous-confirmation audit must contain only aggregate metadata"
            )
        phases[name] = phase
    before = phases["before_reject"]
    after = phases["after_reject"]
    if (
        before["task_count"] < 1
        or before["waiting_confirmation_count"] < 1
        or before["critical_confirmation_required_count"] < 1
        or before["critical_executed_count"] != 0
        or after["task_count"] < 1
        or after["cancelled_count"] < 1
        or after["waiting_confirmation_count"] != 0
        or after["active_count"] != 0
        or after["critical_executed_count"] != 0
    ):
        raise EvidenceValidationError(
            f"{case_id}: dangerous confirmation was not observed before rejection and safe cancellation"
        )


def _validate_desktop_voice_global_stop(
    results: dict[str, object], *, case_id: str
) -> None:
    proof = _raw_object(results, "global_stop", case_id=case_id, field="results.global_stop")
    expected_events = {
        "global_stop_recording": "stt_cancelled",
        "global_stop_stt": "stt_cancelled",
        "global_stop_agent": "agent_cancelled",
        "global_stop_tts": "tts_stopped",
    }
    audit_fields = {
        "receipt_count",
        "first_target_count",
        "second_target_count",
        "first_settled",
        "second_settled",
        "second_idempotent_no_active_target",
        "metadata_only",
    }
    machine_fields = {
        "stt_cancelled",
        "agent_cancelled",
        "tts_stopped",
        "voice_terminal",
        "active_voice",
        "active_stt",
        "active_tts",
        "active_tasks",
        "active_queue",
        "temporary_audio_files",
    }
    if set(proof) != set(expected_events):
        raise EvidenceValidationError(f"{case_id}: global-stop stages are incomplete")
    for stage_name, required_event in expected_events.items():
        stage = _raw_object(proof, stage_name, case_id=case_id, field=f"results.global_stop.{stage_name}")
        audit = _raw_object(stage, "audit", case_id=case_id, field=f"results.global_stop.{stage_name}.audit")
        machine = _raw_object(stage, "machine", case_id=case_id, field=f"results.global_stop.{stage_name}.machine")
        if (
            set(audit) != audit_fields
            or set(machine) != machine_fields
            or audit.get("receipt_count") != 2
            or not isinstance(audit.get("first_target_count"), int)
            or isinstance(audit.get("first_target_count"), bool)
            or audit["first_target_count"] < 1
            or audit.get("second_target_count") != 0
            or audit.get("first_settled") is not True
            or audit.get("second_settled") is not True
            or audit.get("second_idempotent_no_active_target") is not True
            or audit.get("metadata_only") is not True
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in machine.values())
            or machine[required_event] < 1
            or machine["voice_terminal"] < 1
            or any(machine[name] != 0 for name in ("active_voice", "active_stt", "active_tts", "active_tasks", "active_queue", "temporary_audio_files"))
        ):
            raise EvidenceValidationError(
                f"{case_id}: global-stop stage lacks two settled, metadata-only audit receipts"
            )


def _validate_desktop_voice_privacy(
    results: dict[str, object], database: dict[str, object], *, case_id: str
) -> None:
    privacy = _raw_object(results, "privacy", case_id=case_id, field="results.privacy")
    scans = _raw_object(privacy, "runtime_scans", case_id=case_id, field="results.privacy.runtime_scans")
    if set(scans) != {"logs", "crash", "temp"}:
        raise EvidenceValidationError(f"{case_id}: formal privacy scan scope is incomplete")
    for area, value in scans.items():
        scan = value if isinstance(value, dict) else {}
        if (
            scan.get("marker_matches") != 0
            or not isinstance(scan.get("bytes_scanned"), int)
            or isinstance(scan.get("bytes_scanned"), bool)
            or int(scan["bytes_scanned"]) < 0
            or not isinstance(scan.get("aggregate_sha256"), str)
            or re.fullmatch(r"[0-9A-F]{64}", str(scan["aggregate_sha256"])) is None
        ):
            raise EvidenceValidationError(f"{case_id}: privacy scan {area} is invalid")
    diagnostics = _raw_object(
        privacy, "diagnostics", case_id=case_id, field="results.privacy.diagnostics"
    )
    database_privacy = _raw_object(
        privacy, "database", case_id=case_id, field="results.privacy.database"
    )
    forbidden = _raw_object(
        database_privacy,
        "forbidden_storage_matches",
        case_id=case_id,
        field="results.privacy.database.forbidden_storage_matches",
    )
    messages = _raw_object(database, "messages", case_id=case_id, field="database.messages")
    if (
        diagnostics.get("entry_count", 0) < 1
        or diagnostics.get("marker_matches") != 0
        or database_privacy.get("all_forbidden_zero") is not True
        or not forbidden
        or any(value != 0 for value in forbidden.values())
        or messages.get("privacy_message_count", 0) < 1
        or privacy.get("temporary_audio_files_after") != 0
    ):
        raise EvidenceValidationError(
            f"{case_id}: real-microphone privacy/diagnostic/temp proof is incomplete"
        )


def _validate_desktop_voice_offline(
    results: dict[str, object], database: dict[str, object], *, case_id: str
) -> None:
    network = _raw_object(results, "network", case_id=case_id, field="results.network")
    offline = _raw_object(network, "offline", case_id=case_id, field="results.network.offline")
    provider = _raw_object(network, "provider", case_id=case_id, field="results.network.provider")
    connections = _raw_object(
        network, "connections", case_id=case_id, field="results.network.connections"
    )
    database_offline = _raw_object(
        database, "offline", case_id=case_id, field="database.offline"
    )
    if (
        not _positive_int(offline.get("physical_count"))
        or offline.get("up_count") != 0
        or provider.get("provider_id") != "ollama"
        or provider.get("base_url") not in {
            "http://127.0.0.1:11434",
            "http://localhost:11434",
        }
        or provider.get("model") != "qwen3:4b"
        or connections.get("public_connections") != []
        or database_offline.get("successful_ollama_runs", 0) < 1
        or database_offline.get("completed_tts", 0) < 1
        or network.get("after") != network.get("before")
    ):
        raise EvidenceValidationError(
            f"{case_id}: disconnected local Ollama voice/STT/TTS proof is incomplete"
        )


def _validate_desktop_voice_live(
    raw_payload: dict[str, object], *, case_id: str
) -> None:
    """Reject arbitrary UI exit-zero or synthetic/file-upload evidence."""

    input_contract = _raw_object(
        raw_payload, "input_contract", case_id=case_id, field="input_contract"
    )
    cleanup = _raw_object(raw_payload, "cleanup", case_id=case_id, field="cleanup")
    if (
        input_contract.get("microphone") != "REAL_WINDOWS_DEVICE_ONLY"
        or input_contract.get("audio_file_upload") != "FORBIDDEN"
        or input_contract.get("synthetic_speech") != "FORBIDDEN"
        or cleanup.get("owned_processes_released") is not True
        or cleanup.get("runtime_removed") is not True
        or cleanup.get("model_junction_removed") is not True
        or cleanup.get("external_processes_protected") is not True
        or cleanup.get("forced") is True
    ):
        raise EvidenceValidationError(
            f"{case_id}: desktop voice input or owned cleanup contract is incomplete"
        )
    _validate_desktop_voice_candidate(raw_payload, case_id=case_id)
    _validate_desktop_voice_events(raw_payload, case_id=case_id)
    results, database = _desktop_voice_results(raw_payload, case_id=case_id)
    if case_id == "A15":
        _validate_desktop_voice_dangerous_confirmation(results, case_id=case_id)
    elif case_id == "A17":
        _validate_desktop_voice_global_stop(results, case_id=case_id)
    elif case_id == "A19":
        _validate_desktop_voice_offline(results, database, case_id=case_id)
    elif case_id == "A24":
        _validate_desktop_voice_privacy(results, database, case_id=case_id)


def _a23_object(value: object, *, case_id: str, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"{case_id}: {field} must be an object")
    return value


def _a23_nonnegative_number(value: object, *, case_id: str, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise EvidenceValidationError(f"{case_id}: {field} must be a non-negative number")
    return float(value)


def _validate_a23_operator_events(raw_payload: dict[str, object], *, case_id: str) -> None:
    events = raw_payload.get("events")
    if not isinstance(events, list) or not events:
        raise EvidenceValidationError(f"{case_id}: operator event timeline is missing")
    counts: Counter[str] = Counter()
    challenges: set[str] = set()
    previous_elapsed = -1.0
    previous_completed: datetime | None = None
    duration = _a23_nonnegative_number(
        raw_payload.get("duration_seconds"), case_id=case_id, field="duration_seconds"
    )
    for expected_sequence, event_raw in enumerate(events, 1):
        event = _a23_object(event_raw, case_id=case_id, field="events[]")
        action = event.get("action")
        challenge = event.get("challenge")
        elapsed = _a23_nonnegative_number(
            event.get("run_elapsed_seconds"), case_id=case_id, field="events[].run_elapsed_seconds"
        )
        confirmation_elapsed = _a23_nonnegative_number(
            event.get("elapsed_seconds"), case_id=case_id, field="events[].elapsed_seconds"
        )
        if (
            event.get("sequence") != expected_sequence
            or action not in A23_ACTION_REQUIREMENTS
            or event.get("valid") is not True
            or event.get("outcome") != "PASS"
            or not isinstance(challenge, str)
            or not re.fullmatch(r"[0-9A-F]{24}", challenge)
            or challenge in challenges
            or not isinstance(event.get("confirmation_sha256"), str)
            or not re.fullmatch(r"[0-9A-F]{64}", str(event["confirmation_sha256"]))
            or confirmation_elapsed < 0.25
            or confirmation_elapsed > 300
            or elapsed < previous_elapsed
            or elapsed > duration
        ):
            raise EvidenceValidationError(f"{case_id}: operator event timeline is invalid or forge-like")
        variant = event.get("variant")
        if action == "device_change" and variant not in {"REAL", "SIMULATED"}:
            raise EvidenceValidationError(f"{case_id}: device-change confirmation lacks a valid variant")
        if action == "model_load_unload" and variant not in {"OLLAMA", "STT"}:
            raise EvidenceValidationError(f"{case_id}: model-cycle confirmation lacks a valid variant")
        if action not in {"device_change", "model_load_unload"} and variant is not None:
            raise EvidenceValidationError(f"{case_id}: an unrelated event carries a confirmation variant")
        digest_material = " ".join(
            item for item in ("PASS", challenge, str(variant or "")) if item
        ).encode("ascii")
        if event.get("confirmation_sha256") != hashlib.sha256(digest_material).hexdigest().upper():
            raise EvidenceValidationError(f"{case_id}: operator challenge digest does not verify")
        try:
            issued = datetime.fromisoformat(str(event.get("issued_at")).replace("Z", "+00:00"))
            completed = datetime.fromisoformat(str(event.get("completed_at")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceValidationError(f"{case_id}: operator confirmation timestamp is invalid") from exc
        if (
            completed < issued
            or (previous_completed is not None and issued < previous_completed)
            or abs((completed - issued).total_seconds() - confirmation_elapsed) > 10
        ):
            raise EvidenceValidationError(f"{case_id}: operator confirmation timing is inconsistent")
        challenges.add(challenge)
        counts[str(action)] += 1
        previous_elapsed = elapsed
        previous_completed = completed
    if any(counts[action] < minimum for action, minimum in A23_ACTION_REQUIREMENTS.items()):
        raise EvidenceValidationError(f"{case_id}: one or more endurance action counts are below plan minima")
    results = _a23_object(raw_payload.get("results"), case_id=case_id, field="results")
    declared_counts = _a23_object(
        results.get("action_counts"), case_id=case_id, field="results.action_counts"
    )
    if any(declared_counts.get(action) != counts[action] for action in A23_ACTION_REQUIREMENTS):
        raise EvidenceValidationError(f"{case_id}: declared action counts do not match the timeline")
    trend = results.get("failure_rate_trend")
    if (
        not isinstance(trend, list)
        or len(trend) != len(events)
        or any(
            not isinstance(item, dict)
            or item.get("sequence") != sequence
            or item.get("failures") != 0
            or item.get("failure_rate") != 0
            for sequence, item in enumerate(trend, 1)
        )
    ):
        raise EvidenceValidationError(f"{case_id}: failure-rate trend is missing or inconsistent")


def _validate_a23_resource_samples(
    raw_payload: dict[str, object], *, ownership: dict[str, object], case_id: str
) -> None:
    samples = raw_payload.get("resource_samples")
    if not isinstance(samples, list) or len(samples) < A23_MINIMUM_RESOURCE_SAMPLES:
        raise EvidenceValidationError(f"{case_id}: 30-minute resource trend has too few samples")
    first_time: datetime | None = None
    last_time: datetime | None = None
    previous_time: datetime | None = None
    desktop_pid = int(
        _a23_object(ownership.get("desktop"), case_id=case_id, field="ownership.desktop").get("pid") or 0
    )
    sidecar_pid = int(ownership.get("sidecar_pid") or 0)
    for sample_raw in samples:
        sample = _a23_object(sample_raw, case_id=case_id, field="resource_samples[]")
        try:
            recorded = datetime.fromisoformat(str(sample.get("recorded_at")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceValidationError(f"{case_id}: resource sample timestamp is invalid") from exc
        first_time = first_time or recorded
        last_time = recorded
        process = _a23_object(sample.get("process"), case_id=case_id, field="resource_samples[].process")
        gpu = _a23_object(sample.get("gpu"), case_id=case_id, field="resource_samples[].gpu")
        audio = _a23_object(sample.get("audio_devices"), case_id=case_id, field="resource_samples[].audio_devices")
        filesystem = _a23_object(sample.get("filesystem"), case_id=case_id, field="resource_samples[].filesystem")
        memory = _a23_object(sample.get("system_memory"), case_id=case_id, field="resource_samples[].system_memory")
        process_rows = process.get("processes")
        pids = {
            int(item.get("pid") or 0)
            for item in process_rows
            if isinstance(item, dict)
        } if isinstance(process_rows, list) else set()
        if (
            (previous_time is not None and (recorded <= previous_time or (recorded - previous_time).total_seconds() > 45))
            or
            sample.get("desktop_alive") is not True
            or sample.get("sidecar_alive") is not True
            or sample.get("api_healthy") is not True
            or int(process.get("process_count") or 0) < 2
            or desktop_pid not in pids
            or sidecar_pid not in pids
            or gpu.get("available") is not True
            or audio.get("available") is not True
            or int(audio.get("count") or 0) < 1
            or not isinstance(filesystem.get("temp"), dict)
            or not isinstance(filesystem.get("cache"), dict)
            or int(memory.get("total_bytes") or 0) <= 0
        ):
            raise EvidenceValidationError(f"{case_id}: a required resource sample is incomplete or unhealthy")
        previous_time = recorded
    if first_time is None or last_time is None or (last_time - first_time).total_seconds() < 1770:
        raise EvidenceValidationError(f"{case_id}: resource samples do not span the 30-minute run")


def _validate_a23_assessments(raw_payload: dict[str, object], *, case_id: str) -> None:
    corroboration = _a23_object(
        raw_payload.get("database_corroboration"), case_id=case_id, field="database_corroboration"
    )
    database_checks = _a23_object(
        corroboration.get("checks"), case_id=case_id, field="database_corroboration.checks"
    )
    if corroboration.get("passed") is not True or any(
        database_checks.get(name) is not True for name in A23_REQUIRED_DATABASE_CHECKS
    ):
        raise EvidenceValidationError(f"{case_id}: operator claims lack production-database corroboration")
    results = _a23_object(raw_payload.get("results"), case_id=case_id, field="results")
    resource = _a23_object(
        results.get("resource_assessment"), case_id=case_id, field="results.resource_assessment"
    )
    resource_checks = _a23_object(
        resource.get("checks"), case_id=case_id, field="results.resource_assessment.checks"
    )
    if resource.get("passed") is not True or any(
        resource_checks.get(name) is not True for name in A23_REQUIRED_RESOURCE_CHECKS
    ):
        raise EvidenceValidationError(f"{case_id}: resource leak/release assessment did not pass")
    cleanup = _a23_object(raw_payload.get("cleanup"), case_id=case_id, field="cleanup")
    if (
        cleanup.get("owned_processes_released") is not True
        or cleanup.get("runtime_removed") is not True
        or cleanup.get("external_processes_protected") is not True
        or cleanup.get("external_ollama_before") != cleanup.get("external_ollama_after")
        or cleanup.get("forced") is True
        or cleanup.get("remaining_owned_pids") not in ([], None)
    ):
        raise EvidenceValidationError(f"{case_id}: owned cleanup or external-process protection failed")


def _validate_a23_endurance_live(raw_payload: dict[str, object], *, case_id: str) -> None:
    duration = _a23_nonnegative_number(
        raw_payload.get("duration_seconds"), case_id=case_id, field="duration_seconds"
    )
    candidate = _a23_object(raw_payload.get("candidate"), case_id=case_id, field="candidate")
    health = _a23_object(candidate.get("health"), case_id=case_id, field="candidate.health")
    ownership = _a23_object(raw_payload.get("ownership"), case_id=case_id, field="ownership")
    raw_source = _a23_object(raw_payload.get("source"), case_id=case_id, field="source")
    runtime = _a23_object(raw_payload.get("runtime"), case_id=case_id, field="runtime")
    try:
        wall_duration = (
            datetime.fromisoformat(str(raw_payload.get("finished_at")).replace("Z", "+00:00"))
            - datetime.fromisoformat(str(raw_payload.get("recorded_at")).replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError as exc:
        raise EvidenceValidationError(f"{case_id}: run wall-clock timestamps are invalid") from exc
    if (
        duration < A23_MINIMUM_DURATION_SECONDS
        or wall_duration < 1770
        or raw_payload.get("interactive_operator") is not True
        or raw_payload.get("test_owned_runtime") is not True
        or runtime.get("isolated") is not True
        or runtime.get("retained") is not False
        or candidate.get("identity_verified") is not True
        or not isinstance(candidate.get("sha256"), str)
        or not re.fullmatch(r"[0-9A-F]{64}", str(candidate.get("sha256")))
        or health.get("version") != "14.0.0"
        or health.get("embedded") is not True
        or health.get("git_commit") != raw_source.get("source_commit")
        or health.get("source_fingerprint") != raw_source.get("source_tree_fingerprint")
        or not isinstance(health.get("build_id"), str)
        or not health.get("build_id")
        or not isinstance(health.get("component_build_id"), str)
        or not health.get("component_build_id")
        or ownership.get("verified") is not True
        or int(ownership.get("sidecar_parent_pid") or 0) != int(
            _a23_object(ownership.get("desktop"), case_id=case_id, field="ownership.desktop").get("pid") or -1
        )
        or raw_payload.get("sampler_errors") != []
    ):
        raise EvidenceValidationError(f"{case_id}: run duration, package identity, or PID ownership is invalid")
    _validate_a23_operator_events(raw_payload, case_id=case_id)
    _validate_a23_resource_samples(raw_payload, ownership=ownership, case_id=case_id)
    _validate_a23_assessments(raw_payload, case_id=case_id)


def _installer_artifact(
    raw_payload: dict[str, object], *, case_id: str, expected_suffix: str
) -> dict[str, object]:
    artifacts = _raw_object(raw_payload, "artifacts", case_id=case_id, field="artifacts")
    candidate = _raw_object(artifacts, "candidate", case_id=case_id, field="artifacts.candidate")
    name = candidate.get("name")
    digest = candidate.get("sha256")
    size = candidate.get("bytes")
    if (
        not isinstance(name, str)
        or not name.casefold().endswith(expected_suffix)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1024 * 1024
        or candidate.get("version") != TARGET_VERSION
    ):
        raise EvidenceValidationError(
            f"{case_id}: raw installer evidence has an invalid candidate artifact identity"
        )
    return candidate


def _installer_version(value: object, *, case_id: str, field: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise EvidenceValidationError(f"{case_id}: {field} is not a three-part installer version")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _validate_installer_build_manifest(
    value: object,
    *,
    case_id: str,
    field: str,
    expected_version: str,
    source: dict[str, object] | None = None,
) -> None:
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"{case_id}: installer evidence is missing {field}")
    build_id = value.get("build_id")
    if (
        value.get("product_version") != expected_version
        or value.get("workspace_state") != "CLEAN"
        or not isinstance(value.get("git_commit"), str)
        or not re.fullmatch(r"[0-9A-Fa-f]{40}", str(value.get("git_commit")))
        or not isinstance(value.get("source_fingerprint"), str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", str(value.get("source_fingerprint")))
        or not isinstance(build_id, str)
        or not build_id
        or value.get("component_build_id") != f"sidecar-{build_id}"
        or not isinstance(value.get("bytes"), int)
        or isinstance(value.get("bytes"), bool)
        or int(value.get("bytes")) <= 0
        or not isinstance(value.get("sha256"), str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", str(value.get("sha256")))
    ):
        raise EvidenceValidationError(f"{case_id}: {field} has an invalid embedded build identity")
    if source is not None and (
        value.get("git_commit") != source.get("source_commit")
        or value.get("source_fingerprint") != source.get("source_tree_fingerprint")
    ):
        raise EvidenceValidationError(
            f"{case_id}: candidate embedded build identity does not match the runner-bound source"
        )


def _validate_legacy_onefile_embedded_manifest(
    value: object,
    *,
    case_id: str,
    field: str,
    expected_version: str,
    previous_installer_sha256: object,
) -> None:
    """Validate a pre-v14 onefile sidecar's embedded archive manifest.

    v13's frozen backend was a PyInstaller onefile executable, so its manifest
    is inside the archive rather than adjacent to the installed sidecar.  The
    smoke script reads that one exact entry without executing or extracting the
    application, then binds it to the installed EXE and prior installer hash.
    This does not weaken the v14 candidate's onedir manifest requirement.
    """

    if not isinstance(value, dict):
        raise EvidenceValidationError(f"{case_id}: installer evidence is missing {field}")
    installer_sha256 = value.get("installer_sha256")
    if (
        value.get("identity_mode") != "legacy_onefile_embedded_manifest"
        or value.get("product_version") != expected_version
        or value.get("installer_version") != expected_version
        or value.get("workspace_state") != "CLEAN"
        or not isinstance(value.get("git_commit"), str)
        or not re.fullmatch(r"[0-9A-Fa-f]{40}", str(value.get("git_commit")))
        or not isinstance(value.get("source_fingerprint"), str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", str(value.get("source_fingerprint")))
        or not isinstance(value.get("build_id"), str)
        or not str(value.get("build_id"))
        or value.get("component_build_id") != f"sidecar-{value.get('build_id')}"
        or value.get("embedded_manifest_entry") != "build-info.json"
        or not isinstance(value.get("embedded_manifest_bytes"), int)
        or isinstance(value.get("embedded_manifest_bytes"), bool)
        or not 0 < int(value.get("embedded_manifest_bytes")) <= 128 * 1024
        or not isinstance(value.get("embedded_manifest_sha256"), str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", str(value.get("embedded_manifest_sha256")))
        or not isinstance(value.get("executable_name"), str)
        or not str(value.get("executable_name")).casefold().endswith(".exe")
        or not isinstance(value.get("executable_bytes"), int)
        or isinstance(value.get("executable_bytes"), bool)
        or int(value.get("executable_bytes")) <= 0
        or not isinstance(value.get("executable_sha256"), str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", str(value.get("executable_sha256")))
        or not isinstance(installer_sha256, str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", installer_sha256)
        or not isinstance(previous_installer_sha256, str)
        or installer_sha256.casefold() != previous_installer_sha256.casefold()
    ):
        raise EvidenceValidationError(
            f"{case_id}: {field} has an invalid legacy onefile embedded-manifest identity"
        )


def _validate_installer_upgrade_artifacts(
    raw_payload: dict[str, object],
    *,
    case_id: str,
    expected_suffix: str,
    allow_legacy_onefile_previous: bool = False,
) -> None:
    artifacts = _raw_object(raw_payload, "artifacts", case_id=case_id, field="artifacts")
    source = _raw_object(raw_payload, "source", case_id=case_id, field="source")
    candidate = _raw_object(artifacts, "candidate", case_id=case_id, field="artifacts.candidate")
    previous = _raw_object(artifacts, "previous", case_id=case_id, field="artifacts.previous")
    previous_name = previous.get("name")
    previous_digest = previous.get("sha256")
    previous_size = previous.get("bytes")
    previous_version = previous.get("version")
    previous_tuple = _installer_version(
        previous_version, case_id=case_id, field="artifacts.previous.version"
    )
    target_tuple = _installer_version(TARGET_VERSION, case_id=case_id, field="target_version")
    if (
        not isinstance(previous_name, str)
        or not previous_name.casefold().endswith(expected_suffix)
        or f"_{previous_version}_" not in previous_name
        or previous_tuple >= target_tuple
        or not isinstance(previous_digest, str)
        or not re.fullmatch(r"[0-9A-Fa-f]{64}", previous_digest)
        or previous_digest.casefold() == str(candidate.get("sha256")).casefold()
        or not isinstance(previous_size, int)
        or isinstance(previous_size, bool)
        or previous_size < 1024 * 1024
        or f"_{TARGET_VERSION}_" not in str(candidate.get("name"))
    ):
        raise EvidenceValidationError(
            f"{case_id}: previous installer is not a distinct, older artifact of the same package kind"
        )
    previous_identity = artifacts.get("previous_build_manifest")
    if (
        allow_legacy_onefile_previous
        and isinstance(previous_identity, dict)
        and previous_identity.get("identity_mode") == "legacy_onefile_embedded_manifest"
    ):
        _validate_legacy_onefile_embedded_manifest(
            previous_identity,
            case_id=case_id,
            field="artifacts.previous_build_manifest",
            expected_version=str(previous_version),
            previous_installer_sha256=previous_digest,
        )
    else:
        _validate_installer_build_manifest(
            previous_identity,
            case_id=case_id,
            field="artifacts.previous_build_manifest",
            expected_version=str(previous_version),
        )
    _validate_installer_build_manifest(
        artifacts.get("build_manifest"),
        case_id=case_id,
        field="artifacts.build_manifest",
        expected_version=TARGET_VERSION,
        source=source,
    )


def _validate_installer_run(
    raw_payload: dict[str, object], *, case_id: str, expected_kind: str, expected_elevation: str
) -> dict[str, object]:
    run = _raw_object(raw_payload, "run", case_id=case_id, field="run")
    if (
        run.get("installer_kind") != expected_kind
        or run.get("elevation") != expected_elevation
        or run.get("isolated_test_data") is not True
        or not isinstance(run.get("run_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", str(run.get("run_id")))
    ):
        raise EvidenceValidationError(
            f"{case_id}: raw installer evidence is not bound to the required elevation and isolated run"
        )
    started = parse_recorded_at(run.get("started_at"), field=f"{case_id}.run.started_at")
    finished = parse_recorded_at(run.get("finished_at"), field=f"{case_id}.run.finished_at")
    if finished < started:
        raise EvidenceValidationError(f"{case_id}: installer run timestamps are inconsistent")
    return run


def _validate_a27_nsis_installer_live(raw_payload: dict[str, object], *, case_id: str) -> None:
    _validate_installer_run(
        raw_payload,
        case_id=case_id,
        expected_kind="NSIS",
        expected_elevation="NON_ADMINISTRATOR",
    )
    _installer_artifact(raw_payload, case_id=case_id, expected_suffix="-setup.exe")
    _validate_installer_upgrade_artifacts(
        raw_payload,
        case_id=case_id,
        expected_suffix="-setup.exe",
        allow_legacy_onefile_previous=True,
    )
    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    if (
        results.get("status") != "ok"
        or results.get("version") != TARGET_VERSION
        or results.get("previous_version_upgrade") is not True
        or results.get("schema_migrated") is not True
        or results.get("in_place_upgrade_preserved_data") is not True
        or results.get("uninstall_preserved_data") is not True
        or results.get("reinstall_started") is not True
        or results.get("reinstall_recognized_data") is not True
        or results.get("final_uninstall") is not True
        or not isinstance(results.get("migration_backup"), str)
        or not str(results.get("migration_backup")).strip()
    ):
        raise EvidenceValidationError(
            f"{case_id}: NSIS raw evidence does not prove upgrade, migration, uninstall and reinstall"
        )


def _validate_a28_msi_installer_live(
    raw_payload: dict[str, object],
    *,
    case_id: str,
    repository_root: Path,
    evidence_root: Path,
) -> None:
    _validate_installer_run(
        raw_payload,
        case_id=case_id,
        expected_kind="MSI",
        expected_elevation="ADMINISTRATOR",
    )
    _installer_artifact(raw_payload, case_id=case_id, expected_suffix=".msi")
    _validate_installer_upgrade_artifacts(raw_payload, case_id=case_id, expected_suffix=".msi")
    acceptance = _raw_object(
        raw_payload, "operator_acceptance", case_id=case_id, field="operator_acceptance"
    )
    observations = acceptance.get("observations")
    required_observations = {
        "microphone_permission_grant",
        "microphone_permission_denial",
        "local_stt_transcription",
        "windows_tts_playback",
        "ollama_detection",
    }
    if (
        acceptance.get("interactive") is not True
        or acceptance.get("all_confirmed") is not True
        or not isinstance(acceptance.get("operator"), str)
        or not str(acceptance.get("operator")).strip()
        or not isinstance(observations, list)
    ):
        raise EvidenceValidationError(
            f"{case_id}: MSI evidence lacks interactive installed-desktop acceptance"
        )
    observed: set[str] = set()
    for item in observations:
        if not isinstance(item, dict):
            raise EvidenceValidationError(f"{case_id}: MSI operator observations must be objects")
        name = item.get("name")
        if (
            not isinstance(name, str)
            or name not in required_observations
            or name in observed
            or item.get("confirmed") is not True
            or not isinstance(item.get("challenge_sha256"), str)
            or not re.fullmatch(r"[0-9A-F]{64}", str(item.get("challenge_sha256")))
        ):
            raise EvidenceValidationError(
                f"{case_id}: MSI operator observation is missing, duplicated or not challenge-confirmed"
            )
        parse_recorded_at(item.get("confirmed_at"), field=f"{case_id}.operator.{name}.confirmed_at")
        observed.add(name)
    if observed != required_observations:
        raise EvidenceValidationError(f"{case_id}: MSI operator observation set is incomplete")
    results = _raw_object(raw_payload, "results", case_id=case_id, field="results")
    required_true = (
        "install",
        "desktop_started",
        "sidecar_stopped",
        "previous_version_upgrade",
        "schema_migrated",
        "in_place_upgrade_preserved_data",
        "uninstall",
        "uninstall_preserved_data",
        "uninstall_preserved_models",
        "reinstall",
        "reinstall_recognized_data",
        "final_uninstall",
        "package_files_removed",
    )
    if (
        results.get("status") != "ok"
        or results.get("version") != TARGET_VERSION
        or any(results.get(name) is not True for name in required_true)
        or not isinstance(results.get("migration_backup"), str)
        or not str(results.get("migration_backup")).strip()
    ):
        raise EvidenceValidationError(
            f"{case_id}: MSI raw evidence does not prove the full administrator lifecycle"
        )
    logs = raw_payload.get("logs")
    if not isinstance(logs, list) or len(logs) < 3:
        raise EvidenceValidationError(f"{case_id}: MSI evidence must retain install, upgrade and uninstall logs")
    log_labels = set()
    for item in logs:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not str(item.get("path")).startswith("build/v1400-evidence/")
            or not str(item.get("path")).endswith(".log")
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9A-F]{64}", str(item.get("sha256")))
            or not isinstance(item.get("bytes"), int)
            or isinstance(item.get("bytes"), bool)
            or int(item.get("bytes")) <= 0
        ):
            raise EvidenceValidationError(f"{case_id}: MSI log attachment identity is invalid")
        path = str(item["path"])
        log_path = resolve_repository_path(
            repository_root, path, field=f"{case_id}.logs.path"
        )
        try:
            log_path.resolve().relative_to(evidence_root.resolve())
        except ValueError as exc:
            raise EvidenceValidationError(
                f"{case_id}: MSI logs must stay under the current evidence root"
            ) from exc
        if (
            log_path.is_symlink()
            or not log_path.is_file()
            or log_path.stat().st_size != item["bytes"]
            or sha256(log_path) != item["sha256"]
        ):
            raise EvidenceValidationError(
                f"{case_id}: MSI log attachment no longer matches its recorded hash"
            )
        if "msi-install-" in path:
            log_labels.add("install")
        elif "msi-upgrade-" in path:
            log_labels.add("upgrade")
        elif "msi-uninstall-" in path:
            log_labels.add("uninstall")
    if log_labels != {"install", "upgrade", "uninstall"}:
        raise EvidenceValidationError(f"{case_id}: MSI lifecycle log set is incomplete")


def _validate_case_specific_attested_payload(
    raw_payload: dict[str, object],
    *,
    case_id: str,
    policy: dict[str, object],
    repository_root: Path,
    evidence_root: Path,
) -> None:
    required_checks = policy.get("required_checks")
    if required_checks is not None:
        _validate_required_attested_checks(
            raw_payload, case_id=case_id, required_checks=required_checks
        )
    if case_id == "A02":
        _validate_a02_windows_tts_live(raw_payload, case_id=case_id)
    elif case_id in DESKTOP_VOICE_CASE_IDS:
        _validate_desktop_voice_live(raw_payload, case_id=case_id)
    elif case_id == "A08":
        _validate_a08_accuracy_review(raw_payload, case_id=case_id)
    elif case_id == "A09":
        scope = _raw_object(raw_payload, "scope", case_id=case_id, field="scope")
        if scope.get("model_download") == "CALLED":
            _validate_a09_download_flow(raw_payload, case_id=case_id)
        elif scope.get("model_download") == "VERIFIED_PRIOR_ACTUAL":
            _validate_a09_verified_prior_actual(
                raw_payload,
                case_id=case_id,
                repository_root=repository_root,
                evidence_root=evidence_root,
            )
        else:
            raise EvidenceValidationError(
                f"{case_id}: raw evidence must be an actual download or a verified prior actual-download receipt"
            )
    elif case_id == "A20":
        _validate_a20_resource_live(raw_payload, case_id=case_id)
    elif case_id == "A23":
        _validate_a23_endurance_live(raw_payload, case_id=case_id)
    elif case_id == "A26":
        _validate_a26_ollama_live(raw_payload, case_id=case_id)
    elif case_id == "A27":
        _validate_a27_nsis_installer_live(raw_payload, case_id=case_id)
    elif case_id == "A28":
        _validate_a28_msi_installer_live(
            raw_payload,
            case_id=case_id,
            repository_root=repository_root,
            evidence_root=evidence_root,
        )


def _requested_attested_outputs(
    entry: dict[str, object],
    *,
    case_id: str,
    required: bool,
) -> list[str]:
    requested_raw = entry.get("attested_outputs")
    if requested_raw is None:
        if required:
            raise EvidenceValidationError(
                f"{case_id}: live/performance PASS requires attested_outputs bound by the runner"
            )
        return []
    requested = string_list(requested_raw, field=f"{case_id}.evidence.attested_outputs")
    if len(requested) != len(set(requested)):
        raise EvidenceValidationError(f"{case_id}: evidence.attested_outputs must not repeat a path")
    if required and not requested:
        raise EvidenceValidationError(
            f"{case_id}: live/performance PASS requires at least one attested raw output"
        )
    return requested


def _collect_attested_outputs(
    envelope: dict[str, object],
    *,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
    case_id: str,
    source: dict[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    envelope_raw = envelope.get("attested_outputs")
    if not isinstance(envelope_raw, list) or not envelope_raw:
        raise EvidenceValidationError(
            f"{case_id}: execution envelope is missing its runner-bound attested_outputs"
        )
    attachments: dict[str, dict[str, object]] = {}
    raw_payloads: dict[str, dict[str, object]] = {}
    for raw_attachment in envelope_raw:
        if not isinstance(raw_attachment, dict):
            raise EvidenceValidationError(f"{case_id}: execution envelope attested_outputs must contain objects")
        raw_path, relative = _attested_path(
            repository_root=repository_root,
            evidence_root=evidence_root,
            case_id=case_id,
            value=raw_attachment.get("path"),
        )
        if relative in attachments:
            raise EvidenceValidationError(f"{case_id}: execution envelope repeats an attested raw output")
        required_fields = {
            "sha256": str,
            "bytes": int,
            "status": str,
            "actual_run": bool,
            "target_version": str,
            "source_version": str,
            "source_commit": str,
            "source_tree_fingerprint": str,
            "workspace_clean": bool,
            "source_identity_mode": str,
            "report_type": str,
        }
        for field, expected_type in required_fields.items():
            # bool is a subclass of int; bytes must be a real non-negative integer.
            value = raw_attachment.get(field)
            if not isinstance(value, expected_type) or (
                field == "bytes" and (isinstance(value, bool) or value < 0)
            ):
                raise EvidenceValidationError(
                    f"{case_id}: runner-bound raw attachment has invalid {field}"
                )
        if raw_attachment["target_version"] != target_version:
            raise EvidenceValidationError(f"{case_id}: raw attachment target_version does not match")
        for field in ("source_version", "source_commit", "source_tree_fingerprint", "workspace_clean"):
            if raw_attachment[field] != source[field]:
                raise EvidenceValidationError(
                    f"{case_id}: raw attachment {field} does not match the current source identity"
                )
        if raw_attachment["source_identity_mode"] not in {"raw_report", "runner_verified_equivalent"}:
            raise EvidenceValidationError(f"{case_id}: raw attachment has an unknown source_identity_mode")
        if raw_attachment["status"] != "PASS" or raw_attachment["actual_run"] is not True:
            raise EvidenceValidationError(
                f"{case_id}: PASS cannot be based on a failing or not-run raw attachment"
            )

        try:
            raw_bytes = raw_path.read_bytes()
            raw_payload = json.loads(raw_bytes.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceValidationError(
                f"{case_id}: attested raw output is unreadable JSON"
            ) from exc
        if not isinstance(raw_payload, dict):
            raise EvidenceValidationError(f"{case_id}: attested raw output must be a JSON object")
        if len(raw_bytes) != raw_attachment["bytes"] or sha256(raw_path) != raw_attachment["sha256"]:
            raise EvidenceValidationError(
                f"{case_id}: attested raw output no longer matches the runner-bound SHA-256/bytes"
            )
        for field in ("status", "actual_run", "target_version", "report_type"):
            if raw_payload.get(field) != raw_attachment[field]:
                raise EvidenceValidationError(
                    f"{case_id}: attested raw output {field} no longer matches its envelope binding"
                )
        producer = raw_attachment.get("producer")
        if producer is not None and raw_payload.get("producer") != producer:
            raise EvidenceValidationError(
                f"{case_id}: attested raw output producer no longer matches its envelope binding"
            )
        if raw_attachment["source_identity_mode"] == "raw_report":
            raw_source = raw_payload.get("source")
            if not isinstance(raw_source, dict):
                raise EvidenceValidationError(f"{case_id}: raw attachment is missing its source identity")
            for field in ("source_version", "source_commit", "source_tree_fingerprint", "workspace_clean"):
                if raw_source.get(field) != source[field]:
                    raise EvidenceValidationError(
                        f"{case_id}: raw attachment source {field} does not match the current source identity"
                    )
        attachments[relative] = {
            **raw_attachment,
            "path": relative,
            "sha256": raw_attachment["sha256"],
            "bytes": raw_attachment["bytes"],
        }
        raw_payloads[relative] = raw_payload
    return attachments, raw_payloads


def _validate_attested_policy(
    requested: list[str],
    attachments: dict[str, dict[str, object]],
    raw_payloads: dict[str, dict[str, object]],
    *,
    envelope: dict[str, object],
    repository_root: Path,
    evidence_root: Path,
    case_id: str,
) -> None:
    if set(requested) != set(attachments):
        raise EvidenceValidationError(
            f"{case_id}: evidence.attested_outputs must exactly match the envelope raw attachments"
        )
    policy = ATTESTED_CASE_POLICIES.get(case_id)
    if policy is not None:
        expected_type = policy["report_type"]
        expected_command_path = policy["command_path"]
        assert isinstance(expected_type, str) and isinstance(expected_command_path, str)
        command_contract = envelope.get("command_contract")
        command_paths = command_contract.get("paths") if isinstance(command_contract, dict) else None
        if not isinstance(command_paths, list) or expected_command_path not in command_paths:
            raise EvidenceValidationError(
                f"{case_id}: live/performance envelope must execute {expected_command_path}"
            )
        if any(item["report_type"] != expected_type for item in attachments.values()):
            raise EvidenceValidationError(
                f"{case_id}: live/performance raw attachment must use report_type={expected_type}"
            )
        if policy.get("raw_source") is True and any(
            item["source_identity_mode"] != "raw_report" for item in attachments.values()
        ):
            raise EvidenceValidationError(
                f"{case_id}: live raw report must record its own source identity"
            )
        for relative in requested:
            _validate_case_specific_attested_payload(
                raw_payloads[relative],
                case_id=case_id,
                policy=policy,
                repository_root=repository_root,
                evidence_root=evidence_root,
            )


def validate_attested_outputs(
    entry: dict[str, object],
    envelope: dict[str, object],
    *,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
    case_id: str,
    source: dict[str, object],
    required: bool,
) -> list[dict[str, object]]:
    """Verify a ledger's exact raw-report attachments against its envelope.

    The runner makes each raw report immutable-at-create and records its hash.
    This second verification deliberately re-reads the report when a PASS is
    promoted so a later replacement, a manually added path, or an unrelated
    raw report cannot be smuggled through a syntactically valid envelope.
    """

    requested = _requested_attested_outputs(entry, case_id=case_id, required=required)
    if entry.get("attested_outputs") is None:
        return []
    attachments, raw_payloads = _collect_attested_outputs(
        envelope,
        repository_root=repository_root,
        evidence_root=evidence_root,
        target_version=target_version,
        case_id=case_id,
        source=source,
    )
    _validate_attested_policy(
        requested,
        attachments,
        raw_payloads,
        envelope=envelope,
        repository_root=repository_root,
        evidence_root=evidence_root,
        case_id=case_id,
    )
    return [attachments[path] for path in requested]


def _read_a08_manual_review_payload(
    *,
    artifact: Path,
    command: str,
    recorded_at: str,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
) -> tuple[dict[str, object], str]:
    """Validate a separately authored semantic review of the A08 raw run.

    The STT live runner deliberately emits ``decision=NOT_AUTOMATED``.  A08 can
    therefore be promoted only when a reviewer has inspected every reference
    and observed transcript and recorded a second, independently hashed JSON
    artifact.  This artifact is not a runner envelope and must not pretend that
    an automatic character comparison made the acceptance decision.
    """

    reviews_root = evidence_root / "reviews"
    try:
        artifact.relative_to(reviews_root.resolve())
    except ValueError as exc:
        raise EvidenceValidationError(
            "A08: manual accuracy review must be stored under "
            f"{repository_relative(repository_root, reviews_root)}"
        ) from exc
    if artifact.is_symlink() or not artifact.is_file():
        raise EvidenceValidationError(
            "A08: manual accuracy review must be an existing regular file"
        )

    payload = read_json(artifact)
    if (
        payload.get("schema_version") != A08_MANUAL_REVIEW_SCHEMA_VERSION
        or payload.get("report_type") != A08_MANUAL_REVIEW_REPORT_TYPE
        or payload.get("producer") != A08_MANUAL_REVIEW_PRODUCER
        or payload.get("target_version") != target_version
        or payload.get("case_id") != "A08"
        or payload.get("actual_review") is not True
        or payload.get("review_mode") != "manual_semantic_inspection"
        or payload.get("automated_decision") is not False
    ):
        raise EvidenceValidationError(
            "A08: manual accuracy review has an invalid schema, identity, "
            "or manual-review declaration"
        )
    decision = payload.get("decision")
    if decision not in A08_MANUAL_REVIEW_DECISIONS:
        allowed = ", ".join(sorted(A08_MANUAL_REVIEW_DECISIONS))
        raise EvidenceValidationError(
            f"A08: manual accuracy review decision must be one of {allowed}"
        )
    if payload.get("procedure") != command:
        raise EvidenceValidationError(
            "A08: manual accuracy review procedure must exactly match the ledger procedure"
        )
    payload_recorded_at = parse_recorded_at(
        payload.get("recorded_at"), field="A08.manual_review.recorded_at"
    )
    if payload_recorded_at != recorded_at:
        raise EvidenceValidationError(
            "A08: manual accuracy review recorded_at must exactly match the ledger"
        )
    assert isinstance(decision, str)
    return payload, decision


def _a08_manual_reviewer_source(
    payload: dict[str, object], *, source: dict[str, object]
) -> tuple[str, str]:
    reviewer = _raw_object(payload, "reviewer", case_id="A08", field="reviewer")
    reviewer_identity = reviewer.get("identity")
    reviewer_role = reviewer.get("role")
    if (
        not isinstance(reviewer_identity, str)
        or not reviewer_identity.strip()
        or not isinstance(reviewer_role, str)
        or not reviewer_role.strip()
    ):
        raise EvidenceValidationError(
            "A08: manual accuracy review must identify its reviewer and role"
        )

    review_source = _raw_object(payload, "source", case_id="A08", field="source")
    for field in ("source_version", "source_commit", "source_tree_fingerprint", "workspace_clean"):
        if review_source.get(field) != source.get(field):
            raise EvidenceValidationError(
                f"A08: manual accuracy review source {field} does not match "
                "the current source identity"
            )
    assert isinstance(reviewer_identity, str) and isinstance(reviewer_role, str)
    return reviewer_identity, reviewer_role


def _a08_manual_source_raw(
    payload: dict[str, object],
    *,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
    source: dict[str, object],
) -> tuple[str, str, int, list[object], dict[str, object]]:
    source_raw = _raw_object(payload, "source_raw", case_id="A08", field="source_raw")
    raw_path, raw_relative = _attested_path(
        repository_root=repository_root,
        evidence_root=evidence_root,
        case_id="A08",
        value=source_raw.get("path"),
    )
    expected_raw_prefix = f"{repository_relative(repository_root, evidence_root)}/raw/"
    if not raw_relative.startswith(expected_raw_prefix):
        raise EvidenceValidationError(
            "A08: manual accuracy review source_raw must be under the raw evidence directory"
        )
    raw_sha256 = source_raw.get("sha256")
    raw_bytes = source_raw.get("bytes")
    if (
        not isinstance(raw_sha256, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", raw_sha256) is None
        or not isinstance(raw_bytes, int)
        or isinstance(raw_bytes, bool)
        or raw_bytes <= 0
    ):
        raise EvidenceValidationError(
            "A08: manual accuracy review source_raw needs valid SHA-256 and byte length"
        )
    if sha256(raw_path) != raw_sha256.upper() or raw_path.stat().st_size != raw_bytes:
        raise EvidenceValidationError(
            "A08: manual accuracy review source_raw no longer matches its SHA-256/bytes"
        )
    raw_payload = read_json(raw_path)
    if (
        raw_payload.get("report_type") != ATTESTED_CASE_POLICIES["A08"]["report_type"]
        or raw_payload.get("target_version") != target_version
        or raw_payload.get("status") != "PASS"
        or raw_payload.get("actual_run") is not True
    ):
        raise EvidenceValidationError(
            "A08: manual accuracy review must reference a successful v14 STT live raw report"
        )
    raw_source = _raw_object(raw_payload, "source", case_id="A08", field="source_raw.source")
    for field in ("source_version", "source_commit", "source_tree_fingerprint", "workspace_clean"):
        if raw_source.get(field) != source.get(field):
            raise EvidenceValidationError(
                f"A08: manual accuracy review raw source {field} does not match "
                "the current source identity"
            )
    _validate_a08_accuracy_review(raw_payload, case_id="A08")
    raw_results = _raw_object(raw_payload, "results", case_id="A08", field="source_raw.results")
    raw_review = _raw_object(
        raw_results,
        "accuracy_review",
        case_id="A08",
        field="source_raw.results.accuracy_review",
    )
    raw_categories = raw_review.get("categories_exercised")
    raw_samples = raw_review.get("samples")
    assert isinstance(raw_categories, list) and isinstance(raw_samples, dict)
    assert isinstance(raw_sha256, str) and isinstance(raw_bytes, int)
    return raw_relative, raw_sha256, raw_bytes, raw_categories, raw_samples


def _a08_manual_sample_reviews(
    payload: dict[str, object],
    *,
    raw_categories: list[object],
    raw_samples: dict[str, object],
) -> tuple[list[str], dict[str, object], bool]:
    review = _raw_object(payload, "review", case_id="A08", field="review")
    categories_reviewed = string_list(
        review.get("categories_reviewed"), field="A08.manual_review.categories_reviewed"
    )
    if (
        set(categories_reviewed) != set(raw_categories)
        or A08_REQUIRED_ACCURACY_CATEGORIES.difference(categories_reviewed)
    ):
        raise EvidenceValidationError(
            "A08: manual accuracy review must cover every fixed-corpus category in source_raw"
        )
    sample_reviews = review.get("sample_reviews")
    if not isinstance(sample_reviews, dict) or set(sample_reviews) != set(raw_samples):
        raise EvidenceValidationError(
            "A08: manual accuracy review must cover every source_raw reference/observed sample"
        )
    sample_has_warning = False
    for sample_name, sample_review in sample_reviews.items():
        if not isinstance(sample_review, dict):
            raise EvidenceValidationError(
                f"A08: manual review for sample {sample_name!r} is invalid"
            )
        assessment = sample_review.get("assessment")
        notes = sample_review.get("notes")
        if assessment not in {"PASS", "WARNING"}:
            raise EvidenceValidationError(
                f"A08: manual review for sample {sample_name!r} must be PASS or WARNING"
            )
        if (
            sample_review.get("reference_text_reviewed") is not True
            or sample_review.get("observed_text_reviewed") is not True
            or not isinstance(notes, str)
            or not notes.strip()
        ):
            raise EvidenceValidationError(
                f"A08: manual review for sample {sample_name!r} must confirm "
                "both texts and record notes"
            )
        sample_has_warning = sample_has_warning or assessment == "WARNING"
    return categories_reviewed, sample_reviews, sample_has_warning


def _validate_a08_manual_acceptance(payload: dict[str, object]) -> None:
    acceptance = _raw_object(payload, "acceptance", case_id="A08", field="acceptance")
    if (
        acceptance.get("local_chinese_actual_inference") != "PASS"
        or acceptance.get("microphone_capture") != "NOT_RUN"
        or acceptance.get("input_audio_scope") != "SYNTHETIC_NON_MICROPHONE"
    ):
        raise EvidenceValidationError(
            "A08: manual review may pass only local Chinese actual inference and "
            "must not claim microphone capture"
        )


def _a08_manual_warning_boundaries(
    payload: dict[str, object],
) -> tuple[dict[str, dict[str, object]], bool]:
    warning_boundaries = _raw_object(
        payload, "warning_boundaries", case_id="A08", field="warning_boundaries"
    )
    if set(warning_boundaries) != set(A08_MANUAL_REVIEW_BOUNDARIES):
        raise EvidenceValidationError(
            "A08: manual review must explicitly bound terminology, mixed-language, "
            "and hallucination/repetition claims"
        )
    boundary_has_warning = False
    normalized_boundaries: dict[str, dict[str, object]] = {}
    for boundary_name in sorted(A08_MANUAL_REVIEW_BOUNDARIES):
        boundary = warning_boundaries[boundary_name]
        if not isinstance(boundary, dict):
            raise EvidenceValidationError(
                f"A08: warning boundary {boundary_name!r} must be an object"
            )
        assessment = boundary.get("assessment")
        release_claim = boundary.get("release_claim")
        observations = string_list(
            boundary.get("observations"),
            field=f"A08.manual_review.warning_boundaries.{boundary_name}.observations",
        )
        if assessment not in A08_MANUAL_REVIEW_ASSESSMENTS:
            raise EvidenceValidationError(
                f"A08: warning boundary {boundary_name!r} has an invalid assessment"
            )
        if not isinstance(release_claim, str) or not release_claim.strip() or not observations:
            raise EvidenceValidationError(
                f"A08: warning boundary {boundary_name!r} needs observations and "
                "an explicit release claim"
            )
        boundary_has_warning = boundary_has_warning or assessment != "PASS"
        normalized_boundaries[boundary_name] = {
            "assessment": assessment,
            "observations": observations,
            "release_claim": release_claim.strip(),
        }
    return normalized_boundaries, boundary_has_warning


def _validate_a08_manual_decision(
    decision: str, *, sample_has_warning: bool, boundary_has_warning: bool
) -> None:
    has_warning = sample_has_warning or boundary_has_warning
    if decision == "PASS" and has_warning:
        raise EvidenceValidationError(
            "A08: decision PASS cannot conceal sample or claim-boundary warnings"
        )
    if decision == "PASS_WITH_WARNING" and not has_warning:
        raise EvidenceValidationError(
            "A08: PASS_WITH_WARNING requires at least one explicit warning boundary"
        )


def _normalize_a08_manual_review_evidence(
    entry: dict[str, object],
    *,
    artifact: Path,
    command: str,
    recorded_at: str,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
    source: dict[str, object],
) -> dict[str, object]:
    payload, decision = _read_a08_manual_review_payload(
        artifact=artifact,
        command=command,
        recorded_at=recorded_at,
        repository_root=repository_root,
        evidence_root=evidence_root,
        target_version=target_version,
    )
    reviewer_identity, reviewer_role = _a08_manual_reviewer_source(payload, source=source)
    raw_relative, raw_sha256, raw_bytes, raw_categories, raw_samples = (
        _a08_manual_source_raw(
            payload,
            repository_root=repository_root,
            evidence_root=evidence_root,
            target_version=target_version,
            source=source,
        )
    )
    categories_reviewed, sample_reviews, sample_has_warning = _a08_manual_sample_reviews(
        payload, raw_categories=raw_categories, raw_samples=raw_samples
    )
    _validate_a08_manual_acceptance(payload)
    normalized_boundaries, boundary_has_warning = _a08_manual_warning_boundaries(payload)
    _validate_a08_manual_decision(
        decision,
        sample_has_warning=sample_has_warning,
        boundary_has_warning=boundary_has_warning,
    )
    normalized: dict[str, object] = {
        "path": repository_relative(repository_root, artifact),
        "sha256": sha256(artifact),
        "kind": "manual",
        "actual_run": True,
        "outcome": "PASS",
        "command": command,
        "recorded_at": recorded_at,
        "decision": decision,
        "reviewer": {
            "identity": reviewer_identity.strip(),
            "role": reviewer_role.strip(),
        },
        "source_raw": {
            "path": raw_relative,
            "sha256": raw_sha256.upper(),
            "bytes": raw_bytes,
        },
        "categories_reviewed": sorted(set(categories_reviewed)),
        "reviewed_samples": sorted(sample_reviews),
        "warning_boundaries": normalized_boundaries,
    }
    for optional in ("scope", "operator", "notes"):
        value = entry.get(optional)
        if isinstance(value, str) and value.strip():
            normalized[optional] = value.strip()
    return normalized


def _validate_a08_pass_evidence_set(evidence: list[dict[str, object]]) -> None:
    actual_raw_paths = {
        attachment["path"]
        for item in evidence
        if item.get("kind") != "manual"
        for attachment in item.get("attested_outputs", [])
        if isinstance(attachment, dict) and isinstance(attachment.get("path"), str)
    }
    if not actual_raw_paths:
        raise EvidenceValidationError(
            "A08: PASS requires runner-attested STT live execution evidence in "
            "addition to manual review"
        )
    manual_reviews = [item for item in evidence if item.get("kind") == "manual"]
    if not manual_reviews:
        raise EvidenceValidationError(
            "A08: PASS requires independent structured manual accuracy review evidence"
        )
    for review in manual_reviews:
        source_raw = review.get("source_raw")
        source_raw_path = source_raw.get("path") if isinstance(source_raw, dict) else None
        if source_raw_path not in actual_raw_paths:
            raise EvidenceValidationError(
                "A08: manual accuracy review source_raw is not the runner-attested STT raw evidence"
            )


def normalize_actual_evidence(
    entry: object,
    *,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
    case_id: str,
    source: dict[str, object],
    require_attestation: bool = True,
) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise EvidenceValidationError(f"{case_id}: PASS evidence must be an object")
    evidence_type = entry.get("kind")
    if evidence_type not in ACTUAL_EVIDENCE_KINDS:
        allowed = ", ".join(sorted(ACTUAL_EVIDENCE_KINDS))
        raise EvidenceValidationError(f"{case_id}: PASS evidence kind must be one of {allowed}")
    if entry.get("actual_run") is not True:
        raise EvidenceValidationError(f"{case_id}: PASS evidence must set actual_run=true")
    if entry.get("outcome") != "PASS":
        raise EvidenceValidationError(f"{case_id}: PASS evidence must set outcome=PASS")
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        raise EvidenceValidationError(f"{case_id}: PASS evidence needs the real command or manual procedure")
    recorded_at = parse_recorded_at(entry.get("recorded_at"), field=f"{case_id}.evidence.recorded_at")
    artifact = resolve_repository_path(repository_root, entry.get("path"), field=f"{case_id}.evidence.path")
    if not artifact.is_file():
        raise EvidenceValidationError(f"{case_id}: PASS evidence artifact does not exist: {artifact}")
    try:
        artifact.relative_to(evidence_root.resolve())
    except ValueError as exc:
        raise EvidenceValidationError(
            f"{case_id}: PASS evidence must be collected under {repository_relative(repository_root, evidence_root)}"
        ) from exc
    if case_id == "A08" and evidence_type == "manual":
        if entry.get("attested_outputs") is not None:
            raise EvidenceValidationError(
                "A08: manual accuracy review must bind source_raw inside its review JSON, "
                "not attested_outputs"
            )
        return _normalize_a08_manual_review_evidence(
            entry,
            artifact=artifact,
            command=command.strip(),
            recorded_at=recorded_at,
            repository_root=repository_root,
            evidence_root=evidence_root,
            target_version=target_version,
            source=source,
        )
    envelope = validate_pass_report(
        artifact,
        target_version=target_version,
        case_id=case_id,
        command=command.strip(),
        recorded_at=recorded_at,
        source=source,
        repository_root=repository_root,
    )
    attested_outputs = validate_attested_outputs(
        entry,
        envelope,
        repository_root=repository_root,
        evidence_root=evidence_root,
        target_version=target_version,
        case_id=case_id,
        source=source,
        required=require_attestation and case_id in ATTESTED_CASE_POLICIES,
    )

    normalized: dict[str, object] = {
        "path": repository_relative(repository_root, artifact),
        "sha256": sha256(artifact),
        "kind": evidence_type,
        "actual_run": True,
        "outcome": "PASS",
        "command": command.strip(),
        "recorded_at": recorded_at,
    }
    for optional in ("scope", "operator", "notes"):
        value = entry.get(optional)
        if isinstance(value, str) and value.strip():
            normalized[optional] = value.strip()
    if attested_outputs:
        normalized["attested_outputs"] = attested_outputs
    return normalized


def string_list(value: object, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise EvidenceValidationError(f"{field} must be a list of non-empty strings")
    return [item.strip() for item in value]


def normalize_cases(
    ledger: dict[str, Any],
    *,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
    source: dict[str, object],
) -> list[dict[str, object]]:
    raw_cases = ledger.get("cases")
    if not isinstance(raw_cases, list):
        raise EvidenceValidationError("cases must be a list")
    raw_by_id: dict[str, dict[str, Any]] = {}
    for case in raw_cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise EvidenceValidationError("each case must be an object with id")
        case_id = case["id"]
        if case_id in raw_by_id:
            raise EvidenceValidationError(f"duplicate case id: {case_id}")
        raw_by_id[case_id] = case
    expected_ids = {case_id for case_id, _ in CASE_CATALOG}
    found_ids = set(raw_by_id)
    missing = [case_id for case_id, _ in CASE_CATALOG if case_id not in found_ids]
    extra = sorted(found_ids - expected_ids)
    if missing or extra:
        raise EvidenceValidationError(
            f"v14 matrix must contain exactly A01-A28; missing={missing}, extra={extra}"
        )

    normalized_cases: list[dict[str, object]] = []
    for case_id, case_name in CASE_CATALOG:
        raw = raw_by_id[case_id]
        status = raw.get("status")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise EvidenceValidationError(f"{case_id}: invalid status {status!r}")
        reason = raw.get("reason", "")
        if not isinstance(reason, str):
            raise EvidenceValidationError(f"{case_id}: reason must be a string")
        reason = reason.strip()
        raw_evidence = raw.get("evidence", [])
        if not isinstance(raw_evidence, list):
            raise EvidenceValidationError(f"{case_id}: evidence must be a list")
        if status == "PASS":
            if not raw_evidence:
                raise EvidenceValidationError(f"{case_id}: PASS requires at least one actual execution evidence artifact")
            evidence = [
                normalize_actual_evidence(
                    entry,
                    repository_root=repository_root,
                    evidence_root=evidence_root,
                    target_version=target_version,
                    case_id=case_id,
                    source=source,
                )
                for entry in raw_evidence
            ]
            if case_id == "A08":
                _validate_a08_pass_evidence_set(evidence)
        else:
            if not reason:
                raise EvidenceValidationError(f"{case_id}: {status} requires a truthful reason")
            # Supporting references can document why work is blocked/not run, but
            # are intentionally not accepted as PASS evidence.
            evidence = raw_evidence
        normalized: dict[str, object] = {
            "id": case_id,
            "name": case_name,
            "status": status,
            "evidence": evidence,
        }
        if reason:
            normalized["reason"] = reason
        result = raw.get("result")
        if isinstance(result, str) and result.strip():
            normalized["result"] = result.strip()
        source_references = string_list(raw.get("source_references"), field=f"{case_id}.source_references")
        if source_references:
            normalized["source_references"] = source_references
        normalized_cases.append(normalized)
    return normalized_cases


def case_has_verified_execution(case: dict[str, object]) -> bool:
    evidence = case.get("evidence")
    return (
        case.get("status") == "PASS"
        and isinstance(evidence, list)
        and any(
            isinstance(item, dict)
            and item.get("actual_run") is True
            and item.get("outcome") == "PASS"
            and isinstance(item.get("sha256"), str)
            for item in evidence
        )
    )


def normalize_redline_evidence(
    value: object,
    *,
    redlines: dict[str, str],
    cases: list[dict[str, object]],
) -> dict[str, list[str]]:
    """Bind a green redline to current, validated acceptance evidence.

    A bare ``"privacy": "PASS"`` in a human-editable ledger is not a
    release gate.  The binding deliberately points to already-normalized case
    evidence, so the source identity, command contract, and execution envelope
    have all been checked before a redline can turn green.
    """

    if value is None:
        raw: dict[str, object] = {}
    elif isinstance(value, dict):
        raw = value
    else:
        raise EvidenceValidationError("redline_evidence must be an object")
    unknown = sorted(set(raw) - set(REDLINE_CASE_BINDINGS))
    if unknown:
        raise EvidenceValidationError(f"redline_evidence has unknown redlines: {unknown}")
    by_id = {str(case["id"]): case for case in cases}
    normalized: dict[str, list[str]] = {}
    for name, allowed_cases in REDLINE_CASE_BINDINGS.items():
        case_ids = string_list(raw.get(name), field=f"redline_evidence.{name}")
        if len(case_ids) != len(set(case_ids)):
            raise EvidenceValidationError(f"redline_evidence.{name} must not repeat a case")
        if redlines[name] == "PASS":
            if not case_ids:
                raise EvidenceValidationError(
                    f"redlines.{name}=PASS requires current case evidence in redline_evidence.{name}"
                )
            disallowed = [case_id for case_id in case_ids if case_id not in allowed_cases]
            if disallowed:
                raise EvidenceValidationError(
                    f"redline_evidence.{name} may only bind {sorted(allowed_cases)}, got {disallowed}"
                )
            missing_execution = [
                case_id
                for case_id in case_ids
                if not case_has_verified_execution(by_id.get(case_id, {}))
            ]
            if missing_execution:
                raise EvidenceValidationError(
                    f"redline_evidence.{name} requires PASS cases with validated execution evidence: "
                    f"{missing_execution}"
                )
        elif case_ids:
            raise EvidenceValidationError(
                f"redline_evidence.{name} is only allowed when redlines.{name}=PASS"
            )
        normalized[name] = case_ids
    return normalized


def is_tracked_repository_file(repository_root: Path, path: Path) -> bool:
    relative = repository_relative(repository_root, path)
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=repository_root,
            check=False,
            capture_output=True,
        )
    except OSError:
        return False
    return completed.returncode == 0


def normalize_not_applicable_decisions(
    value: object,
    *,
    repository_root: Path,
    evidence_root: Path,
    target_version: str,
    source: dict[str, object],
    cases: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Validate explicit, source-controlled decisions for NOT_APPLICABLE cases.

    NOT_APPLICABLE is never a free pass.  A release-ready ledger needs a
    tracked formal decision document and a current runner-produced execution
    artifact for every such case.  This keeps optional/non-gating product
    choices visible without allowing an editor to erase a test obligation by
    changing a status string.
    """

    if value is None:
        raw: dict[str, object] = {}
    elif isinstance(value, dict):
        raw = value
    else:
        raise EvidenceValidationError("not_applicable_decisions must be an object")
    not_applicable = {
        str(case["id"]): case for case in cases if case.get("status") == "NOT_APPLICABLE"
    }
    unknown = sorted(set(raw) - set(not_applicable))
    if unknown:
        raise EvidenceValidationError(
            f"not_applicable_decisions has cases that are not NOT_APPLICABLE: {unknown}"
        )
    normalized: dict[str, dict[str, object]] = {}
    expected_document_prefix = f"docs/{target_version}/decisions/"
    for case_id in not_applicable:
        entry = raw.get(case_id)
        if not isinstance(entry, dict):
            # This is deliberately a release-state blocker rather than a
            # parse failure: collection can truthfully record a N/A candidate
            # before the product owner supplies a formal decision.
            continue
        decision_id = entry.get("decision_id")
        if not isinstance(decision_id, str) or not re.fullmatch(
            r"PD-V14-[A-Z0-9][A-Z0-9_-]{2,}", decision_id
        ):
            raise EvidenceValidationError(
                f"{case_id}: NOT_APPLICABLE needs a formal decision_id such as PD-V14-OPTIONAL_MODEL"
            )
        document = resolve_repository_path(
            repository_root, entry.get("document"), field=f"{case_id}.not_applicable_decision.document"
        )
        document_relative = repository_relative(repository_root, document)
        if not document_relative.startswith(expected_document_prefix) or document.suffix.lower() != ".json":
            raise EvidenceValidationError(
                f"{case_id}: formal decision must be a JSON document under {expected_document_prefix}"
            )
        if not document.is_file() or not is_tracked_repository_file(repository_root, document):
            raise EvidenceValidationError(
                f"{case_id}: formal decision document must exist and be Git tracked: {document_relative}"
            )
        document_payload = read_json(document)
        if (
            document_payload.get("schema_version") != 1
            or document_payload.get("document_type") != "v14_formal_product_decision"
            or document_payload.get("target_version") != target_version
            or document_payload.get("case_id") != case_id
            or document_payload.get("decision_id") != decision_id
            or document_payload.get("disposition") != "NOT_APPLICABLE"
        ):
            raise EvidenceValidationError(
                f"{case_id}: formal decision document does not identify this v14 NOT_APPLICABLE decision"
            )
        approved_by = document_payload.get("approved_by")
        reason = document_payload.get("reason")
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise EvidenceValidationError(f"{case_id}: formal decision needs approved_by")
        if not isinstance(reason, str) or not reason.strip():
            raise EvidenceValidationError(f"{case_id}: formal decision needs a non-empty reason")
        approved_at = parse_recorded_at(
            document_payload.get("approved_at"), field=f"{case_id}.formal_decision.approved_at"
        )
        raw_evidence = entry.get("evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise EvidenceValidationError(
                f"{case_id}: formal NOT_APPLICABLE decision requires current execution evidence"
            )
        evidence = [
            normalize_actual_evidence(
                evidence_entry,
                repository_root=repository_root,
                evidence_root=evidence_root,
                target_version=target_version,
                case_id=case_id,
                source=source,
                require_attestation=False,
            )
            for evidence_entry in raw_evidence
        ]
        normalized[case_id] = {
            "decision_id": decision_id,
            "document": document_relative,
            "document_sha256": sha256(document),
            "approved_by": approved_by.strip(),
            "approved_at": approved_at,
            "reason": reason.strip(),
            "evidence": evidence,
        }
    return normalized


def value_requires_case_evidence(value: object) -> bool:
    """Return whether a machine-summary value claims a verified observation."""

    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().upper() not in {
            "",
            "NOT_RUN",
            "BLOCKED",
            "FAIL",
            "PARTIAL",
            "UNKNOWN",
            "EXPERIMENTAL_NON_BLOCKING",
        }
    if isinstance(value, list):
        return False
    return True


def normalize_machine_summary_evidence(
    ledger: dict[str, Any], *, target_version: str, cases: list[dict[str, object]]
) -> dict[str, list[str]]:
    raw_summary = ledger.get("machine_summary", {})
    if not isinstance(raw_summary, dict):
        raise EvidenceValidationError("machine_summary must be an object")
    template = default_ledger(target_version)["machine_summary"]
    assert isinstance(template, dict)
    raw_bindings = ledger.get("machine_summary_evidence", {})
    if raw_bindings is None:
        raw_bindings = {}
    if not isinstance(raw_bindings, dict):
        raise EvidenceValidationError("machine_summary_evidence must be an object")
    by_id = {str(case["id"]): case for case in cases}
    required_bindings: set[str] = set()
    for section in ("stt", "voice", "performance", "artifacts"):
        provided = raw_summary.get(section)
        if provided is None:
            continue
        if not isinstance(provided, dict):
            raise EvidenceValidationError(f"machine_summary.{section} must be an object")
        allowed_fields = set(template[section])
        unknown = sorted(set(provided) - allowed_fields)
        if unknown:
            raise EvidenceValidationError(
                f"machine_summary.{section} contains unknown or generated-only fields: {unknown}"
            )
        for field, item in provided.items():
            key = f"{section}.{field}"
            if value_requires_case_evidence(item):
                required_bindings.add(key)

    unknown_bindings = sorted(set(raw_bindings) - required_bindings)
    if unknown_bindings:
        raise EvidenceValidationError(
            f"machine_summary_evidence has no matching verified summary claim: {unknown_bindings}"
        )
    normalized: dict[str, list[str]] = {}
    for key in sorted(required_bindings):
        allowed_cases = MACHINE_SUMMARY_CASE_BINDINGS.get(key)
        if not allowed_cases:
            raise EvidenceValidationError(f"{key}: no controlled acceptance binding is defined")
        case_ids = string_list(raw_bindings.get(key), field=f"machine_summary_evidence.{key}")
        if not case_ids:
            raise EvidenceValidationError(f"{key}: verified machine summary claim needs case evidence")
        if len(case_ids) != len(set(case_ids)):
            raise EvidenceValidationError(f"{key}: machine summary evidence must not repeat a case")
        disallowed = [case_id for case_id in case_ids if case_id not in allowed_cases]
        if disallowed:
            raise EvidenceValidationError(
                f"{key}: may only bind {sorted(allowed_cases)}, got {disallowed}"
            )
        missing_execution = [
            case_id
            for case_id in case_ids
            if not case_has_verified_execution(by_id.get(case_id, {}))
        ]
        if missing_execution:
            raise EvidenceValidationError(
                f"{key}: requires PASS cases with validated execution evidence: {missing_execution}"
            )
        normalized[key] = case_ids
    return normalized


def summary(cases: list[dict[str, object]]) -> dict[str, object]:
    counts = {status.lower(): 0 for status in VALID_STATUSES}
    for case in cases:
        counts[str(case["status"]).lower()] += 1
    passed = counts["pass"]
    failed = counts["fail"]
    total = len(cases)
    pass_denominator = passed + failed
    return {
        "total": total,
        "pass": passed,
        "fail": failed,
        "blocked": counts["blocked"],
        "skipped": counts["skipped"],
        "not_run": counts["not_run"],
        "not_applicable": counts["not_applicable"],
        "pass_rate": round(passed / pass_denominator, 4) if pass_denominator else None,
        # Mirrors plan section 26: blocked/skipped are reported execution
        # dispositions, not passes. NOT_APPLICABLE remains separately visible.
        "execution_coverage": round(
            (passed + failed + counts["blocked"] + counts["skipped"]) / total,
            4,
        ) if total else 0.0,
    }


def normalize_redlines(value: object) -> dict[str, str]:
    if value is None:
        return {"security": "NOT_RUN", "privacy": "NOT_RUN"}
    if not isinstance(value, dict):
        raise EvidenceValidationError("redlines must be an object")
    normalized: dict[str, str] = {}
    for name in ("security", "privacy"):
        status = value.get(name, "NOT_RUN")
        if status not in REDLINE_STATUSES:
            raise EvidenceValidationError(
                f"redlines.{name} must be one of {sorted(REDLINE_STATUSES)}"
            )
        normalized[name] = status
    return normalized


def normalize_release_decision(value: object) -> dict[str, str]:
    if value is None:
        return {"requested_status": "BLOCKED", "reason": "未提供发布放行决定。"}
    if not isinstance(value, dict):
        raise EvidenceValidationError("release_decision must be an object")
    requested = value.get("requested_status", "BLOCKED")
    if requested not in RELEASE_DECISIONS:
        raise EvidenceValidationError(
            "release_decision.requested_status must be one of "
            f"{sorted(RELEASE_DECISIONS)}"
        )
    reason = value.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        raise EvidenceValidationError("release_decision.reason must be a non-empty string")
    return {"requested_status": requested, "reason": reason.strip()}


def case_is_release_resolved(
    case: dict[str, object], *, not_applicable_decisions: dict[str, dict[str, object]]
) -> bool:
    status = case.get("status")
    return status == "PASS" or (
        status == "NOT_APPLICABLE" and str(case.get("id")) in not_applicable_decisions
    )


def release_state(
    *,
    target_version: str,
    source: dict[str, object],
    matrix_summary: dict[str, object],
    redlines: dict[str, str],
    requested: dict[str, str],
    cases: list[dict[str, object]],
    not_applicable_decisions: dict[str, dict[str, object]],
) -> tuple[str, str]:
    failed = int(matrix_summary["fail"])
    unresolved = (
        failed
        + int(matrix_summary["blocked"])
        + int(matrix_summary["not_run"])
        + int(matrix_summary["skipped"])
    )
    unresolved_not_applicable = [
        str(case["id"])
        for case in cases
        if case.get("status") == "NOT_APPLICABLE"
        and str(case["id"]) not in not_applicable_decisions
    ]
    if failed:
        return "FAILED", "验收矩阵存在 FAIL，禁止发布。"
    if source["source_version"] != target_version:
        return "BLOCKED", "VERSION 尚未同步到目标版本；当前仅允许收集预发布证据。"
    if source["workspace_clean"] is not True:
        return "BLOCKED", "工作树并非已核验的干净发布状态。"
    if redlines["security"] != "PASS" or redlines["privacy"] != "PASS":
        return "BLOCKED", "安全或隐私红线尚未明确通过。"
    if unresolved_not_applicable:
        return (
            "BLOCKED",
            "NOT_APPLICABLE cases need a controlled formal product decision and current execution evidence: "
            + ", ".join(unresolved_not_applicable),
        )
    if unresolved:
        return "BLOCKED", "A01-A28 仍有 FAIL、BLOCKED、SKIPPED 或 NOT_RUN 项。"
    if requested["requested_status"] != "READY_FOR_RELEASE":
        return "BLOCKED", "全部证据通过后仍需显式作出 READY_FOR_RELEASE 决定。"
    # This tool cannot publish, push, or observe an installer running.  It may
    # express readiness, never RELEASED, so a later release workflow remains
    # responsible for the actual external state transition.
    return "READY_FOR_RELEASE", requested["reason"]


def merge_machine_summary(
    ledger: dict[str, Any],
    *,
    target_version: str,
    source: dict[str, object],
    matrix_summary: dict[str, object],
    cases: list[dict[str, object]],
    release_status: str,
    evidence_bindings: dict[str, list[str]],
) -> dict[str, object]:
    raw = ledger.get("machine_summary", {})
    if not isinstance(raw, dict):
        raise EvidenceValidationError("machine_summary must be an object")
    template = default_ledger(target_version)["machine_summary"]
    assert isinstance(template, dict)
    merged = json.loads(json.dumps(template, ensure_ascii=False))
    for section in ("stt", "voice", "performance", "artifacts"):
        provided = raw.get(section)
        if provided is None:
            continue
        if not isinstance(provided, dict):
            raise EvidenceValidationError(f"machine_summary.{section} must be an object")
        merged[section].update(provided)
    case_status = {str(case["id"]): str(case["status"]) for case in cases}
    artifacts = merged["artifacts"]
    assert isinstance(artifacts, dict)
    artifacts["feedback_path"] = f"build/v{compact_version(target_version)}-evidence/IMPLEMENTATION_FEEDBACK.md"
    artifacts["evidence_path"] = f"build/v{compact_version(target_version)}-evidence"
    return {
        "version": target_version,
        "release_status": release_status,
        "git_head": source["source_commit"] or "",
        "workspace_clean": source["workspace_clean"],
        "remote_pushed": ledger.get("remote_pushed") is True,
        "tests": matrix_summary,
        "release_gates": {
            "model_download": case_status["A09"],
            "windows_tts": case_status["A02"],
            "melotts": "EXPERIMENTAL_NON_BLOCKING",
            "durability_30m": case_status["A23"],
            "nsis": case_status["A27"],
            "msi": case_status["A28"],
        },
        "stt": merged["stt"],
        "voice": merged["voice"],
        "performance": merged["performance"],
        "artifacts": artifacts,
        "evidence_bindings": evidence_bindings,
    }


def feedback_markdown(
    *,
    target_version: str,
    source: dict[str, object],
    plan: dict[str, object],
    matrix_summary: dict[str, object],
    cases: list[dict[str, object]],
    release_status: str,
    release_reason: str,
    redlines: dict[str, str],
    machine_summary: dict[str, object],
    narrative: object,
) -> str:
    if narrative is None:
        narrative = {}
    if not isinstance(narrative, dict):
        raise EvidenceValidationError("narrative must be an object when supplied")

    def note(key: str) -> str:
        value = narrative.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else "尚未提供可引用的实际执行结论。"

    def case_line(case_id: str) -> str:
        case = next(item for item in cases if item["id"] == case_id)
        detail = str(case.get("result") or case.get("reason") or "已记录实际执行证据。")
        return f"- `{case_id}` `{case['status']}`：{detail}"

    database_source = (SCRIPT_ROOT / "siyi" / "app" / "database.py").read_text(
        encoding="utf-8"
    )
    schema_match = re.search(
        r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", database_source, re.MULTILINE
    )
    if schema_match is None:
        raise EvidenceValidationError("unable to read database schema version for feedback")
    schema_version = schema_match.group(1)

    lines = [
        f"# 司忆 v{target_version} 实施反馈",
        "",
        "本文件由 `scripts/v14-evidence.py` 根据当前证据台账生成。它不执行测试，也不会将代码审查或接口存在写为 PASS。",
        "",
        "## 基本信息",
        "",
        f"- 目标版本：`{target_version}`",
        f"- 当前 VERSION：`{source['source_version']}`",
        f"- Git HEAD：`{source['source_commit'] or '未取得'}`",
        f"- 工作树已清洁核验：`{source['workspace_clean']}`",
        f"- 计划：`{plan['path']}`",
        f"- 计划 SHA-256：`{plan['sha256']}`",
        f"- 计划总行数：`{plan['line_count']}`",
        "",
        "## Git 状态",
        "",
        f"- source_commit=`{source['source_commit'] or '未取得'}`；workspace_clean=`{source['workspace_clean']}`；source_tree_fingerprint=`{source['source_tree_fingerprint']}`。",
        "",
        "## 版本与 Schema",
        "",
        f"- 目标版本=`{target_version}`；当前 VERSION=`{source['source_version']}`；SQLite Schema=`{schema_version}`。",
        "",
        "## 原始目标",
        "",
        "- 发布门禁清零与语音输出正式化。",
        "- 本地 STT、录音和语音输入闭环。",
        "- 全链路语音交互、资源协调与稳定性验证。",
        "",
        "## 发布判定",
        "",
        f"- 当前状态：`{release_status}`",
        f"- 判定依据：{release_reason}",
        f"- 安全红线：`{redlines['security']}`；隐私红线：`{redlines['privacy']}`",
        "",
        "## 逐项完成情况（A01–A28）",
        "",
        "| 编号 | 能力 | 状态 | 说明 |",
        "|---|---|---|---|",
    ]
    for case in cases:
        detail = str(case.get("result") or case.get("reason") or "已记录实际执行证据。")
        detail = detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {case['id']} | {case['name']} | {case['status']} | {detail} |")

    lines.extend(
        [
            "",
            "## 发布门禁处理",
            "",
            note("implementation"),
            "",
            "## TTS 正式化",
            "",
            case_line("A02"),
            "",
            "## 麦克风与录音",
            "",
            "\n".join(case_line(item) for item in ("A04", "A05", "A06")),
            "",
            "## STT Provider",
            "",
            case_line("A08"),
            "",
            "## STT 模型",
            "",
            case_line("A09"),
            "",
            "## STT API",
            "",
            "\n".join(case_line(item) for item in ("A10", "A11")),
            "",
            "## Voice Session",
            "",
            "\n".join(case_line(item) for item in ("A07", "A12")),
            "",
            "## 前端交互",
            "",
            case_line("A13"),
            "",
            "## Agent 联动",
            "",
            "\n".join(case_line(item) for item in ("A14", "A15", "A19")),
            "",
            "## 停止与打断",
            "",
            "\n".join(case_line(item) for item in ("A16", "A17", "A18")),
            "",
            "## 资源",
            "",
            case_line("A20"),
            "",
            "## 隐私",
            "",
            case_line("A24"),
            "",
            "## 性能",
            "",
            "\n".join(case_line(item) for item in ("A21", "A22", "A25")),
            "",
            "## 30 分钟耐久",
            "",
            case_line("A23"),
            "",
            "## 故障注入",
            "",
            note("verification"),
            "",
            "## NSIS",
            "",
            case_line("A27"),
            "",
            "## MSI",
            "",
            case_line("A28"),
            "",
            "## 失败、阻塞、跳过和未运行",
            "",
        ]
    )
    unresolved = [case for case in cases if case["status"] != "PASS"]
    if unresolved:
        for case in unresolved:
            lines.append(f"- `{case['id']}` `{case['status']}`：{case.get('reason') or case.get('result') or '未提供说明。'}")
    else:
        lines.append("- 无。所有 A01–A28 均绑定了当前目标版本下的实际执行证据。")
    lines.extend(
        [
            "",
            "## 测试统计",
            "",
            "- total={total}；pass={pass_count}；fail={fail}；blocked={blocked}；"
            "skipped={skipped}；not_run={not_run}；not_applicable={not_applicable}".format(
                total=matrix_summary["total"],
                pass_count=matrix_summary["pass"],
                fail=matrix_summary["fail"],
                blocked=matrix_summary["blocked"],
                skipped=matrix_summary["skipped"],
                not_run=matrix_summary["not_run"],
                not_applicable=matrix_summary["not_applicable"],
            ),
            f"- pass_rate={matrix_summary['pass_rate']}；execution_coverage={matrix_summary['execution_coverage']}",
            "",
            "## 已知问题",
            "",
            note("follow_up"),
            "",
            "## 技术债务",
            "",
            note("technical_debt"),
            "",
            "## 后续候选",
            "",
            note("next_candidates"),
            "",
            "## 用户决策",
            "",
            note("user_decisions"),
            "",
            "## 机器可读摘要",
            "",
            "```json",
            json.dumps(machine_summary, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _build_v14_matrix(
    *,
    target_version: str,
    source: dict[str, object],
    plan: dict[str, object],
    matrix_summary: dict[str, object],
    cases: list[dict[str, object]],
    redline_evidence: list[dict[str, object]],
    not_applicable_decisions: list[dict[str, object]],
    machine_summary_evidence: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": 4,
        "target_version": target_version,
        **source,
        "plan": plan,
        "summary": matrix_summary,
        "cases": cases,
        "redline_evidence": redline_evidence,
        "not_applicable_decisions": not_applicable_decisions,
        "machine_summary_evidence": machine_summary_evidence,
    }


def _build_v14_release_status(
    *,
    ledger: dict[str, Any],
    target_version: str,
    source: dict[str, object],
    matrix_summary: dict[str, object],
    cases: list[dict[str, object]],
    release_status: str,
    release_reason: str,
    redlines: dict[str, str],
    redline_evidence: list[dict[str, object]],
    not_applicable_decisions: list[dict[str, object]],
    machine_summary: dict[str, object],
) -> dict[str, object]:
    all_cases_release_resolved = all(
        case_is_release_resolved(
            case, not_applicable_decisions=not_applicable_decisions
        )
        for case in cases
    )
    distribution_ready = release_status == "READY_FOR_RELEASE"
    if source["source_version"] != target_version:
        metadata_check_status = "BLOCKED_PRE_VERSION_SYNC"
    elif distribution_ready:
        metadata_check_status = "READY_TO_RUN"
    else:
        metadata_check_status = "NOT_READY"
    return {
        "schema_version": 4,
        "target_version": target_version,
        **source,
        "implementation_status": "COMPLETE" if all_cases_release_resolved else "PARTIAL",
        "test_status": "READY" if all_cases_release_resolved else "NOT_READY",
        # check-release-metadata.py --release requires READY.  This tool sets
        # it only for a fully resolved, target-version readiness decision; it
        # never means that an installer was already distributed.
        "distribution_status": "READY" if distribution_ready else "NOT_READY",
        "release_status": release_status,
        "release_reason": release_reason,
        "matrix": matrix_summary,
        "blocking_cases": [
            case["id"]
            for case in cases
            if not case_is_release_resolved(
                case, not_applicable_decisions=not_applicable_decisions
            )
        ],
        "redline_assessments": redlines,
        "redline_evidence": redline_evidence,
        "not_applicable_decisions": not_applicable_decisions,
        "security_redline": (
            True
            if redlines["security"] == "FAIL"
            else (False if redlines["security"] == "PASS" else None)
        ),
        "privacy_redline": (
            True
            if redlines["privacy"] == "FAIL"
            else (False if redlines["privacy"] == "PASS" else None)
        ),
        "github_pushed": ledger.get("remote_pushed") is True,
        "release_metadata_check": {
            "command": "python scripts/check-release-metadata.py --release",
            "status": metadata_check_status,
            "required_source_version": target_version,
            "required_distribution_status": "READY",
            "documentation_root": f"docs/{target_version}",
        },
        "documentation_mirror": {
            "root": f"docs/{target_version}",
            "files": list(GENERATED_DOCUMENT_FILENAMES),
        },
        "machine_readable_summary": machine_summary,
    }


def _write_v14_core_documents(
    *,
    output_root: Path,
    matrix: dict[str, object],
    feedback: str,
    status: dict[str, object],
) -> tuple[Path, Path, Path]:
    matrix_path = output_root / "TEST_MATRIX.json"
    status_path = output_root / "RELEASE_STATUS.json"
    feedback_path = output_root / "IMPLEMENTATION_FEEDBACK.md"
    write_json_atomic(matrix_path, matrix)
    write_text_atomic(feedback_path, feedback)
    write_json_atomic(status_path, status)
    return matrix_path, status_path, feedback_path


def _build_v14_manifest(
    *,
    repository_root: Path,
    output_root: Path,
    target_version: str,
    source: dict[str, object],
    plan: dict[str, object],
    cases: list[dict[str, object]],
    release_status: str,
    machine_summary: dict[str, object],
    matrix_path: Path,
    status_path: Path,
    feedback_path: Path,
) -> dict[str, object]:
    evidence_by_path: dict[str, dict[str, object]] = {}
    for case in cases:
        for evidence in case["evidence"]:
            if isinstance(evidence, dict) and isinstance(evidence.get("path"), str) and "sha256" in evidence:
                evidence_by_path[evidence["path"]] = evidence
    return {
        "schema_version": 4,
        "target_version": target_version,
        **source,
        "plan_sha256": plan["sha256"],
        "release_status": repository_relative(repository_root, status_path),
        "test_matrix": repository_relative(repository_root, matrix_path),
        "implementation_feedback": repository_relative(repository_root, feedback_path),
        "evidence_root": repository_relative(repository_root, output_root),
        "generated_at": utc_now(),
        "generated_files": [
            {
                "path": repository_relative(repository_root, matrix_path),
                "sha256": sha256(matrix_path),
            },
            {
                "path": repository_relative(repository_root, status_path),
                "sha256": sha256(status_path),
            },
            {
                "path": repository_relative(repository_root, feedback_path),
                "sha256": sha256(feedback_path),
            },
        ],
        "evidence": list(evidence_by_path.values()),
        "artifacts": machine_summary["artifacts"],
        "status": release_status,
        "documentation_mirror": {
            "root": f"docs/{target_version}",
            "files": list(GENERATED_DOCUMENT_FILENAMES),
        },
    }


def build_documents(
    *,
    repository_root: Path,
    target_version: str,
    plan_path: Path,
    ledger: dict[str, Any],
    output_root: Path,
    expected_plan_sha256: str = V14_PLAN_SHA256,
) -> dict[str, dict[str, object]]:
    source = source_identity(repository_root)
    plan = plan_identity(repository_root, plan_path, expected_sha256=expected_plan_sha256)
    if ledger.get("target_version") != target_version:
        raise EvidenceValidationError("ledger.target_version must exactly match --target-version")
    cases = normalize_cases(
        ledger,
        repository_root=repository_root,
        evidence_root=output_root,
        target_version=target_version,
        source=source,
    )
    matrix_summary = summary(cases)
    redlines = normalize_redlines(ledger.get("redlines"))
    redline_evidence = normalize_redline_evidence(
        ledger.get("redline_evidence"), redlines=redlines, cases=cases
    )
    not_applicable_decisions = normalize_not_applicable_decisions(
        ledger.get("not_applicable_decisions"),
        repository_root=repository_root,
        evidence_root=output_root,
        target_version=target_version,
        source=source,
        cases=cases,
    )
    machine_summary_evidence = normalize_machine_summary_evidence(
        ledger, target_version=target_version, cases=cases
    )
    requested = normalize_release_decision(ledger.get("release_decision"))
    release_status, release_reason = release_state(
        target_version=target_version,
        source=source,
        matrix_summary=matrix_summary,
        redlines=redlines,
        requested=requested,
        cases=cases,
        not_applicable_decisions=not_applicable_decisions,
    )
    machine_summary = merge_machine_summary(
        ledger,
        target_version=target_version,
        source=source,
        matrix_summary=matrix_summary,
        cases=cases,
        release_status=release_status,
        evidence_bindings=machine_summary_evidence,
    )
    matrix = _build_v14_matrix(
        target_version=target_version,
        source=source,
        plan=plan,
        matrix_summary=matrix_summary,
        cases=cases,
        redline_evidence=redline_evidence,
        not_applicable_decisions=not_applicable_decisions,
        machine_summary_evidence=machine_summary_evidence,
    )
    feedback = feedback_markdown(
        target_version=target_version,
        source=source,
        plan=plan,
        matrix_summary=matrix_summary,
        cases=cases,
        release_status=release_status,
        release_reason=release_reason,
        redlines=redlines,
        machine_summary=machine_summary,
        narrative=ledger.get("narrative"),
    )
    status = _build_v14_release_status(
        ledger=ledger,
        target_version=target_version,
        source=source,
        matrix_summary=matrix_summary,
        cases=cases,
        release_status=release_status,
        release_reason=release_reason,
        redlines=redlines,
        redline_evidence=redline_evidence,
        not_applicable_decisions=not_applicable_decisions,
        machine_summary=machine_summary,
    )
    matrix_path, status_path, feedback_path = _write_v14_core_documents(
        output_root=output_root,
        matrix=matrix,
        feedback=feedback,
        status=status,
    )
    manifest = _build_v14_manifest(
        repository_root=repository_root,
        output_root=output_root,
        target_version=target_version,
        source=source,
        plan=plan,
        cases=cases,
        release_status=release_status,
        machine_summary=machine_summary,
        matrix_path=matrix_path,
        status_path=status_path,
        feedback_path=feedback_path,
    )
    manifest_path = output_root / "EVIDENCE_MANIFEST.json"
    write_json_atomic(manifest_path, manifest)
    sync_documents_to_docs(
        repository_root=repository_root,
        target_version=target_version,
        output_root=output_root,
    )
    return {"matrix": matrix, "status": status, "manifest": manifest}


def sync_documents_to_docs(*, repository_root: Path, target_version: str, output_root: Path) -> None:
    """Mirror every v14 deliverable into the release documentation directory.

    The mirror is intentionally byte-for-byte copied from the evidence root so
    the human feedback and the three machine-readable files cannot drift apart.
    Generated mirrors are excluded from the source fingerprint, allowing a
    later verification command to bind to the same candidate source tree.
    """

    documentation_root = repository_root / "docs" / target_version
    for filename in GENERATED_DOCUMENT_FILENAMES:
        source = output_root / filename
        if not source.is_file():
            raise EvidenceValidationError(f"generated evidence document is missing: {source}")
        write_text_atomic(
            documentation_root / filename,
            source.read_text(encoding="utf-8"),
        )


def copy_feedback_to_docs(*, repository_root: Path, target_version: str, output_root: Path, destination: Path) -> None:
    expected = repository_root / "docs" / target_version / "IMPLEMENTATION_FEEDBACK.md"
    if destination.resolve() != expected.resolve():
        raise EvidenceValidationError(
            "--feedback-doc must be the release documentation target "
            f"{repository_relative(repository_root, expected)}"
        )
    sync_documents_to_docs(
        repository_root=repository_root,
        target_version=target_version,
        output_root=output_root,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate/validate conservative v14.0.0 release evidence")
    parser.add_argument(
        "--target-version",
        default=TARGET_VERSION,
        help="Must be v14.0.0; may differ from current VERSION during collection.",
    )
    parser.add_argument("--plan", type=Path, required=True, help="The exact user-supplied v14 plan file.")
    parser.add_argument("--input", type=Path, help="Evidence ledger JSON. PASS entries need current execution artifacts.")
    parser.add_argument("--init", action="store_true", help="Write a conservative editable evidence ledger under the target evidence directory.")
    parser.add_argument("--repository-root", type=Path, default=SCRIPT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--output-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--feedback-doc", type=Path, help="Also copy feedback to docs/14.0.0/IMPLEMENTATION_FEEDBACK.md.")
    arguments = parser.parse_args(argv)
    if bool(arguments.input) == bool(arguments.init):
        parser.error("provide exactly one of --input or --init")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv or sys.argv[1:])
    target_version = normalize_version(arguments.target_version)
    if target_version != TARGET_VERSION:
        raise EvidenceValidationError(f"this generator is scoped to v{TARGET_VERSION}, not v{target_version}")
    repository_root = arguments.repository_root.resolve()
    plan_path = arguments.plan.resolve()
    output_root = (
        arguments.output_root.resolve()
        if arguments.output_root
        else expected_evidence_root(repository_root, target_version).resolve()
    )
    expected_root = expected_evidence_root(repository_root, target_version).resolve()
    if output_root != expected_root:
        raise EvidenceValidationError(f"v14 evidence must be written to {expected_root}, not {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    if arguments.init:
        template_path = output_root / "V14_EVIDENCE_INPUT.json"
        if template_path.exists():
            raise EvidenceValidationError(f"refusing to overwrite existing evidence ledger: {template_path}")
        write_json_atomic(template_path, default_ledger(target_version))
        print(json.dumps({"status": "TEMPLATE_WRITTEN", "path": repository_relative(repository_root, template_path)}, ensure_ascii=False))
        return 0

    ledger = read_json(arguments.input.resolve())
    documents = build_documents(
        repository_root=repository_root,
        target_version=target_version,
        plan_path=plan_path,
        ledger=ledger,
        output_root=output_root,
    )
    if arguments.feedback_doc:
        copy_feedback_to_docs(
            repository_root=repository_root,
            target_version=target_version,
            output_root=output_root,
            destination=arguments.feedback_doc.resolve(),
        )
    print(
        json.dumps(
            {
                "status": documents["status"]["release_status"],
                "target_version": target_version,
                "source_version": documents["status"]["source_version"],
                "evidence_root": repository_relative(repository_root, output_root),
                "matrix": documents["status"]["matrix"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidenceValidationError as exc:
        print(f"v14 evidence validation failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
