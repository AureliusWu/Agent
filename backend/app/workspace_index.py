from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import threading
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


SOURCE_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
}
IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
}
MAX_FILES = 10_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 50_000_000


@dataclass(frozen=True)
class IndexedFile:
    path: str
    language: str
    size: int
    modified_ns: int


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    container: str | None = None
    signature: str | None = None


@dataclass(frozen=True)
class Reference:
    name: str
    kind: str
    path: str
    line: int
    container: str | None = None


@dataclass(frozen=True)
class Dependency:
    source: str
    target: str
    kind: str
    line: int


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    severity: str
    message: str
    source: str


@dataclass(frozen=True)
class WorkspaceIndex:
    root: str
    fingerprint: str
    files: tuple[IndexedFile, ...]
    symbols: tuple[Symbol, ...]
    references: tuple[Reference, ...]
    dependencies: tuple[Dependency, ...]
    diagnostics: tuple[Diagnostic, ...]
    truncated: bool


_CACHE: dict[str, WorkspaceIndex] = {}
_CACHE_LOCK = threading.Lock()


def _source_files(root: Path) -> tuple[list[tuple[Path, str, int, int]], bool]:
    entries: list[tuple[Path, str, int, int]] = []
    total_bytes = 0
    truncated = False
    stop = False
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            (name for name in directories if name not in IGNORED_DIRECTORIES),
            key=str.casefold,
        )
        for filename in sorted(filenames, key=str.casefold):
            path = Path(current) / filename
            language = SOURCE_LANGUAGES.get(path.suffix.lower())
            if not language:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_FILE_BYTES:
                truncated = True
                continue
            if len(entries) >= MAX_FILES or total_bytes + stat.st_size > MAX_TOTAL_BYTES:
                truncated = True
                stop = True
                break
            entries.append((path, language, stat.st_size, stat.st_mtime_ns))
            total_bytes += stat.st_size
        if stop:
            break
    return entries, truncated


def _fingerprint(root: Path, entries: Iterable[tuple[Path, str, int, int]]) -> str:
    digest = hashlib.sha256()
    for path, language, size, modified_ns in entries:
        digest.update(f"{path.relative_to(root).as_posix()}:{language}:{size}:{modified_ns}\0".encode())
    return digest.hexdigest()


