from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from littlems.immich import (
    ImmichAlbum,
    ImmichAsset,
    ImmichClient,
    ImmichResumeState,
    default_album_sync_output_path,
    default_asset_sync_output_path,
    describe_immich_album,
    describe_immich_album_to_file,
    generate_immich_album_report,
    inspect_immich_resume_state,
    parse_immich_description,
    render_immich_description,
)
from littlems.models import PhotoMetadata, VisionDescription, VisionProvider, VisionResult


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


def test_parse_immich_description_restores_structured_fields() -> None:
    parsed = parse_immich_description(
        "宝宝躺在床上看向镜头\n动作: 踢腿，挥手\n表情: 开心\n场景: 卧室\n物件: 安抚巾，玩具熊\n亮点: 第一次主动盯镜头\n说明: 右手动作有些模糊"
    )

    assert parsed == {
        "summary": "宝宝躺在床上看向镜头",
        "actions": ["踢腿", "挥手"],
        "expressions": ["开心"],
        "scene": "卧室",
        "objects": ["安抚巾", "玩具熊"],
        "highlights": ["第一次主动盯镜头"],
        "uncertainty": "右手动作有些模糊",
    }


def test_immich_client_requests_expected_endpoints() -> None:
    observed: list[tuple[str, str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = None
        if request.content:
            payload = json.loads(request.content.decode("utf-8"))
        observed.append((request.method, request.url.path, payload))
        if request.method == "GET" and request.url.path == "/api/albums":
            return httpx.Response(200, json=[{"id": "album-1", "albumName": "宝宝成长 2026-03", "assetCount": 1}])
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
                                "originalMimeType": "image/jpeg",
                                "localDateTime": "2026-03-10T10:00:00",
                                "fileCreatedAt": "2026-03-10T02:00:00.000Z",
                                "createdAt": "2026-03-11T02:00:00.000Z",
                                "updatedAt": "2026-03-11T03:00:00.000Z",
                                "type": "IMAGE",
                                "description": "hello",
                            }
                        ],
                        "nextPage": None,
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/api/assets/asset-1":
            return httpx.Response(
                200,
                json={
                    "id": "asset-1",
                    "originalFileName": "a.jpg",
                    "originalPath": "/library/a.jpg",
                    "originalMimeType": "image/jpeg",
                    "localDateTime": "2026-03-10T10:00:00",
                    "fileCreatedAt": "2026-03-10T02:00:00.000Z",
                    "createdAt": "2026-03-11T02:00:00.000Z",
                    "updatedAt": "2026-03-11T03:00:00.000Z",
                    "type": "IMAGE",
                    "description": "hello",
                },
            )
        if request.method == "GET" and request.url.path == "/api/assets/asset-1/original":
            return httpx.Response(200, content=b"raw")
        if request.method == "PUT" and request.url.path == "/api/assets":
            return httpx.Response(204)
        if request.method == "PATCH" and request.url.path == "/api/albums/album-1":
            return httpx.Response(200, json={"id": "album-1", "albumName": "宝宝成长 2026-03", "description": payload["description"]})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        async with ImmichClient("http://immich.lan/api", "test-key", transport=transport) as client:
            albums = await client.list_albums()
            assets = await client.search_assets(album_ids=["album-1"])
            asset = await client.get_asset("asset-1")
            original = await client.fetch_asset_original("asset-1")
            await client.update_assets_description(["asset-1"], "hello")
            updated_album = await client.update_album_description("album-1", "album desc")
            assert albums[0].album_name == "宝宝成长 2026-03"
            assert assets[0].id == "asset-1"
            assert asset.description == "hello"
            assert original == b"raw"
            assert updated_album.description == "album desc"

    asyncio.run(run())

    assert ("GET", "/api/albums", None) in observed
    assert (
        "POST",
        "/api/search/metadata",
        {"albumIds": ["album-1"], "page": 1, "size": 200, "withExif": True, "order": "asc", "type": "IMAGE"},
    ) in observed
    assert ("GET", "/api/assets/asset-1", None) in observed
    assert ("GET", "/api/assets/asset-1/original", None) in observed
    assert ("PUT", "/api/assets", {"ids": ["asset-1"], "description": "hello"}) in observed
    assert ("PATCH", "/api/albums/album-1", {"description": "album desc"}) in observed


