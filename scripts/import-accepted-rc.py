"""Materialize an immutable accepted v16 artifact into generated paths only.

The artifact keeps repository-relative layout and an index of every file hash.
This does not rebuild, install, approve, or publish anything. Existing files are
never overwritten; missing or mismatched content blocks the import.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat

ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 20000
MAX_TOTAL_BYTES = 8 * 1024**3
# Public Mozilla trust roots from the locked certifi==2026.6.17 wheel.
# This is not a general PEM exception: changing that dependency requires a
# reviewed resource update. File and ZIP readers verify these exact bytes.
CERTIFI_CA_BYTES = 234354
CERTIFI_CA_SHA256 = "bbc7e9c01d7551bb8a159b5dedd989b8ee3ce105aff522b68eb1b01bf854cab0"
# Public OpenSSL distrust record required by the managed build interpreter.
# Exact resource bytes only: this does not authorize private/general PEMs.
SSL_COMPANION_CA_BYTES = 906
SSL_COMPANION_CA_SHA256 = "c916ce1b14ed8ca69e9b5c9fab22799a900dd610d6d4ec9b1347f4fd48e53718"
PUBLIC_TLS_RESOURCES = {
    "/_internal/certifi/cacert.pem": (CERTIFI_CA_BYTES, CERTIFI_CA_SHA256),
    "/_internal/codex-rejected-cas.pem": (SSL_COMPANION_CA_BYTES, SSL_COMPANION_CA_SHA256),
}
# Exact public notices independently matched to official PyPI cp312 win_amd64
# wheel bytes, not merely an installed RECORD. No general license/email rule.
# numpy 2.5.1 wheel: f7d60026c0bdb1380e83bfa7a0419c4577ee4b9a08880afcb6dadeb74c649fa2
# onnxruntime 1.28.0: c35064f9b3c43c81c5d5d282091401d0f1ff22796d93ccade4ea2ece5e137ab8
PUBLIC_NOTICE_RESOURCES = {
    "/_internal/numpy-2.5.1.dist-info/licenses/numpy/_core/include/numpy/libdivide/LICENSE.txt":
        (1039, "d544761558b510866c21f7e8a2d5716fca76e0b98117e3b6b86314db7244d150"),
    "/_internal/numpy-2.5.1.dist-info/licenses/numpy/random/LICENSE.md":
        (3582, "b4bc2f4fa1c95778f1ed3dd8f14706bece399475b978f9f34b5d3cd72521b48a"),
    "/_internal/numpy-2.5.1.dist-info/licenses/numpy/random/src/mt19937/LICENSE.md":
        (2986, "92c10699884321f1c2947fe2a70ab23b0fbf1507f756d89dd08f38c61fc3d4fa"),
    "/_internal/numpy-2.5.1.dist-info/licenses/numpy/random/src/pcg64/LICENSE.md":
        (1176, "41190663b77bee5302386495a07b7eabfbfa571aa0ae2f11da6764162062fc4f"),
    "/_internal/numpy-2.5.1.dist-info/licenses/numpy/random/src/splitmix64/LICENSE.md":
        (340, "454ec7350e478e037061850b67395753b425b1cb0a34bbc26290b020bf89d763"),
    "/_internal/onnxruntime/ThirdPartyNotices.txt":
        (331175, "fb0af774b4d7cffc5b9d046f2aaeade2f37df2f80abf8033c95dfffcc77a8866"),
}


def is_public_certifi_ca(name: str) -> bool:
    return name.endswith("/_internal/certifi/cacert.pem")


def public_tls_identity(name: str) -> tuple[int, str] | None:
    for suffix, identity in PUBLIC_TLS_RESOURCES.items():
        if name.endswith(suffix):
            return identity
        if name.casefold().endswith(suffix.casefold()):
            raise ValueError("public TLS resource path has a case alias")
    return None


def public_notice_identity(name: str) -> tuple[int, str] | None:
    for suffix, identity in PUBLIC_NOTICE_RESOURCES.items():
        if name.endswith(suffix):
            return identity
        if name.casefold().endswith(suffix.casefold()):
            raise ValueError("public notice path has a case alias")
    return None


def validate_public_resource(name: str, size: int, sha256: str) -> None:
    if Path(name).suffix.casefold() == ".pem":
        identity = public_tls_identity(name)
        if identity is None or type(size) is not int or (size, sha256) != identity:
            raise ValueError("PEM is not an exact reviewed public TLS resource")
    notice = public_notice_identity(name)
    if notice is not None and (type(size) is not int or (size, sha256) != notice):
        raise ValueError("public notice is not the exact verified wheel resource")


def allowed_path(name: object) -> Path:
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("artifact path must be a canonical relative path")
    path = Path(name)
    if (path.is_absolute() or ".." in path.parts or path.as_posix() != name
            or any(part.endswith((".", " ")) for part in path.parts)):
        raise ValueError("artifact path escapes its generated destination")
    permitted = (
        name.startswith("desktop/src-tauri/target/release/")
        or name.startswith("build/v1601-evidence/accepted/")
        or name in {"build/v1601-evidence/nsis-installer-smoke.json", "build/v1601-evidence/msi-installer-smoke.json",
                    "build/generated/build-info.json", "dist/release/agent-sbom.cdx.json", "dist/release/THIRD_PARTY_NOTICES.txt"}
        or (name.startswith("build/upgrade-baseline/") and path.suffix.lower() in {".exe", ".msi"})
    )
    if not permitted:
        raise ValueError("artifact cannot write source, configuration or user-data paths")
    if path.suffix.casefold() == ".pem" and public_tls_identity(name) is None:
        raise ValueError("only exact packaged public TLS PEM paths are permitted")
    public_notice_identity(name)  # Known resources cannot use Windows case aliases.
    return path


def no_links(path: Path, boundary: Path, *, existing_only: bool = False) -> None:
    if not path.absolute().is_relative_to(boundary.absolute()):
        raise ValueError("artifact path is outside its boundary")
    for item in (path, *path.parents):
        if existing_only and not item.exists():
            continue
        info = item.lstat()
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("artifact links/reparse paths are forbidden")
        if item == boundary:
            break


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def plan(directory: Path, root: Path, expected_commit: str) -> list[tuple[Path, Path, str]]:
    directory = directory.absolute()
    no_links(directory, root, existing_only=True)
    index = directory / "artifact-index.json"
    no_links(index, directory)
    if index.stat().st_size > 8 * 1024**2:
        raise ValueError("artifact index is too large")
    data = json.loads(index.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("target_version") != "16.0.1"
            or data.get("source_commit") != expected_commit or data.get("accepted_candidate") is not True):
        raise ValueError("accepted artifact source/version contract does not match")
    files = data.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError("accepted artifact file list is missing or exceeds its bound")
    result, seen, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("artifact index entry is invalid")
        relative = allowed_path(item["path"])
        key = relative.as_posix().casefold()
        if key in seen:
            raise ValueError("artifact has repeated Windows paths")
        seen.add(key)
        source, target = directory / relative, root / relative
        no_links(source, directory)
        no_links(target.parent, root, existing_only=True)
        metadata = source.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or type(item["bytes"]) is not int or item["bytes"] != metadata.st_size
                or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in item["sha256"])):
            raise ValueError("artifact file identity is invalid")
        total += metadata.st_size
        actual_digest = digest(source)
        if total > MAX_TOTAL_BYTES or actual_digest != item["sha256"]:
            raise ValueError("artifact byte/hash limits do not match")
        validate_public_resource(item["path"], metadata.st_size, actual_digest)
        if target.exists():
            no_links(target, root)
            if not target.is_file() or digest(target) != item["sha256"]:
                raise ValueError("refusing to replace an existing generated artifact with different bytes")
        result.append((source, target, item["sha256"]))
    required = {"build/v1601-evidence/accepted/rc-bundle.json", "build/v1601-evidence/accepted/default-model-identity.json",
                "build/v1601-evidence/nsis-installer-smoke.json", "build/v1601-evidence/msi-installer-smoke.json"}
    if not required <= seen:
        raise ValueError("accepted artifact lacks the total RC/installer evidence")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        entries = plan(args.directory, ROOT, args.commit)
        if not args.dry_run:
            for source, target, expected in entries:
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                no_links(target.parent, ROOT)
                with source.open("rb") as origin, target.open("xb") as destination:
                    shutil.copyfileobj(origin, destination, length=1024 * 1024)
                if digest(target) != expected:
                    raise ValueError("materialized artifact hash changed")
        print(json.dumps({"status": "VALIDATED" if args.dry_run else "IMPORTED", "files": len(entries), "rebuilt": False, "published": False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "BLOCKED", "detail": "Accepted artifact is missing, unsafe or does not match immutable content"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
