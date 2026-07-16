import io
import json
import uuid
import zipfile

import pytest

from app.full_backup import export_complete_backup, inspect_complete_backup, restore_complete_backup
from app.long_term_memory import create_memory, get_memory, update_memory


def test_complete_backup_requires_confirmation_and_restores_database() -> None:
    with pytest.raises(PermissionError):
        export_complete_backup(include_sensitive=True, administrator_confirmed=False)
    marker = uuid.uuid4().hex
    memory = create_memory(memory_type="semantic", content=f"备份事实 {marker}", user_confirmed=True)
    payload, manifest = export_complete_backup(include_sensitive=True, administrator_confirmed=True)
    update_memory(memory["id"], {"content": f"已改变 {marker}"}, administrator_confirmed=True)
    result = restore_complete_backup(payload, administrator_confirmed=True)
    restored = get_memory(memory["id"])
    assert manifest["contains_sensitive_data"] is True
    assert result["restored"] is True
    assert restored["content"] == f"备份事实 {marker}"


def test_complete_backup_rejects_hash_mismatch() -> None:
    payload, _ = export_complete_backup(include_sensitive=True, administrator_confirmed=True)
    source = zipfile.ZipFile(io.BytesIO(payload))
    manifest = json.loads(source.read("manifest.json"))
    database = source.read("agent.db") + b"tampered"
    continuity = source.read("kokoro_autobiography.md")
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("agent.db", database)
        archive.writestr("kokoro_autobiography.md", continuity)
    with pytest.raises(ValueError, match="hash"):
        inspect_complete_backup(target.getvalue())
