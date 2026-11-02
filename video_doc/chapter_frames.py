"""阶段14-A：章内代表帧匹配与确定性说明（ChapterFrameSelection 1.0）。

契约唯一权威：``docs/development/阶段14A-章内代表帧与确定性说明裁决.md``
（§二Q1—Q6逐项裁决＋§三实现契约汇总）。本模块零模型调用、零网络、零外发、
零预算：不接model_roles、不读.env、不调用任何API（裁决Q5.5）。

发布/缓存/中断/审计工程逐字沿F1/F2/12/13-1/14-B先例，不新发明（裁决Q5.3）。

实现决定（编号供G1审查报告§三点一致性与§五.1—§五.7核对）：
1. 入口签名``select_chapter_frames(task_id, *, output_root=...)``按F1/F2/13-1/
   14-B全部先例的task_id位参形态落地；裁决Q5.1的``(output_root, task_id)``
   记作简写，语义（编排＋发布＋返回状态对象）不变。
2. 上游current复验失败沿用F2先例的域专属错误码（boundaries→
   INPUT_CHAPTER_PLAN_CORRUPT、timelines→INPUT_TIMELINE_CORRUPT、frames→
   INPUT_FRAME_SET_CORRUPT、page-changes→INPUT_PAGE_CHANGE_SET_CORRUPT、
   ocr→INPUT_OCR_SET_CORRUPT）；仅本域自身current用CURRENT_POINTER_CORRUPT。
   全部current类失败在类型化异常上携带结构化``domain``字段（裁决Q5.8
   "JSON摘要带domain结构化字段"）。
3. 裁决§3.4.2行文中``pe.source_event_id``/``oe.source_event_id``映射到实际
   产物字段：事件自身``id``（peNNNNNN/oeNNNNNN）＋``payload.candidate_id``/
   ``payload.frame_id``；交叉校验按裁决Q2.3列举的具体字段逐项执行
   （裁决§〇.3已确认pe/oe.start_ms恒等于对应set条目decoded_ms）。
4. 模态↔事件一致性不变量按13-1实际实现（``_ocr_modality_for``＋构建期
   852/858/860行）：``frame_change.status=="available"``⇔``pe``事件数>0；
   ``ocr.status=="available"``⇔``oe``事件数>0；``empty/disabled/not_run``⇒
   对应事件数=0；``failed``可带``oe``事件且照常消费（ocr-set degraded形态
   经13-1映射为模态``failed``，裁决E2"degraded如实记录"即记录该实际值）。
   违反⇒INPUT_IDENTITY_MISMATCH。
5. 条件消费（裁决Q2.4）：仅当timeline含``pe``事件才读取并绑定pc域、含
   ``oe``事件才读取并绑定ocr域；模态非available且无对应事件时即使该域
   current存在也不读取（source_*＝null，null照录进缓存身份）。
6. 缓存命中路径的组复验（含按当前上游重推导）失败一律映射CACHE_CORRUPT
   （裁决Q5.8"自身既有组/复验不一致"）；重推导中出现的上游矛盾同样以
   CACHE_CORRUPT呈现——已发布组与fresh输入不一致时不重写旧组。发布前
   临时组/正式组自校验失败为SELECTION_VALIDATION_FAILED；锁前纯构建路径
   的上游矛盾为INPUT_IDENTITY_MISMATCH。
7. ``result_id``＝选择对象（无自身ID字段）``canonical_json_bytes``的SHA-256
   前12位＋前缀``chapter-frame-selection-``（裁决Q1）；组文件恰两文件
   （chapter-frame-selection.json＋manifest.json，无execution-report）。
8. manifest无计数扩展字段：裁决§3.2对manifest字段已逐项列明（10个核心键），
   不照抄F1/F2/13-1/12各家自行追加的count扩展。
9. ``chapters[0].boundary_source_event_id``直接拷贝``plan.boundaries[0].
   source_event_id``——14-B校验器强制首条恒null，故与裁决§3.2"首章null"
   等价，无需特殊分支；其余章逐字拷贝plan对应值（emergency单章亦为null）。
10. 失败注入点沿14-B减去模型两点：``after_inputs``/``before_group_publish``/
    ``after_group_publish``/``before_current``。
11. 不设SOURCE_VIDEO_MISMATCH：本模块不读视频文件（裁决Q5.8显式差异，
    提交G1§五.4核对）。
12. 上游set文档的字段级校验在本模块内按各自冻结Schema独立实现（裁决Q2.1
    授权路径：page_change_detector/ocr_extractor无公开set loader）；只校验
    本模块消费与身份绑定所需字段＋精确键集，不重算帧图片哈希、不读图片
    （裁决Q2.6/Q3）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from video_doc.boundary_planner_v2 import (
    BoundaryPlannerV2Error,
    load_chapter_plan_current,
    load_published_chapter_plan,
)
from video_doc.frame_sampler import (
    FRAME_MODULE_TOKEN,
    FrameSamplerError,
    artifact_group_path,
    is_safe_result_id,
    load_frame_set_current,
)
from video_doc.ocr_extractor import (
    OCR_INDEX_FILE,
    OCR_ROOT_DIRECTORY,
)
from video_doc.page_change_detector import (
    PAGE_CHANGE_INDEX_FILE,
    PAGE_CHANGE_ROOT_DIRECTORY,
)
from video_doc.task_workspace import validate_task_id
from video_doc.timeline_builder_v2 import (
    TimelineBuilderV2Error,
    load_published_timeline,
    load_timeline_current,
)
from video_doc.unified_timeline import (
    OCR_EVENT_TYPE,
    PAGE_CHANGE_EVENT_TYPE,
    UnifiedTimelineError,
    canonical_json_bytes,
    canonical_json_sha256,
)

# ---------------------------------------------------------------------------
# 冻结身份字符串（裁决Q1/Q4/Q5，逐字）
# ---------------------------------------------------------------------------

CHAPTER_FRAMES_VERSION = "1.0"
SELECTION_SCHEMA_NAME = "ChapterFrameSelection"
SELECTION_SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "chapter_frame_selection"
MODULE_TOKEN = "chapter_frames"
ROOT_DIRECTORY = "chapter-frames"
GROUP_DIRECTORY = "formal-results"
CURRENT_FILE = "current.json"
LOCK_FILE = "chapter-frames.lock"
SELECTION_FILE = "chapter-frame-selection.json"
MANIFEST_FILE = "manifest.json"

SELECTOR_NAME = "representative-frame-selector"
SELECTOR_VERSION = "1.0"
SELECTION_POLICY_VERSION = "chapter-frame-policy-1.0"
CAPTION_TEMPLATE_VERSION = "deterministic-caption-zh-1.0"
MAX_FRAMES_PER_CHAPTER = 3
CAPTION_MAX_LINES = 3
CAPTION_MAX_LINE_CHARS = 40
CHAPTER_INTERVAL = "half_open"

MANIFEST_SCHEMA_NAME = "ArtifactGroupManifest"
MANIFEST_SCHEMA_VERSION = "1.0"
CURRENT_SCHEMA_NAME = "ArtifactCurrentPointer"
CURRENT_SCHEMA_VERSION = "1.0"
FAILURE_SCHEMA_NAME = "ModuleFailureAudit"
FAILURE_SCHEMA_VERSION = "1.0"
CACHE_SCHEMA_NAME = "ChapterFrameSelectionCacheIdentity"
CACHE_SCHEMA_VERSION = "1.0"

RESULT_ID_PREFIX = "chapter-frame-selection-"

# 命令级失败码（裁决Q5.8：13个；无SOURCE_VIDEO_MISMATCH）
ERROR_INPUT_TASK_MISSING = "INPUT_TASK_MISSING"
ERROR_INPUT_CHAPTER_PLAN_MISSING = "INPUT_CHAPTER_PLAN_MISSING"
ERROR_INPUT_CHAPTER_PLAN_CORRUPT = "INPUT_CHAPTER_PLAN_CORRUPT"
ERROR_INPUT_TIMELINE_MISSING = "INPUT_TIMELINE_MISSING"
ERROR_INPUT_TIMELINE_CORRUPT = "INPUT_TIMELINE_CORRUPT"
ERROR_INPUT_FRAME_SET_MISSING = "INPUT_FRAME_SET_MISSING"
ERROR_INPUT_FRAME_SET_CORRUPT = "INPUT_FRAME_SET_CORRUPT"
ERROR_INPUT_PAGE_CHANGE_SET_MISSING = "INPUT_PAGE_CHANGE_SET_MISSING"
ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT = "INPUT_PAGE_CHANGE_SET_CORRUPT"
ERROR_INPUT_OCR_SET_MISSING = "INPUT_OCR_SET_MISSING"
ERROR_INPUT_OCR_SET_CORRUPT = "INPUT_OCR_SET_CORRUPT"
ERROR_INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"
ERROR_SELECTION_VALIDATION_FAILED = "SELECTION_VALIDATION_FAILED"

# caption四形态与tier↔form冻结映射（裁决Q3/Q4.3）
CAPTION_FORM_PE_OCR = "pe_ocr"
CAPTION_FORM_PE_ONLY = "pe_only"
CAPTION_FORM_OCR_ONLY = "ocr_only"
CAPTION_FORM_TIME_ONLY = "time_only"
TIER_TO_FORM: dict[int, str] = {
    1: CAPTION_FORM_PE_OCR,
    2: CAPTION_FORM_OCR_ONLY,
    3: CAPTION_FORM_PE_ONLY,
    4: CAPTION_FORM_TIME_ONLY,
}
CAPTION_EXCERPT_JOINER = "；"

_OCR_MODALITY_STATUSES = frozenset(
    {"available", "empty", "disabled", "not_run", "failed"}
)
_FRAME_MODALITY_STATUSES = frozenset({"available", "empty", "not_run"})
_NO_OCR_EVENT_STATUSES = frozenset({"empty", "disabled", "not_run"})
_NO_PE_EVENT_STATUSES = frozenset({"empty", "not_run"})

_FAILURE_INJECTION_POINTS = (
    "after_inputs",
    "before_group_publish",
    "after_group_publish",
    "before_current",
)

_CURRENT_KEYS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "result_id",
    "group",
    "manifest_sha256",
}
_MANIFEST_KEYS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "task_id",
    "result_id",
    "status",
    "cache_identity_sha256",
    "inputs",
    "versions",
    "files",
}
_MANIFEST_INPUT_KEYS = {
    "plan_result_id",
    "plan_manifest_sha256",
    "timeline_id",
    "timeline_manifest_sha256",
    "frame_set_id",
    "frame_set_manifest_sha256",
    "page_change_set_id",
    "page_change_set_manifest_sha256",
    "ocr_set_id",
    "ocr_set_manifest_sha256",
}
_MANIFEST_VERSION_KEYS = {
    "chapter_frames",
    "chapter_frame_selection_schema",
    "selection_policy_version",
    "caption_template_version",
    "artifact_group_manifest",
}
_SELECTION_TOP_KEYS = {
    "schema_name",
    "schema_version",
    "task_id",
    "status",
    "duration_ms",
    "selector",
    "parameters",
    "source_plan",
    "source_timeline",
    "source_frame_set",
    "source_page_change_set",
    "source_ocr_set",
    "ocr_modality_status",
    "chapters",
    "totals",
}
_SELECTOR_KEYS = {"name", "version"}
_PARAMETERS_KEYS = {
    "selection_policy_version",
    "caption_template_version",
    "max_frames_per_chapter",
    "caption_max_lines",
    "caption_max_line_chars",
    "chapter_interval",
}
_SOURCE_PLAN_KEYS = {
    "result_id",
    "manifest_sha256",
    "result_source",
    "quality_level",
    "boundary_count",
}
_SOURCE_TIMELINE_KEYS = {"timeline_id", "manifest_sha256"}
_SOURCE_FRAME_SET_KEYS = {"frame_set_id", "manifest_sha256", "frame_count"}
_SOURCE_PAGE_CHANGE_KEYS = {"page_change_set_id", "manifest_sha256", "candidate_count"}
_SOURCE_OCR_KEYS = {
    "ocr_set_id",
    "manifest_sha256",
    "mode",
    "status",
    "frame_count",
}
_CHAPTER_KEYS = {
    "chapter_index",
    "start_ms",
    "end_ms",
    "title",
    "boundary_source_event_id",
    "selected_frame_count",
    "frames",
}
_FRAME_KEYS = {
    "frame_id",
    "rank",
    "tier",
    "decoded_ms",
    "caption_form",
    "caption_text",
    "page_change",
    "ocr",
}
_FRAME_PAGE_CHANGE_KEYS = {
    "event_id",
    "candidate_id",
    "previous_frame_id",
    "change_strength",
    "perceptual_distance",
}
_FRAME_OCR_KEYS = {
    "event_id",
    "candidate_id",
    "text_sha256",
    "line_count",
    "excerpt_lines",
    "excerpt_line_count",
}
_TOTALS_KEYS = {
    "chapter_count",
    "selected_frame_count",
    "chapters_without_frames",
    "tier_counts",
    "caption_form_counts",
}
_TIER_COUNTS_KEYS = {"tier1", "tier2", "tier3", "tier4"}
_CAPTION_FORM_COUNTS_KEYS = {"pe_ocr", "pe_only", "ocr_only", "time_only"}

# 上游set文档的字段级校验键集（F1/F2/阶段12冻结Schema，裁决§〇实测键集）
_FRAME_DOC_KEYS = {
    "cache_identity_sha256",
    "frame_count",
    "frame_set_id",
    "frames",
    "profile",
    "profile_id",
    "sampling",
    "schema_name",
    "schema_version",
    "source_video",
    "status",
    "task_id",
}
_FRAME_ENTRY_KEYS = {
    "decoded_ms",
    "file",
    "frame_id",
    "is_first",
    "is_last",
    "ordinal",
    "planned_ms",
    "requested_ms",
    "role",
    "sha256",
    "size_bytes",
}
_PC_DOC_KEYS = {
    "cache_identity_sha256",
    "candidate_count",
    "candidates",
    "comparison_count",
    "comparisons",
    "input_frame_set",
    "page_change_set_id",
    "profile",
    "profile_id",
    "schema_name",
    "schema_version",
    "selection",
    "status",
    "task_id",
}
_PC_CANDIDATE_KEYS = {
    "candidate_id",
    "change_strength",
    "decoded_ms",
    "frame_id",
    "ordinal",
    "perceptual_distance",
    "planned_ms",
    "previous_frame_id",
    "requested_ms",
}
_OCR_DOC_KEYS = {
    "cache_identity_sha256",
    "deduplicated_count",
    "engine",
    "failed_frame_count",
    "filtered_line_count",
    "frame_count",
    "frames",
    "input",
    "line_count",
    "mode",
    "ocr_set_id",
    "profile",
    "profile_id",
    "reason_codes",
    "schema_name",
    "schema_version",
    "status",
    "task_id",
}
_OCR_ENTRY_KEYS = {
    "candidate_id",
    "canonical_text",
    "decoded_ms",
    "duplicate_of",
    "filtered_line_count",
    "frame_id",
    "line_count",
    "lines",
    "ordinal",
    "reason_codes",
    "status",
    "text_sha256",
}
_OCR_LINE_KEYS = {"box", "confidence", "line_index", "text"}

_EVENT_KEYS = {
    "id",
    "type",
    "start_ms",
    "end_ms",
    "source_kind",
    "source_segment_id",
    "payload",
    "payload_schema",
    "legacy_event_id",
}
_PE_PAYLOAD_KEYS = {
    "candidate_id",
    "frame_id",
    "previous_frame_id",
    "change_strength",
    "perceptual_distance",
}
_OE_PAYLOAD_KEYS = {"candidate_id", "frame_id", "line_count", "text", "text_sha256"}


# ---------------------------------------------------------------------------
# 类型化异常（裁决Q5.8；base携带domain，CLI JSON摘要透传）
# ---------------------------------------------------------------------------


class ChapterFramesError(RuntimeError):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = False
    next_action = "none"
    domain: str | None = None


class InputTaskMissingError(ChapterFramesError):
    error_code = ERROR_INPUT_TASK_MISSING


class InputChapterPlanMissingError(ChapterFramesError):
    error_code = ERROR_INPUT_CHAPTER_PLAN_MISSING
    recoverable = True
    next_action = "run_plan_chapters_v2"
    domain = "boundaries"


class InputChapterPlanCorruptError(ChapterFramesError):
    error_code = ERROR_INPUT_CHAPTER_PLAN_CORRUPT
    domain = "boundaries"


class InputTimelineMissingError(ChapterFramesError):
    error_code = ERROR_INPUT_TIMELINE_MISSING
    recoverable = True
    next_action = "run_build_timeline_v2"
    domain = "timelines"


class InputTimelineCorruptError(ChapterFramesError):
    error_code = ERROR_INPUT_TIMELINE_CORRUPT
    domain = "timelines"


class InputFrameSetMissingError(ChapterFramesError):
    error_code = ERROR_INPUT_FRAME_SET_MISSING
    recoverable = True
    next_action = "run_sample_frames"
    domain = "frames"


class InputFrameSetCorruptError(ChapterFramesError):
    error_code = ERROR_INPUT_FRAME_SET_CORRUPT
    domain = "frames"


class InputPageChangeSetMissingError(ChapterFramesError):
    error_code = ERROR_INPUT_PAGE_CHANGE_SET_MISSING
    recoverable = True
    next_action = "run_detect_page_changes"
    domain = "page-changes"


class InputPageChangeSetCorruptError(ChapterFramesError):
    error_code = ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT
    domain = "page-changes"


class InputOcrSetMissingError(ChapterFramesError):
    error_code = ERROR_INPUT_OCR_SET_MISSING
    recoverable = True
    next_action = "run_ocr"
    domain = "ocr"


class InputOcrSetCorruptError(ChapterFramesError):
    error_code = ERROR_INPUT_OCR_SET_CORRUPT
    domain = "ocr"


class InputIdentityMismatchError(ChapterFramesError):
    error_code = ERROR_INPUT_IDENTITY_MISMATCH


class CurrentPointerCorruptError(ChapterFramesError):
    error_code = ERROR_CURRENT_POINTER_CORRUPT
    domain = ROOT_DIRECTORY


class CacheCorruptError(ChapterFramesError):
    error_code = ERROR_CACHE_CORRUPT


class ConcurrentRunError(ChapterFramesError):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "retry_after_other_run_finishes"


class PublishFailedError(ChapterFramesError):
    error_code = ERROR_PUBLISH_FAILED


class SelectionValidationFailedError(ChapterFramesError):
    error_code = ERROR_SELECTION_VALIDATION_FAILED


class InjectedChapterFramesFailure(ChapterFramesError):
    pass


# ---------------------------------------------------------------------------
# 通用工程助手（沿14-B先例）
# ---------------------------------------------------------------------------


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(
    path: Path, label: str, error_type: type[ChapterFramesError]
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise error_type(f"{label}无法读取或JSON无效。") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label}必须是JSON对象。")
    return value


def _current_path_state(path: Path, label: str, error_type: type[ChapterFramesError]) -> bool:
    """current路径是普通文件时返回True，不存在时返回False；其余一律硬失败。"""

    if path.is_symlink():
        raise error_type(f"{label}存在但不是普通文件。")
    if path.is_file():
        return True
    if path.exists():
        raise error_type(f"{label}存在但不是普通文件。")
    return False


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str, error_type: type) -> None:
    if set(value) != expected:
        raise error_type(f"{label}字段集合无效。")


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    )


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _inject(selected: str | None, point: str) -> None:
    if selected == point:
        raise InjectedChapterFramesFailure(f"注入失败：{point}")


def _ensure_task_identity(output_root: Path, task_id: str) -> None:
    """task.json缺失/不可读/task_id不符都是INPUT_TASK_MISSING（裁决Q5.8）。"""

    task_json = output_root / task_id / "task.json"
    if not _current_path_state(task_json, "task.json", InputTaskMissingError):
        raise InputTaskMissingError(f"{ERROR_INPUT_TASK_MISSING}：任务工作区缺少task.json登记信息。")
    record = _read_json(task_json, "task.json", InputTaskMissingError)
    if record.get("task_id") != task_id:
        raise InputTaskMissingError(
            f"{ERROR_INPUT_TASK_MISSING}：task.json记录的task_id与请求不一致。"
        )


# ---------------------------------------------------------------------------
# 时间渲染与caption模板（裁决Q4；冻结字符串，禁止自由文本参数）
# ---------------------------------------------------------------------------


def _format_clock(decoded_ms: int) -> str:
    """TIME＝HH:MM:SS，decoded_ms//1000向下取整，零填充（裁决Q4.2）。"""

    seconds_total, _millis = divmod(int(decoded_ms), 1000)
    hours, rest = divmod(seconds_total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _pe_sentence(time_str: str, change_strength: Any) -> str:
    return f"画面于{time_str}发生变化（相对前一采样帧的变化强度{float(change_strength):.4f}）。"


def _ocr_sentence(excerpt_lines: Sequence[str]) -> str:
    joined = CAPTION_EXCERPT_JOINER.join(excerpt_lines)
    return f"该帧OCR识别文本（前{len(excerpt_lines)}行）：{joined}"


def _time_only_prefix(time_str: str) -> str:
    return f"{time_str}采样帧。"


def render_caption_text(
    *,
    caption_form: str,
    decoded_ms: int,
    change_strength: Any = None,
    excerpt_lines: Sequence[str] | None = None,
) -> str:
    """四形态caption的唯一渲染函数；builder与validator共用（裁决Q4.3/Q4.7）。"""

    time_str = _format_clock(decoded_ms)
    if caption_form == CAPTION_FORM_PE_OCR:
        return _pe_sentence(time_str, change_strength) + _ocr_sentence(excerpt_lines or [])
    if caption_form == CAPTION_FORM_PE_ONLY:
        return _pe_sentence(time_str, change_strength)
    if caption_form == CAPTION_FORM_OCR_ONLY:
        return _time_only_prefix(time_str) + _ocr_sentence(excerpt_lines or [])
    if caption_form == CAPTION_FORM_TIME_ONLY:
        return _time_only_prefix(time_str)
    raise SelectionValidationFailedError(f"{ERROR_SELECTION_VALIDATION_FAILED}：未知caption形态{caption_form!r}。")


def _excerpt_lines(ocr_entry: Mapping[str, Any]) -> tuple[list[str], int, int]:
    """返回(截取行, nonblank行数, nonblank字符总数)（裁决Q3/Q4.4）。

    行文本取ocr-set ``lines[].text``原文（非canonical_text，裁决§〇.3）；
    按line_index升序、strip后非空、至多CAPTION_MAX_LINES行；每行strip后
    行内换行/制表符替换为单空格、超过CAPTION_MAX_LINE_CHARS截断加``…``。
    """

    lines = ocr_entry["lines"]
    indexes = [line["line_index"] for line in lines]
    if len(set(indexes)) != len(indexes):
        raise InputOcrSetCorruptError(
            f"{ERROR_INPUT_OCR_SET_CORRUPT}：ocr-set帧条目line_index重复。"
        )
    nonblank: list[str] = []
    for line in sorted(lines, key=lambda item: item["line_index"]):
        stripped = line["text"].strip()
        if stripped:
            nonblank.append(stripped)
    char_count = sum(len(text) for text in nonblank)
    excerpt: list[str] = []
    for text in nonblank[:CAPTION_MAX_LINES]:
        rendered = text.replace("\n", " ").replace("\t", " ")
        if len(rendered) > CAPTION_MAX_LINE_CHARS:
            rendered = rendered[:CAPTION_MAX_LINE_CHARS] + "…"
        excerpt.append(rendered)
    return excerpt, len(nonblank), char_count


# ---------------------------------------------------------------------------
# 上游set文档字段级校验（裁决§3.12实现决定12；按各自冻结Schema）
# ---------------------------------------------------------------------------


def _checked_frame_entries(frame_doc: Mapping[str, Any], task_id: str) -> list[dict[str, Any]]:
    error = InputFrameSetCorruptError
    prefix = ERROR_INPUT_FRAME_SET_CORRUPT
    _exact_keys(frame_doc, _FRAME_DOC_KEYS, "frame-set", error)
    if frame_doc["schema_name"] != "FrameSet" or frame_doc["schema_version"] != "1.0":
        raise error(f"{prefix}：frame-set的schema_name/schema_version无效。")
    if frame_doc["task_id"] != task_id:
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：frame-set的task_id与请求任务不一致。"
        )
    frames = frame_doc["frames"]
    if not isinstance(frames, list) or not _is_int(frame_doc["frame_count"]):
        raise error(f"{prefix}：frame-set的frames/frame_count无效。")
    if len(frames) != frame_doc["frame_count"]:
        raise error(f"{prefix}：frame-set的frame_count与frames数组不一致。")
    seen: set[str] = set()
    for position, entry in enumerate(frames):
        label = f"frames[{position}]"
        if not isinstance(entry, dict):
            raise error(f"{prefix}：{label}必须是JSON对象。")
        _exact_keys(entry, _FRAME_ENTRY_KEYS, label, error)
        if not _is_nonempty_str(entry["frame_id"]) or not _is_int(entry["decoded_ms"]) or entry["decoded_ms"] < 0:
            raise error(f"{prefix}：{label}的frame_id/decoded_ms无效。")
        if entry["frame_id"] in seen:
            raise error(f"{prefix}：{label}的frame_id重复。")
        seen.add(str(entry["frame_id"]))
    return frames


