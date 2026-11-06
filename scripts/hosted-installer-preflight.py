"""Build and observe a non-release installer on fresh GitHub-hosted Windows jobs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
import urllib.request
import zipfile

from rc_desktop_candidate import PRIOR, ROOT, REPOSITORY, api, digest, download_prior, module, reference, require


WORKFLOW = ".github/workflows/installer-preflight.yml"
BUNDLE = ROOT / "desktop/src-tauri/target/release/bundle"
KINDS = ("nsis", "msi")
CAP = 128 * 1024**2
SHA256 = re.compile(r"[0-9a-f]{64}")
# This patch is tested from the published v17.0.0 packages, not the historical
# v8.0.1 compatibility baseline. GitHub metadata is mutable: pin IDs, the tag
# target and the asset bytes, and recheck the binding after transport.
PATCH_BASELINE_TAG = "v17.0.0"
PATCH_BASELINE_RELEASE_ID = 436343717
PATCH_BASELINE_BUILD = {
    "product_version": "17.0.0",
    "git_commit": "d447ccd33acb8c71c1b3037008c7463f136a4c2b",
    "workspace_state": "CLEAN",
    "source_fingerprint": "595CD3A4F346DAFC2069DD30C69D290CF70FF83D35D9E85885A2D7C41EE9B9D3",
    "build_id": "8aca054f8b49ae7fec157900",
    "component_build_id": "sidecar-8aca054f8b49ae7fec157900",
}
PATCH_BASELINE_ASSETS = {
    "nsis": {"id": 716707668, "name": "Siyi_17.0.0_x64-setup.exe", "size": 54445173,
             "sha256": "56559bbb4e2bce509a0a639780b19413c8f9a94a87310bcec663e6302b25cb3c"},
    "msi": {"id": 716707678, "name": "Siyi_17.0.0_x64_zh-CN.msi", "size": 83492875,
            "sha256": "a916dcff13e55317faafa4bc0ad9841d5c9d7fe013fe177ef81665e9a2f4c17f"},
}
# The locked Pillow 12.3.0 wheel's PIL.ImageFont.load_default embeds a public
# TrueType font as a 17,125-byte base64 literal. Stored zlib blocks can split
# that literal in the PE, so verify an exact 84-byte window containing the
# entire token-like substring and 32 public font bytes on each side.
PUBLIC_PILLOW_FONT_CONTEXT_BYTES = 84
PUBLIC_PILLOW_FONT_CONTEXT_SHA256 = "45204ea793a7e2c361bda197fcaefc412de9887ac055fdb85e6cb421f10fb3fe"
PUBLIC_PILLOW_FONT_AWS_MATCH_OFFSET = 32
TAURI_MARKER = b"__TAURI_BUNDLE_TYPE_VAR_UNK"
# Exact members of the locked official Windows wheels (archive digest and
# member bytes verified independently). These native extensions contain the
# public upstream Rust build profile, which collides with our hosted profile.
OFFICIAL_VENDOR_MEMBERS = {
    "_internal/hf_xet/hf_xet.pyd": (
        9_393_664, "1d7086190c8f67568f670d4e1e1493097ce31058a634d9ce5c7f2922875e13e6"),
    "_internal/pydantic_core/_pydantic_core.cp312-win_amd64.pyd": (
        5_253_120, "775bcb0ce04b93cb18410bb28e589e1dd6a441f1f93007d2dc80a305d70c74c9"),
    "_internal/tokenizers/tokenizers.pyd": (
        7_565_312, "619b18e51500ef40a7de56d823d3c34499e59745487562acc619367ac5d8ae39"),
    "_internal/watchfiles/_rust_notify.cp312-win_amd64.pyd": (
        636_416, "51ea9b485458e0469a67250c64f9dda9a86c9a670ce67182cfd36dc991d286d5"),
}
PUBLIC_UPSTREAM_BUILD_PREFIXES = {
    separator.join(("c:", "users", "runneradmin", *suffix)).encode(encoding):
    separator.join(("c:", "users", "runneradmin", ".cargo", "registry", "src", "index.crates.io-")).encode(encoding)
    for separator in ("/", "\\") for suffix in ((), (".cargo",))
    for encoding in ("utf-8", "utf-16-le")
}
MSI_CHILD_PREFIX = b"SIYI_MSI_PREFLIGHT_FAILURE "
MSI_CHILD_LOG_LIMIT = 512 * 1024
MSI_CHILD_RECORD_LIMIT = 512
MSI_CHILD_STAGES = frozenset({
    "prepare", "install_previous", "previous_identity", "seed_fixture", "upgrade", "candidate_identity",
    "capture_installed", "launch_candidate", "validate_migration", "repair_same_version", "uninstall",
    "reinstall", "launch_reinstall", "final_uninstall", "check_removed", "write_evidence",
})
MSI_CHILD_REASONS = frozenset({
    "LIFECYCLE_CHECK_FAILED", "MSI_EXIT", "MISSING_DESKTOP_EXE", "NATIVE_READINESS_TIMEOUT", "NATIVE_EXITED",
    "NATIVE_WINDOW_MISSING", "NATIVE_SHUTDOWN_TIMEOUT", "SIDECAR_LEFT_RUNNING", "MSI_ADMINISTRATOR_REQUIRED",
})


class Stage(str, Enum):
    CONTEXT = "context"
    PREPARE_BUILD = "prepare_build"
    COMPILE = "compile_candidate"
    BIND_BUILD = "bind_candidate_source"
    INVENTORY = "inventory_payload"
    SCAN_PAYLOAD = "scan_payload"
    STAGE_BUNDLE = "stage_bundle_inputs"
    BUNDLE = "bundle_installers"
    VERIFY_BUNDLES = "verify_bundles"
    SCAN_INSTALLER = "scan_installer"
    STAGE_UPLOAD = "stage_upload"
    RECEIVE = "receive_metadata"
    DOWNLOAD = "download_candidate"
    EXTRACT = "extract_candidate"
    BASELINE = "receive_baseline"
    RECORD_RECEIVE = "record_received_candidate"
    BIND_INSTALL = "bind_install_inputs"
    HOST = "check_install_host"
    INSTALL = "observe_installer"
    VERIFY_INSTALL = "verify_install_evidence"
    SUMMARY = "write_summary"


class Role(str, Enum):
    DESKTOP = "desktop"
    SIDECAR = "sidecar"
    SUPPORT = "sidecar_support"
    NSIS = "nsis"
    MSI = "msi"
    DESCRIPTOR = "descriptor"


class InvalidInvocation(ValueError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default error output can echo untrusted argument values.
        raise InvalidInvocation from None


# Exact validator literals map to fixed public codes. Do not serialize a
# message, traceback, argv, exception type name, or a filename here.
SAFE_VALIDATION_REASONS = {
    "links and reparse ancestors are forbidden": "REPARSE_PATH",
    "only unlinked ordinary files are permitted": "NON_ORDINARY_FILE",
    "credential-bearing bytes cannot be exported": "CREDENTIAL_PATTERN",
    "private database or key content is forbidden": "PRIVATE_DATABASE_OR_KEY",
    "private machine metadata requires a new safe capture": "PRIVATE_TEXT_METADATA",
    "private runtime filename": "PRIVATE_FILENAME",
    "private material cannot enter a public RC transport": "PRIVATE_FILENAME",
    "build-machine path in candidate": "BUILD_MACHINE_PATH",
    "sidecar changed during scan": "CHANGED_INPUT",
    "PEM is not an exact reviewed public TLS resource": "PUBLIC_RESOURCE_IDENTITY",
    "public notice is not the exact verified wheel resource": "PUBLIC_RESOURCE_IDENTITY",
    "unknown public notice resource": "PUBLIC_RESOURCE_IDENTITY",
    "official upstream resource identity differs": "PUBLIC_RESOURCE_IDENTITY",
    "frozen standard library ZIP exceeds member limits": "FROZEN_LIBRARY_LIMIT",
    "frozen standard library ZIP contains unexpected material": "FROZEN_LIBRARY_MEMBER",
    "noncanonical Windows artifact path": "UNSAFE_ARCHIVE_PATH",
    "artifact path is unsafe on Windows": "UNSAFE_ARCHIVE_PATH",
    "JSON attachment exceeds its reader limit": "JSON_LIMIT",
    "duplicate JSON keys are forbidden": "DUPLICATE_JSON_KEYS",
    "JSON attachment must contain an object": "INVALID_JSON_OBJECT",
    "candidate subprocess failed; private log retained": "SUBPROCESS_FAILED",
    "actual subprocess output missing": "SUBPROCESS_OUTPUT_MISSING",
    "NSIS size gate failed; no installer artifact is published": "SIZE_GATE",
    "fresh desktop has existing software, installer activity, or missing prerequisites": "HOST_NOT_READY",
    "visible native window station unavailable": "NATIVE_DESKTOP_UNAVAILABLE",
    "interactive native session unavailable": "NATIVE_DESKTOP_UNAVAILABLE",
    "input desktop unavailable": "NATIVE_DESKTOP_UNAVAILABLE",
    "native desktop handle did not close": "NATIVE_DESKTOP_UNAVAILABLE",
}


def failure_reason(error: Exception) -> str:
    if isinstance(error, InvalidInvocation):
        return "INVALID_ARGUMENTS"
    if isinstance(error, ValueError) and len(error.args) == 1 and type(error.args[0]) is str:
        code = SAFE_VALIDATION_REASONS.get(error.args[0])
        if code is not None:
            return code
    if isinstance(error, subprocess.TimeoutExpired):
        return "SUBPROCESS_TIMEOUT"
    if isinstance(error, HTTPError):
        return "HTTP_FAILURE"
    if isinstance(error, URLError):
        return "NETWORK_FAILURE"
    if isinstance(error, zipfile.BadZipFile):
        return "INVALID_ZIP"
    if isinstance(error, json.JSONDecodeError):
        return "INVALID_JSON"
    if isinstance(error, FileNotFoundError):
        return "MISSING_FILE"
    if isinstance(error, OSError):
        return "FILESYSTEM_OR_PROCESS_ERROR"
    if isinstance(error, ValueError):
        return "VALIDATION_FAILED"
    return "UNEXPECTED_ERROR"


@dataclass
class Diagnostics:
    stage: Stage = Stage.CONTEXT
    role: Role | None = None
    relative_path_sha256: str | None = None
    msi_child: dict[str, str | int] | None = None

    def mark(self, stage: Stage, role: Role | None = None, relative: str | None = None) -> None:
        self.stage, self.role = stage, role
        self.relative_path_sha256 = hashlib.sha256(relative.encode("utf-8")).hexdigest() if relative else None
        self.msi_child = None

    def public_failure(self, phase: str, error: Exception) -> dict:
        result = {"event": "installer_preflight_failed", "phase": phase, "stage": self.stage.value,
                  "reason": failure_reason(error)}
        if self.role is not None:
            result["role"] = self.role.value
        if self.relative_path_sha256 is not None:
            result["relative_path_sha256"] = self.relative_path_sha256
        if phase == "observe" and self.stage is Stage.INSTALL and self.role is Role.MSI and self.msi_child is not None:
            result["msi_child"] = self.msi_child
        return result


def read_msi_child_failure(path: Path) -> dict[str, str | int] | None:
    """Project one bounded typed MSI marker; never export the private log."""
    transport = module("export-accepted-rc")
    try:
        info = transport.ordinary(path)
        if not 0 < info.st_size <= MSI_CHILD_LOG_LIMIT:
            return None
        read = 0
        result = None
        with path.open("rb") as stream:
            while line := stream.readline(MSI_CHILD_LOG_LIMIT - read + 1):
                read += len(line)
                if read > MSI_CHILD_LOG_LIMIT:
                    return None
                if not line.startswith(MSI_CHILD_PREFIX):
                    continue
                if result is not None or not line.endswith(b"\n"):
                    return None
                value = transport.json_object(line[len(MSI_CHILD_PREFIX):], limit=MSI_CHILD_RECORD_LIMIT)
                keys = set(value)
                if keys not in ({"stage", "reason"}, {"stage", "reason", "msi_exit"}):
                    return None
                stage, reason = value["stage"], value["reason"]
                if type(stage) is not str or stage not in MSI_CHILD_STAGES or type(reason) is not str or reason not in MSI_CHILD_REASONS:
                    return None
                result = {"stage": stage, "reason": reason}
                if "msi_exit" in keys:
                    code = value["msi_exit"]
                    if reason != "MSI_EXIT" or type(code) is not int or not 1 <= code <= 65535:
                        return None
                    result["msi_exit"] = code
        after = transport.ordinary(path)
        if read != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            return None
        return result
    except (OSError, ValueError):
        return None


def context() -> tuple[dict, Path]:
    env = os.environ
    sha = env.get("INPUT_SOURCE_SHA", "")
    require(
        os.name == "nt" and re.fullmatch(r"[0-9a-f]{40}", sha) and sha == env.get("GITHUB_SHA")
        and env.get("GITHUB_REPOSITORY") == REPOSITORY and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_ACTIONS") == "true"
        and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and env.get("GITHUB_WORKFLOW_REF") == f"{REPOSITORY}/{WORKFLOW}@refs/heads/main"
        and all(re.fullmatch(r"[1-9][0-9]*", env.get(key, "")) for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")),
        "own-main exact-SHA hosted dispatch required",
    )
    source = module("generate_build_info")._release_source_identity(ROOT)
    version = source["source_version"]
    require(source["source_commit"] == sha and source["workspace_clean"] is True
            and re.fullmatch(r"(?:1[7-9]|[2-9][0-9]|[1-9][0-9]{2,})\.[0-9]+\.[0-9]+", version),
            "clean exact source of version 17 or later required")
    temp = Path(env.get("RUNNER_TEMP", ""))
    require(temp.is_absolute() and temp.is_dir(), "ordinary runner temp required")
    module("export-accepted-rc").no_links(temp)
    return source, temp / f"siyi-installer-preflight-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}"


def package_paths(version: str) -> dict[str, str]:
    return {"nsis": f"nsis/司忆_{version}_x64-setup.exe", "msi": f"msi/司忆_{version}_x64_zh-CN.msi"}


def package_files(root: Path, version: str) -> dict:
    transport = module("export-accepted-rc")
    result = {}
    for kind, name in package_paths(version).items():
        path = root / name
        size = transport.ordinary(path).st_size
        require(1024**2 <= size <= CAP and list(path.parent.iterdir()) == [path], "expected one bounded installer per kind")
        result[kind] = {"path": name, "bytes": size, "sha256": digest(path)}
    return result


def sidecar_secret_scan_view(data: bytes, aws_pattern: re.Pattern[bytes]) -> bytes:
    """Suppress only the public Pillow font's one false hit in a scan buffer."""
    if not data.startswith(b"MZ"):
        return data
    verified = []
    for match in aws_pattern.finditer(data):
        start = match.start() - PUBLIC_PILLOW_FONT_AWS_MATCH_OFFSET
        if start < 0 or start + PUBLIC_PILLOW_FONT_CONTEXT_BYTES > len(data):
            continue
        context = data[start:start + PUBLIC_PILLOW_FONT_CONTEXT_BYTES]
        if hashlib.sha256(context).hexdigest() == PUBLIC_PILLOW_FONT_CONTEXT_SHA256:
            verified.append(match.span())
    if len(verified) != 1:
        return data
    start, end = verified[0]
    return data[:start] + b"\0" * (end - start) + data[end:]


