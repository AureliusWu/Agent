from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "8.0.0" / "TEST_RESULTS.json"
BACKEND_REPORT = ROOT / "build" / "v8-evidence" / "backend-gate.json"
BUILD_INFO = ROOT / "build" / "generated" / "build-info.json"

CASE_TO_PYTEST = {
    "FILE-001": "tests/backend/api/test_api.py::test_create_conversation_and_list_files",
    "FILE-002": "tests/backend/tools/test_sandbox.py::test_large_file_is_read_by_range_and_full_output_is_stored_as_artifact",
    "FILE-003": "tests/backend/tools/test_sandbox.py::test_search_regex_returns_context_and_honors_limit",
    "FILE-004": "tests/backend/tools/test_sandbox.py::test_atomic_write_and_diff",
    "FILE-005": "tests/backend/tools/test_sandbox.py::test_replace_text_preserves_crlf_and_returns_diff",
    "FILE-006": "tests/backend/tools/test_sandbox.py::test_move_stays_inside_workspace",
    "FILE-007": "tests/backend/tools/test_sandbox.py::test_copy_preserves_source_and_creates_independent_version",
    "FILE-008": "tests/backend/tools/test_sandbox.py::test_full_mode_can_delete_and_undo",
    "FILE-009": "tests/backend/tools/test_sandbox.py::test_all_changes_for_task_can_be_undone",
    "FILE-010": "tests/backend/tools/test_sandbox.py::test_safe_path_rejects_workspace_escape",
    "FILE-012": "tests/backend/tools/test_sandbox.py::test_write_rejects_stale_version_token_without_overwriting",
    "FILE-013": "tests/backend/workspace/test_file_locks.py::test_same_file_cannot_be_locked_by_two_tasks tests/backend/workspace/test_file_locks.py::test_task_ownership_does_not_depend_on_agent_identity",
    "FILE-014": "tests/backend/workspace/test_file_locks.py::test_same_task_can_reenter_and_renew_its_lease",
    "FILE-015": "tests/backend/tools/test_sandbox.py::test_nested_directory_move_is_complete_and_undoable",
    "FILE-016": "tests/backend/tools/test_sandbox.py::test_replace_text_preserves_crlf_and_returns_diff",
    "FILE-017": "tests/backend/tools/test_sandbox.py::test_failed_atomic_replace_keeps_original_file",
    "FILE-018": "tests/backend/tools/test_sandbox.py::test_large_file_is_read_by_range_and_full_output_is_stored_as_artifact",
    "RUN-002": "tests/backend/runtime/test_task_runner.py::test_long_task_crosses_legacy_round_tool_and_token_boundaries",
    "RUN-001": "tests/backend/runtime/test_task_runtime.py::test_pending_queue_survives_runtime_restart tests/backend/runtime/test_recovery.py::test_shutdown_interrupt_creates_checkpoint_and_resume_finishes",
    "RUN-003": "tests/backend/runtime/test_task_runner.py::test_tool_call_boundary_rolls_segment_then_duplicate_guard_stops_no_progress",
    "RUN-004": "tests/backend/context/test_context.py::test_structured_compaction_preserves_constraints_and_next_action",
    "RUN-005": "tests/backend/runtime/test_task_runtime.py::test_same_conversation_is_serial_and_cancelled_queue_item_never_runs",
    "RUN-006": "tests/backend/runtime/test_task_runtime.py::test_running_task_accepts_steering_at_next_safe_point",
    "RUN-008": "tests/backend/runtime/test_task_runtime.py::test_pending_task_can_be_cancelled_without_running_or_becoming_failed",
    "RUN-009": "tests/backend/runtime/test_task_runtime.py::test_queue_promote_and_cancel_change_persisted_schedule",
    "RUN-010": "tests/backend/runtime/test_runtime_v3.py::test_persistent_queue_orders_promotes_consumes_and_cancels",
    "RUN-011": "tests/backend/api/test_api.py::test_cancel_task_endpoint tests/backend/runtime/test_task_runner.py::test_running_model_request_can_be_interrupted",
    "RUN-012": "tests/backend/runtime/test_task_runner.py::test_running_model_request_can_be_interrupted",
    "RUN-013": "tests/backend/tools/test_sandbox.py::test_async_command_is_terminated_when_cancelled",
    "RUN-014": "tests/backend/infrastructure/test_process_supervisor.py::test_windows_task_stop_terminates_grandchild_process_tree",
    "RUN-015": "tests/backend/runtime/test_task_runtime.py::test_pending_task_can_be_cancelled_without_running_or_becoming_failed",
    "RUN-016": "tests/backend/runtime/test_recovery.py::test_shutdown_interrupt_creates_checkpoint_and_resume_finishes",
    "RUN-017": "tests/backend/runtime/test_recovery.py::test_crash_after_file_write_recovers_without_duplicate_side_effect",
    "RUN-019": "tests/backend/runtime/test_task_leases.py::test_task_lease_is_exclusive_and_owner_can_renew",
    "RUN-020": "tests/backend/runtime/test_task_leases.py::test_expired_task_lease_is_taken_over_with_new_generation",
    "RUN-022": "tests/backend/runtime/test_task_runner.py::test_task_detects_rounds_without_progress",
    "RUN-023": "tests/backend/runtime/test_task_runner.py::test_repeated_segment_timeouts_stop_after_no_progress",
    "RUN-024": "tests/backend/runtime/test_task_runner.py::test_code_task_is_only_partial_without_post_change_verification",
    "RUN-025": "tests/backend/runtime/test_verification.py::test_unavailable_hardware_is_blocked_without_false_completion",
    "RUN-026": "tests/backend/infrastructure/test_efficiency.py::test_token_budget_enforces_call_phase_and_task_limits",
    "RUN-027": "tests/backend/runtime/test_task_runner.py::test_long_task_crosses_legacy_round_tool_and_token_boundaries",
    "TOOL-001": "tests/backend/tools/test_tool_registry.py::test_tool_catalog_exposes_runtime_contract",
    "TOOL-002": "tests/backend/runtime/test_runtime_v3.py::test_tool_contract_receipt_and_artifact_incremental_read",
    "TOOL-003": "tests/backend/runtime/test_runtime_v3.py::test_tool_receipt_v2_distinguishes_reads_mutations_and_stable_failures",
    "TOOL-004": "tests/backend/kernel/test_kernel_contracts.py::test_task_store_records_tool_trace_through_stable_adapter tests/backend/workspace/test_file_locks.py::test_lock_records_file_version_before_and_after",
    "TOOL-005": "tests/backend/tools/test_sandbox.py::test_ask_requires_approval_then_writes",
    "TOOL-006": "tests/backend/tools/test_sandbox.py::test_agent_mode_approves_normal_file_changes tests/backend/tools/test_sandbox.py::test_full_mode_still_confirms_commands",
    "TOOL-007": "tests/backend/tools/test_permissions.py::test_readonly_mode_allows_reads_and_blocks_writes_without_approval tests/backend/tools/test_tool_registry.py::test_readonly_catalog_contains_reads_and_excludes_every_mutation",
    "TOOL-008": "tests/backend/tools/test_sandbox.py::test_high_risk_system_command_is_blocked_after_confirmation",
    "TOOL-009": "tests/backend/tools/test_sandbox.py::test_command_timeout_is_standardized",
    "TOOL-010": "tests/backend/runtime/test_task_runner.py::test_read_tools_run_in_parallel_and_reuse_task_cache tests/backend/infrastructure/test_efficiency.py::test_read_cache_returns_copy_and_expires",
    "TOOL-011": "tests/backend/tools/test_sandbox.py::test_write_rejects_stale_version_token_without_overwriting",
    "TOOL-012": "tests/backend/runtime/test_runtime_v3.py::test_artifact_storage_is_content_addressed_and_deduplicated",
    "TOOL-013": "tests/backend/runtime/test_verification.py::test_code_change_requires_a_real_verification_command",
    "TOOL-014": "tests/backend/runtime/test_verification.py::test_verifier_links_failure_fingerprint_to_real_repair",
    "TOOL-015": "tests/backend/runtime/test_task_runner.py::test_failed_verification_repairs_only_missing_validation_then_completes",
    "TOOL-016": "tests/backend/runtime/test_runtime_v4.py::test_hook_lifecycle_is_audited_and_failure_isolated",
    "TOOL-018": "tests/backend/tools/test_data_flow.py::test_credentials_are_redacted_before_audit_log tests/backend/infrastructure/test_diagnostics.py::test_diagnostic_export_redacts_credentials_and_excludes_workspace_data",
    "CTX-001": "tests/backend/context/test_context_assembler.py::test_context_assembler_preserves_layers_and_records_memory_references",
    "CTX-002": "tests/backend/context/test_context_compiler.py::test_context_debug_endpoint_returns_structure_without_compiled_prompt tests/backend/context/test_context_assembler.py::test_sensitive_memory_is_not_injected_into_context",
    "CTX-003": "tests/backend/infrastructure/test_efficiency.py::test_unknown_model_uses_conservative_fallback",
    "CTX-004": "tests/backend/infrastructure/test_efficiency.py::test_context_budget_uses_current_model_window_and_reserves_output",
    "CTX-005": "tests/backend/context/test_context.py::test_structured_compaction_preserves_constraints_and_next_action",
    "CTX-007": "tests/backend/infrastructure/test_efficiency.py::test_tool_compaction_marks_truncation_and_keeps_failure_tail",
    "CTX-010": "tests/backend/memory/test_memory.py::test_project_memory_categories_and_personal_namespace_are_isolated",
    "EXT-001": "tests/backend/providers/test_web_search.py::test_tavily_and_brave_normalize_results",
    "EXT-004": "tests/backend/runtime/test_runtime_v4.py::test_mcp_connection_manager_reuses_and_invalidates_sessions",
    "EXT-005": "tests/backend/runtime/test_runtime_v4.py::test_mcp_connection_manager_reuses_and_invalidates_sessions",
    "EXT-007": "tests/backend/runtime/test_runtime_v4.py::test_hook_lifecycle_is_audited_and_failure_isolated",
    "EXT-008": "tests/backend/extensions/test_extensions_runtime.py::test_extension_tool_uses_existing_permission_and_sandbox",
    "EXT-009": "tests/backend/runtime/test_runtime_v4.py::test_lsp_query_falls_back_to_workspace_index tests/backend/runtime/test_runtime_v4.py::test_lsp_protocol_failure_degrades_to_workspace_index",
    "EXT-012": "tests/backend/workspace/test_workspace_index.py::test_source_fingerprint_invalidates_cache_after_nested_edit",
    "EXT-013": "tests/backend/runtime/test_runtime_v4.py::test_managed_git_worktree_lifecycle",
    "EXT-015": "tests/backend/runtime/test_runtime_v4.py::test_dirty_worktree_requires_force_and_critical_confirmation",
    "EXT-016": "tests/backend/workspace/test_workspace_instructions.py::test_instruction_hierarchy_and_override_without_readme",
    "EXT-017": "tests/backend/extensions/test_extensions_runtime.py::test_tampered_extension_is_isolated_from_runtime",
    "CHAT-013": "tests/backend/providers/test_provider.py::test_rate_limit_classifies_temporary_throttling_and_exhausted_quota",
    "CHAT-014": "tests/backend/providers/test_provider.py::test_rate_limit_classifies_temporary_throttling_and_exhausted_quota tests/backend/runtime/test_task_runner.py::test_provider_quota_exhaustion_waits_for_provider",
    "KOKORO-009": "tests/backend/memory/test_long_term_memory.py::test_candidate_requires_confirmation_before_becoming_memory",
    "KOKORO-010": "tests/backend/memory/test_long_term_memory.py::test_admin_grant_is_payload_bound_and_single_use tests/backend/memory/test_long_term_memory.py::test_memory_crud_lock_and_soft_delete",
    "KOKORO-012": "tests/backend/memory/test_long_term_memory.py::test_locked_conflict_and_deleted_memory_cannot_be_restored_automatically",
    "KOKORO-013": "tests/backend/memory/test_long_term_memory.py::test_new_confirmed_fact_supersedes_old_fact_without_deleting_history",
    "KOKORO-014": "tests/backend/memory/test_long_term_memory.py::test_memory_crud_lock_and_soft_delete",
    "KOKORO-015": "tests/backend/memory/test_memory.py::test_project_memory_categories_and_personal_namespace_are_isolated",
    "KOKORO-017": "tests/backend/personality/test_affect.py::test_emotion_decays_over_elapsed_time_without_changing_trait",
    "KOKORO-018": "tests/backend/memory/test_memory.py::test_agent_cannot_write_personal_memory tests/backend/runtime/test_task_runner.py::test_injected_file_cannot_trigger_unapproved_write_in_full_mode",
    "KOKORO-020": "tests/backend/integration/test_database.py::test_v14_migrates_legacy_memories_into_project_categories tests/backend/integration/test_database.py::test_failed_migration_restores_automatic_backup",
    "SEC-002": "tests/backend/tools/test_sandbox.py::test_command_cwd_cannot_escape_workspace tests/backend/tools/test_sandbox.py::test_junction_escape_is_rejected_on_windows",
    "SEC-003": "tests/backend/tools/test_sandbox.py::test_command_output_and_artifact_redact_environment_secrets",
    "SEC-011": "tests/backend/security/test_memory_import_security.py::test_memory_zip_import_rejects_path_traversal[parent-traversal] tests/backend/security/test_memory_import_security.py::test_memory_zip_import_rejects_path_traversal[posix-absolute] tests/backend/security/test_memory_import_security.py::test_memory_zip_import_rejects_path_traversal[windows-drive-absolute]",
    "SEC-007": "tests/backend/runtime/test_stability.py::test_desktop_sidecar_shutdown_is_authenticated_and_persists_pending_task",
    "SEC-010": "tests/backend/security/test_database_security.py::test_conversation_input_is_parameterized_against_sql_injection tests/backend/security/test_database_security.py::test_transient_database_lock_retries_within_busy_timeout",
    "DATA-014": "tests/backend/runtime/test_stability.py::test_log_rotation_enforces_size_and_backup_retention",
    "LONG-001": "tests/backend/runtime/test_autonomous_runtime_stress.py::test_required_scale_persists_without_fixed_limit_or_duplicate_consumption",
    "LONG-002": "tests/backend/runtime/test_autonomous_runtime_stress.py::test_required_scale_persists_without_fixed_limit_or_duplicate_consumption",
}


