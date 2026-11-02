import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import video_doc.cli as cli
from video_doc.llm_adapter import LLMConfig
from video_doc.llm_strategy import ChapterPlanError, ChapterPlanResult, validate_chapter_plan
from video_doc.task_workspace import initialize_task_workspace
from video_doc.two_stage_llm_strategy import (
    STABILITY_TWO_STAGE_PROMPT_VERSION,
    TWO_STAGE_PROMPT_VERSION,
    build_alignment_messages,
    build_alignment_windows,
    build_outline_messages,
    normalize_alignment_response,
    plan_task_chapters_two_stage,
)


def _timeline() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "duration_seconds": 180.0,
        "language": {"code": "en", "probability": 0.99},
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
                "text": "Opening example",
            },
            {
                "id": "a0002",
                "type": "asr",
                "source_segment_id": 2,
                "start": 60.0,
                "end": 70.0,
                "pause_before_seconds": 45.0,
                "text": "The second topic begins here",
            },
            {
                "id": "a0003",
                "type": "asr",
                "source_segment_id": 3,
                "start": 120.0,
                "end": 130.0,
                "pause_before_seconds": 50.0,
                "text": "The third topic begins here",
            },
            {
                "id": "a0004",
                "type": "asr",
                "source_segment_id": 4,
                "start": 160.0,
                "end": 170.0,
                "pause_before_seconds": 30.0,
                "text": "Closing remarks",
            },
        ],
    }


def _outline_response() -> dict[str, Any]:
    return {
        "chapters": [
            {"title": "Opening", "rough_timestamp": 0.0},
            {"title": "Second topic", "rough_timestamp": 58.0},
            {"title": "Third topic", "rough_timestamp": 118.0},
        ]
    }


def _alignment_response() -> dict[str, Any]:
    return {
        "boundaries": [
            {
                "transition_id": "t0001",
                "title": "Second topic",
                "source_event_id": "a0002",
                "previous_topic": "Opening",
                "new_topic": "Second topic",
            },
            {
                "transition_id": "t0002",
                "title": "Third topic",
                "source_event_id": "a0003",
                "previous_topic": "Second topic",
                "new_topic": "Third topic",
            },
        ]
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
        model="",
        timeout_seconds=60.0,
        api_key="",
        mock_response={"unused": True},
        enable_thinking=None,
    )


class SequenceTransport:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[object] = []

    def complete(self, config: object, messages: object, response_format: object) -> Any:
        self.calls.append((config, messages, response_format))
        if not self.outcomes:
            raise AssertionError("缓存命中时不应访问transport")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_two_stage_prompts_separate_topic_and_alignment_tasks() -> None:
    outline_messages = build_outline_messages(_timeline(), min_chapter_seconds=30.0)
    windows = build_alignment_windows(
        _outline_response(), _timeline(), window_seconds=30.0
    )
    alignment_messages = build_alignment_messages(windows)

    assert TWO_STAGE_PROMPT_VERSION in outline_messages[1]["content"]
    assert "不做精确事件定位" in outline_messages[0]["content"]
    assert [window["transition_id"] for window in windows] == ["t0001", "t0002"]
    assert [event["id"] for event in windows[0]["events"]] == ["a0002"]
    assert [event["id"] for event in windows[1]["events"]] == ["a0003"]
    assert "最早被明确引入" in alignment_messages[0]["content"]
    assert "不得自行生成时间" in alignment_messages[0]["content"]


def test_two_stage_stability_prompts_share_concise_chapter_policy() -> None:
    outline_messages = build_outline_messages(
        _timeline(), min_chapter_seconds=60.0, stability_experiment=True
    )
    windows = build_alignment_windows(
        _outline_response(), _timeline(), window_seconds=30.0
    )
    alignment_messages = build_alignment_messages(
        windows, stability_experiment=True
    )

    outline_prompt = outline_messages[1]["content"]
    alignment_prompt = alignment_messages[1]["content"]
    assert STABILITY_TWO_STAGE_PROMPT_VERSION in outline_prompt
    assert "只划分贯穿一段内容的一级主题" in outline_prompt
    assert "3—5章" in outline_prompt
    assert STABILITY_TWO_STAGE_PROMPT_VERSION in alignment_prompt


def test_alignment_rejects_event_outside_its_window() -> None:
    windows = build_alignment_windows(
        _outline_response(), _timeline(), window_seconds=30.0
    )
    response = _alignment_response()
    response["boundaries"][0]["source_event_id"] = "a0001"

    with pytest.raises(ChapterPlanError, match="窗口外事件"):
        normalize_alignment_response(
            response,
            _outline_response(),
            windows,
            _timeline(),
            min_chapter_seconds=30.0,
        )