def has_unreviewed_vendor_path(lowered: bytes, marker: bytes) -> bool:
    prefix = PUBLIC_UPSTREAM_BUILD_PREFIXES.get(marker)
    if prefix is None:
        return marker in lowered
    start = lowered.find(marker)
    while start >= 0:
        if not lowered.startswith(prefix, start):
            return True
        start = lowered.find(marker, start + len(marker))
    return False


def scan_file(path: Path, name: str, blocked: tuple[bytes, ...] = ()) -> None:
    transport = module("export-accepted-rc")
    info = transport.ordinary(path)
    privacy = module("privacy_scan")
    resources = module("import-accepted-rc")
    public_name = "desktop/src-tauri/target/release/preflight/" + name
    tls = resources.public_tls_identity(public_name)
    notice = resources.public_notice_identity(public_name)
    require(not privacy._forbidden_path(name) or tls is not None, "private runtime filename")
    if tls or notice:
        resources.validate_public_resource(public_name, info.st_size, digest(path))
    if notice:
        data = path.read_bytes()
        require(not any(marker in data.lower() for marker in blocked), "build-machine path in candidate")
        transport._public_notice_content(public_name, data)
        return
    if name == "agent-backend.exe":
        require(info.st_size <= CAP, "sidecar exceeds scan byte bound")
        data = path.read_bytes()
        require(len(data) == info.st_size, "sidecar changed during scan")
        require(not any(marker in data.lower() for marker in blocked), "build-machine path in candidate")
        transport.public_content(name, sidecar_secret_scan_view(data, privacy.SECRET_PATTERNS["aws_access_key"]))
        return
    if name in OFFICIAL_VENDOR_MEMBERS and any(marker in PUBLIC_UPSTREAM_BUILD_PREFIXES for marker in blocked):
        size, expected_sha = OFFICIAL_VENDOR_MEMBERS[name]
        require(0 < size <= CAP and info.st_size == size, "official upstream resource identity differs")
        with path.open("rb") as stream:
            data = stream.read(size + 1)
        require(len(data) == size and hashlib.sha256(data).hexdigest() == expected_sha,
                "official upstream resource identity differs")
        after = transport.ordinary(path)
        require((info.st_size, info.st_mtime_ns, info.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino),
                "official upstream resource identity differs")
        # Only the reviewed registry-prefix occurrences of the exact shared
        # markers are public. Other needles and secret checks use original bytes.
        lowered = data.lower()
        require(not any(has_unreviewed_vendor_path(lowered, marker) for marker in blocked), "build-machine path in candidate")
        transport.public_content(name, data)
        return
    carry = b""
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            combined = carry + block
            require(not any(marker in combined.lower() for marker in blocked), "build-machine path in candidate")
            transport.public_content(name, combined, first=not carry)
            carry = combined[-4096:]
    if path.suffix.casefold() == ".zip":
        require(name == "_internal/base_library.zip", "unreviewed nested runtime archive")
        with zipfile.ZipFile(path) as library:
            transport.validate_library(library, time.monotonic())


