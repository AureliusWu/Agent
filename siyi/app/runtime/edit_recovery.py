"""Narrow, evidence-only recovery of built-in edit precondition failures.

This module never executes a tool or changes a task verdict. Missing, stale or
ambiguous evidence leaves the original failure unresolved. AST checks establish
an observable verification association, not correctness of arbitrary code. A
recorded edit to a checker after repair is rejected; untracked external edits
cannot be reconstructed from the current receipt protocol.
"""
from __future__ import annotations

import ast
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from app.database import rows, sanitize_details
from app.sandbox import _file_state, _list_changes, safe_path, workspace_root
from app.runtime.task_verifiers import classify_command, verify_domain
from app.tools.receipts import MUTATION_TOOLS
from app.workspace.file_recovery import checked_path, state_token

_READS = frozenset({"read_file", "read_file_range", "file_metadata", "file_info", "list_files", "list_directory",
    "search_files", "search_text", "file_diff", "view_diff", "compare_files", "get_repo_map", "find_symbol",
    "find_definition", "find_references", "list_module_dependencies", "find_related_tests", "get_call_chain",
    "inspect_diagnostics", "list_file_changes", "list_security_snapshots", "preview_security_snapshot"})
_CODE = "edit_match_precondition"
_MAX_RUNS = 2000


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value) > 100_000:
            raise ValueError("oversized evidence")
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("object evidence required")
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp required")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("explicit timestamp required")
    return result


def _path(root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("path required")
    raw = Path(value)
    lexical = raw if raw.is_absolute() else root / raw
    for ancestor in (lexical, *lexical.parents):
        if ancestor.exists() or ancestor.is_symlink():
            info = ancestor.lstat()
            if ancestor.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("linked evidence path")
        if ancestor == root:
            break
    relative = safe_path(root, value).relative_to(root).as_posix()
    if relative in {".", ""} or relative.split("/", 1)[0].casefold() in {".agent-backups", ".git", ".agent"}:
        raise ValueError("internal/root path")
    checked_path(root, relative)
    return relative


def _overlap(left: str, right: str) -> bool:
    left, right = left.casefold().rstrip("/"), right.casefold().rstrip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _state(value: Any) -> dict[str, Any]:
    value = _object(value)
    if (value.get("exists") is not True or value.get("type") != "file" or type(value.get("size")) is not int
            or not 0 <= value["size"] <= 20 * 1024 * 1024
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("sha256") or ""))):
        raise ValueError("bounded file state required")
    return value


