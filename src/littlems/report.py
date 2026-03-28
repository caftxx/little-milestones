from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time
from pathlib import Path

import httpx

from littlems.config import ProviderPoolSettings, ProviderSettings
from littlems.models import VisionProviderAttempt

logger = logging.getLogger(__name__)

REPORT_VERSION = 2
DEFAULT_REPORT_TIMEOUT = 120.0
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True, slots=True)
class SkillRule:
    skill_name: str
    keywords: tuple[str, ...]


@dataclass(slots=True)
class ReportGenerationResult:
    provider_name: str
    provider_model: str
    markdown: str
    provider_attempts: list[VisionProviderAttempt]


class ReportProviderFailure(RuntimeError):
    def __init__(self, provider_attempts: list[VisionProviderAttempt]) -> None:
        self.provider_attempts = provider_attempts
        joined_errors = "; ".join(
            f"{attempt.provider_name}: {attempt.error}"
            for attempt in provider_attempts
            if attempt.error
        )
        super().__init__(f"All providers failed for report generation: {joined_errors}")


_SKILL_RULES = (
    SkillRule("抓握玩具", ("gripping toy", "holding onto a rattle", "grip", "grasp", "hold a toy", "抓握", "玩具")),
    SkillRule("趴卧", ("lying prone", "on tummy", "prone", "tummy time", "趴卧")),
    SkillRule("抬头", ("resting chin on hands", "lifting head", "head up", "looking forward", "抬头")),
    SkillRule("独坐", ("sitting upright", "sit upright", "sitting unaided", "独坐")),
    SkillRule("看向镜头", ("looking at camera", "direct eye contact", "direct gaze", "看向镜头")),
    SkillRule("与人互动", ("being held", "interaction", "adult hand", "adult holds", "held securely", "互动")),
)


class OpenAITextReportClient:
    def __init__(self, provider: ProviderSettings) -> None:
        self._provider = provider
        self._base_url = provider.base_url.rstrip("/")
        self._timeout = provider.timeout or DEFAULT_REPORT_TIMEOUT

    async def generate(self, source_summary: dict[str, object]) -> str:
        payload = _build_report_payload(source_summary, self._provider.vision_model)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._provider.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.is_error:
            logger.warning(
                "report generation failed provider=%s status=%s body=%s",
                self._provider.name,
                response.status_code,
                _response_text_excerpt(response),
            )
        response.raise_for_status()
        return _extract_text_content(response.json()).strip()


