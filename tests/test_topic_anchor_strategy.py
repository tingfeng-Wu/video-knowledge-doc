import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import video_doc.cli as cli
from video_doc.llm_adapter import LLMConfig, LLMRetryableError
from video_doc.stage8_reference_diagnostics import diagnose_stage8_prediction
from video_doc.task_workspace import initialize_task_workspace
from video_doc.topic_anchor_strategy import (
    CORE_FILES,
    STRATEGY_VERSION,
    TopicAnchorError,
    TopicAnchorResult,
    build_anchor_alignment_messages,
    build_anchor_chapter_plan,
    build_anchor_intervals,
    build_topic_anchor_messages,
    normalize_anchor_alignment_response,
    normalize_topic_anchor_response,
    plan_task_chapters_anchor_v2,
)


def _timeline(*, duration: float = 320.0) -> dict[str, Any]:
    events = []
    for number in range(1, 13):
        start = float((number - 1) * 25)
        events.append(
            {
                "id": f"a{number:04d}",
                "type": "asr",
                "source_segment_id": number,
                "start": start,
                "end": start + 10.0,
                "pause_before_seconds": 0.0 if number == 1 else 15.0,
                "text": f"event {number}",
            }
        )
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "duration_seconds": duration,
        "language": {"code": "zh", "probability": 0.99},
        "source_asr": {
            "schema_version": "1.0",
            "cache_key": "a" * 64,
            "sha256": "b" * 64,
        },
        "events": events,
    }


def _outline() -> dict[str, Any]:
    return {
        "topics": [
            {
                "topic_id": "t0001",
                "title": "Topic one",
                "scope": "First major subject",
                "anchor_event_id": "a0003",
                "anchor_reason": "The first subject is already being developed",
            },
            {
                "topic_id": "t0002",
                "title": "Topic two",
                "scope": "Second major subject",
                "anchor_event_id": "a0007",
                "anchor_reason": "The second subject is already being developed",
            },
            {
                "topic_id": "t0003",
                "title": "Topic three",
                "scope": "Third major subject",
                "anchor_event_id": "a0011",
                "anchor_reason": "The third subject is already being developed",
            },
        ]
    }


def _alignment(*, first: str = "boundary", second: str = "boundary") -> dict[str, Any]:
    def item(number: int, decision: str, source: str) -> dict[str, Any]:
        base = {
            "transition_id": f"t{number:04d}_to_t{number + 1:04d}",
            "decision": decision,
            "source_event_id": None,
            "previous_evidence_ids": [],
            "new_evidence_ids": [],
            "invalid_anchor_topic_ids": [],
            "reason": "Evidence supports this decision",
        }
        if decision == "boundary":
            base.update(
                source_event_id=source,
                previous_evidence_ids=[f"a{int(source[1:]) - 1:04d}"],
                new_evidence_ids=[source, f"a{int(source[1:]) + 1:04d}"],
            )
        elif decision == "merge":
            base.update(
                previous_evidence_ids=[f"a{int(source[1:]) - 1:04d}"],
                new_evidence_ids=[source],
            )
        elif decision == "invalid_anchor":
            base.update(
                previous_evidence_ids=[source],
                invalid_anchor_topic_ids=[f"t{number + 1:04d}"],
            )
        return base

    return {"transitions": [item(1, first, "a0005"), item(2, second, "a0009")]}


def _prepare_task(tmp_path: Path, *, duration: float = 320.0) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not-sent")
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01", source_video=source)
    (workspace.root / "timeline" / "timeline.json").write_text(
        json.dumps(_timeline(duration=duration), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_root


def _config(*, model: str = "mock-anchor", secret: str = "") -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model=model,
        timeout_seconds=30.0,
        api_key=secret,
        mock_response={"unused": True},
        enable_thinking=False,
        temperature=0.0,
    )


class SequenceTransport:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[Any, Any, Any]] = []

    def complete(self, config: Any, messages: Any, response_format: Any) -> Any:
        self.calls.append((config, messages, response_format))
        if not self.outcomes:
            raise AssertionError("transport should not be called")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _normalized() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    outline = normalize_topic_anchor_response(_outline(), _timeline())
    intervals = build_anchor_intervals(outline, _timeline())
    alignment = normalize_anchor_alignment_response(
        _alignment(), outline, intervals, _timeline()
    )
    return outline, intervals, alignment


