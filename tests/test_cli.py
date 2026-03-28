from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from littlems.cli import build_service, main
from littlems.config import ProviderPoolSettings, ProviderSettings
from littlems.service import ResumeState


def test_cli_runs_local_describe_command(monkeypatch, tmp_path: Path) -> None:
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

        def refresh(self) -> None:
            return None

        def update(self, delta: int) -> None:
            self.n += delta

    class StubService:
        def inspect_resume_state(self, input_dir: Path, output_file: Path, recursive: bool = False) -> ResumeState:
            captured["inspect"] = (input_dir, output_file, recursive)
            return ResumeState(total_files=2, skipped=0, failed_to_retry=0, pending=2)

        async def describe_to_file(
            self,
            input_dir: Path,
            output_file: Path,
            recursive: bool,
            progress_callback: object = None,
        ) -> None:
            captured["describe"] = (input_dir, output_file, recursive)
            if progress_callback is not None:
                progress_callback(1, 2, photos / "a.jpg")
                progress_callback(2, 2, photos / "b.jpg")
            output_file.write_text(json.dumps({"ok": True}), encoding="utf-8")

    monkeypatch.setattr("littlems.cli.build_service", lambda settings, *, max_workers: StubService())
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "local",
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
    assert captured["describe"] == (photos, output_path, True)
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"ok": True}
    assert (tmp_path / "log" / "littlems.log").exists()
    assert not any(
        isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) in {sys.stdout, sys.stderr}
        for handler in logging.getLogger().handlers
    )


def test_build_service_defaults_max_workers_to_16() -> None:
    service = build_service(
        ProviderPoolSettings(
            providers=[
                ProviderSettings("fast-a", "http://a.example/v1", "key-a", "model-a"),
            ]
        )
    )
    assert service._max_workers == 16


