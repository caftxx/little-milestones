from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PhotoMetadata:
    captured_at: str | None = None
    timezone: str | None = None
    location: str | None = None
    gps: dict[str, float] | None = None
    device: dict[str, str] | None = None
    metadata_source: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class VisionDescription:
    summary: str
    baby_present: bool
    actions: list[str]
    expressions: list[str]
    scene: str | None
    objects: list[str]
    highlights: list[str]
    uncertainty: str | None


@dataclass(slots=True)
class VisionProvider:
    name: str
    base_url: str
    model: str


@dataclass(slots=True)
class VisionResult:
    provider: VisionProvider
    description: VisionDescription
