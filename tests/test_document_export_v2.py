from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from video_doc.document_export_v2 import (
    DOCUMENT_V2_DOCX_RENDERER_VERSION,
    DOCUMENT_V2_EXPORT_CURRENT_SCHEMA_VERSION,
    DOCUMENT_V2_JSON_RENDERER_VERSION,
    DOCUMENT_V2_MARKDOWN_RENDERER_VERSION,
    DOCUMENT_V2_VIEW_VERSION,
    DocumentExportV2Error,
    build_document_view_v2,
    load_current_document_export_v2,
    load_authoritative_document_content_v2,
    publish_document_export_v2,
    render_document_docx_v2,
    render_document_markdown_v2,
    render_structured_document_json_v2,
    semantic_projection_from_document_v2,
    semantic_projection_from_view_v2,
    validate_document_docx_v2,
    validate_document_export_group_v2,
)
from video_doc.cli import main as cli_main
from video_doc.structured_document_content import (
    DOCUMENT_CONTENT_VALIDATOR_VERSION,
    attach_structured_document_content_id,
    canonical_sha256,
    document_content_versions,
    validate_structured_document_content,
)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _evidence(text: str, paths: list[str], events: list[str]) -> dict[str, Any]:
    return {
        "text": text,
        "content_origin": "stage9_v1",
        "source_content_paths": paths,
        "source_event_ids": events,
    }