def test_prompts_keep_topic_and_boundary_responsibilities_separate() -> None:
    outline, intervals, _ = _normalized()
    topic_prompt = build_topic_anchor_messages(_timeline())
    boundary_prompt = build_anchor_alignment_messages(intervals)
    assert "锚点不是章节起点" in topic_prompt[0]["content"]
    assert "chapter-topic-anchor-2.0" in topic_prompt[1]["content"]
    assert "boundary、merge、uncertain或invalid_anchor" in boundary_prompt[0]["content"]
    assert "chapter-boundary-anchor-2.0" in boundary_prompt[1]["content"]
    assert "manual-reference" not in json.dumps(topic_prompt + boundary_prompt)
    assert outline["topics"][0]["anchor_reason"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["topics"][1].update(anchor_event_id="missing"), "不存在"),
        (lambda value: value["topics"][1].update(anchor_event_id="a0003"), "重复或逆序"),
        (lambda value: value["topics"][1].update(anchor_event_id="a0002"), "重复或逆序"),
        (lambda value: value["topics"][0].update(title=" "), "标题"),
        (lambda value: value["topics"][0].update(scope=""), "范围"),
        (lambda value: value["topics"][0].update(anchor_reason=""), "锚点理由"),
    ],
)
def test_outline_rejects_invalid_anchors_and_empty_fields(mutation: Any, message: str) -> None:
    value = _outline()
    mutation(value)
    with pytest.raises(TopicAnchorError, match=message):
        normalize_topic_anchor_response(value, _timeline())


def test_anchor_intervals_are_deterministic_and_inclusive() -> None:
    outline = normalize_topic_anchor_response(_outline(), _timeline())
    first = build_anchor_intervals(outline, _timeline())
    second = build_anchor_intervals(outline, _timeline())
    assert first == second
    assert [event["id"] for event in first[0]["events"]] == [
        "a0003", "a0004", "a0005", "a0006", "a0007"
    ]


def test_valid_boundary_has_two_sided_evidence() -> None:
    _, _, alignment = _normalized()
    assert [item["source_event_id"] for item in alignment["transitions"]] == [
        "a0005", "a0009"
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["transitions"][0].update(source_event_id="a0009"), "区间内"),
        (lambda value: value["transitions"][0].update(new_evidence_ids=["a0006"]), "必须包含"),
        (lambda value: value["transitions"][0].update(previous_evidence_ids=["a0006"]), "早于边界"),
        (lambda value: value["transitions"].pop(), "数量"),
        (lambda value: value["transitions"].reverse(), "缺失、重复或顺序"),
    ],
)
def test_alignment_rejects_boundary_and_transition_errors(mutate: Any, message: str) -> None:
    outline = normalize_topic_anchor_response(_outline(), _timeline())
    intervals = build_anchor_intervals(outline, _timeline())
    value = _alignment()
    mutate(value)
    with pytest.raises(TopicAnchorError, match=message):
        normalize_anchor_alignment_response(value, outline, intervals, _timeline())


