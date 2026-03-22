from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ProviderSettings:
    name: str
    base_url: str
    api_key: str
    vision_model: str
    max_inflight: int = 1
    timeout: float | None = None


@dataclass(slots=True)
class ProviderPoolSettings:
    providers: list[ProviderSettings]


def load_provider_settings(config_path: Path) -> ProviderPoolSettings:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Provider config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Provider config file is not valid JSON: {config_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit("Provider config must be a JSON object")

    providers_payload = payload.get("providers")
    if not isinstance(providers_payload, list) or not providers_payload:
        raise SystemExit("Provider config must contain a non-empty 'providers' array")

    providers: list[ProviderSettings] = []
    seen_names: set[str] = set()
    for index, item in enumerate(providers_payload):
        provider = _parse_provider(item, index)
        if provider.name in seen_names:
            raise SystemExit(f"Provider '{provider.name}' is duplicated in config")
        seen_names.add(provider.name)
        providers.append(provider)
    return ProviderPoolSettings(providers=providers)


def _parse_provider(item: object, index: int) -> ProviderSettings:
    if not isinstance(item, dict):
        raise SystemExit(f"Provider at index {index} must be a JSON object")

    name = _required_string(item, "name", index)
    base_url = _required_string(item, "base_url", index).rstrip("/")
    api_key = _required_string(item, "api_key", index)
    vision_model = _required_string(item, "vision_model", index)
    max_inflight = _optional_positive_int(item.get("max_inflight"), "max_inflight", name, default=1)
    timeout = _optional_positive_float(item.get("timeout"), "timeout", name)

    return ProviderSettings(
        name=name,
        base_url=base_url,
        api_key=api_key,
        vision_model=vision_model,
        max_inflight=max_inflight,
        timeout=timeout,
    )


def _required_string(item: dict[str, object], field: str, index: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"Provider at index {index} must define non-empty '{field}'")
    return value.strip()


def _optional_positive_int(value: object, field: str, provider_name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0:
        raise SystemExit(f"Provider '{provider_name}' has invalid '{field}'; expected a positive integer")
    return value


def _optional_positive_float(value: object, field: str, provider_name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or value <= 0:
        raise SystemExit(f"Provider '{provider_name}' has invalid '{field}'; expected a positive number")
    return float(value)
