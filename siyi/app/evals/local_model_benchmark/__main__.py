from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Credential-free local model benchmark v1")
    parser.add_argument("--provider", choices=["offline", "ollama"], default="offline")
    parser.add_argument("--model", help="Explicit installed Ollama model id; never automatically downloads")
    parser.add_argument("--output", default="data/evals/local-model-benchmark")
    parser.add_argument("--label", default="local-model-benchmark")
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args(argv)

    # CLI live calls use test-owned telemetry storage. Never modify the user's
    # normal conversation database, provider configuration, or credentials.
    with tempfile.TemporaryDirectory(prefix="siyi-model-benchmark-") as temporary:
        overrides = {
            "AGENT_DATABASE_PATH": str(Path(temporary) / "benchmark.db"),
            "AGENT_LOG_PATH": str(Path(temporary) / "benchmark.log"),
        }
        previous = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        try:
            from app.database import init_db
            from app.config import settings
            from .adapters import build_benchmark_adapter
            from .runner import run_local_model_benchmark

            adapter = build_benchmark_adapter(provider_id=args.provider, model_id=args.model)
            if args.provider == "ollama":
                if settings.database_path.resolve() != Path(overrides["AGENT_DATABASE_PATH"]).resolve():
                    raise RuntimeError("Live benchmark CLI requires a fresh process with isolated settings")
                init_db()
            report = asyncio.run(run_local_model_benchmark(
                adapter=adapter, output_directory=args.output, label=args.label, case_ids=args.case_ids,
            ))
            print(json.dumps({
                "status": report.status, "actual_model_run": report.actual_model_run,
                "release_gate_eligible": report.release_gate_eligible,
                "metrics": report.metrics.model_dump(), "reports": report.report_paths,
            }, ensure_ascii=False, indent=2))
            return 0 if report.status == "completed" else 1
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
