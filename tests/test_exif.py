from __future__ import annotations

from datetime import datetime
from pathlib import Path

import piexif
import pytest
from PIL import Image

from littlems.exif import DEFAULT_GPS, extract_photo_metadata


def _write_image(path: Path, exif_dict: dict | None = None) -> None:
    image = Image.new("RGB", (8, 8), color="white")
    if exif_dict is None:
        image.save(path, format="JPEG")
        return
    image.save(path, format="JPEG", exif=piexif.dump(exif_dict))


def _gps_rational(value: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60 * 1_000_000)
    return ((degrees, 1), (minutes, 1), (seconds, 1_000_000))


class _EmptyExifImage:
    def __enter__(self) -> _EmptyExifImage:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def getexif(self) -> dict[object, object]:
        return {}


def test_extract_photo_metadata_prefers_exif_and_parses_fields(tmp_path: Path) -> None:
    photo = tmp_path / "with-exif.jpg"
    exif_dict = {
        "0th": {
            piexif.ImageIFD.Make: "Apple",
            piexif.ImageIFD.Model: "iPhone 15 Pro",
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: "2025:01:02 10:11:12",
            piexif.ExifIFD.OffsetTimeOriginal: "+08:00",
        },
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: "N",
            piexif.GPSIFD.GPSLatitude: _gps_rational(30.346701),
            piexif.GPSIFD.GPSLongitudeRef: "E",
            piexif.GPSIFD.GPSLongitude: _gps_rational(120.002066),
        },
    }
    _write_image(photo, exif_dict)

    metadata = extract_photo_metadata(photo)

    assert metadata.captured_at == "2025-01-02T10:11:12"
    assert metadata.timezone == "+08:00"
    assert metadata.device == {"make": "Apple", "model": "iPhone 15 Pro"}
    assert metadata.gps == {
        "latitude": pytest.approx(30.346701, abs=1e-6),
        "longitude": pytest.approx(120.002066, abs=1e-6),
    }
    assert metadata.location is None
    assert metadata.metadata_source["captured_at"] == "exif"
    assert metadata.metadata_source["gps"] == "exif"


def test_extract_photo_metadata_uses_default_gps_when_missing(tmp_path: Path) -> None:
    photo = tmp_path / "without-gps.jpg"
    exif_dict = {
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: "2025:01:03 10:11:12",
        }
    }
    _write_image(photo, exif_dict)

    metadata = extract_photo_metadata(photo)

    assert metadata.gps == DEFAULT_GPS
    assert metadata.metadata_source["gps"] == "default_gps"


def test_extract_photo_metadata_falls_back_to_filename_datetime(monkeypatch, tmp_path: Path) -> None:
    photo = tmp_path / "IMG_20260223_222426.dng"
    photo.write_bytes(b"raw")
    monkeypatch.setattr("littlems.exif.open_image", lambda path: _EmptyExifImage())

    metadata = extract_photo_metadata(photo)

    assert metadata.captured_at == "2026-02-23T22:24:26"
    assert metadata.timezone is not None
    assert metadata.metadata_source["captured_at"] == "file_name"
    assert metadata.metadata_source["timezone"] == "inferred_local"


def test_extract_photo_metadata_falls_back_to_file_timestamp(monkeypatch, tmp_path: Path) -> None:
    photo = tmp_path / "plain-name.dng"
    photo.write_bytes(b"raw")
    monkeypatch.setattr("littlems.exif.open_image", lambda path: _EmptyExifImage())

    target_datetime = datetime(2026, 3, 22, 12, 34, 56).astimezone()
    target_timestamp = target_datetime.timestamp()

    class FakeStat:
        st_ctime = target_timestamp
        st_birthtime = target_timestamp

    monkeypatch.setattr(Path, "stat", lambda self: FakeStat())

    metadata = extract_photo_metadata(photo)

    assert metadata.captured_at == "2026-03-22T12:34:56"
    assert metadata.timezone is not None
    assert metadata.metadata_source["captured_at"] == "file_timestamp"
    assert metadata.metadata_source["timezone"] == "file_timestamp"