def _checked_pc_candidates(pc_doc: Mapping[str, Any], task_id: str) -> list[dict[str, Any]]:
    error = InputPageChangeSetCorruptError
    prefix = ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT
    _exact_keys(pc_doc, _PC_DOC_KEYS, "page-change-set", error)
    if pc_doc["schema_name"] != "PageChangeSet" or pc_doc["schema_version"] != "1.0":
        raise error(f"{prefix}：page-change-set的schema_name/schema_version无效。")
    if pc_doc["task_id"] != task_id:
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：page-change-set的task_id与请求任务不一致。"
        )
    candidates = pc_doc["candidates"]
    if not isinstance(candidates, list) or not _is_int(pc_doc["candidate_count"]):
        raise error(f"{prefix}：page-change-set的candidates/candidate_count无效。")
    if len(candidates) != pc_doc["candidate_count"]:
        raise error(f"{prefix}：page-change-set的candidate_count与candidates数组不一致。")
    seen: set[str] = set()
    for position, entry in enumerate(candidates):
        label = f"candidates[{position}]"
        if not isinstance(entry, dict):
            raise error(f"{prefix}：{label}必须是JSON对象。")
        _exact_keys(entry, _PC_CANDIDATE_KEYS, label, error)
        if not _is_nonempty_str(entry["candidate_id"]) or not _is_nonempty_str(entry["frame_id"]):
            raise error(f"{prefix}：{label}的candidate_id/frame_id无效。")
        if not _is_int(entry["decoded_ms"]) or entry["decoded_ms"] < 0:
            raise error(f"{prefix}：{label}的decoded_ms无效。")
        if not _is_number(entry["change_strength"]) or not _is_number(entry["perceptual_distance"]):
            raise error(f"{prefix}：{label}的change_strength/perceptual_distance无效。")
        if entry["candidate_id"] in seen:
            raise error(f"{prefix}：{label}的candidate_id重复。")
        seen.add(str(entry["candidate_id"]))
    return candidates


