from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import inspect
from pathlib import Path

import httpx
from tqdm import tqdm

from littlems.config import ProviderPoolSettings, ProviderSettings, load_provider_settings
from littlems.immich import (
    describe_immich_album_to_file,
    _date_range_from_records,
    generate_immich_album_report,
    inspect_immich_resume_state,
    upload_immich_descriptions_and_report,
)
from littlems.report import generate_report_for_records, load_description_document, select_records_in_range
from littlems.service import PhotoDescriptionService
from littlems.vision import BalancedVisionClient

logger = logging.getLogger(__name__)


class LittlemsHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    def _get_help_string(self, action: argparse.Action) -> str:
        help_text = action.help or ""
        if not action.required and action.option_strings and action.default is not argparse.SUPPRESS:
            if action.default is None and "default:" not in help_text:
                help_text = f"{help_text} (default: not set)".strip()
            elif action.default is not None and "%(default)" not in help_text and "default:" not in help_text:
                help_text = f"{help_text} (default: %(default)s)".strip()
        return help_text


def build_service(settings: ProviderPoolSettings, *, max_workers: int = 16) -> PhotoDescriptionService:
    client = BalancedVisionClient(settings.providers)
    return PhotoDescriptionService(
        vision_client=client,
        provider_names=[provider.name for provider in settings.providers],
        max_workers=max_workers,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="littlems", formatter_class=LittlemsHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_subparser_class())

    local = subparsers.add_parser("local", help="Operate on local photo directories")
    local_subparsers = local.add_subparsers(dest="local_command", required=True, parser_class=_subparser_class())

    local_describe = local_subparsers.add_parser("describe", help="Describe photos in a local directory")
    _add_local_input_arguments(local_describe)
    local_describe.add_argument("--output", required=True, type=Path, help="Output JSON file path")

    local_report = local_subparsers.add_parser("report", help="Generate a report from a local directory")
    _add_local_report_input_arguments(local_report)
    _add_report_arguments(local_report)
    local_report.add_argument("--description-input", type=Path, help="Existing descriptions JSON input path")
    local_report.add_argument("--description-output", type=Path, help="Optional descriptions JSON output path")

    immich = subparsers.add_parser("immich", help="Operate on Immich albums")
    immich_subparsers = immich.add_subparsers(dest="immich_command", required=True, parser_class=_subparser_class())

    immich_describe = immich_subparsers.add_parser("describe", help="Describe assets from an Immich album or the full library")
    _add_immich_common_arguments(immich_describe)
    immich_describe.add_argument("--max-workers", type=_positive_int, default=16, help="Maximum number of asset workers to run concurrently")
    immich_describe.add_argument("--output", required=True, type=Path, help="Output JSON file path")
    immich_describe.add_argument(
        "--upload-description",
        action="store_true",
        help="Upload generated asset descriptions back to Immich",
    )
    immich_describe.add_argument(
        "--force",
        action="store_true",
        help="When used with --upload-description, re-upload descriptions for all successful records",
    )

    immich_report = immich_subparsers.add_parser("report", help="Generate a report from an Immich album")
    _add_immich_report_common_arguments(immich_report)
    _add_immich_report_arguments(immich_report)
    immich_report.add_argument("--description-input", type=Path, help="Existing descriptions JSON input path")
    immich_report.add_argument("--description-output", type=Path, help="Optional descriptions JSON output path")
    immich_report.add_argument(
        "--upload-description",
        action="store_true",
        help="Upload generated asset descriptions and the final album description back to Immich",
    )

    validate = subparsers.add_parser("validate-config", help="Validate a provider config file")
    validate.add_argument("--provider-config", required=True, type=Path, help="JSON config file containing provider definitions")
    validate.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="Logging verbosity")
    validate.add_argument("--log-path", type=Path, help="Log file output path (default: ./log/littlems.log)")
    validate.add_argument("--probe", action="store_true", help="Probe each provider with a lightweight API request")
    return parser


