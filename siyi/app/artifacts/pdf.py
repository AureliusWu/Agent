from __future__ import annotations

import base64
import binascii
import html
import io
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as ReportLabImage,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.artifacts.errors import ArtifactError
from app.artifacts.model import (
    ArtifactDocument,
    ArtifactImage,
    ArtifactTable,
    ContentBlock,
    normalize_document,
)
from app.artifacts.validation import ValidationIssue, ValidationReport


MAX_PDF_INPUT_BYTES = 64 * 1024 * 1024
MAX_PDF_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_PDF_MERGE_FILES = 50
MAX_PDF_MERGE_INPUT_BYTES = 256 * 1024 * 1024
MAX_DOCUMENT_BLOCKS = 10_000
MAX_DOCUMENT_TEXT_CHARS = 5_000_000
MAX_TABLE_CELLS = 100_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000

NO_TEXT_LAYER_MARKER = "[未检测到文本层；未执行 OCR]"

_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpe?g|webp);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
_XML_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)
_FONT_LOCK = threading.Lock()
_FONT_NAME: str | None = None

ImageLoader = Callable[[str], bytes]


@dataclass(frozen=True, slots=True)
class PdfPageText:
    page_number: int
    text: str
    has_text_layer: bool
    scanned_or_empty: bool
    marker: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "text": self.text,
            "has_text_layer": self.has_text_layer,
            "scanned_or_empty": self.scanned_or_empty,
            "marker": self.marker,
        }


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    page_count: int
    pages: tuple[PdfPageText, ...]
    ocr_performed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "page_count": self.page_count,
            "pages": [page.to_dict() for page in self.pages],
            "ocr_performed": self.ocr_performed,
        }


@dataclass(slots=True)
class _PdfResourceBudget:
    image_bytes: int = 0

    def consume_image(self, size: int) -> None:
        self.image_bytes += size
        if self.image_bytes > MAX_DOCUMENT_IMAGE_BYTES:
            raise _limit_error(
                "create",
                "total_image_bytes",
                self.image_bytes,
                MAX_DOCUMENT_IMAGE_BYTES,
            )


def create_pdf(
    document: ArtifactDocument | Mapping[str, Any],
    *,
    image_loader: ImageLoader | None = None,
) -> bytes:
    """Create a paginated, printable PDF entirely in memory."""

    try:
        normalized = normalize_document(document)
        _validate_document_resources(normalized)
        font_name = _register_cjk_font()
        output = io.BytesIO()
        pdf = SimpleDocTemplate(
            output,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=normalized.title or "司忆产物",
            author="司忆",
            pageCompression=1,
        )
        styles = _build_styles(font_name)
        budget = _PdfResourceBudget()
        story = _build_story(
            normalized,
            styles=styles,
            image_loader=image_loader,
            budget=budget,
            usable_width=A4[0] - 36 * mm,
            usable_height=A4[1] - 36 * mm,
        )
        if not story:
            story.append(Spacer(1, 1))
        pdf.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
        payload = output.getvalue()
        if len(payload) > MAX_PDF_OUTPUT_BYTES:
            raise _limit_error(
                "create",
                "output_bytes",
                len(payload),
                MAX_PDF_OUTPUT_BYTES,
            )
        reader = _read_pdf(payload, phase="create")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise _limit_error(
                "create",
                "pages",
                len(reader.pages),
                MAX_PDF_PAGES,
            )
        return payload
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError(
            "artifact_pdf_create_failed",
            "create",
            "The PDF could not be created.",
            details={"reason": type(exc).__name__},
        ) from exc


