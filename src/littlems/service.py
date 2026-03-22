from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from littlems.exif import extract_photo_metadata
from littlems.models import PhotoMetadata, VisionProviderAttempt, VisionResult
from littlems.scanner import scan_photo_paths
from littlems.vision import VisionProviderFailure

logger = logging.getLogger(__name__)


OUTPUT_VERSION = 2
ProviderStats = dict[str, int]


@dataclass(slots=True)
class ResumeState:
    total_files: int
    skipped: int
    failed_to_retry: int
    pending: int


@dataclass(slots=True)
class ProviderMetricTotals:
    attempt_count: int = 0
    attempt_wall_clock_ms_total: int = 0
    wall_clock_ms_total: int = 0


@dataclass(slots=True)
class _RunState:
    input_dir: Path
    recursive: bool
    paths: list[Path]
    output_file: Path | None
    generated_at: str
    skipped_count: int
    prior_wall_clock_ms: int
    current_run_started_ns: int
    records_by_path: dict[str, dict[str, object]]
    failures_by_path: dict[str, dict[str, object]]
    completed_files: set[str]
    failed_files: set[str]
    provider_metrics_base: dict[str, ProviderMetricTotals]
    current_provider_windows: dict[str, ProviderStats]


class VisionClient(Protocol):
    async def describe(self, image_path: Path) -> VisionResult: ...


class ProgressCallback(Protocol):
    def __call__(self, processed: int, total: int, image_path: Path) -> None: ...


