"""Non-release desktop CI transport and observation contracts.

Reuse the accepted-RC byte/redirect/privacy primitives, never its acceptance
index. A successful result here satisfies only the desktop subset; it cannot
create a formal accepted RC or attest manual/model/performance acceptance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REPOSITORY = "AureliusWu/Agent"
WORKFLOW = ".github/workflows/rc-desktop-candidate.yml"
PREFIX = "desktop/src-tauri/target/release/ci-candidate"
PORTABLE = PREFIX + "/portable"
MANIFEST = "build/generated/build-info.json"
INVENTORY = PREFIX + "/payload.json"
BUILD = PREFIX + "/build-observation.json"
INDEX = "candidate-transfer-index.json"
JOBS = {"payload": "Build candidate", "nsis": "Installed lifecycle (nsis)", "msi": "Installed lifecycle (msi)"}
PRIOR = {
    "nsis": {"id": 490089409, "name": "Siyi_8.0.1_x64-setup.exe", "size": 24693052,
             "sha256": "76f771ab727fae74fe3a2c4a1dae3e7650f4e5716bae5da7fda76994d924669a"},
    "msi": {"id": 490089408, "name": "Siyi_8.0.1_x64_zh-CN.msi", "size": 25911296,
            "sha256": "be220314642c9e4f73735c671ed4aa2ae8871bf081311bade96380a370fd349e"},
}
MAX_METADATA = 4 * 1024**2
FAILURE_STAGES = frozenset({"entry", "source", "metadata", "candidate_shell", "candidate_build", "candidate_bundle", "candidate_validation",
    "candidate_transport", "native_preflight", "host_preflight", "prior_download", "installed_lifecycle", "matrix_validation", "public_projection"})
LIFECYCLE_STAGES = ("install_previous", "launch_previous", "seed_fixture", "upgrade_current", "launch_upgraded",
    "uninstall_current", "verify_retention", "reinstall_current", "launch_reinstalled", "final_uninstall", "verify_final")
_GUARD_MESSAGES = {
    "CLI_INPUT_REJECTED": ("invalid candidate CLI",),
    "DISPATCH_REJECTED": ("exact current dispatch SHA required", "only explicit own main dispatch on a fresh hosted runner is supported", "typed run/attempt required"),
    "SOURCE_MISMATCH": ("exact current clean v16.0.1 source required", "candidate manifest does not match current clean source", "source changed during candidate build", "source changed during real installation"),
    "MAIN_MOVED": ("main moved from selected candidate",),
    "PROVENANCE_MISMATCH": ("independent attempt metadata differs", "producer job missing or ambiguous", "job provenance differs", "run/attempt/SHA drift", "foreign repository or fork", "different workflow or trigger", "producer job did not complete successfully", "candidate run/attempt/job/artifact drift during download", "run provenance drifted while building"),
    "ARTIFACT_SELECTION_REJECTED": ("artifact identity/digest/size missing", "artifact belongs to other source or repository", "artifact outside exact producer job/attempt window", "candidate artifact missing or ambiguous", "explicit artifact ID required", "explicit build artifact ID required"),
    "DOWNLOAD_BYTES_REJECTED": ("downloaded asset differs from the explicitly authorized SHA-256", "download exceeds its exact size/time bound"),
    "REDIRECT_REJECTED": ("asset redirected outside the fixed GitHub transport", "GitHub metadata redirect refused", "metadata origin changed"),
    "PRIOR_RELEASE_REJECTED": ("fixed official baseline not available", "fixed official baseline asset changed", "official baseline metadata drifted during download"),
    "DISK_INSUFFICIENT": ("insufficient disk for bounded candidate operation",),
    "NATIVE_WINDOW_STATION_UNAVAILABLE": ("visible native window station unavailable",),
    "NATIVE_SESSION_UNAVAILABLE": ("interactive native session unavailable",),
    "NATIVE_INPUT_DESKTOP_UNAVAILABLE": ("input desktop unavailable", "native desktop handle did not close"),
    "HOST_NOT_READY": ("host WebView2/admin/product namespace not ready", "already-installed WebView2 required; no bootstrap download", "preexisting installer or application activity is not owned"),
    "SUBPROCESS_FAILED": ("candidate subprocess failed; private log retained", "actual subprocess output missing"),
    "SHELL_UNSUPPORTED": ("trusted PowerShell 7 required",),
    "CANDIDATE_BYTES_MISMATCH": ("bundle inputs differ from candidate", "source, original bundle marker, sidecar or complete payload changed", "public snapshot differs from original complete candidate payload", "package differs from actual build receipt", "candidate entry hash mismatch", "candidate extraction changed bytes"),
    "BUNDLE_CONFIG_REJECTED": ("candidate configuration differs from reviewed generated build-only overlay", "unreviewed bundle inputs/hook/override"),
    "INSTALLER_SIZE_REJECTED": ("unique package inside original lifecycle byte bound required", "installer size outside reviewed bounds"),
    "LIFECYCLE_FAILED": ("real installed lifecycle did not pass",),
    "DESKTOP_RENDER_TIMEOUT": ("actual installed desktop readiness timed out",),
    "DESKTOP_EXIT_FAILED": ("actual retained desktop and sidecar did not both exit normally", "installed application required forced or unknown cleanup"),
    "MATRIX_REJECTED": ("desktop matrix rejected by original specialized contract", "desktop-only evidence must not pretend complete RC acceptance"),
}
GUARD_CODE_BY_MESSAGE = {message: code for code, messages in _GUARD_MESSAGES.items() for message in messages}
GUARD_CODES = frozenset(_GUARD_MESSAGES) | {"GUARD_REJECTED", "EXISTING_VALIDATOR_REJECTED", "HTTP_REQUEST_FAILED", "NETWORK_REQUEST_FAILED", "SUBPROCESS_TIMEOUT", "UNEXPECTED_ERROR"}


class CandidateError(ValueError):
    pass


def failure_projection(mode, stage, error, observations=None):
    """Typed public diagnostics only. Never stringify or serialize an exception."""
    values = observations if isinstance(observations, dict) else {}
    name = type(error).__name__
    classes = {"CandidateError", "TransportError", "SafetyError", "GateError", "PublicReportError", "EvidenceError",
               "HTTPError", "URLError", "TimeoutExpired", "TimeoutError", "OSError", "FileNotFoundError", "PermissionError",
               "ValueError", "TypeError", "KeyError", "ImportError", "ModuleNotFoundError", "BadZipFile"}
    exception_class = name if name in classes else "UnknownError"
    args = getattr(error, "args", ())
    # Equality with reviewed source literals is the only message inspection.
    message = args[0] if isinstance(args, tuple) and len(args) == 1 and type(args[0]) is str else None
    code = GUARD_CODE_BY_MESSAGE.get(message)
    if code is None:
        code = {"CandidateError": "GUARD_REJECTED", "TransportError": "EXISTING_VALIDATOR_REJECTED",
                "SafetyError": "EXISTING_VALIDATOR_REJECTED", "GateError": "EXISTING_VALIDATOR_REJECTED",
                "PublicReportError": "EXISTING_VALIDATOR_REJECTED", "EvidenceError": "EXISTING_VALIDATOR_REJECTED",
                "HTTPError": "HTTP_REQUEST_FAILED", "URLError": "NETWORK_REQUEST_FAILED",
                "TimeoutExpired": "SUBPROCESS_TIMEOUT"}.get(exception_class, "UNEXPECTED_ERROR")
    def boolean(value):
        return value if type(value) is bool else None
    def number(value, low=0, high=20000):
        return value if type(value) is int and low <= value <= high else None
    def sha(value):
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None
    result = {"schema_version": 1, "report_type": "candidate_typed_failure_not_acceptance", "status": "BLOCKED",
              "mode": mode if type(mode) is str and mode in {"build", "nsis", "msi", "summarize"} else "unknown",
              "stage": stage if type(stage) is str and stage in FAILURE_STAGES else "entry", "exception_class": exception_class,
              "guard_code": code, "formal_rc_accepted": False,
              "http_status": number(getattr(error, "code", None), 100, 599) if exception_class == "HTTPError" else None}
    process = values.get("subprocess")
    if isinstance(process, dict):
        label = process.get("label")
        result["subprocess"] = {"label": label if type(label) is str and label in {"candidate-build", "candidate-bundle", "candidate-shell-probe"} else "unknown",
            "actual_run": boolean(process.get("actual_run")), "exit_code": number(process.get("exit_code"), -(2**31), 2**32 - 1),
            "timed_out": boolean(process.get("timed_out")), "log_sha256": sha(process.get("log_sha256"))}
    native = values.get("native")
    if isinstance(native, dict):
        result["native"] = {key: boolean(native.get(key)) for key in ("visible_window_station", "non_service_session", "input_desktop_opened", "input_desktop_closed")}
    disk = values.get("disk")
    if isinstance(disk, dict):
        result["disk"] = {key: number(disk.get(key), 0, 2**63 - 1) for key in ("free_bytes", "minimum_bytes")}
    host = values.get("host")
    if isinstance(host, dict):
        result["host"] = {"is_admin": boolean(host.get("is_admin")), "installer_busy": boolean(host.get("installer_busy"))}
        for key in ("webviews", "installations", "related_products", "install_paths", "shortcuts", "running"):
            result["host"][key + "_count"] = number(len(host[key])) if isinstance(host.get(key), list) else None
    life = values.get("lifecycle")
    if isinstance(life, dict):
        last = life.get("last_completed_stage")
        state = life.get("state")
        result["lifecycle"] = {"last_completed_stage": last if type(last) is str and last in LIFECYCLE_STAGES else None,
            "actual_run": boolean(life.get("actual_run")), "fixture_retained": boolean(life.get("fixture_retained")),
            "uncertain_operator_recovery": state == "UNCERTAIN_OPERATOR_RECOVERY" if type(state) is str else None,
            "public_projection_failed": state == "PUBLIC_REPORT_FAILED_PRIVATE_RETAINED" if type(state) is str else None}
        failure = life.get("failure_type")
        result["lifecycle"]["failure_class"] = failure if type(failure) is str and failure in classes else "UnknownError"
    dispatch = values.get("owned_dispatch")
    if isinstance(dispatch, dict):
        cleanup = dispatch.get("process_cleanup") if isinstance(dispatch.get("process_cleanup"), dict) else {}
        result["owned_dispatch"] = {"actual_run": boolean(dispatch.get("actual_run")),
            "installer_or_application_effect": boolean(dispatch.get("installer_or_application_effect")),
            "exit_code": number(dispatch.get("exit_code"), -(2**31), 2**32 - 1),
            "forced_termination": boolean(cleanup.get("forced_termination")),
            "active_after_cleanup": number(cleanup.get("active_after_cleanup"))}
    return result


def require(value, message):
    if not value:
        raise CandidateError(message)


@lru_cache(maxsize=None)
def module(name):
    spec = importlib.util.spec_from_file_location("candidate_" + name.replace("-", "_"), SCRIPTS / (name + ".py"))
    require(spec is not None and spec.loader is not None, "required existing validator unavailable")
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


def stamp(value):
    require(isinstance(value, str), "typed timestamp required")
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "timezone required")
    return result.astimezone(timezone.utc)


def positive(value):
    return type(value) is int and value > 0


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            result.update(block)
    return result.hexdigest()


def read(path):
    return module("export-accepted-rc").read_object(path)


def write(path, value):
    transport = module("export-accepted-rc")
    transport.no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def reference(path):
    module("export-accepted-rc").ordinary(path)
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CandidateError("GitHub metadata redirect refused")


def api(endpoint):
    require(endpoint.startswith("/repos/" + REPOSITORY + "/") and not any(c in endpoint for c in "\r\n#"), "fixed own repository API required")
    token = os.environ.get("GH_TOKEN")
    require(bool(token), "read-only Actions token required for metadata")
    url = "https://api.github.com" + endpoint
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Siyi-Desktop-Candidate", "Accept-Encoding": "identity"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        require(response.geturl() == url, "metadata origin changed")
        return module("export-accepted-rc").json_object(response.read(MAX_METADATA + 1), limit=MAX_METADATA)


def pages(endpoint, field):
    rows, count = [], None
    for page in range(1, 101):
        value = api(endpoint + ("&" if "?" in endpoint else "?") + f"per_page=100&page={page}")
        require(type(value.get("total_count")) is int and isinstance(value.get(field), list), "complete typed pagination required")
        if count is None:
            count = value["total_count"]
        require(count == value["total_count"] and 0 <= count <= 10000, "pagination changed or exceeded bound")
        rows.extend(value[field])
        require(len(rows) <= count, "pagination exceeded declared count")
        if len(rows) == count:
            require(all(isinstance(row, dict) and positive(row.get("id")) for row in rows)
                    and len({row["id"] for row in rows}) == len(rows), "duplicate or invalid paginated identity")
            return rows
        require(bool(value[field]), "pagination truncated")
    raise CandidateError("pagination bound reached")


def environment_identity(env):
    commit = env.get("INPUT_SOURCE_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None and commit == env.get("GITHUB_SHA"), "exact current dispatch SHA required")
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and env.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main"
            and env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "only explicit own main dispatch on a fresh hosted runner is supported")
    run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None and re.fullmatch(r"[1-9][0-9]*", attempt) is not None, "typed run/attempt required")
    return {"source_commit": commit, "run_id": int(run_id), "run_attempt": int(attempt)}


def validate_run(run, attempt, expected):
    def identity(value):
        require(isinstance(value, dict), "run metadata missing")
        own, head = value.get("repository", {}), value.get("head_repository", {})
        require(own.get("full_name") == REPOSITORY and head.get("full_name") == REPOSITORY
                and positive(own.get("id")) and positive(head.get("id")) and own.get("id") == head.get("id"), "foreign repository or fork")
        require(positive(value.get("id")) and positive(value.get("run_attempt"))
                and value.get("id") == expected["run_id"] and value.get("run_attempt") == expected["run_attempt"]
                and value.get("head_sha") == expected["source_commit"] and positive(value.get("workflow_id")), "run/attempt/SHA drift")
        require(value.get("event") == "workflow_dispatch" and value.get("head_branch") == "main"
                and value.get("path") in {WORKFLOW, WORKFLOW + "@main", WORKFLOW + "@refs/heads/main"}, "different workflow or trigger")
        require(value.get("status") in {"in_progress", "completed"}
                and (value.get("status") != "completed" or value.get("conclusion") == "success"), "run is not active or successful")
        stamp(value.get("run_started_at"))
        return {**expected, "repository_id": own["id"], "workflow_id": value["workflow_id"],
                "run_started_at": value["run_started_at"], "workflow_path": WORKFLOW}
    result = identity(run)
    require(identity(attempt) == result, "independent attempt metadata differs")
    return result


def current(expected):
    branch = api("/repos/" + REPOSITORY + "/git/ref/heads/main")
    require(branch.get("ref") == "refs/heads/main" and branch.get("object", {}).get("sha") == expected["source_commit"], "main moved from selected candidate")
    base = "/repos/" + REPOSITORY + "/actions/runs/" + str(expected["run_id"])
    return validate_run(api(base), api(base + "/attempts/" + str(expected["run_attempt"])), expected)


def select_job(rows, expected, role, *, completed):
    matches = [row for row in rows if row.get("name") == JOBS[role]]
    require(len(matches) == 1, "producer job missing or ambiguous")
    row = matches[0]
    require(positive(row.get("id")) and positive(row.get("run_id")) and positive(row.get("run_attempt"))
            and row.get("run_id") == expected["run_id"] and row.get("run_attempt") == expected["run_attempt"] and row.get("head_sha") == expected["source_commit"], "job provenance differs")
    stamp(row.get("started_at"))
    if completed:
        require(row.get("status") == "completed" and row.get("conclusion") == "success"
                and stamp(row.get("started_at")) <= stamp(row.get("completed_at")), "producer job did not complete successfully")
    else:
        require(row.get("status") == "in_progress", "current producer job is not running")
    return row


def jobs(expected):
    return pages(f"/repos/{REPOSITORY}/actions/runs/{expected['run_id']}/attempts/{expected['run_attempt']}/jobs", "jobs")


def artifact_name(expected, role):
    return f"Siyi-Desktop-Candidate-{expected['run_id']}-{expected['run_attempt']}-{expected['source_commit']}-{role}"


def selection(artifact, run, job, role):
    origin = artifact.get("workflow_run", {})
    require(positive(artifact.get("id")) and artifact.get("name") == artifact_name(run, role)
            and artifact.get("expired") is False and positive(artifact.get("size_in_bytes"))
            and artifact["size_in_bytes"] <= module("export-accepted-rc").MAX_ARCHIVE_BYTES
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest"))) is not None, "artifact identity/digest/size missing")
    require(positive(origin.get("id")) and positive(origin.get("repository_id")) and positive(origin.get("head_repository_id"))
            and origin.get("id") == run["run_id"] and origin.get("repository_id") == run["repository_id"]
            and origin.get("head_repository_id") == run["repository_id"] and origin.get("head_sha") == run["source_commit"]
            and origin.get("head_branch") == "main", "artifact belongs to other source or repository")
    require(stamp(job["started_at"]) <= stamp(artifact.get("created_at")) <= stamp(job["completed_at"])
            and stamp(run["run_started_at"]) <= stamp(job["started_at"]), "artifact outside exact producer job/attempt window")
    return {"schema_version": 1, "report_type": "candidate_transport_selection_not_acceptance", "role": role,
            "run": run, "producer_job_id": job["id"], "producer_started_at": job["started_at"], "producer_completed_at": job["completed_at"],
            "artifact": {key: artifact[key] for key in ("id", "name", "digest", "size_in_bytes", "created_at")}}


def allowed(role, name):
    transport = module("export-accepted-rc")
    transport.public_path(name)
    if role == "payload":
        require(name in {MANIFEST, INVENTORY, BUILD, PREFIX + "/candidate-build.json"}
                or name in {PORTABLE + "/司忆.exe", PORTABLE + "/agent-backend.exe"}
                or name.startswith(PORTABLE + "/_internal/")
                or re.fullmatch(r"desktop/src-tauri/target/release/bundle/(?:nsis/[^/]+-setup\.exe|msi/[^/]+\.msi)", name), "unrelated candidate upload rejected")
    else:
        require(role in {"nsis", "msi"} and name in {f"build/v1601-evidence/{role}-installer-smoke.json", PREFIX + f"/{role}-checks.json"}, "unrelated lifecycle upload rejected")
    return name


def make_index(role, files, run, job_id):
    require(role in JOBS and positive(job_id) and files and len(files) <= 20000, "bounded candidate closure required")
    seen, entries, total = set(), [], 0
    transport = module("export-accepted-rc")
    started = time.monotonic()
    for name in sorted(files):
        allowed(role, name)
        require(name.casefold() not in seen, "case-insensitive duplicate candidate file")
        seen.add(name.casefold())
        size, sha = transport.checked_hash(ROOT / name, name, started=started)
        entries.append({"path": name, "bytes": size, "sha256": sha})
        total += size
        require(total <= transport.MAX_BYTES, "candidate upload exceeded bound")
    return {"schema_version": 1, "report_type": "desktop_candidate_transport_not_acceptance", "target_version": "16.0.1",
            "accepted_candidate": False, "role": role, "run": run, "producer_job_id": job_id, "files": entries}


def publish(directory, index):
    transport = module("export-accepted-rc")
    transport.no_links(directory)
    require(not directory.exists(), "public staging must be fresh")
    directory.mkdir(parents=True)
    for entry in index["files"]:
        target = directory / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / entry["path"], target)
        require(target.stat().st_size == entry["bytes"] and digest(target) == entry["sha256"], "candidate changed while staging")
    write(directory / INDEX, index)


def validate_zip(archive_path, receipt):
    """Validate all bytes before extraction; a formal accepted RC is forbidden."""
    transport = module("export-accepted-rc")
    require(transport.ordinary(archive_path).st_size <= transport.MAX_ARCHIVE_BYTES, "candidate ZIP too large")
    started, seen, infos, total = time.monotonic(), set(), {}, 0
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        require(0 < len(members) <= transport.MAX_FILES + 1, "candidate member count outside bound")
        for item in members:
            name = transport.canonical_path(item.filename)
            mode = item.external_attr >> 16
            require(not item.is_dir() and not item.flag_bits & 1 and mode & 0xF000 in {0, stat.S_IFREG}
                    and item.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} and name.casefold() not in seen,
                    "candidate ZIP has duplicate, special, encrypted or unsafe members")
            seen.add(name.casefold())
            total += item.file_size
            require(0 <= item.file_size <= transport.MAX_BYTES and total <= transport.MAX_BYTES + transport.MAX_INDEX_BYTES, "candidate expanded bytes exceed bound")
            if name != INDEX:
                allowed(receipt["role"], name)
            infos[name] = item
        require(INDEX in infos and infos[INDEX].file_size <= transport.MAX_INDEX_BYTES, "candidate index missing")
        index = transport.json_object(archive.read(infos[INDEX]), limit=transport.MAX_INDEX_BYTES)
        require(set(index) == {"schema_version", "report_type", "target_version", "accepted_candidate", "role", "run", "producer_job_id", "files"}
                and type(index["schema_version"]) is int and index["schema_version"] == 1
                and index["report_type"] == "desktop_candidate_transport_not_acceptance" and index["target_version"] == "16.0.1"
                and index["accepted_candidate"] is False and index["role"] == receipt["role"]
                and json.dumps(index["run"], sort_keys=True) == json.dumps(receipt["run"], sort_keys=True)
                and positive(index["producer_job_id"]) and index["producer_job_id"] == receipt["producer_job_id"]
                and isinstance(index["files"], list) and 0 < len(index["files"]) <= transport.MAX_FILES, "candidate index provenance differs or pretends formal acceptance")
        indexed = set()
        for entry in index["files"]:
            require(isinstance(entry, dict) and set(entry) == {"path", "bytes", "sha256"}, "invalid candidate index entry")
            name = allowed(index["role"], entry["path"])
            require(name.casefold() not in indexed and name in infos and type(entry["bytes"]) is int
                    and entry["bytes"] == infos[name].file_size and re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])), "indexed candidate bytes differ")
            indexed.add(name.casefold())
            value, carry = hashlib.sha256(), b""
            notice = module("import-accepted-rc").public_notice_identity(name)
            if notice is not None:
                require(infos[name].file_size == notice[0], "known public notice size differs")
                block = archive.read(infos[name])
                transport._public_notice_content(name, block)
                value.update(block)
            else:
                with archive.open(infos[name]) as stream:
                    while block := stream.read(1024**2):
                        require(time.monotonic() - started <= transport.MAX_SECONDS, "candidate ZIP validation timed out")
                        transport.public_content(name, carry + block, first=not carry)
                        carry = block[-4096:]
                        value.update(block)
            require(value.hexdigest() == entry["sha256"], "candidate entry hash mismatch")
            module("import-accepted-rc").validate_public_resource(name, entry["bytes"], value.hexdigest())
            if Path(name).name == "base_library.zip":
                with archive.open(infos[name]) as stream, zipfile.ZipFile(stream) as library:
                    transport.validate_library(library, started)
        require(set(infos) == {entry["path"] for entry in index["files"]} | {INDEX}, "unindexed candidate files")
        return index


def receive(archive_path, receipt):
    index = validate_zip(archive_path, receipt)
    transport = module("export-accepted-rc")
    for entry in index["files"]:
        target = ROOT / entry["path"]
        transport.no_links(target)
        require(not target.exists(), "candidate extraction cannot overwrite generated or source files")
    with zipfile.ZipFile(archive_path) as archive:
        for entry in index["files"]:
            target = ROOT / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            transport.no_links(target)
            with archive.open(entry["path"]) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, 1024**2)
            require(digest(target) == entry["sha256"], "candidate extraction changed bytes")
    return index


def fetch(expected, role, private, *, artifact_id=None):
    run = current(expected)
    job = select_job(jobs(expected), expected, role, completed=True)
    if artifact_id is None:
        matches = [row for row in pages(f"/repos/{REPOSITORY}/actions/runs/{expected['run_id']}/artifacts", "artifacts") if row.get("name") == artifact_name(expected, role)]
        require(len(matches) == 1, "candidate artifact missing or ambiguous")
        artifact_id = matches[0]["id"]
    require(positive(artifact_id), "explicit artifact ID required")
    endpoint = f"/repos/{REPOSITORY}/actions/artifacts/{artifact_id}"
    before = selection(api(endpoint), run, job, role)
    archive = private / (role + ".zip")
    info = before["artifact"]
    module("export-accepted-rc").download_action_artifact(REPOSITORY, artifact_id, info["digest"][7:], info["size_in_bytes"], archive)
    after = selection(api(endpoint), current(expected), select_job(jobs(expected), expected, role, completed=True), role)
    require(before == after, "candidate run/attempt/job/artifact drift during download")
    index = receive(archive, before)
    write(private / (role + "-selection.json"), before)
    return index


def download_prior(kind):
    spec = PRIOR[kind]
    release = api(f"/repos/{REPOSITORY}/releases/tags/v8.0.1")
    require(release.get("tag_name") == "v8.0.1" and release.get("draft") is False and release.get("prerelease") is False, "fixed official baseline not available")
    endpoint = f"/repos/{REPOSITORY}/releases/assets/{spec['id']}"
    meta = api(endpoint)
    require(meta.get("id") == spec["id"] and meta.get("name") == spec["name"] and meta.get("size") == spec["size"]
            and meta.get("digest") == "sha256:" + spec["sha256"] and meta.get("state") == "uploaded"
            and meta.get("url") == "https://api.github.com" + endpoint
            and sum(row.get("id") == spec["id"] and row.get("digest") == meta["digest"] for row in release.get("assets", [])) == 1,
            "fixed official baseline asset changed")
    output = ROOT / "build/upgrade-baseline" / spec["name"]
    headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/octet-stream",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Siyi-Desktop-Candidate", "Accept-Encoding": "identity"}
    transport = module("export-accepted-rc")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), transport.AssetRedirects())
    transport.download_exact(opener, urllib.request.Request("https://api.github.com" + endpoint, headers=headers), spec["size"], spec["sha256"], output)
    identity_fields = ("id", "name", "size", "digest", "state", "url", "browser_download_url", "created_at", "updated_at")
    after = api(endpoint)
    require({key: after.get(key) for key in identity_fields} == {key: meta.get(key) for key in identity_fields}, "official baseline metadata drifted during download")
    receipt = ROOT / "build/upgrade-baseline" / (kind + "-release-receipt.json")
    write(receipt, {"repository": REPOSITORY, "tag": "v8.0.1", "draft": False, "prerelease": False,
                    "assets": [{"kind": kind, **reference(output), "github_asset_digest": meta["digest"]}]})
    return output, receipt