def _host(task_id: str, run: dict[str, Any]) -> dict[str, Any]:
    if (run.get("task_id") != task_id or run.get("source") != "builtin" or type(run.get("id")) is not int
            or run["id"] <= 0 or not isinstance(run.get("execution_id"), str) or not run["execution_id"]):
        raise ValueError("host task execution identity required")
    arguments, output = _object(run.get("input")), _object(run.get("output"))
    receipt = _object(output.get("receipt"))
    call = receipt.get("tool_call_id")
    if (type(receipt.get("receipt_version")) is not int or receipt["receipt_version"] != 2
            or receipt.get("task_id") != task_id or receipt.get("tool") != run.get("tool")
            or not isinstance(call, str) or not call or not isinstance(receipt.get("receipt_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", receipt["receipt_id"])
            # Arguments are sanitized once for tool_runs.input. The receipt's
            # already-sanitized arguments pass through the log sanitizer again.
            or receipt.get("normalized_arguments") != sanitize_details(arguments)
            or receipt.get("status") != run.get("status") or output.get("status") != run.get("status")):
        raise ValueError("task/call/arguments receipt binding required")
    persisted = rows("SELECT * FROM tool_receipts WHERE receipt_id=? AND task_id=?", (receipt["receipt_id"], task_id))
    if (len(persisted) != 1 or persisted[0].get("tool_call_id") != call
            or persisted[0].get("tool_name") != run.get("tool")
            or persisted[0].get("status") != receipt.get("standard_status")
            or _object(persisted[0].get("receipt_json")) != receipt):
        raise ValueError("persisted host receipt differs")
    start, end = _time(run.get("started_at")), _time(run.get("finished_at"))
    if end < start or not start <= _time(receipt.get("finished_at")) <= end:
        raise ValueError("execution interval differs")
    return {"run": run, "arguments": arguments, "output": output, "data": _object(output.get("data", output)),
            "receipt": receipt, "start": start, "end": end, "call": call}


def _success(item: dict[str, Any]) -> bool:
    output, receipt = item["output"], item["receipt"]
    return (item["run"].get("status") == "ok" and output.get("success") is True and receipt.get("success") is True
            and receipt.get("standard_status") == "SUCCESS" and receipt.get("permission_decision") == "approved"
            and not output.get("error_code") and not receipt.get("error_code")
            and output.get("truncated", False) is False and receipt.get("truncated") is False
            and output.get("timed_out", False) is False
            and item["arguments"].get("dry_run", False) is False and item["data"].get("dry_run", False) is False)


def _precondition(item: dict[str, Any], root: Path) -> tuple[str, str]:
    out, data, receipt = item["output"], item["data"], item["receipt"]
    condition = _object(data.get("edit_precondition"))
    target = _path(root, item["arguments"].get("path"))
    expected, actual = condition.get("expected_count"), condition.get("actual_count")
    if (item["run"].get("tool") != "replace_text" or item["run"].get("status") != "error"
            or out.get("success") is not False or receipt.get("success") is not False
            or out.get("error_code") != _CODE or receipt.get("error_code") != _CODE
            or receipt.get("standard_status") != "FAILED" or receipt.get("permission_decision") != "evaluated"
            or receipt.get("operation_kind") != "mutation" or receipt.get("truncated") is not False
            or out.get("truncated", False) is not False or out.get("partial", False) is not False
            or out.get("timed_out", False) is not False or out.get("cancelled", False) is not False
            or out.get("scope_violation") or out.get("authorization_status") == "denied"
            or type(condition.get("schema_version")) is not int or condition["schema_version"] != 1
            or condition.get("kind") != "replace_text_match_count" or condition.get("side_effect") != "not_started"
            or condition.get("path") != target or type(expected) is not int or expected < 0
            or type(actual) is not int or actual < 0 or actual == expected
            or type(item["arguments"].get("expected_count", 1)) is not int or item["arguments"].get("expected_count", 1) != expected
            or receipt.get("changed_files") not in ([], ()) or receipt.get("observed_files") not in ([], ())
            or receipt.get("change_id") or data.get("change_id") or receipt.get("version_after") is not None):
        raise ValueError("not the exact no-side-effect edit precondition")
    version = data.get("version_before")
    # Production logs redact token-named arguments. Built-in _require_version
    # has already checked that request before emitting this exact typed error;
    # the unredacted state chain must still agree across receipt/data/journal.
    if (not isinstance(version, str) or not re.fullmatch(r"file:\d+:[a-f0-9]{64}", version)
            or receipt.get("version_before") != version
            or "expected_version_token" not in item["arguments"]
            or item["arguments"]["expected_version_token"] not in (version, "***")):
        raise ValueError("failed edit pre-version differs")
    return target, version


def _assertion_scope(root: Path, target: str, script: str) -> bool:
    """Observe target values in a small read/assert language; never execute it."""
    if not isinstance(script, str) or len(script) > 16_384:
        return False
    tree = ast.parse(script)
    if len(list(ast.walk(tree))) > 400 or any(not isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign, ast.Assert, ast.Expr)) for n in tree.body):
        return False
    target_names: set[str] = set()
    path_names: set[str] = set()
    path_values: set[str] = set()
    observed: set[str] = set()
    pure = {"len", "str", "int", "float", "bool", "tuple", "list", "set", "dict", "sum", "all", "any", "sorted", "min", "max", "abs", "round", "isinstance", "print"}
    def target_reference(node: ast.AST) -> bool:
        while isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError("private target attribute")
            node = node.value
        return isinstance(node, ast.Name) and node.id in target_names
    def path_object(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in path_values
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in path_names:
            if len(node.args) != 1 or node.keywords or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise ValueError("dynamic path")
            return _path(root, node.args[0].value) == target
        return False
    def dependency(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in observed
        if isinstance(node, ast.Constant):
            return False
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ValueError("private/dynamic attribute")
            return target_reference(node) or dependency(node.value)
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name in path_names:
                path_object(node)
                return False  # Construction/truthiness is not a file observation.
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "read_bytes", "exists", "is_file"}:
                if node.args or any(kw.arg not in {"encoding", "errors"} or not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str) for kw in node.keywords):
                    raise ValueError("dynamic read arguments")
                if not path_object(node.func.value):
                    raise ValueError("read is not bound to the target path")
                return True
            bound = target_reference(node.func)
            if not bound and name not in pure:
                raise ValueError("unknown call")
            values = [dependency(arg) for arg in node.args] + [dependency(kw.value) for kw in node.keywords]
            return False if name == "print" else bound or any(values)
        if isinstance(node, ast.BoolOp):
            return bool(node.values) and all([dependency(value) for value in node.values])
        if isinstance(node, ast.Compare) and len(node.ops) != 1:
            # Later operands of a chained comparison may never execute. A
            # syntactic target reference in one is not an observed check.
            raise ValueError("short-circuiting comparison chain")
        if isinstance(node, (ast.Compare, ast.BinOp, ast.UnaryOp, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Subscript, ast.Slice)):
            return any([dependency(child) for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)])
        raise ValueError("dynamic/unbounded assertion expression")
    found = False
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            for alias in node.names:
                imported = module or alias.name
                if alias.name == "*" or (isinstance(node, ast.ImportFrom) and node.level):
                    return False
                bound = alias.asname or alias.name.split(".", 1)[0]
                # Imports rebind names just like assignments; an old target
                # alias cannot lend provenance to an unrelated new binding.
                target_names.discard(bound)
                path_names.discard(bound)
                path_values.discard(bound)
                observed.discard(bound)
                if imported == "pathlib" and isinstance(node, ast.ImportFrom) and alias.name == "Path":
                    path_names.add(bound)
                elif _path(root, imported.replace(".", "/") + ".py") == target:
                    target_names.add(bound)
                else:
                    return False
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                return False
            is_path, value = path_object(node.value), dependency(node.value)
            name = node.targets[0].id
            target_names.discard(name); path_names.discard(name); path_values.discard(name); observed.discard(name)
            if is_path:
                path_values.add(name)
            elif value:
                observed.add(name)
        elif isinstance(node, ast.Assert):
            found = dependency(node.test) or found
        else:
            dependency(node.value)
    return found


