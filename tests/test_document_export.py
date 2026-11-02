import hashlib
import io
import json
import sys
import zipfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from docx.oxml.ns import qn

from video_doc.chapter_content import (
    CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
    CHAPTER_CONTENT_OUTPUT_LANGUAGE,
    CHAPTER_CONTENT_POLICY_VERSION,
    CHAPTER_CONTENT_PROMPT_VERSION,
    CHAPTER_CONTENT_VALIDATOR_VERSION,
    CHAPTER_WINDOWING_VERSION,
    prepare_task_chapter_inputs,
)
from video_doc.cli import main as cli_main
from video_doc.document_export import (
    DOCUMENT_EXPORT_CURRENT_SCHEMA_VERSION,
    DOCUMENT_EXPORT_MANIFEST_SCHEMA_VERSION,
    DOCUMENT_EXPORT_PUBLISHER_VERSION,
    DOCUMENT_FIELD_MAPPING_VERSION,
    DOCUMENT_DOCX_RENDERER_VERSION,
    DOCUMENT_JSON_RENDERER_VERSION,
    DOCUMENT_MARKDOWN_RENDERER_VERSION,
    DOCUMENT_PYTHON_DOCX_VERSION,
    DOCUMENT_TIME_DISPLAY_PATTERN,
    DOCUMENT_TIME_FORMAT_VERSION,
    DOCUMENT_TIME_RANGE_SEMANTICS,
    DOCUMENT_VIEW_VERSION,
    DOCUMENT_WORD_STYLE_VERSION,
    STRUCTURED_VIDEO_DOCUMENT_SCHEMA_VERSION,
    DocumentExportError,
    InjectedDocumentExportFailure,
    build_document_view,
    build_docx_paragraph_specs,
    build_structured_video_document,
    canonical_sha256,
    document_export_cache_key,
    format_timestamp_milliseconds,
    load_authoritative_chapter_content,
    load_current_document_export,
    publish_document_export,
    render_document_markdown,
    render_document_docx,
    render_structured_document_json,
    semantic_projection_from_document,
    semantic_projection_from_view,
    semantic_projection_sha256,
    status_notice_for_provenance,
    validate_document_docx,
    validate_document_export_group,
    validate_structured_video_document,
)
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


def _prepare_authoritative_content(tmp_path: Path) -> Path:
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
    _write_json(workspace.root / "boundaries" / "llm-execution-report.json", stage8_report)
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
                "chapter-plan.json": hashlib.sha256(stage8_plan_path.read_bytes()).hexdigest(),
                "execution-report.json": hashlib.sha256(stage8_report_path.read_bytes()).hexdigest(),
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
                "execution-report.json": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            },
        },
    )
    (content_root / "content.json").write_bytes(content_path.read_bytes())
    (content_root / "content-execution-report.json").write_bytes(report_path.read_bytes())
    return output_root


def test_stage10_contract_versions_are_frozen() -> None:
    assert STRUCTURED_VIDEO_DOCUMENT_SCHEMA_VERSION == "1.0"
    assert DOCUMENT_FIELD_MAPPING_VERSION == "stage9-bundle-to-structured-document-1.1"
    assert DOCUMENT_TIME_FORMAT_VERSION == "hms-millisecond-1.0"
    assert DOCUMENT_TIME_DISPLAY_PATTERN == "HH:MM:SS.mmm"
    assert DOCUMENT_TIME_RANGE_SEMANTICS == "[start,end)"
    assert DOCUMENT_VIEW_VERSION == "structured-document-view-1.1"
    assert DOCUMENT_JSON_RENDERER_VERSION == "structured-document-json-1.0"
    assert DOCUMENT_MARKDOWN_RENDERER_VERSION == "structured-document-markdown-1.1"
    assert DOCUMENT_DOCX_RENDERER_VERSION == "structured-document-docx-1.1"
    assert DOCUMENT_WORD_STYLE_VERSION == "compact-reference-guide-1.0"
    assert DOCUMENT_PYTHON_DOCX_VERSION == "1.2.0"
    assert DOCUMENT_EXPORT_MANIFEST_SCHEMA_VERSION == "1.0"
    assert DOCUMENT_EXPORT_CURRENT_SCHEMA_VERSION == "1.0"
    assert DOCUMENT_EXPORT_PUBLISHER_VERSION == "document-export-publisher-1.0"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00.000"),
        (Decimal("212.39"), "00:03:32.390"),
        (914.923, "00:15:14.923"),
        (Decimal("3599.9995"), "01:00:00.000"),
        (Decimal("360000.001"), "100:00:00.001"),
    ],
)
def test_format_timestamp_milliseconds(seconds: Any, expected: str) -> None:
    assert format_timestamp_milliseconds(seconds) == expected


