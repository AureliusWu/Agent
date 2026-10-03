from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("rc_owned_desktop")


class FakeWin32:
    """Explicit handles and fault injection, never a real process."""

    def __init__(self, failure=None):
        self.failure = failure
        self.events = []
        self.alive = {}
        self.assigned = set()
        self.closed = []
        self.next_handle = 10

    def create_job(self):
        self.events.append("job")
        return 1

    def create_suspended(self, executable, cwd, environment, arguments=(), *, show_window=True):
        self.events.append("suspended")
        handle = self.next_handle
        self.next_handle += 2
        self.alive[handle] = True
        return 100 + handle, handle, handle + 1

    def creation_time(self, handle):
        self.events.append("identity")
        if self.failure == "identity":
            raise OSError("creation identity unavailable")
        return 1000 + handle

    def assign(self, job, handle):
        self.events.append("assign")
        if self.failure == "assign":
            raise OSError("assignment denied")
        self.assigned.add(handle)

    def is_member(self, process, job):
        self.events.append("membership")
        return process in self.assigned and self.failure != "membership"

    def process_path(self, handle):
        self.events.append("image")
        return Path("wrong.exe") if self.failure == "image" else self.executable

    def resume(self, thread):
        self.events.append("resume")
        if self.failure == "resume":
            raise OSError("resume failed")

    def wait(self, handle, milliseconds):
        return not self.alive[handle]

    def exit_code(self, handle):
        return 0 if not self.alive[handle] else 259

    def terminate_handle(self, handle):
        self.events.append("terminate_exact_handle")
        self.alive[handle] = False

    def active(self, job):
        if self.failure == "accounting":
            raise OSError("accounting unavailable")
        return sum(self.alive[handle] for handle in self.assigned)

    def terminate_job(self, job):
        self.events.append("terminate_exact_job")
        for handle in self.assigned:
            self.alive[handle] = False

    def close(self, handle):
        self.events.append("close")
        if self.failure == "thread_close" and handle == 11:
            self.failure = None
            raise OSError("first thread close failed")
        if handle == 1:
            for process in self.assigned:
                self.alive[process] = False
        self.closed.append(handle)


def fake_job(owned, tmp_path, failure=None):
    executable = tmp_path / "synthetic.exe"
    executable.write_bytes(b"not executed")
    api = FakeWin32(failure)
    api.executable = executable
    return owned.OwnedDesktopJob(api), api, executable


def test_owned_launch_is_suspended_verified_before_resume_and_retains_handles(owned, tmp_path):
    job, api, executable = fake_job(owned, tmp_path)
    process = job.launch(executable, {})
    assert api.events[:7] == ["job", "suspended", "identity", "assign", "membership", "image", "resume"]
    assert process.handle == 10 and process.creation_time == 1010
    assert 11 in api.closed and 10 not in api.closed and 1 not in api.closed
    cleanup = job.cleanup()
    assert cleanup["active_before_cleanup"] == 1
    assert cleanup["active_after_cleanup"] == 0
    assert cleanup["forced_termination"] is True
    assert cleanup["job_handle_closed"] is True
    assert cleanup["errors"] == []
    assert 10 in api.closed and 1 in api.closed


@pytest.mark.parametrize("failure", ["identity", "assign", "membership", "image", "resume", "thread_close"])
def test_launch_failure_cannot_leave_unverified_or_unassigned_process_running(owned, tmp_path, failure):
    job, api, executable = fake_job(owned, tmp_path, failure)
    with pytest.raises((OSError, ValueError)):
        job.launch(executable, {})
    cleanup = job.cleanup()
    assert not any(api.alive.values())
    assert {1, 10, 11} <= set(api.closed)
    assert cleanup["active_after_cleanup"] == 0 and cleanup["job_handle_closed"] is True
    if failure not in {"resume", "thread_close"}:
        assert "resume" not in api.events
    assert "terminate_exact_handle" in api.events or "terminate_exact_job" in api.events


def test_normal_shutdown_is_not_inferred_from_forced_job_cleanup(owned, tmp_path):
    job, api, executable = fake_job(owned, tmp_path)
    process = job.launch(executable, {})
    api.alive[process.handle] = False
    cleanup = job.cleanup()
    assert cleanup["active_before_cleanup"] == cleanup["active_after_cleanup"] == 0
    assert cleanup["forced_termination"] is False
    assert "terminate_exact_job" not in api.events


