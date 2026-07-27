from __future__ import annotations

from app.database import rows
from app.providers.capabilities import provider_capability_matrix, provider_identity, record_provider_observation


def test_provider_observations_merge_capabilities_without_storing_endpoint() -> None:
    base_url = "https://provider-capabilities.example/api"
    record_provider_observation(base_url=base_url, model="model-a", status="ok", latency_ms=120, streaming=True)
    matrix = record_provider_observation(base_url=base_url, model="model-a", status="ok", latency_ms=80, native_tool_calls=True)

    stored = rows("SELECT * FROM provider_capabilities WHERE model='model-a'")[-1]
    assert matrix["capabilities"]["streaming"] == "supported"
    assert matrix["capabilities"]["native_tool_calls"] == "supported"
    assert matrix["sample_count"] == 2
    assert matrix["success_count"] == 2
    assert matrix["latency_ms"] == 80
    assert base_url not in str(stored)
    assert len(stored["endpoint_hash"]) == 16


def test_provider_capability_matrix_is_scoped_by_endpoint_and_model() -> None:
    record_provider_observation(base_url="https://one.example", model="shared", status="ok", streaming=True)
    second = provider_capability_matrix(base_url="https://two.example", model="shared")

    assert second["source"] == "client_contract"
    assert second["capabilities"]["streaming"] == "unknown"
    assert provider_identity("https://one.example") != provider_identity("https://two.example")
