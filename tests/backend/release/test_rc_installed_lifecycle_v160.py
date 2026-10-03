"""Synthetic lifecycle/fault tests. Never run an installer or promote manual PASS."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
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
        "files": [{"id": "Path", "component": "Path", "name": "司忆.exe"}, {"id": "payload", "component": "payload", "name": "build-info.json"}],
        "registry": [{"component": "Path", "root": 1, "key": r"Software\github\司忆", "name": "InstallDir", "value": "[INSTALLDIR]"}],
        "shortcuts": [], "remove_files": [], "media": [{"cabinet": "#app.cab", "source": ""}],
        "upgrades": [], "reg_locators": [], "app_search": [], "signatures": []}


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
