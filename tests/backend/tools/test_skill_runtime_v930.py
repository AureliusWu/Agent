from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import connect, now_iso
from app.main import app
from app.tools.skills import (
    BUILTIN_SKILLS,
    SkillManifestError,
    discover_skills,
    install_skill,
    parse_skill_manifest,
    skill_context,
    uninstall_skill,
)


def manifest_text(
    name: str,
    *,
    version: str = "1.0.0",
    description: str = "Deterministic test skill",
    tools: tuple[str, ...] = ("read_file",),
    dependencies: tuple[str, ...] = (),
    permissions: tuple[str, ...] = ("files.read",),
    triggers: tuple[str, ...] = ("run deterministic skill",),
    negative: tuple[str, ...] = ("do not run deterministic skill",),
    body: str = "TEST_SKILL_BODY",
) -> str:
    def values(key: str, entries: tuple[str, ...]) -> str:
        return f"{key}:\n" + "".join(f"  - {entry}\n" for entry in entries)

    return (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: {description}\n"
        "type: workflow\n"
        "entrypoint: SKILL.md\n"
        + values("requires_tools", tools)
        + values("requires_skills", dependencies)
        + values("permissions", permissions)
        + values("platforms", ("windows",))
        + "risk: low\n"
        + "license: Test-Only\n"
        + values("trigger_examples", triggers)
        + values("negative_trigger_examples", negative)
        + "---\n"
        + body
    )


def _task(workspace: Path, task_id: str = "skill-runtime-task") -> str:
    stamp = now_iso()
    with connect() as db:
        conversation = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("skill runtime", str(workspace), "ask", stamp, stamp),
        ).lastrowid
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation, "running", "skill", stamp, stamp),
        )
    return task_id


def test_all_builtin_skills_have_complete_valid_manifests_and_ui_modes() -> None:
    assert set(BUILTIN_SKILLS) == {
        "release-checklist",
        "data-reliability-audit",
        "performance-regression-check",
        "deploy-smoke-test",
        "daily-report",
        "ui-design",
    }
    for item in BUILTIN_SKILLS.values():
        public = item["manifest"].public()
        assert all(
            key in public
            for key in (
                "name",
                "version",
                "description",
                "type",
                "entrypoint",
                "requires_tools",
                "requires_skills",
                "permissions",
                "platforms",
                "risk",
                "license",
                "trigger_examples",
                "negative_trigger_examples",
            )
        )
    ui_body = BUILTIN_SKILLS["ui-design"]["content"]
    assert {"existing-product", "greenfield-product", "strict-design-system"} <= {
        mode for mode in ("existing-product", "greenfield-product", "strict-design-system") if mode in ui_body
    }


def test_discovery_reads_only_manifest_until_skill_is_triggered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_skill(str(tmp_path), "lazy", manifest_text("lazy", body="LAZY_FULL_BODY"))
    reads = 0
    original = __import__("app.tools.skills", fromlist=["_read_skill"])._read_skill

    def tracked(path):
        nonlocal reads
        reads += 1
        return original(path)

    monkeypatch.setattr("app.tools.skills._read_skill", tracked)
    discovered = discover_skills(str(tmp_path))
    assert next(item for item in discovered if item["name"] == "lazy")["description"]
    assert reads == 0
    assert "LAZY_FULL_BODY" in skill_context(str(tmp_path), "run deterministic skill")
    assert reads == 1


def test_positive_negative_trigger_and_token_log(tmp_path: Path) -> None:
    install_skill(
        str(tmp_path),
        "triggered",
        manifest_text(
            "triggered",
            triggers=("generate exact report",),
            negative=("do not generate exact report",),
        ),
    )
    assert "TEST_SKILL_BODY" not in skill_context(
        str(tmp_path), "do not generate exact report"
    )
    task_id = _task(tmp_path)
    assert "TEST_SKILL_BODY" in skill_context(
        str(tmp_path), "generate exact report", task_id
    )
    with connect() as db:
        run = db.execute(
            "SELECT version,content_chars,content_tokens,trigger_reason,status FROM skill_runs WHERE task_id=?",
            (task_id,),
        ).fetchone()
    assert run["version"] == "1.0.0"
    assert run["content_chars"] > 0 and run["content_tokens"] > 0
    assert "generate exact report" in run["trigger_reason"]
    assert run["status"] == "loaded"


