"""对页面变化候选帧运行OCR并发布不可变OCRSet结果组。

本模块只读取 ``m2/frames/current.json`` 与 ``m2/frames/page-changes/current.json``
指向的不可变正式组：不读取源视频、不取帧、不做页面变化检测、不生成语音文本、
不改字幕。产物位于 ``m2/ocr/``，与帧集、页面变化集合和M1完全隔离。

契约冻结见 ``docs/development/阶段12-OCR契约与引擎选型.md``：
行级四点框（原图像素坐标）、确定性阅读顺序、边界校验、逐帧原文保留、
NFKC/空白规整/casefold归一化后只标 ``duplicate_of`` 不删除文本、
缓存身份绑定实际模型指纹与处理配置、``off/auto/required`` 三模式、
``success/empty/disabled/degraded`` 四状态加 ``reason_codes``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

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
    FrameSamplerError,
    artifact_group_path,
    is_safe_result_id,
    load_frame_set_current,
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


OCR_EXTRACTOR_VERSION = "1.0"
OCR_SET_SCHEMA_NAME = "OCRSet"
OCR_SET_SCHEMA_VERSION = "1.0"
OCR_CACHE_SCHEMA_NAME = "OcrExtractorCacheIdentity"
OCR_CACHE_SCHEMA_VERSION = "1.0"
OCR_MANIFEST_SCHEMA_NAME = "ArtifactGroupManifest"
OCR_MANIFEST_SCHEMA_VERSION = "1.0"
OCR_CURRENT_SCHEMA_NAME = "ArtifactCurrentPointer"
OCR_CURRENT_SCHEMA_VERSION = "1.0"
OCR_FAILURE_SCHEMA_NAME = "ModuleFailureAudit"
OCR_FAILURE_SCHEMA_VERSION = "1.0"

OCR_ARTIFACT_TYPE = "ocr_set"
OCR_MODULE_TOKEN = "ocr"
OCR_RESULT_PREFIX = "ocr-set"
OCR_ROOT_DIRECTORY = "ocr"
OCR_GROUP_DIRECTORY = "formal-results"
OCR_INDEX_FILE = "ocr-set.json"
OCR_MANIFEST_FILE = "manifest.json"
OCR_CURRENT_FILE = "current.json"
OCR_LOCK_FILE = "ocr.lock"

OCR_MODES = ("off", "auto", "required")
OCR_STATUSES = ("success", "empty", "disabled", "degraded")

ERROR_OCR_FAILED = "OCR_FAILED"
ERROR_INPUT_FRAME_SET_MISSING = "INPUT_FRAME_SET_MISSING"
ERROR_INPUT_FRAME_SET_CORRUPT = "INPUT_FRAME_SET_CORRUPT"
ERROR_INPUT_PAGE_CHANGE_SET_MISSING = "INPUT_PAGE_CHANGE_SET_MISSING"
ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT = "INPUT_PAGE_CHANGE_SET_CORRUPT"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"

_FAILURE_INJECTION_POINTS = (
    "after_recognition",
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
_OCR_INDEX_KEYS = {
    "schema_name", "schema_version", "status", "ocr_set_id", "task_id", "mode",
    "profile_id", "profile", "input", "engine", "reason_codes",
    "cache_identity_sha256", "frame_count", "line_count", "filtered_line_count",
    "failed_frame_count", "deduplicated_count", "frames",
}
_OCR_INPUT_KEYS = {"frame_set", "page_change_set"}
_OCR_INPUT_FRAME_SET_KEYS = {
    "frame_set_id", "manifest_sha256", "source_video_file_name",
    "source_video_sha256", "source_video_size_bytes", "width", "height",
}
_OCR_INPUT_PAGE_CHANGE_KEYS = {"page_change_set_id", "manifest_sha256", "profile_id"}
_OCR_ENGINE_KEYS = {
    "engine_id", "engine_version", "runtime", "runtime_version", "model_fingerprints",
}
_OCR_ENGINE_NOT_RUN_KEYS = {"state", "reason"}
_OCR_FRAME_ENTRY_KEYS = {
    "frame_id", "candidate_id", "ordinal", "decoded_ms", "status", "reason_codes",
    "line_count", "filtered_line_count", "lines", "canonical_text", "text_sha256",
    "duplicate_of",
}
_OCR_LINE_KEYS = {"line_index", "text", "confidence", "box"}
_OCR_MANIFEST_KEYS = {
    "schema_name", "schema_version", "artifact_type", "task_id", "result_id",
    "status", "cache_identity_sha256", "inputs", "versions", "frame_count",
    "line_count", "files",
}

_WHITESPACE_RUN = re.compile(r"\s+")


class OcrExtractorError(RuntimeError):
    error_code = ERROR_OCR_FAILED
    recoverable = False
    next_action = "none"


class OcrFailedError(OcrExtractorError):
    """required模式下引擎不可用或识别失败。"""

    error_code = ERROR_OCR_FAILED
    recoverable = True
    next_action = "continue_text_pipeline"


class OcrEngineUnavailableError(OcrFailedError):
    pass


class InputFrameSetMissingError(OcrExtractorError):
    error_code = ERROR_INPUT_FRAME_SET_MISSING
    recoverable = True
    next_action = "run_frame_sampler"


class InputFrameSetCorruptError(OcrExtractorError):
    error_code = ERROR_INPUT_FRAME_SET_CORRUPT


class InputPageChangeSetMissingError(OcrExtractorError):
    error_code = ERROR_INPUT_PAGE_CHANGE_SET_MISSING
    recoverable = True
    next_action = "run_page_change_detector"


class InputPageChangeSetCorruptError(OcrExtractorError):
    error_code = ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT


class OcrCacheCorruptError(OcrExtractorError):
    error_code = ERROR_CACHE_CORRUPT


class OcrCurrentPointerCorruptError(OcrExtractorError):
    error_code = ERROR_CURRENT_POINTER_CORRUPT


class OcrConcurrentRunError(OcrExtractorError):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "await_user_confirmation"


class OcrPublishFailedError(OcrExtractorError):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = True
    next_action = "retry_after_cleanup"


class InjectedOcrFailure(OcrExtractorError):
    pass


class _FrameRecognitionFailure(Exception):
    """单帧识别或输出校验失败；auto记录后继续，required升级为OCR_FAILED。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class OcrProfile:
    profile_id: str
    engine_id: str
    language_coverage: str
    min_line_score: float
    reading_order_row_band_pixels: int
    normalization_version: str
    box_validation_version: str
    deduplication_version: str


