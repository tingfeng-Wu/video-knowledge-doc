"""使用完整时间轴直接生成统一ChapterPlan。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_doc.asr_timeline import (
    ASRTimelineError,
    load_task_timeline,
    validate_timeline_payload,
)
from video_doc.chapter_policy import (
    STABILITY_MIN_CHAPTER_SECONDS,
    STABILITY_POLICY_VERSION,
    render_stability_policy,
)
from video_doc.llm_adapter import (
    LLMAdapterError,
    LLMCallResult,
    LLMConfig,
    LLMRetryableError,
    LLMTransport,
    call_task_llm,
)


CHAPTER_PLAN_SCHEMA_VERSION = "1.0"
DEFAULT_MIN_CHAPTER_SECONDS = 30.0
DEFAULT_LLM_CALL_ID = "stage8-llm-chapters"
CHAPTER_PROMPT_VERSION = "chapter-boundary-prompt-1.1"
STABILITY_DIRECT_PROMPT_VERSION = "chapter-boundary-stability-1.0"
STABILITY_DIRECT_EVENT_ID_PROMPT_VERSION = "chapter-boundary-event-id-stability-2.0"
STABILITY_DIRECT_EXPERIMENT_VERSION = "event-id-v2"
LLM_VALIDATION_ID = "chapter-plan-v2-raw"
STABILITY_EVENT_ID_VALIDATION_ID = "chapter-plan-event-id-v3-raw"


class ChapterPlanError(RuntimeError):
    """章节计划输入、LLM响应、校验或输出失败。"""


@dataclass(frozen=True)
class ChapterPlanResult:
    """一次任务级LLM分章的关键结果。"""

    plan_path: Path
    boundary_count: int
    duration_seconds: float
    min_chapter_seconds: float
    attempt_count: int
    llm_cache_hit: bool
    plan_cache_hit: bool


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ChapterPlanError(f"{field}必须是有效数字。") from exc
    if not math.isfinite(number):
        raise ChapterPlanError(f"{field}必须是有限数字。")
    return number


def _validated_min_chapter_seconds(value: Any) -> float:
    seconds = _finite_number(value, "最小章节时长")
    if seconds <= 0:
        raise ChapterPlanError("最小章节时长必须大于0。")
    return round(seconds, 3)


def validate_experiment_id(experiment_id: str) -> None:
    """实验ID同时作为安全目录名和缓存命名空间。"""

    if not isinstance(experiment_id, str):
        raise ChapterPlanError("experiment_id必须是字符串。")
    if not experiment_id or len(experiment_id) > 64:
        raise ChapterPlanError("experiment_id长度必须为1到64。")
    if not experiment_id[0].isalnum() or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in experiment_id
    ):
        raise ChapterPlanError(
            "experiment_id只能包含英文字母、数字、下划线和连字符，且必须以字母或数字开头。"
        )


def experiment_artifact_subdir(
    scheme: str,
    experiment_id: str,
    *,
    experiment_version: str | None = None,
) -> Path:
    validate_experiment_id(experiment_id)
    if scheme not in {"direct", "two-stage"}:
        raise ChapterPlanError("实验分章方案标识无效。")
    path = Path("experiments") / "stage8" / scheme
    if experiment_version is not None:
        validate_experiment_id(experiment_version)
        path /= experiment_version
    return path / experiment_id


def _timeline_sha256(timeline: dict[str, Any]) -> str:
    encoded = json.dumps(
        timeline,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _format_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _render_prompt_timeline(timeline: dict[str, Any]) -> str:
    lines: list[str] = []
    for event in timeline["events"]:
        pause = float(event["pause_before_seconds"])
        pause_text = f" pause_before={pause:.3f}s" if pause > 0 else ""
        lines.append(
            f"[{event['id']}] {_format_timestamp(float(event['start']))}-"
            f"{_format_timestamp(float(event['end']))}{pause_text} {event['text']}"
        )
    return "\n".join(lines)


def build_chapter_messages(
    timeline: dict[str, Any],
    *,
    min_chapter_seconds: float,
    stability_experiment: bool = False,
) -> list[dict[str, str]]:
    """构造只负责章节边界与初始标题的稳定提示词。"""

    try:
        validate_timeline_payload(timeline)
    except ASRTimelineError as exc:
        raise ChapterPlanError(str(exc)) from exc
    minimum = _validated_min_chapter_seconds(min_chapter_seconds)
    duration = float(timeline["duration_seconds"])
    if minimum > duration:
        raise ChapterPlanError("最小章节时长不能超过视频总时长。")
    language = str(timeline["language"]["code"])
    system = (
        "你负责给知识型视频划分章节。只依据给出的完整ASR时间轴判断主题变化，"
        "只输出一个JSON对象，不要输出解释、Markdown或章节摘要。"
    )
    prompt_version = (
        STABILITY_DIRECT_PROMPT_VERSION
        if stability_experiment
        else CHAPTER_PROMPT_VERSION
    )
    policy = (
        render_stability_policy(duration) + "\n" if stability_experiment else ""
    )
    if stability_experiment:
        prompt_version = STABILITY_DIRECT_EVENT_ID_PROMPT_VERSION
        first_event_id = str(timeline["events"][0]["id"])
        user = (
            f"提示词版本：{prompt_version}。\n"
            f"{policy}"
            f"视频时长为{duration:.3f}秒，ASR语言为{language}。\n"
            "boundaries只表示按叙事顺序排列的章节起点。每条边界必须同时返回简短标题和"
            "输入中真实存在的source_event_id，不得自行估算或输出timestamp。"
            f"第一章必须选择首个ASR事件{first_event_id}；后续事件ID必须按时间轴顺序严格"
            f"递增、不得重复，并保证每章（包括末章）至少{minimum:.3f}秒。"
            "标题与事件ID必须保持一一对应；不得为了修复顺序而交换、排序或移动标题。\n"
            "返回格式必须是："
            '{"boundaries":[{"source_event_id":"a0001","title":"..."},'
            '{"source_event_id":"a0042","title":"..."}]}。\n'
            "完整ASR时间轴如下：\n"
            f"{_render_prompt_timeline(timeline)}"
        )
    else:
        user = (
            f"提示词版本：{prompt_version}。\n"
            f"视频时长为{duration:.3f}秒，ASR语言为{language}。\n"
            f"boundaries只表示章节起点，不表示章节终点。每个timestamp必须使用十进制秒，"
            "不得使用HH:MM:SS时间格式。第一条边界必须是0.0秒；后续时间必须严格递增，"
            f"不得输出等于视频总时长的结束边界，且最后一条起点不得晚于"
            f"{duration - minimum:.3f}秒，以保证最后一章也至少{minimum:.3f}秒。"
            "后续边界尽量使用某条ASR事件的start。标题应简洁并与ASR语言一致。\n"
            "返回格式必须是："
            '{"boundaries":[{"timestamp":0.0,"title":"..."},'
            '{"timestamp":123.4,"title":"..."}]}。\n'
            "完整ASR时间轴如下：\n"
            f"{_render_prompt_timeline(timeline)}"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def normalize_chapter_response(
    response: Any,
    timeline: dict[str, Any],
    *,
    min_chapter_seconds: float = DEFAULT_MIN_CHAPTER_SECONDS,
) -> dict[str, Any]:
    """校验LLM业务响应，并将非零边界吸附到最近ASR起点。"""

    try:
        validate_timeline_payload(timeline)
    except ASRTimelineError as exc:
        raise ChapterPlanError(str(exc)) from exc
    minimum = _validated_min_chapter_seconds(min_chapter_seconds)
    duration = float(timeline["duration_seconds"])
    if minimum > duration:
        raise ChapterPlanError("最小章节时长不能超过视频总时长。")
    if not isinstance(response, dict):
        raise ChapterPlanError("LLM分章响应必须是JSON对象。")
    raw_boundaries = response.get("boundaries")
    if not isinstance(raw_boundaries, list) or not raw_boundaries:
        raise ChapterPlanError("LLM分章响应必须包含非空boundaries数组。")

    event_starts = [
        (float(event["start"]), str(event["id"])) for event in timeline["events"]
    ]
    normalized: list[dict[str, Any]] = []
    previous_raw = -1.0
    previous_timestamp = -1.0
    for index, raw_boundary in enumerate(raw_boundaries, start=1):
        if not isinstance(raw_boundary, dict):
            raise ChapterPlanError(f"第{index}条章节边界必须是JSON对象。")
        raw_timestamp = _finite_number(
            raw_boundary.get("timestamp"),
            f"第{index}条章节边界时间",
        )
        title = raw_boundary.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ChapterPlanError(f"第{index}条章节标题不能为空。")
        if raw_timestamp <= previous_raw:
            raise ChapterPlanError("LLM返回的章节边界时间必须严格递增。")
        if index == 1:
            if abs(raw_timestamp) > 0.001:
                raise ChapterPlanError("第一条章节边界必须从0.0秒开始。")
            timestamp = 0.0
            source_event_id: str | None = None
        else:
            if raw_timestamp <= 0 or raw_timestamp >= duration:
                raise ChapterPlanError(
                    f"第{index}条章节边界必须位于0到{duration:.3f}秒之间。"
                )
            timestamp, source_event_id = min(
                event_starts,
                key=lambda item: (abs(item[0] - raw_timestamp), item[0]),
            )
            timestamp = round(timestamp, 3)
        if timestamp <= previous_timestamp:
            raise ChapterPlanError("吸附后的章节边界时间必须严格递增。")
        normalized.append(
            {
                "timestamp": timestamp,
                "title": title.strip(),
                "source_event_id": source_event_id,
            }
        )
        previous_raw = raw_timestamp
        previous_timestamp = timestamp

    timestamps = [float(boundary["timestamp"]) for boundary in normalized]
    chapter_ends = timestamps[1:] + [duration]
    for index, (start, end) in enumerate(zip(timestamps, chapter_ends), start=1):
        if end - start + 1e-9 < minimum:
            raise ChapterPlanError(
                f"第{index}章时长{end - start:.3f}秒，小于最小值{minimum:.3f}秒。"
            )
    return {"boundaries": normalized}


def normalize_event_id_chapter_response(
    response: Any,
    timeline: dict[str, Any],
    *,
    min_chapter_seconds: float = STABILITY_MIN_CHAPTER_SECONDS,
) -> dict[str, Any]:
    """校验稳定性方案A返回的有序事件ID，不排序、删除或合并。"""

    try:
        validate_timeline_payload(timeline)
    except ASRTimelineError as exc:
        raise ChapterPlanError(str(exc)) from exc
    minimum = _validated_min_chapter_seconds(min_chapter_seconds)
    duration = float(timeline["duration_seconds"])
    if minimum > duration:
        raise ChapterPlanError("最小章节时长不能超过视频总时长。")
    if not isinstance(response, dict):
        raise ChapterPlanError("LLM分章响应必须是JSON对象。")
    raw_boundaries = response.get("boundaries")
    if not isinstance(raw_boundaries, list) or not raw_boundaries:
        raise ChapterPlanError("LLM分章响应必须包含非空boundaries数组。")

    events = timeline["events"]
    event_positions = {str(event["id"]): index for index, event in enumerate(events)}
    event_starts = {
        str(event["id"]): round(float(event["start"]), 3) for event in events
    }
    first_event_id = str(events[0]["id"])
    normalized: list[dict[str, Any]] = []
    previous_position = -1
    previous_event_id: str | None = None
    previous_title: str | None = None
    for index, raw_boundary in enumerate(raw_boundaries, start=1):
        if not isinstance(raw_boundary, dict):
            raise ChapterPlanError(f"第{index}条章节边界必须是JSON对象。")
        title = raw_boundary.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ChapterPlanError(f"第{index}条章节标题不能为空。")
        clean_title = title.strip()
        event_id = raw_boundary.get("source_event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ChapterPlanError(
                f"第{index}条章节“{clean_title}”缺少source_event_id。"
            )
        event_id = event_id.strip()
        if event_id not in event_positions:
            raise ChapterPlanError(
                f"第{index}条章节“{clean_title}”选择的ASR事件ID“{event_id}”不存在。"
            )
        if index == 1 and event_id != first_event_id:
            raise ChapterPlanError(
                f"第1条章节“{clean_title}”必须选择首个ASR事件ID“{first_event_id}”，"
                f"当前为“{event_id}”。"
            )
        position = event_positions[event_id]
        if index > 1 and position == previous_position:
            raise ChapterPlanError(
                f"第{index}条章节“{clean_title}”与第{index - 1}条章节"
                f"“{previous_title}”重复选择ASR事件ID“{event_id}”；保持标题和章节顺序"
                f"不变，为第{index}条重新选择更晚的事件ID。"
            )
        if index > 1 and position < previous_position:
            raise ChapterPlanError(
                f"第{index}条章节“{clean_title}”选择“{event_id}”，位于第{index - 1}条"
                f"章节“{previous_title}”选择的“{previous_event_id}”之前；保持标题和章节"
                f"顺序不变，为第{index}条重新选择更晚的事件ID。"
            )
        normalized.append(
            {
                "timestamp": 0.0 if index == 1 else event_starts[event_id],
                "title": clean_title,
                "source_event_id": None if index == 1 else event_id,
            }
        )
        previous_position = position
        previous_event_id = event_id
        previous_title = clean_title

    timestamps = [float(boundary["timestamp"]) for boundary in normalized]
    for index, (start, end) in enumerate(
        zip(timestamps, timestamps[1:] + [duration]), start=1
    ):
        if end - start + 1e-9 < minimum:
            boundary = raw_boundaries[index - 1]
            raise ChapterPlanError(
                f"第{index}章“{str(boundary.get('title', '')).strip()}”从事件ID"
                f"“{boundary.get('source_event_id')}”开始，时长{end - start:.3f}秒，"
                f"小于最小值{minimum:.3f}秒；保持章节顺序并重新选择相邻边界事件ID。"
            )
    return {"boundaries": normalized}


def _build_chapter_plan(
    task_id: str,
    timeline: dict[str, Any],
    normalized_response: dict[str, Any],
    *,
    min_chapter_seconds: float,
    prompt_version: str = CHAPTER_PROMPT_VERSION,
    experiment_id: str | None = None,
    boundary_alignment: str = "nearest_asr_start",
    experiment_version: str | None = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "prompt_version": prompt_version,
        "min_chapter_seconds": _validated_min_chapter_seconds(
            min_chapter_seconds
        ),
        "boundary_alignment": boundary_alignment,
    }
    if experiment_id is not None:
        parameters["experiment_id"] = experiment_id
        parameters["chapter_policy_version"] = STABILITY_POLICY_VERSION
        if experiment_version is not None:
            parameters["experiment_version"] = experiment_version
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
        "boundaries": normalized_response["boundaries"],
    }


def validate_chapter_plan(
    payload: Any,
    *,
    timeline: dict[str, Any] | None = None,
    expected_task_id: str | None = None,
) -> None:
    """校验统一ChapterPlan；提供时间轴时同时校验来源和ASR吸附。"""

    if not isinstance(payload, dict):
        raise ChapterPlanError("ChapterPlan必须是JSON对象。")
    if payload.get("schema_version") != CHAPTER_PLAN_SCHEMA_VERSION:
        raise ChapterPlanError("ChapterPlan Schema版本无效。")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ChapterPlanError("ChapterPlan任务ID无效。")
    if expected_task_id is not None and task_id != expected_task_id:
        raise ChapterPlanError("ChapterPlan任务ID与当前任务不一致。")
    if payload.get("strategy") != "llm":
        raise ChapterPlanError("当前ChapterPlan策略必须是llm。")
    duration = _finite_number(payload.get("duration_seconds"), "ChapterPlan视频时长")
    if duration <= 0:
        raise ChapterPlanError("ChapterPlan视频时长必须大于0。")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ChapterPlanError("ChapterPlan缺少参数信息。")
    prompt_version = parameters.get("prompt_version")
    if not isinstance(prompt_version, str) or not prompt_version.strip():
        raise ChapterPlanError("ChapterPlan提示词版本无效。")
    minimum = _validated_min_chapter_seconds(parameters.get("min_chapter_seconds"))
    if parameters.get("boundary_alignment") not in {
        "nearest_asr_start",
        "selected_asr_event_start",
    }:
        raise ChapterPlanError("ChapterPlan边界吸附方式无效。")
    source = payload.get("source_timeline")
    if not isinstance(source, dict):
        raise ChapterPlanError("ChapterPlan缺少时间轴来源信息。")
    sha256 = source.get("sha256")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ChapterPlanError("ChapterPlan时间轴来源哈希无效。")
    if not isinstance(source.get("event_count"), int) or source["event_count"] <= 0:
        raise ChapterPlanError("ChapterPlan时间轴事件数无效。")

    boundaries = payload.get("boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        raise ChapterPlanError("ChapterPlan必须包含至少一条边界。")
    previous = -1.0
    timestamps: list[float] = []
    for index, boundary in enumerate(boundaries, start=1):
        if not isinstance(boundary, dict):
            raise ChapterPlanError(f"ChapterPlan第{index}条边界无效。")
        timestamp = _finite_number(boundary.get("timestamp"), f"第{index}条边界时间")
        title = boundary.get("title")
        source_event_id = boundary.get("source_event_id")
        if not isinstance(title, str) or not title.strip():
            raise ChapterPlanError(f"ChapterPlan第{index}条标题为空。")
        if index == 1:
            if timestamp != 0.0 or source_event_id is not None:
                raise ChapterPlanError("ChapterPlan第一条边界必须是0.0秒。")
        elif timestamp <= 0 or timestamp >= duration:
            raise ChapterPlanError(f"ChapterPlan第{index}条边界越界。")
        if timestamp <= previous:
            raise ChapterPlanError("ChapterPlan边界必须严格递增。")
        if index > 1 and (not isinstance(source_event_id, str) or not source_event_id):
            raise ChapterPlanError(f"ChapterPlan第{index}条边界缺少ASR事件ID。")
        timestamps.append(timestamp)
        previous = timestamp
    for index, (start, end) in enumerate(
        zip(timestamps, timestamps[1:] + [duration]), start=1
    ):
        if end - start + 1e-9 < minimum:
            raise ChapterPlanError(f"ChapterPlan第{index}章小于最小时长。")

    if timeline is not None:
        try:
            validate_timeline_payload(timeline, expected_task_id=task_id)
        except ASRTimelineError as exc:
            raise ChapterPlanError(str(exc)) from exc
        if round(float(timeline["duration_seconds"]), 3) != round(duration, 3):
            raise ChapterPlanError("ChapterPlan视频时长与时间轴不一致。")
        if source.get("schema_version") != timeline["schema_version"]:
            raise ChapterPlanError("ChapterPlan时间轴Schema来源不一致。")
        if source.get("sha256") != _timeline_sha256(timeline):
            raise ChapterPlanError("ChapterPlan时间轴来源哈希不一致。")
        if source.get("event_count") != len(timeline["events"]):
            raise ChapterPlanError("ChapterPlan时间轴事件数不一致。")
        starts_by_id = {
            str(event["id"]): round(float(event["start"]), 3)
            for event in timeline["events"]
        }
        for index, boundary in enumerate(boundaries[1:], start=2):
            event_id = boundary["source_event_id"]
            if event_id not in starts_by_id:
                raise ChapterPlanError(f"ChapterPlan第{index}条ASR事件ID不存在。")
            if round(float(boundary["timestamp"]), 3) != starts_by_id[event_id]:
                raise ChapterPlanError(f"ChapterPlan第{index}条边界未对齐ASR起点。")


class LLMStrategy:
    """读取完整时间轴并通过结构化LLM直接预测ChapterPlan。"""

    def __init__(
        self,
        output_root: str | Path,
        task_id: str,
        *,
        min_chapter_seconds: float = DEFAULT_MIN_CHAPTER_SECONDS,
        config: LLMConfig | None = None,
        transport: LLMTransport | None = None,
        dotenv_path: str | Path | None = None,
        experiment_id: str | None = None,
    ) -> None:
        self.output_root = Path(output_root).expanduser()
        self.task_id = task_id
        self.min_chapter_seconds = _validated_min_chapter_seconds(
            min_chapter_seconds
        )
        self.config = config
        self.transport = transport
        self.dotenv_path = dotenv_path
        if experiment_id is not None:
            validate_experiment_id(experiment_id)
        self.experiment_id = experiment_id
        self.last_call_result: LLMCallResult | None = None

    def predict(
        self,
        timeline: dict[str, Any],
        candidates: Any = None,
        context: Any = None,
    ) -> dict[str, Any]:
        del candidates, context
        messages = build_chapter_messages(
            timeline,
            min_chapter_seconds=self.min_chapter_seconds,
            stability_experiment=self.experiment_id is not None,
        )

        def validate_response(response: dict[str, Any]) -> dict[str, Any]:
            try:
                if self.experiment_id is not None:
                    return normalize_event_id_chapter_response(
                        response,
                        timeline,
                        min_chapter_seconds=self.min_chapter_seconds,
                    )
                return normalize_chapter_response(
                    response, timeline, min_chapter_seconds=self.min_chapter_seconds
                )
            except ChapterPlanError as exc:
                raise LLMRetryableError(f"LLM章节结构无效：{exc}") from exc

        artifact_subdir: str | Path = "llm"
        cache_namespace: str | None = None
        if self.experiment_id is not None:
            artifact_subdir = experiment_artifact_subdir(
                "direct",
                self.experiment_id,
                experiment_version=STABILITY_DIRECT_EXPERIMENT_VERSION,
            )
            cache_namespace = (
                f"{STABILITY_DIRECT_EXPERIMENT_VERSION}-{self.experiment_id}"
            )
        try:
            call_result = call_task_llm(
                self.output_root,
                self.task_id,
                call_id=DEFAULT_LLM_CALL_ID,
                messages=messages,
                response_format={"type": "json_object"},
                config=self.config,
                transport=self.transport,
                dotenv_path=self.dotenv_path,
                response_validator=validate_response,
                validation_id=(
                    STABILITY_EVENT_ID_VALIDATION_ID
                    if self.experiment_id is not None
                    else LLM_VALIDATION_ID
                ),
                artifact_subdir=artifact_subdir,
                cache_namespace=cache_namespace,
                save_validation_audit=self.experiment_id is not None,
            )
        except LLMAdapterError as exc:
            raise ChapterPlanError(str(exc)) from exc
        self.last_call_result = call_result
        plan = _build_chapter_plan(
            self.task_id,
            timeline,
            call_result.response,
            min_chapter_seconds=self.min_chapter_seconds,
            prompt_version=(
                STABILITY_DIRECT_EVENT_ID_PROMPT_VERSION
                if self.experiment_id is not None
                else CHAPTER_PROMPT_VERSION
            ),
            experiment_id=self.experiment_id,
            boundary_alignment=(
                "selected_asr_event_start"
                if self.experiment_id is not None
                else "nearest_asr_start"
            ),
            experiment_version=(
                STABILITY_DIRECT_EXPERIMENT_VERSION
                if self.experiment_id is not None
                else None
            ),
        )
        validate_chapter_plan(
            plan,
            timeline=timeline,
            expected_task_id=self.task_id,
        )
        return plan


def _remove_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _cached_plan_matches(
    path: Path,
    expected: dict[str, Any],
    timeline: dict[str, Any],
    task_id: str,
) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_chapter_plan(
            payload,
            timeline=timeline,
            expected_task_id=task_id,
        )
    except (ChapterPlanError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == expected


def plan_task_chapters(
    output_root: str | Path,
    task_id: str,
    *,
    min_chapter_seconds: float | None = None,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
    experiment_id: str | None = None,
) -> ChapterPlanResult:
    """对单任务执行LLM直接分章并原子保存统一ChapterPlan。"""

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
    strategy = LLMStrategy(
        output_root,
        task_id,
        min_chapter_seconds=resolved_minimum,
        config=config,
        transport=transport,
        dotenv_path=dotenv_path,
        experiment_id=experiment_id,
    )
    plan = strategy.predict(timeline)
    call_result = strategy.last_call_result
    if call_result is None:
        raise ChapterPlanError("LLM分章未产生调用结果。")

    task_root = Path(output_root).expanduser() / task_id
    if experiment_id is None:
        plan_path = task_root / "boundaries" / "llm.json"
    else:
        plan_path = (
            task_root
            / experiment_artifact_subdir(
                "direct",
                experiment_id,
                experiment_version=STABILITY_DIRECT_EXPERIMENT_VERSION,
            )
            / "chapter-plan.json"
        )
    temporary_path = plan_path.with_name(f"{plan_path.name}.tmp")
    plan_cache_hit = _cached_plan_matches(plan_path, plan, timeline, task_id)
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
            raise ChapterPlanError(f"无法保存ChapterPlan：{plan_path}") from exc
        finally:
            _remove_temporary_file(temporary_path)

    return ChapterPlanResult(
        plan_path=plan_path,
        boundary_count=len(plan["boundaries"]),
        duration_seconds=float(plan["duration_seconds"]),
        min_chapter_seconds=float(plan["parameters"]["min_chapter_seconds"]),
        attempt_count=call_result.attempt_count,
        llm_cache_hit=call_result.cache_hit,
        plan_cache_hit=plan_cache_hit,
    )
