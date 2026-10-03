import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import settings
from app.main import app
from app.plugins.contracts import PluginCall
from app.plugins.registry import PLUGIN_LIBRARY, PluginLibrary
from app.permissions import authorize
from app.runtime.executor import ExecutorToolCall, LocalWindowsExecutor
from app.tools.registry import BASE_TOOLS, REGISTRY, select_model_tools


@pytest.fixture(autouse=True)
def disable_speech(monkeypatch):
    monkeypatch.setattr(settings, "speech_enabled", False)


def test_every_builtin_tool_has_one_owner_and_unchanged_contract() -> None:
    specs = PLUGIN_LIBRARY.tool_specs()
    assert len(specs) == len(REGISTRY) == len(BASE_TOOLS) == 47
    assert {spec.name for spec in specs} == set(REGISTRY)
    assert {plugin.category for plugin in PLUGIN_LIBRARY.plugins.values()} == {
        "hear",
        "speak",
        "read",
        "write",
        "execute",
        "memory",
    }
    for spec in specs:
        assert PLUGIN_LIBRARY.owner(spec.name).id == spec.plugin_id
        assert spec.openai()["function"]["parameters"]["additionalProperties"] is False
    assert REGISTRY["write_file"].required == ("path", "content", "expected_version_token")
    assert REGISTRY["read_file"].risk == "low"
    assert REGISTRY["synthesize_speech"].concurrency_policy == "exclusive"


def test_composition_rejects_duplicate_plugins_tools_and_owner_mismatch() -> None:
    plugin = next(iter(PLUGIN_LIBRARY.plugins.values()))
    with pytest.raises(ValueError, match="Duplicate plugin"):
        PluginLibrary((plugin, plugin))
    with pytest.raises(ValueError, match="Duplicate plugin tool"):
        PluginLibrary((plugin, replace(plugin, id="builtin.duplicate")))
    with pytest.raises(ValueError, match="owner mismatch"):
        PluginLibrary(
            (replace(plugin, tools=(replace(plugin.tools[0], plugin_id="builtin.other"),)),)
        )
    with pytest.raises(TypeError):
        PLUGIN_LIBRARY.plugins["builtin.other"] = plugin


def test_catalog_explains_unconfigured_speech_and_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "speech_api_key", SecretStr("test-private-speech-value"))
    catalog = PLUGIN_LIBRARY.catalog(str(tmp_path))
    speech = [item for item in catalog if item["category"] in {"hear", "speak"}]
    assert all(item["status"] == "unconfigured" for item in speech)
    assert "test-private-speech-value" not in json.dumps(catalog)
    names = {item["function"]["name"] for item in select_model_tools("转写音频并语音合成", (), [])}
    assert not names & {"transcribe_audio", "synthesize_speech"}
    assert {"read_file", "write_file"} <= set(
        asyncio.run(LocalWindowsExecutor().capabilities()).tools
    )
    assert "transcribe_audio" not in asyncio.run(LocalWindowsExecutor().capabilities()).tools


def test_configured_voice_is_labelled_unverified_and_selected(monkeypatch, tmp_path):
    for name, value in {
        "speech_enabled": True,
        "speech_base_url": "https://speech.example/v1",
        "speech_api_key": SecretStr("test-private-speech-value"),
        "speech_transcription_model": "stt-test",
        "speech_synthesis_model": "tts-test",
        "speech_voice": "test-voice",
    }.items():
        monkeypatch.setattr(settings, name, value)
    catalog = PLUGIN_LIBRARY.catalog(str(tmp_path))
    assert all(
        item["status"] == "configured" for item in catalog if item["category"] in {"hear", "speak"}
    )
    names = {item["function"]["name"] for item in select_model_tools("转写音频并语音合成", (), [])}
    assert {"transcribe_audio", "synthesize_speech"} <= names


def test_plugin_dispatch_retains_validation_sandbox_and_receipts(tmp_path: Path):
    (tmp_path / "a.txt").write_text("sample", encoding="utf-8")
    call = ExecutorToolCall(
        workspace=str(tmp_path),
        mode="full",
        name="read_file",
        arguments={"path": "a.txt"},
        tool_call_id="read-sample",
        approved_actions=[],
        approval_scope="once",
        conversation_id=1,
        task_id="plugin-test",
        mcp_routes={},
    )
    executor = LocalWindowsExecutor()
    result = asyncio.run(executor.execute_tool(call))
    assert result.result["content"] == "sample"
    assert result.result["plugin"]["id"] == "builtin.reading"
    assert result.receipt.operation_kind == "read"
    invalid = asyncio.run(
        executor.execute_tool(replace(call, arguments={"path": "a.txt", "unexpected": 1}))
    )
    assert invalid.result["error_code"] == "invalid_arguments"
    escaped = asyncio.run(
        executor.execute_tool(replace(call, arguments={"path": "../outside.txt"}))
    )
    assert not escaped.result["success"]


def test_unconfigured_voice_does_not_invoke_provider(tmp_path, monkeypatch):
    def unexpected():
        raise AssertionError("An unconfigured speech plugin must not call a provider")

    monkeypatch.setattr("app.plugins.voice.speech_adapter", unexpected)
    call = PluginCall(
        str(tmp_path),
        "full",
        "transcribe_audio",
        {"path": "audio.wav"},
        "speech-test",
        [],
        "once",
        1,
        "plugin-test",
        authorize,
    )
    result = asyncio.run(PLUGIN_LIBRARY.execute(call))
    assert result.result["status"] == "unavailable"


def test_plugin_api_is_authenticated_and_reports_actual_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "catalog-test-token")
    client = TestClient(app)
    assert client.get("/api/plugins").status_code == 401
    response = client.get(
        "/api/plugins",
        params={"workspace": str(tmp_path)},
        headers={"X-Agent-Api-Token": "catalog-test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tool_count"] == 47
    assert len(body["plugins"]) == 11
    assert (
        client.get(
            "/api/plugins/builtin.hearing", headers={"X-Agent-Api-Token": "catalog-test-token"}
        ).json()["status"]
        == "unconfigured"
    )
    assert (
        client.get(
            "/api/plugins/missing", headers={"X-Agent-Api-Token": "catalog-test-token"}
        ).status_code
        == 404
    )
