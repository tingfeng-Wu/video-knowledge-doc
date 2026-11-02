import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import video_doc.cli as cli
from video_doc.chapter_review import (
    REVIEW_EXPERIMENT_VERSION,
    ChapterReviewError,
    ChapterReviewResult,
    build_review_messages,
    normalize_review_response,
    review_task_chapters,
)
from video_doc.llm_adapter import LLMConfig
from video_doc.task_workspace import initialize_task_workspace


def _timeline() -> dict[str, Any]:
    events = []
    for index in range(7):
        start = index * 70.0
        events.append(
            {
                "id": f"a{index + 1:04d}",
                "type": "asr",
                "source_segment_id": index + 1,
                "start": start,
                "end": start + 20.0,
                "pause_before_seconds": 0.0 if index == 0 else 50.0,
                "text": f"第{index + 1}段课程原文",
            }
        )
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "duration_seconds": 490.0,
        "language": {"code": "zh", "probability": 0.99},
        "source_asr": {
            "schema_version": "1.0",
            "cache_key": "a" * 64,
            "sha256": "b" * 64,
        },
        "events": events,
    }


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_plan(timeline: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "strategy": "llm",
        "source_timeline": {
            "schema_version": "1.0",
            "sha256": _canonical_hash(timeline),
            "event_count": 7,
        },
        "duration_seconds": 490.0,
        "parameters": {
            "prompt_version": "chapter-boundary-event-id-stability-2.0",
            "min_chapter_seconds": 60.0,
            "boundary_alignment": "selected_asr_event_start",
            "experiment_id": "run-01",
            "experiment_version": "event-id-v2",
            "chapter_policy_version": "concise-top-level-chapters-1.0",
        },
        "boundaries": [
            {"timestamp": 0.0, "title": "导论", "source_event_id": None},
            {"timestamp": 140.0, "title": "核心概念", "source_event_id": "a0003"},
            {"timestamp": 280.0, "title": "应用", "source_event_id": "a0005"},
        ],
    }


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake")
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01", source_video=source)
    timeline = _timeline()
    timeline_path = workspace.root / "timeline" / "timeline.json"
    timeline_path.write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    plan_path = (
        workspace.root
        / "experiments"
        / "stage8"
        / "direct"
        / "event-id-v2"
        / "run-01"
        / "chapter-plan.json"
    )
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        json.dumps(_source_plan(timeline), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_root, plan_path


def _config(*, response: dict[str, Any] | None = None) -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model="mock-review",
        timeout_seconds=60.0,
        api_key="sk-test-secret",
        mock_response=response or {"unused": True},
        enable_thinking=None,
        temperature=0.0,
    )


class SequenceTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[Any] = []

    def complete(self, config: Any, messages: Any, response_format: Any) -> Any:
        self.calls.append((config, messages, response_format))
        if not self.responses:
            raise AssertionError("缓存命中时不应调用transport")
        return self.responses.pop(0)


def _accept() -> dict[str, Any]:
    return {"verdict": "accept", "issues": [], "revised_boundaries": None}


def _issue(
    operation: str,
    original: list[str],
    proposed: list[str],
    *,
    chapter_indexes: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "chapter_indexes": chapter_indexes or [2],
        "original_event_ids": original,
        "proposed_event_ids": proposed,
        "evidence_event_ids": sorted(set(original + proposed)),
        "reason": "ASR显示一级主题在建议事件处开始",
    }


def _revise(operation: str) -> dict[str, Any]:
    if operation == "move":
        boundaries = [
            {"source_event_id": "a0001", "title": "导论"},
            {"source_event_id": "a0004", "title": "核心概念"},
            {"source_event_id": "a0005", "title": "应用"},
        ]
        issue = _issue("move", ["a0003"], ["a0004"])
    elif operation == "merge":
        boundaries = [
            {"source_event_id": "a0001", "title": "导论与核心概念"},
            {"source_event_id": "a0005", "title": "应用"},
        ]
        issue = _issue("merge", ["a0001", "a0003"], ["a0001"], chapter_indexes=[1, 2])
    elif operation == "split":
        boundaries = [
            {"source_event_id": "a0001", "title": "导论"},
            {"source_event_id": "a0003", "title": "核心概念"},
            {"source_event_id": "a0004", "title": "补充机制"},
            {"source_event_id": "a0005", "title": "应用"},
        ]
        issue = _issue("split", ["a0003"], ["a0003", "a0004"], chapter_indexes=[2, 3])
    else:
        boundaries = [
            {"source_event_id": "a0001", "title": "课程导论"},
            {"source_event_id": "a0003", "title": "核心概念"},
            {"source_event_id": "a0005", "title": "应用"},
        ]
        issue = _issue("rename", ["a0001"], ["a0001"], chapter_indexes=[1])
    return {"verdict": "revise", "issues": [issue], "revised_boundaries": boundaries}


