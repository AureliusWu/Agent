from __future__ import annotations

import asyncio
import os

import pytest

from app.providers.deepseek import DeepSeekProvider
from app.providers.registry import assert_paid_api_allowed


pytestmark = [
    pytest.mark.paid_model,
    pytest.mark.skipif(
        os.getenv("SIYI_TEST_PROVIDER") != "deepseek"
        or os.getenv("SIYI_ALLOW_PAID_API") != "true",
        reason="requires explicit paid DeepSeek acceptance permission",
    ),
]


def test_deepseek_paid_acceptance_requires_explicit_permission() -> None:
    assert_paid_api_allowed()
    response = asyncio.run(
        DeepSeekProvider().chat(
            [
                {"role": "system", "content": "Reply exactly PAID_OK."},
                {"role": "user", "content": "Run the minimal paid acceptance."},
            ],
            max_tokens=16,
        )
    )
    assert "PAID_OK" in str(response.get("content") or "")