def test_cleanup_repeated_call_keeps_original_forced_outcome_and_cannot_relaunch(owned, tmp_path):
    job, api, executable = fake_job(owned, tmp_path)
    job.launch(executable, {})
    first = job.cleanup()
    events = list(api.events)
    assert job.cleanup() == first
    assert job.cleanup()["forced_termination"] is True
    assert api.events == events
    with pytest.raises(ValueError, match="closed"):
        job.launch(executable, {})


@pytest.mark.parametrize("failure", ["identity", "assign"])
def test_identity_or_assignment_failure_plus_liveness_query_failure_still_terminates_exact_handle(owned, tmp_path, failure):
    class FaultyWait(FakeWin32):
        fail_wait = True
        def wait(self, handle, milliseconds):
            if self.fail_wait:
                raise OSError("injected liveness query failure")
            return super().wait(handle, milliseconds)
    executable = tmp_path / "synthetic.exe"
    executable.write_bytes(b"not executed")
    api = FaultyWait(failure)
    api.executable = executable
    job = owned.OwnedDesktopJob(api)
    with pytest.raises(OSError):
        job.launch(executable, {})
    assert "terminate_exact_handle" in api.events and "resume" not in api.events
    first = job.cleanup()
    assert api.events.count("terminate_exact_handle") >= 2
    assert first["unassigned_cleanup_complete"] is False
    assert first["owned_process_handles_remaining"] == 1
    assert job.processes[0].handle == 10 and 10 not in api.closed
    assert first["errors"]
    api.fail_wait = False
    second = job.cleanup()
    assert second["unassigned_cleanup_complete"] is True
    assert second["owned_process_handles_remaining"] == 0 and 10 in api.closed
    assert second["active_after_cleanup"] == 0 and second["forced_termination"] is True


def test_resumed_then_thread_close_failure_is_recorded_and_failed_close_handle_retained_for_retry(owned, tmp_path):
    class FaultyClose(FakeWin32):
        fail_thread_close = True
        def close(self, handle):
            if handle == 11 and self.fail_thread_close:
                raise OSError("injected persistent thread close failure")
            return super().close(handle)
    executable = tmp_path / "synthetic.exe"
    executable.write_bytes(b"not executed")
    api = FaultyClose()
    api.executable = executable
    job = owned.OwnedDesktopJob(api)
    with pytest.raises(OSError):
        job.launch(executable, {})
    assert "resumed" in job.launch_events and job.processes[0].resumed is True
    first = job.cleanup()
    assert first["owned_thread_handles_remaining"] == 1 and job.thread_handles == [11]
    assert first["errors"] and 11 not in api.closed
    api.fail_thread_close = False
    second = job.cleanup()
    assert second["owned_thread_handles_remaining"] == 0 and job.thread_handles == []
    assert second["forced_termination"] is True and second["errors"] == []


@pytest.mark.parametrize("failure", ["membership", "identity"])
def test_unverified_sidecar_query_handle_close_failure_is_retained_without_terminating_foreign_process(owned, tmp_path, failure):
    class ForeignQuery(FakeWin32):
        fail_query_close = True
        killed = []
        def open_observed(self, pid):
            self.alive[20] = True
            return 20
        def creation_time(self, handle):
            if handle == 20 and failure == "identity":
                raise OSError("foreign creation query failed")
            return super().creation_time(handle)
        def close(self, handle):
            if handle == 20 and self.fail_query_close:
                raise OSError("foreign read-only query close failed")
            return super().close(handle)
        def terminate_handle(self, handle):
            self.killed.append(handle)
            return super().terminate_handle(handle)
    executable = tmp_path / "synthetic.exe"
    executable.write_bytes(b"not executed")
    api = ForeignQuery()
    api.executable = executable
    job = owned.OwnedDesktopJob(api)
    job.launch(executable, {})
    with pytest.raises(OSError):
        job.observe_sidecar(999, executable)
    first = job.cleanup()
    assert first["owned_process_handles_remaining"] == 1 and len(job.query_handles) == 1
    assert api.alive[20] is True and 20 not in api.killed
    api.fail_query_close = False
    second = job.cleanup()
    assert second["owned_process_handles_remaining"] == 0 and job.query_handles == []
    assert api.alive[20] is True and 20 not in api.killed


def test_unknown_native_accounting_is_retained_as_unknown_not_zero(owned, tmp_path):
    job, api, executable = fake_job(owned, tmp_path)
    job.launch(executable, {})
    api.failure = "accounting"
    cleanup = job.cleanup()
    assert cleanup["active_after_cleanup"] is None
    assert cleanup["forced_termination"] is True
    assert cleanup["errors"] and cleanup["job_handle_closed"] is True
    assert not any(api.alive.values())


