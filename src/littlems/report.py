from __future__ import annotations

import calendar
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from littlems.config import ProviderPoolSettings, ProviderSettings
from littlems.models import VisionProviderAttempt

logger = logging.getLogger(__name__)

REPORT_VERSION = 1
DEFAULT_REPORT_TIMEOUT = 120.0
_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    SkillRule("抓握玩具", ("gripping toy", "holding onto a rattle", "grip", "grasp", "hold a toy")),
    SkillRule("趴卧", ("lying prone", "on tummy", "prone", "tummy time")),
    SkillRule("抬头", ("resting chin on hands", "lifting head", "head up", "looking forward")),
    SkillRule("独坐", ("sitting upright", "sit upright", "sitting unaided")),
    SkillRule("看向镜头", ("looking at camera", "direct eye contact", "direct gaze", "looking directly at the camera")),
    SkillRule("与人互动", ("being held", "interaction", "adult hand", "adult holds", "held securely")),
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


async def generate_report_files(
    *,
    input_path: Path,
    month: str,
    birth_date: str,
    baby_name: str,
    output_path: Path,
    settings: ProviderPoolSettings,
    json_output_path: Path | None = None,
) -> dict[str, object]:
    payload = load_describe_document(input_path)
    month_records = load_month_records(payload, month)
    age_context = build_age_context(birth_date, month)
    source_summary = build_report_source_summary(month_records, payload.get("records"), age_context, baby_name=baby_name)
    result = await generate_markdown_report(source_summary, settings)

    markdown = _normalize_markdown(result.markdown)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    debug_document = {
        "version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "locale": "zh-CN",
        "month": source_summary["month"],
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


def load_describe_document(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Describe 输出文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Describe 输出文件不是合法 JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Describe 输出文件必须是 JSON 对象: {path}")
    records = payload.get("records")
    if not isinstance(records, list):
        raise SystemExit("Describe 输出缺少 records 数组")
    return payload


def load_month_records(payload: dict[str, object], month: str) -> list[dict[str, object]]:
    parsed_month = _parse_month(month)
    records = payload.get("records")
    assert isinstance(records, list)
    month_records: list[dict[str, object]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        captured_at = item.get("captured_at")
        if isinstance(captured_at, str) and captured_at[:7] == parsed_month:
            month_records.append(item)
    if not month_records:
        raise SystemExit(f"指定月份没有可用照片记录: {parsed_month}")
    return sorted(month_records, key=_record_sort_key)


def build_age_context(birth_date: str, month: str) -> dict[str, object]:
    birth = _parse_birth_date(birth_date)
    parsed_month = _parse_month(month)
    year, month_value = (int(part) for part in parsed_month.split("-"))
    month_start = date(year, month_value, 1)
    month_end = date(year, month_value, calendar.monthrange(year, month_value)[1])
    if birth > month_end:
        raise SystemExit(f"出生日期晚于目标月份: {birth_date} > {parsed_month}")

    age_months_start = _full_months_between(birth, month_start)
    age_months_end = _full_months_between(birth, month_end)
    return {
        "birth_date": birth.isoformat(),
        "age_months_start": age_months_start,
        "age_months_end": age_months_end,
        "age_label": _age_label(age_months_start, age_months_end, birth, month_start, month_end),
    }


def build_report_source_summary(
    records: list[dict[str, object]],
    history: object,
    age_context: dict[str, object],
    *,
    baby_name: str,
) -> dict[str, object]:
    history_records = [item for item in history or [] if isinstance(item, dict)]
    if not records:
        raise SystemExit("当前月份没有可用于生成月报的照片记录")
    normalized_baby_name = baby_name.strip()
    if not normalized_baby_name:
        raise SystemExit("--baby-name 不能为空")

    first_record = records[0]
    first_captured = str(first_record.get("captured_at"))
    month = first_captured[:7]
    timeline = [_timeline_item(record) for record in records]
    representative_photos = [_photo_summary(record) for record in _pick_representative_records(records)]
    candidate_new_skills = _candidate_new_skills(records, history_records, month)
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
        "month": month,
        "baby_name": normalized_baby_name,
        "birth_date": age_context["birth_date"],
        "age_months_start": age_context["age_months_start"],
        "age_months_end": age_context["age_months_end"],
        "age_label": age_context["age_label"],
        "record_count": len(records),
        "date_range": {
            "first_captured_at": str(records[0].get("captured_at")),
            "last_captured_at": str(records[-1].get("captured_at")),
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
                    "重点写这个月的新变化、新掌握的能力、让人想记住的日常瞬间，以及一点轻柔的月末寄语。"
                    "可以结合月龄阶段帮助表达，但不要写成医学建议或发展评估结论。"
                    "只能基于提供的事实素材写作，不要编造素材里没有的时间、地点、人物关系、情节或技能。"
                    "默认优先使用提供的宝宝姓名来指代主角，必要时再用“宝宝”补充，不默认使用“她/他”或“孩子”。"
                    "宝宝姓名的使用要自然克制：标题里不必强行出现名字，正文前半段自然出现 1 次即可，后文避免反复点名。"
                    "尽量少用抽象赞美词和空泛抒情，多写看得见的动作、表情、姿态、互动和场景细节，让文字有画面感。"
                    "不要机械复述 JSON 字段名，不要出现“根据素材”“根据数据”“从照片中可以看出”“AI”“模型”等元话语。"
                    "禁止出现“这个月想对你说……”“愿你……”“我们看着你……”“亲爱的宝贝……”这类直接对话式表达。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请根据下面的素材，为宝宝写一篇可以直接阅读的中文月报 Markdown 成品。\n"
                    "写作要求：\n"
                    "1. 标题自然温柔，不必太正式。\n"
                    "2. 正文建议 4 到 6 个短段落，读起来像月末写下的成长观察，不像总结报告。\n"
                    "3. 开头自然带出这个月的月龄阶段和整体变化。\n"
                    "4. 如果素材里提供了宝宝姓名，正文前半段自然使用 1 次即可；标题里不必强行带名字，后文避免反复重复姓名。\n"
                    "5. 中间重点写这个月新学会的本领或正在变稳的能力，比如抓握、趴卧、抬头、独坐、互动。\n"
                    "6. 选 2 到 4 个有画面感的温馨瞬间写进去，但必须来自素材本身。\n"
                    "7. 少用“很美好”“很治愈”“特别珍贵”这类抽象评价，优先写清楚具体发生了什么。\n"
                    "8. 结尾写一句温柔收束的总结或感受，但不要直接对宝宝说话。\n"
                    "9. 如果这个月没有明显的新技能，请写成练习和积累中的变化，不要硬凑 milestone。\n"
                    "10. 尽量避免口号式、模板化、公众号腔、鸡汤腔。\n\n"
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


def _parse_month(value: str) -> str:
    if not _MONTH_PATTERN.fullmatch(value):
        raise SystemExit(f"--month 必须是 YYYY-MM 格式: {value}")
    year, month = value.split("-")
    try:
        parsed = datetime(int(year), int(month), 1)
    except ValueError as exc:
        raise SystemExit(f"--month 必须是 YYYY-MM 格式: {value}") from exc
    return parsed.strftime("%Y-%m")


def _parse_birth_date(value: str) -> date:
    if not _DATE_PATTERN.fullmatch(value):
        raise SystemExit(f"--birth-date 必须是 YYYY-MM-DD 格式: {value}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"--birth-date 必须是 YYYY-MM-DD 格式: {value}") from exc


def _record_sort_key(record: dict[str, object]) -> tuple[str, str]:
    captured_at = record.get("captured_at")
    return (str(captured_at), str(record.get("file_path")))


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
    month_records: list[dict[str, object]],
    history_records: list[dict[str, object]],
    month: str,
) -> list[dict[str, object]]:
    skill_history: dict[str, list[dict[str, object]]] = {}
    for record in sorted(history_records, key=_record_sort_key):
        captured_at = record.get("captured_at")
        if not isinstance(captured_at, str):
            continue
        for skill in _matched_skills(record):
            skill_history.setdefault(skill, []).append(record)

    month_paths = {str(record.get("file_path")) for record in month_records}
    candidates: list[dict[str, object]] = []
    for rule in _SKILL_RULES:
        history = skill_history.get(rule.skill_name, [])
        if not history:
            continue
        first_seen = str(history[0].get("captured_at"))
        if first_seen[:7] != month:
            continue
        evidence = [record for record in history if str(record.get("file_path")) in month_paths]
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


def _age_label(age_months_start: int, age_months_end: int, birth: date, month_start: date, month_end: date) -> str:
    if birth.year == month_start.year and birth.month == month_start.month:
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