def _qualified_container(stack: list[str]) -> str | None:
    return ".".join(stack) or None


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.stack: list[str] = []
        self.symbols: list[Symbol] = []
        self.references: list[Reference] = []
        self.dependencies: list[Dependency] = []

    def _definition(self, node: ast.AST, name: str, kind: str, signature: str | None = None) -> None:
        self.symbols.append(Symbol(name, kind, self.path, node.lineno, getattr(node, "end_lineno", node.lineno), _qualified_container(self.stack), signature))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node, node.name, "class")
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = [argument.arg for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        self._definition(node, node.name, "method" if self.stack else "function", f"{node.name}({', '.join(args)})")
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.dependencies.append(Dependency(self.path, alias.name, "import", node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        prefix = "." * node.level
        self.dependencies.append(Dependency(self.path, f"{prefix}{node.module or ''}", "import", node.lineno))

    def visit_Call(self, node: ast.Call) -> None:
        name = _python_name(node.func)
        if name:
            self.references.append(Reference(name, "call", self.path, node.lineno, _qualified_container(self.stack)))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references.append(Reference(node.id, "read", self.path, node.lineno, _qualified_container(self.stack)))


def _python_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _python_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _parse_python(relative: str, text: str) -> tuple[list[Symbol], list[Reference], list[Dependency], list[Diagnostic]]:
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        diagnostic = Diagnostic(relative, exc.lineno or 1, exc.offset or 0, "error", exc.msg, "python-ast")
        return [], [], [], [diagnostic]
    visitor = _PythonVisitor(relative)
    visitor.visit(tree)
    return visitor.symbols, visitor.references, visitor.dependencies, []


_TS_SYMBOL = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(class|function|interface|type|enum)\s+([A-Za-z_$][\w$]*)")
_TS_VARIABLE = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
_TS_IMPORT = re.compile(r"(?:from\s+|require\s*\(\s*)['\"]([^'\"]+)['\"]")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_RUST_SYMBOL = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(fn|struct|enum|trait|mod)\s+([A-Za-z_][\w]*)")
_RUST_IMPL = re.compile(r"^\s*impl(?:<[^>]+>)?\s+(?:[^\s]+\s+for\s+)?([A-Za-z_][\w:]*)")
_RUST_USE = re.compile(r"^\s*(?:pub\s+)?use\s+([^;]+)")


def _parse_typescript(relative: str, text: str) -> tuple[list[Symbol], list[Reference], list[Dependency], list[Diagnostic]]:
    symbols: list[Symbol] = []
    references: list[Reference] = []
    dependencies: list[Dependency] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        match = _TS_SYMBOL.match(line)
        if match:
            symbols.append(Symbol(match.group(2), match.group(1), relative, line_number, line_number))
        else:
            match = _TS_VARIABLE.match(line)
            if match:
                symbols.append(Symbol(match.group(1), "function", relative, line_number, line_number))
        for target in _TS_IMPORT.findall(line):
            dependencies.append(Dependency(relative, target, "import", line_number))
        for call in _CALL.findall(line):
            if call not in {"if", "for", "while", "switch", "catch"}:
                references.append(Reference(call, "call", relative, line_number))
    return symbols, references, dependencies, []


def _parse_rust(relative: str, text: str) -> tuple[list[Symbol], list[Reference], list[Dependency], list[Diagnostic]]:
    symbols: list[Symbol] = []
    references: list[Reference] = []
    dependencies: list[Dependency] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        match = _RUST_SYMBOL.match(line)
        if match:
            symbols.append(Symbol(match.group(2), match.group(1), relative, line_number, line_number))
        else:
            match = _RUST_IMPL.match(line)
            if match:
                symbols.append(Symbol(match.group(1), "impl", relative, line_number, line_number))
        use_match = _RUST_USE.match(line)
        if use_match:
            dependencies.append(Dependency(relative, use_match.group(1).strip(), "use", line_number))
        for call in _CALL.findall(line):
            references.append(Reference(call, "call", relative, line_number))
    return symbols, references, dependencies, []


def build_workspace_index(workspace: str | Path, *, force: bool = False) -> tuple[WorkspaceIndex, bool]:
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace is not a directory")
    entries, truncated = _source_files(root)
    fingerprint = _fingerprint(root, entries)
    root_key = str(root)
    with _CACHE_LOCK:
        cached = _CACHE.get(root_key)
        if not force and cached and cached.fingerprint == fingerprint:
            return cached, True

    files: list[IndexedFile] = []
    symbols: list[Symbol] = []
    references: list[Reference] = []
    dependencies: list[Dependency] = []
    diagnostics: list[Diagnostic] = []
    for path, language, size, modified_ns in entries:
        relative = path.relative_to(root).as_posix()
        files.append(IndexedFile(relative, language, size, modified_ns))
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            diagnostics.append(Diagnostic(relative, 1, 0, "warning", f"Unable to read source file: {exc}", "indexer"))
            continue
        if language == "python":
            parsed = _parse_python(relative, text)
        elif language in {"typescript", "javascript"}:
            parsed = _parse_typescript(relative, text)
        else:
            parsed = _parse_rust(relative, text)
        parsed_symbols, parsed_references, parsed_dependencies, parsed_diagnostics = parsed
        symbols.extend(parsed_symbols)
        references.extend(parsed_references)
        dependencies.extend(parsed_dependencies)
        diagnostics.extend(parsed_diagnostics)
    index = WorkspaceIndex(root_key, fingerprint, tuple(files), tuple(symbols), tuple(references), tuple(dependencies), tuple(diagnostics), truncated)
    with _CACHE_LOCK:
        _CACHE[root_key] = index
    return index, False


def invalidate_workspace_index(workspace: str | Path) -> None:
    root_key = str(Path(workspace).expanduser().resolve())
    with _CACHE_LOCK:
        _CACHE.pop(root_key, None)


def _git_changes(root: Path) -> list[dict[str, str]]:
    try:
        process = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if process.returncode != 0:
        return []
    return [{"status": line[:2], "path": line[3:]} for line in process.stdout.splitlines()[:500] if len(line) >= 4]


def get_repo_map(workspace: str | Path) -> dict[str, Any]:
    index, cache_hit = build_workspace_index(workspace)
    root = Path(index.root)
    languages = Counter(item.language for item in index.files)
    directories = Counter(Path(item.path).parts[0] if len(Path(item.path).parts) > 1 else "." for item in index.files)
    return {
        "fingerprint": index.fingerprint[:16],
        "cache_hit": cache_hit,
        "truncated": index.truncated,
        "totals": {
            "files": len(index.files),
            "symbols": len(index.symbols),
            "references": len(index.references),
            "dependencies": len(index.dependencies),
            "diagnostics": len(index.diagnostics),
        },
        "languages": dict(sorted(languages.items())),
        "top_directories": [{"path": path, "files": count} for path, count in directories.most_common(30)],
        "git_changes": _git_changes(root),
    }


def find_symbol(workspace: str | Path, query: str, *, exact: bool = False, kind: str | None = None, max_results: int = 50) -> dict[str, Any]:
    index, cache_hit = build_workspace_index(workspace)
    needle = query.casefold().strip()
    matches = [
        symbol for symbol in index.symbols
        if (not kind or symbol.kind == kind)
        and ((symbol.name.casefold() == needle) if exact else (needle in symbol.name.casefold()))
    ]
    matches.sort(key=lambda item: (item.name.casefold() != needle, item.path, item.line))
    return {"matches": [asdict(item) for item in matches[:max_results]], "total": len(matches), "truncated": len(matches) > max_results, "cache_hit": cache_hit}


def find_definition(workspace: str | Path, symbol: str, *, max_results: int = 20) -> dict[str, Any]:
    return find_symbol(workspace, symbol, exact=True, max_results=max_results)


def find_references(workspace: str | Path, symbol: str, *, max_results: int = 100) -> dict[str, Any]:
    index, cache_hit = build_workspace_index(workspace)
    needle = symbol.casefold().strip()
    matches = [item for item in index.references if item.name.casefold() == needle or item.name.casefold().endswith(f".{needle}")]
    matches.sort(key=lambda item: (item.path, item.line, item.kind))
    return {"matches": [asdict(item) for item in matches[:max_results]], "total": len(matches), "truncated": len(matches) > max_results, "cache_hit": cache_hit}


def list_module_dependencies(workspace: str | Path, path: str | None = None, *, max_results: int = 200) -> dict[str, Any]:
    index, cache_hit = build_workspace_index(workspace)
    normalized = Path(path).as_posix().casefold() if path else None
    matches = [item for item in index.dependencies if not normalized or item.source.casefold() == normalized]
    matches.sort(key=lambda item: (item.source, item.line, item.target))
    return {"dependencies": [asdict(item) for item in matches[:max_results]], "total": len(matches), "truncated": len(matches) > max_results, "cache_hit": cache_hit}


def find_related_tests(workspace: str | Path, path: str | None = None, symbol: str | None = None, *, max_results: int = 50) -> dict[str, Any]:
    index, cache_hit = build_workspace_index(workspace)
    terms = {term.casefold() for term in (symbol, Path(path).stem if path else None) if term}
    candidates: list[tuple[int, str, list[str]]] = []
    for item in index.files:
        lowered = item.path.casefold()
        if not ("test" in Path(item.path).name.casefold() or any(part.casefold() in {"test", "tests", "spec", "specs"} for part in Path(item.path).parts)):
            continue
        reasons = [term for term in terms if term in lowered]
        dependency_targets = [dep.target.casefold() for dep in index.dependencies if dep.source == item.path]
        reasons.extend(term for term in terms if any(term in target for target in dependency_targets))
        reference_names = [ref.name.casefold() for ref in index.references if ref.path == item.path]
        reasons.extend(term for term in terms if any(name == term or name.endswith(f".{term}") for name in reference_names))
        score = len(set(reasons))
        if score or not terms:
            candidates.append((score, item.path, sorted(set(reasons))))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return {"tests": [{"path": path_name, "score": score, "matched_terms": reasons} for score, path_name, reasons in candidates[:max_results]], "total": len(candidates), "truncated": len(candidates) > max_results, "cache_hit": cache_hit}


def get_call_chain(workspace: str | Path, symbol: str, *, depth: int = 3, max_results: int = 100) -> dict[str, Any]:
    index, cache_hit = build_workspace_index(workspace)
    callers: dict[str, set[str]] = defaultdict(set)
    for reference in index.references:
        if reference.kind == "call" and reference.container:
            callers[reference.name.split(".")[-1].casefold()].add(reference.container.split(".")[-1])
    queue: deque[tuple[str, int]] = deque([(symbol, 0)])
    seen = {symbol.casefold()}
    edges: list[dict[str, Any]] = []
    while queue and len(edges) < max_results:
        callee, level = queue.popleft()
        if level >= depth:
            continue
        for caller in sorted(callers.get(callee.casefold(), set())):
            edges.append({"caller": caller, "callee": callee, "depth": level + 1})
            if caller.casefold() not in seen:
                seen.add(caller.casefold())
                queue.append((caller, level + 1))
    return {"root": symbol, "edges": edges, "truncated": len(edges) >= max_results, "cache_hit": cache_hit}


def inspect_diagnostics(workspace: str | Path, path: str | None = None, *, max_results: int = 100) -> dict[str, Any]:
    index, cache_hit = build_workspace_index(workspace)
    normalized = Path(path).as_posix().casefold() if path else None
    matches = [item for item in index.diagnostics if not normalized or item.path.casefold() == normalized]
    return {"diagnostics": [asdict(item) for item in matches[:max_results]], "total": len(matches), "truncated": len(matches) > max_results, "cache_hit": cache_hit}
