"""Exercise the packaged attention inbox with an owned, synthetic Mock profile."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import runpy
import socket
import sqlite3
import stat
import statistics
import subprocess
import time
from typing import Any
import uuid


_HERE = Path(__file__).resolve().parent
_ENDPOINT = "/api/tasks/attention"
_FIELDS = {"task_id", "conversation_id", "status", "category", "updated_at"}
_SYNTHETIC_PROMPT = "Internal synthetic attention fixture; do not render in the inbox."
_SNAPSHOT = {"kind": "no_workspace", "key_files": [], "tracked_paths": []}


def assert_unlinked(path: Path) -> None:
    for member in (path, *path.parents):
        try:
            info = member.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked/reparse smoke paths are not accepted")


def seed_attention(database: Path) -> dict[str, dict[str, Any]]:
    """Seed only the caller-owned synthetic DB after real sidecar initialization."""
    if not database.is_file():
        raise ValueError("Sidecar did not initialize the fixture database")
    stamp = datetime.now(timezone.utc).isoformat()
    workspace_hash = hashlib.sha256(json.dumps(_SNAPSHOT, sort_keys=True).encode()).hexdigest()
    definitions = (
        ("confirmation", "Smoke: pending confirmation", "waiting_confirmation", False),
        ("recovery", "Smoke: resume interruption", "interrupted", True),
        ("native", "Smoke: desktop recovery fixture", "interrupted", True),
    )
    seeded: dict[str, dict[str, Any]] = {}
    with closing(sqlite3.connect(database, timeout=8)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        for label, title, status, checkpoint in definitions:
            task_id = uuid.uuid4().hex
            conversation = db.execute(
                "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                (title, "", "ask", stamp, stamp),
            ).lastrowid
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at,"
                "checkpoint_sequence,resumable,termination_reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, conversation, status, _SYNTHETIC_PROMPT, stamp, stamp, int(checkpoint), 1, "synthetic interruption"),
            )
            db.execute(
                "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                (conversation, "user", _SYNTHETIC_PROMPT, task_id, stamp),
            )
            if checkpoint:
                state = json.dumps({"workspace_hash": workspace_hash, "workspace_snapshot": _SNAPSHOT, "goal": _SYNTHETIC_PROMPT})
                db.execute(
                    "INSERT INTO task_checkpoints(task_id,sequence,phase,reason,state,workspace_hash,git_status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (task_id, 1, "analysis", "owned_attention_smoke", state, workspace_hash, "", stamp),
                )
            seeded[label] = {"task_id": task_id, "conversation_id": conversation, "status": status, "updated_at": stamp}
    return seeded


def stored_state(database: Path, fixtures: dict[str, dict[str, Any]]) -> dict[str, tuple[str, int, int]]:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=8)) as db:
        return {
            label: tuple(db.execute(
                "SELECT status,resumable,resume_count FROM agent_tasks WHERE id=?", (item["task_id"],),
            ).fetchone())
            for label, item in fixtures.items()
        }


def run(binary: Path, output: Path, expected_version: str, native_owner: str | None) -> dict[str, Any]:
    assert_unlinked(binary)
    assert_unlinked(output)
    if binary.name.casefold() != "agent-backend.exe" or not binary.is_file() or output.exists():
        raise ValueError("Use a packaged agent-backend.exe and a new output directory")
    if native_owner is not None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", native_owner) is None:
            raise ValueError("Invalid native-profile owner")
        repo = _HERE.parent
        if output.is_relative_to(repo) or repo.is_relative_to(output):
            raise ValueError("Owned desktop fixture must be outside the checkout")
    manifest = binary.parent / "_internal" / "build-info.json"
    assert_unlinked(manifest)
    build = json.loads(manifest.read_text(encoding="utf-8"))
    if build.get("product_version") != expected_version or not build.get("git_commit") or not build.get("build_id"):
        raise ValueError("Packaged manifest does not identify the expected version and source")
    prior_smoke = runpy.run_path(str(_HERE / "smoke-provider-connection.py"))
    output.mkdir(parents=True)
    profile = output / "isolated-profile"
    for relative in ("data", "logs", "config", "home/AppData/Local", "home/AppData/Roaming", "temp", "extensions"):
        (profile / relative).mkdir(parents=True)
    configuration = profile / "config" / "provider-settings.json"
    configuration.write_text(json.dumps(prior_smoke["MOCK_CONFIGURATION"]), encoding="utf-8")
    config_before = configuration.read_bytes()
    empty_env = profile / "config" / "desktop.env"
    empty_env.write_text("", encoding="utf-8")
    database = profile / "data" / "agent.db"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = uuid.uuid4().hex
    environment = prior_smoke["controlled_environment"](profile, port, token)
    environment.update({
        "AGENT_ENV_FILE": str(empty_env), "AGENT_PROVIDER_CONFIG_PATH": str(configuration),
        "AGENT_EXTENSION_DIRECTORY": str(profile / "extensions"),
    })

    def request(
        path: str, *, expected: int = 200, authenticated: bool = True, body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        # No proxy or redirect support: the API token can only go to this port.
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
            headers = {"X-Agent-Api-Token": token} if authenticated else {}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request("POST" if body is not None else "GET", path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            if response.status != expected:
                raise RuntimeError(f"Unexpected HTTP status {response.status}; expected {expected}")
            content = response.read(512 * 1024 + 1)
            if len(content) > 512 * 1024:
                raise RuntimeError("Unbounded smoke response")
            return json.loads(content), response.getheader("Cache-Control")

    process = None
    with (output / "stdout.log").open("xb") as stdout, (output / "stderr.log").open("xb") as stderr:
        try:
            started = time.perf_counter()
            process = subprocess.Popen([str(binary)], env=environment, cwd=binary.parent,
                                       stdout=stdout, stderr=stderr,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ready_by = time.monotonic() + 50
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Isolated sidecar exited before readiness")
                try:
                    health, _ = request("/api/health", authenticated=False)
                    break
                except (OSError, TimeoutError):
                    if time.monotonic() >= ready_by:
                        raise RuntimeError("Isolated sidecar readiness timed out") from None
                    time.sleep(0.2)
            readiness_ms = round((time.perf_counter() - started) * 1000, 2)
            if (health.get("status"), health.get("provider"), health.get("version")) != ("ok", "mock", expected_version):
                raise RuntimeError("Wrong isolated provider, version or health")
            if any(health.get("build", {}).get(key) != build[key] for key in ("git_commit", "build_id")):
                raise RuntimeError("Sidecar runtime build differs from packaged manifest")
            request(_ENDPOINT, expected=401, authenticated=False)
            empty, _ = request(_ENDPOINT)
            if empty != {"items": [], "total": 0}:
                raise RuntimeError("Expected an empty test-owned profile")
            fixtures = seed_attention(database)
            before = stored_state(database, fixtures)
            timings = []
            for _ in range(7):
                sample = time.perf_counter()
                inbox, cache = request(_ENDPOINT)
                timings.append(round((time.perf_counter() - sample) * 1000, 2))
                if (cache != "no-store" or inbox["total"] != 3 or len(inbox["items"]) != 3
                        or any(set(item) != _FIELDS for item in inbox["items"])):
                    raise RuntimeError("Attention count, fields or cache policy mismatch")
                if {item["task_id"]: (item["status"], item["conversation_id"], item["updated_at"])
                    for item in inbox["items"]} != {
                    item["task_id"]: (item["status"], item["conversation_id"], item["updated_at"])
                    for item in fixtures.values()
                }:
                    raise RuntimeError("Attention projection disagrees with the fixture DB")
            capped, _ = request(_ENDPOINT + "?limit=1")
            if capped["total"] != 3 or capped["items"] != inbox["items"][:1]:
                raise RuntimeError("Attention cap changed total or priority")
            if stored_state(database, fixtures) != before:
                raise RuntimeError("Reading attention unexpectedly changed task state")
            abandon_id = fixtures["confirmation"]["task_id"]
            abandoned, _ = request(f"/api/tasks/{abandon_id}/abandon", body={})
            if abandoned.get("status") != "cancelled":
                raise RuntimeError("Original abandon action did not cancel")
            resumed_id = fixtures["recovery"]["task_id"]
            request(f"/api/tasks/{resumed_id}/resume", expected=202, body={"checkpoint_sequence": 1})
            completion_by = time.monotonic() + 25
            while True:
                task, _ = request(f"/api/tasks/{resumed_id}")
                if task.get("status") == "completed" and task.get("result"):
                    break
                if task.get("status") in {"failed", "cancelled", "blocked", "partially_completed"} or time.monotonic() >= completion_by:
                    raise RuntimeError("Original resume action did not complete with Mock")
                time.sleep(0.1)
            if task["result"].get("content") != "mock response":
                raise RuntimeError("Expected synthetic Mock response")
            remaining, _ = request(_ENDPOINT)
            if remaining["total"] != 1 or len(remaining["items"]) != 1 or remaining["items"][0]["task_id"] != fixtures["native"]["task_id"]:
                raise RuntimeError("Processed attention rows did not converge")
            for label in ("confirmation", "recovery"):
                scoped, _ = request(_ENDPOINT + f"?conversation_id={fixtures[label]['conversation_id']}")
                if scoped != {"items": [], "total": 0}:
                    raise RuntimeError("Resolved conversation still has attention")
            if stored_state(database, fixtures) != {
                "confirmation": ("cancelled", 0, 0), "recovery": ("completed", 0, 1), "native": ("interrupted", 1, 0),
            }:
                raise RuntimeError("Operator actions did not converge in SQLite")
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=8)) as db:
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",) or db.execute("SELECT COUNT(*) FROM model_runs").fetchone() != (0,):
                    raise RuntimeError("Fixture DB integrity or offline model evidence failed")
            if configuration.read_bytes() != config_before:
                raise RuntimeError("Smoke changed provider configuration")
            request("/api/desktop/shutdown", expected=202, body={})
            process.wait(timeout=15)
            if process.returncode:
                raise RuntimeError("Isolated sidecar did not exit cleanly")
            if native_owner is not None:
                (profile / "local-profile-owner.json").write_text(json.dumps({
                    "schema_version": 1, "owner": native_owner, "data_root": str(profile),
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "initialized_runtime_commit": build["git_commit"],
                }), encoding="utf-8")
            result = {
                "status": "PASS", "scope": "isolated_packaged_sidecar_not_native_ui",
                "version": expected_version, "build_id": build["build_id"], "source_commit": build["git_commit"],
                "readiness_ms": readiness_ms, "attention_ms": timings,
                "attention_median_ms": statistics.median(timings), "attention_max_ms": max(timings),
                "attention_initial_count": 3, "attention_final_count": 1,
                "unauthenticated_status": 401, "authenticated_cache_control": "no-store",
                "simulated_operator_abandon": "PASS", "simulated_operator_resume_mock": "PASS",
                "sqlite_quick_check": "ok", "model_runs": 0, "paid_api_requests": 0,
                "graceful_shutdown": True, "native_fixture_ready": native_owner is not None,
            }
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            if process is not None and process.poll() is None:
                # Popen retains the exact process handle. Do not target other
                # sidecars or stale numeric process IDs.
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--expected-version", default="16.0.2")
    parser.add_argument("--native-owner", help="Write owner metadata to the new fixture for native UI testing")
    args = parser.parse_args()
    result = run(Path(os.path.abspath(args.binary)), Path(os.path.abspath(args.output_directory)),
                 args.expected_version, args.native_owner)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
