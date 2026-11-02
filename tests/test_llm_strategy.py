import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import video_doc.cli as cli
from video_doc.formal_chaptering import FormalChapterResult
from video_doc.llm_adapter import LLMConfig
from video_doc.llm_strategy import (
    CHAPTER_PROMPT_VERSION,
    STABILITY_DIRECT_EVENT_ID_PROMPT_VERSION,
    STABILITY_DIRECT_EXPERIMENT_VERSION,
    STABILITY_DIRECT_PROMPT_VERSION,
    ChapterPlanError,
    ChapterPlanResult,
    build_chapter_messages,
    normalize_chapter_response,
    normalize_event_id_chapter_response,
    plan_task_chapters,
    validate_chapter_plan,
)
from video_doc.task_workspace import initialize_task_workspace


def _timeline() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "duration_seconds": 180.0,
        "language": {"code": "zh", "probability": 0.99},
        "source_asr": {
            "schema_version": "1.0",
            "cache_key": "a" * 64,
            "sha256": "b" * 64,
        },
        "events": [
            {
                "id": "a0001",
                "type": "asr",
                "source_segment_id": 1,
                "start": 5.0,
                "end": 15.0,
                "pause_before_seconds": 5.0,
                "text": "课程介绍",
            },
            {
                "id": "a0002",
                "type": "asr",
                "source_segment_id": 2,
                "start": 60.0,
                "end": 70.0,
                "pause_before_seconds": 45.0,
                "text": "第二部分",
            },
            {
                "id": "a0003",
                "type": "asr",
                "source_segment_id": 3,
                "start": 120.0,
                "end": 130.0,
                "pause_before_seconds": 50.0,
                "text": "第三部分",
            },
            {
                "id": "a0004",
                "type": "asr",
                "source_segment_id": 4,
                "start": 160.0,
                "end": 170.0,
                "pause_before_seconds": 30.0,
                "text": "结束语",
            },
        ],
    }