def _document() -> dict[str, Any]:
    paragraph = {
        "block_id": "c0001-s001-p001",
        "type": "paragraph",
        "role": "explanation",
        **_evidence(
            "先用连续段落解释核心概念。",
            [
                "chapters[c0001].title",
                "chapters[c0001].summary",
                "chapters[c0001].key_points[0]",
            ],
            ["a0001"],
        ),
    }
    list_block = {
        "block_id": "c0001-s001-list001",
        "type": "list",
        "role": "steps",
        "list_kind": "numbered",
        "items": [
            {
                "item_id": "c0001-s001-list001-i001",
                **_evidence(
                    "第一步核对现象。", ["chapters[c0001].key_points[1]"], ["a0002"]
                ),
            },
            {
                "item_id": "c0001-s001-list001-i002",
                **_evidence(
                    "第二步理解影响。", ["chapters[c0001].key_points[2]"], ["a0003"]
                ),
            },
        ],
    }
    comparison = {
        "block_id": "c0001-s001-cmp001",
        "type": "comparison",
        "role": "comparison",
        "columns": ["现象", "影响"],
        "rows": [
            {
                "row_id": "c0001-s001-cmp001-r001",
                "dimension": "表现",
                "cells": [
                    _evidence("紧张", ["chapters[c0001].key_points[3]"], ["a0001"]),
                    _evidence("回避", ["chapters[c0001].key_points[3]"], ["a0002"]),
                ],
            },
            {
                "row_id": "c0001-s001-cmp001-r002",
                "dimension": "结果",
                "cells": [
                    _evidence("孤立", ["chapters[c0001].key_points[4]"], ["a0003"]),
                    _evidence("受限", ["chapters[c0001].key_points[4]"], ["a0004"]),
                ],
            },
        ],
    }
    callout = {
        "block_id": "c0001-s001-callout001",
        "type": "callout",
        "role": "conclusion",
        **_evidence(
            "本节结论必须保持来源原意。", ["chapters[c0001].summary"], ["a0004"]
        ),
    }
    overview = {
        "block_id": "overview-p001",
        "type": "paragraph",
        "role": "overview",
        **_evidence("视频概述核心问题和展开顺序。", ["chapters[c0001].summary"], ["a0001"]),
    }
    conclusion = {
        "block_id": "conclusion-p001",
        "type": "paragraph",
        "role": "conclusion",
        **_evidence("全文结论回到理解和应对。", ["chapters[c0001].summary"], ["a0004"]),
    }
    evidence_rows = [
        (overview, "视频概述", ["chapters[c0001].summary"], ["a0001"], 0.0, 10.0),
        (
            paragraph,
            "第1章第1段",
            [
                "chapters[c0001].title",
                "chapters[c0001].summary",
                "chapters[c0001].key_points[0]",
            ],
            ["a0001"],
            0.0,
            10.0,
        ),
        (
            list_block,
            "第1章步骤列表",
            ["chapters[c0001].key_points[1]", "chapters[c0001].key_points[2]"],
            ["a0002", "a0003"],
            10.0,
            30.0,
        ),
        (
            comparison,
            "第1章对比表",
            ["chapters[c0001].key_points[3]", "chapters[c0001].key_points[4]"],
            ["a0001", "a0002", "a0003", "a0004"],
            0.0,
            40.0,
        ),
        (callout, "第1章结论", ["chapters[c0001].summary"], ["a0004"], 30.0, 40.0),
        (conclusion, "全文结论", ["chapters[c0001].summary"], ["a0004"], 30.0, 40.0),
    ]
    payload = {
        "schema_version": "2.0",
        "task_id": "SYNTH01",
        "language": "zh-CN",
        "strategy": "llm",
        "document_profile": "short_explainer",
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
            "event_count": 4,
        },
        "chaptering_provenance": {
            "status": "success",
            "result_source": "primary",
            "quality_level": "normal",
            "execution_report_sha256": "6" * 64,
        },
        "title": _evidence("理解问题并形成连续文档", ["chapters[c0001].title"], ["a0001"]),
        "overview": [overview],
        "chapters": [
            {
                "chapter_id": "c0001",
                "index": 1,
                "start": 0.0,
                "end": 40.0,
                "planned_title": "Source chapter",
                "title": _evidence("问题、影响与应对", ["chapters[c0001].title"], ["a0001"]),
                "keywords": [
                    _evidence("核心问题", ["chapters[c0001].keywords[0]"], ["a0001"])
                ],
                "sections": [
                    {
                        "section_id": "c0001-s001",
                        "heading": None,
                        "blocks": [paragraph, list_block, comparison, callout],
                    }
                ],
            }
        ],
        "overall_conclusion": [conclusion],
        "coverage": {
            "items": [
                {
                    "source_content_path": path,
                    "status": "represented",
                    "target_block_ids": ["c0001-s001-p001"],
                }
                for path in (
                    "chapters[c0001].title",
                    "chapters[c0001].summary",
                    "chapters[c0001].key_points[0]",
                )
            ]
        },
        "evidence_index": [
            {
                "block_id": block["block_id"],
                "location_label": label,
                "source_content_paths": paths,
                "source_event_ids": events,
                "start": start,
                "end": end,
            }
            for block, label, paths, events, start, end in evidence_rows
        ],
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
    document = attach_structured_document_content_id(payload)
    validate_structured_document_content(document)
    return document


