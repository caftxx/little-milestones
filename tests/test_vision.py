from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from littlems.models import PhotoMetadata
from littlems.vision import (
    MAX_INLINE_IMAGE_BYTES,
    OpenAIVisionClient,
    _encode_image_as_data_url,
    _parse_description,
    _prepare_image_bytes,
)


def test_vision_client_uses_json_schema_when_supported(monkeypatch, tmp_path: Path) -> None:
    image_path = _create_sample_image(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float) -> httpx.Response:
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
                            )
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr("littlems.vision.httpx.post", fake_post)

    client = OpenAIVisionClient("http://example.test/v1", "test-key", "vision-model")
    description = client.describe(image_path, PhotoMetadata())

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

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float) -> httpx.Response:
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

    monkeypatch.setattr("littlems.vision.httpx.post", fake_post)

    client = OpenAIVisionClient("http://example.test/v1", "test-key", "vision-model")
    description = client.describe(image_path, PhotoMetadata())

    assert description.summary == "baby holding toy"
    assert len(calls) == 2
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[1]["response_format"] == {"type": "text"}


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


def json_dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)
