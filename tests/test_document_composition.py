import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from video_doc.chapter_content import (
    CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
    CHAPTER_CONTENT_OUTPUT_LANGUAGE,
    CHAPTER_CONTENT_POLICY_VERSION,
    CHAPTER_CONTENT_PROMPT_VERSION,
    CHAPTER_CONTENT_VALIDATOR_VERSION,
    CHAPTER_WINDOWING_VERSION,
    prepare_task_chapter_inputs,
)
from video_doc.document_composition import (
    DOCUMENT_COMPOSITION_INPUT_BUILDER_VERSION,
    DOCUMENT_COMPOSITION_INPUT_SCHEMA_VERSION,
    DocumentCompositionError,
    assert_protected_files_unchanged,
    document_composition_input_identity_sha256,
    prepare_document_composition_input,
    validate_document_composition_input,
)
from video_doc.structured_document_content import canonical_sha256
from video_doc.task_workspace import initialize_task_workspace


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _timeline() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "duration_seconds": 120.0,
        "language": {"code": "zh", "probability": 0.99},
        "events": [
            {
                "id": "a0001",
                "type": "asr",
                "start": 0.0,
                "end": 20.0,
                "text": "第一章证据一",
                "source_segment_id": 1,
                "pause_before_seconds": 0.0,
            },
            {
                "id": "a0002",
                "type": "asr",
                "start": 20.0,
                "end": 58.0,
                "text": "第一章证据二",
                "source_segment_id": 2,
                "pause_before_seconds": 0.0,
            },
            {
                "id": "a0003",
                "type": "asr",
                "start": 60.0,
                "end": 90.0,
                "text": "第二章证据一",
                "source_segment_id": 3,
                "pause_before_seconds": 2.0,
            },
            {
                "id": "a0004",
                "type": "asr",
                "start": 90.0,
                "end": 119.0,
                "text": "第二章证据二",
                "source_segment_id": 4,
                "pause_before_seconds": 0.0,
            },
        ],
        "source_asr": {
            "schema_version": "1.0",
            "cache_key": "a" * 64,
            "sha256": "b" * 64,
        },
        "cache_key": "c" * 64,
    }


