import hashlib
import json
from pathlib import Path
from typing import Any

from video_doc.formal_chaptering import (
    FORMAL_POLICY_VERSION,
    plan_task_chapters_formal,
)
from video_doc.llm_adapter import LLMConfig
from video_doc.llm_strategy import validate_chapter_plan
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
    (workspace.root / "timeline" / "timeline.json").write_text(
        json.dumps(_timeline(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_root


def _mock_config() -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model="mock-stage8",
        timeout_seconds=60.0,
        api_key="",
        mock_response={"unused": True},
        enable_thinking=None,
    )


class SequenceTransport:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[Any] = []

    def complete(self, config: object, messages: object, response_format: object) -> Any:
        self.calls.append((config, messages, response_format))
        if not self.outcomes:
            raise AssertionError("缓存命中时不应访问transport")
        return self.outcomes.pop(0)


def _primary_response() -> dict[str, Any]:
    return {
        "boundaries": [
            {"source_event_id": "a0001", "title": "开场"},
            {"source_event_id": "a0002", "title": "第二部分"},
            {"source_event_id": "a0003", "title": "第三部分"},
        ]
    }


def _outline_response() -> dict[str, Any]:
    return {
        "chapters": [
            {"title": "开场", "rough_timestamp": 0.0},
            {"title": "第二部分", "rough_timestamp": 60.0},
            {"title": "第三部分", "rough_timestamp": 120.0},
        ]
    }


def _alignment_response() -> dict[str, Any]:
    return {
        "boundaries": [
            {
                "transition_id": "t0001",
                "title": "第二部分",
                "source_event_id": "a0002",
                "previous_topic": "开场",
                "new_topic": "第二部分",
            },
            {
                "transition_id": "t0002",
                "title": "第三部分",
                "source_event_id": "a0003",
                "previous_topic": "第二部分",
                "new_topic": "第三部分",
            },
        ]
    }


def test_formal_primary_publishes_traceable_atomic_group(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport(_primary_response())

    result = plan_task_chapters_formal(
        output_root, "DEV01", config=_mock_config(), transport=transport
    )

    assert result.result_source == "primary"
    assert result.quality_level == "normal"
    assert result.business_call_count == 1
    assert result.supplier_request_count == 1
    assert len(transport.calls) == 1
    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    validate_chapter_plan(plan, timeline=_timeline(), expected_task_id="DEV01")
    assert plan["parameters"]["formal_policy_version"] == FORMAL_POLICY_VERSION
    assert plan["parameters"]["formal_result_source"] == "primary"
    assert "experiment_id" not in plan["parameters"]
    report = json.loads(result.execution_report_path.read_text(encoding="utf-8"))
    assert report["result_source"] == "primary"
    assert report["final_plan_sha256"]
    manifest = json.loads(
        (result.result_group_path / "manifest.json").read_text(encoding="utf-8")
    )
    for name, expected_hash in manifest["files"].items():
        assert hashlib.sha256(
            (result.result_group_path / name).read_bytes()
        ).hexdigest() == expected_hash
    assert (
        output_root
        / "DEV01"
        / "boundaries"
        / "attempts"
        / "primary"
        / "attempt-report.json"
    ).is_file()


def test_formal_uses_b_only_after_primary_structure_failure(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    invalid = {"boundaries": [{"source_event_id": "missing", "title": "错误"}]}
    transport = SequenceTransport(
        invalid,
        invalid,
        _outline_response(),
        _alignment_response(),
    )

    result = plan_task_chapters_formal(
        output_root, "DEV01", config=_mock_config(), transport=transport
    )

    assert result.result_source == "fallback"
    assert result.quality_level == "unknown"
    assert result.business_call_count == 3
    assert result.supplier_request_count == 4
    assert len(transport.calls) == 4
    report = json.loads(result.execution_report_path.read_text(encoding="utf-8"))
    assert report["errors"][0]["path"] == "primary"
    assert (
        output_root
        / "DEV01"
        / "boundaries"
        / "attempts"
        / "fallback"
        / "attempt-report.json"
    ).is_file()


def test_formal_generates_zero_api_emergency_after_both_fail(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    invalid_primary = {
        "boundaries": [{"source_event_id": "missing", "title": "错误"}]
    }
    invalid_outline = {"chapters": []}
    transport = SequenceTransport(
        invalid_primary,
        invalid_primary,
        invalid_outline,
        invalid_outline,
    )

    result = plan_task_chapters_formal(
        output_root, "DEV01", config=_mock_config(), transport=transport
    )

    assert result.result_source == "emergency"
    assert result.quality_level == "degraded"
    assert result.business_call_count == 3
    assert result.supplier_request_count == 4
    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    assert plan["boundaries"] == [
        {"timestamp": 0.0, "title": "完整内容", "source_event_id": None}
    ]
    assert (
        output_root
        / "DEV01"
        / "boundaries"
        / "attempts"
        / "emergency"
        / "attempt-report.json"
    ).is_file()


def test_formal_primary_cache_does_not_call_supplier_again(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = plan_task_chapters_formal(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(_primary_response()),
    )
    group_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.result_group_path.iterdir()
        if path.is_file()
    }
    top_report_before = first.execution_report_path.read_bytes()
    top_report_mtime_before = first.execution_report_path.stat().st_mtime_ns

    cached = plan_task_chapters_formal(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(),
    )

    assert cached.result_source == "primary"
    assert cached.business_call_count == 0
    assert cached.supplier_request_count == 0
    assert cached.plan_cache_hit is True
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in cached.result_group_path.iterdir()
        if path.is_file()
    } == group_hashes
    assert cached.execution_report_path.read_bytes() == top_report_before
    assert cached.execution_report_path.stat().st_mtime_ns == top_report_mtime_before
    assert cached.execution_report_path.read_bytes() == (
        cached.result_group_path / "execution-report.json"
    ).read_bytes()
