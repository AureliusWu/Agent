"""Process-local, non-serializable authority for one exact file transaction."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from app.database import rows
from app.permissions import PermissionDecision, permission_denial
from app.sandbox import FileVersionError, file_version_token, safe_path
from app.tools.batch_plan import FileBatchPlan
from app.tools.registry import REGISTRY

_ISSUER = object()
BATCH_GRANT_TTL_SECONDS = 120


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class BatchGrant:
    def __init__(self, issuer: object, *, plan: FileBatchPlan, operations_hash: str,
                 mode: str, conversation_id: int | None, task_id: str | None,
                 confirmed: bool, batch_id: str):
        if issuer is not _ISSUER:
            raise PermissionError("Batch grants can only be issued by the batch permission broker")
        self._issuer = issuer
        self._plan = plan
        self._operations_hash = operations_hash
        self._mode, self._conversation_id, self._task_id = mode, conversation_id, task_id
        self._confirmed = confirmed
        self._batch_id = batch_id
        self._expires_at = time.monotonic() + BATCH_GRANT_TTL_SECONDS
        self._active = True
        self._next_index = 0
        self._rollback: dict[str, tuple[str, dict[str, str], str]] = {}
        authority = (rows("SELECT workspace,permission_mode FROM conversations WHERE id=?", (conversation_id,))
                     if conversation_id is not None else [])
        self._persisted_conversation = bool(authority)
        self._authoritative_mode = str(authority[0]["permission_mode"]) if authority else mode
        # Runtime taint handling may tighten agent/full to ask without changing
        # the conversation. Bind both modes, never silently widen either one.
        self._compatible_authority = (
            (mode == self._authoritative_mode or (mode == "ask" and self._authoritative_mode in {"agent", "full"}))
            and (not authority or Path(authority[0]["workspace"]).resolve() == plan.root)
        )

    def __reduce__(self):
        raise TypeError("Batch grants must never be serialized")

    def _denied(self, code: str = "batch_grant_invalid") -> PermissionDecision:
        return PermissionDecision(False, False, {"success": False, "status": "blocked",
            "error_code": code, "error_message": "批操作授权已失效、上下文不匹配或文件已变化"})

    def _context(self, values: dict[str, Any], *, rollback: bool = False) -> PermissionDecision | None:
        if (self._issuer is not _ISSUER or not self._active or not self._compatible_authority
                or (not rollback and time.monotonic() >= self._expires_at)
                or values.get("mode") != self._mode
                or values.get("conversation_id") != self._conversation_id
                or values.get("task_id") != self._task_id
                or Path(str(values.get("workspace") or "")).resolve() != self._plan.root):
            return self._denied()
        if self._conversation_id is not None:
            current = rows("SELECT workspace,permission_mode FROM conversations WHERE id=?", (self._conversation_id,))
            if bool(current) != self._persisted_conversation:
                return self._denied("batch_context_changed")
            if current and (Path(current[0]["workspace"]).resolve() != self._plan.root
                            or current[0]["permission_mode"] != self._authoritative_mode):
                return self._denied("batch_context_changed")
        return permission_denial(mode=self._mode, risk=REGISTRY[values["tool"]].risk,
                                 tool=values["tool"], workspace=str(self._plan.root))

    def child_permission(self, index: int, operations_hash: str):
        def authorize_child(**values: Any) -> PermissionDecision:
            denied = self._context(values)
            step = self._plan.steps[index]
            if denied:
                return denied
            if (operations_hash != self._operations_hash or index != self._next_index
                    or values.get("tool") != step.tool
                    or _hash(values.get("arguments")) != _hash(step.arguments)):
                return self._denied()
            self._next_index += 1
            return PermissionDecision(True, self._confirmed)
        return authorize_child

    def record_change(self, change_id: str, index: int) -> None:
        if not re.fullmatch(r"\d+-[a-f0-9]{8}", change_id):
            raise PermissionError("Invalid batch change id")
        folder = self._plan.root / ".agent-backups" / change_id
        raw = (folder / "manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(raw)
        if manifest.get("tool_call_id") != f"{self._batch_id}:{index}" or manifest.get("task_id") != self._task_id:
            raise PermissionError("Batch change ownership mismatch")
        paths = {entry["path"] for entry in manifest.get("entries", [])}
        if paths != set(self._plan.steps[index].before):
            raise PermissionError("Batch rollback paths differ from the approved step")
        step = self._plan.steps[index]
        for entry in manifest["entries"]:
            before = entry["before"]
            recorded = (f"file:{before['size']}:{before['sha256']}" if before.get("type") == "file"
                        else "missing" if not before.get("exists") else step.before[entry["path"]])
            if recorded != step.before[entry["path"]]:
                raise PermissionError("Batch backup differs from the preflight version")
        after = {**step.before, **step.after}
        for path, expected in after.items():
            if expected == "directory:planned":
                target = safe_path(self._plan.root, path)
                if not target.is_dir():
                    raise FileVersionError("version_conflict", "批操作创建目录后路径类型已变化")
                # Child writes legitimately change the directory timestamp.
                # For a directory created by THIS batch, retain filesystem
                # identity and require emptiness at undo instead.
                after[path] = f"created-directory:{target.stat().st_ino}"
        self._rollback[change_id] = (_hash(manifest), after, str(folder))
        for path, expected in after.items():
            if expected.startswith("created-directory:"):
                continue
            if file_version_token(safe_path(self._plan.root, path)) != expected:
                raise FileVersionError("version_conflict", "批操作后检测到并发文件变化；保留原备份等待处理")

    def rollback_permission(self, change_id: str):
        def authorize_rollback(**values: Any) -> PermissionDecision:
            # Expiry prevents NEW effects; restoring already-issued effects is
            # permitted only while this grant is active and exact proof matches.
            denied = self._context(values, rollback=True)
            if denied:
                return denied
            proof = self._rollback.get(change_id)
            if (not proof or values.get("tool") != "undo_file_change"
                    or values.get("arguments") != {"change_id": change_id}):
                return self._denied()
            try:
                manifest = json.loads((Path(proof[2]) / "manifest.json").read_text(encoding="utf-8"))
                if _hash(manifest) != proof[0]:
                    return self._denied("batch_rollback_conflict")
                for path, expected in proof[1].items():
                    target = safe_path(self._plan.root, path)
                    if expected.startswith("created-directory:"):
                        if (not target.is_dir() or f"created-directory:{target.stat().st_ino}" != expected
                                or any(target.iterdir())):
                            return self._denied("batch_rollback_conflict")
                        continue
                    if file_version_token(target) != expected:
                        return self._denied("batch_rollback_conflict")
                    if expected.startswith("directory:") and any(target.iterdir()):
                        return self._denied("batch_rollback_conflict")
                for entry in manifest["entries"]:
                    if entry.get("backup") and entry["before"]["type"] == "file":
                        backup = Path(proof[2]) / entry["backup"]
                        token = f"file:{entry['before']['size']}:{entry['before']['sha256']}"
                        if file_version_token(backup) != token:
                            return self._denied("batch_rollback_conflict")
            except (OSError, ValueError, KeyError):
                return self._denied("batch_rollback_conflict")
            self._rollback.pop(change_id)
            return PermissionDecision(True, self._confirmed)
        return authorize_rollback

    def close(self) -> None:
        self._active = False
        self._rollback.clear()


def issue_batch_grant(*, decision: PermissionDecision, **values: Any) -> BatchGrant:
    if not decision.allowed:
        raise PermissionError("Denied plans cannot receive a batch grant")
    return BatchGrant(_ISSUER, confirmed=decision.confirmed, **values)
