from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from littlems.config import ProviderSettings
from littlems.models import PhotoMetadata
from littlems.vision import (
    MAX_INLINE_IMAGE_BYTES,
    MAX_MODEL_IMAGE_BYTES,
    BalancedVisionClient,
    OpenAIVisionClient,
    _encode_image_as_data_url,
    _parse_description,
    _prepare_image_bytes,
    _prepare_inline_image_bytes,
)


def test_vision_client_uses_json_schema_when_supported(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)
    calls: list[dict[str, object]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
            timeout: float | None = None,
        ) -> httpx.Response:
            del headers, timeout
            calls.append(json)
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": json_dumps(
                                    {
                                        "summary": "安静的宝宝照片",
                                        "baby_present": True,
                                        "actions": ["平躺"],
                                        "expressions": ["平静"],
                                        "scene": "卧室",
                                        "objects": ["摇铃"],
                                        "highlights": ["抓握较稳"],
                                        "uncertainty": None,
                                    }
                                ),
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("littlems.vision.httpx.AsyncClient", FakeAsyncClient)

    client = OpenAIVisionClient("http://example.test/v1", "test-key", "vision-model")
    description = asyncio.run(client.describe(image_path, PhotoMetadata()))

    assert description.summary == "安静的宝宝照片"
    assert len(calls) == 1
    system_message = calls[0]["messages"][0]["content"]
    user_message = calls[0]["messages"][1]["content"][0]["text"]
    assert isinstance(system_message, str)
    assert isinstance(user_message, str)
    assert "只返回 JSON" in system_message
    assert "所有字符串 value 都必须使用简体中文" in system_message
    assert "key 必须保持以上英文" in system_message
    assert "已知元信息如下" in user_message
    assert calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "vision_description",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "summary": {"type": "string"},
                    "baby_present": {"type": "boolean"},
                    "actions": {"type": "array", "items": {"type": "string"}},
                    "expressions": {"type": "array", "items": {"type": "string"}},
                    "scene": {"type": ["string", "null"]},
                    "objects": {"type": "array", "items": {"type": "string"}},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                    "uncertainty": {"type": ["string", "null"]},
                },
                "required": [
                    "summary",
                    "baby_present",
                    "actions",
                    "expressions",
                    "scene",
                    "objects",
                    "highlights",
                    "uncertainty",
                ],
            },
        },
    }


def test_vision_client_falls_back_to_text_when_schema_is_rejected(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)
    calls: list[dict[str, object]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
            timeout: float | None = None,
        ) -> httpx.Response:
            del headers, timeout
            calls.append(json)
            request = httpx.Request("POST", url)
            if len(calls) == 1:
                return httpx.Response(
                    400,
                    request=request,
                    text='{"error":"\'response_format.type\' must be \'json_schema\' or \'text\'"}',
                )
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "```json\n"
                                + json_dumps(
                                    {
                                        "summary": "宝宝拿着玩具",
                                        "baby_present": True,
                                        "actions": ["抓着玩具"],
                                        "expressions": ["专注"],
                                        "scene": "卧室",
                                        "objects": ["玩具"],
                                        "highlights": ["在辅助下玩耍"],
                                        "uncertainty": "年龄不够明确",
                                    }
                                )
                                + "\n```"
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("littlems.vision.httpx.AsyncClient", FakeAsyncClient)

    client = OpenAIVisionClient("http://example.test/v1", "test-key", "vision-model")
    description = asyncio.run(client.describe(image_path, PhotoMetadata()))

    assert description.summary == "宝宝拿着玩具"
    assert len(calls) == 2
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"] == {"type": "text"}


