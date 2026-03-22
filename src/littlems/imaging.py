from __future__ import annotations

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
