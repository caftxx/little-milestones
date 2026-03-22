from __future__ import annotations

import asyncio
import json
from pathlib import Path

from PIL import Image

from littlems.models import VisionDescription, VisionProvider, VisionResult
from littlems.service import PhotoDescriptionService


class FakeVisionClient:
    async def describe(self, image_path: Path, metadata: object) -> VisionResult:
        if image_path.name == "broken.jpg":
            raise RuntimeError("cannot decode")
        return VisionResult(
            provider=VisionProvider(
                name="provider-a",
                base_url="http://example.test/v1",
                model="test-model",
            ),
            description=VisionDescription(
                summary=f"summary for {image_path.name}",
                baby_present=True,
                actions=["smiling"],
                expressions=["happy"],
                scene="living room",
                objects=["toy"],
                highlights=["looked at camera"],
                uncertainty=None,
            ),
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
        provider_names=["provider-a", "provider-b"],
    )

    document = asyncio.run(service.describe_directory(photos, recursive=False))

    assert document["input"]["directory"] == str(photos.resolve())
    assert document["model"] == {
        "provider": "multi_provider_pool",
        "providers": ["provider-a", "provider-b"],
    }
    assert document["provider_stats"] == {
        "provider-a": {"processed": 2, "failed": 0},
        "provider-b": {"processed": 0, "failed": 0},
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
            "provider_name": None,
        }
    ]
    assert document["records"][0]["gps"] == {
        "latitude": 30.346701,
        "longitude": 120.002066,
    }
    assert document["records"][0]["provider_name"] == "provider-a"
    assert document["records"][0]["provider_model"] == "test-model"


def test_service_can_write_json_output(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (8, 8), color="white").save(photos / "sample.jpg", format="JPEG")
    output_path = tmp_path / "descriptions.json"

    service = PhotoDescriptionService(
        vision_client=FakeVisionClient(),
        provider_names=["provider-a"],
    )

    asyncio.run(service.describe_to_file(photos, output_path, recursive=False))

    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["model"] == {
        "provider": "multi_provider_pool",
        "providers": ["provider-a"],
    }
    assert written["provider_stats"] == {
        "provider-a": {"processed": 1, "failed": 0},
    }
    assert written["summary"]["processed"] == 1


def test_service_reports_progress_for_successes_and_failures(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.jpg", "broken.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")

    class MixedVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            if image_path.name == "broken.jpg":
                raise RuntimeError("boom")
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                description=VisionDescription(
                    summary="ok",
                    baby_present=True,
                    actions=[],
                    expressions=[],
                    scene="room",
                    objects=[],
                    highlights=[],
                    uncertainty=None,
                ),
            )

    service = PhotoDescriptionService(
        vision_client=MixedVisionClient(),
        provider_names=["provider-a"],
    )

    progress_events: list[tuple[int, int, str]] = []
    document = asyncio.run(
        service.describe_directory(
            photos,
            progress_callback=lambda processed, total, image_path: progress_events.append(
                (processed, total, image_path.name)
            ),
        )
    )

    assert document["summary"] == {"total_files": 2, "processed": 1, "failed": 1}
    assert document["provider_stats"] == {
        "provider-a": {"processed": 1, "failed": 0},
    }
    assert progress_events == [(1, 2, "a.jpg"), (2, 2, "broken.jpg")]


def test_service_handles_empty_directory_without_progress(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()

    service = PhotoDescriptionService(
        vision_client=FakeVisionClient(),
        provider_names=["provider-a"],
    )

    progress_events: list[tuple[int, int, str]] = []
    document = asyncio.run(
        service.describe_directory(
            photos,
            progress_callback=lambda processed, total, image_path: progress_events.append(
                (processed, total, image_path.name)
            ),
        )
    )

    assert document["summary"] == {"total_files": 0, "processed": 0, "failed": 0}
    assert document["provider_stats"] == {
        "provider-a": {"processed": 0, "failed": 0},
    }
    assert progress_events == []


def test_service_keeps_input_order_under_async_parallelism(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")

    class OutOfOrderVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            delays = {"a.jpg": 0.03, "b.jpg": 0.01, "c.jpg": 0.02}
            await asyncio.sleep(delays[image_path.name])
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                description=VisionDescription(
                    summary=image_path.name,
                    baby_present=True,
                    actions=[],
                    expressions=[],
                    scene="room",
                    objects=[],
                    highlights=[],
                    uncertainty=None,
                ),
            )

    service = PhotoDescriptionService(
        vision_client=OutOfOrderVisionClient(),
        provider_names=["provider-a"],
    )

    progress_events: list[tuple[int, int, str]] = []
    document = asyncio.run(
        service.describe_directory(
            photos,
            progress_callback=lambda processed, total, image_path: progress_events.append(
                (processed, total, image_path.name)
            ),
        )
    )

    assert [item["file_name"] for item in document["records"]] == ["a.jpg", "b.jpg", "c.jpg"]
    assert document["provider_stats"] == {
        "provider-a": {"processed": 3, "failed": 0},
    }
    assert progress_events == [(1, 3, "b.jpg"), (2, 3, "c.jpg"), (3, 3, "a.jpg")]