def _write_authority(output_root: Path, document: dict[str, Any]) -> Path:
    content_id = document["content_id"]
    root = output_root / document["task_id"] / "content" / "document-v2"
    group = root / "formal-results" / content_id
    group.mkdir(parents=True)
    versions = {
        "global_response_schema": "2.0",
        "global_prompt": "fixture-global-2.0",
        "global_validator": "fixture-validator-2.0",
        "global_cache_namespace": "fixture-cache-2.0",
        "content_policy": "document-asr-evidence-only-2.0",
        "preview_publisher": "document-preview-publisher-2.0",
    }
    document_sha = canonical_sha256(document)
    report = {
        "schema_version": "1.0",
        "status": "success",
        "task_id": document["task_id"],
        "content_id": content_id,
        "input_id": "composition-input-aaaaaaaaaaaa",
        "document_profile": document["document_profile"],
        "document_sha256": document_sha,
        "chapter_count": len(document["chapters"]),
        "business_call_count": 0,
        "supplier_request_count": 0,
        "cache_hit_count": 2,
        "api_request_count": document["generation"]["api_request_count"],
        "versions": versions,
    }
    document_raw = _json_bytes(document)
    report_raw = _json_bytes(report)
    manifest = {
        "schema_version": "1.0",
        "status": "success",
        "task_id": document["task_id"],
        "content_id": content_id,
        "document_profile": document["document_profile"],
        "input": {
            "input_id": report["input_id"],
            "composition_input_sha256": "7" * 64,
            "source_content_result_id": document["source_content_bundle"]["result_id"],
            "source_chapter_plan_result_id": document["source_chapter_plan"]["result_id"],
            "source_timeline_sha256": document["source_timeline"]["canonical_sha256"],
        },
        "document_sha256": document_sha,
        "chapter_count": len(document["chapters"]),
        "calls": {
            field: report[field]
            for field in (
                "business_call_count",
                "supplier_request_count",
                "cache_hit_count",
                "api_request_count",
            )
        },
        "content_versions": document["content_versions"],
        "preview_versions": versions,
        "files": {
            "document-content.json": {
                "sha256": _sha256(document_raw),
                "bytes": len(document_raw),
                "mime_type": "application/json",
            },
            "execution-report.json": {
                "sha256": _sha256(report_raw),
                "bytes": len(report_raw),
                "mime_type": "application/json",
            },
        },
    }
    (group / "document-content.json").write_bytes(document_raw)
    (group / "execution-report.json").write_bytes(report_raw)
    (group / "manifest.json").write_bytes(_json_bytes(manifest))
    current = {
        "schema_version": "1.0",
        "status": "accepted",
        "task_id": document["task_id"],
        "content_id": content_id,
        "group": f"formal-results/{content_id}",
        "document_sha256": document_sha,
        "publisher_version": "document-formal-publisher-2.0",
    }
    (root / "current.json").write_bytes(_json_bytes(current))
    return group


def test_p10_a1_versions_are_frozen() -> None:
    assert DOCUMENT_V2_VIEW_VERSION == "structured-document-view-2.2"
    assert DOCUMENT_V2_JSON_RENDERER_VERSION == "structured-document-json-2.0"
    assert DOCUMENT_V2_MARKDOWN_RENDERER_VERSION == "structured-document-markdown-2.3"


def test_p10_a2_versions_are_frozen() -> None:
    assert DOCUMENT_V2_DOCX_RENDERER_VERSION == "structured-document-docx-2.4"
    assert DOCUMENT_V2_EXPORT_CURRENT_SCHEMA_VERSION == "2.0"


def test_authoritative_loader_validates_current_group_report_and_manifest(tmp_path: Path) -> None:
    document = _document()
    group = _write_authority(tmp_path, document)
    loaded = load_authoritative_document_content_v2(tmp_path, "SYNTH01")
    assert loaded.group_path == group
    assert loaded.document == document
    assert loaded.document_sha256 == canonical_sha256(document)
    assert loaded.document_file_sha256 == _sha256(_json_bytes(document))


def test_loader_uses_current_and_never_scans_newer_directory(tmp_path: Path) -> None:
    document = _document()
    group = _write_authority(tmp_path, document)
    unrelated = group.parent / "document-content-ffffffffffff"
    unrelated.mkdir()
    (unrelated / "newer.txt").write_text("ignore", encoding="utf-8")
    loaded = load_authoritative_document_content_v2(tmp_path, "SYNTH01")
    assert loaded.group_path == group


