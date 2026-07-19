from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import settings
from .database import connect, now_iso, rows
from .extension_sdk import ExtensionManifest, ExtensionToolRoute, build_tool_routes, extension_tool_name, validate_manifest
from .sandbox import safe_path, workspace_root


REQUIRED_PACKAGE_ENTRIES = ("manifest.json", "README.md", "tools", "skills", "tests")


def extension_root() -> Path:
    root = settings.extension_directory or (Path(settings.database_path).resolve().parent / "extensions")
    resolved = Path(root).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _load_manifest(directory: Path) -> ExtensionManifest:
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > 200_000:
        raise ValueError("扩展缺少有效 manifest.json")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ExtensionManifest.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"扩展清单无效：{exc}") from exc
    validate_manifest(manifest)
    return manifest


def _validate_package(directory: Path) -> tuple[ExtensionManifest, int, int]:
    if not directory.is_dir():
        raise ValueError("扩展来源必须是目录")
    for name in REQUIRED_PACKAGE_ENTRIES:
        if not (directory / name).exists():
            raise ValueError(f"扩展包缺少 {name}")
    file_count = 0
    total_bytes = 0
    for item in directory.rglob("*"):
        if item.is_symlink():
            raise ValueError("扩展包不允许包含符号链接")
        if not item.is_file():
            continue
        file_count += 1
        total_bytes += item.stat().st_size
        if file_count > settings.extension_max_files or total_bytes > settings.extension_max_bytes:
            raise ValueError("扩展包超过文件数或总大小限制")
    manifest = _load_manifest(directory)
    for relative in manifest.skills:
        path = (directory / relative).resolve()
        if directory.resolve() not in path.parents or not path.is_file():
            raise ValueError(f"Skill 文件不存在或越界：{relative}")
    return manifest, file_count, total_bytes


def package_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in directory.rglob("*") if item.is_file()), key=lambda item: item.relative_to(directory).as_posix()):
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if relative == "manifest.json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("signature", None)
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        else:
            data = path.read_bytes()
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _active_manifest_rows(exclude_extension_id: str | None = None) -> list[tuple[dict[str, Any], ExtensionManifest]]:
    records = rows("SELECT * FROM extension_packages WHERE enabled=1 ORDER BY extension_id")
    loaded: list[tuple[dict[str, Any], ExtensionManifest]] = []
    for record in records:
        if exclude_extension_id and record["extension_id"] == exclude_extension_id:
            continue
        try:
            directory = Path(record["install_path"]).resolve()
            manifest, _, _ = _validate_package(directory)
            if package_digest(directory) != record["digest"]:
                raise ValueError("扩展文件已在安装后变更")
            loaded.append((record, manifest))
        except Exception as exc:
            with connect() as db:
                db.execute("UPDATE extension_packages SET last_error=? WHERE id=?", (str(exc)[:1000], record["id"]))
    changed = True
    while changed:
        changed = False
        active_ids = {manifest.id for _, manifest in loaded}
        for record, manifest in list(loaded):
            missing = sorted(set(manifest.dependencies) - active_ids)
            if not missing:
                continue
            loaded.remove((record, manifest))
            changed = True
            with connect() as db:
                db.execute(
                    "UPDATE extension_packages SET last_error=? WHERE id=?",
                    (f"已启用依赖不可用：{', '.join(missing)}", record["id"]),
                )
    with connect() as db:
        db.executemany("UPDATE extension_packages SET last_error=NULL WHERE id=?", [(record["id"],) for record, _ in loaded])
    return loaded


def _validate_dependencies(candidate: ExtensionManifest) -> None:
    manifests = [manifest for _, manifest in _active_manifest_rows(exclude_extension_id=candidate.id)]
    available = {item.id for item in manifests}
    missing = sorted(set(candidate.dependencies) - available)
    if missing:
        raise ValueError(f"扩展缺少已启用依赖：{', '.join(missing)}")
    graph = {item.id: set(item.dependencies) for item in [*manifests, candidate]}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("扩展依赖存在循环")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, set()):
            if dependency in graph:
                visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for extension_id in graph:
        visit(extension_id)


