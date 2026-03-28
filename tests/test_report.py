from __future__ import annotations

import asyncio
import json
from pathlib import Path

from littlems.config import ProviderPoolSettings, ProviderSettings
from littlems.report import (
    OpenAITextReportClient,
    _build_report_payload,
    build_age_context,
    build_report_source_summary,
    generate_markdown_report,
    generate_report_files,
    load_month_records,
    select_records_in_range,
)


def test_build_age_context_reports_date_range() -> None:
    context = build_age_context("2025-12-20", "2026-03-01", "2026-03-31")

    assert context == {
        "birth_date": "2025-12-20",
        "age_months_start": 2,
        "age_months_end": 3,
        "age_label": "满2到3个月",
    }


def test_build_age_context_rejects_birth_after_range() -> None:
    try:
        build_age_context("2026-04-01", "2026-03-01", "2026-03-31")
    except SystemExit as exc:
        assert exc.code == "Birth date is later than the target date range: 2026-04-01 > 2026-03-31"
    else:
        raise AssertionError("Expected build_age_context to reject future birth date")


def test_select_records_in_range_uses_only_target_dates() -> None:
    payload = _sample_descriptions_payload()

    records = select_records_in_range(payload["records"], "2026-03-01", "2026-03-31")

    assert [record["file_name"] for record in records] == ["mar-1.jpg", "mar-2.jpg", "mar-no-time.jpg"]


def test_load_month_records_uses_only_target_month() -> None:
    payload = _sample_descriptions_payload()

    records = load_month_records(payload, "2026-03")

    assert [record["file_name"] for record in records] == ["mar-1.jpg", "mar-2.jpg", "mar-no-time.jpg"]


def test_build_report_source_summary_contains_age_and_skill_candidates() -> None:
    payload = _sample_descriptions_payload()
    records = select_records_in_range(payload["records"], "2026-03-01", "2026-03-31")
    age_context = build_age_context("2025-12-20", "2026-03-01", "2026-03-31")

    summary = build_report_source_summary(records, payload["records"], age_context, baby_name="小满")

    assert summary["baby_name"] == "小满"
    assert summary["birth_date"] == "2025-12-20"
    assert summary["age_label"] == "满2到3个月"
    assert summary["record_count"] == 3
    assert summary["date_range"]["from"] == "2026-03-08"
    candidate_names = [item["skill_name"] for item in summary["candidate_new_skills"]]
    assert "趴卧" in candidate_names
    assert "独坐" in candidate_names


def test_generate_markdown_report_fails_over_to_next_provider(monkeypatch) -> None:
    settings = ProviderPoolSettings(
        providers=[
            ProviderSettings("a", "http://a.example/v1", "key-a", "model-a"),
            ProviderSettings("b", "http://b.example/v1", "key-b", "model-b"),
        ]
    )
    calls: list[str] = []

    async def fake_generate(self: OpenAITextReportClient, source_summary: dict[str, object]) -> str:
        del source_summary
        calls.append(self._provider.name)
        if self._provider.name == "a":
            raise RuntimeError("boom")
        return "# report\n\n这个月很温柔。\n"

    monkeypatch.setattr("littlems.report.OpenAITextReportClient.generate", fake_generate)

    result = asyncio.run(generate_markdown_report({"date_range": {"from": "2026-03-01"}}, settings))

    assert calls == ["a", "b"]
    assert result.provider_name == "b"
    assert result.provider_model == "model-b"
    assert result.markdown.startswith("# report")


def test_report_payload_uses_parent_to_baby_warm_voice() -> None:
    payload = _build_report_payload(
        {
            "baby_name": "小满",
            "age_label": "满2到3个月",
            "candidate_new_skills": [{"skill_name": "趴卧"}],
            "timeline": [],
            "representative_photos": [],
        },
        "model-a",
    )

    assert payload["temperature"] == 0.8
    messages = payload["messages"]
    assert isinstance(messages, list)
    system_message = messages[0]["content"]
    user_message = messages[1]["content"]
    assert "爸爸妈妈视角下，对宝宝成长的温柔观察记录" in system_message
    assert "不要直接对宝宝说话" in system_message
    assert "不要出现“根据素材”" in system_message
    assert "成长报告 Markdown 成品" in user_message


