"""Export or receive a source-bound v16 RC transport without granting acceptance.

Export first runs the existing total RC and installer validators. Receive only
validates transport bytes; the workflow must independently run those validators
after the existing importer. No command builds, installs, records manual proof,
calls a model, modifies old evidence, or publishes a release.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 20_000
MAX_BYTES = 8 * 1024**3
MAX_INDEX_BYTES = 8 * 1024**2
MAX_JSON_BYTES = 32 * 1024**2
MAX_ARCHIVE_BYTES = MAX_BYTES + 64 * 1024**2
MAX_SECONDS = 900
REQUIRED_FILES = frozenset({
    "build/v1600-evidence/accepted/rc-bundle.json",
    "build/v1600-evidence/accepted/default-model-identity.json",
    "build/v1600-evidence/nsis-installer-smoke.json",
    "build/v1600-evidence/msi-installer-smoke.json",
    "build/generated/build-info.json", "dist/release/agent-sbom.cdx.json",
    "dist/release/THIRD_PARTY_NOTICES.txt",
})
PRIVATE_SUFFIXES = frozenset({
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".key", ".pem",
    ".p12", ".pfx", ".token", ".dmp", ".dump", ".log", ".wav", ".mp3",
    ".ogg", ".flac", ".m4a", ".webm", ".aac", ".bundle", ".tar", ".7z",
})
PRIVATE_PARTS = frozenset({
    ".git", ".env", "private", "user-data", "user-assets", "uploads",
    "backups", "credentials", "secrets", "model-qualifications", "recordings",
    ".agent-backups", ".agent-runtime", "memory-export", "conversation-export",
})
TEXT_SUFFIXES = frozenset({
    ".json", ".xml", ".txt", ".md", ".csv", ".yaml", ".yml", ".ini",
    ".toml", ".py", ".js", ".ts", ".ps1", ".html", ".css", ".wxs", ".nsi",
})
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class TransportError(ValueError):
    pass


@lru_cache(maxsize=8)
def module(name: str):
    specification = importlib.util.spec_from_file_location(
        "rc_transport_" + name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    if specification is None or specification.loader is None:
        raise TransportError("required validator is unavailable")
    result = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = result
    specification.loader.exec_module(result)
    return result


def json_object(data: bytes, *, limit: int = MAX_JSON_BYTES) -> dict:
    if len(data) > limit:
        raise TransportError("JSON attachment exceeds its reader limit")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise TransportError("duplicate JSON keys are forbidden")
            value[key] = item
        return value

    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(TransportError("non-finite JSON")))
    if not isinstance(value, dict):
        raise TransportError("JSON attachment must contain an object")
    return value


def canonical_path(name: object) -> str:
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or any(ord(character) < 32 or character in '<>"|?*' for character in name)):
        raise TransportError("noncanonical Windows artifact path")
    parts = name.split("/")
    if (PurePosixPath(name).is_absolute() or any(part in {"", ".", ".."} for part in parts)
            or any(part.endswith((" ", ".")) for part in parts)
            or any(re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in parts)):
        raise TransportError("artifact path is unsafe on Windows")
    return name


def public_path(name: object) -> str:
    value = canonical_path(name)
    module("import-accepted-rc").allowed_path(value)
    path = PurePosixPath(value)
    lower = tuple(part.casefold() for part in path.parts)
    if (any(part in PRIVATE_PARTS for part in lower) or path.suffix.casefold() in PRIVATE_SUFFIXES
            or any(part.startswith((".env", "memory-export", "continuity-export", "conversation-export",
                                    "task-export")) or part.endswith((".private.json", ".user.json")) for part in lower)):
        raise TransportError("private material cannot enter a public RC transport")
    if path.suffix.casefold() == ".zip" and not (path.name == "base_library.zip" and "_internal" in path.parts):
        raise TransportError("nested private archives are forbidden")
    return value


def no_links(path: Path) -> None:
    for part in (path.absolute(), *path.absolute().parents):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise TransportError("links and reparse ancestors are forbidden")


def ordinary(path: Path) -> os.stat_result:
    no_links(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
        raise TransportError("only unlinked ordinary files are permitted")
    return metadata


def read_object(path: Path, *, limit: int = MAX_JSON_BYTES) -> dict:
    if ordinary(path).st_size > limit:
        raise TransportError("JSON file exceeds its bounded reader")
    with path.open("rb") as stream:
        return json_object(stream.read(limit + 1), limit=limit)


def public_content(name: str, block: bytes, *, first: bool = True) -> None:
    # A SQLite DLL legitimately contains the format marker as executable data;
    # only a file whose header is SQLite is itself a forbidden database.
    if first and block.startswith(b"SQLite format 3\x00"):
        raise TransportError("private database or key content is forbidden")
    privacy = module("privacy_scan")
    if any(pattern.search(block) for pattern in privacy.SECRET_PATTERNS.values()):
        raise TransportError("credential-bearing bytes cannot be exported")
    if PurePosixPath(name).suffix.casefold() in TEXT_SUFFIXES:
        # Do not use the source scanner's synthetic-path exemptions for raw
        # evidence. An argv containing a real machine path must be recaptured,
        # never edited or redacted to make the old run appear portable.
        if (any(pattern.search(block) for pattern in privacy.PII_PATTERNS.values())
                or privacy.EMAIL_PATTERN.search(block)
                or re.search(rb"(?<![A-Za-z0-9])/(?:Users|home)/[^\s\"'<>]+", block)
                or re.search(rb'(?i)"(?:api[_-]?key|password|authorization|access[_-]?token)"\s*:\s*"[^"\s][^"]*"', block)
                or re.search(rb"(?i)https?://[^/\s:]+:[^/\s@]+@", block)):
            raise TransportError("private machine metadata requires a new safe capture")


def checked_hash(path: Path, name: str, *, started: float) -> tuple[int, str]:
    before = ordinary(path)
    if before.st_size > MAX_BYTES:
        raise TransportError("file exceeds transport byte limit")
    digest, carry = hashlib.sha256(), b""
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if time.monotonic() - started > MAX_SECONDS:
                raise TransportError("transport validation timed out")
            public_content(name, carry + block, first=not carry)
            carry = block[-4096:]
            digest.update(block)
    after = ordinary(path)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise TransportError("attachment changed while being validated")
    if PurePosixPath(name).name == "base_library.zip":
        with zipfile.ZipFile(path) as library:
            validate_library(library, started)
    return before.st_size, digest.hexdigest()


def validate_library(library: zipfile.ZipFile, started: float) -> None:
    members, seen, total = library.infolist(), set(), 0
    if not members or len(members) > MAX_FILES:
        raise TransportError("frozen standard library ZIP exceeds member limits")
    for item in members:
        name = canonical_path(item.filename)
        mode = item.external_attr >> 16
        total += item.file_size
        if (item.is_dir() or item.flag_bits & 1 or item.file_size > MAX_JSON_BYTES
                or total > 256 * 1024**2 or PurePosixPath(name).suffix != ".pyc"
                or mode & 0xF000 not in {0, stat.S_IFREG} or name.casefold() in seen
                or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or time.monotonic() - started > MAX_SECONDS):
            raise TransportError("frozen standard library ZIP contains unexpected material")
        seen.add(name.casefold())
        public_content(name, library.read(item))


def references(value, depth: int = 0, *, candidate_sidecar: dict | None = None,
               candidate_payload: dict | None = None):
    if depth > 64:
        raise TransportError("attachment nesting exceeds its limit")
    if isinstance(value, dict):
        scoped_inventory = False
        if depth == 0 and "public_protocol" in value:
            if candidate_sidecar is None or candidate_payload is None:
                raise TransportError("public installer receipt lacks accepted candidate binding")
            module("rc_installed_public").validate_public_report(
                value, candidate_sidecar=candidate_sidecar, candidate_payload=candidate_payload)
            scoped_inventory = True
        if "path" in value and "sha256" in value:
            yield value
        for name, item in value.items():
            # Only the validated, exact-schema top-level observation is scoped
            # to the install fixture. All ordinary refs retain strict closure.
            if scoped_inventory and name == "installed_sidecar_payload":
                continue
            yield from references(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from references(item, depth + 1)


def public_installer_bindings(root: Path, name: str, payload: dict, expected_commit: str) -> dict:
    """The same typed envelope semantics apply to export and received bytes."""
    if "public_protocol" not in payload:
        return {}
    run, source = payload.get("run"), payload.get("source")
    if not isinstance(run, dict) or not isinstance(source, dict):
        raise TransportError("public installer lacks source or run identity")
    kind = run.get("installer_kind")
    if (kind not in {"NSIS", "MSI"}
            or name != f"build/v1600-evidence/{kind.lower()}-installer-smoke.json"
            or source.get("source_commit") != expected_commit):
        raise TransportError("public installer receipt has wrong fixed path or source")
    bundle = read_object(root / "build/v1600-evidence/accepted/rc-bundle.json")
    binaries = bundle.get("binaries")
    if not isinstance(binaries, dict):
        raise TransportError("public installer lacks accepted binary references")
    sidecar = binaries.get("sidecar")
    accepted_payload = module("rc_installed_public").load_candidate(
        root, candidate_sidecar=sidecar, candidate_payload=bundle.get("sidecar_payload"))
    return {"candidate_sidecar": sidecar, "candidate_payload": accepted_payload}


def index_from_closure(root: Path, expected_commit: str) -> dict:
    """Transport-only unit: this function alone does not grant RC acceptance."""
    if COMMIT.fullmatch(expected_commit) is None:
        raise TransportError("source commit is malformed")
    pending = [(name, None) for name in sorted(REQUIRED_FILES)]
    entries, names, total, started = {}, {}, 0, time.monotonic()
    while pending:
        name, expected = pending.pop()
        name = public_path(name)
        if expected is not None:
            if not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
                raise TransportError("attachment has no valid SHA-256")
            expected = expected.lower()
        key = name.casefold()
        if key in names and names[key] != name:
            raise TransportError("case-insensitive artifact alias")
        if name in entries:
            if expected is not None and entries[name]["sha256"] != expected:
                raise TransportError("conflicting attachment hashes")
            continue
        path = root / name
        size, digest = checked_hash(path, name, started=started)
        if expected is not None and digest != expected:
            raise TransportError("attachment no longer matches its original receipt")
        names[key] = name
        entries[name] = {"path": name, "bytes": size, "sha256": digest}
        total += size
        if len(entries) > MAX_FILES or total > MAX_BYTES:
            raise TransportError("attachment closure exceeds transport limits")
        if path.suffix.casefold() == ".json":
            payload = read_object(path)
            bindings = public_installer_bindings(root, name, payload, expected_commit)
            for item in references(payload, **bindings):
                if len(pending) >= MAX_FILES * 4:
                    raise TransportError("attachment reference queue exceeded its limit")
                pending.append((item["path"], item["sha256"]))
    # Installer validators use name/hash metadata rather than a normal path
    # reference; retain each actual package and the previous accepted package.
    for kind in ("nsis", "msi"):
        payload = read_object(root / f"build/v1600-evidence/{kind}-installer-smoke.json")
        for label in ("candidate", "previous"):
            artifact = payload.get("artifacts", {}).get(label, {})
            basename = artifact.get("name")
            if not isinstance(basename, str) or canonical_path(basename) != basename or "/" in basename:
                raise TransportError("installer metadata lacks a safe package name")
            if label == "candidate":
                candidates = [root / f"desktop/src-tauri/target/release/bundle/{kind}" / basename]
            else:
                directory = root / "build/upgrade-baseline"
                no_links(directory)
                candidates = []
                scanned = 0
                for current, directories, files in os.walk(directory, followlinks=False):
                    for item in directories:
                        no_links(Path(current) / item)
                    scanned += len(directories) + len(files)
                    if scanned > MAX_FILES:
                        raise TransportError("upgrade package search exceeded its limit")
                    candidates.extend(Path(current) / item for item in files if item == basename)
            if len(candidates) != 1:
                raise TransportError("installer package identity is missing or ambiguous")
            path = candidates[0]
            name = public_path(path.relative_to(root).as_posix())
            size, digest = checked_hash(path, name, started=started)
            if digest.upper() != str(artifact.get("sha256", "")).upper() or size != artifact.get("bytes"):
                raise TransportError("installer package no longer matches the accepted bytes")
            if name.casefold() in names and names[name.casefold()] != name:
                raise TransportError("installer case alias")
            if name not in entries:
                names[name.casefold()] = name
                entries[name] = {"path": name, "bytes": size, "sha256": digest}
                total += size
    if len(entries) > MAX_FILES or total > MAX_BYTES:
        raise TransportError("complete transport exceeds limits")
    return {"schema_version": 1, "target_version": "16.0.0", "source_commit": expected_commit,
            "accepted_candidate": True, "files": [entries[key] for key in sorted(entries)]}


def verify_actual_rc(root: Path) -> dict:
    sys.path.insert(0, str(root / "siyi"))
    builder, gate = module("generate_build_info"), module("rc_gate")
    source = builder._release_source_identity(root)
    if source.get("workspace_clean") is not True or source.get("source_version") != "16.0.0":
        raise TransportError("export requires current CLEAN v16 source")
    bundle = read_object(root / "build/v1600-evidence/accepted/rc-bundle.json")
    identity = read_object(root / "build/v1600-evidence/accepted/default-model-identity.json")
    result = gate.check_bundle(root, bundle, current=source, build_manifest=builder.generate_manifest(root, "Release"), model_identity=identity)
    if result.get("status") != "RC_READY_NOT_RELEASED" or result.get("passed") is not True:
        raise TransportError("the existing complete RC gate has not accepted this source")
    module("check-release-evidence").validate_release_evidence(root, root / "build/v1600-evidence")
    return source


def export_archive(root: Path, output: Path) -> dict:
    source = verify_actual_rc(root)
    index = index_from_closure(root, str(source["source_commit"]))
    index_bytes = json.dumps(index, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(index_bytes) > MAX_INDEX_BYTES:
        raise TransportError("export index exceeds its limit")
    no_links(output)
    if output.exists():
        raise TransportError("export output must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    no_links(output.parent)
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("artifact-index.json", index_bytes)
        for entry in index["files"]:
            path = root / entry["path"]
            ordinary(path)
            archive.write(path, entry["path"])
    # Validate the actual copied ZIP bytes before handing it to an upload step.
    validate_archive(output, str(source["source_commit"]))
    if verify_actual_rc(root) != source:
        raise TransportError("source changed during export")
    return {"status": "EXPORTED_NOT_UPLOADED_NOT_RELEASED", "files": len(index["files"]),
            "source_commit": source["source_commit"], "sha256": module("import-accepted-rc").digest(output)}


def validate_archive(archive_path: Path, expected_commit: str) -> dict:
    ordinary(archive_path)
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES or COMMIT.fullmatch(expected_commit) is None:
        raise TransportError("archive identity or byte bound is invalid")
    started, names, infos, total = time.monotonic(), set(), {}, 0
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_FILES + 1:
            raise TransportError("archive member count exceeds limits")
        for item in members:
            name = canonical_path(item.filename)
            mode = item.external_attr >> 16
            if (item.is_dir() or item.flag_bits & 1 or mode & 0xF000 not in {0, stat.S_IFREG}
                    or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or name.casefold() in names):
                raise TransportError("ZIP has duplicate, special, encrypted or unsafe members")
            if name != "artifact-index.json":
                public_path(name)
            names.add(name.casefold())
            infos[name] = item
            total += item.file_size
            if total > MAX_BYTES + MAX_INDEX_BYTES or item.file_size > MAX_BYTES:
                raise TransportError("uncompressed ZIP exceeds limits")
        if "artifact-index.json" not in infos or infos["artifact-index.json"].file_size > MAX_INDEX_BYTES:
            raise TransportError("bounded artifact index is missing")
        index = json_object(archive.read(infos["artifact-index.json"]), limit=MAX_INDEX_BYTES)
        entries = index.get("files")
        if (set(index) != {"schema_version", "target_version", "source_commit", "accepted_candidate", "files"}
                or type(index.get("schema_version")) is not int or index.get("schema_version") != 1
                or index.get("target_version") != "16.0.0"
                or index.get("source_commit") != expected_commit or index.get("accepted_candidate") is not True
                or not isinstance(entries, list) or not entries or len(entries) > MAX_FILES):
            raise TransportError("index is not bound to the selected commit")
        indexed, indexed_total = set(), 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
                raise TransportError("invalid artifact index entry")
            name = public_path(entry["path"])
            if (name.casefold() in indexed or name not in infos or type(entry["bytes"]) is not int
                    or entry["bytes"] < 0 or entry["bytes"] != infos[name].file_size
                    or not isinstance(entry["sha256"], str) or SHA.fullmatch(entry["sha256"]) is None):
                raise TransportError("duplicate, missing or changed indexed attachment")
            indexed.add(name.casefold())
            indexed_total += entry["bytes"]
            digest, carry = hashlib.sha256(), b""
            with archive.open(infos[name]) as stream:
                while block := stream.read(1024 * 1024):
                    if time.monotonic() - started > MAX_SECONDS:
                        raise TransportError("archive validation timed out")
                    public_content(name, carry + block, first=not carry)
                    carry = block[-4096:]
                    digest.update(block)
            if digest.hexdigest() != entry["sha256"]:
                raise TransportError("ZIP bytes differ from the artifact index")
            if PurePosixPath(name).name == "base_library.zip":
                with archive.open(infos[name]) as stream, zipfile.ZipFile(stream) as library:
                    validate_library(library, started)
        if (indexed_total > MAX_BYTES or not {name.casefold() for name in REQUIRED_FILES} <= indexed
                or set(infos) != {entry["path"] for entry in entries} | {"artifact-index.json"}):
            raise TransportError("ZIP has missing required or unindexed extra material")
        return index


def receive_archive(root: Path, archive_path: Path, directory: Path, expected_commit: str) -> dict:
    if not directory.resolve().is_relative_to((root / "build/v1600-evidence").resolve()):
        raise TransportError("receive destination must stay in generated v16 evidence")
    no_links(directory)
    if directory.exists():
        raise TransportError("receive destination must be fresh")
    index = validate_archive(archive_path, expected_commit)
    directory.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        for name in ["artifact-index.json", *(entry["path"] for entry in index["files"])]:
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            no_links(target.parent)
            with archive.open(name) as origin, target.open("xb") as destination:
                shutil.copyfileobj(origin, destination, length=1024 * 1024)
    # The existing importer validates the extracted ordinary files and hashes;
    # validation is not materialization or a new RC verdict.
    validate_directory(root, directory, expected_commit)
    return {"status": "TRANSPORT_VALIDATED_NOT_RC_ACCEPTED", "files": len(index["files"])}


def validate_directory(root: Path, directory: Path, expected_commit: str) -> dict:
    """Recheck the exact expanded transport immediately before artifact upload."""
    no_links(directory)
    if not directory.resolve().is_relative_to((root / "build/v1600-evidence").resolve()):
        raise TransportError("expanded transport must stay inside generated evidence")
    index_path = directory / "artifact-index.json"
    index = read_object(index_path, limit=MAX_INDEX_BYTES)
    if (set(index) != {"schema_version", "target_version", "source_commit", "accepted_candidate", "files"}
            or type(index.get("schema_version")) is not int or index.get("schema_version") != 1
            or COMMIT.fullmatch(expected_commit) is None):
        raise TransportError("expanded index has unexpected metadata")
    entries = module("import-accepted-rc").plan(directory, root, expected_commit)
    expected = {"artifact-index.json"} | {entry["path"] for entry in index["files"]}
    if not REQUIRED_FILES <= expected:
        raise TransportError("expanded transport lost a required accepted attachment")
    observed, nodes, started = set(), 0, time.monotonic()
    for current, directories, files in os.walk(directory, followlinks=False):
        nodes += len(directories) + len(files)
        if nodes > MAX_FILES * 2 or time.monotonic() - started > MAX_SECONDS:
            raise TransportError("expanded transport enumeration exceeded its bound")
        for name in directories:
            no_links(Path(current) / name)
        for name in files:
            path = Path(current) / name
            ordinary(path)
            relative = path.relative_to(directory).as_posix()
            if relative not in expected:
                raise TransportError("unindexed material was added to the expanded transport")
            observed.add(relative)
    if observed != expected:
        raise TransportError("expanded transport is incomplete")
    indexed = {entry["path"]: entry["sha256"] for entry in index["files"]}
    for entry in index["files"]:
        name = public_path(entry["path"])
        size, digest = checked_hash(directory / name, name, started=started)
        if size != entry["bytes"] or digest != entry["sha256"]:
            raise TransportError("expanded immutable transport changed before upload")
        if PurePosixPath(name).suffix.casefold() == ".json":
            payload = read_object(directory / name)
            bindings = public_installer_bindings(directory, name, payload, expected_commit)
            reference_count = 0
            for reference in references(payload, **bindings):
                # Removing the public marker restores ordinary strict closure;
                # it cannot make fixture paths or hidden refs disappear.
                reference_count += 1
                referenced = public_path(reference["path"])
                expected_hash = reference["sha256"]
                if (reference_count > MAX_FILES * 4 or not isinstance(expected_hash, str)
                        or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None
                        or indexed.get(referenced) != expected_hash.lower()):
                    raise TransportError("expanded attachment closure is missing or changed")
    return {"status": "TRANSPORT_VALIDATED_NOT_RC_ACCEPTED", "files": len(entries)}


class AssetRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.hostname not in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
                or parsed.port not in {None, 443} or parsed.username is not None or parsed.password is not None):
            raise TransportError("asset redirected outside the fixed GitHub transport")
        redirected = super().redirect_request(request, response, code, message, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


def download_asset(repository: str, asset_id: int, sha256: str, output: Path) -> dict:
    if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+", repository) is None
            or repository.split("/")[-1] in {".", ".."}
            or type(asset_id) is not int or asset_id <= 0 or SHA.fullmatch(sha256) is None):
        raise TransportError("explicit repository asset ID and SHA-256 are required")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise TransportError("a scoped GitHub token is required for the own-repository asset")
    api = f"https://api.github.com/repos/{repository}/releases/assets/{asset_id}"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "Accept-Encoding": "identity", "User-Agent": "Siyi-RC-Transport"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), AssetRedirects())
    with opener.open(urllib.request.Request(api, headers=headers), timeout=30) as response:
        if response.geturl() != api:
            raise TransportError("asset metadata did not come from the selected repository")
        metadata = json_object(response.read(1024 * 1024 + 1), limit=1024 * 1024)
    parsed = urllib.parse.urlsplit(str(metadata.get("browser_download_url", "")))
    if (metadata.get("id") != asset_id or metadata.get("url") != api or metadata.get("state") != "uploaded"
            or type(metadata.get("size")) is not int or not 0 < metadata["size"] <= MAX_ARCHIVE_BYTES
            or parsed.scheme != "https" or parsed.netloc != "github.com"
            or not parsed.path.startswith("/" + repository + "/releases/download/")
            or not str(metadata.get("name", "")).endswith(".zip")
            or metadata.get("digest") not in {None, "sha256:" + sha256}):
        raise TransportError("asset metadata/hash is not the explicit own-repository transport")
    no_links(output)
    if output.exists():
        raise TransportError("download output must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    no_links(output.parent)
    headers["Accept"] = "application/octet-stream"
    size, digest, started = 0, hashlib.sha256(), time.monotonic()
    with opener.open(urllib.request.Request(api, headers=headers), timeout=30) as response, output.open("xb") as destination:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > metadata["size"] or time.monotonic() - started > MAX_SECONDS:
                raise TransportError("download exceeds its exact size/time bound")
            digest.update(block)
            destination.write(block)
    if size != metadata["size"] or digest.hexdigest() != sha256:
        raise TransportError("downloaded asset differs from the explicitly authorized SHA-256")
    return {"status": "DOWNLOADED_NOT_ACCEPTED", "asset_id": asset_id, "bytes": size, "sha256": sha256}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    receive = commands.add_parser("receive")
    receive.add_argument("--archive", type=Path)
    receive.add_argument("--directory", type=Path, required=True)
    receive.add_argument("--commit", required=True)
    receive.add_argument("--validate-only", action="store_true", help="recheck an existing expanded transport; never overwrite")
    download = commands.add_parser("download")
    download.add_argument("--repository", required=True)
    download.add_argument("--asset-id", type=int, required=True)
    download.add_argument("--sha256", required=True)
    download.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "receive" and ((args.archive is None) != args.validate_only):
        parser.error("receive needs --archive, or --validate-only without --archive")
    try:
        if args.command == "export":
            result = export_archive(ROOT, args.output)
        elif args.command == "receive":
            result = (validate_directory(ROOT, args.directory, args.commit) if args.validate_only
                      else receive_archive(ROOT, args.archive, args.directory, args.commit))
        else:
            result = download_asset(args.repository, args.asset_id, args.sha256, args.output)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError, KeyError, ImportError, RecursionError, zipfile.BadZipFile):
        # Failures can contain private paths/URLs/headers; do not print them.
        print(json.dumps({"status": "BLOCKED", "detail": "Unsafe, private, incomplete or unaccepted RC transport; failed output retained for local inspection"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
