from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "v14-ollama-live-evidence.py"
SPEC = importlib.util.spec_from_file_location("v14_ollama_live_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeSupport:
    def __init__(self, *, release: bool = True) -> None:
        self.identity = SimpleNamespace(
            pid=4567,
            creation_date="20260812123000.000000+480",
            executable_name="ollama.exe",
            command_sha256="C" * 64,
        )
        self.process_reads = 0
        self.process_snapshots: list[list[SimpleNamespace]] | None = None
        self.actions: list[str] = []
        self.release = release
        self.manifest = {
            "mode": "non_mutating_api_intent",
            "regular_file_count": 2,
            "regular_file_bytes": 100,
            "whole_tree_sha256": "d" * 64,
            "links_followed": False,
        }

    @staticmethod
    def owner_sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def identity_as_dict(value: SimpleNamespace) -> dict[str, object]:
        return {
            "pid": value.pid,
            "creation_date": value.creation_date,
            "executable_name": value.executable_name,
            "command_sha256": value.command_sha256,
        }

    def list_ollama_processes(self) -> list[SimpleNamespace]:
        self.process_reads += 1
        if self.process_snapshots is not None:
            index = min(self.process_reads - 1, len(self.process_snapshots) - 1)
            return list(self.process_snapshots[index])
        if self.process_reads in {1, 6}:
            return []
        return [self.identity]

    def ollama_model_store_fingerprint(self, _root: Path) -> dict[str, object]:
        return dict(self.manifest)

    @staticmethod
    def safe_controller_result(value: dict[str, object]) -> dict[str, object]:
        return value

    @staticmethod
    def safe_service_summary(value: dict[str, object]) -> dict[str, object]:
        return value

    @staticmethod
    def validate_ollama_url(value: str) -> str:
        return value

    @staticmethod
    def successful_test_owned_stop(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        service = value.get("service")
        return (
            value.get("ok") is True
            and value.get("action") == "stop_test_owned_service"
            and isinstance(service, dict)
            and service.get("stopped") is True
            and service.get("status") == "INSTALLED_STOPPED"
            and service.get("api_healthy") is False
            and service.get("managed_pid") is None
            and service.get("listener_pid") is None
            and service.get("managed_port") is None
        )

    def run_model_controller(self, _runtime: Path, **kwargs: object) -> dict[str, object]:
        action = str(kwargs["action"])
        self.actions.append(action)
        owner = str(kwargs["test_owned_service_id"])
        service = {
            "status": "MANAGED_RUNNING",
            "mode": "managed",
            "api_healthy": True,
            "owner_sha256": self.owner_sha256(owner),
            "managed_pid": 4567,
            "listener_pid": 4567,
            "managed_port": 11435,
            "managed_state_unowned": False,
        }
        if action == "start_test_owned_service":
            return {"ok": True, "action": action, "service": service}
        if action == "preflight":
            return {
                "ok": True,
                "action": action,
                "service": service,
                "running": [],
                "installed": [{"name": "qwen3:4b", "size": 100, "loaded": False}],
            }
        if action == "preload_qwen":
            return {
                "ok": True,
                "action": action,
                "service": service,
                "loaded": {
                    "status": "LOADED",
                    "model": "qwen3:4b",
                    "keep_alive": "5m",
                    "load_ms": 123.5,
                    "resources_before": {"ollama_rss_bytes": 100},
                    "resources_after": {"ollama_rss_bytes": 200},
                },
                "running_after": [{"name": "qwen3:4b", "size_vram": 10}],
            }
        if action == "unload_owned_qwen":
            return {
                "ok": True,
                "action": action,
                "service": service,
                "unloaded": {
                    "status": "UNLOADED",
                    "model": "qwen3:4b",
                    "resource_release_observed": {
                        "ollama_rss_delta_bytes": 50 if self.release else 0,
                        "gpu_free_delta_bytes": 0,
                    },
                },
                "running_before": [{"name": "qwen3:4b"}],
                "running_after": [],
            }
        if action == "stop_test_owned_service":
            return {
                "ok": True,
                "action": action,
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
            }
        raise AssertionError(action)


def arguments(tmp_path: Path) -> Namespace:
    store = tmp_path / "model-store"
    (store / "manifests").mkdir(parents=True)
    return Namespace(
        output="build/v1400-evidence/raw/a26.json",
        execute_live_test_owned_ollama=True,
        test_owned_ollama_service_id="a26-test-owned-ollama-identity-001",
        model_store=store,
        test_owned_ollama_executable=None,
        startup_timeout_seconds=30.0,
        controller_timeout_seconds=240.0,
    )


def source() -> dict[str, object]:
    return {
        "source_version": "13.0.0",
        "source_commit": "A" * 40,
        "source_tree_fingerprint": "B" * 64,
        "workspace_clean": False,
    }


def test_output_is_bounded_and_immutable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "build" / "v1400-evidence").mkdir(parents=True)
    output = MODULE.resolve_output_path(
        "build/v1400-evidence/raw/a26.json", repository_root=root
    )
    MODULE.write_json_immutable(output, {"status": "PASS"})
    with pytest.raises(MODULE.OllamaEvidenceError, match="overwrite"):
        MODULE.write_json_immutable(output, {"status": "FAIL"})
    with pytest.raises(MODULE.OllamaEvidenceError, match="build/v1400-evidence"):
        MODULE.resolve_output_path("a26.json", repository_root=root)


def test_structured_live_run_cannot_be_an_all_skip_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support = FakeSupport()
    port_reads = iter((False, False))
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "load_support", lambda: support)
    monkeypatch.setattr(MODULE, "source_identity", source)
    monkeypatch.setattr(MODULE, "loopback_port_listening", lambda: next(port_reads))
    monkeypatch.setattr(MODULE.os, "name", "nt")
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    output = tmp_path / "build" / "v1400-evidence" / "raw" / "a26.json"
    output.parent.mkdir(parents=True)

    report = MODULE.run_live_evidence(arguments(tmp_path), output)

    assert report["status"] == "PASS"
    assert report["actual_run"] is True
    assert report["test_summary"]["collected"] == len(MODULE.TEST_NAMES)
    assert report["test_summary"]["executed"] == len(MODULE.TEST_NAMES)
    assert report["test_summary"]["passed"] == len(MODULE.TEST_NAMES)
    assert report["test_summary"]["failed"] == 0
    assert report["test_summary"]["skipped"] == 0
    assert support.actions == [
        "start_test_owned_service",
        "preflight",
        "preload_qwen",
        "unload_owned_qwen",
        "stop_test_owned_service",
    ]
    assert report["checks"]["qwen3_4b_resource_release_observed"]["passed"] is True
    assert report["cleanup"]["ollama_processes_after_stop"] == []
    assert report["scope"]["model_store"] == "NON_MUTATING_API_INTENT_FULL_TREE_VERIFIED"
    assert report["checks"]["non_mutating_model_store_full_tree_fingerprinted"]["passed"] is True


