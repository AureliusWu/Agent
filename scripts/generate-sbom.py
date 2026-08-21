from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from python_license_policy import (
    application_dependency_sets,
    canonicalize_name,
    load_policy,
    require_valid_policy,
)


ROOT = Path(__file__).resolve().parents[1]


def _purl(kind: str, name: str, version: str) -> str:
    safe_name = "/" if kind == "npm" else ""
    return f"pkg:{kind}/{urllib.parse.quote(name, safe=safe_name)}@{urllib.parse.quote(version, safe='')}"


def _component(kind: str, name: str, version: str, *, checksum: str | None = None) -> dict[str, Any]:
    component: dict[str, Any] = {
        "type": "library",
        "name": name,
        "version": version,
        "purl": _purl(kind, name, version),
    }
    if checksum:
        component["hashes"] = [{"alg": "SHA-256", "content": checksum}]
    return component


def python_components() -> list[dict[str, Any]]:
    with (ROOT / "siyi/uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    packages = {
        canonicalize_name(str(item["name"])): item for item in lock.get("package", [])
    }
    runtime, build = application_dependency_sets()
    require_valid_policy()
    policy = load_policy()
    notice_covered = {
        canonicalize_name(name) for name in policy.get("packages", {})
    }
    explicitly_excluded = {
        canonicalize_name(name): metadata
        for name, metadata in policy.get("excluded_packages", {}).items()
    }
    components = []
    for name in sorted(runtime | build):
        package = packages[name]
        item = _component("pypi", str(package["name"]), str(package["version"]))
        properties = [
            {"name": "agent:pythonScope", "value": "runtime" if name in runtime else "build"}
        ]
        canonical = canonicalize_name(name)
        if canonical in explicitly_excluded:
            item["scope"] = "excluded"
            properties.extend(
                [
                    {"name": "agent:frozenArtifactDisposition", "value": "explicitly-excluded"},
                    {
                        "name": "agent:frozenArtifactExclusionReason",
                        "value": str(explicitly_excluded[canonical]["reason"]),
                    },
                ]
            )
        elif canonical in notice_covered:
            properties.append({"name": "agent:thirdPartyNotice", "value": "covered"})
        elif name in runtime:
            raise RuntimeError(
                f"frozen runtime package is missing third-party notice coverage: {name}"
            )
        item["properties"] = properties
        components.append(item)
    return components


def npm_components() -> list[dict[str, Any]]:
    lock = json.loads((ROOT / "desktop/frontend/package-lock.json").read_text(encoding="utf-8"))
    components = []
    for path, item in lock.get("packages", {}).items():
        if not path or not item.get("version"):
            continue
        name = item.get("name") or path.rsplit("node_modules/", 1)[-1]
        components.append(_component("npm", name, str(item["version"])))
    return components


def cargo_components() -> list[dict[str, Any]]:
    with (ROOT / "desktop/src-tauri/Cargo.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    return [
        _component("cargo", item["name"], item["version"], checksum=item.get("checksum"))
        for item in lock.get("package", [])
        if item.get("name") != "app"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the Agent CycloneDX software bill of materials")
    parser.add_argument("--output", default="dist/release/agent-sbom.cdx.json")
    args = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    components = {item["purl"]: item for item in [*python_components(), *npm_components(), *cargo_components()]}
    lock_digest = hashlib.sha256(
        b"".join((ROOT / path).read_bytes() for path in ("siyi/requirements.lock", "siyi/uv.lock", "desktop/frontend/package-lock.json", "desktop/src-tauri/Cargo.lock"))
    ).hexdigest()
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'Agent:{version}:{lock_digest}')}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": {
                "type": "application",
                "name": "Agent",
                "version": version,
                "bom-ref": f"pkg:generic/aureliuswu-agent@{version}",
            },
            "properties": [{"name": "agent:lockDigest", "value": lock_digest}],
        },
        "components": sorted(components.values(), key=lambda item: item["purl"]),
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {output} with {len(components)} locked components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
