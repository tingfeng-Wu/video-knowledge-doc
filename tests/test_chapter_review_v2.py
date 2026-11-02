import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import video_doc.cli as cli
from video_doc.chapter_review import ChapterReviewError, ChapterReviewResult
from video_doc.chapter_review_v2 import (
    REVIEW_V2_EXPERIMENT_VERSION,
    REVIEW_V2_PROMPT_VERSION,
    REVIEW_V2_VALIDATION_VERSION,
    build_review_v2_messages,
    normalize_review_v2_response,
    review_task_chapters_v2,
)
from video_doc.llm_adapter import LLMConfig
from video_doc.task_workspace import initialize_task_workspace


def _timeline() -> dict[str, Any]:
    events = [
        {
            "id": f"a{index + 1:04d}",
            "type": "asr",
            "source_segment_id": index + 1,
            "start": index * 70.0,
            "end": index * 70.0 + 20.0,
            "pause_before_seconds": 0.0 if index == 0 else 50.0,
            "text": f"第{index + 1}段课程原文",
        }
        for index in range(7)
    ]
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
    (workspace.root / "timeline" / "timeline.json").write_text(
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


def _config() -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model="mock-review-v2",
        timeout_seconds=60.0,
        api_key="sk-test-secret",
        mock_response={"unused": True},
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


def _revise(
    boundaries: list[dict[str, str]],
    evidence: list[str],
    reason: str = "ASR显示一级主题在证据附近发生变化",
) -> dict[str, Any]:
    return {
        "verdict": "revise",
        "issues": [{"evidence_event_ids": evidence, "reason": reason}],
        "revised_boundaries": boundaries,
    }


def _boundaries(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"source_event_id": event_id, "title": title}
        for event_id, title in items
    ]


def test_versions_and_prompt_are_v2_and_do_not_leak_known_answer() -> None:
    assert REVIEW_V2_PROMPT_VERSION == "chapter-plan-evidence-review-2.0"
    assert REVIEW_V2_VALIDATION_VERSION == "chapter-plan-review-v2"
    assert REVIEW_V2_EXPERIMENT_VERSION == "evidence-review-v2"
    messages = build_review_v2_messages(_timeline(), _source_plan(_timeline()))
    prompt = messages[1]["content"]

    assert "只输出完整最终计划" in prompt
    assert "evidence_event_ids只用于证明reason" in prompt
    assert all(label in prompt for label in ("示例1", "示例2", "示例3", "示例4"))
    assert "operation" not in prompt and "chapter_indexes" not in prompt
    assert "a0273" not in prompt and "a0327" not in prompt and "COURSE01" not in prompt
    assert ".mp4" not in prompt and "方案B" not in prompt
    assert "人工参考" not in prompt and "manual_results" not in prompt


def test_accept_returns_source_and_empty_difference() -> None:
    normalized, plan, difference = normalize_review_v2_response(
        _accept(), _timeline(), _source_plan(_timeline())
    )
    assert normalized == _accept()
    assert plan == _source_plan(_timeline())
    assert difference["chapter_count_delta"] == 0
    assert difference["labels"] == []