def test_preflight_rejects_link_following_model_store_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support = FakeSupport()
    support.manifest["links_followed"] = True
    port_reads = iter((False, False))
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "load_support", lambda: support)
    monkeypatch.setattr(MODULE, "source_identity", source)
    monkeypatch.setattr(MODULE, "loopback_port_listening", lambda: next(port_reads))
    monkeypatch.setattr(MODULE.os, "name", "nt")
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    output = tmp_path / "build" / "v1400-evidence" / "raw" / "a26-linked-store.json"
    output.parent.mkdir(parents=True)

    report = MODULE.run_live_evidence(arguments(tmp_path), output)

    assert report["status"] == "FAIL"
    assert report["actual_run"] is False
    assert support.actions == []
    assert report["checks"]["non_mutating_model_store_full_tree_fingerprinted"]["passed"] is False
    assert report["failure"]["message"] == "non_mutating_model_store_full_tree_fingerprinted"


def test_zero_resource_release_keeps_a26_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support = FakeSupport(release=False)
    port_reads = iter((False, False))
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "load_support", lambda: support)
    monkeypatch.setattr(MODULE, "source_identity", source)
    monkeypatch.setattr(MODULE, "loopback_port_listening", lambda: next(port_reads))
    monkeypatch.setattr(MODULE.os, "name", "nt")
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    output = tmp_path / "build" / "v1400-evidence" / "raw" / "a26-fail.json"
    output.parent.mkdir(parents=True)

    report = MODULE.run_live_evidence(arguments(tmp_path), output)

    assert report["status"] == "FAIL"
    assert report["actual_run"] is True
    assert report["checks"]["qwen3_4b_unloaded"]["passed"] is True
    assert report["checks"]["qwen3_4b_resource_release_observed"]["passed"] is False
    assert report["test_summary"]["failed"] >= 1


def test_cleanup_waits_for_a_transient_ollama_runner_to_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support = FakeSupport()
    runner = SimpleNamespace(
        pid=4568,
        creation_date="20260812123001.000000+480",
        executable_name="ollama.exe",
        command_sha256="D" * 64,
    )
    support.process_snapshots = [
        [],
        [support.identity],
        [support.identity],
        [support.identity],
        [support.identity, runner],
        [support.identity],
        [],
    ]
    port_reads = iter((False, False))
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "load_support", lambda: support)
    monkeypatch.setattr(MODULE, "source_identity", source)
    monkeypatch.setattr(MODULE, "loopback_port_listening", lambda: next(port_reads))
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(MODULE.os, "name", "nt")
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    output = tmp_path / "build" / "v1400-evidence" / "raw" / "a26-runner.json"
    output.parent.mkdir(parents=True)

    report = MODULE.run_live_evidence(arguments(tmp_path), output)

    assert report["status"] == "PASS"
    assert report["cleanup"]["only_test_owned_before_stop"] is True
    assert report["cleanup"]["processes_before_stop"] == [
        support.identity_as_dict(support.identity)
    ]
    assert report["cleanup"]["post_unload_process_settlement"]["poll_count"] == 2
    assert report["checks"]["only_test_owned_ollama_before_stop"]["passed"] is True


def test_cleanup_refuses_to_pass_with_an_unquiesced_extra_ollama_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support = FakeSupport()
    runner = SimpleNamespace(
        pid=4568,
        creation_date="20260812123001.000000+480",
        executable_name="ollama.exe",
        command_sha256="D" * 64,
    )
    support.process_snapshots = [
        [],
        [support.identity],
        [support.identity],
        [support.identity],
        [support.identity, runner],
        [],
    ]
    port_reads = iter((False, False))
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "load_support", lambda: support)
    monkeypatch.setattr(MODULE, "source_identity", source)
    monkeypatch.setattr(MODULE, "loopback_port_listening", lambda: next(port_reads))
    monkeypatch.setattr(MODULE, "POST_UNLOAD_PROCESS_QUIESCENCE_SECONDS", 0.0)
    monkeypatch.setattr(MODULE.os, "name", "nt")
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    output = tmp_path / "build" / "v1400-evidence" / "raw" / "a26-extra.json"
    output.parent.mkdir(parents=True)

    report = MODULE.run_live_evidence(arguments(tmp_path), output)

    assert report["status"] == "FAIL"
    assert report["cleanup"]["only_test_owned_before_stop"] is False
    assert report["checks"]["only_test_owned_ollama_before_stop"]["passed"] is False
    assert report["test_summary"]["failed"] >= 1
    assert support.actions[-1] == "stop_test_owned_service"
