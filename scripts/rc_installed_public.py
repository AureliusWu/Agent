"""Independent, path-free installer receipts; private argv/logs remain untouched.

This module never installs, builds, uploads or grants RC acceptance. The pure
builder also supports complete synthetic observations, without promoting them.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

VERSION = "16.0.0"
PROTOCOL = "installed-public-v1"
PATH_SCOPE = "fixture-install-v1"
STAGES = ("install_previous", "launch_previous", "seed_fixture", "upgrade_current",
          "launch_upgraded", "uninstall_current", "verify_retention", "reinstall_current",
          "launch_reinstalled", "final_uninstall", "verify_final")
INSTALL_STAGES = frozenset({"install_previous", "upgrade_current", "uninstall_current",
                            "reinstall_current", "final_uninstall"})
LAUNCH_STAGES = frozenset({"launch_previous", "launch_upgraded", "launch_reinstalled"})
SOURCE_FIELDS = frozenset({"source_version", "source_commit", "workspace_clean", "source_tree_fingerprint", "build_id"})
MANIFEST_FIELDS = frozenset({"manifest_version", "product_version", "git_commit", "source_fingerprint", "build_id",
                           "database_schema_version", "component_build_ids", "workspace_state", "build_type"})
PACKAGE_FIELDS = frozenset({"name", "version", "sha256", "bytes", "product_code", "upgrade_code"})
RENDER_FIELDS = frozenset({"schema_version", "report_type", "protocol_version", "actual_run", "status", "acceptance_nonce",
                          "isolated_test_data", "desktop_render_ready", "sidecar_ready", "process_ids", "window_handle",
                          "readiness_ms", "components"})
HISTORICAL_FIELDS = frozenset({"archive_entry", "embedded_manifest_bytes", "embedded_manifest_sha256", "product_version",
                              "git_commit", "source_fingerprint", "workspace_state", "build_id", "component_build_id"})
CLEANUP_FIELDS = frozenset({"protocol_version", "active_before_cleanup", "active_after_cleanup", "forced_termination",
                          "job_handle_closed", "errors", "unassigned_cleanup_complete", "owned_process_handles_remaining",
                          "owned_thread_handles_remaining", "launch_cleanup_errors"})
RESULT_FLAGS = frozenset({"desktop_started", "sidecar_stopped", "previous_version_upgrade", "schema_migrated",
                          "in_place_upgrade_preserved_data", "uninstall_preserved_data", "reinstall", "reinstall_started",
                          "reinstall_recognized_data", "final_uninstall", "package_files_removed", "install", "uninstall",
                          "uninstall_preserved_models"})
TOP_FIELDS = frozenset({"schema_version", "report_type", "public_protocol", "target_version", "status", "actual_run",
                        "rc_eligible", "source", "source_after", "run", "artifacts", "checks", "results", "binary_sha256",
                        "sidecar_payload_sha256", "installed_sidecar_payload", "manual_desktop_acceptance",
                        "credential_manager_isolation", "automatic_registry_or_fixture_cleanup"})
SHA = re.compile(r"[0-9a-fA-F]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
BUILD_ID = re.compile(r"[0-9a-f]{24}\Z")
GUID = re.compile(r"\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}\Z")
FAMILY = "{F769324D-235D-532C-995A-C14A256F4067}"


class PublicReportError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PublicReportError(message)


def exact(value, fields, label: str) -> None:
    require(isinstance(value, dict) and set(value) == set(fields), label + " has unknown or missing fields")


def project(value, fields, label: str) -> dict:
    require(isinstance(value, dict) and set(fields) <= set(value), label + " is incomplete")
    return {field: deepcopy(value[field]) for field in sorted(fields)}


@lru_cache(maxsize=3)
def module(name: str):
    spec = importlib.util.spec_from_file_location("rc_installed_public_" + name.replace("-", "_"),
                                                 Path(__file__).with_name(name + ".py"))
    require(spec is not None and spec.loader is not None, "required validator unavailable")
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


def digest(value) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def hashes(value) -> None:
    exact(value, {"desktop", "sidecar"}, "component byte observations")
    require(all(digest(item) for item in value.values()), "component SHA-256 is invalid")


def cleanup(value) -> None:
    exact(value, CLEANUP_FIELDS, "Job cleanup")
    require(value["protocol_version"] == "exact-native-job-v1" and value["forced_termination"] is False
            and value["job_handle_closed"] is True and value["unassigned_cleanup_complete"] is True
            and value["errors"] == [] and value["launch_cleanup_errors"] == [], "Job cleanup is not normal and complete")
    for field in ("active_before_cleanup", "active_after_cleanup", "owned_process_handles_remaining", "owned_thread_handles_remaining"):
        require(type(value[field]) is int and value[field] == 0, "Job accounting is unknown or nonzero")


def inventory(value, *, scoped: bool) -> str:
    fields = {"schema_version", "report_type", "binary", "file_count", "total_bytes", "entries", "payload_content_sha256"}
    exact(value, fields | ({"path_scope"} if scoped else set()), "complete payload inventory")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["report_type"] == "rc_sidecar_payload_inventory", "typed payload inventory required")
    exact(value["binary"], {"path", "sha256"}, "inventory binary")
    require(digest(value["binary"]["sha256"]), "inventory binary SHA-256 invalid")
    transport = module("export-accepted-rc")
    binary_path = transport.canonical_path(value["binary"]["path"])
    if scoped:
        require(value["path_scope"] == PATH_SCOPE and binary_path == "install/agent-backend.exe",
                "installed inventory has an unknown fixture scope or binary path")
    else:
        transport.public_path(binary_path)
    require(isinstance(value["entries"], list), "inventory entries required")
    for entry in value["entries"]:
        exact(entry, {"path", "bytes", "sha256"}, "inventory entry")
        path = transport.canonical_path(entry["path"])
        if not scoped:
            transport.public_path(path)
    summary = module("rc_payload_inventory").content_summary(value)
    require(type(value["file_count"]) is int and type(value["total_bytes"]) is int
            and value["payload_content_sha256"] == summary, "payload content summary differs")
    return summary


def manifest(value, *, version: str) -> None:
    exact(value, MANIFEST_FIELDS, "component manifest")
    require(type(value["manifest_version"]) is int and value["manifest_version"] == 1
            and value["product_version"] == version and COMMIT.fullmatch(str(value["git_commit"])) is not None
            and digest(value["source_fingerprint"]) and BUILD_ID.fullmatch(str(value["build_id"])) is not None
            and value["workspace_state"] == "CLEAN" and value["build_type"] == "Release"
            and type(value["database_schema_version"]) is int and value["database_schema_version"] > 0,
            "clean source-bound component manifest required")
    exact(value["component_build_ids"], {"react", "tauri", "sidecar"}, "component build IDs")
    require(all(value["component_build_ids"][name] == name + "-" + value["build_id"]
                for name in ("react", "tauri", "sidecar")), "component build IDs differ")


def historical_manifest(value, *, version: str) -> None:
    require(isinstance(value, dict) and set(value) <= MANIFEST_FIELDS and value.get("product_version") == version,
            "available historical manifest identity differs")
    for name, item in value.items():
        if name in {"manifest_version", "database_schema_version"}:
            require(item is None or (type(item) is int and item > 0), "historical numeric identity invalid")
        elif name == "component_build_ids":
            require(item is None or (isinstance(item, dict) and set(item) <= {"react", "tauri", "sidecar"}
                    and all(isinstance(part, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", part) for part in item.values())),
                    "historical component identity invalid")
        else:
            require(item is None or (isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", item)),
                    "historical scalar identity invalid")


def installed_identity(value, *, previous: bool, version: str, locked: dict) -> None:
    require(isinstance(value, dict), "installed identity required")
    if not previous:
        exact(value, {"identity_mode", "manifest"}, "current installed identity")
        require(value["identity_mode"] == "current_onedir_manifest" and value["manifest"] == locked,
                "actual installed component manifest differs")
    elif value.get("identity_mode") == "previous_onedir_manifest":
        exact(value, {"identity_mode", "manifest", "current_candidate_manifest_qualification"}, "previous onedir identity")
        historical_manifest(value["manifest"], version=version)
        require(value["current_candidate_manifest_qualification"] is False, "historical identity is not candidate qualification")
    else:
        exact(value, {"identity_mode", "observation", "current_candidate_manifest_qualification"}, "previous onefile identity")
        require(value["identity_mode"] == "legacy_onefile_embedded_manifest"
                and value["current_candidate_manifest_qualification"] is False, "historical identity protocol differs")
        observation = value["observation"]
        exact(observation, HISTORICAL_FIELDS, "previous embedded observation")
        require(observation["archive_entry"] == "build-info.json"
                and type(observation["embedded_manifest_bytes"]) is int and 0 < observation["embedded_manifest_bytes"] <= 128 * 1024
                and digest(observation["embedded_manifest_sha256"]), "previous embedded observation invalid")
        historical_manifest({name: observation[name] for name in observation if name in MANIFEST_FIELDS}, version=version)
        identifier = observation["component_build_id"]
        require(identifier is None or (isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identifier)),
                "historical sidecar identity invalid")


def render(value, *, actual: bool, locked: dict) -> None:
    exact(value, RENDER_FIELDS, "actual render observation")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["report_type"] == "rc_desktop_runtime_observation" and value["protocol_version"] == "desktop-render-ready-v1"
            and value["actual_run"] is actual and value["status"] == ("PASS" if actual else "SYNTHETIC_PASS")
            and value["isolated_test_data"] is True and value["desktop_render_ready"] is True and value["sidecar_ready"] is True,
            "render observation cannot promote synthetic or partial readiness")
    require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(value["acceptance_nonce"])) is not None,
            "render nonce invalid")
    exact(value["process_ids"], {"desktop", "sidecar"}, "render process identities")
    require(all(type(item) is int and item > 0 for item in value["process_ids"].values())
            and type(value["window_handle"]) is int and 0 < value["window_handle"] < 2 ** 64
            and type(value["readiness_ms"]) is int and value["readiness_ms"] > 0, "render observation lacks positive native identities")
    exact(value["components"], {"react", "tauri", "sidecar"}, "render components")
    require(all(component == locked for component in value["components"].values()), "render components disagree with candidate")


def validate_public_report(value: dict, *, candidate_sidecar: dict, candidate_payload: dict) -> None:
    """Validate only the typed public envelope; this never grants acceptance."""
    exact(value, TOP_FIELDS, "public installer receipt")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["public_protocol"] == PROTOCOL and value["target_version"] == VERSION,
            "public installer protocol differs")
    exact(candidate_sidecar, {"path", "sha256"}, "accepted sidecar reference")
    module("export-accepted-rc").public_path(candidate_sidecar["path"])
    require(digest(candidate_sidecar["sha256"]), "accepted sidecar SHA-256 invalid")
    expected_payload = inventory(candidate_payload, scoped=False)
    require(candidate_payload["binary"] == candidate_sidecar, "accepted inventory binary binding differs")
    observed_payload = inventory(value["installed_sidecar_payload"], scoped=True)
    hashes(value["binary_sha256"])
    require(value["binary_sha256"]["sidecar"] == candidate_sidecar["sha256"]
            and value["installed_sidecar_payload"]["binary"]["sha256"] == candidate_sidecar["sha256"]
            and value["sidecar_payload_sha256"] == expected_payload == observed_payload,
            "installed bytes differ from accepted candidate")
    exact(value["source"], SOURCE_FIELDS, "source identity")
    source = value["source"]
    require(value["source_after"] == source and source["source_version"] == VERSION and source["workspace_clean"] is True
            and COMMIT.fullmatch(str(source["source_commit"])) is not None and digest(source["source_tree_fingerprint"])
            and BUILD_ID.fullmatch(str(source["build_id"])) is not None, "installer source changed or is not clean")
    exact(value["run"], {"installer_kind", "isolated_test_data", "owner_run_id", "fixture_retained", "operator_attested"}, "run")
    kind = value["run"]["installer_kind"]
    require(kind in {"NSIS", "MSI"} and value["run"]["isolated_test_data"] is True
            and value["run"]["fixture_retained"] is True and value["run"]["operator_attested"] is False
            and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(value["run"]["owner_run_id"])) is not None,
            "owned isolated lifecycle required")
    actual = value["actual_run"]
    require(type(actual) is bool and value["rc_eligible"] is actual
            and value["status"] == ("PASS" if actual else "SYNTHETIC_PASS")
            and value["report_type"] == (f"release_{kind.lower()}_installer_live_evidence" if actual else "synthetic_installer_lifecycle"),
            "public receipt cannot promote synthetic acceptance")
    exact(value["artifacts"], {"candidate", "previous", "build_manifest", "previous_build_manifest", "previous_schema_observation"}, "artifacts")
    artifacts = value["artifacts"]
    for label in ("candidate", "previous"):
        package = artifacts[label]
        exact(package, PACKAGE_FIELDS, "package")
        name = module("export-accepted-rc").canonical_path(package["name"])
        suffix = "-setup.exe" if kind == "NSIS" else ".msi"
        require("/" not in name and name.endswith(suffix) and digest(package["sha256"])
                and type(package["bytes"]) is int and package["bytes"] > 0
                and re.fullmatch(r"\d+\.\d+\.\d+", str(package["version"])) is not None, "package identity invalid")
        if kind == "MSI":
            require(GUID.fullmatch(str(package["product_code"])) is not None and package["upgrade_code"] == FAMILY,
                    "MSI identity differs")
        else:
            require(package["product_code"] is None and package["upgrade_code"] is None, "NSIS must not invent MSI identities")
    previous_version = artifacts["previous"]["version"]
    require(artifacts["candidate"]["version"] == VERSION and tuple(map(int, previous_version.split("."))) < (16, 0, 0)
            and artifacts["candidate"]["sha256"] != artifacts["previous"]["sha256"]
            and (kind != "MSI" or artifacts["candidate"]["product_code"] != artifacts["previous"]["product_code"]),
            "distinct older package required")
    manifest(artifacts["build_manifest"], version=VERSION)
    historical_manifest(artifacts["previous_build_manifest"], version=previous_version)
    locked = artifacts["build_manifest"]
    require(locked["git_commit"] == source["source_commit"] and locked["source_fingerprint"] == source["source_tree_fingerprint"]
            and locked["build_id"] == source["build_id"], "manifest source differs")
    previous_schema = artifacts["previous_schema_observation"]
    require(type(previous_schema) is int and 0 < previous_schema < locked["database_schema_version"]
            and artifacts["previous_build_manifest"].get("database_schema_version") in {None, previous_schema},
            "observed previous owned schema differs")
    exact(value["checks"], STAGES, "lifecycle stages")
    render_nonces = set()
    for stage in STAGES:
        check = value["checks"][stage]
        exact(check, {"passed", "result"}, "stage check")
        require(check["passed"] is True, "stage failed")
        result = check["result"]
        if stage in INSTALL_STAGES:
            exact(result, {"passed", "exit_code", "process_cleanup"}, "installer stage")
            require(type(result["exit_code"]) is int and result["exit_code"] == 0, "installer did not exit normally")
            cleanup(result["process_cleanup"])
        elif stage in LAUNCH_STAGES:
            exact(result, {"passed", "protocol", "previous_observation_is_not_render_acceptance", "binary_sha256",
                           "sidecar_payload_sha256", "process_cleanup", "desktop_exit_code", "sidecar_exit_code",
                           "installed_build_identity", "render_observation"}, "launch stage")
            previous = stage == "launch_previous"
            require(result["protocol"] == ("previous-installed-owned-process-v1" if previous else "desktop-render-ready-v1")
                    and result["previous_observation_is_not_render_acceptance"] is previous, "launch protocol differs")
            hashes(result["binary_sha256"])
            require((previous and result["sidecar_payload_sha256"] is None) or digest(result["sidecar_payload_sha256"]), "launch payload digest invalid")
            installed_identity(result["installed_build_identity"], previous=previous, version=previous_version if previous else VERSION, locked=locked)
            if previous:
                require(result["render_observation"] is None, "previous launch is not current render acceptance")
                identity = result["installed_build_identity"]
                observed = identity.get("manifest", identity.get("observation", {}))
                for field in MANIFEST_FIELDS - {"database_schema_version"}:
                    available = artifacts["previous_build_manifest"].get(field)
                    require(available is None or observed.get(field) == available,
                            "historical package and installed identity observations differ")
            if not previous:
                require(result["binary_sha256"] == value["binary_sha256"]
                        and result["sidecar_payload_sha256"] == expected_payload, "candidate component observation differs")
                render(result["render_observation"], actual=actual, locked=locked)
                nonce = result["render_observation"]["acceptance_nonce"]
                require(nonce not in render_nonces, "render observation was reused across current launches")
                render_nonces.add(nonce)
            for field in ("desktop_exit_code", "sidecar_exit_code"):
                require(type(result[field]) is int and result[field] == 0, "component did not exit normally")
            cleanup(result["process_cleanup"])
        elif stage == "seed_fixture":
            exact(result, {"passed", "old_schema"}, "seed observation")
            require(type(result["old_schema"]) is int and result["old_schema"] == previous_schema,
                    "seed schema differs")
        else:
            exact(result, {"passed", "schema", "conversation_preserved", "message_preserved", "model_fixture_preserved", "migration_backup_count"}, "retention observation")
            require(type(result["schema"]) is int and result["schema"] == locked["database_schema_version"]
                    and all(result[name] is True for name in ("conversation_preserved", "message_preserved", "model_fixture_preserved"))
                    and type(result["migration_backup_count"]) is int and result["migration_backup_count"] > 0, "retained fixture failed")
        require(result["passed"] is True, "stage result failed")
    exact(value["results"], RESULT_FLAGS | {"status", "version"}, "lifecycle summary")
    require(value["results"]["status"] == "ok" and value["results"]["version"] == VERSION
            and all(value["results"][name] is True for name in RESULT_FLAGS), "lifecycle summary failed")
    require(value["manual_desktop_acceptance"] == "NOT_RECORDED"
            and value["credential_manager_isolation"] == "NOT_ISOLATED_SAME_WINDOWS_USER"
            and value["automatic_registry_or_fixture_cleanup"] is False, "public receipt expands acceptance scope")


def build_public_report(private_report: dict, *, candidate_sidecar: dict, candidate_payload: dict) -> dict:
    """Capture a new whitelist receipt without mutating the original report."""
    value = project(private_report, TOP_FIELDS - {"public_protocol"}, "private lifecycle")
    value["public_protocol"] = PROTOCOL
    value["source"] = project(private_report["source"], SOURCE_FIELDS, "source")
    require(private_report["source_after"] == private_report["source"], "private source changed")
    value["source_after"] = deepcopy(value["source"])
    value["run"] = project(private_report["run"], {"installer_kind", "isolated_test_data", "owner_run_id", "fixture_retained", "operator_attested"}, "run")
    value["artifacts"] = {label: project(private_report["artifacts"][label],
                            PACKAGE_FIELDS if label in {"candidate", "previous"} else MANIFEST_FIELDS, "artifact")
                          for label in ("candidate", "previous", "build_manifest")}
    old = private_report["artifacts"]["previous_build_manifest"]
    require(isinstance(old, dict), "historical manifest observations required")
    value["artifacts"]["previous_build_manifest"] = {name: deepcopy(item) for name, item in old.items() if name in MANIFEST_FIELDS}
    value["artifacts"]["previous_schema_observation"] = private_report["artifacts"]["previous_schema_observation"]
    value["results"] = project(private_report["results"], RESULT_FLAGS | {"status", "version"}, "results")
    installed = deepcopy(private_report["installed_sidecar_payload"])
    # Legacy collector inventory is already inline and fixture-rooted. Add a
    # type/scope declaration, not a rewritten command or attachment reference.
    require("path_scope" not in installed, "private inventory already has an untrusted scope")
    installed["path_scope"] = PATH_SCOPE
    value["installed_sidecar_payload"] = installed
    exact(private_report["checks"], STAGES, "private lifecycle stages")
    checks = {}
    for stage in STAGES:
        raw = private_report["checks"][stage]
        require(isinstance(raw, dict) and raw.get("passed") is True and isinstance(raw.get("result"), dict), "private stage failed")
        observation = raw["result"]
        require("actual_run" not in observation or observation["actual_run"] is value["actual_run"],
                "private stage actual-run identity differs")
        require(not (observation.get("synthetic_only") is True and value["actual_run"] is True),
                "synthetic stage cannot authorize an actual receipt")
        if stage in INSTALL_STAGES:
            result = project(observation, {"passed", "exit_code", "process_cleanup"}, "private installer result")
        elif stage in LAUNCH_STAGES:
            result = project(observation, {"passed", "protocol", "previous_observation_is_not_render_acceptance", "binary_sha256",
                                         "sidecar_payload_sha256", "process_cleanup", "desktop_exit_code", "sidecar_exit_code"}, "private launch result")
            previous = stage == "launch_previous"
            observed = deepcopy(observation.get("installed_sidecar_payload"))
            if not (previous and observed is None and result["sidecar_payload_sha256"] is None):
                require(isinstance(observed, dict) and "path_scope" not in observed, "private launch inventory required")
                observed["path_scope"] = PATH_SCOPE
                summary = inventory(observed, scoped=True)
                require(summary == observation["sidecar_payload_sha256"]
                        and observed["binary"]["sha256"] == observation["binary_sha256"]["sidecar"], "private launch inventory differs")
            identity = observation["installed_build_identity"]
            mode = identity.get("identity_mode")
            if mode == "legacy_onefile_embedded_manifest":
                result["installed_build_identity"] = {"identity_mode": mode, "current_candidate_manifest_qualification": identity["current_candidate_manifest_qualification"],
                                                       "observation": project(identity["observation"], HISTORICAL_FIELDS, "historical observation")}
            else:
                fields = set(identity["manifest"]) & MANIFEST_FIELDS if previous else MANIFEST_FIELDS
                result["installed_build_identity"] = {"identity_mode": mode, "manifest": project(identity["manifest"], fields, "installed manifest")}
                if previous:
                    result["installed_build_identity"]["current_candidate_manifest_qualification"] = identity["current_candidate_manifest_qualification"]
            if previous:
                require(observation.get("render_receipt") is None, "previous launch cannot record current render acceptance")
                result["render_observation"] = None
            else:
                receipt = project(observation["render_receipt"], RENDER_FIELDS, "render receipt")
                receipt["components"] = {name: project(component, MANIFEST_FIELDS, "render component")
                                          for name, component in receipt["components"].items()}
                result["render_observation"] = receipt
        elif stage == "seed_fixture":
            require(observation.get("passed") is True, "private seed failed")
            result = {"passed": True, "old_schema": observation["fixture"]["old_schema"]}
        else:
            retained = observation["retained"]
            require(observation.get("passed") is True and retained.get("passed") is True
                    and isinstance(retained.get("migration_backups"), list), "private retention failed")
            result = project(retained, {"passed", "schema", "conversation_preserved", "message_preserved", "model_fixture_preserved"}, "retention")
            result["migration_backup_count"] = len(retained["migration_backups"])
        if "process_cleanup" in result:
            result["process_cleanup"] = project(result["process_cleanup"], CLEANUP_FIELDS, "private Job cleanup")
        checks[stage] = {"passed": raw["passed"], "result": result}
    value["checks"] = checks
    validate_public_report(value, candidate_sidecar=candidate_sidecar, candidate_payload=candidate_payload)
    return value


def load_candidate(root: Path, *, candidate_sidecar: dict, candidate_payload: dict) -> dict:
    """Bind the whitelist envelope to actual complete candidate bytes."""
    transport = module("export-accepted-rc")
    for reference in (candidate_sidecar, candidate_payload):
        exact(reference, {"path", "sha256"}, "candidate reference")
        name = transport.public_path(reference["path"])
        require(digest(reference["sha256"]), "candidate reference digest invalid")
        _, actual = transport.checked_hash(root / name, name, started=time.monotonic())
        require(actual == reference["sha256"].lower(), "candidate reference bytes changed")
    value = transport.read_object(root / candidate_payload["path"])
    inventory(value, scoped=False)
    actual = module("rc_payload_inventory").inventory(root, candidate_sidecar)
    require(value == actual, "accepted complete candidate payload differs from actual files")
    return value


def write_public_report(root: Path, output: Path, private_report: dict, *, candidate_sidecar: dict, candidate_payload: dict) -> dict:
    """Fresh fixed-path public stream; never rewrite the private receipt."""
    root, output = root.absolute(), output.absolute()
    kind = private_report.get("run", {}).get("installer_kind")
    require(kind in {"NSIS", "MSI"}, "installer kind required")
    fixed = root / f"build/v1600-evidence/{kind.lower()}-installer-smoke.json"
    require(output == fixed, "public installer output must use its fixed smoke path")
    transport = module("export-accepted-rc")
    transport.no_links(output)
    require(not output.exists(), "public installer output must be fresh")
    payload = load_candidate(root, candidate_sidecar=candidate_sidecar, candidate_payload=candidate_payload)
    result = build_public_report(private_report, candidate_sidecar=candidate_sidecar, candidate_payload=payload)
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    transport.public_content(fixed.relative_to(root).as_posix(), encoded)
    output.parent.mkdir(parents=True, exist_ok=True)
    transport.no_links(output)
    with output.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return result
