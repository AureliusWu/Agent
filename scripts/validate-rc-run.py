"""Validate the selected formal RC run, never the RC evidence itself.

This offline metadata check does not download artifacts, invoke a model, install
software or authorize a release. Recheck the same run attempt after download;
the importer, installer validators and complete RC gate remain mandatory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re


WORKFLOW_PATH = ".github/workflows/rc-acceptance.yml"
MAX_METADATA_BYTES = 2 * 1024 * 1024


def positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("RC attempt/artifact timestamp is missing")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("RC attempt/artifact timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def validate_run(
    run: object, *, repository: str, commit: str, run_id: int,
    expected_receipt: object = None,
) -> dict:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[0-9a-f]{40}", commit) or not positive_integer(run_id)):
        raise ValueError("Explicit repository, exact commit and positive run ID are required")
    if not isinstance(run, dict):
        raise ValueError("RC run metadata must be an object")
    own = run.get("repository")
    head = run.get("head_repository")
    if (not isinstance(own, dict) or not isinstance(head, dict)
            or own.get("full_name") != repository or head.get("full_name") != repository
            or not positive_integer(own.get("id")) or not positive_integer(head.get("id"))
            or head["id"] != own["id"]):
        raise ValueError("RC run and head repository must identify the same explicit repository")
    if run.get("id") != run_id or not positive_integer(run.get("id")):
        raise ValueError("RC run ID differs from the explicit selection")
    if run.get("head_sha") != commit:
        raise ValueError("RC run must be bound to the exact release commit")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("RC run must be completed and successful")
    if (run.get("path") not in {WORKFLOW_PATH, WORKFLOW_PATH + "@main", WORKFLOW_PATH + "@refs/heads/main"}
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main"):
        raise ValueError("RC run must be the formal acceptance workflow dispatched from main")
    if not positive_integer(run.get("run_attempt")) or not positive_integer(run.get("workflow_id")):
        raise ValueError("RC run attempt and workflow identity must be present")
    if timestamp(run.get("run_started_at")) > timestamp(run.get("updated_at")):
        raise ValueError("RC run attempt time window is invalid")
    receipt = {
        "schema_version": 1, "report_type": "rc_run_selection_not_acceptance",
        "repository": repository, "repository_id": own["id"], "source_commit": commit,
        "run_id": run_id, "run_attempt": run["run_attempt"],
        "workflow_id": run["workflow_id"], "workflow_path": WORKFLOW_PATH,
        "event": "workflow_dispatch", "head_branch": "main",
        "run_started_at": run["run_started_at"], "run_updated_at": run["updated_at"],
    }
    if expected_receipt is not None:
        if (not isinstance(expected_receipt, dict)
                or json.dumps(expected_receipt, sort_keys=True) != json.dumps(receipt, sort_keys=True)):
            raise ValueError("Selected RC run attempt or provenance changed during artifact download")
    return receipt


def validate_selection(run: object, artifacts: object, *, artifact_name: str, **kwargs) -> dict:
    expected = kwargs.pop("expected_receipt", None)
    receipt = validate_run(run, **kwargs)
    if re.fullmatch(r"Siyi-RC-Acceptance-v16\.\d+\.\d+", artifact_name) is None:
        raise ValueError("Only the explicit versioned formal RC artifact is accepted")
    if isinstance(artifacts, dict) and "id" in artifacts:
        rows = [artifacts]  # Recheck the previously selected immutable artifact ID.
    else:
        pages = artifacts if isinstance(artifacts, list) else [artifacts]
        if not pages or any(not isinstance(p, dict) or not isinstance(p.get("artifacts"), list) for p in pages):
            raise ValueError("Complete artifact metadata is required")
        rows = [a for p in pages for a in p["artifacts"]]
        if any(type(p.get("total_count")) is not int or p["total_count"] != len(rows) for p in pages):
            raise ValueError("Artifact pagination is incomplete or changed")
        if any(not isinstance(a, dict) or not positive_integer(a.get("id")) for a in rows):
            raise ValueError("Artifact identity is missing")
        if len({a["id"] for a in rows}) != len(rows):
            raise ValueError("Artifact pagination contains duplicates")
    matches = [a for a in rows if a.get("name") == artifact_name]
    if len(matches) != 1:
        raise ValueError("Formal RC artifact is missing or ambiguous")
    artifact = matches[0]
    origin = artifact.get("workflow_run")
    if (not positive_integer(artifact.get("id")) or artifact.get("expired") is not False
            or not positive_integer(artifact.get("size_in_bytes"))
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest"))) is None
            or not isinstance(origin, dict)
            or type(origin.get("id")) is not int or origin["id"] != receipt["run_id"]
            or type(origin.get("repository_id")) is not int or origin["repository_id"] != receipt["repository_id"]
            or type(origin.get("head_repository_id")) is not int or origin["head_repository_id"] != receipt["repository_id"]
            or origin.get("head_sha") != receipt["source_commit"] or origin.get("head_branch") != "main"):
        raise ValueError("Artifact is expired, incomplete or bound to another run/repository/commit")
    created = timestamp(artifact.get("created_at"))
    if not timestamp(receipt["run_started_at"]) < created <= timestamp(receipt["run_updated_at"]):
        raise ValueError("Artifact was not created within the selected run attempt")
    receipt["artifact"] = {key: artifact[key] for key in ("id", "name", "digest", "size_in_bytes", "created_at")}
    if expected is not None and (not isinstance(expected, dict)
            or json.dumps(expected, sort_keys=True) != json.dumps(receipt, sort_keys=True)):
        raise ValueError("Selected RC run attempt or artifact changed during download")
    return receipt


def read_metadata(path: Path) -> object:
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("RC metadata exceeds the bounded reader")
    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate RC metadata field")
            result[name] = value
        return result
    def reject_constant(value):
        raise ValueError("Non-finite RC metadata number")
    return json.loads(path.read_text(encoding="utf-8-sig"),
                      object_pairs_hook=unique_object, parse_constant=reject_constant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--attempt-json", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--artifacts-json", required=True, type=Path)
    parser.add_argument("--artifact-name", required=True)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--write-receipt", type=Path)
    output.add_argument("--expected-receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        run = read_metadata(args.run_json)
        attempt = read_metadata(args.attempt_json)
        identity = dict(repository=args.repository, commit=args.commit, run_id=args.run_id)
        if validate_run(run, **identity) != validate_run(attempt, **identity):
            raise ValueError("Latest run and explicit run-attempt metadata differ")
        receipt = validate_selection(
            attempt, read_metadata(args.artifacts_json),
            artifact_name=args.artifact_name, repository=args.repository,
            commit=args.commit, run_id=args.run_id,
            expected_receipt=(read_metadata(args.expected_receipt)
                              if args.expected_receipt else None),
        )
        if args.write_receipt:
            with args.write_receipt.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, indent=2)
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "BLOCKED", "detail": "RC run provenance is missing, invalid or changed; no acceptance is granted"}))
        return 1
    print(json.dumps({"status": "RC_RUN_SOURCE_VALIDATED_NOT_RC_ACCEPTED",
                      "run_id": receipt["run_id"], "run_attempt": receipt["run_attempt"],
                      "artifact_id": receipt["artifact"]["id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
