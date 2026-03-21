from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from littlems.models import VisionDescription
from littlems.service import PhotoDescriptionService


class FakeVisionClient:
    def describe(self, image_path: Path, metadata: object) -> VisionDescription:
        if image_path.name == "broken.jpg":
            raise RuntimeError("cannot decode")
        return VisionDescription(
            summary=f"summary for {image_path.name}",
            baby_present=True,
            actions=["smiling"],
            expressions=["happy"],
            scene="living room",
            objects=["toy"],
            highlights=["looked at camera"],
            uncertainty=None,
        )


def test_service_builds_output_document_with_stats(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("b.jpg", "a.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")
    (photos / "broken.jpg").write_bytes(b"fake-image")
    (photos / "note.txt").write_text("ignore me", encoding="utf-8")

    service = PhotoDescriptionService(
        vision_client=FakeVisionClient(),
        model_name="test-model",
        base_url="http://example.test/v1",
    )

    document = service.describe_directory(photos, recursive=False)

    assert document["input"]["directory"] == str(photos.resolve())
    assert document["model"] == {
        "provider": "openai_compatible",
        "name": "test-model",
        "base_url": "http://example.test/v1",
    }
    assert document["summary"]["total_files"] == 3
    assert document["summary"]["processed"] == 2
    assert document["summary"]["failed"] == 1
    assert [item["file_name"] for item in document["records"]] == ["a.jpg", "b.jpg"]
    assert document["failures"] == [
        {
            "file_name": "broken.jpg",
            "file_path": str((photos / "broken.jpg").resolve()),
            "error": "cannot decode",
        }
    ]
    assert document["records"][0]["gps"] == {
        "latitude": 30.346701,
        "longitude": 120.002066,
    }


def test_service_can_write_json_output(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (8, 8), color="white").save(photos / "sample.jpg", format="JPEG")
    output_path = tmp_path / "descriptions.json"

    service = PhotoDescriptionService(
        vision_client=FakeVisionClient(),
        model_name="test-model",
        base_url="http://example.test/v1",
    )

    service.describe_to_file(photos, output_path, recursive=False)

    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["model"] == {
        "provider": "openai_compatible",
        "name": "test-model",
        "base_url": "http://example.test/v1",
    }
    assert written["summary"]["processed"] == 1
