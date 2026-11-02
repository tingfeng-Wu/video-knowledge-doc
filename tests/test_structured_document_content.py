from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from video_doc.structured_document_content import (
    DOCUMENT_BLOCK_POLICY_VERSION,
    DOCUMENT_BLOCK_TYPES,
    DOCUMENT_CONTENT_OUTPUT_LANGUAGE,
    DOCUMENT_CONTENT_VALIDATOR_VERSION,
    DOCUMENT_COVERAGE_POLICY_VERSION,
    DOCUMENT_EVIDENCE_POLICY_VERSION,
    DOCUMENT_PROFILE_POLICY_VERSION,
    DOCUMENT_PROFILES,
    DOCUMENT_SOURCE_PATH_VERSION,
    STRUCTURED_DOCUMENT_CONTENT_SCHEMA_VERSION,
    DocumentContentValidationContext,
    StructuredDocumentContentError,
    attach_structured_document_content_id,
    document_content_versions,
    structured_document_content_identity_sha256,
    validate_structured_document_content,
)


def _evidence(
    text: str,
    paths: list[str],
    events: list[str],
    *,
    origin: str = "stage9_v1",
) -> dict[str, Any]:
    return {
        "text": text,
        "content_origin": origin,
        "source_content_paths": paths,
        "source_event_ids": events,
    }


def _context() -> DocumentContentValidationContext:
    known = frozenset(
        {
            "chapters[c0001].title",
            "chapters[c0001].summary",
            "chapters[c0001].key_points[0]",
            "chapters[c0001].key_points[1]",
            "chapters[c0001].keywords[0]",
            "chapters[c0002].title",
            "chapters[c0002].summary",
            "chapters[c0002].key_points[0]",
            "chapters[c0002].keywords[0]",
        }
    )
    required = frozenset(path for path in known if ".keywords[" not in path)
    return DocumentContentValidationContext(
        known_source_paths=known,
        required_core_paths=required,
        chapter_event_ids={
            "c0001": ("a0001", "a0002", "a0003"),
            "c0002": ("a0004", "a0005"),
        },
        event_times={
            "a0001": (0.0, 10.0),
            "a0002": (10.0, 20.0),
            "a0003": (20.0, 29.0),
            "a0004": (30.0, 45.0),
            "a0005": (45.0, 59.0),
        },
    )


def _paragraph(
    block_id: str,
    text: str,
    paths: list[str],
    events: list[str],
    *,
    role: str = "explanation",
    origin: str = "stage9_v1",
) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "type": "paragraph",
        "role": role,
        "text": text,
        "content_origin": origin,
        "source_content_paths": paths,
        "source_event_ids": events,
    }


