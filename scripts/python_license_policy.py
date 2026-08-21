from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from packaging.markers import Marker, default_environment


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "packaging" / "python-license-policy.json"
DEFAULT_LOCK = ROOT / "siyi" / "uv.lock"
WINDOWS_TARGET_ENVIRONMENT = {
    **default_environment(),
    "implementation_name": "cpython",
    "implementation_version": "3.12.0",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_release": "",
    "platform_system": "Windows",
    "platform_version": "",
    "python_full_version": "3.12.0",
    "python_version": "3.12",
    "sys_platform": "win32",
}


def canonicalize_name(name: str) -> str:
    """Return the PEP 503 form used to compare policy and lock names."""

    return re.sub(r"[-_.]+", "-", name).casefold()


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _locked_packages(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("rb") as handle:
        lock = tomllib.load(handle)
    return {
        canonicalize_name(str(item["name"])): item
        for item in lock.get("package", [])
        if item.get("name") and item.get("version")
    }


def _target_marker_applies(item: dict[str, Any]) -> bool:
    marker = item.get("marker")
    return not marker or Marker(str(marker)).evaluate(WINDOWS_TARGET_ENVIRONMENT)


def _extras(item: dict[str, Any]) -> set[str]:
    value = item.get("extra", item.get("extras", ()))
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(extra) for extra in value}
    return set()


def _dependency_request(item: dict[str, Any]) -> tuple[str, set[str]] | None:
    name = item.get("name")
    if not name or not _target_marker_applies(item):
        return None
    return canonicalize_name(str(name)), _extras(item)


def dependency_closure(
    packages: dict[str, dict[str, Any]], roots: list[str | dict[str, Any]]
) -> set[str]:
    """Resolve the actual Windows frozen dependency closure from ``uv.lock``.

    ``uv.lock`` stores requested extras separately from ordinary dependencies.
    In particular, the desktop sidecar depends on ``uvicorn[standard]``; a
    name-only walk silently omits its Windows payloads such as ``httptools``
    and ``websockets``.  This resolver preserves requested extras and applies
    the release target's Windows/Python 3.12 markers.  It may still include a
    module PyInstaller prunes, which is acceptable for notices, but it must
    never omit a locked package selected for the frozen target.
    """

    pending: list[tuple[str, set[str]]] = []
    for root in roots:
        if isinstance(root, str):
            pending.append((canonicalize_name(root), set()))
        elif request := _dependency_request(root):
            pending.append(request)
    result: set[str] = set()
    requested_extras: dict[str, set[str]] = {}
    while pending:
        name, extras = pending.pop()
        if name not in packages:
            continue
        previous = requested_extras.setdefault(name, set())
        new_extras = extras - previous
        if name in result and not new_extras:
            continue
        previous.update(extras)
        result.add(name)
        package = packages[name]
        dependencies = list(package.get("dependencies", []))
        for extra in sorted(previous):
            dependencies.extend(package.get("optional-dependencies", {}).get(extra, []))
        for dependency in dependencies:
            if isinstance(dependency, dict) and (request := _dependency_request(dependency)):
                pending.append(request)
    return result


def application_dependency_sets(
    lock_path: Path = DEFAULT_LOCK,
    application_name: str = "aureliuswu-agent-backend",
) -> tuple[set[str], set[str]]:
    """Return frozen runtime and build-only closures from the locked app."""

    packages = _locked_packages(lock_path)
    application = packages.get(canonicalize_name(application_name))
    if application is None:
        raise RuntimeError(f"application is absent from uv.lock: {application_name}")
    runtime = dependency_closure(packages, list(application.get("dependencies", [])))
    build = dependency_closure(
        packages,
        list(application.get("optional-dependencies", {}).get("dev", [])),
    ) - runtime
    return runtime, build


def audit_policy(
    policy_path: Path = DEFAULT_POLICY,
    lock_path: Path = DEFAULT_LOCK,
) -> dict[str, Any]:
    policy = load_policy(policy_path)
    locked = _locked_packages(lock_path)
    covered = {
        canonicalize_name(name): str(version)
        for name, version in policy.get("packages", {}).items()
    }
    excluded = {
        canonicalize_name(name): item
        for name, item in policy.get("excluded_packages", {}).items()
    }
    roots = [canonicalize_name(str(name)) for name in policy.get("dependency_roots", [])]
    first_party = {
        canonicalize_name(str(name)) for name in policy.get("first_party_packages", [])
    }
    closure = dependency_closure(locked, roots)
    third_party_closure = closure - first_party
    errors: list[str] = []

    if policy.get("schema_version") != 3:
        errors.append("python license policy schema_version must be 3")
    if not roots:
        errors.append("python license policy requires at least one dependency root")
    for root in roots:
        if root not in locked:
            errors.append(f"dependency root is absent from uv.lock: {root}")
    for name in sorted(first_party):
        if name not in locked:
            errors.append(f"first-party package is absent from uv.lock: {name}")
    overlap = sorted(set(covered) & set(excluded))
    if overlap:
        errors.append(f"packages cannot be both covered and excluded: {', '.join(overlap)}")
    first_party_in_policy = sorted(first_party & (set(covered) | set(excluded)))
    if first_party_in_policy:
        errors.append(
            f"first-party packages must not be notice-covered or excluded: {', '.join(first_party_in_policy)}"
        )
    missing = sorted(third_party_closure - set(covered) - set(excluded))
    if missing:
        errors.append(f"frozen Windows dependency closure is missing notices: {', '.join(missing)}")

    for name, expected in covered.items():
        item = locked.get(name)
        if item is None:
            errors.append(f"notice package is absent from uv.lock: {name}")
        elif str(item["version"]) != expected:
            errors.append(
                f"notice version mismatch for {name}: policy={expected}, lock={item['version']}"
            )
    for name, metadata in excluded.items():
        item = locked.get(name)
        expected = str(metadata.get("version", ""))
        reason = str(metadata.get("reason", "")).strip()
        if item is None:
            errors.append(f"excluded package is absent from uv.lock: {name}")
        elif str(item["version"]) != expected:
            errors.append(
                f"excluded version mismatch for {name}: policy={expected}, lock={item['version']}"
            )
        if name not in third_party_closure:
            errors.append(f"excluded package is outside the audited dependency closure: {name}")
        if not reason:
            errors.append(f"excluded package needs a packaging reason: {name}")

    return {
        "passed": not errors,
        "errors": errors,
        "roots": sorted(roots),
        "locked_closure": sorted(third_party_closure),
        "notice_covered": sorted(third_party_closure & set(covered)),
        "excluded": sorted(third_party_closure & set(excluded)),
    }


def require_valid_policy(
    policy_path: Path = DEFAULT_POLICY,
    lock_path: Path = DEFAULT_LOCK,
) -> dict[str, Any]:
    audit = audit_policy(policy_path, lock_path)
    if not audit["passed"]:
        raise RuntimeError("; ".join(audit["errors"]))
    return audit
