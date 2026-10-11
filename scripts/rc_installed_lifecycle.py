"""Fail-closed, retained Windows installer lifecycle orchestration.

No installer, application, elevation or filesystem cleanup occurs on import.
Planning and synthetic adapters are never installation evidence. The production
dispatcher uses exact retained native Job/process handles, never PID cleanup.
"""
from __future__ import annotations

import base64
import ctypes
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any
import uuid

from rc_owned_desktop import OwnedDesktopJob, ordinary, write_once
from rc_owned_desktop import rotate_launch_marker

VERSION = "16.0.0"
FAMILY = "{F769324D-235D-532C-995A-C14A256F4067}"
HASH = re.compile(r"[0-9a-f]{64}")
GUID = re.compile(r"\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}")
STAGES = ("install_previous", "launch_previous", "seed_fixture", "upgrade_current",
          "launch_upgraded", "uninstall_current", "verify_retention", "reinstall_current",
          "launch_reinstalled", "final_uninstall", "verify_final")
MSI_BOOTSTRAP_TARGET = (
    r'powershell.exe -NoProfile -windowstyle hidden try [\{] [\[]Net.ServicePointManager[\]]::SecurityProtocol = '
    r'[\[]Net.SecurityProtocolType[\]]::Tls12 [\}] catch [\{][\}]; Invoke-WebRequest -Uri '
    r'"https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile "$env:TEMP\MicrosoftEdgeWebview2Setup.exe" ; '
    r"""Start-Process -FilePath "$env:TEMP\MicrosoftEdgeWebview2Setup.exe" -ArgumentList ('/silent', '/install') -Wait"""
)


class SafetyError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SafetyError(message)


def load_module(root: Path, name: str):
    spec = importlib.util.spec_from_file_location("installed_" + name.replace("-", "_"), root / "scripts" / (name + ".py"))
    require(spec is not None and spec.loader is not None, "required collector module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def key(path: str | Path) -> str:
    return ntpath.normcase(ntpath.normpath(str(path))).rstrip("\\/")


def _install_location_key(value: object) -> str:
    """Compare a registry InstallLocation without altering its raw observation."""
    require(isinstance(value, str) and bool(value), "typed nonempty InstallLocation required")
    if '"' in value:
        require(value.startswith('"') and value.endswith('"') and value.count('"') == 2,
                "InstallLocation allows only one complete outer quote pair")
        value = value[1:-1]
    require(bool(value) and ntpath.isabs(value) and ".." not in re.split(r"[\\/]", value),
            "absolute nonescaping InstallLocation required")
    return key(value)


class WindowsGuardHandles:
    """Exact handles protect known desktop folders without owning user files."""
    def __init__(self):
        require(os.name == "nt", "desktop guard requires native Windows handles")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        self.kernel.CreateFileW.restype = ctypes.c_void_p
        self.kernel.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        self.kernel.WriteFile.restype = ctypes.c_int
        self.kernel.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        self.kernel.FlushFileBuffers.restype = ctypes.c_int
        self.kernel.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint]
        self.kernel.SetFilePointerEx.restype = ctypes.c_int
        self.kernel.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        self.kernel.ReadFile.restype = ctypes.c_int
        self.kernel.SetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        self.kernel.SetFileInformationByHandle.restype = ctypes.c_int
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int

    def _open(self, path: Path, access: int, sharing: int, disposition: int, flags: int):
        handle = self.kernel.CreateFileW(str(path), access, sharing, None, disposition, flags, None)
        require(handle not in {None, ctypes.c_void_p(-1).value}, "exact desktop guard handle could not be retained")
        return handle

    def open_directory(self, directory: Path):
        # Deny delete-sharing so the directory cannot be replaced or removed.
        return self._open(directory, 0x80, 0x3, 3, 0x02000000)

    def create_sentinel(self, path: Path, payload: bytes):
        # READ/WRITE/DELETE access; allow only external read-sharing. The owned
        # file cannot be overwritten/deleted while its exact handle is retained.
        handle = self._open(path, 0xC0010000, 0x1, 1, 0x80)
        try:
            written = ctypes.c_uint()
            data = ctypes.create_string_buffer(payload)
            require(self.kernel.WriteFile(handle, data, len(payload), ctypes.byref(written), None)
                    and written.value == len(payload) and self.kernel.FlushFileBuffers(handle),
                    "desktop guard sentinel could not be durably written")
        except BaseException:
            self.close(handle)
            raise
        return handle

    def remove_exact_file(self, handle):
        # Mark only this already-open file for deletion; no path-based unlink.
        disposition = ctypes.c_int(1)
        require(self.kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)),
                "exact created desktop sentinel could not be marked for removal")

    def read_sentinel(self, handle, limit: int) -> bytes:
        require(self.kernel.SetFilePointerEx(handle, 0, None, 0), "retained desktop sentinel seek failed")
        data, count = ctypes.create_string_buffer(limit), ctypes.c_uint()
        require(self.kernel.ReadFile(handle, data, limit, ctypes.byref(count), None), "retained desktop sentinel read failed")
        return data.raw[:count.value]

    def close(self, handle):
        require(self.kernel.CloseHandle(handle), "desktop guard handle close failed")


class DesktopDirectoryGuard:
    """Keep a known desktop nonempty; never delete its directory or user files."""
    def __init__(self, directory: Path, owner_run_id: str, *, native=None):
        require(isinstance(owner_run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner_run_id) is not None,
                "bounded owned desktop guard nonce required")
        ordinary(directory)
        require(directory.is_absolute() and directory.is_dir(), "ordinary actual known desktop directory required")
        self.directory, self.owner_run_id = directory, owner_run_id
        self.native = native if native is not None else WindowsGuardHandles()
        self.directory_handle = self.native.open_directory(directory)
        stat = directory.stat()
        self.directory_identity = (stat.st_dev, stat.st_ino)
        self.path = directory / ("siyi-installed-guard-" + owner_run_id + ".tmp")
        self.payload = ("owned-installed-desktop-guard-v1:" + owner_run_id).encode("ascii")
        self.file_handle = None
        try:
            ordinary(self.path)
            require(not self.path.exists(), "desktop sentinel must be exclusively created")
            self.file_handle = self.native.create_sentinel(self.path, self.payload)
            stat = self.path.stat()
            self.file_identity = (stat.st_dev, stat.st_ino)
            self.verify()
        except BaseException:
            # Existing/partially created file is never deleted on failure.
            self.close_retaining()
            raise

    def verify(self) -> None:
        ordinary(self.directory)
        ordinary(self.path)
        require(self.directory_handle is not None and self.file_handle is not None, "desktop guard handles were lost")
        directory, sentinel = self.directory.stat(), self.path.stat()
        require((directory.st_dev, directory.st_ino) == self.directory_identity
                and (sentinel.st_dev, sentinel.st_ino) == self.file_identity
                and sentinel.st_size == len(self.payload)
                and self.native.read_sentinel(self.file_handle, len(self.payload) + 1) == self.payload,
                "actual desktop directory or exact owned sentinel identity changed")

    def private_record(self) -> dict:
        return {"owner_run_id": self.owner_run_id, "known_desktop_directory": str(self.directory),
                "owned_sentinel": str(self.path), "directory_identity": list(self.directory_identity),
                "sentinel_sha256": hashlib.sha256(self.payload).hexdigest(), "retained_exact_handles": True}

    def release_success(self) -> None:
        self.verify()
        self.native.remove_exact_file(self.file_handle)
        self.native.close(self.file_handle)
        self.file_handle = None
        ordinary(self.path)
        require(not self.path.exists(), "exact created desktop sentinel was not removed")
        current = self.directory.stat()
        require((current.st_dev, current.st_ino) == self.directory_identity, "desktop directory identity was not preserved")
        self.native.close(self.directory_handle)
        self.directory_handle = None

    def close_retaining(self) -> None:
        for name in ("file_handle", "directory_handle"):
            handle = getattr(self, name, None)
            if handle is not None:
                self.native.close(handle)
                setattr(self, name, None)


def native_system_directory() -> Path:
    require(os.name == "nt", "actual native Windows system directory required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    require(0 < length < len(buffer), "native system directory query failed")
    result = Path(buffer.value)
    ordinary(result)
    require(result.is_absolute() and result.is_dir(), "ordinary native system directory required")
    return result


def sha(path: Path) -> str:
    ordinary(path)
    require(path.is_file(), "ordinary artifact file required")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    require((before.st_size, before.st_ino, before.st_mtime_ns) ==
            (after.st_size, after.st_ino, after.st_mtime_ns), "artifact changed during hashing")
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    ordinary(path)
    require(path.is_file() and path.stat().st_size <= 32 * 1024 * 1024, "bounded ordinary JSON required")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), "JSON object required")
    return value


def approved_build_argv(command: Any) -> bool:
    """Only a direct reviewed entry point, not an approved name buried in argv."""
    if not (isinstance(command, list) and command and all(isinstance(item, str) and item for item in command)):
        return False
    if any(item.lower() in {"-command", "/c", "-c", "-encodedcommand"} for item in command):
        return False
    first = ntpath.basename(command[0]).lower()
    if first in {"powershell.exe", "pwsh.exe"}:
        files = [index for index, item in enumerate(command) if item.lower() == "-file"]
        return (len(files) == 1 and files[0] + 1 < len(command)
                and ntpath.basename(command[files[0] + 1]).lower() == "build-desktop.ps1")
    if first in {"tauri", "tauri.exe"}:
        return len(command) >= 2 and command[1] == "build"
    if first in {"node", "node.exe"}:
        return len(command) >= 3 and ntpath.basename(command[1]).lower() == "tauri.js" and command[2] == "build"
    return False