def _checked_ocr_entries(ocr_doc: Mapping[str, Any], task_id: str) -> list[dict[str, Any]]:
    error = InputOcrSetCorruptError
    prefix = ERROR_INPUT_OCR_SET_CORRUPT
    _exact_keys(ocr_doc, _OCR_DOC_KEYS, "ocr-set", error)
    if ocr_doc["schema_name"] != "OCRSet" or ocr_doc["schema_version"] != "1.0":
        raise error(f"{prefix}：ocr-set的schema_name/schema_version无效。")
    if ocr_doc["task_id"] != task_id:
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：ocr-set的task_id与请求任务不一致。"
        )
    entries = ocr_doc["frames"]
    if not isinstance(entries, list) or not _is_int(ocr_doc["frame_count"]):
        raise error(f"{prefix}：ocr-set的frames/frame_count无效。")
    if len(entries) != ocr_doc["frame_count"]:
        raise error(f"{prefix}：ocr-set的frame_count与frames数组不一致。")
    for position, entry in enumerate(entries):
        label = f"ocr frames[{position}]"
        if not isinstance(entry, dict):
            raise error(f"{prefix}：{label}必须是JSON对象。")
        _exact_keys(entry, _OCR_ENTRY_KEYS, label, error)
        if not _is_nonempty_str(entry["candidate_id"]) or not _is_nonempty_str(entry["frame_id"]):
            raise error(f"{prefix}：{label}的candidate_id/frame_id无效。")
        if not _is_int(entry["decoded_ms"]) or entry["decoded_ms"] < 0:
            raise error(f"{prefix}：{label}的decoded_ms无效。")
        if not isinstance(entry["status"], str) or not isinstance(entry["canonical_text"], str):
            raise error(f"{prefix}：{label}的status/canonical_text无效。")
        if entry["duplicate_of"] is not None and not isinstance(entry["duplicate_of"], str):
            raise error(f"{prefix}：{label}的duplicate_of无效。")
        if not _is_int(entry["line_count"]) or entry["line_count"] < 0:
            raise error(f"{prefix}：{label}的line_count无效。")
        lines = entry["lines"]
        if not isinstance(lines, list) or len(lines) != entry["line_count"]:
            raise error(f"{prefix}：{label}的lines与line_count不一致。")
        for line_pos, line in enumerate(lines):
            line_label = f"{label}.lines[{line_pos}]"
            if not isinstance(line, dict):
                raise error(f"{prefix}：{line_label}必须是JSON对象。")
            _exact_keys(line, _OCR_LINE_KEYS, line_label, error)
            if not _is_int(line["line_index"]):
                raise error(f"{prefix}：{line_label}的line_index无效。")
            if not isinstance(line["text"], str):
                raise error(f"{prefix}：{line_label}的text必须是字符串。")
    return entries


# ---------------------------------------------------------------------------
# 证据索引与事件级交叉一致性（裁决Q2.3/Q2.4＋实现决定3/4/6）
# ---------------------------------------------------------------------------


def _events_of(timeline: Mapping[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [event for event in timeline["events"] if event["type"] == event_type]


def _modality_status(timeline: Mapping[str, Any], key: str) -> str:
    modalities = timeline["modalities"]
    if not isinstance(modalities, dict) or key not in modalities:
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：Timeline 2.0缺少modalities.{key}。"
        )
    block = modalities[key]
    if not isinstance(block, dict) or not _is_nonempty_str(block.get("status")):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：Timeline 2.0的modalities.{key}.status无效。"
        )
    return str(block["status"])


def _mismatch(detail: str) -> InputIdentityMismatchError:
    return InputIdentityMismatchError(f"{ERROR_INPUT_IDENTITY_MISMATCH}：{detail}")


def _build_pe_index(
    timeline: Mapping[str, Any],
    pc_candidates: Sequence[Mapping[str, Any]] | None,
    frame_ids: set[str],
) -> dict[str, dict[str, Any]]:
    pe_events = _events_of(timeline, PAGE_CHANGE_EVENT_TYPE)
    fc_status = _modality_status(timeline, "frame_change")
    if fc_status not in _FRAME_MODALITY_STATUSES:
        raise _mismatch(f"frame_change模态状态{fc_status!r}不在13-1词汇表内。")
    if (fc_status == "available") != (len(pe_events) > 0):
        raise _mismatch(
            f"frame_change模态状态{fc_status!r}与pe事件数量{len(pe_events)}不一致。"
        )
    if not pe_events:
        return {}
    if pc_candidates is None:
        raise InputPageChangeSetMissingError(
            f"{ERROR_INPUT_PAGE_CHANGE_SET_MISSING}：Timeline含pe事件但没有可消费的page-change-set。"
        )
    by_candidate = {str(candidate["candidate_id"]): candidate for candidate in pc_candidates}
    index: dict[str, dict[str, Any]] = {}
    for event in pe_events:
        payload = event["payload"]
        candidate_id = payload.get("candidate_id")
        candidate = by_candidate.get(str(candidate_id))
        if candidate is None:
            raise _mismatch(f"pe事件{event['id']}的candidate_id在pc-set中不存在。")
        for field in ("frame_id", "change_strength", "perceptual_distance"):
            if payload.get(field) != candidate[field]:
                raise _mismatch(
                    f"pe事件{event['id']}的{field}与pc候选{candidate_id}不一致。"
                )
        if int(event["start_ms"]) != int(candidate["decoded_ms"]):
            raise _mismatch(f"pe事件{event['id']}的start_ms与候选decoded_ms不一致。")
        frame_id = str(candidate["frame_id"])
        if frame_id not in frame_ids:
            raise _mismatch(f"pe事件{event['id']}引用的帧{frame_id}不在frame-set中。")
        if frame_id in index:
            raise _mismatch(f"帧{frame_id}被多个pe事件引用。")
        index[frame_id] = {
            "event_id": str(event["id"]),
            "candidate_id": str(candidate_id),
            "previous_frame_id": candidate["previous_frame_id"],
            "change_strength": candidate["change_strength"],
            "perceptual_distance": candidate["perceptual_distance"],
        }
    return index


def _build_oe_index(
    timeline: Mapping[str, Any],
    ocr_entries: Sequence[Mapping[str, Any]] | None,
    frame_ids: set[str],
) -> dict[str, dict[str, Any]]:
    oe_events = _events_of(timeline, OCR_EVENT_TYPE)
    ocr_status = _modality_status(timeline, "ocr")
    if ocr_status not in _OCR_MODALITY_STATUSES:
        raise _mismatch(f"ocr模态状态{ocr_status!r}不在13-1词汇表内。")
    if ocr_status == "available" and not oe_events:
        raise _mismatch("ocr模态状态available但Timeline不含oe事件。")
    if oe_events and ocr_status in _NO_OCR_EVENT_STATUSES:
        raise _mismatch(
            f"ocr模态状态{ocr_status!r}与oe事件数量{len(oe_events)}不一致。"
        )
    if not oe_events:
        return {}
    if ocr_entries is None:
        raise InputOcrSetMissingError(
            f"{ERROR_INPUT_OCR_SET_MISSING}：Timeline含oe事件但没有可消费的ocr-set。"
        )
    by_candidate = {str(entry["candidate_id"]): entry for entry in ocr_entries}
    index: dict[str, dict[str, Any]] = {}
    for event in oe_events:
        payload = event["payload"]
        candidate_id = payload.get("candidate_id")
        entry = by_candidate.get(str(candidate_id))
        if entry is None:
            raise _mismatch(f"oe事件{event['id']}的candidate_id在ocr-set中不存在。")
        if payload.get("text_sha256") != entry["text_sha256"]:
            raise _mismatch(f"oe事件{event['id']}的text_sha256与ocr条目不一致。")
        if payload.get("frame_id") != entry["frame_id"]:
            raise _mismatch(f"oe事件{event['id']}的frame_id与ocr条目不一致。")
        if int(event["start_ms"]) != int(entry["decoded_ms"]):
            raise _mismatch(f"oe事件{event['id']}的start_ms与ocr条目decoded_ms不一致。")
        # 裁决§〇.3/V9：oe只对status="ok"、duplicate_of=null且canonical非空的帧生成。
        if entry["status"] != "ok" or entry["duplicate_of"] is not None or not entry["canonical_text"]:
            raise _mismatch(f"oe事件{event['id']}指向不满足oe生成条件的ocr帧条目。")
        frame_id = str(entry["frame_id"])
        if frame_id not in frame_ids:
            raise _mismatch(f"oe事件{event['id']}引用的帧{frame_id}不在frame-set中。")
        if frame_id in index:
            raise _mismatch(f"帧{frame_id}被多个oe事件引用。")
        excerpt, nonblank_lines, nonblank_chars = _excerpt_lines(entry)
        index[frame_id] = {
            "event_id": str(event["id"]),
            "candidate_id": str(candidate_id),
            "text_sha256": str(entry["text_sha256"]),
            "line_count": int(entry["line_count"]),
            "excerpt_lines": excerpt,
            "nonblank_line_count": nonblank_lines,
            "nonblank_char_count": nonblank_chars,
        }
    return index


