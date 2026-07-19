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
    assert not kinds("tests/siyi/synthetic.txt", data)


def test_privacy_scan_does_not_treat_task_identifiers_as_api_keys() -> None:
    assert "api_key" not in kinds("siyi/app/runtime.py", b"task-lease-heartbeat-1234567890abcdef")


def test_privacy_scan_distinguishes_pii_from_machine_generated_hex() -> None:
    phone = b"138" + b"0013" + b"8000"
    identity = b"110105" + b"19491231" + b"002X"
    assert {"cn_phone", "cn_id"} <= kinds("notes.txt", phone + b" " + identity)
    assert not ({"cn_phone", "cn_id"} & kinds("sbom.json", b"abc" + phone + b"def 9f" + identity + b"aa"))


def test_filesystem_scan_skips_rebuildable_dependency_directories(tmp_path: Path) -> None:
    ignored = tmp_path / "node_modules" / "package" / "private.db"
    generated = tmp_path / "build-output.exe"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(b"SQLite format 3\x00fixture")
    generated.write_bytes(b"generated")
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("print('ok')\n", encoding="utf-8")
    paths = [path for path, _ in privacy_scan.filesystem_blobs(tmp_path)]
    assert source.as_posix() in paths
    assert ignored.as_posix() not in paths
    assert generated.as_posix() not in paths