def test_review_prompt_is_evidence_only_and_explains_title_direction() -> None:
    messages = build_review_messages(_timeline(), _source_plan(_timeline()))
    prompt = messages[1]["content"]

    assert all(f"a{index:04d}" in prompt for index in range(1, 8))
    assert "下方内容" in prompt
    assert "方案B" not in prompt
    assert ".mp4" not in prompt and "\\" not in prompt
    assert "06:34" not in prompt and "已知问题" not in prompt


def test_accept_creates_independent_reviewed_plan(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    source_before = source_path.read_bytes()
    result = review_task_chapters(
        output_root,
        "DEV01",
        source_run_id="run-01",
        config=_config(),
        transport=SequenceTransport(_accept()),
    )

    assert result.status == "completed" and result.verdict == "accept"
    assert result.reviewed_plan_path is not None
    reviewed = json.loads(result.reviewed_plan_path.read_text(encoding="utf-8"))
    assert reviewed["boundaries"] == json.loads(source_before)["boundaries"]
    assert reviewed["parameters"]["review_source_plan_sha256"] == hashlib.sha256(source_before).hexdigest()
    assert source_path.read_bytes() == source_before


@pytest.mark.parametrize("operation", ["move", "merge", "split", "rename"])
def test_valid_minimal_operations(operation: str) -> None:
    normalized, plan = normalize_review_response(
        _revise(operation), _timeline(), _source_plan(_timeline())
    )
    assert normalized["issues"][0]["operation"] == operation
    assert plan["boundaries"]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value["revised_boundaries"][1].update(source_event_id="missing"), "不存在"),
        (lambda value: value["revised_boundaries"][2].update(source_event_id="a0004"), "重复"),
        (lambda value: value["revised_boundaries"][1].update(source_event_id="a0006"), "倒序|之前"),
        (lambda value: value["revised_boundaries"][0].update(source_event_id="a0002"), "首个ASR事件"),
        (lambda value: value["revised_boundaries"][1].update(title=" "), "标题不能为空"),
        (lambda value: value["revised_boundaries"][1].update(source_event_id="a0007"), "小于最小值|倒序|之前"),
    ],
)
def test_invalid_revised_boundaries_are_rejected(mutator: Any, message: str) -> None:
    response = _revise("move")
    mutator(response)
    with pytest.raises(ChapterReviewError, match=message):
        normalize_review_response(response, _timeline(), _source_plan(_timeline()))


def test_minimum_duration_including_last_chapter_is_enforced() -> None:
    source = _source_plan(_timeline())
    source["parameters"]["min_chapter_seconds"] = 80.0
    with pytest.raises(ChapterReviewError, match="小于最小值"):
        normalize_review_response(_revise("move"), _timeline(), source)


def test_issue_must_trace_changes() -> None:
    response = _revise("move")
    response["issues"][0]["proposed_event_ids"] = ["a0005"]
    with pytest.raises(ChapterReviewError, match="未覆盖|不一致"):
        normalize_review_response(response, _timeline(), _source_plan(_timeline()))


def test_issue_chapter_indexes_must_trace_renamed_title() -> None:
    response = _revise("rename")
    response["issues"][0]["chapter_indexes"] = [2]
    with pytest.raises(ChapterReviewError, match="标题变化"):
        normalize_review_response(response, _timeline(), _source_plan(_timeline()))


