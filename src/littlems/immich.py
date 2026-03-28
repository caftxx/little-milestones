from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from littlems.documents import DESCRIPTION_DOCUMENT_VERSION, build_description_document, ensure_record_source_fields
from littlems.exif import extract_photo_metadata_from_bytes
from littlems.models import PhotoMetadata, VisionInput
from littlems.report import generate_report_for_records
from littlems.scanner import SUPPORTED_EXTENSIONS


@dataclass(slots=True)
class ImmichAsset:
    id: str
    original_file_name: str
    original_path: str | None
    original_mime_type: str | None
    local_date_time: str | None
    file_created_at: str | None
    created_at: str | None
    updated_at: str | None
    description: str | None = None
    asset_type: str | None = None
    exif_info: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ImmichAlbum:
    id: str
    album_name: str
    description: str | None = None
    asset_count: int = 0


@dataclass(slots=True)
class ImmichResumeState:
    total_assets: int
    skipped: int
    failed_to_retry: int
    pending: int


@dataclass(slots=True)
class _ImmichRunState:
    source: dict[str, object]
    output_file: Path
    generated_at: str
    skipped_count: int
    prior_wall_clock_ms: int
    current_run_started_ns: int
    records_by_id: dict[str, dict[str, object]]
    failures_by_id: dict[str, dict[str, object]]
    completed_ids: set[str]
    failed_ids: set[str]
    asset_ids: list[str]
    upload_description: bool
    provider_names: list[str]


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ImmichClient:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_albums(self) -> list[ImmichAlbum]:
        client = self._require_client()
        response = await client.get("/albums")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Immich albums response must be a JSON array")
        return [_parse_album(item) for item in payload if isinstance(item, dict)]

    async def search_assets(
        self,
        *,
        album_ids: list[str] | None = None,
        taken_after: str | None = None,
        taken_before: str | None = None,
        page_size: int = 200,
    ) -> list[ImmichAsset]:
        client = self._require_client()
        page = 1
        assets: list[ImmichAsset] = []
        while True:
            payload: dict[str, object] = {
                "page": page,
                "size": page_size,
                "withExif": True,
                "order": "asc",
                "type": "IMAGE",
            }
            if album_ids:
                payload["albumIds"] = album_ids
            if taken_after is not None:
                payload["takenAfter"] = taken_after
            if taken_before is not None:
                payload["takenBefore"] = taken_before
            response = await client.post(
                "/search/metadata",
                json=payload,
            )
            response.raise_for_status()
            payload = _json_mapping(response)
            assets_payload = _mapping(payload.get("assets"))
            items = assets_payload.get("items")
            if not isinstance(items, list):
                raise ValueError("Immich search response must include assets.items")
            assets.extend(_parse_asset(item) for item in items if isinstance(item, dict))
            next_page = assets_payload.get("nextPage")
            if next_page in {None, ""} and len(items) < page_size:
                break
            page += 1
        return assets

    async def get_asset(self, asset_id: str) -> ImmichAsset:
        client = self._require_client()
        response = await client.get(f"/assets/{asset_id}")
        response.raise_for_status()
        return _parse_asset(_json_mapping(response))

    async def fetch_asset_original(self, asset_id: str) -> bytes:
        client = self._require_client()
        response = await client.get(f"/assets/{asset_id}/original")
        response.raise_for_status()
        return response.content

    async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
        if not asset_ids:
            return
        client = self._require_client()
        response = await client.put(
            "/assets",
            json={
                "ids": asset_ids,
                "description": description,
            },
        )
        response.raise_for_status()

    async def update_album_description(self, album_id: str, description: str) -> ImmichAlbum:
        client = self._require_client()
        response = await client.patch(
            f"/albums/{album_id}",
            json={
                "description": description,
            },
        )
        response.raise_for_status()
        return _parse_album(_json_mapping(response))

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ImmichClient must be used as an async context manager")
        return self._client


def default_asset_sync_output_path(input_path: Path) -> Path:
    return _default_output_path(input_path, "immich-update-asset-description")