# ---------------------------------------------------------------------------
# 选择算法（裁决Q3冻结伪码；纯函数，只依赖五份已发布JSON）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChapterFramesUpstream:
    """一次加载得到的五域文档＋current指针（供build与组复验共用）。"""

    plan: dict[str, Any]
    plan_pointer: dict[str, Any]
    timeline: dict[str, Any]
    timeline_pointer: dict[str, Any]
    frame_doc: dict[str, Any]
    frame_pointer: dict[str, Any]
    page_change_doc: dict[str, Any] | None
    page_change_pointer: dict[str, Any] | None
    ocr_doc: dict[str, Any] | None
    ocr_pointer: dict[str, Any] | None


def chapter_frames_cache_identity(
    *,
    task_id: str,
    plan_ref: Mapping[str, Any],
    timeline_ref: Mapping[str, Any],
    frame_set_ref: Mapping[str, Any],
    page_change_set_ref: Mapping[str, Any] | None = None,
    ocr_set_ref: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """裁决Q5.6精确键集；未消费域的id/sha照录null。"""

    payload = {
        "schema_name": CACHE_SCHEMA_NAME,
        "schema_version": CACHE_SCHEMA_VERSION,
        "task_id": task_id,
        "selector_version": CHAPTER_FRAMES_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "caption_template_version": CAPTION_TEMPLATE_VERSION,
        "max_frames_per_chapter": MAX_FRAMES_PER_CHAPTER,
        "caption_max_lines": CAPTION_MAX_LINES,
        "caption_max_line_chars": CAPTION_MAX_LINE_CHARS,
        "plan_result_id": str(plan_ref["result_id"]),
        "plan_manifest_sha256": str(plan_ref["manifest_sha256"]),
        "timeline_id": str(timeline_ref["result_id"]),
        "timeline_manifest_sha256": str(timeline_ref["manifest_sha256"]),
        "frame_set_id": str(frame_set_ref["result_id"]),
        "frame_set_manifest_sha256": str(frame_set_ref["manifest_sha256"]),
        "page_change_set_id": (
            str(page_change_set_ref["result_id"]) if page_change_set_ref is not None else None
        ),
        "page_change_set_manifest_sha256": (
            str(page_change_set_ref["manifest_sha256"]) if page_change_set_ref is not None else None
        ),
        "ocr_set_id": str(ocr_set_ref["result_id"]) if ocr_set_ref is not None else None,
        "ocr_set_manifest_sha256": (
            str(ocr_set_ref["manifest_sha256"]) if ocr_set_ref is not None else None
        ),
    }
    return payload, canonical_json_sha256(payload)


def _cache_identity_from_document(selection: Mapping[str, Any]) -> dict[str, Any]:
    source_plan = selection["source_plan"]
    source_timeline = selection["source_timeline"]
    source_frames = selection["source_frame_set"]
    source_pc = selection["source_page_change_set"]
    source_ocr = selection["source_ocr_set"]
    return {
        "schema_name": CACHE_SCHEMA_NAME,
        "schema_version": CACHE_SCHEMA_VERSION,
        "task_id": selection["task_id"],
        "selector_version": CHAPTER_FRAMES_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "caption_template_version": CAPTION_TEMPLATE_VERSION,
        "max_frames_per_chapter": MAX_FRAMES_PER_CHAPTER,
        "caption_max_lines": CAPTION_MAX_LINES,
        "caption_max_line_chars": CAPTION_MAX_LINE_CHARS,
        "plan_result_id": str(source_plan["result_id"]),
        "plan_manifest_sha256": str(source_plan["manifest_sha256"]),
        "timeline_id": str(source_timeline["timeline_id"]),
        "timeline_manifest_sha256": str(source_timeline["manifest_sha256"]),
        "frame_set_id": str(source_frames["frame_set_id"]),
        "frame_set_manifest_sha256": str(source_frames["manifest_sha256"]),
        "page_change_set_id": str(source_pc["page_change_set_id"]) if source_pc is not None else None,
        "page_change_set_manifest_sha256": (
            str(source_pc["manifest_sha256"]) if source_pc is not None else None
        ),
        "ocr_set_id": str(source_ocr["ocr_set_id"]) if source_ocr is not None else None,
        "ocr_set_manifest_sha256": str(source_ocr["manifest_sha256"]) if source_ocr is not None else None,
    }


def _chapter_intervals(plan: Mapping[str, Any]) -> list[tuple[int, int]]:
    boundaries = plan["boundaries"]
    duration = int(plan["duration_ms"])
    intervals: list[tuple[int, int]] = []
    for position, boundary in enumerate(boundaries):
        start = int(boundary["timestamp_ms"])
        end = int(boundaries[position + 1]["timestamp_ms"]) if position + 1 < len(boundaries) else duration
        intervals.append((start, end))
    return intervals


def build_chapter_frame_selection(
    plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
    frame_set: Mapping[str, Any],
    page_change_set: Mapping[str, Any] | None,
    ocr_set: Mapping[str, Any] | None,
    *,
    task_id: str,
    plan_ref: Mapping[str, Any],
    timeline_ref: Mapping[str, Any],
    frame_set_ref: Mapping[str, Any],
    page_change_set_ref: Mapping[str, Any] | None = None,
    ocr_set_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """裁决Q3/Q4纯函数核心：五份文档进，ChapterFrameSelection 1.0对象出。

    文档字段级校验、模态一致性（实现决定4）、事件级交叉校验（Q2.3）全部
    在本函数内执行，可用合成fixture直接单测；磁盘读取与发布不在此路径。
    """

    if plan["task_id"] != task_id:
        raise _mismatch("ChapterPlan的task_id与请求任务不一致。")
    if timeline["task_id"] != task_id:
        raise _mismatch("Timeline的task_id与请求任务不一致。")
    if int(plan["duration_ms"]) != int(timeline["duration_ms"]):
        raise _mismatch("ChapterPlan与Timeline的duration_ms不一致。")
    if str(timeline["timeline_id"]) != str(timeline_ref["result_id"]):
        raise _mismatch("timelines current的result_id与时间轴对象timeline_id不一致。")
    if str(plan["source_timeline"]["timeline_id"]) != str(timeline["timeline_id"]):
        raise _mismatch("ChapterPlan引用的timeline_id与当前时间轴不一致。")

    frame_entries = _checked_frame_entries(frame_set, task_id)
    frame_ids = {str(entry["frame_id"]) for entry in frame_entries}
    pc_candidates: list[dict[str, Any]] | None = None
    if page_change_set is not None:
        pc_candidates = _checked_pc_candidates(page_change_set, task_id)
    ocr_entries: list[dict[str, Any]] | None = None
    if ocr_set is not None:
        ocr_entries = _checked_ocr_entries(ocr_set, task_id)

    pe_index = _build_pe_index(timeline, pc_candidates, frame_ids)
    oe_index = _build_oe_index(timeline, ocr_entries, frame_ids)

    plan_parameters = plan["parameters"]
    intervals = _chapter_intervals(plan)
    chapters: list[dict[str, Any]] = []
    tier_totals = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0}
    form_totals = {
        CAPTION_FORM_PE_OCR: 0,
        CAPTION_FORM_PE_ONLY: 0,
        CAPTION_FORM_OCR_ONLY: 0,
        CAPTION_FORM_TIME_ONLY: 0,
    }
    selected_total = 0
    chapters_without_frames = 0

    for chapter_index, (start_ms, end_ms) in enumerate(intervals):
        boundary = plan["boundaries"][chapter_index]
        pool = [
            entry
            for entry in frame_entries
            if start_ms <= int(entry["decoded_ms"]) < end_ms
        ]
        t1 = [
            entry
            for entry in pool
            if entry["frame_id"] in pe_index and entry["frame_id"] in oe_index
        ]
        t2 = [
            entry
            for entry in pool
            if entry["frame_id"] in oe_index and entry["frame_id"] not in pe_index
        ]
        t3 = [
            entry
            for entry in pool
            if entry["frame_id"] in pe_index and entry["frame_id"] not in oe_index
        ]
        t1.sort(key=lambda e: (-e_strength(pe_index, e), int(e["decoded_ms"]), str(e["frame_id"])))
        t3.sort(key=lambda e: (-e_strength(pe_index, e), int(e["decoded_ms"]), str(e["frame_id"])))
        t2.sort(
            key=lambda e: (
                -oe_index[str(e["frame_id"])]["nonblank_line_count"],
                -oe_index[str(e["frame_id"])]["nonblank_char_count"],
                int(e["decoded_ms"]),
                str(e["frame_id"]),
            )
        )
        cascade = t1 + t2 + t3
        chosen_pairs: list[tuple[dict[str, Any], int]] = [
            (entry, tier_of(position, len(t1), len(t2)))
            for position, entry in enumerate(cascade[:MAX_FRAMES_PER_CHAPTER])
        ]
        if not chosen_pairs and pool:
            mid = (start_ms + end_ms) // 2
            fallback = min(
                pool,
                key=lambda e: (abs(int(e["decoded_ms"]) - mid), int(e["decoded_ms"]), str(e["frame_id"])),
            )
            chosen_pairs = [(fallback, 4)]

        frames_out: list[dict[str, Any]] = []
        for rank, (entry, tier) in enumerate(chosen_pairs, start=1):
            frame_id = str(entry["frame_id"])
            decoded_ms = int(entry["decoded_ms"])
            pe = pe_index.get(frame_id) if tier in (1, 3) else None
            oe = oe_index.get(frame_id) if tier in (1, 2) else None
            caption_form = TIER_TO_FORM[tier]
            caption_text = render_caption_text(
                caption_form=caption_form,
                decoded_ms=decoded_ms,
                change_strength=pe["change_strength"] if pe is not None else None,
                excerpt_lines=oe["excerpt_lines"] if oe is not None else None,
            )
            page_change_block = (
                {
                    "event_id": pe["event_id"],
                    "candidate_id": pe["candidate_id"],
                    "previous_frame_id": pe["previous_frame_id"],
                    "change_strength": pe["change_strength"],
                    "perceptual_distance": pe["perceptual_distance"],
                }
                if pe is not None
                else None
            )
            ocr_block = (
                {
                    "event_id": oe["event_id"],
                    "candidate_id": oe["candidate_id"],
                    "text_sha256": oe["text_sha256"],
                    "line_count": oe["line_count"],
                    "excerpt_lines": list(oe["excerpt_lines"]),
                    "excerpt_line_count": len(oe["excerpt_lines"]),
                }
                if oe is not None
                else None
            )
            frames_out.append(
                {
                    "frame_id": frame_id,
                    "rank": rank,
                    "tier": tier,
                    "decoded_ms": decoded_ms,
                    "caption_form": caption_form,
                    "caption_text": caption_text,
                    "page_change": page_change_block,
                    "ocr": ocr_block,
                }
            )
        frames_out.sort(key=lambda frame: (int(frame["decoded_ms"]), str(frame["frame_id"])))
        for frame in frames_out:
            tier_totals[f"tier{frame['tier']}"] += 1
            form_totals[frame["caption_form"]] += 1
        selected_total += len(frames_out)
        if not frames_out:
            chapters_without_frames += 1
        chapters.append(
            {
                "chapter_index": chapter_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "title": boundary["title"],
                # 实现决定9：直接拷贝plan值；14-B校验器保证首条恒null。
                "boundary_source_event_id": boundary["source_event_id"],
                "selected_frame_count": len(frames_out),
                "frames": frames_out,
            }
        )

    consumed_pc = pe_index != {} or _events_of(timeline, PAGE_CHANGE_EVENT_TYPE) != []
    consumed_ocr = oe_index != {} or _events_of(timeline, OCR_EVENT_TYPE) != []

    selection: dict[str, Any] = {
        "schema_name": SELECTION_SCHEMA_NAME,
        "schema_version": SELECTION_SCHEMA_VERSION,
        "task_id": task_id,
        "status": "success",
        "duration_ms": int(plan["duration_ms"]),
        "selector": {"name": SELECTOR_NAME, "version": SELECTOR_VERSION},
        "parameters": {
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "caption_template_version": CAPTION_TEMPLATE_VERSION,
            "max_frames_per_chapter": MAX_FRAMES_PER_CHAPTER,
            "caption_max_lines": CAPTION_MAX_LINES,
            "caption_max_line_chars": CAPTION_MAX_LINE_CHARS,
            "chapter_interval": CHAPTER_INTERVAL,
        },
        "source_plan": {
            "result_id": str(plan_ref["result_id"]),
            "manifest_sha256": str(plan_ref["manifest_sha256"]),
            "result_source": str(plan_parameters["result_source"]),
            "quality_level": str(plan_parameters["quality_level"]),
            "boundary_count": len(plan["boundaries"]),
        },
        "source_timeline": {
            "timeline_id": str(timeline["timeline_id"]),
            "manifest_sha256": str(timeline_ref["manifest_sha256"]),
        },
        "source_frame_set": {
            "frame_set_id": str(frame_set_ref["result_id"]),
            "manifest_sha256": str(frame_set_ref["manifest_sha256"]),
            "frame_count": int(frame_set["frame_count"]),
        },
        "source_page_change_set": (
            {
                "page_change_set_id": str(page_change_set_ref["result_id"]),
                "manifest_sha256": str(page_change_set_ref["manifest_sha256"]),
                "candidate_count": int(page_change_set["candidate_count"]),
            }
            if consumed_pc and page_change_set is not None and page_change_set_ref is not None
            else None
        ),
        "source_ocr_set": (
            {
                "ocr_set_id": str(ocr_set_ref["result_id"]),
                "manifest_sha256": str(ocr_set_ref["manifest_sha256"]),
                "mode": str(ocr_set["mode"]),
                "status": str(ocr_set["status"]),
                "frame_count": int(ocr_set["frame_count"]),
            }
            if consumed_ocr and ocr_set is not None and ocr_set_ref is not None
            else None
        ),
        "ocr_modality_status": _modality_status(timeline, "ocr"),
        "chapters": chapters,
        "totals": {
            "chapter_count": len(chapters),
            "selected_frame_count": selected_total,
            "chapters_without_frames": chapters_without_frames,
            "tier_counts": tier_totals,
            "caption_form_counts": form_totals,
        },
    }
    _validate_selection_document(selection)
    return selection


def e_strength(pe_index: Mapping[str, Mapping[str, Any]], entry: Mapping[str, Any]) -> Any:
    return pe_index[str(entry["frame_id"])]["change_strength"]


def tier_of(position: int, t1_len: int, t2_len: int) -> int:
    """级联截断序中的tier：t1→1、t2→2、t3→3（裁决Q3）。"""

    if position < t1_len:
        return 1
    if position < t1_len + t2_len:
        return 2
    return 3


# ---------------------------------------------------------------------------
# 产物自校验（裁决Q3"图片位于章节范围内"校验器＋Q4.7重渲染＋§3.2精确键集）
# ---------------------------------------------------------------------------


class _ValidationAggregateError(SelectionValidationFailedError):
    pass


def _fail(detail: str) -> None:
    raise SelectionValidationFailedError(f"{ERROR_SELECTION_VALIDATION_FAILED}：{detail}")


def _validate_selection_document(selection: Any) -> None:
    if not isinstance(selection, dict):
        _fail("ChapterFrameSelection必须是JSON对象。")
    _exact_keys(selection, _SELECTION_TOP_KEYS, "ChapterFrameSelection", SelectionValidationFailedError)
    if (
        selection["schema_name"] != SELECTION_SCHEMA_NAME
        or selection["schema_version"] != SELECTION_SCHEMA_VERSION
    ):
        _fail("schema_name/schema_version无效。")
    if not _is_nonempty_str(selection["task_id"]):
        _fail("task_id无效。")
    if selection["status"] != "success":
        _fail("status唯一合法值是success（失败＝无产物）。")
    if not _is_int(selection["duration_ms"]) or selection["duration_ms"] <= 0:
        _fail("duration_ms必须是正整数。")
    selector = selection["selector"]
    if not isinstance(selector, dict):
        _fail("selector必须是JSON对象。")
    _exact_keys(selector, _SELECTOR_KEYS, "selector", SelectionValidationFailedError)
    if selector["name"] != SELECTOR_NAME or selector["version"] != SELECTOR_VERSION:
        _fail("selector身份与冻结常量不一致。")
    parameters = selection["parameters"]
    if not isinstance(parameters, dict):
        _fail("parameters必须是JSON对象。")
    _exact_keys(parameters, _PARAMETERS_KEYS, "parameters", SelectionValidationFailedError)
    if parameters != {
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "caption_template_version": CAPTION_TEMPLATE_VERSION,
        "max_frames_per_chapter": MAX_FRAMES_PER_CHAPTER,
        "caption_max_lines": CAPTION_MAX_LINES,
        "caption_max_line_chars": CAPTION_MAX_LINE_CHARS,
        "chapter_interval": CHAPTER_INTERVAL,
    }:
        _fail("parameters与冻结政策/模板常量不一致。")

    source_plan = selection["source_plan"]
    if not isinstance(source_plan, dict):
        _fail("source_plan必须是JSON对象。")
    _exact_keys(source_plan, _SOURCE_PLAN_KEYS, "source_plan", SelectionValidationFailedError)
    if not _is_nonempty_str(source_plan["result_id"]) or not _sha256_hex(source_plan["manifest_sha256"]):
        _fail("source_plan的result_id/manifest_sha256无效。")
    pair = (source_plan["result_source"], source_plan["quality_level"])
    if pair not in {("primary", "normal"), ("emergency", "degraded")}:
        _fail("source_plan的result_source/quality_level组合无效。")
    if not _is_int(source_plan["boundary_count"]) or source_plan["boundary_count"] < 1:
        _fail("source_plan.boundary_count必须是正整数。")

    source_timeline = selection["source_timeline"]
    if not isinstance(source_timeline, dict):
        _fail("source_timeline必须是JSON对象。")
    _exact_keys(source_timeline, _SOURCE_TIMELINE_KEYS, "source_timeline", SelectionValidationFailedError)
    if not _is_nonempty_str(source_timeline["timeline_id"]) or not _sha256_hex(
        source_timeline["manifest_sha256"]
    ):
        _fail("source_timeline字段无效。")

    source_frames = selection["source_frame_set"]
    if not isinstance(source_frames, dict):
        _fail("source_frame_set必须是JSON对象。")
    _exact_keys(source_frames, _SOURCE_FRAME_SET_KEYS, "source_frame_set", SelectionValidationFailedError)
    if (
        not _is_nonempty_str(source_frames["frame_set_id"])
        or not _sha256_hex(source_frames["manifest_sha256"])
        or not _is_int(source_frames["frame_count"])
        or source_frames["frame_count"] < 0
    ):
        _fail("source_frame_set字段无效。")

    source_pc = selection["source_page_change_set"]
    if source_pc is not None:
        if not isinstance(source_pc, dict):
            _fail("source_page_change_set必须是对象或null。")
        _exact_keys(source_pc, _SOURCE_PAGE_CHANGE_KEYS, "source_page_change_set", SelectionValidationFailedError)
        if (
            not _is_nonempty_str(source_pc["page_change_set_id"])
            or not _sha256_hex(source_pc["manifest_sha256"])
            or not _is_int(source_pc["candidate_count"])
            or source_pc["candidate_count"] < 0
        ):
            _fail("source_page_change_set字段无效。")
    source_ocr = selection["source_ocr_set"]
    if source_ocr is not None:
        if not isinstance(source_ocr, dict):
            _fail("source_ocr_set必须是对象或null。")
        _exact_keys(source_ocr, _SOURCE_OCR_KEYS, "source_ocr_set", SelectionValidationFailedError)
        if (
            not _is_nonempty_str(source_ocr["ocr_set_id"])
            or not _sha256_hex(source_ocr["manifest_sha256"])
            or not _is_nonempty_str(source_ocr["mode"])
            or not _is_nonempty_str(source_ocr["status"])
            or not _is_int(source_ocr["frame_count"])
            or source_ocr["frame_count"] < 0
        ):
            _fail("source_ocr_set字段无效。")

    if not _is_nonempty_str(selection["ocr_modality_status"]):
        _fail("ocr_modality_status无效。")

    chapters = selection["chapters"]
    if not isinstance(chapters, list) or len(chapters) != source_plan["boundary_count"]:
        _fail("chapters数量必须等于boundary_count。")
    duration = int(selection["duration_ms"])
    tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0}
    form_counts = {
        CAPTION_FORM_PE_OCR: 0,
        CAPTION_FORM_PE_ONLY: 0,
        CAPTION_FORM_OCR_ONLY: 0,
        CAPTION_FORM_TIME_ONLY: 0,
    }
    selected_total = 0
    without_frames = 0
    for chapter_index, chapter in enumerate(chapters):
        label = f"chapters[{chapter_index}]"
        if not isinstance(chapter, dict):
            _fail(f"{label}必须是JSON对象。")
        _exact_keys(chapter, _CHAPTER_KEYS, label, SelectionValidationFailedError)
        if chapter["chapter_index"] != chapter_index:
            _fail(f"{label}.chapter_index必须等于数组下标。")
        start_ms = chapter["start_ms"]
        end_ms = chapter["end_ms"]
        if not _is_int(start_ms) or not _is_int(end_ms) or start_ms < 0 or end_ms <= start_ms:
            _fail(f"{label}的start_ms/end_ms无效。")
        if chapter_index == 0 and start_ms != 0:
            _fail("首章start_ms必须为0。")
        if chapter_index > 0 and start_ms != chapters[chapter_index - 1]["end_ms"]:
            _fail(f"{label}.start_ms必须与上一章end_ms相接。")
        if chapter_index == len(chapters) - 1 and end_ms != duration:
            _fail("末章end_ms必须等于duration_ms。")
        if not isinstance(chapter["title"], str) or not chapter["title"].strip():
            _fail(f"{label}.title必须是非空字符串。")
        boundary_source = chapter["boundary_source_event_id"]
        if boundary_source is not None and not _is_nonempty_str(boundary_source):
            _fail(f"{label}.boundary_source_event_id必须是null或非空字符串。")
        frames = chapter["frames"]
        if not isinstance(frames, list) or len(frames) > MAX_FRAMES_PER_CHAPTER:
            _fail(f"{label}.frames数量无效。")
        if chapter["selected_frame_count"] != len(frames):
            _fail(f"{label}.selected_frame_count与frames数组不一致。")
        if not frames:
            without_frames += 1
        ranks: list[int] = []
        previous_decoded: int | None = None
        seen_frame_ids: set[str] = set()
        for position, frame in enumerate(frames):
            frame_label = f"{label}.frames[{position}]"
            if not isinstance(frame, dict):
                _fail(f"{frame_label}必须是JSON对象。")
            _exact_keys(frame, _FRAME_KEYS, frame_label, SelectionValidationFailedError)
            frame_id = frame["frame_id"]
            if not _is_nonempty_str(frame_id) or frame_id in seen_frame_ids:
                _fail(f"{frame_label}.frame_id无效或章内重复。")
            seen_frame_ids.add(str(frame_id))
            tier = frame["tier"]
            if not _is_int(tier) or tier not in (1, 2, 3, 4):
                _fail(f"{frame_label}.tier必须属于{{1,2,3,4}}。")
            rank = frame["rank"]
            if not _is_int(rank) or rank < 1:
                _fail(f"{frame_label}.rank必须是1起的正整数。")
            ranks.append(int(rank))
            decoded_ms = frame["decoded_ms"]
            if not _is_int(decoded_ms) or not (start_ms <= decoded_ms < end_ms):
                _fail(f"{frame_label}.decoded_ms必须位于本章半开区间[start_ms,end_ms)内。")
            if previous_decoded is not None and decoded_ms < previous_decoded:
                _fail(f"{frame_label}的frames数组必须按decoded_ms升序写出。")
            previous_decoded = int(decoded_ms)
            page_change = frame["page_change"]
            ocr = frame["ocr"]
            if page_change is not None:
                if not isinstance(page_change, dict):
                    _fail(f"{frame_label}.page_change必须是对象或null。")
                _exact_keys(
                    page_change, _FRAME_PAGE_CHANGE_KEYS, f"{frame_label}.page_change",
                    SelectionValidationFailedError,
                )
                if (
                    not _is_nonempty_str(page_change["event_id"])
                    or not _is_nonempty_str(page_change["candidate_id"])
                    or not _is_nonempty_str(page_change["previous_frame_id"])
                    or not _is_number(page_change["change_strength"])
                    or not _is_number(page_change["perceptual_distance"])
                ):
                    _fail(f"{frame_label}.page_change字段无效。")
            if ocr is not None:
                if not isinstance(ocr, dict):
                    _fail(f"{frame_label}.ocr必须是对象或null。")
                _exact_keys(ocr, _FRAME_OCR_KEYS, f"{frame_label}.ocr", SelectionValidationFailedError)
                if (
                    not _is_nonempty_str(ocr["event_id"])
                    or not _is_nonempty_str(ocr["candidate_id"])
                    or not _is_nonempty_str(ocr["text_sha256"])
                    or not _is_int(ocr["line_count"])
                    or ocr["line_count"] < 0
                    or not isinstance(ocr["excerpt_lines"], list)
                    or any(not isinstance(line, str) for line in ocr["excerpt_lines"])
                    or not _is_int(ocr["excerpt_line_count"])
                    or ocr["excerpt_line_count"] != len(ocr["excerpt_lines"])
                    or ocr["excerpt_line_count"] > CAPTION_MAX_LINES
                ):
                    _fail(f"{frame_label}.ocr字段无效。")
            expected_form = TIER_TO_FORM[int(tier)]
            if (
                frame["caption_form"] != expected_form
                or (tier in (1, 3)) != (page_change is not None)
                or (tier in (1, 2)) != (ocr is not None)
            ):
                _fail(f"{frame_label}的tier/caption_form/证据块组合违反冻结映射。")
            if frame["caption_form"] in (CAPTION_FORM_OCR_ONLY,) and not ocr["excerpt_lines"]:
                _fail(f"{frame_label}的ocr_only形态excerpt_lines不能为空。")
            if frame["caption_form"] == CAPTION_FORM_PE_OCR and not ocr["excerpt_lines"]:
                _fail(f"{frame_label}的pe_ocr形态excerpt_lines不能为空。")
            rendered = render_caption_text(
                caption_form=frame["caption_form"],
                decoded_ms=int(decoded_ms),
                change_strength=page_change["change_strength"] if page_change is not None else None,
                excerpt_lines=ocr["excerpt_lines"] if ocr is not None else None,
            )
            if frame["caption_text"] != rendered:
                _fail(f"{frame_label}的caption_text与模板重渲染不一致。")
            tier_counts[f"tier{tier}"] += 1
            form_counts[frame["caption_form"]] += 1
        if sorted(ranks) != list(range(1, len(frames) + 1)):
            _fail(f"{label}.rank必须是1..n的排列。")
        selected_total += len(frames)

    totals = selection["totals"]
    if not isinstance(totals, dict):
        _fail("totals必须是JSON对象。")
    _exact_keys(totals, _TOTALS_KEYS, "totals", SelectionValidationFailedError)
    if totals["chapter_count"] != len(chapters):
        _fail("totals.chapter_count与chapters数组不一致。")
    if totals["selected_frame_count"] != selected_total:
        _fail("totals.selected_frame_count重算不一致。")
    if totals["chapters_without_frames"] != without_frames:
        _fail("totals.chapters_without_frames重算不一致。")
    tier_block = totals["tier_counts"]
    if not isinstance(tier_block, dict):
        _fail("totals.tier_counts必须是JSON对象。")
    _exact_keys(tier_block, _TIER_COUNTS_KEYS, "totals.tier_counts", SelectionValidationFailedError)
    if tier_block != tier_counts:
        _fail("totals.tier_counts重算不一致。")
    form_block = totals["caption_form_counts"]
    if not isinstance(form_block, dict):
        _fail("totals.caption_form_counts必须是JSON对象。")
    _exact_keys(form_block, _CAPTION_FORM_COUNTS_KEYS, "totals.caption_form_counts", SelectionValidationFailedError)
    if form_block != form_counts:
        _fail("totals.caption_form_counts重算不一致。")