def test_two_stage_plan_writes_independent_candidate_and_audit_caches(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    direct_path = output_root / "DEV01" / "boundaries" / "llm.json"
    direct_path.write_text("official-v1.1\n", encoding="utf-8")
    transport = SequenceTransport(_outline_response(), _alignment_response())

    result = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=transport,
    )

    assert result.plan_path.name == "llm-two-stage.json"
    assert direct_path.read_text(encoding="utf-8") == "official-v1.1\n"
    assert result.boundary_count == 3
    assert result.attempt_count == 2
    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    validate_chapter_plan(plan, timeline=_timeline(), expected_task_id="DEV01")
    assert plan["parameters"]["prompt_version"] == TWO_STAGE_PROMPT_VERSION
    assert [boundary["timestamp"] for boundary in plan["boundaries"]] == [
        0.0,
        60.0,
        120.0,
    ]
    for call_id in ("stage8-theme-outline-v2", "stage8-boundary-align-v2"):
        payload = json.loads(
            (output_root / "DEV01" / "llm" / f"{call_id}.json").read_text(
                encoding="utf-8"
            )
        )
        assert "raw_response" in payload
        assert "response" in payload


def test_two_stage_alignment_invalid_response_retries_once(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    invalid = _alignment_response()
    invalid["boundaries"][0]["source_event_id"] = "missing"
    transport = SequenceTransport(_outline_response(), invalid, _alignment_response())

    result = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=transport,
    )

    assert result.attempt_count == 3
    assert len(transport.calls) == 3


def test_two_stage_uses_all_caches_without_rewriting(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=SequenceTransport(_outline_response(), _alignment_response()),
    )
    original_hash = hashlib.sha256(first.plan_path.read_bytes()).hexdigest()
    original_mtime = first.plan_path.stat().st_mtime_ns

    second = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=SequenceTransport(),
    )

    assert second.llm_cache_hit is True
    assert second.plan_cache_hit is True
    assert hashlib.sha256(second.plan_path.read_bytes()).hexdigest() == original_hash
    assert second.plan_path.stat().st_mtime_ns == original_mtime


def test_two_stage_failure_preserves_direct_and_existing_candidate(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    direct_path = output_root / "DEV01" / "boundaries" / "llm.json"
    direct_path.write_text("official-v1.1\n", encoding="utf-8")
    candidate_path = output_root / "DEV01" / "boundaries" / "llm-two-stage.json"
    candidate_path.write_text("previous-candidate\n", encoding="utf-8")
    invalid = _alignment_response()
    invalid["boundaries"][0]["source_event_id"] = "missing"

    with pytest.raises(ChapterPlanError, match="ASR事件不存在"):
        plan_task_chapters_two_stage(
            output_root,
            "DEV01",
            window_seconds=30.0,
            config=_mock_config(),
            transport=SequenceTransport(_outline_response(), invalid, invalid),
        )

    assert direct_path.read_text(encoding="utf-8") == "official-v1.1\n"
    assert candidate_path.read_text(encoding="utf-8") == "previous-candidate\n"


def test_two_stage_experiment_isolated_by_run_id_and_uses_its_own_cache(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    task_root = output_root / "DEV01"
    direct_path = task_root / "boundaries" / "llm.json"
    candidate_path = task_root / "boundaries" / "llm-two-stage.json"
    direct_path.write_text("official-direct\n", encoding="utf-8")
    candidate_path.write_text("official-candidate\n", encoding="utf-8")
    first_transport = SequenceTransport(_outline_response(), _alignment_response())

    first = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=first_transport,
        experiment_id="run-01",
    )

    experiment_root = task_root / "experiments" / "stage8" / "two-stage" / "run-01"
    assert first.plan_path == experiment_root / "chapter-plan.json"
    assert first.min_chapter_seconds == 60.0
    assert direct_path.read_text(encoding="utf-8") == "official-direct\n"
    assert candidate_path.read_text(encoding="utf-8") == "official-candidate\n"
    plan = json.loads(first.plan_path.read_text(encoding="utf-8"))
    assert plan["parameters"]["prompt_version"] == STABILITY_TWO_STAGE_PROMPT_VERSION
    assert plan["parameters"]["experiment_id"] == "run-01"
    assert plan["parameters"]["chapter_policy_version"]
    for call_id in ("stage8-theme-outline-v2", "stage8-boundary-align-v2"):
        audit = json.loads(
            (experiment_root / f"{call_id}.json").read_text(encoding="utf-8")
        )
        assert audit["request"]["cache_namespace"] == "run-01"

    original_hash = hashlib.sha256(first.plan_path.read_bytes()).hexdigest()
    original_mtime = first.plan_path.stat().st_mtime_ns
    cached = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=SequenceTransport(),
        experiment_id="run-01",
    )
    assert cached.llm_cache_hit is True
    assert cached.plan_cache_hit is True
    assert hashlib.sha256(cached.plan_path.read_bytes()).hexdigest() == original_hash
    assert cached.plan_path.stat().st_mtime_ns == original_mtime

    second = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=SequenceTransport(_outline_response(), _alignment_response()),
        experiment_id="run-02",
    )
    assert second.plan_path != first.plan_path
    assert second.llm_cache_hit is False


