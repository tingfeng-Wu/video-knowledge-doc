"""阶段9-R4全文离线编排、完整2.0装配与preview原子结果组。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from video_doc.document_chapter_composition import (
    DOCUMENT_CHAPTER_MOCK_MODEL,
    DOCUMENT_CHAPTER_PROMPT_VERSION,
    DeterministicDocumentChapterMockTransport,
    DocumentChapterCompositionError,
    OfflineChapterCompositionResult,
    ValidatedChapterComposition,
    _chapter_context,
    _evidence_leaves,
    _source_maps,
    _validate_high_risk_text,
    compose_document_chapters,
    split_document_chapter_events,
)
from video_doc.document_composition import (
    DocumentCompositionError,
    validate_document_composition_input,
)
from video_doc.llm_adapter import (
    LLMAdapterError,
    LLMCallResult,
    LLMConfig,
    LLMRetryableError,
    LLMTransport,
    OpenAICompatibleTransport,
    call_task_llm,
    load_llm_config,
)
from video_doc.structured_document_content import (
    DOCUMENT_CONTENT_VALIDATOR_VERSION,
    STRUCTURED_DOCUMENT_CONTENT_SCHEMA_VERSION,
    DocumentContentValidationContext,
    StructuredDocumentContentError,
    _validate_block,
    _validate_evidence_text,
    attach_structured_document_content_id,
    canonical_sha256,
    document_content_versions,
    validate_structured_document_content,
)


DOCUMENT_GLOBAL_RESPONSE_SCHEMA_VERSION = "2.0"
DOCUMENT_GLOBAL_PROMPT_VERSION = "document-global-prompt-2.0"
DOCUMENT_GLOBAL_VALIDATION_ID = "document-global-validator-2-0"
DOCUMENT_GLOBAL_CACHE_NAMESPACE = "document-global-composition-json-object-2-0"
DOCUMENT_GLOBAL_MOCK_MODEL = "deterministic-document-global-fixture"
DOCUMENT_COMPOSITION_CONTENT_POLICY_VERSION = "document-asr-evidence-only-2.0"
DOCUMENT_PREVIEW_REPORT_SCHEMA_VERSION = "1.0"
DOCUMENT_PREVIEW_MANIFEST_SCHEMA_VERSION = "1.0"
DOCUMENT_PREVIEW_PUBLISHER_VERSION = "document-preview-publisher-2.0"
DOCUMENT_PREVIEW_FAILURE_AUDIT_SCHEMA_VERSION = "1.0"
DOCUMENT_FORMAL_CURRENT_SCHEMA_VERSION = "1.0"
DOCUMENT_FORMAL_PUBLISHER_VERSION = "document-formal-publisher-2.0"
DOCUMENT_REAL_MAX_BUSINESS_CALLS = 6
DOCUMENT_REAL_MAX_SUPPLIER_REQUESTS = 12

_PREVIEW_FILES = {
    "document-content.json": "application/json",
    "execution-report.json": "application/json",
}
_PREVIEW_FAILURE_INJECTION_POINTS = {
    "after_document_content",
    "after_execution_report",
    "after_manifest",
    "before_group_publish",
    "after_group_publish",
}


class DocumentContentPreviewError(RuntimeError):
    """R4全文编排、装配或preview结果组不符合冻结契约。"""


class InjectedDocumentContentPreviewFailure(DocumentContentPreviewError):
    """仅供R5离线事务测试的受控preview发布故障。"""


@dataclass(frozen=True)
class ValidatedGlobalComposition:
    title: dict[str, Any]
    overview: tuple[dict[str, Any], ...]
    overall_conclusion: tuple[dict[str, Any], ...]
    block_evidence: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]


@dataclass(frozen=True)
class DocumentPreviewGroup:
    group_path: Path
    document_path: Path
    execution_report_path: Path
    manifest_path: Path
    document: dict[str, Any]
    execution_report: dict[str, Any]
    manifest: dict[str, Any]
    cache_hit: bool


@dataclass(frozen=True)
class OfflineDocumentPreviewResult:
    input_id: str
    content_id: str
    document: dict[str, Any]
    chapter_result: OfflineChapterCompositionResult
    global_call: LLMCallResult
    preview: DocumentPreviewGroup
    business_call_count: int
    supplier_request_count: int
    cache_hit_count: int
    all_call_cache_hit: bool


@dataclass(frozen=True)
class DocumentFormalGroup:
    group: DocumentPreviewGroup
    current_path: Path
    current: dict[str, Any]
    cache_hit: bool


def _fail(message: str) -> None:
    raise DocumentContentPreviewError(message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        _fail(f"{location}字段集合不符合R4契约。")


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ordered_unique(groups: Sequence[Sequence[str]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _all_source_maps(
    composition_input: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    fields: dict[str, Mapping[str, Any]] = {}
    events: dict[str, Mapping[str, Any]] = {}
    for chapter in composition_input["chapters"]:
        chapter_fields, chapter_events = _source_maps(chapter)
        fields.update(chapter_fields)
        events.update(chapter_events)
    return fields, events


def _validated_chapter_payloads(
    chapter_result: OfflineChapterCompositionResult,
) -> list[dict[str, Any]]:
    return [
        {
            "chapter": validated.chapter,
            "coverage": validated.coverage,
            "block_evidence": {
                block_id: {
                    "source_content_paths": list(paths),
                    "source_event_ids": list(event_ids),
                }
                for block_id, (paths, event_ids) in validated.block_evidence.items()
            },
        }
        for validated in chapter_result.chapters
    ]


def _allowed_chapter_evidence(
    chapters: Sequence[ValidatedChapterComposition],
) -> tuple[set[str], set[str]]:
    paths: set[str] = set()
    event_ids: set[str] = set()
    for validated in chapters:
        for _, leaf in _evidence_leaves(validated.chapter):
            paths.update(leaf["source_content_paths"])
            event_ids.update(leaf["source_event_ids"])
    return paths, event_ids


def validate_global_composition_response(
    response: Any,
    *,
    composition_input: Mapping[str, Any],
    chapters: Sequence[ValidatedChapterComposition],
    context: DocumentContentValidationContext,
) -> ValidatedGlobalComposition:
    """校验全文标题、概述和可选结论，禁止返回或修改章节。"""

    if not isinstance(response, dict):
        _fail("全文响应必须是JSON对象。")
    _exact_keys(
        response,
        {"schema_version", "title", "overview", "overall_conclusion"},
        "global_response",
    )
    if response["schema_version"] != DOCUMENT_GLOBAL_RESPONSE_SCHEMA_VERSION:
        _fail("全文响应Schema版本无效。")
    try:
        title_paths, title_events = _validate_evidence_text(
            response["title"],
            "global_response.title",
            context=context,
            chapter_id=None,
            chinese=True,
        )
    except StructuredDocumentContentError as exc:
        raise DocumentContentPreviewError(str(exc)) from exc

    block_evidence: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    overview = response["overview"]
    if not isinstance(overview, list) or not 1 <= len(overview) <= 2:
        _fail("global_response.overview必须包含1至2个段落。")
    for index, block in enumerate(overview):
        try:
            block_id, paths, event_ids, block_type = _validate_block(
                block,
                f"global_response.overview[{index}]",
                scope="overview",
                context=context,
            )
        except StructuredDocumentContentError as exc:
            raise DocumentContentPreviewError(str(exc)) from exc
        if block_type != "paragraph" or block.get("role") != "overview":
            _fail("全文概述只允许paragraph(role=overview)。")
        if block_id in block_evidence:
            _fail("全文响应block_id重复。")
        block_evidence[block_id] = (tuple(paths), tuple(event_ids))

    conclusion = response["overall_conclusion"]
    if not isinstance(conclusion, list) or len(conclusion) > 2:
        _fail("global_response.overall_conclusion必须包含0至2项。")
    if len(conclusion) == 2 and any(
        not isinstance(item, dict) or item.get("type") != "paragraph"
        for item in conclusion
    ):
        _fail("两个全文结论块时必须均为paragraph。")
    for index, block in enumerate(conclusion):
        try:
            block_id, paths, event_ids, block_type = _validate_block(
                block,
                f"global_response.overall_conclusion[{index}]",
                scope="conclusion",
                context=context,
            )
        except StructuredDocumentContentError as exc:
            raise DocumentContentPreviewError(str(exc)) from exc
        if block_type not in {"paragraph", "callout"} or block.get("role") != "conclusion":
            _fail("全文结论只允许conclusion段落或callout。")
        if block_id in block_evidence:
            _fail("全文响应block_id重复。")
        block_evidence[block_id] = (tuple(paths), tuple(event_ids))

    allowed_paths, allowed_events = _allowed_chapter_evidence(chapters)
    global_leaves = [("global_response.title", response["title"])] + _evidence_leaves(
        {"overview": overview, "overall_conclusion": conclusion},
        "global_response",
    )
    fields, events = _all_source_maps(composition_input)
    for location, leaf in global_leaves:
        paths = leaf["source_content_paths"]
        event_ids = leaf["source_event_ids"]
        if any(path not in allowed_paths for path in paths) or any(
            event_id not in allowed_events for event_id in event_ids
        ):
            _fail(f"{location}引用了未进入已验证章节的事实或证据。")
        try:
            _validate_high_risk_text(
                str(leaf["text"]),
                source_paths=paths,
                source_event_ids=event_ids,
                fields=fields,
                events=events,
                location=location,
            )
        except DocumentChapterCompositionError as exc:
            raise DocumentContentPreviewError(str(exc)) from exc
    if not title_paths or not title_events:
        _fail("全文标题必须有已验证章节来源。")
    return ValidatedGlobalComposition(
        title=dict(response["title"]),
        overview=tuple(dict(block) for block in overview),
        overall_conclusion=tuple(dict(block) for block in conclusion),
        block_evidence=block_evidence,
    )


def _global_messages(
    composition_input: Mapping[str, Any],
    chapter_result: OfflineChapterCompositionResult,
) -> list[dict[str, str]]:
    payload = {
        "input_id": composition_input["input_id"],
        "document_profile": composition_input["document_profile"],
        "validated_chapters": _validated_chapter_payloads(chapter_result),
        "versions": {
            "global_prompt": DOCUMENT_GLOBAL_PROMPT_VERSION,
            "content_policy": DOCUMENT_COMPOSITION_CONTENT_POLICY_VERSION,
            "validator": DOCUMENT_CONTENT_VALIDATOR_VERSION,
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "只根据全部已验证章节生成文档标题、1至2段视频概述和可选全文结论。"
                "不得返回或修改章节，不得补充外部知识，只返回JSON。"
            ),
        },
        {
            "role": "user",
            "content": "DOCUMENT_GLOBAL_INPUT_JSON="
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


class DeterministicDocumentGlobalMockTransport:
    """从已验证章标题生成最小、确定性的全文响应。"""

    def __init__(self) -> None:
        self.call_count = 0

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        del response_format
        if config.backend != "mock":
            raise LLMRetryableError("R4仅允许mock backend。")
        self.call_count += 1
        marker = "DOCUMENT_GLOBAL_INPUT_JSON="
        content = next(
            (
                str(message["content"])
                for message in reversed(messages)
                if marker in str(message["content"])
            ),
            None,
        )
        if content is None:
            raise LLMRetryableError("mock提示缺少全文输入标记。")
        try:
            payload = json.loads(content.split(marker, 1)[1])
            chapter_titles = [
                item["chapter"]["title"] for item in payload["validated_chapters"]
            ]
            if not chapter_titles:
                raise ValueError("章节为空")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMRetryableError("mock无法解析全文输入。") from exc
        paths = _ordered_unique(
            [title["source_content_paths"] for title in chapter_titles]
        )
        event_ids = _ordered_unique(
            [title["source_event_ids"] for title in chapter_titles]
        )
        texts = [str(title["text"]).rstrip("。；") for title in chapter_titles]
        evidence = {
            "content_origin": "stage9_v1",
            "source_content_paths": paths,
            "source_event_ids": event_ids,
        }
        return {
            "schema_version": DOCUMENT_GLOBAL_RESPONSE_SCHEMA_VERSION,
            "title": {"text": texts[0], **evidence},
            "overview": [
                {
                    "block_id": "overview-p001",
                    "type": "paragraph",
                    "role": "overview",
                    "text": "；".join(texts) + "。",
                    **evidence,
                }
            ],
            "overall_conclusion": [],
        }


def _global_config() -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model=DOCUMENT_GLOBAL_MOCK_MODEL,
        timeout_seconds=1.0,
        api_key="",
        mock_response={},
        enable_thinking=False,
        temperature=0.0,
    )


def _global_validator(
    *,
    composition_input: Mapping[str, Any],
    chapters: Sequence[ValidatedChapterComposition],
    context: DocumentContentValidationContext,
) -> Any:
    def validate(response: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = validate_global_composition_response(
                response,
                composition_input=composition_input,
                chapters=chapters,
                context=context,
            )
            return {
                "schema_version": DOCUMENT_GLOBAL_RESPONSE_SCHEMA_VERSION,
                "title": validated.title,
                "overview": list(validated.overview),
                "overall_conclusion": list(validated.overall_conclusion),
            }
        except DocumentContentPreviewError as exc:
            raise LLMRetryableError(str(exc)) from exc

    return validate


def _call_global(
    output_root: str | Path,
    task_id: str,
    *,
    composition_input: Mapping[str, Any],
    chapter_result: OfflineChapterCompositionResult,
    context: DocumentContentValidationContext,
    config: LLMConfig,
    transport: LLMTransport,
) -> LLMCallResult:
    return call_task_llm(
        output_root,
        task_id,
        call_id="document-global",
        messages=_global_messages(composition_input, chapter_result),
        response_format={"type": "json_object"},
        config=config,
        transport=transport,
        response_validator=_global_validator(
            composition_input=composition_input,
            chapters=chapter_result.chapters,
            context=context,
        ),
        validation_id=DOCUMENT_GLOBAL_VALIDATION_ID,
        artifact_subdir=(
            Path("content")
            / "document-v2"
            / "attempts"
            / composition_input["input_id"]
            / "global"
            / "calls"
        ),
        cache_namespace=DOCUMENT_GLOBAL_CACHE_NAMESPACE,
        save_validation_audit=True,
    )


def _block_evidence_in_order(
    global_result: ValidatedGlobalComposition,
    chapters: Sequence[ValidatedChapterComposition],
) -> list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
    result: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
    for index, block in enumerate(global_result.overview, start=1):
        block_id = block["block_id"]
        paths, events = global_result.block_evidence[block_id]
        result.append((block_id, f"视频概述第{index}段", paths, events))
    for chapter_index, validated in enumerate(chapters, start=1):
        block_number = 0
        for section in validated.chapter["sections"]:
            for block in section["blocks"]:
                block_number += 1
                block_id = block["block_id"]
                paths, events = validated.block_evidence[block_id]
                result.append(
                    (block_id, f"第{chapter_index}章内容块{block_number}", paths, events)
                )
    for index, block in enumerate(global_result.overall_conclusion, start=1):
        block_id = block["block_id"]
        paths, events = global_result.block_evidence[block_id]
        result.append((block_id, f"全文核心结论第{index}项", paths, events))
    return result


def _evidence_index(
    global_result: ValidatedGlobalComposition,
    chapters: Sequence[ValidatedChapterComposition],
    context: DocumentContentValidationContext,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block_id, label, paths, event_ids in _block_evidence_in_order(
        global_result, chapters
    ):
        rows.append(
            {
                "block_id": block_id,
                "location_label": label,
                "source_content_paths": list(paths),
                "source_event_ids": list(event_ids),
                "start": min(context.event_times[event_id][0] for event_id in event_ids),
                "end": max(context.event_times[event_id][1] for event_id in event_ids),
            }
        )
    return rows


def assemble_structured_document_content(
    composition_input: Mapping[str, Any],
    *,
    chapter_result: OfflineChapterCompositionResult,
    global_response: Mapping[str, Any],
    generation_mode: str = "document-composition-mock",
    generation_backend: str = "mock",
    generation_model: str = (
        f"{DOCUMENT_CHAPTER_MOCK_MODEL}+{DOCUMENT_GLOBAL_MOCK_MODEL}"
    ),
    generation_temperature: float = 0.0,
    generation_api_request_count: int = 0,
) -> dict[str, Any]:
    """从同一输入、已验证章节和全文响应确定性装配完整2.0。"""

    try:
        validate_document_composition_input(dict(composition_input))
    except DocumentCompositionError as exc:
        raise DocumentContentPreviewError(str(exc)) from exc
    if chapter_result.input_id != composition_input["input_id"]:
        _fail("章级结果与DocumentCompositionInput身份不一致。")
    context = _chapter_context(composition_input)
    global_result = validate_global_composition_response(
        dict(global_response),
        composition_input=composition_input,
        chapters=chapter_result.chapters,
        context=context,
    )
    coverage_items = [
        item
        for validated in chapter_result.chapters
        for item in validated.coverage["items"]
    ]
    payload = {
        "schema_version": STRUCTURED_DOCUMENT_CONTENT_SCHEMA_VERSION,
        "task_id": composition_input["task_id"],
        "language": composition_input["language"],
        "strategy": composition_input["strategy"],
        "document_profile": composition_input["document_profile"],
        "source_content_bundle": dict(composition_input["source_content_bundle"]),
        "source_chapter_plan": dict(composition_input["source_chapter_plan"]),
        "source_timeline": dict(composition_input["source_timeline"]),
        "chaptering_provenance": dict(composition_input["chaptering_provenance"]),
        "title": global_result.title,
        "overview": list(global_result.overview),
        "chapters": [validated.chapter for validated in chapter_result.chapters],
        "overall_conclusion": list(global_result.overall_conclusion),
        "coverage": {"items": coverage_items},
        "evidence_index": _evidence_index(
            global_result, chapter_result.chapters, context
        ),
        "generation": {
            "status": "success",
            "mode": generation_mode,
            "backend": generation_backend,
            "model": generation_model,
            "temperature": generation_temperature,
            "chapter_prompt_version": DOCUMENT_CHAPTER_PROMPT_VERSION,
            "global_prompt_version": DOCUMENT_GLOBAL_PROMPT_VERSION,
            "content_policy_version": DOCUMENT_COMPOSITION_CONTENT_POLICY_VERSION,
            "validator_version": DOCUMENT_CONTENT_VALIDATOR_VERSION,
            "api_request_count": generation_api_request_count,
        },
        "content_versions": document_content_versions(),
    }
    document = attach_structured_document_content_id(payload)
    try:
        validate_structured_document_content(document, context=context)
    except StructuredDocumentContentError as exc:
        raise DocumentContentPreviewError(str(exc)) from exc
    return document


def _preview_versions() -> dict[str, str]:
    return {
        "global_response_schema": DOCUMENT_GLOBAL_RESPONSE_SCHEMA_VERSION,
        "global_prompt": DOCUMENT_GLOBAL_PROMPT_VERSION,
        "global_validator": DOCUMENT_GLOBAL_VALIDATION_ID,
        "global_cache_namespace": DOCUMENT_GLOBAL_CACHE_NAMESPACE,
        "content_policy": DOCUMENT_COMPOSITION_CONTENT_POLICY_VERSION,
        "preview_publisher": DOCUMENT_PREVIEW_PUBLISHER_VERSION,
    }


def _execution_report(
    composition_input: Mapping[str, Any],
    document: Mapping[str, Any],
    *,
    business_call_count: int,
    supplier_request_count: int,
    cache_hit_count: int,
    api_request_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": DOCUMENT_PREVIEW_REPORT_SCHEMA_VERSION,
        "status": "success",
        "task_id": document["task_id"],
        "content_id": document["content_id"],
        "input_id": composition_input["input_id"],
        "document_profile": document["document_profile"],
        "document_sha256": canonical_sha256(document),
        "chapter_count": len(document["chapters"]),
        "business_call_count": business_call_count,
        "supplier_request_count": supplier_request_count,
        "cache_hit_count": cache_hit_count,
        "api_request_count": api_request_count,
        "versions": _preview_versions(),
    }


def _manifest(
    composition_input: Mapping[str, Any],
    document: Mapping[str, Any],
    report: Mapping[str, Any],
    files: Mapping[str, bytes],
) -> dict[str, Any]:
    return {
        "schema_version": DOCUMENT_PREVIEW_MANIFEST_SCHEMA_VERSION,
        "status": "success",
        "task_id": document["task_id"],
        "content_id": document["content_id"],
        "document_profile": document["document_profile"],
        "input": {
            "input_id": composition_input["input_id"],
            "composition_input_sha256": canonical_sha256(composition_input),
            "source_content_result_id": composition_input["source_content_bundle"]["result_id"],
            "source_chapter_plan_result_id": composition_input["source_chapter_plan"]["result_id"],
            "source_timeline_sha256": composition_input["source_timeline"]["canonical_sha256"],
        },
        "document_sha256": canonical_sha256(document),
        "chapter_count": len(document["chapters"]),
        "calls": {
            "business_call_count": report["business_call_count"],
            "supplier_request_count": report["supplier_request_count"],
            "cache_hit_count": report["cache_hit_count"],
            "api_request_count": report["api_request_count"],
        },
        "content_versions": document_content_versions(),
        "preview_versions": _preview_versions(),
        "files": {
            name: {
                "sha256": _sha256(files[name]),
                "bytes": len(files[name]),
                "mime_type": mime_type,
            }
            for name, mime_type in _PREVIEW_FILES.items()
        },
    }


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("xb") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())


def _inject_preview_failure(selected: str | None, point: str) -> None:
    if selected == point:
        raise InjectedDocumentContentPreviewFailure(f"注入失败：{point}")


def _write_preview_failure_audit(
    document_v2_root: Path,
    *,
    task_id: str,
    content_id: str,
    failure_stage: str,
    error: Exception,
) -> Path:
    failure_id = f"failure-{uuid4().hex[:12]}"
    failure_root = document_v2_root / "failures" / failure_id
    failure_root.mkdir(parents=True, exist_ok=False)
    message = (
        str(error)
        if isinstance(error, InjectedDocumentContentPreviewFailure)
        else "document-v2 preview发布失败；未记录正文、文件路径或底层异常详情。"
    )
    audit = {
        "schema_version": DOCUMENT_PREVIEW_FAILURE_AUDIT_SCHEMA_VERSION,
        "status": "failed",
        "failure_id": failure_id,
        "task_id": task_id,
        "content_id": content_id,
        "failure_stage": failure_stage,
        "error_type": type(error).__name__,
        "message": message,
    }
    path = failure_root / "failure.json"
    _write_fsync(path, _json_bytes(audit))
    return path


def validate_document_preview_group(
    group_path: str | Path,
    *,
    composition_input: Mapping[str, Any],
    expected_document: Mapping[str, Any] | None = None,
    require_directory_name: bool = True,
) -> DocumentPreviewGroup:
    group = Path(group_path)
    if not group.is_dir():
        _fail(f"preview结果组不存在：{group}")
    expected_names = set(_PREVIEW_FILES) | {"manifest.json"}
    entries = list(group.iterdir())
    if {entry.name for entry in entries if entry.is_file()} != expected_names or any(
        entry.is_dir() for entry in entries
    ):
        _fail("preview结果组文件集合不完整或包含额外成员。")
    try:
        file_bytes = {
            name: (group / name).read_bytes() for name in _PREVIEW_FILES
        }
        manifest_bytes = (group / "manifest.json").read_bytes()
        document = json.loads(file_bytes["document-content.json"].decode("utf-8"))
        report = json.loads(file_bytes["execution-report.json"].decode("utf-8"))
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentContentPreviewError("preview结果组不是有效UTF-8 JSON。") from exc
    if not all(isinstance(item, dict) for item in (document, report, manifest)):
        _fail("preview结果组顶层必须均为JSON对象。")
    context = _chapter_context(composition_input)
    try:
        validate_structured_document_content(document, context=context)
    except StructuredDocumentContentError as exc:
        raise DocumentContentPreviewError(str(exc)) from exc
    if expected_document is not None and document != expected_document:
        _fail("preview结果组与预期StructuredDocumentContent不一致。")
    report_expected_keys = {
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
    }
    if set(report) != report_expected_keys:
        _fail("preview执行报告字段集合无效。")
    if (
        report["schema_version"] != DOCUMENT_PREVIEW_REPORT_SCHEMA_VERSION
        or report["status"] != "success"
        or report["task_id"] != document["task_id"]
        or report["content_id"] != document["content_id"]
        or report["input_id"] != composition_input["input_id"]
        or report["document_profile"] != document["document_profile"]
        or report["document_sha256"] != canonical_sha256(document)
        or report["chapter_count"] != len(document["chapters"])
        or report["api_request_count"] != document["generation"]["api_request_count"]
        or report["versions"] != _preview_versions()
    ):
        _fail("preview执行报告身份、计数或版本无效。")
    for field in ("business_call_count", "supplier_request_count", "cache_hit_count"):
        if isinstance(report[field], bool) or not isinstance(report[field], int) or report[field] < 0:
            _fail(f"preview执行报告{field}无效。")
    expected_manifest = _manifest(
        composition_input, document, report, file_bytes
    )
    if manifest != expected_manifest:
        _fail("preview manifest与输入、内容、报告或文件身份不一致。")
    if require_directory_name and group.name != document["content_id"]:
        _fail("preview目录名与content_id不一致。")
    if file_bytes["document-content.json"] != _json_bytes(document) or file_bytes[
        "execution-report.json"
    ] != _json_bytes(report) or manifest_bytes != _json_bytes(manifest):
        _fail("preview JSON不是冻结的确定性序列化。")
    return DocumentPreviewGroup(
        group_path=group,
        document_path=group / "document-content.json",
        execution_report_path=group / "execution-report.json",
        manifest_path=group / "manifest.json",
        document=document,
        execution_report=report,
        manifest=manifest,
        cache_hit=False,
    )


def publish_document_preview(
    output_root: str | Path,
    task_id: str,
    *,
    composition_input: Mapping[str, Any],
    document: Mapping[str, Any],
    business_call_count: int,
    supplier_request_count: int,
    cache_hit_count: int,
    api_request_count: int = 0,
    failure_injection: str | None = None,
) -> DocumentPreviewGroup:
    """在同盘临时目录完整写入、复验后原子发布preview；不创建current。"""

    if failure_injection is not None and failure_injection not in (
        _PREVIEW_FAILURE_INJECTION_POINTS
    ):
        _fail("未知的document-v2 preview失败注入点。")
    if document["task_id"] != task_id:
        _fail("preview文档task_id与目标任务不一致。")
    report = _execution_report(
        composition_input,
        document,
        business_call_count=business_call_count,
        supplier_request_count=supplier_request_count,
        cache_hit_count=cache_hit_count,
        api_request_count=api_request_count,
    )
    files = {
        "document-content.json": _json_bytes(document),
        "execution-report.json": _json_bytes(report),
    }
    manifest = _manifest(composition_input, document, report, files)
    task_root = Path(output_root).expanduser() / task_id
    document_v2_root = task_root / "content" / "document-v2"
    previews_root = document_v2_root / "previews"
    group = previews_root / document["content_id"]
    if group.exists():
        try:
            cached = validate_document_preview_group(
                group,
                composition_input=composition_input,
                expected_document=document,
            )
        except Exception as exc:
            _write_preview_failure_audit(
                document_v2_root,
                task_id=task_id,
                content_id=document["content_id"],
                failure_stage="cache_validation",
                error=exc,
            )
            raise
        return DocumentPreviewGroup(**{**cached.__dict__, "cache_hit": True})
    previews_root.mkdir(parents=True, exist_ok=True)
    temporary = previews_root / f".{document['content_id']}.tmp-{uuid4().hex}"
    created_group = False
    failure_stage = "prepare"
    try:
        temporary.mkdir(exist_ok=False)
        failure_stage = "document-content.json"
        _write_fsync(temporary / "document-content.json", files["document-content.json"])
        _inject_preview_failure(failure_injection, "after_document_content")
        failure_stage = "execution-report.json"
        _write_fsync(temporary / "execution-report.json", files["execution-report.json"])
        _inject_preview_failure(failure_injection, "after_execution_report")
        failure_stage = "manifest.json"
        _write_fsync(temporary / "manifest.json", _json_bytes(manifest))
        _inject_preview_failure(failure_injection, "after_manifest")
        validate_document_preview_group(
            temporary,
            composition_input=composition_input,
            expected_document=document,
            require_directory_name=False,
        )
        failure_stage = "group_publish"
        _inject_preview_failure(failure_injection, "before_group_publish")
        os.replace(temporary, group)
        created_group = True
        _inject_preview_failure(failure_injection, "after_group_publish")
        return validate_document_preview_group(
            group,
            composition_input=composition_input,
            expected_document=document,
        )
    except Exception as exc:
        if created_group and group.exists():
            shutil.rmtree(group)
        _write_preview_failure_audit(
            document_v2_root,
            task_id=task_id,
            content_id=document["content_id"],
            failure_stage=failure_stage,
            error=exc,
        )
        if isinstance(exc, DocumentContentPreviewError):
            raise
        raise DocumentContentPreviewError(
            f"无法原子发布document-v2 preview结果组：{type(exc).__name__}"
        ) from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def publish_accepted_document_preview(
    output_root: str | Path,
    task_id: str,
    *,
    composition_input: Mapping[str, Any],
    content_id: str,
) -> DocumentFormalGroup:
    """0 API把用户已接受的冻结preview原样发布为2.0正式组。"""

    document_v2_root = Path(output_root).expanduser() / task_id / "content" / "document-v2"
    preview_path = document_v2_root / "previews" / content_id
    preview = validate_document_preview_group(
        preview_path,
        composition_input=composition_input,
    )
    if preview.document["task_id"] != task_id or preview.document["content_id"] != content_id:
        _fail("已接受preview身份与正式发布目标不一致。")
    formal_root = document_v2_root / "formal-results"
    target = formal_root / content_id
    current_path = document_v2_root / "current.json"
    current = {
        "schema_version": DOCUMENT_FORMAL_CURRENT_SCHEMA_VERSION,
        "status": "accepted",
        "task_id": task_id,
        "content_id": content_id,
        "group": f"formal-results/{content_id}",
        "document_sha256": canonical_sha256(preview.document),
        "publisher_version": DOCUMENT_FORMAL_PUBLISHER_VERSION,
    }
    cache_hit = target.exists()
    if cache_hit:
        formal = validate_document_preview_group(
            target,
            composition_input=composition_input,
            expected_document=preview.document,
        )
    else:
        formal_root.mkdir(parents=True, exist_ok=True)
        temporary = formal_root / f".{content_id}.tmp-{uuid4().hex}"
        try:
            temporary.mkdir(exist_ok=False)
            for filename in (
                "document-content.json",
                "execution-report.json",
                "manifest.json",
            ):
                _write_fsync(temporary / filename, (preview_path / filename).read_bytes())
            validate_document_preview_group(
                temporary,
                composition_input=composition_input,
                expected_document=preview.document,
                require_directory_name=False,
            )
            temporary.replace(target)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            if target.exists():
                shutil.rmtree(target)
            raise
        formal = validate_document_preview_group(
            target,
            composition_input=composition_input,
            expected_document=preview.document,
        )
    current_bytes = _json_bytes(current)
    if current_path.exists() and current_path.read_bytes() == current_bytes:
        current_cache_hit = True
    else:
        current_cache_hit = False
        temporary_current = document_v2_root / f".current.json.tmp-{uuid4().hex}"
        try:
            _write_fsync(temporary_current, current_bytes)
            os.replace(temporary_current, current_path)
        finally:
            if temporary_current.exists():
                temporary_current.unlink()
    try:
        loaded_current = json.loads(current_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DocumentContentPreviewError("无法重载document-v2 current。") from exc
    if loaded_current != current:
        _fail("document-v2 current写入后复验不一致。")
    return DocumentFormalGroup(
        group=DocumentPreviewGroup(**{**formal.__dict__, "cache_hit": cache_hit}),
        current_path=current_path,
        current=current,
        cache_hit=cache_hit and current_cache_hit,
    )


def _planned_business_call_count(
    composition_input: Mapping[str, Any],
    *,
    max_window_chars: int,
    max_window_events: int,
) -> int:
    count = 1  # 全文槽位
    for chapter in composition_input["chapters"]:
        windows = split_document_chapter_events(
            chapter,
            max_window_chars=max_window_chars,
            max_window_events=max_window_events,
        )
        count += 1 if len(windows) == 1 else len(windows) + 1
    return count


def compose_document_preview(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    config: LLMConfig,
    chapter_transport: LLMTransport,
    global_transport: LLMTransport,
    generation_mode: str,
    max_window_chars: int = 12_000,
    max_window_events: int = 120,
    max_business_call_count: int | None = None,
    max_supplier_request_count: int | None = None,
    preview_failure_injection: str | None = None,
) -> OfflineDocumentPreviewResult:
    """运行共享章级/全文流水线并只发布隔离preview。"""

    try:
        validate_document_composition_input(dict(composition_input))
        planned_business_calls = _planned_business_call_count(
            composition_input,
            max_window_chars=max_window_chars,
            max_window_events=max_window_events,
        )
        if (
            max_business_call_count is not None
            and planned_business_calls > max_business_call_count
        ):
            _fail("document-v2业务槽位超过冻结上限。")
        chapter_result = compose_document_chapters(
            output_root,
            task_id,
            composition_input,
            config=config,
            transport=chapter_transport,
            max_window_chars=max_window_chars,
            max_window_events=max_window_events,
        )
        if (
            max_supplier_request_count is not None
            and chapter_result.supplier_request_count
            > max_supplier_request_count - 2
        ):
            _fail("章级请求已不足以为全文槽位保留两次尝试。")
        context = _chapter_context(composition_input)
        global_call = _call_global(
            output_root,
            task_id,
            composition_input=composition_input,
            chapter_result=chapter_result,
            context=context,
            config=config,
            transport=global_transport,
        )
        supplier_request_count = chapter_result.supplier_request_count + (
            0 if global_call.cache_hit else global_call.attempt_count
        )
        if (
            max_supplier_request_count is not None
            and supplier_request_count > max_supplier_request_count
        ):
            _fail("document-v2供应商请求超过冻结上限。")
        total_attempt_count = chapter_result.total_attempt_count + global_call.attempt_count
        document = assemble_structured_document_content(
            composition_input,
            chapter_result=chapter_result,
            global_response=global_call.response,
            generation_mode=generation_mode,
            generation_backend=config.backend,
            generation_model=config.model,
            generation_temperature=config.temperature,
            generation_api_request_count=(
                0 if config.backend == "mock" else total_attempt_count
            ),
        )
    except (DocumentCompositionError, DocumentChapterCompositionError, LLMAdapterError) as exc:
        raise DocumentContentPreviewError(str(exc)) from exc

    business_call_count = chapter_result.business_call_count + 1
    cache_hit_count = chapter_result.cache_hit_count + int(global_call.cache_hit)
    preview = publish_document_preview(
        output_root,
        task_id,
        composition_input=composition_input,
        document=document,
        business_call_count=business_call_count,
        supplier_request_count=supplier_request_count,
        cache_hit_count=cache_hit_count,
        api_request_count=document["generation"]["api_request_count"],
        failure_injection=preview_failure_injection,
    )
    return OfflineDocumentPreviewResult(
        input_id=str(composition_input["input_id"]),
        content_id=document["content_id"],
        document=document,
        chapter_result=chapter_result,
        global_call=global_call,
        preview=preview,
        business_call_count=business_call_count,
        supplier_request_count=supplier_request_count,
        cache_hit_count=cache_hit_count,
        all_call_cache_hit=cache_hit_count == business_call_count,
    )


def compose_document_preview_offline_mock(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    chapter_transport: LLMTransport | None = None,
    global_transport: LLMTransport | None = None,
    max_window_chars: int = 12_000,
    max_window_events: int = 120,
    preview_failure_injection: str | None = None,
) -> OfflineDocumentPreviewResult:
    """仅供离线测试；显式mock配置保证不读取.env或构造网络transport。"""

    return compose_document_preview(
        output_root,
        task_id,
        composition_input,
        config=_global_config(),
        chapter_transport=(
            chapter_transport or DeterministicDocumentChapterMockTransport()
        ),
        global_transport=(global_transport or DeterministicDocumentGlobalMockTransport()),
        generation_mode="document-composition-mock",
        max_window_chars=max_window_chars,
        max_window_events=max_window_events,
        preview_failure_injection=preview_failure_injection,
    )


def compose_document_preview_real(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    dotenv_path: str | Path | None = None,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
) -> OfflineDocumentPreviewResult:
    """R7真实入口：同一流水线、冻结6/12预算、只生成隔离preview。"""

    resolved_config = config or load_llm_config(dotenv_path=dotenv_path)
    if resolved_config.backend != "openai_compatible":
        _fail("R7真实入口要求openai_compatible backend。")
    selected_transport = transport or OpenAICompatibleTransport()
    return compose_document_preview(
        output_root,
        task_id,
        composition_input,
        config=resolved_config,
        chapter_transport=selected_transport,
        global_transport=selected_transport,
        generation_mode="document-composition-real",
        max_window_chars=12_000,
        max_window_events=120,
        max_business_call_count=DOCUMENT_REAL_MAX_BUSINESS_CALLS,
        max_supplier_request_count=DOCUMENT_REAL_MAX_SUPPLIER_REQUESTS,
    )