OCR_V1 = OcrProfile(
    profile_id="ocr-v1",
    engine_id="rapidocr-onnxruntime",
    language_coverage="zh+en",
    min_line_score=0.5,
    reading_order_row_band_pixels=10,
    normalization_version="nfkc-ws-casefold-v1",
    box_validation_version="source-bounds-v1",
    deduplication_version="frame-canonical-sha256-v1",
)

OCR_PROFILES = {OCR_V1.profile_id: OCR_V1}


@dataclass(frozen=True)
class OcrLine:
    """引擎输出的单行：原文、置信度与原图像素坐标四点框（左上起顺时针）。"""

    text: str
    confidence: float
    box: tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]


class OcrEngine(Protocol):
    """引擎适配接口（契约§二）：绑定描述不创建推理会话，识别按帧调用。"""

    engine_id: str

    def describe_binding(self) -> Mapping[str, Any]: ...

    def recognize(self, image_path: Path) -> Sequence[OcrLine]: ...


class NullOcrEngine:
    """恒返回空的引擎替身，**仅测试使用**。

    生产路径不构造本引擎：``off`` 模式直接跳过引擎（``engine.state=not_run``），
    空候选同样不构造引擎（P4失败矩阵「页面候选为空 → OCR不运行」）。
    """

    engine_id = "null"

    def describe_binding(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "engine_version": OCR_EXTRACTOR_VERSION,
            "runtime": "null",
            "runtime_version": OCR_EXTRACTOR_VERSION,
            "model_fingerprints": {},
        }

    def recognize(self, image_path: Path) -> list[OcrLine]:
        return []


def _rapidocr_binding() -> dict[str, Any]:
    """只读包版本与模型文件指纹，不创建推理会话（缓存命中不加载模型）。"""

    try:
        import importlib.metadata as metadata

        import onnxruntime
        import rapidocr
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise OcrEngineUnavailableError("OCR_FAILED：OCR引擎依赖不可用。") from exc
    models_dir = Path(rapidocr.__file__).parent / "models"
    fingerprints: dict[str, str] = {}
    if models_dir.is_dir():
        for path in sorted(models_dir.glob("*.onnx")):
            fingerprints[path.name] = _file_sha256(path)
    if not fingerprints:  # pragma: no cover - 安装异常形态
        raise OcrEngineUnavailableError("OCR_FAILED：OCR引擎模型文件缺失。")
    return {
        "engine_id": "rapidocr-onnxruntime",
        "engine_version": metadata.version("rapidocr"),
        "runtime": "onnxruntime",
        "runtime_version": onnxruntime.__version__,
        "model_fingerprints": fingerprints,
    }


class RapidOcrEngine:
    """RapidOCR（onnxruntime后端、内嵌PP-OCRv6模型）适配器。

    会话惰性创建：``describe_binding()`` 与构造函数都不加载模型，只有首次
    ``recognize()`` 才建立ONNX会话，保证缓存命中路径零模型加载。
    """

    engine_id = "rapidocr-onnxruntime"

    def __init__(self) -> None:
        self._engine: Any = None

    def describe_binding(self) -> dict[str, Any]:
        return _rapidocr_binding()

    def _ensure_engine(self) -> Any:
        if self._engine is None:
            import logging

            try:
                from rapidocr import RapidOCR
            except ImportError as exc:  # pragma: no cover - 依赖缺失路径
                raise OcrEngineUnavailableError("OCR_FAILED：OCR引擎依赖不可用。") from exc
            logger = logging.getLogger("RapidOCR")
            # rapidocr初始化时会把自身logger重置为INFO并挂stderr handler，
            # 仅setLevel压不住初始化期间的模型加载日志；用logger级过滤器在
            # 初始化窗口内拦截全部记录，完成后固定为WARNING（告警仍保留）。
            def _block_all(record: logging.LogRecord) -> bool:
                return False

            logger.addFilter(_block_all)
            try:
                self._engine = RapidOCR()
            except Exception as exc:
                raise OcrEngineUnavailableError("OCR_FAILED：OCR引擎初始化失败。") from exc
            finally:
                logger.removeFilter(_block_all)
                logger.setLevel(logging.WARNING)
        return self._engine

    def recognize(self, image_path: Path) -> list[OcrLine]:
        engine = self._ensure_engine()
        result = engine(str(image_path))
        boxes = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if boxes is None or txts is None or scores is None:
            return []
        if not (len(boxes) == len(txts) == len(scores)):
            raise _FrameRecognitionFailure("engine_output_invalid", "OCR引擎输出长度不一致。")
        lines: list[OcrLine] = []
        for box, text, score in zip(boxes, txts, scores):
            try:
                points = tuple(
                    (int(round(float(point[0]))), int(round(float(point[1]))))
                    for point in box
                )
            except (TypeError, ValueError) as exc:
                raise _FrameRecognitionFailure("engine_output_invalid", "OCR引擎框坐标无效。") from exc
            if len(points) != 4:
                raise _FrameRecognitionFailure("engine_output_invalid", "OCR引擎返回非四点框。")
            try:
                confidence = float(score)
            except (TypeError, ValueError) as exc:
                raise _FrameRecognitionFailure("engine_output_invalid", "OCR引擎置信度无效。") from exc
            lines.append(
                OcrLine(
                    text=str(text),
                    confidence=confidence,
                    box=points,  # type: ignore[arg-type]
                )
            )
        return lines


def _default_engine_factory(profile: OcrProfile) -> Any:
    if profile.engine_id != RapidOcrEngine.engine_id:
        raise OcrExtractorError(f"不支持的OCR引擎：{profile.engine_id}")
    return RapidOcrEngine()