def stage_bundle_inputs(portable: Path, target: Path, hashes: dict[str, str]) -> dict[Path, str]:
    """Give the bundler private files; Cargo's PE may alias deps via a hardlink."""
    transport = module("export-accepted-rc")
    transport.no_links(target)
    target.mkdir()
    release = target / "release"
    release.mkdir()
    result = {}
    for name in ("司忆.exe", "agent-backend.exe"):
        source = portable / name
        size = transport.ordinary(source).st_size
        require(0 < size <= CAP, "bundle input exceeds its byte bound")
        destination = release / name
        with source.open("rb") as origin, destination.open("xb") as output:
            shutil.copyfileobj(origin, output, length=1024**2)
        require(transport.ordinary(destination).st_size == size and digest(destination) == hashes[name],
                "private bundle input differs from candidate")
        result[destination] = hashes[name]
    return result


def baseline_asset(version: str, kind: str) -> dict:
    return (PATCH_BASELINE_ASSETS if version == "17.0.1" else PRIOR)[kind]


def build(source: dict, private: Path, diagnostics: Diagnostics) -> None:
    diagnostics.mark(Stage.PREPARE_BUILD)
    collector = module("collect-rc-desktop-candidate")
    transport = module("export-accepted-rc")
    private.mkdir()
    collector.disk_preflight(private, 8)
    require(not BUNDLE.exists(), "refusing stale bundle output")
    candidate = ROOT / "build/candidates" / private.name
    relative = candidate.relative_to(ROOT).as_posix()
    shell = collector.build_shell(private)
    diagnostics.mark(Stage.COMPILE)
    collector.execute([shell, "-NoProfile", "-NonInteractive", "-File", "scripts/build-desktop.ps1",
                       "-CandidateOnly", "-CandidateDirectory", relative], ROOT, private, "candidate-build")
    diagnostics.mark(Stage.BIND_BUILD)
    manifest = transport.read_object(candidate / "build-info.json")
    receipt = transport.read_object(candidate / "candidate-build.json")
    version = source["source_version"]
    build_id = manifest.get("build_id", "")
    require(manifest.get("product_version") == version and manifest.get("git_commit") == source["source_commit"]
            and manifest.get("source_fingerprint") == source["source_tree_fingerprint"]
            and manifest.get("workspace_state") == "CLEAN" and manifest.get("build_type") == "Release"
            and re.fullmatch(r"[0-9a-f]{24}", build_id)
            and receipt.get("actual_run") is True and receipt.get("status") == "BUILT"
            and receipt.get("source_commit") == source["source_commit"] and receipt.get("build_id") == build_id,
            "candidate source/build receipt mismatch")
    portable = candidate / "portable"
    hashes = {path: digest(portable / path) for path in ("司忆.exe", "agent-backend.exe", "_internal/build-info.json")}
    require(receipt.get("artifacts") == [{"path": "portable/" + path, "sha256": sha.upper()} for path, sha in hashes.items()]
            and digest(candidate / "build-info.json") == hashes["_internal/build-info.json"], "candidate receipt byte mismatch")
    cargo_desktop = BUNDLE.parent / "司忆.exe"
    sidecar = ROOT / "desktop/src-tauri/binaries/agent-backend-x86_64-pc-windows-msvc.exe"
    original = {cargo_desktop: hashes["司忆.exe"], sidecar: hashes["agent-backend.exe"]}
    for path in original:
        transport.no_links(path)
    require(all(digest(path) == sha for path, sha in original.items()), "candidate bundle source mismatch")
    desktop = portable / "司忆.exe"
    require(transport.ordinary(desktop).st_size <= CAP, "desktop marker input exceeds its bound")
    executable = desktop.read_bytes()
    require(hashlib.sha256(executable).hexdigest() == hashes["司忆.exe"], "portable desktop bytes changed")
    require(executable.count(TAURI_MARKER) == 1, "unbundled Tauri marker missing or ambiguous")
    desktop_sha = {kind: hashlib.sha256(executable.replace(TAURI_MARKER, marker, 1)).hexdigest()
                   for kind, marker in (("nsis", b"__TAURI_BUNDLE_TYPE_VAR_NSS"), ("msi", b"__TAURI_BUNDLE_TYPE_VAR_MSI"))}
    diagnostics.mark(Stage.INVENTORY)
    payload = module("rc_payload_inventory").inventory(portable, {"path": "agent-backend.exe", "sha256": hashes["agent-backend.exe"]})
    paths = (str(ROOT), *(os.environ.get(name, "") for name in ("USERPROFILE", "CARGO_HOME", "RUSTUP_HOME", "RUNNER_TEMP")))
    blocked = tuple(spelling.encode(encoding).lower() for path in paths if len(path) > 3
                    for spelling in {path, path.replace("\\", "/")} for encoding in ("utf-8", "utf-16-le"))
    for name in ("司忆.exe", "agent-backend.exe", *(entry["path"] for entry in payload["entries"])):
        role = Role.DESKTOP if name == "司忆.exe" else Role.SIDECAR if name == "agent-backend.exe" else Role.SUPPORT
        diagnostics.mark(Stage.SCAN_PAYLOAD, role, name)
        scan_file(portable / name, name, blocked)
    diagnostics.mark(Stage.STAGE_BUNDLE)
    bundle_target = private / "bundle-target"
    staged_inputs = stage_bundle_inputs(portable, bundle_target, hashes)
    bundle = bundle_target / "release" / "bundle"
    overlay = private / "bundle-inputs.json"
    overlay.write_text(json.dumps({"bundle": {"resources": {
        "binaries/_internal/": None, (portable / "_internal").as_posix() + "/": "_internal/",
    }}}), encoding="utf-8")
    env = collector.process_environment()
    env["CARGO_TARGET_DIR"] = str(bundle_target)
    diagnostics.mark(Stage.BUNDLE)
    collector.execute(["node", "frontend/node_modules/@tauri-apps/cli/tauri.js", "bundle", "--bundles", "nsis,msi",
                       "--config", str(candidate / "tauri-candidate.json"), "--config", str(overlay), "--ci"],
                      ROOT / "desktop", private, "candidate-bundle", environment=env, timeout=1800)
    diagnostics.mark(Stage.VERIFY_BUNDLES)
    require(source == context()[0] and all(digest(path) == sha for path, sha in original.items())
            and all(transport.ordinary(path).st_size and digest(path) == sha for path, sha in staged_inputs.items())
            and payload == module("rc_payload_inventory").inventory(portable, payload["binary"]),
            "source or bundle input changed")
    packages = package_files(bundle, version)
    checker = module("check-package-size")
    size = checker.size_decision(packages["nsis"]["bytes"], baseline_asset(version, "nsis")["size"])
    print(json.dumps({"installer_size": size, "version": version}, sort_keys=True))
    require(size["passed"], "NSIS size gate failed; no installer artifact is published")
    public = private / "public"
    public.mkdir()
    for kind, item in packages.items():
        name = item["path"]
        diagnostics.mark(Stage.SCAN_INSTALLER, Role(kind), name)
        scan_file(bundle / name, name, blocked)
        diagnostics.mark(Stage.STAGE_UPLOAD, Role(kind), name)
        target = public / name
        target.parent.mkdir()
        shutil.copyfile(bundle / name, target)
    diagnostics.mark(Stage.STAGE_UPLOAD, Role.DESCRIPTOR)
    require(package_files(public, version) == packages, "candidate changed during public staging")
    descriptor = public / "candidate.json"
    descriptor.write_text(json.dumps({"source": {**source, "build_id": build_id}, "packages": packages,
                                     "desktop_sha256": desktop_sha,
                                     "sidecar_sha256": hashes["agent-backend.exe"],
                                     "payload_sha256": payload["payload_content_sha256"]}), encoding="utf-8")
    scan_file(descriptor, "candidate.json", blocked)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"descriptor_sha={digest(descriptor)}\npublic_path={public.as_posix()}\n")