def load_description_document(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Description file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Description file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Description file must contain a JSON object: {path}")
    records = payload.get("records")
    if not isinstance(records, list):
        raise SystemExit("Description file is missing the records array")
    return payload


def select_records_in_range(records: list[dict[str, object]], date_from: str, date_to: str) -> list[dict[str, object]]:
    start, end = parse_date_range(date_from, date_to)
    selected: list[dict[str, object]] = []
    for record in records:
        captured_at = record.get("captured_at")
        if not isinstance(captured_at, str):
            continue
        parsed = _parse_captured_at(captured_at)
        if parsed is None:
            continue
        if start <= parsed <= end:
            selected.append(record)
    if not selected:
        raise SystemExit(f"No photo records found in the requested date range: {date_from}..{date_to}")
    return sorted(selected, key=_record_sort_key)


def load_month_records(payload: dict[str, object], month: str) -> list[dict[str, object]]:
    if not _MONTH_PATTERN.fullmatch(month):
        raise SystemExit(f"--month must use YYYY-MM format: {month}")
    year, month_value = month.split("-")
    from_date = f"{year}-{month_value}-01"
    end_day = 31
    while end_day >= 28:
        try:
            to_date = date(int(year), int(month_value), end_day).isoformat()
            break
        except ValueError:
            end_day -= 1
    else:  # pragma: no cover
        raise SystemExit(f"--month must use YYYY-MM format: {month}")
    records = [item for item in payload.get("records", []) if isinstance(item, dict)]
    return select_records_in_range(records, from_date, to_date)


def parse_date_range(date_from: str, date_to: str) -> tuple[datetime, datetime]:
    start_date = _parse_date(date_from, "--from")
    end_date = _parse_date(date_to, "--to")
    if end_date < start_date:
        raise SystemExit(f"--to cannot be earlier than --from: {date_to} < {date_from}")
    return (
        datetime.combine(start_date, dt_time.min),
        datetime.combine(end_date, dt_time.max),
    )


def build_age_context(birth_date: str, date_from: str, date_to: str) -> dict[str, object]:
    birth = _parse_date(birth_date, "--birth-date")
    start_date = _parse_date(date_from, "--from")
    end_date = _parse_date(date_to, "--to")
    if birth > end_date:
        raise SystemExit(f"Birth date is later than the target date range: {birth_date} > {date_to}")
    age_months_start = _full_months_between(birth, start_date)
    age_months_end = _full_months_between(birth, end_date)
    return {
        "birth_date": birth.isoformat(),
        "age_months_start": age_months_start,
        "age_months_end": age_months_end,
        "age_label": _age_label(age_months_start, age_months_end, birth, start_date, end_date),
    }


def build_report_source_summary(
    records: list[dict[str, object]],
    history: list[dict[str, object]],
    age_context: dict[str, object],
    *,
    baby_name: str,
) -> dict[str, object]:
    if not records:
        raise SystemExit("当前时间范围没有可用于生成报告的照片记录")
    normalized_baby_name = baby_name.strip()
    if not normalized_baby_name:
        raise SystemExit("--baby-name cannot be empty")

    first_captured = str(records[0].get("captured_at"))
    last_captured = str(records[-1].get("captured_at"))
    timeline = [_timeline_item(record) for record in records]
    representative_photos = [_photo_summary(record) for record in _pick_representative_records(records)]
    candidate_new_skills = _candidate_new_skills(records, history)
    uncertain_items = [
        {
            "captured_at": str(record.get("captured_at")),
            "file_name": str(record.get("file_name")),
            "uncertainty": str(record.get("uncertainty")),
        }
        for record in records
        if isinstance(record.get("uncertainty"), str) and str(record.get("uncertainty")).strip()
    ]
    return {
        "baby_name": normalized_baby_name,
        "birth_date": age_context["birth_date"],
        "age_months_start": age_context["age_months_start"],
        "age_months_end": age_context["age_months_end"],
        "age_label": age_context["age_label"],
        "record_count": len(records),
        "date_range": {
            "from": first_captured[:10],
            "to": last_captured[:10],
            "first_captured_at": first_captured,
            "last_captured_at": last_captured,
        },
        "timeline": timeline,
        "representative_photos": representative_photos,
        "top_actions": _top_tags(records, "actions"),
        "top_expressions": _top_tags(records, "expressions"),
        "top_scenes": _top_scene_tags(records),
        "candidate_new_skills": candidate_new_skills,
        "uncertain_items": uncertain_items,
    }


async def generate_markdown_report(
    source_summary: dict[str, object],
    settings: ProviderPoolSettings,
) -> ReportGenerationResult:
    attempts: list[VisionProviderAttempt] = []
    for provider in settings.providers:
        started_ns = time.perf_counter_ns()
        try:
            markdown = await OpenAITextReportClient(provider).generate(source_summary)
            finished_ns = time.perf_counter_ns()
            attempts.append(
                VisionProviderAttempt(
                    provider_name=provider.name,
                    elapsed_ms=_elapsed_ms_between(started_ns, finished_ns),
                    ok=True,
                    started_at_monotonic_ns=started_ns,
                    finished_at_monotonic_ns=finished_ns,
                )
            )
            return ReportGenerationResult(
                provider_name=provider.name,
                provider_model=provider.vision_model,
                markdown=markdown,
                provider_attempts=attempts,
            )
        except Exception as exc:
            finished_ns = time.perf_counter_ns()
            attempts.append(
                VisionProviderAttempt(
                    provider_name=provider.name,
                    elapsed_ms=_elapsed_ms_between(started_ns, finished_ns),
                    ok=False,
                    error=str(exc),
                    started_at_monotonic_ns=started_ns,
                    finished_at_monotonic_ns=finished_ns,
                )
            )
            logger.warning("report provider failed provider=%s error=%s", provider.name, exc)
    raise ReportProviderFailure(attempts)


async def generate_report_files(
    *,
    input_path: Path,
    date_from: str,
    date_to: str,
    birth_date: str,
    baby_name: str,
    output_path: Path,
    settings: ProviderPoolSettings,
    json_output_path: Path | None = None,
) -> dict[str, object]:
    payload = load_description_document(input_path)
    all_records = [item for item in payload.get("records", []) if isinstance(item, dict)]
    selected_records = select_records_in_range(all_records, date_from, date_to)
    return await generate_report_for_records(
        records=selected_records,
        history_records=all_records,
        date_from=date_from,
        date_to=date_to,
        birth_date=birth_date,
        baby_name=baby_name,
        output_path=output_path,
        settings=settings,
        json_output_path=json_output_path,
        source=payload.get("source"),
    )


async def generate_report_for_records(
    *,
    records: list[dict[str, object]],
    history_records: list[dict[str, object]],
    date_from: str,
    date_to: str,
    birth_date: str,
    baby_name: str,
    output_path: Path,
    settings: ProviderPoolSettings,
    json_output_path: Path | None = None,
    source: object = None,
) -> dict[str, object]:
    age_context = build_age_context(birth_date, date_from, date_to)
    source_summary = build_report_source_summary(records, history_records, age_context, baby_name=baby_name)
    result = await generate_markdown_report(source_summary, settings)
    markdown = _normalize_markdown(result.markdown)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    debug_document = {
        "version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "locale": "zh-CN",
        "source": source,
        "date_range": {
            "from": date_from,
            "to": date_to,
        },
        "baby_name": source_summary["baby_name"],
        "birth_date": age_context["birth_date"],
        "age_months_start": age_context["age_months_start"],
        "age_months_end": age_context["age_months_end"],
        "age_label": age_context["age_label"],
        "provider_name": result.provider_name,
        "provider_model": result.provider_model,
        "source_summary": source_summary,
        "markdown": markdown,
    }
    if json_output_path is not None:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(debug_document, ensure_ascii=False, indent=2), encoding="utf-8")
    return debug_document


