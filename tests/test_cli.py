from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from littlems.cli import build_service, main
from littlems.config import ProviderPoolSettings, ProviderSettings
from littlems.service import ResumeState


def test_cli_runs_describe_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)

    captured: dict[str, object] = {}
    progress_updates: list[tuple[int, int, str]] = []

    class StubProgressBar:
        def __init__(self, total: int, desc: str, unit: str, dynamic_ncols: bool) -> None:
            self.total = total
            self.desc = desc
            self.unit = unit
            self.dynamic_ncols = dynamic_ncols
            self.n = 0

        def __enter__(self) -> StubProgressBar:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def set_postfix_str(self, value: str) -> None:
            progress_updates.append((self.n, self.total, value))

        def update(self, delta: int) -> None:
            self.n += delta

    class StubService:
        async def describe_to_file(
            self,
            input_dir: Path,
            output_file: Path,
            recursive: bool,
            progress_callback: object = None,
        ) -> None:
            captured["input_dir"] = input_dir
            captured["output_file"] = output_file
            captured["recursive"] = recursive
            if progress_callback is not None:
                progress_callback(1, 2, photos / "a.jpg")
                progress_callback(2, 2, photos / "b.jpg")
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    captured_settings: dict[str, ProviderPoolSettings] = {}

    def stub_build_service(settings: ProviderPoolSettings, *, max_workers: int) -> StubService:
        captured_settings["settings"] = settings
        captured["max_workers"] = max_workers
        return StubService()

    monkeypatch.setattr("littlems.cli.build_service", stub_build_service)
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "describe",
            "--input",
            str(photos),
            "--output",
            str(output_path),
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    assert captured == {
        "input_dir": photos,
        "output_file": output_path,
        "recursive": True,
        "max_workers": 16,
    }
    assert captured_settings["settings"] == ProviderPoolSettings(
        providers=[
            ProviderSettings(
                name="fast-a",
                base_url="http://a.example/v1",
                api_key="key-a",
                vision_model="model-a",
                max_inflight=2,
                timeout=None,
            )
        ]
    )
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"ok": True}
    assert progress_updates == []
    assert not any(
        isinstance(handler, logging.StreamHandler)
        and getattr(handler, "stream", None) in {sys.stdout, sys.stderr}
        for handler in logging.getLogger().handlers
    )
    assert (tmp_path / "log" / "littlems.log").exists()


def test_cli_log_path_overrides_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    log_path = tmp_path / "custom" / "cli.log"
    config_path = _write_provider_config(tmp_path)

    class StubProgressBar:
        def __init__(self, total: int, desc: str, unit: str, dynamic_ncols: bool) -> None:
            self.total = total
            self.n = 0

        def __enter__(self) -> StubProgressBar:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def set_postfix_str(self, value: str) -> None:
            return None

        def update(self, delta: int) -> None:
            self.n += delta

    class StubService:
        async def describe_to_file(
            self,
            input_dir: Path,
            output_file: Path,
            recursive: bool,
            progress_callback: object = None,
        ) -> None:
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    monkeypatch.setattr("littlems.cli.build_service", lambda settings, *, max_workers: StubService())
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "describe",
            "--input",
            str(photos),
            "--output",
            str(output_path),
            "--provider-config",
            str(config_path),
            "--log-path",
            str(log_path),
        ]
    )

    assert exit_code == 0
    assert log_path.exists()
    assert not (tmp_path / "log" / "littlems.log").exists()


