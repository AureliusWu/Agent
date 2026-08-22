from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-package-size.py"
SPEC = importlib.util.spec_from_file_location("package_size_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_package_size_budget_accepts_bounded_candidate() -> None:
    # The v14 Faster-Whisper runtime is installed with the application, while
    # the much larger model weights remain an explicit post-install download.
    decision = MODULE.size_decision(67_285_142, 38_215_432)
    assert decision["total_pass"] is True
    assert decision["increase_pass"] is True
    assert decision["passed"] is True


def test_package_size_budget_rejects_total_or_growth_overage() -> None:
    total = MODULE.size_decision(67 * 1024 * 1024, 40 * 1024 * 1024)
    assert total["total_pass"] is False
    assert total["passed"] is False

    growth = MODULE.size_decision(65 * 1024 * 1024, 35 * 1024 * 1024)
    assert growth["increase_pass"] is False
    assert growth["passed"] is False
