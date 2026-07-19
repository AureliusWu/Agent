from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import settings
from .sandbox import safe_path, workspace_root
from .database import connect, now_iso, rows
from .data_flow import record_data_flow
from .trust import secure_untrusted_text


_SKILL_CONTENT_CACHE: dict[str, tuple[int, int, str]] = {}
_SKILL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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


def discover_skills(workspace: str, include_content: bool = False, *, _include_location: bool = False) -> list[dict[str, Any]]:
    root = workspace_root(workspace)
    found: list[dict[str, Any]] = []
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
            if not _SKILL_NAME.fullmatch(name):
                name = manifest.parent.name
            description = description[:500]
            relative = str(manifest.relative_to(root))
            setting = rows("SELECT enabled FROM skill_settings WHERE path=?", (f"{root}|{relative}",))
            item = {"name": name, "description": description, "path": relative, "enabled": bool(setting[0]["enabled"]) if setting else True, "source": "workspace", "extension_id": None}
            if include_content:
                item["content"] = text[:20_000]
            if _include_location:
                item["_location"] = str(manifest)
            found.append(item)
    try:
        from .extensions_runtime import active_extension_skill_paths

        extension_skills = active_extension_skill_paths()
    except (ImportError, RuntimeError, ValueError):
        extension_skills = []
    for extension in extension_skills:
        manifest = Path(extension["absolute_path"])
        if not manifest.is_file():
            continue
        text = _read_skill(manifest)
        name, description = manifest.parent.name, ""
        if text.startswith("---"):
            for line in text.split("---", 2)[1].splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
        if not _SKILL_NAME.fullmatch(name):
            name = manifest.parent.name
        display_path = f"extension:{extension['extension_id']}:{extension['version']}/{extension['path']}"
        setting = rows("SELECT enabled FROM skill_settings WHERE path=?", (display_path,))
        item = {
            "name": name,
            "description": description[:500],
            "path": display_path,
            "enabled": bool(setting[0]["enabled"]) if setting else True,
            "source": "extension",
            "extension_id": extension["extension_id"],
        }
        if include_content:
            item["content"] = text[:20_000]
        if _include_location:
            item["_location"] = str(manifest)
        found.append(item)
    return found


def skill_context(workspace: str, user_prompt: str, task_id: str | None = None) -> str:
    root = workspace_root(workspace)
    skills = [item for item in discover_skills(workspace, include_content=False, _include_location=True) if item["enabled"]]
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
        content = _read_skill(Path(item["_location"]))[: min(12_000, remaining)]
        if selected and total_chars + len(content) > settings.max_skill_context_chars:
            continue
        secured, sensitive, findings = secure_untrusted_text(content, f"skill:{item['path']}")
        selected.append({key: value for key, value in {**item, "content": secured}.items() if key != "_location"})
        total_chars += len(secured)
        record_data_flow(
            source=f"skill:{item['path']}",
            sink="model_context",
            classification=sensitive.classification,
            fields=("skill_content",),
            redactions=sensitive.redactions,
            allowed=True,
            reason=f"untrusted skill; injection findings: {','.join(findings)}" if findings else "untrusted skill data",
            task_id=task_id,
        )
    instructions = "\n\n".join(f"### Skill: {item['name']}\n{item['content']}" for item in selected)
    if task_id and selected:
        with connect() as db:
            db.executemany(
                "INSERT INTO skill_runs(task_id, name, path, content_chars, created_at) VALUES(?,?,?,?,?)",
                [(task_id, item["name"], item["path"], len(item["content"]), now_iso()) for item in selected],
            )
    return f"本轮按需加载的 Skill 指令（共 {len(selected)} 个）：\n{instructions}" if instructions else ""


def install_skill(workspace: str, name: str, content: str) -> dict[str, str]:
    if not _SKILL_NAME.fullmatch(name):
        raise ValueError("Skill name must contain only letters, numbers, dots, underscores, or hyphens")
    if len(content.encode("utf-8")) > 200_000:
        raise ValueError("Skill content exceeds 200 KB")
    root = workspace_root(workspace)
    target = safe_path(root, f".agent/skills/{name}/SKILL.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _SKILL_CONTENT_CACHE.pop(str(target), None)
    return {"name": name, "path": str(target.relative_to(root))}
