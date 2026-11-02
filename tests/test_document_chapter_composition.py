import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from video_doc.document_chapter_composition import (
    DOCUMENT_CHAPTER_CACHE_NAMESPACE,
    DOCUMENT_CHAPTER_COVERAGE_POLICY_VERSION,
    DOCUMENT_CHAPTER_FACT_POLICY_VERSION,
    DOCUMENT_CHAPTER_MERGE_PROMPT_VERSION,
    DOCUMENT_CHAPTER_PROMPT_VERSION,
    DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION,
    DOCUMENT_CHAPTER_WINDOWING_VERSION,
    DeterministicDocumentChapterMockTransport,
    DocumentChapterCompositionError,
    compose_document_chapters_offline_mock,
    split_document_chapter_events,
    validate_chapter_composition_response,
)
from video_doc.document_composition import (
    document_composition_input_identity_sha256,
)
from video_doc.llm_adapter import LLMConfig
from video_doc.structured_document_content import (
    DocumentContentValidationContext,
    canonical_sha256,
)
from video_doc.task_workspace import initialize_task_workspace


def _event(event_id: str, start: float, text: str) -> dict[str, Any]:
    return {"id": event_id, "start": start, "end": start + 9.0, "text": text}


def _composition_input(profile: str = "course") -> dict[str, Any]:
    events = [
        _event("a0001", 0.0, "本节比较ModelA与ModelB。"),
        _event("a0002", 10.0, "样本包含14人，没有使用外部数据。"),
        _event("a0003", 20.0, "ModelA需要2步，ModelB可能更快。"),
        _event("a0004", 30.0, "最后总结两种方法的差异。"),
    ]
    source_fields = [
        {
            "source_content_path": "chapters[c0001].title",
            "kind": "title",
            "item_index": None,
            "text": "ModelA与ModelB的比较",
            "source_event_ids": ["a0001"],
            "coverage_required": True,
        },
        {
            "source_content_path": "chapters[c0001].summary",
            "kind": "summary",
            "item_index": None,
            "text": "样本包含14人，没有使用外部数据。",
            "source_event_ids": ["a0002"],
            "coverage_required": True,
        },
        {
            "source_content_path": "chapters[c0001].key_points[0]",
            "kind": "key_point",
            "item_index": 0,
            "text": "ModelA需要2步。",
            "source_event_ids": ["a0003"],
            "coverage_required": True,
        },
        {
            "source_content_path": "chapters[c0001].key_points[1]",
            "kind": "key_point",
            "item_index": 1,
            "text": "ModelB可能更快。",
            "source_event_ids": ["a0003"],
            "coverage_required": True,
        },
        {
            "source_content_path": "chapters[c0001].keywords[0]",
            "kind": "keyword",
            "item_index": 0,
            "text": "ModelA",
            "source_event_ids": ["a0001", "a0003"],
            "coverage_required": False,
        },
        {
            "source_content_path": "chapters[c0001].keywords[1]",
            "kind": "keyword",
            "item_index": 1,
            "text": "ModelB",
            "source_event_ids": ["a0001", "a0003"],
            "coverage_required": False,
        },
    ]
    chapter = {
        "chapter_id": "c0001",
        "index": 1,
        "start": 0.0,
        "end": 40.0,
        "planned_title": "Comparison",
        "window_count": 1,
        "source_fields": source_fields,
        "events": events,
        "event_count": 4,
    }
    chapter["chapter_input_sha256"] = canonical_sha256(chapter)
    payload = {
        "schema_version": "2.0",
        "task_id": "DEV01",
        "language": "zh-CN",
        "strategy": "llm",
        "document_profile": profile,
        "source_content_bundle": {
            "schema_version": "1.0",
            "result_id": "content-" + "b" * 12,
            "canonical_sha256": "b" * 64,
            "content_file_sha256": "c" * 64,
            "execution_report_file_sha256": "d" * 64,
            "manifest_file_sha256": "e" * 64,
            "input_manifest_file_sha256": "f" * 64,
        },
        "source_chapter_plan": {
            "schema_version": "1.0",
            "result_id": "primary-" + "a" * 12,
            "canonical_sha256": "a" * 64,
            "chapter_plan_file_sha256": "1" * 64,
            "execution_report_file_sha256": "2" * 64,
            "manifest_file_sha256": "3" * 64,
        },
        "source_timeline": {
            "schema_version": "1.0",
            "canonical_sha256": "4" * 64,
            "timeline_file_sha256": "5" * 64,
            "event_count": 4,
        },
        "chaptering_provenance": {
            "status": "success",
            "result_source": "primary",
            "quality_level": "normal",
            "execution_report_sha256": "6" * 64,
        },
        "source_generation": {
            "status": "success",
            "output_language": "zh-CN",
            "prompt_version": "v1-prompt",
            "merge_prompt_version": "v1-merge",
            "content_policy_version": "v1-policy",
            "windowing_version": "v1-window",
            "validator_version": "v1-validator",
        },
        "ownership": {
            "policy_version": "asr-event-start-half-open-1.0",
            "rule": "chapter.start <= event.start < chapter.end",
            "assigned_event_count": 4,
            "duplicate_event_count": 0,
            "unassigned_event_count": 0,
        },
        "chapters": [chapter],
        "input_versions": {
            "input_schema": "2.0",
            "input_builder": "document-composition-input-builder-2.0",
            "profile_policy": "document-profile-policy-2.0",
            "source_path": "document-source-path-2.0",
            "ownership_policy": "asr-event-start-half-open-1.0",
        },
    }
    payload["input_id"] = (
        "composition-input-" + document_composition_input_identity_sha256(payload)[:12]
    )
    return payload


