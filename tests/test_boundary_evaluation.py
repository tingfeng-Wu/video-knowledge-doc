"""阶段16边界评价定向测试：《阶段16-标注格式与评价程序裁决》§四15类38例钉死矩阵。

期望值全部照抄裁决§四表内精确数值（禁止实现轮自算新期望回填，§四.3）；
浮点断言用表内钉死的公式/容差。O1真实树只读复验不在pytest内（实现轮证据，
见docs/development/stage16-evidence说明与提交记录）。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from video_doc import boundary_evaluation as module
from video_doc.boundary_evaluation import (
    BOUNDARY_EVALUATION_POLICY_VERSION,
    BoundaryEvaluationError,
    aggregate_boundary_evaluations,
    evaluate_boundaries,
    evaluate_chapter_plan_against_annotation,
    extract_internal_boundaries,
    ground_truth_internal_boundaries,
    load_ground_truth,
    match_boundaries_one_to_one,
)

DEV01_DURATION_MS = 914923
DEV01_TIMELINE_ID = "unified-timeline-95a569bb9e4c"
DEV01_TIMELINE_SHA = "a" * 64


def _matches(pairs: list[tuple[int, int, int]]) -> list[dict[str, int]]:
    return [
        {"gt_ms": gt, "pred_ms": pred, "abs_diff_ms": diff} for gt, pred, diff in pairs
    ]


def valid_annotation(**overrides) -> dict:
    data = {
        "schema_name": "BoundaryGroundTruth",
        "schema_version": "1.0",
        "task_id": "DEV01",
        "annotation_version": "1.0",
        "duration_ms": DEV01_DURATION_MS,
        "source_timeline": {"timeline_id": DEV01_TIMELINE_ID, "sha256": DEV01_TIMELINE_SHA},
        "annotator": "annotator-a",
        "annotated_at": "2026-10-31",
        "reviewed": True,
        "reviewer": "reviewer-a",
        "reviewed_at": "2026-10-31",
        "chapters": [
            {"index": 0, "start_ms": 0, "title": "第一章", "source": "manual_independent", "reviewed": True},
            {"index": 1, "start_ms": 60000, "title": "第二章", "source": "manual_independent", "reviewed": True},
            {"index": 2, "start_ms": 180000, "title": "第三章", "source": "publisher_timestamps_checked", "reviewed": True},
        ],
    }
    data.update(overrides)
    return data


def write_annotation(tmp_path: Path, data: dict, name: str = "DEV01.json") -> Path:
    path = tmp_path / name
    with open(path, "w", encoding="utf-8", newline="") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return path


def valid_plan(
    boundaries_ms: tuple[int, ...] = (0, 60000, 180000, 300000),
    *,
    duration_ms: int = DEV01_DURATION_MS,
    result_source: str = "primary",
    quality_level: str = "normal",
) -> dict:
    return {
        "schema_name": "ChapterPlan",
        "schema_version": "2.0",
        "task_id": "DEV01",
        "strategy": "llm",
        "source_timeline": {
            "schema_name": "TimelineRef",
            "schema_version": "1.0",
            "timeline_id": DEV01_TIMELINE_ID,
            "sha256": DEV01_TIMELINE_SHA,
            "event_count": 308,
            "transcript_event_count": 262,
        },
        "duration_ms": duration_ms,
        "parameters": {
            "result_source": result_source,
            "quality_level": quality_level,
            "prompt_version": "chapter-boundary-unified-timeline-2.0",
            "min_chapter_ms": 60000,
            "boundary_alignment": "selected_transcript_event_start",
        },
        "boundaries": [
            {"timestamp_ms": ms, "title": f"第{i + 1}章", "source_event_id": None if i == 0 else f"te{i:06d}"}
            for i, ms in enumerate(boundaries_ms)
        ],
    }


def valid_pointer(result_id: str = "chapter-plan-5e28a621e2ea") -> dict:
    return {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "chapter_plan",
        "result_id": result_id,
        "group": f"formal-results/{result_id}",
        "manifest_sha256": "b" * 64,
    }


def patch_loaders(monkeypatch: pytest.MonkeyPatch, plan: dict, pointer: dict | None) -> None:
    monkeypatch.setattr(module, "load_chapter_plan_current", lambda root, task_id: pointer)
    monkeypatch.setattr(module, "load_published_chapter_plan", lambda root, task_id, **kwargs: plan)


def code_error(excinfo) -> str:
    return excinfo.value.error_code


# ---------------------------------------------------------------------------
# §四.1 阶段计划四类：A完全命中／B全部漏报／C重复预测／D容差边缘
# ---------------------------------------------------------------------------


def test_a1_exact_hit() -> None:
    result = evaluate_boundaries([60000, 180000, 300000], [60000, 180000, 300000])
    assert set(result) == {
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
    assert result["schema_name"] == "BoundaryEvaluationResult"
    assert result["schema_version"] == "1.0"
    assert result["evaluation_policy_version"] == BOUNDARY_EVALUATION_POLICY_VERSION
    assert result["tolerances_ms"] == [15000, 30000]
    assert result["gt_count"] == 3
    assert result["pred_count"] == 3
    expected = _matches([(60000, 60000, 0), (180000, 180000, 0), (300000, 300000, 0)])
    assert len(result["per_tolerance"]) == 2
    for entry in result["per_tolerance"]:
        assert set(entry) == {"tolerance_ms", "tp", "precision", "recall", "f1", "matches"}
        assert entry["tp"] == 3
        assert entry["precision"] == 1.0
        assert entry["recall"] == 1.0
        assert entry["f1"] == 1.0
        assert entry["matches"] == expected
    assert result["mae_ms"] == 0.0
    assert result["mae_tolerance_ms"] == 30000
    assert result["mae_matched_count"] == 3


def test_b1_all_missed() -> None:
    result = evaluate_boundaries([60000, 180000], [900000, 1200000])
    for entry in result["per_tolerance"]:
        assert entry["tp"] == 0
        assert entry["precision"] == 0.0
        assert entry["recall"] == 0.0
        assert entry["f1"] == 0.0
        assert entry["matches"] == []
    assert result["mae_ms"] is None
    assert result["mae_matched_count"] == 0


def test_c1_duplicate_prediction() -> None:
    result = evaluate_boundaries([100000], [100000, 105000])
    for entry in result["per_tolerance"]:
        # DP选diff=0的(100000,100000)：min总差（裁决C1）
        assert entry["tp"] == 1
        assert entry["matches"] == _matches([(100000, 100000, 0)])
        assert entry["precision"] == 0.5
        assert entry["recall"] == 1.0
        assert abs(entry["f1"] - 2 / 3) <= 1e-12
    assert result["mae_ms"] == 0.0


def test_d1_tolerance_edge_exactly_15s() -> None:
    result = evaluate_boundaries([100000], [115000])
    entry15, entry30 = result["per_tolerance"]
    assert entry15["tolerance_ms"] == 15000
    assert entry15["tp"] == 1  # 闭区间|diff|<=T：恰等于T计为命中（Q3.5）
    assert entry15["f1"] == 1.0
    assert entry30["tp"] == 1
    assert entry30["f1"] == 1.0
    assert result["mae_ms"] == 15000.0


def test_d2_tolerance_edge_exactly_30s() -> None:
    result = evaluate_boundaries([100000], [130000])
    entry15, entry30 = result["per_tolerance"]
    assert entry15["tp"] == 0
    assert entry15["precision"] == 0.0
    assert entry15["recall"] == 0.0
    assert entry15["f1"] == 0.0
    assert entry30["tp"] == 1
    assert entry30["f1"] == 1.0
    assert result["mae_ms"] == 30000.0
    # TP@15 <= TP@30 嵌套性质钉死例（Q3.6）
    assert entry15["tp"] <= entry30["tp"]


def test_d3_tolerance_edge_over_30s_by_one_ms() -> None:
    result = evaluate_boundaries([100000], [130001])
    for entry in result["per_tolerance"]:
        assert entry["tp"] == 0
        assert entry["f1"] == 0.0
    assert result["mae_ms"] is None


# ---------------------------------------------------------------------------
# §四.2 补充类别E—N
# ---------------------------------------------------------------------------


def test_e1_order_preserving_dp_beats_global_greedy() -> None:
    matches = match_boundaries_one_to_one([100000, 114000], [113000, 127000], 15000)
    assert matches == _matches([(100000, 113000, 13000), (114000, 127000, 13000)])
    result = evaluate_boundaries([100000, 114000], [113000, 127000])
    entry15 = result["per_tolerance"][0]
    assert entry15["tp"] == 2
    assert entry15["precision"] == 1.0
    assert entry15["recall"] == 1.0
    assert entry15["f1"] == 1.0
    assert result["mae_ms"] == 13000.0


def test_f1_tie_break_determinism() -> None:
    matches = match_boundaries_one_to_one([100000], [95000, 105000], 15000)
    # 回溯优先级①配对→②跳gt→③跳pred自f[n][m]起：平局下锁定pred=105000（裁决F1）
    assert matches == _matches([(100000, 105000, 5000)])
    result = evaluate_boundaries([100000], [95000, 105000])
    assert result["per_tolerance"][0]["tp"] == 1
    assert result["mae_ms"] == 5000.0


def test_g1_empty_prediction_pure_function() -> None:
    result = evaluate_boundaries([60000, 300000], [])
    assert result["gt_count"] == 2
    assert result["pred_count"] == 0
    for entry in result["per_tolerance"]:
        assert entry["tp"] == 0
        assert entry["precision"] is None  # null按null（Q5表第1行）
        assert entry["recall"] == 0.0
        assert entry["f1"] == 0.0
    assert result["mae_ms"] is None


def test_g2_emergency_plan_through_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan = valid_plan((0,), duration_ms=300000, result_source="emergency", quality_level="degraded")
    pointer = valid_pointer("chapter-plan-977ac79c4748")
    patch_loaders(monkeypatch, plan, pointer)
    annotation = valid_annotation(
        duration_ms=300000,
        source_timeline={"timeline_id": DEV01_TIMELINE_ID, "sha256": DEV01_TIMELINE_SHA},
        chapters=[
            {"index": 0, "start_ms": 0, "title": "第一章", "source": "manual_independent", "reviewed": True},
            {"index": 1, "start_ms": 60000, "title": "第二章", "source": "manual_independent", "reviewed": True},
        ],
    )
    path = write_annotation(tmp_path, annotation)
    record = evaluate_chapter_plan_against_annotation(
        tmp_path / "outputs", "DEV01", path, project_root=tmp_path
    )
    assert set(record) == {"schema_name", "schema_version", "task_id", "plan", "annotation", "evaluation"}
    assert record["schema_name"] == "BoundaryEvaluationVideoRecord"
    assert record["schema_version"] == "1.0"
    assert record["task_id"] == "DEV01"
    assert extract_internal_boundaries(plan) == []
    assert record["plan"]["result_source"] == "emergency"
    assert record["plan"]["quality_level"] == "degraded"
    assert record["plan"]["internal_boundary_count"] == 0
    assert record["plan"]["boundary_count"] == 1
    assert record["plan"]["result_id"] == "chapter-plan-977ac79c4748"
    assert record["plan"]["manifest_sha256"] == "b" * 64
    assert record["plan"]["strategy"] == "llm"
    assert record["plan"]["timeline_id"] == DEV01_TIMELINE_ID
    assert record["plan"]["timeline_sha256"] == DEV01_TIMELINE_SHA
    assert record["plan"]["duration_ms"] == 300000
    evaluation = record["evaluation"]
    # 指标==G1形态：precision null／recall 0.0／f1两容差0.0／mae null
    assert evaluation["gt_count"] == 1
    assert evaluation["pred_count"] == 0
    for entry in evaluation["per_tolerance"]:
        assert entry["precision"] is None
        assert entry["recall"] == 0.0
        assert entry["f1"] == 0.0
    assert evaluation["mae_ms"] is None
    assert record["annotation"]["chapter_count"] == 2
    assert record["annotation"]["annotation_version"] == "1.0"
    assert record["annotation"]["relative_path"] == "DEV01.json"
    assert record["annotation"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_h1_both_empty() -> None:
    result = evaluate_boundaries([], [])
    assert result["gt_count"] == 0
    assert result["pred_count"] == 0
    for entry in result["per_tolerance"]:
        assert entry["precision"] is None
        assert entry["recall"] is None
        assert entry["f1"] is None
    assert result["mae_ms"] is None


def test_h2_aggregate_excludes_double_null() -> None:
    records = [
        {"task_id": "A", "result": evaluate_boundaries([60000, 180000, 300000], [60000, 180000, 300000])},
        {"task_id": "B", "result": evaluate_boundaries([60000, 300000], [])},
        {"task_id": "C", "result": evaluate_boundaries([], [])},
    ]
    aggregate = aggregate_boundary_evaluations(records)
    assert set(aggregate) == {
        "schema_name",
        "schema_version",
        "evaluation_policy_version",
        "video_count",
        "metrics",
        "per_video",
    }
    assert aggregate["schema_name"] == "BoundaryEvaluationAggregate"
    assert aggregate["video_count"] == 3
    stats = aggregate["metrics"]["f1_at_15s"]
    assert stats["non_null_count"] == 2
    assert stats["null_count"] == 1
    assert stats["mean"] == 0.5
    assert stats["std"] == pytest.approx(math.sqrt(0.5), abs=1e-12)
    assert [row["task_id"] for row in aggregate["per_video"]] == ["A", "B", "C"]


@pytest.mark.parametrize(
    ("gt", "pred", "tolerances"),
    [
        ([100000], [200000, 100000], None),  # I1 pred乱序
        ([100000], [100000, 100000], None),  # I2 重复（非严格递增）
        ([-1], [100000], None),  # I3 负值
        ([100.5], [100000], None),  # I4 浮点
        ([100000], [100000], (30000, 15000)),  # I5a 非升序
        ([100000], [100000], (0,)),  # I5b 非正
        ([100000], [100000], (15000, 15000)),  # I5c 重复
    ],
)
def test_i1_to_i5_invalid_inputs(gt, pred, tolerances) -> None:
    kwargs = {} if tolerances is None else {"tolerances_ms": tolerances}
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        evaluate_boundaries(gt, pred, **kwargs)
    assert code_error(excinfo) == "EVALUATION_INPUT_INVALID"
    # 匹配函数同样拒绝非法列表入参
    if tolerances is None:
        with pytest.raises(BoundaryEvaluationError) as excinfo2:
            match_boundaries_one_to_one(gt, pred, 15000)
        assert code_error(excinfo2) == "EVALUATION_INPUT_INVALID"


def test_i_bool_inputs_rejected() -> None:
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        evaluate_boundaries([True], [100000])
    assert code_error(excinfo) == "EVALUATION_INPUT_INVALID"
    with pytest.raises(BoundaryEvaluationError) as excinfo2:
        match_boundaries_one_to_one([100000], [115000], True)
    assert code_error(excinfo2) == "EVALUATION_INPUT_INVALID"


def test_j1_aggregate_mean_and_sample_std() -> None:
    records = [
        {"task_id": "A", "result": evaluate_boundaries([60000], [60000])},
        {"task_id": "B", "result": evaluate_boundaries([100000, 500000], [100000, 900000])},
        {"task_id": "C", "result": evaluate_boundaries([60000], [900000])},
    ]
    aggregate = aggregate_boundary_evaluations(records)
    values = [row["f1_at_15s"] for row in aggregate["per_video"]]
    assert values == [1.0, 0.5, 0.0]
    stats = aggregate["metrics"]["f1_at_15s"]
    assert stats["mean"] == 0.5
    assert stats["std"] == 0.5  # ddof=1：var=(0.25+0+0.25)/2=0.25（裁决J1钉死）


def test_j2_single_video_std_is_null() -> None:
    aggregate = aggregate_boundary_evaluations(
        [{"task_id": "A", "result": evaluate_boundaries([60000], [60000])}]
    )
    stats = aggregate["metrics"]["f1_at_15s"]
    assert stats["mean"] == 1.0
    assert stats["std"] is None  # 禁止ddof=0伪装成0.0（Q6.3）
    assert stats["non_null_count"] == 1
    assert stats["null_count"] == 0


def test_j3_mae_aggregate_with_null() -> None:
    records = [
        {"task_id": "A", "result": evaluate_boundaries([60000, 180000, 300000], [60000, 180000, 300000])},
        {"task_id": "B", "result": evaluate_boundaries([100000, 114000], [113000, 127000])},
        {"task_id": "C", "result": evaluate_boundaries([60000, 180000], [900000, 1200000])},
    ]
    aggregate = aggregate_boundary_evaluations(records)
    stats = aggregate["metrics"]["mae_ms"]
    assert stats["non_null_count"] == 2
    assert stats["null_count"] == 1
    assert stats["mean"] == 6500.0
    assert stats["std"] == pytest.approx(6500 * math.sqrt(2), rel=1e-12)


def _broken_annotation(**overrides) -> dict:
    return valid_annotation(**overrides)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update({"unexpected_key": 1}),  # K2 顶层多余键
        lambda d: d.pop("chapters"),  # K3 缺chapters
        lambda d: d["chapters"][0].update({"start_ms": 1000}),  # K4 首章非0
        lambda d: d["chapters"][1].update({"start_ms": 180000}),  # K5 非严格递增（与后章相等）
        lambda d: d["chapters"].append(
            {"index": 3, "start_ms": DEV01_DURATION_MS, "title": "越界章", "source": "manual_independent", "reviewed": True}
        ),  # K6 start_ms>=duration_ms
        lambda d: d["chapters"][1].update({"title": "  "}),  # K7 title空白
        lambda d: d["chapters"][1].update({"source": "publisher_timestamps"}),  # K8 未检查形态非枚举值
        lambda d: d.update({"schema_version": "2.0"}),  # K9 版本非1.0
        lambda d: d.update({"duration_ms": 0}),  # K10 duration_ms=0
    ],
    ids=["K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "K10"],
)
def test_k2_to_k10_schema_rejections(tmp_path: Path, mutate) -> None:
    data = _broken_annotation()
    mutate(data)
    path = write_annotation(tmp_path, data)
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        load_ground_truth(path)
    assert code_error(excinfo) == "ANNOTATION_SCHEMA_INVALID"


def test_k1_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        load_ground_truth(tmp_path / "NOT_EXIST.json")
    assert code_error(excinfo) == "ANNOTATION_FILE_MISSING"
    directory = tmp_path / "adir"
    directory.mkdir()
    with pytest.raises(BoundaryEvaluationError) as excinfo2:
        load_ground_truth(directory)
    assert code_error(excinfo2) == "ANNOTATION_FILE_MISSING"


def test_l1_file_level_not_reviewed(tmp_path: Path) -> None:
    data = _broken_annotation(reviewed=False, reviewer=None, reviewed_at=None)
    path = write_annotation(tmp_path, data)
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        load_ground_truth(path)
    assert code_error(excinfo) == "ANNOTATION_NOT_REVIEWED"


def test_l1_schema_check_precedes_reviewed(tmp_path: Path) -> None:
    # 裁决§四.2 L1注：未复核＋多余键并存时先报SCHEMA_INVALID（校验顺序§三.1.5）
    data = _broken_annotation(reviewed=False, reviewer=None, reviewed_at=None)
    data["unexpected_key"] = 1
    path = write_annotation(tmp_path, data)
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        load_ground_truth(path)
    assert code_error(excinfo) == "ANNOTATION_SCHEMA_INVALID"


def test_l2_chapter_level_not_reviewed(tmp_path: Path) -> None:
    data = _broken_annotation()
    data["chapters"][2]["reviewed"] = False
    path = write_annotation(tmp_path, data)
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        load_ground_truth(path)
    assert code_error(excinfo) == "ANNOTATION_NOT_REVIEWED"


def test_m1_task_id_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_loaders(monkeypatch, valid_plan(), valid_pointer())
    path = write_annotation(tmp_path, _broken_annotation(task_id="OTHER01"))
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        evaluate_chapter_plan_against_annotation(tmp_path / "outputs", "DEV01", path, project_root=tmp_path)
    assert code_error(excinfo) == "ANNOTATION_IDENTITY_MISMATCH"


def test_m2_duration_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_loaders(monkeypatch, valid_plan(), valid_pointer())
    path = write_annotation(tmp_path, _broken_annotation(duration_ms=DEV01_DURATION_MS - 1))
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        evaluate_chapter_plan_against_annotation(tmp_path / "outputs", "DEV01", path, project_root=tmp_path)
    assert code_error(excinfo) == "ANNOTATION_IDENTITY_MISMATCH"


def test_m3_timeline_sha_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_loaders(monkeypatch, valid_plan(), valid_pointer())
    path = write_annotation(
        tmp_path,
        _broken_annotation(source_timeline={"timeline_id": DEV01_TIMELINE_ID, "sha256": "f" * 64}),
    )
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        evaluate_chapter_plan_against_annotation(tmp_path / "outputs", "DEV01", path, project_root=tmp_path)
    assert code_error(excinfo) == "ANNOTATION_IDENTITY_MISMATCH"


def test_n1_plan_extraction_exact() -> None:
    plan = valid_plan((0, 60000, 180000, 300000))
    assert extract_internal_boundaries(plan) == [60000, 180000, 300000]


def test_n2_incomplete_plan_rejected() -> None:
    plan = valid_plan()
    del plan["strategy"]  # 残缺plan：顶层八键精确不满足
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        extract_internal_boundaries(plan)
    assert code_error(excinfo) == "EVALUATION_INPUT_INVALID"


# ---------------------------------------------------------------------------
# 补充可达性用例（矩阵外的封闭码/函数面覆盖，登记实现决定提交G2核对）
# ---------------------------------------------------------------------------


def test_ground_truth_internal_boundaries_extraction() -> None:
    assert ground_truth_internal_boundaries(valid_annotation()) == [60000, 180000]


def test_input_chapter_plan_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_loaders(monkeypatch, None, None)
    path = write_annotation(tmp_path, valid_annotation())
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        evaluate_chapter_plan_against_annotation(tmp_path / "outputs", "DEV01", path, project_root=tmp_path)
    assert code_error(excinfo) == "INPUT_CHAPTER_PLAN_MISSING"


def test_adapter_propagates_existing_typed_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Q8.3：既有类型化错误原样传播，不包裹不吞码
    from video_doc.boundary_planner_v2 import CurrentPointerCorruptError

    def _raise(root, task_id):
        raise CurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：测试传播。")

    monkeypatch.setattr(module, "load_chapter_plan_current", _raise)
    path = write_annotation(tmp_path, valid_annotation())
    with pytest.raises(CurrentPointerCorruptError):
        evaluate_chapter_plan_against_annotation(tmp_path / "outputs", "DEV01", path, project_root=tmp_path)


def test_aggregate_rejects_non_default_tolerances_and_missing_keys() -> None:
    non_default = evaluate_boundaries([60000], [60000], tolerances_ms=(15000,))
    with pytest.raises(BoundaryEvaluationError) as excinfo:
        aggregate_boundary_evaluations([{"task_id": "A", "result": non_default}])
    assert code_error(excinfo) == "EVALUATION_INPUT_INVALID"
    with pytest.raises(BoundaryEvaluationError) as excinfo2:
        aggregate_boundary_evaluations([{"result": evaluate_boundaries([60000], [60000])}])
    assert code_error(excinfo2) == "EVALUATION_INPUT_INVALID"


def test_relative_path_falls_back_to_file_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_loaders(monkeypatch, valid_plan(), valid_pointer())
    path = write_annotation(tmp_path, valid_annotation())
    record = evaluate_chapter_plan_against_annotation(
        tmp_path / "outputs", "DEV01", path, project_root=tmp_path / "elsewhere"
    )
    assert record["annotation"]["relative_path"] == "DEV01.json"


def test_source_timeline_null_is_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Q1.3：标注source_timeline可为null（标注早于Timeline 2.0时），null跳过该项绑定
    patch_loaders(monkeypatch, valid_plan(), valid_pointer())
    path = write_annotation(tmp_path, valid_annotation(source_timeline=None))
    record = evaluate_chapter_plan_against_annotation(
        tmp_path / "outputs", "DEV01", path, project_root=tmp_path
    )
    assert record["plan"]["timeline_id"] == DEV01_TIMELINE_ID


def test_module_has_no_writes_and_closed_codes() -> None:
    # 封闭码集6项（Q8.2）；模块零写入：公开面无写文件函数由实现轮diff审查保证，
    # 此处钉死码集与错误基类
    assert module.BOUNDARY_EVALUATION_ERROR_CODES == frozenset(
        {
            "ANNOTATION_FILE_MISSING",
            "ANNOTATION_SCHEMA_INVALID",
            "ANNOTATION_NOT_REVIEWED",
            "ANNOTATION_IDENTITY_MISMATCH",
            "EVALUATION_INPUT_INVALID",
            "INPUT_CHAPTER_PLAN_MISSING",
        }
    )
    for exc_class in (
        module.AnnotationFileMissingError,
        module.AnnotationSchemaInvalidError,
        module.AnnotationNotReviewedError,
        module.AnnotationIdentityMismatchError,
        module.EvaluationInputInvalidError,
        module.InputChapterPlanMissingError,
    ):
        assert issubclass(exc_class, BoundaryEvaluationError)
        assert exc_class.error_code in module.BOUNDARY_EVALUATION_ERROR_CODES