def test_parse_description_accepts_chinese_values() -> None:
    description = _parse_description(
        json_dumps(
            {
                "summary": "宝宝趴着看向前方",
                "baby_present": True,
                "actions": ["趴卧", "抬头"],
                "expressions": ["专注"],
                "scene": "客厅地垫",
                "objects": ["地垫", "玩具"],
                "highlights": ["抬头更稳了"],
                "uncertainty": "拍摄时间无法确认",
            }
        )
    )

    assert description.summary == "宝宝趴着看向前方"
    assert description.actions == ["趴卧", "抬头"]
    assert description.scene == "客厅地垫"
    assert description.uncertainty == "拍摄时间无法确认"


def test_balanced_client_uses_lowest_inflight_and_provider_capacity(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)
    active = {"slow": 0, "fast": 0}
    seen: list[str] = []

    async def fake_describe(self: OpenAIVisionClient, image_path: Path, metadata: PhotoMetadata) -> object:
        name = "slow" if self._base_url.endswith("slow/v1") else "fast"
        active[name] += 1
        seen.append(name)
        await asyncio.sleep(0.05 if name == "slow" else 0.01)
        active[name] -= 1
        return _make_description(f"{name}-{image_path.name}")

    monkeypatch.setattr("littlems.vision.OpenAIVisionClient.describe", fake_describe)

    client = BalancedVisionClient(
        [
            ProviderSettings("slow", "http://example.test/slow/v1", "key", "model-slow", max_inflight=1),
            ProviderSettings("fast", "http://example.test/fast/v1", "key", "model-fast", max_inflight=2),
        ]
    )

    async def run() -> list[object]:
        tasks = [
            asyncio.create_task(client.describe(image_path.with_name(f"{index}.jpg"), PhotoMetadata()))
            for index in range(3)
        ]
        return await asyncio.gather(*tasks)

    results = asyncio.run(run())

    assert [result.provider.name for result in results] == ["slow", "fast", "fast"]
    assert seen.count("slow") == 1
    assert seen.count("fast") == 2
    assert active == {"slow": 0, "fast": 0}
    assert all(result.provider_elapsed_ms > 0 for result in results)
    assert all(len(result.provider_attempts) == 1 for result in results)


def test_balanced_client_retries_with_other_provider_on_failure(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)
    attempted: list[str] = []

    async def fake_describe(self: OpenAIVisionClient, image_path: Path, metadata: PhotoMetadata) -> object:
        provider_name = "first" if self._base_url.endswith("first/v1") else "second"
        attempted.append(provider_name)
        if provider_name == "first":
            raise RuntimeError("boom")
        return _make_description("ok")

    monkeypatch.setattr("littlems.vision.OpenAIVisionClient.describe", fake_describe)

    client = BalancedVisionClient(
        [
            ProviderSettings("first", "http://example.test/first/v1", "key", "model-first"),
            ProviderSettings("second", "http://example.test/second/v1", "key", "model-second"),
        ]
    )

    result = asyncio.run(client.describe(image_path, PhotoMetadata()))

    assert attempted == ["first", "second"]
    assert result.provider.name == "second"
    assert result.provider.model == "model-second"
    assert result.provider_elapsed_ms >= 0
    assert [attempt.provider_name for attempt in result.provider_attempts] == ["first", "second"]
    assert [attempt.ok for attempt in result.provider_attempts] == [False, True]


def test_balanced_client_raises_when_all_providers_fail(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)

    async def fake_describe(self: OpenAIVisionClient, image_path: Path, metadata: PhotoMetadata) -> object:
        raise RuntimeError(f"failed from {self._base_url}")

    monkeypatch.setattr("littlems.vision.OpenAIVisionClient.describe", fake_describe)

    client = BalancedVisionClient(
        [
            ProviderSettings("first", "http://example.test/first/v1", "key", "model-first"),
            ProviderSettings("second", "http://example.test/second/v1", "key", "model-second"),
        ]
    )

    with pytest.raises(RuntimeError, match="All providers failed") as exc_info:
        asyncio.run(client.describe(image_path, PhotoMetadata()))
    failure = exc_info.value
    assert getattr(failure, "provider_elapsed_ms") >= 0
    assert [attempt.provider_name for attempt in getattr(failure, "provider_attempts")] == ["first", "second"]


