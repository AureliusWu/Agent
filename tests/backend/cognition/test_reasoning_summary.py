from __future__ import annotations

import json

from app.cognition.reasoning_summary import (
    PRIVATE_REASONING_KEY,
    safe_reasoning_summary,
    sanitize_reasoning_message,
    sanitize_reasoning_payload,
)


PRIVATE_SENTINEL = "PRIVATE_CHAIN_OF_THOUGHT_SENTINEL"


def test_reasoning_message_replaces_private_provider_text() -> None:
    message = {
        "role": "assistant",
        "content": "公开回答",
        PRIVATE_REASONING_KEY: PRIVATE_SENTINEL,
    }

    public = sanitize_reasoning_message(message, "analysis")

    assert public["reasoning_content"] == safe_reasoning_summary("analysis")
    assert PRIVATE_REASONING_KEY not in public
    assert PRIVATE_SENTINEL not in json.dumps(public, ensure_ascii=False)


def test_checkpoint_payload_recursively_removes_private_reasoning() -> None:
    state = {
        "executor_messages": [
            {
                "role": "assistant",
                "content": None,
                PRIVATE_REASONING_KEY: PRIVATE_SENTINEL,
                "tool_calls": [{"id": "call-1"}],
            }
        ],
        "pending": {"reasoning_content": PRIVATE_SENTINEL},
    }

    public = sanitize_reasoning_payload(state, "execution")
    serialized = json.dumps(public, ensure_ascii=False)

    assert PRIVATE_SENTINEL not in serialized
    assert PRIVATE_REASONING_KEY not in serialized
    assert serialized.count(safe_reasoning_summary("execution")) == 2

