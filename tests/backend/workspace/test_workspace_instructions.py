from pathlib import Path

from app.workspace.instructions import load_workspace_instructions


def test_instruction_hierarchy_and_override_without_readme(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (tmp_path / "README.md").write_text("not authoritative", encoding="utf-8")
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    (tmp_path / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    (tmp_path / "src" / "AGENTS.override.md").write_text("src override", encoding="utf-8")

    bundle = load_workspace_instructions(str(tmp_path), ("src/feature/file.py",))

    assert [item.path for item in bundle.sources] == [
        "AGENTS.md",
        "src/AGENTS.md",
        "src/AGENTS.override.md",
    ]
    assert bundle.sources[-1].override is True
    assert bundle.sources[-1].priority > bundle.sources[-2].priority
    assert "not authoritative" not in bundle.text


def test_instruction_loader_ignores_out_of_workspace_target(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    bundle = load_workspace_instructions(str(tmp_path), ("../outside/file.py",))
    assert [item.path for item in bundle.sources] == ["AGENTS.md"]


def test_readme_is_loaded_only_when_agents_explicitly_references_it(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Follow README.md for build rules", encoding="utf-8")
    (tmp_path / "README.md").write_text("npm run build", encoding="utf-8")
    bundle = load_workspace_instructions(str(tmp_path))
    assert [item.path for item in bundle.sources] == ["AGENTS.md", "README.md"]
    assert "npm run build" in bundle.text
