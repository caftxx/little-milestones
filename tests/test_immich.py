from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from littlems.immich import (
    ImmichAlbum,
    ImmichAsset,
    ImmichClient,
    default_album_sync_output_path,
    default_asset_sync_output_path,
    render_immich_description,
    sync_album_description_to_immich,
    sync_asset_descriptions_to_immich,
)


def test_default_asset_sync_output_path_reuses_input_stem(tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"

    assert default_asset_sync_output_path(input_path) == tmp_path / "descriptions.immich-update-asset-description.json"


def test_default_album_sync_output_path_reuses_input_stem(tmp_path: Path) -> None:
    report_path = tmp_path / "report.md"

    assert default_album_sync_output_path(report_path) == tmp_path / "report.immich-update-album-description.json"


def test_render_immich_description_formats_expected_lines() -> None:
    rendered = render_immich_description(
        {
            "summary": "宝宝躺在床上看向镜头",
            "actions": ["踢腿", "挥手"],
            "expressions": ["开心"],
            "scene": "卧室",
            "objects": ["安抚巾", "玩具熊"],
            "highlights": ["第一次主动盯镜头"],
            "uncertainty": "右手动作有些模糊",
        }
    )

    assert rendered == (
        "宝宝躺在床上看向镜头\n"
        "动作: 踢腿，挥手\n"
        "表情: 开心\n"
        "场景: 卧室\n"
        "物件: 安抚巾，玩具熊\n"
        "亮点: 第一次主动盯镜头\n"
        "说明: 右手动作有些模糊"
    )


def test_sync_asset_descriptions_marks_unmatched_and_ambiguous_records(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"
    output_path = tmp_path / "sync.json"
    input_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "file_name": "solo.jpg",
                        "file_path": "/photos/solo.jpg",
                        "captured_at": "2026-03-22T18:52:36",
                        "summary": "single",
                        "actions": [],
                        "expressions": [],
                        "scene": None,
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                    },
                    {
                        "file_name": "missing.jpg",
                        "file_path": "/photos/missing.jpg",
                        "captured_at": "2026-03-22T18:52:36",
                        "summary": "missing",
                        "actions": [],
                        "expressions": [],
                        "scene": None,
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                    },
                    {
                        "file_name": "dupe.jpg",
                        "file_path": "/photos/dupe.jpg",
                        "captured_at": "2026-03-22T18:52:36",
                        "summary": "dupe",
                        "actions": [],
                        "expressions": [],
                        "scene": None,
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            assert base_url == "http://immich.lan/api"
            assert api_key == "test-key"
            assert transport is None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets_by_metadata(
            self,
            *,
            original_file_name: str,
            taken_after: str | None = None,
            taken_before: str | None = None,
            page: int = 1,
            size: int = 10,
        ) -> list[ImmichAsset]:
            assert page == 1
            assert size == 10
            if taken_after is None:
                return []
            assert taken_after == "2026-03-22T18:47:36"
            assert taken_before == "2026-03-22T18:57:36"
            if original_file_name == "solo.jpg":
                return [
                    ImmichAsset(
                        id="asset-1",
                        original_file_name=original_file_name,
                        original_path="/library/solo.jpg",
                        local_date_time="2026-03-22T18:52:36.000Z",
                        file_created_at=None,
                        created_at=None,
                    )
                ]
            if original_file_name == "dupe.jpg":
                return [
                    ImmichAsset(
                        id="asset-2",
                        original_file_name=original_file_name,
                        original_path="/library/dupe-a.jpg",
                        local_date_time="2026-03-22T18:52:36.000Z",
                        file_created_at=None,
                        created_at=None,
                    ),
                    ImmichAsset(
                        id="asset-3",
                        original_file_name=original_file_name,
                        original_path="/library/dupe-b.jpg",
                        local_date_time="2026-03-22T18:52:37.000Z",
                        file_created_at=None,
                        created_at=None,
                    ),
                ]
            return []

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            assert asset_ids == ["asset-1"]
            assert description == "single"

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_asset_descriptions_to_immich(
            input_path=input_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            output_path=output_path,
        )
    )

    assert result["summary"] == {
        "total_records": 3,
        "eligible_records": 3,
        "skipped": 0,
        "matched": 1,
        "updated": 1,
        "update_failed": 0,
        "unmatched": 1,
        "ambiguous": 1,
        "planned_updates": 0,
        "matched_by_time_window": 1,
        "matched_by_path_suffix": 0,
        "matched_by_fallback_time": 0,
    }
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["summary"] == result["summary"]
    assert [record["match_status"] for record in written["records"]] == ["updated", "unmatched", "ambiguous"]
    assert written["records"][0]["matched_asset_id"] == "asset-1"
    assert written["records"][0]["match_strategy"] == "filename+taken_window"
    assert "album_name" not in written["records"][0]


def test_sync_asset_descriptions_dry_run_does_not_mutate(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"
    input_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "file_name": "solo.jpg",
                        "file_path": "/photos/solo.jpg",
                        "captured_at": "2026-03-22T18:52:36",
                        "summary": "single",
                        "actions": ["踢腿"],
                        "expressions": [],
                        "scene": None,
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            del base_url, api_key, transport

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets_by_metadata(self, **kwargs: object) -> list[ImmichAsset]:
            assert kwargs["original_file_name"] == "solo.jpg"
            return [
                ImmichAsset(
                    id="asset-1",
                    original_file_name="solo.jpg",
                    original_path="/library/solo.jpg",
                    local_date_time="2026-03-22T18:52:36.000Z",
                    file_created_at=None,
                    created_at=None,
                )
            ]

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            raise AssertionError(f"dry-run should not update: {asset_ids} {description}")

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_asset_descriptions_to_immich(
            input_path=input_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            dry_run=True,
        )
    )

    assert result["dry_run"] is True
    assert result["summary"]["matched"] == 1
    assert result["summary"]["planned_updates"] == 1
    assert result["records"][0]["match_status"] == "matched"
    assert result["records"][0]["match_strategy"] == "filename+taken_window"
    assert result["records"][0]["description_updated"] is False


def test_sync_asset_descriptions_falls_back_to_filename_unique_match(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"
    input_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "file_name": "solo.jpg",
                        "file_path": "/photos/solo.jpg",
                        "captured_at": "2026-03-22T18:52:36",
                        "metadata_source": {"captured_at": "file_timestamp"},
                        "summary": "single",
                        "actions": [],
                        "expressions": [],
                        "scene": None,
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            del base_url, api_key, transport

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets_by_metadata(self, **kwargs: object) -> list[ImmichAsset]:
            if "taken_after" in kwargs and kwargs["taken_after"] is not None:
                return []
            return [
                ImmichAsset(
                    id="asset-1",
                    original_file_name="solo.jpg",
                    original_path="/library/solo.jpg",
                    local_date_time="2025-12-25T04:49:36.000Z",
                    file_created_at="2025-12-25T04:49:36.000Z",
                    created_at="2026-03-22T12:04:04.768Z",
                )
            ]

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            assert asset_ids == ["asset-1"]
            assert description == "single"

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_asset_descriptions_to_immich(
            input_path=input_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
        )
    )

    assert result["summary"]["matched"] == 1
    assert result["summary"]["matched_by_fallback_time"] == 1
    assert result["records"][0]["match_strategy"] == "filename+fallback_time"
    assert result["records"][0]["match_status"] == "updated"


def test_sync_asset_descriptions_falls_back_to_path_suffix_for_bad_timestamp(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"
    input_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "file_name": "9N5A0031.jpg",
                        "file_path": "/mnt/hd/immich/images/library/admin/2025/2025-12-25/9N5A0031.jpg",
                        "captured_at": "2026-03-22T20:04:04",
                        "metadata_source": {"captured_at": "file_timestamp"},
                        "summary": "camera shot",
                        "actions": [],
                        "expressions": [],
                        "scene": None,
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            del base_url, api_key, transport

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets_by_metadata(self, **kwargs: object) -> list[ImmichAsset]:
            if "taken_after" in kwargs and kwargs["taken_after"] is not None:
                return []
            return [
                ImmichAsset(
                    id="asset-a",
                    original_file_name="9N5A0031.jpg",
                    original_path="/data/library/admin/2025/2025-12-25/9N5A0031.jpg",
                    local_date_time="2025-12-25T04:49:36.000Z",
                    file_created_at="2025-12-25T04:49:36.000Z",
                    created_at="2026-03-22T12:04:04.768Z",
                ),
                ImmichAsset(
                    id="asset-b",
                    original_file_name="9N5A0031.jpg",
                    original_path="/data/library/admin/2025/2025-11-25/9N5A0031.jpg",
                    local_date_time="2025-11-25T04:49:36.000Z",
                    file_created_at="2025-11-25T04:49:36.000Z",
                    created_at="2026-03-21T12:04:04.768Z",
                ),
            ]

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            assert asset_ids == ["asset-a"]
            assert description == "camera shot"

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_asset_descriptions_to_immich(
            input_path=input_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
        )
    )

    assert result["summary"]["matched"] == 1
    assert result["summary"]["matched_by_path_suffix"] == 1
    assert result["records"][0]["match_strategy"] == "filename+path_suffix"
    assert result["records"][0]["matched_asset_id"] == "asset-a"


def test_sync_asset_descriptions_reports_ambiguous_when_filename_matches_cannot_be_resolved(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"
    input_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "file_name": "dupe.jpg",
                        "file_path": "/photos/dupe.jpg",
                        "captured_at": "2026-03-22T18:52:36",
                        "metadata_source": {"captured_at": "file_timestamp"},
                        "summary": "dupe",
                        "actions": [],
                        "expressions": [],
                        "scene": None,
                        "objects": [],
                        "highlights": [],
                        "uncertainty": None,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            del base_url, api_key, transport

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets_by_metadata(self, **kwargs: object) -> list[ImmichAsset]:
            if "taken_after" in kwargs and kwargs["taken_after"] is not None:
                return []
            return [
                ImmichAsset(
                    id="asset-a",
                    original_file_name="dupe.jpg",
                    original_path="/data/library/admin/a/dupe.jpg",
                    local_date_time="2025-12-25T04:49:36.000Z",
                    file_created_at="2025-12-25T04:49:36.000Z",
                    created_at="2026-03-22T12:04:04.768Z",
                ),
                ImmichAsset(
                    id="asset-b",
                    original_file_name="dupe.jpg",
                    original_path="/data/library/admin/b/dupe.jpg",
                    local_date_time="2025-12-26T04:49:36.000Z",
                    file_created_at="2025-12-26T04:49:36.000Z",
                    created_at="2026-03-22T12:04:04.768Z",
                ),
            ]

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            raise AssertionError(f"ambiguous match should not update: {asset_ids} {description}")

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_asset_descriptions_to_immich(
            input_path=input_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
        )
    )

    assert result["summary"]["ambiguous"] == 1
    assert result["records"][0]["match_status"] == "ambiguous"
    assert result["records"][0]["error"] == "Multiple assets matched file name; could not resolve uniquely by path suffix"


def test_sync_asset_descriptions_skips_unsupported_records(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"
    input_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "file_name": "clip.mp4",
                        "file_path": "/photos/clip.mp4",
                        "captured_at": "2026-03-22T18:52:36",
                        "summary": "video",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            del base_url, api_key, transport

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets_by_metadata(self, **kwargs: object) -> list[ImmichAsset]:
            raise AssertionError(f"unsupported record should not search: {kwargs}")

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_asset_descriptions_to_immich(
            input_path=input_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
        )
    )

    assert result["summary"]["skipped"] == 1
    assert result["records"][0]["match_status"] == "skipped"


def test_sync_album_description_updates_existing_album(monkeypatch, tmp_path: Path) -> None:
    report_path = tmp_path / "report.md"
    report_path.write_text("# 2026-03\n\n这是月报正文。", encoding="utf-8")

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            assert base_url == "http://immich.lan/api"
            assert api_key == "test-key"
            assert transport is None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def list_albums(self) -> list[ImmichAlbum]:
            return [ImmichAlbum(id="album-1", album_name="宝宝成长 2026-03", description="old")]

        async def update_album_info(self, album_id: str, *, description: str) -> ImmichAlbum:
            assert album_id == "album-1"
            assert description == "# 2026-03\n\n这是月报正文。"
            return ImmichAlbum(id="album-1", album_name="宝宝成长 2026-03", description=description)

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_album_description_to_immich(
            report_path=report_path,
            month="2026-03",
            immich_url="http://immich.lan/api",
            api_key="test-key",
            album_prefix="宝宝成长",
        )
    )

    assert result["summary"] == {
        "total_targets": 1,
        "updated": 1,
        "missing_album": 0,
        "ambiguous_album": 0,
        "update_failed": 0,
        "planned_updates": 0,
    }
    assert result["records"][0] == {
        "month": "2026-03",
        "album_name": "宝宝成长 2026-03",
        "matched_album_id": "album-1",
        "description_updated": True,
        "status": "updated",
        "error": None,
    }