def _prepare_task(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01", source_video=source)
    timeline_path = workspace.root / "timeline" / "timeline.json"
    timeline_path.write_text(
        json.dumps(_timeline(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_root


def _mock_config() -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model="",
        timeout_seconds=60.0,
        api_key="",
        mock_response={"unused": True},
        enable_thinking=None,
    )


def _valid_response() -> dict[str, Any]:
    return {
        "boundaries": [
            {"timestamp": 0.0, "title": "开场"},
            {"timestamp": 58.4, "title": "核心概念"},
            {"timestamp": 122.0, "title": "总结"},
        ]
    }


def _valid_event_id_response() -> dict[str, Any]:
    return {
        "boundaries": [
            {"source_event_id": "a0001", "title": "开场"},
            {"source_event_id": "a0002", "title": "核心概念"},
            {"source_event_id": "a0003", "title": "总结"},
        ]
    }


class SequenceTransport:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[object] = []

    def complete(self, config: object, messages: object, response_format: object) -> Any:
        self.calls.append((config, messages, response_format))
        if not self.outcomes:
            raise AssertionError("缓存命中时不应访问transport")
        return self.outcomes.pop(0)


def test_normalize_chapter_response_snaps_to_nearest_asr_start() -> None:
    normalized = normalize_chapter_response(_valid_response(), _timeline())

    assert normalized == {
        "boundaries": [
            {"timestamp": 0.0, "title": "开场", "source_event_id": None},
            {"timestamp": 60.0, "title": "核心概念", "source_event_id": "a0002"},
            {"timestamp": 120.0, "title": "总结", "source_event_id": "a0003"},
        ]
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({}, "boundaries"),
        ({"boundaries": [{"timestamp": 5.0, "title": "开场"}]}, "0.0秒"),
        (
            {
                "boundaries": [
                    {"timestamp": 0.0, "title": "开场"},
                    {"timestamp": 80.0, "title": "后段"},
                    {"timestamp": 70.0, "title": "倒序"},
                ]
            },
            "严格递增",
        ),
        (
            {
                "boundaries": [
                    {"timestamp": 0.0, "title": "开场"},
                    {"timestamp": 60.0, "title": " "},
                ]
            },
            "标题不能为空",
        ),
        (
            {
                "boundaries": [
                    {"timestamp": 0.0, "title": "开场"},
                    {"timestamp": 170.0, "title": "过短末章"},
                ]
            },
            "小于最小值",
        ),
    ],
)
def test_normalize_chapter_response_rejects_invalid_business_schema(
    response: dict[str, Any], message: str
) -> None:
    with pytest.raises(ChapterPlanError, match=message):
        normalize_chapter_response(response, _timeline())


def test_prompt_contains_every_event_and_only_requests_boundaries() -> None:
    messages = build_chapter_messages(_timeline(), min_chapter_seconds=30.0)
    prompt = messages[1]["content"]

    assert all(
        event_id in prompt for event_id in ("a0001", "a0002", "a0003", "a0004")
    )
    assert CHAPTER_PROMPT_VERSION in prompt
    assert "十进制秒" in prompt
    assert "不得使用HH:MM:SS" in prompt
    assert "boundaries只表示章节起点" in prompt
    assert "不得输出等于视频总时长的结束边界" in prompt
    assert "最后一章也至少30.000秒" in prompt
    assert "只输出" in messages[0]["content"]
    assert "章节摘要" in messages[0]["content"]


def test_stability_prompt_uses_shared_concise_policy() -> None:
    messages = build_chapter_messages(
        _timeline(),
        min_chapter_seconds=60.0,
        stability_experiment=True,
    )
    prompt = messages[1]["content"]

    assert STABILITY_DIRECT_EVENT_ID_PROMPT_VERSION in prompt
    assert "共用简洁分章政策版本" in prompt
    assert "只划分贯穿一段内容的一级主题" in prompt
    assert "无法确定是否拆分时优先少分" in prompt
    assert "3—5章" in prompt
    assert "source_event_id" in prompt
    assert "不得自行估算或输出timestamp" in prompt
    assert "不得为了修复顺序而交换、排序或移动标题" in prompt


def test_event_id_response_maps_ids_without_sorting_or_merging() -> None:
    normalized = normalize_event_id_chapter_response(
        _valid_event_id_response(), _timeline()
    )

    assert normalized == {
        "boundaries": [
            {"timestamp": 0.0, "title": "开场", "source_event_id": None},
            {"timestamp": 60.0, "title": "核心概念", "source_event_id": "a0002"},
            {"timestamp": 120.0, "title": "总结", "source_event_id": "a0003"},
        ]
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {"boundaries": [{"source_event_id": "missing", "title": "开场"}]},
            "不存在",
        ),
        (
            {
                "boundaries": [
                    {"source_event_id": "a0001", "title": "开场"},
                    {"source_event_id": "a0002", "title": "概念"},
                    {"source_event_id": "a0002", "title": "案例"},
                ]
            },
            "重复选择.*案例.*概念|案例.*概念.*重复选择",
        ),
        (
            {
                "boundaries": [
                    {"source_event_id": "a0001", "title": "开场"},
                    {"source_event_id": "a0003", "title": "后段"},
                    {"source_event_id": "a0002", "title": "倒序"},
                ]
            },
            "倒序.*a0002.*后段.*a0003.*之前",
        ),
        (
            {
                "boundaries": [
                    {"source_event_id": "a0001", "title": "开场"},
                    {"source_event_id": "a0004", "title": "过短末章"},
                ]
            },
            "过短末章.*a0004.*小于最小值",
        ),
    ],
)
def test_event_id_response_rejects_invalid_ids_order_and_duration(
    response: dict[str, Any], message: str
) -> None:
    with pytest.raises(ChapterPlanError, match=message):
        normalize_event_id_chapter_response(response, _timeline())


