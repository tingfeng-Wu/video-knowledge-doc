"""阶段13-0 UnifiedTimeline 2.0契约与M1只读投影的定向单元测试。

被测模块：`video_doc/unified_timeline.py`（契约来源：M2-0-P2设计）。

覆盖：
- `project_legacy_asr_timeline`正常投影、ID映射、毫秒转换、确定性、身份敏感、
  boundary_eligible、重叠事件与primary_transcript/modalities取值；
- M1输入失败（Timeline 1.0校验器拦截）；
- `validate_unified_timeline`顶层/language/primary_transcript/modalities/事件级拒绝；
- `canonical_transcript`模式正常路径与legacy_event_id约束；
- `canonical_json_bytes`对NaN的拒绝；
- `project_task_legacy_timeline`只读加载投影与加载失败。

全部使用合成fixture；除tmp_path外不写盘、不访问网络与真实outputs。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from video_doc.asr_transcribe import ASR_SCHEMA_VERSION
from video_doc.unified_timeline import (
    UnifiedTimelineError,
    canonical_json_bytes,
    canonical_json_sha256,
    compute_timeline_id,
    project_legacy_asr_timeline,
    project_task_legacy_timeline,
    validate_unified_timeline,
)


# ---------------------------------------------------------------------------
# 合成M1 Timeline 1.0 fixture（严格按校验器规则构造）
# ---------------------------------------------------------------------------

DEFAULT_SEGMENTS: list[tuple[float, float, str]] = [
    (16.11, 20.5, "hello world"),
    (20.5, 30.25, "second part"),
    (31.0, 45.0, "third part"),
]


def make_m1_timeline(
    *,
    events: list[tuple[float, float, str]] | None = None,
    task_id: str = "TEST01",
    duration_seconds: float = 60.0,
    language: dict[str, Any] | None = None,
    schema_version: str = "1.0",
) -> dict[str, Any]:
    """构造默认合法的M1时间轴；参数可覆盖以便注入错误。"""

    segments = DEFAULT_SEGMENTS if events is None else events
    built: list[dict[str, Any]] = []
    prev_end = 0.0
    for index, (start, end, text) in enumerate(segments, start=1):
        built.append(
            {
                "id": f"a{index:04d}",
                "type": "asr",
                "source_segment_id": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "pause_before_seconds": round(max(0.0, start - prev_end), 3),
                "text": text,
            }
        )
        prev_end = end
    return {
        "schema_version": schema_version,
        "task_id": task_id,
        "duration_seconds": round(duration_seconds, 3),
        "language": language if language is not None else {"code": "en", "probability": 0.98},
        "source_asr": {
            "schema_version": ASR_SCHEMA_VERSION,
            "cache_key": "k" * 64,
            "sha256": "a" * 64,
        },
        "events": built,
    }


def base_projection() -> dict[str, Any]:
    return project_legacy_asr_timeline(make_m1_timeline())


def reseal(payload: dict[str, Any]) -> dict[str, Any]:
    """篡改后重算timeline_id，避免ID不匹配先于目标检查拦截。"""

    payload.pop("timeline_id")
    payload["timeline_id"] = compute_timeline_id(payload)
    return payload


# ---------------------------------------------------------------------------
# A. 投影正常路径
# ---------------------------------------------------------------------------


def test_a1_project_three_event_m1_passes_validation() -> None:
    unified = base_projection()
    assert isinstance(unified, dict)
    assert validate_unified_timeline(unified) is None


def test_a2_event_id_and_ms_mapping() -> None:
    unified = base_projection()
    events = unified["events"]
    assert [event["id"] for event in events] == [
        "te000001",
        "te000002",
        "te000003",
    ]
    assert [event["source_segment_id"] for event in events] == [
        "legacy:a0001",
        "legacy:a0002",
        "legacy:a0003",
    ]
    assert [event["legacy_event_id"] for event in events] == [
        "a0001",
        "a0002",
        "a0003",
    ]
    assert events[0]["start_ms"] == 16110
    assert events[0]["end_ms"] == 20500
    assert events[1]["start_ms"] == 20500
    assert events[2]["start_ms"] == 31000
    assert events[2]["end_ms"] == 45000
    assert events[2]["payload"]["pause_before_ms"] == 750


def test_a3_projection_is_deterministic() -> None:
    first = base_projection()
    second = base_projection()
    assert first["timeline_id"] == second["timeline_id"]
    assert first == second
    assert compute_timeline_id(first) == first["timeline_id"]


def test_a4_identity_changes_produce_different_timeline_id() -> None:
    baseline = base_projection()["timeline_id"]

    text_variant = project_legacy_asr_timeline(
        make_m1_timeline(
            events=[
                (16.11, 20.5, "hello world"),
                (20.5, 30.25, "second part"),
                (31.0, 45.0, "changed text"),
            ]
        )
    )
    assert text_variant["timeline_id"] != baseline

    start_variant = project_legacy_asr_timeline(
        make_m1_timeline(
            events=[
                (16.11, 20.5, "hello world"),
                (20.5, 30.25, "second part"),
                (32.0, 45.0, "third part"),
            ]
        )
    )
    assert start_variant["timeline_id"] != baseline

    duration_variant = project_legacy_asr_timeline(make_m1_timeline(duration_seconds=90.0))
    assert duration_variant["timeline_id"] != baseline

    language_variant = project_legacy_asr_timeline(
        make_m1_timeline(language={"code": "zh", "probability": 0.98})
    )
    assert language_variant["timeline_id"] != baseline

    task_variant = project_legacy_asr_timeline(make_m1_timeline(task_id="TEST02"))
    assert task_variant["timeline_id"] != baseline


def test_a5_boundary_eligible_and_first_pause() -> None:
    unified = project_legacy_asr_timeline(
        make_m1_timeline(events=[(10.0, 20.0, "first"), (10.0, 25.0, "second")])
    )
    assert validate_unified_timeline(unified) is None
    events = unified["events"]
    assert events[0]["payload"]["boundary_eligible"] is True
    assert events[1]["payload"]["boundary_eligible"] is False
    assert events[0]["payload"]["pause_before_ms"] == events[0]["start_ms"]
    assert events[1]["payload"]["pause_before_ms"] == 0


def test_a6_overlapping_events_project_and_validate() -> None:
    unified = project_legacy_asr_timeline(
        make_m1_timeline(events=[(5.0, 20.0, "one"), (10.0, 25.0, "two")])
    )
    assert validate_unified_timeline(unified) is None
    assert unified["events"][1]["payload"]["pause_before_ms"] == 0


def test_a7_primary_transcript_modalities_and_status() -> None:
    source = make_m1_timeline()
    unified = project_legacy_asr_timeline(source)
    primary = unified["primary_transcript"]
    assert primary["input_mode"] == "legacy_asr_projection"
    assert primary["source_kind"] == "asr"
    assert primary["transcript_id"] is None
    assert primary["selection_id"] is None
    assert primary["canonical_sha256"] is None
    assert primary["legacy_timeline_sha256"] == canonical_json_sha256(source)
    assert unified["modalities"]["transcript"] == {
        "status": "available",
        "reason_codes": [],
    }
    assert unified["modalities"]["frame_change"] == {
        "status": "not_run",
        "reason_codes": [],
    }
    assert unified["modalities"]["ocr"] == {"status": "not_run", "reason_codes": []}
    assert unified["run_status"] == "success"
    assert unified["quality_level"] == "text_only"
    assert unified["schema_name"] == "UnifiedTimeline"
    assert unified["schema_version"] == "2.0"
    assert unified["duration_ms"] == 60000


# ---------------------------------------------------------------------------
# B. 投影输入失败（M1 Timeline 1.0校验器拦截）
# ---------------------------------------------------------------------------


def test_b_illegal_m1_payloads_rejected_by_projection() -> None:
    cases: list[dict[str, Any]] = []

    payload = make_m1_timeline(schema_version="1.1")
    cases.append(payload)

    payload = make_m1_timeline()
    payload["events"][1]["id"] = "a0009"
    cases.append(payload)

    payload = make_m1_timeline()
    payload["events"] = []
    cases.append(payload)

    payload = make_m1_timeline()
    payload["events"][2]["pause_before_seconds"] = 0.0
    cases.append(payload)

    payload = make_m1_timeline()
    payload["events"][0]["text"] = ""
    cases.append(payload)

    payload = make_m1_timeline()
    payload["events"][2]["end"] = 70.0
    cases.append(payload)

    for bad in cases:
        with pytest.raises(UnifiedTimelineError, match="M1 Timeline 1.0校验失败"):
            project_legacy_asr_timeline(bad)


def test_b_non_dict_input_rejected() -> None:
    with pytest.raises(UnifiedTimelineError):
        project_legacy_asr_timeline("not a dict")  # type: ignore[arg-type]
    with pytest.raises(UnifiedTimelineError):
        project_legacy_asr_timeline(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# C. 顶层校验拒绝
# ---------------------------------------------------------------------------


def test_c_missing_top_level_field() -> None:
    payload = base_projection()
    del payload["modalities"]
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_c_extra_top_level_field() -> None:
    payload = base_projection()
    payload["extra_field"] = 1
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_c_wrong_schema_name() -> None:
    payload = base_projection()
    payload["schema_name"] = "ASRTimeline"
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_c_wrong_schema_version() -> None:
    payload = base_projection()
    payload["schema_version"] = "3.0"
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_c_bad_timeline_id_format() -> None:
    payload = base_projection()
    payload["timeline_id"] = "unified-timeline-ZZZZZZZZZZZZ"
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_c_tampered_events_without_reseal_hits_id_mismatch() -> None:
    payload = base_projection()
    payload["events"][0]["payload"]["text"] = "tampered text"
    with pytest.raises(UnifiedTimelineError, match="timeline_id与规范内容不一致"):
        validate_unified_timeline(payload)


def test_c_expected_task_id_mismatch() -> None:
    payload = base_projection()
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload, expected_task_id="TEST02")


@pytest.mark.parametrize("duration_ms", [0, -5, 60000.0, True])
def test_c_bad_duration_ms(duration_ms: Any) -> None:
    payload = base_projection()
    payload["duration_ms"] = duration_ms
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_c_bad_run_status_value() -> None:
    payload = base_projection()
    payload["run_status"] = "partial"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_c_bad_quality_level_value() -> None:
    payload = base_projection()
    payload["quality_level"] = "high"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


# ---------------------------------------------------------------------------
# D. language与primary_transcript拒绝
# ---------------------------------------------------------------------------


def test_d_illegal_language_provenance() -> None:
    payload = base_projection()
    payload["language"]["provenance"] = "asr"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_d_provenance_inconsistent_with_input_mode() -> None:
    payload = base_projection()
    payload["language"]["provenance"] = "canonical_transcript"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


@pytest.mark.parametrize("confidence", [1.5, -0.1])
def test_d_out_of_range_confidence(confidence: float) -> None:
    payload = base_projection()
    payload["language"]["confidence"] = confidence
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_d_legacy_mode_must_not_forge_p1_identities() -> None:
    payload = base_projection()
    payload["primary_transcript"]["transcript_id"] = (
        "canonical-transcript-0123456789ab"
    )
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_d_legacy_mode_requires_legacy_timeline_sha256() -> None:
    payload = base_projection()
    payload["primary_transcript"]["legacy_timeline_sha256"] = None
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


@pytest.mark.parametrize("bad_sha", ["abc", "A" * 64, "z" * 64])
def test_d_legacy_timeline_sha256_must_be_lowercase_hex64(bad_sha: str) -> None:
    payload = base_projection()
    payload["primary_transcript"]["legacy_timeline_sha256"] = bad_sha
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_d_canonical_mode_requires_transcript_id() -> None:
    payload = base_projection()
    payload["primary_transcript"]["input_mode"] = "canonical_transcript"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_d_canonical_mode_forbids_legacy_timeline_sha256() -> None:
    payload = base_projection()
    primary = payload["primary_transcript"]
    primary["input_mode"] = "canonical_transcript"
    primary["transcript_id"] = "canonical-transcript-0123456789ab"
    primary["selection_id"] = "transcript-selection-0123456789ab"
    primary["canonical_sha256"] = "b" * 64
    # legacy_timeline_sha256保持非null → 应拒绝
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_d_illegal_source_kind() -> None:
    payload = base_projection()
    payload["primary_transcript"]["source_kind"] = "subtitle_ocr"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_d_illegal_input_mode() -> None:
    payload = base_projection()
    payload["primary_transcript"]["input_mode"] = "subtitle"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


# ---------------------------------------------------------------------------
# E. modalities与状态组合拒绝
# ---------------------------------------------------------------------------


def test_e_illegal_modality_status() -> None:
    payload = base_projection()
    payload["modalities"]["ocr"]["status"] = "pending"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_e_failed_without_reason_codes() -> None:
    payload = base_projection()
    payload["modalities"]["ocr"]["status"] = "failed"
    payload["modalities"]["ocr"]["reason_codes"] = []
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_e_reason_codes_must_be_list() -> None:
    payload = base_projection()
    payload["modalities"]["ocr"]["reason_codes"] = "boom"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_e_missing_ocr_modality_key() -> None:
    payload = base_projection()
    del payload["modalities"]["ocr"]
    reseal(payload)
    with pytest.raises(UnifiedTimelineError, match="精确匹配"):
        validate_unified_timeline(payload)


def test_e_all_visuals_disabled_requires_success_text_only() -> None:
    payload = base_projection()
    payload["modalities"]["frame_change"]["status"] = "disabled"
    payload["modalities"]["ocr"]["status"] = "disabled"
    payload["run_status"] = "degraded"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_e_both_visuals_available_requires_multimodal() -> None:
    payload = base_projection()
    payload["modalities"]["frame_change"]["status"] = "available"
    payload["modalities"]["ocr"]["status"] = "available"
    # quality_level保持text_only → 期望success/multimodal → 拒绝
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_e_frame_change_failed_requires_degraded() -> None:
    payload = base_projection()
    payload["modalities"]["frame_change"]["status"] = "failed"
    payload["modalities"]["frame_change"]["reason_codes"] = ["ffmpeg_missing"]
    # run_status保持success/quality text_only → 期望degraded/text_only → 拒绝
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def _valid_pe_event(start_ms: int = 50000) -> dict[str, Any]:
    """构造一个合法的page_change事件（13-1决策9：available需≥1个同类型事件）。"""

    return {
        "id": "pe000001",
        "type": "page_change",
        "source_kind": "page_change",
        "source_segment_id": "pc:pc000001",
        "start_ms": start_ms,
        "end_ms": start_ms + 1,
        "payload_schema": "PageChangeEventPayload/1.0",
        "payload": {
            "frame_id": "fr000002",
            "previous_frame_id": "fr000001",
            "candidate_id": "pc000001",
            "change_strength": 0.5,
            "perceptual_distance": 0.3,
        },
        "legacy_event_id": None,
    }


def test_e_one_visual_available_other_not_run_is_valid_multimodal() -> None:
    payload = base_projection()
    payload["modalities"]["frame_change"]["status"] = "available"
    # 决策9：frame_change为available时必须至少有一个page_change事件。
    # pe起点50000ms晚于全部文本事件(≤45000ms)，追加即满足全局排序。
    payload["events"].append(_valid_pe_event(50000))
    payload["run_status"] = "success"
    payload["quality_level"] = "multimodal"
    reseal(payload)
    assert validate_unified_timeline(payload) is None


def test_e_frame_change_available_without_page_change_event_rejected() -> None:
    # 决策9：available但无page_change事件 → 拒绝。
    payload = base_projection()
    payload["modalities"]["frame_change"]["status"] = "available"
    payload["run_status"] = "success"
    payload["quality_level"] = "multimodal"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError, match="page_change"):
        validate_unified_timeline(payload)


def test_e_ocr_available_without_ocr_event_rejected() -> None:
    # 决策9：ocr为available但无ocr事件 → 拒绝。
    payload = base_projection()
    payload["modalities"]["ocr"]["status"] = "available"
    payload["run_status"] = "success"
    payload["quality_level"] = "multimodal"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError, match="ocr"):
        validate_unified_timeline(payload)


def test_e_inactive_visual_with_event_rejected() -> None:
    # 决策9：disabled/not_run/empty的视觉模态不得带同类型事件。
    payload = base_projection()
    payload["events"].append(_valid_pe_event(50000))
    # frame_change保持not_run（base_projection默认），但存在pe事件 → 拒绝。
    reseal(payload)
    with pytest.raises(UnifiedTimelineError, match="不得包含"):
        validate_unified_timeline(payload)


def test_e_transcript_empty_with_success_rejected() -> None:
    payload = base_projection()
    payload["modalities"]["transcript"]["status"] = "empty"
    payload["run_status"] = "success"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_e_transcript_failed_without_reason_codes_rejected() -> None:
    payload = base_projection()
    payload["modalities"]["transcript"]["status"] = "failed"
    payload["modalities"]["transcript"]["reason_codes"] = []
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


# ---------------------------------------------------------------------------
# F. 事件级拒绝（篡改后重算timeline_id）
# ---------------------------------------------------------------------------


def _reject_event_mutation(mutate) -> None:
    payload = base_projection()
    mutate(payload)
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_f_event_type_not_transcript() -> None:
    _reject_event_mutation(lambda p: p["events"][0].__setitem__("type", "asr"))


def test_f_te_ids_not_sequential_after_swap() -> None:
    def mutate(p: dict[str, Any]) -> None:
        p["events"][0], p["events"][1] = p["events"][1], p["events"][0]

    _reject_event_mutation(mutate)


def test_f_event_source_kind_mismatch_with_primary() -> None:
    _reject_event_mutation(
        lambda p: p["events"][0].__setitem__("source_kind", "embedded_subtitle")
    )


def test_f_legacy_mode_null_legacy_event_id() -> None:
    _reject_event_mutation(
        lambda p: p["events"][0].__setitem__("legacy_event_id", None)
    )


def test_f_legacy_event_id_number_mismatch_position() -> None:
    _reject_event_mutation(
        lambda p: p["events"][2].__setitem__("legacy_event_id", "a0002")
    )


def test_f_source_segment_id_not_legacy_mapping() -> None:
    _reject_event_mutation(
        lambda p: p["events"][0].__setitem__("source_segment_id", "legacy:a9999")
    )


def test_f_negative_start_ms() -> None:
    _reject_event_mutation(lambda p: p["events"][0].__setitem__("start_ms", -1))


def test_f_end_ms_not_after_start_ms() -> None:
    def mutate(p: dict[str, Any]) -> None:
        event = p["events"][1]
        event["end_ms"] = event["start_ms"]

    _reject_event_mutation(mutate)


def test_f_end_ms_beyond_duration() -> None:
    _reject_event_mutation(lambda p: p["events"][2].__setitem__("end_ms", 60001))


def test_f_start_ms_out_of_order() -> None:
    _reject_event_mutation(lambda p: p["events"][1].__setitem__("start_ms", 1000))


def test_f_bad_payload_schema() -> None:
    _reject_event_mutation(
        lambda p: p["events"][0].__setitem__("payload_schema", "TranscriptEventPayload/2.0")
    )


def test_f_payload_missing_text() -> None:
    _reject_event_mutation(lambda p: p["events"][0]["payload"].pop("text"))


def test_f_payload_extra_field() -> None:
    _reject_event_mutation(lambda p: p["events"][0]["payload"].__setitem__("extra", 1))


@pytest.mark.parametrize("text", ["", "  ", " padded", "padded "])
def test_f_bad_text(text: str) -> None:
    _reject_event_mutation(lambda p: p["events"][0]["payload"].__setitem__("text", text))


def test_f_pause_before_ms_mismatch() -> None:
    _reject_event_mutation(
        lambda p: p["events"][2]["payload"].__setitem__("pause_before_ms", 749)
    )


def test_f_pause_before_ms_negative() -> None:
    _reject_event_mutation(
        lambda p: p["events"][1]["payload"].__setitem__("pause_before_ms", -1)
    )


def test_f_second_duplicate_start_cannot_be_eligible() -> None:
    payload = project_legacy_asr_timeline(
        make_m1_timeline(events=[(10.0, 20.0, "first"), (10.0, 25.0, "second")])
    )
    payload["events"][1]["payload"]["boundary_eligible"] = True
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


def test_f_boundary_eligible_not_bool() -> None:
    _reject_event_mutation(
        lambda p: p["events"][0]["payload"].__setitem__("boundary_eligible", 1)
    )


# ---------------------------------------------------------------------------
# G. canonical_transcript模式
# ---------------------------------------------------------------------------


def _to_canonical_mode(payload: dict[str, Any]) -> dict[str, Any]:
    primary = payload["primary_transcript"]
    primary["input_mode"] = "canonical_transcript"
    primary["source_kind"] = "asr"
    primary["transcript_id"] = "canonical-transcript-0123456789ab"
    primary["selection_id"] = "transcript-selection-0123456789ab"
    primary["canonical_sha256"] = "b" * 64
    primary["legacy_timeline_sha256"] = None
    payload["language"]["provenance"] = "canonical_transcript"
    for index, event in enumerate(payload["events"], start=1):
        event["source_segment_id"] = f"ct:t{index:06d}"
        event["legacy_event_id"] = None
    return reseal(payload)


def test_g_canonical_mode_valid_projection_passes() -> None:
    payload = _to_canonical_mode(base_projection())
    assert validate_unified_timeline(payload) is None
    assert payload["primary_transcript"]["input_mode"] == "canonical_transcript"
    assert [event["source_segment_id"] for event in payload["events"]] == [
        "ct:t000001",
        "ct:t000002",
        "ct:t000003",
    ]


def test_g_canonical_mode_rejects_non_null_legacy_event_id() -> None:
    payload = _to_canonical_mode(base_projection())
    payload["events"][1]["legacy_event_id"] = "a0002"
    reseal(payload)
    with pytest.raises(UnifiedTimelineError):
        validate_unified_timeline(payload)


# ---------------------------------------------------------------------------
# H. 规范JSON禁止NaN
# ---------------------------------------------------------------------------


def test_h_canonical_json_bytes_rejects_nan() -> None:
    with pytest.raises(UnifiedTimelineError):
        canonical_json_bytes(float("nan"))
    with pytest.raises(UnifiedTimelineError):
        canonical_json_bytes({"value": float("inf")})


# ---------------------------------------------------------------------------
# I. project_task_legacy_timeline
# ---------------------------------------------------------------------------


def test_i_project_task_from_workspace(tmp_path: Path) -> None:
    source = make_m1_timeline()
    timeline_dir = tmp_path / "TEST01" / "timeline"
    timeline_dir.mkdir(parents=True)
    (timeline_dir / "timeline.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    unified = project_task_legacy_timeline(tmp_path, "TEST01")
    assert validate_unified_timeline(unified, expected_task_id="TEST01") is None
    assert unified == project_legacy_asr_timeline(source)


def test_i_missing_workspace_raises(tmp_path: Path) -> None:
    with pytest.raises(UnifiedTimelineError, match="M1时间轴加载失败"):
        project_task_legacy_timeline(tmp_path / "no-such-root", "TEST01")