def validate_msi_metadata(value: dict) -> dict:
    """Narrow static mutation policy; unreviewed standard tables also block.

    The Windows Installer service is not owned by the client Job. This review
    therefore limits package-authored mutation before exact registration checks;
    it does not claim system-wide Windows Installer service process ownership.
    """
    tables = value.get("tables")
    allowed_tables = {"_Validation", "Property", "Directory", "Component", "File", "MsiFileHash", "Media",
        "Feature", "FeatureComponents", "Registry", "Shortcut", "MsiShortcutProperty", "CreateFolder", "RemoveFile", "Upgrade",
        "CustomAction", "InstallExecuteSequence", "InstallUISequence", "AdminExecuteSequence", "AdminUISequence",
        "AdvtExecuteSequence", "AppSearch", "RegLocator", "Signature", "LaunchCondition", "Condition",
        "Control", "ControlCondition", "ControlEvent", "Dialog", "Error", "UIText", "TextStyle", "RadioButton",
        "ListBox", "ListView", "CheckBox", "BBControl", "Billboard", "Binary", "Icon", "ActionText", "EventMapping"}
    require(value.get("database_open_mode") == 0 and isinstance(tables, list) and tables
            and all(isinstance(name, str) for name in tables) and len(set(tables)) == len(tables)
            and set(tables).issubset(allowed_tables), "unreviewed MSI table or incomplete readonly inventory")
    properties, actions = value.get("properties"), value.get("custom_actions")
    require(isinstance(properties, dict) and isinstance(actions, list), "complete MSI properties and actions required")
    allowed_actions = {("SetARPINSTALLLOCATION", 51, "ARPINSTALLLOCATION", "[INSTALLDIR]"),
                       ("SetARPNOMODIFY", 51, "ARPNOMODIFY", "1"),
                       ("LaunchApplication", 210, "Path", "[LAUNCHAPPARGS]"),
                       ("WixUIValidatePath", 65, "WixUIWixca", "ValidatePath"),
                       ("WixUIPrintEula", 65, "WixUIWixca", "PrintEula"),
                       ("DownloadAndInvokeBootstrapper", 1058, "INSTALLDIR", MSI_BOOTSTRAP_TARGET)}
    require(all(isinstance(row, dict) and type(row.get("type")) is int
                and (row.get("name"), row["type"], row.get("source"), row.get("target")) in allowed_actions for row in actions),
            "unknown or executable MSI CustomAction requires a separately reviewed package")
    sequence = value.get("execute_sequence")
    require(isinstance(sequence, list) and len(sequence) <= 32768 and all(isinstance(row, dict)
            and isinstance(row.get("action"), str) and isinstance(row.get("condition"), str)
            and type(row.get("sequence")) is int for row in sequence), "complete typed MSI execution schedule required")
    require(len({row["action"] for row in sequence}) == len(sequence), "duplicate MSI execution action")
    action_names = {row["name"] for row in actions}
    require(len(action_names) == len(actions), "duplicate authored MSI action")
    executable_conditions = {"LaunchApplication": "AUTOLAUNCHAPP AND NOT Installed",
                             "DownloadAndInvokeBootstrapper": "NOT(REMOVE OR INSTALLED_WEBVIEW2_VERSION)"}
    for row in sequence:
        if row["action"] in action_names:
            require(row["action"] not in {"WixUIValidatePath", "WixUIPrintEula"}
                    and (row["action"] not in executable_conditions or row["condition"] == executable_conditions[row["action"]]),
                    "MSI executable action is not disabled by the reviewed silent-install conditions")
    require("AUTOLAUNCHAPP" not in properties, "MSI default auto-launch is forbidden")
    for action_name in action_names.intersection(executable_conditions):
        require(any(row["action"] == action_name for row in sequence), "authored executable action lacks its actual reviewed schedule")
    rows = {}
    for name in ("directories", "components", "files", "registry", "shortcuts", "remove_files", "media", "upgrades", "reg_locators", "app_search", "signatures"):
        rows[name] = value.get(name)
        require(isinstance(rows[name], list) and len(rows[name]) <= 32768
                and all(isinstance(row, dict) for row in rows[name]), "complete bounded MSI table rows required: " + name)
    create_folders = value.get("create_folders", [] if "CreateFolder" not in tables else None)
    require(isinstance(create_folders, list) and len(create_folders) <= 32768
            and all(isinstance(row, dict) and set(row) == {"directory", "component"}
                    and all(isinstance(row[name], str) and 0 < len(row[name]) <= 72
                            for name in ("directory", "component")) for row in create_folders),
            "complete typed bounded MSI CreateFolder inventory required")
    require("CreateFolder" in tables or not create_folders, "MSI CreateFolder rows exist without their table")
    directories = {row.get("id"): row for row in rows["directories"]}
    components = {row.get("id"): row for row in rows["components"]}
    require(len(directories) == len(rows["directories"]) and len(components) == len(rows["components"])
            and "INSTALLDIR" in directories and directories["INSTALLDIR"].get("name") == "司忆",
            "unique actual INSTALLDIR and components required")
    roots = {"TARGETDIR": ("", "SourceDir"), "ProgramFiles64Folder": ("TARGETDIR", "PFiles"),
             "INSTALLDIR": ("ProgramFiles64Folder", "司忆")}
    special_roots = {"DesktopFolder": ("TARGETDIR", "Desktop"), "ProgramMenuFolder": ("TARGETDIR", "."),
                     "ApplicationProgramsFolder": ("ProgramMenuFolder", "司忆")}
    for identifier, expected in {**roots, **special_roots}.items():
        row = directories.get(identifier)
        require((row is not None or identifier in special_roots)
                and (row is None or ((row.get("parent"), row.get("name")) == expected
                     and (not expected[0] or expected[0] in directories))),
                "MSI standard/product directory identity differs from reviewed namespace")
    require(not set(properties).intersection(directories), "MSI Property overrides a reviewed directory namespace")
    def owned_directory(identifier):
        seen = set()
        while identifier != "INSTALLDIR":
            if identifier in seen or identifier not in directories:
                return False
            seen.add(identifier)
            row = directories[identifier]
            name = str(row.get("name", "")).split("|", 1)[-1]
            if not name or name in {".", ".."} or any(character in name for character in "\\/:[]"):
                return False
            identifier = row.get("parent")
        return True
    special = {"ApplicationShortcutDesktop": "DesktopFolder", "ApplicationShortcut": "ApplicationProgramsFolder"}
    require(all(isinstance(identifier, str) and (owned_directory(row.get("directory"))
                or (special.get(identifier) == row.get("directory") and row.get("directory") in directories))
                for identifier, row in components.items()),
            "MSI component escapes owned installation or exact product shortcuts")
    seen_create_folders = set()
    for row in create_folders:
        directory, component = row["directory"], row["component"]
        identity = (directory, component)
        require(identity not in seen_create_folders, "duplicate MSI CreateFolder row")
        seen_create_folders.add(identity)
        require(component in components and directory in directories
                and components[component].get("directory") == directory
                and (owned_directory(directory) or special.get(component) == directory),
                "MSI CreateFolder escapes its exact owned component directory")
    def literal_file_name(value):
        # File.FileName is MSI Filename, not Formatted. Brackets are literal in
        # its long name only; Directory/Registry/property policies stay separate.
        if not isinstance(value, str) or not value or len(value) > 268 or value.count("|") > 1:
            return False
        names = value.split("|")
        devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        devices.update(prefix + suffix for prefix in ("COM", "LPT") for suffix in "123456789¹²³")
        for name in names:
            if not name or name in {".", ".."} or name.endswith((".", " ")):
                return False
            if any(character in '\\/:?*"<>|' or ord(character) < 32 or ord(character) == 127 for character in name):
                return False
            if name.split(".", 1)[0].rstrip(" ").upper() in devices:
                return False
            try:
                if len(name.encode("utf-16-le")) > 510:
                    return False
            except UnicodeEncodeError:
                return False
        short = names[0]
        if any(character in " +,;=[]" for character in short):
            return False
        parts = short.split(".")
        return (len(parts) <= 2 and bool(parts[0]) and len(parts[0].encode("utf-16-le")) <= 16
                and (len(parts) == 1 or len(parts[1].encode("utf-16-le")) <= 6))

    for row in rows["files"]:
        component = components.get(row.get("component"), {})
        require(component and owned_directory(component.get("directory")) and literal_file_name(row.get("name")),
                "MSI file escapes owned install tree or has an invalid literal Filename")
    for row in rows["registry"]:
        require(row.get("component") in components and type(row.get("root")) is int and row["root"] == 1
                and row.get("key", "").casefold() == r"software\github\司忆"
                and row.get("name") in {"InstallDir", "Desktop Shortcut", "Uninstaller Shortcut", "Start Menu Shortcut"}
                and row.get("value") in {"[INSTALLDIR]", "#1"}, "MSI registry mutation is outside exact product namespace")
    for row in rows["shortcuts"]:
        require(row.get("component") in components and row.get("name", "").split("|", 1)[-1] in {"司忆", "Uninstall 司忆"}
                and row.get("directory") in directories
                and (owned_directory(row.get("directory")) or row.get("directory") in {"DesktopFolder", "ApplicationProgramsFolder"})
                and ((row.get("target") == "[!Path]" and row.get("arguments") == "")
                     or (row.get("target") == "[System64Folder]msiexec.exe" and row.get("arguments", "").upper() == "/X [PRODUCTCODE]")),
                "MSI shortcut target/name/namespace is not reviewed")
        if row["target"] == "[!Path]":
            require(any(file.get("id") == "Path" and file.get("component") == "Path"
                        and file.get("name", "").split("|", 1)[-1] == "司忆.exe" for file in rows["files"]),
                    "MSI application shortcut is not bound to the actual desktop File/component")
    for row in rows["remove_files"]:
        require(row.get("component") in components and row.get("filename") == ""
                and (owned_directory(row.get("directory")) or row.get("directory") == "ApplicationProgramsFolder"
                     or (row.get("directory") == "DesktopFolder" and row.get("component") == "ApplicationShortcutDesktop"))
                and row.get("mode") == 2, "MSI remove action may touch a non-owned directory or wildcard file")
    require(rows["media"] and all(isinstance(row.get("cabinet"), str) and row["cabinet"].startswith("#")
            and not row.get("source") for row in rows["media"]), "embedded-only MSI media required")
    require(all(row.get("upgrade_code", "").upper() == FAMILY for row in rows["upgrades"]), "MSI upgrades another product family")
    for row in rows["reg_locators"]:
        expected = (row.get("root"), row.get("key", "").casefold(), row.get("name"))
        product = expected in {(1, r"software\github\司忆", ""), (1, r"software\github\司忆", "InstallDir")}
        webview = (row.get("root") in {1, 2} and row.get("name") == "pv"
                   and row.get("key", "").casefold() in {
                       r"software\microsoft\edgeupdate\clients\{f3017226-fe2a-4295-8bdf-00c3a9a7e4c5}",
                       r"software\wow6432node\microsoft\edgeupdate\clients\{f3017226-fe2a-4295-8bdf-00c3a9a7e4c5}"})
        require(product or webview, "MSI registry search would read an unreviewed namespace")
    approved_searches = {("INSTALLDIR", "PrevInstallDirNoName"), ("INSTALLDIR", "PrevInstallDirWithName"),
        ("INSTALLED_WEBVIEW2_VERSION", "Webview2VersionSystemx64"),
        ("INSTALLED_WEBVIEW2_VERSION", "Webview2VersionSystemx86"), ("INSTALLED_WEBVIEW2_VERSION", "Webview2VersionUser")}
    locator_ids = {row.get("id") for row in rows["reg_locators"]}
    require(len(locator_ids) == len(rows["reg_locators"]) and not rows["signatures"]
            and all((row.get("property"), row.get("signature")) in approved_searches
                    and row["signature"] in locator_ids for row in rows["app_search"]),
            "MSI AppSearch/file search may override or read an unreviewed namespace")
    for row in rows["app_search"]:
        locator = next(item for item in rows["reg_locators"] if item["id"] == row["signature"])
        require((row["property"] == "INSTALLDIR" and locator.get("key", "").casefold() == r"software\github\司忆")
                or (row["property"] == "INSTALLED_WEBVIEW2_VERSION" and locator.get("name") == "pv"),
                "MSI reviewed registry values flow to another namespace")
    if "DownloadAndInvokeBootstrapper" in action_names:
        required_webview_searches = {"Webview2VersionSystemx64", "Webview2VersionSystemx86", "Webview2VersionUser"}
        require(required_webview_searches.issubset({row["signature"] for row in rows["app_search"]
                 if row["property"] == "INSTALLED_WEBVIEW2_VERSION"}),
                "conditional MSI bootstrap requires all actual host WebView2 searches")
        bootstrap = next(row for row in sequence if row["action"] == "DownloadAndInvokeBootstrapper")
        require(any(row["action"] == "AppSearch" and row["condition"] == ""
                    and 0 < row["sequence"] < bootstrap["sequence"] for row in sequence),
                "actual MSI WebView2 detection must precede the conditional bootstrap")
    return properties


