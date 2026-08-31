from copy import deepcopy
import json

from app.providers.provider import provider_profile as legacy_profile
from app.providers.registry import provider_profile


def test_legacy_profile_is_compatible_with_same_v15_provider() -> None:
    from app.providers.profile_contract import profiles_match_for_resume

    assert profiles_match_for_resume(legacy_profile(), provider_profile())


def test_profile_contract_rejects_provider_model_endpoint_and_policy_drift() -> None:
    from app.providers.profile_contract import profiles_match_for_resume

    original = provider_profile()
    for field, changed in (("id", "other"), ("default_model", "different-model"), ("request_url", "https://other.example")):
        current = deepcopy(original)
        current[field] = changed
        assert not profiles_match_for_resume(original, current)
    changed = deepcopy(original)
    changed["descriptor"]["credential_policy"] = "forbidden"
    assert not profiles_match_for_resume(original, changed)


def test_profile_contract_ignores_observational_metadata_only() -> None:
    from app.providers.profile_contract import profiles_match_for_resume

    original = provider_profile()
    current = deepcopy(original)
    current["name"] = "Localized label"
    current["capabilities"] = {"source": "observed", "context_window": 32768}
    assert profiles_match_for_resume(original, current)
    current["descriptor"]["capabilities"]["native_tool_calls"] = False
    assert not profiles_match_for_resume(original, current)


def test_profile_contract_accepts_persisted_json_roundtrip() -> None:
    from app.providers.profile_contract import profiles_match_for_resume

    current = provider_profile()
    stored = json.loads(json.dumps(current))
    assert profiles_match_for_resume(stored, current)
    stored["descriptor"]["retry_policy"]["max_retries"] += 1
    assert not profiles_match_for_resume(stored, current)