def _verification(item: dict[str, Any], root: Path, target: str, plan: Any, project: dict[str, Any], checks: list[dict[str, Any]]) -> set[str] | None:
    if item["run"].get("tool") != "run_command" or not _success(item):
        return None
    data, receipt, arguments = item["data"], item["receipt"], item["arguments"]
    if type(data.get("exit_code")) is not int or data["exit_code"] != 0 or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0:
        return None
    cwd = safe_path(root, str(arguments.get("cwd") or "."))
    if cwd != root:
        return None
    command, args = arguments.get("command"), arguments.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        return None
    affected = arguments.get("affected_paths", [])
    if not isinstance(affected, list) or any(not isinstance(path, str) for path in affected):
        return None
    if affected and not any(safe_path(root, path) == root or _overlap(_path(root, path), target) for path in affected):
        return None
    full = any(command == entry.get("command") and args == entry.get("args") and entry.get("name") in {"test", "check", "lint", "typecheck", "build"}
               for entry in project.get("commands", []) if isinstance(entry, dict))
    protected = {""} if full else set()  # Registered commands may depend on any project script/config.
    if not full:
        args = list(args)
        while args and args[0] in {"-B", "-u"}:
            args.pop(0)
        if Path(command).name.casefold() not in {"python", "python.exe", "python3", "python3.exe"}:
            return None
        if len(args) == 2 and args[0] == "-c":
            script = args[1]
        elif len(args) == 1 and Path(args[0]).name.casefold() == "check.py":
            path = checked_path(root, _path(root, args[0]))
            protected.add(_path(root, args[0]))
            if path.stat().st_size > 16_384:
                return None
            script = path.read_text(encoding="utf-8")
            matching = [c for c in checks if _path(root, c.get("target")) == _path(root, args[0])]
            if matching and (len(matching) != 1 or matching[0].get("status") != "passed" or matching[0].get("expected") != _file_state(path)):
                return None
        else:
            return None
        if not _assertion_scope(root, target, script):
            return None
    text = " ".join([command, *args])
    evidence = {"verification_commands": [{"status": "passed", "command": text, "tags": sorted(classify_command(text)), "project_types": project.get("types", [])}]}
    domains = {getattr(c, "parameters", {}).get("domain") for c in getattr(plan, "acceptance_criteria", ()) if getattr(c, "kind", "") == "domain_verification" and getattr(c, "required", True)}
    domains &= {"code", "api", "ui", "database", "security"}
    return protected if all(verify_domain(domain, "edit_recovery", "write-after verification association", evidence).get("status") == "passed" for domain in (domains or {"code"})) else None


def _interferes(root: Path, run: dict[str, Any], target: str, allowed_commands: set[int]) -> bool:
    if run.get("id") in allowed_commands:
        return False
    tool, source = str(run.get("tool") or ""), run.get("source")
    if source in {"builtin", "builtin:file_core", "cache"} and tool in _READS:
        return False
    payload = _object(run.get("input"))
    if source not in {"builtin", "builtin:file_core"} or tool not in MUTATION_TOOLS or tool == "run_command":
        return True
    paths = [payload.get(key) for key in ("path", "source", "destination") if payload.get(key)]
    if not paths or tool.startswith("undo") or tool == "restore_security_snapshot":
        return True
    return any(not target or _overlap(_path(root, path), target) for path in paths)