def test_merge_and_continuous_merge_build_deterministic_titles() -> None:
    outline = normalize_topic_anchor_response(_outline(), _timeline())
    intervals = build_anchor_intervals(outline, _timeline())
    one_merge = normalize_anchor_alignment_response(
        _alignment(first="merge"), outline, intervals, _timeline()
    )
    plan = build_anchor_chapter_plan("DEV01", "run-01", _timeline(), outline, one_merge)
    assert plan["boundaries"][0]["title"] == "Topic one / Topic two（待阶段9重写）"
    both = normalize_anchor_alignment_response(
        _alignment(first="merge", second="merge"), outline, intervals, _timeline()
    )
    plan = build_anchor_chapter_plan("DEV01", "run-01", _timeline(), outline, both)
    assert len(plan["boundaries"]) == 1
    assert plan["boundaries"][0]["title"] == (
        "Topic one / Topic two / Topic three（待阶段9重写）"
    )


def test_merge_without_evidence_is_rejected() -> None:
    outline = normalize_topic_anchor_response(_outline(), _timeline())
    intervals = build_anchor_intervals(outline, _timeline())
    value = _alignment(first="merge")
    value["transitions"][0]["previous_evidence_ids"] = []
    with pytest.raises(TopicAnchorError, match="merge必须提供"):
        normalize_anchor_alignment_response(value, outline, intervals, _timeline())


@pytest.mark.parametrize("decision", ["uncertain", "invalid_anchor"])
def test_semantic_failure_decisions_never_build_a_plan(decision: str) -> None:
    outline = normalize_topic_anchor_response(_outline(), _timeline())
    intervals = build_anchor_intervals(outline, _timeline())
    alignment = normalize_anchor_alignment_response(
        _alignment(first=decision), outline, intervals, _timeline()
    )
    with pytest.raises(TopicAnchorError, match=decision):
        build_anchor_chapter_plan("DEV01", "run-01", _timeline(), outline, alignment)


def test_minimum_duration_including_last_chapter_is_validated() -> None:
    outline = normalize_topic_anchor_response(_outline(), _timeline())
    intervals = build_anchor_intervals(outline, _timeline())
    value = _alignment()
    value["transitions"][1].update(
        source_event_id="a0011",
        previous_evidence_ids=["a0010"],
        new_evidence_ids=["a0011"],
    )
    alignment = normalize_anchor_alignment_response(value, outline, intervals, _timeline())
    with pytest.raises(TopicAnchorError, match="小于最小时长"):
        build_anchor_chapter_plan(
            "DEV01", "run-01", _timeline(duration=305.0), outline, alignment
        )


def test_primary_success_publishes_atomic_group_and_uses_whole_group_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport(_outline(), _alignment())
    first = plan_task_chapters_anchor_v2(
        output_root, "DEV01", run_id="run-01", config=_config(), transport=transport
    )
    assert first.business_call_count == 2
    assert first.provider_request_count == 2
    assert first.correction_count == 0
    assert first.artifact_cache_hit is False
    assert all((first.run_directory / name).is_file() for name in (*CORE_FILES, "artifact-manifest.json"))
    manifest = json.loads((first.run_directory / "artifact-manifest.json").read_text(encoding="utf-8"))
    assert manifest["strategy_version"] == STRATEGY_VERSION
    assert all(
        manifest["files"][name] == hashlib.sha256((first.run_directory / name).read_bytes()).hexdigest()
        for name in CORE_FILES
    )
    second = plan_task_chapters_anchor_v2(
        output_root, "DEV01", run_id="run-01", config=_config(), transport=SequenceTransport()
    )
    assert second.artifact_cache_hit is True
    assert second.business_call_count == 0
    assert not list(first.run_directory.parent.glob(".run-01.tmp-*"))


