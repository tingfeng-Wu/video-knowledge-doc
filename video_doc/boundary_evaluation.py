"""阶段16边界评价程序：BoundaryGroundTruth 1.0＋boundary-evaluation-policy-1.0。

严格按《阶段16-标注格式与评价程序裁决》§三/§四逐条实现（暂定口径，正式冻结属G2）：

- 两层结构（裁决Q1）：第一层纯函数指标核（整数毫秒列表进、内存dict出，不读盘）；
  第二层身份绑定适配层``evaluate_chapter_plan_against_annotation``经既有公开入口
  ``load_chapter_plan_current``＋``load_published_chapter_plan``读取预测侧，经
  ``load_ground_truth``读取标注侧，任一身份不符``ANNOTATION_IDENTITY_MISMATCH``；
- 边界集合（裁决Q2）：只取内部边界（预测侧``boundaries[1:]``、真值侧
  ``chapters[1:].start_ms``）；首条恒0与视频终点一律排除；emergency单章与单章真值
  都按空集正常计算，无特判；
- 匹配（裁决Q3）：保序DP一对一最优匹配（最大配对数优先、同数下最小总绝对差、
  ①配对→②跳gt→③跳pred的确定性tie-break，前进与回溯同优先级）；容差闭区间
  ``|diff| <= T``；F1@15s与F1@30s各自独立重算匹配集，禁止截断复用；
- MAE（裁决Q4）：基于最大容差（默认30s）匹配集；零匹配``mae_ms=null``并报
  ``mae_matched_count=0``，禁止0.0歧义；单位毫秒不取整；
- null三值约定（裁决Q5）：pred空gt非空→P=null/R=0.0/F1=0.0；gt空pred非空→
  P=0.0/R=null/F1=0.0；双空→全null；一侧null另一侧（若非null）必为0.0；
  模块内部零舍入（全精度原则）；
- 聚合（裁决Q6）：宏平均＝逐视频非null等权均值；标准差样本口径ddof=1，非null
  值<2时null（禁止ddof=0伪装）；每指标报``non_null_count``/``null_count``；
  聚合指标名七项钉死；输入含非默认容差记录或缺键→``EVALUATION_INPUT_INVALID``；
- 标注Schema（裁决Q7/§三.1）：``BoundaryGroundTruth 1.0``顶层12键、章条目5键
  封闭精确键集；Schema全族校验先于reviewed判定；
- 失败语义（裁决Q8）：类型化``BoundaryEvaluationError``＋6码封闭集；
  boundary_planner_v2既有类型化错误原样传播，不包裹不吞码；
- 本轮零CLI、零发布产物组、模块零写入（裁决Q8.4/Q8.5）；不做任何真实标注数据。

实现决定（提交G2核对，2026-10-31实现轮）：

1. §三.2/§三.3标题计数「9键」「5键」与其json字面块键数（10、6）不一致；按裁决
   正文纪律「数值全部写死」以**json字面键集为准**（Result含schema两键共10键、
   Aggregate含schema两键共6键），计数标签按笔误登记——同G1对14-A裁决§五.4
   「13个vs表格17个」计数笔误的先例处置，语义零影响；
2. 适配层执行顺序＝标注先于预测侧（裁决未钉死；矩阵用例不依赖该顺序）；
3. 标注JSON解析失败（非法JSON/顶层非对象）归``ANNOTATION_SCHEMA_INVALID``
   （§三.1「校验规则全族」涵盖文档形态本身）；
4. 标注``reviewed==false``时``reviewer``/``reviewed_at``只按类型面约束
   （非空str或null；str时须匹配日期形态），``reviewed==true``时按§三.1第5条
   强制非空str＋日期形态；
5. ``title``的``len<=200``按原文字符数（未strip后的原串）判；
6. 纯函数列表入参接受list/tuple（拒绝str/bytes/Mapping等其它Sequence）；
   ``tolerances_ms``非空为隐含前提（``mae_tolerance_ms==max(tolerances_ms)``
   要求至少一项，§三.2）；
7. 聚合记录键面：裁决只钉死「缺键与非默认容差非法」（Q8.2），故校验
   ``task_id``/``result``两键存在与result形态，**不**拒绝记录对象上的多余键；
8. 适配层``annotation.sha256``为读文件字节实时重算值（Q1.3），与
   ``load_ground_truth``解析所用同一次读取的字节一致；
9. ``relative_path``在可解析时以POSIX分隔符呈现（``as_posix()``），否则退回
   文件名（§三.3脱敏口径的呈现细化）。

只读边界：本模块不写任何文件/目录/日志；不触碰``outputs/``（O1真实树复验为
只读加载）；既有模块零改动（仅复用两个公开导入）。M1与全部已发布正式组只读。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .boundary_planner_v2 import (
    load_chapter_plan_current,
    load_published_chapter_plan,
)

__all__ = [
    "BOUNDARY_EVALUATION_POLICY_VERSION",
    "GROUND_TRUTH_SCHEMA_NAME",
    "GROUND_TRUTH_SCHEMA_VERSION",
    "DEFAULT_TOLERANCES_MS",
    "BoundaryEvaluationError",
    "AnnotationFileMissingError",
    "AnnotationSchemaInvalidError",
    "AnnotationNotReviewedError",
    "AnnotationIdentityMismatchError",
    "EvaluationInputInvalidError",
    "InputChapterPlanMissingError",
    "load_ground_truth",
    "ground_truth_internal_boundaries",
    "extract_internal_boundaries",
    "match_boundaries_one_to_one",
    "evaluate_boundaries",
    "aggregate_boundary_evaluations",
    "evaluate_chapter_plan_against_annotation",
]

# 版本与常量（裁决Q10.1/§三.4，钉死）
BOUNDARY_EVALUATION_POLICY_VERSION = "boundary-evaluation-policy-1.0"
GROUND_TRUTH_SCHEMA_NAME = "BoundaryGroundTruth"
GROUND_TRUTH_SCHEMA_VERSION = "1.0"
DEFAULT_TOLERANCES_MS = (15000, 30000)

RESULT_SCHEMA_NAME = "BoundaryEvaluationResult"
VIDEO_RECORD_SCHEMA_NAME = "BoundaryEvaluationVideoRecord"
AGGREGATE_SCHEMA_NAME = "BoundaryEvaluationAggregate"
RECORD_SCHEMA_VERSION = "1.0"

AGGREGATE_METRIC_NAMES = (
    "precision_at_15s",
    "recall_at_15s",
    "f1_at_15s",
    "precision_at_30s",
    "recall_at_30s",
    "f1_at_30s",
    "mae_ms",
)

# 封闭错误码集（裁决Q8.2，6码）
ERROR_ANNOTATION_FILE_MISSING = "ANNOTATION_FILE_MISSING"
ERROR_ANNOTATION_SCHEMA_INVALID = "ANNOTATION_SCHEMA_INVALID"
ERROR_ANNOTATION_NOT_REVIEWED = "ANNOTATION_NOT_REVIEWED"
ERROR_ANNOTATION_IDENTITY_MISMATCH = "ANNOTATION_IDENTITY_MISMATCH"
ERROR_EVALUATION_INPUT_INVALID = "EVALUATION_INPUT_INVALID"
ERROR_INPUT_CHAPTER_PLAN_MISSING = "INPUT_CHAPTER_PLAN_MISSING"

BOUNDARY_EVALUATION_ERROR_CODES = frozenset(
    {
        ERROR_ANNOTATION_FILE_MISSING,
        ERROR_ANNOTATION_SCHEMA_INVALID,
        ERROR_ANNOTATION_NOT_REVIEWED,
        ERROR_ANNOTATION_IDENTITY_MISMATCH,
        ERROR_EVALUATION_INPUT_INVALID,
        ERROR_INPUT_CHAPTER_PLAN_MISSING,
    }
)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f]")

_GT_TOP_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "task_id",
        "annotation_version",
        "duration_ms",
        "source_timeline",
        "annotator",
        "annotated_at",
        "reviewed",
        "reviewer",
        "reviewed_at",
        "chapters",
    }
)
_GT_TIMELINE_KEYS = frozenset({"timeline_id", "sha256"})
_GT_CHAPTER_KEYS = frozenset({"index", "start_ms", "title", "source", "reviewed"})
_GT_SOURCE_ENUM = frozenset(
    {"publisher_timestamps_checked", "manual_independent", "other"}
)

# ChapterPlan 2.0最小形状（裁决§三.4：顶层八键精确、条目三键精确）
_PLAN_TOP_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "task_id",
        "strategy",
        "source_timeline",
        "duration_ms",
        "parameters",
        "boundaries",
    }
)
_PLAN_BOUNDARY_KEYS = frozenset({"timestamp_ms", "title", "source_event_id"})

_RESULT_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "evaluation_policy_version",
        "tolerances_ms",
        "gt_count",
        "pred_count",
        "per_tolerance",
        "mae_ms",
        "mae_tolerance_ms",
        "mae_matched_count",
    }
)
_PER_TOLERANCE_KEYS = frozenset(
    {"tolerance_ms", "tp", "precision", "recall", "f1", "matches"}
)
_MATCH_KEYS = frozenset({"gt_ms", "pred_ms", "abs_diff_ms"})
_AGGREGATE_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "evaluation_policy_version",
        "video_count",
        "metrics",
        "per_video",
    }
)


class BoundaryEvaluationError(RuntimeError):
    """评价模块类型化错误基类；error_code必属6码封闭集（裁决Q8.2）。"""

    error_code = ERROR_EVALUATION_INPUT_INVALID


class AnnotationFileMissingError(BoundaryEvaluationError):
    error_code = ERROR_ANNOTATION_FILE_MISSING


class AnnotationSchemaInvalidError(BoundaryEvaluationError):
    error_code = ERROR_ANNOTATION_SCHEMA_INVALID


class AnnotationNotReviewedError(BoundaryEvaluationError):
    error_code = ERROR_ANNOTATION_NOT_REVIEWED


class AnnotationIdentityMismatchError(BoundaryEvaluationError):
    error_code = ERROR_ANNOTATION_IDENTITY_MISMATCH


class EvaluationInputInvalidError(BoundaryEvaluationError):
    error_code = ERROR_EVALUATION_INPUT_INVALID


class InputChapterPlanMissingError(BoundaryEvaluationError):
    error_code = ERROR_INPUT_CHAPTER_PLAN_MISSING


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _exact_keys(
    payload: Any,
    expected: frozenset[str],
    label: str,
    error: type[BoundaryEvaluationError],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise error(f"{label}必须为JSON对象。")
    missing = sorted(expected - payload.keys())
    extra = sorted(payload.keys() - expected)
    if missing or extra:
        raise error(
            f"{label}键集不合法：缺失{missing}，多余{extra}（封闭精确键集）。"
        )
    return payload


def _require_ms_sequence(
    values: Any,
    label: str,
    *,
    allow_empty: bool,
) -> list[int]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values, (list, tuple)
    ):
        raise EvaluationInputInvalidError(f"{label}必须为整数毫秒列表。")
    if not values and not allow_empty:
        raise EvaluationInputInvalidError(f"{label}不得为空列表。")
    previous: int | None = None
    result: list[int] = []
    for value in values:
        if not _is_int(value):
            raise EvaluationInputInvalidError(f"{label}含非整数毫秒条目（bool排除）。")
        if value < 0:
            raise EvaluationInputInvalidError(f"{label}含负值条目。")
        if previous is not None and value <= previous:
            raise EvaluationInputInvalidError(f"{label}必须严格递增。")
        result.append(int(value))
        previous = value
    return result


# ---------------------------------------------------------------------------
# 第一层前置：标注Schema（裁决Q7/§三.1）
# ---------------------------------------------------------------------------


def _validate_ground_truth(data: Any) -> dict[str, Any]:
    """§三.1全族校验；任何违反→ANNOTATION_SCHEMA_INVALID。"""

    payload = _exact_keys(
        data, _GT_TOP_KEYS, "BoundaryGroundTruth顶层", AnnotationSchemaInvalidError
    )
    if (
        payload["schema_name"] != GROUND_TRUTH_SCHEMA_NAME
        or payload["schema_version"] != GROUND_TRUTH_SCHEMA_VERSION
    ):
        raise AnnotationSchemaInvalidError(
            "BoundaryGroundTruth版本身份不符（1.0封闭，不做跨版本迁移）。"
        )
    for field in ("task_id", "annotation_version", "annotator"):
        if not _is_nonempty_str(payload[field]):
            raise AnnotationSchemaInvalidError(f"{field}必须为非空字符串。")
    if not isinstance(payload["annotated_at"], str) or not _DATE_RE.fullmatch(
        payload["annotated_at"]
    ):
        raise AnnotationSchemaInvalidError("annotated_at必须为YYYY-MM-DD。")
    if not _is_int(payload["duration_ms"]) or payload["duration_ms"] <= 0:
        raise AnnotationSchemaInvalidError("duration_ms必须为>0的整数毫秒。")
    source_timeline = payload["source_timeline"]
    if source_timeline is not None:
        timeline = _exact_keys(
            source_timeline,
            _GT_TIMELINE_KEYS,
            "source_timeline",
            AnnotationSchemaInvalidError,
        )
        if not _is_nonempty_str(timeline["timeline_id"]):
            raise AnnotationSchemaInvalidError("source_timeline.timeline_id必须为非空字符串。")
        if not isinstance(timeline["sha256"], str) or not _SHA256_RE.fullmatch(
            timeline["sha256"]
        ):
            raise AnnotationSchemaInvalidError(
                "source_timeline.sha256必须为64位小写hex。"
            )
    if not isinstance(payload["reviewed"], bool):
        raise AnnotationSchemaInvalidError("reviewed必须为布尔值。")
    if payload["reviewed"]:
        if not _is_nonempty_str(payload["reviewer"]):
            raise AnnotationSchemaInvalidError("reviewed=true时reviewer必须为非空字符串。")
        if not isinstance(payload["reviewed_at"], str) or not _DATE_RE.fullmatch(
            payload["reviewed_at"]
        ):
            raise AnnotationSchemaInvalidError(
                "reviewed=true时reviewed_at必须为YYYY-MM-DD。"
            )
    else:
        if payload["reviewer"] is not None and not _is_nonempty_str(payload["reviewer"]):
            raise AnnotationSchemaInvalidError("reviewer必须为非空字符串或null。")
        if payload["reviewed_at"] is not None:
            if not isinstance(payload["reviewed_at"], str) or not _DATE_RE.fullmatch(
                payload["reviewed_at"]
            ):
                raise AnnotationSchemaInvalidError("reviewed_at必须为YYYY-MM-DD或null。")
    chapters = payload["chapters"]
    if not isinstance(chapters, list) or not chapters:
        raise AnnotationSchemaInvalidError("chapters必须为非空数组。")
    previous_start: int | None = None
    for position, raw_chapter in enumerate(chapters):
        chapter = _exact_keys(
            raw_chapter,
            _GT_CHAPTER_KEYS,
            f"chapters[{position}]",
            AnnotationSchemaInvalidError,
        )
        if not _is_int(chapter["index"]) or chapter["index"] != position:
            raise AnnotationSchemaInvalidError(
                f"chapters[{position}].index必须为0基数组下标{position}。"
            )
        if not _is_int(chapter["start_ms"]) or chapter["start_ms"] < 0:
            raise AnnotationSchemaInvalidError(
                f"chapters[{position}].start_ms必须为>=0的整数毫秒。"
            )
        if position == 0 and chapter["start_ms"] != 0:
            raise AnnotationSchemaInvalidError("首章start_ms必须为0。")
        if previous_start is not None and chapter["start_ms"] <= previous_start:
            raise AnnotationSchemaInvalidError("chapters.start_ms必须严格递增。")
        if chapter["start_ms"] >= payload["duration_ms"]:
            raise AnnotationSchemaInvalidError(
                "chapters.start_ms必须全部小于duration_ms。"
            )
        previous_start = chapter["start_ms"]
        title = chapter["title"]
        if not isinstance(title, str) or title.strip() == "":
            raise AnnotationSchemaInvalidError(f"chapters[{position}].title strip后必须非空。")
        if len(title) > 200:
            raise AnnotationSchemaInvalidError(f"chapters[{position}].title长度不得超过200字符。")
        if _CONTROL_CHAR_RE.search(title):
            raise AnnotationSchemaInvalidError(f"chapters[{position}].title不得含控制字符。")
        if chapter["source"] not in _GT_SOURCE_ENUM:
            raise AnnotationSchemaInvalidError(
                f"chapters[{position}].source必须属封闭枚举"
                "publisher_timestamps_checked|manual_independent|other。"
            )
        if not isinstance(chapter["reviewed"], bool):
            raise AnnotationSchemaInvalidError(f"chapters[{position}].reviewed必须为布尔值。")
    return payload


def load_ground_truth(path: str | Path) -> dict[str, Any]:
    """读取并校验BoundaryGroundTruth 1.0；Schema全族先于reviewed判定（§三.1.5/7）。"""

    target = Path(path).expanduser()
    # 沿F3先例：先判is_symlink再判is_file；目录/链接/不可读一律硬失败
    if target.is_symlink() or not target.is_file():
        raise AnnotationFileMissingError("标注文件不存在、不可读或非普通文件。")
    try:
        raw = target.read_bytes()
    except OSError:
        raise AnnotationFileMissingError("标注文件不可读。") from None
    if not raw:
        raise AnnotationSchemaInvalidError("标注文件为空。")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise AnnotationSchemaInvalidError("标注文件不是合法JSON对象。") from None
    payload = _validate_ground_truth(data)
    if payload["reviewed"] is not True:
        raise AnnotationNotReviewedError("标注文件级reviewed必须为true方可评价。")
    if any(chapter["reviewed"] is not True for chapter in payload["chapters"]):
        raise AnnotationNotReviewedError("标注存在未复核章级条目，不得用于评价。")
    return payload


def ground_truth_internal_boundaries(annotation: Mapping[str, Any]) -> list[int]:
    """真值侧内部边界：chapters[1:]的start_ms（裁决Q2.2；首章恒0排除）。"""

    if not isinstance(annotation, Mapping):
        raise EvaluationInputInvalidError("annotation必须为已加载的标注对象。")
    chapters = annotation.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise EvaluationInputInvalidError("annotation.chapters必须为非空数组。")
    return [chapter["start_ms"] for chapter in chapters[1:]]


# ---------------------------------------------------------------------------
# 预测侧提取（裁决Q2.1/§三.4最小形状校验）
# ---------------------------------------------------------------------------


def extract_internal_boundaries(plan: Mapping[str, Any]) -> list[int]:
    """预测侧内部边界：boundaries[1:]的timestamp_ms；首条恒0排除、终点不追加。"""

    payload = _exact_keys(
        plan, _PLAN_TOP_KEYS, "ChapterPlan顶层", EvaluationInputInvalidError
    )
    boundaries = payload["boundaries"]
    if not isinstance(boundaries, list) or not boundaries:
        raise EvaluationInputInvalidError("ChapterPlan.boundaries必须为非空数组。")
    previous: int | None = None
    timestamps: list[int] = []
    for position, raw_boundary in enumerate(boundaries):
        entry = _exact_keys(
            raw_boundary,
            _PLAN_BOUNDARY_KEYS,
            f"boundaries[{position}]",
            EvaluationInputInvalidError,
        )
        if not _is_int(entry["timestamp_ms"]) or entry["timestamp_ms"] < 0:
            raise EvaluationInputInvalidError(
                f"boundaries[{position}].timestamp_ms必须为>=0的整数毫秒。"
            )
        if position == 0 and entry["timestamp_ms"] != 0:
            raise EvaluationInputInvalidError("ChapterPlan首条timestamp_ms必须为0。")
        if previous is not None and entry["timestamp_ms"] <= previous:
            raise EvaluationInputInvalidError("boundaries.timestamp_ms必须严格递增。")
        previous = entry["timestamp_ms"]
        timestamps.append(entry["timestamp_ms"])
    return timestamps[1:]


# ---------------------------------------------------------------------------
# 第一层：纯函数指标核（裁决Q2—Q5）
# ---------------------------------------------------------------------------


def _dp_better(candidate: tuple[int, int], current: tuple[int, int] | None) -> tuple[int, int]:
    """比较键：count更大者优先；count相等时total_diff更小者优先；仍相等取先列者（Q3.3/3.4）。"""

    if current is None:
        return candidate
    if candidate[0] > current[0]:
        return candidate
    if candidate[0] == current[0] and candidate[1] < current[1]:
        return candidate
    return current


def _dp_match(
    gt: Sequence[int], pred: Sequence[int], tolerance_ms: int
) -> list[dict[str, int]]:
    n = len(gt)
    m = len(pred)
    table: list[list[tuple[int, int]]] = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best: tuple[int, int] | None = None
            diff = abs(int(gt[i - 1]) - int(pred[j - 1]))
            if diff <= tolerance_ms:  # 闭区间（Q3.5）
                prev_count, prev_total = table[i - 1][j - 1]
                best = _dp_better((prev_count + 1, prev_total + diff), best)  # ①配对
            best = _dp_better(table[i - 1][j], best)  # ②跳过gt
            best = _dp_better(table[i][j - 1], best)  # ③跳过pred
            table[i][j] = best
    matches: list[dict[str, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        current = table[i][j]
        diff = abs(int(gt[i - 1]) - int(pred[j - 1]))
        if diff <= tolerance_ms:
            prev_count, prev_total = table[i - 1][j - 1]
            if (prev_count + 1, prev_total + diff) == current:  # 回溯①优先（Q3.4）
                matches.append(
                    {
                        "gt_ms": int(gt[i - 1]),
                        "pred_ms": int(pred[j - 1]),
                        "abs_diff_ms": diff,
                    }
                )
                i, j = i - 1, j - 1
                continue
        if table[i - 1][j] == current:  # ②
            i -= 1
            continue
        if table[i][j - 1] == current:  # ③
            j -= 1
            continue
        raise AssertionError("boundary_evaluation：DP表与回溯不一致（内部缺陷）。")
    matches.reverse()
    return matches


def match_boundaries_one_to_one(
    gt_ms: Sequence[int], pred_ms: Sequence[int], tolerance_ms: int
) -> list[dict[str, int]]:
    """保序DP一对一最优匹配（裁决Q3）；返回按gt升序的配对列表。"""

    gt = _require_ms_sequence(gt_ms, "gt_ms", allow_empty=True)
    pred = _require_ms_sequence(pred_ms, "pred_ms", allow_empty=True)
    if not _is_int(tolerance_ms) or tolerance_ms <= 0:
        raise EvaluationInputInvalidError("tolerance_ms必须为正整数毫秒（bool排除）。")
    return _dp_match(gt, pred, int(tolerance_ms))


def _validate_tolerances(tolerances_ms: Any) -> tuple[int, ...]:
    if isinstance(tolerances_ms, (str, bytes, bytearray)) or not isinstance(
        tolerances_ms, (list, tuple)
    ):
        raise EvaluationInputInvalidError("tolerances_ms必须为整数毫秒列表。")
    if not tolerances_ms:
        raise EvaluationInputInvalidError("tolerances_ms不得为空（mae_tolerance_ms需要最大值）。")
    previous: int | None = None
    tolerances: list[int] = []
    for value in tolerances_ms:
        if not _is_int(value) or value <= 0:
            raise EvaluationInputInvalidError(
                "tolerances_ms条目必须为正整数毫秒（bool排除、非正非法）。"
            )
        if previous is not None and value <= previous:
            raise EvaluationInputInvalidError(
                "tolerances_ms必须严格升序且无重复。"
            )
        tolerances.append(int(value))
        previous = int(value)
    return tuple(tolerances)


def _pair_metrics(
    tp: int, gt_count: int, pred_count: int
) -> tuple[float | None, float | None, float | None]:
    """null三值约定（裁决Q5表逐格）；一侧null另一侧（若非null）必为0.0⇒F1=0.0。"""

    if gt_count == 0 and pred_count == 0:
        return None, None, None
    precision = None if pred_count == 0 else tp / pred_count
    recall = None if gt_count == 0 else tp / gt_count
    if precision is None or recall is None:
        f1 = 0.0
    elif precision + recall == 0.0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def evaluate_boundaries(
    gt_ms: Sequence[int],
    pred_ms: Sequence[int],
    *,
    tolerances_ms: Sequence[int] = DEFAULT_TOLERANCES_MS,
) -> dict[str, Any]:
    """纯函数指标核：逐容差独立重算匹配集（禁止截断复用，Q3.6）＋MAE基于最大容差匹配集（Q4）。"""

    gt = _require_ms_sequence(gt_ms, "gt_ms", allow_empty=True)
    pred = _require_ms_sequence(pred_ms, "pred_ms", allow_empty=True)
    tolerances = _validate_tolerances(tolerances_ms)
    gt_count = len(gt)
    pred_count = len(pred)
    per_tolerance: list[dict[str, Any]] = []
    for tolerance in tolerances:
        matches = _dp_match(gt, pred, tolerance)
        tp = len(matches)
        precision, recall, f1 = _pair_metrics(tp, gt_count, pred_count)
        per_tolerance.append(
            {
                "tolerance_ms": tolerance,
                "tp": tp,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "matches": matches,
            }
        )
    mae_tolerance_ms = max(tolerances)
    widest = per_tolerance[-1]  # tolerances严格升序⇒末项即最大容差
    mae_matched_count = int(widest["tp"])
    if mae_matched_count == 0:
        mae_ms: float | None = None  # 零匹配禁止报0.0（Q4.2）
    else:
        mae_ms = sum(entry["abs_diff_ms"] for entry in widest["matches"]) / mae_matched_count
    return {
        "schema_name": RESULT_SCHEMA_NAME,
        "schema_version": RECORD_SCHEMA_VERSION,
        "evaluation_policy_version": BOUNDARY_EVALUATION_POLICY_VERSION,
        "tolerances_ms": list(tolerances),
        "gt_count": gt_count,
        "pred_count": pred_count,
        "per_tolerance": per_tolerance,
        "mae_ms": mae_ms,
        "mae_tolerance_ms": mae_tolerance_ms,
        "mae_matched_count": mae_matched_count,
    }


# ---------------------------------------------------------------------------
# 聚合面（裁决Q6）
# ---------------------------------------------------------------------------


def _result_tolerance_entry(result: Mapping[str, Any], tolerance_ms: int) -> Mapping[str, Any]:
    entries = result.get("per_tolerance")
    if not isinstance(entries, list):
        raise EvaluationInputInvalidError("result.per_tolerance必须为列表。")
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("tolerance_ms") == tolerance_ms:
            return entry
    raise EvaluationInputInvalidError(f"result缺少容差{tolerance_ms}ms的per_tolerance条目。")


def _record_metric_values(result: Mapping[str, Any]) -> dict[str, float | None]:
    _exact_keys(result, _RESULT_KEYS, "BoundaryEvaluationResult", EvaluationInputInvalidError)
    if (
        result["schema_name"] != RESULT_SCHEMA_NAME
        or result["schema_version"] != RECORD_SCHEMA_VERSION
        or result["evaluation_policy_version"] != BOUNDARY_EVALUATION_POLICY_VERSION
    ):
        raise EvaluationInputInvalidError(
            "result必须为boundary-evaluation-policy-1.0的BoundaryEvaluationResult。"
        )
    if list(result["tolerances_ms"]) != list(DEFAULT_TOLERANCES_MS):
        raise EvaluationInputInvalidError("聚合输入必须为默认容差(15000,30000)的结果记录。")
    entry15 = _result_tolerance_entry(result, 15000)
    entry30 = _result_tolerance_entry(result, 30000)
    return {
        "precision_at_15s": entry15["precision"],
        "recall_at_15s": entry15["recall"],
        "f1_at_15s": entry15["f1"],
        "precision_at_30s": entry30["precision"],
        "recall_at_30s": entry30["recall"],
        "f1_at_30s": entry30["f1"],
        "mae_ms": result["mae_ms"],
    }


def _mean_and_std(values: list[float]) -> tuple[float | None, float | None]:
    """宏平均＝等权算术平均；标准差＝样本口径ddof=1；n<2时std=null（禁止0.0，Q6.3）。"""

    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    if n == 1:
        return mean, None
    variance = sum((value - mean) ** 2 for value in values) / (n - 1)
    return mean, math.sqrt(variance)


def aggregate_boundary_evaluations(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """逐视频记录→宏平均聚合（裁决Q6）；缺键或非默认容差→EVALUATION_INPUT_INVALID。"""

    if isinstance(records, (str, bytes, bytearray)) or not isinstance(records, (list, tuple)):
        raise EvaluationInputInvalidError("records必须为记录列表。")
    metric_values: dict[str, list[float | None]] = {name: [] for name in AGGREGATE_METRIC_NAMES}
    per_video: list[dict[str, Any]] = []
    for position, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise EvaluationInputInvalidError(f"records[{position}]必须为映射对象。")
        if "task_id" not in record or "result" not in record:
            raise EvaluationInputInvalidError(
                f"records[{position}]缺少task_id或result键。"
            )
        task_id = record["task_id"]
        if not _is_nonempty_str(task_id):
            raise EvaluationInputInvalidError(f"records[{position}].task_id必须为非空字符串。")
        result = record["result"]
        if not isinstance(result, Mapping):
            raise EvaluationInputInvalidError(f"records[{position}].result必须为结果记录对象。")
        values = _record_metric_values(result)
        for name in AGGREGATE_METRIC_NAMES:
            metric_values[name].append(values[name])
        per_video.append({"task_id": task_id, **values})
    metrics: dict[str, Any] = {}
    for name in AGGREGATE_METRIC_NAMES:
        non_null = [value for value in metric_values[name] if value is not None]
        null_count = len(metric_values[name]) - len(non_null)
        mean, std = _mean_and_std(non_null)
        metrics[name] = {
            "mean": mean,
            "std": std,
            "non_null_count": len(non_null),
            "null_count": null_count,
        }
    return {
        "schema_name": AGGREGATE_SCHEMA_NAME,
        "schema_version": RECORD_SCHEMA_VERSION,
        "evaluation_policy_version": BOUNDARY_EVALUATION_POLICY_VERSION,
        "video_count": len(records),
        "metrics": metrics,
        "per_video": per_video,
    }


# ---------------------------------------------------------------------------
# 第二层：身份绑定适配层（裁决Q1）
# ---------------------------------------------------------------------------


def _annotation_relative_path(path: Path, project_root: Path | None) -> str:
    root = Path.cwd() if project_root is None else Path(project_root)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.name


def evaluate_chapter_plan_against_annotation(
    output_root: str | Path,
    task_id: str,
    annotation_path: str | Path,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """适配层：已发布ChapterPlan 2.0 vs BoundaryGroundTruth 1.0的逐视频评价记录。

    预测侧经既有公开入口完整复验加载（类型化错误原样传播，Q8.3）；标注侧经
    ``load_ground_truth``；身份绑定三项任一不符→ANNOTATION_IDENTITY_MISMATCH（Q1.3）。
    """

    annotation = load_ground_truth(annotation_path)
    pointer = load_chapter_plan_current(output_root, task_id)
    if pointer is None:
        raise InputChapterPlanMissingError(
            "INPUT_CHAPTER_PLAN_MISSING：boundaries current不存在。"
        )
    plan = load_published_chapter_plan(output_root, task_id)
    if plan is None:
        raise InputChapterPlanMissingError(
            "INPUT_CHAPTER_PLAN_MISSING：boundaries current加载返回空。"
        )

    # 身份绑定校验（Q1.3）
    if annotation["task_id"] != task_id:
        raise AnnotationIdentityMismatchError("标注task_id与入参task_id不一致。")
    if annotation["duration_ms"] != plan["duration_ms"]:
        raise AnnotationIdentityMismatchError("标注duration_ms与plan.duration_ms不一致。")
    annotation_timeline = annotation["source_timeline"]
    if annotation_timeline is not None:
        plan_timeline = plan["source_timeline"]
        if (
            annotation_timeline["timeline_id"] != plan_timeline["timeline_id"]
            or annotation_timeline["sha256"] != plan_timeline["sha256"]
        ):
            raise AnnotationIdentityMismatchError(
                "标注source_timeline与plan.source_timeline的timeline_id/sha256不一致。"
            )

    pred_ms = extract_internal_boundaries(plan)
    gt_ms = ground_truth_internal_boundaries(annotation)
    evaluation = evaluate_boundaries(gt_ms, pred_ms)

    target = Path(annotation_path).expanduser()
    annotation_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()

    return {
        "schema_name": VIDEO_RECORD_SCHEMA_NAME,
        "schema_version": RECORD_SCHEMA_VERSION,
        "task_id": task_id,
        "plan": {
            "result_id": pointer["result_id"],
            "manifest_sha256": pointer["manifest_sha256"],
            "strategy": plan["strategy"],
            "result_source": plan["parameters"]["result_source"],
            "quality_level": plan["parameters"]["quality_level"],
            "timeline_id": plan["source_timeline"]["timeline_id"],
            "timeline_sha256": plan["source_timeline"]["sha256"],
            "duration_ms": plan["duration_ms"],
            "boundary_count": len(plan["boundaries"]),
            "internal_boundary_count": len(pred_ms),
        },
        "annotation": {
            "relative_path": _annotation_relative_path(target, project_root),
            "sha256": annotation_sha256,
            "annotation_version": annotation["annotation_version"],
            "chapter_count": len(annotation["chapters"]),
        },
        "evaluation": evaluation,
    }