def _cross_check_selection_with_upstream(selection: Mapping[str, Any], upstream: ChapterFramesUpstream) -> None:
    """current复验/发布前自校验共用：按当前上游重新推导并逐字节比对（裁决Q3）。"""

    rebuilt = build_chapter_frame_selection(
        upstream.plan,
        upstream.timeline,
        upstream.frame_doc,
        upstream.page_change_doc,
        upstream.ocr_doc,
        task_id=upstream.plan["task_id"],
        plan_ref=upstream.plan_pointer,
        timeline_ref=upstream.timeline_pointer,
        frame_set_ref=upstream.frame_pointer,
        page_change_set_ref=upstream.page_change_pointer,
        ocr_set_ref=upstream.ocr_pointer,
    )
    if canonical_json_bytes(dict(rebuilt)) != canonical_json_bytes(dict(selection)):
        raise SelectionValidationFailedError(
            f"{ERROR_SELECTION_VALIDATION_FAILED}：产物与按当前上游重推导的结果不一致。"
        )


def _compute_result_id(selection: Mapping[str, Any]) -> str:
    return RESULT_ID_PREFIX + canonical_json_sha256(dict(selection))[:12]


def _build_manifest(
    selection: Mapping[str, Any],
    *,
    result_id: str,
    cache_sha256: str,
    selection_bytes: bytes,
) -> dict[str, Any]:
    source_plan = selection["source_plan"]
    source_timeline = selection["source_timeline"]
    source_frames = selection["source_frame_set"]
    source_pc = selection["source_page_change_set"]
    source_ocr = selection["source_ocr_set"]
    return {
        "schema_name": MANIFEST_SCHEMA_NAME,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "task_id": selection["task_id"],
        "result_id": result_id,
        "status": "success",
        "cache_identity_sha256": cache_sha256,
        "inputs": {
            "plan_result_id": source_plan["result_id"],
            "plan_manifest_sha256": source_plan["manifest_sha256"],
            "timeline_id": source_timeline["timeline_id"],
            "timeline_manifest_sha256": source_timeline["manifest_sha256"],
            "frame_set_id": source_frames["frame_set_id"],
            "frame_set_manifest_sha256": source_frames["manifest_sha256"],
            "page_change_set_id": source_pc["page_change_set_id"] if source_pc is not None else None,
            "page_change_set_manifest_sha256": (
                source_pc["manifest_sha256"] if source_pc is not None else None
            ),
            "ocr_set_id": source_ocr["ocr_set_id"] if source_ocr is not None else None,
            "ocr_set_manifest_sha256": source_ocr["manifest_sha256"] if source_ocr is not None else None,
        },
        "versions": {
            "chapter_frames": CHAPTER_FRAMES_VERSION,
            "chapter_frame_selection_schema": SELECTION_SCHEMA_VERSION,
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "caption_template_version": CAPTION_TEMPLATE_VERSION,
            "artifact_group_manifest": MANIFEST_SCHEMA_VERSION,
        },
        "files": {
            SELECTION_FILE: {
                "bytes": len(selection_bytes),
                "sha256": _sha256_bytes(selection_bytes),
            }
        },
    }