def extract(archive: Path, destination: Path, source: dict, descriptor_sha: str) -> dict:
    transport = module("export-accepted-rc")
    names = package_paths(source["source_version"])
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        expected = set(names.values()) | {"candidate.json"}
        require(len(members) == len(expected) and {row.filename for row in members} == expected
                and all(not row.is_dir() and not stat.S_ISLNK(row.external_attr >> 16)
                        and not (row.flag_bits & 1) for row in members), "unexpected candidate ZIP member")
        info = zipped.getinfo("candidate.json")
        require(0 < info.file_size <= 64 * 1024, "invalid candidate descriptor bound")
        with zipped.open(info) as member:
            value = member.read(64 * 1024 + 1)
        require(hashlib.sha256(value).hexdigest() == descriptor_sha, "candidate descriptor differs from producer")
        descriptor = transport.json_object(value, limit=64 * 1024)
        bound = descriptor["source"]
        require({k: v for k, v in bound.items() if k != "build_id"} == source
                and re.fullmatch(r"[0-9a-f]{24}", bound.get("build_id", "")), "descriptor source differs")
        for kind, name in names.items():
            item = descriptor["packages"][kind]
            member = zipped.getinfo(name)
            size = member.file_size
            require(item["path"] == name and item["bytes"] == size and 1024**2 <= size <= CAP
                    and SHA256.fullmatch(item["sha256"]), "invalid candidate entry")
            target = destination / name
            transport.no_links(target)
            target.parent.mkdir(parents=True, exist_ok=False)
            with zipped.open(member) as origin, target.open("xb") as output:
                shutil.copyfileobj(origin, output, length=1024**2)
            require(target.stat().st_size == size and digest(target) == item["sha256"], "candidate bytes differ")
        return descriptor