class PhotoDescriptionService:
    def __init__(
        self,
        vision_client: VisionClient,
        provider_names: list[str],
        max_workers: int = 16,
    ) -> None:
        self._vision_client = vision_client
        self._provider_names = provider_names
        self._max_workers = max_workers

    def inspect_resume_state(
        self,
        input_dir: Path,
        output_file: Path,
        recursive: bool = False,
    ) -> ResumeState:
        paths = scan_photo_paths(input_dir, recursive=recursive)
        state = self._prepare_run_state(
            input_dir=input_dir,
            paths=paths,
            recursive=recursive,
            output_file=output_file,
        )
        return ResumeState(
            total_files=len(paths),
            skipped=state.skipped_count,
            failed_to_retry=len(state.failed_files),
            pending=self._pending_count(state),
        )

    async def describe_directory(
        self,
        input_dir: Path,
        recursive: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, object]:
        logger.info("scanning input directory=%s recursive=%s", input_dir, recursive)
        paths = scan_photo_paths(input_dir, recursive=recursive)
        logger.info("found %s supported image files", len(paths))
        state = self._prepare_run_state(
            input_dir=input_dir,
            paths=paths,
            recursive=recursive,
            output_file=None,
        )
        await self._process_pending_paths(state, progress_callback=progress_callback)
        document = self._build_document(state, status="completed")
        logger.info(
            "describe_directory finished total=%s processed=%s failed=%s",
            len(paths),
            len(document["records"]),
            len(document["failures"]),
        )
        return document

    async def describe_to_file(
        self,
        input_dir: Path,
        output_file: Path,
        recursive: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        logger.info("scanning input directory=%s recursive=%s", input_dir, recursive)
        paths = scan_photo_paths(input_dir, recursive=recursive)
        logger.info("found %s supported image files", len(paths))
        state = self._prepare_run_state(
            input_dir=input_dir,
            paths=paths,
            recursive=recursive,
            output_file=output_file,
        )
        logger.info(
            "resume state total=%s skipped=%s failed_to_retry=%s pending=%s",
            len(paths),
            state.skipped_count,
            len(state.failed_files),
            self._pending_count(state),
        )
        self._write_document(state, status="running")
        await self._process_pending_paths(state, progress_callback=progress_callback)
        self._write_document(state, status="completed")
        logger.info("writing output json=%s", output_file)

    async def _process_pending_paths(
        self,
        state: _RunState,
        progress_callback: ProgressCallback | None,
    ) -> None:
        pending_paths = [path for path in state.paths if str(path.resolve()) not in state.completed_files]
        total = len(state.paths)
        if not pending_paths:
            return

        processed = 0
        merge_lock = asyncio.Lock()
        queue: asyncio.Queue[Path] = asyncio.Queue()
        for image_path in pending_paths:
            queue.put_nowait(image_path)

        worker_count = min(self._max_workers, len(pending_paths))

        async def worker() -> None:
            nonlocal processed
            while True:
                try:
                    image_path = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    image_path, record, failure, provider_attempts = await self._describe_one(image_path)
                    async with merge_lock:
                        processed += 1
                        _accumulate_provider_attempts(state.current_provider_windows, provider_attempts)
                        self._merge_result_into_state(state, image_path, record=record, failure=failure)
                        if state.output_file is not None:
                            self._write_document(state, status="running")
                        if progress_callback is not None:
                            progress_callback(state.skipped_count + processed, total, image_path)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await queue.join()
        await asyncio.gather(*workers)

    def _prepare_run_state(
        self,
        input_dir: Path,
        paths: list[Path],
        recursive: bool,
        output_file: Path | None,
    ) -> _RunState:
        generated_at = datetime.now(UTC).isoformat()
        records_by_path: dict[str, dict[str, object]] = {}
        failures_by_path: dict[str, dict[str, object]] = {}
        completed_files: set[str] = set()
        failed_files: set[str] = set()
        provider_metrics_base = {name: ProviderMetricTotals() for name in self._provider_names}
        prior_wall_clock_ms = 0

        if output_file is not None and output_file.exists():
            payload = self._load_existing_output(output_file)
            self._validate_resume_payload(payload, input_dir=input_dir, recursive=recursive)
            generated_at = str(payload["generated_at"])
            prior_wall_clock_ms = int(_int_from_mapping(payload.get("summary"), "wall_clock_ms"))
            records_by_path = _records_by_path(payload.get("records"))
            failures_by_path = _records_by_path(payload.get("failures"))
            run_state = _mapping(payload.get("run_state"), "run_state")
            completed_files = {str(item) for item in _list_of_strings(run_state.get("completed_files"), "run_state.completed_files")}
            failed_files = {str(item) for item in _list_of_strings(run_state.get("failed_files"), "run_state.failed_files")}
            completed_files |= set(records_by_path)
            failed_files |= set(failures_by_path)
            provider_metrics_base = self._load_provider_metrics(payload)

        valid_paths = {str(path.resolve()) for path in paths}
        records_by_path = {path: record for path, record in records_by_path.items() if path in valid_paths}
        failures_by_path = {path: failure for path, failure in failures_by_path.items() if path in valid_paths}
        completed_files &= valid_paths
        failed_files &= valid_paths
        failed_files -= completed_files

        return _RunState(
            input_dir=input_dir,
            recursive=recursive,
            paths=paths,
            output_file=output_file,
            generated_at=generated_at,
            skipped_count=len(completed_files),
            prior_wall_clock_ms=prior_wall_clock_ms,
            current_run_started_ns=time.perf_counter_ns(),
            records_by_path=records_by_path,
            failures_by_path=failures_by_path,
            completed_files=completed_files,
            failed_files=failed_files,
            provider_metrics_base=provider_metrics_base,
            current_provider_windows={name: _new_provider_stats() for name in self._provider_names},
        )

    def _load_existing_output(self, output_file: Path) -> dict[str, object]:
        try:
            payload = json.loads(output_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Output file is not valid JSON: {output_file}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit(f"Output file must contain a JSON object: {output_file}")
        return payload

    def _validate_resume_payload(
        self,
        payload: dict[str, object],
        input_dir: Path,
        recursive: bool,
    ) -> None:
        version = payload.get("version")
        if version != OUTPUT_VERSION:
            raise SystemExit(f"Output file version mismatch: expected {OUTPUT_VERSION}, got {version}")
        input_payload = _mapping(payload.get("input"), "input")
        expected_directory = str(input_dir.resolve())
        actual_directory = str(input_payload.get("directory"))
        if actual_directory != expected_directory:
            raise SystemExit(
                "Output file input directory does not match current run: "
                f"{actual_directory} != {expected_directory}"
            )
        actual_recursive = bool(input_payload.get("recursive"))
        if actual_recursive != recursive:
            raise SystemExit(
                "Output file recursive flag does not match current run: "
                f"{actual_recursive} != {recursive}"
            )
        model_payload = _mapping(payload.get("model"), "model")
        actual_providers = [str(item) for item in _list_of_strings(model_payload.get("providers"), "model.providers")]
        if actual_providers != self._provider_names:
            raise SystemExit(
                "Output file providers do not match current run: "
                f"{actual_providers} != {self._provider_names}"
            )

    def _load_provider_metrics(self, payload: dict[str, object]) -> dict[str, ProviderMetricTotals]:
        metrics = {name: ProviderMetricTotals() for name in self._provider_names}
        run_state = payload.get("run_state")
        if isinstance(run_state, dict):
            provider_metrics = run_state.get("provider_metrics")
            if isinstance(provider_metrics, dict):
                for name, raw in provider_metrics.items():
                    if not isinstance(raw, dict):
                        continue
                    metrics[str(name)] = ProviderMetricTotals(
                        attempt_count=_int_from_mapping(raw, "attempt_count"),
                        attempt_wall_clock_ms_total=_int_from_mapping(raw, "attempt_wall_clock_ms_total"),
                        wall_clock_ms_total=_int_from_mapping(raw, "wall_clock_ms_total"),
                    )
                return metrics

        for record in payload.get("records", []):
            if isinstance(record, dict):
                _accumulate_provider_metric_totals(metrics, record)
        for failure in payload.get("failures", []):
            if isinstance(failure, dict):
                _accumulate_provider_metric_totals(metrics, failure)
        provider_stats = payload.get("provider_stats")
        if isinstance(provider_stats, dict):
            for name, raw in provider_stats.items():
                if not isinstance(raw, dict):
                    continue
                metric = metrics.setdefault(str(name), ProviderMetricTotals())
                metric.wall_clock_ms_total = _int_from_mapping(raw, "wall_clock_ms")
        return metrics

    def _merge_result_into_state(
        self,
        state: _RunState,
        image_path: Path,
        *,
        record: dict[str, object] | None,
        failure: dict[str, object] | None,
    ) -> None:
        file_path = str(image_path.resolve())
        if record is not None:
            state.records_by_path[file_path] = record
            state.failures_by_path.pop(file_path, None)
            state.completed_files.add(file_path)
            state.failed_files.discard(file_path)
        if failure is not None:
            state.failures_by_path[file_path] = failure
            state.records_by_path.pop(file_path, None)
            state.completed_files.discard(file_path)
            state.failed_files.add(file_path)

    def _build_document(self, state: _RunState, status: str) -> dict[str, object]:
        provider_stats, provider_metrics = self._build_provider_stats(state)
        records = self._ordered_records(state.paths, state.records_by_path)
        failures = self._ordered_records(state.paths, state.failures_by_path)
        processed = len(records)
        failed = len(failures)
        total = len(state.paths)
        current_wall_clock_ms = _elapsed_ms_between(state.current_run_started_ns, time.perf_counter_ns())

        return {
            "version": OUTPUT_VERSION,
            "status": status,
            "generated_at": state.generated_at,
            "updated_at": datetime.now(UTC).isoformat(),
            "input": {
                "directory": str(state.input_dir.resolve()),
                "recursive": state.recursive,
            },
            "model": {
                "provider": "multi_provider_pool",
                "providers": self._provider_names,
            },
            "provider_stats": provider_stats,
            "summary": {
                "total_files": total,
                "processed": processed,
                "failed": failed,
                "skipped": state.skipped_count,
                "remaining": max(0, total - processed - failed),
                "wall_clock_ms": state.prior_wall_clock_ms + current_wall_clock_ms,
            },
            "records": records,
            "failures": failures,
            "run_state": {
                "completed_files": sorted(state.completed_files),
                "failed_files": sorted(state.failed_files),
                "provider_metrics": provider_metrics,
            },
        }

    def _build_provider_stats(
        self,
        state: _RunState,
    ) -> tuple[dict[str, ProviderStats], dict[str, dict[str, int]]]:
        current_snapshots = _snapshot_provider_windows(state.current_provider_windows)
        provider_names = set(self._provider_names)
        provider_names |= set(state.provider_metrics_base)
        provider_names |= set(current_snapshots)
        provider_names |= {str(record.get("provider_name")) for record in state.records_by_path.values() if record.get("provider_name")}
        provider_names |= {str(record.get("provider_name")) for record in state.failures_by_path.values() if record.get("provider_name")}

        provider_stats: dict[str, ProviderStats] = {}
        provider_metrics: dict[str, dict[str, int]] = {}
        for provider_name in sorted(provider_names):
            base = state.provider_metrics_base.get(provider_name, ProviderMetricTotals())
            current = current_snapshots.get(provider_name, _empty_provider_window_snapshot())
            attempt_count = base.attempt_count + int(current["attempt_count"])
            attempt_wall_clock_ms_total = base.attempt_wall_clock_ms_total + int(current["attempt_wall_clock_ms_total"])
            wall_clock_ms_total = base.wall_clock_ms_total + int(current["wall_clock_ms"])
            provider_metrics[provider_name] = {
                "attempt_count": attempt_count,
                "attempt_wall_clock_ms_total": attempt_wall_clock_ms_total,
                "wall_clock_ms_total": wall_clock_ms_total,
            }
            provider_stats[provider_name] = {
                "processed": sum(
                    1
                    for record in state.records_by_path.values()
                    if record.get("provider_name") == provider_name
                ),
                "failed": sum(
                    1
                    for failure in state.failures_by_path.values()
                    if failure.get("provider_name") == provider_name
                ),
                "wall_clock_ms": wall_clock_ms_total,
                "wall_clock_ms_avg": round(attempt_wall_clock_ms_total / attempt_count) if attempt_count else 0,
            }
        return provider_stats, provider_metrics

    def _ordered_records(
        self,
        paths: list[Path],
        records_by_path: dict[str, dict[str, object]],
    ) -> list[dict[str, object]]:
        ordered: list[dict[str, object]] = []
        for path in paths:
            resolved = str(path.resolve())
            record = records_by_path.get(resolved)
            if record is not None:
                ordered.append(record)
        return ordered

    def _pending_count(self, state: _RunState) -> int:
        return sum(1 for path in state.paths if str(path.resolve()) not in state.completed_files)

    def _write_document(self, state: _RunState, status: str) -> None:
        if state.output_file is None:
            return
        output_file = state.output_file
        output_file.parent.mkdir(parents=True, exist_ok=True)
        document = self._build_document(state, status=status)
        temp_file = output_file.with_name(f"{output_file.name}.tmp")
        payload = json.dumps(document, ensure_ascii=False, indent=2)
        payload_size = len(payload.encode("utf-8"))
        with temp_file.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_file.replace(output_file)
        logger.debug("output json written bytes=%s path=%s", payload_size, output_file)

    async def _describe_one(
        self,
        image_path: Path,
    ) -> tuple[
        Path,
        dict[str, object] | None,
        dict[str, object] | None,
        list[VisionProviderAttempt],
    ]:
        logger.info("processing image=%s", image_path)
        try:
            result = await self._vision_client.describe(image_path)
            metadata = result.metadata
            if metadata is None:
                metadata = await asyncio.to_thread(extract_photo_metadata, image_path)
            logger.debug("metadata extracted image=%s metadata_source=%s", image_path, metadata.metadata_source)
            logger.debug("vision description complete image=%s summary=%s", image_path, result.description.summary)
            return image_path, _build_record(image_path, metadata, result), None, result.provider_attempts
        except VisionProviderFailure as exc:
            logger.exception("failed to process image=%s", image_path)
            return image_path, None, {
                "file_name": image_path.name,
                "file_path": str(image_path.resolve()),
                "error": str(exc),
                "provider_name": exc.last_provider_name,
                "provider_elapsed_ms": exc.provider_elapsed_ms,
                "provider_attempts": _serialize_provider_attempts(exc.provider_attempts),
            }, exc.provider_attempts
        except Exception as exc:
            logger.exception("failed to process image=%s", image_path)
            return image_path, None, {
                "file_name": image_path.name,
                "file_path": str(image_path.resolve()),
                "error": str(exc),
                "provider_name": None,
                "provider_elapsed_ms": 0,
                "provider_attempts": [],
            }, []


def _build_record(
    image_path: Path,
    metadata: PhotoMetadata,
    result: VisionResult,
) -> dict[str, object]:
    description = result.description
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
        "provider_name": result.provider.name,
        "provider_base_url": result.provider.base_url,
        "provider_model": result.provider.model,
        "provider_elapsed_ms": result.provider_elapsed_ms,
        "provider_attempts": _serialize_provider_attempts(result.provider_attempts),
    }


def _serialize_provider_attempts(attempts: list[VisionProviderAttempt]) -> list[dict[str, object]]:
    return [
        {
            "provider_name": attempt.provider_name,
            "elapsed_ms": attempt.elapsed_ms,
            "ok": attempt.ok,
            "error": attempt.error,
        }
        for attempt in attempts
    ]


def _accumulate_provider_attempts(
    provider_stats: dict[str, ProviderStats],
    attempts: list[VisionProviderAttempt],
) -> None:
    for attempt in attempts:
        provider_name = attempt.provider_name
        stats = provider_stats.setdefault(provider_name, _new_provider_stats())
        started_ns = attempt.started_at_monotonic_ns
        finished_ns = attempt.finished_at_monotonic_ns
        if started_ns is None or finished_ns is None:
            continue
        stats["_attempt_count"] += 1
        stats["_attempt_wall_clock_ms_total"] += _elapsed_ms_between(started_ns, finished_ns)
        if stats["_first_started_ns"] < 0 or started_ns < stats["_first_started_ns"]:
            stats["_first_started_ns"] = started_ns
        if finished_ns > stats["_last_finished_ns"]:
            stats["_last_finished_ns"] = finished_ns


def _snapshot_provider_windows(provider_stats: dict[str, ProviderStats]) -> dict[str, ProviderStats]:
    snapshots: dict[str, ProviderStats] = {}
    for name, stats in provider_stats.items():
        attempt_count = int(stats.get("_attempt_count", 0))
        attempt_wall_clock_ms_total = int(stats.get("_attempt_wall_clock_ms_total", 0))
        first_started_ns = int(stats.get("_first_started_ns", -1))
        last_finished_ns = int(stats.get("_last_finished_ns", -1))
        snapshots[name] = {
            "attempt_count": attempt_count,
            "attempt_wall_clock_ms_total": attempt_wall_clock_ms_total,
            "wall_clock_ms": (
                _elapsed_ms_between(first_started_ns, last_finished_ns)
                if first_started_ns >= 0 and last_finished_ns >= 0
                else 0
            ),
        }
    return snapshots


def _empty_provider_window_snapshot() -> ProviderStats:
    return {
        "attempt_count": 0,
        "attempt_wall_clock_ms_total": 0,
        "wall_clock_ms": 0,
    }


def _new_provider_stats() -> ProviderStats:
    return {
        "processed": 0,
        "failed": 0,
        "wall_clock_ms": 0,
        "wall_clock_ms_avg": 0,
        "_attempt_count": 0,
        "_attempt_wall_clock_ms_total": 0,
        "_first_started_ns": -1,
        "_last_finished_ns": -1,
    }


def _elapsed_ms_between(started_ns: int, finished_ns: int) -> int:
    return max(0, round((finished_ns - started_ns) / 1_000_000))


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SystemExit(f"Output file field must be an object: {name}")
    return value


def _list_of_strings(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"Output file field must be a string array: {name}")
    return list(value)


def _records_by_path(value: object) -> dict[str, dict[str, object]]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise SystemExit("Output file field must be an array: records/failures")
    records: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise SystemExit("Output file records and failures must contain objects")
        file_path = item.get("file_path")
        if not isinstance(file_path, str):
            raise SystemExit("Output file records and failures must contain file_path")
        records[file_path] = item
    return records


def _int_from_mapping(value: object, key: str) -> int:
    if not isinstance(value, dict):
        return 0
    raw = value.get(key)
    return int(raw) if isinstance(raw, int) else 0


def _accumulate_provider_metric_totals(
    metrics: dict[str, ProviderMetricTotals],
    payload: dict[str, object],
) -> None:
    attempts = payload.get("provider_attempts")
    if not isinstance(attempts, list):
        return
    window_by_provider: dict[str, list[int]] = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        provider_name = attempt.get("provider_name")
        elapsed_ms = attempt.get("elapsed_ms")
        if not isinstance(provider_name, str) or not isinstance(elapsed_ms, int):
            continue
        metric = metrics.setdefault(provider_name, ProviderMetricTotals())
        metric.attempt_count += 1
        metric.attempt_wall_clock_ms_total += elapsed_ms
        bounds = window_by_provider.setdefault(provider_name, [metric.wall_clock_ms_total, metric.wall_clock_ms_total])
        bounds[1] += elapsed_ms
    for provider_name, bounds in window_by_provider.items():
        metric = metrics.setdefault(provider_name, ProviderMetricTotals())
        metric.wall_clock_ms_total = max(metric.wall_clock_ms_total, bounds[1])
