from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import httpx

from littlems.config import ProviderPoolSettings, ProviderSettings, load_provider_settings
from littlems.immich import (
    default_album_sync_output_path,
    default_asset_sync_output_path,
    sync_album_description_to_immich,
    sync_asset_descriptions_to_immich,
)
from littlems.report import generate_report_files
from littlems.service import PhotoDescriptionService
from littlems.vision import BalancedVisionClient
from tqdm import tqdm

logger = logging.getLogger(__name__)


def build_service(settings: ProviderPoolSettings, *, max_workers: int = 16) -> PhotoDescriptionService:
    client = BalancedVisionClient(settings.providers)
    return PhotoDescriptionService(
        vision_client=client,
        provider_names=[provider.name for provider in settings.providers],
        max_workers=max_workers,
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
    describe.add_argument(
        "--max-workers",
        type=_positive_int,
        default=16,
        help="Maximum number of file workers to run concurrently (default: 16)",
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

    generate_report = subparsers.add_parser("generate-report", help="Generate a warm Chinese monthly report from descriptions JSON")
    generate_report.add_argument("--input", required=True, type=Path, help="Describe output JSON file path")
    generate_report.add_argument("--month", required=True, help="Target month in YYYY-MM format")
    generate_report.add_argument("--birth-date", required=True, help="Baby birth date in YYYY-MM-DD format")
    generate_report.add_argument("--baby-name", required=True, help="Baby name used in report prompt context")
    generate_report.add_argument("--output", required=True, type=Path, help="Output Markdown file path")
    generate_report.add_argument(
        "--provider-config",
        required=True,
        type=Path,
        help="JSON config file containing provider definitions",
    )
    generate_report.add_argument(
        "--json-output",
        type=Path,
        help="Optional debug JSON output path",
    )
    generate_report.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity for debugging",
    )
    generate_report.add_argument(
        "--log-path",
        type=Path,
        help="Log file output path; defaults to ./log/littlems.log",
    )

    sync_immich = subparsers.add_parser("sync-immich", help="Sync descriptions and reports into Immich metadata")
    sync_subparsers = sync_immich.add_subparsers(dest="sync_command", required=True)

    update_asset_description = sync_subparsers.add_parser(
        "update-asset-description",
        help="Update Immich asset descriptions from descriptions JSON",
    )
    update_asset_description.add_argument("--input", required=True, type=Path, help="Describe output JSON file path")
    update_asset_description.add_argument("--immich-url", required=True, help="Immich API base URL, for example http://immich.lan/api")
    update_asset_description.add_argument(
        "--output",
        type=Path,
        help="Sync result JSON file path; defaults to <input>.immich-update-asset-description.json",
    )
    update_asset_description.add_argument(
        "--match-window-minutes",
        type=_positive_int,
        default=5,
        help="Allowed capture-time difference when matching assets (default: 5)",
    )
    update_asset_description.add_argument(
        "--skip-videos",
        action="store_true",
        default=True,
        help="Skip unsupported non-image records (default: enabled)",
    )
    update_asset_description.add_argument(
        "--include-videos",
        action="store_false",
        dest="skip_videos",
        help="Do not skip unsupported non-image records",
    )
    update_asset_description.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Immich metadata and write the sync report without mutating Immich",
    )
    update_asset_description.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity for debugging",
    )
    update_asset_description.add_argument(
        "--log-path",
        type=Path,
        help="Log file output path; defaults to ./log/littlems.log",
    )

    update_album_description = sync_subparsers.add_parser(
        "update-album-description",
        help="Update an Immich album description from a monthly report Markdown file",
    )
    update_album_description.add_argument("--report", required=True, type=Path, help="Monthly report Markdown file path")
    update_album_description.add_argument("--month", required=True, help="Target month in YYYY-MM format")
    update_album_description.add_argument("--immich-url", required=True, help="Immich API base URL, for example http://immich.lan/api")
    update_album_description.add_argument(
        "--album-prefix",
        default="",
        help="Optional prefix added before the YYYY-MM album name",
    )
    update_album_description.add_argument(
        "--output",
        type=Path,
        help="Sync result JSON file path; defaults to <report>.immich-update-album-description.json",
    )
    update_album_description.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Immich metadata and write the sync report without mutating Immich",
    )
    update_album_description.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity for debugging",
    )
    update_album_description.add_argument(
        "--log-path",
        type=Path,
        help="Log file output path; defaults to ./log/littlems.log",
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
        service = build_service(settings, max_workers=args.max_workers)
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
    if args.command == "generate-report":
        settings = load_provider_settings(args.provider_config)
        logger.info(
            "starting generate-report command input=%s month=%s birth_date=%s baby_name=%s output=%s provider_config=%s providers=%s",
            args.input,
            args.month,
            args.birth_date,
            args.baby_name,
            args.output,
            args.provider_config,
            [provider.name for provider in settings.providers],
        )
        asyncio.run(
            generate_report_files(
                input_path=args.input,
                month=args.month,
                birth_date=args.birth_date,
                baby_name=args.baby_name,
                output_path=args.output,
                settings=settings,
                json_output_path=args.json_output,
            )
        )
        logger.info("generate-report command finished output=%s", args.output)
        return 0
    if args.command == "sync-immich" and args.sync_command == "update-asset-description":
        api_key = os.getenv("IMMICH_API_KEY", "").strip()
        if not api_key:
            raise SystemExit("IMMICH_API_KEY environment variable is required for sync-immich")
        output_path = args.output if args.output is not None else default_asset_sync_output_path(args.input)
        logger.info(
            "starting sync-immich update-asset-description input=%s immich_url=%s output=%s dry_run=%s",
            args.input,
            args.immich_url,
            output_path,
            args.dry_run,
        )
        result = asyncio.run(
            sync_asset_descriptions_to_immich(
                input_path=args.input,
                immich_url=args.immich_url,
                api_key=api_key,
                output_path=output_path,
                match_window_minutes=args.match_window_minutes,
                skip_videos=args.skip_videos,
                dry_run=args.dry_run,
            )
        )
        summary = result["summary"]
        print(
            "Immich asset sync complete: "
            f"matched={summary['matched']} "
            f"updated={summary['updated']} "
            f"unmatched={summary['unmatched']} "
            f"ambiguous={summary['ambiguous']} "
            f"output={output_path}"
        )
        logger.info("sync-immich update-asset-description finished output=%s summary=%s", output_path, summary)
        return 0
    if args.command == "sync-immich" and args.sync_command == "update-album-description":
        api_key = os.getenv("IMMICH_API_KEY", "").strip()
        if not api_key:
            raise SystemExit("IMMICH_API_KEY environment variable is required for sync-immich")
        output_path = args.output if args.output is not None else default_album_sync_output_path(args.report)
        logger.info(
            "starting sync-immich update-album-description report=%s month=%s immich_url=%s output=%s dry_run=%s",
            args.report,
            args.month,
            args.immich_url,
            output_path,
            args.dry_run,
        )
        result = asyncio.run(
            sync_album_description_to_immich(
                report_path=args.report,
                month=args.month,
                immich_url=args.immich_url,
                api_key=api_key,
                album_prefix=args.album_prefix,
                output_path=output_path,
                dry_run=args.dry_run,
            )
        )
        summary = result["summary"]
        print(
            "Immich album sync complete: "
            f"updated={summary['updated']} "
            f"missing_album={summary['missing_album']} "
            f"update_failed={summary['update_failed']} "
            f"output={output_path}"
        )
        logger.info("sync-immich update-album-description finished output=%s summary=%s", output_path, summary)
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _resolve_log_path(args: argparse.Namespace) -> Path:
    if args.log_path is not None:
        return args.log_path
    return Path.cwd() / "log" / "littlems.log"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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
