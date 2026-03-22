from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from littlems.scanner import SUPPORTED_EXTENSIONS


@dataclass(slots=True)
class ImmichAsset:
    id: str
    original_file_name: str
    original_path: str | None
    local_date_time: str | None
    file_created_at: str | None
    created_at: str | None


@dataclass(slots=True)
class ImmichAlbum:
    id: str
    album_name: str
    description: str | None = None
    asset_ids: set[str] = field(default_factory=set)


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
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search_assets_by_metadata(
        self,
        *,
        original_file_name: str,
        taken_after: str | None = None,
        taken_before: str | None = None,
        page: int = 1,
        size: int = 10,
    ) -> list[ImmichAsset]:
        client = self._require_client()
        payload: dict[str, object] = {
            "originalFileName": original_file_name,
            "page": page,
            "size": size,
        }
        if taken_after is not None:
            payload["takenAfter"] = taken_after
        if taken_before is not None:
            payload["takenBefore"] = taken_before

        response = await client.post("/search/metadata", json=payload)
        response.raise_for_status()
        payload = _json_mapping(response)
        assets_payload = _mapping(payload.get("assets"))
        items = assets_payload.get("items")
        if not isinstance(items, list):
            raise ValueError("Immich search response must include assets.items")
        return [_parse_asset(item) for item in items if isinstance(item, dict)]

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

    async def list_albums(self) -> list[ImmichAlbum]:
        client = self._require_client()
        response = await client.get("/albums")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Immich albums response must be a JSON array")
        return [_parse_album(item) for item in payload if isinstance(item, dict)]

    async def update_album_info(
        self,
        album_id: str,
        *,
        description: str,
    ) -> ImmichAlbum:
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


