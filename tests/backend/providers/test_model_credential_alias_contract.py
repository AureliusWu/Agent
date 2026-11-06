"""MODEL-CREDENTIAL-ALIAS-01: shared non-secret desktop/backend vectors."""

import json
from pathlib import Path

import pytest

from app.providers.configuration import ProviderConfiguration, validate_provider_configuration
from app.providers.configuration_guard import model_credential_target


_VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "model_credential_alias_v1.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("vector", _VECTORS, ids=[item["id"] for item in _VECTORS])
def test_model_credential_alias_contract(vector: dict) -> None:
    configuration = validate_provider_configuration(ProviderConfiguration(**vector["configuration"]))
    assert model_credential_target(configuration) == vector["alias"]
