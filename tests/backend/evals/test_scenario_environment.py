from __future__ import annotations

from pathlib import Path

from app.evals.scenario_environment import MODEL_CREDENTIAL_ENV, build_isolated_desktop_environment


def test_scenario_environment_isolates_appdata_and_scrubs_model_keys(tmp_path: Path) -> None:
    inherited = {name: "secret" for name in MODEL_CREDENTIAL_ENV}
    inherited["SAFE_VALUE"] = "kept"
    root = tmp_path / "desktop-data"
    environment = build_isolated_desktop_environment(root, inherited=inherited)
    assert environment["AGENT_DESKTOP_DATA_DIRECTORY"] == str(root.resolve())
    assert environment["AGENT_DEPLOYMENT_MODE"] == "desktop_local"
    assert environment["SIYI_SCENARIO_PROVIDER_MODE"] == "mock"
    assert environment["SAFE_VALUE"] == "kept"
    assert not MODEL_CREDENTIAL_ENV.intersection(environment)
    assert all((root / name).is_dir() for name in ("data", "logs", "artifacts", "screenshots", "temp"))


def test_real_model_mode_requires_explicit_opt_in(tmp_path: Path) -> None:
    inherited = {"SIYI_EVO_MODEL_API_KEY": "one-time-key"}
    environment = build_isolated_desktop_environment(
        tmp_path / "desktop-data",
        inherited=inherited,
        allow_real_model=True,
    )
    assert environment["SIYI_SCENARIO_PROVIDER_MODE"] == "real"
    assert environment["SIYI_EVO_MODEL_API_KEY"] == "one-time-key"
