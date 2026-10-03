from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def _configuration(tmp_path: Path, env_file: Path | None, **overrides: str) -> dict:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "siyi")
    if env_file is not None:
        environment["AGENT_ENV_FILE"] = str(env_file)
    environment.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json; from app.config import settings; "
            "print(json.dumps({'model': settings.model_name, "
            "'database': str(settings.database_path), 'speech': settings.speech_enabled}))"
        )],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_settings_load_external_env_without_copying_into_checkout(tmp_path: Path) -> None:
    local = tmp_path / "private-config" / "agent.env"
    local.parent.mkdir()
    local.write_text("AGENT_MODEL_NAME=external-test-model\nAGENT_SPEECH_ENABLED=false\n", encoding="utf-8")
    assert _configuration(tmp_path, local)["model"] == "external-test-model"
    assert not (tmp_path / ".env").exists()


def test_process_settings_override_external_env(tmp_path: Path) -> None:
    local = tmp_path / "agent.env"
    local.write_text("AGENT_MODEL_NAME=file-test-model\n", encoding="utf-8")
    result = _configuration(tmp_path, local, AGENT_MODEL_NAME="process-test-model")
    assert result["model"] == "process-test-model"


def test_external_runtime_root_is_used_for_database(tmp_path: Path) -> None:
    root = tmp_path / "private-runtime"
    result = _configuration(tmp_path, None, AGENT_DATA_ROOT=str(root))
    assert Path(result["database"]) == root / "data" / "agent.db"
    assert not root.exists()


def test_missing_external_env_does_not_create_local_secret_file(tmp_path: Path) -> None:
    local = tmp_path / "absent" / "agent.env"
    result = _configuration(tmp_path, local)
    assert result["speech"] is False
    assert not local.exists()
    assert not (tmp_path / ".env").exists()


def test_portable_launcher_reads_local_paths_without_embedded_private_paths() -> None:
    root = Path(__file__).resolve().parents[3]
    launcher = (root / "scripts" / "start-desktop.ps1").read_text(encoding="utf-8")
    assert "AGENT_DESKTOP_DATA_DIRECTORY" in launcher
    assert "GetEnvironmentVariable($name, 'User')" in launcher
    assert "GetEnvironmentVariable($name, 'Process')" in launcher
    assert "Users" not in launcher
    assert "start-desktop.ps1" in (root / "启动司忆.cmd").read_text(encoding="utf-8")
