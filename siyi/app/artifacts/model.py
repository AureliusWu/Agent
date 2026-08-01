"""Format-neutral content model used by all Artifact Engine adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from app.artifacts.errors import ArtifactError

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
BlockKind: TypeAlias = Literal[
    "heading",
    "paragraph",
    "bullet_list",
    "numbered_list",
    "page_break",
    "table",
    "image",
]
_BLOCK_KINDS: frozenset[str] = frozenset(
    {
        "heading",
        "paragraph",
        "bullet_list",
        "numbered_list",
        "page_break",
        "table",
        "image",
    }
)


def _model_error(message: str, *, field_name: str) -> ArtifactError:
    return ArtifactError(
        "invalid_artifact_model",
        "normalize",
        message,
        details={"field": field_name},
    )


def _as_mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _model_error("Expected an object.", field_name=field_name)
    return value


def _as_tuple_of_strings(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _model_error("Expected a list of strings.", field_name=field_name)
    items = tuple(str(item) for item in value)
    if any(not item.strip() for item in items):
        raise _model_error("List items must not be empty.", field_name=field_name)
    return items


def _json_value(value: Any, *, field_name: str, depth: int = 0) -> JSONValue:
    if depth > 8:
        raise _model_error("Metadata nesting is too deep.", field_name=field_name)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item, field_name=field_name, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _json_value(item, field_name=field_name, depth=depth + 1)
            for item in value
        ]
    raise _model_error("Metadata must be JSON-compatible.", field_name=field_name)


@dataclass(frozen=True, slots=True)
class ArtifactImage:
    """A workspace-relative image reference."""

    source: str
    alt_text: str = ""
    width_px: int | None = None
    height_px: int | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise _model_error("Image source must not be empty.", field_name="image.source")
        for name, value in (("width_px", self.width_px), ("height_px", self.height_px)):
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise _model_error(
                    "Image dimensions must be positive integers.",
                    field_name=f"image.{name}",
                )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactImage":
        raw = _as_mapping(value, field_name="image")
        source = raw.get("source", raw.get("path", ""))
        return cls(
            source=str(source),
            alt_text=str(raw.get("alt_text", raw.get("alt", ""))),
            width_px=_optional_int(raw.get("width_px"), field_name="image.width_px"),
            height_px=_optional_int(raw.get("height_px"), field_name="image.height_px"),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "source": self.source,
            "alt_text": self.alt_text,
            "width_px": self.width_px,
            "height_px": self.height_px,
        }


@dataclass(frozen=True, slots=True)
class ArtifactTable:
    rows: tuple[tuple[str, ...], ...]
    header_rows: int = 1

    def __post_init__(self) -> None:
        if not self.rows:
            raise _model_error("Table must contain at least one row.", field_name="table.rows")
        width = len(self.rows[0])
        if width == 0 or any(len(row) != width for row in self.rows):
            raise _model_error(
                "Table rows must be non-empty and have equal widths.",
                field_name="table.rows",
            )
        if not 0 <= self.header_rows <= len(self.rows):
            raise _model_error(
                "header_rows is outside the table.",
                field_name="table.header_rows",
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactTable":
        raw = _as_mapping(value, field_name="table")
        rows_value = raw.get("rows")
        if (
            isinstance(rows_value, (str, bytes, bytearray))
            or not isinstance(rows_value, Sequence)
        ):
            raise _model_error("Expected a list of table rows.", field_name="table.rows")
        rows: list[tuple[str, ...]] = []
        for row in rows_value:
            if isinstance(row, (str, bytes, bytearray)) or not isinstance(row, Sequence):
                raise _model_error(
                    "Expected each table row to be a list.",
                    field_name="table.rows",
                )
            rows.append(tuple(str(cell) for cell in row))
        header_rows = raw.get("header_rows", 1)
        if not isinstance(header_rows, int):
            raise _model_error(
                "header_rows must be an integer.",
                field_name="table.header_rows",
            )
        return cls(rows=tuple(rows), header_rows=header_rows)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "rows": [list(row) for row in self.rows],
            "header_rows": self.header_rows,
        }


@dataclass(frozen=True, slots=True)
class ContentBlock:
    kind: BlockKind
    text: str = ""
    level: int | None = None
    items: tuple[str, ...] = ()
    table: ArtifactTable | None = None
    image: ArtifactImage | None = None

    def __post_init__(self) -> None:
        if self.kind not in _BLOCK_KINDS:
            raise _model_error("Unsupported block kind.", field_name="block.kind")
        if self.kind == "heading":
            if not self.text.strip():
                raise _model_error("Heading text must not be empty.", field_name="block.text")
            if self.level is None or not 1 <= self.level <= 6:
                raise _model_error(
                    "Heading level must be between 1 and 6.",
                    field_name="block.level",
                )
        elif self.kind == "paragraph" and not self.text:
            raise _model_error("Paragraph text must not be empty.", field_name="block.text")
        elif self.kind in {"bullet_list", "numbered_list"} and not self.items:
            raise _model_error("List items must not be empty.", field_name="block.items")
        elif self.kind == "table" and self.table is None:
            raise _model_error("Table block requires table data.", field_name="block.table")
        elif self.kind == "image" and self.image is None:
            raise _model_error("Image block requires image data.", field_name="block.image")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContentBlock":
        raw = _as_mapping(value, field_name="block")
        kind = str(raw.get("kind", raw.get("type", "paragraph"))).casefold()
        if kind not in _BLOCK_KINDS:
            raise _model_error("Unsupported block kind.", field_name="block.kind")
        level = _optional_int(raw.get("level"), field_name="block.level")
        table_value = raw.get("table")
        if table_value is None and kind == "table" and "rows" in raw:
            table_value = {"rows": raw["rows"], "header_rows": raw.get("header_rows", 1)}
        image_value = raw.get("image")
        if image_value is None and kind == "image" and ("source" in raw or "path" in raw):
            image_value = raw
        return cls(
            kind=kind,  # type: ignore[arg-type]
            text=str(raw.get("text", "")),
            level=level,
            items=_as_tuple_of_strings(raw.get("items"), field_name="block.items"),
            table=(
                ArtifactTable.from_dict(_as_mapping(table_value, field_name="block.table"))
                if table_value is not None
                else None
            ),
            image=(
                ArtifactImage.from_dict(_as_mapping(image_value, field_name="block.image"))
                if image_value is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "kind": self.kind,
            "text": self.text,
            "level": self.level,
            "items": list(self.items),
            "table": self.table.to_dict() if self.table else None,
            "image": self.image.to_dict() if self.image else None,
        }


@dataclass(frozen=True, slots=True)
class ArtifactSlide:
    title: str = ""
    blocks: tuple[ContentBlock, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactSlide":
        raw = _as_mapping(value, field_name="slide")
        blocks = _blocks_from_value(raw.get("blocks", ()), field_name="slide.blocks")
        return cls(title=str(raw.get("title", "")), blocks=blocks)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "title": self.title,
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(frozen=True, slots=True)
class ArtifactDocument:
    title: str = ""
    blocks: tuple[ContentBlock, ...] = ()
    slides: tuple[ArtifactSlide, ...] = ()
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = _json_value(self.metadata, field_name="metadata")
        if not isinstance(normalized, dict):
            raise _model_error("Metadata must be an object.", field_name="metadata")
        object.__setattr__(self, "metadata", normalized)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactDocument":
        raw = _as_mapping(value, field_name="document")
        slides_value = raw.get("slides", ())
        if (
            isinstance(slides_value, (str, bytes, bytearray))
            or not isinstance(slides_value, Sequence)
        ):
            raise _model_error("Expected a list of slides.", field_name="slides")
        slides = tuple(
            ArtifactSlide.from_dict(_as_mapping(slide, field_name="slides"))
            for slide in slides_value
        )
        metadata = _json_value(raw.get("metadata", {}), field_name="metadata")
        if not isinstance(metadata, dict):
            raise _model_error("Metadata must be an object.", field_name="metadata")
        return cls(
            title=str(raw.get("title", "")),
            blocks=_blocks_from_value(raw.get("blocks", ()), field_name="blocks"),
            slides=slides,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "title": self.title,
            "blocks": [block.to_dict() for block in self.blocks],
            "slides": [slide.to_dict() for slide in self.slides],
            "metadata": dict(self.metadata),
        }


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _model_error("Expected an integer.", field_name=field_name)
    return value


def _blocks_from_value(value: Any, *, field_name: str) -> tuple[ContentBlock, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _model_error("Expected a list of blocks.", field_name=field_name)
    return tuple(
        ContentBlock.from_dict(_as_mapping(block, field_name=field_name))
        for block in value
    )


def normalize_document(
    value: ArtifactDocument | Mapping[str, Any],
) -> ArtifactDocument:
    if isinstance(value, ArtifactDocument):
        return value
    return ArtifactDocument.from_dict(_as_mapping(value, field_name="document"))


def document_to_dict(
    value: ArtifactDocument | Mapping[str, Any],
) -> dict[str, JSONValue]:
    return normalize_document(value).to_dict()


@dataclass(frozen=True, slots=True)
class TextReplacement:
    old: str
    new: str
    expected_count: int = 1

    def __post_init__(self) -> None:
        if not self.old:
            raise _model_error(
                "Replacement source must not be empty.",
                field_name="replacement.old",
            )
        if not isinstance(self.expected_count, int) or self.expected_count < 1:
            raise _model_error(
                "Replacement expected_count must be a positive integer.",
                field_name="replacement.expected_count",
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TextReplacement":
        raw = _as_mapping(value, field_name="replacement")
        expected_count = raw.get("expected_count", 1)
        if not isinstance(expected_count, int):
            raise _model_error(
                "Replacement expected_count must be an integer.",
                field_name="replacement.expected_count",
            )
        return cls(
            old=str(raw.get("old", "")),
            new=str(raw.get("new", "")),
            expected_count=expected_count,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "old": self.old,
            "new": self.new,
            "expected_count": self.expected_count,
        }


def normalize_replacements(
    value: (
        Mapping[str, str]
        | Sequence[TextReplacement | Mapping[str, Any]]
    ),
) -> tuple[TextReplacement, ...]:
    if isinstance(value, Mapping):
        replacements = tuple(
            TextReplacement(old=str(old), new=str(new))
            for old, new in value.items()
        )
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        replacements = tuple(
            item
            if isinstance(item, TextReplacement)
            else TextReplacement.from_dict(
                _as_mapping(item, field_name="replacements")
            )
            for item in value
        )
    else:
        raise _model_error(
            "Expected replacement objects or an old-to-new mapping.",
            field_name="replacements",
        )
    if not replacements:
        raise _model_error(
            "At least one text replacement is required.",
            field_name="replacements",
        )
    return replacements


def _replace_value(value: str, replacement: TextReplacement) -> tuple[str, int]:
    count = value.count(replacement.old)
    return value.replace(replacement.old, replacement.new), count


def _replace_block(
    block: ContentBlock,
    replacement: TextReplacement,
) -> tuple[ContentBlock, int]:
    text, count = _replace_value(block.text, replacement)
    items: list[str] = []
    for item in block.items:
        replaced, item_count = _replace_value(item, replacement)
        items.append(replaced)
        count += item_count
    table = block.table
    if table is not None:
        rows: list[tuple[str, ...]] = []
        for row in table.rows:
            cells: list[str] = []
            for cell in row:
                replaced, cell_count = _replace_value(cell, replacement)
                cells.append(replaced)
                count += cell_count
            rows.append(tuple(cells))
        table = ArtifactTable(rows=tuple(rows), header_rows=table.header_rows)
    return (
        ContentBlock(
            kind=block.kind,
            text=text,
            level=block.level,
            items=tuple(items),
            table=table,
            image=block.image,
        ),
        count,
    )


def apply_text_replacements(
    document: ArtifactDocument | Mapping[str, Any],
    replacements: (
        Mapping[str, str]
        | Sequence[TextReplacement | Mapping[str, Any]]
    ),
) -> tuple[ArtifactDocument, tuple[int, ...]]:
    """Apply bounded exact replacements to the normalized visible text model."""

    current = normalize_document(document)
    counts: list[int] = []
    for replacement in normalize_replacements(replacements):
        title, count = _replace_value(current.title, replacement)
        blocks: list[ContentBlock] = []
        for block in current.blocks:
            replaced, block_count = _replace_block(block, replacement)
            blocks.append(replaced)
            count += block_count
        slides: list[ArtifactSlide] = []
        for slide in current.slides:
            slide_title, slide_title_count = _replace_value(
                slide.title, replacement
            )
            count += slide_title_count
            slide_blocks: list[ContentBlock] = []
            for block in slide.blocks:
                replaced, block_count = _replace_block(block, replacement)
                slide_blocks.append(replaced)
                count += block_count
            slides.append(
                ArtifactSlide(title=slide_title, blocks=tuple(slide_blocks))
            )
        if count != replacement.expected_count:
            raise ArtifactError(
                "artifact_replacement_count_mismatch",
                "edit",
                "Exact text replacement count did not match.",
                details={
                    "expected_count": replacement.expected_count,
                    "actual_count": count,
                },
            )
        counts.append(count)
        current = ArtifactDocument(
            title=title,
            blocks=tuple(blocks),
            slides=tuple(slides),
            metadata=dict(current.metadata),
        )
    return current, tuple(counts)
