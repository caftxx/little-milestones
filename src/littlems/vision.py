from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import ImageOps

from littlems.config import ProviderSettings
from littlems.exif import extract_photo_metadata
from littlems.imaging import open_image
from littlems.models import (
    PhotoMetadata,
    VisionDescription,
    VisionProvider,
    VisionProviderAttempt,
    VisionResult,
)

logger = logging.getLogger(__name__)

MAX_INLINE_IMAGE_BYTES = 4_000_000
MAX_INLINE_IMAGE_DIMENSION = 4096
NORMALIZED_IMAGE_DIMENSION = 1536
NORMALIZED_IMAGE_QUALITY = 80
DEFAULT_REQUEST_TIMEOUT = 60.0
ALWAYS_NORMALIZE_SUFFIXES = {"dng", "heif", "heic"}


class OpenAIVisionClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def describe(
        self,
        image_path: Path,
        prepared_input: PreparedVisionInput | PhotoMetadata,
    ) -> VisionDescription:
        if isinstance(prepared_input, PhotoMetadata):
            prepared_input = _prepared_input_from_metadata(image_path, prepared_input)
        logger.debug("building vision request image=%s model=%s", image_path, self._model)
        schema_payload = _build_payload(
            prepared_input=prepared_input,
            model=self._model,
            response_format=_json_schema_response_format(),
        )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                return await self._send_and_parse(
                    client=client,
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
                prepared_input=prepared_input,
                model=self._model,
                response_format={"type": "text"},
            )
            try:
                return await self._send_and_parse(
                    client=client,
                    image_path=image_path,
                    payload=text_payload,
                    format_name="text",
                )
            except Exception as exc:  # pragma: no cover - exercised through integration
                raise RuntimeError(f"Vision model request failed: {exc}") from exc

    async def _send_and_parse(
        self,
        *,
        client: httpx.AsyncClient,
        image_path: Path,
        payload: dict[str, object],
        format_name: str,
    ) -> VisionDescription:
        logger.info("sending vision request image=%s format=%s", image_path, format_name)
        response = await client.post(
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


@dataclass(slots=True)
class _ProviderRuntime:
    settings: ProviderSettings
    client: OpenAIVisionClient
    inflight: int = 0


@dataclass(slots=True)
class PreparedVisionInput:
    metadata: PhotoMetadata
    metadata_payload: dict[str, object]
    mime_type: str
    image_bytes: bytes
    data_url: str


class VisionProviderFailure(RuntimeError):
    def __init__(
        self,
        image_name: str,
        provider_attempts: list[VisionProviderAttempt],
    ) -> None:
        self.image_name = image_name
        self.provider_attempts = provider_attempts
        self.last_provider_name = provider_attempts[-1].provider_name if provider_attempts else None
        self.provider_elapsed_ms = sum(attempt.elapsed_ms for attempt in provider_attempts)
        joined_errors = "; ".join(
            f"{attempt.provider_name}: {attempt.error}"
            for attempt in provider_attempts
            if attempt.error
        )
        super().__init__(f"All providers failed for {image_name}: {joined_errors}")


class BalancedVisionClient:
    def __init__(self, providers: list[ProviderSettings]) -> None:
        if not providers:
            raise ValueError("BalancedVisionClient requires at least one provider")
        self._providers = [
            _ProviderRuntime(
                settings=provider,
                client=OpenAIVisionClient(
                    base_url=provider.base_url,
                    api_key=provider.api_key,
                    model=provider.vision_model,
                    timeout=provider.timeout or DEFAULT_REQUEST_TIMEOUT,
                ),
            )
            for provider in providers
        ]
        self._condition = asyncio.Condition()

    async def describe(self, image_path: Path, metadata: PhotoMetadata | None = None) -> VisionResult:
        attempted: set[int] = set()
        provider_attempts: list[VisionProviderAttempt] = []
        prepared_input: PreparedVisionInput | None = None

        while len(attempted) < len(self._providers):
            index, runtime = await self._acquire_provider(excluded=attempted)
            started_ns = time.perf_counter_ns()
            try:
                if metadata is None:
                    if prepared_input is None:
                        prepared_input = await asyncio.to_thread(_prepare_vision_input, image_path)
                    request_input: PreparedVisionInput | PhotoMetadata = prepared_input
                else:
                    request_input = metadata
                description = await runtime.client.describe(image_path, request_input)
                finished_ns = time.perf_counter_ns()
                elapsed_ms = _elapsed_ms_between(started_ns, finished_ns)
                attempt = VisionProviderAttempt(
                    provider_name=runtime.settings.name,
                    elapsed_ms=elapsed_ms,
                    ok=True,
                    started_at_monotonic_ns=started_ns,
                    finished_at_monotonic_ns=finished_ns,
                )
                provider_attempts.append(attempt)
                return VisionResult(
                    provider=VisionProvider(
                        name=runtime.settings.name,
                        base_url=runtime.settings.base_url,
                        model=runtime.settings.vision_model,
                    ),
                    description=description,
                    provider_elapsed_ms=sum(item.elapsed_ms for item in provider_attempts),
                    provider_attempts=provider_attempts,
                    metadata=prepared_input.metadata if prepared_input is not None else metadata,
                )
            except Exception as exc:
                finished_ns = time.perf_counter_ns()
                elapsed_ms = _elapsed_ms_between(started_ns, finished_ns)
                attempted.add(index)
                provider_attempts.append(
                    VisionProviderAttempt(
                        provider_name=runtime.settings.name,
                        elapsed_ms=elapsed_ms,
                        ok=False,
                        error=str(exc),
                        started_at_monotonic_ns=started_ns,
                        finished_at_monotonic_ns=finished_ns,
                    )
                )
                logger.warning(
                    "provider failed image=%s provider=%s error=%s",
                    image_path,
                    runtime.settings.name,
                    exc,
                )
            finally:
                await self._release_provider(index)

        raise VisionProviderFailure(image_path.name, provider_attempts)

    async def _acquire_provider(self, *, excluded: set[int]) -> tuple[int, _ProviderRuntime]:
        async with self._condition:
            while True:
                available = [
                    (index, runtime)
                    for index, runtime in enumerate(self._providers)
                    if index not in excluded and runtime.inflight < runtime.settings.max_inflight
                ]
                if available:
                    index, runtime = min(available, key=lambda item: (item[1].inflight, item[0]))
                    runtime.inflight += 1
                    logger.debug(
                        "acquired provider name=%s inflight=%s max_inflight=%s",
                        runtime.settings.name,
                        runtime.inflight,
                        runtime.settings.max_inflight,
                    )
                    return index, runtime
                await self._condition.wait()

    async def _release_provider(self, index: int) -> None:
        async with self._condition:
            runtime = self._providers[index]
            runtime.inflight -= 1
            logger.debug(
                "released provider name=%s inflight=%s max_inflight=%s",
                runtime.settings.name,
                runtime.inflight,
                runtime.settings.max_inflight,
            )
            self._condition.notify_all()


def _elapsed_ms_between(started_ns: int, finished_ns: int) -> int:
    return max(0, round((finished_ns - started_ns) / 1_000_000))


def _build_payload(
    *,
    prepared_input: PreparedVisionInput,
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
                    "你负责为宝宝照片生成后续月报可用的结构化描述。"
                    "只返回 JSON，不要输出代码块、解释、前后缀说明或额外文本。"
                    "JSON 对象只能包含这些固定 key：summary、baby_present、actions、"
                    "expressions、scene、objects、highlights、uncertainty。"
                    "key 必须保持以上英文，不要翻译、改名或新增字段。"
                    "除布尔值和 null 外，JSON 中所有字符串 value 都必须使用简体中文。"
                    "数组中的字符串元素也必须是简体中文。"
                    "不要编造素材里没有的时间、地点、设备细节或其他无法确认的信息。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "已知元信息如下，请结合图片理解，但不要超出事实推断：\n"
                            f"{json.dumps(prepared_input.metadata_payload, ensure_ascii=False)}"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": prepared_input.data_url},
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


def _prepare_vision_input(image_path: Path) -> PreparedVisionInput:
    metadata = extract_photo_metadata(image_path)
    return _prepared_input_from_metadata(image_path, metadata)


def _prepared_input_from_metadata(image_path: Path, metadata: PhotoMetadata) -> PreparedVisionInput:
    mime_type, image_bytes = _prepare_image_bytes(image_path)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return PreparedVisionInput(
        metadata=metadata,
        metadata_payload=_metadata_payload(metadata),
        mime_type=mime_type,
        image_bytes=image_bytes,
        data_url=f"data:{mime_type};base64,{encoded}",
    )


def _prepare_image_bytes(image_path: Path) -> tuple[str, bytes]:
    file_size = image_path.stat().st_size
    suffix = image_path.suffix.lower().lstrip(".") or "jpeg"
    with open_image(image_path) as image:
        width, height = image.size
        if (
            suffix not in ALWAYS_NORMALIZE_SUFFIXES
            and file_size <= MAX_INLINE_IMAGE_BYTES
            and max(width, height) <= MAX_INLINE_IMAGE_DIMENSION
        ):
            return _mime_type_for_suffix(suffix), image_path.read_bytes()

        logger.info(
            "normalizing image for model input image=%s size=%sx%s bytes=%s",
            image_path,
            width,
            height,
            file_size,
        )
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
        "heif": "image/heif",
        "heic": "image/heic",
        "dng": "image/x-adobe-dng",
    }.get(suffix, f"image/{suffix}")


def _extract_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Missing choices in model response")
    message = choices[0]
    if not isinstance(message, dict):
        raise ValueError("Missing message in model response")
    message_payload = message.get("message")
    if not isinstance(message_payload, dict):
        raise ValueError("Missing message payload in model response")
    reasoning_content = message_payload.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content
    content = message_payload.get("content")
    if not isinstance(content, str):
        raise ValueError("Missing content in model response")
    return content


def _parse_description(content: str) -> VisionDescription:
    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = normalized.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    data = json.loads(normalized)
    if not isinstance(data, dict):
        raise ValueError("Model response must be a JSON object")

    summary = _require_string(data, "summary")
    baby_present = _require_bool(data, "baby_present")
    actions = _require_string_list(data, "actions")
    expressions = _require_string_list(data, "expressions")
    scene = _require_optional_string(data, "scene")
    objects = _require_string_list(data, "objects")
    highlights = _require_string_list(data, "highlights")
    uncertainty = _require_optional_string(data, "uncertainty")

    return VisionDescription(
        summary=summary,
        baby_present=baby_present,
        actions=actions,
        expressions=expressions,
        scene=scene,
        objects=objects,
        highlights=highlights,
        uncertainty=uncertainty,
    )


def _require_string(data: dict[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _require_bool(data: dict[str, object], field: str) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _require_string_list(data: dict[str, object], field: str) -> list[str]:
    value = data.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def _require_optional_string(data: dict[str, object], field: str) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _should_fallback_to_text(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    body = _response_text_excerpt(response).lower()
    return "response_format" in body and "text" in body


def _response_text_excerpt(response: httpx.Response, limit: int = 500) -> str:
    text = response.text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
