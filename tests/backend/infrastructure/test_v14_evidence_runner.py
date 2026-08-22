from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
RUNNER_SCRIPT = ROOT / "scripts" / "v14-evidence-runner.py"
EVIDENCE_SCRIPT = ROOT / "scripts" / "v14-evidence.py"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


RUNNER = load_module("v14_evidence_runner", RUNNER_SCRIPT)
EVIDENCE = load_module("v14_evidence", EVIDENCE_SCRIPT)


def repository(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("13.0.0\n", encoding="ascii")
    (root / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    plan = root / "v14-plan.md"
    plan.write_text(
        "# 司忆 v14.0.0 实施计划\n\n"
        "FasterWhisperProvider\n/api/stt/transcribe\n/api/voice/events\nA01-A28\n",
        encoding="utf-8",
    )
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "evidence-test@example.invalid"],
        ["git", "config", "user.name", "Evidence Test"],
        ["git", "add", "VERSION", "v14-plan.md", ".gitignore"],
        ["git", "commit", "-m", "test evidence source"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root, plan


def write_attested_stt_writer(root: Path, *, corrupt_source: bool = False) -> Path:
    """Create a controlled, synthetic raw-report writer for runner contract tests."""

    script = root / "scripts" / "v14-stt-live-evidence.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "source = json.loads(os.environ['SIYI_V14_EVIDENCE_SOURCE_IDENTITY'])\n"
        + ("source['source_tree_fingerprint'] = 'tampered'\n" if corrupt_source else "")
        + "output = Path(sys.argv[1])\n"
        "output.parent.mkdir(parents=True, exist_ok=True)\n"
        "with output.open('x', encoding='utf-8', newline='\\n') as handle:\n"
        "    json.dump({'schema_version': 1, 'report_type': 'v14_stt_live_evidence', "
        "'producer': 'scripts/v14-stt-live-evidence.py', 'target_version': '14.0.0', "
        "'status': 'PASS', 'actual_run': True, 'source': source, "
        "'checks': {'real_cpu_int8_model_load': {'passed': True}, "
        "'fixed_accuracy_corpus_category_coverage': {'passed': True}, "
        "'synthetic_chinese_and_mixed_transcripts_nonempty': {'passed': True}}, "
        "'results': {'accuracy_review': {'manual_review_required': True, "
        "'decision': 'NOT_AUTOMATED', 'categories_exercised': ["
        "'ordinary_chinese', 'numbers_dates_percent', 'english_abbreviation', "
        "'mixed_language', 'siyi', 'natsume', 'file_name', 'technical_terms', "
        "'pause', 'background_noise'], 'samples': {'fixture': {"
        "'reference_text': 'fixture reference', 'observed_text': 'fixture transcript', "
        "'accuracy_review': {'character_differences': []}, "
        "'expected_entities': [], 'observed_entities': []}}}}}, handle, sort_keys=True)\n"
        "    handle.write('\\n')\n",
        encoding="utf-8",
    )
    return script


def test_runner_writes_real_success_envelope_usable_by_v14_ledger(tmp_path: Path) -> None:
    root, plan = repository(tmp_path)
    test_path = root / "tests" / "backend" / "test_runner_contract.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_runner_contract():\n    assert True\n", encoding="utf-8")

    result = RUNNER.main(
        [
            "--repository-root",
            str(root),
            "--case",
            "A01",
            "--case",
            "A25",
            "--output",
            "executions/a01-a25.json",
            "--",
            sys.executable,
            "-m",
            "pytest",
            "tests/backend/test_runner_contract.py",
            "-q",
        ]
    )

    assert result == 0
    report_path = root / "build" / "v1400-evidence" / "executions" / "a01-a25.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == RUNNER.REPORT_SCHEMA_VERSION
    assert report["target_version"] == "14.0.0"
    assert report["source_version"] == "13.0.0"
    assert isinstance(report["source_tree_fingerprint"], str)
    assert report["status"] == "PASS"
    assert report["actual_run"] is True
    assert report["case_ids"] == ["A01", "A25"]
    assert report["execution"]["exit_code"] == 0
    assert report["execution"]["status"] == "PASS"
    assert report["execution"]["actual_run"] is True
    assert report["execution"]["timed_out"] is False
    assert report["execution"]["duration_ms"] >= 0
    assert report["execution"]["stdout"]["bytes"] > 0
    assert report["execution"]["stderr"]["bytes"] >= 0
    assert len(report["execution"]["stdout"]["sha256"]) == 64
    assert len(report["execution"]["stderr"]["sha256"]) == 64
    assert report["command_contract"] == {
        "kind": "pytest",
        "paths": ["tests/backend/test_runner_contract.py"],
    }

    ledger = EVIDENCE.default_ledger()
    for case_id in ("A01", "A25"):
        case = next(item for item in ledger["cases"] if item["id"] == case_id)
        case.update(
            {
                "status": "PASS",
                "reason": "真实受控命令已执行。",
                "evidence": [
                    {
                        "path": "build/v1400-evidence/executions/a01-a25.json",
                        "kind": "automated",
                        "actual_run": True,
                        "outcome": "PASS",
                        "command": report["command"],
                        "recorded_at": report["recorded_at"],
                    }
                ],
            }
        )
    documents = EVIDENCE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=root / "build" / "v1400-evidence",
        expected_plan_sha256=EVIDENCE.sha256(plan),
    )
    assert documents["matrix"]["source_version"] == "13.0.0"
    assert documents["matrix"]["summary"]["pass"] == 2


