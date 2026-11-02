import json
from pathlib import Path
from typing import Any

import pytest

from video_doc.stability_comparison import (
    StabilityComparisonError,
    build_stability_comparison,
    build_stability_summary,
    calculate_scheme_statistics,
    load_experiment_plans,
    write_stability_comparison,
    write_stability_summary,
)
from video_doc.llm_strategy import STABILITY_DIRECT_EXPERIMENT_VERSION


def _timeline() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "COURSE01",
        "duration_seconds": 360.0,
        "language": {"code": "zh", "probability": 0.99},
        "source_asr": {
            "schema_version": "1.0",
            "cache_key": "a" * 64,
            "sha256": "b" * 64,
        },
        "events": [
            {
                "id": f"a{index:04d}",
                "type": "asr",
                "source_segment_id": index,
                "start": float((index - 1) * 30),
                "end": float(index * 30),
                "pause_before_seconds": 0.0,
                "text": f"原文片段{index}",
            }
            for index in range(1, 13)
        ],
    }


def _plan(
    run_id: str,
    timestamps: list[float],
    *,
    direct: bool = False,
) -> dict[str, Any]:
    events = _timeline()["events"]
    boundaries = [
        {"timestamp": 0.0, "title": "开头", "source_event_id": None}
    ]
    for timestamp in timestamps:
        event = next(event for event in events if event["start"] == timestamp)
        boundaries.append(
            {
                "timestamp": timestamp,
                "title": f"主题{int(timestamp)}",
                "source_event_id": event["id"],
            }
        )
    return {
        "schema_version": "1.0",
        "task_id": "COURSE01",
        "strategy": "llm",
        "source_timeline": {
            "schema_version": "1.0",
            "sha256": "placeholder",
            "event_count": 12,
        },
        "duration_seconds": 360.0,
        "parameters": {
            "prompt_version": "experiment",
            "min_chapter_seconds": 60.0,
            "boundary_alignment": (
                "selected_asr_event_start" if direct else "nearest_asr_start"
            ),
            "experiment_id": run_id,
            "chapter_policy_version": "policy",
            **(
                {"experiment_version": STABILITY_DIRECT_EXPERIMENT_VERSION}
                if direct
                else {}
            ),
        },
        "boundaries": boundaries,
    }


def _with_timeline_source(plan: dict[str, Any]) -> dict[str, Any]:
    from video_doc.llm_strategy import _timeline_sha256

    plan["source_timeline"]["sha256"] = _timeline_sha256(_timeline())
    return plan


def _plans() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "direct": {
            "run-01": _with_timeline_source(
                _plan("run-01", [120.0, 240.0], direct=True)
            ),
            "run-02": _with_timeline_source(
                _plan("run-02", [120.0, 240.0], direct=True)
            ),
            "run-03": _with_timeline_source(
                _plan("run-03", [180.0, 240.0], direct=True)
            ),
        },
        "two-stage": {
            "run-01": _with_timeline_source(_plan("run-01", [120.0, 270.0])),
            "run-02": _with_timeline_source(_plan("run-02", [120.0, 270.0])),
            "run-03": _with_timeline_source(_plan("run-03", [120.0, 240.0])),
        },
    }


def test_comparison_contains_all_runs_consensus_context_and_full_asr() -> None:
    content = build_stability_comparison(_timeline(), _plans())

    assert "不使用AI参考章节" in content
    assert content.count("### 方案A（直接分章） · run-") == 3
    assert content.count("### 方案B（两阶段） · run-") == 3
    assert "共识边界" in content
    assert "#### 共识1" in content
    assert "分章线在这里：下一段开始进入新章" in content
    assert "本章从下面这段原文开始（包含）" in content
    assert "下面这一行是新章第一段" in content
    assert "标记永远放在它所对应原文的**上方**" in content
    for event in _timeline()["events"]:
        assert content.count(event["text"]) >= 1