def validate_build_audit(root: Path, package: Artifact, manifest: dict, reference: dict | None) -> tuple[Artifact, ...]:
    """Optionally cross-bind an existing build audit; never require rebuilding old packages.

    This is integrity/provenance checking, not a cryptographic signature. A
    malicious same-user writer who forges every source and receipt is outside
    this collector's threat model, as with the existing RC bundle contract.
    """
    if reference is None:
        return ()
    audit_file = artifact(root, reference)
    audit = read_json(audit_file.path)
    required_source = {"source_version": manifest.get("product_version"), "source_commit": manifest.get("git_commit"),
                       "workspace_clean": True, "source_tree_fingerprint": manifest.get("source_fingerprint")}
    require(audit.get("schema_version") == 1 and audit.get("report_type") == "installer_build_audit"
            and audit.get("protocol") == "no-bootstrap-build-v1" and audit.get("actual_run") is True
            and audit.get("status") == "PASS" and audit.get("source") == required_source
            and audit.get("source_after") == required_source and audit.get("package") == package.reference(root),
            "actual no-bootstrap build audit is missing or belongs to other bytes/source")
    config_file = artifact(root, audit.get("effective_config"))
    manifest_file = artifact(root, audit.get("build_manifest"))
    execution_file = artifact(root, audit.get("execution"))
    config, execution = read_json(config_file.path), read_json(execution_file.path)
    require(read_json(manifest_file.path) == manifest
            and config.get("bundle", {}).get("windows", {}).get("webviewInstallMode", {}).get("type") == "skip",
            "actual effective bundle configuration does not skip bootstrap")
    command = audit.get("command")
    require(approved_build_argv(command)
            and audit.get("cwd") == "." and audit.get("exit_code") == 0 and audit.get("timed_out") is False,
            "direct actual approved build argv and cwd required")
    require(execution.get("schema_version") == 1 and execution.get("report_type") == "installer_build_execution"
            and execution.get("actual_run") is True and execution.get("command") == command
            and execution.get("source") == required_source and execution.get("source_after") == required_source
            and execution.get("exit_code") == 0 and execution.get("timed_out") is False
            and execution.get("package") == package.reference(root) and execution.get("effective_config") == config_file.reference(root),
            "raw build execution does not bind actual argv/source/config/package")
    log_file = artifact(root, execution.get("raw_log"))
    require(log_file.path.stat().st_size > 0, "raw actual build output is missing")
    tools = audit.get("toolchain")
    require(isinstance(tools, list) and tools and all(isinstance(row, dict) and isinstance(row.get("name"), str)
            and row["name"] and isinstance(row.get("version"), str) and row["version"] for row in tools), "locked actual toolchain inventory required")
    require(execution.get("toolchain") == tools, "toolchain observations differ from raw execution")
    tool_files = tuple(artifact(root, row.get("artifact")) for row in tools)
    require(len({row["name"] for row in tools}) == len(tools), "duplicate toolchain observations")
    build_inputs = audit.get("inputs")
    require(isinstance(build_inputs, dict) and set(build_inputs) == {
                "entrypoint", "tauri_config", "tauri_lock", "node_lock", "python_lock", "installer_template"}
            and execution.get("inputs") == build_inputs,
            "complete exact build inputs must be bound to actual execution")
    input_files = tuple(artifact(root, ref) for ref in build_inputs.values())
    return (audit_file, config_file, manifest_file, execution_file, log_file, *tool_files, *input_files)


@dataclass(frozen=True)
class Artifact:
    path: Path
    sha256: str

    def verify(self) -> None:
        require(HASH.fullmatch(self.sha256) is not None and sha(self.path) == self.sha256,
                "explicit artifact byte identity differs")

    def reference(self, root: Path) -> dict:
        return {"path": self.path.relative_to(root).as_posix(), "sha256": self.sha256}


def artifact(root: Path, value: Any) -> Artifact:
    require(isinstance(value, dict) and set(value) == {"path", "sha256"}, "exact path/hash artifact reference required")
    relative = Path(str(value["path"]))
    require(not relative.is_absolute() and relative.parts and ".." not in relative.parts
            and all(":" not in part for part in relative.parts), "artifact path escape or ADS")
    allowed = ("desktop/src-tauri/target/release/", "build/upgrade-baseline/", "build/candidates/",
               "build/generated/", "build/v1600-evidence/")
    require(relative.as_posix().startswith(allowed), "artifact is not in a reviewed build/evidence namespace")
    result = Artifact(root / relative, str(value["sha256"]).lower())
    require(result.path.resolve().is_relative_to(root.resolve()), "artifact escapes repository")
    result.verify()
    return result


@dataclass(frozen=True)
class Package:
    artifact: Artifact
    kind: str
    version: str
    product_code: str | None = None
    upgrade_code: str | None = None

    def validate(self) -> None:
        require(self.kind in {"msi", "nsis"} and re.fullmatch(r"\d+\.\d+\.\d+", self.version) is not None,
                "exact package kind and release version required")
        self.artifact.verify()
        require(1024 * 1024 <= self.artifact.path.stat().st_size <= 256 * 1024 * 1024,
                "installer size outside reviewed bounds")
        require(self.artifact.path.suffix.lower() == (".msi" if self.kind == "msi" else ".exe"), "installer suffix differs")
        if self.kind == "msi":
            require(isinstance(self.product_code, str) and GUID.fullmatch(self.product_code) is not None
                    and self.upgrade_code == FAMILY, "exact MSI ProductCode and fixed UpgradeCode required")
        else:
            require(self.product_code is None and self.upgrade_code is None, "NSIS must not invent MSI identity")


@dataclass(frozen=True)
class Plan:
    root: Path
    output: Path
    current: Package
    previous: Package
    manifest: dict
    previous_manifest: dict
    source: dict
    inputs: tuple[Artifact, ...]
    expected_binaries: dict
    expected_payload: str
    request: dict
    candidate_sidecar: Artifact | None = None
    candidate_payload: Artifact | None = None
    public_output: Path | None = None
    package_observations: dict = field(default_factory=dict)

    def revalidate(self) -> None:
        for item in self.inputs:
            item.verify()