async def sync_asset_descriptions_to_immich(
    *,
    input_path: Path,
    immich_url: str,
    api_key: str,
    output_path: Path | None = None,
    match_window_minutes: int = 5,
    skip_videos: bool = True,
    dry_run: bool = False,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    payload = _load_json_object(
        input_path,
        missing_message="Descriptions JSON file not found: {path}",
        invalid_message="Descriptions JSON file is not valid JSON: {path}: {error}",
        wrong_type_message="Descriptions JSON must contain a top-level object",
    )
    records = payload.get("records")
    if not isinstance(records, list):
        raise SystemExit("Descriptions JSON must contain a 'records' array")

    output_file = output_path or default_asset_sync_output_path(input_path)
    async with ImmichClient(immich_url, api_key, transport=client_transport) as client:
        result = await _sync_asset_records(
            records=records,
            client=client,
            immich_url=immich_url.rstrip("/"),
            match_window_minutes=match_window_minutes,
            skip_videos=skip_videos,
            dry_run=dry_run,
        )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


async def sync_album_description_to_immich(
    *,
    report_path: Path,
    month: str,
    immich_url: str,
    api_key: str,
    album_prefix: str = "",
    output_path: Path | None = None,
    dry_run: bool = False,
    client_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    report_text = _load_text_file(report_path, missing_message="Report Markdown file not found: {path}")
    album_name = _album_name_for_month(month, prefix=album_prefix)
    output_file = output_path or default_album_sync_output_path(report_path)

    async with ImmichClient(immich_url, api_key, transport=client_transport) as client:
        albums = await client.list_albums()
        matches = [album for album in albums if album.album_name == album_name]

        result_record: dict[str, object] = {
            "month": month,
            "album_name": album_name,
            "matched_album_id": None,
            "description_updated": False,
            "status": "missing_album",
            "error": None,
        }
        summary = {
            "total_targets": 1,
            "updated": 0,
            "missing_album": 0,
            "ambiguous_album": 0,
            "update_failed": 0,
            "planned_updates": 0,
        }

        if not matches:
            summary["missing_album"] += 1
            result_record["error"] = f"No album matched name: {album_name}"
        elif len(matches) > 1:
            summary["ambiguous_album"] += 1
            result_record["status"] = "ambiguous_album"
            result_record["error"] = f"Multiple albums matched name: {album_name}"
        else:
            album = matches[0]
            result_record["matched_album_id"] = album.id
            if dry_run:
                summary["planned_updates"] += 1
                result_record["status"] = "planned_update"
            else:
                try:
                    await client.update_album_info(album.id, description=report_text)
                except httpx.HTTPError as exc:
                    summary["update_failed"] += 1
                    result_record["status"] = "update_failed"
                    result_record["error"] = str(exc)
                else:
                    summary["updated"] += 1
                    result_record["status"] = "updated"
                    result_record["description_updated"] = True

    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "immich_url": immich_url.rstrip("/"),
        "dry_run": dry_run,
        "summary": summary,
        "records": [result_record],
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def default_asset_sync_output_path(input_path: Path) -> Path:
    return _default_output_path(input_path, "immich-update-asset-description")


def default_album_sync_output_path(report_path: Path) -> Path:
    return _default_output_path(report_path, "immich-update-album-description")


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


async def _sync_asset_records(
    *,
    records: list[object],
    client: ImmichClient,
    immich_url: str,
    match_window_minutes: int,
    skip_videos: bool,
    dry_run: bool,
) -> dict[str, object]:
    summary = {
        "total_records": len(records),
        "eligible_records": 0,
        "skipped": 0,
        "matched": 0,
        "updated": 0,
        "update_failed": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "planned_updates": 0,
        "matched_by_time_window": 0,
        "matched_by_path_suffix": 0,
        "matched_by_fallback_time": 0,
    }
    result_records: list[dict[str, object]] = []

    for item in records:
        if not isinstance(item, dict):
            continue

        result = {
            "file_path": item.get("file_path"),
            "file_name": item.get("file_name"),
            "captured_at": item.get("captured_at"),
            "match_status": "unmatched",
            "match_strategy": None,
            "matched_asset_id": None,
            "matched_asset_original_path": None,
            "description_updated": False,
            "error": None,
        }

        if skip_videos and _should_skip_record(item):
            summary["skipped"] += 1
            result["match_status"] = "skipped"
            result["error"] = "Skipped unsupported non-image record"
            result_records.append(result)
            continue

        summary["eligible_records"] += 1
        file_name = _string_or_none(item.get("file_name"))
        captured_at = _string_or_none(item.get("captured_at"))
        if file_name is None or captured_at is None:
            summary["unmatched"] += 1
            result["error"] = "Record must include file_name and captured_at"
            result_records.append(result)
            continue

        taken_after, taken_before = _search_window(captured_at, match_window_minutes)
        if taken_after is None or taken_before is None:
            summary["unmatched"] += 1
            result["error"] = "Record captured_at is not a valid ISO datetime"
            result_records.append(result)
            continue

        time_window_candidates = await client.search_assets_by_metadata(
            original_file_name=file_name,
            taken_after=taken_after,
            taken_before=taken_before,
            page=1,
            size=10,
        )
        asset, match_strategy, error = _resolve_asset_match(
            record=item,
            time_window_candidates=time_window_candidates,
        )
        if asset is None and _should_fallback_from_time_window(item, time_window_candidates):
            filename_candidates = await client.search_assets_by_metadata(
                original_file_name=file_name,
                page=1,
                size=10,
            )
            asset, match_strategy, error = _resolve_asset_match(
                record=item,
                time_window_candidates=time_window_candidates,
                filename_candidates=filename_candidates,
            )

        if asset is None:
            if error == "No asset matched by file name":
                summary["unmatched"] += 1
            elif error == "No asset matched file name within takenAfter/takenBefore window":
                summary["unmatched"] += 1
            else:
                summary["ambiguous"] += 1
                result["match_status"] = "ambiguous"
            result["error"] = error
            result_records.append(result)
            continue

        summary["matched"] += 1
        if match_strategy == "filename+taken_window":
            summary["matched_by_time_window"] += 1
        elif match_strategy == "filename+path_suffix":
            summary["matched_by_path_suffix"] += 1
        elif match_strategy == "filename+fallback_time":
            summary["matched_by_fallback_time"] += 1

        result["match_strategy"] = match_strategy
        result["matched_asset_id"] = asset.id
        result["matched_asset_original_path"] = asset.original_path

        description = render_immich_description(item)
        if dry_run:
            summary["planned_updates"] += 1
            result["match_status"] = "matched"
            result_records.append(result)
            continue

        try:
            await client.update_assets_description([asset.id], description)
        except httpx.HTTPError as exc:
            summary["update_failed"] += 1
            result["match_status"] = "update_failed"
            result["error"] = str(exc)
        else:
            summary["updated"] += 1
            result["match_status"] = "updated"
            result["description_updated"] = True
        result_records.append(result)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "immich_url": immich_url,
        "dry_run": dry_run,
        "summary": summary,
        "records": result_records,
    }


def _load_json_object(
    path: Path,
    *,
    missing_message: str,
    invalid_message: str,
    wrong_type_message: str,
) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(missing_message.format(path=path)) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(invalid_message.format(path=path, error=exc)) from exc
    if not isinstance(payload, dict):
        raise SystemExit(wrong_type_message)
    return payload


def _load_text_file(path: Path, *, missing_message: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemExit(missing_message.format(path=path)) from exc


def _default_output_path(path: Path, suffix: str) -> Path:
    if path.suffix:
        return path.with_name(f"{path.stem}.{suffix}.json")
    return path.with_name(f"{path.name}.{suffix}.json")


def _search_window(captured_at: str, window_minutes: int) -> tuple[str | None, str | None]:
    captured_dt = _parse_iso_datetime(captured_at)
    if captured_dt is None:
        return None, None
    delta = timedelta(minutes=window_minutes)
    return (captured_dt - delta).isoformat(), (captured_dt + delta).isoformat()


def _album_name_for_month(month: str, *, prefix: str) -> str:
    stripped_prefix = prefix.strip()
    return f"{stripped_prefix} {month}".strip()


def _should_skip_record(record: dict[str, object]) -> bool:
    candidate = _string_or_none(record.get("file_path")) or _string_or_none(record.get("file_name"))
    if candidate is None:
        return False
    return Path(candidate).suffix.lower() not in SUPPORTED_EXTENSIONS


def _should_fallback_from_time_window(
    record: dict[str, object],
    time_window_candidates: list[ImmichAsset],
) -> bool:
    if time_window_candidates:
        return False
    metadata_source = record.get("metadata_source")
    captured_source = None
    if isinstance(metadata_source, dict):
        raw = metadata_source.get("captured_at")
        if isinstance(raw, str):
            captured_source = raw.strip()
    return captured_source in {None, "", "file_timestamp", "file_name", "exif"}


def _resolve_asset_match(
    *,
    record: dict[str, object],
    time_window_candidates: list[ImmichAsset],
    filename_candidates: list[ImmichAsset] | None = None,
) -> tuple[ImmichAsset | None, str | None, str]:
    if len(time_window_candidates) == 1:
        return time_window_candidates[0], "filename+taken_window", ""
    if len(time_window_candidates) > 1:
        return None, None, "Multiple assets matched file name within takenAfter/takenBefore window"
    if filename_candidates is None:
        return None, None, "No asset matched file name within takenAfter/takenBefore window"
    if not filename_candidates:
        return None, None, "No asset matched by file name"
    if len(filename_candidates) == 1:
        return filename_candidates[0], "filename+fallback_time", ""

    path_suffix_matches = _match_assets_by_path_suffix(record, filename_candidates)
    if len(path_suffix_matches) == 1:
        return path_suffix_matches[0], "filename+path_suffix", ""
    if len(path_suffix_matches) > 1:
        return None, None, "Multiple assets matched file name; could not resolve uniquely by path suffix"

    fallback_match = _match_assets_by_nearest_time(record, filename_candidates)
    if fallback_match is not None:
        return fallback_match, "filename+fallback_time", ""
    return None, None, "Multiple assets matched file name; could not resolve uniquely by path suffix"


def _match_assets_by_path_suffix(
    record: dict[str, object],
    candidates: list[ImmichAsset],
) -> list[ImmichAsset]:
    file_path = _string_or_none(record.get("file_path"))
    if file_path is None:
        return []
    record_suffixes = _candidate_path_suffixes(file_path)
    if not record_suffixes:
        return []

    matches: list[ImmichAsset] = []
    for asset in candidates:
        asset_path = asset.original_path
        if asset_path is None:
            continue
        normalized_asset_path = _normalize_path(asset_path)
        if any(normalized_asset_path.endswith(suffix) for suffix in record_suffixes):
            matches.append(asset)
    return matches


def _match_assets_by_nearest_time(
    record: dict[str, object],
    candidates: list[ImmichAsset],
) -> ImmichAsset | None:
    captured_at = _string_or_none(record.get("captured_at"))
    if captured_at is None:
        return None
    record_dt = _parse_iso_datetime(captured_at)
    if record_dt is None:
        return None

    ranked: list[tuple[float, ImmichAsset]] = []
    for asset in candidates:
        asset_dt = _best_asset_datetime(asset)
        if asset_dt is None:
            continue
        ranked.append((abs((asset_dt - record_dt).total_seconds()), asset))

    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    best_delta, best_asset = ranked[0]
    if best_delta > 172800:
        return None
    if len(ranked) > 1 and ranked[1][0] == best_delta:
        return None
    return best_asset


def _best_asset_datetime(asset: ImmichAsset) -> datetime | None:
    for value in (asset.local_date_time, asset.file_created_at, asset.created_at):
        if value is None:
            continue
        parsed = _parse_iso_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _candidate_path_suffixes(path: str) -> list[str]:
    normalized = _normalize_path(path)
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return []

    suffixes: list[str] = []
    for anchor in ("admin", "library"):
        if anchor in parts:
            index = parts.index(anchor)
            suffixes.append("/".join(parts[index:]))

    for count in (4, 3, 2):
        if len(parts) >= count:
            suffixes.append("/".join(parts[-count:]))

    deduped: list[str] = []
    for item in suffixes:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def _parse_iso_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


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
    return ImmichAsset(
        id=asset_id,
        original_file_name=file_name,
        original_path=_string_or_none(payload.get("originalPath")),
        local_date_time=_string_or_none(payload.get("localDateTime")),
        file_created_at=_string_or_none(payload.get("fileCreatedAt")),
        created_at=_string_or_none(payload.get("createdAt")),
    )


def _parse_album(payload: dict[str, object]) -> ImmichAlbum:
    album_id = _string_or_none(payload.get("id"))
    album_name = _string_or_none(payload.get("albumName"))
    if album_id is None or album_name is None:
        raise ValueError("Immich album must include id and albumName")
    assets_payload = payload.get("assets")
    asset_ids: set[str] = set()
    if isinstance(assets_payload, list):
        for item in assets_payload:
            if not isinstance(item, dict):
                continue
            asset_id_value = _string_or_none(item.get("id"))
            if asset_id_value is not None:
                asset_ids.add(asset_id_value)
    return ImmichAlbum(
        id=album_id,
        album_name=album_name,
        description=_string_or_none(payload.get("description")),
        asset_ids=asset_ids,
    )
