from __future__ import annotations

import importlib
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "privacy_scan.py"
sys.path.insert(0, str(SCRIPT.parent))
privacy_scan = importlib.import_module("privacy_scan")


def kinds(path: str, data: bytes) -> set[str]:
    return {item.kind for item in privacy_scan.scan_blob(path, data, {"forbidden", "secrets", "pii"})}


def test_privacy_scan_blocks_runtime_data_and_sqlite_headers() -> None:
    assert "forbidden_runtime_path" in kinds("data/agent.db", b"SQLite format 3\x00payload")
    assert "sqlite_header" in kinds("renamed.bin", b"SQLite format 3\x00payload")


def test_privacy_scan_blocks_secret_and_personal_path_without_echoing_value() -> None:
    secret = b"sk" + b"-live_like_value_123456789"
    personal_path = b"C" + b":" + b"\\Users\\actual-user\\file.txt"
    result = kinds("notes.txt", b"token=" + secret + b" " + personal_path)
    assert {"api_key", "windows_user_path", "absolute_drive_path"} <= result


def test_privacy_scan_allows_declared_synthetic_fixtures() -> None:
    data = b"sk-test_DO_NOT_USE_000000000000 C:\\Users\\test\\AppData\\Local agent@example.invalid"
    assert not kinds("backend/tests/synthetic.txt", data)