def _build_report_payload(source_summary: dict[str, object], model: str) -> dict[str, object]:
    return {
        "model": model,
        "temperature": 0.8,
        "response_format": {"type": "text"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一位细腻、克制、温暖的中文成长月报写作者。"
                    "请把月报写成爸爸妈妈视角下，对宝宝成长的温柔观察记录。"
                    "保留亲近感和情感温度，但不要直接对宝宝说话，不要写成信件、寄语或家书。"
                    "输出必须是简体中文 Markdown，语气自然、温柔、真诚，不要夸张煽情，不要空泛抒情。"
                    "不要夹带英文标题、英文小结、英文解释、字段翻译或其他英文元话语。"
                    "重点写这个时间范围里的新变化、新掌握的能力、让人想记住的日常瞬间，以及一点轻柔的收束。"
                    "可以结合月龄阶段帮助表达，但不要写成医学建议或发展评估结论。"
                    "只能基于提供的事实素材写作，不要编造素材里没有的时间、地点、人物关系、情节或技能。"
                    "默认优先使用提供的宝宝姓名来指代主角，必要时再用“宝宝”补充，不默认使用“她/他”或“孩子”。"
                    "尽量少用抽象赞美词和空泛抒情，多写看得见的动作、表情、姿态、互动和场景细节，让文字有画面感。"
                    "不要机械复述 JSON 字段名，不要出现“根据素材”“根据数据”“从照片中可以看出”“AI”“模型”等元话语。"
                    "禁止出现“这个月想对你说……”“愿你……”“我们看着你……”“亲爱的宝贝……”这类直接对话式表达。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请根据下面的素材，为宝宝写一篇可以直接阅读的中文成长报告 Markdown 成品。\n"
                    "写作要求：\n"
                    "1. 标题自然温柔，不必太正式。\n"
                    "2. 正文建议 4 到 6 个短段落，读起来像阶段性成长观察，不像总结报告。\n"
                    "3. 开头自然带出这个时间范围的月龄阶段和整体变化。\n"
                    "4. 如果素材里提供了宝宝姓名，正文前半段自然使用 1 次即可。\n"
                    "5. 中间重点写这个阶段新学会的本领或正在变稳的能力。\n"
                    "6. 选 2 到 4 个有画面感的温馨瞬间写进去，但必须来自素材本身。\n"
                    "7. 少用抽象评价，优先写清楚具体发生了什么。\n"
                    "8. 结尾写一句温柔收束的总结或感受，但不要直接对宝宝说话。\n"
                    "9. 如果没有明显的新技能，请写成练习和积累中的变化，不要硬凑 milestone。\n"
                    f"素材如下：\n{json.dumps(source_summary, ensure_ascii=False, indent=2)}"
                ),
            },
        ],
    }


def _extract_text_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Missing choices in model response")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("Missing message in model response")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("Missing message payload in model response")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
        if parts:
            return "\n".join(parts)
    raise ValueError("Missing content in model response")


def _response_text_excerpt(response: httpx.Response, limit: int = 500) -> str:
    text = response.text.strip()
    return text if len(text) <= limit else f"{text[:limit]}..."


def _parse_date(value: str, label: str) -> date:
    if not _DATE_PATTERN.fullmatch(value):
        raise SystemExit(f"{label} 必须是 YYYY-MM-DD 格式: {value}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{label} 必须是 YYYY-MM-DD 格式: {value}") from exc


def _parse_captured_at(value: str) -> datetime | None:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _record_sort_key(record: dict[str, object]) -> tuple[str, str]:
    captured_at = record.get("captured_at")
    return (str(captured_at), str(record.get("file_path") or record.get("source_id") or record.get("file_name")))


