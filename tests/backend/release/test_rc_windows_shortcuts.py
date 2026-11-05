"""Pure protocol/retained-file tests: no COM, install, target execution or repair."""
import copy
import hashlib
from pathlib import Path
import struct
import uuid

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
TARGET = "C:/repo/owned/司忆.exe"


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import rc_windows_shortcuts
    return rc_windows_shortcuts


def header(flags=0):
    data = bytearray(76)
    data[:4] = b"L\0\0\0"
    data[4:20] = uuid.UUID("00021401-0000-0000-c000-000000000046").bytes_le
    struct.pack_into("<I", data, 20, flags)
    return bytes(data)


def observation(**changes):
    value = {"raw_target": TARGET, "raw_status": 0, "arguments": "", "arguments_status": 0,
             "pidl_target": None, "pidl_status": 1, "pidl_present": False, "pidl_filesystem": None,
             "msi_status": 1627, "descriptor_fields_empty": True, "darwin_bytes_absent": True,
             "product_code": "", "feature_id": "", "component_code": ""}
    return {**value, **changes}


def rejected(reader, function, code, *arguments):
    with pytest.raises(reader.ShortcutReadError) as captured:
        function(*arguments)
    assert captured.value.code == code
    assert str(captured.value) == code


@pytest.mark.parametrize("flags", [0, 1, 2, 3, 0x80, 0x4000000])
def test_locked_header_extracts_bounded_facts_without_resolving(reader, flags):
    assert reader.link_header(header(flags)) == {
        "has_idlist": bool(flags & 1), "has_link_info": bool(flags & 2), "darwin_bytes_absent": True}


@pytest.mark.parametrize("data", [None, "L", bytearray(header()), b"", header()[:75], b"X" + header()[1:],
    header()[:4] + b"x" * 16 + header()[20:], header()[:66] + b"\1" + header()[67:],
    header()[:75] + b"\1", header() + b"x" * (1024 * 1024 - 75)],
    ids=["none", "text", "mutable", "empty", "short", "signature", "clsid", "reserved", "last-reserved", "over-bound"])
def test_invalid_or_oversize_link_header_is_rejected(reader, data):
    rejected(reader, reader.link_header, "LNK_HEADER_INVALID", data)


@pytest.mark.parametrize("bit", [27, 28, 29, 30, 31])
def test_unknown_high_link_flag_is_not_accepted(reader, bit):
    rejected(reader, reader.link_header, "LNK_FLAGS_UNKNOWN", header(1 << bit))


@pytest.mark.parametrize("data", [header(0x1000), header() + b"\x06\0\0\xa0",
    header() + b"junk\x06\0\0\xa0junk"])
def test_darwin_flag_or_signature_cannot_hide_behind_msi_api_error(reader, data):
    rejected(reader, reader.link_header, "DARWIN_BYTES_PRESENT", data)


def test_raw_path_and_arguments_are_actual_observations_not_expected_path_substitution(reader):
    assert reader.selected_identity(observation(arguments="/x {fixture-guid}")) == (TARGET, "/x {fixture-guid}")
    # The reader does not decide package ownership or rewrite arguments.
    actual = "C:/repo/another-owned-file.exe"
    assert reader.selected_identity(observation(raw_target=actual))[0] == actual


def test_empty_raw_path_can_use_consistent_actual_filesystem_pidl(reader):
    value = observation(raw_target="", raw_status=1, pidl_present=True, pidl_status=0,
                        pidl_filesystem=True, pidl_target=TARGET)
    assert reader.selected_identity(value) == (TARGET, "")


def test_matching_raw_and_pidl_paths_preserve_raw_spelling(reader):
    value = observation(pidl_present=True, pidl_status=0, pidl_filesystem=True,
                        pidl_target=TARGET.upper().replace("/", "\\"))
    assert reader.selected_identity(value) == (TARGET, "")