@pytest.mark.parametrize("value", [-0.001, float("nan"), float("inf"), True])
def test_format_timestamp_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(DocumentExportError):
        format_timestamp_milliseconds(value)


def test_load_authoritative_content_validates_entire_stage9_chain(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)

    loaded = load_authoritative_chapter_content(output_root, "DEV01")

    assert loaded.result_id.startswith("content-")
    assert loaded.bundle["schema_version"] == "1.0"
    assert loaded.bundle["chaptering_provenance"] == {
        "status": "success",
        "result_source": "primary",
        "quality_level": "normal",
        "execution_report_sha256": loaded.input_manifest["chaptering_provenance"][
            "execution_report_sha256"
        ],
    }
    assert loaded.content_canonical_sha256 == canonical_sha256(loaded.bundle)
    assert loaded.content_file_sha256 == hashlib.sha256(
        loaded.content_path.read_bytes()
    ).hexdigest()


def test_load_rejects_top_level_alias_drift(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    alias = output_root / "DEV01" / "content" / "content.json"
    payload = json.loads(alias.read_text(encoding="utf-8"))
    payload["chapters"][0]["title"]["text"] = "被篡改"
    _write_json(alias, payload)

    with pytest.raises(DocumentExportError, match="顶层权威内容.*正式结果组不一致"):
        load_authoritative_chapter_content(output_root, "DEV01")


def test_load_rejects_formal_group_hash_mismatch(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    report = json.loads(
        (output_root / "DEV01" / "content" / "content-execution-report.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_path = (
        output_root
        / "DEV01"
        / "content"
        / "formal-results"
        / report["result_id"]
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["content.json"] = "0" * 64
    _write_json(manifest_path, manifest)

    with pytest.raises(DocumentExportError, match="content.json"):
        load_authoritative_chapter_content(output_root, "DEV01")


def test_load_rejects_invalid_field_level_evidence(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    report_path = output_root / "DEV01" / "content" / "content-execution-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    formal_root = output_root / "DEV01" / "content" / "formal-results"
    group = formal_root / report["result_id"]
    content_path = group / "content.json"
    bundle = json.loads(content_path.read_text(encoding="utf-8"))
    bundle["chapters"][0]["summary"]["source_event_ids"] = ["a9999"]
    content_hash = canonical_sha256(bundle)
    result_id = f"content-{content_hash[:12]}"
    report["content_sha256"] = content_hash
    report["result_id"] = result_id
    new_group = formal_root / result_id
    new_content_path = new_group / "content.json"
    new_report_path = new_group / "execution-report.json"
    _write_json(new_content_path, bundle)
    _write_json(new_report_path, report)
    _write_json(
        new_group / "manifest.json",
        {
            "schema_version": "1.0",
            "result_id": result_id,
            "task_id": "DEV01",
            "files": {
                "content.json": hashlib.sha256(new_content_path.read_bytes()).hexdigest(),
                "execution-report.json": hashlib.sha256(
                    new_report_path.read_bytes()
                ).hexdigest(),
            },
        },
    )
    content_root = output_root / "DEV01" / "content"
    (content_root / "content.json").write_bytes(new_content_path.read_bytes())
    (content_root / "content-execution-report.json").write_bytes(
        new_report_path.read_bytes()
    )

    with pytest.raises(DocumentExportError, match="章外或未知ASR事件ID"):
        load_authoritative_chapter_content(output_root, "DEV01")


def _structured_document(tmp_path: Path) -> dict[str, Any]:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")
    return build_structured_video_document(source)


def test_build_structured_document_preserves_stage9_semantics(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")

    document = build_structured_video_document(source)

    assert document["export_id"].startswith("export-")
    assert document["source_content"]["result_id"] == source.result_id
    assert document["source_content"]["canonical_sha256"] == source.content_canonical_sha256
    assert document["source_chapter_plan"] == source.bundle["source_chapter_plan"]
    assert document["source_timeline"] == source.bundle["source_timeline"]
    assert document["chaptering_provenance"] == source.bundle["chaptering_provenance"]
    assert document["generation"] == source.bundle["generation"]
    for exported, original in zip(document["chapters"], source.bundle["chapters"], strict=True):
        assert exported["chapter_id"] == original["chapter_id"]
        assert exported["time_range"]["start_seconds"] == original["start"]
        assert exported["time_range"]["end_seconds"] == original["end"]
        assert exported["title"] == original["title"]
        assert exported["summary"] == original["summary"]
        assert exported["key_points"] == original["key_points"]
        assert exported["keywords"] == original["keywords"]
        assert exported["source_event_ids"] == original["source_event_ids"]


def test_structured_document_identity_is_deterministic(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")

    first = build_structured_video_document(source)
    second = build_structured_video_document(source)

    assert first == second
    assert first["export_id"] == second["export_id"]


def test_validate_rejects_time_string_drift(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    document["chapters"][0]["time_range"]["start_text"] = "00:00:00.001"

    with pytest.raises(DocumentExportError, match="时间字符串与数值不一致"):
        validate_structured_video_document(document)


def test_validate_rejects_evidence_drift(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    document["chapters"][0]["summary"]["source_event_ids"] = ["a9999"]

    with pytest.raises(DocumentExportError, match="不可追溯"):
        validate_structured_video_document(document)


def test_validate_preserves_stage9_event_order_not_field_visit_order(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    chapter = document["chapters"][0]
    chapter["title"]["source_event_ids"] = [chapter["source_event_ids"][-1]]

    validate_structured_video_document(document)


def test_document_view_has_same_semantic_projection(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    view = build_document_view(document)

    from_document = semantic_projection_from_document(document)
    from_view = semantic_projection_from_view(view)

    assert from_document == from_view
    assert semantic_projection_sha256(view) == canonical_sha256(from_document)
    assert len(view.source_details) > 10


def test_json_renderer_is_deterministic_lossless_and_memory_only(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)

    first = render_structured_document_json(document)
    second = render_structured_document_json(document)

    assert first == second
    assert first.endswith("\n")
    assert json.loads(first) == document
    assert not list(tmp_path.rglob("result.json"))


def test_markdown_renderer_uses_shared_view_and_exposes_all_evidence(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    view = build_document_view(document)

    first = render_document_markdown(view)
    second = render_document_markdown(view)

    assert first == second
    assert first.startswith("# DEV01 知识型视频结构化文档\n")
    assert "## 来源与生成信息" in first
    assert "## 目录" in first
    assert "### 摘要" in first
    assert "### 知识点" in first
    assert "### 关键词" in first
    for chapter in document["chapters"]:
        assert chapter["time_range"]["start_text"] in first
        assert chapter["time_range"]["end_text"] in first
        for event_id in chapter["source_event_ids"]:
            assert f"`{event_id}`" in first
        for field in ("title", "summary"):
            assert chapter[field]["text"] in first
        for field in ("key_points", "keywords"):
            for item in chapter[field]:
                assert item["text"] in first
    assert not list(tmp_path.rglob("result.md"))


def test_markdown_renderer_deterministically_escapes_special_characters(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    document["chapters"][0]["planned_title"] = "A | B `code` *重点*\n下一行"
    view = build_document_view(document)

    markdown = render_document_markdown(view)

    assert "A \\| B \\`code\\` \\*重点\\*<br>下一行" in markdown


def test_docx_renderer_is_deterministic_reopenable_and_memory_only(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    view = build_document_view(document)

    first = render_document_docx(view)
    second = render_document_docx(view)
    report = validate_document_docx(first, view)

    assert first == second
    assert first.startswith(b"PK")
    assert report == {
        "status": "valid",
        "paragraph_count": len(build_docx_paragraph_specs(view)),
        "numbered_paragraph_count": 14,
        "chapter_count": 2,
        "semantic_projection_sha256": semantic_projection_sha256(view),
    }
    assert not list(tmp_path.rglob("result.docx"))


def test_docx_uses_real_numbering_with_independent_list_groups(tmp_path: Path) -> None:
    view = build_document_view(_structured_document(tmp_path))
    payload = render_document_docx(view)
    reopened = Document(io.BytesIO(payload))
    specs = build_docx_paragraph_specs(view)

    groups: dict[str, set[str]] = {}
    for paragraph, spec in zip(reopened.paragraphs, specs, strict=True):
        if spec.list_group is None:
            assert paragraph._p.pPr is None or paragraph._p.pPr.numPr is None
            continue
        num_id = paragraph._p.pPr.numPr.numId.get(qn("w:val"))
        groups.setdefault(spec.list_group, set()).add(num_id)

    assert all(len(values) == 1 for values in groups.values())
    assert len({next(iter(values)) for values in groups.values()}) == len(groups)
    numbering_xml = reopened.part.numbering_part.element.xml
    assert 'w:left="540"' in numbering_xml
    assert 'w:hanging="270"' in numbering_xml
    assert 'w:val="bullet"' in numbering_xml
    assert 'w:val="decimal"' in numbering_xml


def test_docx_page_geometry_styles_header_footer_and_natural_pagination_are_frozen(
    tmp_path: Path,
) -> None:
    view = build_document_view(_structured_document(tmp_path))
    payload = render_document_docx(view)
    reopened = Document(io.BytesIO(payload))

    section = reopened.sections[0]
    assert int(section.page_width) == 7_772_400
    assert int(section.page_height) == 10_058_400
    assert int(section.left_margin) == 914_400
    assert int(section.right_margin) == 914_400
    assert section.header.paragraphs[0].text == "DEV01 | 知识型视频结构化文档"
    assert "PAGE" in section.footer._element.xml
    assert reopened.styles["Normal"].font.name == "Calibri"
    assert reopened.styles["Normal"].font.size.pt == 11
    assert reopened.styles["Heading 1"].font.size.pt == 16
    chapter_headings = [
        paragraph
        for paragraph in reopened.paragraphs
        if paragraph.text.startswith(("1. 章节标题", "2. 章节标题"))
    ]
    assert all(
        paragraph.paragraph_format.page_break_before is False
        for paragraph in chapter_headings
    )
    assert all(paragraph.paragraph_format.keep_with_next is True for paragraph in chapter_headings)


def test_docx_contains_every_business_text_and_field_level_citation(tmp_path: Path) -> None:
    document = _structured_document(tmp_path)
    view = build_document_view(document)
    reopened = Document(io.BytesIO(render_document_docx(view)))
    all_text = "\n".join(paragraph.text for paragraph in reopened.paragraphs)

    for chapter in document["chapters"]:
        assert chapter["planned_title"] in all_text
        assert chapter["time_range"]["start_text"] in all_text
        assert chapter["time_range"]["end_text"] in all_text
        for field in ("title", "summary"):
            assert chapter[field]["text"] in all_text
            for event_id in chapter[field]["source_event_ids"]:
                assert event_id in all_text
        for field in ("key_points", "keywords"):
            for item in chapter[field]:
                assert item["text"] in all_text
                for event_id in item["source_event_ids"]:
                    assert event_id in all_text


def test_docx_validation_rejects_corrupt_zip(tmp_path: Path) -> None:
    view = build_document_view(_structured_document(tmp_path))

    with pytest.raises(DocumentExportError, match="有效ZIP"):
        validate_document_docx(b"not-a-docx", view)


def test_docx_validation_rejects_semantic_drift(tmp_path: Path) -> None:
    view = build_document_view(_structured_document(tmp_path))
    payload = render_document_docx(view)
    reopened = Document(io.BytesIO(payload))
    reopened.paragraphs[-1].text = "生成窗口数：999"
    buffer = io.BytesIO()
    reopened.save(buffer)

    with pytest.raises(DocumentExportError, match="语义或样式漂移"):
        validate_document_docx(buffer.getvalue(), view)


def test_docx_required_ooxml_parts_exist(tmp_path: Path) -> None:
    view = build_document_view(_structured_document(tmp_path))
    payload = render_document_docx(view)

    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        names = set(archive.namelist())
    assert "word/document.xml" in names
    assert "word/styles.xml" in names
    assert "word/numbering.xml" in names
    assert any(name.startswith("word/header") for name in names)
    assert any(name.startswith("word/footer") for name in names)


@pytest.mark.parametrize(
    ("result_source", "quality_level", "expected"),
    [
        (
            "fallback",
            "unknown",
            "降级提示：结果来自技术fallback，质量级别unknown；请人工复核。",
        ),
        (
            "emergency",
            "degraded",
            "降级提示：结果来自emergency单章结果，质量级别degraded；必须人工复核。",
        ),
    ],
)
def test_degraded_notice_is_identical_in_json_markdown_and_docx(
    tmp_path: Path,
    result_source: str,
    quality_level: str,
    expected: str,
) -> None:
    document = _structured_document(tmp_path)
    document["chaptering_provenance"]["result_source"] = result_source
    document["chaptering_provenance"]["quality_level"] = quality_level
    document["status_notice"] = status_notice_for_provenance(
        document["chaptering_provenance"]
    )
    view = build_document_view(document)

    assert document["status_notice"] == expected
    assert expected in render_document_markdown(view)
    docx_payload = render_document_docx(view)
    assert expected in "\n".join(
        paragraph.text for paragraph in Document(io.BytesIO(docx_payload)).paragraphs
    )
    validate_document_docx(docx_payload, view)


def _variant_source(source: Any, marker: str) -> Any:
    bundle = json.loads(json.dumps(source.bundle, ensure_ascii=False))
    bundle["chapters"][0]["title"]["text"] = f"章节标题-{marker}"
    content_hash = canonical_sha256(bundle)
    synthetic_file_hash = hashlib.sha256(
        f"variant-file-{marker}-{content_hash}".encode("utf-8")
    ).hexdigest()
    return replace(
        source,
        result_id=f"content-{content_hash[:12]}",
        bundle=bundle,
        content_canonical_sha256=content_hash,
        content_file_sha256=synthetic_file_hash,
    )


def _snapshot_files(paths: list[Path]) -> dict[str, tuple[str, int, int]]:
    return {
        str(path): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in paths
    }


def test_publish_document_export_creates_one_atomic_four_file_group(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")

    published = publish_document_export(output_root, source)
    loaded = load_current_document_export(
        output_root,
        "DEV01",
        expected_document=published.document,
    )

    assert published.cache_hit is False
    assert loaded.cache_hit is True
    assert published.export_id == f"export-{published.cache_key[:12]}"
    assert published.cache_key == document_export_cache_key(published.document)
    assert {path.name for path in published.group_path.iterdir()} == {
        "result.json",
        "result.md",
        "result.docx",
        "manifest.json",
    }
    assert published.manifest["schema_version"] == "1.0"
    assert published.manifest["chapter_count"] == 2
    assert set(published.manifest["files"]) == {
        "result.json",
        "result.md",
        "result.docx",
    }
    current = json.loads(published.current_path.read_text(encoding="utf-8"))
    assert current["schema_version"] == "1.0"
    assert current["export_id"] == published.export_id
    assert current["group"] == f"formal-results/{published.export_id}"
    assert not (published.documents_root / "result.json").exists()
    assert not (published.documents_root / "result.md").exists()
    assert not (published.documents_root / "result.docx").exists()


def test_publish_document_export_whole_group_cache_does_not_rewrite(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")
    first = publish_document_export(output_root, source)
    protected = [first.current_path, *sorted(first.group_path.iterdir())]
    before = _snapshot_files(protected)

    second = publish_document_export(output_root, source)

    assert second.cache_hit is True
    assert second.group_path == first.group_path
    assert _snapshot_files(protected) == before
    assert not list(first.documents_root.glob(".tmp-*"))


def test_changed_source_creates_new_group_and_preserves_old_group(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")
    first = publish_document_export(output_root, source)
    old_snapshot = _snapshot_files(sorted(first.group_path.iterdir()))

    second = publish_document_export(output_root, _variant_source(source, "v2"))

    assert second.export_id != first.export_id
    assert second.cache_hit is False
    assert first.group_path.is_dir()
    assert _snapshot_files(sorted(first.group_path.iterdir())) == old_snapshot
    assert load_current_document_export(output_root, "DEV01").export_id == second.export_id


@pytest.mark.parametrize(
    "failure_point",
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
def test_failure_injection_preserves_old_group_and_current_pointer(
    tmp_path: Path,
    failure_point: str,
) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")
    first = publish_document_export(output_root, source)
    protected = [first.current_path, *sorted(first.group_path.iterdir())]
    before = _snapshot_files(protected)
    variant = _variant_source(source, failure_point)
    variant_document = build_structured_video_document(variant)

    with pytest.raises(InjectedDocumentExportFailure, match=failure_point):
        publish_document_export(
            output_root,
            variant,
            failure_injection=failure_point,
        )

    assert _snapshot_files(protected) == before
    assert not (
        first.documents_root / "formal-results" / variant_document["export_id"]
    ).exists()
    assert not list(first.documents_root.glob(".tmp-*"))
    assert not list(first.documents_root.glob(".current.json.tmp-*"))
    failures = list((first.documents_root / "failures").glob("*/failure.json"))
    assert len(failures) == 1
    failure = json.loads(failures[0].read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["failure_stage"] in {
        "result.json",
        "result.md",
        "result.docx",
        "manifest.json",
        "group_publish",
        "current_switch",
    }
    assert "章节摘要" not in failures[0].read_text(encoding="utf-8")
    assert str(output_root) not in failures[0].read_text(encoding="utf-8")


def test_corrupt_existing_target_group_hard_fails_without_switching_current(
    tmp_path: Path,
) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")
    first = publish_document_export(output_root, source)
    current_before = first.current_path.read_bytes()
    variant = _variant_source(source, "corrupt-target")
    variant_document = build_structured_video_document(variant)
    corrupt_group = (
        first.documents_root / "formal-results" / variant_document["export_id"]
    )
    corrupt_group.mkdir(parents=True)
    (corrupt_group / "result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DocumentExportError, match="文件集合不完整"):
        publish_document_export(output_root, variant)

    assert first.current_path.read_bytes() == current_before
    assert load_current_document_export(output_root, "DEV01").export_id == first.export_id


def test_group_validation_rejects_extra_file_and_cross_group_mixing(tmp_path: Path) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")
    first = publish_document_export(output_root, source)
    second = publish_document_export(output_root, _variant_source(source, "second"))
    mixed_markdown = first.group_path / "result.md"
    (second.group_path / "result.md").write_bytes(mixed_markdown.read_bytes())

    with pytest.raises(DocumentExportError, match="result.md"):
        validate_document_export_group(second.group_path)

    (first.group_path / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(DocumentExportError, match="额外成员"):
        validate_document_export_group(first.group_path)


def test_v1_publisher_remains_available_as_library_and_hits_whole_group_cache(
    tmp_path: Path,
) -> None:
    output_root = _prepare_authoritative_content(tmp_path)
    source = load_authoritative_chapter_content(output_root, "DEV01")
    first = publish_document_export(output_root, source)
    before = _snapshot_files([first.current_path, *sorted(first.group_path.iterdir())])
    second = publish_document_export(output_root, source)
    assert second.cache_hit is True
    assert second.export_id == first.export_id
    assert _snapshot_files([second.current_path, *sorted(second.group_path.iterdir())]) == before


def test_export_document_cli_reports_invalid_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "video-doc",
            "export-document",
            "DEV01",
            "--output-root",
            str(tmp_path / "missing"),
        ],
    )

    assert cli_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "处理失败" in captured.err
    assert "2.0 current" in captured.err
