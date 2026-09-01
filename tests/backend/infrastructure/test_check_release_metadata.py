from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-release-metadata.py"
SPEC = importlib.util.spec_from_file_location("check_release_metadata", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

REQUIRED_RELEASE_DOCUMENTS = (
    "RELEASE_NOTES.md",
    "IMPLEMENTATION_FEEDBACK.md",
    "TEST_MATRIX.json",
    "RELEASE_STATUS.json",
    "EVIDENCE_MANIFEST.json",
    "MODEL_BENCHMARK_REPORT.md",
    "MODEL_BENCHMARK.json",
)
CORE_RELEASE_JSON_DOCUMENTS = (
    "TEST_MATRIX.json",
    "RELEASE_STATUS.json",
    "EVIDENCE_MANIFEST.json",
)

REQUIRED_V15_RELEASE_GATES = {
    "automated": (
        "python_full_tests",
        "coverage_80",
        "frontend_lint",
        "frontend_build",
        "frontend_security_tests",
        "rust_tests",
        "readonly_matrix",
        "recovery_matrix",
        "file_symlink_matrix",
        "mcp_contract_tests",
        "provider_contract_tests",
        "local_model_benchmark_basic",
        "version_consistency",
        "git_tag_consistency",
    ),
    "desktop": (
        "tauri_build",
        "nsis",
        "msi",
        "install",
        "launch",
        "sidecar_health",
        "upgrade",
        "uninstall",
    ),
    "manual": (
        "chat",
        "file_create_edit_move_delete_undo",
        "readonly",
        "ask",
        "ollama",
        "deepseek",
        "mcp",
        "voice_basic",
        "memory",
    ),
}


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("13.0.0\n", encoding="ascii")
    (root / "README.md").write_text("controlled source\n", encoding="utf-8")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "release-test@example.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "VERSION", "README.md"],
        ["git", "commit", "-m", "release metadata fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root


def ready_status() -> dict[str, object]:
    return {
        "implementation_status": "COMPLETE",
        "test_status": "READY",
        "distribution_status": "READY",
        "release_status": "READY",
        "source_commit": MODULE._git_head(),
    }


def ready_matrix() -> dict[str, object]:
    release_gates: dict[str, list[dict[str, object]]] = {}
    for category, gate_ids in REQUIRED_V15_RELEASE_GATES.items():
        release_gates[category] = []
        for gate_id in gate_ids:
            gate: dict[str, object] = {
                "id": gate_id,
                "status": "PASS",
                "evidence": [
                    {
                        "kind": category,
                        "actual_run": True,
                        "outcome": "PASS",
                    }
                ],
            }
            if category == "manual":
                gate["evidence"][0]["operator_attested"] = True
            if gate_id == "local_model_benchmark_basic":
                gate["actual_model_run"] = True
                gate["basic_suite_pass_rate"] = 1.0
                gate["model_digest"] = "a" * 64
            release_gates[category].append(gate)
    return {"schema_version": 5, "release_gates": release_gates}


def ready_benchmark(*, version: str = "13.0.0") -> dict[str, object]:
    return {
        "app_version": version,
        "actual_model_run": True,
        "provider": {"model_digest": "a" * 64},
        "metrics": {"suite_success_rates": {"basic": 1.0}},
    }


def release_checks(
    expected: str,
    status: dict[str, object],
    *,
    matrix: dict[str, object],
    require_tag: bool = False,
) -> list[str]:
    return MODULE._release_checks(
        expected,
        status,
        matrix=matrix,
        benchmark=ready_benchmark(version=expected),
        require_tag=require_tag,
    )


def write_machine_metadata(
    root: Path,
    *,
    version: str = "13.0.0",
    uv_version: str | None = None,
    frontend_lock_root_version: str | None = None,
    frontend_lock_package_version: str | None = None,
) -> None:
    uv_version = uv_version or version
    frontend_lock_root_version = frontend_lock_root_version or version
    frontend_lock_package_version = frontend_lock_package_version or version

    (root / "siyi" / "app").mkdir(parents=True)
    (root / "siyi" / "pyproject.toml").write_text(
        '[project]\nname = "aureliuswu-agent-backend"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "siyi" / "app" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "siyi" / "uv.lock").write_text(
        "version = 1\n\n[[package]]\n"
        'name = "aureliuswu-agent-backend"\n'
        f'version = "{uv_version}"\n',
        encoding="utf-8",
    )

    frontend = root / "desktop" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text(
        json.dumps({"name": "agent-frontend", "version": version}), encoding="utf-8"
    )
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "agent-frontend",
                "version": frontend_lock_root_version,
                "packages": {
                    "": {
                        "name": "agent-frontend",
                        "version": frontend_lock_package_version,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    tauri = root / "desktop" / "src-tauri"
    tauri.mkdir(parents=True)
    (tauri / "tauri.conf.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    (tauri / "Cargo.toml").write_text(
        f'[package]\nname = "app"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tauri / "Cargo.lock").write_text(
        f'[[package]]\nname = "app"\nversion = "{version}"\n', encoding="utf-8"
    )


def write_release_documents(root: Path, *, version: str = "13.0.0") -> Path:
    docs = root / "docs"
    release = docs / version
    release.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        f"当前版本：`{version}` test fixture\n", encoding="utf-8"
    )
    (docs / "CURRENT_ARCHITECTURE.md").write_text(
        f"司忆 `{version}` test fixture\n", encoding="utf-8"
    )
    (release / "RELEASE_NOTES.md").write_text(
        f"# 司忆 v{version} test fixture\n", encoding="utf-8"
    )
    (release / "IMPLEMENTATION_FEEDBACK.md").write_text(
        "# Implementation feedback\n", encoding="utf-8"
    )
    (release / "MODEL_BENCHMARK_REPORT.md").write_text(
        "# Model benchmark\n", encoding="utf-8"
    )
    (release / "MODEL_BENCHMARK.json").write_text("{}\n", encoding="utf-8")
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for filename in CORE_RELEASE_JSON_DOCUMENTS:
        payload = {
            "target_version": version,
            "source_version": version,
            "source_commit": source_commit,
        }
        if filename == "RELEASE_STATUS.json":
            payload["test_status"] = "NOT_READY"
        (release / filename).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return release


def test_machine_versions_checks_uv_and_both_frontend_lock_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root)
    monkeypatch.setattr(MODULE, "ROOT", root)

    versions = MODULE.machine_versions()

    assert versions["backend lock"] == "13.0.0"
    assert versions["frontend lock root"] == "13.0.0"
    assert versions["frontend lock"] == "13.0.0"
    assert len(versions) == 9


def test_machine_versions_exposes_uv_project_version_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root, uv_version="12.9.9")
    monkeypatch.setattr(MODULE, "ROOT", root)

    versions = MODULE.machine_versions()

    assert versions["backend package"] == "13.0.0"
    assert versions["backend lock"] == "12.9.9"


def test_machine_versions_exposes_frontend_lock_root_mismatch_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root, frontend_lock_root_version="12.9.9")
    monkeypatch.setattr(MODULE, "ROOT", root)

    versions = MODULE.machine_versions()

    assert versions["frontend lock root"] == "12.9.9"
    assert versions["frontend lock"] == "13.0.0"