@pytest.mark.parametrize(("changes", "code"), [
    ({"raw_target": "", "raw_status": 0}, "COM_PATH_FAILED"),
    ({"raw_target": TARGET, "raw_status": 1}, "COM_PATH_FAILED"),
    ({"raw_status": -1}, "COM_PATH_FAILED"), ({"raw_status": 2}, "COM_PATH_FAILED"),
    ({"raw_status": True}, "COM_PATH_FAILED"), ({"raw_status": "0"}, "COM_PATH_FAILED"),
    ({"arguments_status": 1}, "COM_ARGUMENTS_FAILED"),
    ({"arguments_status": -1}, "COM_ARGUMENTS_FAILED"),
    ({"arguments_status": False}, "COM_ARGUMENTS_FAILED"),
    ({"pidl_present": True, "pidl_status": 1}, "COM_IDLIST_FAILED"),
    ({"pidl_status": 0}, "COM_IDLIST_FAILED"), ({"pidl_status": -1}, "COM_IDLIST_FAILED"),
    ({"pidl_status": True}, "COM_IDLIST_FAILED"),
    ({"pidl_present": 1}, "NATIVE_OBSERVATION_INVALID"),
    ({"pidl_target": TARGET}, "NATIVE_OBSERVATION_INVALID"),
    ({"pidl_filesystem": False}, "NATIVE_OBSERVATION_INVALID"),
    ({"msi_status": 0}, "MSI_DESCRIPTOR_PRESENT"),
    ({"msi_status": 1}, "MSI_STATUS_UNKNOWN"), ({"msi_status": 1605}, "MSI_STATUS_UNKNOWN"),
    ({"msi_status": True}, "NATIVE_OBSERVATION_INVALID"),
    ({"descriptor_fields_empty": False}, "MSI_STATUS_UNKNOWN"),
    ({"descriptor_fields_empty": 1}, "MSI_STATUS_UNKNOWN"),
    ({"darwin_bytes_absent": False}, "DARWIN_BYTES_PRESENT"),
    ({"darwin_bytes_absent": 1}, "DARWIN_BYTES_PRESENT"),
    ({"raw_target": None}, "NATIVE_OBSERVATION_INVALID"),
    ({"raw_target": "x\0y"}, "NATIVE_OBSERVATION_INVALID"),
    ({"arguments": None}, "NATIVE_OBSERVATION_INVALID"),
    ({"arguments": "x\0y"}, "NATIVE_OBSERVATION_INVALID"),
    ({"raw_target": "x" * 32767}, "NATIVE_OBSERVATION_INVALID"),
    ({"arguments": "x" * 32767}, "NATIVE_OBSERVATION_INVALID"),
])
def test_native_inconsistency_and_status_unknown_are_fail_closed(reader, changes, code):
    rejected(reader, reader.selected_identity, code, observation(**changes))


@pytest.mark.parametrize("value", [None, [], "", 0])
def test_native_identity_requires_object(reader, value):
    rejected(reader, reader.selected_identity, "NATIVE_OBSERVATION_INVALID", value)


@pytest.mark.parametrize("target", [None, "", "x\0y", "x" * 32767], ids=["none", "empty", "nul", "over-bound"])
def test_present_pidl_requires_bounded_nonempty_filesystem_target(reader, target):
    value = observation(raw_target="", raw_status=1, pidl_present=True, pidl_status=0,
                        pidl_filesystem=True, pidl_target=target)
    rejected(reader, reader.selected_identity, "PIDL_NOT_FILESYSTEM", value)


@pytest.mark.parametrize("filesystem", [None, False, 1, "yes"])
def test_nonfilesystem_pidl_is_never_replaced_with_the_expected_exe(reader, filesystem):
    rejected(reader, reader.selected_identity, "PIDL_NOT_FILESYSTEM",
        observation(pidl_present=True, pidl_status=0, pidl_filesystem=filesystem, pidl_target=TARGET))


def test_conflicting_actual_raw_and_pidl_paths_rejected(reader):
    rejected(reader, reader.selected_identity, "PATH_CONFLICT", observation(
        pidl_present=True, pidl_status=0, pidl_filesystem=True, pidl_target="C:/repo/other.exe"))


@pytest.mark.parametrize("target", ["", "relative.exe", "C:relative.exe", "/repo/a.exe", "\\repo\\a.exe",
    "//server/share/a.exe", "\\\\server\\share\\a.exe", "\\\\?\\" + TARGET[:2] + "\\repo\\a.exe",
    "C:/repo/../a.exe", TARGET[:2] + "\\repo\\..\\a.exe"])
def test_only_explicit_local_absolute_nontraversing_target_is_allowed(reader, target):
    value = observation(raw_target=target, raw_status=0 if target else 1)
    rejected(reader, reader.selected_identity, "TARGET_UNKNOWN", value)


@pytest.mark.parametrize("bad", [None, "path/token/private-value", RuntimeError("private-value"), 17])
def test_error_messages_are_only_fixed_codes(reader, bad):
    error = reader.ShortcutReadError(bad)
    assert error.code == "NATIVE_OBSERVATION_INVALID"
    assert str(error) == "NATIVE_OBSERVATION_INVALID"