def test_runner_writes_failure_envelope_and_never_promotes_it_to_pass(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)

    result = RUNNER.main(
        [
            "--repository-root",
            str(root),
            "--case",
            "A21",
            "--output",
            "executions/a21-failure.json",
            "--",
            sys.executable,
            "-c",
            "import sys; print('failure', file=sys.stderr); raise SystemExit(7)",
        ]
    )

    assert result == 1
    report = json.loads(
        (root / "build" / "v1400-evidence" / "executions" / "a21-failure.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["actual_run"] is True
    assert report["status"] == "FAIL"
    assert report["execution"]["status"] == "FAIL"
    assert report["execution"]["exit_code"] == 7
    assert report["execution"]["stderr"]["bytes"] > 0


def test_runner_binds_fresh_raw_live_report_and_ledger_rehashes_it(tmp_path: Path) -> None:
    root, plan = repository(tmp_path)
    write_attested_stt_writer(root)
    raw_relative = "raw/a08-live.json"
    raw_path = root / "build" / "v1400-evidence" / raw_relative

    result = RUNNER.main(
        [
            "--repository-root",
            str(root),
            "--case",
            "A08",
            "--output",
            "executions/a08.json",
            "--attest-output",
            raw_relative,
            "--",
            sys.executable,
            "scripts/v14-stt-live-evidence.py",
            "build/v1400-evidence/raw/a08-live.json",
        ]
    )

    assert result == 0
    envelope_path = root / "build" / "v1400-evidence" / "executions" / "a08.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["schema_version"] == 4
    assert envelope["command_contract"] == {
        "kind": "repository_script",
        "paths": ["scripts/v14-stt-live-evidence.py"],
    }
    assert len(envelope["attested_outputs"]) == 1
    attachment = envelope["attested_outputs"][0]
    assert attachment["path"] == "build/v1400-evidence/raw/a08-live.json"
    assert attachment["sha256"] == RUNNER.sha256_bytes(raw_path.read_bytes())
    assert attachment["bytes"] == raw_path.stat().st_size
    assert attachment["status"] == "PASS"
    assert attachment["actual_run"] is True
    assert attachment["target_version"] == "14.0.0"
    assert attachment["source_identity_mode"] == "raw_report"
    for field in ("source_version", "source_commit", "source_tree_fingerprint", "workspace_clean"):
        assert attachment[field] == envelope[field]

    manual_procedure = (
        "Manually compare every A08 reference and observed transcript and record "
        "bounded release claims"
    )
    manual_recorded_at = "2026-08-03T00:05:00Z"
    manual_path = root / "build" / "v1400-evidence" / "reviews" / "a08-manual.json"
    manual_path.parent.mkdir(parents=True)
    manual_path.write_text(
        json.dumps(
            {
                "schema_version": EVIDENCE.A08_MANUAL_REVIEW_SCHEMA_VERSION,
                "report_type": EVIDENCE.A08_MANUAL_REVIEW_REPORT_TYPE,
                "producer": EVIDENCE.A08_MANUAL_REVIEW_PRODUCER,
                "target_version": "14.0.0",
                "case_id": "A08",
                "actual_review": True,
                "review_mode": "manual_semantic_inspection",
                "automated_decision": False,
                "decision": "PASS_WITH_WARNING",
                "procedure": manual_procedure,
                "recorded_at": manual_recorded_at,
                "reviewer": {
                    "identity": "release-reviewer",
                    "role": "release acceptance reviewer",
                },
                "source": {field: envelope[field] for field in (
                    "source_version",
                    "source_commit",
                    "source_tree_fingerprint",
                    "workspace_clean",
                )},
                "source_raw": {
                    "path": attachment["path"],
                    "sha256": attachment["sha256"],
                    "bytes": attachment["bytes"],
                },
                "review": {
                    "categories_reviewed": [
                        "ordinary_chinese",
                        "numbers_dates_percent",
                        "english_abbreviation",
                        "mixed_language",
                        "siyi",
                        "natsume",
                        "file_name",
                        "technical_terms",
                        "pause",
                        "background_noise",
                    ],
                    "sample_reviews": {
                        "fixture": {
                            "assessment": "WARNING",
                            "reference_text_reviewed": True,
                            "observed_text_reviewed": True,
                            "notes": "The controlled fixture differs and is disclosed as a warning.",
                        }
                    },
                },
                "acceptance": {
                    "local_chinese_actual_inference": "PASS",
                    "microphone_capture": "NOT_RUN",
                    "input_audio_scope": "SYNTHETIC_NON_MICROPHONE",
                },
                "warning_boundaries": {
                    "terminology": {
                        "assessment": "WARNING",
                        "observations": ["The fixture does not prove terminology accuracy."],
                        "release_claim": "No terminology-accuracy PASS is claimed.",
                    },
                    "mixed_language": {
                        "assessment": "WARNING",
                        "observations": ["The fixture does not prove mixed-language accuracy."],
                        "release_claim": "No mixed-language-accuracy PASS is claimed.",
                    },
                    "hallucination_repetition": {
                        "assessment": "PASS",
                        "observations": ["No unprompted phrase exists in this controlled fixture."],
                        "release_claim": "The claim is limited to this reviewed fixture.",
                    },
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    ledger = EVIDENCE.default_ledger()
    ledger["cases"][7] = {
        "id": "A08",
        "status": "PASS",
        "reason": "controlled synthetic writer verifies evidence plumbing only",
        "evidence": [
            {
                "path": "build/v1400-evidence/executions/a08.json",
                "kind": "automated",
                "actual_run": True,
                "outcome": "PASS",
                "command": envelope["command"],
                "recorded_at": envelope["recorded_at"],
                "attested_outputs": [attachment["path"]],
            },
            {
                "path": "build/v1400-evidence/reviews/a08-manual.json",
                "kind": "manual",
                "actual_run": True,
                "outcome": "PASS",
                "command": manual_procedure,
                "recorded_at": manual_recorded_at,
            },
        ],
    }
    documents = EVIDENCE.build_documents(
        repository_root=root,
        target_version="14.0.0",
        plan_path=plan,
        ledger=ledger,
        output_root=root / "build" / "v1400-evidence",
        expected_plan_sha256=EVIDENCE.sha256(plan),
    )
    evidence = documents["matrix"]["cases"][7]["evidence"][0]
    assert evidence["attested_outputs"] == [attachment]


def test_runner_refuses_preexisting_attested_output_before_command_starts(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)
    write_attested_stt_writer(root)
    raw_path = root / "build" / "v1400-evidence" / "raw" / "preexisting.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text('{"status":"PASS"}\n', encoding="utf-8")

    with pytest.raises(RUNNER.RunnerValidationError, match="must not exist"):
        RUNNER.main(
            [
                "--repository-root",
                str(root),
                "--case",
                "A08",
                "--output",
                "executions/a08-preexisting.json",
                "--attest-output",
                "raw/preexisting.json",
                "--",
                sys.executable,
                "scripts/v14-stt-live-evidence.py",
                "build/v1400-evidence/raw/preexisting.json",
            ]
        )


def test_runner_marks_envelope_fail_when_raw_source_is_not_the_same_run(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)
    write_attested_stt_writer(root, corrupt_source=True)

    result = RUNNER.main(
        [
            "--repository-root",
            str(root),
            "--case",
            "A08",
            "--output",
            "executions/a08-source-mismatch.json",
            "--attest-output",
            "raw/a08-source-mismatch.json",
            "--",
            sys.executable,
            "scripts/v14-stt-live-evidence.py",
            "build/v1400-evidence/raw/a08-source-mismatch.json",
        ]
    )
    assert result == 1
    envelope = json.loads(
        (root / "build" / "v1400-evidence" / "executions" / "a08-source-mismatch.json").read_text(
            encoding="utf-8"
        )
    )
    assert envelope["status"] == "FAIL"
    assert "source identity" in envelope["execution"]["error"]
    assert envelope["attested_outputs"] == []


def test_runner_reports_a_missing_attested_raw_without_claiming_path_escape(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)

    result = RUNNER.main(
        [
            "--repository-root",
            str(root),
            "--case",
            "A08",
            "--output",
            "executions/a08-missing-raw.json",
            "--attest-output",
            "raw/a08-missing-raw.json",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
    )

    assert result == 1
    envelope = json.loads(
        (root / "build" / "v1400-evidence" / "executions" / "a08-missing-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert envelope["execution"]["error"] == "attested output was not created by the command"
    assert "escaped" not in envelope["execution"]["error"]


def test_runner_rejects_a_fresh_path_hard_linked_to_an_old_raw_report(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)
    old_raw = root / "build" / "v1400-evidence" / "raw" / "old.json"
    old_raw.parent.mkdir(parents=True)
    old_raw.write_text('{"status":"PASS"}\n', encoding="utf-8")
    script = root / "scripts" / "v14-stt-live-evidence.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import os\nfrom pathlib import Path\nimport sys\n"
        "output = Path(sys.argv[1])\noutput.parent.mkdir(parents=True, exist_ok=True)\n"
        "os.link('build/v1400-evidence/raw/old.json', output)\n",
        encoding="utf-8",
    )

    result = RUNNER.main(
        [
            "--repository-root",
            str(root),
            "--case",
            "A08",
            "--output",
            "executions/a08-hard-link.json",
            "--attest-output",
            "raw/a08-hard-link.json",
            "--",
            sys.executable,
            "scripts/v14-stt-live-evidence.py",
            "build/v1400-evidence/raw/a08-hard-link.json",
        ]
    )

    assert result == 1
    envelope = json.loads(
        (root / "build" / "v1400-evidence" / "executions" / "a08-hard-link.json").read_text(
            encoding="utf-8"
        )
    )
    assert "non-linked" in envelope["execution"]["error"]


def test_runner_refuses_to_overwrite_an_existing_execution_envelope(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)
    existing = root / "build" / "v1400-evidence" / "executions" / "a01.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"status":"old"}\n', encoding="utf-8")

    with pytest.raises(RUNNER.RunnerValidationError, match="must not overwrite"):
        RUNNER.main(
            [
                "--repository-root",
                str(root),
                "--case",
                "A01",
                "--output",
                "executions/a01.json",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(0)",
            ]
        )


def test_runner_records_but_does_not_promote_an_arbitrary_exit_zero_command(tmp_path: Path) -> None:
    root, plan = repository(tmp_path)
    result = RUNNER.main(
        [
            "--repository-root",
            str(root),
            "--case",
            "A01",
            "--output",
            "executions/a01-uncontrolled.json",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ]
    )
    assert result == 0
    report_path = root / "build" / "v1400-evidence" / "executions" / "a01-uncontrolled.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["command_contract"] == {"kind": "uncontrolled", "paths": []}

    ledger = EVIDENCE.default_ledger()
    ledger["cases"][0] = {
        "id": "A01",
        "status": "PASS",
        "reason": "must not promote an ad-hoc command",
        "evidence": [
            {
                "path": "build/v1400-evidence/executions/a01-uncontrolled.json",
                "kind": "automated",
                "actual_run": True,
                "outcome": "PASS",
                "command": report["command"],
                "recorded_at": report["recorded_at"],
            }
        ],
    }
    with pytest.raises(EVIDENCE.EvidenceValidationError, match="command_contract.kind"):
        EVIDENCE.build_documents(
            repository_root=root,
            target_version="14.0.0",
            plan_path=plan,
            ledger=ledger,
            output_root=root / "build" / "v1400-evidence",
            expected_plan_sha256=EVIDENCE.sha256(plan),
        )


def test_runner_rejects_output_path_that_escapes_evidence_root(tmp_path: Path) -> None:
    root, _ = repository(tmp_path)

    try:
        RUNNER.main(
            [
                "--repository-root",
                str(root),
                "--case",
                "A01",
                "--output",
                "../outside.json",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(0)",
            ]
        )
    except RUNNER.RunnerValidationError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("runner accepted an output path outside build/v1400-evidence")