def test_dependencies_are_loaded_in_order_and_missing_dependency_fails_closed(
    tmp_path: Path,
) -> None:
    install_skill(
        str(tmp_path),
        "base-skill",
        manifest_text(
            "base-skill",
            triggers=("base only",),
            body="BASE_DEPENDENCY_BODY",
        ),
    )
    install_skill(
        str(tmp_path),
        "parent-skill",
        manifest_text(
            "parent-skill",
            dependencies=("base-skill",),
            triggers=("run parent workflow",),
            body="PARENT_BODY",
        ),
    )
    context = skill_context(str(tmp_path), "run parent workflow")
    assert context.index("BASE_DEPENDENCY_BODY") < context.index("PARENT_BODY")

    install_skill(
        str(tmp_path),
        "missing-parent",
        manifest_text(
            "missing-parent",
            dependencies=("not-installed",),
            triggers=("run missing workflow",),
        ),
    )
    assert skill_context(str(tmp_path), "run missing workflow") == ""


def test_dependency_cycle_and_same_name_conflict_are_detected(tmp_path: Path) -> None:
    install_skill(
        str(tmp_path),
        "cycle-a",
        manifest_text(
            "cycle-a",
            dependencies=("cycle-b",),
            triggers=("run cycle",),
        ),
    )
    install_skill(
        str(tmp_path),
        "cycle-b",
        manifest_text(
            "cycle-b",
            dependencies=("cycle-a",),
            triggers=("cycle helper",),
        ),
    )
    assert skill_context(str(tmp_path), "run cycle") == ""

    install_skill(
        str(tmp_path),
        "daily-report",
        manifest_text("daily-report", version="2.0.0"),
    )
    conflicts = [
        item
        for item in discover_skills(str(tmp_path))
        if item["name"] == "daily-report"
    ]
    assert len(conflicts) == 2
    assert {item["status"] for item in conflicts} == {"conflict"}


def test_unknown_tools_and_missing_permissions_are_rejected() -> None:
    with pytest.raises(SkillManifestError, match="未知工具"):
        parse_skill_manifest(manifest_text("bad-tool", tools=("raw_socket",)))
    with pytest.raises(SkillManifestError, match="缺少工具所需权限"):
        parse_skill_manifest(
            manifest_text(
                "bad-permission",
                tools=("run_command",),
                permissions=("files.read",),
            )
        )


def test_enable_disable_and_recoverable_workspace_uninstall(tmp_path: Path) -> None:
    installed = install_skill(str(tmp_path), "lifecycle", manifest_text("lifecycle"))
    with TestClient(app) as client:
        disabled = client.patch(
            "/api/skills/enabled",
            params={"workspace": str(tmp_path), "path": installed["path"]},
            json={"enabled": False},
        )
        listed = client.get("/api/skills", params={"workspace": str(tmp_path)})
        removed = client.delete(
            "/api/skills",
            params={"workspace": str(tmp_path), "path": installed["path"]},
        )
    assert disabled.status_code == 200
    assert next(item for item in listed.json() if item["name"] == "lifecycle")["enabled"] is False
    assert removed.status_code == 200
    archive = tmp_path / removed.json()["archived_to"]
    assert (archive / "SKILL.md").is_file()
    assert not (tmp_path / installed["path"]).exists()


def test_invalid_skill_failure_does_not_write_conversation_messages(tmp_path: Path) -> None:
    target = tmp_path / ".agent" / "skills" / "invalid" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: invalid\n---\nBAD_BODY", encoding="utf-8")
    task_id = _task(tmp_path, "invalid-skill-task")
    with connect() as db:
        before = db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=(SELECT conversation_id FROM agent_tasks WHERE id=?)",
            (task_id,),
        ).fetchone()[0]
    assert skill_context(str(tmp_path), "invalid", task_id) == ""
    with connect() as db:
        after = db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=(SELECT conversation_id FROM agent_tasks WHERE id=?)",
            (task_id,),
        ).fetchone()[0]
    assert after == before