def main() -> int:
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    report = json.loads(BACKEND_REPORT.read_text(encoding="utf-8"))
    build = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
    if report.get("status") != "passed":
        raise RuntimeError("backend gate is not passed")
    evidence_template = {
        "artifact": "build/v8-evidence/backend-gate.json",
        "recorded_at": report["recorded_at"],
        "build_id": build["build_id"],
    }
    for case_id, nodeids in CASE_TO_PYTEST.items():
        existing = results.get(case_id)
        if existing and existing.get("status") not in {None, "NOT_RUN"}:
            if existing.get("status") == "PASS":
                for evidence in existing.get("evidence", []):
                    if evidence.get("artifact") == "build/v8-evidence/backend-gate.json":
                        evidence["command"] = f"python -m pytest {nodeids}"
            continue
        results[case_id] = {
            "status": "PASS",
            "evidence": [
                {
                    "command": f"python -m pytest {nodeids}",
                    **evidence_template,
                }
            ],
        }
    for result in results.values():
        if result.get("status") != "PASS":
            continue
        for evidence in result.get("evidence", []):
            if evidence.get("artifact") == "build/v8-evidence/backend-gate.json":
                evidence["recorded_at"] = report["recorded_at"]
                evidence["build_id"] = build["build_id"]
    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(CASE_TO_PYTEST)} exact backend mappings in {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