@pytest.mark.parametrize(
    ("response", "label", "removed", "added", "delta"),
    [
        (
            _revise(_boundaries(("a0001", "导论"), ("a0005", "应用")), ["a0003"]),
            "chapter-count-decrease",
            ["a0003"],
            [],
            -1,
        ),
        (
            _revise(
                _boundaries(
                    ("a0001", "导论"),
                    ("a0003", "核心概念"),
                    ("a0004", "补充机制"),
                    ("a0005", "应用"),
                ),
                ["a0003", "a0004"],
            ),
            "chapter-count-increase",
            [],
            ["a0004"],
            1,
        ),
        (
            _revise(
                _boundaries(
                    ("a0001", "导论"),
                    ("a0004", "核心概念"),
                    ("a0005", "应用"),
                ),
                ["a0003", "a0004"],
            ),
            "boundary-replacement",
            ["a0003"],
            ["a0004"],
            0,
        ),
        (
            _revise(
                _boundaries(
                    ("a0001", "课程导论"),
                    ("a0003", "核心概念"),
                    ("a0005", "应用"),
                ),
                ["a0001"],
            ),
            "title-change",
            [],
            [],
            0,
        ),
        (
            _revise(_boundaries(("a0001", "课程导论"), ("a0005", "应用")), ["a0001", "a0003"]),
            "compound-change",
            ["a0003"],
            [],
            -1,
        ),
    ],
)
def test_deterministic_difference_labels(
    response: dict[str, Any],
    label: str,
    removed: list[str],
    added: list[str],
    delta: int,
) -> None:
    _, plan, difference = normalize_review_v2_response(
        response, _timeline(), _source_plan(_timeline())
    )
    assert plan["boundaries"]
    assert difference["labels"] == [label]
    assert difference["removed"] == removed
    assert difference["added"] == added
    assert difference["chapter_count_delta"] == delta


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value.pop("issues"), "只能包含"),
        (lambda value: value.update(extra=True), "只能包含"),
        (
            lambda value: value["issues"][0].update(
                operation="move", chapter_indexes=[2], original_event_ids=["a0003"]
            ),
            "只能包含",
        ),
        (lambda value: value.update(issues=[]), "1至4"),
        (lambda value: value.update(issues=value["issues"] * 5), "1至4"),
        (lambda value: value["issues"][0].update(evidence_event_ids=[]), "1至8"),
        (
            lambda value: value["issues"][0].update(
                evidence_event_ids=[f"a{index:04d}" for index in range(1, 10)]
            ),
            "1至8",
        ),
        (
            lambda value: value["issues"][0].update(
                evidence_event_ids=["a0003", "a0003"]
            ),
            "重复",
        ),
        (
            lambda value: value["issues"][0].update(evidence_event_ids=["missing"]),
            "不存在",
        ),
        (lambda value: value["issues"][0].update(reason=" "), "不能为空"),
    ],
)
def test_v2_schema_rejects_invalid_and_v1_fields(mutator: Any, message: str) -> None:
    response = _revise(
        _boundaries(("a0001", "导论"), ("a0004", "核心概念"), ("a0005", "应用")),
        ["a0003", "a0004"],
    )
    mutator(response)
    with pytest.raises(ChapterReviewError, match=message):
        normalize_review_v2_response(response, _timeline(), _source_plan(_timeline()))


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
def test_revised_plan_structure_is_fully_validated(mutator: Any, message: str) -> None:
    response = _revise(
        _boundaries(("a0001", "导论"), ("a0004", "核心概念"), ("a0005", "应用")),
        ["a0003", "a0004"],
    )
    mutator(response)
    with pytest.raises(ChapterReviewError, match=message):
        normalize_review_v2_response(response, _timeline(), _source_plan(_timeline()))


def test_revise_without_actual_change_is_rejected() -> None:
    response = _revise(
        _boundaries(("a0001", "导论"), ("a0003", "核心概念"), ("a0005", "应用")),
        ["a0003"],
    )
    with pytest.raises(ChapterReviewError, match="未实际修改"):
        normalize_review_v2_response(response, _timeline(), _source_plan(_timeline()))


def test_last_chapter_minimum_duration_is_enforced() -> None:
    response = _revise(
        _boundaries(
            ("a0001", "导论"),
            ("a0003", "核心概念"),
            ("a0005", "应用"),
            ("a0007", "过短结尾"),
        ),
        ["a0007"],
    )
    source = _source_plan(_timeline())
    source["parameters"]["min_chapter_seconds"] = 80.0
    with pytest.raises(ChapterReviewError, match="小于最小值"):
        normalize_review_v2_response(response, _timeline(), source)


def test_evidence_must_be_near_every_changed_boundary() -> None:
    response = _revise(
        _boundaries(("a0001", "导论"), ("a0004", "核心概念"), ("a0005", "应用")),
        ["a0007"],
    )
    with pytest.raises(ChapterReviewError, match="未覆盖变化位置"):
        normalize_review_v2_response(response, _timeline(), _source_plan(_timeline()))