def test_sync_album_description_records_missing_album(monkeypatch, tmp_path: Path) -> None:
    report_path = tmp_path / "report.md"
    report_path.write_text("月报内容", encoding="utf-8")

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            del base_url, api_key, transport

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def list_albums(self) -> list[ImmichAlbum]:
            return []

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_album_description_to_immich(
            report_path=report_path,
            month="2026-03",
            immich_url="http://immich.lan/api",
            api_key="test-key",
        )
    )

    assert result["summary"]["missing_album"] == 1
    assert result["records"][0]["status"] == "missing_album"


def test_sync_album_description_dry_run_does_not_mutate(monkeypatch, tmp_path: Path) -> None:
    report_path = tmp_path / "report.md"
    report_path.write_text("月报内容", encoding="utf-8")

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            del base_url, api_key, transport

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def list_albums(self) -> list[ImmichAlbum]:
            return [ImmichAlbum(id="album-1", album_name="2026-03", description="old")]

        async def update_album_info(self, album_id: str, *, description: str) -> ImmichAlbum:
            raise AssertionError(f"dry-run should not update album: {album_id} {description}")

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    result = asyncio.run(
        sync_album_description_to_immich(
            report_path=report_path,
            month="2026-03",
            immich_url="http://immich.lan/api",
            api_key="test-key",
            dry_run=True,
        )
    )

    assert result["summary"]["planned_updates"] == 1
    assert result["records"][0]["status"] == "planned_update"
    assert result["records"][0]["description_updated"] is False


