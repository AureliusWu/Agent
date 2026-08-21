from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MAX_FUNCTION_LINES = 120
SCRIPTS = (
    ROOT / "scripts" / "v14-stt-live-evidence.py",
    ROOT / "scripts" / "v14-stt-privacy-live-evidence.py",
    ROOT / "scripts" / "v14-windows-tts-live-evidence.py",
    ROOT / "scripts" / "v14-ollama-live-evidence.py",
    ROOT / "scripts" / "v14-a23-endurance-evidence.py",
)
TARGET_FUNCTIONS = {
    "v14-stt-live-evidence.py": {
        "run_live_evidence",
        "download_small_model_via_api",
        "verify_prior_small_download_receipt",
    },
    "v14-stt-privacy-live-evidence.py": {"run_live_evidence"},
    "v14-windows-tts-live-evidence.py": {
        "_exercise_production_manager",
        "run_live_evidence",
    },
    "v14-ollama-live-evidence.py": {"run_live_evidence"},
    "v14-a23-endurance-evidence.py": {"run_live_evidence", "evaluate_report"},
}
LARGE_MODULE_RESPONSIBILITY_REVIEWS = {
    "v14-stt-live-evidence.py": (
        "One immutable STT evidence schema coordinates source identity, the formal API, "
        "owned sidecar cleanup, model receipt verification, and a fixed synthetic corpus. "
        "The security-sensitive lifecycle remains colocated while each phase is a bounded helper."
    ),
    "v14-stt-privacy-live-evidence.py": (
        "One A24 privacy boundary owns the synthetic probe, isolated sidecar, persisted-artifact "
        "scan, diagnostic ZIP scan, redaction, and cleanup so raw text or paths never cross modules."
    ),
    "v14-a23-endurance-evidence.py": (
        "One A23 operator-assisted evidence boundary owns candidate identity, PID-safe lifecycle, "
        "challenge confirmations, resource sampling, database corroboration, and test-runtime cleanup. "
        "The phases are split into bounded helpers while the privacy-sensitive lifetime stays auditable."
    ),
}


def _top_level_functions(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: node.end_lineno - node.lineno + 1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno is not None
    }


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_v14_evidence_functions_stay_within_review_threshold(script: Path) -> None:
    oversized = {
        name: lines
        for name, lines in _top_level_functions(script).items()
        if lines > MAX_FUNCTION_LINES
    }

    assert oversized == {}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_v14_named_complexity_targets_remain_explicit_and_bounded(script: Path) -> None:
    functions = _top_level_functions(script)
    expected = TARGET_FUNCTIONS[script.name]

    assert expected <= functions.keys()
    assert all(functions[name] <= MAX_FUNCTION_LINES for name in expected)


def test_v14_large_evidence_modules_have_a_recorded_responsibility_review() -> None:
    large_modules = {
        script.name
        for script in SCRIPTS
        if len(script.read_text(encoding="utf-8").splitlines()) > 800
    }

    assert large_modules == LARGE_MODULE_RESPONSIBILITY_REVIEWS.keys()
    assert all(
        len(explanation) >= 120
        for explanation in LARGE_MODULE_RESPONSIBILITY_REVIEWS.values()
    )
