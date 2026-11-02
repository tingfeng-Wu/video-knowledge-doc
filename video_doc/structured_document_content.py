"""阶段9-R StructuredDocumentContent 2.0纯数据契约与校验。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


STRUCTURED_DOCUMENT_CONTENT_SCHEMA_VERSION = "2.0"
DOCUMENT_CONTENT_OUTPUT_LANGUAGE = "zh-CN"
DOCUMENT_PROFILE_POLICY_VERSION = "document-profile-policy-2.0"
DOCUMENT_BLOCK_POLICY_VERSION = "document-block-policy-2.0"
DOCUMENT_SOURCE_PATH_VERSION = "document-source-path-2.0"
DOCUMENT_COVERAGE_POLICY_VERSION = "document-v1-coverage-2.0"
DOCUMENT_EVIDENCE_POLICY_VERSION = "document-block-evidence-2.0"
DOCUMENT_CONTENT_VALIDATOR_VERSION = "structured-document-content-validator-2.0"

DOCUMENT_PROFILES = frozenset(
    {"short_explainer", "course", "academic_report", "speech"}
)
DOCUMENT_BLOCK_TYPES = frozenset({"paragraph", "list", "comparison", "callout"})
DOCUMENT_CONTENT_ORIGINS = frozenset({"stage9_v1", "chapter_asr"})
PARAGRAPH_ROLES = frozenset(
    {"overview", "explanation", "argument", "case", "method", "conclusion"}
)
LIST_ROLES = frozenset({"facts", "steps", "criteria", "methods", "evidence"})
CALLOUT_ROLES = frozenset({"conclusion", "limitation", "notice"})
COMPARISON_ROLES = frozenset({"comparison"})
LIST_KINDS = frozenset({"bullet", "numbered"})
COVERAGE_STATUSES = frozenset({"represented", "omitted_duplicate"})
GENERATION_MODES = frozenset(
    {
        "document-composition-mock",
        "document-composition-real",
        "document-composition-offline-publish",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_ID_RE = re.compile(r"^document-content-[0-9a-f]{12}$")
_CHAPTER_ID_RE = re.compile(r"^c[0-9]{4}$")
_EVENT_ID_RE = re.compile(r"^a[0-9]{4,}$")
_SOURCE_PATH_RE = re.compile(
    r"^chapters\[(c[0-9]{4})\]\.(?:title|summary|key_points\[[0-9]+\]|keywords\[[0-9]+\])$"
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


class StructuredDocumentContentError(ValueError):
    """StructuredDocumentContent 2.0不符合冻结契约。"""


@dataclass(frozen=True)
class DocumentContentValidationContext:
    """由R2权威加载器构造；R1测试只使用内存fixture。"""

    known_source_paths: frozenset[str]
    required_core_paths: frozenset[str]
    chapter_event_ids: Mapping[str, tuple[str, ...]]
    event_times: Mapping[str, tuple[int | float, int | float]]


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def structured_document_content_identity_sha256(payload: Mapping[str, Any]) -> str:
    """计算排除自引用content_id后的规范身份。"""

    identity = deepcopy(dict(payload))
    identity.pop("content_id", None)
    return canonical_sha256(identity)


def attach_structured_document_content_id(payload: Mapping[str, Any]) -> dict[str, Any]:
    """返回带确定性content_id的新对象，不修改输入。"""

    result = deepcopy(dict(payload))
    result.pop("content_id", None)
    result["content_id"] = (
        f"document-content-{structured_document_content_identity_sha256(result)[:12]}"
    )
    return result


def document_content_versions() -> dict[str, str]:
    return {
        "schema": STRUCTURED_DOCUMENT_CONTENT_SCHEMA_VERSION,
        "profile_policy": DOCUMENT_PROFILE_POLICY_VERSION,
        "block_policy": DOCUMENT_BLOCK_POLICY_VERSION,
        "source_path": DOCUMENT_SOURCE_PATH_VERSION,
        "coverage_policy": DOCUMENT_COVERAGE_POLICY_VERSION,
        "evidence_policy": DOCUMENT_EVIDENCE_POLICY_VERSION,
        "validator": DOCUMENT_CONTENT_VALIDATOR_VERSION,
    }


def _fail(message: str) -> None:
    raise StructuredDocumentContentError(message)


def _require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{location}必须是对象。")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], location: str
) -> None:
    if set(value) != expected:
        _fail(f"{location}字段集合不符合Schema。")


def _require_nonempty_text(value: Any, location: str, *, chinese: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{location}必须是非空字符串。")
    if chinese and _CJK_RE.search(value) is None:
        _fail(f"{location}必须包含中文字符。")
    return value


def _require_sha256(value: Any, location: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(f"{location}必须是小写64位SHA-256。")
    return value


def _require_number(value: Any, location: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{location}必须是有限数值。")
    if not math.isfinite(float(value)):
        _fail(f"{location}必须是有限数值。")
    return value


def _require_string_list(
    value: Any,
    location: str,
    *,
    allow_empty: bool,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        _fail(f"{location}必须是{'可空' if allow_empty else '非空'}字符串数组。")
    if any(not isinstance(item, str) or not item for item in value):
        _fail(f"{location}包含无效字符串。")
    if len(value) != len(set(value)):
        _fail(f"{location}不得重复。")
    if pattern is not None and any(pattern.fullmatch(item) is None for item in value):
        _fail(f"{location}包含格式无效的值。")
    return value


def _ordered_unique(groups: Sequence[Sequence[str]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _global_event_order(context: DocumentContentValidationContext) -> dict[str, int]:
    event_ids: list[str] = []
    for chapter_id in sorted(context.chapter_event_ids):
        event_ids.extend(context.chapter_event_ids[chapter_id])
    if len(event_ids) != len(set(event_ids)):
        _fail("validation_context中的事件ID跨章重复。")
    return {event_id: index for index, event_id in enumerate(event_ids)}


def _validate_source_and_events(
    payload: Mapping[str, Any],
    location: str,
    *,
    context: DocumentContentValidationContext | None,
    chapter_id: str | None,
) -> tuple[list[str], list[str]]:
    origin = payload.get("content_origin")
    if origin not in DOCUMENT_CONTENT_ORIGINS:
        _fail(f"{location}.content_origin无效。")
    paths = _require_string_list(
        payload.get("source_content_paths"),
        f"{location}.source_content_paths",
        allow_empty=origin == "chapter_asr",
        pattern=_SOURCE_PATH_RE,
    )
    if origin == "chapter_asr" and paths:
        _fail(f"{location}使用chapter_asr时不得伪造v1来源路径。")
    if origin == "stage9_v1" and not paths:
        _fail(f"{location}使用stage9_v1时必须提供来源路径。")
    event_ids = _require_string_list(
        payload.get("source_event_ids"),
        f"{location}.source_event_ids",
        allow_empty=False,
        pattern=_EVENT_ID_RE,
    )
    if chapter_id is not None:
        for path in paths:
            match = _SOURCE_PATH_RE.fullmatch(path)
            if match is None or match.group(1) != chapter_id:
                _fail(f"{location}不得引用其他章节的v1字段。")
    if context is None:
        return paths, event_ids
    unknown_paths = set(paths) - context.known_source_paths
    if unknown_paths:
        _fail(f"{location}引用未知v1字段路径。")
    if chapter_id is None:
        allowed_events = set(_global_event_order(context))
    else:
        if chapter_id not in context.chapter_event_ids:
            _fail(f"validation_context缺少{chapter_id}事件。")
        allowed_events = set(context.chapter_event_ids[chapter_id])
    if any(event_id not in allowed_events for event_id in event_ids):
        _fail(f"{location}引用未知或章外事件。")
    rank = _global_event_order(context)
    if event_ids != sorted(event_ids, key=rank.__getitem__):
        _fail(f"{location}.source_event_ids未按时间轴顺序排列。")
    return paths, event_ids


def _validate_evidence_text(
    payload: Any,
    location: str,
    *,
    context: DocumentContentValidationContext | None,
    chapter_id: str | None,
    chinese: bool,
) -> tuple[list[str], list[str]]:
    evidence = _require_object(payload, location)
    _require_exact_keys(
        evidence,
        {"text", "content_origin", "source_content_paths", "source_event_ids"},
        location,
    )
    _require_nonempty_text(evidence["text"], f"{location}.text", chinese=chinese)
    return _validate_source_and_events(
        evidence, location, context=context, chapter_id=chapter_id
    )


def _expected_block_id_pattern(scope: str, block_type: str) -> re.Pattern[str]:
    suffix = {
        "paragraph": r"p[0-9]{3}",
        "list": r"list[0-9]{3}",
        "comparison": r"cmp[0-9]{3}",
        "callout": r"callout[0-9]{3}",
    }[block_type]
    if scope == "overview":
        return re.compile(rf"^overview-{suffix}$")
    if scope == "conclusion":
        return re.compile(rf"^conclusion-{suffix}$")
    return re.compile(rf"^{scope}-s[0-9]{{3}}-{suffix}$")


def _validate_block(
    payload: Any,
    location: str,
    *,
    scope: str,
    context: DocumentContentValidationContext | None,
) -> tuple[str, list[str], list[str], str]:
    block = _require_object(payload, location)
    block_type = block.get("type")
    if block_type not in DOCUMENT_BLOCK_TYPES:
        _fail(f"{location}.type无效。")
    block_id = _require_nonempty_text(block.get("block_id"), f"{location}.block_id")
    if _expected_block_id_pattern(scope, block_type).fullmatch(block_id) is None:
        _fail(f"{location}.block_id与位置或类型不一致。")
    chapter_id = scope if _CHAPTER_ID_RE.fullmatch(scope) else None
    path_groups: list[list[str]] = []
    event_groups: list[list[str]] = []
    role = block.get("role")

    if block_type in {"paragraph", "callout"}:
        _require_exact_keys(
            block,
            {
                "block_id",
                "type",
                "role",
                "text",
                "content_origin",
                "source_content_paths",
                "source_event_ids",
            },
            location,
        )
        allowed_roles = PARAGRAPH_ROLES if block_type == "paragraph" else CALLOUT_ROLES
        if role not in allowed_roles:
            _fail(f"{location}.role与块类型不匹配。")
        _require_nonempty_text(block["text"], f"{location}.text", chinese=True)
        paths, events = _validate_source_and_events(
            block, location, context=context, chapter_id=chapter_id
        )
        path_groups.append(paths)
        event_groups.append(events)
    elif block_type == "list":
        _require_exact_keys(
            block,
            {"block_id", "type", "role", "list_kind", "items"},
            location,
        )
        if role not in LIST_ROLES or block["list_kind"] not in LIST_KINDS:
            _fail(f"{location}列表role或list_kind无效。")
        items = block["items"]
        if not isinstance(items, list) or not 2 <= len(items) <= 7:
            _fail(f"{location}.items必须包含2至7项。")
        item_ids: set[str] = set()
        for index, item_value in enumerate(items, start=1):
            item_location = f"{location}.items[{index - 1}]"
            item = _require_object(item_value, item_location)
            _require_exact_keys(
                item,
                {
                    "item_id",
                    "text",
                    "content_origin",
                    "source_content_paths",
                    "source_event_ids",
                },
                item_location,
            )
            expected_item_id = f"{block_id}-i{index:03d}"
            if item.get("item_id") != expected_item_id or expected_item_id in item_ids:
                _fail(f"{item_location}.item_id无效。")
            item_ids.add(expected_item_id)
            _require_nonempty_text(item["text"], f"{item_location}.text", chinese=True)
            paths, events = _validate_source_and_events(
                item, item_location, context=context, chapter_id=chapter_id
            )
            path_groups.append(paths)
            event_groups.append(events)
    else:
        _require_exact_keys(
            block,
            {"block_id", "type", "role", "columns", "rows"},
            location,
        )
        if role not in COMPARISON_ROLES:
            _fail(f"{location}.role与comparison不匹配。")
        columns = _require_string_list(
            block["columns"], f"{location}.columns", allow_empty=False
        )
        if not 2 <= len(columns) <= 4:
            _fail(f"{location}.columns必须包含2至4列。")
        rows = block["rows"]
        if not isinstance(rows, list) or not 2 <= len(rows) <= 6:
            _fail(f"{location}.rows必须包含2至6个维度。")
        for row_index, row_value in enumerate(rows, start=1):
            row_location = f"{location}.rows[{row_index - 1}]"
            row = _require_object(row_value, row_location)
            _require_exact_keys(row, {"row_id", "dimension", "cells"}, row_location)
            if row.get("row_id") != f"{block_id}-r{row_index:03d}":
                _fail(f"{row_location}.row_id无效。")
            _require_nonempty_text(row["dimension"], f"{row_location}.dimension", chinese=True)
            cells = row["cells"]
            if not isinstance(cells, list) or len(cells) != len(columns):
                _fail(f"{row_location}.cells必须与columns等长。")
            for cell_index, cell in enumerate(cells):
                paths, events = _validate_evidence_text(
                    cell,
                    f"{row_location}.cells[{cell_index}]",
                    context=context,
                    chapter_id=chapter_id,
                    chinese=True,
                )
                path_groups.append(paths)
                event_groups.append(events)
    return block_id, _ordered_unique(path_groups), _ordered_unique(event_groups), block_type


def _validate_source_identity(document: Mapping[str, Any]) -> None:
    source_content = _require_object(
        document["source_content_bundle"], "source_content_bundle"
    )
    _require_exact_keys(
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
        "source_content_bundle",
    )
    if source_content["schema_version"] != "1.0" or not isinstance(
        source_content["result_id"], str
    ) or not source_content["result_id"].startswith("content-"):
        _fail("source_content_bundle身份无效。")
    for field in (
        "canonical_sha256",
        "content_file_sha256",
        "execution_report_file_sha256",
        "manifest_file_sha256",
        "input_manifest_file_sha256",
    ):
        _require_sha256(source_content[field], f"source_content_bundle.{field}")

    source_plan = _require_object(document["source_chapter_plan"], "source_chapter_plan")
    _require_exact_keys(
        source_plan,
        {
            "schema_version",
            "result_id",
            "canonical_sha256",
            "chapter_plan_file_sha256",
            "execution_report_file_sha256",
            "manifest_file_sha256",
        },
        "source_chapter_plan",
    )
    if source_plan["schema_version"] != "1.0" or not isinstance(
        source_plan["result_id"], str
    ) or not source_plan["result_id"].strip():
        _fail("source_chapter_plan身份无效。")
    for field in (
        "canonical_sha256",
        "chapter_plan_file_sha256",
        "execution_report_file_sha256",
        "manifest_file_sha256",
    ):
        _require_sha256(source_plan[field], f"source_chapter_plan.{field}")

    timeline = _require_object(document["source_timeline"], "source_timeline")
    _require_exact_keys(
        timeline,
        {"schema_version", "canonical_sha256", "timeline_file_sha256", "event_count"},
        "source_timeline",
    )
    if timeline["schema_version"] != "1.0":
        _fail("source_timeline.schema_version无效。")
    _require_sha256(timeline["canonical_sha256"], "source_timeline.canonical_sha256")
    _require_sha256(timeline["timeline_file_sha256"], "source_timeline.timeline_file_sha256")
    if isinstance(timeline["event_count"], bool) or not isinstance(
        timeline["event_count"], int
    ) or timeline["event_count"] < 1:
        _fail("source_timeline.event_count无效。")

    provenance = _require_object(
        document["chaptering_provenance"], "chaptering_provenance"
    )
    _require_exact_keys(
        provenance,
        {"status", "result_source", "quality_level", "execution_report_sha256"},
        "chaptering_provenance",
    )
    allowed = {
        ("success", "primary", "normal"),
        ("success", "fallback", "unknown"),
        ("degraded", "emergency", "degraded"),
    }
    state = (
        provenance["status"],
        provenance["result_source"],
        provenance["quality_level"],
    )
    if state not in allowed:
        _fail("chaptering_provenance状态组合无效。")
    _require_sha256(
        provenance["execution_report_sha256"],
        "chaptering_provenance.execution_report_sha256",
    )


def _validate_generation(document: Mapping[str, Any]) -> None:
    generation = _require_object(document["generation"], "generation")
    _require_exact_keys(
        generation,
        {
            "status",
            "mode",
            "backend",
            "model",
            "temperature",
            "chapter_prompt_version",
            "global_prompt_version",
            "content_policy_version",
            "validator_version",
            "api_request_count",
        },
        "generation",
    )
    if generation["status"] != "success" or generation["mode"] not in GENERATION_MODES:
        _fail("generation状态或模式无效。")
    if generation["backend"] not in {"mock", "openai_compatible"}:
        _fail("generation.backend无效。")
    for field in (
        "model",
        "chapter_prompt_version",
        "global_prompt_version",
        "content_policy_version",
    ):
        _require_nonempty_text(generation[field], f"generation.{field}")
    temperature = _require_number(generation["temperature"], "generation.temperature")
    if temperature < 0:
        _fail("generation.temperature不得为负数。")
    if generation["validator_version"] != DOCUMENT_CONTENT_VALIDATOR_VERSION:
        _fail("generation.validator_version与当前契约不一致。")
    count = generation["api_request_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        _fail("generation.api_request_count无效。")
    if generation["backend"] == "mock" and count != 0:
        _fail("mock generation的api_request_count必须为0。")


def validate_structured_document_content(
    payload: Any,
    *,
    context: DocumentContentValidationContext | None = None,
) -> None:
    """严格校验2.0结构；不读取文件、不构造LLM或网络依赖。"""

    document = _require_object(payload, "StructuredDocumentContent")
    _require_exact_keys(
        document,
        {
            "schema_version",
            "content_id",
            "task_id",
            "language",
            "strategy",
            "document_profile",
            "source_content_bundle",
            "source_chapter_plan",
            "source_timeline",
            "chaptering_provenance",
            "title",
            "overview",
            "chapters",
            "overall_conclusion",
            "coverage",
            "evidence_index",
            "generation",
            "content_versions",
        },
        "StructuredDocumentContent",
    )
    if document["schema_version"] != STRUCTURED_DOCUMENT_CONTENT_SCHEMA_VERSION:
        _fail("StructuredDocumentContent Schema版本无效。")
    if not isinstance(document["content_id"], str) or _CONTENT_ID_RE.fullmatch(
        document["content_id"]
    ) is None:
        _fail("content_id格式无效。")
    expected_id = (
        f"document-content-{structured_document_content_identity_sha256(document)[:12]}"
    )
    if document["content_id"] != expected_id:
        _fail("content_id与规范内容身份不一致。")
    _require_nonempty_text(document["task_id"], "task_id")
    if document["language"] != DOCUMENT_CONTENT_OUTPUT_LANGUAGE:
        _fail("language必须固定为zh-CN。")
    if document["strategy"] not in {"llm", "rule", "ppo"}:
        _fail("strategy无效。")
    if document["document_profile"] not in DOCUMENT_PROFILES:
        _fail("document_profile不属于冻结四种profile。")
    if document["content_versions"] != document_content_versions():
        _fail("content_versions与当前冻结版本不一致。")
    _validate_source_identity(document)
    _validate_generation(document)
    _validate_evidence_text(
        document["title"],
        "title",
        context=context,
        chapter_id=None,
        chinese=True,
    )

    block_evidence: dict[str, tuple[list[str], list[str]]] = {}
    block_scope: dict[str, str] = {}
    overview = document["overview"]
    if not isinstance(overview, list) or not 1 <= len(overview) <= 2:
        _fail("overview必须包含1至2个段落。")
    for index, block in enumerate(overview):
        block_id, paths, events, block_type = _validate_block(
            block,
            f"overview[{index}]",
            scope="overview",
            context=context,
        )
        if block_type != "paragraph" or block.get("role") != "overview":
            _fail("overview只允许paragraph(role=overview)。")
        if block_id in block_evidence:
            _fail("block_id全局重复。")
        block_evidence[block_id] = (paths, events)
        block_scope[block_id] = "overview"

    chapters = document["chapters"]
    if not isinstance(chapters, list) or not chapters:
        _fail("chapters必须是非空数组。")
    previous_end: int | float | None = None
    for expected_index, chapter_value in enumerate(chapters, start=1):
        location = f"chapters[{expected_index - 1}]"
        chapter = _require_object(chapter_value, location)
        _require_exact_keys(
            chapter,
            {
                "chapter_id",
                "index",
                "start",
                "end",
                "planned_title",
                "title",
                "keywords",
                "sections",
            },
            location,
        )
        chapter_id = f"c{expected_index:04d}"
        if chapter["chapter_id"] != chapter_id or chapter["index"] != expected_index:
            _fail(f"{location}章节ID或序号不连续。")
        start = _require_number(chapter["start"], f"{location}.start")
        end = _require_number(chapter["end"], f"{location}.end")
        if start < 0 or end <= start or (previous_end is not None and start != previous_end):
            _fail(f"{location}时间范围无效或不连续。")
        if expected_index == 1 and start != 0:
            _fail("第一章必须从0开始。")
        previous_end = end
        _require_nonempty_text(chapter["planned_title"], f"{location}.planned_title")
        _validate_evidence_text(
            chapter["title"],
            f"{location}.title",
            context=context,
            chapter_id=chapter_id,
            chinese=True,
        )
        keywords = chapter["keywords"]
        if not isinstance(keywords, list) or len(keywords) > 6:
            _fail(f"{location}.keywords必须包含0至6项。")
        for keyword_index, keyword in enumerate(keywords):
            _validate_evidence_text(
                keyword,
                f"{location}.keywords[{keyword_index}]",
                context=context,
                chapter_id=chapter_id,
                chinese=False,
            )
        sections = chapter["sections"]
        if not isinstance(sections, list) or not 1 <= len(sections) <= 4:
            _fail(f"{location}.sections必须包含1至4项。")
        chapter_callouts = 0
        chapter_comparisons = 0
        for section_index, section_value in enumerate(sections, start=1):
            section_location = f"{location}.sections[{section_index - 1}]"
            section = _require_object(section_value, section_location)
            _require_exact_keys(section, {"section_id", "heading", "blocks"}, section_location)
            expected_section_id = f"{chapter_id}-s{section_index:03d}"
            if section["section_id"] != expected_section_id:
                _fail(f"{section_location}.section_id无效。")
            if section["heading"] is not None:
                _validate_evidence_text(
                    section["heading"],
                    f"{section_location}.heading",
                    context=context,
                    chapter_id=chapter_id,
                    chinese=True,
                )
            blocks = section["blocks"]
            if not isinstance(blocks, list) or not 1 <= len(blocks) <= 4:
                _fail(f"{section_location}.blocks必须包含1至4项。")
            for block_index, block in enumerate(blocks):
                block_id, paths, events, block_type = _validate_block(
                    block,
                    f"{section_location}.blocks[{block_index}]",
                    scope=chapter_id,
                    context=context,
                )
                if not block_id.startswith(f"{expected_section_id}-"):
                    _fail(f"{section_location}包含不属于本section的block_id。")
                if block_id in block_evidence:
                    _fail("block_id全局重复。")
                block_evidence[block_id] = (paths, events)
                block_scope[block_id] = chapter_id
                chapter_callouts += block_type == "callout"
                chapter_comparisons += block_type == "comparison"
        if chapter_callouts > 1 or chapter_comparisons > 1:
            _fail(f"{location}最多包含1个callout和1个comparison。")

    conclusion = document["overall_conclusion"]
    if not isinstance(conclusion, list) or len(conclusion) > 2:
        _fail("overall_conclusion必须包含0至2个块。")
    if len(conclusion) == 2 and any(
        not isinstance(item, dict) or item.get("type") != "paragraph"
        for item in conclusion
    ):
        _fail("两个全文结论块时必须均为paragraph。")
    for index, block in enumerate(conclusion):
        block_id, paths, events, block_type = _validate_block(
            block,
            f"overall_conclusion[{index}]",
            scope="conclusion",
            context=context,
        )
        if block_type not in {"paragraph", "callout"} or block.get("role") != "conclusion":
            _fail("overall_conclusion只允许conclusion段落或callout。")
        if block_id in block_evidence:
            _fail("block_id全局重复。")
        block_evidence[block_id] = (paths, events)
        block_scope[block_id] = "conclusion"

    coverage = _require_object(document["coverage"], "coverage")
    _require_exact_keys(coverage, {"items"}, "coverage")
    items = coverage["items"]
    if not isinstance(items, list):
        _fail("coverage.items必须是数组。")
    covered_paths: set[str] = set()
    for index, item_value in enumerate(items):
        location = f"coverage.items[{index}]"
        item = _require_object(item_value, location)
        _require_exact_keys(
            item, {"source_content_path", "status", "target_block_ids"}, location
        )
        path = item["source_content_path"]
        if not isinstance(path, str) or _SOURCE_PATH_RE.fullmatch(path) is None:
            _fail(f"{location}.source_content_path无效。")
        if path in covered_paths:
            _fail("coverage不得重复记录同一来源路径。")
        covered_paths.add(path)
        if item["status"] not in COVERAGE_STATUSES:
            _fail(f"{location}.status无效。")
        targets = _require_string_list(
            item["target_block_ids"], f"{location}.target_block_ids", allow_empty=False
        )
        source_chapter = _SOURCE_PATH_RE.fullmatch(path).group(1)  # type: ignore[union-attr]
        if any(
            target not in block_evidence or block_scope[target] != source_chapter
            for target in targets
        ):
            _fail(f"{location}必须指向同章已存在内容块。")
        if item["status"] == "represented" and not any(
            path in block_evidence[target][0] for target in targets
        ):
            _fail(f"{location}标记represented时目标块必须显式引用该v1路径。")
        if context is not None and path not in context.required_core_paths:
            _fail(f"{location}不是需要覆盖的v1核心路径。")
    if context is not None and covered_paths != set(context.required_core_paths):
        _fail("coverage未完整且唯一覆盖v1核心路径。")

    evidence_index = document["evidence_index"]
    if not isinstance(evidence_index, list):
        _fail("evidence_index必须是数组。")
    indexed_blocks: set[str] = set()
    for index, row_value in enumerate(evidence_index):
        location = f"evidence_index[{index}]"
        row = _require_object(row_value, location)
        _require_exact_keys(
            row,
            {
                "block_id",
                "location_label",
                "source_content_paths",
                "source_event_ids",
                "start",
                "end",
            },
            location,
        )
        block_id = row["block_id"]
        if block_id not in block_evidence or block_id in indexed_blocks:
            _fail(f"{location}.block_id未知或重复。")
        indexed_blocks.add(block_id)
        _require_nonempty_text(row["location_label"], f"{location}.location_label")
        paths = _require_string_list(
            row["source_content_paths"],
            f"{location}.source_content_paths",
            allow_empty=True,
            pattern=_SOURCE_PATH_RE,
        )
        events = _require_string_list(
            row["source_event_ids"],
            f"{location}.source_event_ids",
            allow_empty=False,
            pattern=_EVENT_ID_RE,
        )
        expected_paths, expected_events = block_evidence[block_id]
        if paths != expected_paths or events != expected_events:
            _fail(f"{location}与内容块证据不一致。")
        start = _require_number(row["start"], f"{location}.start")
        end = _require_number(row["end"], f"{location}.end")
        if start < 0 or end <= start:
            _fail(f"{location}时间范围无效。")
        if context is not None:
            try:
                expected_start = min(context.event_times[event_id][0] for event_id in events)
                expected_end = max(context.event_times[event_id][1] for event_id in events)
            except KeyError as exc:
                _fail(f"validation_context缺少事件时间：{exc.args[0]}")
            if start != expected_start or end != expected_end:
                _fail(f"{location}时间必须由事件确定性派生。")
    if indexed_blocks != set(block_evidence):
        _fail("evidence_index必须恰好覆盖所有内容块。")

    if context is not None:
        all_context_events = {
            event_id
            for event_ids in context.chapter_event_ids.values()
            for event_id in event_ids
        }
        if set(context.event_times) != all_context_events:
            _fail("validation_context的事件时间集合与章内事件集合不一致。")
        if document["source_timeline"]["event_count"] != len(all_context_events):
            _fail("source_timeline.event_count与validation_context不一致。")