def _subparser_class() -> type[argparse.ArgumentParser]:
    class _ConfiguredArgumentParser(argparse.ArgumentParser):
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs.setdefault("formatter_class", LittlemsHelpFormatter)
            super().__init__(*args, **kwargs)

    return _ConfiguredArgumentParser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "log_level", "INFO"), _resolve_log_path(args))

    if args.command == "validate-config":
        return _run_validate_config(args)
    if args.command == "local" and args.local_command == "describe":
        return _run_local_describe(args)
    if args.command == "local" and args.local_command == "report":
        return _run_local_report(args)
    if args.command == "immich" and args.immich_command == "describe":
        return _run_immich_describe(args)
    if args.command == "immich" and args.immich_command == "report":
        return _run_immich_report(args)
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _run_local_describe(args: argparse.Namespace) -> int:
    settings = load_provider_settings(args.provider_config)
    service = build_service(settings, max_workers=args.max_workers)
    inspect_resume = getattr(service, "inspect_resume_state", None)
    return _run_describe_with_progress(
        total_count=lambda state: state.total_files if state is not None else 0,
        skipped_count=lambda state: state.skipped if state is not None else 0,
        inspect_fn=(lambda: inspect_resume(args.input, args.output, recursive=args.recursive)) if callable(inspect_resume) else None,
        execute_fn=lambda progress_callback: service.describe_to_file(
            args.input,
            args.output,
            recursive=args.recursive,
            progress_callback=progress_callback,
        ),
        desc="Processing photos",
    )


def _run_local_report(args: argparse.Namespace) -> int:
    _validate_local_report_selection(args)
    if args.description_input is not None:
        document = load_description_document(args.description_input)
    else:
        assert args.provider_config is not None
        assert args.input is not None
        settings = load_provider_settings(args.provider_config)
        service = build_service(settings, max_workers=args.max_workers)
        document = asyncio.run(service.describe_directory(args.input, recursive=args.recursive))
    _write_description_document_if_requested(document, args.description_output)
    all_records = [item for item in document.get("records", []) if isinstance(item, dict)]
    selected_records = select_records_in_range(all_records, args.date_from, args.date_to)
    settings = _load_report_settings(args.provider_config)
    asyncio.run(
        generate_report_for_records(
            records=selected_records,
            history_records=all_records,
            date_from=args.date_from,
            date_to=args.date_to,
            birth_date=args.birth_date,
            baby_name=args.baby_name,
            output_path=args.output,
            settings=settings,
            json_output_path=args.json_output,
            source=document.get("source"),
        )
    )
    return 0


def _run_immich_describe(args: argparse.Namespace) -> int:
    if args.force and not args.upload_description:
        raise SystemExit("immich describe --force requires --upload-description")
    api_key = _require_immich_api_key()
    settings = load_provider_settings(args.provider_config)
    vision_client = BalancedVisionClient(settings.providers)
    provider_names = [provider.name for provider in settings.providers]
    return _run_describe_with_progress(
        total_count=lambda state: state.total_assets if state is not None else 0,
        skipped_count=lambda state: state.skipped if state is not None else 0,
        inspect_fn=lambda: inspect_immich_resume_state(
            album_name=args.album_name,
            output_path=args.output,
            immich_url=args.immich_url,
            api_key=api_key,
            provider_names=provider_names,
            upload_description=args.upload_description,
        ),
        execute_fn=lambda progress_callback: describe_immich_album_to_file(
                album_name=args.album_name,
                output_path=args.output,
                immich_url=args.immich_url,
                api_key=api_key,
                vision_client=vision_client,
                provider_names=provider_names,
                max_workers=args.max_workers,
                upload_description=args.upload_description,
                force=args.force,
                progress_callback=progress_callback,
        ),
        desc="Processing assets",
    )