def _base_without_id(profile: str = "short_explainer") -> dict[str, Any]:
    chapter_one_block = _paragraph(
        "c0001-s001-p001",
        "焦虑表现与影响需要连续解释。",
        [
            "chapters[c0001].title",
            "chapters[c0001].summary",
            "chapters[c0001].key_points[0]",
            "chapters[c0001].key_points[1]",
        ],
        ["a0001", "a0002", "a0003"],
    )
    chapter_two_block = _paragraph(
        "c0002-s001-p001",
        "应对方式从理解过渡到行动。",
        [
            "chapters[c0002].title",
            "chapters[c0002].summary",
            "chapters[c0002].key_points[0]",
        ],
        ["a0004", "a0005"],
    )
    overview = _paragraph(
        "overview-p001",
        "视频先解释焦虑，再讨论应对方式。",
        ["chapters[c0001].summary", "chapters[c0002].summary"],
        ["a0001", "a0004"],
        role="overview",
    )
    conclusion = _paragraph(
        "conclusion-p001",
        "核心结论是理解问题并建立应对资源。",
        ["chapters[c0001].summary", "chapters[c0002].summary"],
        ["a0002", "a0005"],
        role="conclusion",
    )
    blocks = [overview, chapter_one_block, chapter_two_block, conclusion]
    evidence_index = []
    times = _context().event_times
    for label, block in zip(
        ("视频概述", "第1章第1段", "第2章第1段", "全文结论"), blocks
    ):
        events = block["source_event_ids"]
        evidence_index.append(
            {
                "block_id": block["block_id"],
                "location_label": label,
                "source_content_paths": block["source_content_paths"],
                "source_event_ids": events,
                "start": min(times[event_id][0] for event_id in events),
                "end": max(times[event_id][1] for event_id in events),
            }
        )
    required_paths = sorted(_context().required_core_paths)
    targets = {
        "c0001": "c0001-s001-p001",
        "c0002": "c0002-s001-p001",
    }
    return {
        "schema_version": "2.0",
        "task_id": "SYNTH01",
        "language": "zh-CN",
        "strategy": "llm",
        "document_profile": profile,
        "source_content_bundle": {
            "schema_version": "1.0",
            "result_id": "content-aaaaaaaaaaaa",
            "canonical_sha256": "a" * 64,
            "content_file_sha256": "b" * 64,
            "execution_report_file_sha256": "c" * 64,
            "manifest_file_sha256": "d" * 64,
            "input_manifest_file_sha256": "e" * 64,
        },
        "source_chapter_plan": {
            "schema_version": "1.0",
            "result_id": "primary-aaaaaaaaaaaa",
            "canonical_sha256": "f" * 64,
            "chapter_plan_file_sha256": "1" * 64,
            "execution_report_file_sha256": "2" * 64,
            "manifest_file_sha256": "3" * 64,
        },
        "source_timeline": {
            "schema_version": "1.0",
            "canonical_sha256": "4" * 64,
            "timeline_file_sha256": "5" * 64,
            "event_count": 5,
        },
        "chaptering_provenance": {
            "status": "success",
            "result_source": "primary",
            "quality_level": "normal",
            "execution_report_sha256": "6" * 64,
        },
        "title": _evidence(
            "理解焦虑并建立应对资源",
            ["chapters[c0001].title", "chapters[c0002].title"],
            ["a0001", "a0004"],
        ),
        "overview": [overview],
        "chapters": [
            {
                "chapter_id": "c0001",
                "index": 1,
                "start": 0.0,
                "end": 30.0,
                "planned_title": "Anxiety",
                "title": _evidence(
                    "焦虑的表现与影响",
                    ["chapters[c0001].title"],
                    ["a0001"],
                ),
                "keywords": [
                    _evidence(
                        "焦虑",
                        ["chapters[c0001].keywords[0]"],
                        ["a0001"],
                    )
                ],
                "sections": [
                    {
                        "section_id": "c0001-s001",
                        "heading": None,
                        "blocks": [chapter_one_block],
                    }
                ],
            },
            {
                "chapter_id": "c0002",
                "index": 2,
                "start": 30.0,
                "end": 60.0,
                "planned_title": "Coping",
                "title": _evidence(
                    "建立应对资源",
                    ["chapters[c0002].title"],
                    ["a0004"],
                ),
                "keywords": [],
                "sections": [
                    {
                        "section_id": "c0002-s001",
                        "heading": None,
                        "blocks": [chapter_two_block],
                    }
                ],
            },
        ],
        "overall_conclusion": [conclusion],
        "coverage": {
            "items": [
                {
                    "source_content_path": path,
                    "status": "represented",
                    "target_block_ids": [targets[path[9:14]]],
                }
                for path in required_paths
            ]
        },
        "evidence_index": evidence_index,
        "generation": {
            "status": "success",
            "mode": "document-composition-mock",
            "backend": "mock",
            "model": "deterministic-fixture",
            "temperature": 0,
            "chapter_prompt_version": "document-chapter-prompt-2.0",
            "global_prompt_version": "document-global-prompt-2.0",
            "content_policy_version": "document-asr-evidence-only-2.0",
            "validator_version": DOCUMENT_CONTENT_VALIDATOR_VERSION,
            "api_request_count": 0,
        },
        "content_versions": document_content_versions(),
    }


def _document(profile: str = "short_explainer") -> dict[str, Any]:
    return attach_structured_document_content_id(_base_without_id(profile))


def _refresh_id(document: dict[str, Any]) -> dict[str, Any]:
    return attach_structured_document_content_id(document)


def test_r1_contract_versions_profiles_and_blocks_are_frozen() -> None:
    assert STRUCTURED_DOCUMENT_CONTENT_SCHEMA_VERSION == "2.0"
    assert DOCUMENT_CONTENT_OUTPUT_LANGUAGE == "zh-CN"
    assert DOCUMENT_PROFILE_POLICY_VERSION == "document-profile-policy-2.0"
    assert DOCUMENT_BLOCK_POLICY_VERSION == "document-block-policy-2.0"
    assert DOCUMENT_SOURCE_PATH_VERSION == "document-source-path-2.0"
    assert DOCUMENT_COVERAGE_POLICY_VERSION == "document-v1-coverage-2.0"
    assert DOCUMENT_EVIDENCE_POLICY_VERSION == "document-block-evidence-2.0"
    assert DOCUMENT_CONTENT_VALIDATOR_VERSION == "structured-document-content-validator-2.0"
    assert DOCUMENT_PROFILES == {
        "short_explainer",
        "course",
        "academic_report",
        "speech",
    }
    assert DOCUMENT_BLOCK_TYPES == {"paragraph", "list", "comparison", "callout"}