def native_result(reader, **changes):
    return {"protocol_version": reader.PROTOCOL, "identity_stable": True, "deny_write_delete_retained": True,
            "target": TARGET, "arguments": "", **changes}


def test_wsh_empty_values_are_retained_alongside_native_observation(reader):
    row = {"path": "own-link.lnk", "target": "", "arguments": "", "sha256": "a" * 64}
    before = copy.deepcopy(row)
    native = native_result(reader, arguments="/x {fixture-guid}")
    result = reader.combine_wsh(row, native)
    assert row == before
    assert result["wsh_target"] == result["wsh_arguments"] == ""
    assert result["target"] == TARGET and result["arguments"] == "/x {fixture-guid}"
    assert result["native_read"] is native
    assert result["path"] == row["path"] and result["sha256"] == row["sha256"]


def test_wsh_consistent_nonempty_target_is_compared_but_not_lost(reader):
    raw = TARGET.upper().replace("/", "\\")
    result = reader.combine_wsh({"target": raw, "arguments": ""}, native_result(reader))
    assert result["wsh_target"] == raw and result["target"] == TARGET


@pytest.mark.parametrize(("row", "native", "code"), [
    ({"target": "C:/repo/other.exe", "arguments": ""}, {}, "WSH_CONFLICT"),
    ({"target": TARGET, "arguments": "unexpected"}, {}, "WSH_CONFLICT"),
    ({"target": '"' + TARGET + '"', "arguments": ""}, {}, "WSH_CONFLICT"),
    ({"target": None, "arguments": ""}, {}, "NATIVE_OBSERVATION_INVALID"),
    ({"target": "", "arguments": None}, {}, "NATIVE_OBSERVATION_INVALID"),
    ({"target": "", "arguments": ""}, {"protocol_version": "invented"}, "NATIVE_OBSERVATION_INVALID"),
    ({"target": "", "arguments": ""}, {"identity_stable": False}, "NATIVE_OBSERVATION_INVALID"),
    ({"target": "", "arguments": ""}, {"deny_write_delete_retained": False}, "NATIVE_OBSERVATION_INVALID"),
    ({"target": "", "arguments": ""}, {"target": ""}, "NATIVE_OBSERVATION_INVALID"),
])
def test_native_fallback_does_not_erase_conflicting_wsh_or_accept_unproven_identity(reader, row, native, code):
    rejected(reader, reader.combine_wsh, code, row, native_result(reader, **native))


class FakeNative:
    """Deterministic retained handle API, never loads any native library."""
    def __init__(self, data, observed=None):
        self.data = data
        self.observed = observation() if observed is None else observed
        self.trace = []
        self.handles = []
        self.identity = {"volume": 3, "file_index": 4, "bytes": len(data), "links": 1,
                         "attributes": 32, "created": 5, "written": 6}
        self.info_override = None
        self.contents_override = None

    def open(self, path):
        handle = "handle-" + str(len(self.handles) + 1)
        self.handles.append(handle)
        self.trace.append(("open", handle))
        return handle

    def close(self, handle):
        self.trace.append(("close", handle))

    def info(self, handle):
        self.trace.append(("info", handle))
        return dict(self.identity) if self.info_override is None else self.info_override(handle)

    def contents(self, handle, size):
        self.trace.append(("contents", handle, size))
        return self.data if self.contents_override is None else self.contents_override(handle)

    def observe(self, path):
        self.trace.append(("observe",))
        return copy.deepcopy(self.observed)


def retained(reader, monkeypatch, tmp_path, *, data=None, observed=None):
    data = header() if data is None else data
    path = tmp_path / "own-link.lnk"
    path.write_bytes(data)
    fake = FakeNative(data, observed)
    monkeypatch.setattr(reader, "_Native", lambda: fake)
    return path, hashlib.sha256(data).hexdigest(), fake


def test_retained_same_file_hash_is_checked_before_and_after_native_observation(reader, monkeypatch, tmp_path):
    path, digest, fake = retained(reader, monkeypatch, tmp_path)
    result = reader.read_shortcut(path, digest)
    assert result["target"] == TARGET and result["sha256"] == digest
    assert result["identity_stable"] is result["deny_write_delete_retained"] is True
    assert fake.trace == [("open", "handle-1"), ("info", "handle-1"), ("contents", "handle-1", 76), ("observe",),
        ("open", "handle-2"), ("info", "handle-2"), ("close", "handle-2"),
        ("info", "handle-1"), ("contents", "handle-1", 76), ("close", "handle-1")]