def test_source_timeline_hash_mismatch_stops_before_call(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    payload["source_timeline"]["sha256"] = "0" * 64
    source_path.write_text(json.dumps(payload), encoding="utf-8")
    transport = SequenceTransport(_accept())

    with pytest.raises(ChapterReviewError, match="时间轴|来源A计划校验失败"):
        review_task_chapters(
            output_root,
            "DEV01",
            source_run_id="run-01",
            config=_config(),
            transport=transport,
        )
    assert transport.calls == []


def test_invalid_response_falls_back_without_overwriting_source(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    source_before = source_path.read_bytes()
    response = _revise("move")
    response["revised_boundaries"][1]["source_event_id"] = "missing"
    result = review_task_chapters(
        output_root,
        "DEV01",
        source_run_id="run-01",
        config=_config(),
        transport=SequenceTransport(response),
    )

    assert result.status == "failed" and result.fallback_used is True
    assert result.selected_plan_path == source_path
    assert result.reviewed_plan_path is None
    assert source_path.read_bytes() == source_before
    assert not (result.audit_path.parent / "chapter-plan.reviewed.json").exists()
    assert json.loads(result.audit_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_cache_same_run_does_not_rewrite_and_runs_are_isolated(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    first = review_task_chapters(
        output_root, "DEV01", source_run_id="run-01", config=_config(), transport=SequenceTransport(_accept())
    )
    assert first.reviewed_plan_path is not None
    plan_mtime = first.reviewed_plan_path.stat().st_mtime_ns
    audit_mtime = first.audit_path.stat().st_mtime_ns
    cached = review_task_chapters(
        output_root, "DEV01", source_run_id="run-01", config=_config(), transport=SequenceTransport()
    )
    assert cached.llm_cache_hit and cached.plan_cache_hit
    assert cached.reviewed_plan_path is not None
    assert cached.reviewed_plan_path.stat().st_mtime_ns == plan_mtime
    assert cached.audit_path.stat().st_mtime_ns == audit_mtime

    run2_path = Path(str(source_path).replace("run-01", "run-02"))
    run2_path.parent.mkdir(parents=True)
    run2_plan = json.loads(source_path.read_text(encoding="utf-8"))
    run2_plan["parameters"]["experiment_id"] = "run-02"
    run2_path.write_text(json.dumps(run2_plan, ensure_ascii=False), encoding="utf-8")
    second = review_task_chapters(
        output_root, "DEV01", source_run_id="run-02", config=_config(), transport=SequenceTransport(_accept())
    )
    assert second.audit_path.parent.name == "run-02"
    assert second.llm_cache_hit is False


def test_review_artifacts_exclude_key_and_media_references(tmp_path: Path) -> None:
    output_root, _ = _prepare(tmp_path)
    result = review_task_chapters(
        output_root, "DEV01", source_run_id="run-01", config=_config(), transport=SequenceTransport(_accept())
    )
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in result.audit_path.parent.glob("*.json")
    )
    assert "sk-test-secret" not in combined
    assert ".mp4" not in combined and ".wav" not in combined


def test_formal_and_source_artifacts_remain_unchanged(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    formal_a = output_root / "DEV01" / "boundaries" / "llm.json"
    formal_b = output_root / "DEV01" / "boundaries" / "llm-two-stage.json"
    formal_a.parent.mkdir(parents=True, exist_ok=True)
    formal_a.write_bytes(b"formal-a")
    formal_b.write_bytes(b"formal-b")
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source_path, formal_a, formal_b)}
    review_task_chapters(
        output_root, "DEV01", source_run_id="run-01", config=_config(), transport=SequenceTransport(_revise("move"))
    )
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}


def test_review_cli_prints_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    reviewed = tmp_path / "reviewed.json"
    audit = tmp_path / "audit.json"
    monkeypatch.setattr(
        cli,
        "review_task_chapters",
        lambda *args, **kwargs: ChapterReviewResult(
            status="completed",
            selected_plan_path=reviewed,
            reviewed_plan_path=reviewed,
            audit_path=audit,
            verdict="accept",
            issue_count=0,
            attempt_count=1,
            llm_cache_hit=False,
            plan_cache_hit=False,
            fallback_used=False,
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["video-doc", "review-chapters", "DEV01", "--source-run-id", "run-01"],
    )
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"verdict": "accept"' in output and "审阅完成" in output