@pytest.mark.parametrize("damage", ["current", "manifest_hash", "content_bytes", "extra_member"])
def test_loader_rejects_damaged_authority(tmp_path: Path, damage: str) -> None:
    document = _document()
    group = _write_authority(tmp_path, document)
    root = group.parent.parent
    if damage == "current":
        current = json.loads((root / "current.json").read_text(encoding="utf-8"))
        current["status"] = "preview"
        (root / "current.json").write_bytes(_json_bytes(current))
    elif damage == "manifest_hash":
        manifest = json.loads((group / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["document-content.json"]["sha256"] = "0" * 64
        (group / "manifest.json").write_bytes(_json_bytes(manifest))
    elif damage == "content_bytes":
        (group / "document-content.json").write_bytes(
            (group / "document-content.json").read_bytes() + b" "
        )
    else:
        (group / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DocumentExportV2Error):
        load_authoritative_document_content_v2(tmp_path, "SYNTH01")


def test_loader_rejects_unknown_document_field(tmp_path: Path) -> None:
    document = _document()
    document["unknown"] = True
    document = attach_structured_document_content_id(document)
    _write_authority(tmp_path, document)
    with pytest.raises(DocumentExportV2Error, match="Schema"):
        load_authoritative_document_content_v2(tmp_path, "SYNTH01")


def test_view_and_markdown_cover_all_four_blocks_without_reader_audit_appendix() -> None:
    document = _document()
    view = build_document_view_v2(document)
    assert [block.type for block in view.chapters[0].sections[0].blocks] == [
        "paragraph",
        "list",
        "comparison",
        "callout",
    ]
    assert view.chapters[0].sections[0].heading is None
    markdown = render_document_markdown_v2(view)
    assert "1. 第一步核对现象" in markdown
    assert "| 维度 | 现象 | 影响 |" in markdown
    assert "> 本节结论必须保持来源原意" in markdown
    assert "## 证据与时间索引" not in markdown
    assert "## 标题、检索字段与结构来源" not in markdown
    assert "## 覆盖审计" not in markdown
    assert "## 生成与来源信息" not in markdown
    assert "source_content_path" not in markdown
    assert "a0001" not in markdown
    assert "## 摘要" not in markdown
    assert "**全文关键词：** 核心问题" in markdown
    assert "关键词：核心问题" not in markdown.split("## 1.", maxsplit=1)[1]
    assert "第1章关键词" not in markdown


def test_json_is_exact_lossless_deterministic_serialization() -> None:
    document = _document()
    first = render_structured_document_json_v2(document)
    second = render_structured_document_json_v2(document)
    assert first == second
    assert json.loads(first) == document
    assert "export_id" not in json.loads(first)
    assert json.loads(first)["evidence_index"] == document["evidence_index"]
    assert json.loads(first)["coverage"] == document["coverage"]


def test_document_and_shared_view_have_identical_semantic_projection() -> None:
    document = _document()
    view = build_document_view_v2(document)
    assert semantic_projection_from_document_v2(document) == semantic_projection_from_view_v2(
        view
    )
    projection = semantic_projection_from_view_v2(view)
    assert projection["chapters"][0]["sections"][0]["blocks"][2]["rows"][1][
        "cells"
    ][1]["text"] == "受限"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (("success", "fallback", "unknown"), "技术fallback"),
        (("degraded", "emergency", "degraded"), "必须人工复核"),
    ],
)
def test_fallback_and_emergency_notices_are_prominent(
    state: tuple[str, str, str], expected: str
) -> None:
    document = _document()
    document["chaptering_provenance"].update(
        {"status": state[0], "result_source": state[1], "quality_level": state[2]}
    )
    document = attach_structured_document_content_id(document)
    view = build_document_view_v2(document)
    markdown = render_document_markdown_v2(view)
    assert expected in view.status_notice
    assert markdown.index(expected) < markdown.index("## 视频概述")


def test_empty_conclusion_remains_valid_and_is_not_invented() -> None:
    document = _document()
    document["overall_conclusion"] = []
    document["evidence_index"] = [
        row for row in document["evidence_index"] if row["block_id"] != "conclusion-p001"
    ]
    document = attach_structured_document_content_id(document)
    view = build_document_view_v2(document)
    assert view.overall_conclusion == ()
    assert "## 核心结论" not in render_document_markdown_v2(view)


