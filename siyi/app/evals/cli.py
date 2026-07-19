from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from ..config import settings
from .comparison import compare_reports, evaluate_gate, load_policy, load_report, write_comparison
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

    compare = subcommands.add_parser("compare", help="compare two JSON reports")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", required=True)

    gate = subcommands.add_parser("gate", help="check whether a report may be marked stable")
    gate.add_argument("--report", required=True)
    gate.add_argument("--baseline")
    gate.add_argument("--policy")

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
            )
        )
        print(json.dumps({"run_id": report.run_id, "status": report.status, "metrics": report.metrics, "reports": report.report_paths}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-companion":
        contracts = load_companion_contracts(args.contracts)
        print(json.dumps({"status": "ok", "contract_count": len(contracts), "contract_ids": [item.id for item in contracts]}, ensure_ascii=False))
        return 0
    if args.command == "compare":
        comparison = compare_reports(load_report(args.baseline), load_report(args.candidate))
        paths = write_comparison(comparison, args.output)
        print(json.dumps({"has_regressions": comparison["has_regressions"], "regressions": comparison["regressions"], "reports": paths}, ensure_ascii=False, indent=2))
        return 1 if comparison["has_regressions"] else 0
    if args.command == "gate":
        report = load_report(args.report)
        comparison = compare_reports(load_report(args.baseline), report) if args.baseline else None
        result = evaluate_gate(report, load_policy(args.policy), comparison)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