def merge_pdfs(
    pdf_documents: Sequence[bytes | bytearray | memoryview],
) -> bytes:
    """Merge PDFs in caller-provided order without re-rendering pages."""

    if isinstance(pdf_documents, (str, bytes, bytearray, memoryview)) or not isinstance(
        pdf_documents, Sequence
    ):
        raise ArtifactError(
            "artifact_invalid_input",
            "merge",
            "PDF inputs must be a list of byte payloads.",
        )
    if not pdf_documents:
        raise ArtifactError(
            "artifact_invalid_input",
            "merge",
            "At least one PDF is required.",
        )
    if len(pdf_documents) > MAX_PDF_MERGE_FILES:
        raise _limit_error(
            "merge",
            "files",
            len(pdf_documents),
            MAX_PDF_MERGE_FILES,
        )

    writer = PdfWriter()
    total_bytes = 0
    total_pages = 0
    try:
        for index, value in enumerate(pdf_documents, start=1):
            payload = _bounded_pdf_payload(value, phase="merge")
            total_bytes += len(payload)
            if total_bytes > MAX_PDF_MERGE_INPUT_BYTES:
                raise _limit_error(
                    "merge",
                    "total_input_bytes",
                    total_bytes,
                    MAX_PDF_MERGE_INPUT_BYTES,
                )
            reader = _read_pdf(payload, phase="merge", document_index=index)
            total_pages += len(reader.pages)
            if total_pages > MAX_PDF_PAGES:
                raise _limit_error(
                    "merge",
                    "pages",
                    total_pages,
                    MAX_PDF_PAGES,
                )
            for page in reader.pages:
                writer.add_page(page)

        output = io.BytesIO()
        writer.write(output)
        payload = output.getvalue()
        if len(payload) > MAX_PDF_OUTPUT_BYTES:
            raise _limit_error(
                "merge",
                "output_bytes",
                len(payload),
                MAX_PDF_OUTPUT_BYTES,
            )
        merged = _read_pdf(payload, phase="merge")
        if len(merged.pages) != total_pages:
            raise ArtifactError(
                "artifact_pdf_merge_failed",
                "merge",
                "The merged PDF page count is inconsistent.",
                details={"expected_pages": total_pages, "actual_pages": len(merged.pages)},
            )
        return payload
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError(
            "artifact_pdf_merge_failed",
            "merge",
            "The PDFs could not be merged.",
            details={"reason": type(exc).__name__},
        ) from exc
    finally:
        writer.close()


def extract_pdf_text(
    pdf_bytes: bytes | bytearray | memoryview,
) -> PdfExtraction:
    """Extract each page's real text layer without claiming OCR."""

    payload = _bounded_pdf_payload(pdf_bytes, phase="extract")
    reader = _read_pdf(payload, phase="extract")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise _limit_error("extract", "pages", len(reader.pages), MAX_PDF_PAGES)

    pages: list[PdfPageText] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            extracted = page.extract_text() or ""
        except Exception as exc:
            raise ArtifactError(
                "artifact_pdf_extract_failed",
                "extract",
                "A PDF page text layer could not be extracted.",
                details={"page_number": page_number, "reason": type(exc).__name__},
            ) from exc
        text = extracted.strip()
        has_text = bool(text)
        pages.append(
            PdfPageText(
                page_number=page_number,
                text=text,
                has_text_layer=has_text,
                scanned_or_empty=not has_text,
                marker=None if has_text else NO_TEXT_LAYER_MARKER,
            )
        )
    return PdfExtraction(page_count=len(pages), pages=tuple(pages))


def validate_pdf(
    pdf_bytes: bytes | bytearray | memoryview,
) -> ValidationReport:
    """Independently verify PDF structure without trusting create/merge results."""

    checks: dict[str, bool] = {
        "signature": False,
        "eof_marker": False,
        "readable": False,
        "unencrypted": False,
        "page_dimensions": False,
    }
    errors: list[ValidationIssue] = []
    risks: list[ValidationIssue] = []
    page_count = 0

    try:
        payload = _bounded_pdf_payload(pdf_bytes, phase="validate")
    except ArtifactError as exc:
        return ValidationReport(
            format="pdf",
            status="FAIL",
            openable=False,
            page_count=0,
            risks=(),
            errors=(ValidationIssue.from_error(exc),),
        )

    checks["signature"] = payload.startswith(b"%PDF-")
    checks["eof_marker"] = b"%%EOF" in payload[-2048:]
    if not checks["signature"]:
        errors.append(
            _validation_issue(
                "invalid_pdf_signature",
                "The file does not have a PDF signature.",
            )
        )
    if not checks["eof_marker"]:
        errors.append(
            _validation_issue(
                "missing_pdf_eof_marker",
                "The PDF end-of-file marker is missing.",
            )
        )

    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            errors.append(
                _validation_issue(
                    "encrypted_pdf_not_supported",
                    "Encrypted PDFs are not supported.",
                )
            )
        else:
            checks["unencrypted"] = True
            page_count = len(reader.pages)
            if page_count > MAX_PDF_PAGES:
                errors.append(
                    _validation_issue(
                        "pdf_page_limit_exceeded",
                        "The PDF page count exceeds the configured limit.",
                        {"actual": page_count, "limit": MAX_PDF_PAGES},
                    )
                )
            dimensions_ok = True
            for page in reader.pages:
                width = float(page.mediabox.width)
                height = float(page.mediabox.height)
                if width <= 0 or height <= 0:
                    dimensions_ok = False
                    break
            checks["page_dimensions"] = dimensions_ok
            if not dimensions_ok:
                errors.append(
                    _validation_issue(
                        "invalid_pdf_page_dimensions",
                        "A PDF page has invalid dimensions.",
                    )
                )
            checks["readable"] = page_count <= MAX_PDF_PAGES and dimensions_ok
            if page_count == 0:
                risks.append(
                    _validation_issue(
                        "pdf_has_no_pages",
                        "The PDF contains no pages.",
                    )
                )
    except Exception as exc:
        errors.append(
            _validation_issue(
                "pdf_parse_failed",
                "The PDF could not be reopened by the independent validator.",
                {"reason": type(exc).__name__},
            )
        )

    passed = all(checks.values()) and not errors and not risks
    return ValidationReport(
        format="pdf",
        status="PASS" if passed else "FAIL",
        openable=passed,
        page_count=page_count,
        checks=tuple(name for name, succeeded in checks.items() if succeeded),
        risks=tuple(risks),
        errors=tuple(errors),
    )


