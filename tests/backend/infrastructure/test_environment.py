from pathlib import Path

from app.environment import detect_build_environment
from app.memory.service import invalidate_project_signature


def test_build_environment_detection_is_cached_and_invalidated(tmp_path: Path) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_text('{"name":"one"}', encoding="utf-8")

    first = detect_build_environment(str(tmp_path))
    second = detect_build_environment(str(tmp_path))
    manifest.write_text('{"name":"two","scripts":{}}', encoding="utf-8")
    invalidate_project_signature(str(tmp_path))
    third = detect_build_environment(str(tmp_path))

    assert first["stacks"] == ["node"]
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert third["cache_hit"] is False
    assert third["project_signature"] != first["project_signature"]
