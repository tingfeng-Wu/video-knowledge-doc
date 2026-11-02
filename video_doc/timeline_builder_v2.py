"""TimelineBuilderV2：组合已发布来源并原子发布UnifiedTimeline 2.0（阶段13-1）。

本模块只读取明确列出的上游current及其manifest（P4 §四）：

- M1 ``timeline/timeline.json``：经13-0 ``project_task_legacy_timeline``只读投影
  为文本事件（不运行ASR、不扫描字幕、不改写任何M1文件）；
- ``m2/frames/current.json`` 与 ``m2/frames/page-changes/current.json``：
  存在且完整复验后生成page_change事件；缺失时frame_change记``not_run``；
- ``m2/ocr/current.json``：``--ocr off``不读取并记``disabled``；``auto``下
  存在则完整复验并按集合状态映射，不存在记``not_run``；``required``下
  必须存在且集合状态为success/empty，否则当前命令失败。

产物位于 ``m2/timelines/formal-results/<timeline_id>/``：
``unified-timeline.json``（P1规范JSON字节，即逻辑对象本身）＋ ``manifest.json``
（ArtifactGroupManifest 1.0，记录全部上游身份与cache identity）。发布顺序、
锁、current原子切换、失败审计与缓存保护与F1/F2/OCR完全一致（P4 §七）。

13-1实现决策（提交G1审查）：

1. 构造器**从不运行OCR/引擎**：``--ocr``只是对已发布OCR集合的消费请求
   （off不读取；auto消费或记not_run；required强制有效消费）。OCR执行仍归
   ``run-ocr``，编排由阶段15 ``run-m2``负责；
2. OCR集合状态→Timeline modality映射：``success→available``、
   ``empty→empty``、``disabled→disabled``、``degraded→failed``并携带集合
   reason_codes（degraded集合允许保留成功帧事件，对应校验器决策9）；
3. 上游链断裂硬失败：帧current缺失但页面变化/OCR current存在，或页面变化
   缺失但OCR存在，都按INPUT_*_MISSING/CORRUPT失败，不静默丢弃可用证据；
   仅当整条视觉链都不存在时才记not_run（文本先行合法）；
4. 视觉事件``end_ms = start_ms + 1``；``decoded_ms + 1 > duration_ms``的候选
   被跳过并记入该modality的reason_codes（``page_change_beyond_duration``/
   ``ocr_beyond_duration``），不静默丢弃；
5. ``timeline_id``即内容身份（去自身ID规范JSON前12位），正式组目录名=
   timeline_id；manifest另记``cache_identity_sha256``（builder版本、Schema
   版本与全部上游身份）。cache identity与manifest**只由内容和上游身份决定，
   不含``--ocr``请求值**：``auto``与``required``在OCR可用时产出逐字节相同的
   时间轴（同一timeline_id），若把请求值计入身份会让同一不可变组因调用策略
   不同而得到不同cache identity、误报CACHE_CORRUPT；``off``不消费OCR，其
   ``ocr_set``身份为null且内容本就不同（另一timeline_id），因此请求策略无需
   进入身份。同内容ID但manifest记录的cache identity与当前上游身份不符时按
   CACHE_CORRUPT硬失败（禁止覆盖同ID组）；
6. page_change/OCR事件ID为类型内连续编号（pe/oe加6位），排序按校验器
   冻结的``(start_ms, type优先级, id)``；文本事件编号只统计transcript，
   因此任何视觉输入变化与``--ocr``开关都不改变既有文本事件ID（P5阶段闸门）；
7. 构造器不探测源视频文件：视频身份经F1帧集索引→F2→OCR→本组的身份链
   传递，与F2/OCR不重复探测的先例一致；
8. run_status=degraded（OCR degraded等）且主文本available时正常发布并切换
   current（P2 §七表第3行）；主文本不可用不发布——本版本主文本恒为legacy
   投影，不可用即投影失败，不进入发布路径。
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
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from video_doc.frame_sampler import (
    FRAME_GROUP_DIRECTORY,
    FRAME_IMAGE_DIRECTORY,
    FRAME_INDEX_FILE,
    FRAME_MANIFEST_FILE,
    FRAME_MODULE_TOKEN,
    FRAME_SET_MANIFEST_SCHEMA_NAME,
    FRAME_SET_MANIFEST_SCHEMA_VERSION,
    FRAME_SET_SCHEMA_NAME,
    FRAME_SET_SCHEMA_VERSION,
    FrameSamplerError,
    artifact_group_path,
    is_safe_result_id,
    load_frame_set_current,
)
from video_doc.ocr_extractor import (
    OCR_ARTIFACT_TYPE,
    OCR_CURRENT_FILE,
    OCR_CURRENT_SCHEMA_NAME,
    OCR_CURRENT_SCHEMA_VERSION,
    OCR_GROUP_DIRECTORY,
    OCR_INDEX_FILE,
    OCR_MANIFEST_FILE,
    OCR_ROOT_DIRECTORY,
    OcrExtractorError,
    OcrInputContext,
    resolve_ocr_profile,
    validate_ocr_group,
)
from video_doc.page_change_detector import (
    PAGE_CHANGE_ARTIFACT_TYPE,
    PAGE_CHANGE_CURRENT_FILE,
    PAGE_CHANGE_CURRENT_SCHEMA_NAME,
    PAGE_CHANGE_CURRENT_SCHEMA_VERSION,
    PAGE_CHANGE_GROUP_DIRECTORY,
    PAGE_CHANGE_INDEX_FILE,
    PAGE_CHANGE_MANIFEST_FILE,
    PAGE_CHANGE_ROOT_DIRECTORY,
    FrameSetInput,
    PageChangeDetectorError,
    resolve_page_change_profile,
    validate_page_change_group,
)
from video_doc.task_workspace import validate_task_id
from video_doc.unified_timeline import (
    EVENT_TYPE_PRIORITY,
    OCR_EVENT_PAYLOAD_SCHEMA,
    OCR_EVENT_TYPE,
    PAGE_CHANGE_EVENT_PAYLOAD_SCHEMA,
    PAGE_CHANGE_EVENT_TYPE,
    TRANSCRIPT_EVENT_TYPE,
    UnifiedTimelineError,
    canonical_json_bytes,
    compute_timeline_id,
    derive_run_outcome,
    project_task_legacy_timeline,
    validate_unified_timeline,
)

TIMELINE_BUILDER_VERSION = "1.0"
TIMELINE_ARTIFACT_TYPE = "unified_timeline"
TIMELINE_MODULE_TOKEN = "timelines"
TIMELINE_ROOT_DIRECTORY = "timelines"
TIMELINE_GROUP_DIRECTORY = "formal-results"
TIMELINE_INDEX_FILE = "unified-timeline.json"
TIMELINE_MANIFEST_FILE = "manifest.json"
TIMELINE_CURRENT_FILE = "current.json"
TIMELINE_LOCK_FILE = "timelines.lock"
TIMELINE_MANIFEST_SCHEMA_NAME = "ArtifactGroupManifest"
TIMELINE_MANIFEST_SCHEMA_VERSION = "1.0"
TIMELINE_CURRENT_SCHEMA_NAME = "ArtifactCurrentPointer"
TIMELINE_CURRENT_SCHEMA_VERSION = "1.0"
TIMELINE_FAILURE_SCHEMA_NAME = "ModuleFailureAudit"
TIMELINE_FAILURE_SCHEMA_VERSION = "1.0"
TIMELINE_CACHE_SCHEMA_NAME = "TimelineBuilderV2CacheIdentity"
TIMELINE_CACHE_SCHEMA_VERSION = "1.0"

OCR_REQUESTS = ("off", "auto", "required")

ERROR_TIMELINE_BUILD_FAILED = "TIMELINE_BUILD_FAILED"
ERROR_INPUT_ASR_TIMELINE_MISSING = "INPUT_ASR_TIMELINE_MISSING"
ERROR_INPUT_ASR_TIMELINE_CORRUPT = "INPUT_ASR_TIMELINE_CORRUPT"
ERROR_INPUT_FRAME_SET_MISSING = "INPUT_FRAME_SET_MISSING"
ERROR_INPUT_FRAME_SET_CORRUPT = "INPUT_FRAME_SET_CORRUPT"
ERROR_INPUT_PAGE_CHANGE_SET_MISSING = "INPUT_PAGE_CHANGE_SET_MISSING"
ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT = "INPUT_PAGE_CHANGE_SET_CORRUPT"
ERROR_INPUT_OCR_SET_MISSING = "INPUT_OCR_SET_MISSING"
ERROR_INPUT_OCR_SET_CORRUPT = "INPUT_OCR_SET_CORRUPT"
ERROR_REQUIRED_OCR_UNAVAILABLE = "REQUIRED_OCR_UNAVAILABLE"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"

_FAILURE_INJECTION_POINTS = (
    "after_inputs",
    "after_index",
    "after_manifest",
    "before_group_publish",
    "after_group_publish",
    "before_current",
)

_CURRENT_KEYS = {
    "schema_name", "schema_version", "artifact_type", "result_id", "group",
    "manifest_sha256",
}
_FRAME_INDEX_KEYS = {
    "schema_name", "schema_version", "status", "frame_set_id", "task_id",
    "profile_id", "profile", "source_video", "sampling", "cache_identity_sha256",
    "frame_count", "frames",
}
_FRAME_KEYS = {
    "frame_id", "ordinal", "role", "planned_ms", "requested_ms", "decoded_ms",
    "is_first", "is_last", "file", "sha256", "size_bytes",
}
_FRAME_MANIFEST_KEYS = {
    "schema_name", "schema_version", "artifact_type", "task_id", "result_id",
    "status", "cache_identity_sha256", "inputs", "versions", "frame_count", "files",
}
_TIMELINE_MANIFEST_KEYS = {
    "schema_name", "schema_version", "artifact_type", "task_id", "result_id",
    "status", "cache_identity_sha256", "inputs", "versions", "event_counts", "files",
}


class TimelineBuilderV2Error(RuntimeError):
    error_code = ERROR_TIMELINE_BUILD_FAILED
    recoverable = False
    next_action = "none"


class InputAsrTimelineMissingError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_ASR_TIMELINE_MISSING
    recoverable = True
    next_action = "run_m1_pipeline"


class InputAsrTimelineCorruptError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_ASR_TIMELINE_CORRUPT
    recoverable = False
    next_action = "none"


class InputFrameSetMissingError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_FRAME_SET_MISSING
    recoverable = True
    next_action = "run_sample_frames"


class InputFrameSetCorruptError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_FRAME_SET_CORRUPT
    recoverable = False
    next_action = "none"


class InputPageChangeSetMissingError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_PAGE_CHANGE_SET_MISSING
    recoverable = True
    next_action = "run_detect_page_changes"


class InputPageChangeSetCorruptError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT
    recoverable = False
    next_action = "none"


class InputOcrSetMissingError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_OCR_SET_MISSING
    recoverable = True
    next_action = "run_ocr"


class RequiredOcrUnavailableError(TimelineBuilderV2Error):
    error_code = ERROR_REQUIRED_OCR_UNAVAILABLE
    recoverable = True
    next_action = "run_ocr"


class InputOcrSetCorruptError(TimelineBuilderV2Error):
    error_code = ERROR_INPUT_OCR_SET_CORRUPT
    recoverable = False
    next_action = "none"


class TimelineCacheCorruptError(TimelineBuilderV2Error):
    error_code = ERROR_CACHE_CORRUPT
    recoverable = False
    next_action = "none"


class TimelineCurrentPointerCorruptError(TimelineBuilderV2Error):
    error_code = ERROR_CURRENT_POINTER_CORRUPT
    recoverable = False
    next_action = "none"


class TimelineConcurrentRunError(TimelineBuilderV2Error):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "retry_after_other_run_finishes"


class TimelinePublishFailedError(TimelineBuilderV2Error):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = True
    next_action = "retry_or_inspect_failures"


class InjectedTimelineFailure(TimelineBuilderV2Error):
    recoverable = True
    next_action = "none"


@dataclass(frozen=True)
class VisualInputs:
    """已复验的帧集与页面变化输入；整链缺失时为None字段（not_run）。"""

    frame_set: FrameSetInput | None
    frames_by_id: Mapping[str, Mapping[str, Any]] | None
    page_change_set_id: str | None
    page_change_manifest_sha256: str | None
    page_change_profile_id: str | None
    page_change_status: str | None
    candidates: tuple[Mapping[str, Any], ...] | None
    width: int | None
    height: int | None


@dataclass(frozen=True)
class OcrConsumed:
    """已完整复验的OCR集合与其记录身份。"""

    ocr_set_id: str
    manifest_sha256: str
    mode: str
    profile_id: str
    status: str
    reason_codes: tuple[str, ...]
    frames: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class TimelineGroup:
    group_path: Path
    index_path: Path
    manifest_path: Path
    index: dict[str, Any]
    manifest: dict[str, Any]
    cache_hit: bool
    pointer_repaired: bool


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _frame_json_bytes(payload: Any) -> bytes:
    """F1 FrameSet 1.0冻结的带缩进序列化。"""

    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str, error_type: type[TimelineBuilderV2Error]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise error_type(f"{label}无法读取或JSON无效。") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label}必须是JSON对象。")
    return value


def _current_path_state(path: Path, label: str, error_type: type[TimelineBuilderV2Error]) -> bool:
    """current路径是普通文件时返回True，不存在时返回False。

    与F1/F2/OCR相同：先判``is_symlink()``；活符号链接、断链、目录及其它
    非普通文件一律硬失败，只有确实不存在才视为“无current”。
    """

    if path.is_symlink():
        raise error_type(f"{label}存在但不是普通文件。")
    if path.is_file():
        return True
    if path.exists():
        raise error_type(f"{label}存在但不是普通文件。")
    return False


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str, error_type: type[TimelineBuilderV2Error]) -> None:
    if set(value) != expected:
        raise error_type(f"{label}字段集合无效。")


def _safe_relative_file(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


# ---------------------------------------------------------------------------
# 输入加载（只读、显式current、完整复验）
# ---------------------------------------------------------------------------


def _load_transcript(output_root: Path, task_id: str) -> dict[str, Any]:
    timeline_path = output_root / task_id / "timeline" / "timeline.json"
    if not timeline_path.is_file():
        raise InputAsrTimelineMissingError(
            "INPUT_ASR_TIMELINE_MISSING：M1正式时间轴不存在。"
        )
    try:
        return project_task_legacy_timeline(output_root, task_id)
    except UnifiedTimelineError as exc:
        raise InputAsrTimelineCorruptError(
            f"INPUT_ASR_TIMELINE_CORRUPT：{exc}"
        ) from exc


def _load_frame_set(output_root: Path, task_id: str) -> FrameSetInput | None:
    """加载并复验帧集；current不存在返回None（视觉链未运行）。"""

    frames_root = output_root / task_id / "m2" / FRAME_MODULE_TOKEN
    try:
        pointer = load_frame_set_current(output_root, task_id)
    except FrameSamplerError as exc:
        raise InputFrameSetCorruptError(f"INPUT_FRAME_SET_CORRUPT：{exc}") from exc
    if pointer is None:
        return None
    result_id = str(pointer["result_id"])
    group = artifact_group_path(frames_root, FRAME_GROUP_DIRECTORY, result_id)
    if group is None or not group.is_dir():
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：frames current指向的组缺失或越界。")
    entries = {entry.name for entry in group.iterdir()}
    if entries != {FRAME_INDEX_FILE, FRAME_MANIFEST_FILE, FRAME_IMAGE_DIRECTORY}:
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧集成员集合无效。")
    manifest_path = group / FRAME_MANIFEST_FILE
    manifest_bytes = manifest_path.read_bytes()
    if _sha256_bytes(manifest_bytes) != pointer["manifest_sha256"]:
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：frames manifest哈希不匹配。")
    manifest = _read_json(manifest_path, "frames manifest", InputFrameSetCorruptError)
    index_path = group / FRAME_INDEX_FILE
    index = _read_json(index_path, "frame set", InputFrameSetCorruptError)
    _exact_keys(index, _FRAME_INDEX_KEYS, "frame set", InputFrameSetCorruptError)
    _exact_keys(manifest, _FRAME_MANIFEST_KEYS, "frames manifest", InputFrameSetCorruptError)
    if (
        index["schema_name"] != FRAME_SET_SCHEMA_NAME
        or index["schema_version"] != FRAME_SET_SCHEMA_VERSION
        or index["status"] != "success"
        or index["frame_set_id"] != result_id
        or index["task_id"] != task_id
        or manifest["schema_name"] != FRAME_SET_MANIFEST_SCHEMA_NAME
        or manifest["schema_version"] != FRAME_SET_MANIFEST_SCHEMA_VERSION
        or manifest["artifact_type"] != "frame_set"
        or manifest["result_id"] != result_id
        or manifest["task_id"] != task_id
    ):
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧集Schema或来源身份无效。")
    if index_path.read_bytes() != _frame_json_bytes(index) or manifest_bytes != _frame_json_bytes(manifest):
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧集JSON序列化不确定。")
    frames = index.get("frames")
    if not isinstance(frames, list) or index.get("frame_count") != len(frames) or len(frames) < 1:
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧数量无效。")
    source = index.get("source_video")
    if not isinstance(source, dict):
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：source_video无效。")
    width = source.get("width")
    height = source.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：源分辨率无效。")
    previous_decoded_ms = -1
    file_names: set[str] = set()
    for ordinal, frame in enumerate(frames, start=1):
        if not isinstance(frame, dict):
            raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧条目无效。")
        _exact_keys(frame, _FRAME_KEYS, "帧条目", InputFrameSetCorruptError)
        expected_file = f"{FRAME_IMAGE_DIRECTORY}/fr{ordinal:06d}.png"
        if (
            frame["ordinal"] != ordinal
            or frame["frame_id"] != f"fr{ordinal:06d}"
            or frame["file"] != expected_file
            or not _safe_relative_file(frame["file"])
            or not isinstance(frame["decoded_ms"], int)
            or frame["decoded_ms"] <= previous_decoded_ms
        ):
            raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧身份、路径或时间顺序无效。")
        file_names.add(str(frame["file"]))
        previous_decoded_ms = frame["decoded_ms"]
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict) or set(manifest_files) != file_names | {FRAME_INDEX_FILE}:
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧集manifest文件清单无效。")
    for frame in frames:
        recorded = manifest_files.get(str(frame["file"]))
        if (
            not isinstance(recorded, dict)
            or recorded.get("sha256") != frame["sha256"]
            or recorded.get("bytes") != frame["size_bytes"]
        ):
            raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：manifest与帧条目身份不一致。")
    return FrameSetInput(frames_root, group, index, manifest, str(pointer["manifest_sha256"]))


def _load_page_change(frame_set: FrameSetInput) -> tuple[dict[str, Any], str, str]:
    """复验页面变化current与结果组，返回(index, manifest_sha256, profile_id)。"""

    root = frame_set.root / PAGE_CHANGE_ROOT_DIRECTORY
    path = root / PAGE_CHANGE_CURRENT_FILE
    if not _current_path_state(path, "page changes current", InputPageChangeSetCorruptError):
        raise InputPageChangeSetMissingError(
            "INPUT_PAGE_CHANGE_SET_MISSING：page changes current不存在。"
        )
    pointer = _read_json(path, "page changes current", InputPageChangeSetCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "page changes current", InputPageChangeSetCorruptError)
    if (
        pointer["schema_name"] != PAGE_CHANGE_CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != PAGE_CHANGE_CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != PAGE_CHANGE_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{PAGE_CHANGE_GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise InputPageChangeSetCorruptError("INPUT_PAGE_CHANGE_SET_CORRUPT：page changes current身份无效。")
    group = artifact_group_path(root, PAGE_CHANGE_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise InputPageChangeSetCorruptError("INPUT_PAGE_CHANGE_SET_CORRUPT：page changes current的组路径越界。")
    manifest_path = group / PAGE_CHANGE_MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise InputPageChangeSetCorruptError("INPUT_PAGE_CHANGE_SET_CORRUPT：page changes current目标缺失或损坏。")
    profile_id = _read_json(group / PAGE_CHANGE_INDEX_FILE, "page change set", InputPageChangeSetCorruptError).get("profile_id")
    if not isinstance(profile_id, str):
        raise InputPageChangeSetCorruptError("INPUT_PAGE_CHANGE_SET_CORRUPT：页面变化profile无效。")
    try:
        profile = resolve_page_change_profile(profile_id)
        validated = validate_page_change_group(group, frame_set=frame_set, profile=profile)
    except PageChangeDetectorError as exc:
        raise InputPageChangeSetCorruptError(f"INPUT_PAGE_CHANGE_SET_CORRUPT：{exc}") from exc
    return validated.index, str(pointer["manifest_sha256"]), profile_id


def _load_visual(output_root: Path, task_id: str, ocr_request: str) -> VisualInputs:
    """加载帧集＋页面变化；整链缺失返回全None（frame_change记not_run）。

    决策3：帧集存在但页面变化缺失、或页面变化存在而帧集缺失时硬失败，
    只有两者都不存在才视为“尚未执行”。OCR current的链完整性只在将要消费
    OCR（``--ocr`` 非off）时检查——off不读取OCR，悬空OCR current不阻断文本构建。
    """

    check_ocr_chain = ocr_request != "off"
    frame_set = _load_frame_set(output_root, task_id)
    m2_root = output_root / task_id / "m2"
    ocr_current = m2_root / OCR_ROOT_DIRECTORY / OCR_CURRENT_FILE
    if frame_set is None:
        pc_path = (
            m2_root / FRAME_MODULE_TOKEN / PAGE_CHANGE_ROOT_DIRECTORY / PAGE_CHANGE_CURRENT_FILE
        )
        if _current_path_state(pc_path, "page changes current", InputPageChangeSetCorruptError):
            raise InputFrameSetMissingError(
                "INPUT_FRAME_SET_MISSING：页面变化current存在但帧集current不存在。"
            )
        if check_ocr_chain and _current_path_state(
            ocr_current, "ocr current", InputOcrSetCorruptError
        ):
            raise InputFrameSetMissingError(
                "INPUT_FRAME_SET_MISSING：OCR current存在但帧集current不存在。"
            )
        return VisualInputs(None, None, None, None, None, None, None, None, None)
    try:
        pc_index, pc_manifest_sha, pc_profile_id = _load_page_change(frame_set)
    except InputPageChangeSetMissingError:
        if check_ocr_chain and _current_path_state(
            ocr_current, "ocr current", InputOcrSetCorruptError
        ):
            raise InputPageChangeSetMissingError(
                "INPUT_PAGE_CHANGE_SET_MISSING：OCR current存在但页面变化current不存在。"
            )
        return VisualInputs(
            frame_set,
            {str(frame["frame_id"]): frame for frame in frame_set.index["frames"]},
            None,
            None,
            None,
            None,
            None,
            int(frame_set.index["source_video"]["width"]),
            int(frame_set.index["source_video"]["height"]),
        )
    candidates = tuple(pc_index["candidates"])
    return VisualInputs(
        frame_set,
        {str(frame["frame_id"]): frame for frame in frame_set.index["frames"]},
        str(pc_index["page_change_set_id"]),
        pc_manifest_sha,
        pc_profile_id,
        str(pc_index["status"]),
        candidates,
        int(frame_set.index["source_video"]["width"]),
        int(frame_set.index["source_video"]["height"]),
    )


def _load_ocr(
    output_root: Path,
    task_id: str,
    visual: VisualInputs,
) -> OcrConsumed | None:
    """复验并消费OCR current指向的集合；不存在返回None（not_run）。"""

    root = output_root / task_id / "m2" / OCR_ROOT_DIRECTORY
    if not _current_path_state(
        root / OCR_CURRENT_FILE, "ocr current", InputOcrSetCorruptError
    ):
        return None
    pointer = _read_json(root / OCR_CURRENT_FILE, "ocr current", InputOcrSetCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "ocr current", InputOcrSetCorruptError)
    if (
        pointer["schema_name"] != OCR_CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != OCR_CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != OCR_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{OCR_GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise InputOcrSetCorruptError("INPUT_OCR_SET_CORRUPT：ocr current身份无效。")
    group = artifact_group_path(root, OCR_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise InputOcrSetCorruptError("INPUT_OCR_SET_CORRUPT：ocr current的组路径越界。")
    manifest_path = group / OCR_MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise InputOcrSetCorruptError("INPUT_OCR_SET_CORRUPT：ocr current目标缺失或损坏。")
    probe = _read_json(group / OCR_INDEX_FILE, "ocr set", InputOcrSetCorruptError)
    profile_id = probe.get("profile_id")
    mode = probe.get("mode")
    engine = probe.get("engine")
    if not isinstance(profile_id, str) or not isinstance(mode, str) or not isinstance(engine, dict):
        raise InputOcrSetCorruptError("INPUT_OCR_SET_CORRUPT：OCR集合记录的模式或引擎无效。")
    assert visual.frame_set is not None and visual.frames_by_id is not None  # 上游决策3保证
    try:
        profile = resolve_ocr_profile(profile_id)
        context = OcrInputContext(
            task_id=task_id,
            frame_set=visual.frame_set,
            frames_by_id=visual.frames_by_id,
            candidates=visual.candidates or (),
            page_change_set_id=str(visual.page_change_set_id),
            page_change_manifest_sha256=str(visual.page_change_manifest_sha256),
            page_change_profile_id=str(visual.page_change_profile_id),
            width=int(visual.width),
            height=int(visual.height),
        )
        validated = validate_ocr_group(
            group,
            context=context,
            profile=profile,
            mode=mode,
            engine_binding=engine,
        )
    except (OcrExtractorError, PageChangeDetectorError) as exc:
        raise InputOcrSetCorruptError(f"INPUT_OCR_SET_CORRUPT：{exc}") from exc
    index = validated.index
    reason_codes = index.get("reason_codes")
    if not isinstance(reason_codes, list) or not all(
        isinstance(code, str) for code in reason_codes
    ):
        raise InputOcrSetCorruptError("INPUT_OCR_SET_CORRUPT：OCR集合reason_codes无效。")
    return OcrConsumed(
        ocr_set_id=str(index["ocr_set_id"]),
        manifest_sha256=str(pointer["manifest_sha256"]),
        mode=mode,
        profile_id=profile_id,
        status=str(index["status"]),
        reason_codes=tuple(reason_codes),
        frames=tuple(index["frames"]),
    )


# ---------------------------------------------------------------------------
# 事件构造与逻辑对象组装
# ---------------------------------------------------------------------------


def _ocr_modality_for(
    consumed: OcrConsumed | None, ocr_request: str
) -> tuple[str, tuple[str, ...]]:
    """决策1/2：--ocr请求值与已发布集合状态映射到Timeline ocr modality。"""

    if ocr_request == "off":
        return "disabled", ("NOT_ENABLED",)
    if consumed is None:
        return "not_run", ()
    if consumed.status == "success":
        return "available", consumed.reason_codes
    if consumed.status == "empty":
        reason = consumed.reason_codes or ("no_text_detected",)
        return "empty", reason
    if consumed.status == "disabled":
        reason = consumed.reason_codes or ("engine_disabled",)
        return "disabled", reason
    reason = consumed.reason_codes or ("ocr_degraded_frames",)
    return "failed", reason


def _build_page_change_events(
    visual: VisualInputs, duration_ms: int
) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    reasons: list[str] = []
    assert visual.candidates is not None
    for candidate in visual.candidates:
        start_ms = int(candidate["decoded_ms"])
        if start_ms + 1 > duration_ms:
            if "page_change_beyond_duration" not in reasons:
                reasons.append("page_change_beyond_duration")
            continue
        events.append(
            {
                "id": f"pe{len(events) + 1:06d}",
                "type": PAGE_CHANGE_EVENT_TYPE,
                "source_kind": PAGE_CHANGE_EVENT_TYPE,
                "source_segment_id": f"pc:{candidate['candidate_id']}",
                "start_ms": start_ms,
                "end_ms": start_ms + 1,
                "payload_schema": PAGE_CHANGE_EVENT_PAYLOAD_SCHEMA,
                "payload": {
                    "frame_id": str(candidate["frame_id"]),
                    "previous_frame_id": str(candidate["previous_frame_id"]),
                    "candidate_id": str(candidate["candidate_id"]),
                    "change_strength": candidate["change_strength"],
                    "perceptual_distance": candidate["perceptual_distance"],
                },
                "legacy_event_id": None,
            }
        )
    return events, reasons


def _build_ocr_events(
    consumed: OcrConsumed, duration_ms: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """决策4：ok、非duplicate且有canonical_text的帧条目生成oe事件。

    直接消费OCR集合已复验的``canonical_text``与``text_sha256``（二者由
    ``validate_ocr_group``保证逐字节一致），不在构造器内重新归一化，避免
    事件文本与冻结OCR产物出现任何漂移。
    """

    events: list[dict[str, Any]] = []
    reasons: list[str] = []
    for frame in consumed.frames:
        if frame["status"] != "ok" or frame["duplicate_of"] is not None:
            continue
        text = frame["canonical_text"]
        if not isinstance(text, str) or not text.strip():
            if "ocr_frame_text_blank" not in reasons:
                reasons.append("ocr_frame_text_blank")
            continue
        start_ms = int(frame["decoded_ms"])
        if start_ms + 1 > duration_ms:
            if "ocr_beyond_duration" not in reasons:
                reasons.append("ocr_beyond_duration")
            continue
        events.append(
            {
                "id": f"oe{len(events) + 1:06d}",
                "type": OCR_EVENT_TYPE,
                "source_kind": OCR_EVENT_TYPE,
                "source_segment_id": f"oc:{frame['frame_id']}",
                "start_ms": start_ms,
                "end_ms": start_ms + 1,
                "payload_schema": OCR_EVENT_PAYLOAD_SCHEMA,
                "payload": {
                    "frame_id": str(frame["frame_id"]),
                    "candidate_id": str(frame["candidate_id"]),
                    "text": text,
                    "text_sha256": str(frame["text_sha256"]),
                    "line_count": int(frame["line_count"]),
                },
                "legacy_event_id": None,
            }
        )
    return events, reasons


def _merge_events(
    text_events: Sequence[dict[str, Any]],
    page_events: Sequence[dict[str, Any]],
    ocr_events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按校验器冻结的(start_ms, type优先级, id)合并；各类型内部已按时间升序。"""

    merged = sorted(
        list(text_events) + list(page_events) + list(ocr_events),
        key=lambda event: (
            event["start_ms"],
            EVENT_TYPE_PRIORITY[event["type"]],
            event["id"],
        ),
    )
    return merged


