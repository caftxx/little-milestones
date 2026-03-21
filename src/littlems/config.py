from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class Settings:
    base_url: str | None
    api_key: str | None
    vision_model: str | None


def load_settings() -> Settings:
    return Settings(
        base_url=_optional_env("OPENAI_BASE_URL"),
        api_key=_optional_env("OPENAI_API_KEY"),
        vision_model=_optional_env("VISION_MODEL"),
    )


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text.rstrip("/") if name == "OPENAI_BASE_URL" else text
