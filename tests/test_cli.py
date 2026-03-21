from __future__ import annotations

import json
from pathlib import Path

from littlems.cli import main
from littlems.config import Settings


def test_cli_runs_describe_command(monkeypatch, tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"

    captured: dict[str, object] = {}

    class StubService:
        def describe_to_file(self, input_dir: Path, output_file: Path, recursive: bool) -> None:
            captured["input_dir"] = input_dir
            captured["output_file"] = output_file
            captured["recursive"] = recursive
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    captured_settings: dict[str, Settings] = {}

    def stub_build_service(settings: Settings) -> StubService:
        captured_settings["settings"] = settings
        return StubService()

    monkeypatch.setattr("littlems.cli.build_service", stub_build_service)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("VISION_MODEL", "env-model")

    exit_code = main(["describe", "--input", str(photos), "--output", str(output_path)])

    assert exit_code == 0
    assert captured == {
        "input_dir": photos,
        "output_file": output_path,
        "recursive": False,
    }
    assert captured_settings["settings"] == Settings(
        base_url="http://env.example/v1",
        api_key="env-key",
        vision_model="env-model",
    )
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"ok": True}


def test_cli_args_override_environment(monkeypatch, tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "descriptions.json"

    captured_settings: dict[str, Settings] = {}

    class StubService:
        def describe_to_file(self, input_dir: Path, output_file: Path, recursive: bool) -> None:
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    def stub_build_service(settings: Settings) -> StubService:
        captured_settings["settings"] = settings
        return StubService()

    monkeypatch.setattr("littlems.cli.build_service", stub_build_service)
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
