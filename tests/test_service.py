from __future__ import annotations

import asyncio
import json
from pathlib import Path

from PIL import Image

from littlems.models import VisionDescription, VisionProvider, VisionProviderAttempt, VisionResult
from littlems.service import OUTPUT_VERSION, PhotoDescriptionService
from littlems.vision import VisionProviderFailure
from littlems.models import PhotoMetadata


def _attempt(
    provider_name: str,
    *,
    start_ms: int,
    end_ms: int,
    ok: bool,
    error: str | None = None,
) -> VisionProviderAttempt:
    return VisionProviderAttempt(
        provider_name=provider_name,
        elapsed_ms=end_ms - start_ms,
        ok=ok,
        error=error,
        started_at_monotonic_ns=start_ms * 1_000_000,
        finished_at_monotonic_ns=end_ms * 1_000_000,
    )


class FakeVisionClient:
    async def describe(self, image_path: Path, metadata: object) -> VisionResult:
        if image_path.name == "broken.jpg":
            raise RuntimeError("cannot decode")
        windows = {
            "a.jpg": (0, 12),
            "b.jpg": (12, 24),
            "sample.jpg": (0, 12),
        }
        start_ms, end_ms = windows[image_path.name]
        return VisionResult(
            provider=VisionProvider(
                name="provider-a",
                base_url="http://example.test/v1",
                model="test-model",
            ),
            provider_elapsed_ms=12,
            provider_attempts=[
                _attempt("provider-a", start_ms=start_ms, end_ms=end_ms, ok=True)
            ],
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
        "provider-a": {"processed": 2, "failed": 0, "wall_clock_ms": 24, "wall_clock_ms_avg": 12},
        "provider-b": {"processed": 0, "failed": 0, "wall_clock_ms": 0, "wall_clock_ms_avg": 0},
    }
    assert document["summary"]["total_files"] == 3
    assert document["summary"]["processed"] == 2
    assert document["summary"]["failed"] == 1
    assert isinstance(document["summary"]["wall_clock_ms"], int)
    assert document["summary"]["wall_clock_ms"] >= 0
    assert [item["file_name"] for item in document["records"]] == ["a.jpg", "b.jpg"]
    assert document["failures"] == [
        {
            "file_name": "broken.jpg",
            "file_path": str((photos / "broken.jpg").resolve()),
            "error": "cannot decode",
            "provider_name": None,
            "provider_elapsed_ms": 0,
            "provider_attempts": [],
        }
    ]
    assert document["records"][0]["gps"] == {
        "latitude": 30.346701,
        "longitude": 120.002066,
    }
    assert document["records"][0]["provider_name"] == "provider-a"
    assert document["records"][0]["provider_model"] == "test-model"
    assert document["records"][0]["provider_elapsed_ms"] == 12


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
    assert written["version"] == OUTPUT_VERSION
    assert written["status"] == "completed"
    assert written["model"] == {
        "provider": "multi_provider_pool",
        "providers": ["provider-a"],
    }
    assert written["provider_stats"] == {
        "provider-a": {"processed": 1, "failed": 0, "wall_clock_ms": 12, "wall_clock_ms_avg": 12},
    }
    assert written["summary"]["processed"] == 1
    assert written["summary"]["skipped"] == 0
    assert written["summary"]["remaining"] == 0
    assert isinstance(written["summary"]["wall_clock_ms"], int)
    assert written["run_state"]["completed_files"] == [str((photos / "sample.jpg").resolve())]
    assert written["run_state"]["failed_files"] == []


