"""阶段10文档导出契约与阶段9权威输入加载。"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from uuid import uuid4

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from video_doc.asr_timeline import ASRTimelineError, load_task_timeline
from video_doc.chapter_content import (
    ChapterContentError,
    validate_chapter_content_bundle,
    validate_chapter_input_manifest,
)
from video_doc.task_workspace import TaskWorkspaceError, validate_task_id


STRUCTURED_VIDEO_DOCUMENT_SCHEMA_VERSION = "1.0"
DOCUMENT_FIELD_MAPPING_VERSION = "stage9-bundle-to-structured-document-1.1"
DOCUMENT_TIME_FORMAT_VERSION = "hms-millisecond-1.0"
DOCUMENT_TIME_DISPLAY_PATTERN = "HH:MM:SS.mmm"
DOCUMENT_TIME_RANGE_SEMANTICS = "[start,end)"
DOCUMENT_VIEW_VERSION = "structured-document-view-1.1"
DOCUMENT_JSON_RENDERER_VERSION = "structured-document-json-1.0"
DOCUMENT_MARKDOWN_RENDERER_VERSION = "structured-document-markdown-1.1"
DOCUMENT_DOCX_RENDERER_VERSION = "structured-document-docx-1.1"
DOCUMENT_WORD_STYLE_VERSION = "compact-reference-guide-1.0"
DOCUMENT_PYTHON_DOCX_VERSION = "1.2.0"
DOCUMENT_EXPORT_MANIFEST_SCHEMA_VERSION = "1.0"
DOCUMENT_EXPORT_PUBLISHER_VERSION = "document-export-publisher-1.0"
DOCUMENT_EXPORT_CURRENT_SCHEMA_VERSION = "1.0"


class DocumentExportError(RuntimeError):
    """阶段10契约或权威输入校验失败。"""


@dataclass(frozen=True)
class AuthoritativeChapterContent:
    """经过结果组、manifest和阶段9Schema复验的唯一内容输入。"""

    task_root: Path
    result_group_path: Path
    content_path: Path
    execution_report_path: Path
    manifest_path: Path
    input_manifest_path: Path
    result_id: str
    bundle: dict[str, Any]
    execution_report: dict[str, Any]
    input_manifest: dict[str, Any]
    content_canonical_sha256: str
    content_file_sha256: str
    execution_report_file_sha256: str
    manifest_file_sha256: str
    input_manifest_file_sha256: str


@dataclass(frozen=True)
class EvidenceTextView:
    text: str
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class ChapterDocumentView:
    chapter_id: str
    index: int
    start_seconds: int | float
    end_seconds: int | float
    start_text: str
    end_text: str
    planned_title: str
    title: EvidenceTextView
    summary: EvidenceTextView
    key_points: tuple[EvidenceTextView, ...]
    keywords: tuple[EvidenceTextView, ...]
    source_event_ids: tuple[str, ...]
    window_count: int


@dataclass(frozen=True)
class DocumentView:
    export_id: str
    document_title: str
    metadata: tuple[tuple[str, str], ...]
    status_notice: str
    source_details: tuple[tuple[str, str], ...]
    chapters: tuple[ChapterDocumentView, ...]


@dataclass(frozen=True)
class DocxParagraphSpec:
    role: str
    style_name: str
    text: str
    list_kind: str | None = None
    list_group: str | None = None
    page_break_before: bool = False
    keep_with_next: bool = False


@dataclass(frozen=True)
class DocumentExportGroup:
    documents_root: Path
    group_path: Path
    current_path: Path
    export_id: str
    cache_key: str
    document: dict[str, Any]
    manifest: dict[str, Any]
    view: DocumentView
    cache_hit: bool


class InjectedDocumentExportFailure(DocumentExportError):
    """仅供离线测试验证原子发布和旧产物保护。"""


def canonical_sha256(payload: Any) -> str:
    """计算与阶段8/9一致的规范化JSON SHA-256。"""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    """流式计算文件SHA-256。"""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise DocumentExportError(f"权威输入文件不存在：{path}") from exc
    except OSError as exc:
        raise DocumentExportError(f"无法读取权威输入文件：{path}") from exc
    return digest.hexdigest()


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DocumentExportError(f"{description}不存在：{path}") from exc
    except OSError as exc:
        raise DocumentExportError(f"无法读取{description}：{path}") from exc
    except UnicodeError as exc:
        raise DocumentExportError(f"{description}不是有效UTF-8文本：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DocumentExportError(f"{description}不是有效JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise DocumentExportError(f"{description}必须是JSON对象：{path}")
    return payload


def _valid_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise DocumentExportError(f"{field}必须是64位SHA-256。")
    return value.lower()


def _require_byte_alias(alias: Path, source: Path, description: str) -> None:
    try:
        if alias.read_bytes() != source.read_bytes():
            raise DocumentExportError(f"{description}与正式结果组不一致。")
    except FileNotFoundError as exc:
        raise DocumentExportError(f"{description}不存在：{alias}") from exc
    except OSError as exc:
        raise DocumentExportError(f"无法读取{description}：{alias}") from exc


def format_timestamp_milliseconds(value: int | float | Decimal) -> str:
    """按`HH:MM:SS.mmm`确定性格式化非负秒数。"""

    if isinstance(value, bool):
        raise DocumentExportError("时间值不能是布尔值。")
    if isinstance(value, float) and not math.isfinite(value):
        raise DocumentExportError("时间值必须是有限非负数。")
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DocumentExportError("时间值必须是有限非负数。") from exc
    if not seconds.is_finite() or seconds < 0:
        raise DocumentExportError("时间值必须是有限非负数。")

    rounded = seconds.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    total_milliseconds = int(rounded * 1000)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def load_authoritative_chapter_content(
    output_root: str | Path,
    task_id: str,
) -> AuthoritativeChapterContent:
    """只读加载并严格复验阶段9当前权威正式内容结果组。"""

    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise DocumentExportError(str(exc)) from exc

    task_root = Path(output_root).expanduser() / task_id
    content_root = task_root / "content"
    top_content_path = content_root / "content.json"
    top_report_path = content_root / "content-execution-report.json"
    top_bundle = _read_json(top_content_path, "阶段9顶层权威内容")
    top_report = _read_json(top_report_path, "阶段9顶层权威执行报告")

    result_id = top_report.get("result_id")
    if (
        top_report.get("schema_version") != "1.0"
        or top_report.get("task_id") != task_id
        or top_report.get("status") != "success"
        or not isinstance(result_id, str)
        or not result_id.startswith("content-")
    ):
        raise DocumentExportError("阶段9顶层权威执行报告任务、状态或结果ID无效。")

    result_group = content_root / "formal-results" / result_id
    group_content_path = result_group / "content.json"
    group_report_path = result_group / "execution-report.json"
    group_manifest_path = result_group / "manifest.json"
    group_bundle = _read_json(group_content_path, "阶段9正式结果组内容")
    group_report = _read_json(group_report_path, "阶段9正式结果组执行报告")
    group_manifest = _read_json(group_manifest_path, "阶段9正式结果组manifest")

    files = group_manifest.get("files")
    if (
        group_manifest.get("schema_version") != "1.0"
        or group_manifest.get("task_id") != task_id
        or group_manifest.get("result_id") != result_id
        or not isinstance(files, dict)
        or set(files) != {"content.json", "execution-report.json"}
    ):
        raise DocumentExportError("阶段9正式结果组manifest结构或来源无效。")

    content_file_hash = file_sha256(group_content_path)
    report_file_hash = file_sha256(group_report_path)
    if content_file_hash != _valid_sha256(files.get("content.json"), "content.json文件哈希"):
        raise DocumentExportError("阶段9正式结果组文件哈希不一致：content.json")
    if report_file_hash != _valid_sha256(
        files.get("execution-report.json"), "execution-report.json文件哈希"
    ):
        raise DocumentExportError(
            "阶段9正式结果组文件哈希不一致：execution-report.json"
        )

    _require_byte_alias(top_content_path, group_content_path, "阶段9顶层权威内容")
    _require_byte_alias(top_report_path, group_report_path, "阶段9顶层权威执行报告")
    if top_bundle != group_bundle or top_report != group_report:
        raise DocumentExportError("阶段9顶层权威产物与正式结果组JSON不一致。")

    canonical_hash = canonical_sha256(group_bundle)
    if (
        group_report.get("content_sha256") != canonical_hash
        or result_id != f"content-{canonical_hash[:12]}"
    ):
        raise DocumentExportError("阶段9内容规范哈希、结果ID或正式Bundle不一致。")
    if group_report.get("chapter_count") != len(group_bundle.get("chapters", [])):
        raise DocumentExportError("阶段9执行报告章节数与正式Bundle不一致。")

    source_plan = group_bundle.get("source_chapter_plan")
    source_timeline = group_bundle.get("source_timeline")
    if not isinstance(source_plan, dict) or not isinstance(source_timeline, dict):
        raise DocumentExportError("阶段9正式Bundle缺少有效上游来源。")
    source_result_id = source_plan.get("result_id")
    timeline_hash = _valid_sha256(source_timeline.get("sha256"), "时间轴规范哈希")
    if (
        not isinstance(source_result_id, str)
        or group_report.get("source_stage8_result_id") != source_result_id
    ):
        raise DocumentExportError("阶段9执行报告与Bundle的分章来源不一致。")

    input_manifest_path = (
        content_root
        / "attempts"
        / f"input-{source_result_id}-{timeline_hash[:12]}"
        / "chapter-input-manifest.json"
    )
    input_manifest = _read_json(input_manifest_path, "阶段9章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
        validate_chapter_input_manifest(input_manifest, timeline=timeline)
        validate_chapter_content_bundle(group_bundle, input_manifest=input_manifest)
    except (ASRTimelineError, ChapterContentError) as exc:
        raise DocumentExportError(f"阶段9正式内容Schema或来源复验失败：{exc}") from exc
    if group_report.get("input_manifest_cache_key") != input_manifest.get("cache_key"):
        raise DocumentExportError("阶段9执行报告与章节输入manifest缓存身份不一致。")

    return AuthoritativeChapterContent(
        task_root=task_root,
        result_group_path=result_group,
        content_path=group_content_path,
        execution_report_path=group_report_path,
        manifest_path=group_manifest_path,
        input_manifest_path=input_manifest_path,
        result_id=result_id,
        bundle=group_bundle,
        execution_report=group_report,
        input_manifest=input_manifest,
        content_canonical_sha256=canonical_hash,
        content_file_sha256=content_file_hash,
        execution_report_file_sha256=report_file_hash,
        manifest_file_sha256=file_sha256(group_manifest_path),
        input_manifest_file_sha256=file_sha256(input_manifest_path),
    )


def _export_versions() -> dict[str, str]:
    return {
        "field_mapping": DOCUMENT_FIELD_MAPPING_VERSION,
        "time_format": DOCUMENT_TIME_FORMAT_VERSION,
        "document_view": DOCUMENT_VIEW_VERSION,
        "json_renderer": DOCUMENT_JSON_RENDERER_VERSION,
        "markdown_renderer": DOCUMENT_MARKDOWN_RENDERER_VERSION,
        "docx_renderer": DOCUMENT_DOCX_RENDERER_VERSION,
        "word_style": DOCUMENT_WORD_STYLE_VERSION,
        "python_docx": DOCUMENT_PYTHON_DOCX_VERSION,
        "publisher": DOCUMENT_EXPORT_PUBLISHER_VERSION,
    }


def build_document_export_identity(document: dict[str, Any]) -> dict[str, Any]:
    """返回决定整个四文件结果组身份的唯一缓存输入。"""

    return {
        "schema_version": document["schema_version"],
        "task_id": document["task_id"],
        "source_content": deepcopy(document["source_content"]),
        "export_versions": deepcopy(document["export_versions"]),
    }


def document_export_cache_key(document: dict[str, Any]) -> str:
    return canonical_sha256(build_document_export_identity(document))


def build_structured_video_document(
    source: AuthoritativeChapterContent,
) -> dict[str, Any]:
    """从唯一阶段9权威输入无损构造三格式共用的语义对象。"""

    bundle = source.bundle
    chapters: list[dict[str, Any]] = []
    for chapter in bundle["chapters"]:
        chapters.append(
            {
                "chapter_id": chapter["chapter_id"],
                "index": chapter["index"],
                "time_range": {
                    "start_seconds": chapter["start"],
                    "end_seconds": chapter["end"],
                    "start_text": format_timestamp_milliseconds(chapter["start"]),
                    "end_text": format_timestamp_milliseconds(chapter["end"]),
                },
                "planned_title": chapter["planned_title"],
                "title": deepcopy(chapter["title"]),
                "summary": deepcopy(chapter["summary"]),
                "key_points": deepcopy(chapter["key_points"]),
                "keywords": deepcopy(chapter["keywords"]),
                "source_event_ids": deepcopy(chapter["source_event_ids"]),
                "window_count": chapter["window_count"],
            }
        )

    source_content = {
        "schema_version": bundle["schema_version"],
        "result_id": source.result_id,
        "canonical_sha256": source.content_canonical_sha256,
        "content_file_sha256": source.content_file_sha256,
        "execution_report_file_sha256": source.execution_report_file_sha256,
        "manifest_file_sha256": source.manifest_file_sha256,
        "input_manifest_file_sha256": source.input_manifest_file_sha256,
    }
    document = {
        "schema_version": STRUCTURED_VIDEO_DOCUMENT_SCHEMA_VERSION,
        "export_id": "",
        "task_id": bundle["task_id"],
        "document_title": f"{bundle['task_id']} 知识型视频结构化文档",
        "language": bundle["generation"]["output_language"],
        "strategy": bundle["strategy"],
        "source_content": source_content,
        "source_chapter_plan": deepcopy(bundle["source_chapter_plan"]),
        "source_timeline": deepcopy(bundle["source_timeline"]),
        "chaptering_provenance": deepcopy(bundle["chaptering_provenance"]),
        "status_notice": status_notice_for_provenance(bundle["chaptering_provenance"]),
        "generation": deepcopy(bundle["generation"]),
        "time_format": {
            "version": DOCUMENT_TIME_FORMAT_VERSION,
            "display_pattern": DOCUMENT_TIME_DISPLAY_PATTERN,
            "range_semantics": DOCUMENT_TIME_RANGE_SEMANTICS,
        },
        "export_versions": _export_versions(),
        "chapters": chapters,
    }
    cache_key = document_export_cache_key(document)
    document["export_id"] = f"export-{cache_key[:12]}"
    validate_structured_video_document(document)
    return document


def _require_keys(payload: dict[str, Any], expected: set[str], location: str) -> None:
    if set(payload) != expected:
        raise DocumentExportError(f"{location}字段集合不符合Schema。")


def _validate_evidence_text(
    payload: Any,
    chapter_event_ids: list[str],
    location: str,
) -> None:
    if not isinstance(payload, dict):
        raise DocumentExportError(f"{location}必须是对象。")
    _require_keys(payload, {"text", "source_event_ids"}, location)
    text = payload["text"]
    event_ids = payload["source_event_ids"]
    if not isinstance(text, str) or not text.strip():
        raise DocumentExportError(f"{location}.text必须是非空字符串。")
    if (
        not isinstance(event_ids, list)
        or not event_ids
        or any(not isinstance(item, str) for item in event_ids)
        or len(event_ids) != len(set(event_ids))
        or any(item not in chapter_event_ids for item in event_ids)
    ):
        raise DocumentExportError(f"{location}.source_event_ids无效或不可追溯。")


def validate_structured_video_document(document: Any) -> None:
    """严格校验阶段10唯一语义对象，防止格式渲染前发生漂移。"""

    if not isinstance(document, dict):
        raise DocumentExportError("StructuredVideoDocument必须是对象。")
    _require_keys(
        document,
        {
            "schema_version",
            "export_id",
            "task_id",
            "document_title",
            "language",
            "strategy",
            "source_content",
            "source_chapter_plan",
            "source_timeline",
            "chaptering_provenance",
            "status_notice",
            "generation",
            "time_format",
            "export_versions",
            "chapters",
        },
        "StructuredVideoDocument",
    )
    if document["schema_version"] != STRUCTURED_VIDEO_DOCUMENT_SCHEMA_VERSION:
        raise DocumentExportError("StructuredVideoDocument Schema版本无效。")
    for field in ("export_id", "task_id", "document_title", "language", "strategy"):
        if not isinstance(document[field], str) or not document[field].strip():
            raise DocumentExportError(f"{field}必须是非空字符串。")
    if not isinstance(document["status_notice"], str) or not document["status_notice"].strip():
        raise DocumentExportError("status_notice必须是非空字符串。")
    if not document["export_id"].startswith("export-"):
        raise DocumentExportError("export_id格式无效。")
    if document["export_versions"] != _export_versions():
        raise DocumentExportError("导出版本集合不匹配当前实现。")
    expected_export_id = f"export-{document_export_cache_key(document)[:12]}"
    if document["export_id"] != expected_export_id:
        raise DocumentExportError("export_id与单一缓存身份不一致。")
    if document["time_format"] != {
        "version": DOCUMENT_TIME_FORMAT_VERSION,
        "display_pattern": DOCUMENT_TIME_DISPLAY_PATTERN,
        "range_semantics": DOCUMENT_TIME_RANGE_SEMANTICS,
    }:
        raise DocumentExportError("时间格式契约无效。")

    source_content = document["source_content"]
    if not isinstance(source_content, dict):
        raise DocumentExportError("source_content必须是对象。")
    _require_keys(
        source_content,
        {
            "schema_version",
            "result_id",
            "canonical_sha256",
            "content_file_sha256",
            "execution_report_file_sha256",
            "manifest_file_sha256",
            "input_manifest_file_sha256",
        },
        "source_content",
    )
    for field in (
        "canonical_sha256",
        "content_file_sha256",
        "execution_report_file_sha256",
        "manifest_file_sha256",
        "input_manifest_file_sha256",
    ):
        _valid_sha256(source_content[field], f"source_content.{field}")
    if not isinstance(source_content["result_id"], str) or not source_content[
        "result_id"
    ].startswith("content-"):
        raise DocumentExportError("source_content.result_id无效。")
    for field in (
        "source_chapter_plan",
        "source_timeline",
        "chaptering_provenance",
        "generation",
    ):
        if not isinstance(document[field], dict):
            raise DocumentExportError(f"{field}必须是对象。")
    if document["generation"].get("output_language") != document["language"]:
        raise DocumentExportError("language与generation.output_language不一致。")
    if document["status_notice"] != status_notice_for_provenance(
        document["chaptering_provenance"]
    ):
        raise DocumentExportError("status_notice与分章来源状态不一致。")

    chapters = document["chapters"]
    if not isinstance(chapters, list) or not chapters:
        raise DocumentExportError("chapters必须是非空数组。")
    previous_end: int | float | None = None
    for expected_index, chapter in enumerate(chapters, start=1):
        location = f"chapters[{expected_index - 1}]"
        if not isinstance(chapter, dict):
            raise DocumentExportError(f"{location}必须是对象。")
        _require_keys(
            chapter,
            {
                "chapter_id",
                "index",
                "time_range",
                "planned_title",
                "title",
                "summary",
                "key_points",
                "keywords",
                "source_event_ids",
                "window_count",
            },
            location,
        )
        if chapter["index"] != expected_index or chapter["chapter_id"] != f"c{expected_index:04d}":
            raise DocumentExportError(f"{location}编号不连续。")
        time_range = chapter["time_range"]
        if not isinstance(time_range, dict):
            raise DocumentExportError(f"{location}.time_range必须是对象。")
        _require_keys(
            time_range,
            {"start_seconds", "end_seconds", "start_text", "end_text"},
            f"{location}.time_range",
        )
        start = time_range["start_seconds"]
        end = time_range["end_seconds"]
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or start < 0
            or end <= start
            or (previous_end is not None and start != previous_end)
        ):
            raise DocumentExportError(f"{location}时间范围无效或不连续。")
        if time_range["start_text"] != format_timestamp_milliseconds(start) or time_range[
            "end_text"
        ] != format_timestamp_milliseconds(end):
            raise DocumentExportError(f"{location}时间字符串与数值不一致。")
        previous_end = end
        if not isinstance(chapter["planned_title"], str) or not chapter["planned_title"].strip():
            raise DocumentExportError(f"{location}.planned_title必须是非空字符串。")
        event_ids = chapter["source_event_ids"]
        if (
            not isinstance(event_ids, list)
            or not event_ids
            or any(not isinstance(item, str) for item in event_ids)
            or len(event_ids) != len(set(event_ids))
        ):
            raise DocumentExportError(f"{location}.source_event_ids无效。")
        evidence_fields = [chapter["title"], chapter["summary"]]
        key_points = chapter["key_points"]
        keywords = chapter["keywords"]
        if not isinstance(key_points, list) or not key_points:
            raise DocumentExportError(f"{location}.key_points必须是非空数组。")
        if not isinstance(keywords, list) or not keywords:
            raise DocumentExportError(f"{location}.keywords必须是非空数组。")
        evidence_fields.extend(key_points)
        evidence_fields.extend(keywords)
        cited: set[str] = set()
        for evidence_index, evidence in enumerate(evidence_fields):
            _validate_evidence_text(evidence, event_ids, f"{location}.evidence[{evidence_index}]")
            cited.update(evidence["source_event_ids"])
        if cited != set(event_ids):
            raise DocumentExportError(f"{location}.source_event_ids与字段引用集合不一致。")
        if not isinstance(chapter["window_count"], int) or isinstance(
            chapter["window_count"], bool
        ) or chapter["window_count"] < 1:
            raise DocumentExportError(f"{location}.window_count无效。")


def _json_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def status_notice_for_provenance(provenance: Any) -> str:
    if not isinstance(provenance, dict):
        raise DocumentExportError("chaptering_provenance必须是对象。")
    result_source = provenance.get("result_source")
    quality_level = provenance.get("quality_level")
    if result_source == "primary" and quality_level == "normal":
        return "结果来源：primary；质量级别：normal。"
    if result_source == "fallback" and quality_level == "unknown":
        return "降级提示：结果来自技术fallback，质量级别unknown；请人工复核。"
    if result_source == "emergency" and quality_level == "degraded":
        return "降级提示：结果来自emergency单章结果，质量级别degraded；必须人工复核。"
    return f"来源提示：结果来源为{result_source}，质量级别为{quality_level}；请人工复核。"


def _flatten_leaves(value: Any, prefix: str) -> list[tuple[str, str]]:
    flattened: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            flattened.extend(_flatten_leaves(value[key], f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flattened.extend(_flatten_leaves(item, f"{prefix}[{index}]"))
    else:
        flattened.append((prefix, _json_scalar(value)))
    return flattened


def _evidence_view(payload: dict[str, Any]) -> EvidenceTextView:
    return EvidenceTextView(payload["text"], tuple(payload["source_event_ids"]))


def build_document_view(document: dict[str, Any]) -> DocumentView:
    """从唯一语义对象派生JSON/Markdown/Word共用的只读视图。"""

    validate_structured_video_document(document)
    metadata = (
        ("schema_version", document["schema_version"]),
        ("export_id", document["export_id"]),
        ("task_id", document["task_id"]),
        ("language", document["language"]),
        ("strategy", document["strategy"]),
    )
    source_details: list[tuple[str, str]] = []
    for field in (
        "source_content",
        "source_chapter_plan",
        "source_timeline",
        "chaptering_provenance",
        "generation",
        "time_format",
        "export_versions",
    ):
        source_details.extend(_flatten_leaves(document[field], field))
    chapters = tuple(
        ChapterDocumentView(
            chapter_id=chapter["chapter_id"],
            index=chapter["index"],
            start_seconds=chapter["time_range"]["start_seconds"],
            end_seconds=chapter["time_range"]["end_seconds"],
            start_text=chapter["time_range"]["start_text"],
            end_text=chapter["time_range"]["end_text"],
            planned_title=chapter["planned_title"],
            title=_evidence_view(chapter["title"]),
            summary=_evidence_view(chapter["summary"]),
            key_points=tuple(_evidence_view(item) for item in chapter["key_points"]),
            keywords=tuple(_evidence_view(item) for item in chapter["keywords"]),
            source_event_ids=tuple(chapter["source_event_ids"]),
            window_count=chapter["window_count"],
        )
        for chapter in document["chapters"]
    )
    return DocumentView(
        export_id=document["export_id"],
        document_title=document["document_title"],
        metadata=metadata,
        status_notice=document["status_notice"],
        source_details=tuple(source_details),
        chapters=chapters,
    )


def semantic_projection_from_document(document: dict[str, Any]) -> dict[str, Any]:
    """提取跨格式必须完全一致的语义投影。"""

    return semantic_projection_from_view(build_document_view(document))


def semantic_projection_from_view(view: DocumentView) -> dict[str, Any]:
    def evidence(item: EvidenceTextView) -> dict[str, Any]:
        return {"text": item.text, "source_event_ids": list(item.source_event_ids)}

    return {
        "export_id": view.export_id,
        "document_title": view.document_title,
        "metadata": [list(item) for item in view.metadata],
        "status_notice": view.status_notice,
        "source_details": [list(item) for item in view.source_details],
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "index": chapter.index,
                "start_seconds": chapter.start_seconds,
                "end_seconds": chapter.end_seconds,
                "start_text": chapter.start_text,
                "end_text": chapter.end_text,
                "planned_title": chapter.planned_title,
                "title": evidence(chapter.title),
                "summary": evidence(chapter.summary),
                "key_points": [evidence(item) for item in chapter.key_points],
                "keywords": [evidence(item) for item in chapter.keywords],
                "source_event_ids": list(chapter.source_event_ids),
                "window_count": chapter.window_count,
            }
            for chapter in view.chapters
        ],
    }


def semantic_projection_sha256(view: DocumentView) -> str:
    return canonical_sha256(semantic_projection_from_view(view))


def render_structured_document_json(document: dict[str, Any]) -> str:
    """确定性生成result.json文本；不执行文件写入。"""

    validate_structured_video_document(document)
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _markdown_text(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in "`*_{}[]<>#+-.!|":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped.replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")


def _citation_text(event_ids: tuple[str, ...]) -> str:
    return "、".join(f"`{_markdown_text(event_id)}`" for event_id in event_ids)


def render_document_markdown(view: DocumentView) -> str:
    """由共享DocumentView确定性生成result.md文本。"""

    lines = [f"# {_markdown_text(view.document_title)}", "", "## 文档元数据", ""]
    for key, value in view.metadata:
        lines.append(f"- `{key}`：{_markdown_text(value)}")
    lines.extend(["", f"> {_markdown_text(view.status_notice)}"])
    lines.extend(["", "## 来源与生成信息", ""])
    for key, value in view.source_details:
        lines.append(f"- `{key}`：`{_markdown_text(value)}`")
    lines.extend(["", "## 目录", ""])
    for chapter in view.chapters:
        lines.append(
            f"- {chapter.index}. {_markdown_text(chapter.title.text)} "
            f"（{chapter.start_text}–{chapter.end_text}）"
        )
    for chapter in view.chapters:
        lines.extend(
            [
                "",
                f"## {chapter.index}. {_markdown_text(chapter.title.text)}",
                "",
                f"- 时间范围：`{chapter.start_text}`–`{chapter.end_text}`（{DOCUMENT_TIME_RANGE_SEMANTICS}）",
                f"- 计划标题：{_markdown_text(chapter.planned_title)}",
                f"- 标题引用：{_citation_text(chapter.title.source_event_ids)}",
                "",
                "### 摘要",
                "",
                _markdown_text(chapter.summary.text),
                "",
                f"引用事件：{_citation_text(chapter.summary.source_event_ids)}",
                "",
                "### 知识点",
                "",
            ]
        )
        for index, item in enumerate(chapter.key_points, start=1):
            lines.append(
                f"{index}. {_markdown_text(item.text)}（引用：{_citation_text(item.source_event_ids)}）"
            )
        lines.extend(["", "### 关键词", ""])
        for item in chapter.keywords:
            lines.append(
                f"- {_markdown_text(item.text)}（引用：{_citation_text(item.source_event_ids)}）"
            )
        lines.extend(
            [
                "",
                f"章节引用事件：{_citation_text(chapter.source_event_ids)}",
                "",
                f"生成窗口数：{chapter.window_count}",
            ]
        )
    return "\n".join(lines) + "\n"


def _docx_citation_text(event_ids: tuple[str, ...]) -> str:
    return "、".join(event_ids)


def build_docx_paragraph_specs(view: DocumentView) -> tuple[DocxParagraphSpec, ...]:
    """把共享视图映射为Word段落节点；渲染和重开校验共用该序列。"""

    specs: list[DocxParagraphSpec] = [
        DocxParagraphSpec("title", "Title", view.document_title, keep_with_next=True),
        DocxParagraphSpec("section", "Heading 1", "文档元数据", keep_with_next=True),
    ]
    specs.extend(
        DocxParagraphSpec("metadata", "Document Metadata", f"{key}：{value}")
        for key, value in view.metadata
    )
    specs.append(DocxParagraphSpec("status_notice", "Document Status", view.status_notice))
    specs.append(
        DocxParagraphSpec("section", "Heading 1", "来源与生成信息", keep_with_next=True)
    )
    specs.extend(
        DocxParagraphSpec("source", "Document Metadata", f"{key}：{value}")
        for key, value in view.source_details
    )
    specs.append(DocxParagraphSpec("section", "Heading 1", "目录", keep_with_next=True))
    specs.extend(
        DocxParagraphSpec(
            "toc_item",
            "Document TOC",
            f"{chapter.title.text}（{chapter.start_text}–{chapter.end_text}）",
            list_kind="decimal",
            list_group="toc",
        )
        for chapter in view.chapters
    )
    for chapter in view.chapters:
        specs.extend(
            [
                DocxParagraphSpec(
                    "chapter_title",
                    "Heading 1",
                    f"{chapter.index}. {chapter.title.text}",
                    keep_with_next=True,
                ),
                DocxParagraphSpec(
                    "chapter_time",
                    "Document Metadata",
                    f"时间范围：{chapter.start_text}–{chapter.end_text}（{DOCUMENT_TIME_RANGE_SEMANTICS}）",
                    keep_with_next=True,
                ),
                DocxParagraphSpec(
                    "planned_title",
                    "Document Metadata",
                    f"计划标题：{chapter.planned_title}",
                ),
                DocxParagraphSpec(
                    "title_evidence",
                    "Document Evidence",
                    f"标题引用：{_docx_citation_text(chapter.title.source_event_ids)}",
                ),
                DocxParagraphSpec("subheading", "Heading 2", "摘要", keep_with_next=True),
                DocxParagraphSpec("summary", "Normal", chapter.summary.text),
                DocxParagraphSpec(
                    "summary_evidence",
                    "Document Evidence",
                    f"引用事件：{_docx_citation_text(chapter.summary.source_event_ids)}",
                ),
                DocxParagraphSpec("subheading", "Heading 2", "知识点", keep_with_next=True),
            ]
        )
        specs.extend(
            DocxParagraphSpec(
                "key_point",
                "Document Numbered Evidence",
                f"{item.text}（引用：{_docx_citation_text(item.source_event_ids)}）",
                list_kind="decimal",
                list_group=f"key-points-{chapter.chapter_id}",
            )
            for item in chapter.key_points
        )
        specs.append(
            DocxParagraphSpec("subheading", "Heading 2", "关键词", keep_with_next=True)
        )
        specs.extend(
            DocxParagraphSpec(
                "keyword",
                "Document Bulleted Evidence",
                f"{item.text}（引用：{_docx_citation_text(item.source_event_ids)}）",
                list_kind="bullet",
                list_group=f"keywords-{chapter.chapter_id}",
            )
            for item in chapter.keywords
        )
        specs.extend(
            [
                DocxParagraphSpec(
                    "chapter_evidence",
                    "Document Audit",
                    f"章节引用事件：{_docx_citation_text(chapter.source_event_ids)}",
                ),
                DocxParagraphSpec(
                    "window_count",
                    "Document Audit",
                    f"生成窗口数：{chapter.window_count}",
                ),
            ]
        )
    return tuple(specs)


def _set_style_font(style: Any, font_name: str, east_asia: str, size: float) -> None:
    style.font.name = font_name
    style.font.size = Pt(size)
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:ascii"), font_name)
    fonts.set(qn("w:hAnsi"), font_name)
    fonts.set(qn("w:eastAsia"), east_asia)


def _configure_docx_styles(document: Any) -> None:
    styles = document.styles
    normal = styles["Normal"]
    _set_style_font(normal, "Calibri", "微软雅黑", 11)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.widow_control = True

    title = styles["Title"]
    _set_style_font(title, "Calibri", "微软雅黑", 24)
    title.font.bold = True
    title.font.color.rgb = RGBColor.from_string("0B2545")
    title.paragraph_format.space_before = Pt(0)
    title.paragraph_format.space_after = Pt(12)
    title.paragraph_format.keep_with_next = True

    for name, size, color, before, after in (
        ("Heading 1", 16, "2E74B5", 18, 10),
        ("Heading 2", 13, "2E74B5", 14, 7),
        ("Heading 3", 12, "1F4D78", 10, 5),
    ):
        style = styles[name]
        _set_style_font(style, "Calibri", "微软雅黑", size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.keep_together = True

    custom_styles = (
        ("Document Metadata", 9, "555555", 0, 2, 1.0),
        ("Document Status", 10, "1F3A5F", 4, 8, 1.15),
        ("Document Evidence", 9, "555555", 0, 4, 1.0),
        ("Document Audit", 9, "555555", 0, 4, 1.0),
        ("Document TOC", 10.5, "0B2545", 0, 3, 1.15),
        ("Document Numbered Evidence", 11, "000000", 0, 4, 1.25),
        ("Document Bulleted Evidence", 11, "000000", 0, 4, 1.25),
    )
    for name, size, color, before, after, spacing in custom_styles:
        style = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = normal
        _set_style_font(style, "Calibri", "微软雅黑", size)
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = spacing
        style.paragraph_format.widow_control = True

    for style_name in ("Header", "Footer"):
        style = styles[style_name]
        _set_style_font(style, "Calibri", "微软雅黑", 9)
        style.font.color.rgb = RGBColor.from_string("666666")


def _next_numbering_id(numbering: Any, tag: str, attribute: str) -> int:
    values = [
        int(element.get(qn(attribute)))
        for element in numbering.findall(qn(tag))
        if element.get(qn(attribute)) is not None
    ]
    return max(values, default=0) + 1


def _add_abstract_numbering(numbering: Any, kind: str) -> int:
    abstract_id = _next_numbering_id(numbering, "w:abstractNum", "w:abstractNumId")
    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    multi = OxmlElement("w:multiLevelType")
    multi.set(qn("w:val"), "singleLevel")
    abstract.append(multi)
    level = OxmlElement("w:lvl")
    level.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:start")
    start.set(qn("w:val"), "1")
    level.append(start)
    num_format = OxmlElement("w:numFmt")
    num_format.set(qn("w:val"), "decimal" if kind == "decimal" else "bullet")
    level.append(num_format)
    level_text = OxmlElement("w:lvlText")
    level_text.set(qn("w:val"), "%1." if kind == "decimal" else "•")
    level.append(level_text)
    justification = OxmlElement("w:lvlJc")
    justification.set(qn("w:val"), "left")
    level.append(justification)
    paragraph_properties = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "num")
    tab.set(qn("w:pos"), "540")
    tabs.append(tab)
    paragraph_properties.append(tabs)
    indent = OxmlElement("w:ind")
    indent.set(qn("w:left"), "540")
    indent.set(qn("w:hanging"), "270")
    paragraph_properties.append(indent)
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:after"), "80")
    spacing.set(qn("w:line"), "300")
    spacing.set(qn("w:lineRule"), "auto")
    paragraph_properties.append(spacing)
    level.append(paragraph_properties)
    if kind == "bullet":
        run_properties = OxmlElement("w:rPr")
        fonts = OxmlElement("w:rFonts")
        fonts.set(qn("w:ascii"), "Arial")
        fonts.set(qn("w:hAnsi"), "Arial")
        run_properties.append(fonts)
        level.append(run_properties)
    abstract.append(level)
    numbering.append(abstract)
    return abstract_id


def _add_numbering_instance(numbering: Any, abstract_id: int) -> int:
    num_id = _next_numbering_id(numbering, "w:num", "w:numId")
    number = OxmlElement("w:num")
    number.set(qn("w:numId"), str(num_id))
    abstract_reference = OxmlElement("w:abstractNumId")
    abstract_reference.set(qn("w:val"), str(abstract_id))
    number.append(abstract_reference)
    numbering.append(number)
    return num_id


def _apply_numbering(paragraph: Any, num_id: int) -> None:
    paragraph_properties = paragraph._p.get_or_add_pPr()
    number_properties = paragraph_properties.find(qn("w:numPr"))
    if number_properties is None:
        number_properties = OxmlElement("w:numPr")
        paragraph_properties.append(number_properties)
    level = OxmlElement("w:ilvl")
    level.set(qn("w:val"), "0")
    number_reference = OxmlElement("w:numId")
    number_reference.set(qn("w:val"), str(num_id))
    number_properties.append(level)
    number_properties.append(number_reference)


def _set_docx_page_geometry(document: Any) -> None:
    for section in document.sections:
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
        section.top_margin = Inches(1)
        section.right_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.header_distance = Inches(0.492)
        section.footer_distance = Inches(0.492)


def _add_page_field(paragraph: Any) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    display = OxmlElement("w:t")
    display.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instruction, separate, display, end):
        run._r.append(element)


def _configure_docx_header_footer(document: Any, view: DocumentView) -> None:
    task_id = dict(view.metadata)["task_id"]
    for section in document.sections:
        header = section.header.paragraphs[0]
        header.style = document.styles["Header"]
        header.text = f"{task_id} | 知识型视频结构化文档"
        footer = section.footer.paragraphs[0]
        footer.style = document.styles["Footer"]
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer.add_run("第 ")
        _add_page_field(footer)
        footer.add_run(" 页")


def _normalize_docx_archive(payload: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(payload), "r")
    destination_buffer = io.BytesIO()
    with source, zipfile.ZipFile(
        destination_buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as destination:
        for name in sorted(source.namelist()):
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            destination.writestr(info, source.read(name))
    return destination_buffer.getvalue()


def render_document_docx(view: DocumentView) -> bytes:
    """由共享DocumentView确定性生成DOCX字节；不执行文件写入。"""

    document = Document()
    _set_docx_page_geometry(document)
    _configure_docx_styles(document)
    _configure_docx_header_footer(document, view)
    core = document.core_properties
    core.title = view.document_title
    core.subject = "知识型视频结构化文档"
    core.identifier = f"semantic-projection-sha256:{semantic_projection_sha256(view)}"
    core.author = "video-doc"
    core.last_modified_by = "video-doc"
    core.created = datetime(2000, 1, 1)
    core.modified = datetime(2000, 1, 1)
    core.revision = 1

    numbering = document.part.numbering_part.element
    abstract_ids = {
        "decimal": _add_abstract_numbering(numbering, "decimal"),
        "bullet": _add_abstract_numbering(numbering, "bullet"),
    }
    group_num_ids: dict[str, int] = {}
    for spec in build_docx_paragraph_specs(view):
        paragraph = document.add_paragraph(style=spec.style_name)
        paragraph.add_run(spec.text)
        paragraph.paragraph_format.keep_with_next = spec.keep_with_next
        paragraph.paragraph_format.page_break_before = spec.page_break_before
        if spec.list_kind is not None and spec.list_group is not None:
            if spec.list_group not in group_num_ids:
                group_num_ids[spec.list_group] = _add_numbering_instance(
                    numbering, abstract_ids[spec.list_kind]
                )
            _apply_numbering(paragraph, group_num_ids[spec.list_group])

    buffer = io.BytesIO()
    document.save(buffer)
    return _normalize_docx_archive(buffer.getvalue())


def validate_document_docx(payload: bytes, view: DocumentView) -> dict[str, Any]:
    """重开DOCX并校验包结构、段落语义、样式、列表、页眉页脚和投影身份。"""

    if not isinstance(payload, bytes) or not payload:
        raise DocumentExportError("DOCX必须是非空字节串。")
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            bad_file = archive.testzip()
            names = set(archive.namelist())
            if bad_file is not None:
                raise DocumentExportError(f"DOCX ZIP成员损坏：{bad_file}")
            required = {
                "[Content_Types].xml",
                "word/document.xml",
                "word/styles.xml",
                "word/numbering.xml",
                "docProps/core.xml",
            }
            if not required.issubset(names):
                raise DocumentExportError("DOCX缺少必需OOXML部件。")
            if not any(name.startswith("word/header") for name in names) or not any(
                name.startswith("word/footer") for name in names
            ):
                raise DocumentExportError("DOCX缺少页眉或页脚部件。")
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentExportError("DOCX不是可重开的有效ZIP包。") from exc

    try:
        document = Document(io.BytesIO(payload))
    except Exception as exc:
        raise DocumentExportError("python-docx无法重新打开DOCX。") from exc
    expected_specs = build_docx_paragraph_specs(view)
    actual_paragraphs = document.paragraphs
    if len(actual_paragraphs) != len(expected_specs):
        raise DocumentExportError("DOCX段落数量与共享展示树不一致。")
    numbered_count = 0
    for paragraph, spec in zip(actual_paragraphs, expected_specs, strict=True):
        if paragraph.text != spec.text or paragraph.style.name != spec.style_name:
            raise DocumentExportError(f"DOCX段落语义或样式漂移：{spec.role}")
        has_numbering = paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None
        if has_numbering != (spec.list_kind is not None):
            raise DocumentExportError(f"DOCX真实列表结构不一致：{spec.role}")
        if has_numbering:
            numbered_count += 1
        if paragraph.paragraph_format.page_break_before != spec.page_break_before:
            raise DocumentExportError(f"DOCX分页属性不一致：{spec.role}")
        if paragraph.paragraph_format.keep_with_next != spec.keep_with_next:
            raise DocumentExportError(f"DOCX段落连带属性不一致：{spec.role}")

    section = document.sections[0]
    geometry = (
        section.page_width,
        section.page_height,
        section.top_margin,
        section.right_margin,
        section.bottom_margin,
        section.left_margin,
        section.header_distance,
        section.footer_distance,
    )
    expected_geometry = tuple(
        int(value)
        for value in (
            Inches(8.5),
            Inches(11),
            Inches(1),
            Inches(1),
            Inches(1),
            Inches(1),
            Inches(0.492),
            Inches(0.492),
        )
    )
    if any(
        abs(int(actual) - expected) > 635
        for actual, expected in zip(geometry, expected_geometry, strict=True)
    ):
        raise DocumentExportError("DOCX页面尺寸或边距不符合固定样式。")
    task_id = dict(view.metadata)["task_id"]
    if section.header.paragraphs[0].text != f"{task_id} | 知识型视频结构化文档":
        raise DocumentExportError("DOCX页眉不一致。")
    footer_xml = section.footer._element.xml
    if "PAGE" not in footer_xml or "第 " not in section.footer.paragraphs[0].text:
        raise DocumentExportError("DOCX页脚缺少真实页码字段。")
    expected_projection = semantic_projection_sha256(view)
    if document.core_properties.identifier != f"semantic-projection-sha256:{expected_projection}":
        raise DocumentExportError("DOCX语义投影身份不一致。")

    normal = document.styles["Normal"]
    if normal.font.name != "Calibri" or normal.font.size != Pt(11):
        raise DocumentExportError("DOCX正文样式不符合固定预设。")
    if normal.paragraph_format.line_spacing != 1.25:
        raise DocumentExportError("DOCX正文行距不符合固定预设。")
    return {
        "status": "valid",
        "paragraph_count": len(actual_paragraphs),
        "numbered_paragraph_count": numbered_count,
        "chapter_count": len(view.chapters),
        "semantic_projection_sha256": expected_projection,
    }


_EXPORT_FILE_SPECS = {
    "result.json": "application/json",
    "result.md": "text/markdown; charset=utf-8",
    "result.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_FAILURE_INJECTION_POINTS = {
    "after_result_json",
    "after_result_md",
    "after_result_docx",
    "after_manifest",
    "before_group_publish",
    "before_current_switch",
    "after_current_temp_write",
}


def _bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_bytes_fsync(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())


def _atomic_replace_bytes(path: Path, payload: bytes, *, inject_after_temp: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid4().hex}"
    try:
        _write_bytes_fsync(temporary, payload)
        if inject_after_temp:
            raise InjectedDocumentExportFailure("注入失败：after_current_temp_write")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _inject_failure(selected: str | None, point: str) -> None:
    if selected == point:
        raise InjectedDocumentExportFailure(f"注入失败：{point}")


def _build_export_file_payloads(
    document: dict[str, Any],
    view: DocumentView,
) -> dict[str, bytes]:
    return {
        "result.json": render_structured_document_json(document).encode("utf-8"),
        "result.md": render_document_markdown(view).encode("utf-8"),
        "result.docx": render_document_docx(view),
    }


def _build_document_export_manifest(
    document: dict[str, Any],
    view: DocumentView,
    files: dict[str, bytes],
) -> dict[str, Any]:
    cache_key = document_export_cache_key(document)
    return {
        "schema_version": DOCUMENT_EXPORT_MANIFEST_SCHEMA_VERSION,
        "status": "success",
        "task_id": document["task_id"],
        "export_id": document["export_id"],
        "cache_key": cache_key,
        "source_content": deepcopy(document["source_content"]),
        "export_versions": deepcopy(document["export_versions"]),
        "structured_document_sha256": canonical_sha256(document),
        "semantic_projection_sha256": semantic_projection_sha256(view),
        "chapter_count": len(view.chapters),
        "files": {
            name: {
                "sha256": _bytes_sha256(files[name]),
                "bytes": len(files[name]),
                "mime_type": mime_type,
            }
            for name, mime_type in _EXPORT_FILE_SPECS.items()
        },
    }


def _load_json_bytes(path: Path, description: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise DocumentExportError(f"{description}不存在：{path}") from exc
    except OSError as exc:
        raise DocumentExportError(f"无法读取{description}：{path}") from exc
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentExportError(f"{description}不是有效UTF-8 JSON：{path}") from exc
    if not isinstance(parsed, dict):
        raise DocumentExportError(f"{description}必须是JSON对象：{path}")
    return parsed, payload


def validate_document_export_group(
    group_path: str | Path,
    *,
    expected_document: dict[str, Any] | None = None,
    require_directory_name: bool = True,
) -> DocumentExportGroup:
    """严格验证不可拆分的result.json/result.md/result.docx/manifest结果组。"""

    group = Path(group_path)
    if not group.is_dir():
        raise DocumentExportError(f"文档正式结果组不存在：{group}")
    actual_files = {path.name for path in group.iterdir() if path.is_file()}
    expected_files = set(_EXPORT_FILE_SPECS) | {"manifest.json"}
    if actual_files != expected_files or any(path.is_dir() for path in group.iterdir()):
        raise DocumentExportError("文档正式结果组文件集合不完整或包含额外成员。")

    manifest, manifest_bytes = _load_json_bytes(group / "manifest.json", "文档manifest")
    required_manifest_keys = {
        "schema_version",
        "status",
        "task_id",
        "export_id",
        "cache_key",
        "source_content",
        "export_versions",
        "structured_document_sha256",
        "semantic_projection_sha256",
        "chapter_count",
        "files",
    }
    if set(manifest) != required_manifest_keys:
        raise DocumentExportError("文档manifest字段集合无效。")
    if (
        manifest["schema_version"] != DOCUMENT_EXPORT_MANIFEST_SCHEMA_VERSION
        or manifest["status"] != "success"
        or not isinstance(manifest["files"], dict)
        or set(manifest["files"]) != set(_EXPORT_FILE_SPECS)
    ):
        raise DocumentExportError("文档manifest Schema、状态或文件集合无效。")

    file_payloads: dict[str, bytes] = {}
    for name, mime_type in _EXPORT_FILE_SPECS.items():
        try:
            payload = (group / name).read_bytes()
        except OSError as exc:
            raise DocumentExportError(f"无法读取文档结果文件：{name}") from exc
        record = manifest["files"].get(name)
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "bytes", "mime_type"}
            or record["sha256"] != _bytes_sha256(payload)
            or record["bytes"] != len(payload)
            or record["mime_type"] != mime_type
        ):
            raise DocumentExportError(f"文档结果文件身份不一致：{name}")
        file_payloads[name] = payload

    try:
        document = json.loads(file_payloads["result.json"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentExportError("result.json不是有效UTF-8 JSON。") from exc
    validate_structured_video_document(document)
    if expected_document is not None and document != expected_document:
        raise DocumentExportError("正式结果组与预期StructuredVideoDocument不一致。")
    view = build_document_view(document)
    cache_key = document_export_cache_key(document)
    expected_manifest = _build_document_export_manifest(document, view, file_payloads)
    if manifest != expected_manifest:
        raise DocumentExportError("文档manifest与结果对象、版本或语义投影不一致。")
    if manifest["export_id"] != f"export-{cache_key[:12]}" or (
        require_directory_name and group.name != manifest["export_id"]
    ):
        raise DocumentExportError("结果组目录、export_id和缓存键不一致。")
    if file_payloads["result.json"] != render_structured_document_json(document).encode(
        "utf-8"
    ):
        raise DocumentExportError("result.json不是当前版本的确定性序列化。")
    if file_payloads["result.md"] != render_document_markdown(view).encode("utf-8"):
        raise DocumentExportError("result.md与共享DocumentView不一致。")
    validate_document_docx(file_payloads["result.docx"], view)
    if file_payloads["result.docx"] != render_document_docx(view):
        raise DocumentExportError("result.docx与共享DocumentView或固定渲染版本不一致。")

    documents_root = group.parent.parent
    return DocumentExportGroup(
        documents_root=documents_root,
        group_path=group,
        current_path=documents_root / "current.json",
        export_id=manifest["export_id"],
        cache_key=cache_key,
        document=document,
        manifest=manifest,
        view=view,
        cache_hit=False,
    )


def load_current_document_export(
    output_root: str | Path,
    task_id: str,
    *,
    expected_document: dict[str, Any] | None = None,
) -> DocumentExportGroup:
    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise DocumentExportError(str(exc)) from exc
    documents_root = Path(output_root).expanduser() / task_id / "documents"
    current_path = documents_root / "current.json"
    current, _ = _load_json_bytes(current_path, "文档当前指针")
    if set(current) != {
        "schema_version",
        "task_id",
        "export_id",
        "cache_key",
        "manifest_sha256",
        "group",
    }:
        raise DocumentExportError("文档当前指针字段集合无效。")
    export_id = current.get("export_id")
    if (
        current.get("schema_version") != DOCUMENT_EXPORT_CURRENT_SCHEMA_VERSION
        or current.get("task_id") != task_id
        or not isinstance(export_id, str)
        or current.get("group") != f"formal-results/{export_id}"
    ):
        raise DocumentExportError("文档当前指针任务或结果组身份无效。")
    group = validate_document_export_group(
        documents_root / "formal-results" / export_id,
        expected_document=expected_document,
    )
    if (
        current["cache_key"] != group.cache_key
        or current["manifest_sha256"] != file_sha256(group.group_path / "manifest.json")
    ):
        raise DocumentExportError("文档当前指针与正式结果组不一致。")
    return DocumentExportGroup(**{**group.__dict__, "cache_hit": True})


def _write_failure_audit(
    documents_root: Path,
    *,
    task_id: str,
    export_id: str,
    failure_stage: str,
    error: Exception,
    previous_export_id: str | None,
) -> Path:
    failure_id = f"failure-{uuid4().hex[:12]}"
    failure_root = documents_root / "failures" / failure_id
    failure_root.mkdir(parents=True, exist_ok=False)
    message = (
        str(error)
        if isinstance(error, InjectedDocumentExportFailure)
        else "文档结果组发布失败；未记录正文、文件路径或底层异常详情。"
    )
    audit = {
        "schema_version": "1.0",
        "status": "failed",
        "failure_id": failure_id,
        "task_id": task_id,
        "export_id": export_id,
        "failure_stage": failure_stage,
        "error_type": type(error).__name__,
        "message": message,
        "previous_export_id": previous_export_id,
    }
    path = failure_root / "failure.json"
    _write_bytes_fsync(path, _json_bytes(audit))
    return path


def publish_document_export(
    output_root: str | Path,
    source: AuthoritativeChapterContent,
    *,
    failure_injection: str | None = None,
) -> DocumentExportGroup:
    """将三种格式作为一个不可拆分结果组原子发布；仅P10-5接入真实CLI。"""

    if failure_injection is not None and failure_injection not in _FAILURE_INJECTION_POINTS:
        raise DocumentExportError("未知的文档导出失败注入点。")
    document = build_structured_video_document(source)
    view = build_document_view(document)
    task_id = document["task_id"]
    documents_root = Path(output_root).expanduser() / task_id / "documents"
    formal_root = documents_root / "formal-results"
    formal_root.mkdir(parents=True, exist_ok=True)
    group_path = formal_root / document["export_id"]
    current_path = documents_root / "current.json"
    previous_current_bytes = current_path.read_bytes() if current_path.is_file() else None
    previous_export_id: str | None = None
    if previous_current_bytes is not None:
        try:
            previous_current = json.loads(previous_current_bytes.decode("utf-8"))
            if isinstance(previous_current, dict) and isinstance(
                previous_current.get("export_id"), str
            ):
                previous_export_id = previous_current["export_id"]
        except (UnicodeError, json.JSONDecodeError):
            previous_export_id = None

    if group_path.exists():
        cached = validate_document_export_group(group_path, expected_document=document)
        pointer = {
            "schema_version": DOCUMENT_EXPORT_CURRENT_SCHEMA_VERSION,
            "task_id": task_id,
            "export_id": cached.export_id,
            "cache_key": cached.cache_key,
            "manifest_sha256": file_sha256(cached.group_path / "manifest.json"),
            "group": f"formal-results/{cached.export_id}",
        }
        pointer_bytes = _json_bytes(pointer)
        if previous_current_bytes != pointer_bytes:
            _atomic_replace_bytes(current_path, pointer_bytes)
        return DocumentExportGroup(**{**cached.__dict__, "cache_hit": True})

    temp_root = documents_root / f".tmp-{document['export_id']}-{uuid4().hex}"
    created_group = False
    failure_stage = "prepare"
    try:
        temp_root.mkdir(parents=True, exist_ok=False)
        files = _build_export_file_payloads(document, view)
        for name, injection_point in (
            ("result.json", "after_result_json"),
            ("result.md", "after_result_md"),
            ("result.docx", "after_result_docx"),
        ):
            failure_stage = name
            _write_bytes_fsync(temp_root / name, files[name])
            _inject_failure(failure_injection, injection_point)
        manifest = _build_document_export_manifest(document, view, files)
        failure_stage = "manifest.json"
        _write_bytes_fsync(temp_root / "manifest.json", _json_bytes(manifest))
        _inject_failure(failure_injection, "after_manifest")
        validate_document_export_group(
            temp_root,
            expected_document=document,
            require_directory_name=False,
        )
        failure_stage = "group_publish"
        _inject_failure(failure_injection, "before_group_publish")
        os.replace(temp_root, group_path)
        created_group = True
        published = validate_document_export_group(group_path, expected_document=document)
        pointer = {
            "schema_version": DOCUMENT_EXPORT_CURRENT_SCHEMA_VERSION,
            "task_id": task_id,
            "export_id": published.export_id,
            "cache_key": published.cache_key,
            "manifest_sha256": file_sha256(published.group_path / "manifest.json"),
            "group": f"formal-results/{published.export_id}",
        }
        failure_stage = "current_switch"
        _inject_failure(failure_injection, "before_current_switch")
        _atomic_replace_bytes(
            current_path,
            _json_bytes(pointer),
            inject_after_temp=failure_injection == "after_current_temp_write",
        )
        loaded = load_current_document_export(
            output_root,
            task_id,
            expected_document=document,
        )
        return DocumentExportGroup(**{**loaded.__dict__, "cache_hit": False})
    except Exception as exc:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        if created_group and group_path.exists():
            shutil.rmtree(group_path)
        if previous_current_bytes is not None:
            if not current_path.is_file() or current_path.read_bytes() != previous_current_bytes:
                _atomic_replace_bytes(current_path, previous_current_bytes)
        elif current_path.exists():
            current_path.unlink()
        _write_failure_audit(
            documents_root,
            task_id=task_id,
            export_id=document["export_id"],
            failure_stage=failure_stage,
            error=exc,
            previous_export_id=previous_export_id,
        )
        if isinstance(exc, DocumentExportError):
            raise
        raise DocumentExportError(f"文档结果组发布失败：{type(exc).__name__}") from exc
