from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-package-size.py"
SPEC = importlib.util.spec_from_file_location("package_size_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_package_size_budget_accepts_bounded_candidate() -> None:
    decision = MODULE.size_decision(50 * 1024 * 1024, 26 * 1024 * 1024)
    assert decision["total_pass"] is True
    assert decision["increase_pass"] is True
    assert decision["passed"] is True


def test_package_size_budget_rejects_total_or_growth_overage() -> None:
    total = MODULE.size_decision(56 * 1024 * 1024, 40 * 1024 * 1024)
    assert total["total_pass"] is False
    assert total["passed"] is False

    growth = MODULE.size_decision(54 * 1024 * 1024, 28 * 1024 * 1024)
    assert growth["increase_pass"] is False
    assert growth["passed"] is False