def timeline_cache_identity(
    *,
    transcript_sha256: str,
    visual: VisualInputs,
    consumed: OcrConsumed | None,
) -> tuple[dict[str, Any], str]:
    payload = {
        "schema_name": TIMELINE_CACHE_SCHEMA_NAME,
        "schema_version": TIMELINE_CACHE_SCHEMA_VERSION,
        "builder_version": TIMELINE_BUILDER_VERSION,
        "unified_timeline_schema_version": "2.0",
        "transcript": {"legacy_timeline_sha256": transcript_sha256},
        "frame_set": (
            None
            if visual.frame_set is None
            else {
                "frame_set_id": visual.frame_set.index["frame_set_id"],
                "manifest_sha256": visual.frame_set.manifest_sha256,
            }
        ),
        "page_change_set": (
            None
            if visual.page_change_set_id is None
            else {
                "page_change_set_id": visual.page_change_set_id,
                "manifest_sha256": visual.page_change_manifest_sha256,
            }
        ),
        "ocr_set": (
            None
            if consumed is None
            else {
                "ocr_set_id": consumed.ocr_set_id,
                "manifest_sha256": consumed.manifest_sha256,
                "mode": consumed.mode,
                "profile_id": consumed.profile_id,
            }
        ),
    }
    return payload, _sha256_bytes(_json_bytes(payload))


