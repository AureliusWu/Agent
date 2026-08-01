from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.artifacts.docx import read_docx
from app.artifacts.pptx import read_pptx
from app.artifacts.service import execute_artifact_tool
from app.artifacts.store import read_artifact_bytes, store_artifact
from app.database import connect, now_iso
from app.main import app
from app.sandbox import file_version_token


def _create_task() -> str:
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            ("artifact-service", "", "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, int(cursor.lastrowid), "pending", "artifact service", stamp, stamp),
        )
    return task_id


def test_service_creates_and_edits_real_docx_and_pptx(tmp_path) -> None:
    created_docx = execute_artifact_tool(
        str(tmp_path),
        "artifact.docx.create",
        {
            "path": "report.docx",
            "title": "司忆周报",
            "content": "# 本周进展\n\n旧值\n\n| 项目 | 状态 |\n| --- | --- |\n| 文档 | 完成 |",
        },
    )
    assert created_docx["success"] is True
    assert created_docx["validation"]["status"] == "PASS"
    assert (tmp_path / "report.docx").read_bytes().startswith(b"PK")

    version = file_version_token(tmp_path / "report.docx")
    edited_docx = execute_artifact_tool(
        str(tmp_path),
        "artifact.docx.edit",
        {
            "path": "report.docx",
            "replacements": [{"old": "旧值", "new": "新值", "expected_count": 1}],
            "expected_version_token": version,
        },
    )
    assert edited_docx["success"] is True
    assert "新值" in str(read_docx((tmp_path / "report.docx").read_bytes()).to_dict())

    created_pptx = execute_artifact_tool(
        str(tmp_path),
        "artifact.pptx.create",
        {
            "path": "briefing.pptx",
            "title": "司忆",
            "slides": [
                {
                    "title": "当前状态",
                    "blocks": [
                        {"kind": "paragraph", "text": "旧结论"},
                        {"kind": "bullet_list", "items": ["中文字体", "真实文件"]},
                    ],
                }
            ],
        },
    )
    assert created_pptx["success"] is True
    assert created_pptx["validation"]["slide_count"] == 1

    pptx_version = file_version_token(tmp_path / "briefing.pptx")
    edited_pptx = execute_artifact_tool(
        str(tmp_path),
        "artifact.pptx.edit",
        {
            "path": "briefing.pptx",
            "replacements": [{"old": "旧结论", "new": "新结论", "expected_count": 1}],
            "expected_version_token": pptx_version,
        },
    )
    assert edited_pptx["success"] is True
    assert "新结论" in str(read_pptx((tmp_path / "briefing.pptx").read_bytes()).to_dict())


def test_service_pdf_create_merge_extract_and_true_render(tmp_path) -> None:
    (tmp_path / "rendered").mkdir()
    for index in (1, 2):
        created = execute_artifact_tool(
            str(tmp_path),
            "artifact.pdf.create",
            {
                "path": f"part-{index}.pdf",
                "title": f"第 {index} 部分",
                "content": f"# 第 {index} 部分\n\n这是第 {index} 页的中文内容。",
            },
        )
        assert created["success"] is True
        assert created["validation"]["status"] == "PASS"

    merged = execute_artifact_tool(
        str(tmp_path),
        "artifact.pdf.merge",
        {
            "path": "merged.pdf",
            "inputs": ["part-1.pdf", "part-2.pdf"],
        },
    )
    assert merged["success"] is True
    assert merged["validation"]["page_count"] == 2

    extracted = execute_artifact_tool(
        str(tmp_path),
        "artifact.pdf.extract",
        {"path": "merged.pdf", "max_pages": 10, "max_chars": 20_000},
    )
    assert extracted["success"] is True
    assert extracted["page_count"] == 2
    assert extracted["ocr_performed"] is False
    assert all(page["has_text_layer"] for page in extracted["pages"])

    rendered = execute_artifact_tool(
        str(tmp_path),
        "artifact.render",
        {"path": "merged.pdf", "output_directory": "rendered", "dpi": 96},
    )
    assert rendered["success"] is True
    assert rendered["page_count"] == 2
    for relative in rendered["paths"]:
        assert (tmp_path / relative).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_service_rejects_unsafe_or_unverifiable_operations_without_writing(tmp_path) -> None:
    escaped = execute_artifact_tool(
        str(tmp_path),
        "artifact.pdf.create",
        {"path": "../escape.pdf", "content": "# forbidden"},
    )
    assert escaped["success"] is False
    assert escaped["error_code"] == "artifact_workspace_error"
    assert not (tmp_path.parent / "escape.pdf").exists()

    undeclared_image = execute_artifact_tool(
        str(tmp_path),
        "artifact.docx.create",
        {"path": "image.docx", "content": "![proof](proof.png)"},
    )
    assert undeclared_image["success"] is False
    assert undeclared_image["error_code"] == "artifact_image_not_authorized"
    assert not (tmp_path / "image.docx").exists()

    dry_run = execute_artifact_tool(
        str(tmp_path),
        "artifact.markdown.create",
        {"path": "preview.md", "content": "# Preview", "dry_run": True},
    )
    assert dry_run["success"] is True
    assert dry_run["dry_run"] is True
    assert not (tmp_path / "preview.md").exists()

    (tmp_path / "broken.pdf").write_bytes(b"not-a-pdf")
    invalid = execute_artifact_tool(
        str(tmp_path),
        "artifact.validate",
        {"path": "broken.pdf"},
    )
    assert invalid["success"] is False
    assert invalid["error_code"] == "artifact_validation_failed"

    (tmp_path / "unrendered.docx").write_bytes(b"PK")
    native_render = execute_artifact_tool(
        str(tmp_path),
        "artifact.render",
        {"path": "unrendered.docx", "output_directory": "."},
    )
    assert native_render["success"] is False
    assert native_render["error_code"] == "artifact_native_renderer_unavailable"