def prepare(root: Path, request: dict, adapter, source: dict) -> Plan:
    """Validate selected real bytes before an install intent can be persisted."""
    require(request.get("schema_version") == 1 and request.get("kind") in {"msi", "nsis"}, "versioned lifecycle request required")
    names = ("candidate", "previous", "candidate_manifest", "previous_release", "desktop", "sidecar", "payload")
    items = {name: artifact(root, request.get(name)) for name in names}
    if request.get("previous_manifest") is not None:
        items["previous_manifest"] = artifact(root, request["previous_manifest"])
    kind = request["kind"]
    manifest = read_json(items["candidate_manifest"].path)
    # An official historical package need not expose fields/protocols invented
    # after its publication. Missing historical fields remain unknown, not CLEAN.
    older = read_json(items["previous_manifest"].path) if "previous_manifest" in items else {}
    require(manifest.get("product_version") == VERSION and manifest.get("workspace_state") == "CLEAN"
            and manifest.get("build_type") == "Release" and re.fullmatch(r"[0-9a-f]{24}", str(manifest.get("build_id", ""))) is not None,
            "locked clean Release candidate manifest required")
    require(type(manifest.get("manifest_version")) is int and manifest["manifest_version"] == 1
            and type(manifest.get("database_schema_version")) is int and manifest["database_schema_version"] > 0
            and re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_commit", ""))) is not None
            and HASH.fullmatch(str(manifest.get("source_fingerprint", "")).lower()) is not None
            and manifest.get("component_build_ids") == {name: name + "-" + manifest["build_id"]
                for name in ("react", "tauri", "sidecar")}, "complete locked current three-component manifest required")
    require(source.get("workspace_clean") is True and source.get("source_version") == VERSION
            and manifest.get("git_commit") == source.get("source_commit")
            and manifest.get("source_fingerprint") == source.get("source_tree_fingerprint"), "manifest is not current clean source")
    receipt = read_json(items["previous_release"].path)
    previous_version = str(receipt.get("tag", "")).removeprefix("v")
    require(re.fullmatch(r"\d+\.\d+\.\d+", previous_version) is not None
            and tuple(map(int, previous_version.split("."))) < (16, 0, 0)
            and (older.get("product_version") is None or older["product_version"] == previous_version),
            "actual official previous version or available historical identity differs")
    matches = [row for row in receipt.get("assets", []) if isinstance(row, dict)
               and row.get("kind") == kind and row.get("path") == request["previous"]["path"]]
    require(receipt.get("repository") == "AureliusWu/Agent" and receipt.get("tag") == "v" + previous_version
            and receipt.get("draft") is False and receipt.get("prerelease") is False
            and len(matches) == 1 and matches[0].get("github_asset_digest") == "sha256:" + items["previous"].sha256
            and matches[0].get("sha256") == items["previous"].sha256, "official previous release asset digest is not bound")
    audits = []
    for name, selected_manifest in (("candidate", manifest), ("previous", older)):
        audits.extend(validate_build_audit(root, items[name], selected_manifest, request.get(name + "_audit")))
    if hasattr(adapter, "bind_packages"):
        adapter.bind_packages({items["candidate"].sha256: {"product_version": VERSION, "origin": "current-clean-candidate"},
                              items["previous"].sha256: {"product_version": previous_version, "origin": "own-repository-official-release"}})
    packages = []
    observations = {}
    for name, version in (("candidate", VERSION), ("previous", previous_version)):
        observed = adapter.inspect_package(items[name], kind)
        require(observed.get("product_name") == "司忆" and observed.get("version") == version
                and (observed.get("publisher") == "github" or (kind == "nsis" and observed.get("publisher") in {None, ""})),
                "actual package product/version or available publisher differs")
        observations[name] = observed
        package = Package(items[name], kind, version, observed.get("product_code"), observed.get("upgrade_code"))
        package.validate()
        packages.append(package)
    current, previous = packages
    require(current.artifact.sha256 != previous.artifact.sha256
            and (kind != "msi" or current.product_code != previous.product_code), "distinct real major-upgrade baseline required")
    require(items["desktop"].path.stat().st_size <= 128 * 1024 * 1024, "portable EXE exceeds bounded reader")
    portable = items["desktop"].path.read_bytes()
    require(portable.count(b"__TAURI_BUNDLE_TYPE_VAR_UNK") == 1,
            "bounded unambiguous accepted portable bundle marker required")
    marker = b"__TAURI_BUNDLE_TYPE_VAR_MSI" if kind == "msi" else b"__TAURI_BUNDLE_TYPE_VAR_NSS"
    hashes = {"desktop": hashlib.sha256(portable.replace(b"__TAURI_BUNDLE_TYPE_VAR_UNK", marker, 1)).hexdigest(),
              "sidecar": items["sidecar"].sha256}
    payload_module = load_module(root, "rc_payload_inventory")
    expected = read_json(items["payload"].path)
    actual = payload_module.inventory(root, items["sidecar"].reference(root))
    require(expected == actual, "accepted onedir complete inventory differs")
    output = Path(request.get("output", ""))
    require(output.is_absolute() and output.suffix == ".json", "absolute fresh evidence output required")
    ordinary(output)
    require(not output.exists() and not output.with_suffix(".run").exists(), "lifecycle output and owner namespace must be fresh")
    public_output = root / "build/v1600-evidence" / (kind + "-installer-smoke.json")
    ordinary(public_output)
    require(not public_output.exists() and output.resolve() != public_output.resolve(), "public lifecycle output must be fresh and distinct from private evidence")
    boundary = Path(request.get("test_boundary", ""))
    require(boundary.is_absolute() and boundary.is_dir() and output.resolve().is_relative_to(boundary.resolve())
            and output.parent.resolve() != boundary.parent.resolve(), "explicit existing test-only boundary required")
    ordinary(boundary)
    require(not boundary.resolve().is_relative_to(root.resolve()) or boundary.resolve().is_relative_to((root / "build/v1600-evidence").resolve()),
            "repository data must remain under ignored acceptance boundary")
    return Plan(root, output, current, previous, manifest, older, {**source, "build_id": manifest["build_id"]},
                (*items.values(), *audits), hashes, actual["payload_content_sha256"], request,
                items["sidecar"], items["payload"], public_output, observations)


class OwnedFixture:
    def __init__(self, plan: Plan):
        self.plan, self.run_id = plan, str(uuid.uuid4())
        self.root = plan.output.with_suffix(".run")
        ordinary(self.root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.install, self.data = self.root / "install", self.root / "data"
        self.data.mkdir()
        self.marker = {"schema_version": 1, "protocol_version": "owned-installed-lifecycle-v1",
                       "owner_run_id": self.run_id, "run_directory": str(self.root), "install_directory": str(self.install),
                       "data_directory": str(self.data), "candidate_sha256": plan.current.artifact.sha256}
        write_once(self.root / "owner.json", self.marker)
        self.marker_sha = sha(self.root / "owner.json")

    def validate(self) -> None:
        ordinary(self.root)
        ordinary(self.install)
        ordinary(self.data)
        require(sha(self.root / "owner.json") == self.marker_sha and read_json(self.root / "owner.json") == self.marker,
                "retained fixture owner marker changed")
        require(self.install.parent == self.root and self.data.parent == self.root, "fixture location changed")


def validate_host(value: dict, package: Package | None, fixture: OwnedFixture, expected_links: list[dict]) -> None:
    """Missing observation fields are unknown, never an empty host."""
    for field in ("installations", "related_products", "install_paths", "shortcuts", "running", "webviews"):
        require(isinstance(value.get(field), list), "complete host observation required: " + field)
    require(type(value.get("is_admin")) is bool and type(value.get("installer_busy")) is bool,
            "typed token and installer-busy observations required")
    require(not value["installer_busy"] and not value["running"], "preexisting installer or application activity is not owned")
    require(value["webviews"] and all(isinstance(row, str) and re.fullmatch(r"[1-9]\d*\.\d+\.\d+\.\d+", row)
            for row in value["webviews"]), "already-installed WebView2 required; no bootstrap download")
    require(value["shortcuts"] == expected_links, "shortcut bytes/targets changed since the owner snapshot")
    codes = []
    for row in value["related_products"]:
        require(isinstance(row, dict) and row.get("upgrade_code") == FAMILY
                and GUID.fullmatch(str(row.get("product_code", ""))) is not None, "unknown MSI family observation")
        codes.append(row["product_code"])
        require(package is not None and package.kind == "msi" and row.get("product_code") == package.product_code
                and row.get("version") == package.version and key(row.get("path", "")) == key(fixture.install),
                "related MSI registration is not self-owned")
    require(len(set(codes)) == len(codes) and set(codes) == ({package.product_code} if package and package.kind == "msi" else set()),
            "exact per-stage MSI ProductCode set differs")
    if package is None:
        require(not value["installations"] and not value["install_paths"], "non-owned product namespace is occupied")
        return
    require(len(value["installations"]) == 1 and value["install_paths"], "exact product registration and install path required")
    row = value["installations"][0]
    require(isinstance(row, dict) and row.get("name") == "司忆" and row.get("version") == package.version
            and row.get("publisher") == "github" and row.get("product_code") == package.product_code
            and _install_location_key(row.get("path", "")) == key(fixture.install), "installed namespace differs from exact self-owned stage")
    if package.kind == "nsis":
        require(key(str(row.get("uninstall", "")).strip('"')) == key(fixture.install / "uninstall.exe"), "registered NSIS uninstaller is not self-owned")
    for row in value["install_paths"]:
        require(isinstance(row, dict) and row.get("key", "").casefold() in {r"hkcu:\software\github\司忆", r"hklm:\software\github\司忆"}
                and key(row.get("path", "")) == key(fixture.install) and row.get("subkey_count") == 0
                and isinstance(row.get("value_names"), list) and set(row["value_names"]).issubset(
                    {"", "InstallDir", "Installer Language", "Desktop Shortcut", "Uninstaller Shortcut", "Start Menu Shortcut"}),
                "unexpected product registry keys or values")


def validate_new_links(rows: list[dict], package: Package | None, fixture: OwnedFixture) -> list[dict]:
    require(isinstance(rows, list), "shortcut inventory required")
    if package is None:
        require(not rows, "uninstall retained package shortcuts")
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and all(isinstance(row.get(name), str) for name in ("path", "target", "arguments", "sha256"))
                and HASH.fullmatch(row["sha256"]) is not None and key(row["path"]) not in seen,
                "complete unique shortcut bytes required")
        seen.add(key(row["path"]))
        require(package is not None and ((key(row["target"]) == key(fixture.install / "司忆.exe") and row["arguments"] == "")
                or (package.kind == "nsis" and key(row["target"]) == key(fixture.install / "uninstall.exe") and row["arguments"] == "")
                or (package.kind == "msi" and key(row["target"]) == key(native_system_directory() / "msiexec.exe")
                    and row["arguments"].upper() == "/X " + package.product_code)), "new shortcut does not target the exact owned package")
    return rows


