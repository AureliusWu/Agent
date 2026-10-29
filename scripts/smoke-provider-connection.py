"""Bounded frozen-runtime smoke: no user profile, credentials, or chat calls.

The optional DeepSeek probe uses a deliberately invalid, non-secret credential
with GET /models. An authentication rejection proves TLS transport only, never
that a real account or a model completion has been validated.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid


SYSTEM_ENVIRONMENT = {
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "PROGRAMDATA",
    "ALLUSERSPROFILE", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "USERNAME", "USERDOMAIN", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER", "OS", "SESSIONNAME",
}
MOCK_CONFIGURATION = {
    "provider_id": "mock", "base_url": "", "model": "", "timeout_seconds": 12,
    "max_tokens": 8192, "max_retries": 0, "allow_tools": True,
    "allow_streaming": True,
}


def controlled_environment(root: Path, port: int, token: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in SYSTEM_ENVIRONMENT}
    environment.update({
        "AGENT_PORT": str(port), "AGENT_PARENT_PID": str(os.getpid()),
        "AGENT_API_TOKEN": token, "AGENT_DEPLOYMENT_MODE": "desktop_local",
        "AGENT_BIND_HOST": "127.0.0.1", "AGENT_RUNTIME_ENV": "production",
        "AGENT_DATA_ROOT": str(root), "AGENT_DESKTOP_DATA_DIRECTORY": str(root),
        "AGENT_DATABASE_PATH": str(root / "data" / "agent.db"),
        "AGENT_LOG_PATH": str(root / "logs" / "agent.log"),
        "AGENT_PROVIDER_CONFIG_PATH": str(root / "config" / "provider.json"),
        "AGENT_ENV_FILE": str(root / "config" / "empty.env"),
        "AGENT_ALLOW_LOCAL_MCP": "false", "AGENT_MODEL_MAX_RETRIES": "0",
        "AGENT_MODEL_ESCALATION_ENABLED": "false", "AGENT_NETWORK_MAX_REDIRECTS": "0",
        "SIYI_TEST_PROVIDER": "mock", "SIYI_ALLOW_PAID_API": "false",
        "HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
        "APPDATA": str(root / "home" / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(root / "home" / "AppData" / "Local"),
        "TEMP": str(root / "temp"), "TMP": str(root / "temp"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return environment


def authentication_rejection(result: dict) -> bool:
    return (result.get("status") == "error"
            and result.get("error_type") == "authentication"
            and result.get("http_status") == 401
            and not result.get("models"))


def run(binary: Path, output: Path, probe_deepseek: bool) -> dict:
    binary = binary.resolve(strict=True)
    if not (binary.parent / "_internal" / "build-info.json").is_file():
        raise ValueError("A complete, source-identified frozen runtime is required")
    if output.exists():
        raise ValueError("Use a new output directory; prior results are never overwritten")
    for ancestor in (output, *output.parents):
        if ancestor.exists() and (ancestor.is_symlink() or ancestor.is_junction()):
            raise ValueError("Linked smoke directories are not accepted")
    output.mkdir(parents=True)
    root = output / "isolated-profile"
    for relative in ("data", "logs", "config", "home/AppData/Local", "home/AppData/Roaming", "temp"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    configuration = root / "config" / "provider.json"
    configuration.write_text(json.dumps(MOCK_CONFIGURATION), encoding="utf-8")
    (root / "config" / "empty.env").write_text("", encoding="utf-8")
    before = configuration.read_bytes()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = uuid.uuid4().hex
    environment = controlled_environment(root, port, token)
    tls_samples = []
    for _ in range(3):
        started = time.perf_counter()
        probe = subprocess.run([str(binary), "--tls-self-check"], env=environment,
                               cwd=binary.parent, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30, check=False)
        if probe.returncode:
            raise RuntimeError("Frozen offline TLS self-check failed; inspect private output")
        payload = json.loads(probe.stdout)
        if (payload.get("status") != "passed" or not payload.get("frozen")
                or not payload.get("certificate_verification") or not payload.get("hostname_verification")
                or payload.get("network_requests") != 0 or payload.get("credential_reads") != 0
                or payload.get("application_started") is not False):
            raise RuntimeError("Frozen TLS self-check did not report success")
        tls_samples.append(round((time.perf_counter() - started) * 1000, 2))

    opener = build_opener(ProxyHandler({}))

    def request(path: str, body: dict | None = None, credential: str | None = None) -> dict:
        if path not in {"/api/health", "/api/provider/configuration", "/api/provider/models", "/api/desktop/shutdown"}:
            raise ValueError("Smoke is restricted to non-chat diagnostic endpoints")
        headers = {"X-Agent-Api-Token": token}
        if credential:
            headers["X-Model-Api-Key"] = credential
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        call = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
        with opener.open(call, timeout=25) as response:
            return json.load(response)

    process = None
    with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
        try:
            started = time.perf_counter()
            process = subprocess.Popen([str(binary)], env=environment, cwd=binary.parent,
                                       stdout=stdout, stderr=stderr,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + 50
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Isolated sidecar exited before readiness")
                try:
                    health = request("/api/health")
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Isolated sidecar readiness timed out")
                    time.sleep(0.25)
            readiness_ms = round((time.perf_counter() - started) * 1000, 2)
            if health.get("status") != "ok" or health.get("provider") != "mock":
                raise RuntimeError("Isolated runtime identity/provider check failed")
            health_samples = []
            for _ in range(5):
                started = time.perf_counter()
                request("/api/health")
                health_samples.append(round((time.perf_counter() - started) * 1000, 2))
            draft = request("/api/provider/models", MOCK_CONFIGURATION)
            if draft.get("status") != "ok" or not draft.get("models"):
                raise RuntimeError("Packaged draft model discovery failed")
            if configuration.read_bytes() != before or request("/api/provider/configuration") != MOCK_CONFIGURATION:
                raise RuntimeError("Draft model discovery persisted configuration unexpectedly")
            remote_probe = "NOT_RUN"
            if probe_deepseek:
                draft_configuration = {**MOCK_CONFIGURATION, "provider_id": "deepseek",
                                       "base_url": "https://api.deepseek.com", "model": "deepseek-flash"}
                rejected = request("/api/provider/models", draft_configuration,
                                   "siyi-connectivity-probe-not-a-real-key")
                if not authentication_rejection(rejected):
                    raise RuntimeError("Expected unauthenticated DeepSeek rejection was not observed")
                remote_probe = "TLS_CONNECTED_EXPECTED_401_NO_REAL_CREDENTIAL"
            if configuration.read_bytes() != before:
                raise RuntimeError("DeepSeek draft probe changed saved configuration")
            result = {
                "status": "PASS", "scope": "isolated_frozen_runtime_diagnostics_not_account_validation",
                "version": health["version"], "build_id": health["build"]["build_id"],
                "compiled_commit": health["build"].get("git_commit"),
                "schema": health["database"]["schema_version"],
                "tls_offline_runs": 3, "tls_process_ms": tls_samples,
                "tls_median_ms": statistics.median(tls_samples), "readiness_ms": readiness_ms,
                "health_ms": health_samples, "health_median_ms": statistics.median(health_samples),
                "health_max_ms": max(health_samples), "mock_model_discovery": "PASS",
                "draft_configuration_unchanged": True, "deepseek_transport": remote_probe,
                "real_credentials_read": False, "chat_requests": 0, "paid_validation": False,
                "user_profile_accessed": False,
            }
            request("/api/desktop/shutdown", {})
            process.wait(timeout=15)
            if process.returncode:
                raise RuntimeError("Isolated sidecar did not exit cleanly")
            result["graceful_shutdown"] = True
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            # Popen owns the exact process handle; never target a reused numeric PID.
            if process is not None and process.poll() is None:
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
    parser.add_argument("--probe-deepseek", action="store_true",
                        help="GET official /models with a synthetic invalid key; no paid model request")
    arguments = parser.parse_args()
    result = run(arguments.binary, arguments.output_directory.absolute(), arguments.probe_deepseek)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
