from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from littlems.config import ProviderSettings
from littlems.models import PhotoMetadata
from littlems.vision import (
    MAX_INLINE_IMAGE_BYTES,
    BalancedVisionClient,
    OpenAIVisionClient,
    _encode_image_as_data_url,
    _parse_description,
    _prepare_image_bytes,
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
                                        "summary": "calm baby photo",
                                        "baby_present": True,
                                        "actions": ["lying down"],
                                        "expressions": ["calm"],
                                        "scene": "bedroom",
                                        "objects": ["rattle"],
                                        "highlights": ["good grip"],
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

    assert description.summary == "calm baby photo"
    assert len(calls) == 1
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
                                        "summary": "baby holding toy",
                                        "baby_present": True,
                                        "actions": ["holding toy"],
                                        "expressions": ["focused"],
                                        "scene": "bedroom",
                                        "objects": ["toy"],
                                        "highlights": ["assisted play"],
                                        "uncertainty": "age unclear",
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

    assert description.summary == "baby holding toy"
    assert len(calls) == 2
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"] == {"type": "text"}


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

    with pytest.raises(RuntimeError, match="All providers failed"):
        asyncio.run(client.describe(image_path, PhotoMetadata()))


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


def test_prepare_image_bytes_normalizes_large_image(tmp_path: Path) -> None:
    image_path = tmp_path / "large.jpg"
    Image.new("RGB", (5000, 4000), color="white").save(
        image_path,
        format="JPEG",
        quality=100,
    )

    mime_type, image_bytes = _prepare_image_bytes(image_path)

    assert mime_type == "image/jpeg"
    assert len(image_bytes) < image_path.stat().st_size
    assert image_bytes != image_path.read_bytes()


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
