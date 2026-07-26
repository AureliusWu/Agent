from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize(
    "member_name",
    ("../escaped.txt", "/absolute.txt", "C" + r":\drive-absolute.txt"),
    ids=("parent-traversal", "posix-absolute", "windows-drive-absolute"),
)
def test_memory_zip_import_rejects_path_traversal(
    tmp_path: Path, member_name: str
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(member_name, "must not escape")

    escaped = tmp_path.parent / "escaped.txt"
    with TestClient(app) as client:
        response = client.post(
            "/api/memories/import",
            data={"namespace": "personal", "workspace": str(tmp_path)},
            files={"file": ("memories.zip", payload.getvalue(), "application/zip")},
        )

    assert response.status_code == 400
    assert not escaped.exists()