def test_balanced_client_reports_integer_elapsed_ms(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)

    async def fake_describe(self: OpenAIVisionClient, image_path: Path, metadata: PhotoMetadata) -> object:
        await asyncio.sleep(0.01)
        return _make_description("ok")

    monkeypatch.setattr("littlems.vision.OpenAIVisionClient.describe", fake_describe)

    client = BalancedVisionClient(
        [
            ProviderSettings("only", "http://example.test/only/v1", "key", "model-only"),
        ]
    )

    result = asyncio.run(client.describe(image_path, PhotoMetadata()))

    assert isinstance(result.provider_elapsed_ms, int)
    assert isinstance(result.provider_attempts[0].elapsed_ms, int)


def test_balanced_client_waits_for_provider_before_preparing_next_image(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)
    prepare_calls: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    def fake_prepare(path: Path):
        prepare_calls.append(path.name)
        return SimpleNamespace(metadata=PhotoMetadata())

    async def fake_describe(self: OpenAIVisionClient, image_path: Path, metadata: object) -> object:
        del metadata
        if image_path.name == "first.jpg":
            first_started.set()
            await release_first.wait()
        return _make_description("ok")

    monkeypatch.setattr("littlems.vision._prepare_vision_input", fake_prepare)
    monkeypatch.setattr("littlems.vision.OpenAIVisionClient.describe", fake_describe)

    client = BalancedVisionClient(
        [
            ProviderSettings("only", "http://example.test/only/v1", "key", "model-only", max_inflight=1),
        ]
    )

    async def run() -> None:
        first_task = asyncio.create_task(client.describe(image_path.with_name("first.jpg")))
        await first_started.wait()
        second_task = asyncio.create_task(client.describe(image_path.with_name("second.jpg")))
        await asyncio.sleep(0.02)
        assert prepare_calls == ["first.jpg"]
        release_first.set()
        await asyncio.gather(first_task, second_task)

    asyncio.run(run())
    assert prepare_calls == ["first.jpg", "second.jpg"]


def test_balanced_client_prepares_image_once_across_provider_retries(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)
    prepare_calls: list[str] = []
    attempted: list[str] = []

    def fake_prepare(path: Path):
        prepare_calls.append(path.name)
        return SimpleNamespace(metadata=PhotoMetadata())

    async def fake_describe(self: OpenAIVisionClient, image_path: Path, metadata: object) -> object:
        del image_path, metadata
        provider_name = "first" if self._base_url.endswith("first/v1") else "second"
        attempted.append(provider_name)
        if provider_name == "first":
            raise RuntimeError("boom")
        return _make_description("ok")

    monkeypatch.setattr("littlems.vision._prepare_vision_input", fake_prepare)
    monkeypatch.setattr("littlems.vision.OpenAIVisionClient.describe", fake_describe)

    client = BalancedVisionClient(
        [
            ProviderSettings("first", "http://example.test/first/v1", "key", "model-first"),
            ProviderSettings("second", "http://example.test/second/v1", "key", "model-second"),
        ]
    )

    result = asyncio.run(client.describe(image_path))

    assert attempted == ["first", "second"]
    assert prepare_calls == ["sample.jpg"]
    assert result.provider.name == "second"


def test_parse_description_fails_for_non_json_response() -> None:
    with pytest.raises(json.JSONDecodeError):
        _parse_description("not json")


def test_parse_description_fails_for_wrong_field_types() -> None:
    with pytest.raises(ValueError, match="baby_present"):
        _parse_description(
            json_dumps(
                {
                    "summary": "baby photo",
                    "baby_present": "yes",
                    "actions": ["holding"],
                    "expressions": ["happy"],
                    "scene": "bedroom",
                    "objects": ["toy"],
                    "highlights": ["smile"],
                    "uncertainty": None,
                }
            )
        )