def test_generate_report_files_writes_markdown_and_debug_json(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "descriptions.json"
    output_path = tmp_path / "report.md"
    json_output_path = tmp_path / "report.json"
    input_path.write_text(json.dumps(_sample_descriptions_payload(), ensure_ascii=False), encoding="utf-8")
    settings = ProviderPoolSettings(providers=[ProviderSettings("a", "http://a.example/v1", "key-a", "model-a")])

    async def fake_generate_markdown_report(source_summary: dict[str, object], settings: ProviderPoolSettings) -> object:
        del settings
        assert source_summary["age_label"] == "满2到3个月"
        return type(
            "FakeResult",
            (),
            {
                "provider_name": "a",
                "provider_model": "model-a",
                "markdown": "# 三月报告\n\n这个阶段学会了趴卧。\n",
                "provider_attempts": [],
            },
        )()

    monkeypatch.setattr("littlems.report.generate_markdown_report", fake_generate_markdown_report)

    debug_document = asyncio.run(
        generate_report_files(
            input_path=input_path,
            date_from="2026-03-01",
            date_to="2026-03-31",
            birth_date="2025-12-20",
            baby_name="小满",
            output_path=output_path,
            settings=settings,
            json_output_path=json_output_path,
        )
    )

    assert output_path.read_text(encoding="utf-8") == "# 三月报告\n\n这个阶段学会了趴卧。\n"
    written_debug = json.loads(json_output_path.read_text(encoding="utf-8"))
    assert written_debug["locale"] == "zh-CN"
    assert written_debug["provider_name"] == "a"
    assert written_debug["baby_name"] == "小满"
    assert written_debug["date_range"] == {"from": "2026-03-01", "to": "2026-03-31"}
    assert debug_document["age_months_start"] == 2


def test_build_report_source_summary_rejects_blank_baby_name() -> None:
    payload = _sample_descriptions_payload()
    records = select_records_in_range(payload["records"], "2026-03-01", "2026-03-31")
    age_context = build_age_context("2025-12-20", "2026-03-01", "2026-03-31")

    try:
        build_report_source_summary(records, payload["records"], age_context, baby_name="   ")
    except SystemExit as exc:
        assert exc.code == "--baby-name cannot be empty"
    else:
        raise AssertionError("Expected blank baby name to be rejected")


def _sample_descriptions_payload() -> dict[str, object]:
    return {
        "version": 3,
        "source": {"kind": "local"},
        "records": [
            {
                "file_name": "feb-1.jpg",
                "file_path": "/tmp/feb-1.jpg",
                "source_id": "/tmp/feb-1.jpg",
                "captured_at": "2026-02-23T22:24:26",
                "summary": "A baby lying on a patterned mat while an adult holds a colorful rattle near the infant's hand.",
                "actions": ["lying down", "gripping toy"],
                "expressions": ["calm"],
                "scene": "Indoor setting",
                "highlights": ["Gentle interaction with the toy"],
                "uncertainty": None,
                "baby_present": True,
            },
            {
                "file_name": "mar-1.jpg",
                "file_path": "/tmp/mar-1.jpg",
                "source_id": "/tmp/mar-1.jpg",
                "captured_at": "2026-03-08T09:11:54",
                "summary": "A close-up photo of a baby lying on their stomach on a bed, looking directly at the camera.",
                "actions": ["Lying prone (on tummy)", "Looking forward"],
                "expressions": ["Curious"],
                "scene": "Bedroom",
                "highlights": ["Direct eye contact with the camera"],
                "uncertainty": None,
                "baby_present": True,
            },
            {
                "file_name": "mar-2.jpg",
                "file_path": "/tmp/mar-2.jpg",
                "source_id": "/tmp/mar-2.jpg",
                "captured_at": "2026-03-14T18:38:10",
                "summary": "A baby is sitting upright on a textured mat.",
                "actions": ["sitting upright"],
                "expressions": ["calm"],
                "scene": "Indoor setting",
                "highlights": ["steady posture"],
                "uncertainty": None,
                "baby_present": True,
            },
            {
                "file_name": "mar-no-time.jpg",
                "file_path": "/tmp/mar-no-time.jpg",
                "source_id": "/tmp/mar-no-time.jpg",
                "captured_at": "2026-03-20T08:00:00",
                "summary": "A baby smiles while resting.",
                "actions": ["resting"],
                "expressions": ["happy"],
                "scene": "Indoor setting",
                "highlights": [],
                "uncertainty": "The activity is partially obscured.",
                "baby_present": True,
            },
        ],
    }
