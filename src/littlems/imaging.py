from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image


logger = logging.getLogger(__name__)

_HEIF_OPENER_REGISTERED = False


def open_image(image_path: Path) -> Image.Image:
    register_optional_image_openers()
    if image_path.suffix.lower() == ".dng":
        return _open_dng_as_image(image_path)
    return Image.open(image_path)


def open_image_bytes(
    image_bytes: bytes,
    *,
    image_name: str | None = None,
    mime_type: str | None = None,
) -> Image.Image:
    register_optional_image_openers()
    if _is_dng_input(image_name=image_name, mime_type=mime_type):
        return _open_dng_bytes_as_image(image_bytes)
    return Image.open(io.BytesIO(image_bytes))


def register_optional_image_openers() -> None:
    global _HEIF_OPENER_REGISTERED

    if _HEIF_OPENER_REGISTERED:
        return

    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        logger.debug("pillow-heif is not installed; HEIF images may not be readable")
        return

    register_heif_opener()
    _HEIF_OPENER_REGISTERED = True
    logger.debug("registered pillow-heif opener")


def _open_dng_as_image(image_path: Path) -> Image.Image:
    try:
        import rawpy
    except ImportError as exc:
        raise RuntimeError("DNG support requires rawpy to be installed") from exc

    with rawpy.imread(str(image_path)) as raw:
        rgb = raw.postprocess()
    return Image.fromarray(rgb)


def _open_dng_bytes_as_image(image_bytes: bytes) -> Image.Image:
    try:
        import rawpy
    except ImportError as exc:
        raise RuntimeError("DNG support requires rawpy to be installed") from exc

    with rawpy.imread(io.BytesIO(image_bytes)) as raw:
        rgb = raw.postprocess()
    return Image.fromarray(rgb)


def _is_dng_input(*, image_name: str | None, mime_type: str | None) -> bool:
    normalized_name = (image_name or "").lower()
    normalized_mime_type = (mime_type or "").lower()
    return normalized_name.endswith(".dng") or normalized_mime_type in {"image/x-adobe-dng", "image/dng"}
