from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SCENARIO_IDS = {f"RS-{index:03d}" for index in range(1, 19)}
STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "NOT_APPLICABLE"}
REQUIRED_OUTPUTS = {
    "scenario.json",
    "events.jsonl",
    "tool-receipts.jsonl",
    "token-ledger.json",
    "screenshots",
    "artifacts",
    "final-report.md",
}
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_catalog(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"catalog cannot be read: {exc}"]
    if payload.get("schema_version") != 1:
        errors.append("catalog schema_version must be 1")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        return errors + ["catalog scenarios must be a list"]
    identifiers = [item.get("id") for item in scenarios if isinstance(item, dict)]
    if len(identifiers) != len(set(identifiers)):
        errors.append("catalog scenario ids must be unique")
    missing = sorted(SCENARIO_IDS - set(identifiers))
    extra = sorted(set(identifiers) - SCENARIO_IDS)
    if missing:
        errors.append(f"catalog is missing scenarios: {missing}")
    if extra:
        errors.append(f"catalog contains unknown scenarios: {extra}")
    for item in scenarios:
        if not isinstance(item, dict):
            errors.append("catalog scenario must be an object")
            continue
        if not str(item.get("title") or "").strip():
            errors.append(f"{item.get('id')}: title is required")
        requirements = item.get("requirement_ids")
        if not isinstance(requirements, list) or not requirements:
            errors.append(f"{item.get('id')}: requirement_ids must be non-empty")
    return errors


def _validate_timestamp(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        errors.append(f"{label} must be an ISO-8601 timestamp with timezone")


def validate_scenario_directory(directory: Path) -> list[str]:
    errors: list[str] = []
    missing = sorted(name for name in REQUIRED_OUTPUTS if not (directory / name).exists())
    if missing:
        errors.append(f"{directory.name}: missing outputs: {missing}")
        return errors
    try:
        payload = _load_json(directory / "scenario.json")
        _load_json(directory / "token-ledger.json")
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{directory.name}: invalid JSON: {exc}"]
    scenario_id = payload.get("scenario_id")
    if scenario_id != directory.name or scenario_id not in SCENARIO_IDS:
        errors.append(f"{directory.name}: scenario_id does not match directory")
    status = payload.get("status")
    if status not in STATUSES:
        errors.append(f"{directory.name}: invalid status {status!r}")
        return errors
    _validate_timestamp(payload.get("started_at"), f"{directory.name}.started_at", errors)
    _validate_timestamp(payload.get("finished_at"), f"{directory.name}.finished_at", errors)
    if status == "PASS":
        build = payload.get("build")
        if not isinstance(build, dict) or not all(str(build.get(key) or "").strip() for key in ("product_version", "build_id", "source_fingerprint")):
            errors.append(f"{directory.name}: PASS requires complete build identity")
        commands = payload.get("commands")
        if not isinstance(commands, list) or not commands:
            errors.append(f"{directory.name}: PASS requires at least one executed command")
        else:
            for index, command in enumerate(commands):
                if not isinstance(command, dict) or not str(command.get("command") or "").strip():
                    errors.append(f"{directory.name}: command {index} is incomplete")
                    continue
                _validate_timestamp(command.get("started_at"), f"{directory.name}.commands[{index}].started_at", errors)
                _validate_timestamp(command.get("finished_at"), f"{directory.name}.commands[{index}].finished_at", errors)
                if command.get("exit_code") != 0:
                    errors.append(f"{directory.name}: PASS command {index} did not exit 0")
        evidence_files = payload.get("evidence_files")
        if not isinstance(evidence_files, list) or not evidence_files:
            errors.append(f"{directory.name}: PASS requires evidence_files")
        else:
            root = directory.resolve()
            for relative in evidence_files:
                target = (directory / str(relative)).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    errors.append(f"{directory.name}: evidence path escapes scenario directory: {relative}")
                    continue
                if not target.is_file():
                    errors.append(f"{directory.name}: evidence file does not exist: {relative}")
    if status == "BLOCKED" and not str(payload.get("blocker") or "").strip():
        errors.append(f"{directory.name}: BLOCKED requires blocker")
    return errors


def validate_run_root(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_dir():
        return [f"run root does not exist: {path}"]
    present = {item.name for item in path.iterdir() if item.is_dir() and item.name.startswith("RS-")}
    missing = sorted(SCENARIO_IDS - present)
    if missing:
        errors.append(f"run root is missing scenarios: {missing}")
    for scenario_id in sorted(present & SCENARIO_IDS):
        errors.extend(validate_scenario_directory(path / scenario_id))
    return errors