def _workspace(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    output_root = tmp_path / "outputs"
    initialize_task_workspace(output_root, "DEV01", source_video=source)
    return output_root


def _context(payload: Mapping[str, Any]) -> DocumentContentValidationContext:
    chapter = payload["chapters"][0]
    paths = frozenset(field["source_content_path"] for field in chapter["source_fields"])
    required = frozenset(
        field["source_content_path"]
        for field in chapter["source_fields"]
        if field["coverage_required"]
    )
    return DocumentContentValidationContext(
        known_source_paths=paths,
        required_core_paths=required,
        chapter_event_ids={"c0001": tuple(event["id"] for event in chapter["events"])},
        event_times={
            event["id"]: (event["start"], event["end"])
            for event in chapter["events"]
        },
    )


def _response(payload: Mapping[str, Any]) -> dict[str, Any]:
    chapter = payload["chapters"][0]
    fields = chapter["source_fields"]
    core = [field for field in fields if field["coverage_required"]]
    block_id = "c0001-s001-p001"
    evidence = {
        "content_origin": "stage9_v1",
        "source_content_paths": [field["source_content_path"] for field in core],
        "source_event_ids": ["a0001", "a0002", "a0003"],
    }
    return {
        "schema_version": "2.0",
        "chapter_id": "c0001",
        "title": {
            "text": fields[0]["text"],
            "content_origin": "stage9_v1",
            "source_content_paths": [fields[0]["source_content_path"]],
            "source_event_ids": fields[0]["source_event_ids"],
        },
        "keywords": [],
        "sections": [
            {
                "section_id": "c0001-s001",
                "heading": None,
                "blocks": [
                    {
                        "block_id": block_id,
                        "type": "paragraph",
                        "role": "explanation",
                        "text": "；".join(field["text"] for field in core),
                        **evidence,
                    }
                ],
            }
        ],
        "coverage": {
            "items": [
                {
                    "source_content_path": field["source_content_path"],
                    "status": "represented",
                    "target_block_ids": [block_id],
                }
                for field in core
            ]
        },
    }


def _refresh_input(payload: dict[str, Any]) -> None:
    chapter = payload["chapters"][0]
    without_hash = dict(chapter)
    without_hash.pop("chapter_input_sha256", None)
    chapter["chapter_input_sha256"] = canonical_sha256(without_hash)
    payload["input_id"] = (
        "composition-input-" + document_composition_input_identity_sha256(payload)[:12]
    )


class FailIfCalledTransport:
    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        del config, messages, response_format
        raise AssertionError("缓存命中时不得调用transport")


class FailSecondWindowTransport(DeterministicDocumentChapterMockTransport):
    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        if self.call_count >= 1:
            self.call_count += 1
            return {"invalid": "不得发布"}
        return super().complete(config, messages, response_format)


class RetryOnceTransport(DeterministicDocumentChapterMockTransport):
    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        if self.call_count == 0:
            self.call_count += 1
            return {"invalid": "首次结构错误"}
        return super().complete(config, messages, response_format)


def test_r3_versions_are_frozen() -> None:
    assert DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION == "2.0"
    assert DOCUMENT_CHAPTER_PROMPT_VERSION == "document-chapter-prompt-2.2"
    assert DOCUMENT_CHAPTER_MERGE_PROMPT_VERSION == "document-chapter-merge-prompt-2.0"
    assert DOCUMENT_CHAPTER_WINDOWING_VERSION == "document-chapter-windowing-2.0"
    assert DOCUMENT_CHAPTER_FACT_POLICY_VERSION == "document-high-risk-fact-screen-2.0"
    assert DOCUMENT_CHAPTER_COVERAGE_POLICY_VERSION == "document-chapter-coverage-2.0"
    assert DOCUMENT_CHAPTER_CACHE_NAMESPACE == (
        "document-chapter-composition-json-object-2-0"
    )


@pytest.mark.parametrize(
    "profile", ["short_explainer", "course", "academic_report", "speech"]
)
def test_offline_mock_supports_all_profiles(tmp_path: Path, profile: str) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input(profile)
    transport = DeterministicDocumentChapterMockTransport()

    result = compose_document_chapters_offline_mock(
        output_root, "DEV01", payload, transport=transport
    )

    assert result.business_call_count == 1
    assert result.supplier_request_count == 1
    assert result.cache_hit_count == 0
    assert transport.call_count == 1
    chapter = result.chapters[0].chapter
    assert chapter["sections"][0]["heading"] is None
    assert [block["type"] for block in chapter["sections"][0]["blocks"]] == [
        "paragraph"
    ]
    assert not (output_root / "DEV01" / "content" / "document-v2" / "previews").exists()


def test_single_window_call_cache_is_zero_transport_on_rerun(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    first = compose_document_chapters_offline_mock(output_root, "DEV01", payload)
    before = {path: path.read_bytes() for path in first.call_paths}

    second = compose_document_chapters_offline_mock(
        output_root, "DEV01", payload, transport=FailIfCalledTransport()
    )

    assert second.all_cache_hit
    assert second.supplier_request_count == 0
    assert {path: path.read_bytes() for path in second.call_paths} == before


def test_single_structure_correction_is_audited(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    result = compose_document_chapters_offline_mock(
        output_root, "DEV01", payload, transport=RetryOnceTransport()
    )

    call_path = result.call_paths[0]
    call = json.loads(call_path.read_text(encoding="utf-8"))
    audit = json.loads(
        call_path.with_name("doc-c0001-single-validation-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert call["attempt_count"] == 2
    assert audit["status"] == "completed_after_retry"
    assert len(audit["attempts"]) == 1


def test_deterministic_multiwindow_calls_and_merge(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    transport = DeterministicDocumentChapterMockTransport()

    result = compose_document_chapters_offline_mock(
        output_root,
        "DEV01",
        payload,
        transport=transport,
        max_window_events=2,
    )

    assert result.business_call_count == 3
    assert result.supplier_request_count == 3
    assert transport.call_count == 3
    assert [path.stem for path in result.call_paths] == [
        "doc-c0001-w001",
        "doc-c0001-w002",
        "doc-c0001-merge",
    ]
    assert len(result.chapters) == 1


def test_split_is_complete_ordered_and_marks_oversize_event() -> None:
    chapter = _composition_input()["chapters"][0]
    windows = split_document_chapter_events(
        chapter, max_window_events=2, max_window_chars=1
    )

    assert [event_id for window in windows for event_id in window.event_ids] == [
        "a0001",
        "a0002",
        "a0003",
        "a0004",
    ]
    assert all(window.oversize_single_event for window in windows)
    assert [window.window_id for window in windows] == [
        "c0001-w001",
        "c0001-w002",
        "c0001-w003",
        "c0001-w004",
    ]


def test_partial_window_failure_writes_audit_but_no_merge_or_preview(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()

    with pytest.raises(DocumentChapterCompositionError):
        compose_document_chapters_offline_mock(
            output_root,
            "DEV01",
            payload,
            transport=FailSecondWindowTransport(),
            max_window_events=2,
        )

    calls = (
        output_root
        / "DEV01"
        / "content"
        / "document-v2"
        / "attempts"
        / payload["input_id"]
        / "chapters"
        / "c0001"
        / "calls"
    )
    audit = json.loads(
        (calls / "doc-c0001-w002-validation-audit.json").read_text(encoding="utf-8")
    )
    assert audit["status"] == "failed"
    assert len(audit["attempts"]) == 2
    assert not (calls / "doc-c0001-merge.json").exists()
    assert not (output_root / "DEV01" / "content" / "document-v2" / "previews").exists()


def test_merge_must_reference_every_deterministic_window(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()

    with pytest.raises(DocumentChapterCompositionError, match="每个确定性窗口"):
        compose_document_chapters_offline_mock(
            output_root,
            "DEV01",
            payload,
            max_window_events=1,
        )

    audit_path = (
        output_root
        / "DEV01"
        / "content"
        / "document-v2"
        / "attempts"
        / payload["input_id"]
        / "chapters"
        / "c0001"
        / "calls"
        / "doc-c0001-merge-validation-audit.json"
    )
    assert json.loads(audit_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_corrupted_cache_is_not_reused(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    first = compose_document_chapters_offline_mock(output_root, "DEV01", payload)
    call_path = first.call_paths[0]
    call_path.write_text("{broken", encoding="utf-8")
    transport = DeterministicDocumentChapterMockTransport()

    second = compose_document_chapters_offline_mock(
        output_root, "DEV01", payload, transport=transport
    )

    assert not second.all_cache_hit
    assert transport.call_count == 1
    assert json.loads(call_path.read_text(encoding="utf-8"))["schema_version"] == "1.0"


def test_explicit_mock_config_never_loads_dotenv(tmp_path: Path, monkeypatch: Any) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("R3不得读取.env")

    monkeypatch.setattr("video_doc.llm_adapter.load_llm_config", forbidden)
    result = compose_document_chapters_offline_mock(output_root, "DEV01", payload)
    assert len(result.chapters) == 1


def test_unknown_profile_fails_before_any_call(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    payload["document_profile"] = "generic"
    _refresh_input(payload)
    transport = DeterministicDocumentChapterMockTransport()

    with pytest.raises(DocumentChapterCompositionError, match="profile"):
        compose_document_chapters_offline_mock(
            output_root, "DEV01", payload, transport=transport
        )
    assert transport.call_count == 0


def test_validator_accepts_all_optional_block_shapes() -> None:
    payload = _composition_input()
    response = _response(payload)
    paragraph = response["sections"][0]["blocks"][0]
    paragraph["role"] = "case"
    source = {
        "content_origin": "stage9_v1",
        "source_content_paths": ["chapters[c0001].key_points[0]"],
        "source_event_ids": ["a0003"],
    }
    response["sections"][0]["blocks"].extend(
        [
            {
                "block_id": "c0001-s001-list002",
                "type": "list",
                "role": "steps",
                "list_kind": "numbered",
                "items": [
                    {"item_id": "c0001-s001-list002-i001", "text": "ModelA需要2步。", **source},
                    {
                        "item_id": "c0001-s001-list002-i002",
                        "text": "ModelB可能更快。",
                        "content_origin": "stage9_v1",
                        "source_content_paths": ["chapters[c0001].key_points[1]"],
                        "source_event_ids": ["a0003"],
                    },
                ],
            },
            {
                "block_id": "c0001-s001-cmp003",
                "type": "comparison",
                "role": "comparison",
                "columns": ["ModelA", "ModelB"],
                "rows": [
                    {
                        "row_id": "c0001-s001-cmp003-r001",
                        "dimension": "步骤",
                        "cells": [
                            {"text": "ModelA需要2步。", **source},
                            {
                                "text": "ModelB可能更快。",
                                "content_origin": "stage9_v1",
                                "source_content_paths": ["chapters[c0001].key_points[1]"],
                                "source_event_ids": ["a0003"],
                            },
                        ],
                    },
                    {
                        "row_id": "c0001-s001-cmp003-r002",
                        "dimension": "结论",
                        "cells": [
                            {"text": "ModelA需要2步。", **source},
                            {
                                "text": "ModelB可能更快。",
                                "content_origin": "stage9_v1",
                                "source_content_paths": ["chapters[c0001].key_points[1]"],
                                "source_event_ids": ["a0003"],
                            },
                        ],
                    },
                ],
            },
            {
                "block_id": "c0001-s001-callout004",
                "type": "callout",
                "role": "conclusion",
                "text": "ModelB可能更快。",
                "content_origin": "stage9_v1",
                "source_content_paths": ["chapters[c0001].key_points[1]"],
                "source_event_ids": ["a0003"],
            },
        ]
    )

    validated = validate_chapter_composition_response(
        response, chapter_input=payload["chapters"][0], context=_context(payload)
    )
    assert len(validated.block_evidence) == 4


def test_comparison_labels_cannot_introduce_new_entity() -> None:
    payload = _composition_input()
    response = _response(payload)
    paragraph = response["sections"][0]["blocks"][0]
    source = {
        "content_origin": "stage9_v1",
        "source_content_paths": ["chapters[c0001].key_points[0]"],
        "source_event_ids": ["a0003"],
    }
    response["sections"][0]["blocks"] = [
        paragraph,
        {
            "block_id": "c0001-s001-cmp002",
            "type": "comparison",
            "role": "comparison",
            "columns": ["ModelA", "ModelZ"],
            "rows": [
                {
                    "row_id": "c0001-s001-cmp002-r001",
                    "dimension": "步骤",
                    "cells": [
                        {"text": "ModelA需要2步。", **source},
                        {"text": "ModelA需要2步。", **source},
                    ],
                },
                {
                    "row_id": "c0001-s001-cmp002-r002",
                    "dimension": "方法",
                    "cells": [
                        {"text": "ModelA需要2步。", **source},
                        {"text": "ModelA需要2步。", **source},
                    ],
                },
            ],
        },
    ]

    with pytest.raises(DocumentChapterCompositionError, match="拉丁实体"):
        validate_chapter_composition_response(
            response, chapter_input=payload["chapters"][0], context=_context(payload)
        )


@pytest.mark.parametrize(
    "replacement,match",
    [
        ("样本包含15人。", "数字/比例"),
        ("使用ModelZ处理。", "拉丁实体"),
        ("结果必然成立。", "否定/模态/建议"),
    ],
)
def test_high_risk_fact_drift_is_rejected(replacement: str, match: str) -> None:
    payload = _composition_input()
    response = _response(payload)
    response["sections"][0]["blocks"][0]["text"] = replacement

    with pytest.raises(DocumentChapterCompositionError, match=match):
        validate_chapter_composition_response(
            response, chapter_input=payload["chapters"][0], context=_context(payload)
        )


def test_wulun_is_not_treated_as_single_character_negation() -> None:
    payload = _composition_input()
    response = _response(payload)
    response["sections"][0]["blocks"][0]["text"] = (
        "无论采用哪种方法，最后都要总结两种方法的差异。"
    )

    validate_chapter_composition_response(
        response, chapter_input=payload["chapters"][0], context=_context(payload)
    )


def test_cross_chapter_or_unknown_event_is_rejected() -> None:
    payload = _composition_input()
    response = _response(payload)
    response["title"]["source_event_ids"] = ["a9999"]

    with pytest.raises(DocumentChapterCompositionError, match="章外事件"):
        validate_chapter_composition_response(
            response, chapter_input=payload["chapters"][0], context=_context(payload)
        )


def test_coverage_omitted_duplicate_requires_compatible_evidence() -> None:
    payload = _composition_input()
    response = _response(payload)
    item = response["coverage"]["items"][1]
    item["status"] = "omitted_duplicate"
    paragraph = response["sections"][0]["blocks"][0]
    paragraph["source_event_ids"] = ["a0001", "a0003"]

    with pytest.raises(DocumentChapterCompositionError, match="缺少相容章内证据"):
        validate_chapter_composition_response(
            response, chapter_input=payload["chapters"][0], context=_context(payload)
        )


def test_coverage_order_and_explicit_representation_are_enforced() -> None:
    payload = _composition_input()
    response = _response(payload)
    response["coverage"]["items"][0], response["coverage"]["items"][1] = (
        response["coverage"]["items"][1],
        response["coverage"]["items"][0],
    )

    with pytest.raises(DocumentChapterCompositionError, match="顺序"):
        validate_chapter_composition_response(
            response, chapter_input=payload["chapters"][0], context=_context(payload)
        )


def test_invalid_block_type_is_rejected() -> None:
    payload = _composition_input()
    response = _response(payload)
    response["sections"][0]["blocks"][0]["type"] = "table"

    with pytest.raises(DocumentChapterCompositionError, match="type无效"):
        validate_chapter_composition_response(
            response, chapter_input=payload["chapters"][0], context=_context(payload)
        )
