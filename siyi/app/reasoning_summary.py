from __future__ import annotations

from typing import Final


_PHASE_SUMMARIES: Final[dict[str, str]] = {
    "conversation": "正在理解对话上下文并组织回答。",
    "analysis": "正在分析任务目标、约束和可用能力。",
    "planning": "正在整理执行步骤和验收条件。",
    "execution": "正在依据计划执行并检查工具结果。",
    "repair": "正在根据验证结果修正未满足的部分。",
    "verification": "正在核对结果与验收条件。",
    "finalization": "正在整理已验证的最终结果。",
}


def safe_reasoning_summary(phase: str) -> str:
    """Return a public progress summary without exposing provider chain-of-thought."""

    return _PHASE_SUMMARIES.get(phase, "正在处理当前任务并核对结果。")


def sanitize_reasoning_message(message: dict[str, object], phase: str) -> dict[str, object]:
    """Replace provider-private reasoning with a deterministic public summary."""

    if message.get("reasoning_content"):
        message["reasoning_content"] = safe_reasoning_summary(phase)
    return message
