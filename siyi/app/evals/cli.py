from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from ..config import settings
from .. import __version__
from .comparison import compare_reports, evaluate_gate, load_policy, load_report, write_comparison
from .contracts import task_contract
from .loader import load_companion_contracts, load_tasks
from .runner import run_evaluation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-eval", description="Agent repeatable evaluation and release gate")
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate", help="validate the fixed task contract")
    validate.add_argument("--tasks")
    validate.add_argument("--suite", default="core")
    validate.add_argument("--task", action="append", dest="task_ids")

    run = subcommands.add_parser("run", help="run the evaluation suite")
    run.add_argument("--label", required=True)
    run.add_argument("--mode", choices=["scripted_runtime", "live_model", "adversarial"], default="scripted_runtime")
    run.add_argument("--suite", default="core")
    run.add_argument("--tasks")
    run.add_argument("--output", default="data/evals")
    run.add_argument("--task", action="append", dest="task_ids")
    run.add_argument("--capture-source", action="store_true", help="bind repository source before/after the run for RC evidence")
    run.add_argument(
        "--require-passed",
        action="store_true",
        help="return a non-zero exit code unless every selected task meets its expectation",
    )

    compare = subcommands.add_parser("compare", help="compare two JSON reports")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", required=True)

    gate = subcommands.add_parser("gate", help="check an evaluation report; not desktop/install/release acceptance")
    gate.add_argument("--report", required=True)
    gate.add_argument("--baseline")
    gate.add_argument("--policy")
    gate.add_argument("--tasks")
    gate.add_argument("--suite", default="core")
    gate.add_argument("--mode", choices=["scripted_runtime", "live_model", "adversarial"], required=True)
    gate.add_argument("--version", default=__version__)

    companion = subcommands.add_parser("validate-companion", help="validate companion capability test interfaces")
    companion.add_argument("--contracts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        tasks = load_tasks(args.tasks, suite=args.suite, task_ids=args.task_ids)
        print(json.dumps({"status": "ok", "task_count": len(tasks), "task_ids": [task.id for task in tasks]}, ensure_ascii=False))
        return 0
    if args.command == "run":
        api_key = os.environ.get("AGENT_DEEPSEEK_API_KEY") or settings.deepseek_api_key or None
        report = asyncio.run(
            run_evaluation(
                label=args.label,
                mode=args.mode,
                suite=args.suite,
                task_ids=args.task_ids,
                tasks_path=args.tasks,
                output_root=args.output,
                api_key=api_key,
                capture_source=args.capture_source,
            )
        )
        print(json.dumps({"run_id": report.run_id, "status": report.status, "metrics": report.metrics, "reports": report.report_paths}, ensure_ascii=False, indent=2))
        if args.require_passed:
            metrics = report.metrics
            passed = (
                report.status == "completed"
                and int(metrics.get("task_success_count") or 0) == int(metrics.get("task_count") or 0)
                and int(metrics.get("false_success_count") or 0) == 0
                and int(metrics.get("permission_violation_count") or 0) == 0
                and int(metrics.get("sandbox_violation_count") or 0) == 0
                and int(metrics.get("unrelated_file_modification_count") or 0) == 0
            )
            return 0 if passed else 2
        return 0
    if args.command == "validate-companion":
        contracts = load_companion_contracts(args.contracts)
        print(json.dumps({"status": "ok", "contract_count": len(contracts), "contract_ids": [item.id for item in contracts]}, ensure_ascii=False))
        return 0
    if args.command == "compare":
        comparison = compare_reports(load_report(args.baseline), load_report(args.candidate))
        paths = write_comparison(comparison, args.output)
        print(json.dumps({"comparable": comparison["comparable"], "compatibility_errors": comparison["compatibility_errors"], "has_regressions": comparison["has_regressions"], "regressions": comparison["regressions"], "reports": paths}, ensure_ascii=False, indent=2))
        return 1 if not comparison["comparable"] or comparison["has_regressions"] else 0
    if args.command == "gate":
        report = load_report(args.report)
        comparison = compare_reports(load_report(args.baseline), report) if args.baseline else None
        expected = task_contract(load_tasks(args.tasks, suite=args.suite), args.suite)
        result = evaluate_gate(report, load_policy(args.policy), comparison, expected_contract=expected, expected_mode=args.mode, expected_version=args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
