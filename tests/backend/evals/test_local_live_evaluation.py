"""Synthetic provider-boundary regressions; no model/credential/API is used."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import importlib.util
import json
import os
from pathlib import Path

import pytest

from app.config import settings
from app.evals import local_live, runner, source_identity
from app.evals.cli import main
from app.evals.comparison import report_integrity_errors
from app.evals.contracts import digest
from app.evals.loader import load_tasks
from app.evals.models import EvalTaskResult
from app.providers.capabilities import provider_identity
from app.providers.configuration import ProviderConfiguration
from app.providers.effective_capabilities import configuration_identity, context_profile_key

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ProviderConfiguration(provider_id="ollama", base_url="http://127.0.0.1:11434", model="qwen3:4b", max_retries=0)
SOURCE = {"source_version": "16.0.1", "source_commit": "a" * 40, "workspace_clean": True, "source_tree_fingerprint": "B" * 64}


def configuration_file(tmp_path, payload=None):
    path = tmp_path / "selected.json"
    path.write_text(json.dumps(asdict(CONFIG) if payload is None else payload), encoding="utf-8")
    return path


def effective(**changes):
    return {"provider_id": "ollama", "endpoint_hash": provider_identity(CONFIG.base_url)[1], "model": CONFIG.model,
            "configuration_hash": configuration_identity(CONFIG), "identity_hash": "a" * 64, "model_digest": "b" * 64,
            "deployment_hash": "c" * 64, "context_window_tokens": 16384, "runtime_context_window": 16384,
            "local": True, "observation_stale": False, "declared_capabilities": {"native_tool_calls": True},
            "observed_capabilities": {"native_tool_calls": "supported"},
            "explicit_profile": {"context_window_tokens": 16384}, **changes}


def snapshot(**changes):
    value = effective()
    result = {key: value[key] for key in local_live.SNAPSHOT_FIELDS - {"capabilities_sha256", "endpoint_sha256"}}
    result.update(endpoint_sha256=local_live._endpoint_digest(CONFIG), capabilities_sha256="d" * 64)
    return {**result, **changes}


def synthetic_result(spec):
    return EvalTaskResult(task_id=spec.id, title="synthetic test only", status="passed", expected_outcome=spec.expected_outcome,
        expectation_met=True, runtime_status="completed", started_at="2026-11-05T00:00:00+00:00",
        finished_at="2026-11-05T00:00:01+00:00", duration_ms=10,
        metrics={"model_calls": 1, "tool_calls": 0, "tool_errors": 0, "retry_count": 0, "total_tokens": 8,
                 "human_interventions": 0, "tests_run": 0, "tests_passed": 0, "builds_run": 0, "builds_passed": 0,
                 "permission_violations": 0, "sandbox_violations": 0})


def test_explicit_configuration_is_read_only_and_credential_free(tmp_path):
    path = configuration_file(tmp_path)
    original = path.read_bytes()
    config, sha = local_live.read_configuration(path)
    assert config == CONFIG and len(sha) == 64 and path.read_bytes() == original


@pytest.mark.parametrize("mutation", [
    {"provider_id": "deepseek", "base_url": "https://api.deepseek.com"},
    {"provider_id": "openai_compatible", "base_url": "https://example.invalid"},
    {"provider_id": "mock", "base_url": "", "model": ""},
    {"base_url": "http://127.0.0.1:11435"}, {"base_url": "http://192.0.2.1:11434"},
    {"base_url": "https://127.0.0.1:11434"}, {"base_url": "http://127.0.0.1:11434/v1"},
    {"base_url": "http://user:secret@127.0.0.1:11434"}, {"base_url": "http://127.0.0.1:11434?key=secret"},
    {"base_url": ""}, {"api_key": "not-a-key"}, {"unknown": True}, {"timeout_seconds": True},
    {"max_tokens": "2048"}, {"max_retries": 0.5}, {"allow_tools": 1}, {"model": "../other"},
])
def test_bad_or_cloud_configuration_rejected(tmp_path, mutation):
    with pytest.raises(ValueError):
        local_live.read_configuration(configuration_file(tmp_path, {**asdict(CONFIG), **mutation}))


@pytest.mark.parametrize("raw", [b"[]", b"{}", b"not json", b'\xff', b'"value"', b'{"provider_id":"deepseek","provider_id":"ollama"}', b" " * 16385])
def test_non_object_duplicate_invalid_and_oversize_inputs_rejected(tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    with pytest.raises((ValueError, OSError)):
        local_live.read_configuration(path)


def test_hard_link_configuration_refused(tmp_path):
    source = configuration_file(tmp_path)
    alias = tmp_path / "alias.json"
    os.link(source, alias)
    with pytest.raises(ValueError):
        local_live.read_configuration(alias)


def test_reader_actual_byte_bound_is_independent_of_stat(tmp_path, monkeypatch):
    path = configuration_file(tmp_path)
    original_open = Path.open
    class ChangedRead:
        def __enter__(self):
            self.stream = original_open(path, "rb")
            return self
        def fileno(self):
            return self.stream.fileno()
        def read(self, limit):
            assert limit == local_live.MAX_CONFIGURATION_BYTES + 1
            return b"x" * limit
        def __exit__(self, *args):
            self.stream.close()
    monkeypatch.setattr(Path, "open", lambda selected, *args, **kwargs: ChangedRead() if selected == path else original_open(selected, *args, **kwargs))
    with pytest.raises(ValueError, match="bounded reader"):
        local_live.read_configuration(path)


def test_local_stage_restores_profile_and_environment_even_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "user-profile.json"))
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_PROFILES_JSON", '{"original":{}}')
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "true")
    previous_env = {name: os.environ.get(name) for name in ("AGENT_PROVIDER_CONFIG_PATH", "AGENT_MODEL_CONTEXT_PROFILES_JSON", "SIYI_ALLOW_PAID_API")}
    original_profile = settings.model_context_profiles_json
    with pytest.raises(RuntimeError):
        with local_live.isolated_configuration(CONFIG, 16384, tmp_path):
            assert Path(os.environ["AGENT_PROVIDER_CONFIG_PATH"]).parent == tmp_path
            assert json.loads(os.environ["AGENT_MODEL_CONTEXT_PROFILES_JSON"]) == {context_profile_key(CONFIG): {"context_window_tokens": 16384}}
            assert settings.model_context_profiles_json == os.environ["AGENT_MODEL_CONTEXT_PROFILES_JSON"]
            assert os.environ["SIYI_ALLOW_PAID_API"] == "false"
            raise RuntimeError("synthetic boundary")
    assert settings.model_context_profiles_json == original_profile
    assert all(os.environ.get(key) == value for key, value in previous_env.items())
    assert not (tmp_path / "user-profile.json").exists()


@pytest.mark.parametrize("mutation", [
    {"provider_id": "deepseek"}, {"model": "other:1b"}, {"local": False}, {"observation_stale": True},
    {"configuration_hash": "e" * 64}, {"endpoint_hash": "e" * 16}, {"deployment_hash": None}, {"identity_hash": "fake"},
    {"model_digest": None}, {"runtime_context_window": 8192}, {"context_window_tokens": 8192},
])
def test_actual_resolver_unknown_or_wrong_identity_cannot_be_used(tmp_path, monkeypatch, mutation):
    configuration_file(tmp_path)
    async def diagnostics(_self):
        return {"status": "ok"}
    monkeypatch.setattr(local_live.OllamaProvider, "diagnostics", diagnostics)
    monkeypatch.setattr(local_live, "resolve_effective_capabilities", lambda **_: type("Observed", (), {"public": lambda self: effective(**mutation)})())
    with local_live.isolated_configuration(CONFIG, 16384, tmp_path):
        with pytest.raises(ValueError):
            asyncio.run(local_live.resident_snapshot(CONFIG, 16384))


def test_resident_observation_accepts_matching_actual_resolver_shape(tmp_path, monkeypatch):
    async def diagnostics(_self):
        return {"status": "ok"}
    monkeypatch.setattr(local_live.OllamaProvider, "diagnostics", diagnostics)
    monkeypatch.setattr(local_live, "resolve_effective_capabilities", lambda **_: type("Observed", (), {"public": lambda self: effective()})())
    with local_live.isolated_configuration(CONFIG, 16384, tmp_path):
        observed = asyncio.run(local_live.resident_snapshot(CONFIG, 16384))
    assert local_live.snapshot_errors(observed) == []
    assert observed["configuration_hash"] == configuration_identity(CONFIG)


def test_metadata_error_blocks_without_runtime_or_cloud_fallback(tmp_path, monkeypatch):
    async def diagnostics(_self):
        raise RuntimeError("synthetic metadata failure, never print this private value")
    async def forbidden(*args, **kwargs):
        pytest.fail("Unknown local metadata must stop before Runtime dispatch")
    monkeypatch.setattr(local_live.OllamaProvider, "diagnostics", diagnostics)
    monkeypatch.setattr(runner, "_execute_task", forbidden)
    result = asyncio.run(runner.run_evaluation(label="metadata-error", mode="live_model", task_ids=["project-structure"],
        output_root=tmp_path / "out", configuration_path=configuration_file(tmp_path), context_window=16384))
    assert result.status == "blocked" and result.metrics["model_call_count"] == 0
    assert "synthetic metadata failure" not in result.model_dump_json()


def test_explicit_local_executes_without_fake_key_and_binds_original_source(tmp_path, monkeypatch):
    path = configuration_file(tmp_path)
    calls = []
    async def observe(config, context):
        assert config == CONFIG and context == 16384
        return snapshot()
    async def execute(spec, stage, *, mode, api_key, local_live=False):
        assert mode == "live_model" and api_key is None and local_live is True
        assert Path(os.environ["AGENT_PROVIDER_CONFIG_PATH"]).parent == stage
        calls.append(spec.id)
        return synthetic_result(spec)
    monkeypatch.setattr(local_live, "resident_snapshot", observe)
    monkeypatch.setattr(runner, "_execute_task", execute)
    monkeypatch.setattr(source_identity, "repository_identity", lambda: SOURCE)
    result = asyncio.run(runner.run_evaluation(label="synthetic-local", mode="live_model", task_ids=["project-structure"],
        output_root=tmp_path / "output", configuration_path=path, context_window=16384, capture_source=True))
    assert calls == ["project-structure"] and result.status == "completed"
    assert result.provider["model"] == CONFIG.model
    assert local_live.provider_errors(result.provider) == []
    assert result.configuration["source_identity"] == result.configuration["source_identity_after"] == SOURCE
    assert not report_integrity_errors(result)


@pytest.mark.parametrize("phase", ["initial", "between", "after"])
def test_identity_failure_stops_future_dispatch_and_cannot_claim_pass(tmp_path, monkeypatch, phase):
    path = configuration_file(tmp_path)
    observations, dispatches = [], []
    async def observe(config, context):
        observations.append(True)
        if phase == "initial":
            raise ValueError("synthetic missing resident")
        changed = (phase == "between" and len(observations) >= 2) or (phase == "after" and len(observations) >= 3)
        return snapshot(deployment_hash=("e" if changed else "c") * 64)
    async def execute(spec, stage, **kwargs):
        dispatches.append(spec.id)
        return synthetic_result(spec)
    monkeypatch.setattr(local_live, "resident_snapshot", observe)
    monkeypatch.setattr(runner, "_execute_task", execute)
    result = asyncio.run(runner.run_evaluation(label="synthetic-drift", mode="live_model", task_ids=["project-structure", "fix-clear-bug"],
        output_root=tmp_path / "output", configuration_path=path, context_window=16384))
    assert result.status == ("blocked" if phase == "initial" else "invalid")
    assert len(dispatches) == {"initial": 0, "between": 1, "after": 2}[phase]
    assert len(result.task_results) == 2
    assert local_live.provider_errors(result.provider)


def test_legacy_cloud_missing_key_remains_blocked_without_any_network(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Missing legacy key must not reach any Runtime/model call")
    monkeypatch.setattr(runner, "_run_runtime", forbidden)
    result = asyncio.run(runner.run_evaluation(label="no-cloud-key", mode="live_model", api_key=None,
        task_ids=["project-structure"], output_root=tmp_path))
    assert result.status == "blocked" and result.metrics["model_call_count"] == 0
    assert result.task_results[0].failed_step == "provider_key"


@pytest.mark.parametrize("configuration", ["selected.json", ""])
def test_local_cli_never_forwards_cloud_environment_or_settings_key(tmp_path, monkeypatch, capsys, configuration):
    from app.evals import cli
    monkeypatch.setenv("AGENT_DEEPSEEK_API_KEY", "synthetic-never-used")
    monkeypatch.setattr(settings, "deepseek_api_key", "synthetic-never-used")
    async def observe(**kwargs):
        assert kwargs["api_key"] is None
        raise ValueError("synthetic stop before any model call")
    monkeypatch.setattr(cli, "run_evaluation", observe)
    assert main(["run", "--mode", "live_model", "--label", "synthetic", "--configuration", configuration, "--context-window", "16384"]) == 2
    assert "synthetic-never-used" not in capsys.readouterr().out


@pytest.mark.parametrize("mode,key,window", [("scripted_runtime", None, 16384), ("adversarial", None, 16384),
    ("live_model", "synthetic-not-a-real-key", 16384), ("live_model", None, None), ("live_model", None, True),
    ("live_model", None, 0), ("live_model", None, 10_000_001)])
def test_wrong_mode_key_or_context_never_dispatches(tmp_path, monkeypatch, mode, key, window):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid explicit local invocation reached Runtime")
    monkeypatch.setattr(runner, "_execute_task", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(runner.run_evaluation(label="invalid-invocation", mode=mode, api_key=key, task_ids=["project-structure"],
            output_root=tmp_path / "out", configuration_path=configuration_file(tmp_path), context_window=window))


def test_live_runtime_uses_original_run_chat_with_no_canned_completion(tmp_path, monkeypatch):
    calls = []
    async def actual_boundary(payload, api_key, *, completion_fn, limits):
        calls.append(payload)
        assert api_key is None and completion_fn is None
        assert limits.max_tool_calls == load_tasks(task_ids=["project-structure"])[0].max_tool_calls
        return {"task_status": "completed", "content": "synthetic network-boundary response"}
    monkeypatch.setattr(runner, "run_chat", actual_boundary)
    monkeypatch.setattr(runner, "_task_trace", lambda ids: {"tasks": [], "tool_runs": [], "model_runs": []})
    spec = load_tasks(task_ids=["project-structure"])[0]
    result = asyncio.run(runner._run_runtime(spec, tmp_path, mode="live_model", api_key=None))
    assert len(calls) == 1 and result[0] == "completed"


def test_local_identity_survives_original_public_projection_and_rejects_tampering():
    # Reuse the existing public protocol's synthetic fixture, not a real run.
    spec = importlib.util.spec_from_file_location("local_eval_public_fixture", ROOT / "tests/backend/release/test_rc_eval_public_v160.py")
    fixture = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(fixture)
    raw = fixture.raw_report()
    raw["mode"], raw["layer"] = "live_model", "autonomous_model"
    raw["provider"] = local_live.public_provider(CONFIG, "f" * 64, 16384, snapshot(), snapshot())
    public = fixture.public.build_public_report(fixture.encoded(raw), projector=fixture.projector())
    view = fixture.public.as_evaluation_view(public)
    assert view.provider == raw["provider"]
    assert not report_integrity_errors(view)
    corrupted = deepcopy(public)
    corrupted["measurement"]["provider"]["local_live"]["after"]["deployment_hash"] = "e" * 64
    with pytest.raises(ValueError):
        fixture.public.as_evaluation_view(corrupted)


def test_input_configuration_change_and_source_change_are_not_hidden(tmp_path, monkeypatch):
    path = configuration_file(tmp_path)
    async def observe(config, context):
        return snapshot()
    async def execute(spec, stage, **kwargs):
        path.write_text(json.dumps({**asdict(CONFIG), "max_tokens": 128}), encoding="utf-8")
        return synthetic_result(spec)
    values = iter((SOURCE, {**SOURCE, "source_commit": "b" * 40}))
    monkeypatch.setattr(source_identity, "repository_identity", lambda: next(values))
    monkeypatch.setattr(local_live, "resident_snapshot", observe)
    monkeypatch.setattr(runner, "_execute_task", execute)
    report = asyncio.run(runner.run_evaluation(label="input-drift", mode="live_model", task_ids=["project-structure"],
        output_root=tmp_path / "out", configuration_path=path, context_window=16384, capture_source=True))
    assert report.status == "invalid"
    assert report.configuration["source_identity"] != report.configuration["source_identity_after"]


@pytest.mark.parametrize("mutation", ["endpoint", "model", "deployment", "digest", "context", "extra", "protocol"])
def test_forged_local_public_identity_rejected(mutation):
    provider = local_live.public_provider(CONFIG, "f" * 64, 16384, snapshot(), snapshot())
    if mutation == "endpoint": provider["endpoint_sha256"] = "e" * 64
    elif mutation == "model": provider["model"] = "other:1b"
    elif mutation == "deployment": provider["local_live"]["after"]["deployment_hash"] = "e" * 64
    elif mutation == "digest": provider["local_live"]["before"]["model_digest"] = None
    elif mutation == "context": provider["local_live"]["after"]["context_window_tokens"] = 8192
    elif mutation == "extra": provider["local_live"]["before"]["url"] = "https://example.invalid/private"
    else: provider["local_live"]["protocol"] = "not-the-protocol"
    assert local_live.provider_errors(provider)