@dataclass(frozen=True)
class OcrInputContext:
    """已通过完整复验的输入：帧集、页面变化集合与候选帧。"""

    task_id: str
    frame_set: FrameSetInput
    frames_by_id: Mapping[str, Mapping[str, Any]]
    candidates: tuple[Mapping[str, Any], ...]
    page_change_set_id: str
    page_change_manifest_sha256: str
    page_change_profile_id: str
    width: int
    height: int


@dataclass(frozen=True)
class OcrSetGroup:
    group_path: Path
    index_path: Path
    manifest_path: Path
    index: dict[str, Any]
    manifest: dict[str, Any]
    cache_hit: bool
    pointer_repaired: bool


@dataclass(frozen=True)
class OcrDegradedOutcome:
    """auto模式引擎级失败：不发布新组、current不变、审计已写、文本链继续。"""

    task_id: str
    mode: str
    status: str
    reason_codes: tuple[str, ...]
    failure_id: str
    error_code: str


def resolve_ocr_profile(profile_id: str = OCR_V1.profile_id) -> OcrProfile:
    profile = OCR_PROFILES.get(profile_id)
    if profile is None:
        raise OcrExtractorError(f"不支持OCR profile：{profile_id}")
    return profile


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


def _read_json(path: Path, label: str, error_type: type[OcrExtractorError]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise error_type(f"{label}无法读取或JSON无效。") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label}必须是JSON对象。")
    return value


def _current_path_state(path: Path, label: str, error_type: type[OcrExtractorError]) -> bool:
    """current路径是普通文件时返回True，不存在时返回False。

    与F1/F2相同：先判``is_symlink()``——指向普通文件的活符号链接``is_file()``
    同样为True；目录和断链按损坏硬失败，只有确实不存在才视为“无current”。
    """

    if path.is_symlink():
        raise error_type(f"{label}存在但不是普通文件。")
    if path.is_file():
        return True
    if path.exists():
        raise error_type(f"{label}存在但不是普通文件。")
    return False


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str, error_type: type[OcrExtractorError]) -> None:
    if set(value) != expected:
        raise error_type(f"{label}字段集合无效。")


