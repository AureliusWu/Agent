from __future__ import annotations

import json
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "build" / "v8-evidence" / "evopolicygym"
RESULTS = ROOT / "docs" / "8.0.0" / "TEST_RESULTS.json"
LOCK = ROOT / "evals" / "evopolicygym.lock.json"
AGGREGATE = ROOT / "build" / "v8-evidence" / "evopolicygym-aggregate.json"
HIDDEN = ROOT / "build" / "v8-evidence" / "evopolicygym-hidden-boundary.json"


def summaries() -> list[tuple[Path, dict]]:
    rows = []
    for path in EVIDENCE.glob("*-summary.json"):
        rows.append((path, json.loads(path.read_text(encoding="utf-8-sig"))))
    return sorted(rows, key=lambda item: item[0].name)


def evidence(artifact: str, recorded_at: str, build_id: str, command: str) -> dict:
    return {
        "command": command,
        "artifact": artifact,
        "recorded_at": recorded_at,
        "build_id": build_id,
    }


def main() -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    passed = [
        (path, payload)
        for path, payload in summaries()
        if payload.get("status") == "passed"
        and payload.get("evopolicygym_commit") == lock["commit"]
    ]
    formal = next(
        (
            (path, payload)
            for path, payload in reversed(passed)
            if len(payload.get("runs") or []) == 6
        ),
        None,
    )
    diagnostic = next(
        (
            (path, payload)
            for path, payload in reversed(passed)
            if len(payload.get("runs") or []) == 2
        ),
        None,
    )
    if formal is None or diagnostic is None:
        raise RuntimeError("passed 1x1 diagnostic and 3x8 formal Evo runs are required")
    formal_path, formal_payload = formal
    diagnostic_path, diagnostic_payload = diagnostic
    build_id = str(formal_payload["build_id"])
    formal_run_id = formal_path.name.removesuffix("-summary.json")
    session_root = (
        Path(tempfile.gettempdir()) / "Siyi-Evals" / f"evopolicygym-{formal_run_id}"
    )
    hidden = json.loads(HIDDEN.read_text(encoding="utf-8-sig"))
    if hidden.get("status") != "passed":
        raise RuntimeError("hidden-boundary probe is not passed")

    runs: list[dict] = []
    by_environment: dict[str, list[dict]] = {"toy": [], "cartpole": []}
    for item in formal_payload["runs"]:
        run_path = ROOT / item["run_json"]
        run = json.loads(run_path.read_text(encoding="utf-8"))
        if (
            item.get("exit_code") != 0
            or item.get("status") != "completed"
            or run.get("outcome", {}).get("status") != "completed"
            or run.get("experiment_dimensions", {}).get("episode_budget") != 8
        ):
            raise RuntimeError(f"incomplete formal Evo run: {item}")
        name = f"{item['environment']}-repeat-{item['repeat']}"
        source = session_root / name
        harness = source / "logs" / "harness.log"
        feedback = source / "workspace" / "feedback" / "submit_000" / "summary.json"
        if not harness.is_file() or not feedback.is_file():
            raise RuntimeError(f"formal Evo audit files are missing: {name}")
        events = [
            json.loads(line)
            for line in harness.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        submit_finishes = [
            event for event in events if event.get("event") == "submit.finish"
        ]
        closes = [event for event in events if event.get("event") == "run.close"]
        if (
            len(submit_finishes) != 1
            or submit_finishes[0].get("cost") != 8
            or submit_finishes[0].get("remaining_budget") != 0
            or not closes
            or closes[-1].get("budget_used") != 8
        ):
            raise RuntimeError(f"budget conservation failed: {name}")
        workspace_paths = [
            path.relative_to(source / "workspace").as_posix()
            for path in (source / "workspace").rglob("*")
            if path.is_file()
        ]
        forbidden = [
            path
            for path in workspace_paths
            if any(part in path.lower() for part in ("validation", "heldout", "hidden"))
        ]
        if forbidden:
            raise RuntimeError(f"hidden files leaked into workspace: {name}: {forbidden}")
        row = {
            "environment": item["environment"],
            "repeat": item["repeat"],
            "episode_budget": 8,
            "submit_count": 1,
            "budget_used": 8,
            "remaining_budget": 0,
            "invalid_submit_count": 0,
            "train_feedback": json.loads(feedback.read_text(encoding="utf-8")),
            "validation_scores": run["outcome"].get("val_scores"),
            "heldout_mean_return": run["outcome"].get("heldout_mean_return"),
            "heldout_std_return": run["outcome"].get("heldout_std_return"),
            "final_score": run["outcome"].get("final_score"),
            "wall_time_seconds": run["timing"].get("wall_time_seconds"),
            "workspace_hidden_path_count": 0,
            "run_json": item["run_json"],
        }
        runs.append(row)
        by_environment[item["environment"]].append(row)

    aggregates = {}
    for environment, rows in by_environment.items():
        scores = [float(row["final_score"]) for row in rows]
        heldout = [float(row["heldout_mean_return"]) for row in rows]
        walls = [float(row["wall_time_seconds"]) for row in rows]
        aggregates[environment] = {
            "runs": len(rows),
            "final_score_median": statistics.median(scores),
            "final_score_worst": min(scores),
            "final_score_population_variance": statistics.pvariance(scores),
            "heldout_mean_median": statistics.median(heldout),
            "heldout_mean_worst": min(heldout),
            "wall_time_median_seconds": statistics.median(walls),
            "first_improvement_budget": 8,
            "best_score_budget": 8,
            "invalid_submit_rate": 0.0,
        }
    recorded_at = datetime.now(timezone.utc).isoformat()
    aggregate = {
        "schema_version": 1,
        "status": "passed",
        "recorded_at": recorded_at,
        "build_id": build_id,
        "evopolicygym_commit": lock["commit"],
        "formal_summary": f"build/v8-evidence/evopolicygym/{formal_path.name}",
        "diagnostic_summary": f"build/v8-evidence/evopolicygym/{diagnostic_path.name}",
        "hidden_boundary": "build/v8-evidence/evopolicygym-hidden-boundary.json",
        "runs": runs,
        "aggregates": aggregates,
        "limitations": [
            "Each formal run used one accepted submit that consumed all 8 episodes.",
            "No feedback-driven second policy revision occurred within a run.",
            "No same-base-model comparison harness was provided.",
        ],
    }
    AGGREGATE.write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    formal_artifact = f"build/v8-evidence/evopolicygym/{formal_path.name}"
    diagnostic_artifact = f"build/v8-evidence/evopolicygym/{diagnostic_path.name}"
    formal_evidence = evidence(
        formal_artifact,
        str(formal_payload["finished_at"]),
        build_id,
        "powershell -File scripts/run-evopolicygym.ps1 -Repeats 3 -Budget 8",
    )
    aggregate_evidence = evidence(
        "build/v8-evidence/evopolicygym-aggregate.json",
        recorded_at,
        build_id,
        "python scripts/update-v8-evo-results.py",
    )
    diagnostic_evidence = evidence(
        diagnostic_artifact,
        str(diagnostic_payload["finished_at"]),
        str(diagnostic_payload["build_id"]),
        "powershell -File scripts/run-evopolicygym.ps1 -Repeats 1 -Budget 1",
    )
    hidden_evidence = evidence(
        "build/v8-evidence/evopolicygym-hidden-boundary.json",
        str(hidden["recorded_at"]),
        build_id,
        "probe official loopback /validation /heldout /hidden and audit workspace paths",
    )
    for case_id in ("EVO-001", "EVO-002", "EVO-003", "EVO-014"):
        results[case_id] = {
            "status": "PASS",
            "evidence": [formal_evidence, aggregate_evidence],
        }
    results["EVO-004"] = {"status": "PASS", "evidence": [hidden_evidence]}
    results["EVO-005"] = {
        "status": "PASS",
        "evidence": [diagnostic_evidence, formal_evidence, aggregate_evidence],
    }
    results["EVO-006"] = {
        "status": "FAIL",
        "reason": "Toy smoke-8 completed, but the single submit consumed all 8 episodes; it did not demonstrate staged diagnosis and budget allocation.",
        "evidence": [formal_evidence, aggregate_evidence],
    }
    results["EVO-007"] = {
        "status": "BLOCKED",
        "reason": "CartPole completed with held-out scores, but no explicit baseline run was supplied, so relative improvement cannot be proven.",
        "evidence": [formal_evidence, aggregate_evidence],
    }
    results["EVO-008"] = {
        "status": "FAIL",
        "reason": "Every formal run used one submit; there was no feedback-to-second-code-revision causal trace.",
        "evidence": [aggregate_evidence],
    }
    results["EVO-009"] = {
        "status": "NOT_RUN",
        "reason": "No formal run produced multiple candidates or a score regression within one session.",
    }
    for case_id in ("EVO-010", "EVO-011", "EVO-012"):
        results[case_id] = {"status": "PASS", "evidence": [aggregate_evidence]}
    results["EVO-013"] = {
        "status": "BLOCKED",
        "reason": "No same-base-model comparison harness was supplied.",
    }
    RESULTS.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(AGGREGATE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
