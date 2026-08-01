from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


FORBIDDEN_PARTS = {
    "data", "private", "user-data", "user-assets", "uploads", "artifacts",
    "backups", "logs", "cache", "temp", "crash", "state", "checkpoints",
    "tool-output", "model-output", ".agent", ".agent-backups", ".agent-runtime",
    "private-state", "secrets", "credentials",
}
ALLOWED_SOURCE_DIRECTORY_PARTS = {
    ("siyi", "app", "artifacts"): {"artifacts"},
    ("tests", "backend", "artifacts"): {"artifacts"},
}
APPROVED_LARGE_SOURCE_FILES = {
    "desktop/frontend/src/assets/characters/natsume-kokoro.png",
}
FORBIDDEN_SUFFIXES = {
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".key", ".pem",
    ".p12", ".pfx", ".token", ".dmp", ".dump", ".trace", ".log",
}
FORBIDDEN_NAME_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^kokoro_autobiography\.md$", r"^memory-export", r"^continuity-export",
        r"^conversation-export", r"^task-export", r"\.user\.json$", r"\.private\.json$",
    )
)
SECRET_PATTERNS = {
    "api_key": re.compile(rb"(?<![A-Za-z0-9])(?:sk|tvly)-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(rb"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}"),
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "google_api_key": re.compile(rb"AIza[0-9A-Za-z_-]{30,}"),
    "oauth_token": re.compile(rb"ya29\.[0-9A-Za-z_-]{20,}"),
    "jwt": re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}
PII_PATTERNS = {
    "cn_phone": re.compile(rb"(?<![A-Za-z0-9])1[3-9]\d{9}(?![A-Za-z0-9])"),
    "cn_id": re.compile(rb"(?<![A-Za-z0-9])\d{17}[0-9Xx](?![A-Za-z0-9])"),
    "windows_user_path": re.compile(rb"(?i)[A-Z]:[\\/]Users[\\/][^\\/\r\n]+"),
    "absolute_drive_path": re.compile(rb"(?i)(?<![A-Z0-9_])[A-Z]:[\\/][^\r\n\t<>|]+"),
}
EMAIL_PATTERN = re.compile(rb"(?i)(?<![\w.+-])[A-Z][A-Z0-9._%+-]*@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])")
SYNTHETIC_SECRET = b"sk-test_DO_NOT_USE_000000000000"
SYNTHETIC_PATHS = (
    rb"C:\Users\test", rb"C:\Users\private", rb"C:\Temp\agent-smoke-data",
    rb"C:\Program Files", rb"C:\Program Files (x86)",
    rb"C:/repo", rb"C:/workspace", rb"C:/Windows/win.ini", rb"D:/workspace-a", rb"D:/workspace-b",
)
TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".csv", ".html", ".ini", ".js", ".json",
    ".jsx", ".md", ".mjs", ".ps1", ".py", ".rs", ".sh", ".toml", ".ts",
    ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
FILESYSTEM_SKIP_DIRECTORIES = {
    ".git", ".venv", "node_modules", "target", "dist", "build", "binaries",
    "__pycache__", ".pytest_cache", "htmlcov",
}
FILESYSTEM_SKIP_NAMES = {".coverage"}
FILESYSTEM_SKIP_SUFFIXES = {".exe", ".dll", ".lib", ".pdb", ".rlib"}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    kind: str


def _git(*arguments: str, binary: bool = False) -> bytes | str:
    return subprocess.check_output(
        ["git", *arguments],
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
    )


def tracked_blobs(staged: bool = False) -> Iterator[tuple[str, bytes]]:
    if staged:
        output = str(_git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"))
    else:
        output = str(_git("ls-files", "-z"))
    for value in output.split("\0"):
        if not value:
            continue
        path = Path(value)
        if path.is_file():
            yield path.as_posix(), path.read_bytes()


def history_blobs(revision: str = "HEAD") -> Iterator[tuple[str, bytes]]:
    rows = str(_git("rev-list", "--objects", revision)).splitlines()
    paths: dict[str, str] = {}
    for row in rows:
        sha, *rest = row.split(" ", 1)
        paths.setdefault(sha, rest[0] if rest else "")
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    output, _ = process.communicate(("\n".join(paths) + "\n").encode())
    position = 0
    while position < len(output):
        end = output.find(b"\n", position)
        if end < 0:
            break
        header = output[position:end].decode("ascii", "replace").split()
        position = end + 1
        if len(header) < 3 or header[1] == "missing":
            continue
        sha, kind, size_text = header[:3]
        size = int(size_text)
        data = output[position : position + size]
        position += size + 1
        if kind == "blob":
            yield paths.get(sha, f"blob:{sha}"), data


def filesystem_blobs(root: Path) -> Iterator[tuple[str, bytes]]:
    if root.is_file():
        yield root.as_posix(), root.read_bytes()
        return
    for current, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if name not in FILESYSTEM_SKIP_DIRECTORIES]
        base = Path(current)
        for filename in filenames:
            path = base / filename
            if filename in FILESYSTEM_SKIP_NAMES or path.suffix.lower() in FILESYSTEM_SKIP_SUFFIXES:
                continue
            yield path.as_posix(), path.read_bytes()


