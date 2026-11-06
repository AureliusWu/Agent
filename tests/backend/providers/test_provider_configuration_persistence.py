from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.providers import configuration
from app.providers.configuration import ProviderConfiguration


@pytest.fixture()
def config_target(monkeypatch, tmp_path):
    path = tmp_path / "中文配置" / "provider-settings.json"
    path.parent.mkdir()
    original = ProviderConfiguration(model="deepseek-flash", max_tokens=8192)
    path.write_text(json.dumps(asdict(original)), encoding="utf-8")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(path))
    return path, original


def staged_paths(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def test_save_syncs_complete_utf8_file_before_closed_handle_replace(config_target, monkeypatch):
    path, original = config_target
    expected = replace(original, timeout_seconds=123)
    old_bytes = path.read_bytes()
    real_fsync, real_replace = configuration.os.fsync, Path.replace
    observed = []

    def sync(fd):
        candidates = staged_paths(path)
        assert len(candidates) == 1
        assert candidates[0].parent == path.parent
        assert json.loads(candidates[0].read_text(encoding="utf-8")) == asdict(expected)
        assert path.read_bytes() == old_bytes
        observed.append("fsync")
        return real_fsync(fd)

    def commit(temporary, target):
        assert target == path
        assert observed == ["fsync"]
        # A real Windows rename proves the staging handle is already closed.
        result = real_replace(temporary, target)
        observed.append("replace")
        return result

    monkeypatch.setattr(configuration.os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", commit)
    assert configuration.save_provider_configuration(expected) == expected
    assert configuration.load_provider_configuration() == expected
    assert observed == ["fsync", "replace"]
    assert path.read_bytes().endswith(b"\n")
    assert staged_paths(path) == []


@pytest.mark.parametrize("failure_point", ["write", "flush", "fsync", "replace"])
def test_save_failure_preserves_old_config_and_only_removes_owned_temp(config_target, monkeypatch, failure_point):
    path, original = config_target
    old_bytes = path.read_bytes()
    legacy_temp = path.with_suffix(".tmp")
    foreign_temp = path.parent / f".{path.name}.another-save.tmp"
    legacy_temp.write_bytes(b"old staging evidence")
    foreign_temp.write_bytes(b"unrelated staging evidence")
    failure = OSError(f"injected {failure_point} failure")

    def fail(*_args, **_kwargs):
        raise failure

    if failure_point in {"write", "flush"}:
        real_temp = configuration.tempfile.NamedTemporaryFile

        class FailingStream:
            def __init__(self, stream):
                self.stream = stream
                self.name = stream.name

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, text):
                if failure_point == "write":
                    self.stream.write(text[:10])
                    raise failure
                return self.stream.write(text)

            def flush(self):
                if failure_point == "flush":
                    raise failure
                return self.stream.flush()

            def fileno(self):
                return self.stream.fileno()

        monkeypatch.setattr(configuration.tempfile, "NamedTemporaryFile", lambda **kwargs: FailingStream(real_temp(**kwargs)))
    elif failure_point == "fsync":
        monkeypatch.setattr(configuration.os, "fsync", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)

    with pytest.raises(OSError) as caught:
        configuration.save_provider_configuration(replace(original, timeout_seconds=124))
    assert caught.value is failure
    assert path.read_bytes() == old_bytes
    assert configuration.load_provider_configuration() == original
    assert legacy_temp.read_bytes() == b"old staging evidence"
    assert foreign_temp.read_bytes() == b"unrelated staging evidence"
    assert staged_paths(path) == [foreign_temp]


def test_invalid_config_does_not_create_staging_file(config_target):
    path, original = config_target
    before = path.read_bytes()
    with pytest.raises(ValueError):
        configuration.save_provider_configuration(replace(original, max_retries=99))
    assert path.read_bytes() == before
    assert staged_paths(path) == []


def test_simultaneous_saves_have_distinct_staging_files(config_target, monkeypatch):
    path, original = config_target
    choices = [replace(original, timeout_seconds=value) for value in (120, 180)]
    barrier = threading.Barrier(2, timeout=10)
    staged: set[Path] = set()
    lock = threading.Lock()
    real_replace = Path.replace

    def commit(temporary, target):
        with lock:
            staged.add(temporary)
        barrier.wait()
        return real_replace(temporary, target)

    monkeypatch.setattr(Path, "replace", commit)

    def save(choice):
        try:
            return configuration.save_provider_configuration(choice)
        except PermissionError as exc:
            # Windows can refuse simultaneous replace operations. Callers use
            # configuration_update_guard; direct writers must still fail
            # visibly without corrupting the winner or sharing staging files.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, choices))
    saved = [outcome for outcome in outcomes if isinstance(outcome, ProviderConfiguration)]
    assert saved
    assert len(staged) == 2
    assert all(item.parent == path.parent for item in staged)
    assert configuration.load_provider_configuration() in saved
    assert staged_paths(path) == []


def test_temp_creation_failure_preserves_config(config_target, monkeypatch):
    path, original = config_target
    before = path.read_bytes()

    def fail(**_kwargs):
        raise PermissionError("injected exclusive-create failure")

    monkeypatch.setattr(configuration.tempfile, "NamedTemporaryFile", fail)
    with pytest.raises(PermissionError):
        configuration.save_provider_configuration(replace(original, timeout_seconds=124))
    assert path.read_bytes() == before
    assert staged_paths(path) == []


def test_cleanup_failure_preserves_original_error_and_other_files(config_target, monkeypatch):
    path, original = config_target
    before = path.read_bytes()
    failure = OSError("injected persistence failure")
    attempted_cleanup = []

    def fail_sync(_fd):
        raise failure

    def fail_cleanup(temporary, *, missing_ok=False):
        assert missing_ok is True
        attempted_cleanup.append(temporary)
        raise PermissionError("injected cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(configuration.os, "fsync", fail_sync)
        patch.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(OSError) as caught:
            configuration.save_provider_configuration(replace(original, timeout_seconds=124))
    assert caught.value is failure
    assert caught.value.__notes__ == ["Provider temporary-file cleanup failed: PermissionError"]
    assert path.read_bytes() == before
    assert attempted_cleanup == staged_paths(path)
    assert len(attempted_cleanup) == 1