def _timeline_item(record: dict[str, object]) -> dict[str, object]:
    return {
        "captured_at": str(record.get("captured_at")),
        "summary": record.get("summary"),
        "actions": _string_list(record.get("actions")),
        "expressions": _string_list(record.get("expressions")),
        "highlights": _string_list(record.get("highlights")),
        "uncertainty": record.get("uncertainty"),
    }


def _photo_summary(record: dict[str, object]) -> dict[str, object]:
    return {
        "file_name": str(record.get("file_name")),
        "captured_at": str(record.get("captured_at")),
        "summary": record.get("summary"),
        "actions": _string_list(record.get("actions")),
        "highlights": _string_list(record.get("highlights")),
    }


def _pick_representative_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    ranked = sorted(
        records,
        key=lambda record: (
            0 if record.get("baby_present") is True else 1,
            0 if _string_list(record.get("highlights")) else 1,
            str(record.get("captured_at")),
        ),
    )
    picked: list[dict[str, object]] = []
    used_days: set[str] = set()
    for record in ranked:
        captured_at = str(record.get("captured_at"))
        day = captured_at[:10]
        if day in used_days and len(picked) >= 3:
            continue
        used_days.add(day)
        picked.append(record)
        if len(picked) == 5:
            break
    return picked


def _candidate_new_skills(
    selected_records: list[dict[str, object]],
    history_records: list[dict[str, object]],
) -> list[dict[str, object]]:
    selected_ids = {
        str(record.get("source_id") or record.get("file_path") or record.get("file_name"))
        for record in selected_records
    }
    skill_history: dict[str, list[dict[str, object]]] = {}
    for record in sorted(history_records, key=_record_sort_key):
        for skill in _matched_skills(record):
            skill_history.setdefault(skill, []).append(record)

    candidates: list[dict[str, object]] = []
    for rule in _SKILL_RULES:
        history = skill_history.get(rule.skill_name, [])
        if not history:
            continue
        evidence = [
            record
            for record in history
            if str(record.get("source_id") or record.get("file_path") or record.get("file_name")) in selected_ids
        ]
        if not evidence:
            continue
        first_seen = str(history[0].get("captured_at"))
        candidates.append(
            {
                "skill_name": rule.skill_name,
                "first_seen_at": first_seen,
                "evidence_count": len(evidence),
                "evidence_summaries": [str(record.get("summary")) for record in evidence[:3]],
            }
        )
    return candidates


def _matched_skills(record: dict[str, object]) -> list[str]:
    haystacks = []
    haystacks.extend(text.lower() for text in _string_list(record.get("actions")))
    haystacks.extend(text.lower() for text in _string_list(record.get("highlights")))
    summary = record.get("summary")
    if isinstance(summary, str):
        haystacks.append(summary.lower())
    text = "\n".join(haystacks)
    return [rule.skill_name for rule in _SKILL_RULES if any(keyword in text for keyword in rule.keywords)]


def _top_tags(records: list[dict[str, object]], field: str) -> list[str]:
    counter: Counter[str] = Counter()
    for record in records:
        for item in _string_list(record.get(field)):
            counter[item] += 1
    return [name for name, _ in counter.most_common(8)]


def _top_scene_tags(records: list[dict[str, object]]) -> list[str]:
    counter: Counter[str] = Counter()
    for record in records:
        scene = record.get("scene")
        if isinstance(scene, str) and scene.strip():
            counter[scene.strip()] += 1
    return [name for name, _ in counter.most_common(5)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _full_months_between(birth: date, target: date) -> int:
    months = (target.year - birth.year) * 12 + (target.month - birth.month)
    if target.day < birth.day:
        months -= 1
    return max(0, months)


def _age_label(age_months_start: int, age_months_end: int, birth: date, start_date: date, end_date: date) -> str:
    if birth.year == start_date.year and birth.month == start_date.month:
        return "出生当月"
    if age_months_start == age_months_end:
        return f"满{age_months_end}个月"
    return f"满{age_months_start}到{age_months_end}个月"


def _elapsed_ms_between(started_ns: int, finished_ns: int) -> int:
    return max(0, round((finished_ns - started_ns) / 1_000_000))


def _normalize_markdown(markdown: str) -> str:
    text = markdown.strip()
    if text.startswith("```markdown"):
        text = text.removeprefix("```markdown").removesuffix("```").strip()
    elif text.startswith("```md"):
        text = text.removeprefix("```md").removesuffix("```").strip()
    elif text.startswith("```"):
        text = text.removeprefix("```").removesuffix("```").strip()
    return f"{text}\n"