def test_two_stage_experiment_failure_preserves_formal_and_previous_run(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    task_root = output_root / "DEV01"
    direct_path = task_root / "boundaries" / "llm.json"
    candidate_path = task_root / "boundaries" / "llm-two-stage.json"
    direct_path.write_text("official-direct\n", encoding="utf-8")
    candidate_path.write_text("official-candidate\n", encoding="utf-8")
    first = plan_task_chapters_two_stage(
        output_root,
        "DEV01",
        window_seconds=30.0,
        config=_mock_config(),
        transport=SequenceTransport(_outline_response(), _alignment_response()),
        experiment_id="run-01",
    )
    previous = first.plan_path.read_bytes()
    invalid = _alignment_response()
    invalid["boundaries"][0]["source_event_id"] = "missing"

    with pytest.raises(ChapterPlanError, match="ASR事件不存在"):
        plan_task_chapters_two_stage(
            output_root,
            "DEV01",
            min_chapter_seconds=59.0,
            window_seconds=31.0,
            config=_mock_config(),
            transport=SequenceTransport(_outline_response(), invalid, invalid),
            experiment_id="run-01",
        )

    assert direct_path.read_text(encoding="utf-8") == "official-direct\n"
    assert candidate_path.read_text(encoding="utf-8") == "official-candidate\n"
    assert first.plan_path.read_bytes() == previous


def test_two_stage_cli_candidate_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate = tmp_path / "outputs" / "DEV01" / "boundaries" / "llm-two-stage.json"

    def fake_plan(*args: object, **kwargs: object) -> ChapterPlanResult:
        return ChapterPlanResult(
            plan_path=candidate,
            boundary_count=3,
            duration_seconds=180.0,
            min_chapter_seconds=30.0,
            attempt_count=2,
            llm_cache_hit=False,
            plan_cache_hit=False,
        )

    monkeypatch.setattr(cli, "plan_task_chapters_two_stage", fake_plan)
    monkeypatch.setattr(
        "sys.argv",
        [
            "video-doc",
            "plan-chapters",
            "DEV01",
            "--output-root",
            str(tmp_path / "outputs"),
            "--pipeline",
            "two-stage",
        ],
    )

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"pipeline": "two-stage"' in output
    assert "llm-two-stage.json" in output


def test_two_stage_cli_forwards_experiment_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path = (
        tmp_path
        / "outputs"
        / "DEV01"
        / "experiments"
        / "stage8"
        / "two-stage"
        / "run-01"
        / "chapter-plan.json"
    )
    captured: dict[str, object] = {}

    def fake_plan(*args: object, **kwargs: object) -> ChapterPlanResult:
        captured.update(kwargs)
        return ChapterPlanResult(
            plan_path=plan_path,
            boundary_count=3,
            duration_seconds=180.0,
            min_chapter_seconds=60.0,
            attempt_count=2,
            llm_cache_hit=False,
            plan_cache_hit=False,
        )

    monkeypatch.setattr(cli, "plan_task_chapters_two_stage", fake_plan)
    monkeypatch.setattr(
        "sys.argv",
        [
            "video-doc",
            "plan-chapters",
            "DEV01",
            "--output-root",
            str(tmp_path / "outputs"),
            "--pipeline",
            "two-stage",
            "--experiment-id",
            "run-01",
        ],
    )

    assert cli.main() == 0
    assert captured["experiment_id"] == "run-01"
    assert captured["min_chapter_seconds"] is None
    assert '"experiment_id": "run-01"' in capsys.readouterr().out
