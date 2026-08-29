from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import ProviderDescriptor
from app.providers.configuration import (
    OLLAMA_BASE_URL,
    ProviderConfiguration,
    load_provider_configuration,
    save_provider_configuration,
    validate_provider_configuration,
)
from app.providers.model_routing import apply_manual_override, classify_task
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.provider import provider_health as transport_health
from app.providers.registry import get_provider, provider_descriptor


def test_provider_descriptor_has_one_canonical_v2_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))

    deepseek = provider_descriptor("deepseek")
    ollama = provider_descriptor(
        "ollama",
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model="llama3.2:3b",
        ),
    )
    compatible = provider_descriptor(
        "openai_compatible",
        ProviderConfiguration(
            provider_id="openai_compatible",
            base_url="http://127.0.0.1:1234/v1",
            model="local-model",
        ),
    )

    expected_fields = {
        "provider_id",
        "display_name",
        "provider_type",
        "endpoint",
        "model",
        "credential_policy",
        "capabilities",
        "timeout",
        "retry_policy",
        "local",
        "health_strategy",
    }
    assert set(asdict(deepseek)) == expected_fields
    assert deepseek.credential_policy == "required"
    assert ollama.model == "llama3.2:3b"
    assert ollama.credential_policy == "forbidden" and ollama.local is True
    assert compatible.credential_policy == "forbidden" and compatible.local is True
    assert isinstance(compatible, ProviderDescriptor)


@pytest.mark.parametrize("model", ["llama3.2:3b", "qwen3:8b", "gemma3:4b-it-qat"])
def test_ollama_configuration_accepts_any_safe_selected_model(model: str) -> None:
    configured = validate_provider_configuration(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model=model,
            max_tokens=4096,
        )
    )
    assert configured.model == model


@pytest.mark.parametrize("model", ["", " ", "../model", "model\nsecond"])
def test_ollama_configuration_rejects_missing_or_unsafe_model(model: str) -> None:
    with pytest.raises(ValueError, match="model|模型"):
        validate_provider_configuration(
            ProviderConfiguration(
                provider_id="ollama",
                base_url=OLLAMA_BASE_URL,
                model=model,
                max_tokens=4096,
            )
        )


def test_openai_compatible_local_provider_never_forwards_cloud_credential(monkeypatch) -> None:
    captured: dict = {}

    async def completion(*_args, **kwargs):
        captured.update(kwargs)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr("app.providers.openai_compatible.transport_completion", completion)
    provider = OpenAICompatibleProvider(
        ProviderConfiguration(
            provider_id="openai_compatible",
            base_url="http://localhost:1234/v1",
            model="local-model",
        )
    )

    assert asyncio.run(provider.chat([{"role": "user", "content": "hello"}], api_key="cloud-secret"))["content"] == "ok"
    assert captured["api_key"] is None
    assert captured["credential_policy"] == "forbidden"
    assert captured["provider_id_override"] == "openai_compatible"


@pytest.mark.parametrize(
    "configuration",
    [
        ProviderConfiguration(
            provider_id="deepseek",
            base_url="https://user:secret@api.deepseek.com",
        ),
        ProviderConfiguration(provider_id="mock", model="credential-must-not-persist"),
        ProviderConfiguration(
            provider_id="openai_compatible",
            base_url="https://user:secret@models.example/v1",
            model="example-model",
        ),
    ],
)
def test_provider_configuration_never_accepts_embedded_credentials(configuration: ProviderConfiguration) -> None:
    with pytest.raises(ValueError):
        validate_provider_configuration(configuration)


def test_local_health_omits_authorization_header_even_when_cloud_secret_exists(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    async def guarded(_client, _method, _url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.providers.provider.guarded_request", guarded)
    monkeypatch.setattr("app.providers.provider.settings.deepseek_api_key", "must-not-leak")

    result = asyncio.run(
        transport_health(
            "request-secret",
            base_url="http://127.0.0.1:1234/v1",
            model="local-model",
            allow_private_provider=True,
            credential_policy="forbidden",
        )
    )

    assert result["status"] == "ok"
    assert "Authorization" not in captured["headers"]


def test_provider_configuration_round_trip_preserves_openai_compatible(monkeypatch, tmp_path) -> None:
    path = tmp_path / "provider.json"
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(path))
    expected = ProviderConfiguration(
        provider_id="openai_compatible",
        base_url="https://models.example.test/v1",
        model="example-chat",
        timeout_seconds=42,
        max_retries=1,
    )

    save_provider_configuration(expected)

    assert load_provider_configuration() == expected
    assert get_provider().id == "openai_compatible"
    assert "credential" not in path.read_text(encoding="utf-8").casefold()


def test_pre_v15_ollama_configuration_without_model_migrates_in_memory(monkeypatch, tmp_path) -> None:
    path = tmp_path / "provider.json"
    path.write_text(
        json.dumps(
            {
                "provider_id": "ollama",
                "base_url": OLLAMA_BASE_URL,
                "max_tokens": 512,
                "unknown_legacy_field": "preserved-outside-runtime",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(path))

    migrated = load_provider_configuration()

    assert migrated.model == "qwen3:4b"
    assert migrated.max_tokens == 2048
    # Loading is non-destructive; explicit save remains the only write path.
    assert json.loads(path.read_text(encoding="utf-8"))["unknown_legacy_field"] == "preserved-outside-runtime"


def test_model_routing_uses_selected_local_model_instead_of_deepseek_tiers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    save_provider_configuration(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model="llama3.2:3b",
            max_tokens=4096,
        )
    )

    assert classify_task("总结文件").model == "llama3.2:3b"
    assert classify_task("修复复杂安全问题").model == "llama3.2:3b"

    monkeypatch.setattr("app.providers.model_routing.settings.model_routing_enabled", False)
    assert classify_task("任意任务").model == "llama3.2:3b"
    route = apply_manual_override(classify_task("任意任务"), preferred_model="deepseek-chat")
    assert route.model == "llama3.2:3b"


def test_deepseek_runtime_uses_descriptor_model(monkeypatch) -> None:
    captured: dict = {}

    async def completion(*_args, **kwargs):
        captured.update(kwargs)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr("app.providers.deepseek.transport_completion", completion)
    # The registry preview uses the configured DeepSeek identity. Exercise the
    # provider directly with a non-default model to prove the descriptor and
    # transport cannot drift apart.
    from app.providers.deepseek import DeepSeekProvider

    provider = DeepSeekProvider(
        ProviderConfiguration(provider_id="deepseek", model="deepseek-reasoner")
    )
    assert asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))["content"] == "ok"
    assert captured["model"] == "deepseek-reasoner"


def test_provider_policy_exposes_one_consistent_provider_model_and_routing_identity(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    save_provider_configuration(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model="llama3.2:3b",
            max_tokens=4096,
        )
    )

    with TestClient(app) as client:
        payload = client.get("/api/provider/policy").json()

    assert payload["provider"]["id"] == "ollama"
    assert payload["provider"]["default_model"] == "llama3.2:3b"
    assert payload["provider"]["descriptor"]["model"] == "llama3.2:3b"
    assert payload["provider"]["descriptor"]["credential_policy"] == "forbidden"
    assert set(payload["models"].values()) == {"llama3.2:3b"}
    assert {item["model"] for item in payload["capability_matrix"]} == {"llama3.2:3b"}

    with TestClient(app) as client:
        descriptor = client.get("/api/provider/descriptor").json()
    assert descriptor["provider_id"] == "ollama"
    assert descriptor["model"] == "llama3.2:3b"
    assert descriptor["credential_policy"] == "forbidden"