def verified_edit_recoveries(task_id: str, workspace: str, tool_runs: list[dict[str, Any]], file_checks: list[dict[str, Any]], plan: Any, project: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Return only positively proven per-failure recoveries, never a task verdict."""
    recovered: dict[int, dict[str, Any]] = {}
    try:
        if not task_id or not 0 < len(tool_runs) <= _MAX_RUNS or len(file_checks) > 400:
            return recovered
        root = workspace_root(workspace)
        if any(type(run.get("id")) is not int or run.get("task_id") != task_id for run in tool_runs):
            return recovered
        if [run["id"] for run in tool_runs] != sorted({run["id"] for run in tool_runs}):
            return recovered
        declared = {_path(root, p) for p in (*getattr(plan, "expected_paths", ()), *getattr(plan, "expected_changes", ()))}
        changes = _list_changes(root, task_id, include_restored=True)
        cache: dict[int, dict[str, Any] | None] = {}
        def host(run: dict[str, Any]) -> dict[str, Any]:
            if run["id"] not in cache:
                try:
                    cache[run["id"]] = _host(task_id, run)
                except Exception:
                    cache[run["id"]] = None
            if cache[run["id"]] is None:
                raise ValueError("untrusted host evidence")
            return cache[run["id"]]  # type: ignore[return-value]
        for index, failed_run in enumerate(tool_runs):
            if failed_run.get("tool") != "replace_text" or failed_run.get("status") != "error":
                continue
            try:
                failed = host(failed_run)
                target, before = _precondition(failed, root)
                if target not in declared or any(change.get("tool_call_id") == failed["call"] for change in changes):
                    continue
                for position in range(index + 1, len(tool_runs)):
                    candidate_run = tool_runs[position]
                    if candidate_run.get("tool") not in {"write_file", "replace_text"} or candidate_run.get("status") != "ok":
                        continue
                    candidate = host(candidate_run)
                    if not _success(candidate) or _path(root, candidate["arguments"].get("path")) != target or candidate["start"] < failed["end"]:
                        continue
                    data, receipt = candidate["data"], candidate["receipt"]
                    if (data.get("version_before") != before or receipt.get("version_before") != before
                            or "expected_version_token" not in candidate["arguments"]
                            or candidate["arguments"]["expected_version_token"] not in (before, "***")
                            or receipt.get("operation_kind") != "mutation" or not receipt.get("change_id")
                            or receipt.get("change_id") != data.get("change_id")
                            or receipt.get("changed_files") not in ([target], (target,))):
                        continue
                    matches = [c for c in changes if c.get("id") == receipt["change_id"]]
                    if len(matches) != 1:
                        continue
                    change = matches[0]
                    if (change.get("task_id") != task_id or change.get("tool_call_id") != candidate["call"]
                            or change.get("operation") != candidate_run["tool"] or change.get("recovery")
                            or len(change.get("entries") or []) != 1):
                        continue
                    entry = change["entries"][0]
                    after = _state(entry.get("after"))
                    if (_path(root, entry.get("path")) != target or state_token(_state(entry.get("before"))) != before
                            or data.get("version_after") != state_token(after) or receipt.get("version_after") != state_token(after)):
                        continue
                    checks = [c for c in file_checks if _path(root, c.get("target")) == target]
                    if len(checks) != 1 or checks[0].get("status") != "passed" or checks[0].get("expected") != after or checks[0].get("actual") != after or _file_state(checked_path(root, target)) != after:
                        continue
                    later_changes = [c for c in changes if c.get("id") != change["id"] and (int(str(c["id"]).split("-", 1)[0]) > int(str(change["id"]).split("-", 1)[0]) or c.get("recovery"))]
                    if any(_overlap(_path(root, e.get("path")), target) for c in later_changes for e in c.get("entries") or []):
                        continue
                    commands, protected = [], set()
                    for run in tool_runs[position + 1:]:
                        try:
                            check = host(run)
                            scope = _verification(check, root, target, plan, project, file_checks) if check["start"] >= candidate["end"] else None
                            if scope is not None:
                                commands.append(run["id"])
                                protected.update(scope)
                        except Exception:
                            continue
                    if not commands or any(_interferes(root, run, target, set(commands)) for run in tool_runs[position + 1:]):
                        continue
                    if any(_interferes(root, run, path, set(commands)) for path in protected for run in tool_runs[position + 1:]):
                        continue
                    if any(not path or _overlap(_path(root, entry.get("path")), path) for path in protected for later in later_changes for entry in later.get("entries") or []):
                        continue
                    if any(_interferes(root, run, target, set()) for run in tool_runs[index + 1:position]):
                        continue
                    recovered[failed_run["id"]] = {"repaired_by_run_id": candidate_run["id"], "verification_run_id": commands[-1], "recovery_kind": "verified_builtin_edit", "target": target, "change_id": receipt["change_id"]}
                    break
            except Exception:
                continue
    except Exception:
        return {}
    return recovered