def test_retained_data_is_not_removed_and_cannot_be_reused_by_another_owner(owned, tmp_path):
    output = tmp_path / "build/v1600-evidence/start.json"
    run, data, owner = owned.retained_data(tmp_path, output)
    (data / "synthetic.db").write_bytes(b"synthetic retained data")
    assert owned.retained_data(tmp_path, output, data_directory=data.relative_to(tmp_path).as_posix(), owner_run_id=owner) == (run, data, owner)
    with pytest.raises(ValueError, match="another"):
        owned.retained_data(tmp_path, output, data_directory=data.relative_to(tmp_path).as_posix(), owner_run_id=str(uuid.uuid4()))
    assert (data / "synthetic.db").read_bytes() == b"synthetic retained data"


@pytest.mark.parametrize("completion", [None, {"active_after_cleanup": None, "job_handle_closed": True},
                                         {"active_after_cleanup": 1, "job_handle_closed": True},
                                         {"active_after_cleanup": 0, "job_handle_closed": False}])
def test_nonce_rotation_requires_previous_exact_owner_cleanup_proof(owned, tmp_path, completion):
    output = tmp_path / "build/v1600-evidence/start.json"
    run, data, owner = owned.retained_data(tmp_path, output)
    nonce = str(uuid.uuid4())
    archive = owned.rotate_launch_marker(run, data, owner, nonce)
    if completion is not None:
        owned.write_once(archive / "completion.json", {"owner_run_id": owner, "acceptance_nonce": nonce, "process_cleanup": completion})
    with pytest.raises(ValueError):
        owned.rotate_launch_marker(run, data, owner, str(uuid.uuid4()))
    assert json.loads((data / "rc-acceptance-owner.json").read_text())["acceptance_nonce"] == nonce


def test_nonce_rotation_archives_completed_receipt_without_deleting_data(owned, tmp_path):
    output = tmp_path / "build/v1600-evidence/start.json"
    run, data, owner = owned.retained_data(tmp_path, output)
    nonce = str(uuid.uuid4())
    archive = owned.rotate_launch_marker(run, data, owner, nonce)
    owned.write_once(archive / "completion.json", {"status": "FAIL", "owner_run_id": owner, "acceptance_nonce": nonce,
                     "process_cleanup": {"protocol_version": "exact-native-job-v1", "active_after_cleanup": 0,
                      "job_handle_closed": True, "unassigned_cleanup_complete": True,
                      "owned_process_handles_remaining": 0, "owned_thread_handles_remaining": 0, "errors": []}})
    owned.write_once(data / "rc-desktop-observation.json", {"synthetic": True})
    (data / "synthetic.db").write_bytes(b"synthetic retained data")
    new_nonce = str(uuid.uuid4())
    owned.rotate_launch_marker(run, data, owner, new_nonce)
    assert json.loads((archive / "rc-desktop-observation.json").read_text()) == {"synthetic": True}
    assert json.loads((data / "rc-acceptance-owner.json").read_text())["acceptance_nonce"] == new_nonce
    assert (data / "synthetic.db").exists()
    assert json.loads((archive / "completion.json").read_text())["status"] == "FAIL"


@pytest.mark.parametrize("mutation", ["protocol", "errors", "unassigned", "process_handles", "thread_handles", "boolean_zero"])
def test_nonce_rotation_rejects_incomplete_or_wrong_cleanup_protocol_even_with_job_count_zero(owned, tmp_path, mutation):
    output = tmp_path / "build/v1600-evidence/start.json"
    run, data, owner = owned.retained_data(tmp_path, output)
    nonce = str(uuid.uuid4())
    archive = owned.rotate_launch_marker(run, data, owner, nonce)
    cleanup = {"protocol_version": "exact-native-job-v1", "active_after_cleanup": 0,
               "job_handle_closed": True, "unassigned_cleanup_complete": True,
               "owned_process_handles_remaining": 0, "owned_thread_handles_remaining": 0, "errors": []}
    if mutation == "protocol": cleanup["protocol_version"] = "old-unverified"
    elif mutation == "errors": cleanup["errors"] = ["unresolved native failure"]
    elif mutation == "unassigned": cleanup["unassigned_cleanup_complete"] = False
    elif mutation == "process_handles": cleanup["owned_process_handles_remaining"] = 1
    elif mutation == "thread_handles": cleanup["owned_thread_handles_remaining"] = 1
    elif mutation == "boolean_zero": cleanup["active_after_cleanup"] = False
    owned.write_once(archive / "completion.json", {"owner_run_id": owner, "acceptance_nonce": nonce, "process_cleanup": cleanup})
    with pytest.raises(ValueError, match="not proven stopped"):
        owned.rotate_launch_marker(run, data, owner, str(uuid.uuid4()))
    assert (data / "rc-acceptance-owner.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows exact-Job probe")