class Lifecycle:
    def __init__(self, plan: Plan, adapter):
        self.plan, self.adapter = plan, adapter
        self.fixture = OwnedFixture(plan)
        self.installed = None
        self.links: list[dict] = []
        self.uninstaller_sha: str | None = None
        self.index, self.uncertain = 0, False
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.seed = None
        self.installed_payload = None
        self.previous_schema = None
        self.previous_build_manifest = None

    def event(self, value: dict) -> None:
        self.fixture.validate()
        write_once(self.fixture.root / (f"event-{len(self.events):03d}.json"), value)
        self.events.append(value)

    def transition(self, stage: str) -> dict:
        require(self.lock.acquire(blocking=False), "concurrent lifecycle transition forbidden")
        try:
            require(not self.uncertain and self.index < len(STAGES) and STAGES[self.index] == stage, "uncertain, repeated or out-of-order stage")
            self.fixture.validate()
            self.plan.revalidate()
            before = self.adapter.observe(self.fixture)
            validate_host(before, self.installed, self.fixture, self.links)
            if self.plan.current.kind == "msi":
                require(before["is_admin"] is True, "MSI requires an already-elevated token; no automatic UAC")
            if self.installed and self.installed.kind == "nsis":
                require(self.uninstaller_sha and sha(self.fixture.install / "uninstall.exe") == self.uninstaller_sha,
                        "full NSIS uninstaller byte identity changed")
            self.event({"stage": stage, "state": "INTENT_UNCERTAIN", "owner_run_id": self.fixture.run_id,
                        "automatic_recovery_allowed": False, "prior_product_code": self.installed.product_code if self.installed else None})
            self.uncertain = True
            try:
                result, expected = self._dispatch(stage)
                require(isinstance(result, dict) and result.get("passed") is True, "actual stage did not pass")
                after = self.adapter.observe(self.fixture)
                links = validate_new_links(after.get("shortcuts"), expected, self.fixture)
                validate_host(after, expected, self.fixture, links)
                self.event({"stage": stage, "state": "OBSERVED_COMPLETE", "owner_run_id": self.fixture.run_id,
                            "result": result, "actual_run": self.adapter.actual_run})
                self.installed, self.links = expected, links
                self.uninstaller_sha = sha(self.fixture.install / "uninstall.exe") if expected and expected.kind == "nsis" else None
                self.index += 1
                self.uncertain = False
                return result
            except BaseException as exc:
                self.event({"stage": stage, "state": "UNCERTAIN_OPERATOR_RECOVERY", "failure_type": type(exc).__name__,
                            "owner_run_id": self.fixture.run_id, "fixture_retained": True, "automatic_recovery_allowed": False})
                raise
        finally:
            self.lock.release()

    def _dispatch(self, stage: str) -> tuple[dict, Package | None]:
        if stage in {"install_previous", "upgrade_current", "reinstall_current", "uninstall_current", "final_uninstall"}:
            target = self.plan.previous if stage == "install_previous" else self.plan.current
            remove = stage in {"uninstall_current", "final_uninstall"}
            result = self.adapter.install(target, self.fixture, remove=remove, uninstaller_sha=self.uninstaller_sha, stage=stage)
            return result, None if remove else target
        if stage in {"launch_previous", "launch_upgraded", "launch_reinstalled"}:
            package = self.plan.previous if stage == "launch_previous" else self.plan.current
            current = package is self.plan.current
            result = self.adapter.launch(package, self.fixture, current=current, plan=self.plan, stage=stage)
            if current:
                self.installed_payload = result.get("installed_sidecar_payload")
                require(result.get("binary_sha256") == self.plan.expected_binaries
                        and result.get("sidecar_payload_sha256") == self.plan.expected_payload, "installed bytes differ from accepted portable derivation")
                self.adapter.verify_fixture(self.fixture, self.seed, int(self.plan.manifest["database_schema_version"]), require_backup=True)
            else:
                observed_schema = self.adapter.database_schema(self.fixture.data / "data/agent.db")
                require(type(observed_schema) is int and 0 < observed_schema < int(self.plan.manifest["database_schema_version"]),
                        "actual previous owned database schema is not a valid migration baseline")
                recorded_schema = self.plan.previous_manifest.get("database_schema_version")
                require(recorded_schema is None or (type(recorded_schema) is int and recorded_schema == observed_schema),
                        "actual previous owned database differs from available historical schema")
                identity = result.get("installed_build_identity")
                require(isinstance(identity, dict), "actual installed previous identity observation required")
                observed_manifest = identity.get("manifest", identity.get("observation"))
                require(isinstance(observed_manifest, dict) and observed_manifest.get("product_version") == package.version,
                        "actual installed previous version must agree with official package")
                # This is a newly collected receipt, not a patch to historical
                # evidence. Only observed fields are retained; old absent build
                # type/source/component fields are not promoted to CLEAN/current.
                self.previous_build_manifest = {**observed_manifest, "database_schema_version": observed_schema}
                self.previous_schema = observed_schema
            return result, self.installed
        if stage == "seed_fixture":
            require(type(self.previous_schema) is int, "actual previous owned database observation is required before seeding")
            self.seed = self.adapter.seed_fixture(self.fixture, self.previous_schema)
            return {"passed": True, "fixture": self.seed}, self.installed
        require(stage in {"verify_retention", "verify_final"}, "unknown lifecycle stage")
        result = self.adapter.verify_fixture(self.fixture, self.seed, int(self.plan.manifest["database_schema_version"]), require_backup=True)
        require(not (self.fixture.install / "司忆.exe").exists() and not (self.fixture.install / "agent-backend.exe").exists()
                and not (self.fixture.install / "_internal").exists(), "package files were not removed")
        if stage == "verify_final" and hasattr(self.adapter, "finish_desktop_guards"):
            self.adapter.finish_desktop_guards(self.fixture)
        return {"passed": True, "retained": result}, None

    def run(self) -> dict:
        results = {}
        try:
            for stage in STAGES:
                results[stage] = self.transition(stage)
            source_after = self.adapter.source_identity(self.plan.root)
            require({**source_after, "build_id": self.plan.manifest["build_id"]} == self.plan.source, "source changed during installed lifecycle")
            actual = self.adapter.actual_run is True
            kind = self.plan.current.kind.upper()
            flags = {"status": "ok", "version": VERSION, "desktop_started": True, "sidecar_stopped": True,
                "previous_version_upgrade": True, "schema_migrated": True, "in_place_upgrade_preserved_data": True,
                "uninstall_preserved_data": True, "reinstall": True, "reinstall_started": True,
                "reinstall_recognized_data": True, "final_uninstall": True, "package_files_removed": True,
                "install": True, "uninstall": True, "uninstall_preserved_models": True}
            def package_record(package):
                return {"name": package.artifact.path.name, "version": package.version, "sha256": package.artifact.sha256,
                        "bytes": package.artifact.path.stat().st_size, "product_code": package.product_code, "upgrade_code": package.upgrade_code}
            report = {"schema_version": 1, "report_type": f"release_{kind.lower()}_installer_live_evidence" if actual else "synthetic_installer_lifecycle",
                "target_version": VERSION, "status": "PASS" if actual else "SYNTHETIC_PASS", "actual_run": actual,
                "rc_eligible": actual, "source": self.plan.source, "source_after": {**source_after, "build_id": self.plan.manifest["build_id"]},
                "run": {"installer_kind": kind, "isolated_test_data": True, "owner_run_id": self.fixture.run_id,
                        "fixture_retained": True, "operator_attested": False},
                "artifacts": {"candidate": package_record(self.plan.current), "previous": package_record(self.plan.previous),
                              "build_manifest": self.plan.manifest, "previous_build_manifest": self.previous_build_manifest,
                              "previous_schema_observation": self.previous_schema,
                              "package_observations": self.plan.package_observations},
                "checks": {name: {"passed": True, "result": value} for name, value in results.items()},
                "results": flags, "binary_sha256": self.plan.expected_binaries,
                "sidecar_payload_sha256": self.plan.expected_payload, "installed_sidecar_payload": self.installed_payload,
                "manual_desktop_acceptance": "NOT_RECORDED", "credential_manager_isolation": "NOT_ISOLATED_SAME_WINDOWS_USER",
                "automatic_registry_or_fixture_cleanup": False}
        except BaseException as exc:
            self.uncertain = True
            report = {"schema_version": 1, "report_type": "retained_installed_lifecycle_failure", "status": "BLOCKED",
                "actual_run": getattr(self.adapter, "mutation_started", False) is True, "rc_eligible": False, "failure_type": type(exc).__name__,
                "last_completed_stage": STAGES[self.index - 1] if self.index else None, "state": "UNCERTAIN_OPERATOR_RECOVERY",
                "fixture_retained": True, "automatic_registry_or_fixture_cleanup": False, "events": self.events,
                "owner_run_id": self.fixture.run_id}
        write_once(self.plan.output, report)
        # Preserve the complete private report first. Publication cannot rewrite
        # private evidence or turn an actual installation into actual_run=false.
        if report["status"] in {"PASS", "SYNTHETIC_PASS"}:
            try:
                if self.plan.public_output is not None:
                    require(self.plan.candidate_sidecar is not None and self.plan.candidate_payload is not None,
                            "accepted candidate attachments required for public report")
                    writer = load_module(self.plan.root, "rc_installed_public")
                    writer.write_public_report(self.plan.root, self.plan.public_output, report,
                        candidate_sidecar=self.plan.candidate_sidecar.reference(self.plan.root),
                        candidate_payload=self.plan.candidate_payload.reference(self.plan.root))
                else:
                    require(self.adapter.actual_run is False, "actual lifecycle requires its planned fresh public output")
            except Exception as exc:
                return {**report, "status": "BLOCKED", "rc_eligible": False,
                        "state": "PUBLIC_REPORT_FAILED_PRIVATE_RETAINED", "failure_type": type(exc).__name__,
                        "private_evidence_retained": True}
        return report


def clean_exit(value: dict) -> bool:
    return (isinstance(value, dict) and value.get("protocol_version") == "exact-native-job-v1"
            and type(value.get("active_before_cleanup")) is int and value["active_before_cleanup"] == 0
            and type(value.get("active_after_cleanup")) is int and value["active_after_cleanup"] == 0
            and value.get("forced_termination") is False
            and value.get("job_handle_closed") is True and value.get("errors") == []
            and value.get("unassigned_cleanup_complete") is True
            and type(value.get("owned_process_handles_remaining")) is int and value["owned_process_handles_remaining"] == 0
            and type(value.get("owned_thread_handles_remaining")) is int and value["owned_thread_handles_remaining"] == 0
            and value.get("launch_cleanup_errors") == [])