def test_short_chapter_reason_creates_warning_without_rejecting() -> None:
    response = _revise(
        _boundaries(("a0001", "导论与核心"), ("a0005", "应用")),
        ["a0003"],
        reason="原第二章章节过短，应与前章合并",
    )
    _, _, difference = normalize_review_v2_response(
        response, _timeline(), _source_plan(_timeline())
    )
    warnings = difference["reason_warnings"]
    assert warnings and warnings[0]["code"] == "short-chapter-claim-conflicts-with-valid-plan"


def test_accept_writes_v2_artifacts_and_keeps_v1_isolated(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    source_before = source_path.read_bytes()
    v1_dir = output_root / "DEV01" / "experiments" / "stage8" / "review" / "evidence-review-v1" / "run-01"
    v1_dir.mkdir(parents=True)
    v1_file = v1_dir / "review-failure.json"
    v1_file.write_bytes(b"v1-frozen")

    result = review_task_chapters_v2(
        output_root,
        "DEV01",
        source_run_id="run-01",
        config=_config(),
        transport=SequenceTransport(_accept()),
    )

    assert result.status == "completed" and result.verdict == "accept"
    assert result.audit_path.parent.parent.name == REVIEW_V2_EXPERIMENT_VERSION
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["review"]["prompt_version"] == REVIEW_V2_PROMPT_VERSION
    assert audit["difference"]["labels"] == []
    assert source_path.read_bytes() == source_before
    assert v1_file.read_bytes() == b"v1-frozen"


def test_same_run_cache_and_cross_run_isolation(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    first = review_task_chapters_v2(
        output_root, "DEV01", source_run_id="run-01", config=_config(), transport=SequenceTransport(_accept())
    )
    assert first.reviewed_plan_path is not None
    plan_mtime = first.reviewed_plan_path.stat().st_mtime_ns
    audit_mtime = first.audit_path.stat().st_mtime_ns
    cached = review_task_chapters_v2(
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
    second = review_task_chapters_v2(
        output_root, "DEV01", source_run_id="run-02", config=_config(), transport=SequenceTransport(_accept())
    )
    assert second.llm_cache_hit is False and second.audit_path.parent.name == "run-02"


def test_invalid_response_falls_back_and_redacts_secret(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    source_before = source_path.read_bytes()
    invalid = _revise(
        _boundaries(("a0001", "导论"), ("missing", "核心概念"), ("a0005", "应用")),
        ["a0003"],
        reason="sk-test-secret",
    )
    result = review_task_chapters_v2(
        output_root,
        "DEV01",
        source_run_id="run-01",
        config=_config(),
        transport=SequenceTransport(invalid),
    )
    assert result.status == "failed" and result.fallback_used
    assert result.selected_plan_path == source_path and result.reviewed_plan_path is None
    assert source_path.read_bytes() == source_before
    failure_text = result.audit_path.read_text(encoding="utf-8")
    assert "sk-test-secret" not in failure_text and "[REDACTED]" in failure_text
    assert not (result.audit_path.parent / "chapter-plan.reviewed.json").exists()


def test_formal_source_and_media_references_are_protected(tmp_path: Path) -> None:
    output_root, source_path = _prepare(tmp_path)
    formal_a = output_root / "DEV01" / "boundaries" / "llm.json"
    formal_b = output_root / "DEV01" / "boundaries" / "llm-two-stage.json"
    formal_a.parent.mkdir(parents=True, exist_ok=True)
    formal_a.write_bytes(b"formal-a")
    formal_b.write_bytes(b"formal-b")
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (source_path, formal_a, formal_b)
    }
    result = review_task_chapters_v2(
        output_root,
        "DEV01",
        source_run_id="run-01",
        config=_config(),
        transport=SequenceTransport(_accept()),
    )
    assert before == {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before
    }
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in result.audit_path.parent.glob("*.json")
    )
    assert "sk-test-secret" not in combined
    assert ".mp4" not in combined and ".wav" not in combined
    assert not list(result.audit_path.parent.glob("*.tmp"))


def test_review_v2_cli_prints_version_and_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    reviewed = tmp_path / "reviewed.json"
    audit = tmp_path / "audit.json"
    monkeypatch.setattr(
        cli,
        "review_task_chapters_v2",
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
        ["video-doc", "review-chapters-v2", "DEV01", "--source-run-id", "run-01"],
    )
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"review_version": "v2"' in output and "Review v2审阅完成" in output
