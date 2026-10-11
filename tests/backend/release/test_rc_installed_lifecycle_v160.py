"""Synthetic lifecycle/fault tests. Never run an installer or promote manual PASS."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import rc_installed_lifecycle as lifecycle


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return lifecycle.Artifact(path, lifecycle.sha(path))


@pytest.fixture
def plan(tmp_path):
    current_file, previous_file = tmp_path / "current.msi", tmp_path / "previous.msi"
    current_file.write_bytes(b"current" + b"0" * (1024 * 1024))
    previous_file.write_bytes(b"previous" + b"0" * (1024 * 1024))
    current = lifecycle.Package(lifecycle.Artifact(current_file, lifecycle.sha(current_file)), "msi", "16.0.0",
                                "{AAAAAAAA-1111-2222-3333-444444444444}", lifecycle.FAMILY)
    previous = lifecycle.Package(lifecycle.Artifact(previous_file, lifecycle.sha(previous_file)), "msi", "8.0.1",
                                 "{BBBBBBBB-1111-2222-3333-444444444444}", lifecycle.FAMILY)
    manifest = {"product_version": "16.0.0", "database_schema_version": 46, "build_id": "a" * 24}
    older = {"product_version": "8.0.1", "database_schema_version": 23}
    source = {"source_version": "16.0.0", "source_commit": "a" * 40, "workspace_clean": True,
              "source_tree_fingerprint": "b" * 64, "build_id": "a" * 24}
    return lifecycle.Plan(tmp_path, tmp_path / "lifecycle.json", current, previous, manifest, older, source,
        (current.artifact, previous.artifact), {"desktop": digest(b"candidate"), "sidecar": digest(b"sidecar")},
        "d" * 64, {"schema_version": 1})


class SyntheticAdapter(lifecycle.WindowsAdapter):
    actual_run = False
    mutation_started = False

    def __init__(self, source, fail_stage=None):
        self.source = {key: value for key, value in source.items() if key != "build_id"}
        self.product = None
        self.invocations = []
        self.fail_stage = fail_stage
        self.override_host = None

    def source_identity(self, root):
        return self.source

    def observe(self, fixture):
        if self.override_host is not None:
            return self.override_host
        package = self.product
        return {"installations": [] if package is None else [{"name": "司忆", "version": package.version,
            "publisher": "github", "path": str(fixture.install), "product_code": package.product_code,
            "uninstall": str(fixture.install / "uninstall.exe")}],
            "related_products": [] if package is None or package.kind != "msi" else [{"product_code": package.product_code,
                "upgrade_code": lifecycle.FAMILY, "version": package.version, "path": str(fixture.install)}],
            "install_paths": [] if package is None else [{"key": r"HKCU:\Software\github\司忆", "path": str(fixture.install),
                "value_names": ["InstallDir"], "subkey_count": 0}],
            "shortcuts": [], "running": [], "is_admin": True, "installer_busy": False, "webviews": ["130.0.0.0"]}

    def install(self, package, fixture, *, remove, uninstaller_sha, stage):
        fixture.validate()
        self.invocations.append(stage)
        if remove:
            assert fixture.install.parent == fixture.root and fixture.root.parent == fixture.plan.output.parent
            shutil.rmtree(fixture.install)
            self.product = None
        else:
            fixture.install.mkdir(exist_ok=True)
            (fixture.install / "司忆.exe").write_bytes(b"candidate" if package.version == "16.0.0" else b"older")
            (fixture.install / "agent-backend.exe").write_bytes(b"sidecar")
            (fixture.install / "_internal").mkdir(exist_ok=True)
            if package.kind == "nsis":
                (fixture.install / "uninstall.exe").write_bytes(b"MZsynthetic-uninstaller")
            self.product = package
        if self.fail_stage == stage:
            raise lifecycle.SafetyError("synthetic partial installation")
        return {"passed": True, "exit_code": 0, "synthetic_only": True}

    def launch(self, package, fixture, *, current, plan, stage):
        self.invocations.append(stage)
        if self.fail_stage == stage:
            return {"passed": False, "forced_termination": True}
        path = fixture.data / "data/agent.db"
        if not path.exists():
            path.parent.mkdir()
            with sqlite3.connect(path) as db:
                db.executescript("""
                 CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                 INSERT INTO schema_migrations VALUES(23,'synthetic');
                 CREATE TABLE conversations(id INTEGER PRIMARY KEY,title TEXT,workspace TEXT,permission_mode TEXT,created_at TEXT,updated_at TEXT);
                 CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id INTEGER,role TEXT,content TEXT,created_at TEXT);
                """)
        if current and self.database_schema(path) != 46:
            backups = fixture.data / "backups"
            backups.mkdir()
            shutil.copyfile(path, backups / "pre-migration-v23-to-v46-synthetic.db")
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO schema_migrations VALUES(46,'synthetic')")
        return {"passed": True, "binary_sha256": plan.expected_binaries if current else {"desktop": digest(b"older"), "sidecar": digest(b"sidecar")},
                "sidecar_payload_sha256": plan.expected_payload, "installed_sidecar_payload": {"synthetic": True},
                "installed_build_identity": {"identity_mode": "synthetic_test_only",
                    "observation": {"product_version": package.version, **plan.previous_manifest}},
                "actual_run": False}


def test_complete_synthetic_lifecycle_never_claims_real_or_manual_acceptance(plan):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    result = runner.run()
    assert result["status"] == "SYNTHETIC_PASS"
    assert result["actual_run"] is False and result["rc_eligible"] is False
    assert result["report_type"] == "synthetic_installer_lifecycle"
    assert result["manual_desktop_acceptance"] == "NOT_RECORDED"
    assert result["run"]["operator_attested"] is False
    assert runner.index == len(lifecycle.STAGES)
    assert runner.fixture.data.is_dir() and runner.fixture.root.is_dir()
    assert not runner.fixture.install.exists()
    assert len(runner.events) == len(lifecycle.STAGES) * 2


@pytest.mark.parametrize("kind", ["msi", "nsis"])
@pytest.mark.parametrize("quoted", [False, True])
def test_install_location_accepts_only_owned_bare_or_single_outer_quote_pair(plan, kind, quoted):
    package = plan.previous if kind == "msi" else replace(plan.previous, kind="nsis", product_code=None, upgrade_code=None)
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    adapter.product = package
    host = adapter.observe(runner.fixture)
    raw_location = str(runner.fixture.install)
    host["installations"][0]["path"] = f'"{raw_location}"' if quoted else raw_location
    original_observation = json.loads(json.dumps(host))
    lifecycle.validate_host(host, package, runner.fixture, host["shortcuts"])
    assert host == original_observation, "registry observation must remain verbatim"


@pytest.mark.parametrize("kind", ["msi", "nsis"])
@pytest.mark.parametrize("fault", [
    "none", "boolean", "integer", "mapping", "list", "empty", "empty_pair", "relative", "drive_relative",
    "foreign", "owned_traversal_alias", "quoted_relative", "quoted_foreign", "quoted_traversal_alias",
    "opening_only", "closing_only", "extra_pair", "embedded_quote", "argument_suffix", "outside_whitespace",
])
def test_install_location_rejects_invalid_quotes_relative_escape_or_foreign_paths(plan, kind, fault):
    package = plan.previous if kind == "msi" else replace(plan.previous, kind="nsis", product_code=None, upgrade_code=None)
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    adapter.product = package
    host = adapter.observe(runner.fixture)
    owned = str(runner.fixture.install)
    foreign = str(runner.fixture.root / "foreign-install")
    traversal = str(runner.fixture.install / ".." / "install")
    raw = {
        "none": None, "boolean": False, "integer": 17, "mapping": {}, "list": [], "empty": "", "empty_pair": '""',
        "relative": "install", "drive_relative": "C:install", "foreign": foreign, "owned_traversal_alias": traversal,
        "quoted_relative": '"install"', "quoted_foreign": f'"{foreign}"', "quoted_traversal_alias": f'"{traversal}"',
        "opening_only": '"' + owned, "closing_only": owned + '"', "extra_pair": f'""{owned}""',
        "embedded_quote": owned[:1] + '"' + owned[1:], "argument_suffix": f'"{owned}" /S',
        "outside_whitespace": f' "{owned}" ',
    }[fault]
    host["installations"][0]["path"] = raw
    original_observation = json.loads(json.dumps(host))
    with pytest.raises(lifecycle.SafetyError):
        lifecycle.validate_host(host, package, runner.fixture, host["shortcuts"])
    assert host == original_observation


@pytest.mark.parametrize("kind", ["msi", "nsis"])
def test_quoted_install_location_preserves_all_eleven_synthetic_stage_guards(plan, kind):
    selected = plan if kind == "msi" else replace(plan,
        current=replace(plan.current, kind="nsis", product_code=None, upgrade_code=None),
        previous=replace(plan.previous, kind="nsis", product_code=None, upgrade_code=None))
    class QuotedLocationAdapter(SyntheticAdapter):
        def __init__(self, source):
            super().__init__(source)
            self.raw_hosts = []
        def observe(self, fixture):
            value = super().observe(fixture)
            if value["installations"]:
                value["installations"][0]["path"] = f'"{fixture.install}"'
            self.raw_hosts.append(value)
            return value
    adapter = QuotedLocationAdapter(selected.source)
    runner = lifecycle.Lifecycle(selected, adapter)
    result = runner.run()
    assert result["status"] == "SYNTHETIC_PASS" and result["actual_run"] is False and result["rc_eligible"] is False
    assert runner.index == len(lifecycle.STAGES) == 11
    assert len(runner.events) == 22 and not runner.fixture.install.exists()
    assert adapter.invocations == [stage for stage in lifecycle.STAGES if stage not in {"seed_fixture", "verify_retention", "verify_final"}]
    assert all(value["installations"][0]["path"] == f'"{runner.fixture.install}"' for value in adapter.raw_hosts if value["installations"])


def test_quoted_install_location_does_not_bypass_fixture_reparse_guard(plan, monkeypatch):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    original_ordinary = lifecycle.ordinary
    def reject_reparse(path):
        if path == runner.fixture.install:
            raise ValueError("owned acceptance paths cannot contain reparse points")
        return original_ordinary(path)
    monkeypatch.setattr(lifecycle, "ordinary", reject_reparse)
    with pytest.raises(ValueError, match="reparse"):
        runner.transition("install_previous")
    assert adapter.invocations == [] and runner.events == []


def test_unknown_historical_manifest_fields_stay_unknown_but_actual_owned_schema_is_recorded(plan):
    selected = replace(plan, previous_manifest={})
    runner = lifecycle.Lifecycle(selected, SyntheticAdapter(plan.source))
    result = runner.run()
    assert result["status"] == "SYNTHETIC_PASS"
    assert result["artifacts"]["previous_build_manifest"] == {"product_version": "8.0.1", "database_schema_version": 23}
    assert "workspace_state" not in result["artifacts"]["previous_build_manifest"]
    assert "build_type" not in result["artifacts"]["previous_build_manifest"]
    assert result["artifacts"]["previous_schema_observation"] == 23
    assert result["checks"]["seed_fixture"]["result"]["fixture"]["old_schema"] == 23


def test_available_previous_schema_cannot_be_replaced_with_another_observation(plan):
    selected = replace(plan, previous_manifest={"product_version": "8.0.1", "database_schema_version": 24})
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(selected, adapter)
    result = runner.run()
    assert result["status"] == "BLOCKED"
    assert runner.previous_schema is None and runner.seed is None
    assert adapter.invocations == ["install_previous", "launch_previous"]


@pytest.mark.parametrize("stage", ["install_previous", "upgrade_current", "launch_upgraded", "uninstall_current", "reinstall_current", "launch_reinstalled", "final_uninstall"])
def test_failed_or_partial_stage_retains_fixture_and_blocks_automatic_retry(plan, stage):
    adapter = SyntheticAdapter(plan.source, fail_stage=stage)
    runner = lifecycle.Lifecycle(plan, adapter)
    result = runner.run()
    assert result["status"] == "BLOCKED" and result["rc_eligible"] is False
    assert result["fixture_retained"] is True and result["automatic_registry_or_fixture_cleanup"] is False
    assert runner.uncertain is True
    assert runner.fixture.root.is_dir() and runner.fixture.data.is_dir()
    before = list(adapter.invocations)
    with pytest.raises(lifecycle.SafetyError, match="uncertain"):
        runner.transition(lifecycle.STAGES[runner.index])
    assert adapter.invocations == before


@pytest.mark.parametrize("change", ["registration", "family", "shortcut", "running", "busy", "no_webview", "bad_webview", "missing_observation", "not_admin", "legacy_renamed", "legacy_unnamed"])
def test_preflight_conflicts_are_rejected_before_installer_dispatch(plan, change):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    host = adapter.observe(runner.fixture)
    if change == "registration":
        host["installations"] = [{"name": "司忆", "path": "not-owned"}]
    elif change == "family":
        host["related_products"] = [{"product_code": plan.previous.product_code, "upgrade_code": lifecycle.FAMILY,
                                     "path": "not-owned", "version": "8.0.1"}]
    elif change == "shortcut":
        host["shortcuts"] = [{"path": "existing-link", "target": "user-application", "arguments": "", "sha256": "f" * 64}]
    elif change == "running":
        host["running"] = [{"pid": 1, "name": "司忆.exe"}]
    elif change == "busy":
        host["installer_busy"] = True
    elif change == "no_webview":
        host["webviews"] = []
    elif change == "bad_webview":
        host["webviews"] = ["not-a-version"]
    elif change in {"legacy_renamed", "legacy_unnamed"}:
        host["installations"] = [{"registry_key": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Agent",
                                  "name": "OtherApp" if change == "legacy_renamed" else "", "uninstall": "not-owned"}]
    elif change == "not_admin":
        host["is_admin"] = False
    else:
        del host["related_products"]
    adapter.override_host = host
    report = runner.run()
    assert report["status"] == "BLOCKED"
    assert adapter.invocations == []
    assert report["actual_run"] is False


def test_marker_tamper_and_reparse_owner_are_not_accepted(plan):
    runner = lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    (runner.fixture.root / "owner.json").write_text("{}", encoding="utf-8")
    with pytest.raises(lifecycle.SafetyError, match="marker"):
        runner.transition("install_previous")


def test_artifact_mutation_before_next_stage_never_runs_dispatch(plan):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    plan.current.artifact.path.write_bytes(b"replaced")
    with pytest.raises(lifecycle.SafetyError, match="identity"):
        runner.transition("install_previous")
    assert adapter.invocations == []


def test_existing_output_or_owner_namespace_never_overwritten(plan):
    first = lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    with pytest.raises(FileExistsError):
        lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    assert (first.fixture.root / "owner.json").is_file()


def test_out_of_order_repeated_and_concurrent_transition_rejected(plan):
    runner = lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    with pytest.raises(lifecycle.SafetyError, match="out-of-order"):
        runner.transition("upgrade_current")
    runner.lock.acquire()
    try:
        with pytest.raises(lifecycle.SafetyError, match="concurrent"):
            runner.transition("install_previous")
    finally:
        runner.lock.release()
    runner.transition("install_previous")
    with pytest.raises(lifecycle.SafetyError, match="repeated"):
        runner.transition("install_previous")


def test_intent_persistence_failure_prevents_dispatch(plan, monkeypatch):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    def fail(value):
        raise OSError("synthetic storage fault")
    monkeypatch.setattr(runner, "event", fail)
    result = runner.run()
    assert result["status"] == "BLOCKED" and adapter.invocations == []


def test_postinstall_product_code_drift_is_uncertain_not_auto_removed(plan, monkeypatch):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    original = adapter.install
    def drift(package, fixture, **keywords):
        result = original(package, fixture, **keywords)
        adapter.product = replace(package, product_code="{CCCCCCCC-1111-2222-3333-444444444444}")
        return result
    monkeypatch.setattr(adapter, "install", drift)
    result = runner.run()
    assert result["status"] == "BLOCKED" and runner.fixture.install.is_dir()
    assert adapter.invocations == ["install_previous"]


@pytest.mark.parametrize("missing", ["model", "message"])
def test_missing_model_or_message_fails_retention(plan, missing):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    for stage in lifecycle.STAGES[:5]:
        runner.transition(stage)
    if missing == "model":
        path = runner.fixture.data / "voice/models/installer-fixture/sentinel.txt"
        path.write_bytes(b"corrupt")
    else:
        with sqlite3.connect(runner.fixture.data / "data/agent.db") as db:
            db.execute("DELETE FROM messages")
    with pytest.raises(lifecycle.SafetyError, match=missing):
        adapter.verify_fixture(runner.fixture, runner.seed, 46, require_backup=True)


def test_missing_backup_is_not_schema_migration_pass(plan):
    adapter = SyntheticAdapter(plan.source)
    runner = lifecycle.Lifecycle(plan, adapter)
    for stage in lifecycle.STAGES[:5]:
        runner.transition(stage)
    backup = runner.fixture.data / "backups/pre-migration-v23-to-v46-synthetic.db"
    backup.rename(backup.with_suffix(".retained-not-a-backup"))
    with pytest.raises(lifecycle.SafetyError, match="backup"):
        adapter.verify_fixture(runner.fixture, runner.seed, 46, require_backup=True)


def test_nsis_full_uninstaller_tamper_prevents_removal(plan):
    current = replace(plan.current, kind="nsis", product_code=None, upgrade_code=None)
    previous = replace(plan.previous, kind="nsis", product_code=None, upgrade_code=None)
    runner = lifecycle.Lifecycle(replace(plan, current=current, previous=previous), SyntheticAdapter(plan.source))
    # The injected installer is synthetic; it does not execute the .msi fixture.
    runner.transition("install_previous")
    (runner.fixture.install / "uninstall.exe").write_bytes(b"MZtampered")
    with pytest.raises(lifecycle.SafetyError, match="uninstaller"):
        runner.transition("launch_previous")


@pytest.mark.parametrize("change", ["forced_termination", "active_before_cleanup", "active_after_cleanup", "errors", "job_handle_closed", "unassigned_cleanup_complete", "owned_process_handles_remaining", "owned_thread_handles_remaining", "launch_cleanup_errors", "protocol_version"])
def test_native_cleanup_unknown_or_forced_never_passes(change):
    good = {"protocol_version": "exact-native-job-v1", "active_before_cleanup": 0, "active_after_cleanup": 0,
            "forced_termination": False, "job_handle_closed": True, "errors": [], "unassigned_cleanup_complete": True,
            "owned_process_handles_remaining": 0, "owned_thread_handles_remaining": 0, "launch_cleanup_errors": []}
    assert lifecycle.clean_exit(good)
    good[change] = None
    assert not lifecycle.clean_exit(good)


def test_argv_dispatcher_is_owned_native_no_legacy_or_global_cleanup():
    text = (ROOT / "scripts/rc_installed_lifecycle.py").read_text(encoding="utf-8")
    assert "subprocess.Popen" not in text and "taskkill" not in text and "Win32_Product" not in text
    assert "-Verb RunAs" not in text and "DeveloperMode" not in text
    assert "shutil.rmtree" not in text and "DeleteRegKey" not in text


def test_empty_audit_boolean_or_template_hash_does_not_authorize_package(plan):
    relative = Path("build/candidates/test/package.msi")
    artifact_path = plan.root / relative
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"synthetic")
    package = lifecycle.Artifact(artifact_path, lifecycle.sha(artifact_path))
    audit_file = save(plan.root / "build/candidates/test/audit.json", {"actual_run": True, "no_download_verified": True, "template_sha256": "f" * 64})
    with pytest.raises(lifecycle.SafetyError, match="audit"):
        lifecycle.validate_build_audit(plan.root, package, plan.manifest, audit_file.reference(plan.root))


def test_render_receipt_must_be_actual_this_launch_and_all_three_components():
    manifest = {"manifest_version": 1, "product_version": "16.0.0", "git_commit": "a" * 40,
                "source_fingerprint": "b" * 64, "build_id": "c" * 24, "database_schema_version": 46,
                "component_build_ids": {"react": "r", "tauri": "t", "sidecar": "s"},
                "workspace_state": "CLEAN", "build_type": "Release"}
    receipt = {"report_type": "rc_desktop_runtime_observation", "schema_version": 1,
               "protocol_version": "desktop-render-ready-v1", "actual_run": True, "status": "PASS",
               "acceptance_nonce": "nonce", "isolated_test_data": True, "desktop_render_ready": True,
               "sidecar_ready": True, "process_ids": {"desktop": 123, "sidecar": 456},
               "window_handle": 789, "readiness_ms": 10,
               "components": {name: dict(manifest) for name in ("react", "tauri", "sidecar")}}
    lifecycle.WindowsAdapter.validate_render_receipt(receipt, "nonce", 123, manifest)
    receipt["components"]["react"]["build_id"] = "different"
    with pytest.raises(lifecycle.SafetyError, match="components"):
        lifecycle.WindowsAdapter.validate_render_receipt(receipt, "nonce", 123, manifest)


@pytest.mark.parametrize("command,approved", [
    (["powershell.exe", "-NoProfile", "-File", "scripts/build-desktop.ps1", "-Bundle"], True),
    (["node.exe", "node_modules/@tauri-apps/cli/tauri.js", "build"], True),
    (["tauri.exe", "build"], True),
    (["unreviewed.exe", "build-desktop.ps1", "build"], False),
    (["powershell.exe", "-Command", "scripts/build-desktop.ps1"], False),
    (["powershell.exe", "-EncodedCommand", "build-desktop.ps1"], False),
    (["node.exe", "unreviewed.js", "tauri.js", "build"], False),
    (["tauri.exe", "dev", "build"], False),
])
def test_build_argv_is_a_direct_reviewed_entry_not_buried_name(command, approved):
    assert lifecycle.approved_build_argv(command) is approved


def build_audit_fixture(root):
    directory = root / "build/candidates/test"
    directory.mkdir(parents=True)
    package_path = directory / "package.msi"
    package_path.write_bytes(b"synthetic-package")
    package = lifecycle.Artifact(package_path, lifecycle.sha(package_path))
    manifest = {"product_version": "16.0.0", "git_commit": "a" * 40, "source_fingerprint": "b" * 64,
                "workspace_state": "CLEAN", "build_type": "Release"}
    source = {"source_version": "16.0.0", "source_commit": "a" * 40, "source_tree_fingerprint": "b" * 64,
              "workspace_clean": True}
    config = save(directory / "config.json", {"bundle": {"windows": {"webviewInstallMode": {"type": "skip"}}}})
    manifest_file = save(directory / "manifest.json", manifest)
    raw_log = directory / "raw-private.log"
    raw_log.write_bytes(b"synthetic unit fixture, NOT formal build evidence")
    tool = directory / "synthetic-tool.exe"
    tool.write_bytes(b"synthetic tool identity")
    tools = [{"name": "synthetic", "version": "unit-test", "artifact": lifecycle.Artifact(tool, lifecycle.sha(tool)).reference(root)}]
    inputs = {}
    for name in ("entrypoint", "tauri_config", "tauri_lock", "node_lock", "python_lock", "installer_template"):
        item = directory / (name + ".snapshot")
        item.write_bytes(("synthetic-" + name).encode())
        inputs[name] = lifecycle.Artifact(item, lifecycle.sha(item)).reference(root)
    command = ["powershell.exe", "-NoProfile", "-File", "scripts/build-desktop.ps1", "-Bundle"]
    execution = {"schema_version": 1, "report_type": "installer_build_execution", "actual_run": True,
        "command": command, "source": source, "source_after": source, "package": package.reference(root),
        "effective_config": config.reference(root), "exit_code": 0, "timed_out": False,
        "raw_log": lifecycle.Artifact(raw_log, lifecycle.sha(raw_log)).reference(root), "toolchain": tools, "inputs": inputs}
    execution_file = save(directory / "execution.json", execution)
    audit = {"schema_version": 1, "report_type": "installer_build_audit", "protocol": "no-bootstrap-build-v1",
        "actual_run": True, "status": "PASS", "source": source, "source_after": source,
        "package": package.reference(root), "effective_config": config.reference(root),
        "build_manifest": manifest_file.reference(root), "execution": execution_file.reference(root),
        "command": command, "cwd": ".", "exit_code": 0, "timed_out": False, "toolchain": tools, "inputs": inputs}
    audit_file = save(directory / "audit.json", audit)
    return package, manifest, audit, execution, audit_file


def test_complete_build_audit_cross_binds_every_actual_attachment(tmp_path):
    package, manifest, audit, execution, file = build_audit_fixture(tmp_path)
    pins = lifecycle.validate_build_audit(tmp_path, package, manifest, file.reference(tmp_path))
    assert len(pins) == 12
    for pin in pins:
        pin.verify()


def test_missing_new_build_audit_is_optional_not_a_requirement_to_rebuild_old_official_bytes(tmp_path):
    package, manifest, *_ = build_audit_fixture(tmp_path)
    assert lifecycle.validate_build_audit(tmp_path, package, manifest, None) == ()


@pytest.mark.parametrize("fault", ["source", "after", "package", "config", "argv", "raw_argv", "toolchain", "inputs", "missing_log", "tampered_tool", "tampered_input"])
def test_build_audit_incomplete_or_disagreeing_bytes_never_admit(tmp_path, fault):
    package, manifest, audit, execution, file = build_audit_fixture(tmp_path)
    if fault == "source": audit["source"]["source_commit"] = "f" * 40
    elif fault == "after": audit["source_after"] = {**audit["source_after"], "workspace_clean": False}
    elif fault == "package": audit["package"]["sha256"] = "f" * 64
    elif fault == "config":
        path = tmp_path / audit["effective_config"]["path"]
        audit["effective_config"] = save(path, {"bundle": {"windows": {"webviewInstallMode": {"type": "downloadBootstrapper"}}}}).reference(tmp_path)
    elif fault == "argv": audit["command"] = ["unreviewed.exe", "build-desktop.ps1", "build"]
    elif fault == "raw_argv": execution["command"] = ["tauri.exe", "dev"]
    elif fault == "toolchain": execution["toolchain"] = []
    elif fault == "inputs": execution["inputs"] = {}
    elif fault == "missing_log": (tmp_path / execution["raw_log"]["path"]).write_bytes(b"")
    elif fault == "tampered_tool": (tmp_path / audit["toolchain"][0]["artifact"]["path"]).write_bytes(b"changed")
    else: (tmp_path / audit["inputs"]["installer_template"]["path"]).write_bytes(b"changed")
    # Rebinding the outer JSON does not repair mismatched internal execution evidence.
    audit["execution"] = save(tmp_path / audit["execution"]["path"], execution).reference(tmp_path)
    new_file = save(file.path, audit)
    with pytest.raises(lifecycle.SafetyError):
        lifecycle.validate_build_audit(tmp_path, package, manifest, new_file.reference(tmp_path))


def clean_receipt():
    return {"protocol_version": "exact-native-job-v1", "active_before_cleanup": 0, "active_after_cleanup": 0,
            "forced_termination": False, "job_handle_closed": True, "errors": [], "unassigned_cleanup_complete": True,
            "owned_process_handles_remaining": 0, "owned_thread_handles_remaining": 0, "launch_cleanup_errors": [],
            "launch_events": ["created_suspended", "resumed"]}


@pytest.mark.parametrize("fault", ["none", "launch_before_resume", "launch_after_resume", "timeout", "forced", "unknown", "cleanup_error"])
def test_owned_dispatcher_retains_true_resume_and_cleanup_on_all_failures(tmp_path, fault):
    class Process:
        creation_time = 123
        def wait(self, timeout):
            if fault == "timeout": raise TimeoutError("synthetic timeout")
            return 0
    class Job:
        launch_events = []
        def launch(self, executable, environment, arguments, **options):
            self.launch_events = ["created_suspended"] if fault == "launch_before_resume" else ["created_suspended", "resumed"]
            if fault in {"launch_before_resume", "launch_after_resume"}: raise RuntimeError("synthetic late launch failure")
            return Process()
        def wait_empty(self, timeout): return None
        def cleanup(self):
            if fault == "cleanup_error": raise OSError("synthetic retained cleanup failure")
            value = clean_receipt()
            if fault == "launch_before_resume": value["launch_events"] = ["created_suspended"]
            if fault == "forced": value["forced_termination"] = True
            if fault == "unknown": value["active_after_cleanup"] = None
            return value
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    adapter.probes = tmp_path
    adapter.job_factory = Job
    adapter.native_hashes = {}
    adapter.mutation_started = False
    exe = tmp_path / "synthetic.exe"
    exe.write_bytes(b"MZsynthetic-never-executed")
    if fault == "none": assert adapter.owned_command(exe, [], {}, mutation=True)["passed"] is True
    else:
        with pytest.raises((RuntimeError, TimeoutError, lifecycle.SafetyError)):
            adapter.owned_command(exe, [], {}, mutation=True)
    assert adapter.mutation_started is (fault != "launch_before_resume")
    receipts = list(tmp_path.glob("owned-command-*.json"))
    assert len(receipts) == 1
    retained = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert retained["actual_run"] is (fault != "launch_before_resume")
    assert retained["process_cleanup"]["protocol_version"] == "exact-native-job-v1"


@pytest.mark.parametrize("field", ["active_before_cleanup", "active_after_cleanup"])
def test_boolean_zero_is_not_typed_job_accounting(field):
    receipt = clean_receipt()
    receipt[field] = False
    assert not lifecycle.clean_exit(receipt)


def msi_fixture():
    return {"database_open_mode": 0, "tables": ["Property", "Directory", "Component", "File", "Media", "Registry", "Shortcut", "RemoveFile"],
        "properties": {"ProductName": "司忆"}, "custom_actions": [],
        "directories": [{"id": "TARGETDIR", "parent": "", "name": "SourceDir"},
                        {"id": "ProgramFiles64Folder", "parent": "TARGETDIR", "name": "PFiles"},
                        {"id": "INSTALLDIR", "parent": "ProgramFiles64Folder", "name": "司忆"},
                        {"id": "payload", "parent": "INSTALLDIR", "name": "_internal"}],
        "components": [{"id": "Path", "directory": "INSTALLDIR"}, {"id": "payload", "directory": "payload"}],
        "files": [{"id": "Path", "component": "Path", "name": "司忆.exe"}, {"id": "payload", "component": "payload", "name": "BUILDI~1.JSO|build-info.json"}],
        "registry": [{"component": "Path", "root": 1, "key": r"Software\github\司忆", "name": "InstallDir", "value": "[INSTALLDIR]"}],
        "shortcuts": [], "remove_files": [], "media": [{"cabinet": "#app.cab", "source": ""}],
        "upgrades": [], "reg_locators": [], "app_search": [], "signatures": [], "execute_sequence": []}


def legacy_msi_fixture():
    value = msi_fixture()
    value["custom_actions"] = [
        {"name": "LaunchApplication", "type": 210, "source": "Path", "target": "[LAUNCHAPPARGS]"},
        {"name": "WixUIValidatePath", "type": 65, "source": "WixUIWixca", "target": "ValidatePath"},
        {"name": "WixUIPrintEula", "type": 65, "source": "WixUIWixca", "target": "PrintEula"},
        {"name": "DownloadAndInvokeBootstrapper", "type": 1058, "source": "INSTALLDIR", "target": lifecycle.MSI_BOOTSTRAP_TARGET}]
    value["execute_sequence"] = [
        {"action": "AppSearch", "condition": "", "sequence": 50},
        {"action": "LaunchApplication", "condition": "AUTOLAUNCHAPP AND NOT Installed", "sequence": 6601},
        {"action": "DownloadAndInvokeBootstrapper", "condition": "NOT(REMOVE OR INSTALLED_WEBVIEW2_VERSION)", "sequence": 6599}]
    for identifier, root, prefix in (("Webview2VersionSystemx64", 2, "SOFTWARE\\WOW6432Node"),
                                     ("Webview2VersionSystemx86", 2, "SOFTWARE"),
                                     ("Webview2VersionUser", 1, "SOFTWARE")):
        value["reg_locators"].append({"id": identifier, "root": root,
            "key": prefix + r"\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "name": "pv"})
        value["app_search"].append({"property": "INSTALLED_WEBVIEW2_VERSION", "signature": identifier})
    return value


def create_folder_msi_fixture():
    value = msi_fixture()
    value["tables"].append("CreateFolder")
    value["create_folders"] = [{"directory": "payload", "component": "payload"}]
    return value


@pytest.mark.parametrize("directory,component", [
    ("INSTALLDIR", "Path"), ("payload", "payload"),
    ("DesktopFolder", "ApplicationShortcutDesktop"),
    ("ApplicationProgramsFolder", "ApplicationShortcut"),
])
def test_reviewed_sdk_create_folders_bind_exact_component_and_directory(directory, component):
    value = create_folder_msi_fixture()
    if directory == "DesktopFolder":
        value["directories"].append({"id": directory, "parent": "TARGETDIR", "name": "Desktop"})
    elif directory == "ApplicationProgramsFolder":
        value["directories"].extend([
            {"id": "ProgramMenuFolder", "parent": "TARGETDIR", "name": "."},
            {"id": directory, "parent": "ProgramMenuFolder", "name": "司忆"},
        ])
    if component.startswith("ApplicationShortcut"):
        value["components"].append({"id": component, "directory": directory})
        value["shortcuts"].append({"component": component, "directory": directory,
            "name": "司忆", "target": "[!Path]", "arguments": ""})
    value["create_folders"] = [{"directory": directory, "component": component}]
    assert lifecycle.validate_msi_metadata(value)["ProductName"] == "司忆"


@pytest.mark.parametrize("fault", [
    "missing_inventory", "null_inventory", "non_list", "null_row", "missing_directory", "missing_component",
    "extra_field", "boolean_directory", "null_component", "integer_component", "empty_directory",
    "long_identifier", "unhashable_identifier", "unknown_component", "unknown_directory", "directory_mismatch",
    "outside_directory", "foreign_shortcut_directory", "wrong_shortcut_component", "duplicate", "over_bound",
    "rows_without_table",
])
def test_create_folder_inventory_is_complete_typed_bounded_and_owned(fault):
    value = create_folder_msi_fixture()
    rows = value["create_folders"]
    if fault == "missing_inventory": del value["create_folders"]
    elif fault == "null_inventory": value["create_folders"] = None
    elif fault == "non_list": value["create_folders"] = {}
    elif fault == "null_row": rows[0] = None
    elif fault == "missing_directory": del rows[0]["directory"]
    elif fault == "missing_component": del rows[0]["component"]
    elif fault == "extra_field": rows[0]["unreviewed"] = "hidden-reference"
    elif fault == "boolean_directory": rows[0]["directory"] = True
    elif fault == "null_component": rows[0]["component"] = None
    elif fault == "integer_component": rows[0]["component"] = 1
    elif fault == "empty_directory": rows[0]["directory"] = ""
    elif fault == "long_identifier": rows[0]["directory"] = "x" * 73
    elif fault == "unhashable_identifier": rows[0]["component"] = []
    elif fault == "unknown_component": rows[0]["component"] = "unknown"
    elif fault == "unknown_directory": rows[0]["directory"] = "unknown"
    elif fault == "directory_mismatch": rows[0]["directory"] = "INSTALLDIR"
    elif fault == "outside_directory":
        value["directories"].append({"id": "foreign", "parent": "TARGETDIR", "name": "outside"})
        rows[0]["directory"] = "foreign"
    elif fault == "foreign_shortcut_directory":
        value["directories"].extend([
            {"id": "ProgramMenuFolder", "parent": "TARGETDIR", "name": "."},
            {"id": "ApplicationProgramsFolder", "parent": "ProgramMenuFolder", "name": "OtherApp"},
        ])
        value["components"].append({"id": "ApplicationShortcut", "directory": "ApplicationProgramsFolder"})
        rows[0].update(directory="ApplicationProgramsFolder", component="ApplicationShortcut")
    elif fault == "wrong_shortcut_component":
        value["directories"].append({"id": "DesktopFolder", "parent": "TARGETDIR", "name": "Desktop"})
        rows[0].update(directory="DesktopFolder", component="payload")
    elif fault == "duplicate": rows.append(dict(rows[0]))
    elif fault == "over_bound": value["create_folders"] = [dict(rows[0]) for _ in range(32769)]
    elif fault == "rows_without_table": value["tables"].remove("CreateFolder")
    with pytest.raises(lifecycle.SafetyError):
        lifecycle.validate_msi_metadata(value)


@pytest.mark.parametrize("inventory", ["absent", "empty"])
def test_old_msi_without_create_folder_table_only_allows_absent_or_empty_inventory(inventory):
    value = msi_fixture()
    if inventory == "empty": value["create_folders"] = []
    assert lifecycle.validate_msi_metadata(value)["ProductName"] == "司忆"


def test_create_folder_metadata_reads_actual_directory_and_component_columns_readonly():
    assert "$database=$installer.OpenDatabase($env:SIYI_PACKAGE,0)" in lifecycle.MSI_METADATA
    assert "create_folders=@(Read-MsiRows 'CreateFolder' @('Directory_','Component_') @('directory','component') @())" in lifecycle.MSI_METADATA


@pytest.mark.parametrize("name", [
    "ra-rqmg4.xml|[Content_Types].xml", "DATA~1.TXT|Project [Status].txt", "LICENSE", "FILE.TXT",
    "SIYI~1.TXT|中文 文件.txt", "CONFIG~1|.config", "COM0.TXT", "COM10.TXT",
    "DATA~1.TXT|long+comma,semi;equal=.txt", "LONG~1.TXT|" + "x" * 255,
])
def test_msi_file_literal_long_name_accepts_brackets_without_relaxing_other_fields(name):
    value = msi_fixture()
    value["files"][1]["name"] = name
    assert lifecycle.validate_msi_metadata(value)["ProductName"] == "司忆"


@pytest.mark.parametrize("name", [
    None, False, 17, {}, [], "", ".", "..", "../out.txt", r"..\out.txt", "C:" + chr(92) + "out.txt",
    "C:out.txt", r"\\host\share\out.txt", "FILE.TXT:stream", "A*.TXT", "A?.TXT", 'A".TXT',
    "A<.TXT", "A>.TXT", "SHORT.TXT|long|tail", "SHORT.TXT|../out.txt", "|long.txt", "SHORT.TXT|",
    "SHORT.TXT|tail.", "SHORT.TXT|tail ", "SHORT.TXT|tail\x00.txt", "SHORT.TXT|tail\n.txt",
    "SHORT.TXT|tail\x7f.txt", "SHORT.TXT|" + "x" * 256, "SHORT.TXT|" + "😀" * 128,
    "A+.TXT|long.txt", "A,.TXT|long.txt", "A;.TXT|long.txt", "A=.TXT|long.txt", "[A].TXT|long.txt",
    "A B.TXT|long.txt", "A .TXT|long.txt", "NINECHARS.TXT|long.txt", "FILE.LONG|long.txt",
    "A.B.TXT|long.txt", "CON", "nul.txt", "COM1.TXT", "LPT9", "SHORT.TXT|AUX.tar.gz",
    "SHORT.TXT|con .txt", "SHORT.TXT|COM¹.txt", "SHORT.TXT|LPT³.txt",
])
def test_msi_file_literal_rejects_types_escape_stream_syntax_and_reserved_devices(name):
    value = msi_fixture()
    value["files"][1]["name"] = name
    with pytest.raises(lifecycle.SafetyError):
        lifecycle.validate_msi_metadata(value)


@pytest.mark.parametrize("code", range(32))
def test_msi_file_literal_never_accepts_ascii_control_characters(code):
    value = msi_fixture()
    value["files"][1]["name"] = "SHORT.TXT|bad" + chr(code) + ".txt"
    with pytest.raises(lifecycle.SafetyError):
        lifecycle.validate_msi_metadata(value)


def test_msi_file_literal_requires_name_inventory_and_preserves_directory_bracket_policy():
    value = msi_fixture()
    del value["files"][1]["name"]
    with pytest.raises(lifecycle.SafetyError):
        lifecycle.validate_msi_metadata(value)
    value = msi_fixture()
    value["files"][1]["name"] = "ra-rqmg4.xml|[Content_Types].xml"
    value["directories"][-1]["name"] = "[UnreviewedProperty]"
    with pytest.raises(lifecycle.SafetyError):
        lifecycle.validate_msi_metadata(value)


@pytest.mark.parametrize("fault", ["none", "unknown_target", "unconditional_bootstrap", "unconditional_launch", "default_launch", "ui_in_execute", "missing_schedule", "missing_webview_search", "search_after_bootstrap", "conditional_search"])
def test_official_legacy_msi_actions_only_admitted_under_actual_disabled_silent_conditions(fault):
    value = legacy_msi_fixture()
    if fault == "unknown_target": value["custom_actions"][-1]["target"] += " ; unreviewed"
    elif fault == "unconditional_bootstrap": value["execute_sequence"][-1]["condition"] = "1"
    elif fault == "unconditional_launch": value["execute_sequence"][1]["condition"] = "1"
    elif fault == "default_launch": value["properties"]["AUTOLAUNCHAPP"] = "1"
    elif fault == "ui_in_execute": value["execute_sequence"].append({"action": "WixUIValidatePath", "condition": "1", "sequence": 200})
    elif fault == "missing_schedule": value["execute_sequence"].pop()
    elif fault == "missing_webview_search": value["app_search"].pop()
    elif fault == "search_after_bootstrap": value["execute_sequence"][0]["sequence"] = 6600
    elif fault == "conditional_search": value["execute_sequence"][0]["condition"] = "NOT Installed"
    if fault == "none": assert lifecycle.validate_msi_metadata(value)["ProductName"] == "司忆"
    else:
        with pytest.raises(lifecycle.SafetyError): lifecycle.validate_msi_metadata(value)


@pytest.mark.parametrize("fault", ["none", "service", "unknown_action", "outside_component", "registry", "file_escape", "remove_desktop", "external_media", "other_family", "unknown_search", "foreign_program_folder", "directory_property", "missing_shortcut_file", "app_search_override", "file_search"])
def test_complete_msi_static_mutation_policy_blocks_unreviewed_authored_effects(fault):
    value = msi_fixture()
    if fault == "service": value["tables"].append("ServiceInstall")
    elif fault == "unknown_action": value["custom_actions"] = [{"name": "LaunchApplication", "type": 210, "source": "Path", "target": ""}]
    elif fault == "outside_component": value["components"][0]["directory"] = "DesktopFolder"
    elif fault == "registry": value["registry"][0]["key"] = r"Software\UnrelatedApp"
    elif fault == "file_escape": value["files"][0]["name"] = "../outside.exe"
    elif fault == "remove_desktop": value["remove_files"] = [{"component": "Path", "filename": "", "directory": "DesktopFolder", "mode": 2}]
    elif fault == "external_media": value["media"][0]["cabinet"] = "external.cab"
    elif fault == "other_family": value["upgrades"] = [{"upgrade_code": "{AAAAAAAA-1111-2222-3333-444444444444}"}]
    elif fault == "unknown_search": value["reg_locators"] = [{"root": 1, "key": r"Software\UserSecret", "name": "Password"}]
    elif fault == "app_search_override":
        value["reg_locators"] = [{"id": "PrevInstallDirNoName", "root": 1, "key": r"Software\github\司忆", "name": ""}]
        value["app_search"] = [{"property": "DesktopFolder", "signature": "PrevInstallDirNoName"}]
    elif fault == "file_search": value["signatures"] = [{"id": "PrevInstallDirNoName"}]
    elif fault == "directory_property": value["properties"]["INSTALLDIR"] = "foreign"
    elif fault in {"foreign_program_folder", "missing_shortcut_file"}:
        value["directories"].extend([{"id": "ProgramMenuFolder", "parent": "TARGETDIR", "name": "."},
            {"id": "ApplicationProgramsFolder", "parent": "ProgramMenuFolder", "name": "司忆"}])
        value["components"].append({"id": "ApplicationShortcut", "directory": "ApplicationProgramsFolder"})
        value["shortcuts"].append({"component": "ApplicationShortcut", "directory": "ApplicationProgramsFolder", "name": "司忆", "target": "[!Path]", "arguments": ""})
        if fault == "foreign_program_folder":
            value["directories"][-1].update(parent="DesktopFolder", name="ForeignExistingFolder")
        else: value["files"] = []
    if fault == "none": assert lifecycle.validate_msi_metadata(value)["ProductName"] == "司忆"
    else:
        with pytest.raises(lifecycle.SafetyError): lifecycle.validate_msi_metadata(value)


def test_cli_final_report_storage_failure_does_not_deny_prior_owned_effect(plan, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("installed_lifecycle_cli", ROOT / "scripts/record-rc-installed-lifecycle.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    adapter = SyntheticAdapter(plan.source)
    original_install = adapter.install
    def install(package, fixture, **arguments):
        adapter.mutation_started = True
        return original_install(package, fixture, **arguments)
    adapter.install = install
    monkeypatch.setattr(cli, "WindowsAdapter", lambda *args: adapter)
    monkeypatch.setattr(cli, "load_module", lambda *args: SimpleNamespace(_release_source_identity=lambda root: adapter.source))
    monkeypatch.setattr(cli, "prepare", lambda *args: plan)
    request = save(plan.root / "private-request.json", {"output": str(plan.output), "test_boundary": str(plan.root)})
    original_write = lifecycle.write_once
    def write(path, value):
        if path == plan.output: raise OSError("private-user-path-must-not-be-printed")
        return original_write(path, value)
    monkeypatch.setattr(lifecycle, "write_once", write)
    assert cli.main(["--request", str(request.path), "--execute"]) == 1
    console = json.loads(capsys.readouterr().out.strip())
    assert console["actual_run"] is True and console["rc_eligible"] is False
    assert console["state"] == "UNCERTAIN_OPERATOR_RECOVERY"
    assert console["fixture_retained"] is True and Path(console["fixture"]).is_dir()
    assert "private-user-path-must-not-be-printed" not in console["detail"]


@pytest.mark.parametrize("desktop_code,sidecar_code", [(0, 0), (1, 0), (0, 1), (None, 0), (0, None)])
def test_installed_launch_requires_actual_retained_normal_exit_codes(plan, monkeypatch, desktop_code, sidecar_code):
    runner = lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    fixture = runner.fixture
    SyntheticAdapter(plan.source).install(plan.current, fixture, remove=False, uninstaller_sha=None, stage="synthetic-install")
    manifest = {**plan.manifest, "workspace_state": "CLEAN", "build_type": "Release", "manifest_version": 1,
                "git_commit": "a" * 40, "source_fingerprint": "b" * 64,
                "component_build_ids": {"react": "r", "tauri": "t", "sidecar": "s"}}
    selected = replace(plan, manifest=manifest)
    save(fixture.install / "_internal/build-info.json", manifest)
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    adapter.root = plan.root
    adapter.mutation_started = False
    adapter.environment = lambda data, nonce: {}
    class Process:
        pid = 123
        def poll(self): return None
        def wait(self, timeout): return desktop_code
    class Sidecar:
        def wait(self, timeout): return sidecar_code
    class Job:
        launch_events = ["created_suspended", "resumed"]
        def launch(self, exe, environment, **options):
            bridge = lifecycle.read_json(fixture.data / "rc-acceptance-owner.json")
            receipt = {"report_type": "rc_desktop_runtime_observation", "schema_version": 1,
                "protocol_version": "desktop-render-ready-v1", "actual_run": True, "status": "PASS",
                "acceptance_nonce": bridge["acceptance_nonce"], "isolated_test_data": True,
                "desktop_render_ready": True, "sidecar_ready": True, "process_ids": {"desktop": 123, "sidecar": 456},
                "window_handle": 789, "readiness_ms": 10, "components": {name: dict(manifest) for name in ("react", "tauri", "sidecar")}}
            lifecycle.write_once(fixture.data / "rc-desktop-observation.json", receipt)
            return Process()
        def observe_sidecar(self, pid, expected):
            assert pid == 456 and expected == fixture.install / "agent-backend.exe"
            return Sidecar()
        def wait_empty(self, timeout): return None
        def cleanup(self): return clean_receipt()
    adapter.job_factory = Job
    inventory = SimpleNamespace(inventory=lambda *args: {"payload_content_sha256": plan.expected_payload})
    close = SimpleNamespace(close_owned_desktop=lambda process, hwnd: None)
    monkeypatch.setattr(lifecycle, "load_module", lambda root, name: inventory if name == "rc_payload_inventory" else close)
    if desktop_code == 0 and sidecar_code == 0:
        assert adapter.launch(plan.current, fixture, current=True, plan=selected, stage="synthetic-launch")["passed"] is True
    else:
        with pytest.raises(lifecycle.SafetyError, match="exit normally"):
            adapter.launch(plan.current, fixture, current=True, plan=selected, stage="synthetic-launch")
        assert not list((fixture.root / "launches").glob("*/completion.json"))
    assert len(list((fixture.root / "launches").glob("*/cleanup.json"))) == 1


@pytest.fixture(params=["msi", "nsis"])
def readonly_request(tmp_path, monkeypatch, request):
    kind = request.param
    candidate_dir = tmp_path / "build/candidates/only-synthetic"
    previous_dir = tmp_path / "build/upgrade-baseline/only-synthetic"
    candidate_dir.mkdir(parents=True)
    previous_dir.mkdir(parents=True)
    suffix = ".msi" if kind == "msi" else ".exe"
    current = candidate_dir / ("candidate" + suffix)
    previous = previous_dir / ("official-previous" + suffix)
    current.write_bytes(b"synthetic-current" + b"0" * (1024 * 1024))
    previous.write_bytes(b"synthetic-previous" + b"0" * (1024 * 1024))
    source = {"source_version": "16.0.0", "source_commit": "a" * 40, "workspace_clean": True,
              "source_tree_fingerprint": "b" * 64}
    manifest = {"product_version": "16.0.0", "workspace_state": "CLEAN", "build_type": "Release",
                "git_commit": source["source_commit"], "source_fingerprint": source["source_tree_fingerprint"],
                "build_id": "c" * 24, "manifest_version": 1, "database_schema_version": 46,
                "component_build_ids": {name: name + "-" + "c" * 24 for name in ("react", "tauri", "sidecar")}}
    desktop, sidecar = candidate_dir / "司忆.exe", candidate_dir / "agent-backend.exe"
    desktop.write_bytes(b"MZsynthetic-__TAURI_BUNDLE_TYPE_VAR_UNK")
    sidecar.write_bytes(b"MZsynthetic-sidecar")
    inventory = {"payload_content_sha256": "d" * 64, "synthetic_only": True}
    ref = lambda path: lifecycle.Artifact(path, lifecycle.sha(path)).reference(tmp_path)
    previous_ref = ref(previous)
    receipt = {"repository": "AureliusWu/Agent", "tag": "v8.0.1", "draft": False, "prerelease": False,
               "assets": [{"kind": kind, **previous_ref, "github_asset_digest": "sha256:" + previous_ref["sha256"]}]}
    boundary = tmp_path / "build/v1600-evidence"
    boundary.mkdir()
    payload = save(candidate_dir / "inventory.json", inventory)
    selected_request = {"schema_version": 1, "kind": kind, "candidate": ref(current), "previous": previous_ref,
        "candidate_manifest": save(candidate_dir / "manifest.json", manifest).reference(tmp_path),
        "previous_release": save(previous_dir / "release-receipt.json", receipt).reference(tmp_path),
        "desktop": ref(desktop), "sidecar": ref(sidecar), "payload": payload.reference(tmp_path),
        "output": str(boundary / "private/lifecycle.json"), "test_boundary": str(boundary)}
    class ReadonlyAdapter:
        def __init__(self): self.observed = []
        def bind_packages(self, values): self.bound = values
        def inspect_package(self, item, selected_kind):
            self.observed.append(item.sha256)
            previous_package = item.sha256 == previous_ref["sha256"]
            return {"product_name": "司忆", "version": "8.0.1" if previous_package else "16.0.0",
                    "publisher": None if kind == "nsis" else "github",
                    "product_code": ("{BBBBBBBB-1111-2222-3333-444444444444}" if previous_package
                        else "{AAAAAAAA-1111-2222-3333-444444444444}") if kind == "msi" else None,
                    "upgrade_code": lifecycle.FAMILY if kind == "msi" else None}
    adapter = ReadonlyAdapter()
    original_load = lifecycle.load_module
    monkeypatch.setattr(lifecycle, "load_module", lambda root, name: SimpleNamespace(inventory=lambda *args: inventory)
                        if name == "rc_payload_inventory" else original_load(root, name))
    return tmp_path, selected_request, adapter, source, manifest, inventory


def test_readonly_plan_admits_actual_official_old_version_without_new_manifest_or_build_receipts(readonly_request):
    root, request, adapter, source, *_ = readonly_request
    selected = lifecycle.prepare(root, request, adapter, source)
    assert selected.previous.version == "8.0.1"
    assert selected.previous_manifest == {}
    assert selected.candidate_sidecar.reference(root) == request["sidecar"]
    assert selected.candidate_payload.reference(root) == request["payload"]
    assert selected.public_output == root / "build/v1600-evidence" / (request["kind"] + "-installer-smoke.json")
    assert len(adapter.observed) == 2
    assert not selected.output.exists() and not selected.output.with_suffix(".run").exists()


@pytest.mark.parametrize("fingerprint,valid", [("B" * 64, True), ("G" * 64, False), ("F" * 63, False), ("A" * 65, False)])
def test_current_manifest_sha_format_accepts_generator_uppercase_hex_only_without_rewriting_identity(readonly_request, fingerprint, valid):
    root, request, adapter, source, manifest, *_ = readonly_request
    manifest["source_fingerprint"] = source["source_tree_fingerprint"] = fingerprint
    request["candidate_manifest"] = save(root / request["candidate_manifest"]["path"], manifest).reference(root)
    if valid:
        selected = lifecycle.prepare(root, request, adapter, source)
        assert selected.manifest["source_fingerprint"] == fingerprint
        assert selected.source["source_tree_fingerprint"] == fingerprint
    else:
        with pytest.raises(lifecycle.SafetyError, match="manifest"):
            lifecycle.prepare(root, request, adapter, source)


def test_manifest_sha_format_normalization_does_not_relax_original_source_identity_equality(readonly_request):
    root, request, adapter, source, manifest, *_ = readonly_request
    manifest["source_fingerprint"] = source["source_tree_fingerprint"].upper()
    request["candidate_manifest"] = save(root / request["candidate_manifest"]["path"], manifest).reference(root)
    with pytest.raises(lifecycle.SafetyError, match="current clean source"):
        lifecycle.prepare(root, request, adapter, source)


@pytest.mark.parametrize("fault", ["candidate_dirty", "source_dirty", "official_digest", "foreign_repository", "duplicate_marker", "tampered_sidecar", "public_output_exists", "payload_drift", "missing_candidate_components", "wrong_candidate_schema"])
def test_legacy_compatibility_never_relaxes_current_source_bytes_payload_or_fresh_public_output(readonly_request, fault, monkeypatch):
    root, request, adapter, source, manifest, inventory = readonly_request
    if fault in {"candidate_dirty", "missing_candidate_components", "wrong_candidate_schema"}:
        if fault == "candidate_dirty": manifest["workspace_state"] = "DIRTY"
        elif fault == "missing_candidate_components": del manifest["component_build_ids"]
        else: manifest["database_schema_version"] = False
        request["candidate_manifest"] = save(root / request["candidate_manifest"]["path"], manifest).reference(root)
    elif fault == "source_dirty": source["workspace_clean"] = False
    elif fault in {"official_digest", "foreign_repository"}:
        receipt = lifecycle.read_json(root / request["previous_release"]["path"])
        if fault == "official_digest": receipt["assets"][0]["github_asset_digest"] = "sha256:" + "f" * 64
        else: receipt["repository"] = "Other/Untrusted"
        request["previous_release"] = save(root / request["previous_release"]["path"], receipt).reference(root)
    elif fault == "duplicate_marker":
        path = root / request["desktop"]["path"]
        path.write_bytes(path.read_bytes() + b"__TAURI_BUNDLE_TYPE_VAR_UNK")
        request["desktop"]["sha256"] = lifecycle.sha(path)
    elif fault == "tampered_sidecar": (root / request["sidecar"]["path"]).write_bytes(b"changed")
    elif fault == "public_output_exists": save(root / "build/v1600-evidence" / (request["kind"] + "-installer-smoke.json"), {"previous": "never overwrite"})
    else:
        monkeypatch.setattr(lifecycle, "load_module", lambda *args: SimpleNamespace(inventory=lambda *args: {**inventory, "payload_content_sha256": "f" * 64}))
    with pytest.raises(lifecycle.SafetyError): lifecycle.prepare(root, request, adapter, source)
    assert not Path(request["output"]).with_suffix(".run").exists()


def test_old_onefile_reuses_bounded_reader_and_does_not_invent_current_manifest_fields(plan, monkeypatch):
    sidecar = plan.root / "legacy-install/agent-backend.exe"
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"synthetic-onefile-never-launched")
    observed = {"archive_entry": "build-info.json", "embedded_manifest_bytes": 150,
                "embedded_manifest_sha256": "D" * 64, "product_version": "8.0.1",
                "git_commit": None, "source_fingerprint": None, "workspace_state": None, "build_id": None,
                "component_build_id": None}
    def load(root, name):
        assert name == "read-pyinstaller-build-info", "old onefile must not invoke current inventory"
        return SimpleNamespace(extract_embedded_build_info=lambda path: dict(observed))
    monkeypatch.setattr(lifecycle, "load_module", load)
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    adapter.root = plan.root
    identity = adapter.previous_identity(sidecar, plan.previous, {})
    assert identity["identity_mode"] == "legacy_onefile_embedded_manifest"
    assert identity["observation"] == observed
    assert identity["current_candidate_manifest_qualification"] is False
    with pytest.raises(lifecycle.SafetyError, match="historical identity"):
        adapter.previous_identity(sidecar, plan.previous, {"git_commit": "a" * 40})


def test_current_package_missing_onedir_manifest_cannot_use_legacy_reader(plan, monkeypatch):
    runner = lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    SyntheticAdapter(plan.source).install(plan.current, runner.fixture, remove=False, uninstaller_sha=None, stage="synthetic-install")
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    adapter.root = plan.root
    def load(root, name):
        assert name == "rc_payload_inventory"
        return SimpleNamespace(inventory=lambda *args: {"payload_content_sha256": plan.expected_payload})
    monkeypatch.setattr(lifecycle, "load_module", load)
    monkeypatch.setattr(adapter, "previous_identity", lambda *args: pytest.fail("candidate must never use legacy identity"))
    with pytest.raises(lifecycle.SafetyError, match="JSON"):
        adapter.launch(plan.current, runner.fixture, current=True, plan=plan, stage="synthetic-no-current-manifest")


def test_public_failure_retains_unredacted_private_report_and_true_actual_run(plan, monkeypatch):
    public_output = plan.root / "build/v1600-evidence/msi-installer-smoke.json"
    selected = replace(plan, public_output=public_output, candidate_sidecar=plan.current.artifact,
                       candidate_payload=plan.previous.artifact)
    adapter = SyntheticAdapter(plan.source)
    # Synthetic fault injection only; no external execution occurs in this test.
    adapter.actual_run = True
    original_launch = adapter.launch
    def launch(*args, **kwargs):
        return {**original_launch(*args, **kwargs), "command": ["private-unredacted-observation"]}
    adapter.launch = launch
    def publish(root, output, private, **refs):
        assert lifecycle.read_json(selected.output) == private
        assert private["actual_run"] is True
        assert refs["candidate_sidecar"] == selected.candidate_sidecar.reference(root)
        raise OSError("synthetic publication storage failure")
    monkeypatch.setattr(lifecycle, "load_module", lambda root, name: SimpleNamespace(write_public_report=publish))
    runner = lifecycle.Lifecycle(selected, adapter)
    returned = runner.run()
    assert returned["status"] == "BLOCKED" and returned["actual_run"] is True and returned["rc_eligible"] is False
    assert returned["state"] == "PUBLIC_REPORT_FAILED_PRIVATE_RETAINED"
    private = lifecycle.read_json(selected.output)
    assert private["status"] == "PASS" and private["actual_run"] is True
    assert private["checks"]["launch_previous"]["result"]["command"] == ["private-unredacted-observation"]
    assert not public_output.exists()


class SyntheticGuardHandles:
    """Test-owned paths only; this does not validate native handle flags."""
    def __init__(self): self.calls = []
    def open_directory(self, directory):
        self.calls.append(("open-directory", directory))
        return {"directory": directory, "closed": False}
    def create_sentinel(self, path, payload):
        with path.open("xb") as stream: stream.write(payload)
        self.calls.append(("create-sentinel", path))
        return {"path": path, "closed": False, "remove": False}
    def read_sentinel(self, handle, limit): return handle["path"].read_bytes()[:limit]
    def remove_exact_file(self, handle):
        self.calls.append(("mark-exact-delete", handle["path"]))
        handle["remove"] = True
    def close(self, handle):
        assert handle["closed"] is False
        handle["closed"] = True
        if handle.get("remove"): handle["path"].unlink()


def test_owned_desktop_guard_never_removes_directory_or_user_files_and_deletes_only_its_exact_handle(tmp_path):
    directory = tmp_path / "test-only-desktop"
    directory.mkdir()
    user_file = directory / "user-owned.txt"
    user_file.write_bytes(b"must-survive")
    native = SyntheticGuardHandles()
    guard = lifecycle.DesktopDirectoryGuard(directory, "synthetic-owned-run", native=native)
    assert guard.path.is_file() and len(list(directory.iterdir())) == 2
    guard.verify()
    identity = directory.stat().st_ino
    guard.release_success()
    assert directory.stat().st_ino == identity and user_file.read_bytes() == b"must-survive"
    assert not guard.path.exists()
    assert [name for name, *_ in native.calls].count("mark-exact-delete") == 1


def test_desktop_guard_failure_retains_owned_sentinel_and_never_marks_any_file_for_delete(tmp_path):
    directory = tmp_path / "test-only-desktop"
    directory.mkdir()
    native = SyntheticGuardHandles()
    guard = lifecycle.DesktopDirectoryGuard(directory, "synthetic-owned-run", native=native)
    guard.path.write_bytes(b"synthetic-external-change")
    with pytest.raises(lifecycle.SafetyError, match="identity changed"): guard.verify()
    guard.close_retaining()
    assert guard.path.read_bytes() == b"synthetic-external-change" and directory.is_dir()
    assert not any(name == "mark-exact-delete" for name, *_ in native.calls)


def test_preexisting_guard_name_is_never_overwritten_or_deleted(tmp_path):
    directory = tmp_path / "test-only-desktop"
    directory.mkdir()
    existing = directory / "siyi-installed-guard-synthetic-owned-run.tmp"
    existing.write_bytes(b"preexisting")
    native = SyntheticGuardHandles()
    with pytest.raises(lifecycle.SafetyError, match="exclusively created"):
        lifecycle.DesktopDirectoryGuard(directory, "synthetic-owned-run", native=native)
    assert existing.read_bytes() == b"preexisting"
    assert not any(name in {"create-sentinel", "mark-exact-delete"} for name, *_ in native.calls)


def test_native_desktop_guard_has_no_delete_write_sharing_and_only_handle_bound_deletion():
    text = (ROOT / "scripts/rc_installed_lifecycle.py").read_text(encoding="utf-8")
    assert "self._open(directory, 0x80, 0x3, 3, 0x02000000)" in text
    assert "self._open(path, 0xC0010000, 0x1, 1, 0x80)" in text
    assert "SetFileInformationByHandle(handle, 4" in text
    assert ".unlink(" not in text and "Remove-Item" not in text


@pytest.mark.skipif(os.name != "nt", reason="native Windows sharing/disposition API")
def test_windows_native_guard_denies_replacement_and_deletes_only_test_owned_sentinel(tmp_path):
    # This is a pytest-owned directory, never the real Desktop known folder.
    directory = tmp_path / "native-test-only-desktop"
    directory.mkdir()
    guard = lifecycle.DesktopDirectoryGuard(directory, "synthetic-native-test")
    try:
        guard.verify()
        with pytest.raises(OSError):
            with guard.path.open("wb") as stream: stream.write(b"forbidden")
        with pytest.raises(OSError): guard.path.unlink()
        with pytest.raises(OSError): directory.rename(tmp_path / "forbidden-directory-replacement")
        guard.verify()
        guard.release_success()
        assert directory.is_dir() and not guard.path.exists()
    finally:
        guard.close_retaining()


def test_readonly_host_environment_excludes_credentials_and_user_modules_without_changing_application_isolation(tmp_path, monkeypatch):
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    adapter.root = ROOT
    adapter.powershell = tmp_path / "trusted-native/v1.0/powershell.exe"
    modules = adapter.powershell.parent / "Modules"
    modules.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "synthetic-real-host-profile"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "synthetic-real-host-appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "synthetic-real-host-localappdata"))
    monkeypatch.setenv("PSModulePath", "untrusted-runtime-or-user-modules")
    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", "synthetic-user-provider.json")
    host = adapter.host_probe_environment()
    assert host["USERPROFILE"] == str(tmp_path / "synthetic-real-host-profile")
    assert host["PSModulePath"] == str(modules)
    assert not {"AGENT_API_TOKEN", "OPENAI_API_KEY", "AGENT_PROVIDER_CONFIG_PATH"}.intersection(host)
    data = tmp_path / "owned-app-data"
    application = adapter.environment(data, "synthetic-owner")
    assert application["USERPROFILE"] == str(data / "home")
    assert application["APPDATA"] == str(data / "home/AppData/Roaming")
    assert application["AGENT_DESKTOP_DATA_DIRECTORY"] == str(data)
    assert application["AGENT_PROVIDER_CONFIG_PATH"] == str(data / "config/provider.json")
    assert "PSModulePath" not in application and "OPENAI_API_KEY" not in application


def test_only_fixed_readonly_host_probes_may_receive_real_host_environment(monkeypatch):
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    records = []
    monkeypatch.setattr(adapter, "host_probe_environment", lambda: {"USERPROFILE": "synthetic-host-profile"})
    monkeypatch.setattr(adapter, "_ps", lambda code, variables, environment: records.append((code, variables, environment)) or {})
    adapter.host_ps("host_observation")
    adapter.host_ps("known_folders")
    with pytest.raises(lifecycle.SafetyError, match="fixed readonly"):
        adapter.host_ps("unreviewed-command")
    assert len(records) == 2
    assert {record[0] for record in records} == {lifecycle.HOST_OBSERVATION, lifecycle.KNOWN_FOLDER_OBSERVATION}
    assert all(record[1] is None for record in records)


@pytest.mark.parametrize("missing", ["desktop", "common_desktop"])
def test_unknown_real_known_desktop_folder_blocks_before_any_guard_creation(plan, monkeypatch, missing):
    runner = lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    adapter.desktop_guard_owner, adapter.desktop_guards = None, []
    folder = plan.root / "test-only-known-desktop"
    folder.mkdir()
    observed = {"desktop": str(folder), "common_desktop": str(folder)}
    observed[missing] = ""
    monkeypatch.setattr(adapter, "host_ps", lambda probe: observed)
    monkeypatch.setattr(lifecycle, "DesktopDirectoryGuard", lambda *args, **kwargs: pytest.fail("unknown folder must not create any guard"))
    with pytest.raises(lifecycle.SafetyError, match="known desktop folders are unknown"):
        adapter.ensure_desktop_guards(runner.fixture)
    assert adapter.desktop_guard_owner is None and adapter.desktop_guards == []
    assert not list(runner.fixture.root.glob("desktop-guard-*"))


def test_host_observation_and_guards_never_use_redirected_application_probe_environment(plan, monkeypatch):
    runner = lifecycle.Lifecycle(plan, SyntheticAdapter(plan.source))
    adapter = lifecycle.WindowsAdapter.__new__(lifecycle.WindowsAdapter)
    observed = []
    monkeypatch.setattr(adapter, "host_ps", lambda name: observed.append(name) or {"read_only": True})
    monkeypatch.setattr(adapter, "ps", lambda *args, **kwargs: pytest.fail("host observation must not use the application environment"))
    assert adapter.observe(runner.fixture) == {"read_only": True}
    assert observed == ["host_observation"]
    assert "IsNullOrWhiteSpace" in lifecycle.HOST_OBSERVATION
    assert "IsNullOrWhiteSpace" in lifecycle.KNOWN_FOLDER_OBSERVATION