def _forbidden_path(path: str) -> list[Finding]:
    normalized = path.replace("\\", "/")
    parts = [part.lower() for part in normalized.split("/")]
    name = parts[-1] if parts else ""
    findings: list[Finding] = []
    allowed_parts: set[str] = set()
    for prefix, names in ALLOWED_SOURCE_DIRECTORY_PARTS.items():
        for index in range(len(parts) - len(prefix) + 1):
            if tuple(parts[index : index + len(prefix)]) == prefix:
                allowed_parts.update(names)
    if any(part in FORBIDDEN_PARTS and part not in allowed_parts for part in parts[:-1]):
        findings.append(Finding(path, "forbidden_runtime_path"))
    if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        findings.append(Finding(path, "forbidden_file_type"))
    if any(pattern.search(name) for pattern in FORBIDDEN_NAME_PATTERNS):
        findings.append(Finding(path, "private_export_name"))
    return findings


def scan_blob(path: str, data: bytes, checks: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    if "forbidden" in checks:
        findings.extend(_forbidden_path(path))
        if data.startswith(b"SQLite format 3\x00"):
            findings.append(Finding(path, "sqlite_header"))
        normalized_path = path.replace("\\", "/").lower()
        if (
            len(data) > 1024 * 1024
            and Path(path).suffix.lower() not in {".svg", ".json", ".md"}
            and normalized_path not in APPROVED_LARGE_SOURCE_FILES
        ):
            findings.append(Finding(path, "unapproved_large_binary"))
    if "secrets" in checks:
        sanitized = data.replace(SYNTHETIC_SECRET, b"")
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(sanitized):
                findings.append(Finding(path, name))
    if "pii" in checks and Path(path).suffix.lower() in TEXT_SUFFIXES:
        sanitized = data
        for marker in SYNTHETIC_PATHS:
            sanitized = sanitized.replace(marker, b"")
            sanitized = sanitized.replace(marker.replace(b"\\", b"\\\\"), b"")
        sanitized = re.sub(rb"(?i)https?://[^\s\"'<>]+", b"", sanitized)
        for match in EMAIL_PATTERN.finditer(sanitized):
            value = match.group(0).lower()
            if not value.endswith((b"@example.invalid", b"@example.com")):
                findings.append(Finding(path, "email"))
                break
        for name, pattern in PII_PATTERNS.items():
            if pattern.search(sanitized):
                findings.append(Finding(path, name))
    return findings


def scan(entries: Iterable[tuple[str, bytes]], checks: set[str]) -> list[Finding]:
    findings: set[Finding] = set()
    for path, data in entries:
        findings.update(scan_blob(path, data, checks))
    return sorted(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description="Block private runtime data from Git and release inputs.")
    parser.add_argument("--tracked", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument(
        "--history-all-refs",
        action="store_true",
        help="Audit every local ref instead of only the current branch ancestry.",
    )
    parser.add_argument("--scan-path", type=Path)
    parser.add_argument("--checks", choices=("all", "forbidden", "secrets", "pii"), default="all")
    arguments = parser.parse_args()
    checks = {"forbidden", "secrets", "pii"} if arguments.checks == "all" else {arguments.checks}
    sources: list[tuple[str, Iterable[tuple[str, bytes]]]] = []
    if arguments.tracked or not any(
        (arguments.staged, arguments.history, arguments.history_all_refs, arguments.scan_path)
    ):
        sources.append(("tracked", tracked_blobs()))
    if arguments.staged:
        sources.append(("staged", tracked_blobs(staged=True)))
    if arguments.history:
        sources.append(("history", history_blobs()))
    if arguments.history_all_refs:
        sources.append(("history-all-refs", history_blobs("--all")))
    if arguments.scan_path:
        sources.append(("filesystem", filesystem_blobs(arguments.scan_path)))
    findings: set[Finding] = set()
    for _, entries in sources:
        findings.update(scan(entries, checks))
    if findings:
        print(f"Privacy scan blocked {len(findings)} path-level finding(s).")
        for finding in sorted(findings):
            print(f"{finding.kind}\t{finding.path}")
        return 1
    print("Privacy scan passed: no forbidden private data patterns were found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
