from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings
from app.sandbox import safe_path, workspace_root


SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP"}
TILE_SIZE = 1536
TILE_OVERLAP = 128


class ImageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedImage:
    image_id: str
    path_name: str
    sha256: str
    source_format: str
    source_bytes: int
    source_width: int
    source_height: int
    part_ids: tuple[str, ...]
    parts: tuple[dict[str, Any], ...]
    preprocessing: dict[str, Any]

    def public_metadata(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "name": self.path_name,
            "sha256": self.sha256,
            "source_format": self.source_format,
            "source_bytes": self.source_bytes,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "part_count": len(self.parts),
            "preprocessing": self.preprocessing,
        }


def _encode_clean_jpeg(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=92,
        optimize=True,
        progressive=False,
        exif=b"",
    )
    return output.getvalue()


def _data_part(content: bytes, detail: str) -> dict[str, Any]:
    encoded = base64.b64encode(content).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/jpeg;base64,{encoded}",
            "detail": "high" if detail == "tile" else "auto",
        },
    }


def _tile_boxes(width: int, height: int, limit: int) -> list[tuple[int, int, int, int]]:
    if limit <= 0 or (width <= settings.vision_max_dimension and height <= settings.vision_max_dimension):
        return []
    stride = TILE_SIZE - TILE_OVERLAP
    boxes: list[tuple[int, int, int, int]] = []
    for top in range(0, height, stride):
        for left in range(0, width, stride):
            right = min(width, left + TILE_SIZE)
            bottom = min(height, top + TILE_SIZE)
            left = max(0, right - TILE_SIZE)
            top = max(0, bottom - TILE_SIZE)
            box = (left, top, right, bottom)
            if box not in boxes:
                boxes.append(box)
            if len(boxes) >= limit:
                return boxes
    return boxes


def prepare_workspace_image(workspace: str, relative_path: str, image_id: str) -> PreparedImage:
    root = workspace_root(workspace)
    path = safe_path(root, relative_path, must_exist=True)
    if not path.is_file():
        raise ImageValidationError("图片路径不是文件")
    size = path.stat().st_size
    if size <= 0 or size > settings.vision_max_image_bytes:
        raise ImageValidationError(f"图片大小必须在 1 到 {settings.vision_max_image_bytes} 字节之间")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    Image.MAX_IMAGE_PIXELS = settings.vision_max_total_pixels
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            source_format = str(probe.format or "").upper()
            if source_format not in SUPPORTED_FORMATS:
                raise ImageValidationError("只支持 JPEG、PNG 和 WEBP 图片")
            if int(getattr(probe, "n_frames", 1)) != 1:
                raise ImageValidationError("不支持多帧或动画图片")
            source_width, source_height = probe.size
            if source_width <= 0 or source_height <= 0:
                raise ImageValidationError("图片尺寸无效")
            if source_width * source_height > settings.vision_max_total_pixels:
                raise ImageValidationError("图片总像素超过安全上限")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as loaded:
            oriented = ImageOps.exif_transpose(loaded).convert("RGB")
            preview = oriented.copy()
            preview.thumbnail(
                (settings.vision_max_dimension, settings.vision_max_dimension),
                Image.Resampling.LANCZOS,
            )
            part_ids = [image_id]
            parts: list[dict[str, Any]] = [_data_part(_encode_clean_jpeg(preview), "overview")]
            boxes = _tile_boxes(oriented.width, oriented.height, settings.vision_max_tiles)
            for index, box in enumerate(boxes, start=1):
                tile = oriented.crop(box)
                part_ids.append(f"{image_id}-tile-{index}")
                parts.append(_data_part(_encode_clean_jpeg(tile), "tile"))
    except Image.DecompressionBombError as exc:
        raise ImageValidationError("图片像素量触发解压安全限制") from exc
    except (Image.DecompressionBombWarning, UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, ImageValidationError):
            raise
        raise ImageValidationError("图片内容损坏或格式与签名不一致") from exc
    return PreparedImage(
        image_id=image_id,
        path_name=path.name,
        sha256=digest,
        source_format=source_format,
        source_bytes=size,
        source_width=source_width,
        source_height=source_height,
        part_ids=tuple(part_ids),
        parts=tuple(parts),
        preprocessing={
            "orientation_normalized": True,
            "metadata_removed": True,
            "output_format": "JPEG",
            "overview_size": [preview.width, preview.height],
            "tiled": bool(boxes),
            "tile_count": len(boxes),
        },
    )