def test_cli_sets_progress_from_resume_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)

    observed: dict[str, object] = {}

    class StubProgressBar:
        def __init__(self, total: int, desc: str, unit: str, dynamic_ncols: bool) -> None:
            self.total = total
            self.desc = desc
            self.unit = unit
            self.dynamic_ncols = dynamic_ncols
            self.n = 0
            self.refreshed = False

        def __enter__(self) -> StubProgressBar:
            observed["progress"] = self
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def refresh(self) -> None:
            self.refreshed = True

        def set_postfix_str(self, value: str) -> None:
            return None

        def update(self, delta: int) -> None:
            self.n += delta

    class StubService:
        def inspect_resume_state(self, input_dir: Path, output_file: Path, recursive: bool = False) -> ResumeState:
            observed["inspect"] = (input_dir, output_file, recursive)
            return ResumeState(total_files=8, skipped=3, failed_to_retry=2, pending=5)

        async def describe_to_file(
            self,
            input_dir: Path,
            output_file: Path,
            recursive: bool,
            progress_callback: object = None,
        ) -> None:
            observed["describe"] = (input_dir, output_file, recursive)
            if progress_callback is not None:
                progress_callback(4, 8, photos / "d.jpg")

    monkeypatch.setattr("littlems.cli.build_service", lambda settings, *, max_workers: StubService())
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "describe",
            "--input",
            str(photos),
            "--output",
            str(output_path),
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    progress = observed["progress"]
    assert isinstance(progress, StubProgressBar)
    assert progress.total == 8
    assert progress.refreshed is True
    assert progress.n == 4


def test_build_service_defaults_max_workers_to_16() -> None:
    service = build_service(
        ProviderPoolSettings(
            providers=[
                ProviderSettings("fast-a", "http://a.example/v1", "key-a", "model-a"),
            ]
        )
    )

    assert service._max_workers == 16


def test_cli_passes_custom_max_workers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)

    captured: dict[str, object] = {}

    class StubProgressBar:
        def __init__(self, total: int, desc: str, unit: str, dynamic_ncols: bool) -> None:
            self.total = total
            self.n = 0

        def __enter__(self) -> StubProgressBar:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def update(self, delta: int) -> None:
            self.n += delta

    class StubService:
        async def describe_to_file(
            self,
            input_dir: Path,
            output_file: Path,
            recursive: bool,
            progress_callback: object = None,
        ) -> None:
            del input_dir, recursive, progress_callback
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    def stub_build_service(settings: ProviderPoolSettings, *, max_workers: int) -> StubService:
        captured["providers"] = [provider.name for provider in settings.providers]
        captured["max_workers"] = max_workers
        return StubService()

    monkeypatch.setattr("littlems.cli.build_service", stub_build_service)
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "describe",
            "--input",
            str(photos),
            "--output",
            str(output_path),
            "--provider-config",
            str(config_path),
            "--max-workers",
            "7",
        ]
    )

    assert exit_code == 0
    assert captured == {"providers": ["fast-a"], "max_workers": 7}


