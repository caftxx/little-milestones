from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from littlems.cli import main
from littlems.config import Settings


def test_cli_runs_describe_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"

    captured: dict[str, object] = {}
    progress_updates: list[int] = []

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

        def update(self, delta: int) -> None:
            self.n += delta
            progress_updates.append(self.n)

    class StubService:
        async def describe_to_file(
            self,
            input_dir: Path,
            output_file: Path,
            recursive: bool,
            parallelism: int = 4,
            progress_callback: object = None,
        ) -> None:
            captured["input_dir"] = input_dir
            captured["output_file"] = output_file
            captured["recursive"] = recursive
            captured["parallelism"] = parallelism
            if progress_callback is not None:
                progress_callback(1, 2, photos / "a.jpg")
                progress_callback(2, 2, photos / "b.jpg")
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    captured_settings: dict[str, Settings] = {}

    def stub_build_service(settings: Settings) -> StubService:
        captured_settings["settings"] = settings
        return StubService()

    monkeypatch.setattr("littlems.cli.build_service", stub_build_service)
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("VISION_MODEL", "env-model")

    exit_code = main(["describe", "--input", str(photos), "--output", str(output_path)])

    assert exit_code == 0
    assert captured == {
        "input_dir": photos,
        "output_file": output_path,
        "recursive": False,
        "parallelism": 4,
    }
    assert captured_settings["settings"] == Settings(
        base_url="http://env.example/v1",
        api_key="env-key",
        vision_model="env-model",
    )
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"ok": True}
    assert progress_updates == [1, 2]
    assert not any(
        isinstance(handler, logging.StreamHandler)
        and getattr(handler, "stream", None) in {sys.stdout, sys.stderr}
        for handler in logging.getLogger().handlers
    )
    assert (tmp_path / "log" / "littlems.log").exists()


def test_cli_args_override_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"

    captured_settings: dict[str, Settings] = {}

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
            parallelism: int = 4,
            progress_callback: object = None,
        ) -> None:
            captured_settings["parallelism"] = parallelism
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    def stub_build_service(settings: Settings) -> StubService:
        captured_settings["settings"] = settings
        return StubService()

    monkeypatch.setattr("littlems.cli.build_service", stub_build_service)
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("VISION_MODEL", "env-model")

    exit_code = main(
        [
            "describe",
            "--input",
            str(photos),
            "--output",
            str(output_path),
            "--openai-base-url",
            "http://cli.example/v1/",
            "--openai-api-key",
            "cli-key",
            "--vision-model",
            "cli-model",
        ]
    )

    assert exit_code == 0
    assert captured_settings["settings"] == Settings(
        base_url="http://cli.example/v1",
        api_key="cli-key",
        vision_model="cli-model",
    )
    assert captured_settings["parallelism"] == 4


def test_cli_log_path_overrides_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    log_path = tmp_path / "custom" / "cli.log"

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
            parallelism: int = 4,
            progress_callback: object = None,
        ) -> None:
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    monkeypatch.setattr("littlems.cli.build_service", lambda settings: StubService())
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("VISION_MODEL", "env-model")

    exit_code = main(
        [
            "describe",
            "--input",
            str(photos),
            "--output",
            str(output_path),
            "--log-path",
            str(log_path),
        ]
    )

    assert exit_code == 0
    assert log_path.exists()
    assert not (tmp_path / "log" / "littlems.log").exists()


def test_cli_parallelism_overrides_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"
    captured: dict[str, int] = {}

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
            parallelism: int = 4,
            progress_callback: object = None,
        ) -> None:
            captured["parallelism"] = parallelism
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    monkeypatch.setattr("littlems.cli.build_service", lambda settings: StubService())
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("VISION_MODEL", "env-model")

    exit_code = main(
        [
            "describe",
            "--input",
            str(photos),
            "--output",
            str(output_path),
            "--parallelism",
            "8",
        ]
    )

    assert exit_code == 0
    assert captured["parallelism"] == 8


def test_cli_rejects_invalid_parallelism(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"

    try:
        main(
            [
                "describe",
                "--input",
                str(photos),
                "--output",
                str(output_path),
                "--parallelism",
                "0",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected CLI to reject non-positive parallelism")


def test_cli_fails_when_openai_config_is_missing(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"

    try:
        main(["describe", "--input", str(photos), "--output", str(output_path)])
    except SystemExit as exc:
        assert exc.code == (
            "Missing OpenAI configuration. Provide CLI arguments or set environment variables: "
            "OPENAI_BASE_URL, OPENAI_API_KEY, VISION_MODEL"
        )
    else:
        raise AssertionError("Expected CLI to fail when OpenAI configuration is missing")
