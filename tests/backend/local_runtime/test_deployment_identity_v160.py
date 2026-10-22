"""Synthetic OS/metadata fixtures, not live Ollama qualification evidence."""
import copy
import socket
import struct
import os

import pytest

import app.local_runtime.deployment_identity as deployment

URL = "http://127.0.0.1:11434"
SERVICE = {"pid": 123, "creation": "win:456"}
RESIDENT = {"digest": "a" * 64, "context_length": 16384, "size": 4096, "size_vram": 4096}


@pytest.fixture
def instance(monkeypatch):
    monkeypatch.setattr(deployment, "service_instance", lambda _: dict(SERVICE))


def test_observation_binds_service_and_allocation_without_paths_or_environment(instance):
    first = deployment.observe_deployment(URL, "0.17.0", RESIDENT, SERVICE)
    assert first is not None
    assert deployment.current_deployment_hash(URL, first) == first["hash"]
    assert set(first) == {"version", "service", "server_version", "resident", "hash"}
    for field, value in [("size_vram", 0), ("context_length", 8192), ("digest", "b" * 64), ("size", 8192)]:
        other = deployment.observe_deployment(URL, "0.17.0", {**RESIDENT, field: value}, SERVICE)
        assert other is not None and other["hash"] != first["hash"]
    assert deployment.observe_deployment(URL, "0.18.0", RESIDENT, SERVICE)["hash"] != first["hash"]


def test_restart_or_unknown_instance_never_reuses_old_qualification(instance, monkeypatch):
    observation = deployment.observe_deployment(URL, "0.17.0", RESIDENT, SERVICE)
    for current in (None, {**SERVICE, "pid": 124}, {**SERVICE, "creation": "win:457"}):
        monkeypatch.setattr(deployment, "service_instance", lambda _, current=current: current)
        assert deployment.current_deployment_hash(URL, observation) is None
        assert deployment.observe_deployment(URL, "0.17.0", RESIDENT, SERVICE) is None


@pytest.mark.parametrize("value", [None, {}, [], "self-asserted-hash", True])
def test_unknown_or_caller_hash_is_not_an_observation(instance, value):
    assert deployment.sanitize_deployment(value) is None
    assert deployment.current_deployment_hash(URL, value) is None


@pytest.mark.parametrize("field,value", [("size", True), ("size", 0), ("size", 2**51),
                                      ("size_vram", -1), ("size_vram", False),
                                      ("context_length", 0), ("context_length", "16384"),
                                      ("digest", "unverified")])
def test_malformed_resident_metadata_cannot_qualify(instance, field, value):
    assert deployment.observe_deployment(URL, "0.17.0", {**RESIDENT, field: value}, SERVICE) is None


def test_deployment_hash_is_recomputed_and_extra_fields_are_rejected(instance):
    observation = deployment.observe_deployment(URL, "0.17.0", RESIDENT, SERVICE)
    for changed in ({**observation, "hash": "b" * 64}, {**observation, "environment": "secret"}):
        assert deployment.sanitize_deployment(changed) is None
    changed = copy.deepcopy(observation)
    changed["resident"]["size_vram"] = 0
    assert deployment.sanitize_deployment(changed) is None


def test_ipv4_and_ipv6_native_layout_and_network_port():
    port = socket.htons(11434)
    ipv4 = struct.pack("<I6I", 1, 2, int.from_bytes(b"\x7f\0\0\x01", "little"), port, 0, 0, 123)
    assert deployment._decode_tcp_rows(ipv4, socket.AF_INET) == [(b"\x7f\0\0\x01", 11434, 123)]
    ipv6 = struct.pack("<I16sII16sIIII", 1, b"\0" * 15 + b"\x01", 0, port, b"\0" * 16, 0, 0, 2, 124)
    assert deployment._decode_tcp_rows(ipv6, socket.AF_INET6) == [(b"\0" * 15 + b"\x01", 11434, 124)]
    assert deployment._decode_tcp_rows(struct.pack("<I", 0), socket.AF_INET) == []
    for raw in (b"", b"123", struct.pack("<I", 2) + ipv4[4:]):
        with pytest.raises(ValueError):
            deployment._decode_tcp_rows(raw, socket.AF_INET)


def test_windows_listener_is_exact_and_unambiguous(monkeypatch):
    # Patch only the observer's platform discriminator, not global os.name.
    if deployment.os.name != "nt":
        pytest.skip("Windows implementation")
    monkeypatch.setattr(deployment, "_process_identity", lambda _: "win:456")
    monkeypatch.setattr(deployment, "_tcp_rows", lambda _: [(b"\x7f\0\0\x01", 11434, 123)])
    assert deployment.service_instance(URL) == SERVICE
    for url in ("https://127.0.0.1:11434", "http://127.0.0.1:1234", "http://user:key@127.0.0.1:11434", "http://remote.test:11434"):
        assert deployment.service_instance(url) is None
    monkeypatch.setattr(deployment, "_tcp_rows", lambda _: [(b"\x7f\0\0\x01", 11434, 123), (b"\x7f\0\0\x01", 11434, 124)])
    assert deployment.service_instance(URL) is None
    monkeypatch.setattr(deployment, "_tcp_rows", lambda _: [(b"\0" * 4, 11434, 123)])
    assert deployment.service_instance(URL) is None


def test_ipv6_wildcard_cannot_be_misattributed_to_ipv4_listener(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows implementation")
    monkeypatch.setattr(deployment, "_process_identity", lambda _: "win:456")
    monkeypatch.setattr(deployment, "_tcp_rows", lambda family: (
        [(b"\x7f\0\0\x01", 11434, 123)] if family == socket.AF_INET else [(b"\0" * 16, 11434, 124)]))
    assert deployment.service_instance("http://localhost:11434") is None
    assert deployment.service_instance(URL) is None


def test_native_table_observes_only_our_ephemeral_loopback_listener():
    if os.name != "nt":
        pytest.skip("Windows implementation")
    # No Ollama/API is queried or started; the socket is owned by this test.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert (b"\x7f\0\0\x01", port, os.getpid()) in deployment._tcp_rows(socket.AF_INET)
        assert deployment._process_identity(os.getpid()).startswith("win:")