def test_encode_image_as_data_url_uses_standard_jpeg_mime_type(tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)

    data_url = _encode_image_as_data_url(image_path)

    assert data_url.startswith("data:image/jpeg;base64,")


def test_prepare_image_bytes_keeps_small_original_image(tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)

    mime_type, image_bytes = _prepare_image_bytes(image_path)

    assert mime_type == "image/jpeg"
    assert image_bytes == image_path.read_bytes()
    assert len(image_bytes) <= MAX_MODEL_IMAGE_BYTES


def test_prepare_image_bytes_normalizes_large_image(tmp_path: Path) -> None:
    image_path = tmp_path / "large.jpg"
    Image.effect_noise((5000, 4000), 100.0).convert("RGB").save(
        image_path,
        format="JPEG",
        quality=100,
    )

    mime_type, image_bytes = _prepare_image_bytes(image_path)

    assert mime_type == "image/jpeg"
    assert len(image_bytes) < image_path.stat().st_size
    assert image_bytes != image_path.read_bytes()
    assert len(image_bytes) <= MAX_MODEL_IMAGE_BYTES


def test_prepare_inline_image_bytes_normalizes_large_image_under_budget(tmp_path: Path) -> None:
    image_path = tmp_path / "inline-large.jpg"
    Image.effect_noise((5000, 4000), 100.0).convert("RGB").save(
        image_path,
        format="JPEG",
        quality=100,
    )

    mime_type, image_bytes = _prepare_inline_image_bytes(
        image_bytes=image_path.read_bytes(),
        mime_type="image/jpeg",
        image_name=image_path.name,
    )

    assert mime_type == "image/jpeg"
    assert len(image_bytes) <= MAX_MODEL_IMAGE_BYTES


def test_prepare_image_bytes_normalizes_heif_into_jpeg(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.heif"
    image_path.write_bytes(b"heif-data")

    normalized = Image.new("RGB", (8, 8), color="white")

    class FakeImage:
        size = (8, 8)

        def __enter__(self) -> FakeImage:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def load(self) -> None:
            return None

        def getexif(self) -> dict[object, object]:
            return {}

        def copy(self) -> FakeImage:
            return self

        def convert(self, mode: str) -> Image.Image:
            assert mode == "RGB"
            return normalized.copy()

    monkeypatch.setattr("littlems.vision.open_image", lambda path: FakeImage())

    mime_type, image_bytes = _prepare_image_bytes(image_path)

    assert mime_type == "image/jpeg"
    assert image_bytes != b"heif-data"
    reopened = Image.open(io.BytesIO(image_bytes))
    assert reopened.format == "JPEG"


def test_prepare_image_bytes_normalizes_dng_into_jpeg(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.dng"
    image_path.write_bytes(b"dng-data")

    normalized = Image.new("RGB", (8, 8), color="white")

    class FakeImage:
        size = (8, 8)

        def __enter__(self) -> FakeImage:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def load(self) -> None:
            return None

        def getexif(self) -> dict[object, object]:
            return {}

        def copy(self) -> FakeImage:
            return self

        def convert(self, mode: str) -> Image.Image:
            assert mode == "RGB"
            return normalized.copy()

    monkeypatch.setattr("littlems.vision.open_image", lambda path: FakeImage())

    mime_type, image_bytes = _prepare_image_bytes(image_path)

    assert mime_type == "image/jpeg"
    assert image_bytes != b"dng-data"
    reopened = Image.open(io.BytesIO(image_bytes))
    assert reopened.format == "JPEG"


def _create_sample_image(tmp_path: Path) -> Path:
    image_path = tmp_path / "sample.jpg"
    Image.new("RGB", (8, 8), color="white").save(image_path, format="JPEG")
    return image_path


def _make_description(summary: str):
    from littlems.models import VisionDescription

    return VisionDescription(
        summary=summary,
        baby_present=True,
        actions=[],
        expressions=[],
        scene="room",
        objects=[],
        highlights=[],
        uncertainty=None,
    )


def json_dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)
