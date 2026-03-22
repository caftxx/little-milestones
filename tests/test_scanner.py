from __future__ import annotations

from pathlib import Path

from littlems.scanner import scan_photo_paths


def test_scan_photo_paths_includes_dng_heif_and_heic(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("b.heif", "a.dng", "c.jpg", "d.heic", "ignore.txt"):
        (photos / name).write_bytes(b"data")

    paths = scan_photo_paths(photos, recursive=False)

    assert [path.name for path in paths] == ["a.dng", "b.heif", "c.jpg", "d.heic"]
