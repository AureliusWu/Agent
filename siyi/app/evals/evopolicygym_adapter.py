from __future__ import annotations

import argparse
import ast
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import httpx


JsonObject = dict[str, Any]


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    path = Path(value).resolve()
    if not path.exists():
        raise RuntimeError(f"{name} does not exist: {path}")
    return path


def _require_isolated_workspace(workspace: Path, system: Path, feedback: Path) -> None:
    if system.parent != workspace or feedback.parent != workspace:
        raise RuntimeError("EvoPolicyGym system and feedback paths must be direct workspace children")
    if system.name != "system" or feedback.name != "feedback":
        raise RuntimeError("unexpected EvoPolicyGym workspace layout")
    if not (workspace / "AGENTS.md").is_file():
        raise RuntimeError("EvoPolicyGym workspace is missing AGENTS.md")


@dataclass(slots=True)
class SiyiEvoSession:
    repo_root: Path
    workspace: Path
    data_root: Path
    model_api_key: str
    timeout_seconds: float = 900.0
    process: subprocess.Popen[bytes] | None = None
    client: httpx.Client | None = None
    conversation_id: int | None = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    port: int = field(default_factory=_free_loopback_port)
    stderr_handle: TextIO | None = None
    benchmark_endpoint: str | None = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Agent-Api-Token": self.token}

    def start(self) -> None:
        if self.process is not None:
            return
        self.data_root.mkdir(parents=True, exist_ok=False)
        stderr_path = self.data_root / "sidecar.stderr.log"
        self.stderr_handle = stderr_path.open("w", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "AGENT_PORT": str(self.port),
                "AGENT_DATA_ROOT": str(self.data_root),
                "AGENT_DATABASE_PATH": str(self.data_root / "data" / "agent.db"),
                "AGENT_LOG_PATH": str(self.data_root / "logs" / "agent.log"),
                "AGENT_EXTENSION_DIRECTORY": str(self.data_root / "extensions"),
                "AGENT_BIND_HOST": "127.0.0.1",
                "AGENT_API_TOKEN": self.token,
                "AGENT_DEPLOYMENT_MODE": "desktop_local",
                "AGENT_ALLOW_LOCAL_MCP": "false",
                "AGENT_ALLOW_PRIVATE_MODEL_PROVIDER": "false",
                "AGENT_NETWORK_ALLOW_HTTP": "false",
            }
        )
        python = self.repo_root / "siyi" / ".venv" / "Scripts" / "python.exe"
        server = self.repo_root / "siyi" / "run_server.py"
        if not python.is_file() or not server.is_file():
            raise RuntimeError("Siyi backend runtime is unavailable")
        self.process = subprocess.Popen(
            [str(python), str(server)],
            cwd=str(server.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self.stderr_handle,
        )
        self.client = httpx.Client(timeout=self.timeout_seconds)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Siyi sidecar exited during startup: {self.process.returncode}")
            try:
                response = self.client.get(f"{self.endpoint}/api/health", timeout=1)
                if response.status_code == 200 and response.json().get("status") == "ok":
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Siyi sidecar did not become ready within 30 seconds")
        response = self.client.post(
            f"{self.endpoint}/api/conversations",
            headers=self.headers,
            json={
                "title": "EvoPolicyGym isolated run",
                "workspace": str(self.workspace),
                "permission_mode": "full",
                "agent_profile_id": "general",
            },
        )
        response.raise_for_status()
        self.conversation_id = int(response.json()["id"])

    def step(self, prompt: str) -> JsonObject:
        if self.client is None or self.conversation_id is None:
            raise RuntimeError("Siyi Evo session is not started")
        prompt = self._augment_with_benchmark_context(prompt)
        headers = self.headers
        if self.model_api_key:
            headers = {**headers, "X-Model-Api-Key": self.model_api_key}
        response = self.client.post(
            f"{self.endpoint}/api/chat",
            headers=headers,
            json={
                "conversation_id": self.conversation_id,
                "content": prompt,
                "task_id": uuid.uuid4().hex,
                "interaction_mode": "conversation",
                "data_location": "local_workspace",
                "privacy_scope": "workspace",
                "memory_write_policy": "deny",
                "orchestration_mode": "single",
                "agent_count": 1,
                "reasoning_effort": "high",
            },
        )
        response.raise_for_status()
        payload = response.json()
        materialized = self._materialize_policy_from_response(
            str(payload.get("content") or "")
        )
        task_id = str(payload.get("task_id") or "")
        if not materialized and task_id:
            materialized = self._materialize_pending_policy(task_id)
            if materialized:
                self.client.post(
                    f"{self.endpoint}/api/tasks/{task_id}/cancel",
                    headers=self.headers,
                ).raise_for_status()
        bridge = self._submit_policy_if_ready()
        content = str(payload.get("content") or "")
        if materialized:
            content += "\n\nEvoPolicyGym bridge materialized the validated model policy."
        if bridge is not None:
            content += (
                "\n\nEvoPolicyGym bridge submitted the policy produced in this turn. "
                f"Official response: {json.dumps(bridge, ensure_ascii=True, default=str)}"
            )
        return {
            "text": content,
            "stop": payload.get("task_status")
            in {
                "failed",
                "blocked",
                "cancelled",
                "interrupted",
                "waiting_provider",
                "partially_completed",
            },
            "data": {
                "task_id": payload.get("task_id"),
                "task_status": payload.get("task_status"),
                "verification": payload.get("verification"),
                "usage": payload.get("usage"),
            },
        }

    def _augment_with_benchmark_context(self, prompt: str) -> str:
        if self.client is None:
            raise RuntimeError("Siyi Evo session is not started")
        if self.benchmark_endpoint is None:
            for token in prompt.split():
                candidate = token.rstrip(".,;)]}")
                parsed = urlparse(candidate)
                if (
                    parsed.scheme == "http"
                    and parsed.hostname == "127.0.0.1"
                    and parsed.port is not None
                ):
                    self.benchmark_endpoint = f"http://127.0.0.1:{parsed.port}"
                    break
        if self.benchmark_endpoint is None:
            return prompt
        info_response = self.client.get(f"{self.benchmark_endpoint}/info")
        task_response = self.client.get(f"{self.benchmark_endpoint}/task")
        info_response.raise_for_status()
        task_response.raise_for_status()
        task_text = task_response.text
        return (
            f"{prompt}\n\n"
            "The isolated Siyi Evo bridge fetched the official loopback API for this turn. "
            "Do not stop after inspection, do not call the HTTP API yourself, and do not use "
            "write or command tools. Produce the complete system/policy.py source in your "
            "final response exactly between POLICY_PY_BEGIN and POLICY_PY_END. The isolated "
            "bridge will syntax-check and atomically materialize those exact model-generated "
            "bytes, then submit that real file after your turn.\n\n"
            f"OFFICIAL_INFO_JSON:\n{json.dumps(info_response.json(), ensure_ascii=True)}\n\n"
            f"OFFICIAL_TASK:\n{task_text[:30_000]}"
        )

    def _materialize_policy_from_response(self, content: str) -> bool:
        match = re.search(
            r"POLICY_PY_BEGIN(.*?)POLICY_PY_END",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if match is None:
            return False
        source = match.group(1).strip()
        source = re.sub(r"^```(?:python)?\s*\r?\n", "", source, flags=re.IGNORECASE)
        source = re.sub(r"\r?\n```\s*$", "", source)
        self._write_policy_source(source)
        return True

    def _materialize_pending_policy(self, task_id: str) -> bool:
        database = self.data_root / "data" / "agent.db"
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT result FROM task_operations "
                "WHERE task_id=? AND tool='create_file' AND status='waiting_confirmation' "
                "ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return False
        proposal = json.loads(str(row[0]))
        arguments = proposal.get("arguments") or {}
        if arguments.get("path") != "system/policy.py":
            raise RuntimeError("pending Evo policy proposal targets an unexpected path")
        source = arguments.get("content")
        if not isinstance(source, str):
            raise RuntimeError("pending Evo policy proposal has no text content")
        self._write_policy_source(source)
        return True

    def _write_policy_source(self, source: str) -> None:
        source = source.strip() + "\n"
        if len(source.encode("utf-8")) > 100_000:
            raise RuntimeError("model-generated Evo policy exceeds 100 KB")
        tree = ast.parse(source, filename="system/policy.py")
        if not any(
            isinstance(node, ast.ClassDef) and node.name == "Policy"
            for node in tree.body
        ):
            raise RuntimeError("model-generated Evo policy is missing class Policy")
        target = self.workspace / "system" / "policy.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".policy.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(source)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def _submit_policy_if_ready(self) -> JsonObject | None:
        if self.client is None or self.benchmark_endpoint is None:
            return None
        policy = self.workspace / "system" / "policy.py"
        if not policy.is_file():
            return None
        info_response = self.client.get(f"{self.benchmark_endpoint}/info")
        info_response.raise_for_status()
        info = info_response.json()
        state = info.get("state") or {}
        if state.get("is_finalized"):
            return {"status": "already_finalized"}
        remaining = int(state.get("remaining_budget") or 0)
        maximum = int(info.get("max_episodes_per_submit") or 1)
        total = int((info.get("env_meta") or {}).get("n_env_instances") or 1)
        count = min(remaining, maximum, total)
        if count < 1:
            return {"status": "no_budget"}
        submit = self.client.post(
            f"{self.benchmark_endpoint}/submit",
            json={"env_instances": list(range(count))},
        )
        submit.raise_for_status()
        payload = submit.json()
        return payload if isinstance(payload, dict) else {"result": payload}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.stderr_handle is not None:
            self.stderr_handle.close()
            self.stderr_handle = None


def run_jsonl(
    session: SiyiEvoSession,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        session.start()
        for raw in stdin:
            try:
                request = json.loads(raw)
                if not isinstance(request, dict) or request.get("type") != "prompt":
                    raise ValueError("request must be a prompt JSON object")
                turn = request.get("turn")
                if not isinstance(turn, int):
                    raise ValueError("request turn must be an integer")
                message = request.get("message")
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("request message must be a non-empty string")
                reply = session.step(message)
                frame = {"turn": turn, **reply}
            except Exception as exc:
                print(f"SiyiEvoAdapter turn failed: {type(exc).__name__}: {exc}", file=stderr, flush=True)
                frame = {
                    "turn": request.get("turn", 0) if isinstance(request, dict) else 0,
                    "text": "",
                    "stop": True,
                    "data": {"error_type": type(exc).__name__, "error": str(exc)},
                }
            # The official Windows command-agent opens pipes with the active
            # console code page. Keep the wire bytes ASCII-safe while retaining
            # full Unicode semantics through JSON escapes.
            stdout.write(json.dumps(frame, ensure_ascii=True, separators=(",", ":")) + "\n")
            stdout.flush()
            if frame["stop"]:
                break
        return 0
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent JSONL adapter from EvoPolicyGym to Siyi Runtime")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    args = parser.parse_args()

    workspace = _required_path("EVOPOLICYGYM_WORKSPACE")
    system = _required_path("EVOPOLICYGYM_SYSTEM")
    feedback = _required_path("EVOPOLICYGYM_FEEDBACK")
    _require_isolated_workspace(workspace, system, feedback)
    data_root = args.data_root
    if data_root is None:
        parent = Path(tempfile.gettempdir()) / "Siyi-Evals"
        parent.mkdir(parents=True, exist_ok=True)
        data_root = parent / f"evopolicygym-{uuid.uuid4().hex}"
    session = SiyiEvoSession(
        repo_root=args.repo_root.resolve(),
        workspace=workspace,
        data_root=data_root.resolve(),
        model_api_key=os.environ.get("SIYI_EVO_MODEL_API_KEY", "").strip(),
        timeout_seconds=args.timeout_seconds,
    )
    return run_jsonl(session, sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
