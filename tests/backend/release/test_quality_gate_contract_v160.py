from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "desktop" / "frontend"
REQUIRED_FRONTEND_GATES = {
    "lint", "build", "test:security", "test:desktop", "test:voice-errors", "test:build-info",
}


def _reachable_npm_scripts(source: str, scripts: dict[str, str]) -> set[str]:
    # Match executable lines/&& clauses, not mentions in comments or step names.
    pending = re.findall(r"(?:^|&&|run:)\s*npm run ([\w:-]+)", source, re.MULTILINE)
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        assert name in scripts, f"missing npm script: {name}"
        if name in reached:
            continue
        reached.add(name)
        pending.extend(re.findall(r"(?:^|&&)\s*npm run ([\w:-]+)", scripts[name]))
    return reached


def test_default_backend_coverage_is_the_same_eighty_percent_as_local_gate() -> None:
    config = tomllib.loads((ROOT / "siyi" / "pyproject.toml").read_text(encoding="utf-8"))
    options = shlex.split(config["tool"]["pytest"]["ini_options"]["addopts"])
    default = [int(item.split("=", 1)[1]) for item in options if item.startswith("--cov-fail-under=")]
    local = (ROOT / "scripts" / "test.ps1").read_text(encoding="utf-8")
    overrides = [int(value) for value in re.findall(r"--cov-fail-under=(\d+)", local)]
    assert default == [80], "PR CI and bare pytest must not silently use the old 70% gate"
    assert config["tool"]["coverage"]["report"]["fail_under"] == 80
    assert all(value == 80 for value in overrides), "local override must not diverge from the shared gate"


@pytest.mark.parametrize("entrypoint", (".github/workflows/ci.yml", "scripts/test.ps1"))
def test_ci_and_local_entrypoints_reach_all_frontend_gates(entrypoint: str) -> None:
    scripts = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))["scripts"]
    reached = _reachable_npm_scripts((ROOT / entrypoint).read_text(encoding="utf-8"), scripts)
    assert REQUIRED_FRONTEND_GATES <= reached, sorted(REQUIRED_FRONTEND_GATES - reached)
    assert "voice-capture-errors.test.mjs" in scripts["test:voice-errors"]
    assert "build-info.test.ts" in scripts["test:build-info"]


def test_ci_installs_cross_stack_toolchain_before_backend_contract_tests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert workflow.index("actions/setup-node@") < workflow.index("run: npm ci")
    assert workflow.index("run: npm ci") < workflow.index("run: python ../scripts/rc_test_evidence.py")


def test_ci_adversarial_run_checks_case_outcomes_not_only_process_completion() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    commands = [line for line in workflow.splitlines() if "run: python -m app.evals.cli run" in line]
    assert commands and all("--require-passed" in line for line in commands)


def _node() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.fail("Node.js is required to verify the frontend gate; install the locked CI toolchain")
    return executable


def _run_node(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_node(), "--experimental-strip-types", str(script)],
        cwd=script.parents[1], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=45, check=False,
    )