def _safe_relative_file(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


def normalize_line_text(text: str) -> str:
    """行归一化：NFKC → 连续空白规整为单个空格 → 首尾去空白 → casefold。

    禁止无条件删除全部空白：词间单空格保留；原文永不改写，归一化只进入
    ``canonical_text``。
    """

    return _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def frame_canonical_text(texts: Sequence[str]) -> str:
    return "\n".join(normalize_line_text(text) for text in texts)


def _reading_order_key(entry: Mapping[str, Any], band_pixels: int) -> tuple[int, int, int]:
    xs = [point[0] for point in entry["box"]]
    ys = [point[1] for point in entry["box"]]
    return (round(min(ys) / band_pixels), min(xs), min(ys))


def _box_within_bounds(box: Sequence[Sequence[int]], width: int, height: int) -> bool:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    if min(xs) < 0 or max(xs) > width or min(ys) < 0 or max(ys) > height:
        return False
    return max(xs) > min(xs) and max(ys) > min(ys)


def _recognize_frame(
    engine: Any,
    image_path: Path,
    *,
    profile: OcrProfile,
    width: int,
    height: int,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """识别单帧并做置信度过滤、边界校验与确定性阅读顺序排序。"""

    raw_lines = engine.recognize(image_path)
    kept: list[OcrLine] = []
    filtered = 0
    for line in raw_lines:
        if not isinstance(line, OcrLine):
            raise _FrameRecognitionFailure("engine_output_invalid", "OCR引擎返回了非法行对象。")
        if not 0.0 <= line.confidence <= 1.0:
            raise _FrameRecognitionFailure("engine_output_invalid", "OCR引擎置信度越界。")
        if len(line.box) != 4:
            raise _FrameRecognitionFailure("engine_output_invalid", "OCR引擎返回非四点框。")
        if not _box_within_bounds(line.box, width, height):
            raise _FrameRecognitionFailure("box_out_of_bounds", "OCR框越出源分辨率边界。")
        if line.confidence < profile.min_line_score:
            filtered += 1
            continue
        kept.append(line)
    ordered = sorted(
        kept,
        key=lambda item: (
            round(min(point[1] for point in item.box) / profile.reading_order_row_band_pixels),
            min(point[0] for point in item.box),
            min(point[1] for point in item.box),
        ),
    )
    entries = [
        {
            "line_index": line_index,
            "text": line.text,
            "confidence": round(line.confidence, 6),
            "box": [[int(x), int(y)] for x, y in line.box],
        }
        for line_index, line in enumerate(ordered, start=1)
    ]
    reasons = ["low_score_filtered"] if filtered else []
    return entries, filtered, reasons


def _load_frame_set(output_root: Path, task_id: str) -> FrameSetInput:
    """加载并复验输入帧集（指针、组结构、Schema、序列化与manifest清单）。

    复用F1公开入口``load_frame_set_current``做指针复验；组内帧图片的逐字节
    校验只对候选帧执行（见``_load_inputs``），全量图片身份已由F1发布与F2
    输入校验保障。
    """

    frames_root = output_root / task_id / "m2" / "frames"
    try:
        pointer = load_frame_set_current(output_root, task_id)
    except FrameSamplerError as exc:
        raise InputFrameSetCorruptError(f"INPUT_FRAME_SET_CORRUPT：{exc}") from exc
    if pointer is None:
        raise InputFrameSetMissingError("INPUT_FRAME_SET_MISSING：frames current不存在。")
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


def _load_page_change_current(frames_root: Path) -> tuple[dict[str, Any], Path]:
    """加载并复验页面变化current指针；不存在时抛缺失错误。"""

    root = frames_root / PAGE_CHANGE_ROOT_DIRECTORY
    path = root / PAGE_CHANGE_CURRENT_FILE
    if not _current_path_state(path, "page changes current", InputPageChangeSetCorruptError):
        raise InputPageChangeSetMissingError("INPUT_PAGE_CHANGE_SET_MISSING：page changes current不存在。")
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
    return pointer, group


def _load_inputs(output_root: Path, task_id: str) -> OcrInputContext:
    """加载帧集与页面变化两个输入正式组，并逐字节校验候选帧图片。"""

    frame_set = _load_frame_set(output_root, task_id)
    pointer, pc_group = _load_page_change_current(frame_set.root)
    pc_index_path = pc_group / PAGE_CHANGE_INDEX_FILE
    pc_index_probe = _read_json(pc_index_path, "page change set", InputPageChangeSetCorruptError)
    profile_id = pc_index_probe.get("profile_id")
    if not isinstance(profile_id, str):
        raise InputPageChangeSetCorruptError("INPUT_PAGE_CHANGE_SET_CORRUPT：页面变化profile无效。")
    try:
        pc_profile = resolve_page_change_profile(profile_id)
        validated = validate_page_change_group(pc_group, frame_set=frame_set, profile=pc_profile)
    except PageChangeDetectorError as exc:
        raise InputPageChangeSetCorruptError(f"INPUT_PAGE_CHANGE_SET_CORRUPT：{exc}") from exc
    pc_index = validated.index
    frames_by_id = {str(frame["frame_id"]): frame for frame in frame_set.index["frames"]}
    candidates = tuple(pc_index["candidates"])
    width = int(frame_set.index["source_video"]["width"])
    height = int(frame_set.index["source_video"]["height"])
    for candidate in candidates:
        frame = frames_by_id.get(str(candidate["frame_id"]))
        if frame is None:
            raise InputPageChangeSetCorruptError("INPUT_PAGE_CHANGE_SET_CORRUPT：候选引用未知帧。")
        image_path = frame_set.group_path / str(frame["file"])
        if (
            not image_path.is_file()
            or image_path.stat().st_size != frame["size_bytes"]
            or _file_sha256(image_path) != frame["sha256"]
        ):
            raise InputFrameSetCorruptError("INPUT_FRAME_SET_CORRUPT：候选帧图片缺失或身份不匹配。")
    return OcrInputContext(
        task_id=task_id,
        frame_set=frame_set,
        frames_by_id=frames_by_id,
        candidates=candidates,
        page_change_set_id=str(pc_index["page_change_set_id"]),
        page_change_manifest_sha256=str(pointer["manifest_sha256"]),
        page_change_profile_id=profile_id,
        width=width,
        height=height,
    )


def ocr_cache_identity(
    profile: OcrProfile,
    mode: str,
    context: OcrInputContext,
    engine_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    payload = {
        "schema_name": OCR_CACHE_SCHEMA_NAME,
        "schema_version": OCR_CACHE_SCHEMA_VERSION,
        "extractor_version": OCR_EXTRACTOR_VERSION,
        "mode": mode,
        "profile": asdict(profile),
        "input_frame_set_id": context.frame_set.index["frame_set_id"],
        "input_frame_set_manifest_sha256": context.frame_set.manifest_sha256,
        "input_page_change_set_id": context.page_change_set_id,
        "input_page_change_set_manifest_sha256": context.page_change_manifest_sha256,
        "engine_binding": dict(engine_binding),
    }
    return payload, _sha256_bytes(_json_bytes(payload))


def _result_id(cache_sha256: str) -> str:
    return f"{OCR_RESULT_PREFIX}-{cache_sha256[:12]}"


def _build_index(
    context: OcrInputContext,
    profile: OcrProfile,
    mode: str,
    cache_sha256: str,
    engine_binding: Mapping[str, Any],
    status: str,
    reason_codes: Sequence[str],
    frames: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result_id = _result_id(cache_sha256)
    source = context.frame_set.index["source_video"]
    return {
        "schema_name": OCR_SET_SCHEMA_NAME,
        "schema_version": OCR_SET_SCHEMA_VERSION,
        "status": status,
        "ocr_set_id": result_id,
        "task_id": context.task_id,
        "mode": mode,
        "profile_id": profile.profile_id,
        "profile": asdict(profile),
        "input": {
            "frame_set": {
                "frame_set_id": context.frame_set.index["frame_set_id"],
                "manifest_sha256": context.frame_set.manifest_sha256,
                "source_video_file_name": source["file_name"],
                "source_video_sha256": source["sha256"],
                "source_video_size_bytes": source["size_bytes"],
                "width": context.width,
                "height": context.height,
            },
            "page_change_set": {
                "page_change_set_id": context.page_change_set_id,
                "manifest_sha256": context.page_change_manifest_sha256,
                "profile_id": context.page_change_profile_id,
            },
        },
        "engine": dict(engine_binding),
        "reason_codes": list(reason_codes),
        "cache_identity_sha256": cache_sha256,
        "frame_count": len(frames),
        "line_count": sum(int(frame["line_count"]) for frame in frames),
        "filtered_line_count": sum(int(frame["filtered_line_count"]) for frame in frames),
        "failed_frame_count": sum(1 for frame in frames if frame["status"] == "failed"),
        "deduplicated_count": sum(1 for frame in frames if frame["duplicate_of"] is not None),
        "frames": [dict(frame) for frame in frames],
    }


def _build_manifest(index: Mapping[str, Any]) -> dict[str, Any]:
    index_bytes = _json_bytes(index)
    return {
        "schema_name": OCR_MANIFEST_SCHEMA_NAME,
        "schema_version": OCR_MANIFEST_SCHEMA_VERSION,
        "artifact_type": OCR_ARTIFACT_TYPE,
        "task_id": index["task_id"],
        "result_id": index["ocr_set_id"],
        "status": index["status"],
        "cache_identity_sha256": index["cache_identity_sha256"],
        "inputs": {
            "frame_set_id": index["input"]["frame_set"]["frame_set_id"],
            "frame_set_manifest_sha256": index["input"]["frame_set"]["manifest_sha256"],
            "page_change_set_id": index["input"]["page_change_set"]["page_change_set_id"],
            "page_change_set_manifest_sha256": index["input"]["page_change_set"]["manifest_sha256"],
        },
        "versions": {
            "ocr_extractor": OCR_EXTRACTOR_VERSION,
            "ocr_set_schema": OCR_SET_SCHEMA_VERSION,
            "artifact_group_manifest": OCR_MANIFEST_SCHEMA_VERSION,
        },
        "frame_count": index["frame_count"],
        "line_count": index["line_count"],
        "files": {OCR_INDEX_FILE: {"sha256": _sha256_bytes(index_bytes), "bytes": len(index_bytes)}},
    }


def _validate_engine_binding(value: Any) -> None:
    if not isinstance(value, dict):
        raise OcrCacheCorruptError("CACHE_CORRUPT：engine绑定无效。")
    if value.get("state") == "not_run":
        _exact_keys(value, _OCR_ENGINE_NOT_RUN_KEYS, "engine绑定", OcrCacheCorruptError)
        if value.get("reason") not in {"disabled", "no_candidate_frames"}:
            raise OcrCacheCorruptError("CACHE_CORRUPT：engine未运行原因无效。")
        return
    _exact_keys(value, _OCR_ENGINE_KEYS, "engine绑定", OcrCacheCorruptError)
    fingerprints = value.get("model_fingerprints")
    if not isinstance(fingerprints, dict) or not fingerprints:
        raise OcrCacheCorruptError("CACHE_CORRUPT：模型指纹无效。")


def validate_ocr_group(
    group_path: str | Path,
    *,
    context: OcrInputContext,
    profile: OcrProfile,
    mode: str,
    engine_binding: Mapping[str, Any],
    require_directory_name: bool = True,
) -> OcrSetGroup:
    """完整复验OCRSet结果组：成员、Schema、身份、计数、逐帧结构、去重关系、
    阅读顺序、框边界、归一化文本、manifest与确定性序列化。"""

    group = Path(group_path)
    if not group.is_dir() or {entry.name for entry in group.iterdir()} != {OCR_INDEX_FILE, OCR_MANIFEST_FILE}:
        raise OcrCacheCorruptError("CACHE_CORRUPT：OCR结果组成员无效。")
    index_path = group / OCR_INDEX_FILE
    manifest_path = group / OCR_MANIFEST_FILE
    index = _read_json(index_path, "OCR集合", OcrCacheCorruptError)
    manifest = _read_json(manifest_path, "OCR manifest", OcrCacheCorruptError)
    _exact_keys(index, _OCR_INDEX_KEYS, "OCR集合", OcrCacheCorruptError)
    _exact_keys(manifest, _OCR_MANIFEST_KEYS, "OCR manifest", OcrCacheCorruptError)
    _, cache_sha256 = ocr_cache_identity(profile, mode, context, engine_binding)
    expected_id = _result_id(cache_sha256)
    if (
        index["schema_name"] != OCR_SET_SCHEMA_NAME
        or index["schema_version"] != OCR_SET_SCHEMA_VERSION
        or index["status"] not in OCR_STATUSES
        or index["ocr_set_id"] != expected_id
        or index["task_id"] != context.task_id
        or index["mode"] != mode
        or index["profile_id"] != profile.profile_id
        or index["profile"] != asdict(profile)
        or index["cache_identity_sha256"] != cache_sha256
    ):
        raise OcrCacheCorruptError("CACHE_CORRUPT：OCR集合身份无效。")
    _exact_keys(index["input"], _OCR_INPUT_KEYS, "OCR input", OcrCacheCorruptError)
    _exact_keys(index["input"]["frame_set"], _OCR_INPUT_FRAME_SET_KEYS, "OCR input frame_set", OcrCacheCorruptError)
    _exact_keys(index["input"]["page_change_set"], _OCR_INPUT_PAGE_CHANGE_KEYS, "OCR input page_change_set", OcrCacheCorruptError)
    source = context.frame_set.index["source_video"]
    if (
        index["input"]["frame_set"]["frame_set_id"] != context.frame_set.index["frame_set_id"]
        or index["input"]["frame_set"]["manifest_sha256"] != context.frame_set.manifest_sha256
        or index["input"]["frame_set"]["source_video_file_name"] != source["file_name"]
        or index["input"]["frame_set"]["source_video_sha256"] != source["sha256"]
        or index["input"]["frame_set"]["source_video_size_bytes"] != source["size_bytes"]
        or index["input"]["frame_set"]["width"] != context.width
        or index["input"]["frame_set"]["height"] != context.height
        or index["input"]["page_change_set"]["page_change_set_id"] != context.page_change_set_id
        or index["input"]["page_change_set"]["manifest_sha256"] != context.page_change_manifest_sha256
        or index["input"]["page_change_set"]["profile_id"] != context.page_change_profile_id
    ):
        raise OcrCacheCorruptError("CACHE_CORRUPT：OCR输入身份不一致。")
    if index["engine"] != dict(engine_binding):
        raise OcrCacheCorruptError("CACHE_CORRUPT：engine绑定与当前身份不一致。")
    _validate_engine_binding(index["engine"])
    if not isinstance(index["reason_codes"], list) or any(not isinstance(code, str) for code in index["reason_codes"]):
        raise OcrCacheCorruptError("CACHE_CORRUPT：reason_codes无效。")
    frames = index["frames"]
    if not isinstance(frames, list) or index["frame_count"] != len(frames):
        raise OcrCacheCorruptError("CACHE_CORRUPT：OCR frame_count无效。")
    status = index["status"]
    failed_frames = [frame for frame in frames if isinstance(frame, dict) and frame.get("status") == "failed"]
    if index["failed_frame_count"] != len(failed_frames):
        raise OcrCacheCorruptError("CACHE_CORRUPT：failed_frame_count不一致。")
    if (status == "disabled") != (mode == "off"):
        raise OcrCacheCorruptError("CACHE_CORRUPT：disabled状态与模式不一致。")
    if mode == "off" and frames:
        raise OcrCacheCorruptError("CACHE_CORRUPT：disabled集合不得含帧条目。")
    if status == "degraded" and (mode != "auto" or not failed_frames):
        raise OcrCacheCorruptError("CACHE_CORRUPT：degraded状态与模式或失败帧不一致。")
    if status == "success" and (failed_frames or index["line_count"] == 0):
        raise OcrCacheCorruptError("CACHE_CORRUPT：success状态与内容不一致。")
    if status == "empty" and frames and index["line_count"] != 0:
        raise OcrCacheCorruptError("CACHE_CORRUPT：empty状态与行数不一致。")
    if mode != "off" and len(frames) != len(context.candidates):
        raise OcrCacheCorruptError("CACHE_CORRUPT：帧条目与候选数量不一致。")
    total_lines = 0
    total_filtered = 0
    first_seen: dict[str, str] = {}
    previous_ms = -1
    for frame, candidate in (
        zip(frames, context.candidates) if mode != "off" else []
    ):
        if not isinstance(frame, dict):
            raise OcrCacheCorruptError("CACHE_CORRUPT：帧条目无效。")
        _exact_keys(frame, _OCR_FRAME_ENTRY_KEYS, "OCR帧条目", OcrCacheCorruptError)
        if (
            frame["frame_id"] != candidate["frame_id"]
            or frame["candidate_id"] != candidate["candidate_id"]
            or frame["ordinal"] != candidate["ordinal"]
            or frame["decoded_ms"] != candidate["decoded_ms"]
        ):
            raise OcrCacheCorruptError("CACHE_CORRUPT：帧条目与候选不一致。")
        if frame["status"] not in {"ok", "failed"}:
            raise OcrCacheCorruptError("CACHE_CORRUPT：帧状态无效。")
        if not isinstance(frame["reason_codes"], list):
            raise OcrCacheCorruptError("CACHE_CORRUPT：帧reason_codes无效。")
        if not isinstance(frame["decoded_ms"], int) or frame["decoded_ms"] <= previous_ms:
            raise OcrCacheCorruptError("CACHE_CORRUPT：帧时间未严格递增。")
        previous_ms = frame["decoded_ms"]
        lines = frame["lines"]
        if not isinstance(lines, list):
            raise OcrCacheCorruptError("CACHE_CORRUPT：lines无效。")
        if frame["line_count"] != len(lines):
            raise OcrCacheCorruptError("CACHE_CORRUPT：line_count不一致。")
        if not isinstance(frame["filtered_line_count"], int) or frame["filtered_line_count"] < 0:
            raise OcrCacheCorruptError("CACHE_CORRUPT：filtered_line_count无效。")
        if frame["status"] == "failed" and (lines or not frame["reason_codes"]):
            raise OcrCacheCorruptError("CACHE_CORRUPT：失败帧内容无效。")
        texts: list[str] = []
        for line_index, line in enumerate(lines, start=1):
            if not isinstance(line, dict):
                raise OcrCacheCorruptError("CACHE_CORRUPT：行条目无效。")
            _exact_keys(line, _OCR_LINE_KEYS, "OCR行", OcrCacheCorruptError)
            if line["line_index"] != line_index or not isinstance(line["text"], str):
                raise OcrCacheCorruptError("CACHE_CORRUPT：行序号或文本无效。")
            confidence = line["confidence"]
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= confidence <= 1.0:
                raise OcrCacheCorruptError("CACHE_CORRUPT：置信度无效。")
            if confidence < profile.min_line_score:
                raise OcrCacheCorruptError("CACHE_CORRUPT：置信度低于过滤阈值仍被保留。")
            box = line["box"]
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(
                    not isinstance(point, list) or len(point) != 2
                    or not all(isinstance(value, int) and not isinstance(value, bool) for value in point)
                    for point in box
                )
            ):
                raise OcrCacheCorruptError("CACHE_CORRUPT：框格式无效。")
            if not _box_within_bounds(box, int(context.width), int(context.height)):
                raise OcrCacheCorruptError("CACHE_CORRUPT：框越出源分辨率边界。")
            texts.append(line["text"])
        if lines:
            expected_order = sorted(
                range(len(lines)),
                key=lambda i: _reading_order_key(lines[i], profile.reading_order_row_band_pixels),
            )
            if expected_order != list(range(len(lines))):
                raise OcrCacheCorruptError("CACHE_CORRUPT：行阅读顺序无效。")
        canonical = frame_canonical_text(texts)
        if frame["canonical_text"] != canonical:
            raise OcrCacheCorruptError("CACHE_CORRUPT：canonical_text不一致。")
        if frame["text_sha256"] != _sha256_bytes(canonical.encode("utf-8")):
            raise OcrCacheCorruptError("CACHE_CORRUPT：text_sha256不一致。")
        expected_duplicate: str | None = None
        if lines:
            digest = frame["text_sha256"]
            if digest in first_seen:
                expected_duplicate = first_seen[digest]
            else:
                first_seen[digest] = str(frame["frame_id"])
        if frame["duplicate_of"] != expected_duplicate:
            raise OcrCacheCorruptError("CACHE_CORRUPT：duplicate_of关系无效。")
        if frame["duplicate_of"] is not None and frame["status"] != "ok":
            raise OcrCacheCorruptError("CACHE_CORRUPT：失败帧不得参与去重。")
        total_lines += len(lines)
        total_filtered += int(frame["filtered_line_count"])
    if index["line_count"] != total_lines or index["filtered_line_count"] != total_filtered:
        raise OcrCacheCorruptError("CACHE_CORRUPT：行数汇总不一致。")
    if index["deduplicated_count"] != sum(1 for frame in frames if isinstance(frame, dict) and frame.get("duplicate_of") is not None):
        raise OcrCacheCorruptError("CACHE_CORRUPT：deduplicated_count不一致。")
    expected_manifest = _build_manifest(index)
    if (
        manifest != expected_manifest
        or index_path.read_bytes() != _json_bytes(index)
        or manifest_path.read_bytes() != _json_bytes(manifest)
    ):
        raise OcrCacheCorruptError("CACHE_CORRUPT：OCR manifest或序列化无效。")
    if require_directory_name and group.name != expected_id:
        raise OcrCacheCorruptError("CACHE_CORRUPT：OCR目录名无效。")
    return OcrSetGroup(group, index_path, manifest_path, index, manifest, False, False)


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _current_payload(index: Mapping[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": OCR_CURRENT_SCHEMA_NAME,
        "schema_version": OCR_CURRENT_SCHEMA_VERSION,
        "artifact_type": OCR_ARTIFACT_TYPE,
        "result_id": index["ocr_set_id"],
        "group": f"{OCR_GROUP_DIRECTORY}/{index['ocr_set_id']}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
    }


def _load_current(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读取并复验OCR current指针；路径不存在表示“尚无current”。

    指针路径存在时（含非普通文件），符号链接、非法JSON、字段缺失或多余、
    身份无效、路径逃逸、目标缺失和manifest哈希不匹配都按
    ``CURRENT_POINTER_CORRUPT``硬失败。
    """

    path = root / OCR_CURRENT_FILE
    if not _current_path_state(path, "ocr current", OcrCurrentPointerCorruptError):
        return None, None
    pointer = _read_json(path, "ocr current", OcrCurrentPointerCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "ocr current", OcrCurrentPointerCorruptError)
    if (
        pointer["schema_name"] != OCR_CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != OCR_CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != OCR_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{OCR_GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise OcrCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：ocr current身份无效。")
    group = artifact_group_path(root, OCR_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise OcrCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：ocr current的组路径越出正式组目录。")
    manifest_path = group / OCR_MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise OcrCurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：ocr current目标缺失或损坏。")
    return pointer, str(pointer["result_id"])


def _replace_current(root: Path, payload: Mapping[str, Any]) -> None:
    temporary = root / f".{OCR_CURRENT_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(payload))
        os.replace(temporary, root / OCR_CURRENT_FILE)
    finally:
        # 只按精确路径清理本次调用自己写出的临时指针（与F1/F2相同）。
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _disk_current_identity(root: Path) -> tuple[str, str] | None:
    """宽容读取磁盘上的current身份，只用于清理和审计决策。"""

    try:
        raw = (root / OCR_CURRENT_FILE).read_bytes()
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

    if not (root / OCR_CURRENT_FILE).exists():
        return True
    identity = _disk_current_identity(root)
    return identity is not None and identity[0] != result_id


@contextmanager
def _module_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    locks = m2_root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / OCR_LOCK_FILE
    payload = _json_bytes({
        "schema_name": "ModuleLock",
        "schema_version": "1.0",
        "task_id": task_id,
        "module": OCR_MODULE_TOKEN,
        "pid": os.getpid(),
    })
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise OcrConcurrentRunError("CONCURRENT_RUN：OCR锁已存在。") from exc
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
        raise InjectedOcrFailure(f"注入失败：{point}")


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
    failure_id = f"failure-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    root = m2_root / "failures" / OCR_MODULE_TOKEN / failure_id
    root.mkdir(parents=True, exist_ok=False)
    code = getattr(error, "error_code", ERROR_OCR_FAILED)
    message = str(error) if isinstance(error, OcrExtractorError) else f"OCR失败：{type(error).__name__}"
    payload = {
        "schema_name": OCR_FAILURE_SCHEMA_NAME,
        "schema_version": OCR_FAILURE_SCHEMA_VERSION,
        "failure_id": failure_id,
        "task_id": task_id,
        "module": OCR_MODULE_TOKEN,
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


def _extract_frames(
    engine: Any,
    context: OcrInputContext,
    profile: OcrProfile,
    mode: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按候选时间序逐帧识别；auto记录失败帧继续，required升级为OCR_FAILED。"""

    entries: list[dict[str, Any]] = []
    aggregate_reasons: set[str] = set()
    first_seen: dict[str, str] = {}
    for candidate in context.candidates:
        frame = context.frames_by_id[str(candidate["frame_id"])]
        image_path = context.frame_set.group_path / str(frame["file"])
        status = "ok"
        try:
            lines, filtered, reasons = _recognize_frame(
                engine, image_path, profile=profile, width=context.width, height=context.height
            )
        except _FrameRecognitionFailure as exc:
            if mode == "required":
                raise OcrFailedError(f"OCR_FAILED：候选帧识别失败（{exc.reason}）。") from exc
            status = "failed"
            lines, filtered, reasons = [], 0, [exc.reason]
        except OcrEngineUnavailableError:
            raise
        except Exception as exc:
            if mode == "required":
                raise OcrFailedError(f"OCR_FAILED：候选帧识别失败（recognition_failed）。") from exc
            status = "failed"
            lines, filtered, reasons = [], 0, ["recognition_failed"]
        texts = [line["text"] for line in lines]
        canonical = frame_canonical_text(texts)
        digest = _sha256_bytes(canonical.encode("utf-8"))
        duplicate_of: str | None = None
        if lines:
            if digest in first_seen:
                duplicate_of = first_seen[digest]
            else:
                first_seen[digest] = str(candidate["frame_id"])
        if status == "failed":
            aggregate_reasons.update(reasons)
        elif "low_score_filtered" in reasons:
            aggregate_reasons.add("low_score_filtered")
        entries.append({
            "frame_id": candidate["frame_id"],
            "candidate_id": candidate["candidate_id"],
            "ordinal": candidate["ordinal"],
            "decoded_ms": candidate["decoded_ms"],
            "status": status,
            "reason_codes": sorted(reasons),
            "line_count": len(lines),
            "filtered_line_count": filtered,
            "lines": lines,
            "canonical_text": canonical,
            "text_sha256": digest,
            "duplicate_of": duplicate_of,
        })
    return entries, sorted(aggregate_reasons)


def run_ocr(
    task_id: str,
    *,
    output_root: str | Path = "outputs",
    mode: str = "auto",
    profile_id: str = OCR_V1.profile_id,
    failure_injection: str | None = None,
    _engine_factory: Callable[[OcrProfile], Any] = _default_engine_factory,
) -> OcrSetGroup | OcrDegradedOutcome:
    """只读候选帧，按模式运行OCR并发布不可变结果组。

    ``off``跳过引擎发布``disabled``；候选为空不构造引擎发布``empty``；
    ``auto``引擎级失败写审计并返回降级结果（不发布、current不变）；
    ``required``引擎级或帧级失败抛``OCR_FAILED``（不发布、current不变）。
    """

    if mode not in OCR_MODES:
        raise OcrExtractorError(f"不支持OCR模式：{mode}")
    if failure_injection is not None and failure_injection not in _FAILURE_INJECTION_POINTS:
        raise OcrExtractorError("未知的OCR失败注入点。")
    validate_task_id(task_id)
    profile = resolve_ocr_profile(profile_id)
    output = Path(output_root).expanduser()
    m2_root = output / task_id / "m2"
    context = _load_inputs(output, task_id)
    root = m2_root / OCR_ROOT_DIRECTORY
    input_ids = {
        "frame_set_id": context.frame_set.index["frame_set_id"],
        "frame_set_manifest_sha256": context.frame_set.manifest_sha256,
        "page_change_set_id": context.page_change_set_id,
        "page_change_set_manifest_sha256": context.page_change_manifest_sha256,
        "profile_id": profile.profile_id,
        "mode": mode,
    }

    engine: Any = None
    if mode == "off":
        engine_binding: Mapping[str, Any] = {"state": "not_run", "reason": "disabled"}
    elif not context.candidates:
        engine_binding = {"state": "not_run", "reason": "no_candidate_frames"}
    else:
        try:
            engine = _engine_factory(profile)
            engine_binding = dict(engine.describe_binding())
        except OcrExtractorError as exc:
            disk_identity = _disk_current_identity(root)
            failure_id = _write_failure(
                m2_root,
                task_id=task_id,
                stage="engine_init",
                error=exc,
                input_ids=input_ids,
                cache_sha256=None,
                previous_current_id=disk_identity[0] if disk_identity else None,
                cleanup_status="not_created",
                current_unchanged=True,
            )
            if mode == "required":
                raise
            return OcrDegradedOutcome(
                task_id=task_id,
                mode=mode,
                status="degraded",
                reason_codes=("engine_unavailable",),
                failure_id=failure_id,
                error_code=getattr(exc, "error_code", ERROR_OCR_FAILED),
            )

    _, cache_sha256 = ocr_cache_identity(profile, mode, context, engine_binding)
    result_id = _result_id(cache_sha256)
    groups = root / OCR_GROUP_DIRECTORY
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
                cached = validate_ocr_group(
                    group,
                    context=context,
                    profile=profile,
                    mode=mode,
                    engine_binding=engine_binding,
                )
                if previous_current_id != result_id:
                    _replace_current(root, _current_payload(cached.index, cached.manifest_path.read_bytes()))
                    return OcrSetGroup(cached.group_path, cached.index_path, cached.manifest_path, cached.index, cached.manifest, True, True)
                return OcrSetGroup(cached.group_path, cached.index_path, cached.manifest_path, cached.index, cached.manifest, True, False)
            stage = "recognition"
            if mode == "off":
                status = "disabled"
                reason_codes = ["engine_disabled"]
                frame_entries: list[dict[str, Any]] = []
                frame_reasons: list[str] = []
            elif not context.candidates:
                status = "empty"
                reason_codes = ["no_candidate_frames"]
                frame_entries = []
                frame_reasons = []
            else:
                try:
                    frame_entries, frame_reasons = _extract_frames(engine, context, profile, mode)
                except OcrEngineUnavailableError as exc:
                    # 惰性会话可能到首帧识别才失败：auto写审计并返回降级结果
                    #（不发布、current不变）；required经外层审计路径升级抛出。
                    if mode == "required":
                        raise
                    failure_id = _write_failure(
                        m2_root,
                        task_id=task_id,
                        stage="engine_init",
                        error=exc,
                        input_ids=input_ids,
                        cache_sha256=cache_sha256,
                        previous_current_id=previous_current_id,
                        cleanup_status="not_created",
                        current_unchanged=_disk_current_identity(root) == initial_current_identity,
                    )
                    return OcrDegradedOutcome(
                        task_id=task_id,
                        mode=mode,
                        status="degraded",
                        reason_codes=("engine_unavailable",),
                        failure_id=failure_id,
                        error_code=exc.error_code,
                    )
                failed = sum(1 for entry in frame_entries if entry["status"] == "failed")
                lines_total = sum(entry["line_count"] for entry in frame_entries)
                if failed:
                    status = "degraded"
                elif lines_total == 0:
                    status = "empty"
                    frame_reasons = [*frame_reasons, "no_text_detected"]
                else:
                    status = "success"
                reason_codes = sorted(set(frame_reasons))
            _inject(failure_injection, "after_recognition")
            index = _build_index(context, profile, mode, cache_sha256, engine_binding, status, reason_codes, frame_entries)
            manifest = _build_manifest(index)
            groups.mkdir(parents=True, exist_ok=True)
            temporary = groups / f".tmp-{result_id}-{uuid.uuid4().hex}"
            temporary.mkdir(exist_ok=False)
            cleanup_status = "created"
            stage = "index"
            _write_fsync(temporary / OCR_INDEX_FILE, _json_bytes(index))
            _inject(failure_injection, "after_index")
            stage = "manifest"
            _write_fsync(temporary / OCR_MANIFEST_FILE, _json_bytes(manifest))
            _inject(failure_injection, "after_manifest")
            stage = "group_validation"
            validate_ocr_group(
                temporary,
                context=context,
                profile=profile,
                mode=mode,
                engine_binding=engine_binding,
                require_directory_name=False,
            )
            stage = "group_publish"
            _inject(failure_injection, "before_group_publish")
            os.replace(temporary, group)
            created_group = True
            cleanup_status = "published"
            _inject(failure_injection, "after_group_publish")
            # 正式组复验必须早于current切换（与F1/F2一致）。
            stage = "group_revalidation"
            published = validate_ocr_group(
                group,
                context=context,
                profile=profile,
                mode=mode,
                engine_binding=engine_binding,
            )
            stage = "current_publish"
            _inject(failure_injection, "before_current")
            _replace_current(root, _current_payload(index, (group / OCR_MANIFEST_FILE).read_bytes()))
            return published
        except BaseException as exc:
            # 清理与审计以磁盘上的current为唯一事实依据（与F1/F2一致）。
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
            if isinstance(exc, OcrExtractorError):
                raise
            raise OcrPublishFailedError(f"无法发布OCR结果：{type(exc).__name__}") from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
