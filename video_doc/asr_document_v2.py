"""把通用ASR-only正文升级为待复核的StructuredDocumentContent 2.0。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from video_doc.document_composition import prepare_document_composition_input
from video_doc.document_content_preview import (
    DocumentPreviewGroup,
    assemble_structured_document_content,
    publish_document_preview,
    validate_document_preview_group,
)
from video_doc.document_export_v2 import AuthoritativeDocumentContentV2
from video_doc.fact_card_composition import (
    build_continuous_content_payload,
    build_continuous_global_payload,
    build_continuous_reviewed_chapter_result,
    compose_continuous_content_chapter_once,
    compose_continuous_global_once,
    derive_continuous_global_evidence,
    validate_continuous_global_response,
)
from video_doc.llm_adapter import (
    LLMAdapterError,
    OpenAICompatibleTransport,
    load_llm_config,
)
from video_doc.structured_document_content import canonical_sha256


CANDIDATE_CURRENT_SCHEMA_VERSION = "1.0"
CANDIDATE_PUBLISHER_VERSION = "document-candidate-publisher-1.0"


class ASRDocumentV2Error(RuntimeError):
    """通用2.0候选生成、选择或加载失败。"""


@dataclass(frozen=True)
class ASRDocumentV2Result:
    source: AuthoritativeDocumentContentV2
    preview: DocumentPreviewGroup
    document_profile: str
    review_status: str
    planned_call_count: int
    business_call_count: int
    supplier_request_count: int
    cache_hit_count: int
    all_llm_cache_hit: bool
    reused_candidate: bool


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )


def _cap_document_paragraphs(
    paragraphs: list[Mapping[str, Any]], *, max_blocks: int = 4
) -> list[dict[str, Any]]:
    normalized = [
        {
            "source_unit_ids": list(item["source_unit_ids"]),
            "fact_ids": list(item["fact_ids"]),
            "text": str(item["text"]).strip(),
        }
        for item in paragraphs
    ]
    while len(normalized) > max_blocks:
        merge_at = min(
            range(len(normalized) - 1),
            key=lambda index: (
                len(normalized[index]["text"])
                + len(normalized[index + 1]["text"]),
                index,
            ),
        )
        left, right = normalized[merge_at : merge_at + 2]
        merged = {
            "source_unit_ids": list(
                dict.fromkeys(left["source_unit_ids"] + right["source_unit_ids"])
            ),
            "fact_ids": list(dict.fromkeys(left["fact_ids"] + right["fact_ids"])),
            "text": f"{left['text']} {right['text']}",
        }
        normalized[merge_at : merge_at + 2] = [merged]
    return normalized


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_pointer_path(output_root: str | Path, task_id: str) -> Path:
    return (
        Path(output_root).expanduser()
        / task_id
        / "content"
        / "document-v2"
        / "candidate-current.json"
    )


def _has_recorded_json_parse_failure(
    output_root: str | Path,
    task_id: str,
    *,
    input_id: str,
    chapter_id: str,
) -> bool:
    audit = (
        Path(output_root).expanduser()
        / task_id
        / "content"
        / "document-v2"
        / "attempts"
        / input_id
        / "continuous-content-v1"
        / chapter_id
        / "calls"
        / f"continuous-content-{chapter_id}-validation-audit.json"
    )
    if not audit.is_file():
        return False
    try:
        payload = json.loads(audit.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    attempts = payload.get("attempts") if isinstance(payload, dict) else None
    return bool(
        payload.get("status") == "failed"
        and isinstance(attempts, list)
        and attempts
        and attempts[-1].get("error") == "LLM返回内容不是合法JSON。"
    )


def _load_global_conclusion_recovery(
    output_root: str | Path,
    task_id: str,
    *,
    input_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    audit = (
        Path(output_root).expanduser()
        / task_id
        / "content"
        / "document-v2"
        / "attempts"
        / input_id
        / "continuous-content-v1"
        / "global"
        / "calls"
        / "continuous-content-global-validation-audit.json"
    )
    if not audit.is_file():
        return None
    try:
        record = json.loads(audit.read_text(encoding="utf-8"))
        attempt = record["attempts"][-1]
        raw = attempt["raw_response"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    conclusions = raw.get("conclusions") if isinstance(raw, dict) else None
    if (
        record.get("status") != "failed"
        or attempt.get("error") != "请求C核心结论必须为1至2段。"
        or not isinstance(conclusions, list)
        or len(conclusions) != 3
        or any(not isinstance(item, str) or not item.strip() for item in conclusions)
    ):
        return None
    normalized = dict(raw)
    normalized["conclusions"] = [
        f"{conclusions[0].strip()} {conclusions[1].strip()}",
        conclusions[2].strip(),
    ]
    return validate_continuous_global_response(normalized, payload=payload)


def _preview_source(
    preview: DocumentPreviewGroup, *, pointer_path: Path
) -> AuthoritativeDocumentContentV2:
    return AuthoritativeDocumentContentV2(
        task_root=preview.group_path.parents[3],
        group_path=preview.group_path,
        current_path=pointer_path,
        document_path=preview.document_path,
        execution_report_path=preview.execution_report_path,
        manifest_path=preview.manifest_path,
        content_id=str(preview.document["content_id"]),
        document=preview.document,
        execution_report=preview.execution_report,
        manifest=preview.manifest,
        document_sha256=canonical_sha256(preview.document),
        document_file_sha256=_file_sha256(preview.document_path),
        execution_report_file_sha256=_file_sha256(preview.execution_report_path),
        manifest_file_sha256=_file_sha256(preview.manifest_path),
    )


def _write_candidate_pointer(
    path: Path,
    *,
    task_id: str,
    composition_input: Mapping[str, Any],
    preview: DocumentPreviewGroup,
) -> bool:
    payload = {
        "schema_version": CANDIDATE_CURRENT_SCHEMA_VERSION,
        "status": "needs_review",
        "task_id": task_id,
        "input_id": composition_input["input_id"],
        "document_profile": composition_input["document_profile"],
        "content_id": preview.document["content_id"],
        "group": f"previews/{preview.document['content_id']}",
        "document_sha256": canonical_sha256(preview.document),
        "publisher_version": CANDIDATE_PUBLISHER_VERSION,
    }
    content = _json_bytes(payload)
    if path.is_file() and path.read_bytes() == content:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        with temporary.open("xb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if path.read_bytes() != content:
        raise ASRDocumentV2Error("2.0待复核候选指针写入后复验不一致。")
    return False


def _load_candidate(
    output_root: str | Path,
    task_id: str,
    *,
    composition_input: Mapping[str, Any],
) -> tuple[DocumentPreviewGroup, Path] | None:
    pointer_path = _candidate_pointer_path(output_root, task_id)
    if not pointer_path.exists():
        return None
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ASRDocumentV2Error("2.0待复核候选指针无法读取。") from exc
    expected_keys = {
        "schema_version",
        "status",
        "task_id",
        "input_id",
        "document_profile",
        "content_id",
        "group",
        "document_sha256",
        "publisher_version",
    }
    content_id = payload.get("content_id") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload.get("schema_version") != CANDIDATE_CURRENT_SCHEMA_VERSION
        or payload.get("status") != "needs_review"
        or payload.get("task_id") != task_id
        or payload.get("input_id") != composition_input["input_id"]
        or payload.get("document_profile") != composition_input["document_profile"]
        or not isinstance(content_id, str)
        or payload.get("group") != f"previews/{content_id}"
        or payload.get("publisher_version") != CANDIDATE_PUBLISHER_VERSION
        or pointer_path.read_bytes() != _json_bytes(payload)
    ):
        raise ASRDocumentV2Error("2.0待复核候选指针身份或状态无效。")
    preview = validate_document_preview_group(
        pointer_path.parent / "previews" / content_id,
        composition_input=composition_input,
    )
    if payload["document_sha256"] != canonical_sha256(preview.document):
        raise ASRDocumentV2Error("2.0待复核候选指针与preview内容不一致。")
    return preview, pointer_path


def compose_task_document_v2_candidate(
    output_root: str | Path,
    task_id: str,
    *,
    document_profile: str,
    allow_llm: bool,
    max_business_call_count: int,
) -> ASRDocumentV2Result:
    """生成或复用2.0机器候选；候选始终保持needs_review。"""

    prepared = prepare_document_composition_input(
        output_root, task_id, document_profile=document_profile
    )
    composition_input = prepared.composition_input
    planned = len(composition_input["chapters"]) + 1
    if planned > max_business_call_count:
        raise ASRDocumentV2Error(
            "DOCUMENT_V2_CALL_BUDGET_EXCEEDED：2.0正文预计业务调用数"
            f"{planned}超过上限{max_business_call_count}。"
        )
    existing = _load_candidate(
        output_root, task_id, composition_input=composition_input
    )
    if existing is not None:
        preview, pointer_path = existing
        return ASRDocumentV2Result(
            source=_preview_source(preview, pointer_path=pointer_path),
            preview=preview,
            document_profile=document_profile,
            review_status="needs_review",
            planned_call_count=planned,
            business_call_count=0,
            supplier_request_count=0,
            cache_hit_count=planned,
            all_llm_cache_hit=True,
            reused_candidate=True,
        )
    if not allow_llm:
        raise ASRDocumentV2Error(
            "LLM_APPROVAL_REQUIRED：尚无可复用的2.0待复核候选；"
            "如允许本次调用项目配置的LLM，请增加--allow-llm。"
        )

    config = load_llm_config()
    transport = OpenAICompatibleTransport()
    candidate_chapters: list[dict[str, Any]] = []
    supplier_requests = 0
    cache_hits = 0
    for chapter_index in range(1, len(composition_input["chapters"]) + 1):
        chapter_id = str(
            composition_input["chapters"][chapter_index - 1]["chapter_id"]
        )
        recorded_parse_failure = _has_recorded_json_parse_failure(
            output_root,
            task_id,
            input_id=prepared.input_id,
            chapter_id=chapter_id,
        )
        try:
            result, _, _, _ = compose_continuous_content_chapter_once(
                output_root,
                task_id,
                composition_input,
                chapter_index=chapter_index,
                config=config,
                transport=transport,
                compact_recovery=recorded_parse_failure,
            )
        except LLMAdapterError as exc:
            if recorded_parse_failure or str(exc) != "LLM返回内容不是合法JSON。":
                raise
            supplier_requests += 1
            result, _, _, _ = compose_continuous_content_chapter_once(
                output_root,
                task_id,
                composition_input,
                chapter_index=chapter_index,
                config=config,
                transport=transport,
                compact_recovery=True,
            )
        payload, _, _ = build_continuous_content_payload(
            composition_input, chapter_index=chapter_index
        )
        candidate_chapters.append(
            {
                "chapter_id": chapter_id,
                "title": payload["title"],
                "paragraphs": _cap_document_paragraphs(
                    result.response["paragraphs"]
                ),
            }
        )
        if result.cache_hit:
            cache_hits += 1
        else:
            supplier_requests += result.attempt_count
    candidate = {"chapters": candidate_chapters}
    chapter_result = build_continuous_reviewed_chapter_result(
        composition_input, candidate
    )
    global_payload = build_continuous_global_payload(
        candidate, document_profile=document_profile
    )
    recovered_global = _load_global_conclusion_recovery(
        output_root,
        task_id,
        input_id=prepared.input_id,
        payload=global_payload,
    )
    if recovered_global is None:
        global_call = compose_continuous_global_once(
            output_root,
            task_id,
            candidate,
            document_profile=document_profile,
            input_id=prepared.input_id,
            config=config,
            transport=transport,
        )
        global_response = global_call.response
        if global_call.cache_hit:
            cache_hits += 1
        else:
            supplier_requests += global_call.attempt_count
    else:
        global_response = recovered_global
        cache_hits += 1
    global_evidence = derive_continuous_global_evidence(
        global_response,
        payload=global_payload,
        chapter_result=chapter_result,
    )
    document = assemble_structured_document_content(
        composition_input,
        chapter_result=chapter_result,
        global_response=global_evidence,
        generation_mode="document-composition-real",
        generation_backend=config.backend,
        generation_model=config.model,
        generation_temperature=config.temperature,
        generation_api_request_count=planned,
    )
    preview = publish_document_preview(
        output_root,
        task_id,
        composition_input=composition_input,
        document=document,
        business_call_count=planned,
        supplier_request_count=planned,
        cache_hit_count=cache_hits,
        api_request_count=planned,
    )
    pointer_path = _candidate_pointer_path(output_root, task_id)
    _write_candidate_pointer(
        pointer_path,
        task_id=task_id,
        composition_input=composition_input,
        preview=preview,
    )
    return ASRDocumentV2Result(
        source=_preview_source(preview, pointer_path=pointer_path),
        preview=preview,
        document_profile=document_profile,
        review_status="needs_review",
        planned_call_count=planned,
        business_call_count=planned - cache_hits,
        supplier_request_count=supplier_requests,
        cache_hit_count=cache_hits,
        all_llm_cache_hit=supplier_requests == 0 and cache_hits == planned,
        reused_candidate=False,
    )