def _frontend_fixture(tmp_path: Path) -> Path:
    """Only synthetic source copies, never an installed app or release artifact."""
    target = tmp_path / "frontend-contract-fixture"
    (target / "scripts").mkdir(parents=True)
    (target / "src" / "hooks").mkdir(parents=True)
    (target / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    return target


def test_build_identity_gate_rejects_a_real_mutation(tmp_path: Path) -> None:
    fixture = _frontend_fixture(tmp_path)
    script = fixture / "scripts" / "build-info.test.ts"
    shutil.copyfile(FRONTEND / "scripts" / script.name, script)
    module = fixture / "src" / "buildInfoModel.ts"
    original = (FRONTEND / "src" / module.name).read_text(encoding="utf-8")
    module.write_text(original, encoding="utf-8")
    control = _run_node(script)
    assert control.returncode == 0, control.stderr
    mutation = "const consistent = new Set(Object.values(buildIds)).size === 1"
    assert mutation in original, "update mutation when the identity contract moves"
    module.write_text(original.replace(mutation, "const consistent = true"), encoding="utf-8")
    broken = _run_node(script)
    assert broken.returncode != 0
    assert "AssertionError" in broken.stderr and "mismatch" in broken.stderr


@pytest.mark.parametrize("newline", ("\n", "\r\n"), ids=("lf", "crlf"))
def test_static_voice_contracts_are_checkout_newline_portable(tmp_path: Path, newline: str) -> None:
    fixture = _frontend_fixture(tmp_path)
    # Include only imports and source inputs required by these existing tests.
    paths = (
        "scripts/voice-capture-sse-race.test.ts", "scripts/voice-session-submit.test.ts",
        "scripts/tts-playback-settlement.test.ts", "src/voiceSseSessionFence.ts",
        "src/voiceCapturePrivacy.ts", "src/voiceAutoSendPolicy.ts", "src/ttsPlaybackSettlement.ts",
        "src/hooks/useVoiceCapture.ts", "src/hooks/useAgentChat.ts",
        "src/components/chat/VoiceInputControl.tsx", "src/components/providers/LocalAiPanel.tsx",
    )
    for relative in paths:
        destination = fixture / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((FRONTEND / relative).read_text(encoding="utf-8"), encoding="utf-8", newline=newline)
    for script in fixture.joinpath("scripts").glob("*.test.ts"):
        result = _run_node(script)
        assert result.returncode == 0, f"{script.name} ({newline!r}): {result.stderr}"


def test_voice_race_gate_rejects_lost_failed_terminal_state(tmp_path: Path) -> None:
    fixture = _frontend_fixture(tmp_path)
    for source in (FRONTEND / "src").glob("*.ts"):
        shutil.copyfile(source, fixture / "src" / source.name)
    hook = fixture / "src" / "hooks" / "useVoiceCapture.ts"
    original = (FRONTEND / "src" / "hooks" / hook.name).read_text(encoding="utf-8")
    hook.write_text(original, encoding="utf-8")
    script = fixture / "scripts" / "voice-capture-errors.test.mjs"
    # Reuse the installed compiler without a node_modules copy/junction; all
    # production source mutation remains isolated in the test-owned directory.
    compiler = FRONTEND / "node_modules" / "typescript" / "lib" / "typescript.js"
    assert compiler.is_file(), "npm ci must precede cross-stack contract tests"
    source = (FRONTEND / "scripts" / script.name).read_text(encoding="utf-8")
    script.write_text(source.replace("from 'typescript'", f"from {json.dumps(compiler.as_uri())}"), encoding="utf-8")
    control = _run_node(script)
    assert control.returncode == 0, control.stderr
    mutation = "resources.terminalFailed = eventStatus === 'FAILED'"
    assert mutation in original, "update mutation when terminal ownership changes"
    hook.write_text(original.replace(mutation, "resources.terminalFailed = false"), encoding="utf-8")
    broken = _run_node(script)
    assert broken.returncode != 0
    assert "AssertionError" in broken.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell path contract")
def test_candidate_path_checks_need_no_historical_artifact_sentinels(tmp_path: Path) -> None:
    """Fresh synthetic checkout, not evidence that any installer/model ran."""
    fixture = tmp_path / "fresh-checkout"
    scripts = fixture / "scripts"
    scripts.mkdir(parents=True)
    (fixture / "VERSION").write_text("16.0.0\n", encoding="ascii")
    for name in ("v14-candidate-runtime-smoke.ps1", "evidence-root.ps1"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows CI must provide PowerShell for path boundary checks"
    arguments = [
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(scripts / "v14-candidate-runtime-smoke.ps1"), "-EvidenceVersion", "16.0.0-testfixture",
        "-RunId", "synthetic-path-contract",
    ]
    preflight = subprocess.run(
        [*arguments, "-ValidateOnly"], cwd=fixture, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout.lstrip("\ufeff"))["status"] == "VALID"
    assert not (fixture / "build").exists(), "validation must not create artifact fixtures"
    attempted = subprocess.run(
        arguments, cwd=fixture, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert attempted.returncode != 0
    assert "A required runtime artifact or its verified performance baseline is missing" in attempted.stderr
    assert not list(fixture.rglob("*.exe")), "a fake binary must never stand in for accepted release evidence"
