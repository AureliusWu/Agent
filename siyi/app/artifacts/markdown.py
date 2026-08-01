from __future__ import annotations

import re
from collections.abc import Sequence

from app.artifacts.errors import ArtifactError
from app.artifacts.model import (
    ArtifactDocument,
    ArtifactImage,
    ArtifactTable,
    ContentBlock,
)


MAX_MARKDOWN_CHARS = 2_000_000
MAX_MARKDOWN_LINES = 50_000
MAX_CONTENT_BLOCKS = 10_000
MAX_TABLE_CELLS = 100_000

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_UNORDERED_RE = re.compile(r"^[ \t]{0,3}[-+*][ \t]+(.+?)\s*$")
_ORDERED_RE = re.compile(r"^[ \t]{0,3}\d+[.)][ \t]+(.+?)\s*$")
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'].*[\"'])?\)\s*$")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HTML_TAG_RE = re.compile(r"<[^>\n]+>")


def markdown_to_document(markdown: str, *, title: str | None = None) -> ArtifactDocument:
    """Parse bounded Markdown into the format-neutral artifact model.

    The parser intentionally implements a conservative Markdown subset. Unsupported
    syntax is retained as paragraph text instead of being evaluated as HTML.
    """

    if not isinstance(markdown, str):
        raise _error(
            "artifact_invalid_input",
            "parse",
            "Markdown content must be text.",
            {"actual_type": type(markdown).__name__},
        )
    if len(markdown) > MAX_MARKDOWN_CHARS:
        raise _limit_error("characters", len(markdown), MAX_MARKDOWN_CHARS)

    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if len(lines) > MAX_MARKDOWN_LINES:
        raise _limit_error("lines", len(lines), MAX_MARKDOWN_LINES)

    blocks: list[ContentBlock] = []
    document_title = (title or "").strip()
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            language = stripped[3:].strip()
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(fence):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code_text = "\n".join(code_lines)
            blocks.append(
                ContentBlock(
                    kind="paragraph",
                    text=((f"[{language}]\n" if language else "") + code_text)
                    or "\u200b",
                )
            )
            _ensure_block_limit(blocks)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            text = _plain_inline(heading.group(2))
            if level == 1 and not document_title:
                document_title = text
            blocks.append(ContentBlock(kind="heading", text=text, level=level))
            _ensure_block_limit(blocks)
            index += 1
            continue

        image = _IMAGE_RE.match(stripped)
        if image:
            blocks.append(
                ContentBlock(
                    kind="image",
                    image=ArtifactImage(
                        source=image.group(2),
                        alt_text=_plain_inline(image.group(1)),
                    ),
                )
            )
            _ensure_block_limit(blocks)
            index += 1
            continue

        if index + 1 < len(lines) and _looks_like_table_row(line):
            if _TABLE_SEPARATOR_RE.match(lines[index + 1]):
                table_lines = [line]
                index += 2
                while index < len(lines) and _looks_like_table_row(lines[index]):
                    table_lines.append(lines[index])
                    index += 1
                rows = tuple(_parse_table_row(item) for item in table_lines)
                _ensure_table_limit(rows)
                blocks.append(
                    ContentBlock(
                        kind="table",
                        table=ArtifactTable(rows=rows, header_rows=1),
                    )
                )
                _ensure_block_limit(blocks)
                continue

        unordered = _UNORDERED_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if unordered or ordered:
            expression = _UNORDERED_RE if unordered else _ORDERED_RE
            items: list[str] = []
            while index < len(lines):
                match = expression.match(lines[index])
                if match is None:
                    break
                items.append(_plain_inline(match.group(1)))
                index += 1
            blocks.append(
                ContentBlock(
                    kind="numbered_list" if ordered else "bullet_list",
                    items=tuple(items),
                )
            )
            _ensure_block_limit(blocks)
            continue

        if stripped in {"---", "***", "___"}:
            blocks.append(ContentBlock(kind="paragraph", text="────────"))
            _ensure_block_limit(blocks)
            index += 1
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip() or _starts_new_block(lines, index):
                break
            paragraph_lines.append(candidate.strip())
            index += 1
        blocks.append(
            ContentBlock(
                kind="paragraph",
                text=_plain_inline(" ".join(paragraph_lines)),
            )
        )
        _ensure_block_limit(blocks)

    return ArtifactDocument(
        title=document_title,
        blocks=tuple(blocks),
        metadata={"source_format": "markdown"},
    )


def create_markdown_document(markdown: str, *, title: str | None = None) -> ArtifactDocument:
    """Compatibility alias used by registry/service adapters."""

    return markdown_to_document(markdown, title=title)


def parse_markdown(markdown: str, *, title: str | None = None) -> ArtifactDocument:
    """Explicit parser alias for callers that distinguish parse from create."""

    return markdown_to_document(markdown, title=title)


def _starts_new_block(lines: Sequence[str], index: int) -> bool:
    value = lines[index]
    stripped = value.strip()
    if (
        _HEADING_RE.match(value)
        or _IMAGE_RE.match(stripped)
        or _UNORDERED_RE.match(value)
        or _ORDERED_RE.match(value)
        or stripped.startswith(("```", "~~~"))
        or stripped in {"---", "***", "___"}
    ):
        return True
    return (
        index + 1 < len(lines)
        and _looks_like_table_row(value)
        and _TABLE_SEPARATOR_RE.match(lines[index + 1]) is not None
    )


def _looks_like_table_row(line: str) -> bool:
    return "|" in line and bool(line.strip().strip("|").strip())


def _parse_table_row(line: str) -> tuple[str, ...]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return tuple(_plain_inline(cell.strip()) for cell in _split_unescaped_pipes(value))


def _split_unescaped_pipes(value: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current))
    return cells


def _plain_inline(value: str) -> str:
    """Return safe plain text while preserving user-visible inline content."""

    text = _LINK_RE.sub(lambda match: match.group(1), value)
    text = _INLINE_CODE_RE.sub(lambda match: match.group(1), text)
    text = _HTML_TAG_RE.sub("", text)
    text = re.sub(r"(?<!\\)(?:\*\*|__)(.+?)(?<!\\)(?:\*\*|__)", r"\1", text)
    text = re.sub(r"(?<!\\)(?:\*|_)(.+?)(?<!\\)(?:\*|_)", r"\1", text)
    return (
        text.replace(r"\*", "*")
        .replace(r"\_", "_")
        .replace(r"\[", "[")
        .replace(r"\]", "]")
        .strip()
    )


def _ensure_block_limit(blocks: Sequence[ContentBlock]) -> None:
    if len(blocks) > MAX_CONTENT_BLOCKS:
        raise _limit_error("blocks", len(blocks), MAX_CONTENT_BLOCKS)


def _ensure_table_limit(rows: Sequence[Sequence[str]]) -> None:
    cells = sum(len(row) for row in rows)
    if cells > MAX_TABLE_CELLS:
        raise _limit_error("table_cells", cells, MAX_TABLE_CELLS)


def _limit_error(resource: str, actual: int, limit: int) -> ArtifactError:
    return _error(
        "artifact_limit_exceeded",
        "parse",
        f"Markdown {resource} exceed the configured limit.",
        {"resource": resource, "actual": actual, "limit": limit},
    )


def _error(
    error_code: str,
    phase: str,
    message: str,
    details: dict[str, object],
) -> ArtifactError:
    return ArtifactError(error_code, phase, message, details=details)