def test_cli_runs_local_report_command(monkeypatch, tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    output_path = tmp_path / "report.md"
    description_output_path = tmp_path / "descriptions.json"
    json_output_path = tmp_path / "report.json"
    config_path = _write_provider_config(tmp_path)

    document = {
        "source": {"kind": "local", "directory": str(photos)},
        "records": [
            {"file_name": "a.jpg", "captured_at": "2026-03-10T10:00:00", "summary": "A", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True},
            {"file_name": "b.jpg", "captured_at": "2026-03-18T10:00:00", "summary": "B", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True},
        ],
    }

    class StubService:
        async def describe_directory(self, input_dir: Path, recursive: bool = False, progress_callback: object = None) -> dict[str, object]:
            assert input_dir == photos
            assert recursive is True
            assert progress_callback is None
            return document

    captured: dict[str, object] = {}

    async def fake_generate_report_for_records(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("# report\n", encoding="utf-8")
        json_path = kwargs["json_output_path"]
        assert isinstance(json_path, Path)
        json_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return {"markdown": "# report\n"}

    monkeypatch.setattr("littlems.cli.build_service", lambda settings, *, max_workers: StubService())
    monkeypatch.setattr("littlems.cli.generate_report_for_records", fake_generate_report_for_records)

    exit_code = main(
        [
            "local",
            "report",
            "--input",
            str(photos),
            "--from",
            "2026-03-01",
            "--to",
            "2026-03-31",
            "--birth-date",
            "2025-12-20",
            "--baby-name",
            "小满",
            "--output",
            str(output_path),
            "--json-output",
            str(json_output_path),
            "--description-output",
            str(description_output_path),
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == "# report\n"
    assert json.loads(description_output_path.read_text(encoding="utf-8"))["source"]["kind"] == "local"
    assert captured["date_from"] == "2026-03-01"
    assert captured["date_to"] == "2026-03-31"
    assert len(captured["records"]) == 2


def test_cli_runs_local_report_from_description_input(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "report.md"
    description_input_path = tmp_path / "descriptions.json"
    description_output_path = tmp_path / "descriptions-copy.json"
    json_output_path = tmp_path / "report.json"
    config_path = _write_provider_config(tmp_path)
    description_input_path.write_text(
        json.dumps(
            {
                "source": {"kind": "local", "directory": str(tmp_path / "photos")},
                "records": [
                    {"file_name": "a.jpg", "captured_at": "2026-03-10T10:00:00", "summary": "A", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True},
                    {"file_name": "b.jpg", "captured_at": "2026-03-18T10:00:00", "summary": "B", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def fail_describe_directory(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("describe_directory should not be called when using --description-input")

    class StubService:
        describe_directory = fail_describe_directory

    captured: dict[str, object] = {}

    async def fake_generate_report_for_records(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("# report\n", encoding="utf-8")
        return {"markdown": "# report\n"}

    monkeypatch.setattr("littlems.cli.build_service", lambda settings, *, max_workers: StubService())
    monkeypatch.setattr("littlems.cli.generate_report_for_records", fake_generate_report_for_records)

    exit_code = main(
        [
            "local",
            "report",
            "--description-input",
            str(description_input_path),
            "--from",
            "2026-03-01",
            "--to",
            "2026-03-31",
            "--birth-date",
            "2025-12-20",
            "--baby-name",
            "小满",
            "--output",
            str(output_path),
            "--json-output",
            str(json_output_path),
            "--description-output",
            str(description_output_path),
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == "# report\n"
    assert json.loads(description_output_path.read_text(encoding="utf-8"))["source"]["kind"] == "local"
    assert len(captured["records"]) == 2


def test_cli_rejects_local_report_when_description_input_and_input_are_both_provided(tmp_path: Path) -> None:
    description_input_path = tmp_path / "descriptions.json"
    description_input_path.write_text(json.dumps({"records": []}), encoding="utf-8")
    config_path = _write_provider_config(tmp_path)

    try:
        main(
            [
                "local",
                "report",
                "--description-input",
                str(description_input_path),
                "--input",
                str(tmp_path / "photos"),
                "--from",
                "2026-03-01",
                "--to",
                "2026-03-31",
                "--birth-date",
                "2025-12-20",
                "--baby-name",
                "小满",
                "--output",
                str(tmp_path / "report.md"),
                "--provider-config",
                str(config_path),
            ]
        )
    except SystemExit as exc:
        assert exc.code == "local report cannot use --description-input and --input together"
    else:
        raise AssertionError("Expected local report selection validation to fail")


def test_cli_runs_immich_describe_command(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    captured: dict[str, object] = {}

    class StubProgressBar:
        def __init__(self, total: int, desc: str, unit: str, dynamic_ncols: bool) -> None:
            captured["progress_total"] = total
            self.total = total
            self.n = 0

        def __enter__(self) -> StubProgressBar:
            captured["progress"] = self
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def refresh(self) -> None:
            captured["refreshed"] = True

        def update(self, delta: int) -> None:
            self.n += delta

    async def fake_inspect_immich_resume_state(**kwargs: object):
        captured["inspect"] = kwargs
        from littlems.immich import ImmichResumeState

        return ImmichResumeState(total_assets=3, skipped=1, failed_to_retry=1, pending=2)

    async def fake_describe_immich_album_to_file(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_text(json.dumps({"ok": True}), encoding="utf-8")
        progress_callback = kwargs["progress_callback"]
        progress_callback(2, 3, object())
        progress_callback(3, 3, object())
        return {"ok": True}

    monkeypatch.setattr("littlems.cli.inspect_immich_resume_state", fake_inspect_immich_resume_state)
    monkeypatch.setattr("littlems.cli.describe_immich_album_to_file", fake_describe_immich_album_to_file)
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "immich",
            "describe",
            "--album-name",
            "宝宝成长 2026-03",
            "--output",
            str(output_path),
            "--immich-url",
            "http://immich.lan/api",
            "--provider-config",
            str(config_path),
            "--upload-description",
        ]
    )

    assert exit_code == 0
    assert captured["album_name"] == "宝宝成长 2026-03"
    assert captured["immich_url"] == "http://immich.lan/api"
    assert captured["api_key"] == "test-key"
    assert captured["upload_description"] is True
    assert captured["max_workers"] == 16
    assert captured["progress_total"] == 3
    assert captured["refreshed"] is True
    progress = captured["progress"]
    assert isinstance(progress, StubProgressBar)
    assert progress.n == 3


def test_cli_runs_immich_describe_command_with_force(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    class StubProgressBar:
        def __init__(self, total: int, desc: str, unit: str, dynamic_ncols: bool) -> None:
            self.total = total
            self.n = 0

        def __enter__(self) -> StubProgressBar:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def refresh(self) -> None:
            return None

        def update(self, delta: int) -> None:
            self.n += delta

    captured: dict[str, object] = {}

    async def fake_inspect_immich_resume_state(**kwargs: object):
        from littlems.immich import ImmichResumeState

        return ImmichResumeState(total_assets=1, skipped=0, failed_to_retry=0, pending=1)

    async def fake_describe_immich_album_to_file(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("littlems.cli.inspect_immich_resume_state", fake_inspect_immich_resume_state)
    monkeypatch.setattr("littlems.cli.describe_immich_album_to_file", fake_describe_immich_album_to_file)
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "immich",
            "describe",
            "--output",
            str(output_path),
            "--immich-url",
            "http://immich.lan/api",
            "--provider-config",
            str(config_path),
            "--upload-description",
            "--force",
        ]
    )

    assert exit_code == 0
    assert captured["force"] is True


def test_cli_rejects_force_without_upload_description(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_provider_config(tmp_path)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    try:
        main(
            [
                "immich",
                "describe",
                "--output",
                str(tmp_path / "descriptions.json"),
                "--immich-url",
                "http://immich.lan/api",
                "--provider-config",
                str(config_path),
                "--force",
            ]
        )
    except SystemExit as exc:
        assert exc.code == "immich describe --force requires --upload-description"
    else:
        raise AssertionError("Expected immich describe --force without --upload-description to fail")


def test_cli_runs_immich_describe_without_album_name(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

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

    async def fake_inspect_immich_resume_state(**kwargs: object):
        captured["inspect"] = kwargs
        from littlems.immich import ImmichResumeState

        return ImmichResumeState(total_assets=1, skipped=0, failed_to_retry=0, pending=1)

    async def fake_describe_immich_album_to_file(**kwargs: object) -> dict[str, object]:
        captured["describe"] = kwargs
        return {"ok": True}

    monkeypatch.setattr("littlems.cli.inspect_immich_resume_state", fake_inspect_immich_resume_state)
    monkeypatch.setattr("littlems.cli.describe_immich_album_to_file", fake_describe_immich_album_to_file)
    monkeypatch.setattr("littlems.cli.tqdm", StubProgressBar)

    exit_code = main(
        [
            "immich",
            "describe",
            "--output",
            str(output_path),
            "--immich-url",
            "http://immich.lan/api",
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    assert captured["inspect"]["album_name"] is None
    assert captured["describe"]["album_name"] is None


def test_cli_runs_immich_report_command(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "report.md"
    description_output_path = tmp_path / "descriptions.json"
    json_output_path = tmp_path / "report.json"
    config_path = _write_provider_config(tmp_path)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    captured: dict[str, object] = {}

    async def fake_generate_immich_album_report(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("# report\n", encoding="utf-8")
        return {"markdown": "# report\n"}

    monkeypatch.setattr("littlems.cli.generate_immich_album_report", fake_generate_immich_album_report)

    exit_code = main(
        [
            "immich",
            "report",
            "--album-name",
            "宝宝成长 2026-03",
            "--birth-date",
            "2025-12-20",
            "--baby-name",
            "小满",
            "--output",
            str(output_path),
            "--json-output",
            str(json_output_path),
            "--description-output",
            str(description_output_path),
            "--immich-url",
            "http://immich.lan/api",
            "--provider-config",
            str(config_path),
            "--upload-description",
        ]
    )

    assert exit_code == 0
    assert captured["album_name"] == "宝宝成长 2026-03"
    assert captured["upload_description"] is True
    assert captured["description_output_path"] == description_output_path
    assert captured["json_output_path"] == json_output_path
    assert captured["date_from"] is None
    assert captured["date_to"] is None


def test_cli_runs_immich_report_with_date_range_only(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "report.md"
    config_path = _write_provider_config(tmp_path)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    captured: dict[str, object] = {}

    async def fake_generate_immich_album_report(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"markdown": "# report\n"}

    monkeypatch.setattr("littlems.cli.generate_immich_album_report", fake_generate_immich_album_report)

    exit_code = main(
        [
            "immich",
            "report",
            "--from",
            "2026-03-01",
            "--to",
            "2026-03-31",
            "--birth-date",
            "2025-12-20",
            "--baby-name",
            "小满",
            "--output",
            str(output_path),
            "--immich-url",
            "http://immich.lan/api",
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    assert captured["album_name"] is None
    assert captured["date_from"] == "2026-03-01"
    assert captured["date_to"] == "2026-03-31"


def test_cli_runs_immich_report_from_description_input(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "report.md"
    description_input_path = tmp_path / "descriptions.json"
    description_output_path = tmp_path / "descriptions-copy.json"
    json_output_path = tmp_path / "report.json"
    config_path = _write_provider_config(tmp_path)
    description_input_path.write_text(
        json.dumps(
            {
                "source": {"kind": "immich", "scope": "album"},
                "records": [
                    {"file_name": "a.jpg", "captured_at": "2026-03-10T10:00:00", "summary": "A", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True, "source_kind": "immich", "source_id": "asset-1", "source_album_name": "宝宝成长 2026-03"},
                    {"file_name": "b.jpg", "captured_at": "2026-03-18T10:00:00", "summary": "B", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True, "source_kind": "immich", "source_id": "asset-2", "source_album_name": "宝宝成长 2026-03"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def fake_generate_report_for_records(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("# report\n", encoding="utf-8")
        return {"markdown": "# report\n"}

    async def fake_upload_immich_descriptions_and_report(**kwargs: object) -> None:
        captured["upload"] = kwargs

    async def fail_generate_immich_album_report(**kwargs: object) -> dict[str, object]:
        raise AssertionError("generate_immich_album_report should not be called when using --description-input")

    monkeypatch.setattr("littlems.cli.generate_report_for_records", fake_generate_report_for_records)
    monkeypatch.setattr("littlems.cli.upload_immich_descriptions_and_report", fake_upload_immich_descriptions_and_report)
    monkeypatch.setattr("littlems.cli.generate_immich_album_report", fail_generate_immich_album_report)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    exit_code = main(
        [
            "immich",
            "report",
            "--description-input",
            str(description_input_path),
            "--album-name",
            "宝宝成长 2026-03",
            "--birth-date",
            "2025-12-20",
            "--baby-name",
            "小满",
            "--output",
            str(output_path),
            "--json-output",
            str(json_output_path),
            "--description-output",
            str(description_output_path),
            "--immich-url",
            "http://immich.lan/api",
            "--upload-description",
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == "# report\n"
    assert json.loads(description_output_path.read_text(encoding="utf-8"))["source"]["kind"] == "immich"
    assert captured["source"] == {"kind": "immich", "scope": "album"}
    assert captured["date_from"] == "2026-03-10"
    assert captured["date_to"] == "2026-03-18"
    assert captured["upload"]["album_name"] == "宝宝成长 2026-03"
    assert captured["upload"]["immich_url"] == "http://immich.lan/api"
    assert captured["upload"]["api_key"] == "test-key"
    assert [record["source_id"] for record in captured["upload"]["records"]] == ["asset-1", "asset-2"]


def test_cli_runs_immich_report_from_library_description_input_by_fetching_album_assets(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "report.md"
    description_input_path = tmp_path / "descriptions.json"
    config_path = _write_provider_config(tmp_path)
    description_input_path.write_text(
        json.dumps(
            {
                "source": {"kind": "immich", "scope": "library"},
                "records": [
                    {"file_name": "a.jpg", "captured_at": "2026-02-10T10:00:00", "summary": "A", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True, "source_kind": "immich", "source_id": "asset-1", "source_album_name": None},
                    {"file_name": "b.jpg", "captured_at": "2026-02-18T10:00:00", "summary": "B", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True, "source_kind": "immich", "source_id": "asset-2", "source_album_name": None},
                    {"file_name": "c.jpg", "captured_at": "2026-02-20T10:00:00", "summary": "C", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True, "source_kind": "immich", "source_id": "asset-3", "source_album_name": None},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def fake_generate_report_for_records(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("# report\n", encoding="utf-8")
        return {"markdown": "# report\n"}

    async def fail_generate_immich_album_report(**kwargs: object) -> dict[str, object]:
        raise AssertionError("generate_immich_album_report should not be called when using --description-input")

    async def fake_resolve_immich_album_asset_ids(**kwargs: object) -> set[str]:
        captured["resolve"] = kwargs
        return {"asset-1", "asset-3"}

    monkeypatch.setattr("littlems.cli.generate_report_for_records", fake_generate_report_for_records)
    monkeypatch.setattr("littlems.cli.generate_immich_album_report", fail_generate_immich_album_report)
    monkeypatch.setattr("littlems.cli.resolve_immich_album_asset_ids", fake_resolve_immich_album_asset_ids)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    exit_code = main(
        [
            "immich",
            "report",
            "--description-input",
            str(description_input_path),
            "--album-name",
            "2026-02",
            "--birth-date",
            "2025-12-20",
            "--baby-name",
            "小满",
            "--output",
            str(output_path),
            "--immich-url",
            "http://immich.lan/api",
            "--provider-config",
            str(config_path),
        ]
    )

    assert exit_code == 0
    assert output_path.read_text(encoding="utf-8") == "# report\n"
    assert captured["resolve"]["album_name"] == "2026-02"
    assert captured["resolve"]["immich_url"] == "http://immich.lan/api"
    assert captured["resolve"]["api_key"] == "test-key"
    assert captured["date_from"] == "2026-02-10"
    assert captured["date_to"] == "2026-02-20"
    assert [record["source_id"] for record in captured["records"]] == ["asset-1", "asset-3"]


def test_cli_rejects_immich_report_description_input_when_album_and_range_are_both_provided(tmp_path: Path) -> None:
    description_input_path = tmp_path / "descriptions.json"
    description_input_path.write_text(json.dumps({"records": []}), encoding="utf-8")
    config_path = _write_provider_config(tmp_path)

    try:
        main(
            [
                "immich",
                "report",
                "--description-input",
                str(description_input_path),
                "--album-name",
                "宝宝成长 2026-03",
                "--from",
                "2026-03-01",
                "--to",
                "2026-03-31",
                "--birth-date",
                "2025-12-20",
                "--baby-name",
                "小满",
                "--output",
                str(tmp_path / "report.md"),
                "--provider-config",
                str(config_path),
            ]
        )
    except SystemExit as exc:
        assert exc.code == "immich report with --description-input still requires exactly one selector: either --album-name or both --from and --to"
    else:
        raise AssertionError("Expected immich report description-input validation to fail")


def test_cli_rejects_immich_report_description_input_upload_without_immich_url(monkeypatch, tmp_path: Path) -> None:
    description_input_path = tmp_path / "descriptions.json"
    description_input_path.write_text(
        json.dumps(
            {
                "source": {"kind": "immich", "scope": "album"},
                "records": [
                    {"file_name": "a.jpg", "captured_at": "2026-03-10T10:00:00", "summary": "A", "actions": [], "expressions": [], "scene": None, "objects": [], "highlights": [], "uncertainty": None, "baby_present": True, "source_kind": "immich", "source_id": "asset-1", "source_album_name": "宝宝成长 2026-03"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config_path = _write_provider_config(tmp_path)

    async def fake_generate_report_for_records(**kwargs: object) -> dict[str, object]:
        return {"markdown": "# report\n"}

    monkeypatch.setattr("littlems.cli.generate_report_for_records", fake_generate_report_for_records)

    try:
        main(
            [
                "immich",
                "report",
                "--description-input",
                str(description_input_path),
                "--album-name",
                "宝宝成长 2026-03",
                "--birth-date",
                "2025-12-20",
                "--baby-name",
                "小满",
                "--output",
                str(tmp_path / "report.md"),
                "--provider-config",
                str(config_path),
                "--upload-description",
            ]
        )
    except SystemExit as exc:
        assert exc.code == "immich report requires --immich-url when --upload-description is enabled"
    else:
        raise AssertionError("Expected immich report upload validation to fail")


def test_cli_rejects_immich_report_when_album_and_range_are_both_provided(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_provider_config(tmp_path)
    monkeypatch.setenv("IMMICH_API_KEY", "test-key")

    try:
        main(
            [
                "immich",
                "report",
                "--album-name",
                "宝宝成长 2026-03",
                "--from",
                "2026-03-01",
                "--to",
                "2026-03-31",
                "--birth-date",
                "2025-12-20",
                "--baby-name",
                "小满",
                "--output",
                str(tmp_path / "report.md"),
                "--immich-url",
                "http://immich.lan/api",
                "--provider-config",
                str(config_path),
            ]
        )
    except SystemExit as exc:
        assert exc.code == "immich report requires exactly one selector: either --album-name or both --from and --to"
    else:
        raise AssertionError("Expected immich report selection validation to fail")


def test_immich_commands_require_api_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    config_path = _write_provider_config(tmp_path)

    try:
        main(
            [
                "immich",
                "describe",
                "--album-name",
                "宝宝成长 2026-03",
                "--output",
                str(tmp_path / "out.json"),
                "--immich-url",
                "http://immich.lan/api",
                "--provider-config",
                str(config_path),
            ]
        )
    except SystemExit as exc:
        assert exc.code == "IMMICH_API_KEY environment variable is required for immich commands"
    else:
        raise AssertionError("Expected immich command to require IMMICH_API_KEY")


def _write_provider_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "fast-a",
                        "base_url": "http://a.example/v1",
                        "api_key": "key-a",
                        "vision_model": "model-a",
                        "max_inflight": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return config_path
