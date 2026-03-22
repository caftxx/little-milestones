from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import httpx

from littlems.config import ProviderPoolSettings, ProviderSettings, load_provider_settings
from littlems.service import PhotoDescriptionService
from littlems.vision import BalancedVisionClient
from tqdm import tqdm

logger = logging.getLogger(__name__)


def build_service(settings: ProviderPoolSettings) -> PhotoDescriptionService:
    client = BalancedVisionClient(settings.providers)
    return PhotoDescriptionService(
        vision_client=client,
        provider_names=[provider.name for provider in settings.providers],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="littlems")
    subparsers = parser.add_subparsers(dest="command", required=True)

    describe = subparsers.add_parser("describe", help="Describe photos in a directory")
    describe.add_argument("--input", required=True, type=Path, help="Directory containing photos")
    describe.add_argument("--output", required=True, type=Path, help="Output JSON file path")
    describe.add_argument(
        "--provider-config",
        required=True,
        type=Path,
        help="JSON config file containing provider definitions",
    )
    describe.add_argument(
        "--recursive",
        action="store_true",
        default=True,
        help="Scan subdirectories recursively",
    )
    describe.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity for debugging",
    )
    describe.add_argument(
        "--log-path",
        type=Path,
        help="Log file output path; defaults to ./log/littlems.log",
    )

    validate = subparsers.add_parser("validate-config", help="Validate a provider config file")
    validate.add_argument(
        "--provider-config",
        required=True,
        type=Path,
        help="JSON config file containing provider definitions",
    )
    validate.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity for debugging",
    )
    validate.add_argument(
        "--log-path",
        type=Path,
        help="Log file output path; defaults to ./log/littlems.log",
    )
    validate.add_argument(
        "--probe",
        action="store_true",
        help="Probe each provider with a lightweight API request after validating the config",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level, _resolve_log_path(args))

    if args.command == "describe":
        settings = load_provider_settings(args.provider_config)
        logger.info(
            "starting describe command input=%s output=%s recursive=%s provider_config=%s providers=%s",
            args.input,
            args.output,
            args.recursive,
            args.provider_config,
            [provider.name for provider in settings.providers],
        )
        service = build_service(settings)
        resume_state = None
        inspect_resume = getattr(service, "inspect_resume_state", None)
        if callable(inspect_resume):
            resume_state = inspect_resume(
                args.input,
                args.output,
                recursive=args.recursive,
            )
            logger.info(
                "describe resume summary total=%s skipped=%s failed_to_retry=%s pending=%s",
                resume_state.total_files,
                resume_state.skipped,
                resume_state.failed_to_retry,
                resume_state.pending,
            )
        with tqdm(
            total=resume_state.total_files if resume_state is not None else 0,
            desc="Processing photos",
            unit="image",
            dynamic_ncols=True,
        ) as progress:
            if resume_state is not None and resume_state.skipped:
                progress.n = resume_state.skipped
                if hasattr(progress, "refresh"):
                    progress.refresh()
            asyncio.run(
                service.describe_to_file(
                    args.input,
                    args.output,
                    recursive=args.recursive,
                    progress_callback=lambda processed, total, image_path: _update_progress(
                        progress,
                        processed,
                        total,
                        image_path,
                    ),
                )
            )
        logger.info("describe command finished output=%s", args.output)
        return 0
    if args.command == "validate-config":
        settings = load_provider_settings(args.provider_config)
        if args.probe:
            probe_results = asyncio.run(_probe_provider_pool(settings))
            for result in probe_results:
                if result["ok"]:
                    print(
                        f"OK   {result['name']}  {result['base_url']}  model={result['model']}"
                    )
                else:
                    print(
                        f"FAIL {result['name']}  {result['base_url']}  model={result['model']}  "
                        f"kind={result['error_kind']}  error={result['error']}"
                    )
            failures = [result for result in probe_results if not result["ok"]]
            if failures:
                failed_names = ", ".join(str(result["name"]) for result in failures)
                raise SystemExit(f"Provider probe failed for: {failed_names}")
        else:
            print(
                f"Config OK: {args.provider_config} ({len(settings.providers)} providers)"
            )
        logger.info(
            "validated provider config path=%s providers=%s probe=%s",
            args.provider_config,
            [provider.name for provider in settings.providers],
            args.probe,
        )
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _resolve_log_path(args: argparse.Namespace) -> Path:
    if args.log_path is not None:
        return args.log_path
    return Path.cwd() / "log" / "littlems.log"


def _configure_logging(level_name: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )
    logging.captureWarnings(True)
    logger.debug("logging configured path=%s cwd=%s pid=%s", log_path, Path.cwd(), os.getpid())


def _update_progress(progress: tqdm, processed: int, total: int, image_path: Path) -> None:
    if progress.total != total:
        progress.total = total
    progress.update(processed - progress.n)


async def _probe_provider_pool(settings: ProviderPoolSettings) -> list[dict[str, object]]:
    tasks = [
        asyncio.create_task(_probe_provider(provider))
        for provider in settings.providers
    ]
    return await asyncio.gather(*tasks)


async def _probe_provider(provider: ProviderSettings) -> dict[str, object]:
    timeout = 5.0
    logger.info("probing provider name=%s base_url=%s model=%s", provider.name, provider.base_url, provider.vision_model)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{provider.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": provider.vision_model,
                    "temperature": 0,
                    "max_tokens": 1,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Reply with OK.",
                        }
                    ],
                },
            )
        if response.is_error:
            return {
                "name": provider.name,
                "base_url": provider.base_url,
                "model": provider.vision_model,
                "ok": False,
                "error_kind": _classify_http_error(response.status_code),
                "error": f"HTTP {response.status_code} {response.text.strip()}",
            }
        return {
            "name": provider.name,
            "base_url": provider.base_url,
            "model": provider.vision_model,
            "ok": True,
            "error_kind": None,
            "error": None,
        }
    except httpx.TimeoutException as exc:
        return {
            "name": provider.name,
            "base_url": provider.base_url,
            "model": provider.vision_model,
            "ok": False,
            "error_kind": "timeout",
            "error": str(exc),
        }
    except httpx.ConnectError as exc:
        return {
            "name": provider.name,
            "base_url": provider.base_url,
            "model": provider.vision_model,
            "ok": False,
            "error_kind": "connect_error",
            "error": str(exc),
        }
    except httpx.HTTPError as exc:
        return {
            "name": provider.name,
            "base_url": provider.base_url,
            "model": provider.vision_model,
            "ok": False,
            "error_kind": "http_error",
            "error": str(exc),
        }


def _classify_http_error(status_code: int) -> str:
    if status_code == 400:
        return "bad_request"
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 408:
        return "timeout"
    if status_code == 429:
        return "rate_limited"
    if 500 <= status_code <= 599:
        return "server_error"
    return "http_error"


if __name__ == "__main__":
    raise SystemExit(main())
