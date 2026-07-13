from __future__ import annotations

from pathlib import Path
import re

from .config import settings
from .sandbox import safe_path, workspace_root
from .database import connect, now_iso, rows


_SKILL_CONTENT_CACHE: dict[str, tuple[int, int, str]] = {}


def _read_skill(path: Path) -> str:
    stat = path.stat()
    key = str(path)
    cached = _SKILL_CONTENT_CACHE.get(key)
    signature = (stat.st_mtime_ns, stat.st_size)
    if cached and cached[:2] == signature:
        return cached[2]
    text = path.read_text(encoding="utf-8", errors="replace")
    _SKILL_CONTENT_CACHE[key] = (signature[0], signature[1], text)
    return text


def _terms(value: str) -> set[str]:
    lowered = value.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {word for word in words if word}


def discover_skills(workspace: str, include_content: bool = False) -> list[dict[str, str]]:
    root = workspace_root(workspace)
    found: list[dict[str, str]] = []
    for skill_root in (root / ".agent" / "skills", root / ".codex" / "skills"):
        if not skill_root.exists():
            continue
        for manifest in skill_root.glob("*/SKILL.md"):
            text = _read_skill(manifest)
            name, description = manifest.parent.name, ""
            if text.startswith("---"):
                for line in text.split("---", 2)[1].splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip()
            relative = str(manifest.relative_to(root))
            setting = rows("SELECT enabled FROM skill_settings WHERE path=?", (f"{root}|{relative}",))
            item = {"name": name, "description": description, "path": relative, "enabled": bool(setting[0]["enabled"]) if setting else True}
            if include_content:
                item["content"] = text[:20_000]
            found.append(item)
    return found


def skill_context(workspace: str, user_prompt: str, task_id: str | None = None) -> str:
    root = workspace_root(workspace)
    skills = [item for item in discover_skills(workspace, include_content=False) if item["enabled"]]
    if not skills:
        return ""
    lowered = user_prompt.lower()
    prompt_terms = _terms(lowered)
    scored: list[tuple[int, dict[str, str]]] = []
    for item in skills:
        label = f"{item['name']} {item['description']}".lower()
        terms = _terms(label)
        score = len(prompt_terms & terms) + (5 if item["name"].lower() in lowered else 0)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1]["name"]), reverse=True)
    selected: list[dict[str, str]] = []
    total_chars = 0
    for _, item in scored[: settings.max_skill_count]:
        remaining = settings.max_skill_context_chars - total_chars
        if remaining <= 0:
            break
        content = _read_skill(root / item["path"])[: min(12_000, remaining)]
        if selected and total_chars + len(content) > settings.max_skill_context_chars:
            continue
        selected.append({**item, "content": content})
        total_chars += len(content)
    instructions = "\n\n".join(f"### Skill: {item['name']}\n{item['content']}" for item in selected)
    if task_id and selected:
        with connect() as db:
            db.executemany(
                "INSERT INTO skill_runs(task_id, name, path, content_chars, created_at) VALUES(?,?,?,?,?)",
                [(task_id, item["name"], item["path"], len(item["content"]), now_iso()) for item in selected],
            )
    return f"本轮按需加载的 Skill 指令（共 {len(selected)} 个）：\n{instructions}" if instructions else ""


def install_skill(workspace: str, name: str, content: str) -> dict[str, str]:
    root = workspace_root(workspace)
    target = safe_path(root, f".agent/skills/{name}/SKILL.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _SKILL_CONTENT_CACHE.pop(str(target), None)
    return {"name": name, "path": str(target.relative_to(root))}