def _validate_manifest(
    manifest: Any,
    *,
    selection: Mapping[str, Any],
    cache_sha256: str,
    expected_result_id: str,
    selection_bytes: bytes,
) -> None:
    if not isinstance(manifest, dict):
        _fail("manifest必须是JSON对象。")
    _exact_keys(manifest, _MANIFEST_KEYS, "manifest", SelectionValidationFailedError)
    if (
        manifest["schema_name"] != MANIFEST_SCHEMA_NAME
        or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION
        or manifest["artifact_type"] != ARTIFACT_TYPE
    ):
        _fail("manifest的schema/artifact_type身份无效。")
    if manifest["task_id"] != selection["task_id"]:
        _fail("manifest.task_id与选择对象不一致。")
    if manifest["result_id"] != expected_result_id:
        _fail("manifest.result_id与目标组身份不一致。")
    if manifest["status"] != "success":
        _fail("manifest.status无效。")
    if manifest["cache_identity_sha256"] != cache_sha256:
        _fail("manifest.cache_identity_sha256与本次缓存身份不一致。")
    expected_manifest = _build_manifest(
        selection,
        result_id=expected_result_id,
        cache_sha256=cache_sha256,
        selection_bytes=selection_bytes,
    )
    if manifest["inputs"] != expected_manifest["inputs"]:
        _fail("manifest.inputs与选择对象source_*不一致。")
    if manifest["versions"] != expected_manifest["versions"]:
        _fail("manifest.versions与冻结政策/模板版本不一致。")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != {SELECTION_FILE}:
        _fail("manifest.files必须恰含chapter-frame-selection.json一个文件。")
    record = files[SELECTION_FILE]
    if not isinstance(record, dict) or set(record) != {"bytes", "sha256"}:
        _fail("manifest.files条目键集无效。")
    if record["sha256"] != _sha256_bytes(selection_bytes) or record["bytes"] != len(selection_bytes):
        _fail("manifest.files记录的哈希/字节数与实际内容不一致。")


def validate_chapter_frame_selection_group(
    group_path: str | Path,
    *,
    cache_sha256: str,
    expected_result_id: str | None = None,
    require_directory_name: bool = True,
    upstream: ChapterFramesUpstream | None = None,
    publish_mode: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """组级复验：返回(selection, manifest)。

    ``upstream``给定时额外执行"按当前上游重推导逐字节比对"（发布前自校验与
    current复验共用；裁决Q3）；未给定时仅做对象自洽（结构、精确键集、重渲染、
    计数重算、缓存身份重算、manifest一致性）——供
    ``load_published_chapter_frame_selection``只读消费。

    ``publish_mode=True``＝发布前自校验路径，校验失败原样抛
    SELECTION_VALIDATION_FAILED；默认False＝既有组复读路径，校验失败映射为
    CACHE_CORRUPT（裁决Q4.7：发布前失败=SELECTION_VALIDATION_FAILED、
    篡改已发布组=复读CACHE_CORRUPT）。
    """

    try:
        return _validate_group_core(
            group_path,
            cache_sha256=cache_sha256,
            expected_result_id=expected_result_id,
            require_directory_name=require_directory_name,
            upstream=upstream,
        )
    except SelectionValidationFailedError as exc:
        if publish_mode:
            raise
        raise CacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：既有ChapterFrameSelection组校验失败：{exc}"
        ) from exc


