"""按low-frequency-v1低频抽取视频帧，并原子发布不可变帧集。

本模块只负责取帧和保存帧身份，不做页面变化检测、OCR或文字说明。
全部产物写入 `outputs/<task_id>/m2/frames/`，不读写任何M1正式结果。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator, Mapping, Sequence

from video_doc.asr_only_pipeline import (
    ASROnlyPipelineError,
    MediaTools,
    _media_tool_path,
    resolve_media_tools,
)
from video_doc.task_workspace import (
    load_task_manifest,
    validate_task_id,
    TaskWorkspaceError,
)
from video_doc.video_probe import VideoProbeError, probe_video


FRAME_SAMPLER_VERSION = "1.0"

FRAME_SET_SCHEMA_NAME = "FrameSet"
FRAME_SET_SCHEMA_VERSION = "1.0"
FRAME_SET_MANIFEST_SCHEMA_NAME = "ArtifactGroupManifest"
FRAME_SET_MANIFEST_SCHEMA_VERSION = "1.0"
FRAME_SET_CURRENT_SCHEMA_NAME = "ArtifactCurrentPointer"
FRAME_SET_CURRENT_SCHEMA_VERSION = "1.0"
FRAME_SET_FAILURE_SCHEMA_NAME = "ModuleFailureAudit"
FRAME_SET_FAILURE_SCHEMA_VERSION = "1.0"
FRAME_SAMPLER_CACHE_IDENTITY_SCHEMA_NAME = "FrameSamplerCacheIdentity"
FRAME_SAMPLER_CACHE_IDENTITY_SCHEMA_VERSION = "1.0"
FRAME_SAMPLER_LOCK_SCHEMA_NAME = "ModuleLock"

FRAME_ARTIFACT_TYPE = "frame_set"
FRAME_MODULE_TOKEN = "frames"
FRAME_GROUP_DIRECTORY = "formal-results"
FRAME_INDEX_FILE = "frame-set.json"
FRAME_MANIFEST_FILE = "manifest.json"
FRAME_CURRENT_FILE = "current.json"
FRAME_IMAGE_DIRECTORY = "frames"
FRAME_IMAGE_FORMAT = "png"
FRAME_IMAGE_SUFFIX = f".{FRAME_IMAGE_FORMAT}"
FRAME_ID_PREFIX = "fr"
FRAME_ID_DIGITS = 6
FRAME_RESULT_ID_PREFIX = "frame-set"

ERROR_SOURCE_VIDEO_MISMATCH = "SOURCE_VIDEO_MISMATCH"
ERROR_FRAME_EXTRACTION_FAILED = "FRAME_EXTRACTION_FAILED"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"
ERROR_FRAME_SAMPLER_FAILED = "FRAME_SAMPLER_FAILED"

_MEDIA_TOOL_ERROR_CODES = ("MEDIA_TOOL_NOT_FOUND", "MEDIA_TOOL_INVALID")

# FFmpeg/FFprobe解析失败属于F1自己的能力边界：在这里收敛为F1类型化失败。
# 消息刻意不含目录和绝对路径，CLI和审计都可以原样输出。
_MEDIA_TOOL_UNAVAILABLE_MESSAGE = (
    "FRAME_EXTRACTION_FAILED：FFmpeg/FFprobe不可用或无法执行；"
    "请安装FFmpeg，或用--ffmpeg-dir指定同时包含ffmpeg和ffprobe的目录。"
)

_FRAME_FAILURE_INJECTION_POINTS = (
    "after_frame_extraction",
    "after_frames_written",
    "after_index",
    "after_manifest",
    "before_group_publish",
    "after_group_publish",
    "before_current",
)

_FRAME_EXTRACTION_TIMEOUT_SECONDS = 120
_PTS_TIME_PATTERN = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_FRAME_SET_INDEX_KEYS = {
    "schema_name",
    "schema_version",
    "status",
    "frame_set_id",
    "task_id",
    "profile_id",
    "profile",
    "source_video",
    "sampling",
    "cache_identity_sha256",
    "frame_count",
    "frames",
}
_FRAME_SET_FRAME_KEYS = {
    "frame_id",
    "ordinal",
    "role",
    "planned_ms",
    "requested_ms",
    "decoded_ms",
    "is_first",
    "is_last",
    "file",
    "sha256",
    "size_bytes",
}
_ARTIFACT_GROUP_MANIFEST_KEYS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "task_id",
    "result_id",
    "status",
    "cache_identity_sha256",
    "inputs",
    "versions",
    "frame_count",
    "files",
}
_ARTIFACT_CURRENT_POINTER_KEYS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "result_id",
    "group",
    "manifest_sha256",
}


class FrameSamplerError(RuntimeError):
    """帧抽取或帧集发布失败。"""

    error_code = ERROR_FRAME_SAMPLER_FAILED
    recoverable = False
    next_action = "none"


class SourceVideoMismatchError(FrameSamplerError):
    error_code = ERROR_SOURCE_VIDEO_MISMATCH


class FrameExtractionFailedError(FrameSamplerError):
    error_code = ERROR_FRAME_EXTRACTION_FAILED
    recoverable = True
    next_action = "continue_text_pipeline"


class FrameSetCacheCorruptError(FrameSamplerError):
    error_code = ERROR_CACHE_CORRUPT


class CurrentPointerCorruptError(FrameSamplerError):
    error_code = ERROR_CURRENT_POINTER_CORRUPT


class ConcurrentRunError(FrameSamplerError):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "await_user_confirmation"


class FrameSetPublishFailedError(FrameSamplerError):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = True
    next_action = "retry_after_cleanup"


class InjectedFrameSamplerFailure(FrameSamplerError):
    """仅供测试注入使用的失败。"""


@dataclass(frozen=True)
class SamplingProfile:
    """影响帧集身份的取帧参数。

    页面变化相关的F2参数（变化代理宽度、候选速率上下限、固定场景阈值）
    刻意不进入身份：调整F2不应当让已发布的帧集失效。
    """

    profile_id: str
    base_interval_ms: int
    max_sample_frames_per_video: int
    include_first_decodable_frame: bool
    include_last_decodable_frame: bool
    long_video_distribution: str
    stored_frame_resolution: str


LOW_FREQUENCY_V1 = SamplingProfile(
    profile_id="low-frequency-v1",
    base_interval_ms=5000,
    max_sample_frames_per_video=360,
    include_first_decodable_frame=True,
    include_last_decodable_frame=True,
    long_video_distribution="uniform_between_first_and_last",
    stored_frame_resolution="source",
)

SAMPLING_PROFILES: dict[str, SamplingProfile] = {
    LOW_FREQUENCY_V1.profile_id: LOW_FREQUENCY_V1,
}

# 阶段11-F0冻结但属于F2的参数，当前不参与取帧，仅在此登记来源。
LOW_FREQUENCY_V1_DEFERRED_PARAMETERS: dict[str, Any] = {
    "change_proxy_width_pixels": 320,
    "page_change_candidate_rate_per_minute": 1.5,
    "page_change_candidate_minimum": 12,
    "page_change_candidate_maximum": 90,
    "fixed_scene_threshold": None,
    "threshold_status": "deferred_to_f2_manual_window_evaluation",
}


@dataclass(frozen=True)
class SamplingSlot:
    """一个计划中的取帧时间点。"""

    ordinal: int
    frame_id: str
    role: str
    requested_ms: int
    is_first: bool
    is_last: bool


@dataclass(frozen=True)
class SourceVideoIdentity:
    """正式JSON允许保存的源视频身份，不含绝对路径。"""

    file_name: str
    sha256: str
    size_bytes: int
    container_duration_ms: int
    video_stream_duration_ms: int
    width: int | None
    height: int | None
    frame_rate: float | None
    video_codec: str | None


@dataclass(frozen=True)
class FrameSetGroup:
    """已发布的不可变帧集。"""

    group_path: Path
    index_path: Path
    manifest_path: Path
    frame_paths: tuple[Path, ...]
    index: dict[str, Any]
    manifest: dict[str, Any]
    cache_hit: bool
    pointer_repaired: bool


def _fail(message: str) -> None:
    raise FrameSamplerError(message)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        _fail(f"{location}字段集合不符合冻结契约。")


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _uniform_indices(count: int, target: int) -> list[int]:
    """在保持首尾的前提下等距挑选target个下标。"""

    if target >= count:
        return list(range(count))
    if target <= 1:
        return [0]
    step = (count - 1) / (target - 1)
    return [min(count - 1, math.floor(index * step + 0.5)) for index in range(target)]


def resolve_sampling_profile(profile_id: str = LOW_FREQUENCY_V1.profile_id) -> SamplingProfile:
    """按版本化profile ID取用冻结的取帧参数。"""

    profile = SAMPLING_PROFILES.get(profile_id)
    if profile is None:
        supported = "、".join(sorted(SAMPLING_PROFILES))
        _fail(f"未知的取帧profile：{profile_id!r}；当前支持：{supported}")
    return profile


def plan_sampling_slots(
    container_duration_ms: int,
    video_stream_duration_ms: int,
    profile: SamplingProfile = LOW_FREQUENCY_V1,
) -> tuple[list[SamplingSlot], dict[str, Any]]:
    """由视频时长推导确定性的取帧时间点。

    规则：以容器时长按基础间隔铺网格，丢弃越过视频流末尾的网格点，
    再补一个末尾可解码帧；超过上限时在首尾之间等距重采样。
    """

    if container_duration_ms <= 0 or video_stream_duration_ms <= 0:
        raise FrameExtractionFailedError("视频时长为0或不可用，无法规划取帧时间点。")
    if profile.base_interval_ms <= 0:
        _fail("取帧基础间隔必须为正整数。")

    video_end_ms = min(container_duration_ms, video_stream_duration_ms)
    grid_total = _ceil_div(container_duration_ms, profile.base_interval_ms)
    grid = [
        index * profile.base_interval_ms
        for index in range(grid_total)
    ]
    kept = [value for value in grid if value < video_end_ms]
    dropped = len(grid) - len(kept)

    selected: list[tuple[int, str]] = [(value, "grid") for value in kept]
    if selected and not profile.include_first_decodable_frame:
        selected = selected[1:]
    if profile.include_last_decodable_frame:
        selected.append((video_end_ms, "tail"))
    if not selected:
        raise FrameExtractionFailedError("视频过短，未能规划出任何取帧时间点。")

    resampled = False
    planned_slot_count = len(selected)
    if len(selected) > profile.max_sample_frames_per_video:
        if profile.long_video_distribution != "uniform_between_first_and_last":
            _fail(
                "不支持的长时间视频分布策略："
                f"{profile.long_video_distribution!r}"
            )
        indices = _uniform_indices(len(selected), profile.max_sample_frames_per_video)
        selected = [selected[index] for index in indices]
        resampled = True

    slots = [
        SamplingSlot(
            ordinal=position,
            frame_id=f"{FRAME_ID_PREFIX}{position:0{FRAME_ID_DIGITS}d}",
            role=role,
            requested_ms=requested_ms,
            is_first=position == 1,
            is_last=position == len(selected),
        )
        for position, (requested_ms, role) in enumerate(selected, start=1)
    ]
    span_ms = slots[-1].requested_ms - slots[0].requested_ms
    plan = {
        "base_interval_ms": profile.base_interval_ms,
        "max_sample_frames_per_video": profile.max_sample_frames_per_video,
        "grid_slot_count": len(grid),
        "grid_slots_dropped_beyond_stream_end": dropped,
        "planned_slot_count_before_cap": planned_slot_count,
        "slot_count": len(slots),
        "uniformly_resampled": resampled,
        "effective_interval_ms": round(span_ms / (len(slots) - 1), 6)
        if len(slots) > 1
        else 0.0,
    }
    return slots, plan


def frame_sampler_cache_identity(
    profile: SamplingProfile,
    source: SourceVideoIdentity,
) -> tuple[dict[str, Any], str]:
    """计算帧集身份；只覆盖真正决定帧内容的输入。"""

    payload = {
        "schema_name": FRAME_SAMPLER_CACHE_IDENTITY_SCHEMA_NAME,
        "schema_version": FRAME_SAMPLER_CACHE_IDENTITY_SCHEMA_VERSION,
        "sampler_version": FRAME_SAMPLER_VERSION,
        "frame_image_format": FRAME_IMAGE_FORMAT,
        "profile": asdict(profile),
        "source_video": {
            "file_name": source.file_name,
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
        },
    }
    return payload, _canonical_sha256(payload)


def frame_set_result_id(cache_identity_sha256: str) -> str:
    return f"{FRAME_RESULT_ID_PREFIX}-{cache_identity_sha256[:12]}"


def _probe_video_stream(tools: MediaTools, video_path: Path) -> dict[str, Any]:
    """读取视频流自身的时长与帧数；容器时长可能被音轨拉长。"""

    command = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration,nb_frames,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        with _media_tool_path(tools):
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_FRAME_EXTRACTION_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FrameExtractionFailedError("FFprobe无法读取视频流时长。") from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FrameExtractionFailedError("FFprobe返回了无法解析的视频流信息。") from exc
    streams = payload.get("streams") or []
    if not streams:
        raise FrameExtractionFailedError("视频中没有可用的视频流。")
    return streams[0]


def _stream_duration_ms(stream: Mapping[str, Any], fallback_ms: int) -> int:
    raw = stream.get("duration")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        frame_count = stream.get("nb_frames")
        rate_raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        try:
            numerator, denominator = str(rate_raw).split("/", maxsplit=1)
            rate = float(numerator) / float(denominator)
            value = float(frame_count) / rate
        except (TypeError, ValueError, ZeroDivisionError):
            value = 0.0
    if value <= 0:
        return fallback_ms
    return int(round(value * 1000))


def describe_source_video(video_path: str | Path, tools: MediaTools) -> SourceVideoIdentity:
    """读取源视频身份；正式JSON只保留文件名、哈希、大小和几何信息。"""

    source = Path(video_path).expanduser()
    if not source.is_file():
        raise FrameExtractionFailedError(f"源视频不存在：{source.name}")
    try:
        metadata = probe_video(source)
    except VideoProbeError as exc:
        raise FrameExtractionFailedError("无法读取源视频元数据。") from exc
    stream = _probe_video_stream(tools, source)
    container_duration_ms = int(round(metadata.duration_seconds * 1000))
    stream_duration_ms = _stream_duration_ms(stream, container_duration_ms)
    return SourceVideoIdentity(
        file_name=metadata.file_name,
        sha256=_file_sha256(source),
        size_bytes=metadata.file_size_bytes,
        container_duration_ms=container_duration_ms,
        video_stream_duration_ms=stream_duration_ms,
        width=metadata.width,
        height=metadata.height,
        frame_rate=metadata.frame_rate,
        video_codec=metadata.video_codec,
    )


def _frame_interval_ms(source: SourceVideoIdentity) -> int:
    if not source.frame_rate or source.frame_rate <= 0:
        return 1000
    return max(1, math.ceil(1000 / source.frame_rate))


def _tail_seek_candidates(
    video_end_ms: int,
    frame_interval_ms: int,
) -> list[int]:
    """末尾可解码帧的确定性候选序列，按由后向前顺序尝试。"""

    candidates = [
        video_end_ms - offset * frame_interval_ms for offset in (0, 1, 2, 4, 8)
    ]
    return [value for value in candidates if value > 0]


def _extraction_command(
    ffmpeg: Path,
    video_path: Path,
    requested_ms: int,
    destination: Path,
) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-copyts",
        "-ss",
        f"{requested_ms / 1000:.3f}",
        "-i",
        str(video_path),
        "-an",
        "-frames:v",
        "1",
        "-vf",
        "showinfo",
        "-update",
        "1",
        "-y",
        str(destination),
    ]


def _first_decoded_ms(stderr: str) -> int | None:
    match = _PTS_TIME_PATTERN.search(stderr or "")
    if match is None:
        return None
    return int(round(float(match.group(1)) * 1000))


def _extract_frame(
    tools: MediaTools,
    video_path: Path,
    requested_ms: int,
    destination: Path,
) -> int | None:
    """抽取不早于requested_ms的第一帧，返回其真实解码毫秒。"""

    command = _extraction_command(tools.ffmpeg, video_path, requested_ms, destination)
    try:
        with _media_tool_path(tools):
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_FRAME_EXTRACTION_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is None or completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        return None
    decoded_ms = _first_decoded_ms(completed.stderr)
    if decoded_ms is None or decoded_ms < requested_ms - 1:
        destination.unlink(missing_ok=True)
        return None
    return decoded_ms


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        return None
    return (
        int.from_bytes(header[16:20], "big"),
        int.from_bytes(header[20:24], "big"),
    )


def _inject_failure(selected: str | None, point: str) -> None:
    if selected == point:
        raise InjectedFrameSamplerFailure(f"注入失败：{point}")


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("xb") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())


def _error_code_for(error: BaseException) -> str:
    if isinstance(error, FrameSamplerError):
        return error.error_code
    message = str(error)
    for code in _MEDIA_TOOL_ERROR_CODES:
        if message.startswith(code):
            return code
    return ERROR_FRAME_SAMPLER_FAILED


def _safe_message_for(error: BaseException) -> str:
    if isinstance(error, InjectedFrameSamplerFailure):
        return str(error)
    return (
        "帧集发布失败；未记录视频路径、帧图像内容或底层异常详情。"
        f"（错误类型：{type(error).__name__}）"
    )


def _write_failure_audit(
    m2_root: Path,
    *,
    task_id: str,
    stage: str,
    error: BaseException,
    input_ids: Mapping[str, Any],
    cache_identity_sha256: str | None,
    previous_current_id: str | None,
    temporary_cleanup_status: str,
    current_unchanged: bool,
) -> Path:
    failure_id = f"failure-{uuid.uuid4().hex[:12]}"
    failure_root = m2_root / "failures" / FRAME_MODULE_TOKEN / failure_id
    failure_root.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_name": FRAME_SET_FAILURE_SCHEMA_NAME,
        "schema_version": FRAME_SET_FAILURE_SCHEMA_VERSION,
        "status": "failed",
        "failure_id": failure_id,
        "task_id": task_id,
        "module": FRAME_MODULE_TOKEN,
        "stage": stage,
        "error_code": _error_code_for(error),
        "safe_message": _safe_message_for(error),
        "input_ids": dict(input_ids),
        "cache_identity_sha256": cache_identity_sha256,
        "previous_current_id": previous_current_id,
        "temporary_cleanup_status": temporary_cleanup_status,
        "business_call_count": 0,
        "supplier_request_count": 0,
        "current_unchanged": current_unchanged,
        "recoverable": bool(getattr(error, "recoverable", False)),
        "next_action": str(getattr(error, "next_action", "none")),
    }
    path = failure_root / "failure.json"
    _write_fsync(path, _json_bytes(audit))
    return path


@contextmanager
def _frames_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    """模块级独占锁；发现已有锁直接失败，不自动清理。"""

    locks_root = m2_root / ".locks"
    locks_root.mkdir(parents=True, exist_ok=True)
    lock_path = locks_root / f"{FRAME_MODULE_TOKEN}.lock"
    payload = _json_bytes(
        {
            "schema_name": FRAME_SAMPLER_LOCK_SCHEMA_NAME,
            "schema_version": "1.0",
            "task_id": task_id,
            "module": FRAME_MODULE_TOKEN,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ConcurrentRunError(
            f"CONCURRENT_RUN：{FRAME_MODULE_TOKEN}模块已有运行中的锁；"
            "请确认进程状态后手动处理。"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _read_json_object(path: Path, location: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrameSetCacheCorruptError(f"CACHE_CORRUPT：{location}不是有效UTF-8 JSON。") from exc
    if not isinstance(payload, dict):
        raise FrameSetCacheCorruptError(f"CACHE_CORRUPT：{location}顶层必须是JSON对象。")
    return payload


def _read_current_object(path: Path, location: str) -> dict[str, Any]:
    """读取current指针；任何形态问题都归为``CURRENT_POINTER_CORRUPT``。

    与``_read_json_object``的差别只在错误分类：索引和manifest属于结果组内容，
    损坏时是``CACHE_CORRUPT``；current指针是“哪个组是正式结果”的唯一依据，
    任何形态问题都必须硬失败，绝不能被降级成“没有current”而静默改写正式结果。
    """

    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}不是有效UTF-8 JSON。"
        ) from exc
    if not isinstance(payload, dict):
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}顶层必须是JSON对象。"
        )
    return payload


def _exact_current_keys(
    value: Mapping[str, Any], expected: set[str], location: str
) -> None:
    if set(value) != expected:
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}字段集合不符合冻结契约。"
        )


def is_safe_result_id(value: object) -> bool:
    """result_id必须是artifact根目录下的单个普通目录名。

    同时排除``.``/``..``、``/``、``\\``、盘符与UNC语义，以及Windows会把末尾
    点号和空格折叠掉的形态，确保它既不携带任何路径分隔符，也不会因为文件系统
    的名称折叠而指向另一个组。
    """

    if not isinstance(value, str) or not value:
        return False
    if value.startswith(".") or value.endswith(".") or value != value.strip():
        return False
    if "/" in value or "\\" in value or ":" in value:
        return False
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.drive or windows.root:
        return False
    return posix.name == value and windows.name == value


def artifact_group_path(root: Path, group_directory: str, result_id: str) -> Path | None:
    """返回``root/group_directory/result_id``；越出该目录时返回None。

    除名称本身外还复验解析结果，且要求是**精确身份**而不是父子关系：
    ``resolved_parent``必须恰好是``resolved_root/group_directory``，
    ``resolved_candidate``必须恰好是``resolved_parent/result_id``。

    只比较``resolved_candidate.parent == resolved_parent``不足以覆盖两种链接：
    其一，``root/group_directory``本身是指向artifact根目录外的符号链接或目录联接时，
    parent与candidate会一起解析到外部，父子关系仍然成立；其二，
    ``group_directory/<result_id>``是指向同级其它组的符号链接时，解析结果依然落在
    同一个``group_directory``内。两种都会把读取位置带到预期之外，因此必须按
    ``resolved_root``下的预期路径做相等比较；任一resolve失败或身份不一致都返回None。

    ``result_id``必须是安全的单层名称（复用``is_safe_result_id``）。这一条不能只靠
    调用方：``pathlib``在拼接时会折叠掉``.``，``"."``因此能通过上面的精确相等比较，
    助手自身必须拒绝它。
    """

    if not is_safe_result_id(result_id):
        return None
    parent = root / group_directory
    candidate = parent / result_id
    try:
        resolved_root = root.resolve()
        resolved_parent = parent.resolve()
        resolved_candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved_parent != resolved_root / group_directory:
        return None
    if resolved_candidate != resolved_parent / result_id:
        return None
    return candidate


def _require_regular_current_file(path: Path, location: str) -> bool:
    """current路径是普通文件时返回True，不存在时返回False。

    目录、符号链接及其它非普通文件一律硬失败。判断顺序必须先看``is_symlink()``：
    指向普通文件的活符号链接``is_file()``同样为True，而目录和断链符号链接的
    ``is_file()``/``exists()``为False；按“不存在”处理就会被当成缺少current，
    进而在发布新组时改写发布状态。
    """

    if path.is_symlink():
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}存在但不是普通文件。"
        )
    if path.is_file():
        return True
    if path.exists():
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}存在但不是普通文件。"
        )
    return False


def _build_index(
    *,
    task_id: str,
    profile: SamplingProfile,
    source: SourceVideoIdentity,
    source_declared_name: str,
    result_id: str,
    cache_identity_sha256: str,
    plan: Mapping[str, Any],
    frames: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    frame_width = frames[0]["width"] if frames else None
    frame_height = frames[0]["height"] if frames else None
    return {
        "schema_name": FRAME_SET_SCHEMA_NAME,
        "schema_version": FRAME_SET_SCHEMA_VERSION,
        "status": "success",
        "frame_set_id": result_id,
        "task_id": task_id,
        "profile_id": profile.profile_id,
        "profile": asdict(profile),
        "source_video": {
            "file_name": source.file_name,
            "declared_file_name": source_declared_name,
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "container_duration_ms": source.container_duration_ms,
            "video_stream_duration_ms": source.video_stream_duration_ms,
            "width": source.width,
            "height": source.height,
            "frame_rate": source.frame_rate,
            "video_codec": source.video_codec,
        },
        "sampling": {
            **dict(plan),
            "frame_image_format": FRAME_IMAGE_FORMAT,
            "stored_frame_resolution": profile.stored_frame_resolution,
            "frame_width": frame_width,
            "frame_height": frame_height,
        },
        "cache_identity_sha256": cache_identity_sha256,
        "frame_count": len(frames),
        "frames": [
            {key: frame[key] for key in _FRAME_SET_FRAME_KEYS} for frame in frames
        ],
    }


def _build_manifest(
    index: Mapping[str, Any],
    file_identities: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_name": FRAME_SET_MANIFEST_SCHEMA_NAME,
        "schema_version": FRAME_SET_MANIFEST_SCHEMA_VERSION,
        "artifact_type": FRAME_ARTIFACT_TYPE,
        "task_id": index["task_id"],
        "result_id": index["frame_set_id"],
        "status": "success",
        "cache_identity_sha256": index["cache_identity_sha256"],
        "inputs": {
            "profile_id": index["profile_id"],
            "source_video_file_name": index["source_video"]["file_name"],
            "source_video_sha256": index["source_video"]["sha256"],
            "source_video_size_bytes": index["source_video"]["size_bytes"],
        },
        "versions": {
            "frame_sampler": FRAME_SAMPLER_VERSION,
            "frame_set_schema": FRAME_SET_SCHEMA_VERSION,
            "artifact_group_manifest": FRAME_SET_MANIFEST_SCHEMA_VERSION,
        },
        "frame_count": index["frame_count"],
        "files": {
            name: {
                "sha256": identity["sha256"],
                "bytes": identity["size_bytes"],
            }
            for name, identity in file_identities.items()
        },
    }


def _file_identities_from_index(index: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    identities = {
        frame["file"]: {
            "sha256": frame["sha256"],
            "size_bytes": frame["size_bytes"],
        }
        for frame in index["frames"]
    }
    identities[FRAME_INDEX_FILE] = {
        "sha256": _hashlib_sha256(_json_bytes(index)),
        "size_bytes": len(_json_bytes(index)),
    }
    return identities


def _hashlib_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _current_payload(index: Mapping[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": FRAME_SET_CURRENT_SCHEMA_NAME,
        "schema_version": FRAME_SET_CURRENT_SCHEMA_VERSION,
        "artifact_type": FRAME_ARTIFACT_TYPE,
        "result_id": index["frame_set_id"],
        "group": f"{FRAME_GROUP_DIRECTORY}/{index['frame_set_id']}",
        "manifest_sha256": _hashlib_sha256(manifest_bytes),
    }


def load_frame_set_current(
    output_root: str | Path,
    task_id: str,
) -> dict[str, Any] | None:
    """读取frames current指针；不存在时返回None。

    复验与``sample_frames``内部读取完全一致：指针存在但形态损坏（含非普通
    文件、``../``路径逃逸）或目标组、manifest缺失、哈希不匹配时抛
    ``CurrentPointerCorruptError``。调用方不得把损坏的指针当成“没有current”。
    """

    validate_task_id(task_id)
    frames_root = (
        Path(output_root).expanduser() / task_id / "m2" / FRAME_MODULE_TOKEN
    )
    return _validated_current_pointer(frames_root)


def validate_frame_set_group(
    group_path: str | Path,
    *,
    source_video: str | Path,
    tools: MediaTools,
    profile: SamplingProfile = LOW_FREQUENCY_V1,
    require_directory_name: bool = True,
) -> FrameSetGroup:
    """从正式组重新加载并复验身份、时间点、文件哈希和确定性序列化。"""

    group = Path(group_path)
    if not group.is_dir():
        raise FrameSetCacheCorruptError(f"CACHE_CORRUPT：帧集结果组不存在：{group.name}")

    image_directory = group / FRAME_IMAGE_DIRECTORY
    entries = {entry.name for entry in group.iterdir()}
    if entries != {FRAME_INDEX_FILE, FRAME_MANIFEST_FILE, FRAME_IMAGE_DIRECTORY}:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集结果组成员集合不完整或含额外成员。")
    if not image_directory.is_dir():
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集结果组缺少帧图像目录。")

    index_path = group / FRAME_INDEX_FILE
    manifest_path = group / FRAME_MANIFEST_FILE
    index = _read_json_object(index_path, "帧集索引")
    manifest = _read_json_object(manifest_path, "帧集manifest")
    _exact_keys(index, _FRAME_SET_INDEX_KEYS, "帧集索引")
    _exact_keys(manifest, _ARTIFACT_GROUP_MANIFEST_KEYS, "帧集manifest")

    if (
        index["schema_name"] != FRAME_SET_SCHEMA_NAME
        or index["schema_version"] != FRAME_SET_SCHEMA_VERSION
        or index["status"] != "success"
    ):
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集索引Schema或状态无效。")
    if index["profile_id"] != profile.profile_id or index["profile"] != asdict(profile):
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集索引记录的profile与当前profile不一致。")
    if not isinstance(index["frame_count"], int) or isinstance(index["frame_count"], bool):
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集索引frame_count无效。")

    source = describe_source_video(source_video, tools)
    _, cache_identity_sha256 = frame_sampler_cache_identity(profile, source)
    if cache_identity_sha256 != index["cache_identity_sha256"]:
        raise FrameSetCacheCorruptError(
            "CACHE_CORRUPT：帧集cache identity与当前源视频或参数不一致。"
        )
    expected_result_id = frame_set_result_id(cache_identity_sha256)
    if index["frame_set_id"] != expected_result_id:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集ID与cache identity不一致。")

    recorded_source = index["source_video"]
    for field, expected in (
        ("file_name", source.file_name),
        ("sha256", source.sha256),
        ("size_bytes", source.size_bytes),
        ("container_duration_ms", source.container_duration_ms),
        ("video_stream_duration_ms", source.video_stream_duration_ms),
        ("width", source.width),
        ("height", source.height),
        ("video_codec", source.video_codec),
    ):
        if recorded_source.get(field) != expected:
            raise FrameSetCacheCorruptError(f"CACHE_CORRUPT：帧集记录的源视频{field}不一致。")

    if len(index["frames"]) != index["frame_count"]:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集frame_count与帧条目数不一致。")
    slots, plan = plan_sampling_slots(
        source.container_duration_ms,
        source.video_stream_duration_ms,
        profile,
    )
    if len(slots) != index["frame_count"]:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集数量与当前profile规划不一致。")
    for slot, frame in zip(slots, index["frames"]):
        _exact_keys(frame, _FRAME_SET_FRAME_KEYS, "帧集帧条目")
        if (
            frame["frame_id"] != slot.frame_id
            or frame["ordinal"] != slot.ordinal
            or frame["role"] != slot.role
            or frame["planned_ms"] != slot.requested_ms
            or frame["is_first"] is not slot.is_first
            or frame["is_last"] is not slot.is_last
        ):
            raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集时间点或帧身份与规划不一致。")
        for field in ("planned_ms", "requested_ms", "decoded_ms"):
            if not isinstance(frame[field], int) or isinstance(frame[field], bool):
                raise FrameSetCacheCorruptError(f"CACHE_CORRUPT：帧集{field}无效。")
        if frame["decoded_ms"] < frame["requested_ms"] - 1:
            raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集decoded_ms早于请求时间。")
        if frame["requested_ms"] > frame["planned_ms"]:
            raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集请求时间晚于计划时间。")

    recording = index["sampling"]
    expected_sampling_keys = set(plan) | {
        "frame_image_format",
        "stored_frame_resolution",
        "frame_width",
        "frame_height",
    }
    _exact_keys(recording, expected_sampling_keys, "帧集sampling")
    for key, value in plan.items():
        if recording[key] != value:
            raise FrameSetCacheCorruptError(f"CACHE_CORRUPT：帧集sampling.{key}与规划不一致。")
    if recording["frame_image_format"] != FRAME_IMAGE_FORMAT:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集图像格式不受支持。")
    if recording["stored_frame_resolution"] != profile.stored_frame_resolution:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集存储分辨率策略不一致。")

    expected_names = {
        f"{FRAME_IMAGE_DIRECTORY}/{frame['frame_id']}{FRAME_IMAGE_SUFFIX}"
        for frame in index["frames"]
    }
    actual_names = {f"{FRAME_IMAGE_DIRECTORY}/{entry.name}" for entry in image_directory.iterdir()}
    if actual_names != expected_names:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧图像文件集合与帧条目不一致。")

    frame_width = recording["frame_width"]
    frame_height = recording["frame_height"]
    for frame in index["frames"]:
        image_path = group / frame["file"]
        try:
            digest = _file_sha256(image_path)
            size_bytes = image_path.stat().st_size
        except OSError as exc:
            raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧图像无法读取。") from exc
        if digest != frame["sha256"] or size_bytes != frame["size_bytes"]:
            raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧图像哈希或大小与索引不一致。")
        if _png_dimensions(image_path) != (frame_width, frame_height):
            raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧图像分辨率与索引不一致。")
    if frame_width is None or frame_height is None:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集未记录帧图像分辨率。")

    expected_manifest = _build_manifest(index, _file_identities_from_index(index))
    if manifest != expected_manifest:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集manifest与索引或文件身份不一致。")
    if manifest_path.read_bytes() != _json_bytes(manifest):
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集manifest不是冻结的确定性序列化。")
    if index_path.read_bytes() != _json_bytes(index):
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集索引不是冻结的确定性序列化。")
    if require_directory_name and group.name != index["frame_set_id"]:
        raise FrameSetCacheCorruptError("CACHE_CORRUPT：帧集目录名与frame_set_id不一致。")

    return FrameSetGroup(
        group_path=group,
        index_path=index_path,
        manifest_path=manifest_path,
        frame_paths=tuple(group / frame["file"] for frame in index["frames"]),
        index=index,
        manifest=manifest,
        cache_hit=False,
        pointer_repaired=False,
    )


def _validate_current_pointer(
    frames_root: Path, pointer: Mapping[str, Any], location: str
) -> str:
    """复验current指针的字段与目标组，返回result_id。

    ``load_frame_set_current``与``sample_frames``内部读取共用本函数，保证公开
    入口和内部读取对损坏形态的分类完全一致。所有形态问题都按
    ``CURRENT_POINTER_CORRUPT``硬失败。
    """

    _exact_current_keys(pointer, _ARTIFACT_CURRENT_POINTER_KEYS, location)
    if (
        pointer["schema_name"] != FRAME_SET_CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != FRAME_SET_CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != FRAME_ARTIFACT_TYPE
    ):
        raise CurrentPointerCorruptError(f"CURRENT_POINTER_CORRUPT：{location}Schema无效。")
    result_id = pointer["result_id"]
    if not is_safe_result_id(result_id):
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}result_id不是安全的单层名称。"
        )
    if pointer["group"] != f"{FRAME_GROUP_DIRECTORY}/{result_id}":
        raise CurrentPointerCorruptError(f"CURRENT_POINTER_CORRUPT：{location}group不合法。")
    group = artifact_group_path(frames_root, FRAME_GROUP_DIRECTORY, str(result_id))
    if group is None:
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}group越出正式组目录。"
        )
    manifest_sha256 = pointer["manifest_sha256"]
    if not isinstance(manifest_sha256, str):
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}manifest哈希字段无效。"
        )
    manifest_path = group / FRAME_MANIFEST_FILE
    if not manifest_path.is_file():
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}指向的正式组缺失。"
        )
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}指向的manifest无法读取。"
        ) from exc
    if _hashlib_sha256(manifest_bytes) != manifest_sha256:
        raise CurrentPointerCorruptError(
            f"CURRENT_POINTER_CORRUPT：{location}指向的manifest哈希不匹配。"
        )
    return str(result_id)


def _validated_current_pointer(frames_root: Path) -> dict[str, Any] | None:
    """读取并完整复验current指针；返回None表示尚无current。"""

    current_path = frames_root / FRAME_CURRENT_FILE
    if not _require_regular_current_file(current_path, "frames current指针"):
        return None
    pointer = _read_current_object(current_path, "frames current指针")
    _validate_current_pointer(frames_root, pointer, "frames current指针")
    return pointer


def _load_current_pointer(
    frames_root: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """读取并复验current指针，返回(指针, previous_current_id)。

    指针路径不存在表示“尚无current”；只要该路径存在（含非普通文件），符号链接、
    非法JSON、非对象、字段缺失或多余、Schema错误、result_id或group不合法、路径
    逃逸、目标组缺失和manifest哈希不匹配都按``CURRENT_POINTER_CORRUPT``硬失败。
    """

    pointer = _validated_current_pointer(frames_root)
    if pointer is None:
        return None, None
    return pointer, str(pointer["result_id"])


def _replace_current(frames_root: Path, pointer: Mapping[str, Any]) -> None:
    temporary = frames_root / f".{FRAME_CURRENT_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(pointer))
        os.replace(temporary, frames_root / FRAME_CURRENT_FILE)
    finally:
        # 只按精确路径清理本次调用自己写出的临时指针，不使用通配删除，避免误删
        # 其它调用或其它进程的文件。os.replace成功后该路径已不存在，这里是空操作；
        # 清理本身失败（例如Windows句柄未释放）也不允许覆盖原异常。
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _disk_current_identity(frames_root: Path) -> tuple[str, str] | None:
    """宽容读取磁盘上的current身份，只用于清理和审计决策。

    current的``os.replace``可能已经成功而内存状态尚未更新，因此清理和审计
    不能依赖内存标志。这里任何异常都按“无法确认”处理，不抛出。
    """

    try:
        raw = (frames_root / FRAME_CURRENT_FILE).read_bytes()
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
    # 不安全的result_id按“无法确认”处理：它不可能是本模块发布的正式组，
    # 当成指向别的结果就会误删本次新发布的组。
    if not is_safe_result_id(result_id) or not isinstance(manifest_sha256, str):
        return None
    return str(result_id), manifest_sha256


def _may_discard_published_group(frames_root: Path, result_id: str) -> bool:
    """判断本次发布的新组是否可以被安全删除。

    只有磁盘上的current明确不存在，或明确指向别的结果时才允许删除；current
    存在但无法解析时按“不确定”处理并保留新组。误删current实际指向的组会直接
    造成悬空指针，而多留一个未被引用的正式组不会破坏任何已发布结果：下一次
    运行会在current校验阶段以``CURRENT_POINTER_CORRUPT``硬失败，只有人工修复
    或移除损坏指针之后，该组才可能作为缓存命中被复用。
    """

    if not (frames_root / FRAME_CURRENT_FILE).exists():
        return True
    identity = _disk_current_identity(frames_root)
    return identity is not None and identity[0] != result_id


def sample_frames(
    task_id: str,
    *,
    output_root: str | Path = "outputs",
    source_video: str | Path | None = None,
    profile_id: str = LOW_FREQUENCY_V1.profile_id,
    ffmpeg_dir: str | Path | None = None,
    failure_injection: str | None = None,
) -> FrameSetGroup:
    """按版本化profile抽取帧并原子发布不可变帧集，最后切换current。"""

    if failure_injection is not None and failure_injection not in _FRAME_FAILURE_INJECTION_POINTS:
        _fail("未知的帧集失败注入点。")

    profile = resolve_sampling_profile(profile_id)
    validate_task_id(task_id)
    task_root = Path(output_root).expanduser() / task_id
    task_manifest_path = task_root / "task.json"
    if not task_manifest_path.is_file():
        raise FrameSamplerError(f"任务目录缺少task.json：{task_root}")
    try:
        task_manifest = load_task_manifest(task_manifest_path, expected_task_id=task_id)
    except TaskWorkspaceError as exc:
        raise FrameSamplerError(str(exc)) from exc

    declared = task_manifest.get("source_video")
    if not declared:
        raise FrameSamplerError("SOURCE_VIDEO_REQUIRED：task.json未记录源视频路径。")
    declared_path = Path(declared).expanduser()
    selected_source = (
        Path(source_video).expanduser() if source_video is not None else declared_path
    )
    if source_video is not None and declared_path.name != selected_source.name:
        raise SourceVideoMismatchError(
            "SOURCE_VIDEO_MISMATCH：显式源视频与task.json记录的文件名不一致。"
        )

    try:
        tools = resolve_media_tools(ffmpeg_dir)
    except ASROnlyPipelineError as exc:
        # 不把编排层的异常类型泄漏给F1调用方，也不泄漏原始消息中的目录。
        raise FrameExtractionFailedError(_MEDIA_TOOL_UNAVAILABLE_MESSAGE) from exc
    source = describe_source_video(selected_source, tools)
    if declared_path.is_file() and _file_sha256(declared_path) != source.sha256:
        raise SourceVideoMismatchError(
            "SOURCE_VIDEO_MISMATCH：源视频内容与task.json记录不一致。"
        )

    _, cache_identity_sha256 = frame_sampler_cache_identity(profile, source)
    result_id = frame_set_result_id(cache_identity_sha256)
    input_ids = {
        "task_id": task_id,
        "profile_id": profile.profile_id,
        "source_video_file_name": source.file_name,
        "source_video_sha256": source.sha256,
        "source_video_size_bytes": source.size_bytes,
    }

    m2_root = task_root / "m2"
    frames_root = m2_root / FRAME_MODULE_TOKEN
    groups_root = frames_root / FRAME_GROUP_DIRECTORY
    group = groups_root / result_id

    temporary: Path | None = None
    created_group = False
    failure_stage = "prepare"
    cleanup_status = "not_created"
    previous_current_id: str | None = None
    initial_current_identity: tuple[str, str] | None = None

    with _frames_lock(m2_root, task_id):
        try:
            # 先记录磁盘上的初始current，异常处理据此判断本次调用是否改变了它。
            initial_current_identity = _disk_current_identity(frames_root)
            failure_stage = "current_validation"
            _, previous_current_id = _load_current_pointer(frames_root)

            failure_stage = "cache_validation"
            if group.exists():
                try:
                    cached = validate_frame_set_group(
                        group,
                        source_video=selected_source,
                        tools=tools,
                        profile=profile,
                    )
                except FrameSamplerError:
                    raise
                except Exception as exc:
                    raise FrameSetCacheCorruptError(
                        "CACHE_CORRUPT：已有帧集结果组校验失败。"
                    ) from exc
                if previous_current_id != result_id:
                    pointer = _current_payload(cached.index, cached.manifest_path.read_bytes())
                    _replace_current(frames_root, pointer)
                    return FrameSetGroup(
                        group_path=cached.group_path,
                        index_path=cached.index_path,
                        manifest_path=cached.manifest_path,
                        frame_paths=cached.frame_paths,
                        index=cached.index,
                        manifest=cached.manifest,
                        cache_hit=True,
                        pointer_repaired=True,
                    )
                return FrameSetGroup(
                    group_path=cached.group_path,
                    index_path=cached.index_path,
                    manifest_path=cached.manifest_path,
                    frame_paths=cached.frame_paths,
                    index=cached.index,
                    manifest=cached.manifest,
                    cache_hit=True,
                    pointer_repaired=False,
                )

            failure_stage = "frame_extraction"
            slots, plan = plan_sampling_slots(
                source.container_duration_ms,
                source.video_stream_duration_ms,
                profile,
            )
            groups_root.mkdir(parents=True, exist_ok=True)
            temporary = groups_root / f".tmp-{result_id}-{uuid.uuid4().hex}"
            temporary.mkdir(exist_ok=False)
            cleanup_status = "created"
            image_directory = temporary / FRAME_IMAGE_DIRECTORY
            image_directory.mkdir(exist_ok=False)

            frames: list[dict[str, Any]] = []
            frame_interval_ms = _frame_interval_ms(source)
            previous_decoded_ms = -1
            for slot in slots:
                candidates = (
                    _tail_seek_candidates(slot.requested_ms, frame_interval_ms)
                    if slot.role == "tail"
                    else [slot.requested_ms]
                )
                for candidate_ms in candidates:
                    if candidate_ms <= previous_decoded_ms:
                        continue
                    destination = image_directory / f"{slot.frame_id}{FRAME_IMAGE_SUFFIX}"
                    decoded_ms = _extract_frame(
                        tools, selected_source, candidate_ms, destination
                    )
                    if decoded_ms is None or decoded_ms <= previous_decoded_ms:
                        destination.unlink(missing_ok=True)
                        continue
                    dimensions = _png_dimensions(destination)
                    if dimensions is None:
                        destination.unlink(missing_ok=True)
                        continue
                    frames.append(
                        {
                            "frame_id": slot.frame_id,
                            "ordinal": slot.ordinal,
                            "role": slot.role,
                            "planned_ms": slot.requested_ms,
                            "requested_ms": candidate_ms,
                            "decoded_ms": decoded_ms,
                            "is_first": slot.is_first,
                            "is_last": slot.is_last,
                            "file": f"{FRAME_IMAGE_DIRECTORY}/{slot.frame_id}{FRAME_IMAGE_SUFFIX}",
                            "sha256": _file_sha256(destination),
                            "size_bytes": destination.stat().st_size,
                            "width": dimensions[0],
                            "height": dimensions[1],
                        }
                    )
                    previous_decoded_ms = decoded_ms
                    break
                else:
                    raise FrameExtractionFailedError(
                        "FRAME_EXTRACTION_FAILED："
                        f"{slot.frame_id}在{slot.requested_ms}毫秒附近没有可解码帧。"
                    )
            _inject_failure(failure_injection, "after_frame_extraction")
            if len(frames) != len(slots):
                raise FrameExtractionFailedError("FRAME_EXTRACTION_FAILED：抽取帧数与规划不一致。")
            resolved_width = frames[0]["width"]
            resolved_height = frames[0]["height"]
            if any(
                frame["width"] != resolved_width or frame["height"] != resolved_height
                for frame in frames
            ):
                raise FrameExtractionFailedError(
                    "FRAME_EXTRACTION_FAILED：同一视频抽取出的帧分辨率不一致。"
                )
            if source.width is not None and resolved_width != source.width:
                raise FrameExtractionFailedError(
                    "FRAME_EXTRACTION_FAILED：抽取帧宽度与源视频不一致。"
                )
            if source.height is not None and resolved_height != source.height:
                raise FrameExtractionFailedError(
                    "FRAME_EXTRACTION_FAILED：抽取帧高度与源视频不一致。"
                )
            failure_stage = "index"
            index = _build_index(
                task_id=task_id,
                profile=profile,
                source=source,
                source_declared_name=declared_path.name,
                result_id=result_id,
                cache_identity_sha256=cache_identity_sha256,
                plan=plan,
                frames=frames,
            )
            manifest = _build_manifest(index, _file_identities_from_index(index))
            _inject_failure(failure_injection, "after_index")
            manifest_bytes = _json_bytes(manifest)
            _write_fsync(temporary / FRAME_INDEX_FILE, _json_bytes(index))
            failure_stage = "manifest"
            _write_fsync(temporary / FRAME_MANIFEST_FILE, manifest_bytes)
            _inject_failure(failure_injection, "after_manifest")
            failure_stage = "group_validation"
            validate_frame_set_group(
                temporary,
                source_video=selected_source,
                tools=tools,
                profile=profile,
                require_directory_name=False,
            )
            failure_stage = "group_publish"
            _inject_failure(failure_injection, "before_group_publish")
            os.replace(temporary, group)
            created_group = True
            cleanup_status = "published"
            _inject_failure(failure_injection, "after_group_publish")
            # 正式组复验必须早于current切换：复验失败时异常处理会删掉新组，
            # 而旧current仍指向旧组，不会留下指向已删除组的悬空指针。
            failure_stage = "group_revalidation"
            published = validate_frame_set_group(
                group,
                source_video=selected_source,
                tools=tools,
                profile=profile,
            )
            failure_stage = "current_publish"
            _inject_failure(failure_injection, "before_current")
            _replace_current(
                frames_root, _current_payload(index, (group / FRAME_MANIFEST_FILE).read_bytes())
            )
            return published
        except BaseException as exc:
            # 清理与审计以磁盘上的current为唯一事实依据：current的os.replace一旦
            # 成功，内存状态可能尚未更新就发生普通异常、KeyboardInterrupt或
            # SystemExit，此时删除新组会留下悬空指针，并把current_unchanged记反。
            current_identity = _disk_current_identity(frames_root)
            if (
                created_group
                and group.exists()
                and _may_discard_published_group(frames_root, result_id)
            ):
                shutil.rmtree(group, ignore_errors=True)
                cleanup_status = "group_removed"
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
                cleanup_status = "temp_removed"
            try:
                _write_failure_audit(
                    m2_root,
                    task_id=task_id,
                    stage=failure_stage,
                    error=exc,
                    input_ids=input_ids,
                    cache_identity_sha256=cache_identity_sha256,
                    previous_current_id=previous_current_id,
                    temporary_cleanup_status=cleanup_status,
                    current_unchanged=current_identity == initial_current_identity,
                )
            except BaseException:
                # 审计只是尽力而为：写入失败（含磁盘满、权限错误）或写入期间再次
                # 被中断，都不允许覆盖或替换下面要原样重新抛出的原始异常。
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, FrameSamplerError):
                raise
            raise FrameSetPublishFailedError(
                f"无法原子发布帧集结果组：{type(exc).__name__}"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