def test_machine_versions_rejects_duplicate_uv_project_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root)
    uv_lock = root / "siyi" / "uv.lock"
    uv_lock.write_text(
        uv_lock.read_text(encoding="utf-8")
        + '\n[[package]]\nname = "aureliuswu-agent-backend"\nversion = "13.0.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "ROOT", root)

    with pytest.raises(RuntimeError, match="must define exactly one"):
        MODULE.machine_versions()


@pytest.mark.parametrize("missing", REQUIRED_RELEASE_DOCUMENTS)
def test_normal_metadata_requires_all_seven_release_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    root = repository(tmp_path)
    write_machine_metadata(root)
    release = write_release_documents(root)
    (release / missing).unlink()
    monkeypatch.setattr(MODULE, "ROOT", root)

    with pytest.raises(RuntimeError) as captured:
        MODULE.collected_versions("13.0.0")

    assert missing in str(captured.value)


@pytest.mark.parametrize("filename", CORE_RELEASE_JSON_DOCUMENTS)
@pytest.mark.parametrize("field", ("target_version", "source_version"))
def test_each_core_release_json_version_must_equal_version_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    field: str,
) -> None:
    root = repository(tmp_path)
    release = write_release_documents(root)
    path = release / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = "12.9.9"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(MODULE, "ROOT", root)

    with pytest.raises(RuntimeError) as captured:
        MODULE.evidence_versions()

    message = str(captured.value)
    assert filename in message
    assert field in message
    assert "VERSION=13.0.0" in message