def _prepare_authoritative_chain(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01", source_video=source)
    timeline = _timeline()
    timeline_hash = canonical_sha256(timeline)
    plan = {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "strategy": "llm",
        "duration_seconds": 120.0,
        "source_timeline": {
            "schema_version": "1.0",
            "sha256": timeline_hash,
            "event_count": 4,
        },
        "parameters": {
            "prompt_version": "chapter-boundary-event-id-stability-2.0",
            "min_chapter_seconds": 60.0,
            "boundary_alignment": "selected_asr_event_start",
            "formal_policy_version": "stage8-frozen-a-primary-b-fallback-1.0",
            "formal_result_source": "primary",
        },
        "boundaries": [
            {"timestamp": 0.0, "title": "第一章", "source_event_id": None},
            {"timestamp": 60.0, "title": "第二章", "source_event_id": "a0003"},
        ],
    }
    plan_hash = canonical_sha256(plan)
    stage8_result_id = f"primary-{plan_hash[:12]}"
    stage8_report = {
        "schema_version": "1.0",
        "formal_policy_version": "stage8-frozen-a-primary-b-fallback-1.0",
        "task_id": "DEV01",
        "status": "success",
        "result_source": "primary",
        "quality_level": "normal",
        "business_call_count": 1,
        "supplier_request_count": 1,
        "primary_strategy": "direct-event-id-v2",
        "fallback_strategy": "two-stage-stability-1.0",
        "emergency_strategy": "local-emergency-single-chapter-1.0",
        "errors": [],
        "final_plan_sha256": plan_hash,
        "result_id": stage8_result_id,
    }
    _write_json(workspace.root / "timeline" / "timeline.json", timeline)
    _write_json(workspace.root / "boundaries" / "llm.json", plan)
    _write_json(
        workspace.root / "boundaries" / "llm-execution-report.json", stage8_report
    )
    stage8_group = workspace.root / "boundaries" / "formal-results" / stage8_result_id
    stage8_plan_path = stage8_group / "chapter-plan.json"
    stage8_report_path = stage8_group / "execution-report.json"
    _write_json(stage8_plan_path, plan)
    _write_json(stage8_report_path, stage8_report)
    _write_json(
        stage8_group / "manifest.json",
        {
            "schema_version": "1.0",
            "formal_policy_version": "stage8-frozen-a-primary-b-fallback-1.0",
            "result_id": stage8_result_id,
            "task_id": "DEV01",
            "files": {
                "chapter-plan.json": hashlib.sha256(
                    stage8_plan_path.read_bytes()
                ).hexdigest(),
                "execution-report.json": hashlib.sha256(
                    stage8_report_path.read_bytes()
                ).hexdigest(),
            },
        },
    )

    prepared = prepare_task_chapter_inputs(output_root, "DEV01")
    input_manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    chapters = []
    for expected in input_manifest["chapters"]:
        event_ids = expected["event_ids"]
        chapters.append(
            {
                "chapter_id": expected["chapter_id"],
                "index": expected["index"],
                "start": expected["start"],
                "end": expected["end"],
                "planned_title": expected["planned_title"],
                "title": {"text": "章节标题", "source_event_ids": [event_ids[0]]},
                "summary": {"text": "章节摘要", "source_event_ids": event_ids},
                "key_points": [
                    {"text": "知识点一", "source_event_ids": [event_ids[0]]},
                    {"text": "知识点二", "source_event_ids": [event_ids[-1]]},
                    {"text": "知识点三", "source_event_ids": event_ids},
                ],
                "keywords": [
                    {"text": "关键词一", "source_event_ids": [event_ids[0]]},
                    {"text": "关键词二", "source_event_ids": [event_ids[-1]]},
                    {"text": "关键词三", "source_event_ids": event_ids},
                ],
                "source_event_ids": event_ids,
                "window_count": 1,
            }
        )
    bundle = {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "strategy": "llm",
        "source_chapter_plan": input_manifest["source_chapter_plan"],
        "source_timeline": input_manifest["source_timeline"],
        "chaptering_provenance": input_manifest["chaptering_provenance"],
        "generation": {
            "status": "success",
            "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
            "prompt_version": CHAPTER_CONTENT_PROMPT_VERSION,
            "merge_prompt_version": CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
            "content_policy_version": CHAPTER_CONTENT_POLICY_VERSION,
            "windowing_version": CHAPTER_WINDOWING_VERSION,
            "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
        },
        "chapters": chapters,
    }
    content_hash = canonical_sha256(bundle)
    result_id = f"content-{content_hash[:12]}"
    report = {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "status": "success",
        "result_id": result_id,
        "content_sha256": content_hash,
        "source_stage8_result_id": stage8_result_id,
        "input_manifest_cache_key": input_manifest["cache_key"],
        "chapter_count": len(chapters),
    }
    content_root = workspace.root / "content"
    group = content_root / "formal-results" / result_id
    content_path = group / "content.json"
    report_path = group / "execution-report.json"
    _write_json(content_path, bundle)
    _write_json(report_path, report)
    _write_json(
        group / "manifest.json",
        {
            "schema_version": "1.0",
            "result_id": result_id,
            "task_id": "DEV01",
            "files": {
                "content.json": hashlib.sha256(content_path.read_bytes()).hexdigest(),
                "execution-report.json": hashlib.sha256(
                    report_path.read_bytes()
                ).hexdigest(),
            },
        },
    )
    (content_root / "content.json").write_bytes(content_path.read_bytes())
    (content_root / "content-execution-report.json").write_bytes(
        report_path.read_bytes()
    )
    return output_root


def _refresh_input_identity(payload: dict[str, Any]) -> None:
    for chapter in payload["chapters"]:
        without_hash = dict(chapter)
        without_hash.pop("chapter_input_sha256", None)
        chapter["chapter_input_sha256"] = canonical_sha256(without_hash)
    payload["input_id"] = (
        "composition-input-" + document_composition_input_identity_sha256(payload)[:12]
    )


def _file_state(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_r2_contract_versions_are_frozen() -> None:
    assert DOCUMENT_COMPOSITION_INPUT_SCHEMA_VERSION == "2.0"
    assert (
        DOCUMENT_COMPOSITION_INPUT_BUILDER_VERSION
        == "document-composition-input-builder-2.0"
    )


@pytest.mark.parametrize(
    "profile", ["short_explainer", "course", "academic_report", "speech"]
)
def test_prepare_builds_valid_input_for_each_profile(
    tmp_path: Path, profile: str
) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)

    result = prepare_document_composition_input(
        output_root, "DEV01", document_profile=profile
    )

    validate_document_composition_input(result.composition_input)
    assert result.document_profile == profile
    assert result.input_id.startswith("composition-input-")
    assert len(result.sources.protected_files) == 12
    assert [chapter["event_count"] for chapter in result.composition_input["chapters"]] == [
        2,
        2,
    ]
    assert set(result.composition_input["chapters"][0]["events"][0]) == {
        "id",
        "start",
        "end",
        "text",
    }


def test_prepare_is_deterministic_and_writes_nothing(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    task_root = output_root / "DEV01"
    before = _file_state(task_root)

    first = prepare_document_composition_input(
        output_root, "DEV01", document_profile="course"
    )
    second = prepare_document_composition_input(
        output_root, "DEV01", document_profile="course"
    )

    assert first.composition_input == second.composition_input
    assert _file_state(task_root) == before
    assert not (task_root / "content" / "document-v2").exists()


def test_source_paths_and_coverage_flags_are_stable(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    chapter = prepare_document_composition_input(
        output_root, "DEV01", document_profile="short_explainer"
    ).composition_input["chapters"][0]

    assert [field["source_content_path"] for field in chapter["source_fields"]] == [
        "chapters[c0001].title",
        "chapters[c0001].summary",
        "chapters[c0001].key_points[0]",
        "chapters[c0001].key_points[1]",
        "chapters[c0001].key_points[2]",
        "chapters[c0001].keywords[0]",
        "chapters[c0001].keywords[1]",
        "chapters[c0001].keywords[2]",
    ]
    assert [field["coverage_required"] for field in chapter["source_fields"]] == [
        True,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
    ]


def test_profile_participates_in_input_identity(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    course = prepare_document_composition_input(
        output_root, "DEV01", document_profile="course"
    )
    speech = prepare_document_composition_input(
        output_root, "DEV01", document_profile="speech"
    )

    assert course.input_id != speech.input_id


def test_unknown_profile_is_rejected_before_read(tmp_path: Path) -> None:
    with pytest.raises(DocumentCompositionError, match="profile"):
        prepare_document_composition_input(
            tmp_path / "missing", "DEV01", document_profile="interview"
        )


def test_stage9_top_alias_drift_is_rejected(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    alias = output_root / "DEV01" / "content" / "content.json"
    payload = json.loads(alias.read_text(encoding="utf-8"))
    payload["chapters"][0]["title"]["text"] = "已漂移"
    _write_json(alias, payload)

    with pytest.raises(DocumentCompositionError, match="顶层内容.*不一致"):
        prepare_document_composition_input(
            output_root, "DEV01", document_profile="course"
        )


def test_stage8_manifest_hash_drift_is_rejected(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    plan = json.loads(
        (output_root / "DEV01" / "content" / "content.json").read_text(
            encoding="utf-8"
        )
    )["source_chapter_plan"]
    manifest_path = (
        output_root
        / "DEV01"
        / "boundaries"
        / "formal-results"
        / plan["result_id"]
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["chapter-plan.json"] = "0" * 64
    _write_json(manifest_path, manifest)

    with pytest.raises(DocumentCompositionError, match="chapter-plan.json.*哈希不一致"):
        prepare_document_composition_input(
            output_root, "DEV01", document_profile="course"
        )


def test_timeline_drift_is_rejected(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    timeline_path = output_root / "DEV01" / "timeline" / "timeline.json"
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    timeline["events"][0]["text"] = "时间轴已漂移"
    _write_json(timeline_path, timeline)

    with pytest.raises(DocumentCompositionError, match="复验失败|规范哈希"):
        prepare_document_composition_input(
            output_root, "DEV01", document_profile="course"
        )


def test_protected_snapshot_detects_replacement(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    result = prepare_document_composition_input(
        output_root, "DEV01", document_profile="course"
    )
    path = result.sources.task_root / result.sources.protected_files[0].relative_path
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(DocumentCompositionError, match="保护清单发生变化"):
        assert_protected_files_unchanged(
            result.sources.task_root, result.sources.protected_files
        )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value["source_timeline"].update(event_count=3), "ownership"),
        (
            lambda value: value["chapters"][0]["events"][0].update(
                text="被篡改的事件"
            ),
            "chapter_input_sha256",
        ),
        (
            lambda value: value["chapters"][0]["source_fields"][2].update(
                item_index=1,
                source_content_path="chapters[c0001].key_points[1]",
            ),
            "从0连续递增",
        ),
        (
            lambda value: value["chapters"][0]["source_fields"][0].update(
                source_event_ids=["a0002", "a0001"]
            ),
            "证据ID无效或乱序",
        ),
    ],
)
def test_pure_validator_rejects_drift(
    tmp_path: Path, mutation: Any, match: str
) -> None:
    output_root = _prepare_authoritative_chain(tmp_path)
    payload = deepcopy(
        prepare_document_composition_input(
            output_root, "DEV01", document_profile="course"
        ).composition_input
    )
    mutation(payload)
    if "chapter_input_sha256" not in match:
        _refresh_input_identity(payload)
    else:
        payload["input_id"] = (
            "composition-input-"
            + document_composition_input_identity_sha256(payload)[:12]
        )

    with pytest.raises(DocumentCompositionError, match=match):
        validate_document_composition_input(payload)