class WindowsAdapter:
    actual_run = True

    def __init__(self, root: Path, probe_directory: Path, *, job_factory=OwnedDesktopJob):
        require(os.name == "nt", "installed lifecycle execution requires Windows")
        self.root, self.probes, self.job_factory = root, probe_directory, job_factory
        ordinary(probe_directory)
        probe_directory.mkdir(parents=True, exist_ok=False)
        system = native_system_directory()
        self.powershell = system / "WindowsPowerShell/v1.0/powershell.exe"
        self.msiexec = system / "msiexec.exe"
        self.native_hashes = {self.powershell: sha(self.powershell), self.msiexec: sha(self.msiexec)}
        self.bound_packages = {}
        self.desktop_guards = []
        self.desktop_guard_owner = None
        self.mutation_started = False

    def bind_packages(self, values: dict) -> None:
        self.bound_packages = dict(values)

    def source_identity(self, root):
        return load_module(root, "generate_build_info")._release_source_identity(root)

    def environment(self, data: Path, nonce: str) -> dict:
        collector = load_module(self.root, "record-rc-desktop-startup")
        environment = collector.desktop_environment(dict(os.environ), data, nonce)
        environment.update(AGENT_PROVIDER_CONFIG_PATH=str(data / "config/provider.json"),
                           AGENT_STT_ENABLED="false", AGENT_SPEECH_ENABLED="false", AGENT_AUTO_TITLE_ENABLED="false")
        require(not (data / "config/provider.json").exists(), "isolated provider configuration must remain absent")
        return environment

    def owned_command(self, executable: Path, arguments: list[str], environment: dict, *, timeout=120, mutation=False) -> dict:
        ordinary(executable)
        if executable in self.native_hashes:
            require(sha(executable) == self.native_hashes[executable], "native dispatcher executable changed")
        record_path = self.probes / ("owned-command-" + str(uuid.uuid4()) + ".json")
        job, process, cleanup, code, failure = self.job_factory(), None, None, None, None
        try:
            process = job.launch(executable, environment, arguments, show_window=False)
            if mutation:
                self.mutation_started = True
            code = process.wait(timeout)
            job.wait_empty(15)
        except BaseException as exc:
            failure = type(exc).__name__
            raise
        finally:
            events = list(getattr(job, "launch_events", ()))
            if mutation and "resumed" in events:
                self.mutation_started = True
            cleanup_failed = False
            try:
                cleanup = job.cleanup()
            except BaseException as exc:
                cleanup_failed = True
                cleanup = {"protocol_version": "exact-native-job-v1", "active_after_cleanup": None,
                           "launch_events": events, "errors": [type(exc).__name__]}
            if mutation and "resumed" in cleanup.get("launch_events", []):
                self.mutation_started = True
            write_once(record_path, {"schema_version": 1, "report_type": "private_owned_dispatcher_observation",
                "command": [str(executable), *arguments], "installer_or_application_effect": mutation,
                "actual_run": "resumed" in cleanup.get("launch_events", []), "exit_code": code,
                "failure_type": failure, "process_cleanup": cleanup})
            require(not cleanup_failed, "owned cleanup failed; retained native state is uncertain")
        require(type(code) is int and code == 0 and clean_exit(cleanup), "owned dispatcher failed, rebooted, timed out or required forced cleanup")
        return {"passed": True, "exit_code": code, "command": [str(executable), *arguments], "process_cleanup": cleanup,
                "retained_process_creation_time": process.creation_time}

    def host_probe_environment(self) -> dict:
        """Host identity only, never an application/installer launch environment."""
        allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "COMPUTERNAME",
                   "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROGRAMFILES", "PROGRAMFILES(X86)",
                   "PROGRAMW6432", "OS", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
        environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
        modules = self.powershell.parent / "Modules"
        ordinary(modules)
        require(modules.is_dir(), "trusted native PowerShell modules unavailable for readonly host observation")
        # Never inherit user/runtime PSModulePath, Provider settings or keys.
        environment["PSModulePath"] = str(modules)
        return environment

    def host_ps(self, probe: str) -> dict:
        probes = {"host_observation": HOST_OBSERVATION, "known_folders": KNOWN_FOLDER_OBSERVATION}
        require(probe in probes, "only fixed readonly host probes may use the host environment")
        return self._ps(probes[probe], None, self.host_probe_environment())

    def ps(self, code: str, variables: dict | None = None) -> dict:
        data = self.probes / "private-environment"
        return self._ps(code, variables, self.environment(data, str(uuid.uuid4())))

    def _ps(self, code: str, variables: dict | None, environment: dict) -> dict:
        output = self.probes / (str(uuid.uuid4()) + ".json")
        environment.update(variables or {})
        environment["SIYI_PROBE_OUTPUT"] = str(output)
        tail = r"""
        $json=$result|ConvertTo-Json -Depth 10 -Compress;
        $bytes=[Text.Encoding]::UTF8.GetBytes($json);
        $file=[IO.File]::Open($env:SIYI_PROBE_OUTPUT,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None);
        try {$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
        """
        encoded = base64.b64encode(("$ErrorActionPreference='Stop';" + code + tail).encode("utf-16-le")).decode()
        self.owned_command(self.powershell, ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], environment, timeout=45)
        return read_json(output)

    def inspect_package(self, item: Artifact, kind: str) -> dict:
        item.verify()
        bound = self.bound_packages.get(item.sha256)
        require(isinstance(bound, dict) and bound.get("origin") in {"current-clean-candidate", "own-repository-official-release"},
                "package lacks clean candidate or official own-repository byte binding")
        if kind == "nsis":
            # NSIS is opaque authored code, not statically approved in entirety.
            # Trust is restricted to the already-bound own-repository package;
            # WebView2, empty product namespaces and owned fixture are checked
            # before every mutation. A blank resource publisher stays unknown.
            require(item.path.stat().st_size <= 256 * 1024 * 1024, "bounded NSIS header reader required")
            data = item.path.read_bytes()
            require(data.startswith(b"MZ") and data.count(b"\xef\xbe\xad\xdeNullsoftInst") == 1,
                    "actual bounded NSIS package header required")
            value = self.ps(r"$f=[Diagnostics.FileVersionInfo]::GetVersionInfo($env:SIYI_PACKAGE);$result=@{product_name=[string]$f.ProductName;version=[string]$f.ProductVersion;publisher=[string]$f.CompanyName}",
                            {"SIYI_PACKAGE": str(item.path)})
            require(value.get("version") == bound["product_version"], "actual NSIS version resource differs")
            item.verify()
            return {**value, "publisher_observation": "UNKNOWN" if not value.get("publisher") else "OBSERVED",
                    "trust_basis": bound["origin"], "whole_program_static_review": False,
                    "bootstrap_policy": "PREEXISTING_WEBVIEW2_REQUIRED"}
        value = self.ps(MSI_METADATA, {"SIYI_PACKAGE": str(item.path)})
        properties = validate_msi_metadata(value)
        item.verify()
        return {"product_name": properties.get("ProductName"), "publisher": properties.get("Manufacturer"),
                "version": properties.get("ProductVersion"), "product_code": str(properties.get("ProductCode", "")).upper(),
                "upgrade_code": str(properties.get("UpgradeCode", "")).upper(),
                "trust_basis": bound["origin"], "authored_mutation_review": "readonly-msi-tables",
                "bootstrap_policy": "PREEXISTING_WEBVIEW2_REQUIRED"}

    def observe(self, fixture: OwnedFixture) -> dict:
        fixture.validate()
        return self.host_ps("host_observation")

    def ensure_desktop_guards(self, fixture: OwnedFixture) -> None:
        fixture.validate()
        if self.desktop_guard_owner is None:
            require(not self.desktop_guards, "desktop guard state is not fresh")
            observed = self.host_ps("known_folders")
            paths = [Path(observed.get(name, "")) for name in ("desktop", "common_desktop")]
            require(all(path.is_absolute() and path.is_dir() for path in paths), "actual Windows known desktop folders are unknown")
            self.desktop_guard_owner = fixture.run_id
            try:
                for path in {key(path): path for path in paths}.values():
                    guard = DesktopDirectoryGuard(path, fixture.run_id)
                    self.desktop_guards.append(guard)
                    write_once(fixture.root / (f"desktop-guard-{len(self.desktop_guards):02d}.json"), guard.private_record())
            except BaseException:
                # Do not remove created sentinels after partial failure.
                write_once(fixture.root / "desktop-guard-failure.json", {"owner_run_id": fixture.run_id,
                    "created_guards": [guard.private_record() for guard in self.desktop_guards],
                    "attempted_known_desktops": [str(path) for path in paths], "guard_files_retained": True})
                raise
        require(self.desktop_guard_owner == fixture.run_id and self.desktop_guards, "desktop guards belong to another fixture")
        for guard in self.desktop_guards:
            guard.verify()

    def finish_desktop_guards(self, fixture: OwnedFixture) -> None:
        # Synthetic adapters do not create host sentinels or native handles.
        guards = getattr(self, "desktop_guards", [])
        if not guards:
            return
        require(self.desktop_guard_owner == fixture.run_id, "desktop guard owner differs at completion")
        for guard in guards:
            guard.release_success()
        write_once(fixture.root / "desktop-guards-completed.json", {"owner_run_id": fixture.run_id,
            "actual_desktop_directories_preserved": True, "only_owned_sentinels_removed": True})

    def install(self, package: Package, fixture: OwnedFixture, *, remove: bool, uninstaller_sha: str | None, stage: str) -> dict:
        fixture.validate()
        package.validate()
        environment = self.environment(fixture.data, str(uuid.uuid4()))
        if package.kind == "msi":
            self.ensure_desktop_guards(fixture)
            target = package.product_code if remove else str(package.artifact.path)
            log = fixture.root / (stage + ".msi.log")
            require(not log.exists(), "installer log must be fresh")
            args = ["/x" if remove else "/i", target, "/qn", "/norestart", "/L*v", str(log)]
            if not remove:
                args.append("INSTALLDIR=" + str(fixture.install))
                # Standard Tauri auto-launch is strictly opt-in; keep it absent.
                require("AUTOLAUNCHAPP" not in environment, "installer auto-launch environment is forbidden")
            return self.owned_command(self.msiexec, args, environment, timeout=180, mutation=True)
        require(not any(character.isspace() for character in str(fixture.install)), "NSIS /D raw tail requires an unambiguous no-space owned installation path")
        executable = fixture.install / "uninstall.exe" if remove else package.artifact.path
        if remove:
            require(uninstaller_sha is not None and sha(executable) == uninstaller_sha, "actual NSIS uninstaller bytes differ")
        args = ["/S", "_?=" + str(fixture.install)] if remove else ["/S", "/D=" + str(fixture.install)]
        return self.owned_command(executable, args, environment, timeout=180, mutation=True)

    def launch(self, package: Package, fixture: OwnedFixture, *, current: bool, plan: Plan, stage: str) -> dict:
        fixture.validate()
        desktop, sidecar = fixture.install / "司忆.exe", fixture.install / "agent-backend.exe"
        hashes = {"desktop": sha(desktop), "sidecar": sha(sidecar)}
        payload = None
        if current:
            payload = load_module(self.root, "rc_payload_inventory").inventory(fixture.root,
                      {"path": sidecar.relative_to(fixture.root).as_posix(), "sha256": hashes["sidecar"]})
            embedded = read_json(fixture.install / "_internal/build-info.json")
            require(embedded == plan.manifest, "actual installed complete build manifest differs")
            require(hashes == plan.expected_binaries and payload["payload_content_sha256"] == plan.expected_payload,
                    "actual installed EXE or complete payload changed")
            installed_identity = {"identity_mode": "current_onedir_manifest", "manifest": embedded}
        else:
            installed_identity = self.previous_identity(sidecar, package, plan.previous_manifest)
        nonce = str(uuid.uuid4())
        environment = self.environment(fixture.data, nonce)
        receipt_path = fixture.data / "rc-desktop-observation.json"
        bridge = fixture.data / "rc-acceptance-owner.json"
        archive = rotate_launch_marker(fixture.root, fixture.data, fixture.run_id, nonce)
        write_once(archive / "attempt.json", {"stage": stage, "owner_run_id": fixture.run_id, "acceptance_nonce": nonce,
                                            "binary_sha256": hashes, "previous_observation_is_not_render_acceptance": not current})
        job, process, owned_sidecar = self.job_factory(), None, None
        cleanup = None
        receipt = None
        desktop_code = sidecar_code = None
        try:
            process = job.launch(desktop, environment, show_window=True)
            self.mutation_started = True
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                require(process.poll() is None, "installed desktop exited before readiness")
                if current and receipt_path.is_file():
                    receipt = read_json(receipt_path)
                    self.validate_render_receipt(receipt, nonce, process.pid, plan.manifest)
                    owned_sidecar = job.observe_sidecar(receipt["process_ids"]["sidecar"], sidecar)
                    window = receipt["window_handle"]
                    break
                if not current:
                    observed = self.ps(OLD_DESKTOP_OBSERVATION, {"SIYI_DESKTOP_PID": str(process.pid)})
                    children = observed.get("sidecars", [])
                    database = fixture.data / "data/agent.db"
                    if len(children) == 1 and observed.get("main_window") and database.is_file():
                        owned_sidecar = job.observe_sidecar(children[0], sidecar)
                        observed_schema = self.database_schema(database)
                        require(type(observed_schema) is int and 0 < observed_schema < int(plan.manifest["database_schema_version"]),
                                "previous real owned application schema is not ready")
                        known_schema = plan.previous_manifest.get("database_schema_version")
                        require(known_schema is None or observed_schema == known_schema,
                                "previous real owned application schema differs from historical observation")
                        window = observed["main_window"]
                        break
                time.sleep(.1)
            else:
                raise SafetyError("actual installed desktop readiness timed out")
            load_module(self.root, "record-rc-desktop-startup").close_owned_desktop(process, window)
            desktop_code = process.wait(30)
            sidecar_code = owned_sidecar.wait(15)
            require(type(desktop_code) is int and desktop_code == 0 and type(sidecar_code) is int and sidecar_code == 0,
                    "actual retained desktop and sidecar did not both exit normally")
            job.wait_empty(15)
        finally:
            cleanup = job.cleanup()
            if "resumed" in cleanup.get("launch_events", []):
                self.mutation_started = True
            write_once(archive / "cleanup.json", cleanup)
        require(clean_exit(cleanup), "installed application required forced or unknown cleanup")
        if receipt is not None:
            write_once(archive / "actual-render-receipt.json", receipt)
        write_once(archive / "completion.json", {"schema_version": 1, "owner_run_id": fixture.run_id,
                    "acceptance_nonce": nonce, "process_cleanup": cleanup})
        return {"passed": True, "protocol": "desktop-render-ready-v1" if current else "previous-installed-owned-process-v1",
                "previous_observation_is_not_render_acceptance": not current, "binary_sha256": hashes,
                "sidecar_payload_sha256": payload["payload_content_sha256"] if payload is not None else None,
                "installed_sidecar_payload": payload, "installed_build_identity": installed_identity, "render_receipt": receipt,
                "process_cleanup": cleanup, "desktop_exit_code": desktop_code, "sidecar_exit_code": sidecar_code}

    def previous_identity(self, sidecar: Path, package: Package, known: dict) -> dict:
        """Read historical identity without forcing current onedir/layout fields."""
        ordinary(sidecar)
        path = sidecar.parent / "_internal/build-info.json"
        ordinary(path)
        if path.is_file():
            observed = read_json(path)
            identity = {"identity_mode": "previous_onedir_manifest", "manifest": observed}
        else:
            reader = load_module(self.root, "read-pyinstaller-build-info")
            observed = reader.extract_embedded_build_info(sidecar)
            require(observed.get("archive_entry") == "build-info.json"
                    and type(observed.get("embedded_manifest_bytes")) is int
                    and 0 < observed["embedded_manifest_bytes"] <= 128 * 1024
                    and HASH.fullmatch(str(observed.get("embedded_manifest_sha256", "")).lower()) is not None,
                    "bounded actual historical onefile manifest required")
            identity = {"identity_mode": "legacy_onefile_embedded_manifest", "observation": observed}
        require(observed.get("product_version") == package.version, "actual historical installed version differs from official package")
        for field_name in ("product_version", "git_commit", "source_fingerprint", "workspace_state", "build_id"):
            expected = known.get(field_name)
            require(expected is None or observed.get(field_name) == expected, "available historical identity field differs: " + field_name)
        identity["current_candidate_manifest_qualification"] = False
        return identity

    @staticmethod
    def validate_render_receipt(receipt, nonce, pid, manifest):
        require(receipt.get("report_type") == "rc_desktop_runtime_observation" and receipt.get("schema_version") == 1
                and receipt.get("protocol_version") == "desktop-render-ready-v1" and receipt.get("actual_run") is True
                and receipt.get("status") == "PASS" and receipt.get("acceptance_nonce") == nonce
                and receipt.get("isolated_test_data") is True and receipt.get("desktop_render_ready") is True
                and receipt.get("sidecar_ready") is True and receipt.get("process_ids", {}).get("desktop") == pid,
                "candidate did not produce this actual render-ready observation")
        require(type(receipt.get("window_handle")) is int and 0 < receipt["window_handle"] < 2 ** 64
                and type(receipt.get("readiness_ms")) is int and receipt["readiness_ms"] > 0
                and type(receipt.get("process_ids", {}).get("sidecar")) is int and receipt["process_ids"]["sidecar"] > 0,
                "actual HWND, retained sidecar identity and positive readiness required")
        components = receipt.get("components")
        require(isinstance(components, dict) and set(components) == {"react", "tauri", "sidecar"}, "all actual components required")
        fields = ("manifest_version", "product_version", "git_commit", "source_fingerprint", "build_id", "database_schema_version", "component_build_ids",
                  "workspace_state", "build_type")
        require(all(isinstance(value, dict) and all(value.get(field) == manifest.get(field) for field in fields) for value in components.values()),
                "observed components disagree with locked candidate")
        require(manifest.get("workspace_state") == "CLEAN" and manifest.get("build_type") == "Release",
                "render-ready acceptance requires a clean Release candidate")

    @staticmethod
    def database_schema(path: Path) -> int:
        ordinary(path)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "isolated DB integrity failed")
            return int(db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])

    def seed_fixture(self, fixture: OwnedFixture, old_schema: int) -> dict:
        fixture.validate()
        path = fixture.data / "data/agent.db"
        require(self.database_schema(path) == old_schema, "fixture must use actual previous application database")
        nonce = fixture.run_id
        with closing(sqlite3.connect(path)) as db, db:
            require(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='release_upgrade_sentinel'").fetchone()[0] == 0, "fixture sentinel already exists")
            db.execute("CREATE TABLE release_upgrade_sentinel(value TEXT NOT NULL)")
            db.execute("INSERT INTO release_upgrade_sentinel(value) VALUES(?)", (nonce,))
            stamp = "2026-10-03T00:00:00+00:00"
            cursor = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                                ("Owned installer fixture", str(fixture.root / "workspace"), "ask", stamp, stamp))
            conversation = cursor.lastrowid
            db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
                       (conversation, "user", "synthetic-installer-retention-" + nonce, stamp))
        model = fixture.data / "voice/models/installer-fixture/sentinel.txt"
        ordinary(model)
        model.parent.mkdir(parents=True, exist_ok=False)
        with model.open("xb") as stream:
            stream.write(nonce.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        return {"nonce": nonce, "conversation_id": conversation, "model_sha256": sha(model), "old_schema": old_schema}

    def verify_fixture(self, fixture: OwnedFixture, seed: dict, schema: int, *, require_backup: bool) -> dict:
        fixture.validate()
        require(isinstance(seed, dict) and seed.get("nonce") == fixture.run_id, "exact retained fixture seed required")
        path = fixture.data / "data/agent.db"
        require(self.database_schema(path) == schema, "actual migrated schema differs")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            require(db.execute("SELECT value FROM release_upgrade_sentinel").fetchall() == [(seed["nonce"],)], "sentinel data was lost")
            require(db.execute("SELECT title FROM conversations WHERE id=?", (seed["conversation_id"],)).fetchone() == ("Owned installer fixture",), "conversation was lost")
            require(db.execute("SELECT content FROM messages WHERE conversation_id=?", (seed["conversation_id"],)).fetchall() == [("synthetic-installer-retention-" + seed["nonce"],)], "message was lost")
        require(sha(fixture.data / "voice/models/installer-fixture/sentinel.txt") == seed["model_sha256"], "model fixture was removed or overwritten")
        backups = []
        if require_backup:
            directory = fixture.data / "backups"
            ordinary(directory)
            require(directory.is_dir(), "migration backup missing")
            for candidate in directory.iterdir():
                if candidate.name.startswith("pre-migration-v") and candidate.suffix == ".db":
                    ordinary(candidate)
                    if self.database_schema(candidate) == seed["old_schema"]:
                        with closing(sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)) as db:
                            if db.execute("SELECT value FROM release_upgrade_sentinel").fetchall() == [(seed["nonce"],)]:
                                backups.append({"name": candidate.name, "sha256": sha(candidate)})
            require(backups, "actual pre-upgrade backup does not retain seeded data")
        return {"passed": True, "schema": schema, "conversation_preserved": True, "message_preserved": True,
                "model_fixture_preserved": True, "migration_backups": backups}


