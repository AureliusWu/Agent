"""Fixed, synthetic disk fixtures; distinct from the historical memory simulator."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FileCase:
    case_id: str
    prompt: str
    initial: dict[str, str]
    expected: dict[str, str]
    actions: tuple[tuple[str, dict[str, Any]], ...] = ()
    mode: str = "full"
    control: bool = False


CASES = (
    FileCase("basic-chat", "只回答 4，不调用工具。", {}, {}),
    FileCase("structured-plan", '只输出 JSON {"steps":["inspect","edit","verify"]}。', {}, {}),
    FileCase("readonly-read", "读取 `note.txt` 并原样返回内容，不改动任何文件。", {"note.txt": "qualified-read"}, {"note.txt": "qualified-read"}, (("read_file", {"path": "note.txt"}),), "readonly"),
    FileCase("file-create", "使用 create_file 创建 `笔记.txt`，内容精确为 alpha。", {}, {"笔记.txt": "alpha"}, (("create_file", {"path": "笔记.txt", "content": "alpha"}),)),
    FileCase("file-edit", "使用 read_file 读取 `note.txt`，再使用 replace_text 把 alpha 替换为 beta，保留其他内容。", {"note.txt": "alpha\nkeep"}, {"note.txt": "beta\nkeep"}, (("read_file", {"path": "note.txt"}), ("replace_text", {"path": "note.txt", "old_text": "alpha", "new_text": "beta", "expected_count": 1}))),
    FileCase("file-rename", "使用 read_file 读取 `note.txt`，再使用 rename_file 重命名为 `renamed.txt`，内容保持不变。", {"note.txt": "alpha"}, {"renamed.txt": "alpha"}, (("read_file", {"path": "note.txt"}), ("rename_file", {"source": "note.txt", "destination": "renamed.txt", "expected_destination_version_token": "missing"}))),
    FileCase("file-move", "使用 read_file 读取 `note.txt`，再使用 move_file 移动到 `archive/note.txt`，内容保持不变。", {"note.txt": "alpha"}, {"archive/note.txt": "alpha"}, (("read_file", {"path": "note.txt"}), ("move_file", {"source": "note.txt", "destination": "archive/note.txt", "expected_destination_version_token": "missing"}))),
    FileCase("file-undo", "使用 read_file 读取 `note.txt`，使用 write_file 将内容修改为 beta，然后使用 undo_file_change 撤销刚才的变更，最终恢复原来的 alpha。", {"note.txt": "alpha"}, {"note.txt": "alpha"}, (("read_file", {"path": "note.txt"}), ("write_file", {"path": "note.txt", "content": "beta"}), ("undo_file_change", {}))),
    # Kernel controls deliberately force a rejected tool call; these are never
    # reported as a live model's refusal or tool-selection capability.
    FileCase("kernel-readonly-deny", "读取工作区状态，遇到拒绝时明确停止，不要绕过。", {}, {}, (("create_file", {"path": "denied.txt", "content": "forbidden"}),), "readonly", True),
    FileCase("kernel-path-deny", "读取工作区状态，遇到沙箱拒绝时明确停止，不要绕过。", {}, {}, (("read_file", {"path": "../outside.txt"}),), "full", True),
)
REQUIRED_CASES = tuple(case.case_id for case in CASES)
LEVEL_CASES = {
    "basic": ("basic-chat",),
    "readonly_tools": ("basic-chat", "readonly-read", "kernel-readonly-deny", "kernel-path-deny"),
    "structured_plan": ("basic-chat", "structured-plan"),
    "file_agent": REQUIRED_CASES,
}


def scripted_completion(case: FileCase):
    index = 0
    async def complete(messages, api_key=None, **kwargs):
        nonlocal index
        if kwargs.get("phase") == "planning":
            return {"content": json.dumps({"goal": case.prompt, "steps": [], "acceptance_criteria": ["返回结果"], "risk": "low"}, ensure_ascii=False)}
        if index < len(case.actions):
            name, arguments = case.actions[index]
            arguments = dict(arguments)
            if name in {"write_file", "replace_text", "rename_file", "move_file"}:
                # Use the actual read receipt, never manufacture a version.
                def find_token(value):
                    if isinstance(value, dict):
                        if isinstance(value.get("version_token"), str):
                            return value["version_token"]
                        return next((found for item in value.values() if (found := find_token(item))), None)
                    if isinstance(value, list):
                        return next((found for item in value if (found := find_token(item))), None)
                    return None
                for message in reversed(messages):
                    if message.get("role") == "tool":
                        try:
                            token = find_token(json.loads(message.get("content") or "{}"))
                        except (ValueError, TypeError):
                            token = None
                        if token:
                            arguments["expected_version_token"] = token
                            break
            index += 1
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"qualification-{index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}
        content = {"basic-chat": "4", "structured-plan": '{"steps":["inspect","edit","verify"]}', "readonly-read": "qualified-read"}.get(case.case_id, "请求已处理，请依据实际工具回执和文件状态判断。")
        return {"role": "assistant", "content": content}
    return complete