def _build_story(
    document: ArtifactDocument,
    *,
    styles: dict[str, ParagraphStyle],
    image_loader: ImageLoader | None,
    budget: _PdfResourceBudget,
    usable_width: float,
    usable_height: float,
) -> list[object]:
    story: list[object] = []
    first_block_is_title = bool(
        document.blocks
        and document.blocks[0].kind == "heading"
        and document.blocks[0].level == 1
        and document.blocks[0].text.strip() == document.title.strip()
    )
    if document.title and not first_block_is_title:
        story.append(Paragraph(_paragraph_text(document.title), styles["title"]))
        story.append(Spacer(1, 5 * mm))

    for block in document.blocks:
        story.extend(
            _block_flowables(
                block,
                styles=styles,
                image_loader=image_loader,
                budget=budget,
                usable_width=usable_width,
                usable_height=usable_height,
            )
        )
    return story


def _block_flowables(
    block: ContentBlock,
    *,
    styles: dict[str, ParagraphStyle],
    image_loader: ImageLoader | None,
    budget: _PdfResourceBudget,
    usable_width: float,
    usable_height: float,
) -> list[object]:
    if block.kind == "heading":
        style = styles[f"heading{block.level or 1}"]
        return [Paragraph(_paragraph_text(block.text), style), Spacer(1, 2 * mm)]
    if block.kind == "paragraph":
        return [
            Paragraph(_paragraph_text(block.text), styles["body"]),
            Spacer(1, 2.5 * mm),
        ]
    if block.kind in {"bullet_list", "numbered_list"}:
        items = [
            ListItem(
                Paragraph(_paragraph_text(item), styles["body"]),
                leftIndent=4 * mm,
            )
            for item in block.items
        ]
        return [
            ListFlowable(
                items,
                bulletType="1" if block.kind == "numbered_list" else "bullet",
                start="1",
                leftIndent=7 * mm,
                bulletFontName=styles["body"].fontName,
                bulletFontSize=styles["body"].fontSize,
            ),
            Spacer(1, 2.5 * mm),
        ]
    if block.kind == "page_break":
        return [PageBreak()]
    if block.kind == "table" and block.table is not None:
        return [
            _table_flowable(
                block.table,
                styles=styles,
                usable_width=usable_width,
            ),
            Spacer(1, 3 * mm),
        ]
    if block.kind == "image" and block.image is not None:
        return [
            _image_flowable(
                block.image,
                image_loader=image_loader,
                budget=budget,
                usable_width=usable_width,
                usable_height=usable_height,
            ),
            Spacer(1, 3 * mm),
        ]
    raise ArtifactError(
        "invalid_artifact_model",
        "create",
        "The document contains an unsupported content block.",
        details={"block_kind": block.kind},
    )


