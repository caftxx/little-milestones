from __future__ import annotations

import argparse
import logging
from pathlib import Path

from littlems.config import Settings, load_settings
from littlems.service import PhotoDescriptionService
from littlems.vision import OpenAIVisionClient

logger = logging.getLogger(__name__)


def build_service(settings: Settings) -> PhotoDescriptionService:
    client = OpenAIVisionClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.vision_model,
    )
    return PhotoDescriptionService(vision_client=client, model_name=settings.vision_model)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="littlems")
    subparsers = parser.add_subparsers(dest="command", required=True)

    describe = subparsers.add_parser("describe", help="Describe photos in a directory")
    describe.add_argument("--input", required=True, type=Path, help="Directory containing photos")
    describe.add_argument("--output", required=True, type=Path, help="Output JSON file path")
    describe.add_argument(
        "--recursive",
        action="store_true",
        help="Scan subdirectories recursively",
    )
    describe.add_argument(
        "--openai-base-url",
        help="OpenAI-compatible API base URL; overrides OPENAI_BASE_URL",
    )
    describe.add_argument(
        "--openai-api-key",
        help="OpenAI-compatible API key; overrides OPENAI_API_KEY",
    )
    describe.add_argument(
        "--vision-model",
        help="Vision model name; overrides VISION_MODEL",
    )
    describe.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity for debugging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    if args.command == "describe":
        settings = _resolve_settings(args)
        logger.info(
            "starting describe command input=%s output=%s recursive=%s model=%s base_url=%s",
            args.input,
            args.output,
            args.recursive,
            settings.vision_model,
            settings.base_url,
        )
        service = build_service(settings)
        service.describe_to_file(args.input, args.output, recursive=args.recursive)
        logger.info("describe command finished output=%s", args.output)
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _resolve_settings(args: argparse.Namespace) -> Settings:
    env_settings = load_settings()
    settings = Settings(
        base_url=_clean_base_url(args.openai_base_url) or env_settings.base_url,
        api_key=(args.openai_api_key or env_settings.api_key),
        vision_model=(args.vision_model or env_settings.vision_model),
    )
    missing_fields = [
        env_name
        for env_name, value in (
            ("OPENAI_BASE_URL", settings.base_url),
            ("OPENAI_API_KEY", settings.api_key),
            ("VISION_MODEL", settings.vision_model),
        )
        if not value
    ]
    if missing_fields:
        missing_text = ", ".join(missing_fields)
        raise SystemExit(
            "Missing OpenAI configuration. Provide CLI arguments or set environment variables: "
            f"{missing_text}"
        )
    return settings


def _clean_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().rstrip("/")
    return text or None


def _configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
