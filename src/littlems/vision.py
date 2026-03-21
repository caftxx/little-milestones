from __future__ import annotations

import base64
import io
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from littlems.models import PhotoMetadata, VisionDescription

logger = logging.getLogger(__name__)

MAX_INLINE_IMAGE_BYTES = 4_000_000
MAX_INLINE_IMAGE_DIMENSION = 4096
NORMALIZED_IMAGE_DIMENSION = 1536
NORMALIZED_IMAGE_QUALITY = 80


class OpenAIVisionClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def describe(self, image_path: Path, metadata: PhotoMetadata) -> VisionDescription:
        logger.debug("building vision request image=%s model=%s", image_path, self._model)
        schema_payload = _build_payload(
            image_path=image_path,
            metadata=metadata,
            model=self._model,
            response_format=_json_schema_response_format(),
        )

        try:
            return self._send_and_parse(
                image_path=image_path,
                payload=schema_payload,
                format_name="json_schema",
            )
        except httpx.HTTPStatusError as exc:
            if not _should_fallback_to_text(exc.response):
                raise RuntimeError(f"Vision model request failed: {exc}") from exc
            logger.warning(
                "vision request rejected structured format image=%s status=%s body=%s",
                image_path,
                exc.response.status_code,
                _response_text_excerpt(exc.response),
            )
            logger.info("falling back from json_schema to text image=%s", image_path)

        text_payload = _build_payload(
            image_path=image_path,
            metadata=metadata,
            model=self._model,
            response_format={"type": "text"},
        )
        try:
            return self._send_and_parse(
                image_path=image_path,
                payload=text_payload,
                format_name="text",
            )
        except Exception as exc:  # pragma: no cover - exercised through integration
            raise RuntimeError(f"Vision model request failed: {exc}") from exc

    def _send_and_parse(
        self,
        *,
        image_path: Path,
        payload: dict[str, object],
        format_name: str,
    ) -> VisionDescription:
        logger.info("sending vision request image=%s format=%s", image_path, format_name)
        response = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout,
        )
        if response.is_error:
            logger.warning(
                "vision request failed image=%s format=%s status=%s body=%s",
                image_path,
                format_name,
                response.status_code,
                _response_text_excerpt(response),
            )
        response.raise_for_status()
        content = _extract_content(response.json())
        logger.debug("vision response received image=%s format=%s", image_path, format_name)
        return _parse_description(content)


def _build_payload(
    *,
    image_path: Path,
    metadata: PhotoMetadata,
    model: str,
    response_format: dict[str, object],
) -> dict[str, object]:
    return {
        "model": model,
        "temperature": 0.1,
        "response_format": response_format,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You describe baby photos for later monthly reports. "
                    "Return JSON only with keys: summary, baby_present, actions, "
                    "expressions, scene, objects, highlights, uncertainty. "
                    "Do not invent time, location, or device details."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Known metadata:\n"
                            f"{json.dumps(_metadata_payload(metadata), ensure_ascii=False)}"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": _encode_image_as_data_url(image_path)},
                    },
                ],
            },
        ],
    }


def _json_schema_response_format() -> dict[str, object]:
    return {
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


def _metadata_payload(metadata: PhotoMetadata) -> dict[str, object]:
    return {
        "captured_at": metadata.captured_at,
        "timezone": metadata.timezone,
        "location": metadata.location,
        "gps": metadata.gps,
        "device": metadata.device,
    }


def _encode_image_as_data_url(image_path: Path) -> str:
    mime_type, image_bytes = _prepare_image_bytes(image_path)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _prepare_image_bytes(image_path: Path) -> tuple[str, bytes]:
    file_size = image_path.stat().st_size
    with Image.open(image_path) as image:
        width, height = image.size
    if file_size <= MAX_INLINE_IMAGE_BYTES and max(width, height) <= MAX_INLINE_IMAGE_DIMENSION:
        return _mime_type_for_suffix(image_path.suffix.lower().lstrip(".") or "jpeg"), image_path.read_bytes()

    logger.info(
        "normalizing image for model input image=%s size=%sx%s bytes=%s",
        image_path,
        width,
        height,
        file_size,
    )
    with Image.open(image_path) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        normalized.thumbnail((NORMALIZED_IMAGE_DIMENSION, NORMALIZED_IMAGE_DIMENSION))
        buffer = io.BytesIO()
        normalized.save(buffer, format="JPEG", quality=NORMALIZED_IMAGE_QUALITY)
        normalized_bytes = buffer.getvalue()
    logger.debug(
        "normalized image ready image=%s bytes=%s max_dimension=%s",
        image_path,
        len(normalized_bytes),
        NORMALIZED_IMAGE_DIMENSION,
    )
    return "image/jpeg", normalized_bytes


def _mime_type_for_suffix(suffix: str) -> str:
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(suffix, f"image/{suffix}")


def _extract_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Missing choices in model response")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("Missing message in model response")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content
    if isinstance(content, list):
        text_chunks = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "".join(text_chunks)
    raise ValueError("Unsupported model response content")


def _parse_description(content: str) -> VisionDescription:
    parsed = _load_json_object(content)
    return VisionDescription(
        summary=_required_string(parsed, "summary"),
        baby_present=_required_bool(parsed, "baby_present"),
        actions=_required_string_list(parsed, "actions"),
        expressions=_required_string_list(parsed, "expressions"),
        scene=_nullable_string(parsed, "scene"),
        objects=_required_string_list(parsed, "objects"),
        highlights=_required_string_list(parsed, "highlights"),
        uncertainty=_nullable_string(parsed, "uncertainty"),
    )


def _load_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) < 3:
            raise ValueError("Invalid fenced JSON response")
        cleaned = "\n".join(lines[1:-1]).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Model response must be a JSON object")
    return parsed


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Field {key!r} must be a non-empty string")
    return value.strip()


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Field {key!r} must be a boolean")
    return value


def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Field {key!r} must be a list of strings")
    return [item.strip() for item in value]


def _nullable_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Field {key!r} must be a string or null")
    text = value.strip()
    return text or None


def _should_fallback_to_text(response: httpx.Response) -> bool:
    if response.status_code < 400 or response.status_code >= 500:
        return False
    body = _response_text_excerpt(response).lower()
    return "response_format" in body or "json_schema" in body


def _response_text_excerpt(response: httpx.Response, limit: int = 500) -> str:
    text = response.text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
