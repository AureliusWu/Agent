from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


MODEL_CREDENTIAL_ENV = {
    "AGENT_DEEPSEEK_API_KEY",
    "AGENT_TAVILY_API_KEY",
    "SIYI_EVO_MODEL_API_KEY",
}


def build_isolated_desktop_environment(
    root: Path,
    *,
    inherited: Mapping[str, str] | None = None,
    allow_real_model: bool = False,
) -> dict[str, str]:
    """Build a subprocess environment that cannot use real user data by default."""

    resolved = root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    for name in ("data", "logs", "artifacts", "screenshots", "temp"):
        (resolved / name).mkdir(exist_ok=True)
    environment = dict(inherited if inherited is not None else os.environ)
    if not allow_real_model:
        for name in MODEL_CREDENTIAL_ENV:
            environment.pop(name, None)
    environment.update(
        {
            "AGENT_DESKTOP_DATA_DIRECTORY": str(resolved),
            "AGENT_DEPLOYMENT_MODE": "desktop_local",
            "SIYI_SCENARIO_RUN_ROOT": str(resolved),
            "SIYI_SCENARIO_PROVIDER_MODE": "real" if allow_real_model else "mock",
        }
    )
    return environment