def test_direct_experiment_isolates_runs_and_preserves_formal_plan(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    formal_path = output_root / "DEV01" / "boundaries" / "llm.json"
    formal_path.write_text("official-v1.1\n", encoding="utf-8")
    first_transport = SequenceTransport(_valid_event_id_response())

    first = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=first_transport,
        experiment_id="run-01",
    )
    first_hash = hashlib.sha256(first.plan_path.read_bytes()).hexdigest()
    first_mtime = first.plan_path.stat().st_mtime_ns
    cached = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(),
        experiment_id="run-01",
    )
    second = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(_valid_event_id_response()),
        experiment_id="run-02",
    )

    expected_first = (
        output_root
        / "DEV01"
        / "experiments"
        / "stage8"
        / "direct"
        / STABILITY_DIRECT_EXPERIMENT_VERSION
        / "run-01"
        / "chapter-plan.json"
    )
    assert first.plan_path == expected_first
    assert second.plan_path.parent.name == "run-02"
    assert formal_path.read_text(encoding="utf-8") == "official-v1.1\n"
    assert first.min_chapter_seconds == 60.0
    assert cached.llm_cache_hit is True
    assert cached.plan_cache_hit is True
    assert hashlib.sha256(cached.plan_path.read_bytes()).hexdigest() == first_hash
    assert cached.plan_path.stat().st_mtime_ns == first_mtime
    plan = json.loads(first.plan_path.read_text(encoding="utf-8"))
    assert plan["parameters"]["prompt_version"] == (
        STABILITY_DIRECT_EVENT_ID_PROMPT_VERSION
    )
    assert plan["parameters"]["experiment_id"] == "run-01"
    assert plan["parameters"]["experiment_version"] == (
        STABILITY_DIRECT_EXPERIMENT_VERSION
    )
    assert plan["parameters"]["boundary_alignment"] == "selected_asr_event_start"
    assert plan["parameters"]["chapter_policy_version"] == (
        "concise-top-level-chapters-1.0"
    )
    raw_path = first.plan_path.parent / "stage8-llm-chapters.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw["request"]["cache_namespace"] == "event-id-v2-run-01"
    assert raw["request"]["validation_id"] == "chapter-plan-event-id-v3-raw"
    assert raw["raw_response"] == _valid_event_id_response()
    assert "共用简洁分章政策版本" in first_transport.calls[0][1][1]["content"]


