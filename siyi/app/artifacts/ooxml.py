"""Shared secure OOXML package handling for DOCX and PPTX.

This module is intentionally format-adapter neutral.  It performs bounded ZIP
inspection, relationship validation, active-content rejection, image limits,
and embeds a normalized format-independent model in a private custom XML part.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import stat
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from lxml import etree

from app.artifacts.errors import ArtifactError
from app.artifacts.model import ArtifactDocument, document_to_dict, normalize_document

OOXMLFormat = Literal["docx", "pptx"]

_CONTENT_TYPES = "[Content_Types].xml"
_ROOT_RELS = "_rels/.rels"
_METADATA_PART = "customXml/siyi-artifact.xml"
_METADATA_NAMESPACE = "urn:siyi:artifact-engine:model:1"
_CUSTOM_XML_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
)
_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_RELATIONSHIPS_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_MAIN_PARTS: dict[OOXMLFormat, str] = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
}
_MAIN_CONTENT_TYPES: dict[OOXMLFormat, str] = {
    "docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    ),
    "pptx": (
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation.main+xml"
    ),
}
_ACTIVE_PATH_MARKERS = (
    "/activex/",
    "/embeddings/",
    "vbaproject",
    "vbadata",
)
_ACTIVE_REL_MARKERS = ("vbaproject", "activex", "oleobject")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_SAFE_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


@dataclass(frozen=True, slots=True)
class OOXMLLimits:
    max_package_bytes: int = 64 * 1024 * 1024
    max_entries: int = 2_048
    max_entry_bytes: int = 32 * 1024 * 1024
    max_uncompressed_bytes: int = 128 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_metadata_bytes: int = 4 * 1024 * 1024
    max_image_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 40_000_000


DEFAULT_OOXML_LIMITS = OOXMLLimits()


@dataclass(frozen=True, slots=True)
class OOXMLInspection:
    artifact_format: OOXMLFormat
    entry_count: int
    total_uncompressed_bytes: int
    max_observed_compression_ratio: float
    image_count: int
    has_engine_metadata: bool

    def to_dict(self) -> dict[str, str | int | float | bool]:
        return {
            "format": self.artifact_format,
            "entry_count": self.entry_count,
            "total_uncompressed_bytes": self.total_uncompressed_bytes,
            "max_observed_compression_ratio": round(
                self.max_observed_compression_ratio, 4
            ),
            "image_count": self.image_count,
            "has_engine_metadata": self.has_engine_metadata,
        }


def _security_error(
    error_code: str,
    message: str,
    *,
    phase: str = "inspect",
    details: dict[str, object] | None = None,
) -> ArtifactError:
    return ArtifactError(error_code, phase, message, details=details)


def _validate_format(expected_format: str) -> OOXMLFormat:
    normalized = expected_format.casefold().lstrip(".")
    if normalized not in _MAIN_PARTS:
        raise _security_error(
            "unsupported_artifact_format",
            "Only DOCX and PPTX OOXML packages are supported.",
            details={"format": normalized},
        )
    return normalized  # type: ignore[return-value]


def _safe_member_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise _security_error(
            "ooxml_unsafe_entry",
            "The OOXML package contains an unsafe entry name.",
        )
    if name.startswith("/") or _DRIVE_PATH.match(name):
        raise _security_error(
            "ooxml_zip_slip",
            "The OOXML package contains an absolute entry.",
        )
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _security_error(
            "ooxml_zip_slip",
            "The OOXML package contains a traversal entry.",
        )
    return path.as_posix()


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _parse_xml(raw: bytes, *, phase: str, member_kind: str) -> etree._Element:
    lowered = raw[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _security_error(
            "ooxml_xml_active_content",
            "DTD and entity declarations are not allowed in OOXML.",
            phase=phase,
            details={"member_kind": member_kind},
        )
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=False,
        remove_comments=False,
    )
    try:
        return etree.fromstring(raw, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise _security_error(
            "ooxml_malformed_xml",
            "The OOXML package contains malformed XML.",
            phase=phase,
            details={"member_kind": member_kind, "reason": type(exc).__name__},
        ) from None


def _relationship_source(rels_name: str) -> str:
    if rels_name == _ROOT_RELS:
        return ""
    marker = "/_rels/"
    if marker not in rels_name or not rels_name.endswith(".rels"):
        raise _security_error(
            "ooxml_invalid_relationships",
            "The OOXML relationship part has an invalid location.",
        )
    prefix, rel_name = rels_name.rsplit(marker, 1)
    source_name = rel_name.removesuffix(".rels")
    return posixpath.join(prefix, source_name)


def _resolved_relationship_target(rels_name: str, target: str) -> str:
    decoded = unquote(target).replace("\\", "/")
    if not decoded or "\x00" in decoded:
        raise _security_error(
            "ooxml_invalid_relationship",
            "An OOXML relationship has an empty or invalid target.",
        )
    split = urlsplit(decoded)
    if split.scheme or split.netloc or decoded.startswith("//") or _DRIVE_PATH.match(decoded):
        raise _security_error(
            "ooxml_external_relationship",
            "External OOXML relationships are not allowed.",
        )
    source = _relationship_source(rels_name)
    base = posixpath.dirname(source)
    if decoded.startswith("/"):
        resolved = posixpath.normpath(decoded.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(base, decoded))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise _security_error(
            "ooxml_relationship_escape",
            "An OOXML relationship escapes the package.",
        )
    return resolved


def _inspect_relationships(
    archive: zipfile.ZipFile,
    names: set[str],
    *,
    limits: OOXMLLimits,
) -> None:
    for rels_name in sorted(name for name in names if name.endswith(".rels")):
        info = archive.getinfo(rels_name)
        if info.file_size > limits.max_entry_bytes:
            raise _security_error(
                "ooxml_entry_too_large",
                "An OOXML relationship part exceeds the size limit.",
                details={"entry_kind": "relationships", "bytes": info.file_size},
            )
        root = _parse_xml(
            archive.read(rels_name),
            phase="relationships",
            member_kind="relationships",
        )
        for relationship in root.xpath(
            "//*[local-name()='Relationship']"
        ):
            target = str(relationship.get("Target", ""))
            target_mode = str(relationship.get("TargetMode", "")).casefold()
            rel_type = str(relationship.get("Type", "")).casefold()
            if target_mode == "external":
                raise _security_error(
                    "ooxml_external_relationship",
                    "External OOXML relationships are not allowed.",
                    phase="relationships",
                )
            if any(marker in rel_type for marker in _ACTIVE_REL_MARKERS):
                raise _security_error(
                    "ooxml_active_content",
                    "Active or embedded OOXML content is not allowed.",
                    phase="relationships",
                )
            resolved = _resolved_relationship_target(rels_name, target)
            if resolved not in names:
                raise _security_error(
                    "ooxml_missing_relationship_target",
                    "An OOXML relationship target is missing.",
                    phase="relationships",
                    details={"target_kind": PurePosixPath(resolved).suffix or "part"},
                )


def _inspect_content_types(
    archive: zipfile.ZipFile,
    names: set[str],
    artifact_format: OOXMLFormat,
) -> None:
    root = _parse_xml(
        archive.read(_CONTENT_TYPES),
        phase="content_types",
        member_kind="content_types",
    )
    content_types = {
        str(node.get("ContentType", ""))
        for node in root.xpath("//*[local-name()='Override' or local-name()='Default']")
    }
    if any(
        "macroenabled" in content_type.casefold()
        or "vbaproject" in content_type.casefold()
        for content_type in content_types
    ):
        raise _security_error(
            "ooxml_macro_not_allowed",
            "Macro-enabled OOXML packages are not allowed.",
            phase="content_types",
        )
    expected_type = _MAIN_CONTENT_TYPES[artifact_format]
    expected_part = f"/{_MAIN_PARTS[artifact_format]}"
    matches_main = any(
        str(node.get("PartName", "")) == expected_part
        and str(node.get("ContentType", "")) == expected_type
        for node in root.xpath("//*[local-name()='Override']")
    )
    if not matches_main:
        raise _security_error(
            "ooxml_missing_main_content_type",
            "The OOXML main document content type is missing.",
            phase="content_types",
        )
    if _MAIN_PARTS[artifact_format] not in names:
        raise _security_error(
            "ooxml_missing_main_document",
            "The OOXML main document part is missing.",
            phase="content_types",
        )


def _inspect_image(raw: bytes, *, limits: OOXMLLimits) -> None:
    if len(raw) > limits.max_image_bytes:
        raise _security_error(
            "ooxml_image_too_large",
            "An embedded image exceeds the byte limit.",
            phase="images",
            details={"bytes": len(raw)},
        )
    try:
        from PIL import Image

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > limits.max_image_pixels:
                    raise _security_error(
                        "ooxml_image_too_large",
                        "An embedded image exceeds the pixel limit.",
                        phase="images",
                        details={"pixels": max(width, 0) * max(height, 0)},
                    )
                image.verify()
    except ArtifactError:
        raise
    except Exception as exc:
        raise _security_error(
            "ooxml_invalid_image",
            "An embedded image cannot be decoded safely.",
            phase="images",
            details={"reason": type(exc).__name__},
        ) from None


def inspect_ooxml(
    package: bytes,
    expected_format: str,
    *,
    limits: OOXMLLimits = DEFAULT_OOXML_LIMITS,
) -> OOXMLInspection:
    """Inspect a DOCX/PPTX package without extracting it to disk."""

    artifact_format = _validate_format(expected_format)
    if not isinstance(package, bytes):
        raise _security_error(
            "invalid_artifact_bytes",
            "OOXML input must be bytes.",
            details={"input_type": type(package).__name__},
        )
    if not package or len(package) > limits.max_package_bytes:
        raise _security_error(
            "ooxml_package_size_limit",
            "The OOXML package size is outside the allowed range.",
            details={"bytes": len(package), "limit": limits.max_package_bytes},
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(package), mode="r")
    except (zipfile.BadZipFile, OSError):
        raise _security_error(
            "ooxml_invalid_zip",
            "The document is not a valid OOXML ZIP package.",
        ) from None
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise _security_error(
                "ooxml_entry_limit",
                "The OOXML package contains too many entries.",
                details={"entries": len(infos), "limit": limits.max_entries},
            )
        names: set[str] = set()
        names_casefold: set[str] = set()
        total = 0
        max_ratio = 1.0
        image_count = 0
        for info in infos:
            name = _safe_member_name(info.filename)
            folded = name.casefold()
            if name in names or folded in names_casefold:
                raise _security_error(
                    "ooxml_duplicate_entry",
                    "The OOXML package contains duplicate entries.",
                )
            names.add(name)
            names_casefold.add(folded)
            if info.flag_bits & 0x1:
                raise _security_error(
                    "ooxml_encrypted_entry",
                    "Encrypted OOXML entries are not supported.",
                )
            if _is_symlink(info):
                raise _security_error(
                    "ooxml_symlink_entry",
                    "Symbolic-link OOXML entries are not allowed.",
                )
            if info.file_size > limits.max_entry_bytes:
                raise _security_error(
                    "ooxml_entry_too_large",
                    "An OOXML entry exceeds the size limit.",
                    details={"bytes": info.file_size, "limit": limits.max_entry_bytes},
                )
            total += info.file_size
            if total > limits.max_uncompressed_bytes:
                raise _security_error(
                    "ooxml_expansion_limit",
                    "The OOXML package exceeds the expanded-size limit.",
                    details={"bytes": total, "limit": limits.max_uncompressed_bytes},
                )
            ratio = (
                float(info.file_size)
                if info.compress_size == 0 and info.file_size
                else info.file_size / max(info.compress_size, 1)
            )
            max_ratio = max(max_ratio, ratio)
            if ratio > limits.max_compression_ratio:
                raise _security_error(
                    "ooxml_compression_ratio_limit",
                    "An OOXML entry exceeds the compression-ratio limit.",
                    details={"ratio": round(ratio, 2)},
                )
            lowered_path = f"/{folded}"
            if any(marker in lowered_path for marker in _ACTIVE_PATH_MARKERS):
                raise _security_error(
                    "ooxml_active_content",
                    "Active or embedded OOXML content is not allowed.",
                )

        if _CONTENT_TYPES not in names:
            raise _security_error(
                "ooxml_missing_content_types",
                "The OOXML content-types part is missing.",
            )
        _inspect_content_types(archive, names, artifact_format)
        _inspect_relationships(archive, names, limits=limits)

        for name in sorted(names):
            lowered = name.casefold()
            if lowered.endswith((".xml", ".rels")):
                _parse_xml(
                    archive.read(name),
                    phase="xml",
                    member_kind=PurePosixPath(name).suffix.lstrip(".") or "xml",
                )
            if "/media/" in f"/{lowered}":
                image_count += 1
                _inspect_image(archive.read(name), limits=limits)

        return OOXMLInspection(
            artifact_format=artifact_format,
            entry_count=len(infos),
            total_uncompressed_bytes=total,
            max_observed_compression_ratio=max_ratio,
            image_count=image_count,
            has_engine_metadata=_METADATA_PART in names,
        )


def _metadata_xml(document: ArtifactDocument) -> bytes:
    payload = json.dumps(
        document_to_dict(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    root = etree.Element(f"{{{_METADATA_NAMESPACE}}}artifactModel", nsmap={None: _METADATA_NAMESPACE})
    root.set("schemaVersion", "1")
    model = etree.SubElement(root, f"{{{_METADATA_NAMESPACE}}}json")
    model.text = payload
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def _updated_content_types(raw: bytes) -> bytes:
    root = _parse_xml(raw, phase="metadata", member_kind="content_types")
    part_name = f"/{_METADATA_PART}"
    overrides = root.xpath("//*[local-name()='Override']")
    existing = next(
        (node for node in overrides if node.get("PartName") == part_name),
        None,
    )
    if existing is None:
        existing = etree.SubElement(
            root,
            f"{{{_CONTENT_TYPES_NAMESPACE}}}Override",
        )
        existing.set("PartName", part_name)
    existing.set("ContentType", "application/xml")
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def _updated_root_relationships(raw: bytes | None) -> bytes:
    if raw is None:
        root = etree.Element(
            f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationships",
            nsmap={None: _RELATIONSHIPS_NAMESPACE},
        )
    else:
        root = _parse_xml(raw, phase="metadata", member_kind="relationships")
    relationships = root.xpath("//*[local-name()='Relationship']")
    existing = next(
        (
            node
            for node in relationships
            if str(node.get("Type", "")) == _CUSTOM_XML_REL
            and str(node.get("Target", "")).replace("\\", "/")
            == _METADATA_PART
        ),
        None,
    )
    if existing is None:
        ids = {str(node.get("Id", "")) for node in relationships}
        rel_id = "rIdSiyiArtifact"
        index = 1
        while rel_id in ids:
            rel_id = f"rIdSiyiArtifact{index}"
            index += 1
        existing = etree.SubElement(
            root,
            f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationship",
        )
        existing.set("Id", rel_id)
    existing.set("Type", _CUSTOM_XML_REL)
    existing.set("Target", _METADATA_PART)
    existing.attrib.pop("TargetMode", None)
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def embed_model_metadata(
    package: bytes,
    expected_format: str,
    document: ArtifactDocument | dict[str, object],
    *,
    limits: OOXMLLimits = DEFAULT_OOXML_LIMITS,
) -> bytes:
    """Embed a canonical model part and return a new OOXML byte string."""

    artifact_format = _validate_format(expected_format)
    normalized = normalize_document(document)
    inspect_ooxml(package, artifact_format, limits=limits)
    metadata = _metadata_xml(normalized)
    if len(metadata) > limits.max_metadata_bytes:
        raise _security_error(
            "ooxml_metadata_size_limit",
            "The normalized model exceeds the metadata size limit.",
            phase="metadata",
            details={"bytes": len(metadata), "limit": limits.max_metadata_bytes},
        )

    source_buffer = io.BytesIO(package)
    output = io.BytesIO()
    with zipfile.ZipFile(source_buffer, mode="r") as source, zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as target:
        root_rels_raw = source.read(_ROOT_RELS) if _ROOT_RELS in source.namelist() else None
        replacements = {
            _CONTENT_TYPES: _updated_content_types(source.read(_CONTENT_TYPES)),
            _ROOT_RELS: _updated_root_relationships(root_rels_raw),
            _METADATA_PART: metadata,
        }
        for info in source.infolist():
            if info.filename in replacements:
                continue
            target.writestr(info, source.read(info.filename))
        for name, raw in replacements.items():
            target.writestr(name, raw)
    result = output.getvalue()
    inspection = inspect_ooxml(result, artifact_format, limits=limits)
    if not inspection.has_engine_metadata:
        raise _security_error(
            "ooxml_metadata_missing",
            "The normalized model metadata was not embedded.",
            phase="metadata",
        )
    return result


def read_model_metadata(
    package: bytes,
    expected_format: str,
    *,
    limits: OOXMLLimits = DEFAULT_OOXML_LIMITS,
) -> ArtifactDocument | None:
    """Read and validate the private normalized model part, if present."""

    inspection = inspect_ooxml(package, expected_format, limits=limits)
    if not inspection.has_engine_metadata:
        return None
    with zipfile.ZipFile(io.BytesIO(package), mode="r") as archive:
        raw = archive.read(_METADATA_PART)
    if len(raw) > limits.max_metadata_bytes:
        raise _security_error(
            "ooxml_metadata_size_limit",
            "The normalized model exceeds the metadata size limit.",
            phase="metadata",
        )
    root = _parse_xml(raw, phase="metadata", member_kind="engine_metadata")
    if root.tag != f"{{{_METADATA_NAMESPACE}}}artifactModel":
        raise _security_error(
            "ooxml_invalid_metadata",
            "The normalized model metadata has an invalid root.",
            phase="metadata",
        )
    if root.get("schemaVersion") != "1":
        raise _security_error(
            "ooxml_metadata_version",
            "The normalized model metadata version is unsupported.",
            phase="metadata",
        )
    nodes = root.xpath("./*[local-name()='json']")
    if len(nodes) != 1 or not nodes[0].text:
        raise _security_error(
            "ooxml_invalid_metadata",
            "The normalized model metadata payload is missing.",
            phase="metadata",
        )
    try:
        value = json.loads(nodes[0].text)
    except (json.JSONDecodeError, TypeError):
        raise _security_error(
            "ooxml_invalid_metadata",
            "The normalized model metadata payload is invalid.",
            phase="metadata",
        ) from None
    if not isinstance(value, dict):
        raise _security_error(
            "ooxml_invalid_metadata",
            "The normalized model metadata must be an object.",
            phase="metadata",
        )
    return normalize_document(value)


def resolve_workspace_image(
    image_source: str,
    workspace_root: Path | None,
    *,
    limits: OOXMLLimits = DEFAULT_OOXML_LIMITS,
) -> Path:
    """Resolve and validate an image without allowing workspace escape."""

    if workspace_root is None:
        raise _security_error(
            "artifact_image_workspace_required",
            "A workspace root is required for document images.",
            phase="images",
        )
    source = image_source.replace("\\", "/")
    if (
        not source
        or source.startswith("/")
        or source.startswith("//")
        or _DRIVE_PATH.match(source)
        or any(part in {"", ".", ".."} for part in PurePosixPath(source).parts)
    ):
        raise _security_error(
            "artifact_image_path_invalid",
            "The image path must be a safe workspace-relative path.",
            phase="images",
        )
    root = workspace_root.resolve(strict=True)
    candidate = (root / Path(*PurePosixPath(source).parts)).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise _security_error(
            "artifact_image_path_escape",
            "The image path escapes the selected workspace.",
            phase="images",
        ) from None
    if not candidate.is_file() or candidate.suffix.casefold() not in _SAFE_IMAGE_SUFFIXES:
        raise _security_error(
            "artifact_image_invalid",
            "The image must be a supported workspace file.",
            phase="images",
            details={"extension": candidate.suffix.casefold()},
        )
    _inspect_image(candidate.read_bytes(), limits=limits)
    return candidate