@pytest.mark.parametrize("profile", sorted(DOCUMENT_PROFILES))
def test_r1_all_four_profiles_share_one_schema(profile: str) -> None:
    validate_structured_document_content(_document(profile), context=_context())


def test_r1_short_chapter_with_only_paragraphs_and_no_optional_keywords_is_valid() -> None:
    document = _document()
    assert document["chapters"][1]["keywords"] == []
    assert document["chapters"][1]["sections"][0]["heading"] is None
    validate_structured_document_content(document, context=_context())


def test_r1_content_identity_is_deterministic_and_excludes_only_content_id() -> None:
    payload = _base_without_id()
    first = attach_structured_document_content_id(payload)
    second = attach_structured_document_content_id(payload)
    assert first == second
    assert first["content_id"] == (
        f"document-content-{structured_document_content_identity_sha256(first)[:12]}"
    )
    assert "content_id" not in payload


def test_r1_rejects_unknown_profile_and_extra_schema_field() -> None:
    document = _document()
    document["document_profile"] = "generic"
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="四种profile"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    document["invented"] = True
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="字段集合"):
        validate_structured_document_content(document, context=_context())


def test_r1_rejects_unknown_block_type_and_unstable_block_id() -> None:
    document = _document()
    block = document["chapters"][0]["sections"][0]["blocks"][0]
    block["type"] = "quote"
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="type无效"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    document["chapters"][0]["sections"][0]["blocks"][0]["block_id"] = "random"
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="block_id"):
        validate_structured_document_content(document, context=_context())


def test_r1_rejects_chapter_event_crossing_and_event_order_drift() -> None:
    document = _document()
    document["chapters"][0]["title"]["source_event_ids"] = ["a0004"]
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="章外事件"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    document["overview"][0]["source_event_ids"] = ["a0004", "a0001"]
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="时间轴顺序"):
        validate_structured_document_content(document, context=_context())


def test_r1_rejects_source_path_crossing_and_origin_mismatch() -> None:
    document = _document()
    block = document["chapters"][0]["sections"][0]["blocks"][0]
    block["source_content_paths"] = ["chapters[c0002].summary"]
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="其他章节"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    block = document["chapters"][0]["sections"][0]["blocks"][0]
    block["content_origin"] = "chapter_asr"
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="不得伪造"):
        validate_structured_document_content(document, context=_context())


def test_r1_chapter_asr_origin_requires_events_but_no_v1_path() -> None:
    document = _document()
    block = document["chapters"][0]["sections"][0]["blocks"][0]
    block["content_origin"] = "chapter_asr"
    block["source_content_paths"] = []
    for item in document["coverage"]["items"]:
        if item["target_block_ids"] == ["c0001-s001-p001"]:
            item["status"] = "omitted_duplicate"
    document["evidence_index"][1]["source_content_paths"] = []
    document = _refresh_id(document)
    validate_structured_document_content(document, context=_context())


def test_r1_rejects_incomplete_duplicate_or_cross_chapter_coverage() -> None:
    document = _document()
    document["coverage"]["items"].pop()
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="完整且唯一"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    document["coverage"]["items"][0]["target_block_ids"] = ["c0002-s001-p001"]
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="同章"):
        validate_structured_document_content(document, context=_context())


def test_r1_omitted_duplicate_must_still_point_to_same_chapter_block() -> None:
    document = _document()
    document["coverage"]["items"][0]["status"] = "omitted_duplicate"
    document = _refresh_id(document)
    validate_structured_document_content(document, context=_context())

    document["coverage"]["items"][0]["target_block_ids"] = ["overview-p001"]
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="同章"):
        validate_structured_document_content(document, context=_context())


def test_r1_represented_coverage_requires_explicit_source_path_on_target() -> None:
    document = _document()
    block = document["chapters"][0]["sections"][0]["blocks"][0]
    block["source_content_paths"].remove("chapters[c0001].title")
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="显式引用"):
        validate_structured_document_content(document, context=_context())


def test_r1_evidence_index_must_exactly_match_blocks_and_derived_time() -> None:
    document = _document()
    document["evidence_index"][1]["source_event_ids"] = ["a0001"]
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="内容块证据"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    document["evidence_index"][1]["end"] = 999.0
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="确定性派生"):
        validate_structured_document_content(document, context=_context())


