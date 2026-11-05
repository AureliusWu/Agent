"""Explicit hosted-Windows candidate preparation and real installed observations.

This is not rc-acceptance.yml and never exports an accepted RC. All installer
effects occur only inside the existing owned lifecycle on a fresh hosted VM.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
from rc_desktop_candidate import (BUILD, INVENTORY, JOBS, MANIFEST, PORTABLE, PREFIX, ROOT,
    CandidateError, current, digest, download_prior, environment_identity, fetch, jobs,
    failure_projection, make_index, module, publish, read, reference, require, safety_code, select_job, write)

DIAGNOSTICS = {"stage": "entry"}


def note(stage=None, **values):
    if stage is not None:
        DIAGNOSTICS["stage"] = stage
    DIAGNOSTICS.update(values)


def public_failure(mode, error):
    boundary = Path(os.environ.get("RUNNER_TEMP", ""))
    require(boundary.is_absolute() and boundary.is_dir(), "ordinary RUNNER_TEMP required for typed failure")
    module("export-accepted-rc").no_links(boundary)
    value = failure_projection(mode, DIAGNOSTICS.get("stage"), error, DIAGNOSTICS)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    module("export-accepted-rc").public_content("failure.json", encoded)
    write(boundary / "desktop-failure-public/failure.json", value)


def owned_dispatch_diagnostic(adapter):
    """Only typed fields are later projected; never publish these raw records."""
    candidates = list(adapter.probes.glob("owned-command-*.json"))
    if not 0 < len(candidates) <= 200:
        return
    latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    if latest.stat().st_size <= 1024**2:
        note(owned_dispatch=read(latest))


def observing_types(lifecycle):
    """Observe original calls/results/errors only; no validator or cleanup changes."""
    class ObservedLifecycle(lifecycle.Lifecycle):
        def transition(self, stage):
            try:
                return super().transition(stage)
            except lifecycle.SafetyError as error:
                try:
                    note(lifecycle_guard={"failed_stage": stage, "guard_code": safety_code(error, lifecycle.SafetyError)})
                    args = error.args
                    if len(args) == 1 and type(args[0]) is str and len(args[0]) <= 65536:
                        write(self.plan.output.parent / "private-transition-failure.json", {"stage": stage, "message": args[0]})
                except Exception:
                    pass
                raise

    class ObservedAdapter(lifecycle.WindowsAdapter):
        def owned_command(self, executable, arguments, environment, *, timeout=120, mutation=False):
            before, failure = None, None
            if mutation:
                note(last_mutation={})  # Never relabel an earlier mutation as the newest one.
                try:
                    before = set(self.probes.glob("owned-command-*.json"))
                except Exception:
                    pass
            try:
                return super().owned_command(executable, arguments, environment, timeout=timeout, mutation=mutation)
            except BaseException as error:
                failure = error
                raise
            finally:
                if mutation:
                    try:
                        fresh = set(self.probes.glob("owned-command-*.json")) - before if before is not None else set()
                        if len(fresh) == 1:
                            path = fresh.pop()
                            if path.stat().st_size <= 1024**2:
                                original = read(path)
                                recorded = dict(original)
                                recorded["timed_out"] = (True if isinstance(failure, (TimeoutError, subprocess.TimeoutExpired))
                                    or original.get("failure_type") in {"TimeoutError", "TimeoutExpired"}
                                    else False if original.get("failure_type") is None else None)
                                note(last_mutation=recorded)
                    except Exception:
                        pass
    return ObservedLifecycle, ObservedAdapter


def source(expected):
    value = module("generate_build_info")._release_source_identity(ROOT)
    require(value.get("source_commit") == expected["source_commit"] and value.get("workspace_clean") is True
            and value.get("source_version") == "16.0.1", "exact current clean v16.0.1 source required")
    return value


def private_directory(role):
    require(os.name == "nt", "native Windows required")
    boundary = Path(os.environ.get("RUNNER_TEMP", ""))
    require(boundary.is_absolute() and boundary.is_dir() and not any(c.isspace() for c in str(boundary)), "ordinary no-space RUNNER_TEMP required")
    module("export-accepted-rc").no_links(boundary)
    output = boundary / ("siyi-desktop-" + role + "-" + uuid.uuid4().hex)
    output.mkdir()
    return output


def disk_preflight(private, minimum_gib):
    free = shutil.disk_usage(ROOT).free
    value = {"free_bytes": free, "minimum_bytes": minimum_gib * 1024**3}
    note(disk=value)
    write(private / "disk-preflight.json", value)
    require(free >= minimum_gib * 1024**3, "insufficient disk for bounded candidate operation")


def native_desktop():
    """Require an actual visible window station/input desktop, never headless."""
    require(os.name == "nt", "native desktop required")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    class Flags(ctypes.Structure):
        _fields_ = [("inherit", wintypes.BOOL), ("reserved", wintypes.BOOL), ("flags", wintypes.DWORD)]
    user32.GetProcessWindowStation.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    flags, length, session = Flags(), wintypes.DWORD(), wintypes.DWORD()
    station = user32.GetProcessWindowStation()
    native = {"visible_window_station": bool(station) and bool(user32.GetUserObjectInformationW(station, 1, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(length))) and bool(flags.flags & 1)}
    note(native=native)
    require(native["visible_window_station"], "visible native window station unavailable")
    native["non_service_session"] = bool(kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))) and session.value != 0
    require(native["non_service_session"], "interactive native session unavailable")
    desktop = user32.OpenInputDesktop(0, False, 1)
    native["input_desktop_opened"] = bool(desktop)
    require(native["input_desktop_opened"], "input desktop unavailable")
    native["input_desktop_closed"] = bool(user32.CloseDesktop(desktop))
    require(native["input_desktop_closed"], "native desktop handle did not close")
    return {"visible_window_station": True, "input_desktop_opened_and_closed": True, "non_service_session": True,
            "actual_render_acceptance": "NOT_YET_OBSERVED"}


def process_environment():
    # Build tooling needs its installed caches. It never receives workflow
    # credentials, deployment settings, paid Provider configuration or .env.
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "COMPUTERNAME",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROGRAMW6432", "OS", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
               "CARGO_HOME", "RUSTUP_HOME", "PYTHONHOME", "PYTHONPATH", "PYTHON_ROOT", "VIRTUAL_ENV"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8", CI="true",
                  SIYI_ALLOW_PAID_API="false", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    return result


def execute(argv, cwd, private, label, *, environment=None, timeout=7200):
    log = private / (label + ".log")
    execution = {"schema_version": 1, "report_type": "private_candidate_subprocess", "actual_run": False,
                 "command": argv, "cwd": str(cwd), "started_at": datetime.now(timezone.utc).isoformat(),
                 "exit_code": None, "timed_out": False}
    execution_path = private / (label + "-execution.json")
    try:
        with log.open("xb") as stream:
            process = subprocess.Popen(argv, cwd=cwd, env=environment or process_environment(), stdout=stream,
                                       stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            execution["actual_run"] = True
            try:
                execution["exit_code"] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
                raise
    except subprocess.TimeoutExpired:
        execution["timed_out"] = True
        raise
    finally:
        execution["finished_at"] = datetime.now(timezone.utc).isoformat()
        write(execution_path, execution)
        log_sha = digest(log) if log.exists() and log.stat().st_size <= 256 * 1024**2 else None
        note(subprocess={"label": label, "actual_run": execution["actual_run"], "exit_code": execution["exit_code"],
                         "timed_out": execution["timed_out"], "log_sha256": log_sha})
    require(execution["exit_code"] == 0, "candidate subprocess failed; private log retained")
    require(log.stat().st_size > 0, "actual subprocess output missing")
    return {"raw_execution_sha256": digest(execution_path), "raw_log_sha256": digest(log)}


def build_shell(private):
    shell = Path(os.environ.get("PROGRAMFILES", "")) / "PowerShell/7/pwsh.exe"
    require(shell.is_absolute(), "trusted PowerShell 7 required")
    module("export-accepted-rc").ordinary(shell)
    execute([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.Major"],
            ROOT, private, "candidate-shell-probe", timeout=30)
    value = (private / "candidate-shell-probe.log").read_bytes()
    require(len(value) <= 32 and value.strip().isdigit() and int(value.strip()) >= 7, "trusted PowerShell 7 required")
    return str(shell)


def validate_payload(expected):
    actual = source(expected)
    manifest = read(ROOT / MANIFEST)
    require(manifest.get("product_version") == "16.0.1" and manifest.get("workspace_state") == "CLEAN"
            and manifest.get("build_type") == "Release" and manifest.get("git_commit") == actual["source_commit"]
            and manifest.get("source_fingerprint") == actual["source_tree_fingerprint"], "candidate manifest does not match current clean source")
    binaries = {key: reference(ROOT / PORTABLE / name) for key, name in (("desktop", "司忆.exe"), ("sidecar", "agent-backend.exe"))}
    gate = module("rc_gate")
    for ref in binaries.values():
        gate.executable_attachment(ROOT, ref)
    gate.validate_sidecar_payload(ROOT, reference(ROOT / INVENTORY), binaries["sidecar"])
    payload = read(ROOT / INVENTORY)
    require(read(ROOT / PORTABLE / "_internal/build-info.json") == manifest, "frozen complete manifest differs")
    observation = read(ROOT / BUILD)
    gate.bound_source(observation, actual)
    require(observation.get("report_type") == "candidate_desktop_build_observation" and observation.get("actual_run") is True
            and observation.get("status") == "BUILT_NOT_RELEASED" and observation.get("exit_code") == 0
            and type(observation.get("exit_code")) is int
            and observation.get("timed_out") is False and observation.get("build_id") == manifest["build_id"]
            and observation.get("binary_sha256") == {key: ref["sha256"] for key, ref in binaries.items()}
            and observation.get("sidecar_payload_sha256") == payload["payload_content_sha256"]
            and observation.get("accepted_candidate") is False, "actual build observation missing or mismatched")
    for stage in ("private_build_execution", "private_bundle_execution"):
        value = observation.get(stage)
        require(isinstance(value, dict) and set(value) == {"raw_execution_sha256", "raw_log_sha256"}
                and all(isinstance(sha, str) and len(sha) == 64 and set(sha) <= set("0123456789abcdef") for sha in value.values()),
                "actual private command/log digest receipt required")
    packages = {}
    for kind in ("nsis", "msi"):
        matches = list((ROOT / "desktop/src-tauri/target/release/bundle" / kind).glob("*-setup.exe" if kind == "nsis" else "*.msi"))
        require(len(matches) == 1 and 1024**2 <= matches[0].stat().st_size <= 256 * 1024**2,
                "unique package inside original lifecycle byte bound required")
        packages[kind] = matches[0]
        require(observation.get("packages", {}).get(kind) == reference(matches[0]), "package differs from actual build receipt")
    return actual, manifest, binaries, payload, packages


def build(expected, private):
    note("metadata")
    run = current(expected)
    producer = select_job(jobs(expected), expected, "payload", completed=False)
    before = source(expected)
    disk_preflight(private, 8)
    candidate = ROOT / f"build/candidates/ci-desktop-{expected['run_id']}-{expected['run_attempt']}"
    require(not candidate.exists(), "candidate preparation must be fresh")
    for kind in ("nsis", "msi"):
        folder = ROOT / "desktop/src-tauri/target/release/bundle" / kind
        require(not folder.exists(), "refusing any stale installer output")
    note("candidate_shell")
    powershell = build_shell(private)
    relative = candidate.relative_to(ROOT).as_posix()
    build_argv = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/build-desktop.ps1", "-CandidateOnly", "-CandidateDirectory", relative]
    note("candidate_build")
    build_log = execute(build_argv, ROOT, private, "candidate-build")
    require(source(expected) == before, "source changed during candidate build")
    receipt = read(candidate / "candidate-build.json")
    manifest = read(candidate / "build-info.json")
    require(receipt.get("actual_run") is True and receipt.get("status") == "BUILT" and receipt.get("workspace_state") == "CLEAN"
            and receipt.get("source_commit") == expected["source_commit"] and receipt.get("build_id") == manifest.get("build_id"), "candidate-only actual receipt missing")
    require(isinstance(receipt.get("artifacts"), list) and len(receipt["artifacts"]) == 3
            and {row.get("path") for row in receipt["artifacts"] if isinstance(row, dict)} ==
            {"portable/司忆.exe", "portable/agent-backend.exe", "portable/_internal/build-info.json"}, "complete candidate receipt closure required")
    for ref in receipt["artifacts"]:
        require(set(ref) == {"path", "sha256"} and ref["path"] in {"portable/司忆.exe", "portable/agent-backend.exe", "portable/_internal/build-info.json"}
                and digest(candidate / ref["path"]).lower() == ref["sha256"].lower(), "candidate-only artifact changed")
    target_exe = ROOT / "desktop/src-tauri/target/release/司忆.exe"
    external_sidecar = ROOT / "desktop/src-tauri/binaries/agent-backend-x86_64-pc-windows-msvc.exe"
    require(digest(target_exe) == digest(candidate / "portable/司忆.exe") and
            digest(external_sidecar) == digest(candidate / "portable/agent-backend.exe"), "bundle inputs differ from candidate")
    candidate_sidecar = reference(candidate / "portable/agent-backend.exe")
    candidate_payload = module("rc_payload_inventory").inventory(ROOT, candidate_sidecar)
    require(read(candidate / "tauri-candidate.json") == {"build": {
        "frontendDist": "../../" + relative + "/frontend-dist", "beforeBuildCommand": "npm --prefix ../frontend run build:desktop"}},
        "candidate configuration differs from reviewed generated build-only overlay")
    base = read(ROOT / "desktop/src-tauri/tauri.conf.json")
    require(base.get("bundle", {}).get("resources") == {"binaries/_internal/": "_internal/"}
            and not base.get("build", {}).get("beforeBundleCommand")
            and not list((ROOT / "desktop/src-tauri").glob("tauri.windows.conf.*"))
            and not (ROOT / "desktop/src-tauri/Tauri.windows.toml").exists(), "unreviewed bundle inputs/hook/override")
    overlay = private / "bundle-inputs.json"
    write(overlay, {"bundle": {"resources": {"binaries/_internal/": None, (candidate / "portable/_internal").as_posix() + "/": "_internal/"}}})
    environment = process_environment()
    environment["CARGO_TARGET_DIR"] = str(ROOT / "desktop/src-tauri/target")
    bundle_argv = ["node", "frontend/node_modules/@tauri-apps/cli/tauri.js", "bundle", "--bundles", "nsis,msi",
                   "--config", str(candidate / "tauri-candidate.json"), "--config", str(overlay), "--ci"]
    note("candidate_bundle")
    bundle_log = execute(bundle_argv, ROOT / "desktop", private, "candidate-bundle", environment=environment, timeout=1800)
    require(source(expected) == before and digest(target_exe) == digest(candidate / "portable/司忆.exe")
            and digest(external_sidecar) == candidate_sidecar["sha256"]
            and module("rc_payload_inventory").inventory(ROOT, candidate_sidecar) == candidate_payload,
            "source, original bundle marker, sidecar or complete payload changed")
    snapshot = ROOT / PREFIX
    require(not snapshot.exists() and not (ROOT / MANIFEST).exists(), "public candidate snapshot must be fresh")
    snapshot.mkdir(parents=True)
    shutil.copytree(candidate / "portable", snapshot / "portable")
    shutil.copyfile(candidate / "candidate-build.json", snapshot / "candidate-build.json")
    (ROOT / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(candidate / "build-info.json", ROOT / MANIFEST)
    binaries = {key: reference(snapshot / "portable" / name) for key, name in (("desktop", "司忆.exe"), ("sidecar", "agent-backend.exe"))}
    payload = module("rc_payload_inventory").inventory(ROOT, binaries["sidecar"])
    require(payload["payload_content_sha256"] == candidate_payload["payload_content_sha256"], "public snapshot differs from original complete candidate payload")
    write(ROOT / INVENTORY, payload)
    packages = {}
    for kind in ("nsis", "msi"):
        matches = list((ROOT / "desktop/src-tauri/target/release/bundle" / kind).glob("*-setup.exe" if kind == "nsis" else "*.msi"))
        require(len(matches) == 1, "fresh unique dual installer outputs required")
        packages[kind] = reference(matches[0])
    write(ROOT / BUILD, {"schema_version": 1, "report_type": "candidate_desktop_build_observation", "actual_run": True,
        "status": "BUILT_NOT_RELEASED", "accepted_candidate": False, "source": before, "source_after": source(expected),
        "build_id": manifest["build_id"], "binary_sha256": {key: ref["sha256"] for key, ref in binaries.items()},
        "sidecar_payload_sha256": payload["payload_content_sha256"], "packages": packages,
        "entrypoint": "scripts/build-desktop.ps1", "build_mode": "CandidateOnly", "bundle_method": "tauri-existing-release-bundle",
        "private_build_execution": build_log, "private_bundle_execution": bundle_log, "exit_code": 0, "timed_out": False})
    note("candidate_validation")
    validate_payload(expected)
    require(current(expected) == run, "run provenance drifted while building")
    names = [MANIFEST, BUILD, INVENTORY, PREFIX + "/candidate-build.json", *[value["path"] for value in packages.values()]]
    names.extend(path.relative_to(ROOT).as_posix() for path in (snapshot / "portable").rglob("*") if path.is_file())
    note("public_projection")
    index = make_index("payload", names, run, producer["id"])
    publish(Path(os.environ["RUNNER_TEMP"]) / "candidate-public", index)


def observe(kind, expected, private, artifact_id):
    note("candidate_transport")
    fetch(expected, "payload", private, artifact_id=artifact_id)
    note("candidate_validation")
    actual, manifest, binaries, payload, packages = validate_payload(expected)
    disk_preflight(private, 4)
    note("native_preflight")
    write(private / "native-desktop-preflight.json", native_desktop())
    note("host_preflight")
    lifecycle = module("rc_installed_lifecycle")
    ObservedLifecycle, ObservedAdapter = observing_types(lifecycle)
    adapter = ObservedAdapter(ROOT, private / "host-preflight")
    host = adapter.host_ps("host_observation")
    note(host=host)
    write(private / "host-observation.json", host)
    require(host.get("is_admin") is True and host.get("installer_busy") is False
            and host.get("webviews") and all(host.get(name) == [] for name in ("installations", "related_products", "install_paths", "shortcuts", "running")), "host WebView2/admin/product namespace not ready")
    note("prior_download")
    previous, receipt = download_prior(kind)
    current(expected)
    request = {"schema_version": 1, "kind": kind, "candidate": reference(packages[kind]), "previous": reference(previous),
        "candidate_manifest": reference(ROOT / MANIFEST), "previous_release": reference(receipt),
        "desktop": binaries["desktop"], "sidecar": binaries["sidecar"], "payload": reference(ROOT / INVENTORY),
        "output": str(private / "owned/lifecycle.json"), "test_boundary": str(private)}
    write(private / "request.json", request)
    # These original adapters construct their own strict whitelist environment;
    # GH_TOKEN remains only in this API parent, never in installer/app processes.
    note("installed_lifecycle")
    try:
        plan = lifecycle.prepare(ROOT, request, adapter, actual)
        report = ObservedLifecycle(plan, adapter).run()
        note(lifecycle=report)
    finally:
        try:
            owned_dispatch_diagnostic(adapter)
        except Exception:
            pass  # Diagnostic absence never changes the original failure.
    require(report.get("actual_run") is True and report.get("status") == "PASS", "real installed lifecycle did not pass")
    require(source(expected) == actual, "source changed during real installation")
    run = current(expected)
    public_path = f"build/v1601-evidence/{kind}-installer-smoke.json"
    note("public_projection")
    public = read(ROOT / public_path)
    module("rc_installed_public").validate_public_report(public, candidate_sidecar=binaries["sidecar"], candidate_payload=payload)
    module("check-release-evidence")._validate_one(public, root=ROOT, version="16.0.1", head=expected["source_commit"], kind=kind.upper())
    names = ["tauri_build", kind, "install", "launch", "sidecar_health", "upgrade", "uninstall"]
    gate = module("rc_gate")
    checks = {gate.requirement_id("desktop", name): True for name in names}
    output = ROOT / PREFIX / (kind + "-checks.json")
    write(output, {"schema_version": 1, "report_type": "rc_check_evidence", "target_version": "16.0.1", "kind": "desktop",
        "actual_run": True, "status": "PASS", "source": actual, "source_after": source(expected), "build_id": manifest["build_id"],
        "binary_sha256": public["binary_sha256"], "sidecar_payload_sha256": payload["payload_content_sha256"], "artifact_variant": kind,
        "checks": checks, "installer_evidence": reference(ROOT / public_path), "build_observation": reference(ROOT / BUILD),
        "manual_acceptance": "NOT_RECORDED", "formal_rc_accepted": False})
    producer = select_job(jobs(expected), expected, kind, completed=False)
    index = make_index(kind, [public_path, output.relative_to(ROOT).as_posix()], run, producer["id"])
    publish(Path(os.environ["RUNNER_TEMP"]) / (kind + "-public"), index)


def desktop_matrix(expected):
    actual, manifest, binaries, payload, _ = validate_payload(expected)
    gate = module("rc_gate")
    variants = {kind: gate.installer_binary_hashes(ROOT, binaries, kind.upper()) for kind in ("nsis", "msi")}
    for kind in ("nsis", "msi"):
        public = read(ROOT / f"build/v1601-evidence/{kind}-installer-smoke.json")
        module("rc_installed_public").validate_public_report(public, candidate_sidecar=binaries["sidecar"], candidate_payload=payload)
        module("check-release-evidence")._validate_one(public, root=ROOT, version="16.0.1", head=expected["source_commit"], kind=kind.upper())
    records = []
    for name in gate.required_gates()["desktop"]:
        kind = "msi" if name == "msi" else "nsis"
        ref = reference(ROOT / PREFIX / (kind + "-checks.json"))
        records.append({"id": name, "requirement_id": gate.requirement_id("desktop", name), "status": "PASS",
                        "evidence": [{"kind": "desktop", "actual_run": True, "outcome": "PASS", "report": ref}]})
    matrix = {"schema_version": 5, "target_version": "16.0.1", "protocol_version": "siyi-rc-v1", "release_gates": {"desktop": records}}
    errors = gate.validate_matrix(ROOT, matrix, actual, manifest["build_id"], binary_hashes={key: value["sha256"] for key, value in binaries.items()},
                                  payload_hash=payload["payload_content_sha256"], installer_hashes=variants)
    # The original validator still rejects all absent non-desktop requirements.
    # Only those exact known missing-group/missing-gate errors are expected.
    pending = set()
    for category in ("automated", "manual"):
        pending.add(f"TEST_MATRIX release_gates.{category} must be a list")
        for name in gate.required_gates()[category]:
            pending.add(f"TEST_MATRIX is missing required gate {category}.{name}")
            pending.add(f"{gate.requirement_id(category, name)}: missing/duplicate mandatory gate")
    require(all(error in pending for error in errors), "desktop matrix rejected by original specialized contract")
    require(bool(errors), "desktop-only evidence must not pretend complete RC acceptance")
    return matrix, errors, manifest


def summarize(expected, private, artifact_id):
    note("candidate_transport")
    fetch(expected, "payload", private, artifact_id=artifact_id)
    for kind in ("nsis", "msi"):
        fetch(expected, kind, private)
    note("matrix_validation")
    matrix, pending, manifest = desktop_matrix(expected)
    current(expected)
    note("public_projection")
    output = Path(os.environ["RUNNER_TEMP"]) / "desktop-summary-public"
    output.mkdir()
    write(output / "desktop-matrix.json", matrix)
    result = {"schema_version": 1, "report_type": "candidate_desktop_subset_not_rc_acceptance", "status": "DESKTOP_8_OBSERVED_NOT_RC_READY",
        "source_commit": expected["source_commit"], "build_id": manifest["build_id"], "desktop_passed": 8,
        "manual_attested": False, "formal_rc_accepted": False, "published": False, "total_rc_status": "BLOCKED",
        "pending_original_matrix_errors": pending, "other_gates": "model/performance/manual/evaluations/importer/full RC remain mandatory"}
    write(output / "desktop-result.json", result)
    for path in output.iterdir():
        module("export-accepted-rc").public_content(path.name, path.read_bytes())


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise CandidateError("invalid candidate CLI")  # Never echo untrusted argv.
    parser = Parser(description=__doc__)
    parser.add_argument("mode", choices=("build", "nsis", "msi", "summarize"))
    parser.add_argument("--artifact-id", type=int)
    DIAGNOSTICS.clear()
    note("entry")
    private = args = None
    try:
        args = parser.parse_args(argv)
        expected = environment_identity(os.environ)
        private = private_directory(args.mode)
        note("source")
        source(expected)
        if args.mode == "build":
            build(expected, private)
        elif args.mode == "summarize":
            require(args.artifact_id is not None, "explicit build artifact ID required")
            summarize(expected, private, args.artifact_id)
        else:
            require(args.artifact_id is not None, "explicit build artifact ID required")
            observe(args.mode, expected, private, args.artifact_id)
        print(json.dumps({"status": "CANDIDATE_STAGE_COMPLETE_NOT_FORMAL_RC", "stage": args.mode, "formal_rc_accepted": False, "published": False}))
        return 0
    except Exception as exc:
        mode = args.mode if args is not None else "unknown"
        if private is not None:
            try:
                write(private / "failure.json", {"status": "BLOCKED", "exception_type": type(exc).__name__, "private_detail": str(exc), "formal_rc_accepted": False})
            except Exception:
                pass
        projected = False
        try:
            public_failure(mode, exc)
            projected = True
        except Exception:
            pass  # Never print raw diagnostics or turn a failure into success.
        print(json.dumps({"status": "BLOCKED_PRIVATE_EVIDENCE_RETAINED", "mode": mode,
                          "failure_projection_available": projected, "formal_rc_accepted": False, "published": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
