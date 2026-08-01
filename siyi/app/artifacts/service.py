"""Permission-aware orchestration for the unified Artifact Engine."""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from typing import Any

from app.artifacts.docx import create_docx, edit_docx
from app.artifacts.errors import ArtifactError
from app.artifacts.markdown import markdown_to_document
from app.artifacts.model import ArtifactDocument
from app.artifacts.pdf import create_pdf, extract_pdf_text, merge_pdfs, validate_pdf
from app.artifacts.pptx import create_pptx, edit_pptx
from app.artifacts.render import render_pdf_to_png
from app.artifacts.store import store_artifact
from app.artifacts.validation import (
    ValidationReport,
    validate_docx,
    validate_pptx,
)
from app.artifacts.ooxml import resolve_workspace_image
from app.sandbox import (
    FileVersionError,
    SandboxError,
    file_version_token,
    safe_path,
    workspace_root,
    write_workspace_binaries,
    write_workspace_binary,
)
from app.tools.registry import ToolValidationError, validate_arguments


ARTIFACT_TOOLS = frozenset(
    {
        "artifact.markdown.create",
        "artifact.docx.create",
        "artifact.docx.edit",
        "artifact.pdf.create",
        "artifact.pdf.merge",
        "artifact.pdf.extract",
        "artifact.pptx.create",
        "artifact.pptx.edit",
        "artifact.render",
        "artifact.validate",
    }
)
ARTIFACT_MUTATION_TOOLS = frozenset(
    ARTIFACT_TOOLS - {"artifact.pdf.extract", "artifact.validate"}
)

_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
}
_MAX_WORKSPACE_INPUT_BYTES = 256 * 1024 * 1024


def _ok(data: dict[str, Any], *, started: float) -> dict[str, Any]:
    return {
        "success": True,
        "status": "ok",
        "data": data,
        **data,
        "error_code": None,
        "error_message": None,
        "error": None,
        "retryable": False,
        "truncated": bool(data.get("truncated", False)),
        "metadata": {"duration_ms": round((time.perf_counter() - started) * 1000)},
    }