def _run_immich_report(args: argparse.Namespace) -> int:
    _validate_immich_report_selection(args)
    if args.description_input is not None:
        document = load_description_document(args.description_input)
        _write_description_document_if_requested(document, args.description_output)
        all_records = [item for item in document.get("records", []) if isinstance(item, dict)]
        if args.album_name:
            selected_records = [
                item for item in all_records
                if item.get("source_album_name") == args.album_name or item.get("album_name") == args.album_name
            ]
            if not selected_records:
                raise SystemExit(f"No records for album found in description document: {args.album_name}")
            resolved_date_from, resolved_date_to = _date_range_from_records(selected_records)
        else:
            assert args.date_from is not None and args.date_to is not None
            selected_records = select_records_in_range(all_records, args.date_from, args.date_to)
            resolved_date_from, resolved_date_to = args.date_from, args.date_to
        settings = _load_report_settings(args.provider_config)
        report = asyncio.run(
            generate_report_for_records(
                records=selected_records,
                history_records=all_records,
                date_from=resolved_date_from,
                date_to=resolved_date_to,
                birth_date=args.birth_date,
                baby_name=args.baby_name,
                output_path=args.output,
                settings=settings,
                json_output_path=args.json_output,
                source=document.get("source"),
            )
        )
        if args.upload_description:
            if not args.immich_url:
                raise SystemExit("immich report requires --immich-url when --upload-description is enabled")
            api_key = _require_immich_api_key()
            asyncio.run(
                upload_immich_descriptions_and_report(
                    records=selected_records,
                    markdown=str(report["markdown"]),
                    album_name=args.album_name,
                    immich_url=args.immich_url,
                    api_key=api_key,
                )
            )
        return 0

    api_key = _require_immich_api_key()
    assert args.provider_config is not None
    assert args.immich_url is not None
    settings = load_provider_settings(args.provider_config)
    vision_client = BalancedVisionClient(settings.providers)
    asyncio.run(
        generate_immich_album_report(
            album_name=args.album_name,
            output_path=args.output,
            immich_url=args.immich_url,
            api_key=api_key,
            vision_client=vision_client,
            report_settings=settings,
            date_from=args.date_from,
            date_to=args.date_to,
            birth_date=args.birth_date,
            baby_name=args.baby_name,
            json_output_path=args.json_output,
            description_output_path=args.description_output,
            upload_description=args.upload_description,
        )
    )
    return 0


def _run_validate_config(args: argparse.Namespace) -> int:
    settings = load_provider_settings(args.provider_config)
    if args.probe:
        probe_results = asyncio.run(_probe_provider_pool(settings))
        for result in probe_results:
            if result["ok"]:
                print(f"OK   {result['name']}  {result['base_url']}  model={result['model']}")
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
        print(f"Config OK: {args.provider_config} ({len(settings.providers)} providers)")
    return 0


def _add_local_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, type=Path, help="Directory containing photos")
    parser.add_argument("--provider-config", required=True, type=Path, help="JSON config file containing provider definitions")
    parser.add_argument("--recursive", action="store_true", default=True, help="Scan subdirectories recursively")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="Logging verbosity")
    parser.add_argument("--log-path", type=Path, help="Log file output path (default: ./log/littlems.log)")
    parser.add_argument("--max-workers", type=_positive_int, default=16, help="Maximum number of file workers to run concurrently")


def _add_local_report_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, help="Directory containing photos; use instead of --description-input")
    parser.add_argument("--provider-config", type=Path, help="JSON config file containing provider definitions; required when not using --description-input")
    parser.add_argument("--recursive", action="store_true", default=True, help="Scan subdirectories recursively")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="Logging verbosity")
    parser.add_argument("--log-path", type=Path, help="Log file output path (default: ./log/littlems.log)")
    parser.add_argument("--max-workers", type=_positive_int, default=16, help="Maximum number of file workers to run concurrently")


def _add_immich_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--album-name", help="Exact Immich album name")
    parser.add_argument("--immich-url", required=True, help="Immich API base URL, for example http://immich.lan/api")
    parser.add_argument("--provider-config", required=True, type=Path, help="JSON config file containing provider definitions")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="Logging verbosity")
    parser.add_argument("--log-path", type=Path, help="Log file output path (default: ./log/littlems.log)")


def _add_immich_report_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--album-name", help="Exact Immich album name; use instead of --from/--to or --description-input")
    parser.add_argument("--immich-url", help="Immich API base URL, for example http://immich.lan/api; required for online report mode")
    parser.add_argument("--provider-config", type=Path, help="JSON config file containing provider definitions")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO", help="Logging verbosity")
    parser.add_argument("--log-path", type=Path, help="Log file output path (default: ./log/littlems.log)")


def _add_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="date_from", required=True, help="Start date in YYYY-MM-DD format")
    parser.add_argument("--to", dest="date_to", required=True, help="End date in YYYY-MM-DD format")
    parser.add_argument("--birth-date", required=True, help="Baby birth date in YYYY-MM-DD format")
    parser.add_argument("--baby-name", required=True, help="Baby name used in report prompt context")
    parser.add_argument("--output", required=True, type=Path, help="Output Markdown file path")
    parser.add_argument("--json-output", type=Path, help="Optional debug JSON output path")


