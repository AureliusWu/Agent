"""Native read integration; inert owned files only, no installer or app launch."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import rc_windows_shortcuts as reader
    import rc_desktop_candidate as candidate
    return reader, candidate.module("rc_installed_lifecycle"), candidate


def adapter_for(lifecycle, tmp_path, original, reader, native):
    adapter = object.__new__(lifecycle.WindowsAdapter)
    adapter.root, adapter.probes = ROOT, tmp_path / "private-probes"
    adapter.probes.mkdir()
    adapter.host_ps = lambda probe: original
    fake = SimpleNamespace(ShortcutReadError=reader.ShortcutReadError, need=reader.need,
        read_shortcut=lambda path, sha: native, combine_wsh=reader.combine_wsh)
    return adapter, fake


def test_native_observe_preserves_wsh_and_original_exact_target_gate(modules, monkeypatch, tmp_path):
    reader, lifecycle, _ = modules
    install = tmp_path / "install"
    native = {"protocol_version": reader.PROTOCOL, "target": str(install / "司忆.exe"), "arguments": "",
              "identity_stable": True, "deny_write_delete_retained": True}
    raw = {"shortcuts": [{"path": str(tmp_path / "owned.lnk"), "target": "", "arguments": "", "sha256": "a" * 64}]}
    adapter, fake = adapter_for(lifecycle, tmp_path, raw, reader, native)
    monkeypatch.setattr(lifecycle, "load_module", lambda root, name: fake)
    fixture = SimpleNamespace(install=install, validate=lambda: None)
    observed = adapter.observe(fixture)
    assert raw["shortcuts"][0]["target"] == "" and adapter._native_shortcut_original_host is raw
    row = observed["shortcuts"][0]
    assert set(row) == {"path", "target", "arguments", "sha256"} and row["target"] == native["target"]
    package = SimpleNamespace(kind="msi", product_code="{00000000-0000-0000-0000-000000000000}")
    assert lifecycle.validate_new_links([row], package, fixture) == [row]
    private = list(adapter.probes.glob("private-native-shortcut-*.json"))
    assert len(private) == 1
    saved = json.loads(private[0].read_text(encoding="utf-8"))
    assert saved["wsh_target"] == "" and saved["native_read"] == native
    foreign = dict(row, target=str(tmp_path / "foreign" / "司忆.exe"))
    with pytest.raises(lifecycle.SafetyError, match="new shortcut does not target the exact owned package"):
        lifecycle.validate_new_links([foreign], package, fixture)


def test_wsh_empty_to_same_nonempty_cache_does_not_change_original_four_column_snapshot(modules, monkeypatch, tmp_path):
    reader, lifecycle, _ = modules
    native = {"protocol_version": reader.PROTOCOL, "target": str(tmp_path / "install" / "司忆.exe"), "arguments": "",
              "identity_stable": True, "deny_write_delete_retained": True}
    raw = {"shortcuts": [{"path": str(tmp_path / "owned.lnk"), "target": "", "arguments": "", "sha256": "a" * 64}]}
    adapter, fake = adapter_for(lifecycle, tmp_path, raw, reader, native)
    monkeypatch.setattr(lifecycle, "load_module", lambda root, name: fake)
    fixture = SimpleNamespace(validate=lambda: None)
    first = adapter.observe(fixture)
    raw["shortcuts"][0]["target"] = native["target"]
    second = adapter.observe(fixture)
    assert first["shortcuts"] == second["shortcuts"]
    assert set(second["shortcuts"][0]) == {"path", "target", "arguments", "sha256"}
    assert len(list(adapter.probes.glob("private-native-shortcut-*.json"))) == 2


@pytest.mark.parametrize("field", ["target", "arguments", "sha256"])
def test_native_target_arguments_and_link_bytes_drift_still_fail_original_snapshot_guard(modules, field):
    _, lifecycle, _ = modules
    row = {"path": "owned.lnk", "target": "owned.exe", "arguments": "", "sha256": "a" * 64}
    changed = dict(row, **{field: "different"})
    host = {"shortcuts": [changed], "installations": [], "related_products": [], "install_paths": [],
            "running": [], "webviews": ["130.0.0.0"], "is_admin": True, "installer_busy": False}
    with pytest.raises(lifecycle.SafetyError, match="shortcut bytes/targets changed since the owner snapshot"):
        lifecycle.validate_host(host, None, object(), [row])


def test_native_observe_rethrows_same_failure_and_records_only_fixed_diagnostic(modules, monkeypatch, tmp_path):
    reader, lifecycle, candidate = modules
    raw = {"shortcuts": [{"path": str(tmp_path / "owned.lnk"), "target": "", "arguments": "", "sha256": "a" * 64}]}
    adapter, fake = adapter_for(lifecycle, tmp_path, raw, reader, {})
    error = reader.ShortcutReadError("TARGET_UNKNOWN")
    error.diagnostics = {"raw_status": 1, "pidl_status": 1, "msi_status": 1627, "private_path": "never publish"}
    def fail(*args): raise error
    fake.read_shortcut = fail
    monkeypatch.setattr(lifecycle, "load_module", lambda root, name: fake)
    with pytest.raises(reader.ShortcutReadError) as caught:
        adapter.observe(SimpleNamespace(validate=lambda: None))
    assert caught.value is error and adapter._native_shortcut_original_host is raw
    public = candidate.failure_projection("msi", "installed_lifecycle", error,
        {"native_shortcuts": adapter._native_shortcut_diagnostic})["native_shortcuts"]
    assert public["status"] == "BLOCKED" and public["guard_code"] == "TARGET_UNKNOWN" and public["completed_count"] == 0
    assert public["raw_status"] == public["pidl_status"] == 1 and public["msi_status"] == 1627
    assert "private" not in json.dumps(public)


def test_native_diagnostics_reject_forged_paths_urls_codes_and_wrong_scalar_types(modules, tmp_path):
    _, _, candidate = modules
    secret = "https://example.invalid/" + str(tmp_path / "private") + "?token=private"
    projected = candidate.failure_projection("msi", "installed_lifecycle", ValueError(secret), {"native_shortcuts": {
        "status": secret, "guard_code": secret, "raw_status": secret, "msi_status": True,
        "pidl_present": secret, "cleanup_completed": 1, "observed_count": secret, "completed_count": -1,
        "target": secret, "arguments": secret}})["native_shortcuts"]
    assert projected["status"] == projected["guard_code"] == "UNKNOWN"
    assert all(projected[key] is None for key in ("raw_status", "msi_status", "pidl_present", "cleanup_completed", "observed_count", "completed_count"))
    assert secret not in json.dumps(projected) and "target" not in projected and "arguments" not in projected


@pytest.mark.parametrize("failed_action", ["pidl", "persist", "link", "uninitialize"])
def test_all_com_cleanup_is_attempted_after_each_independent_error(modules, failed_action):
    reader, _, _ = modules
    native, calls = object.__new__(reader._Native), []
    def action(label):
        calls.append(label)
        if label == failed_action: raise OSError("private cleanup failure")
    native.ole = SimpleNamespace(CoTaskMemFree=lambda ptr: action("pidl"), CoUninitialize=lambda: action("uninitialize"))
    native.method = lambda ptr, slot, result: lambda same: action("persist" if ptr.value == 2 else "link")
    assert native.cleanup(ctypes.c_void_p(1), ctypes.c_void_p(2), ctypes.c_void_p(3), True) is False
    assert calls == ["pidl", "persist", "link", "uninitialize"]


def test_primary_com_exception_identity_survives_cleanup_failure(modules):
    reader, _, _ = modules
    native = object.__new__(reader._Native)
    def create(cls, outer, context, iid, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 1
        return 0
    native.ole = SimpleNamespace(CoInitializeEx=lambda *args: 0, CoCreateInstance=create)
    original = reader.ShortcutReadError("COM_LOAD_FAILED")
    def method(pointer, slot, result, *arguments):
        if slot == 0:
            def query(this, iid, output):
                ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 2
                return 0
            return query
        def load(*args): raise original
        return load
    native.method = method
    native.cleanup = lambda *args: False
    with pytest.raises(reader.ShortcutReadError) as caught:
        native.observe(Path("owned.lnk"))
    assert caught.value is original and original.diagnostics["cleanup_completed"] is False


def create_inert_link(reader, tmp_path):
    target, link_file = tmp_path / "司忆.exe", tmp_path / "司忆.lnk"
    target.write_bytes(b"INERT OWNED TEST; NEVER EXECUTED\n")
    native = reader._Native()
    link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    initialized = False
    try:
        assert native.ole.CoInitializeEx(None, 2) in {0, 1}
        initialized = True
        cls = reader._Guid.parse("00021401-0000-0000-c000-000000000046")
        iid = reader._Guid.parse("000214f9-0000-0000-c000-000000000046")
        assert native.ole.CoCreateInstance(ctypes.byref(cls), None, 1, ctypes.byref(iid), ctypes.byref(link)) == 0
        assert native.method(link, 20, ctypes.c_int32, ctypes.c_wchar_p)(link, str(target)) == 0
        pi = reader._Guid.parse("0000010b-0000-0000-c000-000000000046")
        assert native.method(link, 0, ctypes.c_int32, ctypes.POINTER(reader._Guid), ctypes.POINTER(ctypes.c_void_p))(link, ctypes.byref(pi), ctypes.byref(persist)) == 0
        assert native.method(persist, 6, ctypes.c_int32, ctypes.c_wchar_p, ctypes.c_int)(persist, str(link_file), 1) == 0
    finally:
        assert native.cleanup(ctypes.c_void_p(), persist, link, initialized)
    return target, link_file


@pytest.mark.skipif(os.name != "nt", reason="Actual read-only Windows COM fixture; no installer/app execution")
def test_real_native_unicode_link_keeps_read_handle_and_denies_write_and_delete(modules, tmp_path, monkeypatch):
    reader, _, _ = modules
    target, path = create_inert_link(reader, tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original = reader._Native.observe
    checked = []
    def observing(native, same_path):
        with pytest.raises(PermissionError):
            same_path.open("wb")
        with pytest.raises(PermissionError):
            same_path.rename(tmp_path / "must-not-move.lnk")
        checked.append(True)
        return original(native, same_path)
    monkeypatch.setattr(reader._Native, "observe", observing)
    result = reader.read_shortcut(path, before)
    assert checked == [True] and result["target"] == str(target) and result["arguments"] == ""
    assert result["native"]["raw_status"] == result["native"]["pidl_status"] == 0
    assert result["native"]["pidl_filesystem"] is True and result["native"]["msi_status"] == 1627
    assert result["identity_stable"] is result["deny_write_delete_retained"] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
