from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CODE_SUFFIXES = {".c", ".cpp", ".cs", ".go", ".h", ".java", ".js", ".jsx", ".py", ".rs", ".svelte", ".ts", ".tsx", ".vue"}
UI_SUFFIXES = {".css", ".html", ".jsx", ".scss", ".svelte", ".tsx", ".vue"}
DOCUMENT_SUFFIXES = {".docx", ".md", ".pdf", ".txt"}
MULTIMODAL_SUFFIXES = {".aac", ".flac", ".gif", ".jpeg", ".jpg", ".m4a", ".mp3", ".mp4", ".png", ".wav", ".webp"}


def detect_verifier_domains(goal: str, paths: Iterable[str]) -> tuple[str, ...]:
    text = f"{goal} {' '.join(paths)}".lower()
    suffixes = {Path(path).suffix.lower() for path in paths}
    domains: list[str] = []

    def add(name: str, condition: bool) -> None:
        if condition and name not in domains:
            domains.append(name)

    add("code", bool(suffixes & CODE_SUFFIXES) or any(word in text for word in ("代码", "code", "bug", "重构", "refactor")))
    add("api", any(word in text for word in (" api", "api ", "接口", "endpoint", "fastapi", "route", "路由", "/api/")))
    add("ui", bool(suffixes & UI_SUFFIXES) or any(word in text for word in ("界面", "前端", " ui", "ui ", "pwa", "页面", "frontend")))
    add("database", any(word in text for word in ("数据库", "sqlite", "database", "migration", "schema", ".sql")))
    add("security", any(word in text for word in ("安全", "权限", "密钥", "token", "auth", "security", "sandbox", "ssrf", "secret")))
    add("document", bool(suffixes & DOCUMENT_SUFFIXES) or any(word in text for word in ("文档", "docx", "pdf", "markdown")))
    add("multimodal", bool(suffixes & MULTIMODAL_SUFFIXES) or any(word in text for word in ("多模态", "图片", "音频", "视频", "ocr", "vision")))
    return tuple(domains)


def classify_command(command: str) -> set[str]:
    text = command.lower()
    tags: set[str] = set()
    if any(word in text for word in ("pytest", "unittest", "mypy", "ruff", "python ", "pyright")):
        tags.update(("code", "python"))
    if any(word in text for word in ("npm ", "pnpm ", "yarn ", "vitest", "jest", "eslint", "tsc", "playwright", "vite")):
        tags.update(("code", "javascript", "ui"))
    if any(word in text for word in ("cargo ", "rustc")):
        tags.update(("code", "rust"))
    if any(word in text for word in ("go test", "go vet")):
        tags.update(("code", "go"))
    if any(word in text for word in ("curl ", "httpie", "test_api", "api_test", "endpoint", "fastapi")):
        tags.add("api")
    if any(word in text for word in ("alembic", "sqlite", "migration", "migrate", "test_database", "test_db", "schema")):
        tags.add("database")
    if any(word in text for word in ("bandit", "pip-audit", "npm audit", "semgrep", "test_security", "test_auth", "ssrf", "secret")):
        tags.add("security")
    if any(word in text for word in ("render_docx", "render_pdf", "pandoc", "document", "markdownlint")):
        tags.add("document")
    if any(word in text for word in ("pytest", "unittest", "vitest", "jest", "cargo test", "go test")):
        tags.add("test")
    verification_patterns = (
        r"\b(?:pytest|unittest|mypy|ruff|pyright|vitest|jest|eslint|tsc|playwright|bandit|pip-audit|semgrep|markdownlint)\b",
        r"\bpython(?:\.exe)?\b.*(?:\btest\b|check\.py|test_[\w.-]+\.py)",
        r"\b(?:npm|pnpm|yarn)\b.*\b(?:test|build|lint|typecheck|check)\b",
        r"\bcargo\b.*\b(?:test|check)\b",
        r"\bgo\b.*\b(?:test|vet)\b",
        r"\b(?:curl|httpie)\b",
        r"\b(?:render_docx|render_pdf|pandoc)\b",
        r"\b(?:make|cmake|dotnet|mvn|gradle)\b.*\b(?:test|build|check)\b",
    )
    if any(re.search(pattern, text) for pattern in verification_patterns):
        tags.add("verification")
    return tags


