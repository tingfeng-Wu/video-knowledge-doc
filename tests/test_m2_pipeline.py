"""阶段15-0 E0+E1+E3单元定向测试：run-m2参数面、运行计划Schema、失败码词表、预算纯函数、preflight与编排单元件。

对应裁决§四：
- E0：运行计划JSON形态断言、``M2PipelineError``词表封闭性、预算fixtures；
- E1：媒体工具三层发现矩阵＋preflight两分支（注入/伪目录，不依赖真实FFmpeg）；
- E3单元件：journal缓冲/LF、门序、this_run计数分离、摘要8键等编排单元断言
  （全链e2e见``tests/test_m2_pipeline_e2e.py``）。
全程零网络、零供应商请求、零``outputs/``写入（沙箱tmp目录内）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_doc import asr_only_pipeline as asr_mod
from video_doc import m2_pipeline
from video_doc.asr_only_pipeline import (
    ASROnlyPipelineError,
    MediaTools,
)
from video_doc.m2_pipeline import (
    M2_CALL_BUDGETS,
    M2_ENTRY_ERROR_CODES,
    M2_INPUT_BUDGET_LIMITS,
    M2_INPUT_BUDGET_POLICY_VERSION,
    M2_INPUT_BUDGET_STATUSES,
    M2_PLAN_DISPOSITIONS,
    M2_ROLE_BY_STAGE,
    M2_STAGE_SEQUENCE,
    M2_STAGE_STATUSES,
    M2PipelineError,
    InputBudgetRoleDecision,
    M2RunPlan,
    M2StagePlanEntry,
    evaluate_input_budget,
    preflight_task_m2,
    run_task_m2,
)
from video_doc.task_workspace import (
    TaskWorkspaceError,
    initialize_task_workspace,
)

TASK_ID = "M2PRE01"


@pytest.fixture(autouse=True)
def _clean_ffmpeg_env(monkeypatch):
    monkeypatch.delenv("VIDEO_DOC_FFMPEG_DIR", raising=False)


def _tree_snapshot(root: Path) -> dict[str, str]:
    """相对路径→内容哈希/目录标记，用于E01零写断言。"""

    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_dir():
            snapshot[rel] = "dir"
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[rel] = digest
    return snapshot


def _make_fake_tools_dir(base: Path, name: str, *, broken: bool = False) -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    payload = b"fail" if broken else b"ok"
    (directory / "ffmpeg.exe").write_bytes(payload)
    (directory / "ffprobe.exe").write_bytes(payload)
    return directory


def _fake_subprocess_run(monkeypatch):
    def fake_run(cmd, **kwargs):
        path = Path(cmd[0])
        try:
            payload = path.read_bytes()
        except OSError:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0 if payload == b"ok" else 1)

    monkeypatch.setattr(subprocess, "run", fake_run)


def _make_source(tmp_path: Path, name: str = "source.mp4", size: int = 16) -> Path:
    source = tmp_path / name
    source.write_bytes(b"x" * size)
    return source


# ---------------------------------------------------------------------------
# E0：词表与常量封闭性
# ---------------------------------------------------------------------------


def test_entry_error_code_set_matches_adjudication():
    assert M2_ENTRY_ERROR_CODES == frozenset(
        {
            "SOURCE_VIDEO_REQUIRED",
            "SOURCE_VIDEO_MISMATCH",
            "MEDIA_TOOL_NOT_FOUND",
            "MEDIA_TOOL_INVALID",
            "LLM_APPROVAL_REQUIRED",
            "INPUT_BUDGET_UNKNOWN",
            "INPUT_BUDGET_EXCEEDED",
        }
    )


def test_stage_sequence_exact_with_chapter_frames_position():
    assert M2_STAGE_SEQUENCE == (
        "preflight",
        "task_init",
        "transcript",
        "frames",
        "page_changes",
        "ocr",
        "timeline_v2",
        "chapter_plan_v2",
        "chapter_frames",
        "document_content_v2",
        "export_v2",
        "completed",
    )
    assert M2_STAGE_SEQUENCE.index("chapter_frames") == 8
    assert M2_ROLE_BY_STAGE == {
        "chapter_plan_v2": "boundary_model",
        "document_content_v2": "content_model",
    }


def test_input_budget_policy_constants_exact():
    assert M2_INPUT_BUDGET_POLICY_VERSION == "m2-input-budget-policy-1.0"
    assert M2_INPUT_BUDGET_LIMITS["boundary_model"] == {
        "limit_message_chars": 150_000,
        "limit_utf8_bytes": 300_000,
    }
    assert M2_INPUT_BUDGET_LIMITS["content_model"] == {
        "limit_message_chars": 60_000,
        "limit_utf8_bytes": 180_000,
    }
    assert M2_CALL_BUDGETS["boundary_model"] == {
        "business_call_hard_limit": 1,
        "supplier_request_hard_limit": 2,
    }
    # 修复裁决D5：content条目{3,6}（validation-feedback-1.0一次纠错）
    assert M2_CALL_BUDGETS["content_model"] == {
        "business_call_hard_limit": 3,
        "supplier_request_hard_limit": 6,
    }
    assert M2_CALL_BUDGETS["vision_caption_model"] == {
        "business_call_hard_limit": 0,
        "supplier_request_hard_limit": 0,
    }


def test_plan_and_stage_vocabularies_closed():
    assert M2_PLAN_DISPOSITIONS == frozenset(
        {"cache_hit", "will_run", "unmeasurable", "disabled"}
    )
    assert M2_STAGE_STATUSES == frozenset(
        {"started", "cache_hit", "completed", "degraded", "failed"}
    )
    assert M2_INPUT_BUDGET_STATUSES == frozenset({"pass", "unknown", "exceeded"})


def test_m2_error_rejects_codes_outside_closed_set():
    with pytest.raises(ValueError):
        M2PipelineError("NO_AUDIO_TRACK")
    with pytest.raises(ValueError):
        M2PipelineError("SOURCE_VIDEO_REQUIRED", failed_stage="not_a_stage")
    error = M2PipelineError("MEDIA_TOOL_INVALID")
    assert error.reason_codes == ("MEDIA_TOOL_INVALID",)
    assert error.failed_stage == "preflight"
    assert "MEDIA_TOOL_INVALID" in str(error)


# ---------------------------------------------------------------------------
# E0：evaluate_input_budget纯函数（裁决§一1.4/1.5）
# ---------------------------------------------------------------------------


def test_zero_planned_calls_pass_even_if_over_limit():
    decision = evaluate_input_budget(
        "boundary_model",
        {"message_chars": 999_999, "message_bytes": 999_999},
        planned_business_calls=0,
    )
    assert decision.status == "pass"
    assert decision.planned_business_calls == 0


def test_boundary_dev01_anchor_pass_with_full_fields():
    decision = evaluate_input_budget(
        "boundary_model",
        {"message_chars": 22_109, "message_bytes": 33_441},
        planned_business_calls=1,
    )
    assert decision.status == "pass"
    assert decision.role == "boundary_model"
    assert decision.policy_version == M2_INPUT_BUDGET_POLICY_VERSION
    assert decision.limit_message_chars == 150_000
    assert decision.limit_utf8_bytes == 300_000
    assert decision.business_call_hard_limit == 1
    assert decision.supplier_request_hard_limit == 2


@pytest.mark.parametrize(
    "chars,bytes_",
    [
        (150_000, 300_000),  # 恰好等于上限：pass（裁决§一1.2为≤）
        (150_001, 100),  # 字符超限
        (100, 300_001),  # 字节超限
    ],
)
def test_boundary_limit_edges(chars, bytes_):
    decision = evaluate_input_budget(
        "boundary_model",
        {"message_chars": chars, "message_bytes": bytes_},
        planned_business_calls=1,
    )
    expected = "pass" if chars <= 150_000 and bytes_ <= 300_000 else "exceeded"
    assert decision.status == expected


def test_content_multi_slot_per_slot_evaluation():
    ok = {"message_chars": 9_128, "message_bytes": 13_800}
    over = {"message_chars": 60_001, "message_bytes": 100}
    assert (
        evaluate_input_budget(
            "content_model", [ok, ok, ok], planned_business_calls=3
        ).status
        == "pass"
    )
    assert (
        evaluate_input_budget(
            "content_model", [ok, over, ok], planned_business_calls=3
        ).status
        == "exceeded"
    )
    assert (
        evaluate_input_budget(
            "content_model", [ok, None], planned_business_calls=2
        ).status
        == "unknown"
    )
    # 超限优先于不可测（实现约定，登记于模块docstring指向的裁决§一1.6）
    assert (
        evaluate_input_budget(
            "content_model", [None, over], planned_business_calls=2
        ).status
        == "exceeded"
    )


def test_content_empty_measurements_with_calls_unknown():
    decision = evaluate_input_budget("content_model", [], planned_business_calls=1)
    assert decision.status == "unknown"
    assert decision.slot_measurements == ()


@pytest.mark.parametrize(
    "measurement",
    [
        {"message_chars": 10},  # 缺bytes
        {"message_bytes": 10},  # 缺chars
        {"message_chars": -1, "message_bytes": 10},  # 负数
        {"message_chars": 1.5, "message_bytes": 10},  # 非整数
        {"message_chars": True, "message_bytes": 10},  # bool不是计数
        "not-a-mapping",
    ],
)
def test_malformed_measurements_treated_unknown(measurement):
    decision = evaluate_input_budget(
        "boundary_model", [measurement, None], planned_business_calls=1
    )
    assert decision.status == "unknown"


def test_evaluate_rejects_bad_role_and_calls():
    with pytest.raises(ValueError):
        evaluate_input_budget("vision_caption_model", None, planned_business_calls=1)
    with pytest.raises(ValueError):
        evaluate_input_budget("boundary_model", None, planned_business_calls=-1)
    with pytest.raises(ValueError):
        evaluate_input_budget(
            "boundary_model", None, planned_business_calls=True
        )
    with pytest.raises(ValueError):
        evaluate_input_budget("boundary_model", "abc", planned_business_calls=1)


def test_decision_to_json_shape():
    decision = evaluate_input_budget(
        "boundary_model",
        {"message_chars": 22_109, "message_bytes": 33_441},
        planned_business_calls=1,
    )
    payload = decision.to_json()
    # 审查B1修正：呈现层恰为裁决§一1.5的8键，不增不减
    assert set(payload) == {
        "policy_version",
        "limit_chars",
        "limit_bytes",
        "measured_chars",
        "measured_bytes",
        "status",
        "planned_business_calls",
        "supplier_request_limit",
    }
    assert payload["policy_version"] == M2_INPUT_BUDGET_POLICY_VERSION
    assert payload["limit_chars"] == 150_000
    assert payload["limit_bytes"] == 300_000
    # G1解释（审查报告§四.2）：measured为按调用槽位排序的列表，boundary单调用单元素
    assert payload["measured_chars"] == [22_109]
    assert payload["measured_bytes"] == [33_441]
    assert payload["status"] == "pass"
    assert payload["planned_business_calls"] == 1
    assert payload["supplier_request_limit"] == 2
    json.dumps(payload, ensure_ascii=False)


def test_decision_to_json_zero_calls_measured_null():
    decision = evaluate_input_budget(
        "boundary_model",
        {"message_chars": 10, "message_bytes": 10},
        planned_business_calls=0,
    )
    payload = decision.to_json()
    # 裁决§一1.5「measured记null」字面：planned=0时双null，即便度量可得
    assert payload["planned_business_calls"] == 0
    assert payload["measured_chars"] is None
    assert payload["measured_bytes"] is None
    assert payload["status"] == "pass"


def test_decision_to_json_multi_slot_lists_keep_nulls_in_order():
    ok = {"message_chars": 9_128, "message_bytes": 13_800}
    decision = evaluate_input_budget(
        "content_model", [ok, None, ok], planned_business_calls=3
    )
    payload = decision.to_json()
    # 逐槽列表按调用序、不可测槽为null（content三调用拓扑）
    assert payload["measured_chars"] == [9_128, None, 9_128]
    assert payload["measured_bytes"] == [13_800, None, 13_800]
    assert payload["limit_chars"] == 60_000
    assert payload["limit_bytes"] == 180_000
    assert payload["supplier_request_limit"] == 6  # 修复裁决D5
    assert payload["status"] == "unknown"


def test_decision_to_json_empty_measurements_empty_lists():
    decision = evaluate_input_budget("content_model", [], planned_business_calls=1)
    payload = decision.to_json()
    assert payload["measured_chars"] == []
    assert payload["measured_bytes"] == []
    assert payload["status"] == "unknown"


# ---------------------------------------------------------------------------
# E0：运行计划JSON形态（裁决§三.3）
# ---------------------------------------------------------------------------


def _full_stage_entries(**overrides) -> tuple[M2StagePlanEntry, ...]:
    entries = []
    for stage in M2_STAGE_SEQUENCE:
        disposition = overrides.get(stage, "unmeasurable")
        budget = None
        if stage == "chapter_plan_v2":
            budget = evaluate_input_budget(
                "boundary_model",
                {"message_chars": 22_109, "message_bytes": 33_441},
                planned_business_calls=1,
            )
        entries.append(
            M2StagePlanEntry(stage=stage, disposition=disposition, input_budget=budget)
        )
    return tuple(entries)


def test_stage_plan_entry_validations():
    with pytest.raises(ValueError):
        M2StagePlanEntry(stage="not_a_stage", disposition="will_run")
    with pytest.raises(ValueError):
        M2StagePlanEntry(stage="frames", disposition="skipped")
    with pytest.raises(ValueError):
        M2StagePlanEntry(
            stage="frames",
            disposition="will_run",
            input_budget=evaluate_input_budget(
                "boundary_model", None, planned_business_calls=1
            ),
        )


def test_run_plan_full_shape_json():
    plan = M2RunPlan(
        task_id=TASK_ID,
        is_new_task=True,
        dry_run=True,
        ocr_mode="off",
        vision_caption="off",
        allow_llm=False,
        model_registry="D:/somewhere/config/model-roles.json",
        source_video_sha256="a" * 64,
        size_bytes=112_142_208,
        duration_ms=None,
        has_audio=None,
        media_tools=("ffmpeg.exe", "ffprobe.exe"),
        stages=_full_stage_entries(
            preflight="cache_hit", ocr="disabled", task_init="will_run"
        ),
    )
    payload = plan.to_json()
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["schema_name"] == "M2RunPlan"
    assert payload["schema_version"] == "1.0"
    # 绝对路径不入运行计划：只记basename（P6 §三）
    assert payload["model_registry"] == "model-roles.json"
    assert "D:/" not in text and "D:\\" not in text and "somewhere" not in text
    assert [entry["stage"] for entry in payload["stages"]] == list(M2_STAGE_SEQUENCE)
    assert payload["stages"][0] == {
        "stage": "preflight",
        "disposition": "cache_hit",
        "input_budget": None,
    }
    ocr_entry = next(e for e in payload["stages"] if e["stage"] == "ocr")
    assert ocr_entry["disposition"] == "disabled"
    plan_entry = next(e for e in payload["stages"] if e["stage"] == "chapter_plan_v2")
    assert plan_entry["disposition"] == "unmeasurable"
    # 审查B1修正：运行计划JSON内的input_budget对象同为§一1.5的8键封闭形状
    assert set(plan_entry["input_budget"]) == {
        "policy_version",
        "limit_chars",
        "limit_bytes",
        "measured_chars",
        "measured_bytes",
        "status",
        "planned_business_calls",
        "supplier_request_limit",
    }
    assert plan_entry["input_budget"]["policy_version"] == (
        M2_INPUT_BUDGET_POLICY_VERSION
    )
    assert plan_entry["input_budget"]["status"] == "pass"
    assert plan_entry["input_budget"]["measured_chars"] == [22_109]
    assert payload["source_video"] == {
        "sha256": "a" * 64,
        "size_bytes": 112_142_208,
        "duration_ms": None,
        "has_audio": None,
    }


def test_run_plan_rejects_bad_order_and_vocab():
    entries = _full_stage_entries()
    with pytest.raises(ValueError):
        M2RunPlan(
            task_id=TASK_ID,
            is_new_task=False,
            dry_run=True,
            ocr_mode="auto",
            vision_caption="off",
            allow_llm=False,
            model_registry=None,
            source_video_sha256=None,
            size_bytes=None,
            duration_ms=None,
            has_audio=None,
            media_tools=("ffmpeg", "ffprobe"),
            stages=(entries[1], entries[0], *entries[2:]),
        )
    with pytest.raises(ValueError):
        M2RunPlan(
            task_id=TASK_ID,
            is_new_task=False,
            dry_run=True,
            ocr_mode="bogus",
            vision_caption="off",
            allow_llm=False,
            model_registry=None,
            source_video_sha256=None,
            size_bytes=None,
            duration_ms=None,
            has_audio=None,
            media_tools=("ffmpeg", "ffprobe"),
            stages=entries,
        )
    with pytest.raises(ValueError):
        M2RunPlan(
            task_id=TASK_ID,
            is_new_task=False,
            dry_run=True,
            ocr_mode="auto",
            vision_caption="on",
            allow_llm=False,
            model_registry=None,
            source_video_sha256=None,
            size_bytes=None,
            duration_ms=None,
            has_audio=None,
            media_tools=("ffmpeg", "ffprobe"),
            stages=entries,
        )


# ---------------------------------------------------------------------------
# E1：媒体工具三层发现矩阵（注入/伪目录，不依赖真实FFmpeg）
# ---------------------------------------------------------------------------


def _preflight_media(tmp_path, monkeypatch, **kwargs):
    """以全新任务分支驱动真实resolve_media_tools：源先行、媒体随后。"""

    output_root = tmp_path / "outputs"
    output_root.mkdir(exist_ok=True)
    source = _make_source(tmp_path)
    _fake_subprocess_run(monkeypatch)
    monkeypatch.setattr(
        m2_pipeline, "_resolve_source", lambda root, tid, sv: (source, True)
    )
    return preflight_task_m2(output_root, TASK_ID, source_video=source, **kwargs)


def test_explicit_dir_wins_over_env_and_path(tmp_path, monkeypatch):
    env_dir = _make_fake_tools_dir(tmp_path, "env_ff")
    explicit_dir = _make_fake_tools_dir(tmp_path, "explicit_ff")
    monkeypatch.setenv("VIDEO_DOC_FFMPEG_DIR", str(env_dir))
    monkeypatch.setattr(asr_mod, "shutil", SimpleNamespace(which=lambda n: None))
    result = _preflight_media(tmp_path, monkeypatch, ffmpeg_dir=explicit_dir)
    assert result.media_tools.directory == explicit_dir.resolve()


def test_env_var_used_when_no_explicit_dir(tmp_path, monkeypatch):
    env_dir = _make_fake_tools_dir(tmp_path, "env_ff")
    monkeypatch.setenv("VIDEO_DOC_FFMPEG_DIR", str(env_dir))
    monkeypatch.setattr(asr_mod, "shutil", SimpleNamespace(which=lambda n: None))
    result = _preflight_media(tmp_path, monkeypatch)
    assert result.media_tools.directory == env_dir.resolve()


def test_path_discovery_same_directory_success(tmp_path, monkeypatch):
    path_dir = _make_fake_tools_dir(tmp_path, "path_ff")
    monkeypatch.setattr(
        asr_mod,
        "shutil",
        SimpleNamespace(which=lambda name: str(path_dir / f"{name}.exe")),
    )
    result = _preflight_media(tmp_path, monkeypatch)
    assert result.media_tools.ffmpeg.name == "ffmpeg.exe"


def test_path_discovery_different_directories_invalid(tmp_path, monkeypatch):
    dir_a = _make_fake_tools_dir(tmp_path, "ff_a")
    dir_b = _make_fake_tools_dir(tmp_path, "ff_b")
    mapping = {"ffmpeg": str(dir_a / "ffmpeg.exe"), "ffprobe": str(dir_b / "ffprobe.exe")}
    monkeypatch.setattr(asr_mod, "shutil", SimpleNamespace(which=mapping.get))
    with pytest.raises(M2PipelineError) as caught:
        _preflight_media(tmp_path, monkeypatch)
    assert caught.value.error_code == "MEDIA_TOOL_INVALID"


def test_path_discovery_none_found_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_mod, "shutil", SimpleNamespace(which=lambda n: None))
    with pytest.raises(M2PipelineError) as caught:
        _preflight_media(tmp_path, monkeypatch)
    assert caught.value.error_code == "MEDIA_TOOL_NOT_FOUND"


def test_explicit_missing_dir_invalid(tmp_path, monkeypatch):
    with pytest.raises(M2PipelineError) as caught:
        _preflight_media(tmp_path, monkeypatch, ffmpeg_dir=tmp_path / "nope")
    assert caught.value.error_code == "MEDIA_TOOL_INVALID"


def test_explicit_dir_missing_ffprobe_invalid(tmp_path, monkeypatch):
    directory = tmp_path / "half_ff"
    directory.mkdir()
    (directory / "ffmpeg.exe").write_bytes(b"ok")
    with pytest.raises(M2PipelineError) as caught:
        _preflight_media(tmp_path, monkeypatch, ffmpeg_dir=directory)
    assert caught.value.error_code == "MEDIA_TOOL_INVALID"


def test_version_probe_failure_invalid(tmp_path, monkeypatch):
    broken_dir = _make_fake_tools_dir(tmp_path, "broken_ff", broken=True)
    with pytest.raises(M2PipelineError) as caught:
        _preflight_media(tmp_path, monkeypatch, ffmpeg_dir=broken_dir)
    assert caught.value.error_code == "MEDIA_TOOL_INVALID"


def test_media_entry_messages_carry_no_absolute_paths(tmp_path, monkeypatch):
    with pytest.raises(M2PipelineError) as caught:
        _preflight_media(tmp_path, monkeypatch, ffmpeg_dir=tmp_path / "nope")
    assert str(tmp_path) not in str(caught.value)


# ---------------------------------------------------------------------------
# E1：preflight两分支（零写、E01/E05语义）
# ---------------------------------------------------------------------------


def _stub_tools(tmp_path, monkeypatch) -> MediaTools:
    directory = _make_fake_tools_dir(tmp_path, "stub_ff")
    monkeypatch.setattr(
        m2_pipeline,
        "resolve_media_tools",
        lambda ffmpeg_dir=None: MediaTools(
            directory=directory,
            ffmpeg=directory / "ffmpeg.exe",
            ffprobe=directory / "ffprobe.exe",
        ),
    )
    return directory


def test_new_task_missing_source_fails_before_write(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _stub_tools(tmp_path, monkeypatch)
    before = _tree_snapshot(tmp_path)
    with pytest.raises(M2PipelineError) as caught:
        preflight_task_m2(output_root, TASK_ID)
    assert caught.value.error_code == "SOURCE_VIDEO_REQUIRED"
    assert _tree_snapshot(tmp_path) == before
    assert not (output_root / TASK_ID).exists()


def test_new_task_source_directory_maps_unreadable(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _stub_tools(tmp_path, monkeypatch)
    source_dir = tmp_path / "not-a-file"
    source_dir.mkdir()
    with pytest.raises(M2PipelineError) as caught:
        preflight_task_m2(output_root, TASK_ID, source_video=source_dir)
    error = caught.value
    assert error.error_code == "SOURCE_VIDEO_REQUIRED"
    assert "SOURCE_FILE_UNREADABLE" in error.reason_codes


def test_new_task_zero_byte_source_maps_empty(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _stub_tools(tmp_path, monkeypatch)
    source = tmp_path / "empty.mp4"
    source.write_bytes(b"")
    with pytest.raises(M2PipelineError) as caught:
        preflight_task_m2(output_root, TASK_ID, source_video=source)
    error = caught.value
    assert error.error_code == "SOURCE_VIDEO_REQUIRED"
    assert "SOURCE_FILE_EMPTY" in error.reason_codes


def test_existing_task_without_source_param_ok_zero_write(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    source = _make_source(tmp_path, "known.mp4")
    initialize_task_workspace(output_root, TASK_ID, source_video=source)
    _stub_tools(tmp_path, monkeypatch)
    before = _tree_snapshot(output_root)
    result = preflight_task_m2(output_root, TASK_ID)
    assert result.is_new_task is False
    assert result.source_video == source.resolve()
    assert result.task_manifest is not None
    assert result.task_manifest.get("source_video") == str(source)
    assert _tree_snapshot(output_root) == before


def test_existing_task_mismatch_does_not_rebind(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    source_a = _make_source(tmp_path, "a.mp4", size=32)
    source_b = _make_source(tmp_path, "b.mp4", size=32)
    source_b.write_bytes(b"y" * 32)
    initialize_task_workspace(output_root, TASK_ID, source_video=source_a)
    _stub_tools(tmp_path, monkeypatch)
    task_json = output_root / TASK_ID / "task.json"
    task_json_before = task_json.read_bytes()
    before = _tree_snapshot(output_root)
    with pytest.raises(M2PipelineError) as caught:
        preflight_task_m2(output_root, TASK_ID, source_video=source_b)
    assert caught.value.error_code == "SOURCE_VIDEO_MISMATCH"
    assert task_json.read_bytes() == task_json_before
    assert _tree_snapshot(output_root) == before


def test_unrecorded_source_maps_required(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()

    def raiser(root, tid, sv):
        raise ASROnlyPipelineError("已有task.json未记录源视频路径。")

    monkeypatch.setattr(m2_pipeline, "_resolve_source", raiser)
    with pytest.raises(M2PipelineError) as caught:
        preflight_task_m2(output_root, TASK_ID)
    error = caught.value
    assert error.error_code == "SOURCE_VIDEO_REQUIRED"
    assert "SOURCE_VIDEO_UNRECORDED" in error.reason_codes


def test_unmapped_asr_only_failure_propagates_untyped(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()

    def raiser(root, tid, sv):
        raise ASROnlyPipelineError("无法保存视频元数据：x")

    monkeypatch.setattr(m2_pipeline, "_resolve_source", raiser)
    with pytest.raises(ASROnlyPipelineError):
        preflight_task_m2(output_root, TASK_ID)


def test_invalid_task_id_rejected_before_any_check(tmp_path):
    with pytest.raises(TaskWorkspaceError):
        preflight_task_m2(tmp_path / "outputs", "bad id!")


def test_output_unwritable_raises_workspace_error(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    source = _make_source(tmp_path)
    _stub_tools(tmp_path, monkeypatch)
    task_root = output_root / TASK_ID

    def fake_access(path, mode):
        # task_root不存在时，可写性检查落在最近存在祖先output_root上
        if Path(path) == output_root:
            return False
        return True

    monkeypatch.setattr(m2_pipeline.os, "access", fake_access)
    before = _tree_snapshot(tmp_path)
    with pytest.raises(TaskWorkspaceError):
        preflight_task_m2(output_root, TASK_ID, source_video=source)
    assert _tree_snapshot(tmp_path) == before
    assert not task_root.exists()


# ---------------------------------------------------------------------------
# E0+E1：run_task_m2入口（参数校验、进度行、E2/E3未接线的显式未实现）
# ---------------------------------------------------------------------------


def test_run_task_m2_rejects_bad_ocr_and_vision_without_progress(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    events: list[tuple[str, str]] = []
    with pytest.raises(ValueError):
        run_task_m2(
            output_root,
            TASK_ID,
            ocr_mode="empty",
            progress=lambda s, t, p=None: events.append((s, t)),
        )
    with pytest.raises(ValueError):
        run_task_m2(
            output_root,
            TASK_ID,
            vision_caption="on",
            progress=lambda s, t, p=None: events.append((s, t)),
        )
    assert events == []


def _stub_probe(
    monkeypatch, *, duration_seconds: float = 10.0, has_audio: bool = True
) -> None:
    """E2/E3测试的探测替身：不依赖真实ffprobe（裁决§四离线验收）。"""

    from video_doc.video_probe import VideoMetadata

    def fake_probe(path):
        p = Path(path)
        return VideoMetadata(
            source_path=str(p),
            file_name=p.name,
            file_size_bytes=p.stat().st_size,
            duration_seconds=duration_seconds,
            width=640,
            height=360,
            frame_rate=25.0,
            video_codec="h264",
            has_audio=has_audio,
            audio_codec="aac",
            audio_sample_rate=16000,
        )

    monkeypatch.setattr(m2_pipeline, "probe_video", fake_probe)


def test_run_task_m2_dry_run_new_task_plan_shape_zero_write(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    source = _make_source(tmp_path)
    _stub_tools(tmp_path, monkeypatch)
    _stub_probe(monkeypatch, duration_seconds=10.0)
    events: list[tuple[str, str]] = []
    before = _tree_snapshot(tmp_path)

    plan = run_task_m2(
        output_root,
        TASK_ID,
        source_video=source,
        dry_run=True,
        model_registry=str(tmp_path / "registry" / "models.json"),
        progress=lambda s, t, p=None: events.append((s, t, p)),
    )

    assert isinstance(plan, M2RunPlan)
    payload = plan.to_json()
    assert [entry["stage"] for entry in payload["stages"]] == list(M2_STAGE_SEQUENCE)
    by_stage = {entry["stage"]: entry for entry in payload["stages"]}
    # 全新任务：上游未满足的预算角色阶段=unmeasurable＋unknown呈现（§三.3字面）。
    assert by_stage["preflight"] == {
        "stage": "preflight",
        "disposition": "cache_hit",
        "input_budget": None,
    }
    assert by_stage["task_init"]["disposition"] == "will_run"
    assert by_stage["transcript"]["disposition"] == "will_run"
    assert by_stage["frames"]["disposition"] == "will_run"
    assert by_stage["page_changes"]["disposition"] == "will_run"
    assert by_stage["ocr"]["disposition"] == "will_run"
    assert by_stage["timeline_v2"]["disposition"] == "will_run"
    assert by_stage["chapter_plan_v2"]["disposition"] == "unmeasurable"
    assert by_stage["chapter_plan_v2"]["input_budget"]["status"] == "unknown"
    assert by_stage["chapter_frames"]["disposition"] == "will_run"
    assert by_stage["document_content_v2"]["disposition"] == "unmeasurable"
    assert by_stage["document_content_v2"]["input_budget"]["status"] == "unknown"
    assert by_stage["export_v2"]["disposition"] == "will_run"
    assert by_stage["completed"]["disposition"] == "will_run"
    # budget条目恰8键（B1修正形态）；全新任务零供应商请求事实不入。
    assert set(by_stage["chapter_plan_v2"]["input_budget"]) == {
        "policy_version",
        "limit_chars",
        "limit_bytes",
        "measured_chars",
        "measured_bytes",
        "status",
        "planned_business_calls",
        "supplier_request_limit",
    }
    assert payload["is_new_task"] is True
    assert payload["dry_run"] is True
    assert payload["source_video"]["duration_ms"] == 10000
    assert payload["source_video"]["has_audio"] is True
    assert payload["model_registry"] == "models.json"  # basename脱敏
    assert payload["media_tools"] == {"ffmpeg": "ffmpeg.exe", "ffprobe": "ffprobe.exe"}
    assert hashlib.sha256(source.read_bytes()).hexdigest() == payload["source_video"]["sha256"]
    # dry-run零写入（E02）；进度行仅preflight两行。
    assert _tree_snapshot(tmp_path) == before
    assert not (output_root / TASK_ID).exists()
    assert events[:2] == [
        ("preflight", "started", None),
        ("preflight", "completed", None),
    ]


def test_run_task_m2_dry_run_ocr_off_disabled_never_network(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    source = _make_source(tmp_path)
    _stub_tools(tmp_path, monkeypatch)
    _stub_probe(monkeypatch)
    before = _tree_snapshot(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("dry-run不得构造transport或联网")

    monkeypatch.setattr(m2_pipeline, "build_content_composition_dry_run", _boom)
    monkeypatch.setattr(m2_pipeline, "measure_boundary_egress", _boom)
    plan = run_task_m2(
        output_root,
        TASK_ID,
        source_video=source,
        ocr_mode="off",
        allow_llm=True,  # 即使批准，dry-run也永不联网（§三.3字面）
        dry_run=True,
    )
    by_stage = {e.stage: e for e in plan.stages}
    assert by_stage["ocr"].disposition == "disabled"
    assert by_stage["ocr"].input_budget is None
    # ocr=off不阻断timeline（§三.9消费的是状态）；全新任务其余呈现不变。
    assert by_stage["timeline_v2"].disposition == "will_run"
    assert _tree_snapshot(tmp_path) == before


def test_run_task_m2_preflight_failure_has_no_completed_line(tmp_path, monkeypatch):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    _stub_tools(tmp_path, monkeypatch)
    events: list[tuple[str, str]] = []
    with pytest.raises(M2PipelineError) as caught:
        run_task_m2(
            output_root,
            TASK_ID,
            progress=lambda s, t, p=None: events.append((s, t)),
        )
    assert caught.value.error_code == "SOURCE_VIDEO_REQUIRED"
    assert events == [("preflight", "started")]