def _validate_group_core(
    group_path: str | Path,
    *,
    cache_sha256: str,
    expected_result_id: str | None,
    require_directory_name: bool,
    upstream: ChapterFramesUpstream | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    group = Path(group_path)
    if require_directory_name:
        if not is_safe_result_id(expected_result_id if expected_result_id is not None else group.name):
            raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：目标组名称不是安全的result_id。")
        if expected_result_id is not None and group.name != expected_result_id:
            raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：目标组名称{group.name!r}与result_id不一致。")
    selection_path = group / SELECTION_FILE
    manifest_path = group / MANIFEST_FILE
    if not selection_path.is_file() or not manifest_path.is_file():
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：目标组缺少{SELECTION_FILE}或manifest.json。")
    try:
        selection_bytes = selection_path.read_bytes()
        selection = json.loads(selection_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：{SELECTION_FILE}无法读取或JSON无效。") from exc
    manifest = _read_json(manifest_path, "artifact manifest", CacheCorruptError)

    _validate_selection_document(selection)
    result_id = _compute_result_id(selection)
    if expected_result_id is not None and result_id != expected_result_id:
        raise CacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：选择对象内容哈希与目标组result_id不一致。"
        )
    if require_directory_name and group.name != result_id:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：组目录名与内容哈希不一致。")
    identity_payload = _cache_identity_from_document(selection)
    if canonical_json_sha256(identity_payload) != cache_sha256:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：按产物重算的缓存身份与本次缓存身份不一致。")
    _validate_manifest(
        manifest,
        selection=selection,
        cache_sha256=cache_sha256,
        expected_result_id=result_id,
        selection_bytes=selection_bytes,
    )
    if upstream is not None:
        _cross_check_selection_with_upstream(selection, upstream)
    return dict(selection), dict(manifest)


# ---------------------------------------------------------------------------
# 上游加载（current指针＋组文件；裁决Q2.1—Q2.4）
# ---------------------------------------------------------------------------


def _frames_domain_root(output_root: Path, task_id: str) -> Path:
    return output_root / task_id / "m2" / FRAME_MODULE_TOKEN


def _pc_domain_root(output_root: Path, task_id: str) -> Path:
    return _frames_domain_root(output_root, task_id) / PAGE_CHANGE_ROOT_DIRECTORY


def _ocr_domain_root(output_root: Path, task_id: str) -> Path:
    return output_root / task_id / "m2" / OCR_ROOT_DIRECTORY


def _load_strict_current(
    root: Path,
    *,
    artifact_type: str,
    missing_error: type[ChapterFramesError],
    corrupt_error: type[ChapterFramesError],
) -> dict[str, Any] | None:
    """page-changes/ocr域的严格current读取（本模块实现，裁决Q2.1）。

    与14-B ``_load_current``完全同判据：先``is_symlink()``再``is_file()``、
    六键精确、身份字符串、``is_safe_result_id``、group等式、
    ``artifact_group_path``、manifest逐字节哈希。
    """

    path = root / CURRENT_FILE
    if not _current_path_state(path, f"{root.name} current", corrupt_error):
        return None
    pointer = _read_json(path, f"{root.name} current", corrupt_error)
    _exact_keys(pointer, _CURRENT_KEYS, f"{root.name} current", corrupt_error)
    if (
        pointer["schema_name"] != CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != artifact_type
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise corrupt_error(f"{corrupt_error.error_code}：{root.name} current身份无效。")
    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise corrupt_error(f"{corrupt_error.error_code}：{root.name} current的组路径越出正式组目录。")
    manifest_path = group / MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise corrupt_error(f"{corrupt_error.error_code}：{root.name} current目标缺失或损坏。")
    return pointer


def _load_set_doc(
    root: Path,
    pointer: Mapping[str, Any],
    *,
    set_file: str,
    doc_error: type[ChapterFramesError],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取current指向组内的set JSON并校验manifest记录哈希。

    只要求manifest的files记录``包含``set文件（F1的files还逐项列出全部帧图片；
    本模块不读图片、不重算图片哈希——裁决Q2.6），逐项核对其中的set条目哈希。
    """

    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise doc_error(f"{doc_error.error_code}：current的组路径越出正式组目录。")
    manifest = _read_json(group / MANIFEST_FILE, "artifact manifest", doc_error)
    files = manifest.get("files")
    if not isinstance(files, dict) or set_file not in files:
        raise doc_error(f"{doc_error.error_code}：组manifest缺少{set_file}记录。")
    record = files[set_file]
    if not isinstance(record, dict) or not _sha256_hex(record.get("sha256")):
        raise doc_error(f"{doc_error.error_code}：组manifest文件记录无效。")
    set_path = group / set_file
    if not set_path.is_file():
        raise doc_error(f"{doc_error.error_code}：组内缺少{set_file}。")
    if _file_sha256(set_path) != record["sha256"]:
        raise doc_error(f"{doc_error.error_code}：{set_file}与manifest记录哈希不一致。")
    doc = _read_json(set_path, set_file, doc_error)
    return doc, manifest


def _load_upstream(output_root: Path, task_id: str) -> ChapterFramesUpstream:
    # 1) timelines：既有公开入口（裁决Q2.1）。
    try:
        timeline_pointer = load_timeline_current(output_root, task_id)
    except (TimelineBuilderV2Error, UnifiedTimelineError) as exc:
        raise InputTimelineCorruptError(f"{ERROR_INPUT_TIMELINE_CORRUPT}：{exc}") from exc
    if timeline_pointer is None:
        raise InputTimelineMissingError(
            f"{ERROR_INPUT_TIMELINE_MISSING}：任务没有已发布的Timeline 2.0 current。"
        )
    try:
        timeline = load_published_timeline(output_root, task_id)
    except (TimelineBuilderV2Error, UnifiedTimelineError) as exc:
        raise InputTimelineCorruptError(f"{ERROR_INPUT_TIMELINE_CORRUPT}：{exc}") from exc
    if timeline is None:
        raise InputTimelineMissingError(
            f"{ERROR_INPUT_TIMELINE_MISSING}：任务没有已发布的Timeline 2.0 current。"
        )

    # 2) boundaries：经既有公开入口并传入同一份timeline索引（裁决Q2.1）。
    try:
        plan_pointer = load_chapter_plan_current(output_root, task_id)
    except BoundaryPlannerV2Error as exc:
        raise InputChapterPlanCorruptError(f"{ERROR_INPUT_CHAPTER_PLAN_CORRUPT}：{exc}") from exc
    if plan_pointer is None:
        raise InputChapterPlanMissingError(
            f"{ERROR_INPUT_CHAPTER_PLAN_MISSING}：任务没有已发布的ChapterPlan 2.0 current。"
        )
    try:
        plan = load_published_chapter_plan(output_root, task_id, timeline_index=timeline)
    except BoundaryPlannerV2Error as exc:
        raise InputChapterPlanCorruptError(f"{ERROR_INPUT_CHAPTER_PLAN_CORRUPT}：{exc}") from exc
    if plan is None:
        raise InputChapterPlanMissingError(
            f"{ERROR_INPUT_CHAPTER_PLAN_MISSING}：任务没有已发布的ChapterPlan 2.0 current。"
        )

    # 3) frames：既有公开入口读取指针（F3加固），组文件由本模块读取（不重算图片哈希）。
    try:
        frame_pointer = load_frame_set_current(output_root, task_id)
    except FrameSamplerError as exc:
        raise InputFrameSetCorruptError(f"{ERROR_INPUT_FRAME_SET_CORRUPT}：{exc}") from exc
    if frame_pointer is None:
        raise InputFrameSetMissingError(
            f"{ERROR_INPUT_FRAME_SET_MISSING}：任务没有已发布的frame-set current。"
        )
    frame_doc, _frame_manifest = _load_set_doc(
        _frames_domain_root(output_root, task_id),
        frame_pointer,
        set_file="frame-set.json",
        doc_error=InputFrameSetCorruptError,
    )

    # 4) page-changes/ocr：条件消费（裁决Q2.4）——只有timeline含对应事件才读取。
    pe_present = bool(_events_of(timeline, PAGE_CHANGE_EVENT_TYPE))
    oe_present = bool(_events_of(timeline, OCR_EVENT_TYPE))
    pc_doc: dict[str, Any] | None = None
    pc_pointer: dict[str, Any] | None = None
    if pe_present:
        pc_root = _pc_domain_root(output_root, task_id)
        pc_pointer = _load_strict_current(
            pc_root,
            artifact_type="page_change_set",
            missing_error=InputPageChangeSetMissingError,
            corrupt_error=InputPageChangeSetCorruptError,
        )
        if pc_pointer is None:
            raise InputPageChangeSetMissingError(
                f"{ERROR_INPUT_PAGE_CHANGE_SET_MISSING}：Timeline含pe事件但没有page-change-set current。"
            )
        pc_doc, _ = _load_set_doc(
            pc_root,
            pc_pointer,
            set_file=PAGE_CHANGE_INDEX_FILE,
            doc_error=InputPageChangeSetCorruptError,
        )
    ocr_doc: dict[str, Any] | None = None
    ocr_pointer: dict[str, Any] | None = None
    if oe_present:
        ocr_root = _ocr_domain_root(output_root, task_id)
        ocr_pointer = _load_strict_current(
            ocr_root,
            artifact_type="ocr_set",
            missing_error=InputOcrSetMissingError,
            corrupt_error=InputOcrSetCorruptError,
        )
        if ocr_pointer is None:
            raise InputOcrSetMissingError(
                f"{ERROR_INPUT_OCR_SET_MISSING}：Timeline含oe事件但没有ocr-set current。"
            )
        ocr_doc, _ = _load_set_doc(
            ocr_root,
            ocr_pointer,
            set_file=OCR_INDEX_FILE,
            doc_error=InputOcrSetCorruptError,
        )

    return ChapterFramesUpstream(
        plan=plan,
        plan_pointer=dict(plan_pointer),
        timeline=timeline,
        timeline_pointer=dict(timeline_pointer),
        frame_doc=frame_doc,
        frame_pointer=dict(frame_pointer),
        page_change_doc=pc_doc,
        page_change_pointer=dict(pc_pointer) if pc_pointer is not None else None,
        ocr_doc=ocr_doc,
        ocr_pointer=dict(ocr_pointer) if ocr_pointer is not None else None,
    )


# ---------------------------------------------------------------------------
# 发布工程（裁决Q5.3六件套；逐字沿14-B先例）
# ---------------------------------------------------------------------------


def _own_root(output_root: Path, task_id: str) -> Path:
    return output_root / task_id / "m2" / ROOT_DIRECTORY


def _current_payload(result_id: str, manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": CURRENT_SCHEMA_NAME,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "result_id": result_id,
        "group": f"{GROUP_DIRECTORY}/{result_id}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
    }


def _load_own_current(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = root / CURRENT_FILE
    if not _current_path_state(path, "chapter-frames current", CurrentPointerCorruptError):
        return None, None
    pointer = _read_json(path, "chapter-frames current", CurrentPointerCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "chapter-frames current", CurrentPointerCorruptError)
    if (
        pointer["schema_name"] != CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：chapter-frames current身份无效。"
        )
    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：chapter-frames current的组路径越出正式组目录。"
        )
    manifest_path = group / MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：chapter-frames current目标缺失或损坏。"
        )
    return pointer, str(pointer["result_id"])


def _replace_current(root: Path, payload: Mapping[str, Any]) -> None:
    temporary = root / f".{CURRENT_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(payload))
        os.replace(temporary, root / CURRENT_FILE)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _disk_current_identity(root: Path) -> tuple[str, str] | None:
    try:
        raw = (root / CURRENT_FILE).read_bytes()
    except OSError:
        return None
    try:
        pointer = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(pointer, dict):
        return None
    result_id = pointer.get("result_id")
    manifest_sha256 = pointer.get("manifest_sha256")
    if not is_safe_result_id(result_id) or not isinstance(manifest_sha256, str):
        return None
    return str(result_id), manifest_sha256


def _may_discard_published_group(root: Path, result_id: str) -> bool:
    if not (root / CURRENT_FILE).exists():
        return True
    identity = _disk_current_identity(root)
    return identity is not None and identity[0] != result_id


@contextmanager
def _module_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    locks = m2_root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / LOCK_FILE
    payload = _json_bytes(
        {
            "schema_name": "ModuleLock",
            "schema_version": "1.0",
            "task_id": task_id,
            "module": MODULE_TOKEN,
            "pid": os.getpid(),
        }
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise ConcurrentRunError("CONCURRENT_RUN：chapter-frames锁已存在。") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        path.unlink(missing_ok=True)


def _write_failure(
    m2_root: Path,
    *,
    task_id: str,
    stage: str,
    error: BaseException,
    input_ids: Mapping[str, Any],
    cache_sha256: str | None,
    previous_current_id: str | None,
    cleanup_status: str,
    current_unchanged: bool,
) -> str:
    failure_id = (
        f"failure-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    )
    root = m2_root / "failures" / MODULE_TOKEN / failure_id
    root.mkdir(parents=True, exist_ok=False)
    code = getattr(error, "error_code", ERROR_PUBLISH_FAILED)
    message = (
        str(error)
        if isinstance(error, ChapterFramesError)
        else f"章内代表帧选择失败：{type(error).__name__}"
    )
    payload = {
        "schema_name": FAILURE_SCHEMA_NAME,
        "schema_version": FAILURE_SCHEMA_VERSION,
        "failure_id": failure_id,
        "task_id": task_id,
        "module": MODULE_TOKEN,
        "stage": stage,
        "error_code": code,
        "safe_message": message,
        "input_ids": dict(input_ids),
        "cache_identity_sha256": cache_sha256,
        "previous_current_id": previous_current_id,
        "temporary_cleanup_status": cleanup_status,
        "business_call_count": 0,
        "supplier_request_count": 0,
        "current_unchanged": current_unchanged,
        "recoverable": bool(getattr(error, "recoverable", False)),
        "next_action": str(getattr(error, "next_action", "none")),
    }
    _write_fsync(root / "failure.json", _json_bytes(payload))
    return failure_id


# ---------------------------------------------------------------------------
# 编排与公开入口（裁决Q5.1）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChapterFrameSelectionResult:
    group_path: Path
    result_id: str
    selection: dict[str, Any]
    manifest: dict[str, Any]
    chapter_count: int
    selected_frame_count: int
    chapters_without_frames: int
    cache_hit: bool
    pointer_repaired: bool


def select_chapter_frames(
    task_id: str,
    *,
    output_root: str | Path = "outputs",
    failure_injection: str | None = None,
) -> ChapterFrameSelectionResult:
    """选择章内代表帧、渲染确定性说明并发布不可变ChapterFrameSelection组。

    锁前零写入零审计（task.json、五域上游加载与纯构建都在锁外；上游类失败
    不写ModuleFailureAudit，与F1/F3/13-1/14-B前置策略一致）。本模块零模型、
    零网络、零外发，不需要预算守卫（裁决Q5.5）。
    """

    if failure_injection is not None and failure_injection not in _FAILURE_INJECTION_POINTS:
        raise ChapterFramesError("未知的代表帧失败注入点。")
    validate_task_id(task_id)
    output = Path(output_root).expanduser()
    m2_root = output / task_id / "m2"
    root = m2_root / ROOT_DIRECTORY
    groups_dir = root / GROUP_DIRECTORY

    # --- 锁前：读取与纯构建（全部失败零写入零审计） ---
    _ensure_task_identity(output, task_id)
    upstream = _load_upstream(output, task_id)
    selection = build_chapter_frame_selection(
        upstream.plan,
        upstream.timeline,
        upstream.frame_doc,
        upstream.page_change_doc,
        upstream.ocr_doc,
        task_id=task_id,
        plan_ref=upstream.plan_pointer,
        timeline_ref=upstream.timeline_pointer,
        frame_set_ref=upstream.frame_pointer,
        page_change_set_ref=upstream.page_change_pointer,
        ocr_set_ref=upstream.ocr_pointer,
    )
    _, cache_sha256 = chapter_frames_cache_identity(
        task_id=task_id,
        plan_ref=upstream.plan_pointer,
        timeline_ref=upstream.timeline_pointer,
        frame_set_ref=upstream.frame_pointer,
        page_change_set_ref=upstream.page_change_pointer,
        ocr_set_ref=upstream.ocr_pointer,
    )
    selection_bytes = canonical_json_bytes(selection)
    result_id = _compute_result_id(selection)
    manifest = _build_manifest(
        selection,
        result_id=result_id,
        cache_sha256=cache_sha256,
        selection_bytes=selection_bytes,
    )
    manifest_bytes = _json_bytes(manifest)

    stage = "prepare"
    previous_current_id: str | None = None
    initial_current_identity: tuple[str, str] | None = None
    created_group = False
    temporary: Path | None = None
    cleanup_status = "not_created"
    group = groups_dir / result_id

    with _module_lock(m2_root, task_id):
        try:
            initial_current_identity = _disk_current_identity(root)
            _inject(failure_injection, "after_inputs")

            stage = "current_validation"
            current_pointer, previous_current_id = _load_own_current(root)
            if current_pointer is not None:
                candidate = artifact_group_path(
                    root, GROUP_DIRECTORY, str(current_pointer["result_id"])
                )
                if candidate is None:
                    raise CurrentPointerCorruptError(
                        "CURRENT_POINTER_CORRUPT：chapter-frames current的组路径越界。"
                    )
                manifest_probe = _read_json(
                    candidate / MANIFEST_FILE, "artifact manifest", CacheCorruptError
                )
                if (
                    isinstance(manifest_probe.get("cache_identity_sha256"), str)
                    and manifest_probe["cache_identity_sha256"] == cache_sha256
                ):
                    try:
                        cached_selection, cached_manifest = validate_chapter_frame_selection_group(
                            candidate,
                            cache_sha256=cache_sha256,
                            upstream=upstream,
                        )
                    except ChapterFramesError as exc:
                        # 实现决定6：current复验不一致一律CACHE_CORRUPT，不重写旧组。
                        raise CacheCorruptError(
                            f"{ERROR_CACHE_CORRUPT}：current指向的既有组复验不一致：{exc}"
                        ) from exc
                    return _result_from(
                        candidate,
                        str(current_pointer["result_id"]),
                        cached_selection,
                        cached_manifest,
                        cache_hit=True,
                        pointer_repaired=False,
                    )

            stage = "group_publish"
            if group.exists():
                # 同身份既有组必须逐字节一致才复用（裁决Q5.4）。
                for name, payload in {
                    SELECTION_FILE: selection_bytes,
                    MANIFEST_FILE: manifest_bytes,
                }.items():
                    path = group / name
                    if not path.is_file() or path.read_bytes() != payload:
                        raise CacheCorruptError(
                            "CACHE_CORRUPT：同身份代表帧选择组已存在但内容与本次发布不一致。"
                        )
                reused = True
            else:
                reused = False
                groups_dir.mkdir(parents=True, exist_ok=True)
                temporary = groups_dir / f".tmp-{result_id}-{uuid.uuid4().hex}"
                temporary.mkdir(exist_ok=False)
                cleanup_status = "created"
                _write_fsync(temporary / SELECTION_FILE, selection_bytes)
                # manifest最后写（与F1/F2/12/13-1/14-B一致）。
                _write_fsync(temporary / MANIFEST_FILE, manifest_bytes)
                validate_chapter_frame_selection_group(
                    temporary,
                    cache_sha256=cache_sha256,
                    expected_result_id=result_id,
                    require_directory_name=False,
                    upstream=upstream,
                    publish_mode=True,
                )
                _inject(failure_injection, "before_group_publish")
                os.replace(temporary, group)
                created_group = True
                cleanup_status = "published"
                _inject(failure_injection, "after_group_publish")
                validate_chapter_frame_selection_group(
                    group,
                    cache_sha256=cache_sha256,
                    expected_result_id=result_id,
                    upstream=upstream,
                    publish_mode=True,
                )

            pointer_repaired = reused and previous_current_id != result_id
            if previous_current_id != result_id:
                stage = "current_publish"
                if not reused:
                    _inject(failure_injection, "before_current")
                _replace_current(root, _current_payload(result_id, manifest_bytes))
            return _result_from(
                group,
                result_id,
                selection,
                manifest,
                cache_hit=reused,
                pointer_repaired=pointer_repaired,
            )
        except BaseException as exc:
            current_identity = _disk_current_identity(root)
            if (
                result_id is not None
                and created_group
                and group.exists()
                and _may_discard_published_group(root, result_id)
            ):
                shutil.rmtree(group, ignore_errors=True)
                cleanup_status = "group_removed"
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
                cleanup_status = "temp_removed"
            try:
                _write_failure(
                    m2_root,
                    task_id=task_id,
                    stage=stage,
                    error=exc,
                    input_ids={
                        "task_id": task_id,
                        "plan_result_id": str(upstream.plan_pointer["result_id"]),
                        "timeline_id": str(upstream.timeline_pointer["result_id"]),
                        "frame_set_id": str(upstream.frame_pointer["result_id"]),
                        "page_change_set_id": (
                            str(upstream.page_change_pointer["result_id"])
                            if upstream.page_change_pointer is not None
                            else None
                        ),
                        "ocr_set_id": (
                            str(upstream.ocr_pointer["result_id"])
                            if upstream.ocr_pointer is not None
                            else None
                        ),
                        "result_id": result_id,
                    },
                    cache_sha256=cache_sha256,
                    previous_current_id=previous_current_id,
                    cleanup_status=cleanup_status,
                    current_unchanged=current_identity == initial_current_identity,
                )
            except BaseException:
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, ChapterFramesError):
                raise
            raise PublishFailedError(
                f"无法发布ChapterFrameSelection结果组：{type(exc).__name__}"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)


def _result_from(
    group: Path,
    result_id: str,
    selection: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    cache_hit: bool,
    pointer_repaired: bool,
) -> ChapterFrameSelectionResult:
    totals = selection["totals"]
    return ChapterFrameSelectionResult(
        group_path=group,
        result_id=result_id,
        selection=dict(selection),
        manifest=dict(manifest),
        chapter_count=int(totals["chapter_count"]),
        selected_frame_count=int(totals["selected_frame_count"]),
        chapters_without_frames=int(totals["chapters_without_frames"]),
        cache_hit=cache_hit,
        pointer_repaired=pointer_repaired,
    )


def load_published_chapter_frame_selection(
    output_root: str | Path, task_id: str
) -> dict[str, Any] | None:
    """只读消费入口（供14-C与复验；裁决Q1/Q5.1）：current缺失返回None。

    复验指针、组名、manifest哈希、对象结构自洽、caption逐字节重渲染与缓存
    身份重算；不做上游实时比对（该检查属于``select_chapter_frames``发布/
    缓存路径）。
    """

    validate_task_id(task_id)
    root = Path(output_root).expanduser() / task_id / "m2" / ROOT_DIRECTORY
    pointer, _ = _load_own_current(root)
    if pointer is None:
        return None
    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：chapter-frames current的组路径越界。"
        )
    manifest = _read_json(group / MANIFEST_FILE, "artifact manifest", CacheCorruptError)
    recorded = manifest.get("cache_identity_sha256")
    if not isinstance(recorded, str):
        raise CacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：组manifest缺少cache_identity_sha256记录。"
        )
    selection, _ = validate_chapter_frame_selection_group(
        group,
        cache_sha256=recorded,
    )
    return selection