def test_service_builds_stats_for_mixed_supported_formats(monkeypatch, tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.heif", "b.dng", "skip.txt"):
        (photos / name).write_bytes(b"data")

    class MixedFormatVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            windows = {
                "a.heif": (0, 10),
                "b.dng": (10, 25),
            }
            start_ms, end_ms = windows[image_path.name]
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                provider_elapsed_ms=end_ms - start_ms,
                provider_attempts=[
                    _attempt("provider-a", start_ms=start_ms, end_ms=end_ms, ok=True)
                ],
                description=VisionDescription(
                    summary=f"summary for {image_path.name}",
                    baby_present=True,
                    actions=[],
                    expressions=[],
                    scene="room",
                    objects=[],
                    highlights=[],
                    uncertainty=None,
                ),
            )

    monkeypatch.setattr(
        "littlems.service.extract_photo_metadata",
        lambda image_path: PhotoMetadata(),
    )

    service = PhotoDescriptionService(
        vision_client=MixedFormatVisionClient(),
        provider_names=["provider-a"],
    )

    document = asyncio.run(service.describe_directory(photos, recursive=False))

    assert document["summary"]["total_files"] == 2
    assert document["summary"]["processed"] == 2
    assert document["summary"]["failed"] == 0
    assert [item["file_name"] for item in document["records"]] == ["a.heif", "b.dng"]
    assert document["provider_stats"] == {
        "provider-a": {"processed": 2, "failed": 0, "wall_clock_ms": 25, "wall_clock_ms_avg": 12},
    }


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
                provider_elapsed_ms=8,
                provider_attempts=[
                    _attempt("provider-a", start_ms=5, end_ms=13, ok=True)
                ],
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

    assert document["summary"]["total_files"] == 2
    assert document["summary"]["processed"] == 1
    assert document["summary"]["failed"] == 1
    assert isinstance(document["summary"]["wall_clock_ms"], int)
    assert document["provider_stats"] == {
        "provider-a": {"processed": 1, "failed": 0, "wall_clock_ms": 8, "wall_clock_ms_avg": 8},
    }
    assert [event[0] for event in progress_events] == [1, 2]
    assert {event[2] for event in progress_events} == {"a.jpg", "broken.jpg"}


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

    assert document["summary"]["total_files"] == 0
    assert document["summary"]["processed"] == 0
    assert document["summary"]["failed"] == 0
    assert isinstance(document["summary"]["wall_clock_ms"], int)
    assert document["provider_stats"] == {
        "provider-a": {"processed": 0, "failed": 0, "wall_clock_ms": 0, "wall_clock_ms_avg": 0},
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
                provider_elapsed_ms=5,
                provider_attempts=[
                    _attempt(
                        "provider-a",
                        start_ms={"a.jpg": 0, "b.jpg": 0, "c.jpg": 0}[image_path.name],
                        end_ms={"a.jpg": 30, "b.jpg": 10, "c.jpg": 20}[image_path.name],
                        ok=True,
                    )
                ],
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
        "provider-a": {"processed": 3, "failed": 0, "wall_clock_ms": 30, "wall_clock_ms_avg": 20},
    }
    assert progress_events == [(1, 3, "b.jpg"), (2, 3, "c.jpg"), (3, 3, "a.jpg")]


def test_service_accumulates_provider_elapsed_time_for_failure_attempts(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (8, 8), color="white").save(photos / "broken.jpg", format="JPEG")

    class FailureVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            raise VisionProviderFailure(
                image_path.name,
                [
                    _attempt("provider-a", start_ms=0, end_ms=9, ok=False, error="boom-a"),
                    _attempt("provider-b", start_ms=9, end_ms=20, ok=False, error="boom-b"),
                ],
            )

    service = PhotoDescriptionService(
        vision_client=FailureVisionClient(),
        provider_names=["provider-a", "provider-b"],
    )

    document = asyncio.run(service.describe_directory(photos, recursive=False))

    assert document["summary"]["total_files"] == 1
    assert document["summary"]["processed"] == 0
    assert document["summary"]["failed"] == 1
    assert document["provider_stats"] == {
        "provider-a": {"processed": 0, "failed": 0, "wall_clock_ms": 9, "wall_clock_ms_avg": 9},
        "provider-b": {"processed": 0, "failed": 1, "wall_clock_ms": 11, "wall_clock_ms_avg": 11},
    }
    assert document["failures"][0]["provider_name"] == "provider-b"
    assert document["failures"][0]["provider_elapsed_ms"] == 20