def patch_asset_metadata(spec: dict) -> dict:
    repo = f"/repos/{REPOSITORY}"
    release = api(f"{repo}/releases/tags/{PATCH_BASELINE_TAG}")
    ref = f"refs/tags/{PATCH_BASELINE_TAG}"
    tag = api(f"{repo}/git/ref/tags/{PATCH_BASELINE_TAG}")
    commit = PATCH_BASELINE_BUILD["git_commit"]
    require(release.get("id") == PATCH_BASELINE_RELEASE_ID and release.get("tag_name") == PATCH_BASELINE_TAG
            and release.get("target_commitish") == commit and release.get("draft") is False
            and release.get("prerelease") is False and tag.get("ref") == ref
            and tag.get("object") == {"type": "commit", "sha": commit,
                                      "url": f"https://api.github.com{repo}/git/commits/{commit}"},
            "pinned patch baseline release or tag changed")
    endpoint = f"{repo}/releases/assets/{spec['id']}"
    expected = {"id": spec["id"], "name": spec["name"], "size": spec["size"], "digest": f"sha256:{spec['sha256']}",
                "state": "uploaded", "url": f"https://api.github.com{endpoint}",
                "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/{PATCH_BASELINE_TAG}/{spec['name']}"}
    meta = api(endpoint)
    assets = release.get("assets")
    require(isinstance(assets, list) and all(isinstance(row, dict) for row in assets), "pinned patch baseline asset changed")
    matches = [row for row in assets if row.get("id") == spec["id"] or row.get("name") == spec["name"]]
    fields = (*expected, "created_at", "updated_at")
    identity = {key: meta.get(key) for key in fields}
    require(all(identity[key] == value for key, value in expected.items())
            and all(isinstance(identity[key], str) and identity[key] for key in ("created_at", "updated_at"))
            and len(matches) == 1 and identity == {key: matches[0].get(key) for key in fields},
            "pinned patch baseline asset changed")
    return identity


