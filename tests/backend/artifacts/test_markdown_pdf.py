from __future__ import annotations

import base64
import io

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.artifacts.errors import ArtifactError
from app.artifacts.markdown import markdown_to_document
from app.artifacts.model import (
    ArtifactDocument,
    ArtifactImage,
    ArtifactTable,
    ContentBlock,
)
from app.artifacts.pdf import (
    _DATA_IMAGE_RE,
    NO_TEXT_LAYER_MARKER,
    create_pdf,
    extract_pdf_text,
    merge_pdfs,
    validate_pdf,
)
from app.artifacts.render import render_pdf_to_png


def _image_data_uri() -> str:
    output = io.BytesIO()
    with Image.new("RGB", (160, 80), (37, 115, 170)) as image:
        image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def test_inline_pdf_images_match_frozen_pillow_plugins() -> None:
    assert _DATA_IMAGE_RE.fullmatch("data:image/png;base64,AA==")
    assert _DATA_IMAGE_RE.fullmatch("data:image/jpeg;base64,AA==")
    assert _DATA_IMAGE_RE.fullmatch("data:image/webp;base64,AA==")
    assert _DATA_IMAGE_RE.fullmatch("data:image/gif;base64,AA==") is None


def _simple_document(label: str) -> ArtifactDocument:
    return ArtifactDocument(
        title=label,
        blocks=(
            ContentBlock(kind="heading", text=label, level=1),
            ContentBlock(kind="paragraph", text=f"{label} 的正文内容。"),
        ),
    )


def test_markdown_to_model_supports_chinese_lists_table_and_image() -> None:
    document = markdown_to_document(
        """# 项目简报

这是 **中文** 正文，包含 [公开链接](https://example.invalid)。

- 第一项
- 第二项

1. 先分析
2. 再验证

| 名称 | 状态 |
| --- | --- |
| 产物 | 完成 |

![示意图](charts/result.png)
"""
    )

    assert document.title == "项目简报"
    assert [block.kind for block in document.blocks] == [
        "heading",
        "paragraph",
        "bullet_list",
        "numbered_list",
        "table",
        "image",
    ]
    assert document.blocks[1].text == "这是 中文 正文，包含 公开链接。"
    assert document.blocks[2].items == ("第一项", "第二项")
    assert document.blocks[4].table == ArtifactTable(
        rows=(("名称", "状态"), ("产物", "完成")),
        header_rows=1,
    )
    assert document.blocks[5].image == ArtifactImage(
        source="charts/result.png",
        alt_text="示意图",
    )


def test_markdown_parser_enforces_resource_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.artifacts.markdown.MAX_MARKDOWN_CHARS", 4)

    with pytest.raises(ArtifactError) as captured:
        markdown_to_document("12345")

    assert captured.value.error_code == "artifact_limit_exceeded"
    assert captured.value.safe_details == {
        "resource": "characters",
        "actual": 5,
        "limit": 4,
    }


def test_create_validate_extract_and_render_real_chinese_pdf() -> None:
    document = ArtifactDocument(
        title="季度总结",
        blocks=(
            ContentBlock(kind="heading", text="季度总结", level=1),
            ContentBlock(
                kind="paragraph",
                text="这是由司忆生成的中文 PDF，包含分页、表格和图片。",
            ),
            ContentBlock(
                kind="table",
                table=ArtifactTable(
                    rows=(
                        ("项目", "结果"),
                        ("中文字体", "正常"),
                        ("独立验证", "通过"),
                    )
                ),
            ),
            ContentBlock(
                kind="image",
                image=ArtifactImage(
                    source=_image_data_uri(),
                    alt_text="蓝色测试图",
                    width_px=160,
                    height_px=80,
                ),
            ),
            ContentBlock(kind="page_break"),
            ContentBlock(kind="paragraph", text="第二页真实文本。"),
        ),
    )

    payload = create_pdf(document)
    report = validate_pdf(payload)
    extraction = extract_pdf_text(payload)
    rendered = render_pdf_to_png(payload, dpi=96)

    assert payload.startswith(b"%PDF-")
    assert report.status == "PASS"
    assert report.openable is True
    assert report.page_count == 2
    assert extraction.page_count == 2
    assert "季度总结" in extraction.pages[0].text
    assert "第二页真实文本" in extraction.pages[1].text
    assert len(rendered) == 2
    for png in rendered:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(io.BytesIO(png)) as image:
            assert image.format == "PNG"
            assert image.width > 500
            assert image.height > 700


def test_pdf_layout_automatically_paginates_long_content() -> None:
    document = ArtifactDocument(
        title="长文分页",
        blocks=tuple(
            ContentBlock(
                kind="paragraph",
                text=f"第 {index} 段：" + ("自动分页内容。" * 24),
            )
            for index in range(80)
        ),
    )

    payload = create_pdf(document)
    report = validate_pdf(payload)

    assert report.status == "PASS"
    assert report.page_count is not None
    assert report.page_count >= 3


def test_merge_preserves_document_and_page_order() -> None:
    first = create_pdf(_simple_document("第一份"))
    second = create_pdf(_simple_document("第二份"))

    merged = merge_pdfs((first, second))
    extraction = extract_pdf_text(merged)

    assert extraction.page_count == 2
    assert "第一份" in extraction.pages[0].text
    assert "第二份" in extraction.pages[1].text
    assert validate_pdf(merged).status == "PASS"


def test_extract_marks_page_without_text_layer_and_does_not_fake_ocr() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    output = io.BytesIO()
    writer.write(output)
    writer.close()

    extraction = extract_pdf_text(output.getvalue())
    page = extraction.pages[0]

    assert page.text == ""
    assert page.has_text_layer is False
    assert page.scanned_or_empty is True
    assert page.marker == NO_TEXT_LAYER_MARKER
    assert extraction.ocr_performed is False


def test_validate_corrupt_pdf_returns_unified_failure() -> None:
    report = validate_pdf(b"%PDF-1.7\nnot-a-real-pdf\n%%EOF")

    assert report.status == "FAIL"
    assert report.openable is False
    assert report.errors
    assert report.errors[0].phase == "validate"
    assert all(issue.error_code for issue in report.errors)


def test_local_image_reference_requires_permission_checked_loader() -> None:
    document = ArtifactDocument(
        blocks=(
            ContentBlock(
                kind="image",
                image=ArtifactImage(source="images/private.png"),
            ),
        )
    )

    with pytest.raises(ArtifactError) as captured:
        create_pdf(document)

    assert captured.value.error_code == "artifact_image_source_unresolved"
    assert "private.png" not in str(captured.value.to_dict())


def test_render_page_selection_is_one_based_and_bounded() -> None:
    payload = create_pdf(
        ArtifactDocument(
            blocks=(
                ContentBlock(kind="paragraph", text="第一页"),
                ContentBlock(kind="page_break"),
                ContentBlock(kind="paragraph", text="第二页"),
            )
        )
    )

    selected = render_pdf_to_png(payload, dpi=72, page_numbers=(2,))
    assert len(selected) == 1

    with pytest.raises(ArtifactError) as captured:
        render_pdf_to_png(payload, page_numbers=(3,))
    assert captured.value.error_code == "artifact_page_out_of_range"