def test_structure_error_gets_only_one_correction(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    invalid = _outline()
    invalid["topics"][0]["anchor_reason"] = ""
    transport = SequenceTransport(invalid, _outline(), _alignment())
    result = plan_task_chapters_anchor_v2(
        output_root, "DEV01", run_id="run-01", config=_config(), transport=transport
    )
    assert len(transport.calls) == 3
    assert result.provider_request_count == 3
    assert result.correction_count == 1


def test_exhausted_structure_correction_writes_counted_failure_without_plan(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    invalid = _outline()
    invalid["topics"][0]["anchor_reason"] = ""
    with pytest.raises(TopicAnchorError) as caught:
        plan_task_chapters_anchor_v2(
            output_root,
            "DEV01",
            run_id="run-01",
            config=_config(),
            transport=SequenceTransport(invalid, invalid),
        )
    report = json.loads(caught.value.audit_path.read_text(encoding="utf-8"))
    assert report["failure_type"] == "llm_call_failed"
    assert report["business_call_count"] == 1
    assert report["provider_request_count"] == 2
    assert report["correction_count"] == 1
    assert report["chapter_plan_created"] is False


def test_exhausted_evidence_contradiction_does_not_generate_plan(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    contradictory = _alignment()
    contradictory["transitions"][0]["previous_evidence_ids"] = ["a0006"]
    with pytest.raises(TopicAnchorError) as caught:
        plan_task_chapters_anchor_v2(
            output_root,
            "DEV01",
            run_id="run-01",
            config=_config(),
            transport=SequenceTransport(_outline(), contradictory, contradictory),
        )
    report = json.loads(caught.value.audit_path.read_text(encoding="utf-8"))
    assert report["business_call_count"] == 2
    assert report["provider_request_count"] == 3
    assert report["correction_count"] == 1
    assert not (
        output_root
        / "DEV01"
        / "experiments"
        / "stage8"
        / "two-stage"
        / "anchor-v2"
        / "run-01"
        / "chapter-plan.json"
    ).exists()


def test_whole_group_corruption_invalidates_cache_and_rebuilds_from_llm_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = plan_task_chapters_anchor_v2(
        output_root,
        "DEV01",
        run_id="run-01",
        config=_config(),
        transport=SequenceTransport(_outline(), _alignment()),
    )
    (first.run_directory / "quality-sidecar.json").write_text("{}\n", encoding="utf-8")
    rebuilt = plan_task_chapters_anchor_v2(
        output_root, "DEV01", run_id="run-01", config=_config(), transport=SequenceTransport()
    )
    assert rebuilt.artifact_cache_hit is False
    assert rebuilt.llm_cache_hit_count == 2
    assert rebuilt.provider_request_count == 0
    assert list(first.run_directory.parent.glob(".run-01.invalid-*"))


@pytest.mark.parametrize("decision", ["uncertain", "invalid_anchor"])
def test_experiment_failure_writes_audit_without_plan_or_formal_paths(
    tmp_path: Path, decision: str
) -> None:
    output_root = _prepare_task(tmp_path)
    with pytest.raises(TopicAnchorError) as caught:
        plan_task_chapters_anchor_v2(
            output_root,
            "DEV01",
            run_id="run-01",
            config=_config(),
            transport=SequenceTransport(_outline(), _alignment(first=decision)),
        )
    assert caught.value.failure_type == decision
    assert caught.value.audit_path is not None
    report = json.loads(caught.value.audit_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["result_source"] == "none"
    assert report["recovery_triggered"] is False
    assert report["fallback_triggered"] is False
    assert report["emergency_triggered"] is False
    run_root = output_root / "DEV01" / "experiments" / "stage8" / "two-stage" / "anchor-v2" / "run-01"
    assert not (run_root / "chapter-plan.json").exists()


def test_failure_with_changed_provider_preserves_existing_success_group(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = plan_task_chapters_anchor_v2(
        output_root,
        "DEV01",
        run_id="run-01",
        config=_config(model="first"),
        transport=SequenceTransport(_outline(), _alignment()),
    )
    before = {name: (first.run_directory / name).read_bytes() for name in CORE_FILES}
    with pytest.raises(TopicAnchorError):
        plan_task_chapters_anchor_v2(
            output_root,
            "DEV01",
            run_id="run-01",
            config=_config(model="second"),
            transport=SequenceTransport(_outline(), _alignment(first="uncertain")),
        )
    assert all((first.run_directory / name).read_bytes() == content for name, content in before.items())


def test_over_30_minutes_fails_before_transport_or_artifacts(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path, duration=1800.001)
    transport = SequenceTransport()
    with pytest.raises(TopicAnchorError, match="unsupported_duration") as caught:
        plan_task_chapters_anchor_v2(
            output_root, "DEV01", run_id="run-01", config=_config(), transport=transport
        )
    assert caught.value.failure_type == "unsupported_duration"
    assert transport.calls == []
    assert not (output_root / "DEV01" / "experiments" / "stage8" / "two-stage" / "anchor-v2").exists()


def test_security_and_legacy_outputs_are_preserved(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    task_root = output_root / "DEV01"
    protected = [
        task_root / "boundaries" / "llm.json",
        task_root / "boundaries" / "llm-two-stage.json",
        task_root / "experiments" / "stage8" / "direct" / "event-id-v2" / "run-01" / "chapter-plan.json",
        task_root / "experiments" / "stage8" / "review" / "evidence-review-v2" / "run-01" / "audit.json",
    ]
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"protected:{path.name}\n", encoding="utf-8")
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    secret = "TOP-SECRET-ANCHOR-KEY"
    result = plan_task_chapters_anchor_v2(
        output_root,
        "DEV01",
        run_id="run-01",
        config=_config(secret=secret),
        transport=SequenceTransport(_outline(), _alignment()),
    )
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in hashes.items())
    anchor_root = task_root / "experiments" / "stage8" / "two-stage" / "anchor-v2"
    all_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in anchor_root.rglob("*.json")
    )
    assert secret not in all_text
    assert "source.mp4" not in all_text
    assert not list(task_root.rglob("*.tmp"))
    assert result.plan_path.is_file()


def test_minimal_reference_diagnostic_reports_both_tolerances_and_theme_issues() -> None:
    reference = {"boundaries": [{"timestamp": 0.0}, {"timestamp": 100.0}, {"timestamp": 200.0}]}
    prediction = {"boundaries": [{"timestamp": 0.0}, {"timestamp": 112.0}, {"timestamp": 250.0}]}
    report = diagnose_stage8_prediction(
        reference,
        prediction,
        missing_topics=["final theme"],
        over_split_topics=["example split"],
        incorrect_merges=["two themes merged"],
    )
    assert report["metrics"]["15_seconds"]["tp"] == 1
    assert report["metrics"]["30_seconds"]["tp"] == 1
    assert report["metrics"]["30_seconds"]["matches"][0]["absolute_error_seconds"] == 12.0
    assert report["metrics"]["30_seconds"]["unmatched_reference_seconds"] == [200.0]
    assert report["theme_issues"]["missing_topics"] == ["final theme"]
    assert "不是正式人工真值" in report["notice"]


def test_cli_reports_primary_success_without_formal_fallback_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "chapter-plan.json"
    monkeypatch.setattr(
        cli,
        "plan_task_chapters_anchor_v2",
        lambda *args, **kwargs: TopicAnchorResult(
            run_directory=tmp_path,
            plan_path=plan,
            boundary_count=3,
            duration_seconds=320.0,
            business_call_count=2,
            provider_request_count=2,
            correction_count=0,
            llm_cache_hit_count=0,
            artifact_cache_hit=False,
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["video-doc", "plan-chapters-anchor-v2", "DEV01", "--run-id", "run-01"],
    )
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert payload["result_source"] == "primary"
    assert payload["recovery_triggered"] is False


def test_cli_reports_specific_primary_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise TopicAnchorError("cannot decide", failure_type="uncertain")

    monkeypatch.setattr(cli, "plan_task_chapters_anchor_v2", fail)
    monkeypatch.setattr(
        "sys.argv",
        ["video-doc", "plan-chapters-anchor-v2", "DEV01", "--run-id", "run-01"],
    )
    assert cli.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_type"] == "uncertain"
    assert payload["result_source"] == "none"
    assert payload["fallback_triggered"] is False