def _add_immich_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="date_from", help="Start date in YYYY-MM-DD format")
    parser.add_argument("--to", dest="date_to", help="End date in YYYY-MM-DD format")
    parser.add_argument("--birth-date", required=True, help="Baby birth date in YYYY-MM-DD format")
    parser.add_argument("--baby-name", required=True, help="Baby name used in report prompt context")
    parser.add_argument("--output", required=True, type=Path, help="Output Markdown file path")
    parser.add_argument("--json-output", type=Path, help="Optional debug JSON output path")


def _require_immich_api_key() -> str:
    api_key = os.getenv("IMMICH_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("IMMICH_API_KEY environment variable is required for immich commands")
    return api_key


def _resolve_log_path(args: argparse.Namespace) -> Path:
    if getattr(args, "log_path", None) is not None:
        return args.log_path
    return Path.cwd() / "log" / "littlems.log"


def _write_description_document_if_requested(document: dict[str, object], output_path: Path | None) -> None:
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_report_settings(provider_config: Path | None) -> ProviderPoolSettings:
    if provider_config is None:
        raise SystemExit("Report generation requires --provider-config")
    return load_provider_settings(provider_config)


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


def _update_progress(progress: tqdm, processed: int, total: int, image_path: object) -> None:
    del image_path
    if progress.total != total:
        progress.total = total
    progress.update(processed - progress.n)


def _run_describe_with_progress(
    *,
    total_count: object,
    skipped_count: object,
    inspect_fn: object | None,
    execute_fn: object,
    desc: str,
) -> int:
    if callable(inspect_fn):
        inspected = inspect_fn()
        resume_state = asyncio.run(inspected) if inspect.isawaitable(inspected) else inspected
    else:
        resume_state = None
    with tqdm(
        total=total_count(resume_state),
        desc=desc,
        unit="image",
        dynamic_ncols=True,
    ) as progress:
        skipped = skipped_count(resume_state)
        if skipped:
            progress.n = skipped
            if hasattr(progress, "refresh"):
                progress.refresh()
        asyncio.run(
            execute_fn(lambda processed, total, image_path: _update_progress(progress, processed, total, image_path))
        )
    return 0


def _validate_immich_report_selection(args: argparse.Namespace) -> None:
    if args.description_input is not None:
        has_album = bool(args.album_name)
        has_range = bool(args.date_from or args.date_to)
        if has_album == has_range:
            raise SystemExit("immich report with --description-input still requires exactly one selector: either --album-name or both --from and --to")
        if has_range and (not args.date_from or not args.date_to):
            raise SystemExit("immich report requires both --from and --to when using a date range")
        return

    if not args.provider_config:
        raise SystemExit("immich report online mode requires --provider-config")
    if not args.immich_url:
        raise SystemExit("immich report online mode requires --immich-url")
    has_album = bool(args.album_name)
    has_range = bool(args.date_from or args.date_to)
    if has_album == has_range:
        raise SystemExit("immich report requires exactly one selector: either --album-name or both --from and --to")
    if has_range and (not args.date_from or not args.date_to):
        raise SystemExit("immich report requires both --from and --to when using a date range")


def _validate_local_report_selection(args: argparse.Namespace) -> None:
    if args.description_input is not None and args.input is not None:
        raise SystemExit("local report cannot use --description-input and --input together")
    if args.description_input is None and args.input is None:
        raise SystemExit("local report requires either --description-input or --input")
    if args.description_input is None and args.provider_config is None:
        raise SystemExit("local report requires --provider-config when --description-input is not provided")
    if args.provider_config is None:
        raise SystemExit("local report requires --provider-config")


async def _probe_provider_pool(settings: ProviderPoolSettings) -> list[dict[str, object]]:
    tasks = [asyncio.create_task(_probe_provider(provider)) for provider in settings.providers]
    return await asyncio.gather(*tasks)


async def _probe_provider(provider: ProviderSettings) -> dict[str, object]:
    timeout = 5.0
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
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                },
            )
        if response.is_error:
            return {
                "name": provider.name,
                "base_url": provider.base_url,
                "model": provider.vision_model,
                "ok": False,
                "error_kind": _classify_http_error(response.status_code),
                "error": response.text.strip() or f"HTTP {response.status_code}",
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
    if status_code == 401:
        return "unauthorized"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if 500 <= status_code:
        return "server_error"
    return "http_error"
