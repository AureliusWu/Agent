"""Complete synthetic double-stream/transport tests; never run an installer."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module(name):
    specification = importlib.util.spec_from_file_location("installed_public_test_" + name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    value = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(value)
    return value


public = module("rc_installed_public")
exporter = module("export-accepted-rc")
installer = module("check-release-evidence")
importer = module("import-accepted-rc")
payload_module = module("rc_payload_inventory")
COMMIT = "a" * 40


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}


def save(root, name, value):
    return put(root, name, json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def clean():
    return {"protocol_version": "exact-native-job-v1", "active_before_cleanup": 0, "active_after_cleanup": 0,
            "forced_termination": False, "job_handle_closed": True, "errors": [], "unassigned_cleanup_complete": True,
            "owned_process_handles_remaining": 0, "owned_thread_handles_remaining": 0, "launch_cleanup_errors": [],
            "launch_events": ["created_suspended", "resumed"]}


def manifest(version=public.VERSION, build_id="b" * 24, schema=46):
    return {"manifest_version": 1, "product_version": version, "git_commit": COMMIT, "source_fingerprint": "c" * 64,
            "build_id": build_id, "database_schema_version": schema, "build_type": "Release", "workspace_state": "CLEAN",
            "component_build_ids": {name: name + "-" + build_id for name in ("tauri", "react", "sidecar")},
            "git_branch": "synthetic-test-only", "build_time": "2026-10-03T00:00:00Z"}


def complete_fixture(root, kind="MSI"):
    """Actual fixture file hashes, but no executable ever runs and no PASS."""
    current_manifest, previous_manifest = manifest(), manifest("8.0.1", "d" * 24, 23)
    sidecar = put(root, "desktop/src-tauri/target/release/agent-backend.exe", b"MZsynthetic-never-executed")
    put(root, "desktop/src-tauri/target/release/_internal/code.bin", b"synthetic frozen payload")
    save(root, "desktop/src-tauri/target/release/_internal/build-info.json", current_manifest)
    accepted_payload = payload_module.inventory(root, sidecar)
    payload_ref = save(root, "build/v1600-evidence/accepted/sidecar-payload.json", accepted_payload)
    installed_payload = deepcopy(accepted_payload)
    installed_payload["binary"]["path"] = "install/agent-backend.exe"
    prefix = "desktop/src-tauri/target/release/_internal/"
    for entry in installed_payload["entries"]:
        entry["path"] = "install/_internal/" + entry["path"][len(prefix):]
    suffix = "-setup.exe" if kind == "NSIS" else ".msi"
    artifacts = {}
    for label, version, directory, marker in (("candidate", public.VERSION, "desktop/src-tauri/target/release/bundle/" + kind.lower(), b"current"),
                                               ("previous", "8.0.1", "build/upgrade-baseline", b"previous")):
        name = "synthetic" + version.replace(".", "") + suffix
        reference = put(root, directory + "/" + name, marker + b"0" * (1024 * 1024))
        artifacts[label] = {"name": name, "version": version, "sha256": reference["sha256"],
                            "bytes": (root / reference["path"]).stat().st_size,
                            "product_code": ("{AAAAAAAA-1111-2222-3333-444444444444}" if label == "candidate"
                                             else "{BBBBBBBB-1111-2222-3333-444444444444}") if kind == "MSI" else None,
                            "upgrade_code": public.FAMILY if kind == "MSI" else None}
    artifacts.update(build_manifest=current_manifest, previous_build_manifest=previous_manifest, previous_schema_observation=23)
    source = {"source_version": public.VERSION, "source_commit": COMMIT, "workspace_clean": True,
              "source_tree_fingerprint": "c" * 64, "build_id": "b" * 24}
    binary_hashes = {"desktop": "e" * 64, "sidecar": sidecar["sha256"]}
    checks = {}
    for stage in public.STAGES:
        if stage in public.INSTALL_STAGES:
            result = {"passed": True, "exit_code": 0, "process_cleanup": clean(),
                      "command": ["C:/workspace/msiexec.exe", "/i", "D:/workspace-a/candidate.msi", "/L*v", "D:/workspace-a/install.log"],
                      "retained_process_creation_time": 999}
        elif stage in public.LAUNCH_STAGES:
            previous = stage == "launch_previous"
            receipt = None if previous else {"schema_version": 1, "report_type": "rc_desktop_runtime_observation",
                "protocol_version": "desktop-render-ready-v1", "actual_run": False, "status": "SYNTHETIC_PASS",
                "acceptance_nonce": "12345678-1234-1234-1234-123456789abc" if stage == "launch_upgraded" else "12345678-1234-1234-1234-123456789abd", "isolated_test_data": True,
                "desktop_render_ready": True, "sidecar_ready": True, "process_ids": {"desktop": 111, "sidecar": 222},
                "window_handle": 333, "readiness_ms": 12, "components": {name: deepcopy(current_manifest) for name in ("tauri", "react", "sidecar")}}
            identity = {"identity_mode": "previous_onedir_manifest" if previous else "current_onedir_manifest",
                        "manifest": previous_manifest if previous else current_manifest}
            if previous: identity["current_candidate_manifest_qualification"] = False
            result = {"passed": True, "protocol": "previous-installed-owned-process-v1" if previous else "desktop-render-ready-v1",
                      "previous_observation_is_not_render_acceptance": previous, "binary_sha256": binary_hashes,
                      "sidecar_payload_sha256": accepted_payload["payload_content_sha256"], "installed_sidecar_payload": installed_payload,
                      "process_cleanup": clean(), "desktop_exit_code": 0, "sidecar_exit_code": 0,
                      "installed_build_identity": identity, "render_receipt": receipt}
        elif stage == "seed_fixture":
            result = {"passed": True, "fixture": {"nonce": "test-only", "old_schema": 23, "conversation_id": 1, "model_sha256": "f" * 64}}
        else:
            result = {"passed": True, "retained": {"passed": True, "schema": 46, "conversation_preserved": True,
                      "message_preserved": True, "model_fixture_preserved": True,
                      "migration_backups": [{"name": "pre-migration-v23-synthetic.db", "sha256": "f" * 64}]}}
        checks[stage] = {"passed": True, "result": deepcopy(result)}
    raw = {"schema_version": 1, "report_type": "synthetic_installer_lifecycle", "target_version": public.VERSION,
           "status": "SYNTHETIC_PASS", "actual_run": False, "rc_eligible": False, "source": source, "source_after": source,
           "run": {"installer_kind": kind, "isolated_test_data": True, "owner_run_id": "12345678-1234-1234-1234-123456789abc",
                   "fixture_retained": True, "operator_attested": False}, "artifacts": artifacts, "checks": checks,
           "results": {"status": "ok", "version": public.VERSION, **{flag: True for flag in public.RESULT_FLAGS}},
           "binary_sha256": binary_hashes, "sidecar_payload_sha256": accepted_payload["payload_content_sha256"],
           "installed_sidecar_payload": installed_payload, "manual_desktop_acceptance": "NOT_RECORDED",
           "credential_manager_isolation": "NOT_ISOLATED_SAME_WINDOWS_USER", "automatic_registry_or_fixture_cleanup": False}
    return raw, sidecar, accepted_payload, payload_ref


def build(raw, sidecar, payload):
    return public.build_public_report(raw, candidate_sidecar=sidecar, candidate_payload=payload)


@pytest.mark.parametrize("kind", ["NSIS", "MSI"])
def test_double_stream_preserves_private_bytes_and_synthetic_qualification(tmp_path, kind):
    raw, sidecar, payload, payload_ref = complete_fixture(tmp_path, kind)
    private_ref = save(tmp_path, "private-test/real-shape.json", raw)
    original = (tmp_path / private_ref["path"]).read_bytes()
    before = deepcopy(raw)
    fixed = tmp_path / f"build/v1600-evidence/{kind.lower()}-installer-smoke.json"
    value = public.write_public_report(tmp_path, fixed, raw, candidate_sidecar=sidecar, candidate_payload=payload_ref)
    assert raw == before and (tmp_path / private_ref["path"]).read_bytes() == original
    assert value["public_protocol"] == "installed-public-v1"
    assert value["status"] == "SYNTHETIC_PASS" and value["actual_run"] is False and value["rc_eligible"] is False
    assert value["installed_sidecar_payload"]["path_scope"] == "fixture-install-v1"
    assert value["installed_sidecar_payload"]["binary"] == raw["installed_sidecar_payload"]["binary"]
    assert value["installed_sidecar_payload"]["entries"] == raw["installed_sidecar_payload"]["entries"]
    assert "command" not in fixed.read_text(encoding="utf-8") and "C:/workspace" not in fixed.read_text(encoding="utf-8")
    assert list(exporter.references(value, candidate_sidecar=sidecar, candidate_payload=payload)) == []
    # The unchanged original installer artifact contract consumes the new shape.
    installer._validate_artifacts(value, root=tmp_path, version=public.VERSION, head=COMMIT, kind=kind)
    assert payload_module.content_summary(value["installed_sidecar_payload"]) == payload["payload_content_sha256"]
    with pytest.raises(installer.EvidenceError):
        installer._validate_one(value, root=tmp_path, version=public.VERSION, head=COMMIT, kind=kind)
    with pytest.raises(ValueError):
        exporter.public_content("build/v1600-evidence/accepted/private.json", original)


@pytest.mark.parametrize("mutation", ["source", "summary", "binary", "entries", "install_exit", "desktop_exit", "sidecar_exit",
                                      "cleanup_forced", "cleanup_unknown", "bool_exit", "bool_accounting", "missing_stage", "failed_stage",
                                      "promote_synthetic", "raw_synthetic", "manifest_source", "retention", "pre_scoped", "render_actual", "render_component",
                                      "render_missing", "render_pid", "previous_schema", "flip_top_actual", "reused_render", "previous_identity_mismatch"])
def test_builder_rejects_inconsistent_private_observations(tmp_path, mutation):
    raw, sidecar, payload, _ = complete_fixture(tmp_path)
    if mutation == "source": raw["source_after"] = dict(raw["source"], source_commit="0" * 40)
    elif mutation == "summary": raw["sidecar_payload_sha256"] = "0" * 64
    elif mutation == "binary": raw["binary_sha256"]["sidecar"] = "0" * 64
    elif mutation == "entries": raw["installed_sidecar_payload"]["entries"][0]["sha256"] = "0" * 64
    elif mutation == "install_exit": raw["checks"]["upgrade_current"]["result"]["exit_code"] = 3010
    elif mutation == "desktop_exit": raw["checks"]["launch_upgraded"]["result"]["desktop_exit_code"] = 1
    elif mutation == "sidecar_exit": raw["checks"]["launch_reinstalled"]["result"]["sidecar_exit_code"] = None
    elif mutation == "cleanup_forced": raw["checks"]["final_uninstall"]["result"]["process_cleanup"]["forced_termination"] = True
    elif mutation == "cleanup_unknown": raw["checks"]["launch_upgraded"]["result"]["process_cleanup"]["active_after_cleanup"] = None
    elif mutation == "bool_exit": raw["checks"]["upgrade_current"]["result"]["exit_code"] = False
    elif mutation == "bool_accounting": raw["checks"]["upgrade_current"]["result"]["process_cleanup"]["active_after_cleanup"] = False
    elif mutation == "missing_stage": raw["checks"].pop("launch_reinstalled")
    elif mutation == "failed_stage": raw["checks"]["upgrade_current"]["passed"] = False
    elif mutation == "promote_synthetic": raw["status"] = "PASS"; raw["rc_eligible"] = True
    elif mutation == "raw_synthetic": raw["checks"]["upgrade_current"]["result"]["actual_run"] = True
    elif mutation == "manifest_source": raw["artifacts"]["build_manifest"]["git_commit"] = "0" * 40
    elif mutation == "retention": raw["checks"]["verify_final"]["result"]["retained"]["migration_backups"] = []
    elif mutation == "pre_scoped": raw["installed_sidecar_payload"]["path_scope"] = public.PATH_SCOPE
    elif mutation == "render_actual": raw["checks"]["launch_upgraded"]["result"]["render_receipt"]["actual_run"] = True
    elif mutation == "render_component": raw["checks"]["launch_upgraded"]["result"]["render_receipt"]["components"]["react"]["build_id"] = "0" * 24
    elif mutation == "render_missing": raw["checks"]["launch_upgraded"]["result"]["render_receipt"] = None
    elif mutation == "render_pid": raw["checks"]["launch_upgraded"]["result"]["render_receipt"]["process_ids"]["sidecar"] = 0
    elif mutation == "previous_schema": raw["artifacts"]["previous_schema_observation"] = 46
    elif mutation == "flip_top_actual": raw.update(actual_run=True, status="PASS", rc_eligible=True, report_type="release_msi_installer_live_evidence")
    elif mutation == "previous_identity_mismatch": raw["artifacts"]["previous_build_manifest"]["git_commit"] = "0" * 40
    else: raw["checks"]["launch_reinstalled"]["result"]["render_receipt"] = deepcopy(raw["checks"]["launch_upgraded"]["result"]["render_receipt"])
    with pytest.raises((ValueError, KeyError)):
        build(raw, sidecar, payload)


def test_unknown_historical_onefile_fields_remain_unknown_not_new_candidate_gate(tmp_path):
    raw, sidecar, payload, _ = complete_fixture(tmp_path)
    historical = {"archive_entry": "build-info.json", "embedded_manifest_bytes": 120, "embedded_manifest_sha256": "f" * 64,
                  "product_version": "8.0.1", "git_commit": None, "source_fingerprint": None,
                  "workspace_state": None, "build_id": None, "component_build_id": None}
    raw["artifacts"]["previous_build_manifest"] = {name: historical[name] for name in ("product_version", "git_commit", "source_fingerprint", "workspace_state", "build_id")}
    previous = raw["checks"]["launch_previous"]["result"]
    previous.update(installed_sidecar_payload=None, sidecar_payload_sha256=None,
                    installed_build_identity={"identity_mode": "legacy_onefile_embedded_manifest", "observation": historical,
                                              "current_candidate_manifest_qualification": False})
    value = build(raw, sidecar, payload)
    assert value["artifacts"]["previous_build_manifest"]["workspace_state"] is None
    assert value["checks"]["launch_previous"]["result"]["sidecar_payload_sha256"] is None
    installer._validate_artifacts(value, root=tmp_path, version=public.VERSION, head=COMMIT, kind="MSI")


@pytest.mark.parametrize("mutation", ["scope", "escape", "backslash", "ads", "dot", "case_alias", "summary", "binary",
                                      "unknown_top", "unknown_inventory", "hidden_entry_ref", "hidden_binary_ref", "hidden_manifest_ref",
                                      "hidden_cleanup_ref", "private_argv", "raw_log_ref", "nested_inventory", "bad_protocol", "ordinary_ref"])
def test_exact_public_schema_cannot_hide_private_or_ordinary_references(tmp_path, mutation):
    raw, sidecar, payload, _ = complete_fixture(tmp_path)
    value = build(raw, sidecar, payload)
    observed = value["installed_sidecar_payload"]
    reference = {"path": "build/v1600-evidence/accepted/raw.json", "sha256": "0" * 64}
    if mutation == "scope": observed["path_scope"] = "repository-v1"
    elif mutation in {"escape", "backslash", "ads", "dot"}:
        observed["entries"][0]["path"] = {"escape": "install/_internal/../../outside", "backslash": "install\\_internal\\outside",
                                           "ads": "install/_internal/code:secret", "dot": "install/_internal/./code"}[mutation]
    elif mutation == "case_alias": observed["entries"].append(dict(observed["entries"][0], path=observed["entries"][0]["path"].upper()))
    elif mutation == "summary": observed["payload_content_sha256"] = "0" * 64
    elif mutation == "binary": observed["binary"]["sha256"] = "0" * 64
    elif mutation == "unknown_top": value["extra"] = True
    elif mutation == "unknown_inventory": observed["extra"] = reference
    elif mutation == "hidden_entry_ref": observed["entries"][0]["extra"] = reference
    elif mutation == "hidden_binary_ref": observed["binary"]["extra"] = reference
    elif mutation == "hidden_manifest_ref": value["artifacts"]["build_manifest"]["extra"] = reference
    elif mutation == "hidden_cleanup_ref": value["checks"]["upgrade_current"]["result"]["process_cleanup"]["extra"] = reference
    elif mutation == "private_argv": value["checks"]["upgrade_current"]["result"]["command"] = ["C:/workspace/msiexec.exe"]
    elif mutation == "raw_log_ref": value["raw_log"] = reference
    elif mutation == "nested_inventory": value["checks"]["launch_upgraded"]["result"]["installed_sidecar_payload"] = observed
    elif mutation == "bad_protocol": value["public_protocol"] = "unknown"
    else: value["ordinary"] = reference
    with pytest.raises(ValueError):
        list(exporter.references(value, candidate_sidecar=sidecar, candidate_payload=payload))


def test_only_top_level_validated_envelope_skips_scoped_refs(tmp_path):
    raw, sidecar, payload, _ = complete_fixture(tmp_path)
    value = build(raw, sidecar, payload)
    with pytest.raises(ValueError): list(exporter.references(value))
    refs = list(exporter.references({"nested": value}, candidate_sidecar=sidecar, candidate_payload=payload))
    assert any(ref["path"] == "install/agent-backend.exe" for ref in refs)
    for ref in refs:
        with pytest.raises(ValueError): exporter.public_path(ref["path"])


def transport_tree(root):
    installer_refs = {}
    for kind in ("MSI", "NSIS"):
        raw, sidecar, payload, payload_ref = complete_fixture(root, kind)
        fixed = root / f"build/v1600-evidence/{kind.lower()}-installer-smoke.json"
        value = public.write_public_report(root, fixed, raw, candidate_sidecar=sidecar, candidate_payload=payload_ref)
        installer_refs[kind.lower()] = {"path": fixed.relative_to(root).as_posix(), "sha256": hashlib.sha256(fixed.read_bytes()).hexdigest()}
        installer._validate_artifacts(value, root=root, version=public.VERSION, head=COMMIT, kind=kind)
    bundle = {"synthetic_fixture": True, "binaries": {"sidecar": sidecar}, "sidecar_payload": payload_ref, "installers": installer_refs}
    save(root, "build/v1600-evidence/accepted/rc-bundle.json", bundle)
    save(root, "build/v1600-evidence/accepted/default-model-identity.json", {"synthetic_fixture": True})
    save(root, "build/generated/build-info.json", manifest())
    save(root, "dist/release/agent-sbom.cdx.json", {"synthetic_fixture": True})
    put(root, "dist/release/THIRD_PARTY_NOTICES.txt", b"Synthetic transport only")
    return installer_refs, sidecar, payload, payload_ref


def test_synthetic_original_shape_roundtrips_transport_import_without_rc_eligibility(tmp_path):
    root = tmp_path / "source"
    installer_refs, sidecar, payload, payload_ref = transport_tree(root)
    index = exporter.index_from_closure(root, COMMIT)
    names = {entry["path"] for entry in index["files"]}
    assert sidecar["path"] in names and payload_ref["path"] in names
    assert all(entry["path"] in names for entry in payload["entries"])
    assert not any(name.startswith("install/") or "private-test" in name for name in names)
    archive = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("artifact-index.json", json.dumps(index))
        for entry in index["files"]: stream.write(root / entry["path"], entry["path"])
    destination = tmp_path / "destination"
    destination.mkdir()
    received = destination / "build/v1600-evidence/rc-input"
    result = exporter.receive_archive(destination, archive, received, COMMIT)
    assert result["status"] == "TRANSPORT_VALIDATED_NOT_RC_ACCEPTED"
    entries = importer.plan(received, destination, COMMIT)
    for source, target, expected in entries:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        assert importer.digest(target) == expected
    exporter.index_from_closure(destination, COMMIT)
    for kind in ("MSI", "NSIS"):
        restored = json.loads((destination / installer_refs[kind.lower()]["path"]).read_text(encoding="utf-8"))
        assert restored["status"] == "SYNTHETIC_PASS" and restored["rc_eligible"] is False
        assert payload_module.content_summary(restored["installed_sidecar_payload"]) == payload["payload_content_sha256"]
        installer._validate_artifacts(restored, root=destination, version=public.VERSION, head=COMMIT, kind=kind)
        with pytest.raises(installer.EvidenceError):
            installer._validate_one(restored, root=destination, version=public.VERSION, head=COMMIT, kind=kind)
    # Invoke the unchanged total consumer too: transport mechanics must never
    # turn this deliberately incomplete synthetic tree into accepted RC proof.
    bundle = json.loads((destination / "build/v1600-evidence/accepted/rc-bundle.json").read_text(encoding="utf-8"))
    source = {key: item for key, item in restored["source"].items() if key != "build_id"}
    result = module("rc_gate").check_bundle(destination, bundle, current=source,
                                            build_manifest=manifest(), model_identity={})
    assert result["status"] == "BLOCKED" and result["passed"] is False
    assert len(result["results"]) == 10
    assert next(item for item in result["results"] if item["requirement_id"] == "V160-Q01-INSTALLERS")["status"] == "FAIL"


@pytest.mark.parametrize("mutation", ["scope", "unknown", "hidden_ref", "remove_protocol", "source", "candidate_file", "ordinary_ref"])
@pytest.mark.parametrize("phase", ["receive", "validate_directory"])
def test_rehashed_incoming_index_cannot_bypass_typed_or_ordinary_closure(tmp_path, mutation, phase):
    root = tmp_path / "source"
    _, _, payload, _ = transport_tree(root)
    index = exporter.index_from_closure(root, COMMIT)
    if mutation == "candidate_file":
        changed = root / payload["entries"][0]["path"]
        changed.write_bytes(b"rehashed but not accepted payload")
    elif mutation == "ordinary_ref":
        changed = root / "build/v1600-evidence/accepted/default-model-identity.json"
        changed.write_text(json.dumps({"missing": {"path": "build/v1600-evidence/accepted/missing.json", "sha256": "0" * 64}}), encoding="utf-8")
    else:
        changed = root / "build/v1600-evidence/msi-installer-smoke.json"
        value = json.loads(changed.read_text(encoding="utf-8"))
        if mutation == "scope": value["installed_sidecar_payload"]["path_scope"] = "unknown"
        elif mutation == "unknown": value["unknown"] = True
        elif mutation == "hidden_ref": value["installed_sidecar_payload"]["entries"][0]["hidden"] = {"path": "build/v1600-evidence/accepted/missing.json", "sha256": "0" * 64}
        elif mutation == "source": value["source"]["source_commit"] = "0" * 40
        else: value.pop("public_protocol")
        changed.write_text(json.dumps(value), encoding="utf-8")
        # A malicious sender can also rehash its parent ref. The typed/public
        # schema must reject it independently of an obsolete parent digest.
        bundle_path = root / "build/v1600-evidence/accepted/rc-bundle.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["installers"]["msi"]["sha256"] = hashlib.sha256(changed.read_bytes()).hexdigest()
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    for entry in index["files"]:
        content = (root / entry["path"]).read_bytes()
        entry.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    destination = tmp_path / "destination"
    destination.mkdir()
    directory = destination / "build/v1600-evidence/rc-input"
    if phase == "receive":
        archive = tmp_path / "forged.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("artifact-index.json", json.dumps(index))
            for entry in index["files"]: stream.write(root / entry["path"], entry["path"])
        with pytest.raises(ValueError): exporter.receive_archive(destination, archive, directory, COMMIT)
    else:
        for entry in index["files"]:
            put(directory, entry["path"], (root / entry["path"]).read_bytes())
        save(directory, "artifact-index.json", index)
        with pytest.raises(ValueError): exporter.validate_directory(destination, directory, COMMIT)


@pytest.mark.parametrize("mutation", ["binary", "payload", "file", "path", "existing"])
def test_writer_rechecks_candidate_bytes_and_fixed_fresh_output(tmp_path, mutation):
    raw, sidecar, payload, payload_ref = complete_fixture(tmp_path)
    output = tmp_path / "build/v1600-evidence/msi-installer-smoke.json"
    if mutation == "binary": (tmp_path / sidecar["path"]).write_bytes(b"changed")
    elif mutation == "payload": (tmp_path / payload_ref["path"]).write_bytes(b"{}")
    elif mutation == "file": (tmp_path / payload["entries"][0]["path"]).write_bytes(b"changed")
    elif mutation == "path": output = tmp_path / "build/v1600-evidence/accepted/wrong.json"
    else: output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b"retained")
    with pytest.raises(ValueError):
        public.write_public_report(tmp_path, output, raw, candidate_sidecar=sidecar, candidate_payload=payload_ref)
    assert not output.exists() if mutation != "existing" else output.read_bytes() == b"retained"
