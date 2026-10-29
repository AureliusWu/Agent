"""Keep public companion data required by the build interpreter's SSL policy.

Stock CPython needs no extra file. The managed interpreter can additionally
load a public OpenSSL distrust record next to ssl.py. PyInstaller moves ssl.py
into its archive, so that exact companion belongs in the onedir root. Never
collect arbitrary PEMs or change the interpreter's certificate policy.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import ssl
from types import CodeType


COMPANION_NAME = "codex-rejected-cas.pem"
COMPANION_BYTES = 906
COMPANION_SHA256 = "c916ce1b14ed8ca69e9b5c9fab22799a900dd610d6d4ec9b1347f4fd48e53718"


def _requires_companion(code: CodeType | None) -> bool:
    if code is None:
        return False
    return any(
        value == COMPANION_NAME
        or isinstance(value, CodeType) and _requires_companion(value)
        for value in code.co_consts
    )


def collect_ssl_companion_data(module=ssl) -> list[tuple[str, str]]:
    constructor = getattr(module.SSLContext.__new__, "__code__", None)
    if not _requires_companion(constructor):
        return []
    path = Path(module.__file__).resolve().parent / COMPANION_NAME
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("required public SSL companion is missing or linked")
        content = path.read_bytes()
        if len(content) != COMPANION_BYTES or hashlib.sha256(content).hexdigest() != COMPANION_SHA256:
            raise ValueError("public SSL companion differs from the reviewed certificate resource")
    except OSError as exc:
        raise ValueError("required public SSL companion cannot be read") from exc
    return [(str(path), ".")]


datas = collect_ssl_companion_data()
