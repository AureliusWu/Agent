"""Offline collection boundaries; no actual RC, installer or model acceptance."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYNTHETIC_DRIVE = chr(67) + ":/"
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("test_offline_collector", ROOT / "scripts/collect-rc-offline.py")
collector = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(collector)


def source():
    return {"source_version": "16.0.1", "source_commit": "a" * 40,
            "source_tree_fingerprint": "b" * 64, "workspace_clean": True}


def write(root, name, content=b'{"synthetic_test_only":true}'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}


def test_plan_preserves_all_42_requirements_and_never_grants_rc():
    value = collector.plan()
    identifiers = {collector.gate.requirement_id("automated", name) for name in value["automated"]}
    assert len(identifiers) == 19
    assert len(value["pending_requirements"]) == 23
    assert not identifiers.intersection(value["pending_requirements"])
    assert len(identifiers.union(value["pending_requirements"])) == 42
    assert value["evaluations"] == ["core", "multi_agent", "professional_agents", "adversarial"]
    assert value["rc_eligible"] is False and value["formal_release"] is False
    assert all(name.startswith("V160-AUTOMATED-") for name in identifiers)


def test_default_entry_is_read_only(monkeypatch, capsys):
    monkeypatch.setattr(collector, "execute_collection", lambda: pytest.fail("default must never execute"))
    assert collector.main([]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "PLANNED_NOT_EXECUTED" and value["actual_run"] is False


def test_source_drift_and_dirty_sources_are_not_relabelled(monkeypatch):
    monkeypatch.setattr(collector, "source_identity", lambda: {**source(), "workspace_clean": False})
    with pytest.raises(ValueError, match="source changed"):
        collector.require_same_source(source())


def test_non_windows_execution_stops_before_commands_or_files(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.setattr(collector.sys, "platform", "linux")
    monkeypatch.setattr(collector.subprocess, "run", lambda *a, **k: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="Windows"):
        collector.execute_collection()
    assert not list(tmp_path.iterdir())


def test_missing_or_failed_execution_cannot_become_offline_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    proof = {"report_type": "rc_check_evidence", "target_version": "16.0.1", "kind": "automated",
             "source": source(), "source_after": source(), "actual_run": True, "status": "PASS",
             "exit_code": 0, "timed_out": False,
             "checks": {collector.gate.requirement_id("automated", "version_consistency"): True}}
    monkeypatch.setattr(collector.gate, "automated_command", lambda *a, **k: None)
    monkeypatch.setattr(collector.gate, "validate_backend_evidence", lambda *a, **k: None)
    for field, value in (("actual_run", False), ("status", "FAIL"), ("exit_code", 1),
                         ("exit_code", False), ("timed_out", True), ("checks", {}),
                         ("checks", {collector.gate.requirement_id("automated", "version_consistency"): 1})):
        invalid = {**proof, field: value}
        path = tmp_path / "checks.json"
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError):
            collector.validate_checks(path, source(), ["version_consistency"])


def test_staging_copies_only_explicit_evidence_and_keeps_private_material_local(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    public = write(tmp_path, "build/v1601-evidence/accepted/offline/run/checks.json")
    private = write(tmp_path, "build/v1601-evidence/accepted/offline/run/private/database.json", b'{"private":true}')
    target = tmp_path / "build/v1601-evidence/offline-download/run"
    summary = {"rc_eligible": False, "formal_release": False, "files": [public]}
    collector.stage_evidence(target, summary, [public])
    assert (target / public["path"]).read_bytes() == (tmp_path / public["path"]).read_bytes()
    assert not (target / private["path"]).exists()
    saved = json.loads((target / "offline-summary.json").read_text())
    assert saved["rc_eligible"] is False and saved["formal_release"] is False
    with pytest.raises(ValueError, match="fresh"):
        collector.stage_evidence(target, summary, [public])


@pytest.mark.parametrize("name", ["../escape.json", "/absolute.json", SYNTHETIC_DRIVE + "host.json",
                                    "build/v1601-evidence/accepted/private/data.json"])
def test_staging_rejects_escaping_and_private_references(monkeypatch, tmp_path, name):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    target = tmp_path / "build/v1601-evidence/offline-download/run"
    with pytest.raises(ValueError):
        collector.stage_evidence(target, {}, [{"path": name, "sha256": "a" * 64}])
    assert not target.exists()


@pytest.mark.parametrize("content", [json.dumps({"path": SYNTHETIC_DRIVE + "/".join(("Users", "synthetic-test-only", "private.json"))}).encode(),
                                       b'{"authorization":"Bearer private-credential"}'])
def test_privacy_failure_keeps_raw_evidence_and_creates_no_download(monkeypatch, tmp_path, content):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    ref = write(tmp_path, "build/v1601-evidence/accepted/offline/run/checks.json", content)
    target = tmp_path / "build/v1601-evidence/offline-download/run"
    with pytest.raises(ValueError):
        collector.stage_evidence(target, {}, [ref])
    assert (tmp_path / ref["path"]).read_bytes() == content
    assert not target.exists()


def test_evidence_tamper_is_rejected_before_download(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    ref = write(tmp_path, "build/v1601-evidence/accepted/offline/run/checks.json")
    (tmp_path / ref["path"]).write_bytes(b'{"modified":true}')
    target = tmp_path / "build/v1601-evidence/offline-download/run"
    with pytest.raises(ValueError, match="changed"):
        collector.stage_evidence(target, {}, [ref])
    assert not target.exists()


def test_workflow_has_no_live_secret_or_formal_acceptance_artifact():
    workflow = (ROOT / ".github/workflows/rc-offline.yml").read_text()
    assert "contents: read" in workflow
    assert "secrets." not in workflow and "live_model" not in workflow
    assert "path: build/v1601-evidence/offline-download/" in workflow
    assert "Siyi-RC-Acceptance" not in workflow and "rc_evidence_run_id" not in workflow
    assert "actions/upload-artifact@" in workflow
    assert "if: always()" not in workflow.split("actions/upload-artifact@")[0].split("- name: Upload")[-1]


def test_failed_collection_reports_blocked_without_private_error_output(monkeypatch, capsys):
    def fail():
        raise ValueError(SYNTHETIC_DRIVE + "/".join(("Users", "private-user", "credential-path")))
    monkeypatch.setattr(collector, "execute_collection", fail)
    assert collector.main(["--execute"]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "BLOCKED" and value["rc_eligible"] is False
    assert "private-user" not in json.dumps(value)
