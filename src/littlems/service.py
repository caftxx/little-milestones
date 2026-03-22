from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from littlems.exif import extract_photo_metadata
from littlems.models import PhotoMetadata, VisionDescription
from littlems.scanner import scan_photo_paths

logger = logging.getLogger(__name__)


class VisionClient(Protocol):
    def describe(self, image_path: Path, metadata: PhotoMetadata) -> VisionDescription: ...


class ProgressCallback(Protocol):
    def __call__(self, processed: int, total: int, image_path: Path) -> None: ...


class PhotoDescriptionService:
    def __init__(
        self,
        vision_client: VisionClient,
        model_name: str = "local-vision-model",
        base_url: str | None = None,
    ) -> None:
        self._vision_client = vision_client
        self._model_name = model_name
        self._base_url = base_url

    def describe_directory(
        self,
        input_dir: Path,
        recursive: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, object]:
        logger.info("scanning input directory=%s recursive=%s", input_dir, recursive)
        paths = scan_photo_paths(input_dir, recursive=recursive)
        logger.info("found %s supported image files", len(paths))
        records: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        total = len(paths)

        for processed, image_path in enumerate(paths, start=1):
            logger.info("processing image=%s", image_path)
            try:
                metadata = extract_photo_metadata(image_path)
                logger.debug("metadata extracted image=%s metadata_source=%s", image_path, metadata.metadata_source)
                description = self._vision_client.describe(image_path, metadata)
                logger.debug("vision description complete image=%s summary=%s", image_path, description.summary)
                records.append(_build_record(image_path, metadata, description))
            except Exception as exc:
                logger.exception("failed to process image=%s", image_path)
                failures.append(
                    {
                        "file_name": image_path.name,
                        "file_path": str(image_path.resolve()),
                        "error": str(exc),
                    }
                )
            finally:
                if progress_callback is not None:
                    progress_callback(processed, total, image_path)

        document = {
            "generated_at": datetime.now(UTC).isoformat(),
            "input": {
                "directory": str(input_dir.resolve()),
                "recursive": recursive,
            },
            "model": {
                "provider": "openai_compatible",
                "base_url": self._base_url,
                "name": self._model_name,
            },
            "summary": {
                "total_files": len(paths),
                "processed": len(records),
                "failed": len(failures),
            },
            "records": records,
            "failures": failures,
        }
        logger.info(
            "describe_directory finished total=%s processed=%s failed=%s",
            len(paths),
            len(records),
            len(failures),
        )
        return document

    def describe_to_file(
        self,
        input_dir: Path,
        output_file: Path,
        recursive: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        document = self.describe_directory(
            input_dir,
            recursive=recursive,
            progress_callback=progress_callback,
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info("writing output json=%s", output_file)
        output_file.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug("output json written bytes=%s", output_file.stat().st_size)


def _build_record(
    image_path: Path,
    metadata: PhotoMetadata,
    description: VisionDescription,
) -> dict[str, object]:
    return {
        "file_name": image_path.name,
        "file_path": str(image_path.resolve()),
        "captured_at": metadata.captured_at,
        "timezone": metadata.timezone,
        "location": metadata.location,
        "gps": metadata.gps,
        "device": metadata.device,
        "summary": description.summary,
        "baby_present": description.baby_present,
        "actions": description.actions,
        "expressions": description.expressions,
        "scene": description.scene,
        "objects": description.objects,
        "highlights": description.highlights,
        "uncertainty": description.uncertainty,
        "metadata_source": metadata.metadata_source,
    }
