"""No-network proof that benchmark and Runtime use the same identity/budget."""
import asyncio
import json
import uuid

import pytest

from app.config import settings
from app.evals.local_model_benchmark.adapters import OllamaBenchmarkAdapter, OfflineBenchmarkAdapter
from app.evals.local_model_benchmark.cases import default_benchmark_cases
from app.evals.local_model_benchmark.models import LocalModelBenchmarkReport
from app.evals.local_model_benchmark.runner import run_local_model_benchmark
from app.providers.effective_capabilities import context_profile_key, resolve_effective_capabilities
from app.providers.provider import ProviderError


def adapter(monkeypatch):
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "ollama")
    model = "benchmark-fixture-" + uuid.uuid4().hex
    class Provider:
        id, base_url = "ollama", "http://127.0.0.1:11434"
        calls = []
        async def chat(self, messages, **kwargs):
            self.calls.append(kwargs)
            return {"content": "4"}
    provider = Provider()
    provider.model = model
    async def inventory():
        return [{"name": model, "digest": "sha256:" + "a" * 64}]
    return OllamaBenchmarkAdapter(model_id=model, provider=provider, inventory_loader=inventory)


def test_unknown_window_never_dispatches_benchmark_model(monkeypatch, tmp_path):
    item = adapter(monkeypatch)
    monkeypatch.setattr(settings, "model_context_profiles_json", "{}")
    with pytest.raises(ProviderError) as error:
        asyncio.run(item.invoke(default_benchmark_cases()[0]))
    assert error.value.error_type == "context_window_unknown"
    assert item.provider.calls == []
    report = asyncio.run(run_local_model_benchmark(adapter=item, output_directory=tmp_path, case_ids=["basic-plain-answer"]))
    assert not report.actual_model_run
    assert report.case_results[0].status == "blocked"


def test_small_window_and_snapshot_are_shared(monkeypatch):
    item = adapter(monkeypatch)
    monkeypatch.setattr(settings, "model_context_profiles_json", json.dumps({
        context_profile_key(item.config): {"context_window_tokens": 1024, "max_output_tokens": 32},
    }))
    asyncio.run(item.invoke(default_benchmark_cases()[0]))
    assert item.provider.calls[0]["max_tokens"] == 32
    metadata = asyncio.run(item.metadata())
    snapshot = resolve_effective_capabilities(configuration=item.config).public()
    assert metadata.effective_capabilities == snapshot
    assert metadata.effective_capabilities["context_window_tokens"] == 1024
    assert set(snapshot) >= {"declared_capabilities", "observed_capabilities", "explicit_profile", "identity_hash"}


def test_new_target_ids_do_not_relabel_legacy_evidence(tmp_path):
    report = asyncio.run(run_local_model_benchmark(adapter=OfflineBenchmarkAdapter(), output_directory=tmp_path, case_ids=["basic-plain-answer"]))
    assert report.target_version == "16.0.0"
    assert report.benchmark_version == "local-model-v2"
    assert report.evidence_layer == "in_memory_model_simulation"
    assert report.case_results[0].requirement_id == "V160-LOCAL-MODEL-V2-BASIC-PLAIN-ANSWER"
    assert not report.release_gate_eligible
    legacy = report.model_dump()
    for field in ("target_version", "evidence_layer"):
        legacy.pop(field)
    legacy["benchmark_version"] = "local-model-v1"
    legacy["release_gate_eligible"] = True
    old = LocalModelBenchmarkReport.model_validate(legacy)
    assert old.target_version == "15.0.0"
    assert not old.eligible_for("16.0.0", "local-model-v2", "current")