def test_r1_rejects_content_id_drift_and_version_drift() -> None:
    document = _document()
    document["content_id"] = "document-content-000000000000"
    with pytest.raises(StructuredDocumentContentError, match="规范内容身份"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    document["content_versions"]["block_policy"] = "future"
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="冻结版本"):
        validate_structured_document_content(document, context=_context())


def test_r1_rejects_non_chinese_core_text_and_invalid_generation() -> None:
    document = _document()
    document["title"]["text"] = "English only"
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="中文字符"):
        validate_structured_document_content(document, context=_context())

    document = _document()
    document["generation"]["api_request_count"] = 1
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="必须为0"):
        validate_structured_document_content(document, context=_context())


def test_r1_list_comparison_and_callout_shapes_are_supported() -> None:
    document = _document()
    section = document["chapters"][0]["sections"][0]
    section["blocks"] = [
        section["blocks"][0],
        {
            "block_id": "c0001-s001-list001",
            "type": "list",
            "role": "facts",
            "list_kind": "bullet",
            "items": [
                {
                    "item_id": "c0001-s001-list001-i001",
                    **_evidence(
                        "表现一",
                        ["chapters[c0001].key_points[0]"],
                        ["a0001"],
                    ),
                },
                {
                    "item_id": "c0001-s001-list001-i002",
                    **_evidence(
                        "表现二",
                        ["chapters[c0001].key_points[1]"],
                        ["a0002"],
                    ),
                },
            ],
        },
        {
            "block_id": "c0001-s001-cmp001",
            "type": "comparison",
            "role": "comparison",
            "columns": ["正常焦虑", "焦虑障碍"],
            "rows": [
                {
                    "row_id": "c0001-s001-cmp001-r001",
                    "dimension": "触发条件",
                    "cells": [
                        _evidence("存在压力", ["chapters[c0001].summary"], ["a0001"]),
                        _evidence("缺少威胁", ["chapters[c0001].summary"], ["a0002"]),
                    ],
                },
                {
                    "row_id": "c0001-s001-cmp001-r002",
                    "dimension": "影响程度",
                    "cells": [
                        _evidence("帮助应对", ["chapters[c0001].summary"], ["a0001"]),
                        _evidence("影响生活", ["chapters[c0001].summary"], ["a0003"]),
                    ],
                },
            ],
        },
        {
            "block_id": "c0001-s001-callout001",
            "type": "callout",
            "role": "conclusion",
            "text": "需要区分正常反应与障碍。",
            "content_origin": "stage9_v1",
            "source_content_paths": ["chapters[c0001].summary"],
            "source_event_ids": ["a0002", "a0003"],
        },
    ]
    times = _context().event_times
    for block in section["blocks"][1:]:
        if block["type"] == "list":
            paths = list(
                dict.fromkeys(
                    path for item in block["items"] for path in item["source_content_paths"]
                )
            )
            events = list(
                dict.fromkeys(
                    event for item in block["items"] for event in item["source_event_ids"]
                )
            )
        elif block["type"] == "comparison":
            cells = [cell for row in block["rows"] for cell in row["cells"]]
            paths = list(
                dict.fromkeys(path for cell in cells for path in cell["source_content_paths"])
            )
            events = list(
                dict.fromkeys(event for cell in cells for event in cell["source_event_ids"])
            )
        else:
            paths = block["source_content_paths"]
            events = block["source_event_ids"]
        document["evidence_index"].append(
            {
                "block_id": block["block_id"],
                "location_label": "第1章结构块",
                "source_content_paths": paths,
                "source_event_ids": events,
                "start": min(times[event][0] for event in events),
                "end": max(times[event][1] for event in events),
            }
        )
    document = _refresh_id(document)
    validate_structured_document_content(document, context=_context())


def test_r1_rejects_more_than_one_optional_comparison_or_callout_per_chapter() -> None:
    document = _document()
    section = document["chapters"][0]["sections"][0]
    callout = {
        "block_id": "c0001-s001-callout001",
        "type": "callout",
        "role": "conclusion",
        "text": "结论一。",
        "content_origin": "stage9_v1",
        "source_content_paths": ["chapters[c0001].summary"],
        "source_event_ids": ["a0001"],
    }
    second = deepcopy(callout)
    second["block_id"] = "c0001-s001-callout002"
    second["text"] = "结论二。"
    section["blocks"].extend([callout, second])
    document = _refresh_id(document)
    with pytest.raises(StructuredDocumentContentError, match="最多包含1个callout"):
        validate_structured_document_content(document, context=_context())