def test_binary_artifact_download_preserves_bytes_and_safe_headers() -> None:
    task_id = _create_task()
    payload = b"PK\x03\x04synthetic-docx"
    stored = store_artifact(
        payload,
        task_id=task_id,
        tool_call_id="download-test",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="司忆周报.docx",
    )
    with TestClient(app) as client:
        response = client.get(f"/api/artifacts/{stored['artifact_id']}/raw")
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    disposition = response.headers["content-disposition"]
    assert 'filename="artifact.docx"' in disposition
    assert "filename*=UTF-8''%E5%8F%B8%E5%BF%86%E5%91%A8%E6%8A%A5.docx" in disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_artifact_blob_deduplication_keeps_task_ownership_separate() -> None:
    first_task = _create_task()
    second_task = _create_task()
    payload = b"shared artifact bytes"
    first = store_artifact(
        payload,
        task_id=first_task,
        tool_call_id="first",
        media_type="application/pdf",
        filename="report.pdf",
    )
    second = store_artifact(
        payload,
        task_id=second_task,
        tool_call_id="second",
        media_type="application/pdf",
        filename="../../unsafe.exe",
    )
    assert first["artifact_id"] != second["artifact_id"]
    assert first["content_sha256"] == second["content_sha256"]
    assert second["deduplicated"] is True
    first_record, first_bytes = read_artifact_bytes(first["artifact_id"])
    second_record, second_bytes = read_artifact_bytes(second["artifact_id"])
    assert first_record["task_id"] == first_task
    assert second_record["task_id"] == second_task
    assert second_record["filename"] == "unsafe.pdf"
    assert first_bytes == second_bytes == payload


def test_conversation_reload_keeps_task_artifact_descriptors() -> None:
    task_id = _create_task()
    with connect() as db:
        task = db.execute(
            "SELECT conversation_id FROM agent_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        conversation_id = int(task["conversation_id"])
        db.execute(
            "INSERT INTO messages(conversation_id,role,content,task_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (conversation_id, "assistant", "已生成报告。", task_id, now_iso()),
        )
    stored = store_artifact(
        b"%PDF-1.4\n%%EOF",
        task_id=task_id,
        tool_call_id="persisted-download",
        media_type="application/pdf",
        filename="持久报告.pdf",
    )
    with TestClient(app) as client:
        response = client.get(f"/api/conversations/{conversation_id}/messages")
    assert response.status_code == 200
    assistant = response.json()[-1]
    assert assistant["role"] == "assistant"
    artifact = assistant["artifacts"][0]
    assert artifact["artifact_id"] == stored["artifact_id"]
    assert artifact["content_sha256"] == stored["content_sha256"]
    assert artifact["download_url"] == f"/api/artifacts/{stored['artifact_id']}/raw"
    assert artifact["filename"] == "持久报告.pdf"
    assert artifact["media_type"] == "application/pdf"
    assert artifact["total_bytes"] == 14
