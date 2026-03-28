from __future__ import annotations

from datetime import UTC, datetime


DESCRIPTION_DOCUMENT_VERSION = 3


def build_description_document(
    *,
    source: dict[str, object],
    input_payload: dict[str, object] | None = None,
    model: dict[str, object] | None = None,
    provider_stats: dict[str, object] | None = None,
    records: list[dict[str, object]],
    failures: list[dict[str, object]],
    status: str = "completed",
    generated_at: str | None = None,
    run_state: dict[str, object] | None = None,
    summary_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    summary = {
        "total": len(records) + len(failures),
        "processed": len(records),
        "failed": len(failures),
        "skipped": 0,
        "remaining": 0,
        "wall_clock_ms": 0,
    }
    if summary_extra:
        summary.update(summary_extra)
    return {
        "version": DESCRIPTION_DOCUMENT_VERSION,
        "status": status,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "input": input_payload or {},
        "model": model or {},
        "provider_stats": provider_stats or {},
        "summary": summary,
        "records": records,
        "failures": failures,
        "run_state": run_state or {},
    }


def ensure_record_source_fields(
    record: dict[str, object],
    *,
    source_kind: str,
    source_id: str | None,
    source_path: str | None,
    source_uri: str | None,
    source_album_name: str | None = None,
    existing_description: str | None = None,
    description_origin: str = "generated",
) -> dict[str, object]:
    updated = dict(record)
    updated["source_kind"] = source_kind
    updated["source_id"] = source_id
    updated["source_path"] = source_path
    updated["source_uri"] = source_uri
    updated["source_album_name"] = source_album_name
    updated["existing_description"] = existing_description
    updated["description_origin"] = description_origin
    return updated