def build_timeline_payload(
    *,
    task_id: str,
    transcript: dict[str, Any],
    visual: VisualInputs,
    consumed: OcrConsumed | None,
    ocr_request: str,
) -> dict[str, Any]:
    """把已复验输入组装为UnifiedTimeline 2.0逻辑对象（不写盘）。"""

    duration_ms = int(transcript["duration_ms"])
    page_events: list[dict[str, Any]] = []
    page_reasons: list[str] = []
    if visual.page_change_set_id is not None and visual.candidates:
        page_events, page_reasons = _build_page_change_events(visual, duration_ms)
    # frame_change modality反映页面变化检测阶段：未运行(page_change集合缺失)→
    # not_run；集合存在但无事件产出(合法空集或候选全部越过时长)→empty；否则available。
    if visual.page_change_set_id is None:
        frame_change: tuple[str, tuple[str, ...]] = ("not_run", ())
    elif page_events:
        frame_change = ("available", tuple(page_reasons))
    else:
        reason = tuple(page_reasons) if page_reasons else ("no_page_change_candidates",)
        frame_change = ("empty", reason)

    ocr_events: list[dict[str, Any]] = []
    ocr_drop_reasons: tuple[str, ...] = ()
    if consumed is not None and consumed.status in ("success", "degraded"):
        built, drop_reasons = _build_ocr_events(consumed, duration_ms)
        ocr_events, ocr_drop_reasons = built, tuple(drop_reasons)
    ocr_status, ocr_reason = _ocr_modality_for(consumed, ocr_request)
    if ocr_status == "available" and not ocr_events:
        # available但零事件（成功帧文本全部空白或越过时长）→ 降级为empty。
        ocr_status = "empty"
        ocr_reason = tuple(dict.fromkeys([*ocr_reason, *ocr_drop_reasons])) or (
            "no_ocr_text_events",
        )
    elif ocr_status in ("empty", "disabled", "not_run") and ocr_events:
        raise TimelineBuilderV2Error("内部不一致：该OCR状态不得产生事件。")
    elif ocr_status in ("available", "failed"):
        # available/failed产出部分事件时，仍并入被跳过帧的reason（与frame_change一致，
        # 不静默丢弃越时长/空文本帧的可观测证据）。
        ocr_reason = tuple(dict.fromkeys([*ocr_reason, *ocr_drop_reasons]))

    modalities = {
        "transcript": {"status": "available", "reason_codes": []},
        "frame_change": {
            "status": frame_change[0],
            "reason_codes": list(frame_change[1]),
        },
        "ocr": {"status": ocr_status, "reason_codes": list(ocr_reason)},
    }
    try:
        run_status, quality_level = derive_run_outcome(modalities)
        merged = _merge_events(transcript["events"], page_events, ocr_events)
        body: dict[str, Any] = {
            "schema_name": "UnifiedTimeline",
            "schema_version": "2.0",
            "task_id": task_id,
            "duration_ms": duration_ms,
            "language": transcript["language"],
            "primary_transcript": transcript["primary_transcript"],
            "modalities": modalities,
            "run_status": run_status,
            "quality_level": quality_level,
            "events": merged,
        }
        body["timeline_id"] = compute_timeline_id(body)
        validate_unified_timeline(body, expected_task_id=task_id)
    except UnifiedTimelineError as exc:
        # 构造器自检失败属实现级错误，统一收敛为TIMELINE_BUILD_FAILED，
        # 使CLI始终输出类型化JSON摘要而非裸traceback。
        raise TimelineBuilderV2Error(f"TIMELINE_BUILD_FAILED：{exc}") from exc
    return body