def test_service_reports_provider_wall_clock_for_overlapping_attempts(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.jpg", "b.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")

    class OverlappingVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            attempt_by_name = {
                "a.jpg": _attempt("provider-a", start_ms=0, end_ms=50, ok=True),
                "b.jpg": _attempt("provider-a", start_ms=10, end_ms=60, ok=True),
            }
            attempt = attempt_by_name[image_path.name]
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                provider_elapsed_ms=attempt.elapsed_ms,
                provider_attempts=[attempt],
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
        vision_client=OverlappingVisionClient(),
        provider_names=["provider-a"],
    )

    document = asyncio.run(service.describe_directory(photos, recursive=False))

    assert document["provider_stats"]["provider-a"] == {
        "processed": 2,
        "failed": 0,
        "wall_clock_ms": 60,
        "wall_clock_ms_avg": 50,
    }


def test_service_reports_top_level_wall_clock_for_parallel_run(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.jpg", "b.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")

    class SlowVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            await asyncio.sleep(0.03)
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                provider_elapsed_ms=30,
                provider_attempts=[_attempt("provider-a", start_ms=0, end_ms=30, ok=True)],
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
        vision_client=SlowVisionClient(),
        provider_names=["provider-a"],
    )

    document = asyncio.run(service.describe_directory(photos, recursive=False))

    assert document["summary"]["wall_clock_ms"] >= 20
    assert document["summary"]["wall_clock_ms"] < 80
    assert document["provider_stats"]["provider-a"]["wall_clock_ms"] == 30


def test_service_tracks_multiple_provider_windows_on_failure_retry(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (8, 8), color="white").save(photos / "sample.jpg", format="JPEG")

    class RetryVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            return VisionResult(
                provider=VisionProvider(
                    name="provider-b",
                    base_url="http://example.test/second/v1",
                    model="model-b",
                ),
                provider_elapsed_ms=20,
                provider_attempts=[
                    _attempt("provider-a", start_ms=0, end_ms=9, ok=False, error="boom-a"),
                    _attempt("provider-b", start_ms=12, end_ms=23, ok=True),
                ],
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
        vision_client=RetryVisionClient(),
        provider_names=["provider-a", "provider-b"],
    )

    document = asyncio.run(service.describe_directory(photos, recursive=False))

    assert document["provider_stats"] == {
        "provider-a": {"processed": 0, "failed": 0, "wall_clock_ms": 9, "wall_clock_ms_avg": 9},
        "provider-b": {"processed": 1, "failed": 0, "wall_clock_ms": 11, "wall_clock_ms_avg": 11},
    }


def test_service_writes_incremental_running_document(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.jpg", "b.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")
    output_path = tmp_path / "descriptions.json"

    class SlowVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            await asyncio.sleep(0.01 if image_path.name == "a.jpg" else 0.02)
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                provider_elapsed_ms=5,
                provider_attempts=[_attempt("provider-a", start_ms=0, end_ms=5, ok=True)],
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
        vision_client=SlowVisionClient(),
        provider_names=["provider-a"],
    )

    snapshots: list[dict[str, object]] = []

    def progress_callback(processed: int, total: int, image_path: Path) -> None:
        del processed, total, image_path
        snapshots.append(json.loads(output_path.read_text(encoding="utf-8")))

    asyncio.run(
        service.describe_to_file(
            photos,
            output_path,
            recursive=False,
            progress_callback=progress_callback,
        )
    )

    assert len(snapshots) == 2
    assert snapshots[0]["status"] == "running"
    assert snapshots[0]["summary"]["processed"] == 1
    assert snapshots[0]["summary"]["remaining"] == 1
    assert len(snapshots[0]["records"]) == 1
    final_document = json.loads(output_path.read_text(encoding="utf-8"))
    assert final_document["status"] == "completed"
    assert final_document["summary"]["processed"] == 2
    assert final_document["summary"]["remaining"] == 0