def test_core_release_json_source_commits_must_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    release = write_release_documents(root)
    path = release / "EVIDENCE_MANIFEST.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_commit"] = "0" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(MODULE, "ROOT", root)

    with pytest.raises(RuntimeError, match="source commit mismatch"):
        MODULE.evidence_versions()


def test_release_preflight_main_does_not_require_generated_evidence_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    write_machine_metadata(root)
    docs = root / "docs"
    release = docs / "13.0.0"
    release.mkdir(parents=True)
    (root / "README.md").write_text(
        "当前版本：`13.0.0` test fixture\n", encoding="utf-8"
    )
    (docs / "CURRENT_ARCHITECTURE.md").write_text(
        "司忆 `13.0.0` test fixture\n", encoding="utf-8"
    )
    (release / "RELEASE_NOTES.md").write_text(
        "# 司忆 v13.0.0 test fixture\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "release preflight fixture"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "tag", "v13.0.0"], cwd=root, check=True, capture_output=True
    )
    monkeypatch.setattr(MODULE, "ROOT", root)
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["check-release-metadata.py", "--release-preflight"]
    )

    assert MODULE.main() == 0


def test_release_metadata_uses_the_current_version_generated_evidence_exclusion_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    docs = root / "docs" / "14.0.0"
    docs.mkdir(parents=True)
    for filename in (
        "TEST_MATRIX.json",
        "RELEASE_STATUS.json",
        "EVIDENCE_MANIFEST.json",
        "IMPLEMENTATION_FEEDBACK.md",
    ):
        (docs / filename).write_text("generated mirror\n", encoding="utf-8")
    raw = root / "build" / "v1400-evidence" / "raw" / "a20.json"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"status":"PASS"}\n', encoding="utf-8")

    assert release_checks(
        "14.0.0", ready_status(), matrix=ready_matrix()
    ) == []


def test_release_metadata_does_not_allow_a_previous_versions_generated_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    previous = root / "docs" / "12.0.0" / "TEST_MATRIX.json"
    previous.parent.mkdir(parents=True)
    previous.write_text("stale generated mirror\n", encoding="utf-8")

    errors = release_checks(
        "13.0.0", ready_status(), matrix=ready_matrix()
    )

    assert "official release metadata requires a clean worktree" in errors


def test_release_metadata_requires_an_exact_version_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

    errors = release_checks(
        "13.0.0", ready_status(), matrix=ready_matrix(), require_tag=True
    )
    assert any("must run from tag v13.0.0" in error for error in errors)

    subprocess.run(
        ["git", "tag", "v13.0.0"], cwd=root, check=True, capture_output=True
    )
    assert release_checks(
        "13.0.0", ready_status(), matrix=ready_matrix(), require_tag=True
    ) == []