def _count_events(payload: Mapping[str, Any]) -> dict[str, int]:
    counts = {"total": len(payload["events"]), TRANSCRIPT_EVENT_TYPE: 0, PAGE_CHANGE_EVENT_TYPE: 0, OCR_EVENT_TYPE: 0}
    for event in payload["events"]:
        counts[event["type"]] += 1
    return counts


def _build_manifest(
    payload: Mapping[str, Any],
    *,
    visual: VisualInputs,
    consumed: OcrConsumed | None,
    cache_sha256: str,
) -> dict[str, Any]:
    index_bytes = canonical_json_bytes(payload)
    return {
        "schema_name": TIMELINE_MANIFEST_SCHEMA_NAME,
        "schema_version": TIMELINE_MANIFEST_SCHEMA_VERSION,
        "artifact_type": TIMELINE_ARTIFACT_TYPE,
        "task_id": payload["task_id"],
        "result_id": payload["timeline_id"],
        "status": payload["run_status"],
        "cache_identity_sha256": cache_sha256,
        "inputs": {
            "input_mode": payload["primary_transcript"]["input_mode"],
            "legacy_timeline_sha256": payload["primary_transcript"]["legacy_timeline_sha256"],
            "frame_set_id": None if visual.frame_set is None else visual.frame_set.index["frame_set_id"],
            "frame_set_manifest_sha256": None if visual.frame_set is None else visual.frame_set.manifest_sha256,
            "page_change_set_id": visual.page_change_set_id,
            "page_change_set_manifest_sha256": visual.page_change_manifest_sha256,
            "page_change_profile_id": visual.page_change_profile_id,
            "ocr_set_id": None if consumed is None else consumed.ocr_set_id,
            "ocr_set_manifest_sha256": None if consumed is None else consumed.manifest_sha256,
        },
        "versions": {
            "timeline_builder_v2": TIMELINE_BUILDER_VERSION,
            "unified_timeline_schema": "2.0",
            "artifact_group_manifest": TIMELINE_MANIFEST_SCHEMA_VERSION,
        },
        "event_counts": _count_events(payload),
        "files": {
            TIMELINE_INDEX_FILE: {
                "sha256": _sha256_bytes(index_bytes),
                "bytes": len(index_bytes),
            }
        },
    }