def receive_previous(version: str, kind: str) -> Path:
    if version != "17.0.1":
        return download_prior(kind)[0]
    spec = baseline_asset(version, kind)
    identity = patch_asset_metadata(spec)
    output = ROOT / "build/upgrade-baseline" / spec["name"]
    token = os.environ.get("GH_TOKEN")
    require(bool(token), "read-only Actions token required for patch baseline")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/octet-stream", "Accept-Encoding": "identity",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Siyi-Desktop-Candidate"}
    transport = module("export-accepted-rc")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), transport.AssetRedirects())
    transport.download_exact(opener, urllib.request.Request(identity["url"], headers=headers),
                             spec["size"], spec["sha256"], output)
    require(identity == patch_asset_metadata(spec), "pinned patch baseline drifted during download")
    return output


def verify_patch_baseline_report(report: dict, kind: str) -> dict:
    spec = PATCH_BASELINE_ASSETS[kind]
    previous = report["artifacts"]["previous"]
    manifest = report["artifacts"]["previous_build_manifest"]
    require(previous.get("name") == spec["name"] and previous.get("version") == PATCH_BASELINE_BUILD["product_version"]
            and type(previous.get("bytes")) is int and previous["bytes"] == spec["size"]
            and str(previous.get("sha256")).lower() == spec["sha256"]
            and all(manifest.get(key) == value for key, value in PATCH_BASELINE_BUILD.items()),
            "installed previous build does not match pinned patch baseline")
    return {"tag": PATCH_BASELINE_TAG, "release_id": PATCH_BASELINE_RELEASE_ID, "asset_id": spec["id"],
            "name": spec["name"], "bytes": spec["size"], "sha256": spec["sha256"],
            "commit": PATCH_BASELINE_BUILD["git_commit"], "build_id": PATCH_BASELINE_BUILD["build_id"],
            "source_fingerprint": PATCH_BASELINE_BUILD["source_fingerprint"]}


