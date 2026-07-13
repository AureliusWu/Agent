import asyncio

from app.provider import provider_health


def test_provider_health_reports_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("app.provider.settings.deepseek_api_key", "")
    result = asyncio.run(provider_health())
    assert result["status"] == "unconfigured"
    assert result["latency_ms"] is None
