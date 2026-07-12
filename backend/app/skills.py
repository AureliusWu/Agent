from __future__ import annotations

from pathlib import Path

from .sandbox import safe_path, workspace_root


def discover_skills(workspace: str, include_content: bool = False) -> list[dict[str, str]]:
    root = workspace_root(workspace)
    found: list[dict[str, str]] = []
    for skill_root in (root / ".agent" / "skills", root / ".codex" / "skills"):
        if not skill_root.exists():
            continue
        for manifest in skill_root.glob("*/SKILL.md"):
            text = manifest.read_text(encoding="utf-8", errors="replace")
            name, description = manifest.parent.name, ""
            if text.startswith("---"):
                for line in text.split("---", 2)[1].splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip()
            item = {"name": name, "description": description, "path": str(manifest.relative_to(root))}
            if include_content:
                item["content"] = text[:20_000]
            found.append(item)
    return found


def skill_context(workspace: str, user_prompt: str) -> str:
    skills = discover_skills(workspace, include_content=True)
    if not skills:
        return ""
    lowered = user_prompt.lower()
    selected = [item for item in skills if item["name"].lower() in lowered or any(token and token in lowered for token in item["description"].lower().split()[:8])]
    if not selected and len(skills) <= 4:
        selected = skills
    catalog = "\n".join(f"- {item['name']}: {item['description'] or item['path']}" for item in skills)
    instructions = "\n\n".join(f"### Skill: {item['name']}\n{item['content']}" for item in selected[:3])
    return f"可用 Skill：\n{catalog}" + (f"\n\n本轮相关 Skill 指令：\n{instructions}" if instructions else "")


def install_skill(workspace: str, name: str, content: str) -> dict[str, str]:
    root = workspace_root(workspace)
    target = safe_path(root, f".agent/skills/{name}/SKILL.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"name": name, "path": str(target.relative_to(root))}