def receive(source: dict, private: Path, kind: str, artifact_id: int, descriptor_sha: str, diagnostics: Diagnostics) -> None:
    diagnostics.mark(Stage.RECEIVE, Role(kind))
    env = os.environ
    require(artifact_id > 0 and SHA256.fullmatch(descriptor_sha), "immutable candidate artifact receipt required")
    private.mkdir()
    endpoint = f"/repos/{REPOSITORY}/actions/artifacts/{artifact_id}"
    info = api(endpoint)
    run = info.get("workflow_run", {})
    sha = str(info.get("digest", "")).removeprefix("sha256:")
    size = info.get("size_in_bytes", 0)
    require(info.get("id") == artifact_id and info.get("expired") is False and SHA256.fullmatch(sha)
            and info.get("name") == f"installer-preflight-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}"
            and run.get("id") == int(env["GITHUB_RUN_ID"]) and run.get("head_sha") == source["source_commit"]
            and run.get("head_branch") == "main" and type(size) is int and 0 < size < 2 * CAP + 1024**2,
            "candidate artifact provenance mismatch")
    transport = module("export-accepted-rc")
    archive = private / "candidate.zip"
    diagnostics.mark(Stage.DOWNLOAD, Role(kind))
    transport.download_action_artifact(REPOSITORY, artifact_id, sha, size, archive)
    require(info == api(endpoint) and not BUNDLE.exists(), "candidate metadata drift or stale package output")
    diagnostics.mark(Stage.EXTRACT, Role(kind))
    descriptor = extract(archive, BUNDLE, source, descriptor_sha)
    require(package_files(BUNDLE, source["source_version"]) == descriptor["packages"], "candidate transport differs")
    diagnostics.mark(Stage.BASELINE, Role(kind))
    previous = receive_previous(source["source_version"], kind)
    diagnostics.mark(Stage.RECORD_RECEIVE, Role(kind))
    (private / "received.json").write_text(json.dumps({"descriptor": descriptor, "previous": reference(previous)}), encoding="utf-8")


def write_public_summary(private: Path, summary: dict) -> None:
    # Keep the artifact and its privacy scan on UTF-8; redirected Windows
    # stdout may be a legacy code page and cannot always print package names.
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True).encode("utf-8")
    module("export-accepted-rc").public_content("summary.json", encoded)
    (private / "summary.json").write_bytes(encoded)
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))


