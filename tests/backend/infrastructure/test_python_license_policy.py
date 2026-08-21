from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
POLICY_PATH = ROOT / "packaging" / "python-license-policy.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from python_license_policy import audit_policy  # noqa: E402


AUDITED_STT_DEPENDENCIES = {
    "filelock",
    "flatbuffers",
    "fsspec",
    "hf-xet",
    "numpy",
    "protobuf",
    "tqdm",
}
FROZEN_WINDOWS_RUNTIME_DEPENDENCIES = {
    "annotated-doc",
    "annotated-types",
    "fastapi",
    "httptools",
    "pydantic",
    "pydantic-core",
    "pydantic-settings",
    "python-dotenv",
    "python-multipart",
    "starlette",
    "typing-inspection",
    "uvicorn",
    "watchfiles",
    "websockets",
}


def _load_script(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _properties(component: dict[str, object]) -> dict[str, str]:
    return {
        str(item["name"]): str(item["value"])
        for item in component.get("properties", [])  # type: ignore[union-attr]
    }


def test_notice_policy_covers_locked_stt_closure_except_explicit_pyav() -> None:
    audit = audit_policy()
    assert audit["passed"] is True, audit["errors"]
    assert AUDITED_STT_DEPENDENCIES <= set(audit["notice_covered"])
    assert FROZEN_WINDOWS_RUNTIME_DEPENDENCIES <= set(audit["notice_covered"])
    assert audit["excluded"] == ["av"]

    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    assert "av" not in policy["packages"]
    assert policy["excluded_packages"]["av"]["version"] == "18.0.0"
    assert "--exclude-module av" in policy["excluded_packages"]["av"]["reason"]


def test_notice_generator_emits_audited_dependencies_but_not_pyav() -> None:
    notices = _load_script("third_party_notices", "generate-third-party-notices.py")
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    output = notices.generate(POLICY_PATH)

    for name in AUDITED_STT_DEPENDENCIES:
        assert f"{name} {policy['packages'][name]}" in output
    assert "\nav 18.0.0\n" not in output
    assert "tokenizers 0.23.1\nDeclared license: Apache-2.0" in output


def test_sbom_marks_pyav_excluded_and_notice_coverage() -> None:
    sbom = _load_script("agent_sbom", "generate-sbom.py")
    components = {item["name"]: item for item in sbom.python_components()}

    pyav = components["av"]
    assert pyav["scope"] == "excluded"
    assert _properties(pyav)["agent:frozenArtifactDisposition"] == "explicitly-excluded"
    for name in AUDITED_STT_DEPENDENCIES:
        assert _properties(components[name])["agent:thirdPartyNotice"] == "covered"
    for name in FROZEN_WINDOWS_RUNTIME_DEPENDENCIES:
        properties = _properties(components[name])
        assert properties["agent:pythonScope"] == "runtime"
        assert properties["agent:thirdPartyNotice"] == "covered"
    assert _properties(components["pyinstaller"])["agent:pythonScope"] == "build"
    assert "agent:thirdPartyNotice" not in _properties(components["pyinstaller"])


def test_packaging_and_voice_docs_state_the_release_boundaries() -> None:
    packaging = (ROOT / "packaging" / "README.md").read_text(encoding="utf-8")
    local_voice = (ROOT / "docs" / "14.0.0" / "LOCAL_VOICE_INPUT.md").read_text(
        encoding="utf-8"
    )
    architecture = (ROOT / "docs" / "CURRENT_ARCHITECTURE.md").read_text(
        encoding="utf-8"
    )
    release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "complete Windows frozen-sidecar runtime closure" in packaging
    assert "scope=excluded" in packaging
    assert "不会发送给 Ollama 或任何模型 Provider" in local_voice
    assert "Ollama 收到的仅是这条已经发送的转写文本" in local_voice
    assert "STT 不额外保存转写原文副本" in architecture
    assert "按现有会话规则持久化" in architecture
    assert "dist/release/THIRD_PARTY_NOTICES.txt" in release_workflow
    assert "Resolve-Path \"dist/release/THIRD_PARTY_NOTICES.txt\"" in release_workflow