MSI_METADATA = r"""$installer=$null;$database=$null;$view=$null;
try {
 $installer=New-Object -ComObject WindowsInstaller.Installer;
 $database=$installer.OpenDatabase($env:SIYI_PACKAGE,0);
 $properties=@{};$view=$database.OpenView('SELECT `Property`,`Value` FROM `Property`');$view.Execute();
 while($row=$view.Fetch()){$properties[[string]$row.StringData(1)]=[string]$row.StringData(2)};$view.Close();
 $actions=@();$view=$database.OpenView('SELECT `Action`,`Type`,`Source`,`Target` FROM `CustomAction`');$view.Execute();
 while($row=$view.Fetch()){$actions+=@{name=[string]$row.StringData(1);type=[int]$row.IntegerData(2);source=[string]$row.StringData(3);target=[string]$row.StringData(4)}};$view.Close();
 $tables=@();$view=$database.OpenView('SELECT `Name` FROM `_Tables`');$view.Execute();
 while($row=$view.Fetch()){$tables+=[string]$row.StringData(1)};$view.Close();
 function Read-MsiRows($table,$columns,$names,$ints){
  if($tables -notcontains $table){return};$sql='SELECT '+(($columns|ForEach-Object {'`'+$_+'`'}) -join ',')+' FROM `'+$table+'`';
  $v=$database.OpenView($sql);try{[void]$v.Execute();while($r=$v.Fetch()){$entry=@{};
   for($i=0;$i -lt $names.Count;$i++){if($ints -contains $i){$entry[$names[$i]]=[int]$r.IntegerData($i+1)}else{$entry[$names[$i]]=[string]$r.StringData($i+1)}};$entry}
  }finally{[void]$v.Close();[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($v)}
 }
 $result=@{properties=$properties;custom_actions=$actions;tables=$tables;database_open_mode=0;
 directories=@(Read-MsiRows 'Directory' @('Directory','Directory_Parent','DefaultDir') @('id','parent','name') @());
 components=@(Read-MsiRows 'Component' @('Component','Directory_') @('id','directory') @());
  create_folders=@(Read-MsiRows 'CreateFolder' @('Directory_','Component_') @('directory','component') @());
 files=@(Read-MsiRows 'File' @('File','Component_','FileName') @('id','component','name') @());
 registry=@(Read-MsiRows 'Registry' @('Component_','Root','Key','Name','Value') @('component','root','key','name','value') @(1));
 shortcuts=@(Read-MsiRows 'Shortcut' @('Component_','Directory_','Name','Target','Arguments') @('component','directory','name','target','arguments') @());
 remove_files=@(Read-MsiRows 'RemoveFile' @('Component_','FileName','DirProperty','InstallMode') @('component','filename','directory','mode') @(3));
 media=@(Read-MsiRows 'Media' @('Cabinet','Source') @('cabinet','source') @());
 upgrades=@(Read-MsiRows 'Upgrade' @('UpgradeCode') @('upgrade_code') @());
 reg_locators=@(Read-MsiRows 'RegLocator' @('Signature_','Root','Key','Name') @('id','root','key','name') @(1));
 app_search=@(Read-MsiRows 'AppSearch' @('Property','Signature_') @('property','signature') @());
 signatures=@(Read-MsiRows 'Signature' @('Signature') @('id') @());
 execute_sequence=@(Read-MsiRows 'InstallExecuteSequence' @('Action','Condition','Sequence') @('action','condition','sequence') @(2));
 ui_events=@(Read-MsiRows 'ControlEvent' @('Dialog_','Control_','Event','Argument','Condition') @('dialog','control','event','argument','condition') @())};
}finally{if($view){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)};if($database){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($database)};if($installer){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($installer)}}
"""

