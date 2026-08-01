from __future__ import annotations

import io
import math
from collections.abc import Sequence

import pypdfium2 as pdfium

from app.artifacts.errors import ArtifactError


MAX_RENDER_INPUT_BYTES = 64 * 1024 * 1024
MAX_RENDER_PAGES = 500
MAX_RENDER_DPI = 300
MAX_RENDER_PAGE_PIXELS = 40_000_000
MAX_RENDER_TOTAL_PIXELS = 120_000_000


def render_pdf_to_png(
    pdf_bytes: bytes | bytearray | memoryview,
    *,
    dpi: int = 144,
    page_numbers: Sequence[int] | None = None,
) -> tuple[bytes, ...]:
    """Render real PDF pages to PNG bytes with PDFium.

    Page numbers are one-based to match user-visible PDF page numbering. This
    function is pure in-memory and never writes render intermediates to disk.
    """

    payload = _bounded_payload(pdf_bytes)
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 36 <= dpi <= MAX_RENDER_DPI:
        raise ArtifactError(
            "artifact_invalid_render_options",
            "render",
            "PDF render DPI is outside the supported range.",
            details={"dpi": dpi, "minimum": 36, "maximum": MAX_RENDER_DPI},
        )

    document: pdfium.PdfDocument | None = None
    try:
        document = pdfium.PdfDocument(payload)
        page_count = len(document)
        if page_count > MAX_RENDER_PAGES:
            raise _limit_error("pages", page_count, MAX_RENDER_PAGES)
        selected = _normalize_page_numbers(page_numbers, page_count)
        scale = dpi / 72.0

        dimensions: list[tuple[int, int]] = []
        total_pixels = 0
        for page_number in selected:
            page = document[page_number - 1]
            try:
                width_points, height_points = page.get_size()
            finally:
                page.close()
            width_px = max(1, math.ceil(width_points * scale))
            height_px = max(1, math.ceil(height_points * scale))
            pixels = width_px * height_px
            if pixels > MAX_RENDER_PAGE_PIXELS:
                raise _limit_error(
                    "page_pixels",
                    pixels,
                    MAX_RENDER_PAGE_PIXELS,
                    page_number=page_number,
                )
            total_pixels += pixels
            if total_pixels > MAX_RENDER_TOTAL_PIXELS:
                raise _limit_error(
                    "total_pixels",
                    total_pixels,
                    MAX_RENDER_TOTAL_PIXELS,
                )
            dimensions.append((width_px, height_px))

        rendered: list[bytes] = []
        for page_number, expected_size in zip(selected, dimensions, strict=True):
            page = document[page_number - 1]
            bitmap = None
            image = None
            try:
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                if image.size != expected_size:
                    # PDFium can round fractional page dimensions differently.
                    actual_pixels = image.width * image.height
                    if actual_pixels > MAX_RENDER_PAGE_PIXELS:
                        raise _limit_error(
                            "page_pixels",
                            actual_pixels,
                            MAX_RENDER_PAGE_PIXELS,
                            page_number=page_number,
                        )
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=False)
                rendered.append(output.getvalue())
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
        return tuple(rendered)
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError(
            "artifact_pdf_render_failed",
            "render",
            "The PDF could not be rendered.",
            details={"reason": type(exc).__name__},
        ) from exc
    finally:
        if document is not None:
            document.close()


def render_pdf_pages(
    pdf_bytes: bytes | bytearray | memoryview,
    *,
    dpi: int = 144,
    page_numbers: Sequence[int] | None = None,
) -> tuple[bytes, ...]:
    """Compatibility alias for the generic artifact render service."""

    return render_pdf_to_png(pdf_bytes, dpi=dpi, page_numbers=page_numbers)


def _bounded_payload(value: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ArtifactError(
            "artifact_invalid_input",
            "render",
            "PDF content must be bytes.",
            details={"actual_type": type(value).__name__},
        )
    payload = bytes(value)
    if not payload:
        raise ArtifactError(
            "artifact_invalid_pdf",
            "render",
            "PDF content must not be empty.",
        )
    if len(payload) > MAX_RENDER_INPUT_BYTES:
        raise _limit_error("input_bytes", len(payload), MAX_RENDER_INPUT_BYTES)
    return payload


def _normalize_page_numbers(
    page_numbers: Sequence[int] | None,
    page_count: int,
) -> tuple[int, ...]:
    if page_numbers is None:
        return tuple(range(1, page_count + 1))
    if isinstance(page_numbers, (str, bytes, bytearray)) or not isinstance(
        page_numbers, Sequence
    ):
        raise ArtifactError(
            "artifact_invalid_render_options",
            "render",
            "Page numbers must be a list of one-based integers.",
        )
    selected: list[int] = []
    for value in page_numbers:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ArtifactError(
                "artifact_invalid_render_options",
                "render",
                "Page numbers must be one-based integers.",
            )
        if not 1 <= value <= page_count:
            raise ArtifactError(
                "artifact_page_out_of_range",
                "render",
                "A requested PDF page is outside the document.",
                details={"page_number": value, "page_count": page_count},
            )
        selected.append(value)
    if len(set(selected)) != len(selected):
        raise ArtifactError(
            "artifact_invalid_render_options",
            "render",
            "Duplicate PDF page numbers are not supported.",
        )
    return tuple(selected)


def _limit_error(
    resource: str,
    actual: int,
    limit: int,
    *,
    page_number: int | None = None,
) -> ArtifactError:
    details: dict[str, object] = {
        "resource": resource,
        "actual": actual,
        "limit": limit,
    }
    if page_number is not None:
        details["page_number"] = page_number
    return ArtifactError(
        "artifact_limit_exceeded",
        "render",
        "PDF render resources exceed the configured limit.",
        details=details,
    )
