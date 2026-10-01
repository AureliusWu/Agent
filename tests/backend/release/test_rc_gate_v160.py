from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("rc_gate_tested", ROOT / "scripts/rc_gate.py")
assert spec and spec.loader
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)


def identity():
    return {"source_version": "16.0.0", "source_commit": "a" * 40,
            "source_tree_fingerprint": "B" * 64, "workspace_clean": True}


def reference(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_empty_rc_is_blocked_without_launching_anything(tmp_path):
    result = rc.check_bundle(tmp_path, {}, current=identity(), build_manifest={"build_id": "c" * 24}, model_identity={})
    assert result["status"] == "BLOCKED" and not result["passed"]
    assert result["published"] is False and result["scope"] == "pre_tag_rc_only"
    assert len(result["results"]) == 10
    assert all(row["status"] == "FAIL" for row in result["results"])


@pytest.mark.parametrize("field,value", [("source_version", "15.0.0"), ("source_commit", "d" * 40), ("source_tree_fingerprint", "e" * 64), ("workspace_clean", False)])
def test_wrong_version_or_dirty_or_stale_identity_cannot_be_rc(tmp_path, field, value):
    source = {**identity(), field: value}
    bundle = {"schema_version": 1, "protocol_version": rc.PROTOCOL, "target_version": "16.0.0", "source": source, "source_after": source}
    result = rc.check_bundle(tmp_path, bundle, current=identity(), build_manifest={"build_id": "c" * 24}, model_identity={})
    assert result["results"][0]["status"] == "FAIL"


def test_source_stable_before_after_is_mandatory():
    with pytest.raises(rc.GateError, match="before-and-after"):
        rc.bound_source({"source": identity()}, identity())
    with pytest.raises(rc.GateError, match="before-and-after"):
        rc.bound_source({"source": identity(), "source_after": {**identity(), "workspace_clean": False}}, identity())
    rc.bound_source({"source": identity(), "source_after": identity()}, identity())


def test_attachment_tamper_and_path_escape_are_rejected(tmp_path):
    ref = reference(tmp_path, "report.json", {"status": "PASS"})
    assert rc.attachment(tmp_path, ref)["status"] == "PASS"
    (tmp_path / "report.json").write_text('{}', encoding="utf-8")
    with pytest.raises(rc.GateError, match="SHA-256"):
        rc.attachment(tmp_path, ref)
    with pytest.raises(rc.GateError, match="repository-relative"):
        rc.attachment(tmp_path, {"path": "../secret.json", "sha256": "a" * 64})


def test_candidate_gate_does_not_require_tag_to_avoid_a_release_cycle():
    required = rc.required_gates()
    assert "git_tag_consistency" not in required["automated"]
    assert "coverage_80" in required["automated"]
    assert set(rc.ADDITIONAL_GATES["automated"]) <= set(required["automated"])
    assert "deepseek" in required["manual"] and "voice_basic" in required["manual"]
    assert set(rc.EVALUATIONS) == {"core", "multi_agent", "professional_agents", "adversarial", "default_model"}


def test_unrelated_zero_exit_command_is_not_coverage_or_security_evidence():
    with pytest.raises(rc.GateError, match="does not cover"):
        rc.automated_command({"command": ["python", "-c", "exit(0)"], "cwd": "."}, "readonly_matrix")
    rc.automated_command({"command": ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/test.ps1"], "cwd": "."}, "readonly_matrix")
    with pytest.raises(rc.GateError):
        rc.automated_command({"command": ["npm.cmd", "run", "lint"], "cwd": "desktop/frontend"}, "frontend_security_tests")


def performance():
    common = {"host_fingerprint": "test-host", "startup_path": "installed-nsis", "cache_state": "warm", "measurement_object": "desktop", "measurement_protocol": "desktop-render-ready-v1", "samples_ms": [100, 100, 100, 100, 100]}
    return {"baseline": {**common, "run_id": "old", "build_id": "old"}, "candidate": {**common, "run_id": "new", "build_id": "new"}}


@pytest.mark.parametrize("mutation", ["missing", "self", "host", "nan", "few", "twenty", "ten"])
def test_performance_does_not_call_unknown_or_unpaired_runs_no_regression(mutation):
    value = performance()
    if mutation == "missing": value.pop("baseline")
    elif mutation == "self": value["candidate"]["run_id"] = "old"
    elif mutation == "host": value["candidate"]["host_fingerprint"] = "different"
    elif mutation == "nan": value["candidate"]["samples_ms"] = [float("nan")] * 5
    elif mutation == "few": value["candidate"]["samples_ms"] = [100]
    elif mutation == "twenty": value["candidate"]["samples_ms"] = [121] * 5
    else: value["candidate"]["samples_ms"] = [115] * 5
    with pytest.raises(rc.GateError): rc.performance_comparison(value, "new")


def test_performance_uses_median_and_requires_explanation_for_warning_band():
    value = performance()
    value["candidate"]["samples_ms"] = [112, 113, 114, 115, 120]
    value["explanation"] = "Synthetic fixture: known instrumentation overhead, not real release evidence"
    rc.performance_comparison(value, "new")


def test_same_manual_label_without_operator_and_binary_proof_does_not_pass(tmp_path):
    source = identity()
    rid = rc.requirement_id("manual", "voice_basic")
    proof = {"report_type": "rc_check_evidence", "target_version": "16.0.0", "kind": "automated", "source": source,
             "source_after": source, "status": "PASS", "actual_run": True, "checks": {rid: True}, "build_id": "new"}
    ref = reference(tmp_path, "proof.json", proof)
    matrix = {"schema_version": 5, "target_version": "16.0.0", "protocol_version": rc.PROTOCOL,
              "release_gates": {"manual": [{"id": "voice_basic", "requirement_id": rid, "status": "PASS",
                                             "evidence": [{"kind": "manual", "actual_run": True, "outcome": "PASS", "report": ref}]}]}}
    errors = rc.validate_matrix(tmp_path, matrix, source, "new")
    assert any(rid in error and "type/layer/status" in error for error in errors)


def test_missing_eval_baseline_is_not_satisfied_by_other_rc_sections(tmp_path):
    with pytest.raises(rc.GateError, match="baseline"):
        rc.evaluation_pair(tmp_path, {"candidate": {}}, "core", identity())


def test_performance_script_name_in_an_unrelated_command_is_not_execution():
    raw = {"command": ["powershell", "-NoProfile", "-Command", "Write-Output ok", "scripts/v14-candidate-runtime-smoke.ps1"],
           "cwd": ".", "performance_comparison": performance()}
    with pytest.raises(rc.GateError):
        rc.automated_command(raw, "startup_performance_comparison")


def test_numeric_performance_without_raw_samples_cannot_qualify(tmp_path):
    with pytest.raises(rc.GateError, match="sample|attachment"):
        rc.validate_performance_evidence(tmp_path, performance(), identity(), {"sidecar": "a" * 64})


def variant_binaries(root, marker=b"__TAURI_BUNDLE_TYPE_VAR_UNK"):
    data = bytearray(1024 * 1024 + 256)
    data[:2] = b"MZ"
    data[60:64] = (128).to_bytes(4, "little")
    data[128:132] = b"PE\0\0"
    data[1024:1024 + len(marker)] = marker
    result = {}
    for key, name in (("desktop", "desktop.exe"), ("sidecar", "agent-backend.exe")):
        path = root / name
        path.write_bytes(data)
        result[key] = {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
    return result, bytes(data)


@pytest.mark.parametrize("kind,suffix", [("NSIS", b"NSS"), ("MSI", b"MSI")])
def test_installer_hash_derives_only_the_exact_sdk_marker(tmp_path, kind, suffix):
    binaries, original = variant_binaries(tmp_path)
    result = rc.installer_binary_hashes(tmp_path, binaries, kind)
    expected = original.replace(b"__TAURI_BUNDLE_TYPE_VAR_UNK", b"__TAURI_BUNDLE_TYPE_VAR_" + suffix)
    assert result == {"desktop": hashlib.sha256(expected).hexdigest(), "sidecar": binaries["sidecar"]["sha256"]}
    assert (tmp_path / "desktop.exe").read_bytes() == original
    assert hashlib.sha256(expected + b"unrelated mutation").hexdigest() != result["desktop"]


@pytest.mark.parametrize("marker", [b"missing", b"__TAURI_BUNDLE_TYPE_VAR_UNK__TAURI_BUNDLE_TYPE_VAR_UNK", b"__TAURI_BUNDLE_TYPE_VAR_MSI"])
def test_missing_duplicate_or_already_patched_marker_is_not_portable_identity(tmp_path, marker):
    binaries, _ = variant_binaries(tmp_path, marker)
    with pytest.raises(rc.GateError, match="unambiguous"):
        rc.installer_binary_hashes(tmp_path, binaries, "MSI")


def test_installer_derivation_rejects_stale_bytes_and_unknown_kind(tmp_path):
    binaries, original = variant_binaries(tmp_path)
    with pytest.raises(rc.GateError, match="unsupported"):
        rc.installer_binary_hashes(tmp_path, binaries, "arbitrary")
    (tmp_path / "desktop.exe").write_bytes(original + b"mutation")
    with pytest.raises(rc.GateError, match="SHA-256"):
        rc.installer_binary_hashes(tmp_path, binaries, "MSI")


@pytest.mark.parametrize("variant", ["unknown", {}, [], None])
def test_arbitrary_variant_label_cannot_select_installer_hashes(variant):
    with pytest.raises(rc.GateError, match="unbound artifact variant"):
        rc.artifact_binary_hashes({"artifact_variant": variant}, {"desktop": "portable"}, {"msi": {"desktop": "msi"}})
    assert rc.artifact_binary_hashes({}, {"desktop": "portable"}, None) == {"desktop": "portable"}
    assert rc.artifact_binary_hashes({"artifact_variant": "msi"}, {"desktop": "portable"}, {"msi": {"desktop": "msi"}}) == {"desktop": "msi"}


@pytest.mark.parametrize("mutation", [None, "desktop", "sidecar"])
def test_installer_gate_accepts_only_derived_desktop_and_exact_sidecar(tmp_path, monkeypatch, mutation):
    from types import SimpleNamespace
    binaries, _ = variant_binaries(tmp_path)
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal/code.bin").write_bytes(b"synthetic frozen payload")
    original_module = rc.module
    payload = original_module("rc_payload_inventory").inventory(tmp_path, binaries["sidecar"])
    source = {**identity(), "build_id": "candidate"}
    installers = {}
    for kind in ("NSIS", "MSI"):
        hashes = rc.installer_binary_hashes(tmp_path, binaries, kind)
        if kind == "MSI" and mutation:
            hashes[mutation] = "f" * 64
        raw = {"source": source, "source_after": source, "binary_sha256": hashes,
               "sidecar_payload_sha256": payload["payload_content_sha256"], "installed_sidecar_payload": payload}
        installers[kind.lower()] = reference(tmp_path, kind + ".json", raw)
    # Only the separate legacy lifecycle validator is stubbed. These tests
    # exercise actual PE, accepted bytes, payload, source and installer binding.
    monkeypatch.setattr(rc, "module", lambda name: SimpleNamespace(_validate_one=lambda *args, **kwargs: None) if name == "check-release-evidence" else original_module(name))
    bundle = {"binaries": binaries, "sidecar_payload": reference(tmp_path, "payload.json", payload), "installers": installers}
    result = rc.check_bundle(tmp_path, bundle, current=identity(), build_manifest={"build_id": "candidate"}, model_identity={})
    gate = next(row for row in result["results"] if row["requirement_id"] == "V160-Q01-INSTALLERS")
    assert gate["status"] == ("PASS" if mutation is None else "FAIL")
