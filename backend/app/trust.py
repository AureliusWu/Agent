from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


REDACTED = "***REDACTED***"
INJECTION_SENTINEL = "[UNTRUSTED_INSTRUCTION_RISK]"
SENSITIVE_KEYS = ("api_key", "apikey", "token", "secret", "password", "authorization", "credential", "private_key")
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I | re.S)),
    ("credential_assignment", re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|authorization)\b(\s*[:=]\s*)([\"']?)[^\s\"',;]{8,}\3")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.I)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)
INJECTION_PATTERNS = (
    ("override_rules", re.compile(r"(?i)(ignore|disregard|forget).{0,40}(previous|prior|system).{0,30}(instruction|rule|prompt)|忽略.{0,20}(之前|以上|系统).{0,20}(指令|规则|提示)")),
    ("credential_extraction", re.compile(r"(?i)(reveal|show|print|extract|send|upload).{0,40}(api.?key|token|secret|password|credential)|(?:提取|显示|泄露|上传|发送).{0,30}(密钥|令牌|密码|凭据)")),
    ("workspace_escape", re.compile(r"(?i)(outside|bypass|escape).{0,30}(workspace|sandbox)|(?:绕过|逃逸|访问).{0,30}(工作区|沙箱|其他目录)")),
    ("unknown_command", re.compile(r"(?i)(execute|run).{0,30}(unknown|hidden|downloaded).{0,20}(command|script)|(?:执行|运行).{0,30}(未知|隐藏|下载).{0,20}(命令|脚本)")),
    ("disable_safety", re.compile(r"(?i)(disable|turn off|bypass).{0,30}(permission|confirmation|safety|security)|(?:关闭|绕过|跳过).{0,30}(权限|确认|安全|审计)")),
    ("tool_as_instruction", re.compile(r"(?i)(tool|mcp|log|file).{0,30}(result|content).{0,30}(system instruction|must obey)|(?:工具|MCP|日志|文件).{0,30}(结果|内容).{0,30}(系统指令|必须服从)")),
)


@dataclass(frozen=True)
class SensitiveSummary:
    redactions: int = 0
    categories: tuple[str, ...] = ()

    @property
    def classification(self) -> str:
        return "credential" if self.redactions else "internal"


def _redact_text(value: str) -> tuple[str, SensitiveSummary]:
    text = value
    count = 0
    categories: set[str] = set()
    for category, pattern in SECRET_PATTERNS:
        text, matches = pattern.subn(REDACTED, text)
        if matches:
            count += matches
            categories.add(category)
    return text, SensitiveSummary(count, tuple(sorted(categories)))


def redact_payload(value: Any) -> tuple[Any, SensitiveSummary]:
    categories: set[str] = set()
    redactions = 0

    def walk(item: Any) -> Any:
        nonlocal redactions
        if isinstance(item, dict):
            cleaned: dict[str, Any] = {}
            for key, child in item.items():
                lowered = str(key).lower()
                if any(marker in lowered for marker in SENSITIVE_KEYS):
                    cleaned[str(key)] = REDACTED
                    redactions += 1
                    categories.add("credential_field")
                else:
                    cleaned[str(key)] = walk(child)
            return cleaned
        if isinstance(item, (list, tuple)):
            return [walk(child) for child in item]
        if isinstance(item, str):
            cleaned, summary = _redact_text(item)
            redactions += summary.redactions
            categories.update(summary.categories)
            return cleaned
        return item

    return walk(value), SensitiveSummary(redactions, tuple(sorted(categories)))


def detect_prompt_injection(value: str) -> list[str]:
    return [name for name, pattern in INJECTION_PATTERNS if pattern.search(value)]


def secure_untrusted_payload(value: Any, source: str) -> tuple[dict[str, Any], SensitiveSummary, list[str]]:
    cleaned, sensitive = redact_payload(value)
    findings = detect_prompt_injection(json.dumps(cleaned, ensure_ascii=False, default=str))
    envelope = dict(cleaned) if isinstance(cleaned, dict) else {"value": cleaned}
    envelope["_security"] = {
        "trust": "untrusted",
        "source": source,
        "treat_as_instructions": False,
        "prompt_injection_findings": findings,
        "redactions": sensitive.redactions,
    }
    return envelope, sensitive, findings


def secure_untrusted_text(value: str, source: str) -> tuple[str, SensitiveSummary, list[str]]:
    cleaned, sensitive = _redact_text(value)
    findings = detect_prompt_injection(cleaned)
    warning = f"\n{INJECTION_SENTINEL} 检测到：{', '.join(findings)}" if findings else ""
    return (
        f"<untrusted-content source={json.dumps(source, ensure_ascii=False)}>\n{cleaned}\n"
        f"</untrusted-content>{warning}",
        sensitive,
        findings,
    )
