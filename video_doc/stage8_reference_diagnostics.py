"""阶段8冻结参考的最小只读边界诊断器；不替代阶段16评价。"""

from __future__ import annotations

from typing import Any


REFERENCE_NOTICE = "阶段8冻结诊断参考，不是正式人工真值"


def _match_boundaries(
    reference: list[float], prediction: list[float], tolerance: float
) -> dict[str, Any]:
    candidates = sorted(
        (abs(ref - pred), ref_index, pred_index)
        for ref_index, ref in enumerate(reference)
        for pred_index, pred in enumerate(prediction)
        if abs(ref - pred) <= tolerance
    )
    used_ref: set[int] = set()
    used_pred: set[int] = set()
    matches: list[dict[str, float]] = []
    for error, ref_index, pred_index in candidates:
        if ref_index in used_ref or pred_index in used_pred:
            continue
        used_ref.add(ref_index)
        used_pred.add(pred_index)
        matches.append(
            {
                "reference_seconds": reference[ref_index],
                "prediction_seconds": prediction[pred_index],
                "absolute_error_seconds": round(error, 3),
            }
        )
    tp = len(matches)
    fp = len(prediction) - tp
    fn = len(reference) - tp
    denominator = 2 * tp + fp + fn
    return {
        "tolerance_seconds": tolerance,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "f1": round(2 * tp / denominator, 4) if denominator else 1.0,
        "matches": matches,
        "unmatched_reference_seconds": [
            value for index, value in enumerate(reference) if index not in used_ref
        ],
        "unmatched_prediction_seconds": [
            value for index, value in enumerate(prediction) if index not in used_pred
        ],
    }


def diagnose_stage8_prediction(
    reference_plan: dict[str, Any],
    prediction_plan: dict[str, Any],
    *,
    missing_topics: list[str] | None = None,
    over_split_topics: list[str] | None = None,
    incorrect_merges: list[str] | None = None,
) -> dict[str, Any]:
    """对两份ChapterPlan的非零边界做15/30秒一对一诊断。"""

    reference = [float(item["timestamp"]) for item in reference_plan["boundaries"][1:]]
    prediction = [float(item["timestamp"]) for item in prediction_plan["boundaries"][1:]]
    return {
        "notice": REFERENCE_NOTICE,
        "reference_boundary_count": len(reference),
        "prediction_boundary_count": len(prediction),
        "reference_chapter_count": len(reference_plan["boundaries"]),
        "prediction_chapter_count": len(prediction_plan["boundaries"]),
        "chapter_count_difference": len(prediction_plan["boundaries"])
        - len(reference_plan["boundaries"]),
        "metrics": {
            "15_seconds": _match_boundaries(reference, prediction, 15.0),
            "30_seconds": _match_boundaries(reference, prediction, 30.0),
        },
        "theme_issues": {
            "missing_topics": list(missing_topics or []),
            "over_split_topics": list(over_split_topics or []),
            "incorrect_merges": list(incorrect_merges or []),
        },
    }
