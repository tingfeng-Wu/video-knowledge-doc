"""两阶段LLM分章候选：先识别主题，再选择最早主题引入ASR事件。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from video_doc.asr_timeline import ASRTimelineError, load_task_timeline
from video_doc.chapter_policy import (
    STABILITY_MIN_CHAPTER_SECONDS,
    STABILITY_POLICY_VERSION,
    render_stability_policy,
)
from video_doc.llm_adapter import (
    LLMAdapterError,
    LLMConfig,
    LLMRetryableError,
    LLMTransport,
    call_task_llm,
)
from video_doc.llm_strategy import (
    CHAPTER_PLAN_SCHEMA_VERSION,
    DEFAULT_MIN_CHAPTER_SECONDS,
    ChapterPlanError,
    ChapterPlanResult,
    experiment_artifact_subdir,
    validate_experiment_id,
    validate_chapter_plan,
)


TWO_STAGE_PROMPT_VERSION = "chapter-two-stage-2.0"
STABILITY_TWO_STAGE_PROMPT_VERSION = "chapter-two-stage-stability-1.0"
OUTLINE_CALL_ID = "stage8-theme-outline-v2"
ALIGNMENT_CALL_ID = "stage8-boundary-align-v2"
OUTLINE_VALIDATION_ID = "chapter-outline-v2"
ALIGNMENT_VALIDATION_ID = "chapter-alignment-v2"
DEFAULT_ALIGNMENT_WINDOW_SECONDS = 90.0


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ChapterPlanError(f"{field}必须是有效数字。") from exc
    if not math.isfinite(number):
        raise ChapterPlanError(f"{field}必须是有限数字。")
    return number


def _validated_positive_seconds(value: Any, field: str) -> float:
    seconds = _finite_number(value, field)
    if seconds <= 0:
        raise ChapterPlanError(f"{field}必须大于0。")
    return round(seconds, 3)


def _timeline_sha256(timeline: dict[str, Any]) -> str:
    encoded = json.dumps(
        timeline,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _render_full_timeline(timeline: dict[str, Any]) -> str:
    return "\n".join(
        f"[{event['id']}] start={float(event['start']):.3f}s "
        f"end={float(event['end']):.3f}s text={event['text']}"
        for event in timeline["events"]
    )


def build_outline_messages(
    timeline: dict[str, Any],
    *,
    min_chapter_seconds: float,
    stability_experiment: bool = False,
) -> list[dict[str, str]]:
    """第一阶段只识别连续主题和粗略位置，不承担精确边界定位。"""

    minimum = _validated_positive_seconds(min_chapter_seconds, "最小章节时长")
    duration = float(timeline["duration_seconds"])
    system = (
        "你负责识别知识型视频中按顺序出现的主要章节主题。第一阶段只判断有哪些连续主题"
        "以及大致从哪里开始，不做精确事件定位。只返回JSON对象。"
    )
    prompt_version = (
        STABILITY_TWO_STAGE_PROMPT_VERSION
        if stability_experiment
        else TWO_STAGE_PROMPT_VERSION
    )
    policy = (
        render_stability_policy(duration) + "\n" if stability_experiment else ""
    )
    user = (
        f"提示词版本：{prompt_version}。视频时长{duration:.3f}秒，"
        f"期望每章至少约{minimum:.3f}秒。第一章rough_timestamp必须为0.0，后续粗略时间"
        "必须使用十进制秒并严格递增。标题要概括整章主题，不要使用章内单个例子、名言、"
        "数据或解释句作为整章标题。\n"
        f"{policy}"
        "只返回："
        '{"chapters":[{"title":"...","rough_timestamp":0.0}]}。\n'
        "完整ASR时间轴：\n"
        f"{_render_full_timeline(timeline)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_outline_response(
    response: Any,
    timeline: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ChapterPlanError("主题提取响应必须是JSON对象。")
    raw_chapters = response.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        raise ChapterPlanError("主题提取响应必须包含非空chapters数组。")
    duration = float(timeline["duration_seconds"])
    chapters: list[dict[str, Any]] = []
    previous = -1.0
    for index, raw in enumerate(raw_chapters, start=1):
        if not isinstance(raw, dict):
            raise ChapterPlanError(f"第{index}个粗略章节必须是JSON对象。")
        title = raw.get("title")
        timestamp = _finite_number(raw.get("rough_timestamp"), f"第{index}章粗略时间")
        if not isinstance(title, str) or not title.strip():
            raise ChapterPlanError(f"第{index}章标题不能为空。")
        if index == 1 and abs(timestamp) > 0.001:
            raise ChapterPlanError("第一章粗略时间必须为0.0秒。")
        if index > 1 and not 0 < timestamp < duration:
            raise ChapterPlanError(f"第{index}章粗略时间越界。")
        if timestamp <= previous:
            raise ChapterPlanError("粗略章节时间必须严格递增。")
        chapters.append(
            {"title": title.strip(), "rough_timestamp": round(timestamp, 3)}
        )
        previous = timestamp
    return {"chapters": chapters}


def build_alignment_windows(
    outline: dict[str, Any],
    timeline: dict[str, Any],
    *,
    window_seconds: float = DEFAULT_ALIGNMENT_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """为每个非首章粗略位置构造确定性的局部ASR窗口。"""

    radius = _validated_positive_seconds(window_seconds, "边界定位窗口半径")
    chapters = outline.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ChapterPlanError("粗略章节结果无效。")
    duration = float(timeline["duration_seconds"])
    windows: list[dict[str, Any]] = []
    for index in range(1, len(chapters)):
        rough = float(chapters[index]["rough_timestamp"])
        start = max(0.0, rough - radius)
        end = min(duration, rough + radius)
        events = [
            {
                "id": event["id"],
                "start": event["start"],
                "end": event["end"],
                "text": event["text"],
            }
            for event in timeline["events"]
            if float(event["end"]) > start and float(event["start"]) < end
        ]
        if not events:
            raise ChapterPlanError(f"第{index + 1}章粗略位置附近没有ASR事件。")
        windows.append(
            {
                "transition_id": f"t{index:04d}",
                "previous_title": chapters[index - 1]["title"],
                "new_title": chapters[index]["title"],
                "rough_timestamp": round(rough, 3),
                "window_start": round(start, 3),
                "window_end": round(end, 3),
                "events": events,
            }
        )
    return windows


def build_alignment_messages(
    windows: list[dict[str, Any]],
    *,
    stability_experiment: bool = False,
) -> list[dict[str, str]]:
    """第二阶段批量选择每个新主题最早被明确引入的ASR事件。"""

    system = (
        "你负责精确定位章节边界。对每个局部ASR窗口，选择新主题最早被明确引入的事件。"
        "不得选择主题内部的例子、解释、数据、名言、总结或代表性句子。只能返回输入中存在"
        "的source_event_id，不得自行生成时间。只返回JSON对象。"
    )
    prompt_version = (
        STABILITY_TWO_STAGE_PROMPT_VERSION
        if stability_experiment
        else TWO_STAGE_PROMPT_VERSION
    )
    user = (
        f"提示词版本：{prompt_version}。每个transition必须恰好返回一条边界，"
        "顺序和transition_id必须保持一致。title应概括从该边界开始的整章；"
        "previous_topic和new_topic用于说明你判断的主题转换。只返回："
        '{"boundaries":[{"transition_id":"t0001","title":"...",'
        '"source_event_id":"a0001","previous_topic":"...",'
        '"new_topic":"..."}]}。\n'
        "局部窗口：\n"
        + json.dumps(windows, ensure_ascii=False, separators=(",", ":"))
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_alignment_response(
    response: Any,
    outline: dict[str, Any],
    windows: list[dict[str, Any]],
    timeline: dict[str, Any],
    *,
    min_chapter_seconds: float,
) -> dict[str, Any]:
    """校验事件ID选择，并用事件start派生最终候选边界。"""

    if not isinstance(response, dict):
        raise ChapterPlanError("边界定位响应必须是JSON对象。")
    raw_boundaries = response.get("boundaries")
    if not isinstance(raw_boundaries, list) or len(raw_boundaries) != len(windows):
        raise ChapterPlanError("边界定位结果数量必须与局部窗口数量一致。")
    all_events = {str(event["id"]): event for event in timeline["events"]}
    chapters = outline["chapters"]
    normalized: list[dict[str, Any]] = []
    timestamps = [0.0]
    for index, (raw, window) in enumerate(zip(raw_boundaries, windows), start=1):
        if not isinstance(raw, dict):
            raise ChapterPlanError(f"第{index}条定位边界必须是JSON对象。")
        if raw.get("transition_id") != window["transition_id"]:
            raise ChapterPlanError("边界定位transition_id或顺序无效。")
        title = raw.get("title")
        event_id = raw.get("source_event_id")
        previous_topic = raw.get("previous_topic")
        new_topic = raw.get("new_topic")
        if not isinstance(title, str) or not title.strip():
            raise ChapterPlanError(f"第{index}条定位边界标题不能为空。")
        if not isinstance(event_id, str) or event_id not in all_events:
            raise ChapterPlanError(f"第{index}条定位边界引用的ASR事件不存在。")
        allowed_ids = {str(event["id"]) for event in window["events"]}
        if event_id not in allowed_ids:
            raise ChapterPlanError(f"第{index}条定位边界引用了窗口外事件。")
        if not isinstance(previous_topic, str) or not previous_topic.strip():
            raise ChapterPlanError(f"第{index}条定位边界缺少前一主题。")
        if not isinstance(new_topic, str) or not new_topic.strip():
            raise ChapterPlanError(f"第{index}条定位边界缺少新主题。")
        timestamp = round(float(all_events[event_id]["start"]), 3)
        if timestamp <= timestamps[-1]:
            raise ChapterPlanError("事件ID派生的章节边界必须严格递增。")
        timestamps.append(timestamp)
        normalized.append(
            {
                "transition_id": window["transition_id"],
                "title": title.strip(),
                "source_event_id": event_id,
                "timestamp": timestamp,
                "previous_topic": previous_topic.strip(),
                "new_topic": new_topic.strip(),
            }
        )

    minimum = _validated_positive_seconds(min_chapter_seconds, "最小章节时长")
    duration = float(timeline["duration_seconds"])
    for index, (start, end) in enumerate(
        zip(timestamps, timestamps[1:] + [duration]), start=1
    ):
        if end - start + 1e-9 < minimum:
            raise ChapterPlanError(
                f"两阶段候选第{index}章时长小于{minimum:.3f}秒。"
            )
    return {
        "first_title": chapters[0]["title"],
        "boundaries": normalized,
    }


def _build_candidate_plan(
    task_id: str,
    timeline: dict[str, Any],
    alignment: dict[str, Any],
    *,
    min_chapter_seconds: float,
    window_seconds: float,
    prompt_version: str = TWO_STAGE_PROMPT_VERSION,
    experiment_id: str | None = None,
) -> dict[str, Any]:
    boundaries = [
        {
            "timestamp": 0.0,
            "title": alignment["first_title"],
            "source_event_id": None,
        }
    ]
    boundaries.extend(
        {
            "timestamp": boundary["timestamp"],
            "title": boundary["title"],
            "source_event_id": boundary["source_event_id"],
        }
        for boundary in alignment["boundaries"]
    )
    parameters: dict[str, Any] = {
        "prompt_version": prompt_version,
        "min_chapter_seconds": _validated_positive_seconds(
            min_chapter_seconds, "最小章节时长"
        ),
        "boundary_alignment": "selected_asr_event_start",
        "alignment_window_seconds": _validated_positive_seconds(
            window_seconds, "边界定位窗口半径"
        ),
    }
    if experiment_id is not None:
        parameters["experiment_id"] = experiment_id
        parameters["chapter_policy_version"] = STABILITY_POLICY_VERSION
    return {
        "schema_version": CHAPTER_PLAN_SCHEMA_VERSION,
        "task_id": task_id,
        "strategy": "llm",
        "source_timeline": {
            "schema_version": timeline["schema_version"],
            "sha256": _timeline_sha256(timeline),
            "event_count": len(timeline["events"]),
        },
        "duration_seconds": round(float(timeline["duration_seconds"]), 3),
        "parameters": parameters,
        "boundaries": boundaries,
    }


def _remove_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _cached_candidate_matches(
    path: Path,
    expected: dict[str, Any],
    timeline: dict[str, Any],
    task_id: str,
) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_chapter_plan(payload, timeline=timeline, expected_task_id=task_id)
    except (ChapterPlanError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == expected


def plan_task_chapters_two_stage(
    output_root: str | Path,
    task_id: str,
    *,
    min_chapter_seconds: float | None = None,
    window_seconds: float = DEFAULT_ALIGNMENT_WINDOW_SECONDS,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
    experiment_id: str | None = None,
    save_validation_audit: bool = False,
) -> ChapterPlanResult:
    """生成独立两阶段候选ChapterPlan，不覆盖正式boundaries/llm.json。"""

    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterPlanError(str(exc)) from exc
    if experiment_id is not None:
        validate_experiment_id(experiment_id)
    resolved_minimum = (
        STABILITY_MIN_CHAPTER_SECONDS
        if min_chapter_seconds is None and experiment_id is not None
        else DEFAULT_MIN_CHAPTER_SECONDS
        if min_chapter_seconds is None
        else min_chapter_seconds
    )
    minimum = _validated_positive_seconds(resolved_minimum, "最小章节时长")
    radius = _validated_positive_seconds(window_seconds, "边界定位窗口半径")
    artifact_subdir: str | Path = "llm"
    cache_namespace: str | None = None
    if experiment_id is not None:
        artifact_subdir = experiment_artifact_subdir(
            "two-stage", experiment_id
        )
        cache_namespace = experiment_id

    def validate_outline(response: dict[str, Any]) -> dict[str, Any]:
        try:
            return normalize_outline_response(response, timeline)
        except ChapterPlanError as exc:
            raise LLMRetryableError(f"LLM粗略章节结构无效：{exc}") from exc

    try:
        outline_result = call_task_llm(
            output_root,
            task_id,
            call_id=OUTLINE_CALL_ID,
            messages=build_outline_messages(
                timeline,
                min_chapter_seconds=minimum,
                stability_experiment=experiment_id is not None,
            ),
            response_format={"type": "json_object"},
            config=config,
            transport=transport,
            dotenv_path=dotenv_path,
            response_validator=validate_outline,
            validation_id=OUTLINE_VALIDATION_ID,
            artifact_subdir=artifact_subdir,
            cache_namespace=cache_namespace,
            save_validation_audit=save_validation_audit,
        )
        windows = build_alignment_windows(
            outline_result.response,
            timeline,
            window_seconds=radius,
        )

        def validate_alignment(response: dict[str, Any]) -> dict[str, Any]:
            try:
                return normalize_alignment_response(
                    response,
                    outline_result.response,
                    windows,
                    timeline,
                    min_chapter_seconds=minimum,
                )
            except ChapterPlanError as exc:
                raise LLMRetryableError(f"LLM边界定位结构无效：{exc}") from exc

        alignment_result = call_task_llm(
            output_root,
            task_id,
            call_id=ALIGNMENT_CALL_ID,
            messages=build_alignment_messages(
                windows,
                stability_experiment=experiment_id is not None,
            ),
            response_format={"type": "json_object"},
            config=config,
            transport=transport,
            dotenv_path=dotenv_path,
            response_validator=validate_alignment,
            validation_id=ALIGNMENT_VALIDATION_ID,
            artifact_subdir=artifact_subdir,
            cache_namespace=cache_namespace,
            save_validation_audit=save_validation_audit,
        )
    except LLMAdapterError as exc:
        raise ChapterPlanError(str(exc)) from exc

    plan = _build_candidate_plan(
        task_id,
        timeline,
        alignment_result.response,
        min_chapter_seconds=minimum,
        window_seconds=radius,
        prompt_version=(
            STABILITY_TWO_STAGE_PROMPT_VERSION
            if experiment_id is not None
            else TWO_STAGE_PROMPT_VERSION
        ),
        experiment_id=experiment_id,
    )
    validate_chapter_plan(plan, timeline=timeline, expected_task_id=task_id)
    task_root = Path(output_root).expanduser() / task_id
    if experiment_id is None:
        plan_path = task_root / "boundaries" / "llm-two-stage.json"
    else:
        plan_path = (
            task_root
            / experiment_artifact_subdir("two-stage", experiment_id)
            / "chapter-plan.json"
        )
    temporary_path = plan_path.with_name(f"{plan_path.name}.tmp")
    plan_cache_hit = _cached_candidate_matches(plan_path, plan, timeline, task_id)
    if not plan_cache_hit:
        try:
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            _remove_temporary_file(temporary_path)
            temporary_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(plan_path)
        except OSError as exc:
            raise ChapterPlanError(f"无法保存两阶段候选ChapterPlan：{plan_path}") from exc
        finally:
            _remove_temporary_file(temporary_path)

    return ChapterPlanResult(
        plan_path=plan_path,
        boundary_count=len(plan["boundaries"]),
        duration_seconds=float(plan["duration_seconds"]),
        min_chapter_seconds=minimum,
        attempt_count=outline_result.attempt_count + alignment_result.attempt_count,
        llm_cache_hit=outline_result.cache_hit and alignment_result.cache_hit,
        plan_cache_hit=plan_cache_hit,
    )
