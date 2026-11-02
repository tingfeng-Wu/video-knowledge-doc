"""StructuredDocumentContent 2.0的只读加载与JSON/Markdown纯渲染。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from video_doc.document_export import (
    _add_abstract_numbering,
    _apply_numbering,
    _atomic_replace_bytes,
    _configure_docx_styles,
    _normalize_docx_archive,
    _set_docx_page_geometry,
    _write_bytes_fsync,
    file_sha256,
    format_timestamp_milliseconds,
)
from video_doc.structured_document_content import (
    StructuredDocumentContentError,
    canonical_sha256,
    validate_structured_document_content,
)
from video_doc.task_workspace import TaskWorkspaceError, validate_task_id


DOCUMENT_V2_CURRENT_SCHEMA_VERSION = "1.0"
DOCUMENT_V2_FORMAL_PUBLISHER_VERSION = "document-formal-publisher-2.0"
DOCUMENT_V2_VIEW_VERSION = "structured-document-view-2.2"
DOCUMENT_V2_JSON_RENDERER_VERSION = "structured-document-json-2.0"
DOCUMENT_V2_MARKDOWN_RENDERER_VERSION = "structured-document-markdown-2.3"
DOCUMENT_V2_DOCX_RENDERER_VERSION = "structured-document-docx-2.4"
DOCUMENT_V2_WORD_STYLE_VERSION = "compact-reference-guide-2.0"
DOCUMENT_V2_PYTHON_DOCX_VERSION = "1.2.0"
DOCUMENT_V2_EXPORT_MANIFEST_SCHEMA_VERSION = "2.0"
DOCUMENT_V2_EXPORT_PUBLISHER_VERSION = "document-export-publisher-2.0"
DOCUMENT_V2_EXPORT_CURRENT_SCHEMA_VERSION = "2.0"

_CONTENT_ID_RE = re.compile(r"^document-content-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORMAL_FILES = {
    "document-content.json": "application/json",
    "execution-report.json": "application/json",
}


class DocumentExportV2Error(RuntimeError):
    """2.0权威输入或纯渲染不符合冻结适配契约。"""


class InjectedDocumentExportV2Failure(DocumentExportV2Error):
    """仅供离线测试验证2.0结果组原子发布。"""


@dataclass(frozen=True)
class AuthoritativeDocumentContentV2:
    task_root: Path
    group_path: Path
    current_path: Path
    document_path: Path
    execution_report_path: Path
    manifest_path: Path
    content_id: str
    document: dict[str, Any]
    execution_report: dict[str, Any]
    manifest: dict[str, Any]
    document_sha256: str
    document_file_sha256: str
    execution_report_file_sha256: str
    manifest_file_sha256: str


@dataclass(frozen=True)
class EvidenceViewV2:
    text: str
    content_origin: str
    source_content_paths: tuple[str, ...]
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class ListItemViewV2:
    item_id: str
    evidence: EvidenceViewV2


@dataclass(frozen=True)
class ComparisonRowViewV2:
    row_id: str
    dimension: str
    cells: tuple[EvidenceViewV2, ...]


@dataclass(frozen=True)
class ContentBlockViewV2:
    block_id: str
    type: str
    role: str
    text: str | None
    content_origin: str | None
    source_content_paths: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    list_kind: str | None
    items: tuple[ListItemViewV2, ...]
    columns: tuple[str, ...]
    rows: tuple[ComparisonRowViewV2, ...]


@dataclass(frozen=True)
class SectionViewV2:
    section_id: str
    heading: EvidenceViewV2 | None
    blocks: tuple[ContentBlockViewV2, ...]


@dataclass(frozen=True)
class ChapterViewV2:
    chapter_id: str
    index: int
    start_seconds: int | float
    end_seconds: int | float
    start_text: str
    end_text: str
    planned_title: str
    title: EvidenceViewV2
    keywords: tuple[EvidenceViewV2, ...]
    sections: tuple[SectionViewV2, ...]


@dataclass(frozen=True)
class CoverageItemViewV2:
    source_content_path: str
    status: str
    target_block_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceIndexViewV2:
    block_id: str
    location_label: str
    source_content_paths: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    start_seconds: int | float
    end_seconds: int | float
    start_text: str
    end_text: str


@dataclass(frozen=True)
class DocumentViewV2:
    schema_version: str
    content_id: str
    task_id: str
    language: str
    strategy: str
    document_profile: str
    status_notice: str
    title: EvidenceViewV2
    overview: tuple[ContentBlockViewV2, ...]
    global_keywords: tuple[EvidenceViewV2, ...]
    chapters: tuple[ChapterViewV2, ...]
    overall_conclusion: tuple[ContentBlockViewV2, ...]
    coverage: tuple[CoverageItemViewV2, ...]
    evidence_index: tuple[EvidenceIndexViewV2, ...]
    source_details: tuple[tuple[str, str], ...]
    renderer_versions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DocumentExportGroupV2:
    documents_root: Path
    group_path: Path
    current_path: Path
    export_id: str
    cache_key: str
    document: dict[str, Any]
    manifest: dict[str, Any]
    view: DocumentViewV2
    cache_hit: bool


def _fail(message: str) -> None:
    raise DocumentExportV2Error(message)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json_bytes(path: Path, description: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise DocumentExportV2Error(f"{description}不存在。") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentExportV2Error(f"{description}不是有效UTF-8 JSON。") from exc
    if not isinstance(payload, dict):
        _fail(f"{description}顶层必须是对象。")
    return payload, raw


def _exact_keys(payload: Mapping[str, Any], expected: set[str], location: str) -> None:
    if set(payload) != expected:
        _fail(f"{location}字段集合无效。")


def _valid_sha256(value: Any, location: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(f"{location}必须是小写64位SHA-256。")
    return value


def _nonnegative_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{location}必须是非负整数。")
    return value


def load_authoritative_document_content_v2(
    output_root: str | Path,
    task_id: str,
) -> AuthoritativeDocumentContentV2:
    """只由2.0 current选择并复验已接受正式组；不读取ASR或阶段9 v1。"""

    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise DocumentExportV2Error(str(exc)) from exc

    task_root = Path(output_root).expanduser() / task_id
    document_root = task_root / "content" / "document-v2"
    current_path = document_root / "current.json"
    current, current_raw = _read_json_bytes(current_path, "2.0 current")
    _exact_keys(
        current,
        {
            "schema_version",
            "status",
            "task_id",
            "content_id",
            "group",
            "document_sha256",
            "publisher_version",
        },
        "2.0 current",
    )
    content_id = current.get("content_id")
    if (
        current.get("schema_version") != DOCUMENT_V2_CURRENT_SCHEMA_VERSION
        or current.get("status") != "accepted"
        or current.get("task_id") != task_id
        or not isinstance(content_id, str)
        or _CONTENT_ID_RE.fullmatch(content_id) is None
        or current.get("group") != f"formal-results/{content_id}"
        or current.get("publisher_version") != DOCUMENT_V2_FORMAL_PUBLISHER_VERSION
    ):
        _fail("2.0 current的状态、任务或正式组身份无效。")
    if current_raw != _json_bytes(current):
        _fail("2.0 current不是冻结的确定性JSON。")

    group = document_root / "formal-results" / content_id
    if not group.is_dir():
        _fail("2.0正式结果组不存在。")
    entries = list(group.iterdir())
    expected_names = set(_FORMAL_FILES) | {"manifest.json"}
    if {entry.name for entry in entries if entry.is_file()} != expected_names or any(
        entry.is_dir() for entry in entries
    ):
        _fail("2.0正式结果组文件集合不完整或包含额外成员。")

    document, document_raw = _read_json_bytes(
        group / "document-content.json", "2.0正式内容"
    )
    report, report_raw = _read_json_bytes(
        group / "execution-report.json", "2.0正式执行报告"
    )
    manifest, manifest_raw = _read_json_bytes(group / "manifest.json", "2.0 manifest")
    for payload, raw, location in (
        (document, document_raw, "2.0正式内容"),
        (report, report_raw, "2.0正式执行报告"),
        (manifest, manifest_raw, "2.0 manifest"),
    ):
        if raw != _json_bytes(payload):
            _fail(f"{location}不是冻结的确定性JSON。")
    try:
        validate_structured_document_content(document)
    except StructuredDocumentContentError as exc:
        raise DocumentExportV2Error(f"2.0正式内容Schema无效：{exc}") from exc

    document_hash = canonical_sha256(document)
    if (
        document.get("task_id") != task_id
        or document.get("content_id") != content_id
        or current.get("document_sha256") != document_hash
    ):
        _fail("2.0 current与正式内容身份不一致。")

    _exact_keys(
        report,
        {
            "schema_version",
            "status",
            "task_id",
            "content_id",
            "input_id",
            "document_profile",
            "document_sha256",
            "chapter_count",
            "business_call_count",
            "supplier_request_count",
            "cache_hit_count",
            "api_request_count",
            "versions",
        },
        "2.0正式执行报告",
    )
    for field in (
        "business_call_count",
        "supplier_request_count",
        "cache_hit_count",
        "api_request_count",
    ):
        _nonnegative_int(report.get(field), f"执行报告.{field}")
    if (
        report.get("schema_version") != "1.0"
        or report.get("status") != "success"
        or report.get("task_id") != task_id
        or report.get("content_id") != content_id
        or report.get("document_profile") != document["document_profile"]
        or report.get("document_sha256") != document_hash
        or report.get("chapter_count") != len(document["chapters"])
        or report.get("api_request_count") != document["generation"]["api_request_count"]
        or not isinstance(report.get("input_id"), str)
        or not report["input_id"].startswith("composition-input-")
        or not isinstance(report.get("versions"), dict)
    ):
        _fail("2.0正式执行报告与内容身份、计数或版本不一致。")

    _exact_keys(
        manifest,
        {
            "schema_version",
            "status",
            "task_id",
            "content_id",
            "document_profile",
            "input",
            "document_sha256",
            "chapter_count",
            "calls",
            "content_versions",
            "preview_versions",
            "files",
        },
        "2.0 manifest",
    )
    input_identity = manifest.get("input")
    calls = manifest.get("calls")
    files = manifest.get("files")
    if not isinstance(input_identity, dict) or not isinstance(calls, dict) or not isinstance(
        files, dict
    ):
        _fail("2.0 manifest输入、调用或文件身份无效。")
    _exact_keys(
        input_identity,
        {
            "input_id",
            "composition_input_sha256",
            "source_content_result_id",
            "source_chapter_plan_result_id",
            "source_timeline_sha256",
        },
        "2.0 manifest.input",
    )
    _exact_keys(
        calls,
        {
            "business_call_count",
            "supplier_request_count",
            "cache_hit_count",
            "api_request_count",
        },
        "2.0 manifest.calls",
    )
    if set(files) != set(_FORMAL_FILES):
        _fail("2.0 manifest文件集合无效。")
    file_payloads = {
        "document-content.json": document_raw,
        "execution-report.json": report_raw,
    }
    for name, mime_type in _FORMAL_FILES.items():
        record = files.get(name)
        if not isinstance(record, dict):
            _fail(f"2.0 manifest缺少{name}身份。")
        _exact_keys(record, {"sha256", "bytes", "mime_type"}, f"files.{name}")
        raw = file_payloads[name]
        if (
            record.get("sha256") != _file_sha256(raw)
            or record.get("bytes") != len(raw)
            or record.get("mime_type") != mime_type
        ):
            _fail(f"2.0正式文件身份不一致：{name}")
    for field in calls:
        _nonnegative_int(calls[field], f"manifest.calls.{field}")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("status") != "success"
        or manifest.get("task_id") != task_id
        or manifest.get("content_id") != content_id
        or manifest.get("document_profile") != document["document_profile"]
        or manifest.get("document_sha256") != document_hash
        or manifest.get("chapter_count") != len(document["chapters"])
        or manifest.get("content_versions") != document["content_versions"]
        or manifest.get("preview_versions") != report["versions"]
        or calls
        != {
            field: report[field]
            for field in (
                "business_call_count",
                "supplier_request_count",
                "cache_hit_count",
                "api_request_count",
            )
        }
        or input_identity.get("input_id") != report["input_id"]
        or input_identity.get("source_content_result_id")
        != document["source_content_bundle"]["result_id"]
        or input_identity.get("source_chapter_plan_result_id")
        != document["source_chapter_plan"]["result_id"]
        or input_identity.get("source_timeline_sha256")
        != document["source_timeline"]["canonical_sha256"]
    ):
        _fail("2.0 manifest与内容、报告或来源身份不一致。")
    _valid_sha256(input_identity.get("composition_input_sha256"), "composition_input_sha256")

    return AuthoritativeDocumentContentV2(
        task_root=task_root,
        group_path=group,
        current_path=current_path,
        document_path=group / "document-content.json",
        execution_report_path=group / "execution-report.json",
        manifest_path=group / "manifest.json",
        content_id=content_id,
        document=document,
        execution_report=report,
        manifest=manifest,
        document_sha256=document_hash,
        document_file_sha256=_file_sha256(document_raw),
        execution_report_file_sha256=_file_sha256(report_raw),
        manifest_file_sha256=_file_sha256(manifest_raw),
    )


def _evidence(payload: Mapping[str, Any]) -> EvidenceViewV2:
    return EvidenceViewV2(
        text=payload["text"],
        content_origin=payload["content_origin"],
        source_content_paths=tuple(payload["source_content_paths"]),
        source_event_ids=tuple(payload["source_event_ids"]),
    )


def _block(payload: Mapping[str, Any]) -> ContentBlockViewV2:
    block_type = payload["type"]
    if block_type in {"paragraph", "callout"}:
        return ContentBlockViewV2(
            block_id=payload["block_id"],
            type=block_type,
            role=payload["role"],
            text=payload["text"],
            content_origin=payload["content_origin"],
            source_content_paths=tuple(payload["source_content_paths"]),
            source_event_ids=tuple(payload["source_event_ids"]),
            list_kind=None,
            items=(),
            columns=(),
            rows=(),
        )
    if block_type == "list":
        items = tuple(
            ListItemViewV2(item_id=item["item_id"], evidence=_evidence(item))
            for item in payload["items"]
        )
        return ContentBlockViewV2(
            block_id=payload["block_id"],
            type=block_type,
            role=payload["role"],
            text=None,
            content_origin=None,
            source_content_paths=tuple(
                dict.fromkeys(path for item in items for path in item.evidence.source_content_paths)
            ),
            source_event_ids=tuple(
                dict.fromkeys(event for item in items for event in item.evidence.source_event_ids)
            ),
            list_kind=payload["list_kind"],
            items=items,
            columns=(),
            rows=(),
        )
    rows = tuple(
        ComparisonRowViewV2(
            row_id=row["row_id"],
            dimension=row["dimension"],
            cells=tuple(_evidence(cell) for cell in row["cells"]),
        )
        for row in payload["rows"]
    )
    return ContentBlockViewV2(
        block_id=payload["block_id"],
        type=block_type,
        role=payload["role"],
        text=None,
        content_origin=None,
        source_content_paths=tuple(
            dict.fromkeys(
                path for row in rows for cell in row.cells for path in cell.source_content_paths
            )
        ),
        source_event_ids=tuple(
            dict.fromkeys(
                event for row in rows for cell in row.cells for event in cell.source_event_ids
            )
        ),
        list_kind=None,
        items=(),
        columns=tuple(payload["columns"]),
        rows=rows,
    )


def _flatten(value: Any, prefix: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            result.extend(_flatten(value[key], f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(_flatten(item, f"{prefix}[{index}]"))
    else:
        result.append(
            (prefix, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        )
    return result


def _status_notice(provenance: Mapping[str, Any]) -> str:
    state = (provenance["result_source"], provenance["quality_level"])
    if state == ("primary", "normal"):
        return ""
    if state == ("fallback", "unknown"):
        return "降级提示：章节来自技术fallback，质量级别unknown；请人工复核。"
    return "降级提示：章节来自emergency单章结果，质量级别degraded；必须人工复核。"


def build_document_view_v2(document: Mapping[str, Any]) -> DocumentViewV2:
    """把完整2.0对象无改写映射为JSON/Markdown/DOCX共用只读视图。"""

    try:
        validate_structured_document_content(document)
    except StructuredDocumentContentError as exc:
        raise DocumentExportV2Error(str(exc)) from exc
    chapters = tuple(
        ChapterViewV2(
            chapter_id=chapter["chapter_id"],
            index=chapter["index"],
            start_seconds=chapter["start"],
            end_seconds=chapter["end"],
            start_text=format_timestamp_milliseconds(chapter["start"]),
            end_text=format_timestamp_milliseconds(chapter["end"]),
            planned_title=chapter["planned_title"],
            title=_evidence(chapter["title"]),
            keywords=tuple(_evidence(item) for item in chapter["keywords"]),
            sections=tuple(
                SectionViewV2(
                    section_id=section["section_id"],
                    heading=_evidence(section["heading"])
                    if section["heading"] is not None
                    else None,
                    blocks=tuple(_block(block) for block in section["blocks"]),
                )
                for section in chapter["sections"]
            ),
        )
        for chapter in document["chapters"]
    )
    keyword_evidence: dict[str, dict[str, list[str]]] = {}
    for chapter in chapters:
        for keyword in chapter.keywords[:1]:
            record = keyword_evidence.setdefault(
                keyword.text, {"paths": [], "events": [], "origins": []}
            )
            record["origins"].append(keyword.content_origin)
            record["paths"].extend(keyword.source_content_paths)
            record["events"].extend(keyword.source_event_ids)
    global_keywords = tuple(
        EvidenceViewV2(
            text=text,
            content_origin=record["origins"][0],
            source_content_paths=tuple(dict.fromkeys(record["paths"])),
            source_event_ids=tuple(dict.fromkeys(record["events"])),
        )
        for text, record in keyword_evidence.items()
    )
    source_details: list[tuple[str, str]] = []
    for field in (
        "source_content_bundle",
        "source_chapter_plan",
        "source_timeline",
        "chaptering_provenance",
        "generation",
        "content_versions",
    ):
        source_details.extend(_flatten(document[field], field))
    return DocumentViewV2(
        schema_version=document["schema_version"],
        content_id=document["content_id"],
        task_id=document["task_id"],
        language=document["language"],
        strategy=document["strategy"],
        document_profile=document["document_profile"],
        status_notice=_status_notice(document["chaptering_provenance"]),
        title=_evidence(document["title"]),
        overview=tuple(_block(block) for block in document["overview"]),
        global_keywords=global_keywords,
        chapters=chapters,
        overall_conclusion=tuple(_block(block) for block in document["overall_conclusion"]),
        coverage=tuple(
            CoverageItemViewV2(
                source_content_path=item["source_content_path"],
                status=item["status"],
                target_block_ids=tuple(item["target_block_ids"]),
            )
            for item in document["coverage"]["items"]
        ),
        evidence_index=tuple(
            EvidenceIndexViewV2(
                block_id=row["block_id"],
                location_label=row["location_label"],
                source_content_paths=tuple(row["source_content_paths"]),
                source_event_ids=tuple(row["source_event_ids"]),
                start_seconds=row["start"],
                end_seconds=row["end"],
                start_text=format_timestamp_milliseconds(row["start"]),
                end_text=format_timestamp_milliseconds(row["end"]),
            )
            for row in document["evidence_index"]
        ),
        source_details=tuple(source_details),
        renderer_versions=(
            ("document_view", DOCUMENT_V2_VIEW_VERSION),
            ("json_renderer", DOCUMENT_V2_JSON_RENDERER_VERSION),
            ("markdown_renderer", DOCUMENT_V2_MARKDOWN_RENDERER_VERSION),
            ("docx_renderer", DOCUMENT_V2_DOCX_RENDERER_VERSION),
            ("word_style", DOCUMENT_V2_WORD_STYLE_VERSION),
            ("python_docx", DOCUMENT_V2_PYTHON_DOCX_VERSION),
        ),
    )


def _evidence_projection(item: EvidenceViewV2) -> dict[str, Any]:
    return {
        "text": item.text,
        "content_origin": item.content_origin,
        "source_content_paths": list(item.source_content_paths),
        "source_event_ids": list(item.source_event_ids),
    }


def _block_projection(block: ContentBlockViewV2) -> dict[str, Any]:
    result: dict[str, Any] = {
        "block_id": block.block_id,
        "type": block.type,
        "role": block.role,
    }
    if block.type in {"paragraph", "callout"}:
        result.update(
            {
                "text": block.text,
                "content_origin": block.content_origin,
                "source_content_paths": list(block.source_content_paths),
                "source_event_ids": list(block.source_event_ids),
            }
        )
    elif block.type == "list":
        result.update(
            {
                "list_kind": block.list_kind,
                "items": [
                    {"item_id": item.item_id, **_evidence_projection(item.evidence)}
                    for item in block.items
                ],
            }
        )
    else:
        result.update(
            {
                "columns": list(block.columns),
                "rows": [
                    {
                        "row_id": row.row_id,
                        "dimension": row.dimension,
                        "cells": [_evidence_projection(cell) for cell in row.cells],
                    }
                    for row in block.rows
                ],
            }
        )
    return result


def semantic_projection_from_view_v2(view: DocumentViewV2) -> dict[str, Any]:
    return {
        "identity": {
            "schema_version": view.schema_version,
            "content_id": view.content_id,
            "task_id": view.task_id,
            "language": view.language,
            "strategy": view.strategy,
            "document_profile": view.document_profile,
        },
        "status_notice": view.status_notice,
        "title": _evidence_projection(view.title),
        "overview": [_block_projection(block) for block in view.overview],
        "global_keywords": [_evidence_projection(item) for item in view.global_keywords],
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "index": chapter.index,
                "start_seconds": chapter.start_seconds,
                "end_seconds": chapter.end_seconds,
                "start_text": chapter.start_text,
                "end_text": chapter.end_text,
                "planned_title": chapter.planned_title,
                "title": _evidence_projection(chapter.title),
                "keywords": [_evidence_projection(item) for item in chapter.keywords],
                "sections": [
                    {
                        "section_id": section.section_id,
                        "heading": _evidence_projection(section.heading)
                        if section.heading is not None
                        else None,
                        "blocks": [_block_projection(block) for block in section.blocks],
                    }
                    for section in chapter.sections
                ],
            }
            for chapter in view.chapters
        ],
        "overall_conclusion": [
            _block_projection(block) for block in view.overall_conclusion
        ],
        "coverage": [
            {
                "source_content_path": item.source_content_path,
                "status": item.status,
                "target_block_ids": list(item.target_block_ids),
            }
            for item in view.coverage
        ],
        "evidence_index": [
            {
                "block_id": row.block_id,
                "location_label": row.location_label,
                "source_content_paths": list(row.source_content_paths),
                "source_event_ids": list(row.source_event_ids),
                "start_seconds": row.start_seconds,
                "end_seconds": row.end_seconds,
                "start_text": row.start_text,
                "end_text": row.end_text,
            }
            for row in view.evidence_index
        ],
        "source_details": [list(item) for item in view.source_details],
    }


def semantic_projection_from_document_v2(document: Mapping[str, Any]) -> dict[str, Any]:
    return semantic_projection_from_view_v2(build_document_view_v2(document))


def render_structured_document_json_v2(document: Mapping[str, Any]) -> str:
    try:
        validate_structured_document_content(document)
    except StructuredDocumentContentError as exc:
        raise DocumentExportV2Error(str(exc)) from exc
    return _json_bytes(deepcopy(dict(document))).decode("utf-8")


def _md(value: Any) -> str:
    text = str(value).replace("\\", "\\\\")
    for character in "`*_{}[]<>#+-.!|":
        text = text.replace(character, f"\\{character}")
    return text.replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")


def _md_cell(value: Any) -> str:
    return _md(value).replace("<br>", " ")


def _append_block_markdown(lines: list[str], block: ContentBlockViewV2) -> None:
    if block.type == "paragraph":
        lines.extend([_md(block.text), ""])
    elif block.type == "callout":
        lines.extend([f"> {_md(block.text)}", ""])
    elif block.type == "list":
        for index, item in enumerate(block.items, start=1):
            marker = f"{index}." if block.list_kind == "numbered" else "-"
            lines.append(f"{marker} {_md(item.evidence.text)}")
        lines.append("")
    else:
        lines.append("| 维度 | " + " | ".join(_md_cell(column) for column in block.columns) + " |")
        lines.append("|---|" + "---|" * len(block.columns))
        for row in block.rows:
            lines.append(
                "| "
                + _md_cell(row.dimension)
                + " | "
                + " | ".join(_md_cell(cell.text) for cell in row.cells)
                + " |"
            )
        lines.append("")


def render_document_markdown_v2(view: DocumentViewV2) -> str:
    """只消费共享视图；默认读者成品不展示工程审计附录。"""

    lines = [f"# {_md(view.title.text)}", ""]
    if view.status_notice:
        lines.extend([f"> {_md(view.status_notice)}", ""])
    lines.extend(["## 视频概述", ""])
    for block in view.overview:
        _append_block_markdown(lines, block)
    if view.global_keywords:
        lines.extend(
            [
                "**全文关键词：** "
                + "、".join(_md(item.text) for item in view.global_keywords),
                "",
            ]
        )
    lines.extend(["## 目录", ""])
    for chapter in view.chapters:
        lines.append(
            f"- {chapter.index}. {_md(chapter.title.text)}（{chapter.start_text}–{chapter.end_text}）"
        )
    for chapter in view.chapters:
        lines.extend(
            [
                "",
                f"## {chapter.index}. {_md(chapter.title.text)}",
                "",
                f"时间：`{chapter.start_text}`–`{chapter.end_text}`",
            ]
        )
        lines.append("")
        for section in chapter.sections:
            if section.heading is not None:
                lines.extend([f"### {_md(section.heading.text)}", ""])
            for block in section.blocks:
                _append_block_markdown(lines, block)
    if view.overall_conclusion:
        lines.extend(["## 核心结论", ""])
        for block in view.overall_conclusion:
            _append_block_markdown(lines, block)
    return "\n".join(lines) + "\n"


def semantic_projection_sha256_v2(view: DocumentViewV2) -> str:
    return canonical_sha256(semantic_projection_from_view_v2(view))


def _set_cell_text(cell: Any, text: str, *, bold: bool = False) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.name = "Calibri"
    run.font.size = Pt(9.5)


def _shade(element: Any, color: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), color)
    element.append(shading)


def _configure_table(table: Any, column_count: int) -> None:
    table.autofit = False
    table.style = "Table Grid"
    properties = table._tbl.tblPr
    for tag, attributes in (
        ("w:tblLayout", {"w:type": "fixed"}),
        ("w:tblW", {"w:w": "9360", "w:type": "dxa"}),
        ("w:tblInd", {"w:w": "120", "w:type": "dxa"}),
    ):
        node = OxmlElement(tag)
        for key, value in attributes.items():
            node.set(qn(key), value)
        properties.append(node)
    margins = OxmlElement("w:tblCellMar")
    for side, value in (("top", 80), ("start", 120), ("bottom", 80), ("end", 120)):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        margins.append(node)
    properties.append(margins)
    widths = [1800] + [int(7560 / max(1, column_count - 1))] * (column_count - 1)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            cell.width = Inches(widths[index] / 1440)


def _add_numbering_from_one(numbering: Any, abstract_id: int) -> int:
    identifiers = [
        int(node.get(qn("w:numId")))
        for node in numbering.findall(qn("w:num"))
        if node.get(qn("w:numId")) is not None
    ]
    num_id = max(identifiers, default=0) + 1
    number = OxmlElement("w:num")
    number.set(qn("w:numId"), str(num_id))
    abstract = OxmlElement("w:abstractNumId")
    abstract.set(qn("w:val"), str(abstract_id))
    number.append(abstract)
    override = OxmlElement("w:lvlOverride")
    override.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:startOverride")
    start.set(qn("w:val"), "1")
    override.append(start)
    number.append(override)
    numbering.append(number)
    return num_id


def _add_page_field(paragraph: Any) -> None:
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    paragraph.add_run()._r.extend([begin, instruction, end])


def _configure_docx_v2(document: Any, view: DocumentViewV2) -> None:
    _set_docx_page_geometry(document)
    _configure_docx_styles(document)
    callout = document.styles.add_style("Document Callout", WD_STYLE_TYPE.PARAGRAPH)
    callout.base_style = document.styles["Normal"]
    callout.font.name = "Calibri"
    callout.font.size = Pt(10.5)
    callout.font.color.rgb = RGBColor.from_string("1F3A5F")
    callout.paragraph_format.left_indent = Inches(0.18)
    callout.paragraph_format.space_before = Pt(5)
    callout.paragraph_format.space_after = Pt(7)
    for section in document.sections:
        section.header.paragraphs[0].text = f"{view.task_id} | 结构化视频文档"
        footer = section.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer.add_run("第 ")
        _add_page_field(footer)
        footer.add_run(" 页")


def _add_block_docx(
    document: Any,
    numbering: Any,
    abstract_ids: dict[str, int],
    block: ContentBlockViewV2,
) -> None:
    if block.type == "paragraph":
        document.add_paragraph(block.text, style="Normal")
    elif block.type == "callout":
        paragraph = document.add_paragraph(block.text, style="Document Callout")
        _shade(paragraph._p.get_or_add_pPr(), "F4F6F9")
    elif block.type == "list":
        kind = "decimal" if block.list_kind == "numbered" else "bullet"
        num_id = _add_numbering_from_one(numbering, abstract_ids[kind])
        style = "Document Numbered Evidence" if kind == "decimal" else "Document Bulleted Evidence"
        for item in block.items:
            paragraph = document.add_paragraph(item.evidence.text, style=style)
            _apply_numbering(paragraph, num_id)
    else:
        table = document.add_table(rows=1, cols=len(block.columns) + 1)
        for index, value in enumerate(("维度", *block.columns)):
            _set_cell_text(table.rows[0].cells[index], value, bold=True)
            _shade(table.rows[0].cells[index]._tc.get_or_add_tcPr(), "E8EEF5")
        for row in block.rows:
            cells = table.add_row().cells
            _set_cell_text(cells[0], row.dimension, bold=True)
            for index, cell in enumerate(row.cells, start=1):
                _set_cell_text(cells[index], cell.text)
        _configure_table(table, len(block.columns) + 1)
        document.add_paragraph("")


def render_document_docx_v2(view: DocumentViewV2) -> bytes:
    """由共享2.0视图确定性生成DOCX；不读取文件、不调用API。"""

    document = Document()
    _configure_docx_v2(document, view)
    core = document.core_properties
    core.title = view.title.text
    core.subject = "知识型视频结构化文档"
    core.identifier = f"semantic-projection-sha256:{semantic_projection_sha256_v2(view)}"
    core.author = core.last_modified_by = "video-doc"
    core.created = core.modified = datetime(2000, 1, 1)
    core.revision = 1
    numbering = document.part.numbering_part.element
    abstract_ids = {
        "decimal": _add_abstract_numbering(numbering, "decimal"),
        "bullet": _add_abstract_numbering(numbering, "bullet"),
    }

    document.add_paragraph(view.title.text, style="Title")
    if view.status_notice:
        document.add_paragraph(view.status_notice, style="Document Status")
    document.add_paragraph("视频概述", style="Heading 1")
    for block in view.overview:
        _add_block_docx(document, numbering, abstract_ids, block)
    if view.global_keywords:
        document.add_paragraph(
            "全文关键词：" + "、".join(item.text for item in view.global_keywords),
            style="Document Metadata",
        )
    document.add_paragraph("目录", style="Heading 1")
    toc_id = _add_numbering_from_one(numbering, abstract_ids["decimal"])
    for chapter in view.chapters:
        paragraph = document.add_paragraph(
            f"{chapter.title.text}（{chapter.start_text}–{chapter.end_text}）",
            style="Document TOC",
        )
        _apply_numbering(paragraph, toc_id)
    for chapter in view.chapters:
        chapter_heading = document.add_paragraph(
            f"{chapter.index}. {chapter.title.text}", style="Heading 1"
        )
        chapter_heading.paragraph_format.keep_with_next = True
        chapter_time = document.add_paragraph(
            f"时间：{chapter.start_text}–{chapter.end_text}", style="Document Metadata"
        )
        chapter_time.paragraph_format.keep_with_next = True
        for section in chapter.sections:
            if section.heading is not None:
                document.add_paragraph(section.heading.text, style="Heading 2")
            for block in section.blocks:
                _add_block_docx(document, numbering, abstract_ids, block)
    if view.overall_conclusion:
        document.add_paragraph("核心结论", style="Heading 1")
        for block in view.overall_conclusion:
            _add_block_docx(document, numbering, abstract_ids, block)
    buffer = io.BytesIO()
    document.save(buffer)
    return _normalize_docx_archive(buffer.getvalue())


def validate_document_docx_v2(payload: bytes, view: DocumentViewV2) -> dict[str, Any]:
    if not payload:
        _fail("DOCX必须是非空字节串。")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            required = {
                "[Content_Types].xml",
                "word/document.xml",
                "word/styles.xml",
                "word/numbering.xml",
                "docProps/core.xml",
            }
            if archive.testzip() is not None or not required.issubset(archive.namelist()):
                _fail("DOCX包结构损坏或缺少必需部件。")
            if "startOverride" not in archive.read("word/numbering.xml").decode("utf-8"):
                _fail("DOCX列表缺少显式从1开始的覆盖。")
        document = Document(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise DocumentExportV2Error("DOCX无法重开。") from exc
    all_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    required_text = [
        view.title.text,
        "视频概述",
        "目录",
        *(chapter.title.text for chapter in view.chapters),
    ]
    if any(text not in all_text for text in required_text):
        _fail("DOCX正文缺失共享视图语义。")
    forbidden_audit_headings = (
        "证据与时间索引",
        "标题、检索字段与结构来源",
        "覆盖审计",
        "生成与来源信息",
    )
    if any(text in all_text for text in forbidden_audit_headings):
        _fail("DOCX默认读者成品不得包含工程审计附录。")
    expected_tables = [
        block
        for chapter in view.chapters
        for section in chapter.sections
        for block in section.blocks
        if block.type == "comparison"
    ]
    if len(document.tables) != len(expected_tables):
        _fail("DOCX对比表数量与共享视图不一致。")
    for table, block in zip(document.tables, expected_tables, strict=True):
        expected = [
            ["维度", *block.columns],
            *[
                [row.dimension, *(cell.text for cell in row.cells)]
                for row in block.rows
            ],
        ]
        if [[cell.text for cell in row.cells] for row in table.rows] != expected:
            _fail("DOCX对比表语义漂移。")
    if document.core_properties.identifier != f"semantic-projection-sha256:{semantic_projection_sha256_v2(view)}":
        _fail("DOCX语义投影身份不一致。")
    section = document.sections[0]
    if section.header.paragraphs[0].text != f"{view.task_id} | 结构化视频文档" or "PAGE" not in section.footer._element.xml:
        _fail("DOCX页眉页脚无效。")
    return {
        "status": "valid",
        "paragraph_count": len(document.paragraphs),
        "table_count": len(document.tables),
        "chapter_count": len(view.chapters),
        "semantic_projection_sha256": semantic_projection_sha256_v2(view),
    }


_EXPORT_FILES_V2 = {
    "result.json": "application/json",
    "result.md": "text/markdown; charset=utf-8",
    "result.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_FAILURE_POINTS_V2 = {
    "after_result_json",
    "after_result_md",
    "after_result_docx",
    "after_manifest",
    "before_group_publish",
    "before_current_switch",
    "after_current_temp_write",
}


def _export_versions_v2() -> dict[str, str]:
    return {
        "document_view": DOCUMENT_V2_VIEW_VERSION,
        "json_renderer": DOCUMENT_V2_JSON_RENDERER_VERSION,
        "markdown_renderer": DOCUMENT_V2_MARKDOWN_RENDERER_VERSION,
        "docx_renderer": DOCUMENT_V2_DOCX_RENDERER_VERSION,
        "word_style": DOCUMENT_V2_WORD_STYLE_VERSION,
        "python_docx": DOCUMENT_V2_PYTHON_DOCX_VERSION,
        "publisher": DOCUMENT_V2_EXPORT_PUBLISHER_VERSION,
    }


def document_export_cache_key_v2(source: AuthoritativeDocumentContentV2) -> str:
    return canonical_sha256(
        {
            "source_content_id": source.content_id,
            "source_document_sha256": source.document_sha256,
            "source_files": {
                "document": source.document_file_sha256,
                "execution_report": source.execution_report_file_sha256,
                "manifest": source.manifest_file_sha256,
            },
            "export_versions": _export_versions_v2(),
        }
    )


def _file_payloads_v2(
    source: AuthoritativeDocumentContentV2, view: DocumentViewV2
) -> dict[str, bytes]:
    return {
        "result.json": render_structured_document_json_v2(source.document).encode("utf-8"),
        "result.md": render_document_markdown_v2(view).encode("utf-8"),
        "result.docx": render_document_docx_v2(view),
    }


def _manifest_v2(
    source: AuthoritativeDocumentContentV2,
    view: DocumentViewV2,
    files: dict[str, bytes],
) -> dict[str, Any]:
    cache_key = document_export_cache_key_v2(source)
    block_count = sum(
        len(section.blocks) for chapter in view.chapters for section in chapter.sections
    )
    return {
        "schema_version": DOCUMENT_V2_EXPORT_MANIFEST_SCHEMA_VERSION,
        "status": "success",
        "task_id": view.task_id,
        "export_id": f"export-{cache_key[:12]}",
        "cache_key": cache_key,
        "source_content": {
            "schema_version": "2.0",
            "content_id": source.content_id,
            "canonical_sha256": source.document_sha256,
            "document_file_sha256": source.document_file_sha256,
            "execution_report_file_sha256": source.execution_report_file_sha256,
            "manifest_file_sha256": source.manifest_file_sha256,
        },
        "export_versions": _export_versions_v2(),
        "structured_document_sha256": canonical_sha256(source.document),
        "semantic_projection_sha256": semantic_projection_sha256_v2(view),
        "chapter_count": len(view.chapters),
        "block_count": block_count,
        "files": {
            name: {
                "sha256": _file_sha256(files[name]),
                "bytes": len(files[name]),
                "mime_type": mime,
            }
            for name, mime in _EXPORT_FILES_V2.items()
        },
    }


def validate_document_export_group_v2(
    group_path: str | Path,
    *,
    source: AuthoritativeDocumentContentV2,
    require_directory_name: bool = True,
) -> DocumentExportGroupV2:
    group = Path(group_path)
    if (
        not group.is_dir()
        or {path.name for path in group.iterdir()} != set(_EXPORT_FILES_V2) | {"manifest.json"}
        or any(path.is_dir() for path in group.iterdir())
    ):
        _fail("2.0文档正式结果组文件集合不完整或包含额外成员。")
    manifest, manifest_raw = _read_json_bytes(group / "manifest.json", "2.0文档manifest")
    files = {name: (group / name).read_bytes() for name in _EXPORT_FILES_V2}
    try:
        document = json.loads(files["result.json"].decode("utf-8"))
        validate_structured_document_content(document)
    except (UnicodeError, json.JSONDecodeError, StructuredDocumentContentError) as exc:
        raise DocumentExportV2Error("2.0 result.json无效。") from exc
    if document != source.document:
        _fail("2.0结果组与权威内容不一致。")
    view = build_document_view_v2(document)
    expected = _manifest_v2(source, view, files)
    if manifest_raw != _json_bytes(manifest) or manifest != expected:
        _fail("2.0文档manifest与结果组或来源身份不一致。")
    if require_directory_name and group.name != manifest["export_id"]:
        _fail("2.0结果组目录与export_id不一致。")
    if (
        files["result.json"]
        != render_structured_document_json_v2(document).encode("utf-8")
        or files["result.md"] != render_document_markdown_v2(view).encode("utf-8")
    ):
        _fail("2.0 JSON或Markdown与共享视图不一致。")
    validate_document_docx_v2(files["result.docx"], view)
    if files["result.docx"] != render_document_docx_v2(view):
        _fail("2.0 DOCX不是当前版本确定性渲染。")
    return DocumentExportGroupV2(
        documents_root=group.parent.parent,
        group_path=group,
        current_path=group.parent.parent / "current.json",
        export_id=manifest["export_id"],
        cache_key=manifest["cache_key"],
        document=document,
        manifest=manifest,
        view=view,
        cache_hit=False,
    )


def load_current_document_export_v2(
    output_root: str | Path,
    task_id: str,
    *,
    source: AuthoritativeDocumentContentV2 | None = None,
) -> DocumentExportGroupV2:
    if source is None:
        source = load_authoritative_document_content_v2(output_root, task_id)
    root = Path(output_root).expanduser() / task_id / "documents"
    current, raw = _read_json_bytes(root / "current.json", "2.0文档current")
    expected_keys = {
        "schema_version",
        "task_id",
        "export_id",
        "cache_key",
        "manifest_sha256",
        "group",
        "source_content_id",
    }
    if (
        set(current) != expected_keys
        or raw != _json_bytes(current)
        or current.get("schema_version") != DOCUMENT_V2_EXPORT_CURRENT_SCHEMA_VERSION
        or current.get("task_id") != task_id
        or current.get("source_content_id") != source.content_id
    ):
        _fail("2.0文档current身份无效。")
    group = validate_document_export_group_v2(root / current["group"], source=source)
    if (
        current["export_id"] != group.export_id
        or current["cache_key"] != group.cache_key
        or current["manifest_sha256"] != file_sha256(group.group_path / "manifest.json")
    ):
        _fail("2.0文档current与正式结果组不一致。")
    return DocumentExportGroupV2(**{**group.__dict__, "cache_hit": True})


def _failure_audit_v2(
    root: Path,
    task_id: str,
    export_id: str,
    stage: str,
    exc: Exception,
    previous_export_id: str | None,
) -> None:
    failure_root = root / "failures" / f"failure-{uuid4().hex[:12]}"
    failure_root.mkdir(parents=True, exist_ok=False)
    message = (
        str(exc)
        if isinstance(exc, InjectedDocumentExportV2Failure)
        else "2.0文档结果组发布失败；未记录正文或底层异常详情。"
    )
    audit = {
        "schema_version": "2.0",
        "status": "failed",
        "task_id": task_id,
        "export_id": export_id,
        "failure_stage": stage,
        "error_type": type(exc).__name__,
        "message": message,
        "previous_export_id": previous_export_id,
    }
    _write_bytes_fsync(failure_root / "failure.json", _json_bytes(audit))


def publish_document_export_v2(
    output_root: str | Path,
    source: AuthoritativeDocumentContentV2,
    *,
    failure_injection: str | None = None,
) -> DocumentExportGroupV2:
    if failure_injection is not None and failure_injection not in _FAILURE_POINTS_V2:
        _fail("未知的2.0文档导出失败注入点。")
    view = build_document_view_v2(source.document)
    cache_key = document_export_cache_key_v2(source)
    export_id = f"export-{cache_key[:12]}"
    root = Path(output_root).expanduser() / view.task_id / "documents"
    formal = root / "formal-results"
    formal.mkdir(parents=True, exist_ok=True)
    group = formal / export_id
    current = root / "current.json"
    previous = current.read_bytes() if current.is_file() else None
    previous_export_id: str | None = None
    if previous:
        try:
            parsed = json.loads(previous.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("export_id"), str):
                previous_export_id = parsed["export_id"]
        except (UnicodeError, json.JSONDecodeError):
            pass

    def pointer(manifest_hash: str) -> bytes:
        return _json_bytes(
            {
                "schema_version": DOCUMENT_V2_EXPORT_CURRENT_SCHEMA_VERSION,
                "task_id": view.task_id,
                "export_id": export_id,
                "cache_key": cache_key,
                "manifest_sha256": manifest_hash,
                "group": f"formal-results/{export_id}",
                "source_content_id": source.content_id,
            }
        )

    if group.exists():
        cached = validate_document_export_group_v2(group, source=source)
        desired = pointer(file_sha256(group / "manifest.json"))
        if previous != desired:
            _atomic_replace_bytes(current, desired)
        return DocumentExportGroupV2(**{**cached.__dict__, "cache_hit": True})

    temp = root / f".tmp-{export_id}-{uuid4().hex}"
    created = False
    stage = "prepare"
    try:
        temp.mkdir(parents=True, exist_ok=False)
        files = _file_payloads_v2(source, view)
        for name, point in (
            ("result.json", "after_result_json"),
            ("result.md", "after_result_md"),
            ("result.docx", "after_result_docx"),
        ):
            stage = name
            _write_bytes_fsync(temp / name, files[name])
            if failure_injection == point:
                raise InjectedDocumentExportV2Failure(f"注入失败：{point}")
        manifest = _manifest_v2(source, view, files)
        stage = "manifest.json"
        _write_bytes_fsync(temp / "manifest.json", _json_bytes(manifest))
        if failure_injection == "after_manifest":
            raise InjectedDocumentExportV2Failure("注入失败：after_manifest")
        validate_document_export_group_v2(temp, source=source, require_directory_name=False)
        stage = "group_publish"
        if failure_injection == "before_group_publish":
            raise InjectedDocumentExportV2Failure("注入失败：before_group_publish")
        os.replace(temp, group)
        created = True
        published = validate_document_export_group_v2(group, source=source)
        stage = "current_switch"
        if failure_injection == "before_current_switch":
            raise InjectedDocumentExportV2Failure("注入失败：before_current_switch")
        _atomic_replace_bytes(
            current,
            pointer(file_sha256(group / "manifest.json")),
            inject_after_temp=failure_injection == "after_current_temp_write",
        )
        loaded = load_current_document_export_v2(output_root, view.task_id, source=source)
        return DocumentExportGroupV2(**{**loaded.__dict__, "cache_hit": False})
    except Exception as exc:
        if temp.exists():
            shutil.rmtree(temp)
        if created and group.exists():
            shutil.rmtree(group)
        if previous is not None:
            if not current.is_file() or current.read_bytes() != previous:
                _atomic_replace_bytes(current, previous)
        elif current.exists():
            current.unlink()
        _failure_audit_v2(root, view.task_id, export_id, stage, exc, previous_export_id)
        if isinstance(exc, DocumentExportV2Error):
            raise
        raise DocumentExportV2Error(
            f"2.0文档结果组发布失败：{type(exc).__name__}"
        ) from exc