def test_direct_experiment_failure_preserves_formal_and_previous_run(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    formal_path = output_root / "DEV01" / "boundaries" / "llm.json"
    formal_path.write_text("official-v1.1\n", encoding="utf-8")
    first = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(_valid_event_id_response()),
        experiment_id="run-01",
    )
    previous = first.plan_path.read_bytes()
    invalid = {
        "boundaries": [{"source_event_id": "a0001", "title": ""}]
    }

    with pytest.raises(ChapterPlanError, match="标题不能为空"):
        plan_task_chapters(
            output_root,
            "DEV01",
            min_chapter_seconds=61.0,
            config=_mock_config(),
            transport=SequenceTransport(invalid, invalid),
            experiment_id="run-01",
        )

    assert formal_path.read_text(encoding="utf-8") == "official-v1.1\n"
    assert first.plan_path.read_bytes() == previous
    audit_path = (
        first.plan_path.parent / "stage8-llm-chapters-validation-audit.json"
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "failed"
    assert len(audit["attempts"]) == 2
    assert all("标题不能为空" in attempt["error"] for attempt in audit["attempts"])


def test_event_id_retry_reports_exact_chapters_and_preserves_titles(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    invalid = {
        "boundaries": [
            {"source_event_id": "a0001", "title": "开场"},
            {"source_event_id": "a0002", "title": "核心概念"},
            {"source_event_id": "a0002", "title": "案例"},
        ]
    }
    transport = SequenceTransport(invalid, _valid_event_id_response())

    result = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=transport,
        experiment_id="run-01",
    )

    assert result.attempt_count == 2
    retry_messages = transport.calls[1][1]
    correction = retry_messages[-1]["content"]
    assert "第3条章节“案例”" in correction
    assert "第2条章节“核心概念”" in correction
    assert "a0002" in correction
    assert "保持标题和章节顺序不变" in correction
    audit_path = (
        result.plan_path.parent / "stage8-llm-chapters-validation-audit.json"
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "completed_after_retry"
    assert audit["attempts"][0]["raw_response"] == invalid


@pytest.mark.parametrize("experiment_id", ["../run-01", "run 01", "_run-01"])
def test_direct_experiment_rejects_unsafe_id(
    tmp_path: Path, experiment_id: str
) -> None:
    output_root = _prepare_task(tmp_path)
    with pytest.raises(ChapterPlanError, match="experiment_id"):
        plan_task_chapters(
            output_root,
            "DEV01",
            config=_mock_config(),
            experiment_id=experiment_id,
        )


def test_plan_task_chapters_retries_semantically_invalid_response(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport(
        {"boundaries": [{"timestamp": 0.0, "title": ""}]},
        _valid_response(),
    )

    result = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=transport,
    )

    assert result.attempt_count == 2
    assert result.boundary_count == 3
    assert result.llm_cache_hit is False
    assert len(transport.calls) == 2
    payload = json.loads(result.plan_path.read_text(encoding="utf-8"))
    validate_chapter_plan(payload, timeline=_timeline(), expected_task_id="DEV01")
    assert payload["parameters"]["prompt_version"] == CHAPTER_PROMPT_VERSION
    llm_payload = json.loads(
        (output_root / "DEV01" / "llm" / "stage8-llm-chapters.json").read_text(
            encoding="utf-8"
        )
    )
    assert llm_payload["raw_response"] == _valid_response()
    assert llm_payload["raw_response"]["boundaries"][1]["timestamp"] == 58.4
    assert llm_payload["response"]["boundaries"][1]["timestamp"] == 60.0
    assert llm_payload["request"]["validation_id"] == "chapter-plan-v2-raw"
    events = [
        json.loads(line)["event"]
        for line in (output_root / "DEV01" / "logs" / "llm_calls.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events == ["started", "retrying", "completed"]


def test_plan_task_chapters_uses_both_caches_without_rewriting(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    first_transport = SequenceTransport(_valid_response())
    first = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=first_transport,
    )
    original_hash = hashlib.sha256(first.plan_path.read_bytes()).hexdigest()
    original_mtime = first.plan_path.stat().st_mtime_ns

    second_transport = SequenceTransport()
    second = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=second_transport,
    )

    assert second.llm_cache_hit is True
    assert second.plan_cache_hit is True
    assert second_transport.calls == []
    assert hashlib.sha256(second.plan_path.read_bytes()).hexdigest() == original_hash
    assert second.plan_path.stat().st_mtime_ns == original_mtime


def test_two_invalid_responses_fail_and_preserve_existing_plan(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(_valid_response()),
    )
    original = first.plan_path.read_bytes()
    llm_path = output_root / "DEV01" / "llm" / "stage8-llm-chapters.json"
    original_llm = llm_path.read_bytes()
    invalid = {"boundaries": [{"timestamp": 0.0, "title": ""}]}

    with pytest.raises(ChapterPlanError, match="标题不能为空"):
        plan_task_chapters(
            output_root,
            "DEV01",
            min_chapter_seconds=31.0,
            config=_mock_config(),
            transport=SequenceTransport(invalid, invalid),
        )

    assert first.plan_path.read_bytes() == original
    assert llm_path.read_bytes() == original_llm
    assert not first.plan_path.with_name("llm.json.tmp").exists()


def test_damaged_raw_response_invalidates_llm_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(_valid_response()),
    )
    llm_path = output_root / "DEV01" / "llm" / "stage8-llm-chapters.json"
    payload = json.loads(llm_path.read_text(encoding="utf-8"))
    payload["raw_response"] = {
        "boundaries": [{"timestamp": 0.0, "title": ""}]
    }
    llm_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    transport = SequenceTransport(_valid_response())

    second = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=transport,
    )

    assert first.plan_path == second.plan_path
    assert second.llm_cache_hit is False
    assert len(transport.calls) == 1
    repaired = json.loads(llm_path.read_text(encoding="utf-8"))
    assert repaired["raw_response"] == _valid_response()


def test_damaged_plan_is_rebuilt_from_valid_llm_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(_valid_response()),
    )
    first.plan_path.write_text("damaged", encoding="utf-8")

    second = plan_task_chapters(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(),
    )

    assert second.llm_cache_hit is True
    assert second.plan_cache_hit is False
    payload = json.loads(second.plan_path.read_text(encoding="utf-8"))
    validate_chapter_plan(payload, timeline=_timeline())


def test_plan_chapters_cli_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "outputs" / "DEV01" / "boundaries" / "llm.json"

    def fake_plan(*args: object, **kwargs: object) -> FormalChapterResult:
        return FormalChapterResult(
            plan_path=plan_path,
            execution_report_path=plan_path.with_name("llm-execution-report.json"),
            result_group_path=plan_path.parent / "formal-results" / "primary-test",
            boundary_count=3,
            duration_seconds=180.0,
            min_chapter_seconds=30.0,
            status="success",
            result_source="primary",
            quality_level="normal",
            business_call_count=1,
            supplier_request_count=1,
            plan_cache_hit=False,
        )

    monkeypatch.setattr(cli, "plan_task_chapters_formal", fake_plan)
    monkeypatch.setattr(
        "sys.argv",
        [
            "video-doc",
            "plan-chapters",
            "DEV01",
            "--output-root",
            str(tmp_path / "outputs"),
        ],
    )

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"strategy": "llm"' in output
    assert '"boundary_count": 3' in output
    assert "LLM章节计划生成完成" in output