def observe(source: dict, private: Path, kind: str, diagnostics: Diagnostics) -> None:
    diagnostics.mark(Stage.BIND_INSTALL, Role(kind))
    collector = module("collect-rc-desktop-candidate")
    transport = module("export-accepted-rc")
    received = transport.read_object(private / "received.json")
    descriptor = received["descriptor"]
    previous = ROOT / received["previous"]["path"]
    if source["source_version"] == "17.0.1":
        spec = PATCH_BASELINE_ASSETS[kind]
        require(previous == ROOT / "build/upgrade-baseline" / spec["name"],
                "received previous installer does not match pinned patch baseline")
        transport.ordinary(previous)
        require(previous.stat().st_size == spec["size"] and received["previous"]["sha256"] == spec["sha256"],
                "received previous installer does not match pinned patch baseline")
    require("GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ
            and package_files(BUNDLE, source["source_version"]) == descriptor["packages"]
            and digest(previous) == received["previous"]["sha256"], "observed input hashes or credentials invalid")
    diagnostics.mark(Stage.HOST, Role(kind))
    collector.native_desktop()
    lifecycle = module("rc_installed_lifecycle")
    host = lifecycle.WindowsAdapter(ROOT, private / "host-preflight").host_ps("host_observation")
    require(host.get("is_admin") is True and host.get("installer_busy") is False and host.get("webviews")
            and all(host.get(key) == [] for key in ("installations", "related_products", "install_paths", "shortcuts", "running")),
            "fresh desktop has existing software, installer activity, or missing prerequisites")
    env = module("record-rc-desktop-startup").desktop_environment(dict(os.environ), private / "isolated-env", private.name)
    # This is the generic fixture migration/installed-start smoke, not the RC render-ready protocol.
    env.pop("SIYI_DESKTOP_ACCEPTANCE", None)
    env.pop("SIYI_DESKTOP_ACCEPTANCE_NONCE", None)
    env["SIYI_RELEASE_EVIDENCE_SOURCE_IDENTITY"] = json.dumps(descriptor["source"])
    if kind == "msi":
        env["SIYI_HOSTED_PREFLIGHT_TYPED_MSI"] = "1"
    evidence = ROOT / "build" / f"v{source['source_version'].replace('.', '')}-evidence" / f"{kind}-installer-smoke.json"
    require(not evidence.exists(), "refusing to overwrite installer smoke evidence")
    script = "smoke-installer.ps1" if kind == "nsis" else "smoke-msi.ps1"
    diagnostics.mark(Stage.INSTALL, Role(kind))
    try:
        collector.execute([collector.build_shell(private), "-NoProfile", "-NonInteractive", "-File", str(ROOT / "scripts" / script),
                           "-BundleDirectory", str(BUNDLE), "-PreviousInstaller", str(previous), "-Output", str(evidence)],
                          ROOT, private, "installed-smoke", environment=env, timeout=1800)
    except Exception as error:
        if kind == "msi" and failure_reason(error) == "SUBPROCESS_FAILED":
            diagnostics.msi_child = read_msi_child_failure(private / "installed-smoke.log")
        raise
    diagnostics.mark(Stage.VERIFY_INSTALL, Role(kind))
    report = transport.read_object(evidence)
    module("check-release-evidence")._validate_one(report, root=ROOT, version=source["source_version"],
                                                  head=source["source_commit"], kind=kind.upper())
    patch_baseline = (verify_patch_baseline_report(report, kind) if source["source_version"] == "17.0.1" else None)
    require(report.get("binary_sha256") == {"desktop": descriptor["desktop_sha256"][kind],
                                           "sidecar": descriptor["sidecar_sha256"]}
            and report.get("sidecar_payload_sha256") == descriptor["payload_sha256"] and source == context()[0]
            and report.get("operator_acceptance") is None, "installed payload differs or manual proof was claimed")
    diagnostics.mark(Stage.SUMMARY, Role(kind))
    summary = {"status": "PREFLIGHT_PASS", "actual_run": True, "manual_acceptance": "NOT_RUN", "released": False,
               "source": descriptor["source"], "kind": kind, "installer": descriptor["packages"][kind],
               "checks": sorted(report["checks"])}
    if patch_baseline is not None:
        summary["previous"] = patch_baseline
    write_public_summary(private, summary)


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "receive", "observe"))
    parser.add_argument("--kind", choices=KINDS)
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--descriptor-sha")
    diagnostics = Diagnostics()
    phase = "unknown"
    try:
        args = parser.parse_args(argv)
        phase = args.phase
        source, private = context()
        if args.phase == "build":
            build(source, private, diagnostics)
        else:
            require(args.kind is not None, "one installer kind is required")
            if args.phase == "receive":
                require(args.artifact_id is not None and args.descriptor_sha is not None, "producer receipt is required")
                receive(source, private, args.kind, args.artifact_id, args.descriptor_sha, diagnostics)
            else:
                observe(source, private, args.kind, diagnostics)
    except Exception as error:
        print(json.dumps(diagnostics.public_failure(phase, error), sort_keys=True), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
