"""Read-only Windows service-instance binding, not a KV/FA attestation.

Ollama's inherited server settings are not exposed by its metadata API. Never
infer them from this application's environment or a caller-supplied manifest.
Instead a qualification is restricted to one listener process lifetime and an
observed resident allocation. A restart (including a settings change) requires
fresh evidence. No sockets, model calls, credentials or process mutations here.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import socket
import struct
from typing import Any
from urllib.parse import urlsplit

from app.process_supervisor import _process_identity

MAX_TABLE_BYTES = 4 * 1024 * 1024


def _tcp_rows(family: int) -> list[tuple[bytes, int, int]]:
    """Bounded IP Helper owner-PID listener table; no netstat subprocess."""
    query = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                      ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    query.restype = ctypes.c_uint32
    size = ctypes.c_uint32()
    result = query(None, ctypes.byref(size), False, family, 3, 0)  # OWNER_PID_LISTENER
    if result not in (0, 122) or not 4 <= size.value <= MAX_TABLE_BYTES:
        raise OSError("listener table unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if query(buffer, ctypes.byref(size), False, family, 3, 0) != 0:
        raise OSError("listener table changed")
    return _decode_tcp_rows(buffer.raw[:size.value], family)


def _decode_tcp_rows(raw: bytes, family: int) -> list[tuple[bytes, int, int]]:
    width = 24 if family == socket.AF_INET else 56
    if family not in (socket.AF_INET, socket.AF_INET6) or len(raw) < 4:
        raise ValueError("invalid listener table")
    count = struct.unpack_from("<I", raw)[0]
    if count > (len(raw) - 4) // width:
        raise ValueError("truncated listener table")
    rows = []
    for index in range(count):
        offset = 4 + index * width
        if family == socket.AF_INET:
            state, _, port, _, _, pid = struct.unpack_from("<6I", raw, offset)
            address = raw[offset + 4:offset + 8]
        else:
            address = raw[offset:offset + 16]
            port, state, pid = (struct.unpack_from("<I", raw, offset + field)[0] for field in (20, 48, 52))
        if state == 2:
            rows.append((address, socket.ntohs(port & 0xFFFF), pid))
    return rows


def service_instance(base_url: str) -> dict[str, Any] | None:
    """Unknown/ambiguous listeners fail closed; never use a PID alone."""
    if os.name != "nt":
        return None
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port != 11434 or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            return None
        tables = {family: _tcp_rows(family) for family in (socket.AF_INET, socket.AF_INET6)}
        # An IPv6 wildcard may also accept IPv4-mapped connections. Do not
        # guess which process HTTP/DNS chose when either family is ambiguous.
        if any(port == parsed.port and address == b"\0" * len(address)
               for rows in tables.values() for address, port, _ in rows):
            return None
        addresses = {socket.AF_INET: b"\x7f\0\0\x01", socket.AF_INET6: b"\0" * 15 + b"\x01"}
        families = (socket.AF_INET,) if parsed.hostname == "127.0.0.1" else (
            (socket.AF_INET6,) if parsed.hostname == "::1" else (socket.AF_INET, socket.AF_INET6))
        pids = {pid for family in families for address, port, pid in tables[family]
                if address == addresses[family] and port == parsed.port and type(pid) is int and pid > 0}
        if len(pids) != 1:
            return None
        pid = pids.pop()
        creation = _process_identity(pid)
        if not isinstance(creation, str) or not re.fullmatch(r"win:[0-9]{1,20}", creation):
            return None
        return {"pid": pid, "creation": creation}
    except (OSError, ValueError, AttributeError, OverflowError):
        return None


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sanitize_deployment(value: Any) -> dict | None:
    """An explicit metadata allowlist; no raw provider fields/paths/env."""
    if not isinstance(value, dict) or set(value) != {"version", "service", "server_version", "resident", "hash"}:
        return None
    service, resident = value.get("service"), value.get("resident")
    if (value.get("version") != 1 or type(value.get("version")) is not int
            or not isinstance(service, dict) or set(service) != {"pid", "creation"}
            or type(service.get("pid")) is not int or not 0 < service["pid"] < 2**32
            or not isinstance(service.get("creation"), str) or not re.fullmatch(r"win:[0-9]{1,20}", service["creation"])
            or not isinstance(value.get("server_version"), str) or not re.fullmatch(r"[a-zA-Z0-9.+_-]{1,80}", value["server_version"])
            or not isinstance(resident, dict) or set(resident) != {"digest", "context_length", "size", "size_vram"}
            or not isinstance(resident.get("digest"), str) or not re.fullmatch(r"[a-f0-9]{64}", resident["digest"])
            or any(type(resident.get(key)) is not int or not 0 < resident[key] <= 2**50 for key in ("context_length", "size"))
            or not 0 < resident["context_length"] <= 10_000_000
            or type(resident.get("size_vram")) is not int or not 0 <= resident["size_vram"] <= 2**50):
        return None
    payload = {key: value[key] for key in ("version", "service", "server_version", "resident")}
    return {**payload, "hash": _hash(payload)} if value.get("hash") == _hash(payload) else None


def observe_deployment(base_url: str, server_version: str, resident: Any, before: dict | None) -> dict | None:
    if before is None or service_instance(base_url) != before or not isinstance(resident, dict):
        return None
    payload = {"version": 1, "service": before, "server_version": server_version,
               "resident": {key: resident.get(key) for key in ("digest", "context_length", "size", "size_vram")}}
    return sanitize_deployment({**payload, "hash": _hash(payload)})


def current_deployment_hash(base_url: str, value: Any) -> str | None:
    observation = sanitize_deployment(value)
    if observation is None or service_instance(base_url) != observation["service"]:
        return None
    return observation["hash"]