def test_actual_windows_native_job_contains_and_stops_only_synthetic_parent_child(owned, tmp_path):
    """No product app, model, service, microphone, credential or installation."""
    executable = Path(sys._base_executable).resolve()
    marker = tmp_path / "synthetic-child.json"
    code = (
        "import json,os,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-I','-S','-B','-c','import time;time.sleep(120)'],"
        "creationflags=subprocess.CREATE_NO_WINDOW);"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':child.pid}),encoding='utf-8');"
        "time.sleep(120)"
    )
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}}
    job = owned.OwnedDesktopJob()
    try:
        parent = job.launch(executable, environment, ("-I", "-S", "-B", "-c", code, str(marker)), show_window=False)
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert parent.poll() is None
            assert time.monotonic() < deadline, "pure stdlib native probe did not create its synthetic child"
            time.sleep(.05)
        child = job.observe_sidecar(json.loads(marker.read_text(encoding="utf-8"))["pid"], executable)
        assert parent.creation_time <= child.creation_time and parent.poll() is None and child.poll() is None
        assert job.api.active(job.handle) >= 2
    finally:
        cleanup = job.cleanup()
    assert cleanup["active_after_cleanup"] == 0
    assert cleanup["job_handle_closed"] is True and cleanup["errors"] == []
    assert cleanup["forced_termination"] is True
    assert marker.exists(), "owned synthetic evidence must remain after cleanup"


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows exact-Job probe")
def test_actual_windows_native_job_proves_graceful_synthetic_exit_without_forcing(owned, tmp_path):
    executable = Path(sys._base_executable).resolve()
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}}
    job = owned.OwnedDesktopJob()
    try:
        process = job.launch(executable, environment, ("-I", "-S", "-B", "-c", "pass"), show_window=False)
        assert process.wait(15) == 0
        assert job.wait_empty(15) == 0
    finally:
        cleanup = job.cleanup()
    assert cleanup["active_before_cleanup"] == cleanup["active_after_cleanup"] == 0
    assert cleanup["forced_termination"] is False
    assert cleanup["job_handle_closed"] is True and cleanup["errors"] == []


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows exact-Job probe")
def test_actual_windows_native_job_close_fallback_stops_synthetic_tree_without_inventing_accounting(owned, tmp_path):
    executable = Path(sys._base_executable).resolve()
    marker = tmp_path / "synthetic-child.json"
    code = (
        "import json,pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-I','-S','-B','-c','import time;time.sleep(120)'],"
        "creationflags=subprocess.CREATE_NO_WINDOW);"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':child.pid}),encoding='utf-8');"
        "time.sleep(120)"
    )
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}}
    job = owned.OwnedDesktopJob()
    observations = []
    try:
        parent = job.launch(executable, environment, ("-I", "-S", "-B", "-c", code, str(marker)), show_window=False)
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert parent.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        child = job.observe_sidecar(json.loads(marker.read_text(encoding="utf-8"))["pid"], executable)
        for process in (parent, child):
            observer = owned.RetainedProcess(job.api, job.api.open_observed(process.pid), process.pid, executable)
            observer.capture_identity()
            assert observer.creation_time == process.creation_time
            observations.append(observer)
        def unavailable_accounting(_job):
            raise OSError("injected native accounting failure")
        job.api.active = unavailable_accounting
        # Force the explicit termination API to fail as well. Only the real
        # KILL_ON_JOB_CLOSE fallback can now stop the actual synthetic tree.
        def unavailable_termination(_job):
            raise OSError("injected explicit Job termination failure")
        job.api.terminate_job = unavailable_termination
        cleanup = job.cleanup()
        assert cleanup["job_handle_closed"] is True
        assert cleanup["active_after_cleanup"] is None and cleanup["errors"]
        for observer in observations:
            assert isinstance(observer.wait(15), int), "retained read-only handles must observe actual native exit"
    finally:
        job.cleanup()
        for observer in observations:
            observer.close()