def test_release_metadata_still_rejects_any_non_generated_dirty_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    generated = root / "docs" / "14.0.0" / "TEST_MATRIX.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated mirror\n", encoding="utf-8")
    (root / "docs" / "14.0.0" / "LOCAL_VOICE_INPUT.md").write_text(
        "not a generated evidence mirror\n", encoding="utf-8"
    )

    errors = release_checks(
        "14.0.0", ready_status(), matrix=ready_matrix()
    )

    assert "official release metadata requires a clean worktree" in errors


def test_release_metadata_rejects_ready_evidence_from_a_different_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    status = ready_status()
    status["source_commit"] = "0" * 40

    errors = release_checks("14.0.0", status, matrix=ready_matrix())

    assert any("source_commit must identify current HEAD or its evidence-only source" in error for error in errors)


def test_release_preflight_does_not_require_generated_evidence_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)

    assert MODULE._release_preflight_checks("13.0.0", require_tag=False) == []


def test_release_metadata_accepts_an_evidence_only_commit_bound_to_its_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    source_commit = MODULE._git_head()
    docs = root / "docs" / "13.0.0"
    docs.mkdir(parents=True)
    (docs / "TEST_MATRIX.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "docs/13.0.0/TEST_MATRIX.json"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "release evidence"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    status = ready_status()
    status["source_commit"] = source_commit

    assert release_checks("13.0.0", status, matrix=ready_matrix()) == []


def test_release_metadata_rejects_source_commit_when_code_changed_after_testing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    source_commit = MODULE._git_head()
    (root / "README.md").write_text("changed after testing\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "untested source change"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    status = ready_status()
    status["source_commit"] = source_commit

    errors = release_checks("13.0.0", status, matrix=ready_matrix())

    assert any("source_commit must identify current HEAD or its evidence-only source" in error for error in errors)


@pytest.mark.parametrize(
    ("category", "gate_id"),
    [
        (category, gate_id)
        for category, gate_ids in REQUIRED_V15_RELEASE_GATES.items()
        for gate_id in gate_ids
    ],
)
def test_release_gate_requires_every_v15_plan_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    category: str,
    gate_id: str,
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    matrix["release_gates"][category] = [
        gate
        for gate in matrix["release_gates"][category]
        if gate["id"] != gate_id
    ]

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any(category in error and gate_id in error for error in errors)


@pytest.mark.parametrize("status", [None, "SKIP", "NOT_RUN", "BLOCKED", "FAIL"])
def test_release_gate_rejects_every_non_pass_gate_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: object
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    matrix["release_gates"]["automated"][0]["status"] = status

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any("python_full_tests" in error and "PASS" in error for error in errors)


def test_release_gate_requires_schema_v5(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    matrix["schema_version"] = 4

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any("schema_version must be at least 5" in error for error in errors)


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        [],
        [{"kind": "automated", "actual_run": False, "outcome": "PASS"}],
        [{"kind": "manual", "actual_run": True, "outcome": "PASS"}],
        [{"kind": "automated", "actual_run": True, "outcome": "FAIL"}],
    ],
)
def test_release_gate_requires_actual_passing_category_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    evidence: object,
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    matrix["release_gates"]["automated"][0]["evidence"] = evidence

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any(
        "automated.python_full_tests" in error and "evidence" in error
        for error in errors
    )


def test_release_gate_rejects_automated_evidence_for_manual_deepseek(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    deepseek = next(
        gate
        for gate in matrix["release_gates"]["manual"]
        if gate["id"] == "deepseek"
    )
    deepseek["evidence"] = [
        {"kind": "automated", "actual_run": True, "outcome": "PASS"}
    ]

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any("manual.deepseek" in error and "manual" in error for error in errors)


def test_release_gate_rejects_unattested_manual_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    matrix["release_gates"]["manual"][0]["evidence"][0][
        "operator_attested"
    ] = False

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any("manual.chat" in error and "operator_attested" in error for error in errors)


@pytest.mark.parametrize("actual_model_run", [None, False, 1, "true"])
def test_release_gate_requires_a_real_local_model_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    actual_model_run: object,
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    local_model = next(
        gate
        for gate in matrix["release_gates"]["automated"]
        if gate["id"] == "local_model_benchmark_basic"
    )
    local_model["actual_model_run"] = actual_model_run

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any("actual_model_run=true" in error for error in errors)


@pytest.mark.parametrize("pass_rate", [None, 0.0, 0.99, 100, "1.0"])
def test_release_gate_requires_a_100_percent_local_model_basic_suite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pass_rate: object,
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    matrix = ready_matrix()
    local_model = next(
        gate
        for gate in matrix["release_gates"]["automated"]
        if gate["id"] == "local_model_benchmark_basic"
    )
    local_model["basic_suite_pass_rate"] = pass_rate

    errors = release_checks("13.0.0", ready_status(), matrix=matrix)

    assert any("basic_suite_pass_rate=1.0" in error for error in errors)


def test_model_benchmark_must_match_the_release_version() -> None:
    benchmark = ready_benchmark(version="12.9.9")

    errors = MODULE._model_benchmark_checks(
        "13.0.0", ready_matrix(), benchmark
    )

    assert any("app_version must match VERSION=13.0.0" in error for error in errors)


@pytest.mark.parametrize("actual_model_run", [None, False, 1, "true"])
def test_model_benchmark_must_record_an_actual_model_run(
    actual_model_run: object,
) -> None:
    benchmark = ready_benchmark()
    benchmark["actual_model_run"] = actual_model_run

    errors = MODULE._model_benchmark_checks(
        "13.0.0", ready_matrix(), benchmark
    )

    assert any("MODEL_BENCHMARK actual_model_run=true" in error for error in errors)


@pytest.mark.parametrize("model_digest", [None, "", "   ", 123])
def test_model_benchmark_requires_a_non_empty_model_digest(
    model_digest: object,
) -> None:
    benchmark = ready_benchmark()
    benchmark["provider"]["model_digest"] = model_digest

    errors = MODULE._model_benchmark_checks(
        "13.0.0", ready_matrix(), benchmark
    )

    assert any("provider.model_digest" in error for error in errors)


@pytest.mark.parametrize("basic_pass_rate", [None, False, 0.99, 100, "1.0"])
def test_model_benchmark_requires_a_100_percent_basic_suite(
    basic_pass_rate: object,
) -> None:
    benchmark = ready_benchmark()
    benchmark["metrics"]["suite_success_rates"]["basic"] = basic_pass_rate

    errors = MODULE._model_benchmark_checks(
        "13.0.0", ready_matrix(), benchmark
    )

    assert any(
        "metrics.suite_success_rates.basic=1.0" in error for error in errors
    )


def test_model_benchmark_must_match_the_test_matrix_local_model_gate() -> None:
    benchmark = ready_benchmark()
    matrix = ready_matrix()
    local_model = next(
        gate
        for gate in matrix["release_gates"]["automated"]
        if gate["id"] == "local_model_benchmark_basic"
    )
    local_model["model_digest"] = "b" * 64

    errors = MODULE._model_benchmark_checks("13.0.0", matrix, benchmark)

    assert any("model_digest must match" in error for error in errors)


def test_model_benchmark_and_test_matrix_match_when_both_are_real_and_basic_passes(
) -> None:
    assert MODULE._model_benchmark_checks(
        "13.0.0", ready_matrix(), ready_benchmark()
    ) == []


@pytest.mark.parametrize("release_status", ["READY", "RELEASED"])
def test_release_gate_accepts_consistent_ready_or_released_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    release_status: str,
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    status = ready_status()
    status["release_status"] = release_status

    assert release_checks(
        "13.0.0", status, matrix=ready_matrix()
    ) == []


@pytest.mark.parametrize(
    "release_status", [None, "BLOCKED", "FAILED", "NOT_READY", "RELEASED WITH WARNINGS"]
)
def test_release_gate_rejects_inconsistent_release_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    release_status: object,
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    status = ready_status()
    status["release_status"] = release_status

    errors = release_checks("13.0.0", status, matrix=ready_matrix())

    assert any("release_status" in error and "READY or RELEASED" in error for error in errors)


def test_normal_check_does_not_apply_the_final_release_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("13.0.0\n", encoding="ascii")
    monkeypatch.setattr(MODULE, "ROOT", root)
    monkeypatch.setattr(
        MODULE,
        "collected_versions",
        lambda expected: (
            {"placeholder": {"version": expected}},
            {"release_status": "BLOCKED"},
        ),
    )
    monkeypatch.setattr(
        MODULE,
        "_release_checks",
        lambda *args, **kwargs: pytest.fail("normal check invoked final release gate"),
    )
    monkeypatch.setattr(sys, "argv", ["check-release-metadata.py"])

    assert MODULE.main() == 0


def test_release_main_passes_test_matrix_to_the_final_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("13.0.0\n", encoding="ascii")
    matrix = ready_matrix()
    benchmark = ready_benchmark()
    captured: dict[str, object] = {}
    monkeypatch.setattr(MODULE, "ROOT", root)
    monkeypatch.setattr(
        MODULE,
        "collected_versions",
        lambda expected: (
            {"placeholder": {"version": expected}},
            {"release_status": "READY"},
        ),
    )
    monkeypatch.setattr(
        MODULE,
        "_json",
        lambda path: benchmark if path.endswith("MODEL_BENCHMARK.json") else matrix,
    )

    def release_checks(
        expected: str,
        status: dict[str, object],
        *,
        matrix: dict[str, object] | None = None,
        benchmark: dict[str, object] | None = None,
        require_tag: bool = False,
    ) -> list[str]:
        captured.update(
            expected=expected,
            status=status,
            matrix=matrix,
            benchmark=benchmark,
            require_tag=require_tag,
        )
        return ["synthetic gate failure"]

    monkeypatch.setattr(MODULE, "_release_checks", release_checks)
    monkeypatch.setattr(sys, "argv", ["check-release-metadata.py", "--release"])

    assert MODULE.main() == 1
    assert captured["matrix"] is matrix
    assert captured["benchmark"] is benchmark
    assert captured["require_tag"] is True


def test_release_main_accepts_a_complete_schema_v5_evidence_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    write_machine_metadata(root)
    release = root / "docs" / "13.0.0"
    release.mkdir(parents=True)
    (root / "README.md").write_text(
        "当前版本：`13.0.0` test fixture\n", encoding="utf-8"
    )
    (root / "docs" / "CURRENT_ARCHITECTURE.md").write_text(
        "司忆 `13.0.0` test fixture\n", encoding="utf-8"
    )
    (release / "RELEASE_NOTES.md").write_text(
        "# 司忆 v13.0.0 test fixture\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "release source"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    common = {
        "target_version": "13.0.0",
        "source_version": "13.0.0",
        "source_commit": source_commit,
    }
    matrix = {**common, **ready_matrix()}
    status = {
        **common,
        "implementation_status": "COMPLETE",
        "test_status": "READY",
        "distribution_status": "READY",
        "release_status": "READY",
    }
    for filename, payload in (
        ("TEST_MATRIX.json", matrix),
        ("RELEASE_STATUS.json", status),
        ("EVIDENCE_MANIFEST.json", common),
        ("MODEL_BENCHMARK.json", ready_benchmark()),
    ):
        (release / filename).write_text(json.dumps(payload), encoding="utf-8")
    (release / "IMPLEMENTATION_FEEDBACK.md").write_text(
        "# Implementation feedback\n", encoding="utf-8"
    )
    (release / "MODEL_BENCHMARK_REPORT.md").write_text(
        "# Model benchmark\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "release evidence"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "tag", "v13.0.0"], cwd=root, check=True, capture_output=True
    )
    monkeypatch.setattr(MODULE, "ROOT", root)
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.setattr(sys, "argv", ["check-release-metadata.py", "--release"])

    assert MODULE.main() == 0