def test_docx_is_deterministic_reopenable_and_preserves_all_block_types() -> None:
    view = build_document_view_v2(_document())
    first = render_document_docx_v2(view)
    second = render_document_docx_v2(view)
    assert first == second
    report = validate_document_docx_v2(first, view)
    assert report["status"] == "valid"
    assert report["table_count"] == 1
    assert report["chapter_count"] == 1
    from docx import Document
    from io import BytesIO

    reopened = Document(BytesIO(first))
    paragraphs = [paragraph.text for paragraph in reopened.paragraphs]
    assert [text for text in paragraphs if text.startswith("全文关键词：")] == [
        "全文关键词：核心问题"
    ]
    assert not any(text.startswith("关键词：") for text in paragraphs)
    assert "证据与时间索引" not in paragraphs
    assert "标题、检索字段与结构来源" not in paragraphs
    assert "覆盖审计" not in paragraphs
    assert "生成与来源信息" not in paragraphs
    chapter_heading = next(
        paragraph for paragraph in reopened.paragraphs if paragraph.text.startswith("1. ")
    )
    chapter_time = next(
        paragraph for paragraph in reopened.paragraphs if paragraph.text.startswith("时间：")
    )
    assert chapter_heading.paragraph_format.keep_with_next is True
    assert chapter_time.paragraph_format.keep_with_next is True


def test_publish_v2_creates_independent_group_then_hits_cache(tmp_path: Path) -> None:
    document = _document()
    _write_authority(tmp_path, document)
    documents_root = tmp_path / "SYNTH01" / "documents"
    old_group = documents_root / "formal-results" / "export-old000000"
    old_group.mkdir(parents=True)
    old_marker = old_group / "legacy.txt"
    old_marker.write_text("preserve", encoding="utf-8")
    (documents_root / "current.json").write_text(
        '{"schema_version":"1.0","export_id":"export-old000000"}\n',
        encoding="utf-8",
    )
    source = load_authoritative_document_content_v2(tmp_path, "SYNTH01")
    first = publish_document_export_v2(tmp_path, source)
    assert first.cache_hit is False
    assert first.group_path != old_group
    assert old_marker.read_text(encoding="utf-8") == "preserve"
    assert (first.group_path / "result.json").read_bytes() == (
        source.group_path / "document-content.json"
    ).read_bytes()
    before = {
        path.name: (path.stat().st_mtime_ns, path.read_bytes())
        for path in [first.current_path, *first.group_path.iterdir()]
    }
    second = publish_document_export_v2(tmp_path, source)
    assert second.cache_hit is True
    assert second.export_id == first.export_id
    after = {
        path.name: (path.stat().st_mtime_ns, path.read_bytes())
        for path in [second.current_path, *second.group_path.iterdir()]
    }
    assert after == before
    validate_document_export_group_v2(second.group_path, source=source)
    assert load_current_document_export_v2(tmp_path, "SYNTH01").export_id == first.export_id


@pytest.mark.parametrize(
    "point",
    [
        "after_result_json",
        "after_result_md",
        "after_result_docx",
        "after_manifest",
        "before_group_publish",
        "before_current_switch",
        "after_current_temp_write",
    ],
)
def test_publish_v2_failure_preserves_previous_current(
    tmp_path: Path, point: str
) -> None:
    document = _document()
    _write_authority(tmp_path, document)
    current = tmp_path / "SYNTH01" / "documents" / "current.json"
    current.parent.mkdir(parents=True)
    previous = b'{"schema_version":"1.0","export_id":"legacy"}\n'
    current.write_bytes(previous)
    source = load_authoritative_document_content_v2(tmp_path, "SYNTH01")
    with pytest.raises(DocumentExportV2Error, match="失败"):
        publish_document_export_v2(tmp_path, source, failure_injection=point)
    assert current.read_bytes() == previous
    assert not (current.parent / "formal-results" / f"export-{source.document_sha256[:12]}").exists()
    audits = list((current.parent / "failures").glob("*/failure.json"))
    assert len(audits) == 1
    assert json.loads(audits[0].read_text(encoding="utf-8"))["failure_stage"]


def test_export_document_cli_uses_v2_and_hits_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_authority(tmp_path, _document())
    argv = ["video-doc", "export-document", "SYNTH01", "--output-root", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli_main() == 0
    first = capsys.readouterr().out
    assert '"api_request_count": 0' in first
    assert '"cache_hit": false' in first
    monkeypatch.setattr(sys, "argv", argv)
    assert cli_main() == 0
    second = capsys.readouterr().out
    assert '"cache_hit": true' in second