def test_cli_rejects_invalid_max_workers(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)

    try:
        main(
            [
                "describe",
                "--input",
                str(photos),
                "--output",
                str(output_path),
                "--provider-config",
                str(config_path),
                "--max-workers",
                "0",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected CLI to reject non-positive --max-workers")


def test_cli_requires_provider_config(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"

    try:
        main(["describe", "--input", str(photos), "--output", str(output_path)])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected CLI to require --provider-config")


def test_cli_rejects_missing_provider_config_file(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    missing_config = tmp_path / "missing.json"

    try:
        main(
            [
                "describe",
                "--input",
                str(photos),
                "--output",
                str(output_path),
                "--provider-config",
                str(missing_config),
            ]
        )
    except SystemExit as exc:
        assert exc.code == f"Provider config file not found: {missing_config}"
    else:
        raise AssertionError("Expected CLI to fail when provider config is missing")


def test_validate_config_succeeds_for_valid_provider_config(tmp_path: Path) -> None:
    config_path = _write_provider_config(tmp_path)

    exit_code = main(["validate-config", "--provider-config", str(config_path)])

    assert exit_code == 0


def test_validate_config_prints_probe_summary(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = _write_provider_config(tmp_path)

    async def fake_probe(settings: ProviderPoolSettings) -> list[dict[str, object]]:
        assert [provider.name for provider in settings.providers] == ["fast-a"]
        return [
            {
                "name": "fast-a",
                "base_url": "http://a.example/v1",
                "model": "model-a",
                "ok": True,
                "error_kind": None,
                "error": None,
            }
        ]

    monkeypatch.setattr("littlems.cli._probe_provider_pool", fake_probe)

    exit_code = main(["validate-config", "--provider-config", str(config_path), "--probe"])

    assert exit_code == 0
    assert "OK   fast-a  http://a.example/v1  model=model-a" in capsys.readouterr().out


def test_validate_config_fails_for_invalid_provider_config(tmp_path: Path) -> None:
    config_path = _write_provider_config(tmp_path, payload={"providers": []})

    try:
        main(["validate-config", "--provider-config", str(config_path)])
    except SystemExit as exc:
        assert exc.code == "Provider config must contain a non-empty 'providers' array"
    else:
        raise AssertionError("Expected validate-config to fail for invalid config")


def test_validate_config_with_probe_fails_after_printing_all_failures(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = _write_provider_config(tmp_path)

    async def fake_probe(settings: ProviderPoolSettings) -> list[dict[str, object]]:
        del settings
        return [
            {
                "name": "fast-a",
                "base_url": "http://a.example/v1",
                "model": "model-a",
                "ok": False,
                "error_kind": "timeout",
                "error": "timeout",
            },
            {
                "name": "slow-b",
                "base_url": "http://b.example/v1",
                "model": "model-b",
                "ok": False,
                "error_kind": "unauthorized",
                "error": "HTTP 401 unauthorized",
            },
        ]

    monkeypatch.setattr("littlems.cli._probe_provider_pool", fake_probe)

    try:
        main(["validate-config", "--provider-config", str(config_path), "--probe"])
    except SystemExit as exc:
        assert exc.code == "Provider probe failed for: fast-a, slow-b"
    else:
        raise AssertionError("Expected validate-config --probe to fail when probe fails")
    output = capsys.readouterr().out
    assert "FAIL fast-a  http://a.example/v1  model=model-a  kind=timeout  error=timeout" in output
    assert (
        "FAIL slow-b  http://b.example/v1  model=model-b  kind=unauthorized  error=HTTP 401 unauthorized"
        in output
    )


def test_probe_provider_pool_runs_concurrently() -> None:
    from littlems.cli import _probe_provider_pool

    settings = ProviderPoolSettings(
        providers=[
            ProviderSettings("a", "http://a.example/v1", "key-a", "model-a"),
            ProviderSettings("b", "http://b.example/v1", "key-b", "model-b"),
            ProviderSettings("c", "http://c.example/v1", "key-c", "model-c"),
        ]
    )

    async def run() -> list[dict[str, object]]:
        import littlems.cli as cli_module

        original = cli_module._probe_provider

        async def fake_probe(provider: ProviderSettings) -> dict[str, object]:
            await asyncio.sleep(0.05)
            return {
                "name": provider.name,
                "base_url": provider.base_url,
                "model": provider.vision_model,
                "ok": True,
                "error_kind": None,
                "error": None,
            }

        cli_module._probe_provider = fake_probe
        try:
            started = time.perf_counter()
            results = await _probe_provider_pool(settings)
            elapsed = time.perf_counter() - started
        finally:
            cli_module._probe_provider = original

        assert elapsed < 0.12
        return results

    results = asyncio.run(run())

    assert [result["name"] for result in results] == ["a", "b", "c"]


def test_classify_http_error_covers_common_statuses() -> None:
    from littlems.cli import _classify_http_error

    assert _classify_http_error(401) == "unauthorized"
    assert _classify_http_error(404) == "not_found"
    assert _classify_http_error(429) == "rate_limited"
    assert _classify_http_error(503) == "server_error"
    assert _classify_http_error(418) == "http_error"


def _write_provider_config(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        json.dumps(
            payload
            or {
                "providers": [
                    {
                        "name": "fast-a",
                        "base_url": "http://a.example/v1/",
                        "api_key": "key-a",
                        "vision_model": "model-a",
                        "max_inflight": 2,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config_path
