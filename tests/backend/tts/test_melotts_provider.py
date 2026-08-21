from __future__ import annotations

import asyncio

import pytest

from app.tts.providers.melotts import MeloTTSProvider


@pytest.mark.parametrize(
    ("available", "error", "expected_status", "expected_availability"),
    [
        (False, "MELOTTS_PACKAGE_MISSING", "unavailable", "not_installed"),
        (False, "MELOTTS_MODEL_NOT_CONFIRMED", "unavailable", "model_not_confirmed"),
        (True, None, "ok", "ready"),
    ],
)
def test_health_reports_contract_version_without_claiming_optional_runtime_is_installed(
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    error: str | None,
    expected_status: str,
    expected_availability: str,
) -> None:
    provider = MeloTTSProvider()
    monkeypatch.setattr(provider, "_available", lambda: (available, error))

    health = asyncio.run(provider.health_check())

    assert health["provider"] == "melotts"
    assert health["version"] == "optional-local"
    assert health["maturity"] == "experimental"
    assert health["status"] == expected_status
    assert health["availability"] == expected_availability
    assert health["error_code"] == error