def default_album_sync_output_path(report_path: Path) -> Path:
    return _default_output_path(report_path, "immich-update-album-description")


async def describe_immich_album(
    *,
    album_name: str | None,
    output_path: Path,
    immich_url: str,
    api_key: str,
    vision_client: object,
    max_workers: int = 16,
    upload_description: bool = False,
    force: bool = False,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    return await describe_immich_album_to_file(
        album_name=album_name,
        output_path=output_path,
        immich_url=immich_url,
        api_key=api_key,
        vision_client=vision_client,
        max_workers=max_workers,
        upload_description=upload_description,
        force=force,
        provider_names=_provider_names_from_client(vision_client),
        client_transport=client_transport,
    )


async def inspect_immich_resume_state(
    *,
    album_name: str | None,
    output_path: Path,
    immich_url: str,
    api_key: str,
    provider_names: list[str],
    upload_description: bool = False,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> ImmichResumeState:
    async with ImmichClient(immich_url, api_key, transport=client_transport) as client:
        source, _, assets = await _resolve_asset_scope(client, immich_url=immich_url, album_name=album_name)
        if not assets:
            raise SystemExit(_empty_assets_message(album_name))
        state = _prepare_immich_run_state(
            assets=assets,
            output_file=output_path,
            source=source,
            provider_names=provider_names,
            upload_description=upload_description,
        )
        return ImmichResumeState(
            total_assets=len(assets),
            skipped=state.skipped_count,
            failed_to_retry=len(state.failed_ids),
            pending=_pending_asset_count(state),
        )


async def describe_immich_album_to_file(
    *,
    album_name: str | None,
    output_path: Path,
    immich_url: str,
    api_key: str,
    vision_client: object,
    provider_names: list[str],
    max_workers: int = 16,
    upload_description: bool = False,
    force: bool = False,
    progress_callback: object | None = None,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    async with ImmichClient(immich_url, api_key, transport=client_transport) as client:
        source, album, assets = await _resolve_asset_scope(client, immich_url=immich_url, album_name=album_name)
        if not assets:
            raise SystemExit(_empty_assets_message(album_name))

        state = _prepare_immich_run_state(
            assets=assets,
            output_file=output_path,
            source=source,
            provider_names=provider_names,
            upload_description=upload_description,
        )
        _write_immich_document(state, status="running")

        total = len(assets)
        asset_by_id = {asset.id: asset for asset in assets}
        pending_ids = [asset_id for asset_id in state.asset_ids if asset_id not in state.completed_ids]
        if pending_ids:
            processed = 0
            merge_lock = asyncio.Lock()
            queue: asyncio.Queue[str] = asyncio.Queue()
            for asset_id in pending_ids:
                queue.put_nowait(asset_id)

            worker_count = min(max_workers, len(pending_ids))

            async def worker() -> None:
                nonlocal processed
                while True:
                    try:
                        asset_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    asset = asset_by_id[asset_id]
                    try:
                        full_asset = await client.get_asset(asset.id)
                        record = await _describe_asset(
                            asset=full_asset,
                            album_name=album.album_name if album is not None else None,
                            client=client,
                            immich_url=immich_url,
                            vision_client=vision_client,
                            upload_description=upload_description and not force,
                        )
                        failure = None
                    except Exception as exc:
                        record = None
                        failure = ensure_record_source_fields(
                            {
                                "asset_id": asset.id,
                                "album_name": album.album_name if album is not None else None,
                                "file_name": asset.original_file_name,
                                "file_path": asset.original_path,
                                "error": str(exc),
                                "provider_name": None,
                                "provider_elapsed_ms": 0,
                                "provider_attempts": [],
                            },
                            source_kind="immich",
                            source_id=asset.id,
                            source_path=asset.original_path,
                            source_uri=f"{immich_url.rstrip('/')}/assets/{asset.id}",
                            source_album_name=album.album_name if album is not None else None,
                        )
                    try:
                        async with merge_lock:
                            processed += 1
                            if record is not None:
                                state.records_by_id[asset.id] = record
                                state.failures_by_id.pop(asset.id, None)
                                state.completed_ids.add(asset.id)
                                state.failed_ids.discard(asset.id)
                            if failure is not None:
                                state.failures_by_id[asset.id] = failure
                                state.records_by_id.pop(asset.id, None)
                                state.completed_ids.discard(asset.id)
                                state.failed_ids.add(asset.id)
                            _write_immich_document(state, status="running")
                            if progress_callback is not None:
                                progress_callback(state.skipped_count + processed, total, asset)
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
            await queue.join()
            await asyncio.gather(*workers)
        if upload_description and force:
            await _upload_immich_records(
                client=client,
                records=[state.records_by_id[asset_id] for asset_id in state.asset_ids if asset_id in state.records_by_id],
            )
        _write_immich_document(state, status="completed")
        return _build_immich_document(state, status="completed")


async def generate_immich_album_report(
    *,
    album_name: str | None,
    output_path: Path,
    immich_url: str,
    api_key: str,
    vision_client: object,
    report_settings: object,
    date_from: str | None,
    date_to: str | None,
    birth_date: str,
    baby_name: str,
    json_output_path: Path | None = None,
    description_output_path: Path | None = None,
    upload_description: bool = False,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    async with ImmichClient(immich_url, api_key, transport=client_transport) as client:
        source, album, assets = await _resolve_asset_scope(
            client,
            immich_url=immich_url,
            album_name=album_name,
            date_from=date_from,
            date_to=date_to,
        )
        if not assets:
            raise SystemExit(_empty_assets_message(album_name))

        records, failures, generated_asset_ids = await _collect_immich_records(
            client=client,
            assets=assets,
            album_name=album.album_name if album is not None else None,
            immich_url=immich_url,
            vision_client=vision_client,
            reuse_existing_descriptions=True,
            upload_generated_descriptions=upload_description,
        )

        document = build_description_document(
            source=source,
            records=records,
            failures=failures,
            summary_extra={
                "upload_description": upload_description,
                "generated_records": len(generated_asset_ids),
                "reused_records": len(records) - len(generated_asset_ids),
            },
        )
        if description_output_path is not None:
            description_output_path.parent.mkdir(parents=True, exist_ok=True)
            description_output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

        if album_name is not None:
            resolved_date_from, resolved_date_to = _date_range_from_records(records)
        else:
            assert date_from is not None and date_to is not None
            resolved_date_from, resolved_date_to = date_from, date_to

        report = await generate_report_for_records(
            records=records,
            history_records=records,
            date_from=resolved_date_from,
            date_to=resolved_date_to,
            birth_date=birth_date,
            baby_name=baby_name,
            output_path=output_path,
            settings=report_settings,
            json_output_path=json_output_path,
            source=document["source"],
        )
        if upload_description and album is not None:
            await client.update_album_description(album.id, report["markdown"])
        return report


async def upload_immich_descriptions_and_report(
    *,
    records: list[dict[str, object]],
    markdown: str,
    album_name: str | None,
    immich_url: str,
    api_key: str,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    async with ImmichClient(immich_url, api_key, transport=client_transport) as client:
        await _upload_immich_records(client=client, records=records)
        if album_name is not None:
            album = await _find_album(client, album_name)
            await client.update_album_description(album.id, markdown)


async def _upload_immich_records(*, client: ImmichClient, records: list[dict[str, object]]) -> None:
    for record in records:
        if record.get("source_kind") != "immich":
            continue
        source_id = record.get("source_id") or record.get("asset_id")
        if not isinstance(source_id, str) or not source_id:
            continue
        await client.update_assets_description([source_id], render_immich_description(record))


async def _find_album(client: ImmichClient, album_name: str) -> ImmichAlbum:
    albums = await client.list_albums()
    matches = [album for album in albums if album.album_name == album_name]
    if not matches:
        raise SystemExit(f"Immich 相册不存在: {album_name}")
    if len(matches) > 1:
        raise SystemExit(f"Immich 相册重名，无法唯一匹配: {album_name}")
    return matches[0]


async def _describe_asset(
    *,
    asset: ImmichAsset,
    album_name: str | None,
    client: ImmichClient,
    immich_url: str,
    vision_client: object,
    upload_description: bool,
) -> dict[str, object]:
    if not _is_supported_image(asset):
        raise RuntimeError(f"unsupported asset type: {asset.original_file_name}")
    image_bytes = await client.fetch_asset_original(asset.id)
    metadata = _metadata_for_asset(asset, image_bytes)
    image_input = VisionInput(
        image_name=asset.original_file_name,
        mime_type=asset.original_mime_type or _mime_type_from_name(asset.original_file_name),
        image_bytes=image_bytes,
        metadata=metadata,
    )
    result = await vision_client.describe_input(image_input)
    record = ensure_record_source_fields(
        {
            "asset_id": asset.id,
            "album_name": album_name,
            "file_name": asset.original_file_name,
            "file_path": asset.original_path,
            "captured_at": result.metadata.captured_at if result.metadata is not None else metadata.captured_at,
            "timezone": result.metadata.timezone if result.metadata is not None else metadata.timezone,
            "location": result.metadata.location if result.metadata is not None else metadata.location,
            "gps": result.metadata.gps if result.metadata is not None else metadata.gps,
            "device": result.metadata.device if result.metadata is not None else metadata.device,
            "summary": result.description.summary,
            "baby_present": result.description.baby_present,
            "actions": result.description.actions,
            "expressions": result.description.expressions,
            "scene": result.description.scene,
            "objects": result.description.objects,
            "highlights": result.description.highlights,
            "uncertainty": result.description.uncertainty,
            "metadata_source": metadata.metadata_source,
            "provider_name": result.provider.name,
            "provider_base_url": result.provider.base_url,
            "provider_model": result.provider.model,
            "provider_elapsed_ms": result.provider_elapsed_ms,
            "provider_attempts": [
                {
                    "provider_name": attempt.provider_name,
                    "elapsed_ms": attempt.elapsed_ms,
                    "ok": attempt.ok,
                    "error": attempt.error,
                }
                for attempt in result.provider_attempts
            ],
        },
        source_kind="immich",
        source_id=asset.id,
        source_path=asset.original_path,
        source_uri=f"{immich_url.rstrip('/')}/assets/{asset.id}",
        source_album_name=album_name,
        existing_description=_string_or_none(asset.description),
        description_origin="generated",
    )
    if upload_description:
        await client.update_assets_description([asset.id], render_immich_description(record))
    return record


def _record_from_existing_description(asset: ImmichAsset, album_name: str | None, immich_url: str) -> dict[str, object]:
    description = _string_or_none(asset.description) or ""
    parsed = parse_immich_description(description)
    metadata = _metadata_for_asset(asset, None)
    return ensure_record_source_fields(
        {
            "asset_id": asset.id,
            "album_name": album_name,
            "file_name": asset.original_file_name,
            "file_path": asset.original_path,
            "captured_at": metadata.captured_at,
            "timezone": metadata.timezone,
            "location": metadata.location,
            "gps": metadata.gps,
            "device": metadata.device,
            "summary": parsed["summary"],
            "baby_present": True,
            "actions": parsed["actions"],
            "expressions": parsed["expressions"],
            "scene": parsed["scene"],
            "objects": parsed["objects"],
            "highlights": parsed["highlights"],
            "uncertainty": parsed["uncertainty"],
            "metadata_source": metadata.metadata_source,
            "provider_name": "immich_metadata",
            "provider_base_url": immich_url.rstrip("/"),
            "provider_model": "immich.description",
            "provider_elapsed_ms": 0,
            "provider_attempts": [],
        },
        source_kind="immich",
        source_id=asset.id,
        source_path=asset.original_path,
        source_uri=f"{immich_url.rstrip('/')}/assets/{asset.id}",
        source_album_name=album_name,
        existing_description=description,
        description_origin="metadata",
    )


def render_immich_description(record: dict[str, object]) -> str:
    lines: list[str] = []
    summary = _string_or_none(record.get("summary"))
    if summary:
        lines.append(summary)

    for label, field in (
        ("动作", "actions"),
        ("表情", "expressions"),
        ("场景", "scene"),
        ("物件", "objects"),
        ("亮点", "highlights"),
    ):
        text = _format_field_value(record.get(field))
        if text:
            lines.append(f"{label}: {text}")

    uncertainty = _string_or_none(record.get("uncertainty"))
    if uncertainty:
        lines.append(f"说明: {uncertainty}")

    if not lines:
        return "Little Milestones 未生成可写入的描述"
    return "\n".join(lines)


def parse_immich_description(description: str) -> dict[str, object]:
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    summary = ""
    actions: list[str] = []
    expressions: list[str] = []
    scene: str | None = None
    objects: list[str] = []
    highlights: list[str] = []
    uncertainty: str | None = None

    for index, line in enumerate(lines):
        if ":" not in line and "：" not in line and index == 0:
            summary = line
            continue
        normalized = line.replace("：", ":", 1)
        key, _, value = normalized.partition(":")
        value = value.strip()
        if key == "动作":
            actions = _split_chinese_list(value)
        elif key == "表情":
            expressions = _split_chinese_list(value)
        elif key == "场景":
            scene = value or None
        elif key == "物件":
            objects = _split_chinese_list(value)
        elif key == "亮点":
            highlights = _split_chinese_list(value)
        elif key == "说明":
            uncertainty = value or None
        elif not summary:
            summary = line

    if not summary:
        summary = description.strip()
    return {
        "summary": summary,
        "actions": actions,
        "expressions": expressions,
        "scene": scene,
        "objects": objects,
        "highlights": highlights,
        "uncertainty": uncertainty,
    }


def _metadata_for_asset(asset: ImmichAsset, image_bytes: bytes | None) -> PhotoMetadata:
    metadata = (
        extract_photo_metadata_from_bytes(
            image_bytes,
            image_name=asset.original_file_name,
            mime_type=asset.original_mime_type,
        )
        if image_bytes is not None
        else PhotoMetadata()
    )
    if metadata.captured_at is None:
        for field_name, field_value in (
            ("immich.localDateTime", asset.local_date_time),
            ("immich.fileCreatedAt", asset.file_created_at),
            ("immich.createdAt", asset.created_at),
        ):
            if _string_or_none(field_value):
                metadata.captured_at = _normalize_datetime_string(str(field_value))
                metadata.metadata_source["captured_at"] = field_name
                break
    return metadata


def _normalize_datetime_string(value: str) -> str:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value.strip()
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None).isoformat()
    return parsed.isoformat()


def _is_supported_image(asset: ImmichAsset) -> bool:
    candidate = asset.original_path or asset.original_file_name
    return Path(candidate).suffix.lower() in SUPPORTED_EXTENSIONS


def _split_chinese_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _format_field_value(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, list):
        parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not parts:
            return None
        return "，".join(parts)
    return None


def _string_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _mime_type_from_name(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heif": "image/heif",
        ".heic": "image/heic",
        ".dng": "image/x-adobe-dng",
    }.get(suffix, "application/octet-stream")


def _default_output_path(path: Path, suffix: str) -> Path:
    if path.suffix:
        return path.with_name(f"{path.stem}.{suffix}.json")
    return path.with_name(f"{path.name}.{suffix}.json")


def _provider_names_from_client(vision_client: object) -> list[str]:
    providers = getattr(vision_client, "_providers", None)
    if not isinstance(providers, list):
        return []
    names: list[str] = []
    for runtime in providers:
        settings = getattr(runtime, "settings", None)
        name = getattr(settings, "name", None)
        if isinstance(name, str):
            names.append(name)
    return names


async def _resolve_asset_scope(
    client: ImmichClient,
    *,
    immich_url: str,
    album_name: str | None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[dict[str, object], ImmichAlbum | None, list[ImmichAsset]]:
    if album_name is not None:
        album = await _find_album(client, album_name)
        assets = await client.search_assets(album_ids=[album.id])
        return (
            {
                "kind": "immich",
                "scope": "album",
                "immich_url": immich_url.rstrip("/"),
                "album_name": album.album_name,
                "album_id": album.id,
            },
            album,
            assets,
        )

    if date_from is not None or date_to is not None:
        if not date_from or not date_to:
            raise SystemExit("immich report 使用时间范围时必须同时提供 --from 和 --to")
        taken_after, taken_before = _search_datetime_window(date_from, date_to)
        assets = await client.search_assets(taken_after=taken_after, taken_before=taken_before)
        return (
            {
                "kind": "immich",
                "scope": "date_range",
                "immich_url": immich_url.rstrip("/"),
                "album_name": None,
                "album_id": None,
                "date_from": date_from,
                "date_to": date_to,
            },
            None,
            assets,
        )

    assets = await client.search_assets()
    return (
        {
            "kind": "immich",
            "scope": "library",
            "immich_url": immich_url.rstrip("/"),
            "album_name": None,
            "album_id": None,
        },
        None,
        assets,
    )


async def _collect_immich_records(
    *,
    client: ImmichClient,
    assets: list[ImmichAsset],
    album_name: str | None,
    immich_url: str,
    vision_client: object,
    reuse_existing_descriptions: bool,
    upload_generated_descriptions: bool,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    records: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    generated_asset_ids: list[str] = []
    for asset in assets:
        try:
            full_asset = await client.get_asset(asset.id)
            if reuse_existing_descriptions and _string_or_none(full_asset.description):
                record = _record_from_existing_description(full_asset, album_name, immich_url)
            else:
                record = await _describe_asset(
                    asset=full_asset,
                    album_name=album_name,
                    client=client,
                    immich_url=immich_url,
                    vision_client=vision_client,
                    upload_description=upload_generated_descriptions,
                )
                generated_asset_ids.append(full_asset.id)
            records.append(record)
        except Exception as exc:
            failures.append(
                ensure_record_source_fields(
                    {
                    "asset_id": asset.id,
                    "album_name": album_name,
                    "file_name": asset.original_file_name,
                    "file_path": asset.original_path,
                    "error": str(exc),
                    "provider_name": None,
                    "provider_elapsed_ms": 0,
                    "provider_attempts": [],
                    },
                    source_kind="immich",
                    source_id=asset.id,
                    source_path=asset.original_path,
                    source_uri=f"{immich_url.rstrip('/')}/assets/{asset.id}",
                    source_album_name=album_name,
                )
            )
    return records, failures, generated_asset_ids


def _prepare_immich_run_state(
    *,
    assets: list[ImmichAsset],
    output_file: Path,
    source: dict[str, object],
    provider_names: list[str],
    upload_description: bool,
) -> _ImmichRunState:
    generated_at = datetime.now(UTC).isoformat()
    records_by_id: dict[str, dict[str, object]] = {}
    failures_by_id: dict[str, dict[str, object]] = {}
    completed_ids: set[str] = set()
    failed_ids: set[str] = set()
    prior_wall_clock_ms = 0

    if output_file.exists():
        payload = _load_existing_output(output_file)
        _validate_immich_resume_payload(
            payload,
            source=source,
            provider_names=provider_names,
            upload_description=upload_description,
        )
        generated_at = str(payload.get("generated_at"))
        prior_wall_clock_ms = _int_from_mapping(payload.get("summary"), "wall_clock_ms")
        records_by_id = _records_by_source_id(payload.get("records"))
        failures_by_id = _records_by_source_id(payload.get("failures"))
        run_state = _mapping(payload.get("run_state"))
        completed_ids = set(_list_of_strings(run_state.get("completed")))
        failed_ids = set(_list_of_strings(run_state.get("failed")))
        completed_ids |= set(records_by_id)
        failed_ids |= set(failures_by_id)

    valid_ids = {asset.id for asset in assets}
    records_by_id = {asset_id: record for asset_id, record in records_by_id.items() if asset_id in valid_ids}
    failures_by_id = {asset_id: record for asset_id, record in failures_by_id.items() if asset_id in valid_ids}
    completed_ids &= valid_ids
    failed_ids &= valid_ids
    failed_ids -= completed_ids

    return _ImmichRunState(
        source=source,
        output_file=output_file,
        generated_at=generated_at,
        skipped_count=len(completed_ids),
        prior_wall_clock_ms=prior_wall_clock_ms,
        current_run_started_ns=time.perf_counter_ns(),
        records_by_id=records_by_id,
        failures_by_id=failures_by_id,
        completed_ids=completed_ids,
        failed_ids=failed_ids,
        asset_ids=[asset.id for asset in assets],
        upload_description=upload_description,
        provider_names=provider_names,
    )


def _build_immich_document(state: _ImmichRunState, *, status: str) -> dict[str, object]:
    records = [state.records_by_id[asset_id] for asset_id in state.asset_ids if asset_id in state.records_by_id]
    failures = [state.failures_by_id[asset_id] for asset_id in state.asset_ids if asset_id in state.failures_by_id]
    total = len(state.asset_ids)
    processed = len(records)
    failed = len(failures)
    wall_clock_ms = state.prior_wall_clock_ms + _elapsed_ms_since(state.current_run_started_ns)
    provider_stats = _build_provider_stats(
        provider_names=state.provider_names,
        records=records,
        failures=failures,
    )
    return build_description_document(
        source=state.source,
        input_payload={**state.source, "upload_description": state.upload_description},
        model={
            "provider": "multi_provider_pool",
            "providers": state.provider_names,
        },
        provider_stats=provider_stats,
        records=records,
        failures=failures,
        status=status,
        generated_at=state.generated_at,
        run_state={
            "completed": sorted(state.completed_ids),
            "failed": sorted(state.failed_ids),
            "provider_metrics": {},
        },
        summary_extra={
            "total": total,
            "processed": processed,
            "failed": failed,
            "skipped": state.skipped_count,
            "remaining": max(0, total - processed - failed),
            "wall_clock_ms": wall_clock_ms,
        },
    )


def _write_immich_document(state: _ImmichRunState, *, status: str) -> None:
    state.output_file.parent.mkdir(parents=True, exist_ok=True)
    document = _build_immich_document(state, status=status)
    temp_file = state.output_file.with_name(f"{state.output_file.name}.tmp")
    payload = json.dumps(document, ensure_ascii=False, indent=2)
    temp_file.write_text(payload, encoding="utf-8")
    temp_file.replace(state.output_file)


def _load_existing_output(output_file: Path) -> dict[str, object]:
    try:
        payload = json.loads(output_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Output file is not valid JSON: {output_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Output file must contain a JSON object: {output_file}")
    return payload


def _validate_immich_resume_payload(
    payload: dict[str, object],
    *,
    source: dict[str, object],
    provider_names: list[str],
    upload_description: bool,
) -> None:
    del upload_description
    if payload.get("version") != DESCRIPTION_DOCUMENT_VERSION:
        raise SystemExit(f"Output file version mismatch: expected {DESCRIPTION_DOCUMENT_VERSION}, got {payload.get('version')}")
    payload_source = _mapping(payload.get("source"))
    if payload_source.get("kind") != "immich":
        raise SystemExit("Output file source kind does not match current run: expected immich")
    for key in ("scope", "immich_url", "album_name", "album_id"):
        if payload_source.get(key) != source.get(key):
            raise SystemExit(f"Output file source {key} does not match current run: {payload_source.get(key)} != {source.get(key)}")
    model_payload = _mapping(payload.get("model"))
    actual_providers = _list_of_strings(model_payload.get("providers"))
    if actual_providers != provider_names:
        raise SystemExit(f"Output file providers do not match current run: {actual_providers} != {provider_names}")


def _records_by_source_id(value: object) -> dict[str, dict[str, object]]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise SystemExit("Output file field must be an array: records/failures")
    records: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise SystemExit("Output file records and failures must contain objects")
        source_id = item.get("source_id") or item.get("asset_id")
        if not isinstance(source_id, str):
            raise SystemExit("Output file records and failures must contain source_id")
        records[source_id] = item
    return records


def _list_of_strings(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit("Output file field must be a string array")
    return list(value)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return value


def _int_from_mapping(value: object, key: str) -> int:
    if not isinstance(value, dict):
        return 0
    raw = value.get(key)
    return int(raw) if isinstance(raw, int) else 0


def _build_provider_stats(
    *,
    provider_names: list[str],
    records: list[dict[str, object]],
    failures: list[dict[str, object]],
) -> dict[str, dict[str, int]]:
    names = set(provider_names)
    names |= {str(item.get("provider_name")) for item in records if item.get("provider_name")}
    names |= {str(item.get("provider_name")) for item in failures if item.get("provider_name")}
    provider_stats: dict[str, dict[str, int]] = {}
    for provider_name in sorted(names):
        elapsed_values = [
            int(item.get("provider_elapsed_ms", 0))
            for item in [*records, *failures]
            if item.get("provider_name") == provider_name
        ]
        wall_clock_ms = sum(elapsed_values)
        provider_stats[provider_name] = {
            "processed": sum(1 for item in records if item.get("provider_name") == provider_name),
            "failed": sum(1 for item in failures if item.get("provider_name") == provider_name),
            "wall_clock_ms": wall_clock_ms,
            "wall_clock_ms_avg": round(wall_clock_ms / len(elapsed_values)) if elapsed_values else 0,
        }
    return provider_stats


def _pending_asset_count(state: _ImmichRunState) -> int:
    return sum(1 for asset_id in state.asset_ids if asset_id not in state.completed_ids)


def _elapsed_ms_since(started_ns: int) -> int:
    if started_ns <= 0:
        return 0
    return max(0, round((time.perf_counter_ns() - started_ns) / 1_000_000))


def _search_datetime_window(date_from: str, date_to: str) -> tuple[str, str]:
    start = datetime.fromisoformat(f"{date_from}T00:00:00")
    end = datetime.fromisoformat(f"{date_to}T23:59:59")
    return start.isoformat(), end.isoformat()


def _date_range_from_records(records: list[dict[str, object]]) -> tuple[str, str]:
    captured_values = [
        str(record.get("captured_at"))
        for record in records
        if isinstance(record.get("captured_at"), str) and len(str(record.get("captured_at"))) >= 10
    ]
    if not captured_values:
        raise SystemExit("指定范围没有可用于生成报告的照片记录")
    captured_values.sort()
    return captured_values[0][:10], captured_values[-1][:10]


def _empty_assets_message(album_name: str | None) -> str:
    if album_name is None:
        return "Immich 中没有可处理的图片"
    return f"Immich 相册中没有可处理的图片: {album_name}"


def _json_mapping(response: httpx.Response) -> dict[str, object]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Immich response must be a JSON object")
    return payload


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return value


def _parse_asset(payload: dict[str, object]) -> ImmichAsset:
    asset_id = _string_or_none(payload.get("id"))
    file_name = _string_or_none(payload.get("originalFileName"))
    if asset_id is None or file_name is None:
        raise ValueError("Immich asset must include id and originalFileName")
    exif_info = payload.get("exifInfo")
    return ImmichAsset(
        id=asset_id,
        original_file_name=file_name,
        original_path=_string_or_none(payload.get("originalPath")),
        original_mime_type=_string_or_none(payload.get("originalMimeType")),
        local_date_time=_string_or_none(payload.get("localDateTime")),
        file_created_at=_string_or_none(payload.get("fileCreatedAt")),
        created_at=_string_or_none(payload.get("createdAt")),
        updated_at=_string_or_none(payload.get("updatedAt")),
        description=_string_or_none(payload.get("description")),
        asset_type=_string_or_none(payload.get("type")),
        exif_info=exif_info if isinstance(exif_info, dict) else {},
    )


def _parse_album(payload: dict[str, object]) -> ImmichAlbum:
    album_id = _string_or_none(payload.get("id"))
    album_name = _string_or_none(payload.get("albumName"))
    if album_id is None or album_name is None:
        raise ValueError("Immich album must include id and albumName")
    raw_asset_count = payload.get("assetCount")
    return ImmichAlbum(
        id=album_id,
        album_name=album_name,
        description=_string_or_none(payload.get("description")),
        asset_count=raw_asset_count if isinstance(raw_asset_count, int) else 0,
    )
