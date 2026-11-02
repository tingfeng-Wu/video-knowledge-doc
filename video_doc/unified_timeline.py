"""UnifiedTimeline 2.0契约校验与M1 Timeline 1.0单向只读投影（阶段13-0/13-1）。

本模块实现M2-0-P2冻结的逻辑Schema：

- `UnifiedTimeline 2.0`顶层契约、统一事件信封、`TranscriptEventPayload/1.0`、
  `PageChangeEventPayload/1.0`与`OcrEventPayload/1.0`；
- M1 `ASRTimeline 1.0`到Timeline 2.0的单向只读投影（`legacy_asr_projection`），
  映射为`aNNNN -> teNNNNNN`、`source_segment_id=legacy:aNNNN`、秒到整数毫秒，
  投影后执行双射复验（任务、时长、语言、事件数、顺序、时间、停顿、文本一一相等）。

边界（按《M2字幕功能暂缓决策》与P2 §2.1）：

- 当前版本只实现`legacy_asr_projection`转换器；`canonical_transcript`模式仅由
  校验器按P2完整实现，供未来字幕链启用，不伪造P1正式组（transcript_id、
  selection_id、canonical_sha256在legacy模式必须为null）；
- 不创建文本源选择器、不运行ASR、不扫描字幕；
- 视觉事件（page_change/ocr）由本模块校验器定义规则，由13-1
  `TimelineBuilderV2`构造并发布；本模块自身不写盘、不发布结果组、不触碰M1
  文件。物理manifest、缓存、current与CLI属于`timeline_builder_v2`。

实现决策（P2未逐字规定处，随G1审查包提交）：

1. `language.provenance`必须等于`primary_transcript.input_mode`；
2. 文本事件`source_kind`必须等于`primary_transcript.source_kind`（唯一主文本）；
3. modalities组合表中未列出的“恰好一个视觉模态available、另一个
   disabled/not_run/empty”按`success/multimodal`处理；
4. `transcript`为`available`时事件列表至少包含一个文本事件；非available时
   `run_status`必须为`failed`（P2 §七：不得发布正式Timeline 2.0）；
5. 规范JSON沿用P1 §3.2：UTF-8、键排序、紧凑分隔符、禁止NaN/Infinity；
6. P2 §3.1的`type优先级`冻结为`transcript(0) < page_change(1) < ocr(2)`，
   全体事件按`(start_ms, type优先级, id)`非降序排列；
7. 视觉事件使用独立ID前缀与类型内连续编号（`pe`/`oe`加6位），
   `source_segment_id`命名空间为`pc:<candidate_id>`（页面变化）与
   `oc:<frame_id>`（OCR），`legacy_event_id`固定null；新增视觉事件
   不改变任何既有文本事件ID（文本编号只统计transcript事件）；
8. 视觉payload冻结最小字段：page_change携带帧/候选身份与两项度量，
   ocr携带帧/候选身份、阅读顺序原文、内容哈希与行数；
9. modalities与事件数量一致性：`available`必须有该类型至少一个事件；
   `disabled/not_run/empty`必须没有该类型事件；`failed/unsupported`允许
   存在事件（对应“OCR部分成功仍发布degraded”的可观察形态）；
10. `boundary_eligible`与文本停顿只依据transcript事件序列计算，视觉事件
    不参与；瞬时视觉事件按`end_ms = start_ms + 1`建模（P2允许任意
    `start_ms < end_ms`，校验器不强制+1，由构造器保证）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from video_doc.asr_timeline import (
    ASRTimelineError,
    load_task_timeline,
    validate_timeline_payload,
)
from video_doc.task_workspace import TaskWorkspaceError, validate_task_id


UNIFIED_TIMELINE_SCHEMA_NAME = "UnifiedTimeline"
UNIFIED_TIMELINE_SCHEMA_VERSION = "2.0"
TRANSCRIPT_EVENT_PAYLOAD_SCHEMA = "TranscriptEventPayload/1.0"
PAGE_CHANGE_EVENT_PAYLOAD_SCHEMA = "PageChangeEventPayload/1.0"
OCR_EVENT_PAYLOAD_SCHEMA = "OcrEventPayload/1.0"
TRANSCRIPT_EVENT_TYPE = "transcript"
PAGE_CHANGE_EVENT_TYPE = "page_change"
OCR_EVENT_TYPE = "ocr"

# P2 §3.1“type优先级”冻结（实现决策6）：transcript < page_change < ocr。
EVENT_TYPE_PRIORITY: dict[str, int] = {
    TRANSCRIPT_EVENT_TYPE: 0,
    PAGE_CHANGE_EVENT_TYPE: 1,
    OCR_EVENT_TYPE: 2,
}
_EVENT_TYPES = frozenset(EVENT_TYPE_PRIORITY)
_EVENT_ID_PREFIX = {
    TRANSCRIPT_EVENT_TYPE: "te",
    PAGE_CHANGE_EVENT_TYPE: "pe",
    OCR_EVENT_TYPE: "oe",
}
_EVENT_PAYLOAD_SCHEMA = {
    TRANSCRIPT_EVENT_TYPE: TRANSCRIPT_EVENT_PAYLOAD_SCHEMA,
    PAGE_CHANGE_EVENT_TYPE: PAGE_CHANGE_EVENT_PAYLOAD_SCHEMA,
    OCR_EVENT_TYPE: OCR_EVENT_PAYLOAD_SCHEMA,
}
PAGE_CHANGE_SOURCE_KIND = "page_change"
OCR_SOURCE_KIND = "ocr"

INPUT_MODE_CANONICAL_TRANSCRIPT = "canonical_transcript"
INPUT_MODE_LEGACY_ASR_PROJECTION = "legacy_asr_projection"
INPUT_MODES = frozenset(
    {INPUT_MODE_CANONICAL_TRANSCRIPT, INPUT_MODE_LEGACY_ASR_PROJECTION}
)

TEXT_SOURCE_KINDS = frozenset(
    {"asr", "embedded_subtitle", "sidecar_subtitle", "platform_caption"}
)
_EVENT_SOURCE_KINDS: dict[str, frozenset[str]] = {
    TRANSCRIPT_EVENT_TYPE: TEXT_SOURCE_KINDS,
    PAGE_CHANGE_EVENT_TYPE: frozenset({PAGE_CHANGE_SOURCE_KIND}),
    OCR_EVENT_TYPE: frozenset({OCR_SOURCE_KIND}),
}

TRANSCRIPT_MODALITY = "transcript"
VISUAL_MODALITY_NAMES = ("frame_change", "ocr")
MODALITY_NAMES = (TRANSCRIPT_MODALITY,) + VISUAL_MODALITY_NAMES
MODALITY_EVENT_TYPES = {
    TRANSCRIPT_MODALITY: TRANSCRIPT_EVENT_TYPE,
    "frame_change": PAGE_CHANGE_EVENT_TYPE,
    "ocr": OCR_EVENT_TYPE,
}
MODALITY_STATUSES = frozenset(
    {"available", "disabled", "not_run", "empty", "failed", "unsupported"}
)
VISUAL_INACTIVE_STATUSES = frozenset({"disabled", "not_run", "empty"})
VISUAL_BROKEN_STATUSES = frozenset({"failed", "unsupported"})

RUN_STATUSES = frozenset({"success", "degraded", "failed"})
QUALITY_LEVELS = frozenset({"multimodal", "text_only"})

_TIMELINE_ID_PATTERN = re.compile(r"^unified-timeline-[0-9a-f]{12}$")
_LEGACY_EVENT_ID_PATTERN = re.compile(r"^a([0-9]{4})$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FRAME_ID_PATTERN = re.compile(r"^fr[0-9]{6}$")
_PC_CANDIDATE_ID_PATTERN = re.compile(r"^pc[0-9]{6}$")

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "timeline_id",
        "task_id",
        "duration_ms",
        "language",
        "primary_transcript",
        "modalities",
        "run_status",
        "quality_level",
        "events",
    }
)
_LANGUAGE_FIELDS = frozenset({"code", "confidence", "provenance"})
_PRIMARY_TRANSCRIPT_FIELDS = frozenset(
    {
        "input_mode",
        "source_kind",
        "transcript_id",
        "selection_id",
        "canonical_sha256",
        "legacy_timeline_sha256",
    }
)
_MODALITY_FIELDS = frozenset({"status", "reason_codes"})
_EVENT_FIELDS = frozenset(
    {
        "id",
        "type",
        "source_kind",
        "source_segment_id",
        "start_ms",
        "end_ms",
        "payload_schema",
        "payload",
        "legacy_event_id",
    }
)
_TRANSCRIPT_PAYLOAD_FIELDS = frozenset(
    {"text", "pause_before_ms", "boundary_eligible"}
)
_PAGE_CHANGE_PAYLOAD_FIELDS = frozenset(
    {
        "frame_id",
        "previous_frame_id",
        "candidate_id",
        "change_strength",
        "perceptual_distance",
    }
)
_OCR_PAYLOAD_FIELDS = frozenset(
    {"frame_id", "candidate_id", "text", "text_sha256", "line_count"}
)


class UnifiedTimelineError(RuntimeError):
    """UnifiedTimeline 2.0校验或M1只读投影失败。"""


def canonical_json_bytes(payload: Any) -> bytes:
    """P1 §3.2规范JSON：UTF-8、键排序、紧凑分隔符、禁止NaN/Infinity。"""

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise UnifiedTimelineError("规范JSON序列化失败：存在不可序列化的值。") from exc
    return encoded.encode("utf-8")


def canonical_json_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def compute_timeline_id(payload: dict[str, Any]) -> str:
    """由去掉自身ID后的规范JSON生成`unified-timeline-<sha256前12位>`。"""

    body = {key: value for key, value in payload.items() if key != "timeline_id"}
    return f"unified-timeline-{canonical_json_sha256(body)[:12]}"


def seconds_to_ms(value: Any, field: str) -> int:
    """把3位小数秒精确量化为整数毫秒。"""

    if isinstance(value, bool):
        raise UnifiedTimelineError(f"{field}必须是有限数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UnifiedTimelineError(f"{field}必须是有限数字。") from exc
    if not math.isfinite(number):
        raise UnifiedTimelineError(f"{field}必须是有限数字。")
    return int(round(number * 1000))


def _require_object(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnifiedTimelineError(f"{description}必须是JSON对象。")
    return value


def _require_exact_keys(
    mapping: Any,
    expected: frozenset[str],
    description: str,
) -> dict[str, Any]:
    payload = _require_object(mapping, description)
    missing = sorted(expected - payload.keys())
    extra = sorted(set(payload.keys()) - expected)
    if missing or extra:
        raise UnifiedTimelineError(
            f"{description}字段必须精确匹配：缺少{missing or '无'}，多余{extra or '无'}。"
        )
    return payload


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnifiedTimelineError(f"{field}必须是整数。")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise UnifiedTimelineError(f"{field}必须是64位小写十六进制SHA-256。")
    return value


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnifiedTimelineError(f"{field}必须是非空字符串。")
    return value


def _validate_language_block(
    language: Any,
    input_mode: str,
) -> None:
    block = _require_exact_keys(language, _LANGUAGE_FIELDS, "时间轴语言信息")
    _require_nonempty_str(block.get("code"), "语言代码")
    confidence = block.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool):
            raise UnifiedTimelineError("语言置信度必须是0到1之间的数字或null。")
        try:
            number = float(confidence)
        except (TypeError, ValueError) as exc:
            raise UnifiedTimelineError("语言置信度必须是0到1之间的数字或null。") from exc
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise UnifiedTimelineError("语言置信度必须是0到1之间的数字或null。")
    provenance = block.get("provenance")
    if provenance not in INPUT_MODES:
        raise UnifiedTimelineError("语言provenance必须是合法输入模式。")
    if provenance != input_mode:
        raise UnifiedTimelineError("语言provenance必须与primary_transcript.input_mode一致。")


def _validate_primary_transcript(primary: Any) -> tuple[str, str]:
    block = _require_exact_keys(
        primary,
        _PRIMARY_TRANSCRIPT_FIELDS,
        "primary_transcript",
    )
    input_mode = block.get("input_mode")
    if input_mode not in INPUT_MODES:
        raise UnifiedTimelineError("input_mode必须是canonical_transcript或legacy_asr_projection。")
    source_kind = block.get("source_kind")
    if source_kind not in TEXT_SOURCE_KINDS:
        raise UnifiedTimelineError("primary_transcript.source_kind不是合法文本来源类型。")
    if input_mode == INPUT_MODE_CANONICAL_TRANSCRIPT:
        _require_nonempty_str(block.get("transcript_id"), "transcript_id")
        _require_nonempty_str(block.get("selection_id"), "selection_id")
        _require_sha256(block.get("canonical_sha256"), "canonical_sha256")
        if block.get("legacy_timeline_sha256") is not None:
            raise UnifiedTimelineError(
                "canonical_transcript模式下legacy_timeline_sha256必须为null。"
            )
    else:
        for field in ("transcript_id", "selection_id", "canonical_sha256"):
            if block.get(field) is not None:
                raise UnifiedTimelineError(
                    f"legacy_asr_projection模式不得伪造P1正式组：{field}必须为null。"
                )
        _require_sha256(
            block.get("legacy_timeline_sha256"),
            "legacy_timeline_sha256",
        )
    return input_mode, source_kind


def _validate_modalities(modalities: Any) -> dict[str, str]:
    block = _require_exact_keys(
        modalities,
        frozenset(MODALITY_NAMES),
        "modalities",
    )
    statuses: dict[str, str] = {}
    for name in MODALITY_NAMES:
        entry = _require_exact_keys(
            block.get(name),
            _MODALITY_FIELDS,
            f"modalities.{name}",
        )
        status = entry.get("status")
        if status not in MODALITY_STATUSES:
            raise UnifiedTimelineError(f"modalities.{name}.status不是合法状态。")
        reason_codes = entry.get("reason_codes")
        if not isinstance(reason_codes, list) or not all(
            isinstance(code, str) and code.strip() for code in reason_codes
        ):
            raise UnifiedTimelineError(f"modalities.{name}.reason_codes必须是非空字符串列表。")
        if status == "failed" and not reason_codes:
            raise UnifiedTimelineError(f"modalities.{name}为failed时reason_codes不能为空。")
        statuses[name] = status
    return statuses


def _expected_run_outcome(statuses: dict[str, str]) -> tuple[str, str]:
    """按P2 §七状态表推导(run_status, quality_level)；含13-0实现决策3。

    调用方保证`transcript`为`available`；非available时P2要求run_status=failed，
    由`validate_unified_timeline`单独处理，不经过本函数。
    """

    visual = [statuses[name] for name in VISUAL_MODALITY_NAMES]
    if any(status in VISUAL_BROKEN_STATUSES for status in visual):
        return "degraded", "text_only"
    if all(status in VISUAL_INACTIVE_STATUSES for status in visual):
        return "success", "text_only"
    return "success", "multimodal"


def _require_unit_interval(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnifiedTimelineError(f"{field}必须是0到1之间的数字。")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise UnifiedTimelineError(f"{field}必须是0到1之间的数字。")
    return number


def _validate_transcript_event(
    block: dict[str, Any],
    *,
    text_index: int,
    duration_ms: int,
    input_mode: str,
    source_kind: str,
    prior_coverage_end_ms: int,
    seen_start_ms: set[int],
) -> tuple[int, int, bool]:
    """校验单个文本事件并返回(新覆盖终点, 新起点, 是否首次出现在该start_ms)。"""

    if block.get("id") != f"te{text_index:06d}":
        raise UnifiedTimelineError(
            f"文本事件ID必须是te加6位连续数字，位置{text_index}期望te{text_index:06d}。"
        )
    if block.get("source_kind") != source_kind:
        raise UnifiedTimelineError("文本事件source_kind必须与primary_transcript一致。")
    legacy_event_id = block.get("legacy_event_id")
    segment_id = block.get("source_segment_id")
    if input_mode == INPUT_MODE_LEGACY_ASR_PROJECTION:
        if (
            not isinstance(legacy_event_id, str)
            or not _LEGACY_EVENT_ID_PATTERN.fullmatch(legacy_event_id)
            or int(legacy_event_id[1:]) != text_index
        ):
            raise UnifiedTimelineError(
                f"legacy投影事件{text_index}的legacy_event_id必须是对应位置的aNNNN。"
            )
        if segment_id != f"legacy:{legacy_event_id}":
            raise UnifiedTimelineError(
                f"legacy投影事件{text_index}的source_segment_id必须是legacy:aNNNN。"
            )
    else:
        if legacy_event_id is not None:
            raise UnifiedTimelineError(
                "canonical_transcript路径的legacy_event_id固定为null。"
            )
        if segment_id != f"ct:t{text_index:06d}":
            raise UnifiedTimelineError(
                f"canonical文本事件{text_index}的source_segment_id必须是ct:tNNNNNN。"
            )
    start_ms = _require_int(block.get("start_ms"), f"文本事件{text_index}的start_ms")
    end_ms = _require_int(block.get("end_ms"), f"文本事件{text_index}的end_ms")
    if not 0 <= start_ms < end_ms <= duration_ms:
        raise UnifiedTimelineError(f"文本事件{text_index}时间范围无效。")
    payload = _require_exact_keys(
        block.get("payload"),
        _TRANSCRIPT_PAYLOAD_FIELDS,
        f"文本事件{text_index}的payload",
    )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or text != text.strip():
        raise UnifiedTimelineError(f"文本事件{text_index}文本必须非空且无首尾空白。")
    pause_ms = _require_int(
        payload.get("pause_before_ms"),
        f"文本事件{text_index}的pause_before_ms",
    )
    expected_pause_ms = max(0, start_ms - prior_coverage_end_ms)
    if pause_ms < 0 or pause_ms != expected_pause_ms:
        raise UnifiedTimelineError(
            f"文本事件{text_index}停顿必须等于max(0, start_ms - 此前覆盖终点)。"
        )
    eligible = payload.get("boundary_eligible")
    if not isinstance(eligible, bool):
        raise UnifiedTimelineError(f"文本事件{text_index}的boundary_eligible必须是布尔值。")
    first_at_start = start_ms not in seen_start_ms
    if eligible != first_at_start:
        raise UnifiedTimelineError(
            f"文本事件{text_index}的boundary_eligible必须只对同一start_ms的第一条为true。"
        )
    return max(prior_coverage_end_ms, end_ms), start_ms, first_at_start


def _validate_page_change_event(block: dict[str, Any], *, ordinal: int, duration_ms: int) -> None:
    if block.get("id") != f"pe{ordinal:06d}":
        raise UnifiedTimelineError(
            f"页面变化事件ID必须是pe加6位连续数字，位置{ordinal}期望pe{ordinal:06d}。"
        )
    if block.get("source_kind") != PAGE_CHANGE_SOURCE_KIND:
        raise UnifiedTimelineError("页面变化事件source_kind必须是page_change。")
    if block.get("legacy_event_id") is not None:
        raise UnifiedTimelineError("页面变化事件legacy_event_id固定为null。")
    payload = _require_exact_keys(
        block.get("payload"),
        _PAGE_CHANGE_PAYLOAD_FIELDS,
        f"页面变化事件pe{ordinal:06d}的payload",
    )
    frame_id = payload.get("frame_id")
    candidate_id = payload.get("candidate_id")
    previous_frame_id = payload.get("previous_frame_id")
    if not isinstance(frame_id, str) or not _FRAME_ID_PATTERN.fullmatch(frame_id):
        raise UnifiedTimelineError(f"页面变化事件pe{ordinal:06d}的frame_id格式无效。")
    if not isinstance(previous_frame_id, str) or not _FRAME_ID_PATTERN.fullmatch(previous_frame_id):
        raise UnifiedTimelineError(f"页面变化事件pe{ordinal:06d}的previous_frame_id格式无效。")
    if not isinstance(candidate_id, str) or not _PC_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        raise UnifiedTimelineError(f"页面变化事件pe{ordinal:06d}的candidate_id格式无效。")
    if block.get("source_segment_id") != f"pc:{candidate_id}":
        raise UnifiedTimelineError(
            f"页面变化事件pe{ordinal:06d}的source_segment_id必须是pc:<candidate_id>。"
        )
    start_ms = _require_int(block.get("start_ms"), f"页面变化事件pe{ordinal:06d}的start_ms")
    end_ms = _require_int(block.get("end_ms"), f"页面变化事件pe{ordinal:06d}的end_ms")
    if not 0 <= start_ms < end_ms <= duration_ms:
        raise UnifiedTimelineError(f"页面变化事件pe{ordinal:06d}时间范围无效。")
    _require_unit_interval(payload.get("change_strength"), f"pe{ordinal:06d}的change_strength")
    _require_unit_interval(payload.get("perceptual_distance"), f"pe{ordinal:06d}的perceptual_distance")


def _validate_ocr_event(block: dict[str, Any], *, ordinal: int, duration_ms: int) -> None:
    if block.get("id") != f"oe{ordinal:06d}":
        raise UnifiedTimelineError(
            f"OCR事件ID必须是oe加6位连续数字，位置{ordinal}期望oe{ordinal:06d}。"
        )
    if block.get("source_kind") != OCR_SOURCE_KIND:
        raise UnifiedTimelineError("OCR事件source_kind必须是ocr。")
    if block.get("legacy_event_id") is not None:
        raise UnifiedTimelineError("OCR事件legacy_event_id固定为null。")
    payload = _require_exact_keys(
        block.get("payload"),
        _OCR_PAYLOAD_FIELDS,
        f"OCR事件oe{ordinal:06d}的payload",
    )
    frame_id = payload.get("frame_id")
    candidate_id = payload.get("candidate_id")
    if not isinstance(frame_id, str) or not _FRAME_ID_PATTERN.fullmatch(frame_id):
        raise UnifiedTimelineError(f"OCR事件oe{ordinal:06d}的frame_id格式无效。")
    if not isinstance(candidate_id, str) or not _PC_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        raise UnifiedTimelineError(f"OCR事件oe{ordinal:06d}的candidate_id格式无效。")
    if block.get("source_segment_id") != f"oc:{frame_id}":
        raise UnifiedTimelineError(
            f"OCR事件oe{ordinal:06d}的source_segment_id必须是oc:<frame_id>。"
        )
    start_ms = _require_int(block.get("start_ms"), f"OCR事件oe{ordinal:06d}的start_ms")
    end_ms = _require_int(block.get("end_ms"), f"OCR事件oe{ordinal:06d}的end_ms")
    if not 0 <= start_ms < end_ms <= duration_ms:
        raise UnifiedTimelineError(f"OCR事件oe{ordinal:06d}时间范围无效。")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or text != text.strip():
        raise UnifiedTimelineError(f"OCR事件oe{ordinal:06d}文本必须非空且无首尾空白。")
    _require_sha256(payload.get("text_sha256"), f"oe{ordinal:06d}的text_sha256")
    line_count = _require_int(payload.get("line_count"), f"oe{ordinal:06d}的line_count")
    if line_count < 1:
        raise UnifiedTimelineError(f"OCR事件oe{ordinal:06d}的line_count必须至少为1。")


def _validate_events(
    events: Any,
    *,
    duration_ms: int,
    input_mode: str,
    source_kind: str,
    transcript_available: bool,
) -> dict[str, int]:
    """校验全体事件信封、类型内编号、时间界、排序与逐类型payload。

    返回每种类型的事件数量，供调用方做modalities一致性检查。
    """

    if not isinstance(events, list):
        raise UnifiedTimelineError("events必须是列表。")
    if transcript_available and not events:
        raise UnifiedTimelineError("transcript为available时至少需要一个文本事件。")
    counts = {event_type: 0 for event_type in _EVENT_TYPES}
    prior_coverage_end_ms = 0
    previous_text_start_ms = 0
    seen_start_ms: set[int] = set()
    previous_sort_key: tuple[int, int, str] | None = None
    for position, event in enumerate(events, start=1):
        block = _require_exact_keys(event, _EVENT_FIELDS, f"事件{position}")
        event_type = block.get("type")
        if event_type not in _EVENT_TYPES:
            raise UnifiedTimelineError(f"事件{position}的type必须是已冻结的事件类型之一。")
        if block.get("source_kind") not in _EVENT_SOURCE_KINDS[event_type]:
            raise UnifiedTimelineError(f"事件{position}的source_kind与类型不匹配。")
        start_ms = _require_int(block.get("start_ms"), f"事件{position}的start_ms")
        end_ms = _require_int(block.get("end_ms"), f"事件{position}的end_ms")
        if not 0 <= start_ms < end_ms <= duration_ms:
            raise UnifiedTimelineError(f"事件{position}时间范围无效。")
        if block.get("payload_schema") != _EVENT_PAYLOAD_SCHEMA[event_type]:
            raise UnifiedTimelineError(f"事件{position}的payload_schema与类型不匹配。")
        event_id = block.get("id")
        if not isinstance(event_id, str) or not event_id.startswith(_EVENT_ID_PREFIX[event_type]):
            raise UnifiedTimelineError(f"事件{position}的ID前缀与类型不匹配。")
        sort_key = (start_ms, EVENT_TYPE_PRIORITY[event_type], event_id)
        if previous_sort_key is not None and previous_sort_key > sort_key:
            raise UnifiedTimelineError(
                f"事件{position}违反(start_ms, type优先级, id)全局排序。"
            )
        previous_sort_key = sort_key
        counts[event_type] += 1
        ordinal = counts[event_type]
        if event_type == TRANSCRIPT_EVENT_TYPE:
            if start_ms < previous_text_start_ms:
                raise UnifiedTimelineError(f"文本事件{ordinal}起点乱序。")
            (
                prior_coverage_end_ms,
                previous_text_start_ms,
                _first_at_start,
            ) = _validate_transcript_event(
                block,
                text_index=ordinal,
                duration_ms=duration_ms,
                input_mode=input_mode,
                source_kind=source_kind,
                prior_coverage_end_ms=prior_coverage_end_ms,
                seen_start_ms=seen_start_ms,
            )
            seen_start_ms.add(start_ms)
        elif event_type == PAGE_CHANGE_EVENT_TYPE:
            _validate_page_change_event(block, ordinal=ordinal, duration_ms=duration_ms)
        else:
            _validate_ocr_event(block, ordinal=ordinal, duration_ms=duration_ms)
    return counts


def validate_unified_timeline(
    payload: Any,
    *,
    expected_task_id: str | None = None,
) -> None:
    """严格校验`UnifiedTimeline 2.0`逻辑对象（P2 §二/§三/§七）。"""

    block = _require_exact_keys(payload, _TOP_LEVEL_FIELDS, "时间轴顶层")
    if block.get("schema_name") != UNIFIED_TIMELINE_SCHEMA_NAME:
        raise UnifiedTimelineError("schema_name必须是UnifiedTimeline。")
    if block.get("schema_version") != UNIFIED_TIMELINE_SCHEMA_VERSION:
        raise UnifiedTimelineError("schema_version必须是2.0。")
    timeline_id = block.get("timeline_id")
    if not isinstance(timeline_id, str) or not _TIMELINE_ID_PATTERN.fullmatch(timeline_id):
        raise UnifiedTimelineError("timeline_id格式无效。")
    if compute_timeline_id(block) != timeline_id:
        raise UnifiedTimelineError("timeline_id与规范内容不一致。")
    task_id = block.get("task_id")
    if not isinstance(task_id, str):
        raise UnifiedTimelineError("task_id必须是字符串。")
    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise UnifiedTimelineError(str(exc)) from exc
    if expected_task_id is not None and task_id != expected_task_id:
        raise UnifiedTimelineError("时间轴中的任务ID与当前任务不一致。")
    duration_ms = _require_int(block.get("duration_ms"), "duration_ms")
    if duration_ms <= 0:
        raise UnifiedTimelineError("duration_ms必须大于0。")

    input_mode, source_kind = _validate_primary_transcript(block.get("primary_transcript"))
    _validate_language_block(block.get("language"), input_mode)
    statuses = _validate_modalities(block.get("modalities"))

    run_status = block.get("run_status")
    quality_level = block.get("quality_level")
    if run_status not in RUN_STATUSES:
        raise UnifiedTimelineError("run_status不是合法状态。")
    if quality_level not in QUALITY_LEVELS:
        raise UnifiedTimelineError("quality_level不是合法等级。")
    if statuses[TRANSCRIPT_MODALITY] != "available":
        if run_status != "failed":
            raise UnifiedTimelineError("主文本不可用时run_status必须为failed。")
    else:
        expected_status, expected_quality = _expected_run_outcome(statuses)
        if (run_status, quality_level) != (expected_status, expected_quality):
            raise UnifiedTimelineError(
                "run_status/quality_level与modalities状态组合不符："
                f"期望{expected_status}/{expected_quality}。"
            )

    counts = _validate_events(
        block.get("events"),
        duration_ms=duration_ms,
        input_mode=input_mode,
        source_kind=source_kind,
        transcript_available=statuses[TRANSCRIPT_MODALITY] == "available",
    )
    # 实现决策9：modalities状态与事件数量的一致性。
    for modality_name, event_type in MODALITY_EVENT_TYPES.items():
        status = statuses[modality_name]
        event_count = counts[event_type]
        if status == "available" and event_count < 1:
            raise UnifiedTimelineError(
                f"modalities.{modality_name}为available时必须至少有一个"
                f"{event_type}事件。"
            )
        if status in VISUAL_INACTIVE_STATUSES and event_count != 0:
            raise UnifiedTimelineError(
                f"modalities.{modality_name}为{status}时不得包含{event_type}事件。"
            )


def derive_run_outcome(modalities: dict[str, Any]) -> tuple[str, str]:
    """由已构造的modalities块推导(run_status, quality_level)（13-1构造器使用）。

    仅在主文本available时适用；P2 §七规定主文本非available时不得发布正式
    Timeline 2.0，本函数对此直接拒绝。
    """

    statuses = _validate_modalities(modalities)
    if statuses[TRANSCRIPT_MODALITY] != "available":
        raise UnifiedTimelineError(
            "derive_run_outcome要求transcript为available；不可用时按P2不得发布正式时间轴。"
        )
    return _expected_run_outcome(statuses)


def _verify_legacy_bijection(
    source: dict[str, Any],
    unified: dict[str, Any],
) -> None:
    """P2 §4.1双射复验：任务、时长、语言、事件数、顺序、时间、停顿、文本一一相等。"""

    if unified["task_id"] != source["task_id"]:
        raise UnifiedTimelineError("投影任务ID不一致。")
    if unified["duration_ms"] != seconds_to_ms(source["duration_seconds"], "duration_seconds"):
        raise UnifiedTimelineError("投影时长不一致。")
    if unified["language"]["code"] != source["language"]["code"]:
        raise UnifiedTimelineError("投影语言代码不一致。")
    if unified["language"]["confidence"] != source["language"]["probability"]:
        raise UnifiedTimelineError("投影语言置信度不一致。")
    source_events = source["events"]
    unified_events = unified["events"]
    if len(unified_events) != len(source_events):
        raise UnifiedTimelineError("投影事件数不一致。")
    if unified["primary_transcript"]["legacy_timeline_sha256"] != canonical_json_sha256(source):
        raise UnifiedTimelineError("投影来源哈希不一致。")
    for index, (source_event, event) in enumerate(
        zip(source_events, unified_events), start=1
    ):
        if event["id"] != f"te{index:06d}":
            raise UnifiedTimelineError(f"事件{index}投影ID不一致。")
        if event["legacy_event_id"] != source_event["id"]:
            raise UnifiedTimelineError(f"事件{index}的legacy映射不一致。")
        if event["start_ms"] != seconds_to_ms(source_event["start"], f"事件{index}起点"):
            raise UnifiedTimelineError(f"事件{index}起点不一致。")
        if event["end_ms"] != seconds_to_ms(source_event["end"], f"事件{index}终点"):
            raise UnifiedTimelineError(f"事件{index}终点不一致。")
        if event["payload"]["pause_before_ms"] != seconds_to_ms(
            source_event["pause_before_seconds"], f"事件{index}停顿"
        ):
            raise UnifiedTimelineError(f"事件{index}停顿不一致。")
        if event["payload"]["text"] != source_event["text"]:
            raise UnifiedTimelineError(f"事件{index}文本不一致。")


def project_legacy_asr_timeline(timeline_payload: dict[str, Any]) -> dict[str, Any]:
    """把M1 `ASRTimeline 1.0`内存投影为`UnifiedTimeline 2.0`。

    只读操作：不修改输入、不写盘、不触碰M1文件；不合并、拆分或丢弃事件。
    """

    source = _require_object(timeline_payload, "M1时间轴")
    try:
        validate_timeline_payload(source)
    except ASRTimelineError as exc:
        raise UnifiedTimelineError(f"M1 Timeline 1.0校验失败：{exc}") from exc

    duration_ms = seconds_to_ms(source["duration_seconds"], "duration_seconds")
    if duration_ms <= 0:
        raise UnifiedTimelineError("M1时间轴时长必须大于0。")

    events: list[dict[str, Any]] = []
    prior_coverage_end_ms = 0
    seen_start_ms: set[int] = set()
    for index, source_event in enumerate(source["events"], start=1):
        start_ms = seconds_to_ms(source_event["start"], f"事件{index}起点")
        end_ms = seconds_to_ms(source_event["end"], f"事件{index}终点")
        pause_ms = seconds_to_ms(
            source_event["pause_before_seconds"], f"事件{index}停顿"
        )
        if not 0 <= start_ms < end_ms <= duration_ms:
            raise UnifiedTimelineError(f"事件{index}毫秒时间范围无效。")
        expected_pause_ms = max(0, start_ms - prior_coverage_end_ms)
        if pause_ms != expected_pause_ms:
            raise UnifiedTimelineError(
                f"事件{index}停顿在毫秒口径下不一致：源值{pause_ms}，复算{expected_pause_ms}。"
            )
        events.append(
            {
                "id": f"te{index:06d}",
                "type": TRANSCRIPT_EVENT_TYPE,
                "source_kind": "asr",
                "source_segment_id": f"legacy:{source_event['id']}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "payload_schema": TRANSCRIPT_EVENT_PAYLOAD_SCHEMA,
                "payload": {
                    "text": source_event["text"],
                    "pause_before_ms": pause_ms,
                    "boundary_eligible": start_ms not in seen_start_ms,
                },
                "legacy_event_id": source_event["id"],
            }
        )
        seen_start_ms.add(start_ms)
        prior_coverage_end_ms = max(prior_coverage_end_ms, end_ms)

    body: dict[str, Any] = {
        "schema_name": UNIFIED_TIMELINE_SCHEMA_NAME,
        "schema_version": UNIFIED_TIMELINE_SCHEMA_VERSION,
        "task_id": source["task_id"],
        "duration_ms": duration_ms,
        "language": {
            "code": source["language"]["code"],
            "confidence": source["language"]["probability"],
            "provenance": INPUT_MODE_LEGACY_ASR_PROJECTION,
        },
        "primary_transcript": {
            "input_mode": INPUT_MODE_LEGACY_ASR_PROJECTION,
            "source_kind": "asr",
            "transcript_id": None,
            "selection_id": None,
            "canonical_sha256": None,
            "legacy_timeline_sha256": canonical_json_sha256(source),
        },
        "modalities": {
            TRANSCRIPT_MODALITY: {"status": "available", "reason_codes": []},
            "frame_change": {"status": "not_run", "reason_codes": []},
            "ocr": {"status": "not_run", "reason_codes": []},
        },
        "run_status": "success",
        "quality_level": "text_only",
        "events": events,
    }
    unified = dict(body)
    unified["timeline_id"] = compute_timeline_id(body)

    _verify_legacy_bijection(source, unified)
    validate_unified_timeline(unified, expected_task_id=source["task_id"])
    return unified


def project_task_legacy_timeline(
    output_root: str | Path,
    task_id: str,
) -> dict[str, Any]:
    """只读加载任务M1时间轴并投影为Timeline 2.0；不写任何文件。"""

    try:
        legacy = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise UnifiedTimelineError(f"M1时间轴加载失败：{exc}") from exc
    return project_legacy_asr_timeline(legacy)
