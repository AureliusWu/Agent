from __future__ import annotations

import re
from dataclasses import asdict, dataclass


_DRIFT_PATTERNS = (
    re.compile(r"(?:^|[。！？!?\n])\s*(?:我|本人)\s*(?:是|叫|作为)\s*(?:ChatGPT|Claude|DeepSeek|GPT(?:-?\d\w*)?)", re.IGNORECASE),
    re.compile(r"(?:^|[.!?\n])\s*I\s+am\s+(?:ChatGPT|Claude|DeepSeek|GPT(?:-?\d\w*)?)\b", re.IGNORECASE),
    re.compile(r"(?:^|[。！？!?\n])\s*(?:作为|身为)\s*(?:OpenAI|Anthropic|DeepSeek)\s*(?:的)?(?:模型|助手|AI)", re.IGNORECASE),
)


@dataclass(frozen=True)
class IdentityGuardResult:
    passed: bool
    reason: str = ""
    match: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_identity_claim(content: str) -> IdentityGuardResult:
    # Only inspect direct self-identity claims. Quoted code and provider discussions remain untouched.
    prose = re.sub(r"```.*?```", "", content or "", flags=re.DOTALL)
    for pattern in _DRIFT_PATTERNS:
        match = pattern.search(prose)
        if match:
            return IdentityGuardResult(False, "assistant identity drift", match.group(0).strip())
    return IdentityGuardResult(True)


def repair_instruction() -> str:
    return (
        "你的上一版答复出现身份漂移。保持事实内容和任务结果不变，重新给出答复；"
        "你的身份是夏目心，模型供应商或模型名称仅是运行基座。不要解释本次修复。"
    )


def enforce_identity(content: str) -> str:
    repaired = content
    for pattern in _DRIFT_PATTERNS:
        repaired = pattern.sub("我是夏目心", repaired)
    return repaired
