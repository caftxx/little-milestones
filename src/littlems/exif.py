from __future__ import annotations

import logging
import re
from fractions import Fraction
from datetime import datetime
from pathlib import Path

from PIL import UnidentifiedImageError

from littlems.imaging import open_image
from littlems.models import PhotoMetadata


DEFAULT_GPS = {"latitude": 30.346701, "longitude": 120.002066}
logger = logging.getLogger(__name__)
_FILENAME_DATETIME_PATTERN = re.compile(
    r"(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})[_-]?(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
)


def extract_photo_metadata(image_path: Path) -> PhotoMetadata:
    try:
        with open_image(image_path) as image:
            exif = image.getexif()
            exif_ifd = exif.get_ifd(34665) if hasattr(exif, "get_ifd") else {}
            gps_ifd = exif.get_ifd(34853) if hasattr(exif, "get_ifd") else {}

            metadata = PhotoMetadata()

            captured_at = exif_ifd.get(36867) or exif.get(306)
            if captured_at:
                metadata.captured_at = str(captured_at).replace(":", "-", 2).replace(" ", "T", 1)
                metadata.metadata_source["captured_at"] = "exif"

            timezone = exif_ifd.get(36881)
            if timezone:
                metadata.timezone = str(timezone)
                metadata.metadata_source["timezone"] = "exif"

            _apply_datetime_fallbacks(metadata, image_path)

            make = exif.get(271)
            model = exif.get(272)
            if make or model:
                metadata.device = {
                    "make": str(make or "").strip(),
                    "model": str(model or "").strip(),
                }
                metadata.metadata_source["device"] = "exif"

            gps = _parse_gps(gps_ifd)
            if gps is None:
                metadata.gps = DEFAULT_GPS.copy()
                metadata.metadata_source["gps"] = "default_gps"
                logger.debug("gps missing in exif, using default gps image=%s", image_path)
            else:
                metadata.gps = gps
                metadata.metadata_source["gps"] = "exif"
                logger.debug("gps extracted from exif image=%s gps=%s", image_path, gps)

            logger.debug("exif extraction complete image=%s metadata_source=%s", image_path, metadata.metadata_source)
            return metadata
    except UnidentifiedImageError as exc:
        logger.warning("cannot decode image=%s", image_path)
        raise RuntimeError("cannot decode") from exc


def _apply_datetime_fallbacks(metadata: PhotoMetadata, image_path: Path) -> None:
    filename_datetime = _datetime_from_filename(image_path)
    if filename_datetime is not None:
        if metadata.captured_at is None:
            metadata.captured_at = filename_datetime.strftime("%Y-%m-%dT%H:%M:%S")
            metadata.metadata_source["captured_at"] = "file_name"
        if metadata.timezone is None:
            timezone = _format_utc_offset(filename_datetime)
            if timezone is not None:
                metadata.timezone = timezone
                metadata.metadata_source["timezone"] = "inferred_local"
        return

    file_datetime = _datetime_from_file_timestamp(image_path)
    if file_datetime is None:
        return
    if metadata.captured_at is None:
        metadata.captured_at = file_datetime.strftime("%Y-%m-%dT%H:%M:%S")
        metadata.metadata_source["captured_at"] = "file_timestamp"
    if metadata.timezone is None:
        timezone = _format_utc_offset(file_datetime)
        if timezone is not None:
            metadata.timezone = timezone
            metadata.metadata_source["timezone"] = "file_timestamp"


def _datetime_from_filename(image_path: Path) -> datetime | None:
    match = _FILENAME_DATETIME_PATTERN.search(image_path.stem)
    if match is None:
        return None
    try:
        naive = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
        )
    except ValueError:
        return None
    return naive.astimezone()


def _datetime_from_file_timestamp(image_path: Path) -> datetime | None:
    stat_result = image_path.stat()
    timestamp = getattr(stat_result, "st_birthtime", None)
    if timestamp is None:
        candidates = [
            value
            for value in (
                getattr(stat_result, "st_atime", None),
                getattr(stat_result, "st_mtime", None),
                getattr(stat_result, "st_ctime", None),
            )
            if value is not None
        ]
        if not candidates:
            return None
        timestamp = min(candidates)
    try:
        return datetime.fromtimestamp(timestamp).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def _format_utc_offset(value: datetime) -> str | None:
    offset = value.strftime("%z")
    if not offset:
        return None
    return f"{offset[:3]}:{offset[3:]}"


def _parse_gps(gps_ifd: dict[int, object]) -> dict[str, float] | None:
    latitude = _coordinates_to_decimal(
        gps_ifd.get(2),
        gps_ifd.get(1),
    )
    longitude = _coordinates_to_decimal(
        gps_ifd.get(4),
        gps_ifd.get(3),
    )
    if latitude is None or longitude is None:
        return None
    return {
        "latitude": latitude,
        "longitude": longitude,
    }


def _coordinates_to_decimal(value: object, ref: object) -> float | None:
    if not value or not ref:
        return None
    degrees, minutes, seconds = value
    decimal = float(_as_fraction(degrees))
    decimal += float(_as_fraction(minutes)) / 60
    decimal += float(_as_fraction(seconds)) / 3600
    if str(ref).upper() in {"S", "W"}:
        decimal *= -1
    return round(decimal, 6)


def _as_fraction(value: object) -> Fraction:
    if isinstance(value, tuple):
        numerator, denominator = value
        return Fraction(numerator, denominator)
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return Fraction(value.numerator, value.denominator)
    return Fraction(str(value))