KNOWN_FOLDER_OBSERVATION = r"""$result=@{desktop=[Environment]::GetFolderPath('Desktop');common_desktop=[Environment]::GetFolderPath('CommonDesktopDirectory');
programs=[Environment]::GetFolderPath('Programs');common_programs=[Environment]::GetFolderPath('CommonPrograms')};
foreach($folder in $result.Values){if([string]::IsNullOrWhiteSpace([string]$folder) -or -not [IO.Directory]::Exists([string]$folder)){throw 'Actual Windows known folder is unavailable'}}
"""

HOST_OBSERVATION = r"""$installations=@();$installPaths=@();$related=@();$shortcuts=@();$webviews=@();
foreach($base in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall','HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')){
 if(Test-Path -LiteralPath $base){foreach($reg in Get-ChildItem -LiteralPath $base){$p=Get-ItemProperty -LiteralPath $reg.PSPath;
  if($p.DisplayName -match '^(司忆|Agent|Siyi)$' -or $reg.PSChildName -match '^(司忆|Agent|Siyi)$'){$installations+=@{registry_key=[string]$reg.Name;name=[string]$p.DisplayName;version=[string]$p.DisplayVersion;publisher=[string]$p.Publisher;path=[string]$p.InstallLocation;uninstall=[string]$p.UninstallString;product_code=if($p.WindowsInstaller -eq 1){[string]$reg.PSChildName}else{$null}}}
 }}
}
foreach($reg in @('HKCU:\Software\github\司忆','HKLM:\Software\github\司忆','HKCU:\Software\github\Agent','HKLM:\Software\github\Agent','HKCU:\Software\WOW6432Node\github\司忆','HKLM:\Software\WOW6432Node\github\司忆','HKCU:\Software\WOW6432Node\github\Agent','HKLM:\Software\WOW6432Node\github\Agent')){
 if(Test-Path -LiteralPath $reg){$p=Get-Item -LiteralPath $reg;$installPaths+=@{key=$reg;path=[string]$p.GetValue('InstallDir',[string]$p.GetValue('',''));value_names=@($p.GetValueNames());subkey_count=[int]$p.SubKeyCount}}
}
$installer=$null;try{$installer=New-Object -ComObject WindowsInstaller.Installer;
 foreach($code in $installer.RelatedProducts('{F769324D-235D-532C-995A-C14A256F4067}')){$related+=@{product_code=[string]$code;upgrade_code='{F769324D-235D-532C-995A-C14A256F4067}';version=[string]$installer.ProductInfo($code,'VersionString');path=[string]$installer.ProductInfo($code,'InstallLocation')}}
}finally{if($installer){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($installer)}}
foreach($base in @('HKCU:\Software\Microsoft\EdgeUpdate\Clients','HKLM:\Software\Microsoft\EdgeUpdate\Clients','HKLM:\Software\WOW6432Node\Microsoft\EdgeUpdate\Clients')){
 $reg=Join-Path $base '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';if(Test-Path -LiteralPath $reg){$p=Get-ItemProperty -LiteralPath $reg;if($p.pv){$webviews+=[string]$p.pv}}
}
$shell=$null;try{$shell=New-Object -ComObject WScript.Shell;
 foreach($folder in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('CommonDesktopDirectory'),[Environment]::GetFolderPath('Programs'),[Environment]::GetFolderPath('CommonPrograms'))){
 if([string]::IsNullOrWhiteSpace([string]$folder) -or -not [IO.Directory]::Exists([string]$folder)){throw 'Actual Windows known shortcut folder is unavailable'};
 foreach($name in @('司忆.lnk','司忆.exe.lnk','Agent.lnk','Siyi.lnk','司忆\司忆.lnk','司忆\Uninstall 司忆.lnk')){
 $path=Join-Path $folder $name;if(Test-Path -LiteralPath $path){$p=Get-Item -LiteralPath $path;if($p.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Shortcut reparse forbidden'};
 $link=$shell.CreateShortcut($path);$shortcuts+=@{path=$path;target=[string]$link.TargetPath;arguments=[string]$link.Arguments;sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()}}
 }}
}finally{if($shell){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)}}
$running=@(Get-CimInstance Win32_Process -Filter "Name='司忆.exe' OR Name='Agent.exe' OR Name='agent-backend.exe'"|ForEach-Object {@{pid=[int]$_.ProcessId;name=[string]$_.Name}});
$identity=[Security.Principal.WindowsIdentity]::GetCurrent();$principal=New-Object Security.Principal.WindowsPrincipal($identity);
$result=@{installations=$installations;install_paths=$installPaths;related_products=$related;shortcuts=$shortcuts;webviews=$webviews;running=$running;
 is_admin=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);installer_busy=(Test-Path -LiteralPath 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Installer\InProgress')};
"""

OLD_DESKTOP_OBSERVATION = r"""$process=Get-Process -Id ([int]$env:SIYI_DESKTOP_PID) -ErrorAction Stop;
$children=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.Id) AND Name='agent-backend.exe'"|ForEach-Object {[int]$_.ProcessId});
$result=@{main_window=[long]$process.MainWindowHandle;sidecars=$children};
"""
