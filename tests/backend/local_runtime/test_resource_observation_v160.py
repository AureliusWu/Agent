from datetime import datetime

import pytest

from app.local_runtime import resource_coordinator as resource_module


@pytest.mark.parametrize("available", [None, 0, 617377792, 8 * 1024**3])
def test_resource_sample_preserves_unknown_zero_and_timestamp(monkeypatch, available):
    monkeypatch.setattr(resource_module, "_memory", lambda: (16 * 1024**3, available))
    monkeypatch.setattr(resource_module, "_gpu", lambda: (None, None))
    monkeypatch.setattr(resource_module, "_process_rss", lambda _name: None)
    monkeypatch.setattr(resource_module, "_process_rss_by_pid", lambda _pid: None)
    monkeypatch.setattr(resource_module, "_process_rss_by_pids", lambda _pids: None)
    coordinator = resource_module.ResourceCoordinator()
    snapshot = coordinator.snapshot()
    assert snapshot["system_available_bytes"] == available
    assert datetime.fromisoformat(snapshot["sampled_at"]).tzinfo is not None
    decision = coordinator.assess_admission("stt", snapshot=snapshot)
    assert decision["sampled_at"] == snapshot["sampled_at"]
    assert decision["system_memory_observed"] == (available is not None)
    assert decision["minimum_available_ram_bytes"] == 2 * 1024**3


def test_unknown_historical_sample_does_not_invent_fresh_timestamp():
    decision = resource_module.ResourceCoordinator().assess_admission("stt", snapshot={"system_available_bytes": None})
    assert decision["sampled_at"] is None


def test_negative_resource_value_is_unknown_not_zero():
    decision = resource_module.ResourceCoordinator().assess_admission("stt", snapshot={"system_available_bytes": -1})
    assert decision["system_available_bytes"] is None
    assert decision["system_memory_observed"] is False
