from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from app.database import connect, now_iso
from app.config import settings


SecurityDomain = Literal["normal", "developer", "administrator"]
SECURITY_DOMAINS = {"normal", "developer", "administrator"}
PROTECTED_SKILLS = {"skill-creator", "find-skills", "mcp-builder"}
PACKAGE_MANAGER_COMMANDS = {"pip", "pip3", "npm", "pnpm", "yarn", "bun"}
COMMAND_ALLOWLIST = {
    "python", "python3", "py", "pytest",
    "git", "cargo", "rustc", "rustfmt",
    "node", "npm", "npx", "pnpm", "yarn", "bun",
    "dotnet", "go", "java", "javac", "gradle", "gradlew", "mvn",
    "cmake", "ctest", "ninja", "make", "msbuild", "cl",
}
_INSTALL_ARGUMENTS = {"install", "i", "add"}
_DANGEROUS_PATTERNS = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add)\b", re.IGNORECASE),
    re.compile(r"\b(?:python(?:\.exe)?\s+-m\s+)?pip(?:3)?\s+install\b", re.IGNORECASE),
    re.compile(r"\b(?:invoke-expression|iex|downloadstring|frombase64string)\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b.+\|\s*(?:sh|bash|powershell|pwsh)\b", re.IGNORECASE),
)
_SECRET_REFERENCE = re.compile(r"^secret://([a-z][a-z0-9_.-]{2,79})$")


def get_security_domain() -> SecurityDomain:
    with connect() as db:
        row = db.execute(
            "SELECT value FROM security_settings WHERE key='runtime_domain'"
        ).fetchone()
    value = str(row["value"]) if row else "normal"
    return value if value in SECURITY_DOMAINS else "normal"  # type: ignore[return-value]


def set_security_domain(domain: str) -> SecurityDomain:
    if domain not in SECURITY_DOMAINS:
        raise ValueError("安全域必须为 normal/developer/administrator")
    with connect() as db:
        db.execute(
            "INSERT INTO security_settings(key,value,updated_at) VALUES('runtime_domain',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (domain, now_iso()),
        )
    return domain  # type: ignore[return-value]


def skill_allowed_in_domain(name: str, domain: str | None = None) -> bool:
    active = domain or get_security_domain()
    return name.casefold() not in PROTECTED_SKILLS or active in {
        "developer",
        "administrator",
    }


def command_policy_error(command: str, args: list[str]) -> str | None:
    executable = Path(command).name.casefold()
    normalized = executable
    for suffix in (".exe", ".cmd", ".bat"):
        normalized = normalized.removesuffix(suffix)
    lowered_args = [item.casefold() for item in args]
    if normalized not in COMMAND_ALLOWLIST:
        return f"命令不在允许列表中：{normalized}"
    if normalized in PACKAGE_MANAGER_COMMANDS and any(
        item in _INSTALL_ARGUMENTS for item in lowered_args[:3]
    ):
        return "运行时禁止自动 npm/pip 等依赖安装；依赖必须在锁定构建阶段完成"
    joined = " ".join([normalized, *lowered_args])
    if any(pattern.search(joined) for pattern in _DANGEROUS_PATTERNS):
        return "命令被危险命令策略禁止"
    return None


def scan_third_party_skill(content: str) -> list[str]:
    findings: list[str] = []
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(content):
            findings.append(f"blocked_pattern:{pattern.pattern}")
    if "\x00" in content:
        findings.append("binary_content")
    if len(content.encode("utf-8")) > 200_000:
        findings.append("oversized")
    return findings


def validate_secret_reference(value: str) -> str:
    match = _SECRET_REFERENCE.fullmatch(value.strip())
    if not match:
        raise ValueError("密钥只能通过 secret://name 不透明引用传递")
    return match.group(1)


def public_security_policy() -> dict[str, object]:
    return {
        "domain": get_security_domain(),
        "domains": sorted(SECURITY_DOMAINS),
        "protected_skills": sorted(PROTECTED_SKILLS),
        "runtime_package_install": False,
        "command_allowlist": sorted(COMMAND_ALLOWLIST),
        "secret_transport": "opaque_reference",
        "secret_reference_scheme": "secret://",
        "critical_persistence": False,
        "network_allowed_domains": list(settings.network_allowed_domain_list),
        "network_blocked_domains": list(settings.network_blocked_domain_list),
    }
