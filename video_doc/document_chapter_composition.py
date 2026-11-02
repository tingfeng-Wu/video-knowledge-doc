"""阶段9-R3章级离线编排、校验、调用缓存与失败审计。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    call_task_llm,
)
from video_doc.structured_document_content import (
    DocumentContentValidationContext,
    StructuredDocumentContentError,
    _validate_block,
    _validate_evidence_text,
    canonical_sha256,
)


DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION = "2.0"
DOCUMENT_CHAPTER_PROMPT_VERSION = "document-chapter-prompt-2.2"
DOCUMENT_CHAPTER_MERGE_PROMPT_VERSION = "document-chapter-merge-prompt-2.0"
DOCUMENT_CHAPTER_WINDOWING_VERSION = "document-chapter-windowing-2.0"
DOCUMENT_CHAPTER_FACT_POLICY_VERSION = "document-high-risk-fact-screen-2.0"
DOCUMENT_CHAPTER_COVERAGE_POLICY_VERSION = "document-chapter-coverage-2.0"
DOCUMENT_CHAPTER_CACHE_NAMESPACE = "document-chapter-composition-json-object-2-0"
DOCUMENT_CHAPTER_VALIDATION_ID = "document-chapter-validator-2-0"
DOCUMENT_WINDOW_DRAFT_VALIDATION_ID = "document-window-draft-validator-2-0"
DOCUMENT_CHAPTER_MOCK_MODEL = "deterministic-document-chapter-fixture"
DEFAULT_DOCUMENT_WINDOW_MAX_CHARS = 12_000
DEFAULT_DOCUMENT_WINDOW_MAX_EVENTS = 120

_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[./]\d+)*(?:[.,]\d+)*(?:%|％)?")
_LATIN_ENTITY_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9._-]{1,}(?![A-Za-z0-9_])"
)
_QUOTED_ENTITY_RE = re.compile(r"[“《]([^”》]{2,40})[”》]")
_HIGH_RISK_PHRASES = (
    "并非",
    "不能",
    "不会",
    "不应",
    "不得",
    "没有",
    "无",
    "可能",
    "必然",
    "一定",
    "必须",
    "应当",
    "应该",
    "建议",
    "需要",
    "可以",
)


class DocumentChapterCompositionError(RuntimeError):
    """R3章级编排或本地校验失败。"""


@dataclass(frozen=True)
class DocumentChapterWindow:
    window_id: str
    window_index: int
    events: tuple[dict[str, Any], ...]
    event_ids: tuple[str, ...]
    input_sha256: str
    serialized_chars: int
    oversize_single_event: bool


@dataclass(frozen=True)
class ValidatedChapterComposition:
    chapter: dict[str, Any]
    coverage: dict[str, Any]
    block_evidence: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]


@dataclass(frozen=True)
class OfflineChapterCompositionResult:
    input_id: str
    chapters: tuple[ValidatedChapterComposition, ...]
    business_call_count: int
    supplier_request_count: int
    total_attempt_count: int
    cache_hit_count: int
    all_cache_hit: bool
    call_paths: tuple[Path, ...]


def _fail(message: str) -> None:
    raise DocumentChapterCompositionError(message)


def _exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        _fail(f"{location}字段集合不符合R3契约。")


def _ordered_unique(groups: Sequence[Sequence[str]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _chapter_context(
    composition_input: Mapping[str, Any],
) -> DocumentContentValidationContext:
    source_paths: set[str] = set()
    required_paths: set[str] = set()
    chapter_event_ids: dict[str, tuple[str, ...]] = {}
    event_times: dict[str, tuple[int | float, int | float]] = {}
    for chapter in composition_input["chapters"]:
        chapter_id = chapter["chapter_id"]
        chapter_event_ids[chapter_id] = tuple(event["id"] for event in chapter["events"])
        for event in chapter["events"]:
            event_times[event["id"]] = (event["start"], event["end"])
        for field in chapter["source_fields"]:
            source_paths.add(field["source_content_path"])
            if field["coverage_required"]:
                required_paths.add(field["source_content_path"])
    return DocumentContentValidationContext(
        known_source_paths=frozenset(source_paths),
        required_core_paths=frozenset(required_paths),
        chapter_event_ids=chapter_event_ids,
        event_times=event_times,
    )


def split_document_chapter_events(
    chapter: Mapping[str, Any],
    *,
    max_window_chars: int = DEFAULT_DOCUMENT_WINDOW_MAX_CHARS,
    max_window_events: int = DEFAULT_DOCUMENT_WINDOW_MAX_EVENTS,
) -> list[DocumentChapterWindow]:
    """按完整事件确定性分窗；不重叠、不截断、不遗漏。"""

    if isinstance(max_window_chars, bool) or not isinstance(max_window_chars, int) or max_window_chars < 1:
        _fail("max_window_chars必须是正整数。")
    if isinstance(max_window_events, bool) or not isinstance(max_window_events, int) or max_window_events < 1:
        _fail("max_window_events必须是正整数。")
    events = chapter.get("events")
    if not isinstance(events, list) or not events:
        _fail("章级分窗必须包含非空events。")
    raw_windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        candidate = current + [event]
        serialized = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if current and (
            len(candidate) > max_window_events or len(serialized) > max_window_chars
        ):
            raw_windows.append(current)
            current = [event]
        else:
            current = candidate
    if current:
        raw_windows.append(current)

    result: list[DocumentChapterWindow] = []
    flattened: list[str] = []
    chapter_id = str(chapter["chapter_id"])
    for index, window_events in enumerate(raw_windows, start=1):
        event_ids = tuple(str(event["id"]) for event in window_events)
        flattened.extend(event_ids)
        serialized = json.dumps(window_events, ensure_ascii=False, separators=(",", ":"))
        result.append(
            DocumentChapterWindow(
                window_id=f"{chapter_id}-w{index:03d}",
                window_index=index,
                events=tuple(dict(event) for event in window_events),
                event_ids=event_ids,
                input_sha256=canonical_sha256(window_events),
                serialized_chars=len(serialized),
                oversize_single_event=(
                    len(window_events) == 1 and len(serialized) > max_window_chars
                ),
            )
        )
    expected = [str(event["id"]) for event in events]
    if flattened != expected or len(flattened) != len(set(flattened)):
        _fail("章级确定性分窗发生事件遗漏、重复或乱序。")
    return result


def _source_maps(
    chapter_input: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    fields = {
        str(field["source_content_path"]): field
        for field in chapter_input["source_fields"]
    }
    events = {str(event["id"]): event for event in chapter_input["events"]}
    return fields, events


def _high_risk_tokens(text: str) -> dict[str, set[str]]:
    normalized = text.replace("％", "%")
    negation_text = text.replace("无论", "")
    return {
        "数字/比例": set(_NUMBER_RE.findall(normalized)),
        "拉丁实体": {token.casefold() for token in _LATIN_ENTITY_RE.findall(text)},
        "引号实体": set(_QUOTED_ENTITY_RE.findall(text)),
        "否定/模态/建议": {
            phrase for phrase in _HIGH_RISK_PHRASES if phrase in negation_text
        },
    }


def _validate_high_risk_text(
    text: str,
    *,
    source_paths: Sequence[str],
    source_event_ids: Sequence[str],
    fields: Mapping[str, Mapping[str, Any]],
    events: Mapping[str, Mapping[str, Any]],
    location: str,
) -> None:
    source_text = "\n".join(
        [str(fields[path]["text"]) for path in source_paths]
        + [str(events[event_id]["text"]) for event_id in source_event_ids]
    )
    output_tokens = _high_risk_tokens(text)
    source_tokens = _high_risk_tokens(source_text)
    for category, tokens in output_tokens.items():
        unsupported = tokens - source_tokens[category]
        if unsupported:
            _fail(f"{location}出现来源未支持的{category}：{sorted(unsupported)}。")


def _evidence_leaves(value: Any, location: str = "response") -> list[tuple[str, Mapping[str, Any]]]:
    leaves: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, dict):
        if {
            "text",
            "content_origin",
            "source_content_paths",
            "source_event_ids",
        }.issubset(value):
            leaves.append((location, value))
        for key, item in value.items():
            leaves.extend(_evidence_leaves(item, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            leaves.extend(_evidence_leaves(item, f"{location}[{index}]"))
    return leaves


def validate_chapter_composition_response(
    response: Any,
    *,
    chapter_input: Mapping[str, Any],
    context: DocumentContentValidationContext,
) -> ValidatedChapterComposition:
    """严格校验单章最终响应、覆盖、证据及高风险事实词元。"""

    if not isinstance(response, dict):
        _fail("章级响应必须是JSON对象。")
    _exact_keys(
        response,
        {"schema_version", "chapter_id", "title", "keywords", "sections", "coverage"},
        "chapter_response",
    )
    if response["schema_version"] != DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION:
        _fail("章级响应Schema版本无效。")
    chapter_id = str(chapter_input["chapter_id"])
    if response["chapter_id"] != chapter_id:
        _fail("章级响应chapter_id与输入不一致。")
    try:
        _validate_evidence_text(
            response["title"],
            "chapter_response.title",
            context=context,
            chapter_id=chapter_id,
            chinese=True,
        )
    except StructuredDocumentContentError as exc:
        raise DocumentChapterCompositionError(str(exc)) from exc
    keywords = response["keywords"]
    if not isinstance(keywords, list) or len(keywords) > 6:
        _fail("chapter_response.keywords必须包含0至6项。")
    try:
        for index, keyword in enumerate(keywords):
            _validate_evidence_text(
                keyword,
                f"chapter_response.keywords[{index}]",
                context=context,
                chapter_id=chapter_id,
                chinese=False,
            )
    except StructuredDocumentContentError as exc:
        raise DocumentChapterCompositionError(str(exc)) from exc

    sections = response["sections"]
    if not isinstance(sections, list) or not 1 <= len(sections) <= 4:
        _fail("chapter_response.sections必须包含1至4项。")
    block_evidence: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    callout_count = 0
    comparison_count = 0
    for section_index, section in enumerate(sections, start=1):
        location = f"chapter_response.sections[{section_index - 1}]"
        if not isinstance(section, dict):
            _fail(f"{location}必须是对象。")
        _exact_keys(section, {"section_id", "heading", "blocks"}, location)
        section_id = f"{chapter_id}-s{section_index:03d}"
        if section["section_id"] != section_id:
            _fail(f"{location}.section_id无效。")
        try:
            if section["heading"] is not None:
                _validate_evidence_text(
                    section["heading"],
                    f"{location}.heading",
                    context=context,
                    chapter_id=chapter_id,
                    chinese=True,
                )
        except StructuredDocumentContentError as exc:
            raise DocumentChapterCompositionError(str(exc)) from exc
        blocks = section["blocks"]
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 4:
            _fail(f"{location}.blocks必须包含1至4项。")
        for block_index, block in enumerate(blocks):
            try:
                block_id, paths, event_ids, block_type = _validate_block(
                    block,
                    f"{location}.blocks[{block_index}]",
                    scope=chapter_id,
                    context=context,
                )
            except StructuredDocumentContentError as exc:
                raise DocumentChapterCompositionError(str(exc)) from exc
            if not block_id.startswith(f"{section_id}-") or block_id in block_evidence:
                _fail(f"{location}存在错误归属或重复block_id。")
            block_evidence[block_id] = (tuple(paths), tuple(event_ids))
            callout_count += block_type == "callout"
            comparison_count += block_type == "comparison"
    if callout_count > 1 or comparison_count > 1:
        _fail("单章最多包含1个callout和1个comparison。")

    fields, events = _source_maps(chapter_input)
    for location, leaf in _evidence_leaves(
        {"title": response["title"], "keywords": keywords, "sections": sections}
    ):
        _validate_high_risk_text(
            str(leaf["text"]),
            source_paths=leaf["source_content_paths"],
            source_event_ids=leaf["source_event_ids"],
            fields=fields,
            events=events,
            location=location,
        )
    for section_index, section in enumerate(sections):
        for block_index, block in enumerate(section["blocks"]):
            if block["type"] != "comparison":
                continue
            leaves = _evidence_leaves(block)
            paths = _ordered_unique(
                [leaf["source_content_paths"] for _, leaf in leaves]
            )
            event_ids = _ordered_unique(
                [leaf["source_event_ids"] for _, leaf in leaves]
            )
            labels = list(block["columns"]) + [row["dimension"] for row in block["rows"]]
            for label_index, label in enumerate(labels):
                _validate_high_risk_text(
                    str(label),
                    source_paths=paths,
                    source_event_ids=event_ids,
                    fields=fields,
                    events=events,
                    location=(
                        f"chapter_response.sections[{section_index}].blocks"
                        f"[{block_index}].labels[{label_index}]"
                    ),
                )

    coverage = response["coverage"]
    if not isinstance(coverage, dict):
        _fail("chapter_response.coverage必须是对象。")
    _exact_keys(coverage, {"items"}, "chapter_response.coverage")
    items = coverage["items"]
    required = [
        field["source_content_path"]
        for field in chapter_input["source_fields"]
        if field["coverage_required"]
    ]
    if not isinstance(items, list) or len(items) != len(required):
        _fail("chapter_response.coverage未完整覆盖v1核心路径。")
    seen: list[str] = []
    for index, item in enumerate(items):
        location = f"chapter_response.coverage.items[{index}]"
        if not isinstance(item, dict):
            _fail(f"{location}必须是对象。")
        _exact_keys(item, {"source_content_path", "status", "target_block_ids"}, location)
        path = item["source_content_path"]
        if path != required[index] or path in seen:
            _fail("chapter_response.coverage路径必须按输入核心字段顺序完整且唯一。")
        seen.append(path)
        if item["status"] not in {"represented", "omitted_duplicate"}:
            _fail(f"{location}.status无效。")
        targets = item["target_block_ids"]
        if (
            not isinstance(targets, list)
            or not targets
            or len(targets) != len(set(targets))
            or any(target not in block_evidence for target in targets)
        ):
            _fail(f"{location}.target_block_ids无效。")
        if item["status"] == "represented":
            if not any(path in block_evidence[target][0] for target in targets):
                _fail(f"{location}伪登记represented但目标块未引用来源路径。")
        else:
            source_ids = set(fields[path]["source_event_ids"])
            if not any(source_ids.intersection(block_evidence[target][1]) for target in targets):
                _fail(f"{location}的重复省略目标缺少相容章内证据。")

    chapter = {
        "chapter_id": chapter_id,
        "index": chapter_input["index"],
        "start": chapter_input["start"],
        "end": chapter_input["end"],
        "planned_title": chapter_input["planned_title"],
        "title": response["title"],
        "keywords": keywords,
        "sections": sections,
    }
    return ValidatedChapterComposition(
        chapter=chapter,
        coverage={"items": items},
        block_evidence=block_evidence,
    )


def _window_fields(
    chapter_input: Mapping[str, Any], window_ids: Sequence[str]
) -> list[dict[str, Any]]:
    allowed = set(window_ids)
    return [
        dict(field)
        for field in chapter_input["source_fields"]
        if allowed.intersection(field["source_event_ids"])
    ]


def _window_messages(
    composition_input: Mapping[str, Any],
    chapter_input: Mapping[str, Any],
    window: DocumentChapterWindow,
) -> list[dict[str, str]]:
    payload = {
        "input_id": composition_input["input_id"],
        "document_profile": composition_input["document_profile"],
        "chapter_id": chapter_input["chapter_id"],
        "window_id": window.window_id,
        "source_fields": _window_fields(chapter_input, window.event_ids),
        "events": list(window.events),
        "versions": _chapter_versions(),
    }
    return [
        {
            "role": "system",
            "content": "仅依据本窗口输入整理章内草稿，不得补充外部知识或其他章节内容，只返回JSON。",
        },
        {
            "role": "user",
            "content": "DOCUMENT_WINDOW_INPUT_JSON="
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _chapter_messages(
    composition_input: Mapping[str, Any],
    chapter_input: Mapping[str, Any],
    *,
    window_drafts: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, str]]:
    payload = {
        "input_id": composition_input["input_id"],
        "document_profile": composition_input["document_profile"],
        "chapter": chapter_input,
        "window_drafts": list(window_drafts or []),
        "versions": _chapter_versions(),
    }
    marker = (
        "DOCUMENT_CHAPTER_MERGE_INPUT_JSON="
        if window_drafts is not None
        else "DOCUMENT_CHAPTER_INPUT_JSON="
    )
    response_contract = (
        '输出必须是单个JSON对象，顶层且只能包含'
        'schema_version、chapter_id、title、keywords、sections、coverage。'
        '最小连续正文骨架为：'
        '{"schema_version":"2.0","chapter_id":"使用输入chapter_id",'
        '"title":{"text":"中文标题","content_origin":"stage9_v1",'
        '"source_content_paths":["本章来源路径"],"source_event_ids":["本章事件ID"]},'
        '"keywords":[],"sections":[{"section_id":"c0001-s001",'
        '"heading":null,"blocks":[{"block_id":"c0001-s001-p001",'
        '"type":"paragraph","role":"explanation","text":"连续中文正文",'
        '"content_origin":"stage9_v1","source_content_paths":["本章来源路径"],'
        '"source_event_ids":["按时间顺序的本章事件ID"]}]}],'
        '"coverage":{"items":[{"source_content_path":"每个coverage_required=true的路径，按输入顺序逐项列出",'
        '"status":"represented","target_block_ids":["实际承载该事实的block_id"]}]}}。'
        '必须把示例中的c0001替换为输入chapter_id；section_id依次为<chapter_id>-s001等，'
        '段落block_id依次为<section_id>-p001等。heading若非null必须使用与title相同的四个证据字段。'
        'keywords为0至6个证据对象。sections为1至4项，每节1至4块。'
        '默认只用paragraph；确有必要时才可用list、comparison或callout。'
        'paragraph role只能是overview/explanation/argument/case/method/conclusion；'
        'callout结构与paragraph相同，role只能是conclusion/limitation/notice，ID后缀callout001；'
        'list只能包含block_id/type/role/list_kind/items，role为facts/steps/criteria/methods/evidence，'
        'list_kind为bullet或numbered，每项含item_id、text和三个证据字段；'
        'comparison只能包含block_id/type/role/columns/rows，role固定comparison，'
        '每个row含row_id、dimension、cells，每个cell是四字段证据对象。'
        'content_origin=stage9_v1时source_content_paths不能为空；若仅依据ASR则使用chapter_asr且路径数组为空。'
        '所有事件ID必须来自本章并按时间顺序。coverage必须逐项覆盖全部coverage_required=true字段；'
        'represented的目标块必须引用该路径，只有重复信息才可用omitted_duplicate。'
        '正文中的每个具体症状、数字、实体、否定、判断或建议，都必须由同一块source_event_ids所列事件直接支持；'
        '若无法列出直接证据事件，就删除该具体表述，不得只引用相邻事件。'
    )
    return [
        {
            "role": "system",
            "content": (
                "仅依据本章v1事实骨架和章内ASR编排连续正文。默认段落；小标题、列表、"
                "对比、案例和结论仅在内容确有需要时出现。不得补充外部知识、改变边界或返回全文字段。"
                + response_contract
            ),
        },
        {
            "role": "user",
            "content": marker
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _chapter_versions() -> dict[str, str]:
    return {
        "response_schema": DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION,
        "chapter_prompt": DOCUMENT_CHAPTER_PROMPT_VERSION,
        "merge_prompt": DOCUMENT_CHAPTER_MERGE_PROMPT_VERSION,
        "windowing": DOCUMENT_CHAPTER_WINDOWING_VERSION,
        "fact_policy": DOCUMENT_CHAPTER_FACT_POLICY_VERSION,
        "coverage_policy": DOCUMENT_CHAPTER_COVERAGE_POLICY_VERSION,
    }


def _field_evidence(field: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": field["text"],
        "content_origin": "stage9_v1",
        "source_content_paths": [field["source_content_path"]],
        "source_event_ids": list(field["source_event_ids"]),
    }


def _mock_chapter_response(chapter_input: Mapping[str, Any]) -> dict[str, Any]:
    fields = list(chapter_input["source_fields"])
    title = next(field for field in fields if field["kind"] == "title")
    core = [field for field in fields if field["coverage_required"]]
    keywords = [field for field in fields if field["kind"] == "keyword"][:6]
    block_id = f"{chapter_input['chapter_id']}-s001-p001"
    block_paths = [field["source_content_path"] for field in core]
    block_event_ids = _ordered_unique(
        [field["source_event_ids"] for field in core]
    )
    block_text = "；".join(str(field["text"]).rstrip("。；") for field in core) + "。"
    return {
        "schema_version": DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION,
        "chapter_id": chapter_input["chapter_id"],
        "title": _field_evidence(title),
        "keywords": [_field_evidence(field) for field in keywords],
        "sections": [
            {
                "section_id": f"{chapter_input['chapter_id']}-s001",
                "heading": None,
                "blocks": [
                    {
                        "block_id": block_id,
                        "type": "paragraph",
                        "role": "explanation",
                        "text": block_text,
                        "content_origin": "stage9_v1",
                        "source_content_paths": block_paths,
                        "source_event_ids": block_event_ids,
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


class DeterministicDocumentChapterMockTransport:
    """解析冻结提示输入并返回确定性本地响应。"""

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
            raise LLMRetryableError("R3仅允许mock backend。")
        self.call_count += 1
        window_marker = "DOCUMENT_WINDOW_INPUT_JSON="
        window_content = next(
            (
                str(message["content"])
                for message in reversed(messages)
                if window_marker in str(message["content"])
            ),
            None,
        )
        if window_content is not None:
            try:
                payload = json.loads(window_content.split(window_marker, 1)[1])
                fields = payload["source_fields"]
                events = payload["events"]
                window_id = payload["window_id"]
                event_ids = [event["id"] for event in events]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise LLMRetryableError("mock无法解析窗口输入。") from exc
            paths = [field["source_content_path"] for field in fields]
            text_parts = [str(field["text"]) for field in fields]
            if not text_parts:
                text_parts = [str(event["text"]) for event in events]
            return {
                "schema_version": DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION,
                "window_id": window_id,
                "text": "；".join(text_parts),
                "source_content_paths": paths,
                "source_event_ids": event_ids,
            }
        for marker in (
            "DOCUMENT_CHAPTER_INPUT_JSON=",
            "DOCUMENT_CHAPTER_MERGE_INPUT_JSON=",
        ):
            content = next(
                (
                    str(message["content"])
                    for message in reversed(messages)
                    if marker in str(message["content"])
                ),
                None,
            )
            if content is not None:
                try:
                    payload = json.loads(content.split(marker, 1)[1])
                    return _mock_chapter_response(payload["chapter"])
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise LLMRetryableError("mock无法解析章级输入。") from exc
        raise LLMRetryableError("mock提示缺少R3输入标记。")


def _offline_config() -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model=DOCUMENT_CHAPTER_MOCK_MODEL,
        timeout_seconds=1.0,
        api_key="",
        mock_response={},
        enable_thinking=False,
        temperature=0.0,
    )


def _window_validator(
    *,
    chapter_input: Mapping[str, Any],
    window: DocumentChapterWindow,
) -> Any:
    fields, events = _source_maps(chapter_input)
    allowed_paths = {
        field["source_content_path"] for field in _window_fields(chapter_input, window.event_ids)
    }
    allowed_ids = set(window.event_ids)

    def validate(response: dict[str, Any]) -> dict[str, Any]:
        try:
            _exact_keys(
                response,
                {"schema_version", "window_id", "text", "source_content_paths", "source_event_ids"},
                "window_response",
            )
            if (
                response["schema_version"] != DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION
                or response["window_id"] != window.window_id
                or not isinstance(response["text"], str)
                or not response["text"].strip()
            ):
                _fail("window_response身份或文本无效。")
            paths = response["source_content_paths"]
            event_ids = response["source_event_ids"]
            if (
                not isinstance(paths, list)
                or len(paths) != len(set(paths))
                or any(path not in allowed_paths for path in paths)
                or not isinstance(event_ids, list)
                or not event_ids
                or len(event_ids) != len(set(event_ids))
                or any(event_id not in allowed_ids for event_id in event_ids)
                or event_ids != [event_id for event_id in window.event_ids if event_id in set(event_ids)]
            ):
                _fail("window_response证据无效、重复或乱序。")
            _validate_high_risk_text(
                response["text"],
                source_paths=paths,
                source_event_ids=event_ids,
                fields=fields,
                events=events,
                location="window_response.text",
            )
            return dict(response)
        except DocumentChapterCompositionError as exc:
            raise LLMRetryableError(str(exc)) from exc

    return validate


def _chapter_validator(
    *,
    chapter_input: Mapping[str, Any],
    context: DocumentContentValidationContext,
    window_event_groups: Sequence[Sequence[str]] | None = None,
) -> Any:
    def validate(response: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = validate_chapter_composition_response(
                response, chapter_input=chapter_input, context=context
            )
            if window_event_groups is not None:
                cited = set(validated.chapter["title"]["source_event_ids"])
                for keyword in validated.chapter["keywords"]:
                    cited.update(keyword["source_event_ids"])
                for _, event_ids in validated.block_evidence.values():
                    cited.update(event_ids)
                if any(not cited.intersection(group) for group in window_event_groups):
                    _fail("章内合并结果未引用每个确定性窗口的证据。")
            return {
                "schema_version": DOCUMENT_CHAPTER_RESPONSE_SCHEMA_VERSION,
                "chapter_id": validated.chapter["chapter_id"],
                "title": validated.chapter["title"],
                "keywords": validated.chapter["keywords"],
                "sections": validated.chapter["sections"],
                "coverage": validated.coverage,
            }
        except DocumentChapterCompositionError as exc:
            raise LLMRetryableError(str(exc)) from exc

    return validate


def _call(
    output_root: str | Path,
    task_id: str,
    *,
    call_id: str,
    messages: list[dict[str, str]],
    validator: Any,
    validation_id: str,
    artifact_subdir: Path,
    config: LLMConfig,
    transport: LLMTransport,
) -> LLMCallResult:
    return call_task_llm(
        output_root,
        task_id,
        call_id=call_id,
        messages=messages,
        response_format={"type": "json_object"},
        config=config,
        transport=transport,
        response_validator=validator,
        validation_id=validation_id,
        artifact_subdir=artifact_subdir,
        cache_namespace=DOCUMENT_CHAPTER_CACHE_NAMESPACE,
        save_validation_audit=True,
    )


def compose_document_chapters(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    config: LLMConfig,
    transport: LLMTransport,
    max_window_chars: int = DEFAULT_DOCUMENT_WINDOW_MAX_CHARS,
    max_window_events: int = DEFAULT_DOCUMENT_WINDOW_MAX_EVENTS,
    max_chapters: int | None = None,
) -> OfflineChapterCompositionResult:
    """用显式配置和transport运行共享章级流水线；不发布preview。"""

    try:
        validate_document_composition_input(dict(composition_input))
    except DocumentCompositionError as exc:
        raise DocumentChapterCompositionError(str(exc)) from exc
    if composition_input["task_id"] != task_id:
        _fail("task_id与DocumentCompositionInput不一致。")
    if max_chapters is not None and (
        isinstance(max_chapters, bool)
        or not isinstance(max_chapters, int)
        or not 1 <= max_chapters <= len(composition_input["chapters"])
    ):
        _fail("max_chapters必须是现有章节范围内的正整数。")
    context = _chapter_context(composition_input)
    validated_chapters: list[ValidatedChapterComposition] = []
    calls: list[LLMCallResult] = []
    attempt_root = (
        Path("content")
        / "document-v2"
        / "attempts"
        / composition_input["input_id"]
        / "chapters"
    )
    try:
        selected_chapters = composition_input["chapters"][:max_chapters]
        for chapter_input in selected_chapters:
            chapter_id = chapter_input["chapter_id"]
            windows = split_document_chapter_events(
                chapter_input,
                max_window_chars=max_window_chars,
                max_window_events=max_window_events,
            )
            chapter_artifacts = attempt_root / chapter_id / "calls"
            drafts: list[dict[str, Any]] = []
            if len(windows) > 1:
                for window in windows:
                    result = _call(
                        output_root,
                        task_id,
                        call_id=f"doc-{chapter_id}-w{window.window_index:03d}",
                        messages=_window_messages(composition_input, chapter_input, window),
                        validator=_window_validator(
                            chapter_input=chapter_input, window=window
                        ),
                        validation_id=DOCUMENT_WINDOW_DRAFT_VALIDATION_ID,
                        artifact_subdir=chapter_artifacts,
                        config=config,
                        transport=transport,
                    )
                    calls.append(result)
                    drafts.append(result.response)
                final_result = _call(
                    output_root,
                    task_id,
                    call_id=f"doc-{chapter_id}-merge",
                    messages=_chapter_messages(
                        composition_input,
                        chapter_input,
                        window_drafts=drafts,
                    ),
                    validator=_chapter_validator(
                        chapter_input=chapter_input,
                        context=context,
                        window_event_groups=[window.event_ids for window in windows],
                    ),
                    validation_id=DOCUMENT_CHAPTER_VALIDATION_ID,
                    artifact_subdir=chapter_artifacts,
                    config=config,
                    transport=transport,
                )
            else:
                final_result = _call(
                    output_root,
                    task_id,
                    call_id=f"doc-{chapter_id}-single",
                    messages=_chapter_messages(
                        composition_input,
                        chapter_input,
                        window_drafts=None,
                    ),
                    validator=_chapter_validator(
                        chapter_input=chapter_input, context=context
                    ),
                    validation_id=DOCUMENT_CHAPTER_VALIDATION_ID,
                    artifact_subdir=chapter_artifacts,
                    config=config,
                    transport=transport,
                )
            calls.append(final_result)
            validated_chapters.append(
                validate_chapter_composition_response(
                    final_result.response,
                    chapter_input=chapter_input,
                    context=context,
                )
            )
    except LLMAdapterError as exc:
        raise DocumentChapterCompositionError(str(exc)) from exc

    cache_hit_count = sum(call.cache_hit for call in calls)
    return OfflineChapterCompositionResult(
        input_id=str(composition_input["input_id"]),
        chapters=tuple(validated_chapters),
        business_call_count=len(calls),
        supplier_request_count=sum(0 if call.cache_hit else call.attempt_count for call in calls),
        total_attempt_count=sum(call.attempt_count for call in calls),
        cache_hit_count=cache_hit_count,
        all_cache_hit=cache_hit_count == len(calls),
        call_paths=tuple(call.output_path for call in calls),
    )


def compose_document_chapters_offline_mock(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    transport: LLMTransport | None = None,
    max_window_chars: int = DEFAULT_DOCUMENT_WINDOW_MAX_CHARS,
    max_window_events: int = DEFAULT_DOCUMENT_WINDOW_MAX_EVENTS,
) -> OfflineChapterCompositionResult:
    """只在临时任务中运行章级mock；不读取.env、不联网、不发布preview。"""

    return compose_document_chapters(
        output_root,
        task_id,
        composition_input,
        config=_offline_config(),
        transport=transport or DeterministicDocumentChapterMockTransport(),
        max_window_chars=max_window_chars,
        max_window_events=max_window_events,
    )