def test_immich_client_search_assets_without_album_filters_library() -> None:
    observed: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        observed.append(payload)
        return httpx.Response(200, json={"assets": {"items": [], "nextPage": None}})

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        async with ImmichClient("http://immich.lan/api", "test-key", transport=transport) as client:
            await client.search_assets()

    asyncio.run(run())
    assert observed == [{"page": 1, "size": 200, "withExif": True, "order": "asc", "type": "IMAGE"}]


def test_describe_immich_album_uploads_generated_descriptions(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    updates: list[tuple[list[str], str]] = []

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
            return [ImmichAlbum(id="album-1", album_name="宝宝成长 2026-03")]

        async def search_assets(self, *, album_ids: list[str], page_size: int = 200) -> list[ImmichAsset]:
            assert album_ids == ["album-1"]
            assert page_size == 200
            return [ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-10T10:00:00", file_created_at=None, created_at=None, updated_at=None)]

        async def get_asset(self, asset_id: str) -> ImmichAsset:
            assert asset_id == "asset-1"
            return ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-10T10:00:00", file_created_at=None, created_at=None, updated_at=None)

        async def fetch_asset_original(self, asset_id: str) -> bytes:
            assert asset_id == "asset-1"
            return b"image-bytes"

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            updates.append((asset_ids, description))

    class StubVisionClient:
        async def describe_input(self, image_input: VisionInputLike) -> VisionResult:
            assert image_input.image_name == "a.jpg"
            return _make_result("宝宝躺着看向前方", metadata=image_input.metadata)

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)
    monkeypatch.setattr(
        "littlems.immich.extract_photo_metadata_from_bytes",
        lambda image_bytes, *, image_name=None, mime_type=None: PhotoMetadata(captured_at="2026-03-10T10:00:00"),
    )

    result = asyncio.run(
        describe_immich_album(
            album_name="宝宝成长 2026-03",
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            vision_client=StubVisionClient(),
            upload_description=True,
        )
    )

    assert result["source"]["kind"] == "immich"
    assert result["summary"]["processed"] == 1
    assert result["records"][0]["description_origin"] == "generated"
    assert updates == [(["asset-1"], "宝宝躺着看向前方")]


def test_describe_immich_album_force_uploads_all_successful_records(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    updates: list[tuple[list[str], str]] = []

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            return None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets(self, *, album_ids: list[str] | None = None, page_size: int = 200) -> list[ImmichAsset]:
            return [
                ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-10T10:00:00", file_created_at=None, created_at=None, updated_at=None),
                ImmichAsset(id="asset-2", original_file_name="b.jpg", original_path="/library/b.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-11T10:00:00", file_created_at=None, created_at=None, updated_at=None),
            ]

        async def get_asset(self, asset_id: str) -> ImmichAsset:
            return ImmichAsset(id=asset_id, original_file_name=f"{asset_id}.jpg", original_path=f"/library/{asset_id}.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-11T10:00:00", file_created_at=None, created_at=None, updated_at=None)

        async def fetch_asset_original(self, asset_id: str) -> bytes:
            return b"image-bytes"

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            updates.append((asset_ids, description))

    class StubVisionClient:
        async def describe_input(self, image_input: VisionInputLike) -> VisionResult:
            return _make_result(f"summary-{image_input.image_name}", metadata=image_input.metadata)

    output_path.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "completed",
                "generated_at": "2026-03-22T00:00:00+00:00",
                "updated_at": "2026-03-22T00:00:00+00:00",
                "source": {
                    "kind": "immich",
                    "scope": "library",
                    "immich_url": "http://immich.lan/api",
                    "album_name": None,
                    "album_id": None,
                },
                "model": {"provider": "multi_provider_pool", "providers": ["vision-a"]},
                "input": {"upload_description": False},
                "summary": {"wall_clock_ms": 5},
                "records": [
                    {
                        "source_id": "asset-1",
                        "asset_id": "asset-1",
                        "source_kind": "immich",
                        "summary": "existing summary",
                    }
                ],
                "failures": [],
                "run_state": {"completed": ["asset-1"], "failed": [], "provider_metrics": {}},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)
    monkeypatch.setattr(
        "littlems.immich.extract_photo_metadata_from_bytes",
        lambda image_bytes, *, image_name=None, mime_type=None: PhotoMetadata(captured_at="2026-03-11T10:00:00"),
    )

    result = asyncio.run(
        describe_immich_album_to_file(
            album_name=None,
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            vision_client=StubVisionClient(),
            provider_names=["vision-a"],
            upload_description=True,
            force=True,
        )
    )

    assert result["summary"]["processed"] == 2
    assert updates == [
        (["asset-1"], "existing summary"),
        (["asset-2"], "summary-asset-2.jpg"),
    ]


def test_describe_immich_album_without_album_name_processes_full_library(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    searched: list[dict[str, object]] = []

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            return None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets(
            self,
            *,
            album_ids: list[str] | None = None,
            taken_after: str | None = None,
            taken_before: str | None = None,
            page_size: int = 200,
        ) -> list[ImmichAsset]:
            searched.append(
                {
                    "album_ids": album_ids,
                    "taken_after": taken_after,
                    "taken_before": taken_before,
                    "page_size": page_size,
                }
            )
            return [ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-10T10:00:00", file_created_at=None, created_at=None, updated_at=None)]

        async def get_asset(self, asset_id: str) -> ImmichAsset:
            return ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-10T10:00:00", file_created_at=None, created_at=None, updated_at=None)

        async def fetch_asset_original(self, asset_id: str) -> bytes:
            return b"image-bytes"

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            raise AssertionError("upload should not happen")

    class StubVisionClient:
        async def describe_input(self, image_input: VisionInputLike) -> VisionResult:
            return _make_result("宝宝躺着看向前方", metadata=image_input.metadata)

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)
    monkeypatch.setattr("littlems.immich.extract_photo_metadata_from_bytes", lambda image_bytes: PhotoMetadata(captured_at="2026-03-10T10:00:00"))

    result = asyncio.run(
        describe_immich_album(
            album_name=None,
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            vision_client=StubVisionClient(),
            upload_description=False,
        )
    )

    assert result["source"]["scope"] == "library"
    assert result["source"]["album_name"] is None
    assert searched == [{"album_ids": None, "taken_after": None, "taken_before": None, "page_size": 200}]


def test_inspect_immich_resume_state_reports_skipped_and_pending(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    output_path.write_text(
        json.dumps(
            {
                "version": 3,
                "source": {
                    "kind": "immich",
                    "scope": "album",
                    "immich_url": "http://immich.lan/api",
                    "album_name": "宝宝成长 2026-03",
                    "album_id": "album-1",
                },
                "model": {"provider": "multi_provider_pool", "providers": ["vision-a"]},
                "summary": {"wall_clock_ms": 5},
                "input": {"upload_description": False},
                "records": [
                    {"source_id": "asset-1", "asset_id": "asset-1", "file_name": "a.jpg", "file_path": "/library/a.jpg"}
                ],
                "failures": [
                    {"source_id": "asset-2", "asset_id": "asset-2", "file_name": "b.jpg", "file_path": "/library/b.jpg", "error": "boom"}
                ],
                "run_state": {"completed": ["asset-1"], "failed": ["asset-2"], "provider_metrics": {}},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            return None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def list_albums(self) -> list[ImmichAlbum]:
            return [ImmichAlbum(id="album-1", album_name="宝宝成长 2026-03")]

        async def search_assets(self, *, album_ids: list[str], page_size: int = 200) -> list[ImmichAsset]:
            return [
                ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time=None, file_created_at=None, created_at=None, updated_at=None),
                ImmichAsset(id="asset-2", original_file_name="b.jpg", original_path="/library/b.jpg", original_mime_type="image/jpeg", local_date_time=None, file_created_at=None, created_at=None, updated_at=None),
                ImmichAsset(id="asset-3", original_file_name="c.jpg", original_path="/library/c.jpg", original_mime_type="image/jpeg", local_date_time=None, file_created_at=None, created_at=None, updated_at=None),
            ]

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)

    state = asyncio.run(
        inspect_immich_resume_state(
            album_name="宝宝成长 2026-03",
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            provider_names=["vision-a"],
            upload_description=False,
        )
    )

    assert state == ImmichResumeState(total_assets=3, skipped=1, failed_to_retry=1, pending=2)


def test_describe_immich_album_to_file_resumes_and_retries_failures(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    output_path.write_text(
        json.dumps(
            {
                "version": 3,
                "status": "running",
                "generated_at": "2026-03-22T00:00:00+00:00",
                "updated_at": "2026-03-22T00:00:00+00:00",
                "source": {
                    "kind": "immich",
                    "scope": "album",
                    "immich_url": "http://immich.lan/api",
                    "album_name": "宝宝成长 2026-03",
                    "album_id": "album-1",
                },
                "model": {"provider": "multi_provider_pool", "providers": ["vision-a"]},
                "summary": {"wall_clock_ms": 5},
                "input": {"upload_description": False},
                "records": [
                    {"source_id": "asset-1", "asset_id": "asset-1", "file_name": "a.jpg", "file_path": "/library/a.jpg", "summary": "old"}
                ],
                "failures": [
                    {"source_id": "asset-2", "asset_id": "asset-2", "file_name": "b.jpg", "file_path": "/library/b.jpg", "error": "boom"}
                ],
                "run_state": {"completed": ["asset-1"], "failed": ["asset-2"], "provider_metrics": {}},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    seen_asset_ids: list[str] = []

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            return None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def list_albums(self) -> list[ImmichAlbum]:
            return [ImmichAlbum(id="album-1", album_name="宝宝成长 2026-03")]

        async def search_assets(self, *, album_ids: list[str], page_size: int = 200) -> list[ImmichAsset]:
            return [
                ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-10T10:00:00", file_created_at=None, created_at=None, updated_at=None),
                ImmichAsset(id="asset-2", original_file_name="b.jpg", original_path="/library/b.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-11T10:00:00", file_created_at=None, created_at=None, updated_at=None),
            ]

        async def get_asset(self, asset_id: str) -> ImmichAsset:
            seen_asset_ids.append(asset_id)
            return ImmichAsset(id=asset_id, original_file_name=f"{asset_id}.jpg", original_path=f"/library/{asset_id}.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-11T10:00:00", file_created_at=None, created_at=None, updated_at=None)

        async def fetch_asset_original(self, asset_id: str) -> bytes:
            return b"image-bytes"

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            raise AssertionError("upload should not happen")

    class StubVisionClient:
        async def describe_input(self, image_input: VisionInputLike) -> VisionResult:
            return _make_result("new", metadata=image_input.metadata)

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)
    monkeypatch.setattr(
        "littlems.immich.extract_photo_metadata_from_bytes",
        lambda image_bytes, *, image_name=None, mime_type=None: PhotoMetadata(captured_at="2026-03-11T10:00:00"),
    )

    asyncio.run(
        describe_immich_album_to_file(
            album_name="宝宝成长 2026-03",
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            vision_client=StubVisionClient(),
            provider_names=["vision-a"],
            upload_description=False,
        )
    )

    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["status"] == "completed"
    assert written["summary"]["skipped"] == 1
    assert written["summary"]["remaining"] == 0
    assert written["run_state"]["completed"] == ["asset-1", "asset-2"]
    assert written["run_state"]["failed"] == []
    assert seen_asset_ids == ["asset-2"]


def test_describe_immich_album_to_file_allows_mismatched_upload_flag(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    output_path.write_text(
        json.dumps(
            {
                "version": 3,
                "source": {
                    "kind": "immich",
                    "scope": "album",
                    "immich_url": "http://immich.lan/api",
                    "album_name": "宝宝成长 2026-03",
                    "album_id": "album-1",
                },
                "model": {"provider": "multi_provider_pool", "providers": ["vision-a"]},
                "summary": {"wall_clock_ms": 5},
                "input": {"upload_description": True},
                "records": [],
                "failures": [],
                "run_state": {"completed": [], "failed": [], "provider_metrics": {}},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            return None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def list_albums(self) -> list[ImmichAlbum]:
            return [ImmichAlbum(id="album-1", album_name="宝宝成长 2026-03")]

        async def search_assets(self, *, album_ids: list[str], page_size: int = 200) -> list[ImmichAsset]:
            return [ImmichAsset(id="asset-1", original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time=None, file_created_at=None, created_at=None, updated_at=None)]

        async def get_asset(self, asset_id: str) -> ImmichAsset:
            return ImmichAsset(id=asset_id, original_file_name="a.jpg", original_path="/library/a.jpg", original_mime_type="image/jpeg", local_date_time="2026-03-10T10:00:00", file_created_at=None, created_at=None, updated_at=None)

        async def fetch_asset_original(self, asset_id: str) -> bytes:
            return b"image-bytes"

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            return None

    class StubVisionClient:
        async def describe_input(self, image_input: VisionInputLike) -> VisionResult:
            return _make_result("new", metadata=image_input.metadata)

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)
    monkeypatch.setattr(
        "littlems.immich.extract_photo_metadata_from_bytes",
        lambda image_bytes, *, image_name=None, mime_type=None: PhotoMetadata(captured_at="2026-03-10T10:00:00"),
    )

    result = asyncio.run(
        describe_immich_album_to_file(
            album_name="宝宝成长 2026-03",
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            vision_client=StubVisionClient(),
            provider_names=["vision-a"],
            upload_description=False,
        )
    )

    assert result["status"] == "completed"
    assert result["summary"]["processed"] == 1


def test_generate_immich_album_report_reuses_existing_descriptions(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "report.md"
    json_output_path = tmp_path / "report.json"
    description_output_path = tmp_path / "descriptions.json"
    album_updates: list[tuple[str, str]] = []
    asset_updates: list[tuple[list[str], str]] = []

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            return None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def list_albums(self) -> list[ImmichAlbum]:
            return [ImmichAlbum(id="album-1", album_name="宝宝成长 2026-03")]

        async def search_assets(self, *, album_ids: list[str], page_size: int = 200) -> list[ImmichAsset]:
            return [
                ImmichAsset(
                    id="asset-1",
                    original_file_name="a.jpg",
                    original_path="/library/a.jpg",
                    original_mime_type="image/jpeg",
                    local_date_time="2026-03-10T10:00:00",
                    file_created_at=None,
                    created_at=None,
                    updated_at=None,
                    description="宝宝躺着看向前方",
                ),
                ImmichAsset(
                    id="asset-2",
                    original_file_name="b.jpg",
                    original_path="/library/b.jpg",
                    original_mime_type="image/jpeg",
                    local_date_time="2026-03-12T10:00:00",
                    file_created_at=None,
                    created_at=None,
                    updated_at=None,
                ),
            ]

        async def get_asset(self, asset_id: str) -> ImmichAsset:
            for asset in await self.search_assets(album_ids=["album-1"]):
                if asset.id == asset_id:
                    return asset
            raise AssertionError(asset_id)

        async def fetch_asset_original(self, asset_id: str) -> bytes:
            assert asset_id == "asset-2"
            return b"image-bytes"

        async def update_assets_description(self, asset_ids: list[str], description: str) -> None:
            asset_updates.append((asset_ids, description))

        async def update_album_description(self, album_id: str, description: str) -> ImmichAlbum:
            album_updates.append((album_id, description))
            return ImmichAlbum(id=album_id, album_name="宝宝成长 2026-03", description=description)

    async def fake_generate_report_for_records(**kwargs: object) -> dict[str, object]:
        Path(kwargs["output_path"]).write_text("# report\n", encoding="utf-8")
        json_path = kwargs["json_output_path"]
        assert isinstance(json_path, Path)
        json_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return {"markdown": "# report\n"}

    class StubVisionClient:
        async def describe_input(self, image_input: VisionInputLike) -> VisionResult:
            return _make_result("宝宝在垫子上独坐", metadata=image_input.metadata)

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)
    monkeypatch.setattr(
        "littlems.immich.extract_photo_metadata_from_bytes",
        lambda image_bytes, *, image_name=None, mime_type=None: PhotoMetadata(captured_at="2026-03-12T10:00:00"),
    )
    monkeypatch.setattr("littlems.immich.generate_report_for_records", fake_generate_report_for_records)

    result = asyncio.run(
        generate_immich_album_report(
            album_name="宝宝成长 2026-03",
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            vision_client=StubVisionClient(),
            report_settings=object(),
            date_from="2026-03-01",
            date_to="2026-03-31",
            birth_date="2025-12-20",
            baby_name="小满",
            json_output_path=json_output_path,
            description_output_path=description_output_path,
            upload_description=True,
        )
    )

    assert result["markdown"] == "# report\n"
    written = json.loads(description_output_path.read_text(encoding="utf-8"))
    assert written["summary"]["processed"] == 2
    assert written["records"][0]["description_origin"] == "metadata"
    assert written["records"][1]["description_origin"] == "generated"
    assert asset_updates == [(["asset-2"], "宝宝在垫子上独坐")]
    assert album_updates == [("album-1", "# report\n")]


def test_generate_immich_album_report_with_date_range_searches_without_album(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "report.md"
    searched: list[dict[str, object]] = []

    class StubImmichClient:
        def __init__(self, base_url: str, api_key: str, *, transport: object = None) -> None:
            return None

        async def __aenter__(self) -> StubImmichClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def search_assets(
            self,
            *,
            album_ids: list[str] | None = None,
            taken_after: str | None = None,
            taken_before: str | None = None,
            page_size: int = 200,
        ) -> list[ImmichAsset]:
            searched.append(
                {
                    "album_ids": album_ids,
                    "taken_after": taken_after,
                    "taken_before": taken_before,
                    "page_size": page_size,
                }
            )
            return [
                ImmichAsset(
                    id="asset-1",
                    original_file_name="a.jpg",
                    original_path="/library/a.jpg",
                    original_mime_type="image/jpeg",
                    local_date_time="2026-03-10T10:00:00",
                    file_created_at=None,
                    created_at=None,
                    updated_at=None,
                    description="宝宝躺着看向前方",
                )
            ]

        async def get_asset(self, asset_id: str) -> ImmichAsset:
            return ImmichAsset(
                id="asset-1",
                original_file_name="a.jpg",
                original_path="/library/a.jpg",
                original_mime_type="image/jpeg",
                local_date_time="2026-03-10T10:00:00",
                file_created_at=None,
                created_at=None,
                updated_at=None,
                description="宝宝躺着看向前方",
            )

        async def update_album_description(self, album_id: str, description: str) -> ImmichAlbum:
            raise AssertionError("album update should not happen without album selection")

    async def fake_generate_report_for_records(**kwargs: object) -> dict[str, object]:
        assert kwargs["date_from"] == "2026-03-01"
        assert kwargs["date_to"] == "2026-03-31"
        return {"markdown": "# report\n"}

    monkeypatch.setattr("littlems.immich.ImmichClient", StubImmichClient)
    monkeypatch.setattr("littlems.immich.generate_report_for_records", fake_generate_report_for_records)

    result = asyncio.run(
        generate_immich_album_report(
            album_name=None,
            output_path=output_path,
            immich_url="http://immich.lan/api",
            api_key="test-key",
            vision_client=object(),
            report_settings=object(),
            date_from="2026-03-01",
            date_to="2026-03-31",
            birth_date="2025-12-20",
            baby_name="小满",
            upload_description=False,
        )
    )

    assert result["markdown"] == "# report\n"
    assert searched == [
        {
            "album_ids": None,
            "taken_after": "2026-03-01T00:00:00",
            "taken_before": "2026-03-31T23:59:59",
            "page_size": 200,
        }
    ]


class VisionInputLike:
    image_name: str
    metadata: PhotoMetadata


def _make_result(summary: str, *, metadata: PhotoMetadata) -> VisionResult:
    return VisionResult(
        provider=VisionProvider(name="vision-a", base_url="http://a.example/v1", model="model-a"),
        description=VisionDescription(
            summary=summary,
            baby_present=True,
            actions=[],
            expressions=[],
            scene=None,
            objects=[],
            highlights=[],
            uncertainty=None,
        ),
        provider_elapsed_ms=12,
        provider_attempts=[],
        metadata=metadata,
    )
