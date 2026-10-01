"""Fresh-process, opt-in runner. Never downloads or silently changes a model."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

MAX_CONTEXT_PROFILES_BYTES = 128 * 1024
_SCOPED_PROFILE = re.compile(r"^(?:deepseek|ollama|openai_compatible|mock):[a-f0-9]{16}:[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}$")
_PROFILE_NUMBERS = {"context_window_tokens", "max_output_tokens"}
_PROFILE_FLAGS = {"supports_tools", "supports_parallel_tools", "supports_json_schema", "supports_streaming", "supports_usage_reporting"}


def _read_context_profiles(path: str | Path) -> dict[str, dict]:
    """Copy only explicit credential-free scoped profiles; never read defaults."""
    from app.providers.schema_validation import parse_output
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_CONTEXT_PROFILES_BYTES + 1)
        if len(raw) > MAX_CONTEXT_PROFILES_BYTES:
            raise ValueError("Context profiles exceed 128 KiB")
        profiles = parse_output(raw.decode("utf-8"))
        if len(profiles) > 128:
            raise ValueError("Too many context profiles")
        for key, profile in profiles.items():
            if not _SCOPED_PROFILE.fullmatch(key) or ".." in key.split(":", 2)[-1] or not isinstance(profile, dict):
                raise ValueError("Context profiles require scoped provider/endpoint/model keys")
            if set(profile) - (_PROFILE_NUMBERS | _PROFILE_FLAGS):
                raise ValueError("Context profiles contain unsupported fields")
            for name, value in profile.items():
                if name in _PROFILE_FLAGS and type(value) is not bool:
                    raise ValueError("Context profile capability flags must be boolean")
                if name in _PROFILE_NUMBERS and (type(value) is not int or not 1 <= value <= 10_000_000):
                    raise ValueError("Context profile token limits must be bounded positive integers")
        return profiles
    except (OSError, UnicodeError):
        raise ValueError("Context profiles could not be read as UTF-8 JSON") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated real Runtime file qualification (scripted is not model qualification)")
    parser.add_argument("--mode", choices=("scripted", "local_live"), default="scripted")
    parser.add_argument("--model", help="Explicit installed Ollama model; no download")
    parser.add_argument("--configuration", help="Read-only copy of an existing credential-free ProviderConfiguration JSON, for exact desktop configuration identity")
    parser.add_argument("--samples", type=int, default=3)
    context = parser.add_mutually_exclusive_group()
    context.add_argument("--context-window", type=int, help="Explicit deployed num_ctx, not theoretical model maximum")
    context.add_argument("--context-profiles", help="Read-only explicit scoped context-profile JSON copy (128 KiB maximum); never reads desktop defaults")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", default="data/evals/runtime-file-qualification")
    parser.add_argument("--capture-source", action="store_true", help="Repository-only source fingerprint before/after the run; never used by installed desktop")
    args = parser.parse_args(argv)
    if args.mode == "local_live" and (not (args.model or args.configuration) or os.getenv("SIYI_TEST_PROVIDER") != "ollama"):
        parser.error("local_live requires --model and explicit SIYI_TEST_PROVIDER=ollama")
    if args.context_window is not None and not 1 <= args.context_window <= 10_000_000:
        parser.error("--context-window must be between 1 and 10000000")
    output = Path(args.output).resolve()
    context_profiles = None
    if args.context_profiles:
        try:
            context_profiles = _read_context_profiles(args.context_profiles)
        except ValueError as exc:
            parser.error(str(exc))
    configuration_json = None
    if args.configuration:
        if args.mode != "local_live":
            parser.error("--configuration is only supported with explicit local_live")
        with Path(args.configuration).open("rb") as stream:
            configuration_json = stream.read(16385)
        if len(configuration_json) > 16384:
            parser.error("Configuration exceeds 16 KiB")
    with tempfile.TemporaryDirectory(prefix="siyi-runtime-qualification-") as temporary:
        root = Path(temporary).resolve()
        overrides = {"AGENT_DATA_ROOT": str(root), "AGENT_DATABASE_PATH": str(root / "runtime.db"),
                     "AGENT_LOG_PATH": str(root / "runtime.log"), "AGENT_PROVIDER_CONFIG_PATH": str(root / "provider.json"),
                     "AGENT_MODEL_CONTEXT_PROFILES_JSON": "{}", "SIYI_ALLOW_PAID_API": "false"}
        previous = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        try:
            from app.config import settings
            from app.database import init_db
            from app.providers.configuration import ProviderConfiguration, save_provider_configuration, validate_provider_configuration
            from app.providers.effective_capabilities import context_profile_key
            from .runtime_qualification import PROTOCOL, run_runtime_file_qualification
            if settings.database_path.resolve() != root / "runtime.db":
                raise RuntimeError("Qualification CLI requires a fresh process with isolated settings")
            (root / ".runtime-qualification-owned").write_text(PROTOCOL, encoding="utf-8")
            config = ProviderConfiguration(provider_id="mock", max_retries=0)
            if args.mode == "local_live":
                config = ProviderConfiguration(provider_id="ollama", base_url="http://127.0.0.1:11434", model=args.model, max_retries=0, max_tokens=2048)
                if configuration_json is not None:
                    from app.providers.schema_validation import parse_output
                    config = validate_provider_configuration(ProviderConfiguration(**parse_output(configuration_json.decode("utf-8"))))
                    if config.provider_id != "ollama" or (args.model and config.model != args.model):
                        parser.error("Configuration must match the selected local Ollama model")
            save_provider_configuration(config)
            init_db()
            if args.context_window is not None:
                settings.model_context_profiles_json = json.dumps({context_profile_key(config): {"context_window_tokens": args.context_window}})
            elif context_profiles is not None:
                settings.model_context_profiles_json = json.dumps(context_profiles)
            async def execute():
                return await run_runtime_file_qualification(isolation_root=root, mode=args.mode, samples=args.samples, timeout_seconds=args.timeout)
            source = None
            if args.capture_source:
                from app.evals.source_identity import repository_identity
                source = repository_identity()
            report = asyncio.run(execute())
            if args.capture_source:
                report["source"] = source
                report["source_after"] = repository_identity()
            output.mkdir(parents=True, exist_ok=True)
            (output / "RUNTIME_FILE_QUALIFICATION.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            lines = ["# Runtime file qualification", "", f"Mode: {report['mode']}; protocol: {report['protocol']}; target: {report['target_version']}", "",
                     f"Status: {report['status']}; File Agent qualified: {report['file_agent_qualified']}", "", "| Case | Sample | Origin | Result |", "| --- | --- | --- | --- |"]
            lines.extend(f"| {row['case_id']} | {row['sample']} | {row['origin']} | {row['status']} |" for row in report["case_results"])
            lines.extend(["", "## Boundaries", "", *[f"- {value}" for value in report["limitations"]]])
            (output / "RUNTIME_FILE_QUALIFICATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(json.dumps({"status": report["status"], "mode": report["mode"], "file_agent_qualified": report["file_agent_qualified"], "metrics": report["metrics"], "qualification": {key: value["qualified"] for key, value in report["qualification"].items()}}, ensure_ascii=False))
            return 0 if report["status"] == "passed" else 1
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