def install_extension(workspace: str, source_path: str, *, enable: bool = True) -> dict[str, Any]:
    workspace = str(workspace_root(workspace))
    source = safe_path(workspace_root(workspace), source_path, must_exist=True)
    manifest, file_count, total_bytes = _validate_package(source)
    digest = package_digest(source)
    signature_status = "unsigned"
    if manifest.signature is not None:
        if manifest.signature.digest != digest:
            raise ValueError("扩展签名摘要与包内容不一致")
        signature_status = "verified"
    if enable:
        _validate_dependencies(manifest)

    root = extension_root()
    parent = (root / manifest.id).resolve()
    if root not in parent.parents:
        raise ValueError("扩展 ID 导致非法安装路径")
    parent.mkdir(parents=True, exist_ok=True)
    target = (parent / manifest.version).resolve()
    if target.exists():
        raise ValueError(f"扩展 {manifest.id} {manifest.version} 已安装")
    staging = (root / f".staging-{uuid.uuid4().hex}").resolve()
    if root not in staging.parents:
        raise ValueError("无效扩展暂存路径")
    try:
        shutil.copytree(source, staging)
        staged_manifest, _, _ = _validate_package(staging)
        if staged_manifest != manifest or package_digest(staging) != digest:
            raise ValueError("扩展复制后校验失败")
        staging.replace(target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    stamp = now_iso()
    try:
        with connect() as db:
            if enable:
                db.execute("UPDATE extension_packages SET enabled=0 WHERE extension_id=?", (manifest.id,))
            db.execute(
                "INSERT INTO extension_packages(extension_id, version, name, manifest, install_path, digest, signature_status, enabled, installed_at, activated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest.id,
                    manifest.version,
                    manifest.name,
                    manifest.model_dump_json(),
                    str(target),
                    digest,
                    signature_status,
                    int(enable),
                    stamp,
                    stamp if enable else None,
                ),
            )
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {
        "extension_id": manifest.id,
        "version": manifest.version,
        "name": manifest.name,
        "enabled": enable,
        "digest": digest,
        "signature_status": signature_status,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def list_extensions() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    records = rows("SELECT * FROM extension_packages ORDER BY extension_id, id DESC")
    for item in records:
        try:
            manifest = ExtensionManifest.model_validate_json(item["manifest"])
            permissions = list(manifest.permissions)
            contribution_counts = {"tools": len(manifest.tools), "skills": len(manifest.skills), "agents": len(manifest.agents), "ui": len(manifest.ui)}
            description = manifest.description
        except ValidationError:
            permissions, contribution_counts, description = [], {"tools": 0, "skills": 0, "agents": 0, "ui": 0}, ""
        result.append(
            {
                "extension_id": item["extension_id"],
                "version": item["version"],
                "name": item["name"],
                "description": description,
                "enabled": bool(item["enabled"]),
                "rollback_available": bool(
                    item["enabled"]
                    and any(
                        candidate["extension_id"] == item["extension_id"] and candidate["id"] < item["id"]
                        for candidate in records
                    )
                ),
                "digest": item["digest"],
                "signature_status": item["signature_status"],
                "permissions": permissions,
                "contributions": contribution_counts,
                "installed_at": item["installed_at"],
                "activated_at": item["activated_at"],
                "last_error": item["last_error"],
            }
        )
    return result


def set_extension_enabled(extension_id: str, version: str, enabled: bool) -> dict[str, Any]:
    records = rows("SELECT * FROM extension_packages WHERE extension_id=? AND version=?", (extension_id, version))
    if not records:
        raise ValueError("扩展版本不存在")
    record = records[0]
    manifest, _, _ = _validate_package(Path(record["install_path"]))
    if package_digest(Path(record["install_path"])) != record["digest"]:
        raise ValueError("扩展内容已变更，拒绝启用")
    if enabled:
        _validate_dependencies(manifest)
    else:
        dependents = [item.id for _, item in _active_manifest_rows() if item.id != extension_id and extension_id in item.dependencies]
        if dependents:
            raise ValueError(f"仍有已启用扩展依赖它：{', '.join(sorted(dependents))}")
    stamp = now_iso()
    with connect() as db:
        if enabled:
            db.execute("UPDATE extension_packages SET enabled=0 WHERE extension_id=?", (extension_id,))
        db.execute(
            "UPDATE extension_packages SET enabled=?, activated_at=?, last_error=NULL WHERE id=?",
            (int(enabled), stamp if enabled else record.get("activated_at"), record["id"]),
        )
    return {"extension_id": extension_id, "version": version, "enabled": enabled}


def rollback_extension(extension_id: str) -> dict[str, Any]:
    versions = rows("SELECT * FROM extension_packages WHERE extension_id=? ORDER BY id DESC", (extension_id,))
    current = next((item for item in versions if item["enabled"]), None)
    if current is None:
        raise ValueError("扩展当前没有启用版本")
    candidate = next((item for item in versions if item["id"] < current["id"]), None)
    if candidate is None:
        raise ValueError("没有可回滚的旧版扩展")
    return set_extension_enabled(extension_id, str(candidate["version"]), True)


def active_extension_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for _, manifest in _active_manifest_rows():
        local_tools = {item.id: extension_tool_name(manifest.id, item.id) for item in manifest.tools}
        for declaration in manifest.agents:
            profiles.append(
                {
                    **declaration.model_dump(),
                    "id": f"{manifest.id}.{declaration.id}",
                    "tool_allowlist": [local_tools.get(item, item) for item in declaration.tool_allowlist],
                    "extension_id": manifest.id,
                    "extension_version": manifest.version,
                }
            )
    return profiles


def active_extension_skill_paths() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for record, manifest in _active_manifest_rows():
        directory = Path(record["install_path"])
        for relative in manifest.skills:
            result.append(
                {
                    "extension_id": manifest.id,
                    "version": manifest.version,
                    "path": relative,
                    "absolute_path": str((directory / relative).resolve()),
                }
            )
    return result


def active_extension_tools() -> tuple[list[dict[str, Any]], dict[str, ExtensionToolRoute]]:
    routes: dict[str, ExtensionToolRoute] = {}
    for _, manifest in _active_manifest_rows():
        routes.update(build_tool_routes(manifest))
    return [route.openai() for route in routes.values()], routes