@dataclass(frozen=True)
class DomainVerifier:
    domain: str
    verifier_id: str

    def verify(self, requirement_id: str, description: str, evidence: dict[str, Any]) -> dict[str, Any]:
        commands = [item for item in evidence["verification_commands"] if item["status"] == "passed" and self._command_matches(item)]
        status = "passed" if commands else "not_run"
        reason = f"存在与 {self.verifier_id} 匹配的成功验证证据" if commands else f"没有与 {self.verifier_id} 匹配的成功验证证据"
        return self._result(requirement_id, description, status, commands, reason)

    def _command_matches(self, command: dict[str, Any]) -> bool:
        tags = set(command.get("tags") or ())
        if "verification" not in tags:
            return False
        if self.domain == "code":
            project_types = set(command.get("project_types") or ())
            expected_tags = {"python" if item == "python" else "javascript" if item == "node" else item for item in project_types}
            return "code" in tags and (not expected_tags or "generic" in project_types or bool(tags & expected_tags))
        if self.domain in {"api", "database", "security"}:
            return self.domain in tags or ("test" in tags and self._targeted_test(command))
        if self.domain == "ui":
            return "ui" in tags
        return self.domain in tags

    def _targeted_test(self, command: dict[str, Any]) -> bool:
        text = str(command.get("command") or "").lower()
        markers = {
            "api": ("api", "route", "endpoint"),
            "database": ("database", "db", "sqlite", "migration", "schema"),
            "security": ("security", "auth", "permission", "sandbox", "ssrf", "secret"),
        }[self.domain]
        # A full project test command is relevant; a scoped test must name the domain.
        scoped = any(part in text for part in ("test_", "tests/", "tests\\"))
        return not scoped or any(marker in text for marker in markers)

    def _result(self, requirement_id: str, description: str, status: str, evidence: Any, reason: str) -> dict[str, Any]:
        return {
            "criterion_id": requirement_id,
            "requirement_id": requirement_id,
            "description": description,
            "kind": "domain_verification",
            "verifier": self.verifier_id,
            "required": True,
            "status": status,
            "evidence": evidence,
            "reason": reason,
        }


class CodeVerifier(DomainVerifier):
    def __init__(self) -> None:
        super().__init__("code", "CodeVerifier")


class ApiVerifier(DomainVerifier):
    def __init__(self) -> None:
        super().__init__("api", "ApiVerifier")


class UiVerifier(DomainVerifier):
    def __init__(self) -> None:
        super().__init__("ui", "UiVerifier")


class DatabaseVerifier(DomainVerifier):
    def __init__(self) -> None:
        super().__init__("database", "DatabaseVerifier")


class SecurityVerifier(DomainVerifier):
    def __init__(self) -> None:
        super().__init__("security", "SecurityVerifier")


class DocumentVerifier(DomainVerifier):
    def __init__(self) -> None:
        super().__init__("document", "DocumentVerifier")

    def verify(self, requirement_id: str, description: str, evidence: dict[str, Any]) -> dict[str, Any]:
        expected_paths = {
            str(item.get("parameters", {}).get("path") or "")
            for item in evidence.get("acceptance_criteria") or ()
            if item.get("kind") == "path_state" and item.get("parameters", {}).get("exists") is True
        }
        documents = [
            item
            for item in evidence["final_file_state"]
            if Path(str(item.get("path") or "")).suffix.lower() in DOCUMENT_SUFFIXES
            and (not expected_paths or str(item.get("path") or "") in expected_paths)
        ]
        valid = [item for item in documents if item.get("exists") and item.get("type") == "file" and int(item.get("size") or 0) > 0]
        status = "passed" if documents and len(valid) == len(documents) else "failed"
        reason = "文档产物存在且非空" if status == "passed" else "文档产物缺失或为空"
        return self._result(requirement_id, description, status, documents, reason)


class MultimodalVerifier(DomainVerifier):
    def __init__(self) -> None:
        super().__init__("multimodal", "MultimodalVerifier")

    def verify(self, requirement_id: str, description: str, evidence: dict[str, Any]) -> dict[str, Any]:
        result = self._result(requirement_id, description, "unavailable", [], "多模态验收接口已预留，当前版本不据此宣称完成")
        result["required"] = False
        return result


VERIFIERS: dict[str, DomainVerifier] = {
    item.domain: item
    for item in (CodeVerifier(), ApiVerifier(), UiVerifier(), DatabaseVerifier(), SecurityVerifier(), DocumentVerifier(), MultimodalVerifier())
}


def verify_domain(domain: str, requirement_id: str, description: str, evidence: dict[str, Any]) -> dict[str, Any]:
    verifier = VERIFIERS.get(domain)
    if verifier is None:
        return {
            "criterion_id": requirement_id,
            "requirement_id": requirement_id,
            "description": description,
            "kind": "domain_verification",
            "verifier": "UnknownVerifier",
            "required": True,
            "status": "unavailable",
            "evidence": [],
            "reason": f"未注册任务验收器: {domain}",
        }
    return verifier.verify(requirement_id, description, evidence)