def _error(
    error_code: str,
    message: str,
    *,
    started: float,
    phase: str = "service",
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    safe = ArtifactError(
        error_code,
        phase,
        message,
        details=details,
    ).to_dict()
    return {
        "success": False,
        "status": "error",
        "data": {"phase": safe["phase"], "details": safe["details"]},
        "phase": safe["phase"],
        "details": safe["details"],
        "error_code": safe["error_code"],
        "error_message": safe["message"],
        "error": safe["message"],
        "retryable": retryable,
        "truncated": False,
        "metadata": {"duration_ms": round((time.perf_counter() - started) * 1000)},
    }


def _require_suffix(path: Path, expected: str | tuple[str, ...]) -> str:
    allowed = (expected,) if isinstance(expected, str) else expected
    suffix = path.suffix.casefold()
    if suffix not in allowed:
        raise ArtifactError(
            "artifact_extension_mismatch",
            "validate_input",
            "The artifact path does not use the required file extension.",
            details={"extension": suffix, "allowed_extensions": list(allowed)},
        )
    return suffix


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_workspace_bytes(
    root: Path,
    relative: str,
    *,
    suffix: str | tuple[str, ...],
) -> tuple[Path, bytes]:
    path = safe_path(root, relative, must_exist=True)
    _require_suffix(path, suffix)
    if not path.is_file():
        raise ArtifactError(
            "artifact_input_not_file",
            "read",
            "The artifact input must be a regular file.",
        )
    size = path.stat().st_size
    if size > _MAX_WORKSPACE_INPUT_BYTES:
        raise ArtifactError(
            "artifact_resource_limit_exceeded",
            "read",
            "The artifact input exceeds the configured size limit.",
            details={"resource": "input_bytes", "actual": size, "limit": _MAX_WORKSPACE_INPUT_BYTES},
        )
    return path, path.read_bytes()


def _preflight_output_path(root: Path, path: Path, *, create_only: bool) -> None:
    if not path.parent.is_dir():
        raise ArtifactError(
            "artifact_output_parent_missing",
            "write",
            "The artifact output parent directory does not exist.",
        )
    if path.exists() and not path.is_file():
        raise ArtifactError(
            "artifact_output_not_file",
            "write",
            "The artifact output path is not a regular file.",
        )
    if create_only and path.exists():
        raise ArtifactError(
            "file_exists",
            "write",
            "The artifact output file already exists.",
        )


def _require_expected_version(path: Path, expected: str) -> None:
    if not expected:
        raise FileVersionError("version_token_required", "Missing file version token")
    if file_version_token(path) != expected:
        raise FileVersionError("version_conflict", "File changed after it was read")


def _image_sources(document: ArtifactDocument) -> tuple[str, ...]:
    sources: list[str] = []
    for block in document.blocks:
        if block.image is not None:
            sources.append(block.image.source)
    for slide in document.slides:
        for block in slide.blocks:
            if block.image is not None:
                sources.append(block.image.source)
    return tuple(sources)


def _load_images(
    root: Path,
    document: ArtifactDocument,
    declared_paths: list[str] | None,
    *,
    require_declared: bool,
) -> dict[str, bytes]:
    sources = _image_sources(document)
    declared = {str(value).replace("\\", "/") for value in (declared_paths or [])}
    if require_declared:
        undeclared = sorted(
            source for source in sources if source.replace("\\", "/") not in declared
        )
        if undeclared:
            raise ArtifactError(
                "artifact_image_not_authorized",
                "images",
                "Every embedded image must be listed in image_paths.",
                details={"undeclared_images": [PurePosixPath(value).name for value in undeclared]},
            )
    candidates = set(sources) | set(declared)
    return {
        source: resolve_workspace_image(source, root).read_bytes()
        for source in candidates
    }


def _require_validation(report: ValidationReport) -> dict[str, Any]:
    payload = report.to_dict()
    if report.passed:
        return payload
    issue = report.errors[0] if report.errors else (report.risks[0] if report.risks else None)
    raise ArtifactError(
        issue.error_code if issue else "artifact_validation_failed",
        "validate",
        "The artifact failed independent validation.",
        details=issue.details if issue else {"format": report.format},
    )


def _store_runtime_copy(
    payload: bytes,
    *,
    media_type: str,
    filename: str,
    task_id: str | None,
    tool_call_id: str | None,
) -> dict[str, Any]:
    if not task_id or not tool_call_id:
        return {}
    try:
        stored = store_artifact(
            payload,
            task_id=task_id,
            tool_call_id=tool_call_id,
            media_type=media_type,
            filename=filename,
        )
    except Exception as exc:
        return {
            "artifact_registered": False,
            "artifact_registration_error": type(exc).__name__,
        }
    return {
        **stored,
        "artifact_registered": True,
        "download_url": f"/api/artifacts/{stored['artifact_id']}/raw",
    }


def _published(
    workspace: str,
    relative: str,
    payload: bytes,
    *,
    operation: str,
    create_only: bool,
    expected_version_token: str | None,
    validation: dict[str, Any],
    media_type: str,
    dry_run: bool,
    task_id: str | None,
    tool_call_id: str | None,
) -> dict[str, Any]:
    if dry_run:
        return {
            "dry_run": True,
            "operation": operation,
            "path": relative.replace("\\", "/"),
            "total_bytes": len(payload),
            "validation": validation,
        }
    written = write_workspace_binary(
        workspace,
        relative,
        payload,
        operation=operation,
        create_only=create_only,
        expected_version_token=expected_version_token,
        task_id=task_id,
        tool_call_id=tool_call_id,
    )
    return {
        **written["data"],
        "operation": operation,
        "validation": validation,
        **_store_runtime_copy(
            payload,
            media_type=media_type,
            filename=PurePosixPath(relative.replace("\\", "/")).name,
            task_id=task_id,
            tool_call_id=tool_call_id,
        ),
    }


def _validate_by_suffix(suffix: str, payload: bytes) -> ValidationReport:
    if suffix == ".docx":
        return validate_docx(payload)
    if suffix == ".pdf":
        return validate_pdf(payload)
    if suffix == ".pptx":
        return validate_pptx(payload)
    raise ArtifactError(
        "unsupported_artifact_format",
        "validate",
        "No validator is registered for this artifact format.",
        details={"extension": suffix},
    )


def validate_artifact_bytes(payload: bytes, artifact_format: str) -> dict[str, Any]:
    """Validate supported artifact bytes without reading or writing the workspace."""

    suffix = artifact_format.casefold()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    if suffix in {".md", ".markdown"}:
        try:
            content = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ArtifactError(
                "artifact_markdown_not_utf8",
                "validate",
                "Markdown artifacts must be valid UTF-8.",
                details={"reason": type(exc).__name__},
            ) from None
        model = markdown_to_document(content)
        return {
            "status": "PASS",
            "format": suffix.lstrip("."),
            "openable": True,
            "page_count": None,
            "slide_count": None,
            "count": len(model.blocks),
            "checks": ["utf8_decode", "bounded_markdown_parse"],
            "risks": [],
            "errors": [],
        }
    return _validate_by_suffix(suffix, payload).to_dict()


def _extract_pdf_result(
    payload: bytes,
    *,
    max_pages: int,
    max_chars: int,
) -> dict[str, Any]:
    extraction = extract_pdf_text(payload)
    pages: list[dict[str, Any]] = []
    remaining = max_chars
    truncated = extraction.page_count > max_pages
    for page in extraction.pages[:max_pages]:
        item = page.to_dict()
        text = str(item["text"])
        if len(text) > remaining:
            item["text"] = text[:remaining]
            item["text_truncated"] = True
            remaining = 0
            truncated = True
        else:
            item["text_truncated"] = False
            remaining -= len(text)
        pages.append(item)
        if remaining == 0:
            truncated = truncated or page.page_number < extraction.page_count
            break
    return {
        "format": "pdf",
        "page_count": extraction.page_count,
        "pages_returned": len(pages),
        "pages": pages,
        "ocr_performed": extraction.ocr_performed,
        "truncated": truncated,
    }


def execute_artifact_tool(
    workspace: str,
    name: str,
    arguments: dict[str, Any],
    *,
    task_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Execute one Artifact Engine operation after the caller authorizes it."""

    started = time.perf_counter()
    try:
        if name not in ARTIFACT_TOOLS:
            raise ArtifactError(
                "unknown_artifact_tool",
                "dispatch",
                "The Artifact Engine tool is not registered.",
            )
        validate_arguments(name, arguments)
        root = workspace_root(workspace)
        dry_run = bool(arguments.get("dry_run", False))

        if name == "artifact.markdown.create":
            path = safe_path(root, str(arguments["path"]))
            suffix = _require_suffix(path, (".md", ".markdown"))
            _preflight_output_path(root, path, create_only=True)
            content = str(arguments["content"])
            model = markdown_to_document(content, title=str(arguments.get("title") or ""))
            _load_images(
                root,
                model,
                [str(value) for value in arguments.get("image_paths") or []],
                require_declared=bool(_image_sources(model)),
            )
            payload = content.encode("utf-8")
            result = _published(
                workspace,
                _relative_path(root, path),
                payload,
                operation=name,
                create_only=True,
                expected_version_token=None,
                validation={
                    "status": "PASS",
                    "format": suffix.lstrip("."),
                    "openable": True,
                    "checks": ["utf8_encode", "bounded_markdown_parse"],
                },
                media_type=_MEDIA_TYPES[suffix],
                dry_run=dry_run,
                task_id=task_id,
                tool_call_id=tool_call_id,
            )
            return _ok(result, started=started)

        if name in {"artifact.docx.create", "artifact.pdf.create"}:
            expected_suffix = ".docx" if name == "artifact.docx.create" else ".pdf"
            path = safe_path(root, str(arguments["path"]))
            _require_suffix(path, expected_suffix)
            _preflight_output_path(root, path, create_only=True)
            model = markdown_to_document(
                str(arguments["content"]),
                title=str(arguments.get("title") or ""),
            )
            image_payloads = _load_images(
                root,
                model,
                [str(value) for value in arguments.get("image_paths") or []],
                require_declared=bool(_image_sources(model)),
            )
            if expected_suffix == ".docx":
                payload = create_docx(model, workspace_root=root)
                validation = _require_validation(validate_docx(payload))
            else:
                payload = create_pdf(
                    model,
                    image_loader=lambda source: image_payloads[source],
                )
                validation = _require_validation(validate_pdf(payload))
            result = _published(
                workspace,
                _relative_path(root, path),
                payload,
                operation=name,
                create_only=True,
                expected_version_token=None,
                validation=validation,
                media_type=_MEDIA_TYPES[expected_suffix],
                dry_run=dry_run,
                task_id=task_id,
                tool_call_id=tool_call_id,
            )
            return _ok(result, started=started)

        if name in {"artifact.docx.edit", "artifact.pptx.edit"}:
            expected_suffix = ".docx" if name == "artifact.docx.edit" else ".pptx"
            path, original = _read_workspace_bytes(
                root,
                str(arguments["path"]),
                suffix=expected_suffix,
            )
            _require_expected_version(
                path,
                str(arguments["expected_version_token"]),
            )
            replacements = arguments["replacements"]
            payload = (
                edit_docx(original, replacements)
                if expected_suffix == ".docx"
                else edit_pptx(original, replacements)
            )
            validation = _require_validation(_validate_by_suffix(expected_suffix, payload))
            result = _published(
                workspace,
                _relative_path(root, path),
                payload,
                operation=name,
                create_only=False,
                expected_version_token=str(arguments["expected_version_token"]),
                validation=validation,
                media_type=_MEDIA_TYPES[expected_suffix],
                dry_run=dry_run,
                task_id=task_id,
                tool_call_id=tool_call_id,
            )
            return _ok(result, started=started)

        if name == "artifact.pdf.merge":
            output_path = safe_path(root, str(arguments["path"]))
            _require_suffix(output_path, ".pdf")
            _preflight_output_path(root, output_path, create_only=True)
            inputs = [
                _read_workspace_bytes(root, str(value), suffix=".pdf")[1]
                for value in arguments["inputs"]
            ]
            payload = merge_pdfs(inputs)
            validation = _require_validation(validate_pdf(payload))
            result = _published(
                workspace,
                _relative_path(root, output_path),
                payload,
                operation=name,
                create_only=True,
                expected_version_token=None,
                validation=validation,
                media_type=_MEDIA_TYPES[".pdf"],
                dry_run=dry_run,
                task_id=task_id,
                tool_call_id=tool_call_id,
            )
            return _ok(result, started=started)

        if name == "artifact.pdf.extract":
            path, payload = _read_workspace_bytes(
                root,
                str(arguments["path"]),
                suffix=".pdf",
            )
            result = _extract_pdf_result(
                payload,
                max_pages=int(arguments.get("max_pages") or 500),
                max_chars=int(arguments.get("max_chars") or 40_000),
            )
            return _ok(
                {"path": _relative_path(root, path), **result},
                started=started,
            )

        if name == "artifact.pptx.create":
            path = safe_path(root, str(arguments["path"]))
            _require_suffix(path, ".pptx")
            _preflight_output_path(root, path, create_only=True)
            model = ArtifactDocument.from_dict(
                {
                    "title": str(arguments.get("title") or ""),
                    "slides": arguments["slides"],
                    "metadata": {"source_format": "structured_slides"},
                }
            )
            _load_images(root, model, None, require_declared=False)
            payload = create_pptx(model, workspace_root=root)
            validation = _require_validation(validate_pptx(payload))
            result = _published(
                workspace,
                _relative_path(root, path),
                payload,
                operation=name,
                create_only=True,
                expected_version_token=None,
                validation=validation,
                media_type=_MEDIA_TYPES[".pptx"],
                dry_run=dry_run,
                task_id=task_id,
                tool_call_id=tool_call_id,
            )
            return _ok(result, started=started)

        if name == "artifact.render":
            path, payload = _read_workspace_bytes(
                root,
                str(arguments["path"]),
                suffix=(".docx", ".pdf", ".pptx"),
            )
            suffix = path.suffix.casefold()
            if suffix != ".pdf":
                raise ArtifactError(
                    "artifact_native_renderer_unavailable",
                    "render",
                    "A trusted native renderer is not available for this artifact format.",
                    details={"format": suffix.lstrip("."), "supported_formats": ["pdf"]},
                )
            _require_validation(validate_pdf(payload))
            dpi = int(arguments.get("dpi") or 144)
            pages = render_pdf_to_png(payload, dpi=dpi)
            output_directory = safe_path(
                root,
                str(arguments["output_directory"]),
                must_exist=True,
            )
            if not output_directory.is_dir():
                raise ArtifactError(
                    "artifact_output_directory_invalid",
                    "render",
                    "The render output path must be an existing directory.",
                )
            relative_directory = PurePosixPath(_relative_path(root, output_directory))
            outputs = {
                str(
                    relative_directory
                    / f"{path.stem}-page-{index:03d}.png"
                ): page
                for index, page in enumerate(pages, start=1)
            }
            for relative in outputs:
                _preflight_output_path(
                    root,
                    safe_path(root, relative),
                    create_only=True,
                )
            if dry_run:
                result = {
                    "dry_run": True,
                    "operation": name,
                    "source_path": _relative_path(root, path),
                    "paths": list(outputs),
                    "page_count": len(outputs),
                    "dpi": dpi,
                    "total_bytes": sum(len(value) for value in outputs.values()),
                }
            else:
                written = write_workspace_binaries(
                    workspace,
                    outputs,
                    operation=name,
                    create_only=True,
                    task_id=task_id,
                    tool_call_id=tool_call_id,
                )
                result = {
                    **written["data"],
                    "operation": name,
                    "source_path": _relative_path(root, path),
                    "page_count": len(outputs),
                    "dpi": dpi,
                }
            return _ok(result, started=started)

        if name == "artifact.validate":
            path, payload = _read_workspace_bytes(
                root,
                str(arguments["path"]),
                suffix=(".docx", ".md", ".markdown", ".pdf", ".pptx"),
            )
            validation = validate_artifact_bytes(payload, path.suffix)
            data = {
                "path": _relative_path(root, path),
                "validation": validation,
            }
            if validation["status"] != "PASS":
                return _error(
                    "artifact_validation_failed",
                    "The artifact failed independent validation.",
                    started=started,
                    phase="validate",
                    details=data["validation"],
                )
            return _ok(data, started=started)

        raise ArtifactError(
            "unknown_artifact_tool",
            "dispatch",
            "The Artifact Engine tool is not registered.",
        )
    except ToolValidationError as exc:
        return _error(
            "invalid_arguments",
            str(exc),
            started=started,
            phase="validate_arguments",
        )
    except ArtifactError as exc:
        payload = exc.to_dict()
        return _error(
            str(payload["error_code"]),
            str(payload["message"]),
            started=started,
            phase=str(payload["phase"]),
            details=dict(payload["details"]),
        )
    except FileVersionError as exc:
        return _error(
            exc.code,
            str(exc),
            started=started,
            phase="write",
            retryable=exc.code == "version_conflict",
        )
    except SandboxError as exc:
        return _error(
            "artifact_workspace_error",
            "The artifact operation was rejected by the workspace sandbox.",
            started=started,
            phase="workspace",
            details={"reason": type(exc).__name__},
        )
    except OSError as exc:
        return _error(
            "artifact_io_error",
            "The artifact operation failed because of a local I/O error.",
            started=started,
            phase="io",
            details={
                "reason": type(exc).__name__,
                "errno": exc.errno,
                "winerror": getattr(exc, "winerror", None),
            },
            retryable=isinstance(exc, (PermissionError, BlockingIOError)),
        )
    except Exception as exc:
        return _error(
            "artifact_internal_error",
            "The artifact operation failed unexpectedly without publishing output.",
            started=started,
            phase="service",
            details={"reason": type(exc).__name__},
        )
