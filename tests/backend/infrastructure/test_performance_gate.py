from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-v8-performance-gate.py"
SPEC = importlib.util.spec_from_file_location("performance_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_absolute_performance_limit_remains_authoritative_without_baseline() -> None:
    decision = MODULE.performance_decision([1900, 1901, 1902], [])
    assert decision["absolute_pass"] is True
    assert decision["paired_pass"] is False
    assert decision["passed"] is True

    failed = MODULE.performance_decision([1904, 1905, 1906], [])
    assert failed["absolute_pass"] is False
    assert failed["passed"] is False


def test_paired_same_host_baseline_allows_only_bounded_relative_drift() -> None:
    passing = MODULE.performance_decision([2660, 2670, 2680], [2530, 2540, 2550])
    assert passing["absolute_pass"] is False
    assert passing["paired_pass"] is True
    assert passing["paired_limit_ms"] == int(2540 * 1.15)

    failing = MODULE.performance_decision([2930, 2940, 2950], [2530, 2540, 2550])
    assert failing["paired_pass"] is False
    assert failing["passed"] is False


def test_required_paired_mode_never_falls_back_to_absolute_threshold() -> None:
    missing = MODULE.performance_decision(
        [1800, 1810, 1820],
        [],
        require_paired_baseline=True,
    )
    assert missing["absolute_pass"] is True
    assert missing["paired_pass"] is False
    assert missing["passed"] is False

    paired = MODULE.performance_decision(
        [1800, 1810, 1820],
        [1700, 1710, 1720],
        require_paired_baseline=True,
    )
    assert paired["paired_pass"] is True
    assert paired["passed"] is True