def test_service_resumes_and_skips_completed_files(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")
    output_path = tmp_path / "descriptions.json"

    calls: list[str] = []

    class ResumeVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            del metadata
            calls.append(image_path.name)
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                provider_elapsed_ms=5,
                provider_attempts=[_attempt("provider-a", start_ms=0, end_ms=5, ok=True)],
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

    existing_record = {
        "file_name": "a.jpg",
        "file_path": str((photos / "a.jpg").resolve()),
        "captured_at": None,
        "timezone": None,
        "location": None,
        "gps": None,
        "device": None,
        "summary": "a.jpg",
        "baby_present": True,
        "actions": [],
        "expressions": [],
        "scene": "room",
        "objects": [],
        "highlights": [],
        "uncertainty": None,
        "metadata_source": {},
        "provider_name": "provider-a",
        "provider_base_url": "http://example.test/v1",
        "provider_model": "test-model",
        "provider_elapsed_ms": 5,
        "provider_attempts": [{"provider_name": "provider-a", "elapsed_ms": 5, "ok": True, "error": None}],
    }
    output_path.write_text(
        json.dumps(
            {
                "version": OUTPUT_VERSION,
                "status": "running",
                "generated_at": "2026-03-22T00:00:00+00:00",
                "updated_at": "2026-03-22T00:00:00+00:00",
                "input": {"directory": str(photos.resolve()), "recursive": False},
                "model": {"provider": "multi_provider_pool", "providers": ["provider-a"]},
                "provider_stats": {
                    "provider-a": {"processed": 1, "failed": 0, "wall_clock_ms": 5, "wall_clock_ms_avg": 5}
                },
                "summary": {
                    "total_files": 3,
                    "processed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "remaining": 2,
                    "wall_clock_ms": 5,
                },
                "records": [existing_record],
                "failures": [],
                "run_state": {
                    "completed_files": [str((photos / "a.jpg").resolve())],
                    "failed_files": [],
                    "provider_metrics": {
                        "provider-a": {
                            "attempt_count": 1,
                            "attempt_wall_clock_ms_total": 5,
                            "wall_clock_ms_total": 5,
                        }
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    service = PhotoDescriptionService(
        vision_client=ResumeVisionClient(),
        provider_names=["provider-a"],
    )

    resume_state = service.inspect_resume_state(photos, output_path, recursive=False)
    assert resume_state.total_files == 3
    assert resume_state.skipped == 1
    assert resume_state.failed_to_retry == 0
    assert resume_state.pending == 2

    asyncio.run(service.describe_to_file(photos, output_path, recursive=False))

    assert sorted(calls) == ["b.jpg", "c.jpg"]
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["summary"]["skipped"] == 1
    assert [item["file_name"] for item in written["records"]] == ["a.jpg", "b.jpg", "c.jpg"]


def test_service_retries_failures_and_replaces_them_with_success(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    for name in ("a.jpg", "b.jpg"):
        Image.new("RGB", (8, 8), color="white").save(photos / name, format="JPEG")
    output_path = tmp_path / "descriptions.json"

    calls: list[str] = []

    class RetryFailedVisionClient:
        async def describe(self, image_path: Path, metadata: object) -> VisionResult:
            del metadata
            calls.append(image_path.name)
            return VisionResult(
                provider=VisionProvider(
                    name="provider-a",
                    base_url="http://example.test/v1",
                    model="test-model",
                ),
                provider_elapsed_ms=7,
                provider_attempts=[_attempt("provider-a", start_ms=0, end_ms=7, ok=True)],
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

    output_path.write_text(
        json.dumps(
            {
                "version": OUTPUT_VERSION,
                "status": "completed",
                "generated_at": "2026-03-22T00:00:00+00:00",
                "updated_at": "2026-03-22T00:00:00+00:00",
                "input": {"directory": str(photos.resolve()), "recursive": False},
                "model": {"provider": "multi_provider_pool", "providers": ["provider-a"]},
                "provider_stats": {
                    "provider-a": {"processed": 1, "failed": 1, "wall_clock_ms": 12, "wall_clock_ms_avg": 6}
                },
                "summary": {
                    "total_files": 2,
                    "processed": 1,
                    "failed": 1,
                    "skipped": 0,
                    "remaining": 0,
                    "wall_clock_ms": 12,
                },
                "records": [
                    {
                        "file_name": "a.jpg",
                        "file_path": str((photos / "a.jpg").resolve()),
                        "captured_at": None,
                        "timezone": None,
                        "location": None,
                        "gps": None,
                        "device": None,
                        "summary": "a.jpg",
                        "baby_present": True,
                        "actions": [],
                        "expressions": [],
                        "scene": "room",
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                        "metadata_source": {},
                        "provider_name": "provider-a",
                        "provider_base_url": "http://example.test/v1",
                        "provider_model": "test-model",
                        "provider_elapsed_ms": 5,
                        "provider_attempts": [{"provider_name": "provider-a", "elapsed_ms": 5, "ok": True, "error": None}],
                    }
                ],
                "failures": [
                    {
                        "file_name": "b.jpg",
                        "file_path": str((photos / "b.jpg").resolve()),
                        "error": "boom",
                        "provider_name": "provider-a",
                        "provider_elapsed_ms": 7,
                        "provider_attempts": [{"provider_name": "provider-a", "elapsed_ms": 7, "ok": False, "error": "boom"}],
                    }
                ],
                "run_state": {
                    "completed_files": [str((photos / "a.jpg").resolve())],
                    "failed_files": [str((photos / "b.jpg").resolve())],
                    "provider_metrics": {
                        "provider-a": {
                            "attempt_count": 2,
                            "attempt_wall_clock_ms_total": 12,
                            "wall_clock_ms_total": 12,
                        }
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    service = PhotoDescriptionService(
        vision_client=RetryFailedVisionClient(),
        provider_names=["provider-a"],
    )

    resume_state = service.inspect_resume_state(photos, output_path, recursive=False)
    assert resume_state.skipped == 1
    assert resume_state.failed_to_retry == 1
    assert resume_state.pending == 1

    asyncio.run(service.describe_to_file(photos, output_path, recursive=False))

    assert calls == ["b.jpg"]
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["status"] == "completed"
    assert written["failures"] == []
    assert [item["file_name"] for item in written["records"]] == ["a.jpg", "b.jpg"]


def test_service_rejects_resume_when_input_changes(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (8, 8), color="white").save(photos / "a.jpg", format="JPEG")
    output_path = tmp_path / "descriptions.json"
    output_path.write_text(
        json.dumps(
            {
                "version": OUTPUT_VERSION,
                "status": "running",
                "generated_at": "2026-03-22T00:00:00+00:00",
                "updated_at": "2026-03-22T00:00:00+00:00",
                "input": {"directory": str((tmp_path / "other").resolve()), "recursive": False},
                "model": {"provider": "multi_provider_pool", "providers": ["provider-a"]},
                "provider_stats": {"provider-a": {"processed": 0, "failed": 0, "wall_clock_ms": 0, "wall_clock_ms_avg": 0}},
                "summary": {"total_files": 0, "processed": 0, "failed": 0, "skipped": 0, "remaining": 0, "wall_clock_ms": 0},
                "records": [],
                "failures": [],
                "run_state": {"completed_files": [], "failed_files": [], "provider_metrics": {}},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    service = PhotoDescriptionService(
        vision_client=FakeVisionClient(),
        provider_names=["provider-a"],
    )

    try:
        service.inspect_resume_state(photos, output_path, recursive=False)
    except SystemExit as exc:
        assert "input directory does not match" in str(exc)
    else:
        raise AssertionError("Expected resume validation to fail")