def test_scheme_statistics_reports_variation_short_chapters_and_drift() -> None:
    stats = calculate_scheme_statistics(_plans()["direct"])

    assert stats["valid_runs"] == 3
    assert stats["chapter_count"] == {
        "min": 3,
        "median": 3,
        "max": 3,
        "runs": {"run-01": 3, "run-02": 3, "run-03": 3},
    }
    assert stats["short_chapters"]["count"] == 1
    assert stats["pairwise_boundary_agreement"] == pytest.approx(2 / 3)
    assert stats["matched_boundary_drift_seconds"]["max"] == 0.0
    assert len(stats["consensus_boundaries"]) == 2


def test_summary_has_no_automatic_decision_and_links_each_task() -> None:
    payloads = {
        task_id: (_timeline(), _plans())
        for task_id in ("DEV01", "COURSE01", "EXPLAINER01", "TALK01")
    }

    summary, markdown = build_stability_summary(payloads)

    assert summary["decision"] is None
    assert set(summary["tasks"]) == set(payloads)
    assert "不自动选择方案" in markdown
    for task_id in payloads:
        assert f"./{task_id}/comparison.md" in markdown


def test_load_and_write_comparison_uses_isolated_experiment_paths(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "outputs"
    task_root = output_root / "COURSE01"
    (task_root / "timeline").mkdir(parents=True)
    (task_root / "timeline" / "timeline.json").write_text(
        json.dumps(_timeline(), ensure_ascii=False), encoding="utf-8"
    )
    for scheme, scheme_plans in _plans().items():
        for run_id, plan in scheme_plans.items():
            run_root = task_root / "experiments" / "stage8" / scheme
            if scheme == "direct":
                run_root /= STABILITY_DIRECT_EXPERIMENT_VERSION
            run_root /= run_id
            run_root.mkdir(parents=True)
            (run_root / "chapter-plan.json").write_text(
                json.dumps(plan, ensure_ascii=False), encoding="utf-8"
            )

    loaded = load_experiment_plans(task_root, "COURSE01")
    path = write_stability_comparison(output_root, "COURSE01")

    assert set(loaded) == {"direct", "two-stage"}
    assert path == output_root / "stage8_stability" / "COURSE01" / "comparison.md"
    assert "原文片段12" in path.read_text(encoding="utf-8")


def test_missing_experiment_plan_fails_without_partial_output(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    task_root = output_root / "COURSE01"
    (task_root / "timeline").mkdir(parents=True)
    (task_root / "timeline" / "timeline.json").write_text(
        json.dumps(_timeline(), ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(StabilityComparisonError, match="无法读取实验JSON"):
        write_stability_comparison(output_root, "COURSE01")

    assert not (output_root / "stage8_stability" / "COURSE01" / "comparison.md").exists()


def test_write_summary_creates_json_and_markdown(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    for task_id in ("DEV01", "COURSE01"):
        timeline = _timeline()
        timeline["task_id"] = task_id
        task_root = output_root / task_id
        (task_root / "timeline").mkdir(parents=True)
        (task_root / "timeline" / "timeline.json").write_text(
            json.dumps(timeline, ensure_ascii=False), encoding="utf-8"
        )
        for scheme, scheme_plans in _plans().items():
            for run_id, original_plan in scheme_plans.items():
                plan = json.loads(json.dumps(original_plan))
                plan["task_id"] = task_id
                from video_doc.llm_strategy import _timeline_sha256

                plan["source_timeline"]["sha256"] = _timeline_sha256(timeline)
                run_root = task_root / "experiments" / "stage8" / scheme
                if scheme == "direct":
                    run_root /= STABILITY_DIRECT_EXPERIMENT_VERSION
                run_root /= run_id
                run_root.mkdir(parents=True)
                (run_root / "chapter-plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False), encoding="utf-8"
                )

    json_path, markdown_path = write_stability_summary(
        output_root, ("DEV01", "COURSE01")
    )

    assert json.loads(json_path.read_text(encoding="utf-8"))["decision"] is None
    assert "DEV01" in markdown_path.read_text(encoding="utf-8")
