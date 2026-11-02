"""从正式FrameSet计算页面变化强度并发布去重后的候选集合。

本模块只读取 ``m2/frames/current.json`` 指向的不可变帧集。它不读取源视频、
不重新取帧，也不运行OCR。产物位于 ``m2/frames/page-changes/``，与帧集身份
和current分离。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence

import av
import numpy as np

from video_doc.frame_sampler import (
    FRAME_CURRENT_FILE,
    FRAME_GROUP_DIRECTORY,
    FRAME_IMAGE_DIRECTORY,
    FRAME_INDEX_FILE,
    FRAME_MANIFEST_FILE,
    FRAME_SET_CURRENT_SCHEMA_NAME,
    FRAME_SET_CURRENT_SCHEMA_VERSION,
    FRAME_SET_MANIFEST_SCHEMA_NAME,
    FRAME_SET_MANIFEST_SCHEMA_VERSION,
    FRAME_SET_SCHEMA_NAME,
    FRAME_SET_SCHEMA_VERSION,
    artifact_group_path,
    is_safe_result_id,
)
from video_doc.task_workspace import validate_task_id


PAGE_CHANGE_DETECTOR_VERSION = "1.0"
PAGE_CHANGE_SET_SCHEMA_NAME = "PageChangeSet"
PAGE_CHANGE_SET_SCHEMA_VERSION = "1.0"
PAGE_CHANGE_CACHE_SCHEMA_NAME = "PageChangeDetectorCacheIdentity"
PAGE_CHANGE_CACHE_SCHEMA_VERSION = "1.0"
PAGE_CHANGE_MANIFEST_SCHEMA_NAME = "ArtifactGroupManifest"
PAGE_CHANGE_MANIFEST_SCHEMA_VERSION = "1.0"
PAGE_CHANGE_CURRENT_SCHEMA_NAME = "ArtifactCurrentPointer"
PAGE_CHANGE_CURRENT_SCHEMA_VERSION = "1.0"
PAGE_CHANGE_FAILURE_SCHEMA_NAME = "ModuleFailureAudit"
PAGE_CHANGE_FAILURE_SCHEMA_VERSION = "1.0"

PAGE_CHANGE_ARTIFACT_TYPE = "page_change_set"
PAGE_CHANGE_MODULE_TOKEN = "page-changes"
PAGE_CHANGE_RESULT_PREFIX = "page-change-set"
PAGE_CHANGE_ROOT_DIRECTORY = "page-changes"
PAGE_CHANGE_GROUP_DIRECTORY = "formal-results"
PAGE_CHANGE_INDEX_FILE = "page-change-set.json"
PAGE_CHANGE_MANIFEST_FILE = "manifest.json"
PAGE_CHANGE_CURRENT_FILE = "current.json"
PAGE_CHANGE_LOCK_FILE = "page-changes.lock"

ERROR_INPUT_FRAME_SET_MISSING = "INPUT_FRAME_SET_MISSING"
ERROR_INPUT_FRAME_SET_CORRUPT = "INPUT_FRAME_SET_CORRUPT"
ERROR_IMAGE_DECODE_FAILED = "IMAGE_DECODE_FAILED"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"
ERROR_PAGE_CHANGE_FAILED = "PAGE_CHANGE_FAILED"

_FAILURE_INJECTION_POINTS = (
    "after_detection",
    "after_index",
    "after_manifest",
    "before_group_publish",
    "after_group_publish",
    "before_current",
)

_FRAME_CURRENT_KEYS = {
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
_PAGE_CHANGE_INDEX_KEYS = {
    "schema_name", "schema_version", "status", "page_change_set_id", "task_id",
    "profile_id", "profile", "input_frame_set", "selection", "cache_identity_sha256",
    "comparison_count", "candidate_count", "comparisons", "candidates",
}
_COMPARISON_KEYS = {
    "previous_frame_id", "frame_id", "decoded_ms", "change_strength",
    "perceptual_distance", "threshold_passed", "selected", "exclusion_reason",
}
_CANDIDATE_KEYS = {
    "candidate_id", "frame_id", "previous_frame_id", "ordinal", "planned_ms",
    "requested_ms", "decoded_ms", "change_strength", "perceptual_distance",
}
_SELECTION_KEYS = {
    "adaptive_threshold", "applied_threshold", "candidate_limit",
    "threshold_pass_count", "temporal_neighbor_excluded",
    "perceptual_duplicate_excluded", "candidate_limit_excluded",
}
_MANIFEST_KEYS = {
    "schema_name", "schema_version", "artifact_type", "task_id", "result_id",
    "status", "cache_identity_sha256", "inputs", "versions", "candidate_count", "files",
}


class PageChangeDetectorError(RuntimeError):
    error_code = ERROR_PAGE_CHANGE_FAILED
    recoverable = False
    next_action = "none"


class InputFrameSetMissingError(PageChangeDetectorError):
    error_code = ERROR_INPUT_FRAME_SET_MISSING
    recoverable = True
    next_action = "run_frame_sampler"


class InputFrameSetCorruptError(PageChangeDetectorError):
    error_code = ERROR_INPUT_FRAME_SET_CORRUPT


class FrameImageDecodeError(PageChangeDetectorError):
    error_code = ERROR_IMAGE_DECODE_FAILED
    recoverable = True
    next_action = "continue_text_pipeline"


class PageChangeCacheCorruptError(PageChangeDetectorError):
    error_code = ERROR_CACHE_CORRUPT


class PageChangeCurrentPointerCorruptError(PageChangeDetectorError):
    error_code = ERROR_CURRENT_POINTER_CORRUPT


class PageChangeConcurrentRunError(PageChangeDetectorError):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "await_user_confirmation"


class PageChangePublishFailedError(PageChangeDetectorError):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = True
    next_action = "retry_after_cleanup"


class InjectedPageChangeFailure(PageChangeDetectorError):
    pass


@dataclass(frozen=True)
class PageChangeProfile:
    profile_id: str
    proxy_width_pixels: int
    metric: str
    minimum_change_strength: float
    adaptive_quantile: float
    minimum_candidate_gap_ms: int
    duplicate_hash_distance_max: float
    candidate_rate_per_minute: float
    candidate_minimum: int
    candidate_maximum: int


PAGE_CHANGE_V1 = PageChangeProfile(
    profile_id="page-change-v1",
    proxy_width_pixels=320,
    metric="grayscale_mean_absolute_difference",
    minimum_change_strength=0.03,
    adaptive_quantile=0.75,
    minimum_candidate_gap_ms=10_000,
    duplicate_hash_distance_max=4 / 64,
    candidate_rate_per_minute=1.5,
    candidate_minimum=12,
    candidate_maximum=90,
)

PAGE_CHANGE_PROFILES = {PAGE_CHANGE_V1.profile_id: PAGE_CHANGE_V1}


@dataclass(frozen=True)
class FrameSetInput:
    root: Path
    group_path: Path
    index: dict[str, Any]
    manifest: dict[str, Any]
    manifest_sha256: str


@dataclass(frozen=True)
class PageChangeGroup:
    group_path: Path
    index_path: Path
    manifest_path: Path
    index: dict[str, Any]
    manifest: dict[str, Any]
    cache_hit: bool
    pointer_repaired: bool


@dataclass(frozen=True)
class ProxyFrame:
    pixels: np.ndarray
    perceptual_hash: tuple[bool, ...]


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


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


def _read_json(path: Path, label: str, error_type: type[PageChangeDetectorError]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise error_type(f"{label}无法读取或JSON无效。") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label}必须是JSON对象。")
    return value


def _current_path_state(path: Path, label: str, error_type: type[PageChangeDetectorError]) -> bool:
    """current路径是普通文件时返回True，不存在时返回False。

    目录、符号链接及其它非普通文件一律按损坏硬失败。判断顺序必须先看
    ``is_symlink()``：指向普通文件的活符号链接``is_file()``同样为True，而目录
    和断链符号链接的``is_file()``/``exists()``为False；按“不存在”处理就会被
    当成缺少current，进而在发布新组时改写发布状态。
    """

    if path.is_symlink():
        raise error_type(f"{label}存在但不是普通文件。")
    if path.is_file():
        return True
    if path.exists():
        raise error_type(f"{label}存在但不是普通文件。")
    return False


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str, error_type: type[PageChangeDetectorError]) -> None:
    if set(value) != expected:
        raise error_type(f"{label}字段集合无效。")


def _safe_relative_file(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


def resolve_page_change_profile(profile_id: str = PAGE_CHANGE_V1.profile_id) -> PageChangeProfile:
    profile = PAGE_CHANGE_PROFILES.get(profile_id)
    if profile is None:
        raise PageChangeDetectorError(f"不支持页面变化profile：{profile_id}")
    return profile


def _candidate_limit(duration_ms: int, profile: PageChangeProfile) -> int:
    rate_limit = math.ceil(duration_ms / 60_000 * profile.candidate_rate_per_minute)
    return min(profile.candidate_maximum, max(profile.candidate_minimum, rate_limit))


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[position]


def _hash_distance(left: Sequence[bool], right: Sequence[bool]) -> float:
    if len(left) != len(right) or not left:
        raise PageChangeDetectorError("感知哈希长度无效。")
    return sum(a != b for a, b in zip(left, right)) / len(left)


def _change_strength(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape or left.ndim != 2:
        raise PageChangeDetectorError("页面变化代理图尺寸不一致。")
    return float(np.mean(np.abs(left.astype(np.int16) - right.astype(np.int16))) / 255.0)


def _load_proxy(image_path: Path, width: int) -> ProxyFrame:
    try:
        with av.open(str(image_path), mode="r") as container:
            frame = next(container.decode(video=0))
        target_height = max(1, round(frame.height * width / frame.width))
        pixels = frame.reformat(width=width, height=target_height, format="gray").to_ndarray()
    except (OSError, StopIteration, av.error.FFmpegError, ValueError) as exc:
        raise FrameImageDecodeError(f"帧图像无法解码：{image_path.name}") from exc
    if pixels.ndim != 2 or pixels.size == 0:
        raise FrameImageDecodeError(f"帧图像代理为空：{image_path.name}")
    hash_frame = frame.reformat(width=8, height=8, format="gray").to_ndarray()
    mean = float(hash_frame.mean())
    return ProxyFrame(
        pixels=np.ascontiguousarray(pixels, dtype=np.uint8),
        perceptual_hash=tuple(bool(value >= mean) for value in hash_frame.reshape(-1)),
    )


def select_page_changes(
    frames: Sequence[Mapping[str, Any]],
    proxies: Sequence[ProxyFrame],
    *,
    duration_ms: int,
    profile: PageChangeProfile = PAGE_CHANGE_V1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """计算相邻变化，按自适应阈值、时间间隔、感知哈希和硬上限筛选。"""

    if len(frames) != len(proxies):
        raise PageChangeDetectorError("帧条目与代理图数量不一致。")
    comparisons: list[dict[str, Any]] = []
    for position in range(1, len(frames)):
        strength = round(_change_strength(proxies[position - 1].pixels, proxies[position].pixels), 6)
        distance = round(_hash_distance(proxies[position - 1].perceptual_hash, proxies[position].perceptual_hash), 6)
        comparisons.append({
            "previous_frame_id": frames[position - 1]["frame_id"],
            "frame_id": frames[position]["frame_id"],
            "decoded_ms": frames[position]["decoded_ms"],
            "change_strength": strength,
            "perceptual_distance": distance,
            "threshold_passed": False,
            "selected": False,
            "exclusion_reason": "below_threshold",
        })

    strengths = [item["change_strength"] for item in comparisons]
    adaptive = round(_nearest_rank(strengths, profile.adaptive_quantile), 6)
    applied = max(profile.minimum_change_strength, adaptive)
    limit = _candidate_limit(duration_ms, profile)
    ranked = sorted(
        range(len(comparisons)),
        key=lambda i: (-comparisons[i]["change_strength"], comparisons[i]["decoded_ms"], comparisons[i]["frame_id"]),
    )
    selected_positions: list[int] = []
    for comparison_index in ranked:
        item = comparisons[comparison_index]
        if item["change_strength"] < applied:
            continue
        item["threshold_passed"] = True
        frame_position = comparison_index + 1
        if any(
            abs(frames[frame_position]["decoded_ms"] - frames[selected + 1]["decoded_ms"])
            < profile.minimum_candidate_gap_ms
            for selected in selected_positions
        ):
            item["exclusion_reason"] = "temporal_neighbor"
            continue
        if any(
            _hash_distance(proxies[frame_position].perceptual_hash, proxies[selected + 1].perceptual_hash)
            <= profile.duplicate_hash_distance_max
            for selected in selected_positions
        ):
            item["exclusion_reason"] = "perceptual_duplicate"
            continue
        if len(selected_positions) >= limit:
            item["exclusion_reason"] = "candidate_limit"
            continue
        item["selected"] = True
        item["exclusion_reason"] = "selected"
        selected_positions.append(comparison_index)

    selected_positions.sort(key=lambda i: (comparisons[i]["decoded_ms"], comparisons[i]["frame_id"]))
    candidates: list[dict[str, Any]] = []
    for ordinal, comparison_index in enumerate(selected_positions, start=1):
        comparison = comparisons[comparison_index]
        frame = frames[comparison_index + 1]
        candidates.append({
            "candidate_id": f"pc{ordinal:06d}",
            "frame_id": frame["frame_id"],
            "previous_frame_id": comparison["previous_frame_id"],
            "ordinal": ordinal,
            "planned_ms": frame["planned_ms"],
            "requested_ms": frame["requested_ms"],
            "decoded_ms": frame["decoded_ms"],
            "change_strength": comparison["change_strength"],
            "perceptual_distance": comparison["perceptual_distance"],
        })
    selection = {
        "adaptive_threshold": adaptive,
        "applied_threshold": round(applied, 6),
        "candidate_limit": limit,
        "threshold_pass_count": sum(item["threshold_passed"] for item in comparisons),
        "temporal_neighbor_excluded": sum(item["exclusion_reason"] == "temporal_neighbor" for item in comparisons),
        "perceptual_duplicate_excluded": sum(item["exclusion_reason"] == "perceptual_duplicate" for item in comparisons),
        "candidate_limit_excluded": sum(item["exclusion_reason"] == "candidate_limit" for item in comparisons),
    }
    return comparisons, candidates, selection


def _load_frame_set_current(output_root: Path, task_id: str) -> FrameSetInput:
    frames_root = output_root / task_id / "m2" / "frames"
    current_path = frames_root / FRAME_CURRENT_FILE
    if not _current_path_state(current_path, "frames current", InputFrameSetCorruptError):
        raise InputFrameSetMissingError("INPUT_FRAME_SET_MISSING：frames current不存在。")
    pointer = _read_json(current_path, "frames current", InputFrameSetCorruptError)
    _exact_keys(pointer, _FRAME_CURRENT_KEYS, "frames current", InputFrameSetCorruptError)
    if (
        pointer["schema_name"] != FRAME_SET_CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != FRAME_SET_CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != "frame_set"
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{FRAME_GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：frames current身份无效。")
    group = artifact_group_path(frames_root, FRAME_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise InputFrameSetCorruptError(
            "INPUT_FRAME_SET_CORRUPT：frames current的组路径越出正式组目录。"
        )
    if not group.is_dir() or group.name != pointer["result_id"]:
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：frames current指向的组缺失。")
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
        or index["frame_set_id"] != pointer["result_id"]
        or index["task_id"] != task_id
        or manifest["schema_name"] != FRAME_SET_MANIFEST_SCHEMA_NAME
        or manifest["schema_version"] != FRAME_SET_MANIFEST_SCHEMA_VERSION
        or manifest["artifact_type"] != "frame_set"
        or manifest["result_id"] != pointer["result_id"]
        or manifest["task_id"] != task_id
    ):
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧集Schema或来源身份无效。")
    if index_path.read_bytes() != _frame_json_bytes(index) or manifest_bytes != _frame_json_bytes(manifest):
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧集JSON序列化不确定。")
    frames = index.get("frames")
    if not isinstance(frames, list) or index.get("frame_count") != len(frames) or len(frames) < 1:
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧数量无效。")
    expected_files: dict[str, dict[str, Any]] = {}
    previous_decoded_ms = -1
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
        image_path = group / frame["file"]
        if not image_path.is_file() or image_path.stat().st_size != frame["size_bytes"] or _file_sha256(image_path) != frame["sha256"]:
            raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧图片缺失或身份不匹配。")
        expected_files[frame["file"]] = {"sha256": frame["sha256"], "bytes": frame["size_bytes"]}
        previous_decoded_ms = frame["decoded_ms"]
    expected_files[FRAME_INDEX_FILE] = {"sha256": _sha256_bytes(index_path.read_bytes()), "bytes": index_path.stat().st_size}
    if manifest.get("files") != expected_files or manifest.get("frame_count") != len(frames):
        raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：帧集manifest文件清单无效。")
    return FrameSetInput(frames_root, group, index, manifest, pointer["manifest_sha256"])


def page_change_cache_identity(profile: PageChangeProfile, frame_set: FrameSetInput) -> tuple[dict[str, Any], str]:
    payload = {
        "schema_name": PAGE_CHANGE_CACHE_SCHEMA_NAME,
        "schema_version": PAGE_CHANGE_CACHE_SCHEMA_VERSION,
        "detector_version": PAGE_CHANGE_DETECTOR_VERSION,
        "profile": asdict(profile),
        "input_frame_set_id": frame_set.index["frame_set_id"],
        "input_frame_set_manifest_sha256": frame_set.manifest_sha256,
    }
    return payload, _sha256_bytes(_json_bytes(payload))


def _result_id(cache_sha256: str) -> str:
    return f"{PAGE_CHANGE_RESULT_PREFIX}-{cache_sha256[:12]}"


def _build_index(task_id: str, profile: PageChangeProfile, frame_set: FrameSetInput, cache_sha256: str, comparisons: list[dict[str, Any]], candidates: list[dict[str, Any]], selection: dict[str, Any]) -> dict[str, Any]:
    result_id = _result_id(cache_sha256)
    return {
        "schema_name": PAGE_CHANGE_SET_SCHEMA_NAME,
        "schema_version": PAGE_CHANGE_SET_SCHEMA_VERSION,
        "status": "empty" if not candidates else "success",
        "page_change_set_id": result_id,
        "task_id": task_id,
        "profile_id": profile.profile_id,
        "profile": asdict(profile),
        "input_frame_set": {
            "frame_set_id": frame_set.index["frame_set_id"],
            "manifest_sha256": frame_set.manifest_sha256,
            "source_video_file_name": frame_set.index["source_video"]["file_name"],
            "source_video_sha256": frame_set.index["source_video"]["sha256"],
            "source_video_size_bytes": frame_set.index["source_video"]["size_bytes"],
        },
        "selection": selection,
        "cache_identity_sha256": cache_sha256,
        "comparison_count": len(comparisons),
        "candidate_count": len(candidates),
        "comparisons": comparisons,
        "candidates": candidates,
    }


def _build_manifest(index: Mapping[str, Any]) -> dict[str, Any]:
    index_bytes = _json_bytes(index)
    return {
        "schema_name": PAGE_CHANGE_MANIFEST_SCHEMA_NAME,
        "schema_version": PAGE_CHANGE_MANIFEST_SCHEMA_VERSION,
        "artifact_type": PAGE_CHANGE_ARTIFACT_TYPE,
        "task_id": index["task_id"],
        "result_id": index["page_change_set_id"],
        "status": index["status"],
        "cache_identity_sha256": index["cache_identity_sha256"],
        "inputs": {
            "frame_set_id": index["input_frame_set"]["frame_set_id"],
            "frame_set_manifest_sha256": index["input_frame_set"]["manifest_sha256"],
        },
        "versions": {
            "page_change_detector": PAGE_CHANGE_DETECTOR_VERSION,
            "page_change_set_schema": PAGE_CHANGE_SET_SCHEMA_VERSION,
            "artifact_group_manifest": PAGE_CHANGE_MANIFEST_SCHEMA_VERSION,
        },
        "candidate_count": index["candidate_count"],
        "files": {PAGE_CHANGE_INDEX_FILE: {"sha256": _sha256_bytes(index_bytes), "bytes": len(index_bytes)}},
    }


def validate_page_change_group(group_path: str | Path, *, frame_set: FrameSetInput, profile: PageChangeProfile = PAGE_CHANGE_V1, require_directory_name: bool = True) -> PageChangeGroup:
    group = Path(group_path)
    if not group.is_dir() or {entry.name for entry in group.iterdir()} != {PAGE_CHANGE_INDEX_FILE, PAGE_CHANGE_MANIFEST_FILE}:
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化结果组成员无效。")
    index_path = group / PAGE_CHANGE_INDEX_FILE
    manifest_path = group / PAGE_CHANGE_MANIFEST_FILE
    index = _read_json(index_path, "页面变化集合", PageChangeCacheCorruptError)
    manifest = _read_json(manifest_path, "页面变化manifest", PageChangeCacheCorruptError)
    _exact_keys(index, _PAGE_CHANGE_INDEX_KEYS, "页面变化集合", PageChangeCacheCorruptError)
    _exact_keys(manifest, _MANIFEST_KEYS, "页面变化manifest", PageChangeCacheCorruptError)
    _, cache_sha256 = page_change_cache_identity(profile, frame_set)
    expected_id = _result_id(cache_sha256)
    if (
        index["schema_name"] != PAGE_CHANGE_SET_SCHEMA_NAME
        or index["schema_version"] != PAGE_CHANGE_SET_SCHEMA_VERSION
        or index["status"] not in {"success", "empty"}
        or index["page_change_set_id"] != expected_id
        or index["task_id"] != frame_set.index["task_id"]
        or index["profile_id"] != profile.profile_id
        or index["profile"] != asdict(profile)
        or index["cache_identity_sha256"] != cache_sha256
        or index["input_frame_set"]["frame_set_id"] != frame_set.index["frame_set_id"]
        or index["input_frame_set"]["manifest_sha256"] != frame_set.manifest_sha256
    ):
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化集合身份无效。")
    if index["comparison_count"] != len(index["comparisons"]) or index["candidate_count"] != len(index["candidates"]):
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化计数无效。")
    if (index["status"] == "empty") != (index["candidate_count"] == 0):
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化状态与候选数不一致。")
    if not isinstance(index["selection"], dict):
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化selection无效。")
    _exact_keys(index["selection"], _SELECTION_KEYS, "页面变化selection", PageChangeCacheCorruptError)
    if index["selection"]["candidate_limit"] != _candidate_limit(
        int(frame_set.index["source_video"]["video_stream_duration_ms"]), profile
    ):
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化候选上限无效。")
    if index["candidate_count"] > index["selection"]["candidate_limit"]:
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化候选数超过上限。")
    frame_by_id = {frame["frame_id"]: frame for frame in frame_set.index["frames"]}
    selected_ids = set()
    selected_comparisons: dict[str, Mapping[str, Any]] = {}
    for comparison in index["comparisons"]:
        _exact_keys(comparison, _COMPARISON_KEYS, "变化比较", PageChangeCacheCorruptError)
        if comparison["frame_id"] not in frame_by_id or comparison["previous_frame_id"] not in frame_by_id:
            raise PageChangeCacheCorruptError("CACHE_CORRUPT：变化比较引用未知帧。")
        if not isinstance(comparison["change_strength"], (int, float)) or not 0 <= comparison["change_strength"] <= 1:
            raise PageChangeCacheCorruptError("CACHE_CORRUPT：变化强度无效。")
        if not isinstance(comparison["perceptual_distance"], (int, float)) or not 0 <= comparison["perceptual_distance"] <= 1:
            raise PageChangeCacheCorruptError("CACHE_CORRUPT：感知距离无效。")
        if comparison["selected"]:
            selected_ids.add(comparison["frame_id"])
            selected_comparisons[comparison["frame_id"]] = comparison
    previous_ms = -1
    for ordinal, candidate in enumerate(index["candidates"], start=1):
        _exact_keys(candidate, _CANDIDATE_KEYS, "页面变化候选", PageChangeCacheCorruptError)
        frame = frame_by_id.get(candidate["frame_id"])
        if frame is None or candidate["candidate_id"] != f"pc{ordinal:06d}" or candidate["ordinal"] != ordinal:
            raise PageChangeCacheCorruptError("CACHE_CORRUPT：候选身份无效。")
        for field in ("planned_ms", "requested_ms", "decoded_ms"):
            if candidate[field] != frame[field]:
                raise PageChangeCacheCorruptError("CACHE_CORRUPT：候选时间与帧集不一致。")
        comparison = selected_comparisons.get(candidate["frame_id"])
        if (
            comparison is None
            or candidate["previous_frame_id"] != comparison["previous_frame_id"]
            or candidate["change_strength"] != comparison["change_strength"]
            or candidate["perceptual_distance"] != comparison["perceptual_distance"]
        ):
            raise PageChangeCacheCorruptError("CACHE_CORRUPT：候选变化强度与比较记录不一致。")
        if candidate["decoded_ms"] <= previous_ms:
            raise PageChangeCacheCorruptError("CACHE_CORRUPT：候选时间未严格递增。")
        previous_ms = candidate["decoded_ms"]
    if selected_ids != {candidate["frame_id"] for candidate in index["candidates"]}:
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：候选与比较选择标记不一致。")
    expected_manifest = _build_manifest(index)
    if manifest != expected_manifest or index_path.read_bytes() != _json_bytes(index) or manifest_path.read_bytes() != _json_bytes(manifest):
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化manifest或序列化无效。")
    if require_directory_name and group.name != expected_id:
        raise PageChangeCacheCorruptError("CACHE_CORRUPT：页面变化目录名无效。")
    return PageChangeGroup(group, index_path, manifest_path, index, manifest, False, False)


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _current_payload(index: Mapping[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": PAGE_CHANGE_CURRENT_SCHEMA_NAME,
        "schema_version": PAGE_CHANGE_CURRENT_SCHEMA_VERSION,
        "artifact_type": PAGE_CHANGE_ARTIFACT_TYPE,
        "result_id": index["page_change_set_id"],
        "group": f"{PAGE_CHANGE_GROUP_DIRECTORY}/{index['page_change_set_id']}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
    }


def _load_current(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读取并复验页面变化current指针；路径不存在表示“尚无current”。

    指针路径存在时（含非普通文件），符号链接（含指向普通文件的活链接和断链）、
    非法JSON、非对象、字段缺失或多余、身份无效、result_id或group不合法、``../``
    路径逃逸、目标缺失和manifest哈希不匹配都按``CURRENT_POINTER_CORRUPT``硬失败。
    """

    path = root / PAGE_CHANGE_CURRENT_FILE
    if not _current_path_state(path, "page changes current", PageChangeCurrentPointerCorruptError):
        return None, None
    pointer = _read_json(path, "page changes current", PageChangeCurrentPointerCorruptError)
    _exact_keys(pointer, _FRAME_CURRENT_KEYS, "page changes current", PageChangeCurrentPointerCorruptError)
    if (
        pointer["schema_name"] != PAGE_CHANGE_CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != PAGE_CHANGE_CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != PAGE_CHANGE_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{PAGE_CHANGE_GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise PageChangeCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：page changes current身份无效。")
    group = artifact_group_path(root, PAGE_CHANGE_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise PageChangeCurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：page changes current的组路径越出正式组目录。"
        )
    manifest_path = group / PAGE_CHANGE_MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise PageChangeCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：page changes current目标缺失或损坏。")
    return pointer, str(pointer["result_id"])


def _replace_current(root: Path, payload: Mapping[str, Any]) -> None:
    temporary = root / f".{PAGE_CHANGE_CURRENT_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(payload))
        os.replace(temporary, root / PAGE_CHANGE_CURRENT_FILE)
    finally:
        # 只按精确路径清理本次调用自己写出的临时指针，不使用通配删除，避免误删
        # 其它调用或其它进程的文件。os.replace成功后该路径已不存在，这里是空操作；
        # 清理本身失败（例如Windows句柄未释放）也不允许覆盖原异常。
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _disk_current_identity(root: Path) -> tuple[str, str] | None:
    """宽容读取磁盘上的current身份，只用于清理和审计决策。

    current的``os.replace``可能已经成功而内存状态尚未更新，因此清理和审计
    不能依赖内存标志。这里任何异常都按“无法确认”处理，不抛出。
    """

    try:
        raw = (root / PAGE_CHANGE_CURRENT_FILE).read_bytes()
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


def _may_discard_published_group(root: Path, result_id: str) -> bool:
    """判断本次发布的新组是否可以被安全删除。

    只有磁盘上的current明确不存在，或明确指向别的结果时才允许删除；current
    存在但无法解析时按“不确定”处理并保留新组。误删current实际指向的组会直接
    造成悬空指针，而多留一个未被引用的正式组不会破坏任何已发布结果：下一次
    运行会在current校验阶段以``CURRENT_POINTER_CORRUPT``硬失败，只有人工修复
    或移除损坏指针之后，该组才可能作为缓存命中被复用。
    """

    if not (root / PAGE_CHANGE_CURRENT_FILE).exists():
        return True
    identity = _disk_current_identity(root)
    return identity is not None and identity[0] != result_id


@contextmanager
def _module_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    locks = m2_root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / PAGE_CHANGE_LOCK_FILE
    payload = _json_bytes({"schema_name": "ModuleLock", "schema_version": "1.0", "task_id": task_id, "module": PAGE_CHANGE_MODULE_TOKEN, "pid": os.getpid()})
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise PageChangeConcurrentRunError("CONCURRENT_RUN：页面变化检测锁已存在。") from exc
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
        raise InjectedPageChangeFailure(f"注入失败：{point}")


def _write_failure(m2_root: Path, *, task_id: str, stage: str, error: BaseException, input_ids: Mapping[str, Any], cache_sha256: str | None, previous_current_id: str | None, cleanup_status: str, current_unchanged: bool) -> None:
    failure_id = f"failure-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    root = m2_root / "failures" / PAGE_CHANGE_MODULE_TOKEN / failure_id
    root.mkdir(parents=True, exist_ok=False)
    code = getattr(error, "error_code", ERROR_PAGE_CHANGE_FAILED)
    message = str(error) if isinstance(error, PageChangeDetectorError) else f"页面变化检测失败：{type(error).__name__}"
    payload = {
        "schema_name": PAGE_CHANGE_FAILURE_SCHEMA_NAME,
        "schema_version": PAGE_CHANGE_FAILURE_SCHEMA_VERSION,
        "failure_id": failure_id,
        "task_id": task_id,
        "module": PAGE_CHANGE_MODULE_TOKEN,
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


def detect_page_changes(
    task_id: str,
    *,
    output_root: str | Path = "outputs",
    profile_id: str = PAGE_CHANGE_V1.profile_id,
    failure_injection: str | None = None,
    _proxy_loader: Callable[[Path, int], ProxyFrame] = _load_proxy,
) -> PageChangeGroup:
    """只读正式帧集，发布页面变化强度、筛选和去重结果。"""

    if failure_injection is not None and failure_injection not in _FAILURE_INJECTION_POINTS:
        raise PageChangeDetectorError("未知的页面变化失败注入点。")
    validate_task_id(task_id)
    profile = resolve_page_change_profile(profile_id)
    output = Path(output_root).expanduser()
    m2_root = output / task_id / "m2"
    frame_set = _load_frame_set_current(output, task_id)
    _, cache_sha256 = page_change_cache_identity(profile, frame_set)
    result_id = _result_id(cache_sha256)
    root = frame_set.root / PAGE_CHANGE_ROOT_DIRECTORY
    groups = root / PAGE_CHANGE_GROUP_DIRECTORY
    group = groups / result_id
    input_ids = {"frame_set_id": frame_set.index["frame_set_id"], "frame_set_manifest_sha256": frame_set.manifest_sha256, "profile_id": profile.profile_id}
    temporary: Path | None = None
    created_group = False
    previous_current_id: str | None = None
    initial_current_identity: tuple[str, str] | None = None
    stage = "prepare"
    cleanup_status = "not_created"
    with _module_lock(m2_root, task_id):
        try:
            # 先记录磁盘上的初始current，异常处理据此判断本次调用是否改变了它。
            initial_current_identity = _disk_current_identity(root)
            stage = "current_validation"
            _, previous_current_id = _load_current(root)
            stage = "cache_validation"
            if group.exists():
                cached = validate_page_change_group(group, frame_set=frame_set, profile=profile)
                if previous_current_id != result_id:
                    _replace_current(root, _current_payload(cached.index, cached.manifest_path.read_bytes()))
                    return PageChangeGroup(cached.group_path, cached.index_path, cached.manifest_path, cached.index, cached.manifest, True, True)
                return PageChangeGroup(cached.group_path, cached.index_path, cached.manifest_path, cached.index, cached.manifest, True, False)
            stage = "image_decode"
            proxies = [_proxy_loader(frame_set.group_path / frame["file"], profile.proxy_width_pixels) for frame in frame_set.index["frames"]]
            duration_ms = int(frame_set.index["source_video"]["video_stream_duration_ms"])
            comparisons, candidates, selection = select_page_changes(frame_set.index["frames"], proxies, duration_ms=duration_ms, profile=profile)
            _inject(failure_injection, "after_detection")
            index = _build_index(task_id, profile, frame_set, cache_sha256, comparisons, candidates, selection)
            manifest = _build_manifest(index)
            groups.mkdir(parents=True, exist_ok=True)
            temporary = groups / f".tmp-{result_id}-{uuid.uuid4().hex}"
            temporary.mkdir(exist_ok=False)
            cleanup_status = "created"
            stage = "index"
            _write_fsync(temporary / PAGE_CHANGE_INDEX_FILE, _json_bytes(index))
            _inject(failure_injection, "after_index")
            stage = "manifest"
            _write_fsync(temporary / PAGE_CHANGE_MANIFEST_FILE, _json_bytes(manifest))
            _inject(failure_injection, "after_manifest")
            stage = "group_validation"
            validate_page_change_group(temporary, frame_set=frame_set, profile=profile, require_directory_name=False)
            stage = "group_publish"
            _inject(failure_injection, "before_group_publish")
            os.replace(temporary, group)
            created_group = True
            cleanup_status = "published"
            _inject(failure_injection, "after_group_publish")
            # 正式组复验必须早于current切换：复验失败时异常处理会删掉新组，
            # 而旧current仍指向旧组，不会留下指向已删除组的悬空指针。
            stage = "group_revalidation"
            published = validate_page_change_group(group, frame_set=frame_set, profile=profile)
            stage = "current_publish"
            _inject(failure_injection, "before_current")
            _replace_current(root, _current_payload(index, (group / PAGE_CHANGE_MANIFEST_FILE).read_bytes()))
            return published
        except BaseException as exc:
            # 清理与审计以磁盘上的current为唯一事实依据：current的os.replace一旦
            # 成功，内存状态可能尚未更新就发生普通异常、KeyboardInterrupt或
            # SystemExit，此时删除新组会留下悬空指针，并把current_unchanged记反。
            current_identity = _disk_current_identity(root)
            if (
                created_group
                and group.exists()
                and _may_discard_published_group(root, result_id)
            ):
                shutil.rmtree(group, ignore_errors=True)
                cleanup_status = "group_removed"
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
                cleanup_status = "temp_removed"
            try:
                _write_failure(m2_root, task_id=task_id, stage=stage, error=exc, input_ids=input_ids, cache_sha256=cache_sha256, previous_current_id=previous_current_id, cleanup_status=cleanup_status, current_unchanged=current_identity == initial_current_identity)
            except BaseException:
                # 审计只是尽力而为：写入失败（含磁盘满、权限错误）或写入期间再次
                # 被中断，都不允许覆盖或替换下面要原样重新抛出的原始异常。
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, PageChangeDetectorError):
                raise
            raise PageChangePublishFailedError(f"无法发布页面变化结果：{type(exc).__name__}") from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