# ---------------------------------------------------------------------------
# 结果组复验、current与锁
# ---------------------------------------------------------------------------


def validate_timeline_group_structure(
    group_path: str | Path,
    *,
    require_directory_name: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """复验已发布Timeline 2.0组的自洽性（成员、manifest、序列化、ID、Schema）。

    不比对实时上游（供``load_published_timeline``使用）；构造路径与缓存命中
    使用``validate_timeline_group``做含输入身份的完整复验。
    """

    group = Path(group_path)
    if not group.is_dir() or {entry.name for entry in group.iterdir()} != {
        TIMELINE_INDEX_FILE,
        TIMELINE_MANIFEST_FILE,
    }:
        raise TimelineCacheCorruptError("CACHE_CORRUPT：时间轴结果组成员无效。")
    index_path = group / TIMELINE_INDEX_FILE
    manifest_path = group / TIMELINE_MANIFEST_FILE
    index_bytes = index_path.read_bytes()
    manifest = _read_json(manifest_path, "timeline manifest", TimelineCacheCorruptError)
    try:
        index = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise TimelineCacheCorruptError("CACHE_CORRUPT：时间轴文件无法读取或JSON无效。") from exc
    if not isinstance(index, dict):
        raise TimelineCacheCorruptError("CACHE_CORRUPT：时间轴必须是JSON对象。")
    _exact_keys(manifest, _TIMELINE_MANIFEST_KEYS, "timeline manifest", TimelineCacheCorruptError)
    if index_bytes != canonical_json_bytes(index):
        raise TimelineCacheCorruptError("CACHE_CORRUPT：时间轴文件不是确定的规范JSON字节。")
    try:
        validate_unified_timeline(index, expected_task_id=str(manifest.get("task_id", "")))
    except UnifiedTimelineError as exc:
        raise TimelineCacheCorruptError(f"CACHE_CORRUPT：{exc}") from exc
    expected_manifest = _build_manifest(
        index,
        visual=_visual_from_manifest(manifest),
        consumed=_consumed_from_manifest(manifest),
        cache_sha256=str(manifest["cache_identity_sha256"]),
    )
    if manifest != expected_manifest:
        raise TimelineCacheCorruptError("CACHE_CORRUPT：timeline manifest与时间轴内容不一致。")
    if require_directory_name and group.name != index["timeline_id"]:
        raise TimelineCacheCorruptError("CACHE_CORRUPT：时间轴目录名无效。")
    return index, manifest


def _visual_from_manifest(manifest: Mapping[str, Any]) -> VisualInputs:
    """按manifest记录重建输入身份（缓存/加载复验用）。"""

    inputs = manifest["inputs"]
    frame_set_id = inputs.get("frame_set_id")
    frame_manifest_sha = inputs.get("frame_set_manifest_sha256")
    if frame_set_id is None:
        return VisualInputs(None, None, None, None, None, None, None, None, None)
    frame_set = FrameSetInput(
        Path(),
        Path(),
        {"frame_set_id": frame_set_id},
        {},
        str(frame_manifest_sha),
    )
    return VisualInputs(
        frame_set,
        None,
        inputs.get("page_change_set_id"),
        inputs.get("page_change_set_manifest_sha256"),
        inputs.get("page_change_profile_id"),
        None,
        None,
        None,
        None,
    )


def _consumed_from_manifest(manifest: Mapping[str, Any]) -> OcrConsumed | None:
    inputs = manifest["inputs"]
    ocr_set_id = inputs.get("ocr_set_id")
    if ocr_set_id is None:
        return None
    return OcrConsumed(
        ocr_set_id=str(ocr_set_id),
        manifest_sha256=str(inputs.get("ocr_set_manifest_sha256")),
        mode="",
        profile_id="",
        status="",
        reason_codes=(),
        frames=(),
    )


def validate_timeline_group(
    group_path: str | Path,
    *,
    transcript: Mapping[str, Any],
    visual: VisualInputs,
    consumed: OcrConsumed | None,
    ocr_request: str,
    cache_sha256: str,
    require_directory_name: bool = True,
) -> TimelineGroup:
    """完整复验缓存/待发布组：结构自洽＋与当前上游输入身份一致。"""

    index, manifest = validate_timeline_group_structure(
        group_path, require_directory_name=require_directory_name
    )
    if manifest["cache_identity_sha256"] != cache_sha256:
        raise TimelineCacheCorruptError("CACHE_CORRUPT：组记录的cache identity与当前输入不符。")
    expected_payload = build_timeline_payload(
        task_id=str(manifest["task_id"]),
        transcript=dict(transcript),
        visual=visual,
        consumed=consumed,
        ocr_request=ocr_request,
    )
    if index != expected_payload:
        raise TimelineCacheCorruptError("CACHE_CORRUPT：组内容与当前输入重算结果不一致。")
    group = Path(group_path)
    return TimelineGroup(
        group_path=group,
        index_path=group / TIMELINE_INDEX_FILE,
        manifest_path=group / TIMELINE_MANIFEST_FILE,
        index=index,
        manifest=manifest,
        cache_hit=False,
        pointer_repaired=False,
    )


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _current_payload(index: Mapping[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": TIMELINE_CURRENT_SCHEMA_NAME,
        "schema_version": TIMELINE_CURRENT_SCHEMA_VERSION,
        "artifact_type": TIMELINE_ARTIFACT_TYPE,
        "result_id": index["timeline_id"],
        "group": f"{TIMELINE_GROUP_DIRECTORY}/{index['timeline_id']}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
    }


def _load_current(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读取并复验timelines current指针；路径不存在表示“尚无current”。"""

    path = root / TIMELINE_CURRENT_FILE
    if not _current_path_state(path, "timelines current", TimelineCurrentPointerCorruptError):
        return None, None
    pointer = _read_json(path, "timelines current", TimelineCurrentPointerCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "timelines current", TimelineCurrentPointerCorruptError)
    if (
        pointer["schema_name"] != TIMELINE_CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != TIMELINE_CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != TIMELINE_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{TIMELINE_GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise TimelineCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：timelines current身份无效。")
    group = artifact_group_path(root, TIMELINE_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise TimelineCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：timelines current的组路径越出正式组目录。")
    manifest_path = group / TIMELINE_MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise TimelineCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：timelines current目标缺失或损坏。")
    return pointer, str(pointer["result_id"])


def load_timeline_current(output_root: str | Path, task_id: str) -> dict[str, Any] | None:
    """公开的current指针读取入口（13-1供14+消费）；复验失败硬抛。"""

    validate_task_id(task_id)
    root = Path(output_root).expanduser() / task_id / "m2" / TIMELINE_ROOT_DIRECTORY
    pointer, _ = _load_current(root)
    return pointer


def load_published_timeline(
    output_root: str | Path,
    task_id: str,
) -> dict[str, Any] | None:
    """新的显式Timeline 2.0 loader（P2 §一.5）：只读加载current指向的正式组。

    复验指针、目录名、manifest哈希、规范JSON字节与完整Schema；返回逻辑对象
    本身。不做上游实时比对（该检查属于``build_timeline_v2``的发布/缓存路径）。
    """

    pointer = load_timeline_current(output_root, task_id)
    if pointer is None:
        return None
    root = Path(output_root).expanduser() / task_id / "m2" / TIMELINE_ROOT_DIRECTORY
    group = artifact_group_path(root, TIMELINE_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise TimelineCurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：timelines current的组路径越界。"
        )
    index, _manifest = validate_timeline_group_structure(group)
    return index


def _replace_current(root: Path, payload: Mapping[str, Any]) -> None:
    temporary = root / f".{TIMELINE_CURRENT_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(payload))
        os.replace(temporary, root / TIMELINE_CURRENT_FILE)
    finally:
        # 只按精确路径清理本次调用自己写出的临时指针（与F1/F2/OCR相同）。
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _disk_current_identity(root: Path) -> tuple[str, str] | None:
    """宽容读取磁盘上的current身份，只用于清理和审计决策。"""

    try:
        raw = (root / TIMELINE_CURRENT_FILE).read_bytes()
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
    """只有磁盘current明确不存在或明确指向别的结果时才允许删除新组。"""

    if not (root / TIMELINE_CURRENT_FILE).exists():
        return True
    identity = _disk_current_identity(root)
    return identity is not None and identity[0] != result_id


@contextmanager
def _module_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    locks = m2_root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / TIMELINE_LOCK_FILE
    payload = _json_bytes(
        {
            "schema_name": "ModuleLock",
            "schema_version": "1.0",
            "task_id": task_id,
            "module": TIMELINE_MODULE_TOKEN,
            "pid": os.getpid(),
        }
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise TimelineConcurrentRunError("CONCURRENT_RUN：timelines锁已存在。") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        path.unlink(missing_ok=True)


def _inject(selected: str | None, point: str) -> None:
    if selected == point:
        raise InjectedTimelineFailure(f"注入失败：{point}")


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
    root = m2_root / "failures" / TIMELINE_MODULE_TOKEN / failure_id
    root.mkdir(parents=True, exist_ok=False)
    code = getattr(error, "error_code", ERROR_TIMELINE_BUILD_FAILED)
    message = (
        str(error)
        if isinstance(error, TimelineBuilderV2Error)
        else f"时间轴构建失败：{type(error).__name__}"
    )
    payload = {
        "schema_name": TIMELINE_FAILURE_SCHEMA_NAME,
        "schema_version": TIMELINE_FAILURE_SCHEMA_VERSION,
        "failure_id": failure_id,
        "task_id": task_id,
        "module": TIMELINE_MODULE_TOKEN,
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
# 顶层入口
# ---------------------------------------------------------------------------


def build_timeline_v2(
    task_id: str,
    *,
    output_root: str | Path = "outputs",
    ocr: str = "auto",
    failure_injection: str | None = None,
) -> TimelineGroup:
    """只读已发布上游，组装并发布不可变UnifiedTimeline 2.0结果组。

    ``ocr``只控制对**已发布**OCR集合的消费方式（构造器从不运行OCR）：
    ``off``不读取并记disabled；``auto``消费或记not_run；``required``必须
    存在且集合状态为success/empty，否则以REQUIRED_OCR_UNAVAILABLE失败。
    任何失败不切current；缓存损坏硬失败；发布顺序与F1/F2/OCR一致。
    """

    if ocr not in OCR_REQUESTS:
        raise TimelineBuilderV2Error(f"不支持的--ocr请求：{ocr}")
    if failure_injection is not None and failure_injection not in _FAILURE_INJECTION_POINTS:
        raise TimelineBuilderV2Error("未知的时间轴失败注入点。")
    validate_task_id(task_id)
    output = Path(output_root).expanduser()
    m2_root = output / task_id / "m2"

    transcript = _load_transcript(output, task_id)
    visual = _load_visual(output, task_id, ocr)
    consumed: OcrConsumed | None = None
    if ocr in ("auto", "required"):
        consumed = _load_ocr(output, task_id, visual)
        if ocr == "required":
            if consumed is None:
                raise InputOcrSetMissingError(
                    "INPUT_OCR_SET_MISSING：--ocr required但OCR current不存在。"
                )
            if consumed.status in ("disabled", "degraded"):
                raise RequiredOcrUnavailableError(
                    f"REQUIRED_OCR_UNAVAILABLE：--ocr required但OCR集合状态为{consumed.status}。"
                )
    identity_payload, cache_sha256 = timeline_cache_identity(
        transcript_sha256=str(transcript["primary_transcript"]["legacy_timeline_sha256"]),
        visual=visual,
        consumed=consumed,
    )
    payload = build_timeline_payload(
        task_id=task_id,
        transcript=transcript,
        visual=visual,
        consumed=consumed,
        ocr_request=ocr,
    )
    manifest = _build_manifest(
        payload,
        visual=visual,
        consumed=consumed,
        cache_sha256=cache_sha256,
    )
    result_id = str(payload["timeline_id"])
    _inject(failure_injection, "after_inputs")

    input_ids = {
        "legacy_timeline_sha256": transcript["primary_transcript"]["legacy_timeline_sha256"],
        "frame_set_id": None if visual.frame_set is None else visual.frame_set.index["frame_set_id"],
        "page_change_set_id": visual.page_change_set_id,
        "ocr_set_id": None if consumed is None else consumed.ocr_set_id,
        "ocr_request": ocr,
        "timeline_id": result_id,
    }

    root = m2_root / TIMELINE_ROOT_DIRECTORY
    groups = root / TIMELINE_GROUP_DIRECTORY
    group = groups / result_id
    temporary: Path | None = None
    created_group = False
    previous_current_id: str | None = None
    initial_current_identity: tuple[str, str] | None = None
    stage = "prepare"
    cleanup_status = "not_created"
    with _module_lock(m2_root, task_id):
        try:
            initial_current_identity = _disk_current_identity(root)
            stage = "current_validation"
            _, previous_current_id = _load_current(root)
            stage = "cache_validation"
            if group.exists():
                cached = validate_timeline_group(
                    group,
                    transcript=transcript,
                    visual=visual,
                    consumed=consumed,
                    ocr_request=ocr,
                    cache_sha256=cache_sha256,
                )
                if previous_current_id != result_id:
                    _replace_current(
                        root,
                        _current_payload(cached.index, cached.manifest_path.read_bytes()),
                    )
                    return TimelineGroup(
                        cached.group_path,
                        cached.index_path,
                        cached.manifest_path,
                        cached.index,
                        cached.manifest,
                        True,
                        True,
                    )
                return TimelineGroup(
                    cached.group_path,
                    cached.index_path,
                    cached.manifest_path,
                    cached.index,
                    cached.manifest,
                    True,
                    False,
                )
            groups.mkdir(parents=True, exist_ok=True)
            temporary = groups / f".tmp-{result_id}-{uuid.uuid4().hex}"
            temporary.mkdir(exist_ok=False)
            cleanup_status = "created"
            stage = "index"
            _write_fsync(temporary / TIMELINE_INDEX_FILE, canonical_json_bytes(payload))
            _inject(failure_injection, "after_index")
            stage = "manifest"
            _write_fsync(temporary / TIMELINE_MANIFEST_FILE, _json_bytes(manifest))
            _inject(failure_injection, "after_manifest")
            stage = "group_validation"
            validate_timeline_group(
                temporary,
                transcript=transcript,
                visual=visual,
                consumed=consumed,
                ocr_request=ocr,
                cache_sha256=cache_sha256,
                require_directory_name=False,
            )
            stage = "group_publish"
            _inject(failure_injection, "before_group_publish")
            os.replace(temporary, group)
            created_group = True
            cleanup_status = "published"
            _inject(failure_injection, "after_group_publish")
            # 正式组复验必须早于current切换（与F1/F2/OCR一致）。
            stage = "group_revalidation"
            published = validate_timeline_group(
                group,
                transcript=transcript,
                visual=visual,
                consumed=consumed,
                ocr_request=ocr,
                cache_sha256=cache_sha256,
            )
            stage = "current_publish"
            _inject(failure_injection, "before_current")
            _replace_current(
                root, _current_payload(payload, (group / TIMELINE_MANIFEST_FILE).read_bytes())
            )
            return TimelineGroup(
                published.group_path,
                published.index_path,
                published.manifest_path,
                published.index,
                published.manifest,
                False,
                False,
            )
        except BaseException as exc:
            # 清理与审计以磁盘上的current为唯一事实依据（与F1/F2/OCR一致）。
            current_identity = _disk_current_identity(root)
            if created_group and group.exists() and _may_discard_published_group(root, result_id):
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
                    input_ids=input_ids,
                    cache_sha256=cache_sha256,
                    previous_current_id=previous_current_id,
                    cleanup_status=cleanup_status,
                    current_unchanged=current_identity == initial_current_identity,
                )
            except BaseException:
                # 审计只是尽力而为，不覆盖原始异常。
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, TimelineBuilderV2Error):
                raise
            raise TimelinePublishFailedError(
                f"无法发布Timeline 2.0结果组：{type(exc).__name__}"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