def test_bad_expected_hash_rejects_before_native_target_read_and_closes_handle(reader, monkeypatch, tmp_path):
    path, _, fake = retained(reader, monkeypatch, tmp_path)
    rejected(reader, reader.read_shortcut, "FILE_CHANGED", path, "0" * 64)
    assert ("observe",) not in fake.trace
    assert fake.trace[-1] == ("close", "handle-1")


@pytest.mark.parametrize("field", ["volume", "file_index", "bytes", "links", "attributes", "created", "written"])
def test_reopened_identity_drift_is_not_hidden_by_same_byte_hash(reader, monkeypatch, tmp_path, field):
    path, digest, fake = retained(reader, monkeypatch, tmp_path)
    fake.info_override = lambda handle: {**fake.identity, field: fake.identity[field] + (handle == "handle-2")}
    rejected(reader, reader.read_shortcut, "FILE_CHANGED", path, digest)
    assert fake.trace[-2:] == [("close", "handle-2"), ("close", "handle-1")]


def test_original_retained_handle_identity_drift_also_rejected(reader, monkeypatch, tmp_path):
    path, digest, fake = retained(reader, monkeypatch, tmp_path)
    calls = 0
    def info(handle):
        nonlocal calls
        calls += 1
        return {**fake.identity, "written": fake.identity["written"] + (calls == 3)}
    fake.info_override = info
    rejected(reader, reader.read_shortcut, "FILE_CHANGED", path, digest)
    assert fake.trace[-1] == ("close", "handle-1")


def test_retained_link_bytes_cannot_change_after_native_read(reader, monkeypatch, tmp_path):
    path, digest, fake = retained(reader, monkeypatch, tmp_path)
    calls = 0
    def contents(handle):
        nonlocal calls
        calls += 1
        return fake.data if calls == 1 else fake.data + b"different"
    fake.contents_override = contents
    rejected(reader, reader.read_shortcut, "FILE_CHANGED", path, digest)
    assert fake.trace[-1] == ("close", "handle-1")


def test_locked_bytes_darwin_rejection_precedes_com_and_msi(reader, monkeypatch, tmp_path):
    path, digest, fake = retained(reader, monkeypatch, tmp_path, data=header() + b"\x06\0\0\xa0")
    rejected(reader, reader.read_shortcut, "DARWIN_BYTES_PRESENT", path, digest)
    assert ("observe",) not in fake.trace and fake.trace[-1] == ("close", "handle-1")


def test_native_failure_preserves_exception_and_closes_retained_file(reader, monkeypatch, tmp_path):
    path, digest, fake = retained(reader, monkeypatch, tmp_path)
    failure = reader.ShortcutReadError("COM_LOAD_FAILED")
    def fail(path):
        raise failure
    fake.observe = fail
    with pytest.raises(reader.ShortcutReadError) as caught:
        reader.read_shortcut(path, digest)
    assert caught.value is failure
    assert fake.trace[-1] == ("close", "handle-1")


@pytest.mark.parametrize(("path", "digest"), [("own-link.lnk", "a" * 64), (Path("relative.lnk"), "a" * 64),
    (Path("relative.exe"), "a" * 64), (None, "a" * 64)])
def test_invalid_input_never_opens_native_api(reader, monkeypatch, path, digest):
    monkeypatch.setattr(reader, "_Native", lambda: pytest.fail("invalid input reached native API"))
    rejected(reader, reader.read_shortcut, "FILE_INPUT_INVALID", path, digest)


@pytest.mark.parametrize("digest", [None, "", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
def test_hash_must_be_exact_lowercase_sha256_before_open(reader, monkeypatch, tmp_path, digest):
    monkeypatch.setattr(reader, "_Native", lambda: pytest.fail("invalid digest reached native API"))
    rejected(reader, reader.read_shortcut, "FILE_INPUT_INVALID", tmp_path / "own-link.lnk", digest)


def test_reparse_or_ordinary_path_guard_failure_stops_before_native_open(reader, monkeypatch, tmp_path):
    monkeypatch.setattr(reader, "ordinary", lambda path: (_ for _ in ()).throw(ValueError("private-path")))
    monkeypatch.setattr(reader, "_Native", lambda: pytest.fail("unsafe path reached native API"))
    rejected(reader, reader.read_shortcut, "FILE_UNSAFE", tmp_path / "own-link.lnk", "a" * 64)
