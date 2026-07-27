from __future__ import annotations

from copy import deepcopy
from typing import Any, Final


_PHASE_SUMMARIES: Final[dict[str, str]] = {
    "conversation": "正在理解对话上下文并组织回答。",
    "analysis": "正在分析任务目标、约束和可用能力。",
    "planning": "正在整理执行步骤和验收条件。",
    "execution": "正在依据计划执行并检查工具结果。",
    "repair": "正在根据验证结果修正未满足的部分。",
    "verification": "正在核对结果与验收条件。",
    "finalization": "正在整理已验证的最终结果。",
}

PRIVATE_REASONING_KEY: Final[str] = "_provider_reasoning_content"


def safe_reasoning_summary(phase: str) -> str:
    """Return a public progress summary without exposing provider chain-of-thought."""

    return _PHASE_SUMMARIES.get(phase, "正在处理当前任务并核对结果。")


def has_private_reasoning(message: dict[str, object]) -> bool:
    return bool(message.get(PRIVATE_REASONING_KEY) or message.get("reasoning_content"))


def sanitize_reasoning_message(message: dict[str, object], phase: str) -> dict[str, object]:
    """Replace provider-private reasoning with a deterministic public summary."""

    private = has_private_reasoning(message)
    message.pop(PRIVATE_REASONING_KEY, None)
    message.pop("reasoning_content", None)
    if private:
        message["reasoning_content"] = safe_reasoning_summary(phase)
    return message


def sanitize_reasoning_payload(value: Any, phase: str) -> Any:
    """Return a deep public copy safe for checkpoints, logs, and API results."""

    if isinstance(value, dict):
        cleaned = {
            str(key): sanitize_reasoning_payload(item, phase)
            for key, item in value.items()
            if key not in {PRIVATE_REASONING_KEY, "reasoning_content"}
        }
        if has_private_reasoning(value):
            cleaned["reasoning_content"] = safe_reasoning_summary(phase)
        return cleaned
    if isinstance(value, list):
        return [sanitize_reasoning_payload(item, phase) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_reasoning_payload(item, phase) for item in value)
    return deepcopy(value)