def test_immich_client_requests_expected_endpoints() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            payload = json.loads(request.content.decode("utf-8"))
        else:
            payload = None
        requests.append((request.method, request.url.path, payload))

        if request.method == "POST" and request.url.path == "/api/search/metadata":
            return httpx.Response(
                200,
                json={
                    "assets": {
                        "items": [
                            {
                                "id": "asset-1",
                                "originalFileName": "a.jpg",
                                "originalPath": "/library/a.jpg",
                                "localDateTime": "2026-03-22T18:52:36.000Z",
                                "fileCreatedAt": "2026-03-22T10:52:36.000Z",
                                "createdAt": "2026-03-22T12:06:36.000Z",
                            }
                        ]
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/api/albums":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "album-1",
                        "albumName": "2026-03",
                        "description": "desc",
                        "assets": [{"id": "asset-1"}],
                    }
                ],
            )
        if request.method == "PUT" and request.url.path == "/api/assets":
            return httpx.Response(204)
        if request.method == "PATCH" and request.url.path == "/api/albums/album-1":
            return httpx.Response(
                200,
                json={
                    "id": "album-1",
                    "albumName": "2026-03",
                    "description": payload["description"],
                    "assets": [{"id": "asset-1"}],
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    async def run() -> None:
        async with ImmichClient(
            "http://immich.lan/api",
            "test-key",
            transport=httpx.MockTransport(handler),
        ) as client:
            assets = await client.search_assets_by_metadata(
                original_file_name="a.jpg",
                taken_after="2026-03-22T18:47:36",
                taken_before="2026-03-22T18:57:36",
                page=1,
                size=10,
            )
            albums = await client.list_albums()
            await client.update_assets_description(["asset-1"], "hello")
            updated_album = await client.update_album_info("album-1", description="album desc")

            assert [asset.id for asset in assets] == ["asset-1"]
            assert [(album.id, album.album_name) for album in albums] == [("album-1", "2026-03")]
            assert updated_album.description == "album desc"

    asyncio.run(run())

    assert requests == [
        (
            "POST",
            "/api/search/metadata",
            {
                "originalFileName": "a.jpg",
                "takenAfter": "2026-03-22T18:47:36",
                "takenBefore": "2026-03-22T18:57:36",
                "page": 1,
                "size": 10,
            },
        ),
        ("GET", "/api/albums", None),
        ("PUT", "/api/assets", {"ids": ["asset-1"], "description": "hello"}),
        ("PATCH", "/api/albums/album-1", {"description": "album desc"}),
    ]