def _table_flowable(
    table: ArtifactTable,
    *,
    styles: dict[str, ParagraphStyle],
    usable_width: float,
) -> Table:
    columns = len(table.rows[0])
    cells = len(table.rows) * columns
    if cells > MAX_TABLE_CELLS:
        raise _limit_error("create", "table_cells", cells, MAX_TABLE_CELLS)
    data = [
        [
            Paragraph(
                _paragraph_text(cell),
                styles["table_header"] if row_index < table.header_rows else styles["table"],
            )
            for cell in row
        ]
        for row_index, row in enumerate(table.rows)
    ]
    result = Table(
        data,
        colWidths=[usable_width / columns] * columns,
        repeatRows=table.header_rows,
        hAlign="LEFT",
        splitByRow=1,
        splitInRow=1,
    )
    rules: list[tuple[Any, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#A8A8A8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if table.header_rows:
        rules.append(
            (
                "BACKGROUND",
                (0, 0),
                (-1, table.header_rows - 1),
                colors.HexColor("#EDEDED"),
            )
        )
    result.setStyle(TableStyle(rules))
    return result


def _image_flowable(
    image: ArtifactImage,
    *,
    image_loader: ImageLoader | None,
    budget: _PdfResourceBudget,
    usable_width: float,
    usable_height: float,
) -> ReportLabImage:
    payload = _resolve_image(image.source, image_loader=image_loader)
    budget.consume_image(len(payload))
    try:
        with PILImage.open(io.BytesIO(payload)) as opened:
            width_px, height_px = opened.size
            pixels = width_px * height_px
            if pixels > MAX_IMAGE_PIXELS:
                raise _limit_error("create", "image_pixels", pixels, MAX_IMAGE_PIXELS)
            opened.load()
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError(
            "artifact_invalid_image",
            "create",
            "An artifact image could not be decoded.",
            details={"reason": type(exc).__name__},
        ) from exc

    if image.width_px is not None and image.height_px is None:
        requested_width = image.width_px
        requested_height = max(1, round(height_px * image.width_px / width_px))
    elif image.height_px is not None and image.width_px is None:
        requested_height = image.height_px
        requested_width = max(1, round(width_px * image.height_px / height_px))
    else:
        requested_width = image.width_px or width_px
        requested_height = image.height_px or height_px
    width_points = requested_width * 0.75
    height_points = requested_height * 0.75
    scale = min(
        1.0,
        usable_width / width_points,
        (usable_height * 0.85) / height_points,
    )
    return ReportLabImage(
        io.BytesIO(payload),
        width=width_points * scale,
        height=height_points * scale,
    )


def _resolve_image(source: str, *, image_loader: ImageLoader | None) -> bytes:
    data_match = _DATA_IMAGE_RE.fullmatch(source.strip())
    if data_match:
        try:
            payload = base64.b64decode(data_match.group("data"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ArtifactError(
                "artifact_invalid_image",
                "create",
                "An inline artifact image is not valid base64.",
            ) from exc
    elif image_loader is not None:
        try:
            payload = image_loader(source)
        except ArtifactError:
            raise
        except Exception as exc:
            raise ArtifactError(
                "artifact_image_load_failed",
                "create",
                "An artifact image could not be loaded.",
                details={"reason": type(exc).__name__},
            ) from exc
    else:
        raise ArtifactError(
            "artifact_image_source_unresolved",
            "create",
            "A permission-checked image loader is required for local image references.",
        )

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ArtifactError(
            "artifact_invalid_image",
            "create",
            "The image loader must return bytes.",
        )
    bounded = bytes(payload)
    if not bounded:
        raise ArtifactError(
            "artifact_invalid_image",
            "create",
            "Artifact image content must not be empty.",
        )
    if len(bounded) > MAX_IMAGE_BYTES:
        raise _limit_error("create", "image_bytes", len(bounded), MAX_IMAGE_BYTES)
    return bounded


def _build_styles(font_name: str) -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "ArtifactBody",
        fontName=font_name,
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor("#202020"),
        alignment=TA_LEFT,
        wordWrap="CJK",
        allowWidows=0,
        allowOrphans=0,
    )
    styles: dict[str, ParagraphStyle] = {
        "body": body,
        "title": ParagraphStyle(
            "ArtifactTitle",
            parent=body,
            fontSize=22,
            leading=30,
            alignment=TA_CENTER,
            spaceAfter=8,
        ),
        "table": ParagraphStyle(
            "ArtifactTable",
            parent=body,
            fontSize=8.5,
            leading=12,
        ),
        "table_header": ParagraphStyle(
            "ArtifactTableHeader",
            parent=body,
            fontSize=8.5,
            leading=12,
        ),
    }
    heading_sizes = {1: 18, 2: 15, 3: 13, 4: 12, 5: 11, 6: 10.5}
    for level, size in heading_sizes.items():
        styles[f"heading{level}"] = ParagraphStyle(
            f"ArtifactHeading{level}",
            parent=body,
            fontSize=size,
            leading=size * 1.45,
            spaceBefore=4,
            spaceAfter=3,
        )
    return styles


def _register_cjk_font() -> str:
    global _FONT_NAME
    if _FONT_NAME is not None:
        return _FONT_NAME

    with _FONT_LOCK:
        if _FONT_NAME is not None:
            return _FONT_NAME
        candidates: list[Path] = []
        if os.name == "nt":
            windows = Path(os.environ.get("WINDIR") or ("C:" + "\\Windows"))
            candidates.extend(
                [
                    windows / "Fonts" / "simhei.ttf",
                    windows / "Fonts" / "msyh.ttc",
                    windows / "Fonts" / "simsun.ttc",
                ]
            )
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                name = "ArtifactCJK"
                pdfmetrics.registerFont(TTFont(name, str(candidate)))
                _FONT_NAME = name
                return name
            except Exception:
                continue
        fallback = "STSong-Light"
        if fallback not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(fallback))
        _FONT_NAME = fallback
        return fallback


def _validate_document_resources(document: ArtifactDocument) -> None:
    if len(document.blocks) > MAX_DOCUMENT_BLOCKS:
        raise _limit_error(
            "create",
            "blocks",
            len(document.blocks),
            MAX_DOCUMENT_BLOCKS,
        )
    total_cells = sum(
        len(block.table.rows) * len(block.table.rows[0])
        for block in document.blocks
        if block.kind == "table" and block.table is not None
    )
    if total_cells > MAX_TABLE_CELLS:
        raise _limit_error(
            "create",
            "table_cells",
            total_cells,
            MAX_TABLE_CELLS,
        )
    total_text = len(document.title)
    for block in document.blocks:
        total_text += len(block.text)
        total_text += sum(len(item) for item in block.items)
        if block.table is not None:
            total_text += sum(
                len(cell) for row in block.table.rows for cell in row
            )
        if total_text > MAX_DOCUMENT_TEXT_CHARS:
            raise _limit_error(
                "create",
                "text_characters",
                total_text,
                MAX_DOCUMENT_TEXT_CHARS,
            )


def _read_pdf(
    payload: bytes,
    *,
    phase: str,
    document_index: int | None = None,
) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(payload), strict=False)
        if reader.is_encrypted:
            raise ArtifactError(
                "artifact_encrypted_pdf_unsupported",
                phase,
                "Encrypted PDFs are not supported.",
                details=(
                    {"document_index": document_index}
                    if document_index is not None
                    else None
                ),
            )
        # Force lazy page-tree parsing while the stable error boundary is active.
        len(reader.pages)
        return reader
    except ArtifactError:
        raise
    except Exception as exc:
        details: dict[str, object] = {"reason": type(exc).__name__}
        if document_index is not None:
            details["document_index"] = document_index
        raise ArtifactError(
            "artifact_invalid_pdf",
            phase,
            "The PDF structure is invalid or unsupported.",
            details=details,
        ) from exc


def _bounded_pdf_payload(
    value: bytes | bytearray | memoryview,
    *,
    phase: str,
) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ArtifactError(
            "artifact_invalid_input",
            phase,
            "PDF content must be bytes.",
            details={"actual_type": type(value).__name__},
        )
    payload = bytes(value)
    if not payload:
        raise ArtifactError(
            "artifact_invalid_pdf",
            phase,
            "PDF content must not be empty.",
        )
    if len(payload) > MAX_PDF_INPUT_BYTES:
        raise _limit_error(phase, "input_bytes", len(payload), MAX_PDF_INPUT_BYTES)
    return payload


def _paragraph_text(value: str) -> str:
    cleaned = _XML_CONTROL_RE.sub("", value)
    return html.escape(cleaned).replace("\n", "<br/>")


def _page_footer(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#777777"))
    canvas.drawCentredString(A4[0] / 2, 8 * mm, str(document.page))
    canvas.restoreState()


def _limit_error(
    phase: str,
    resource: str,
    actual: int,
    limit: int,
) -> ArtifactError:
    return ArtifactError(
        "artifact_limit_exceeded",
        phase,
        "Artifact PDF resources exceed the configured limit.",
        details={
            "resource": resource,
            "actual": actual,
            "limit": limit,
        },
    )


def _validation_issue(
    error_code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        error_code=error_code,
        phase="validate",
        message=message,
        details=details or {},
    )
