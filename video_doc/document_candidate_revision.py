"""对冻结的Document 2.0候选执行可审计的离线文本修订。"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from video_doc.asr_document_v2 import (
    ASRDocumentV2Error,
    _candidate_pointer_path,
    _load_candidate,
    _preview_source,
    _write_candidate_pointer,
)
from video_doc.document_composition import prepare_document_composition_input
from video_doc.document_content_preview import publish_document_preview
from video_doc.document_export_v2 import AuthoritativeDocumentContentV2
from video_doc.structured_document_content import (
    attach_structured_document_content_id,
    canonical_sha256,
)


REVISION_SPEC_SCHEMA_VERSION = "1.0"
REVISION_REPORT_SCHEMA_VERSION = "1.0"
REVISION_ENGINE_VERSION = "document-candidate-offline-revision-1.0"


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )


def _atomic_write(path: Path, payload: bytes) -> bool:
    if path.is_file() and path.read_bytes() == payload:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        with temporary.open("xb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return False


def _require_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ASRDocumentV2Error(f"离线修订规范{location}必须是非空文本。")
    return value.strip()


def _validate_spec(payload: Any, *, task_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "revision_id",
        "task_id",
        "document_profile",
        "source_content_id",
        "reason",
        "replacements",
    }:
        raise ASRDocumentV2Error("离线修订规范字段集合无效。")
    if payload["schema_version"] != REVISION_SPEC_SCHEMA_VERSION:
        raise ASRDocumentV2Error("离线修订规范版本无效。")
    if payload["task_id"] != task_id:
        raise ASRDocumentV2Error("离线修订规范task_id与目标任务不一致。")
    for field in ("revision_id", "document_profile", "source_content_id", "reason"):
        _require_text(payload[field], f".{field}")
    replacements = payload["replacements"]
    if not isinstance(replacements, list) or not replacements:
        raise ASRDocumentV2Error("离线修订规范replacements必须是非空数组。")
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(replacements):
        if not isinstance(item, dict) or set(item) != {"scope", "target", "text"}:
            raise ASRDocumentV2Error(f"离线修订规范replacements[{index}]字段无效。")
        scope = item["scope"]
        if scope not in {
            "overview",
            "chapter_block",
            "conclusion",
            "chapter_title",
            "chapter_keyword",
        }:
            raise ASRDocumentV2Error(f"离线修订规范replacements[{index}].scope无效。")
        target = _require_text(item["target"], f".replacements[{index}].target")
        text = _require_text(item["text"], f".replacements[{index}].text")
        identity = (scope, target)
        if identity in seen:
            raise ASRDocumentV2Error("离线修订规范存在重复目标。")
        seen.add(identity)
        normalized.append({"scope": scope, "target": target, "text": text})
    result = deepcopy(payload)
    result["replacements"] = normalized
    return result


def _replace_text(document: dict[str, Any], replacement: Mapping[str, str]) -> str:
    scope = replacement["scope"]
    target = replacement["target"]
    matches: list[dict[str, Any]] = []
    if scope == "overview":
        if not target.isdigit():
            raise ASRDocumentV2Error("overview修订目标必须是十进制下标。")
        index = int(target)
        if 0 <= index < len(document["overview"]):
            matches.append(document["overview"][index])
    elif scope == "conclusion":
        matches = [
            block
            for block in document["overall_conclusion"]
            if block.get("block_id") == target
        ]
    elif scope == "chapter_block":
        matches = [
            block
            for chapter in document["chapters"]
            for section in chapter["sections"]
            for block in section["blocks"]
            if block.get("block_id") == target
        ]
    elif scope == "chapter_title":
        matches = [
            chapter["title"]
            for chapter in document["chapters"]
            if chapter.get("chapter_id") == target
        ]
    else:
        try:
            chapter_id, raw_index = target.split(":", maxsplit=1)
            keyword_index = int(raw_index)
        except (ValueError, TypeError) as exc:
            raise ASRDocumentV2Error(
                "chapter_keyword修订目标必须采用chapter_id:index格式。"
            ) from exc
        chapters = [
            chapter
            for chapter in document["chapters"]
            if chapter.get("chapter_id") == chapter_id
        ]
        if len(chapters) == 1 and 0 <= keyword_index < len(chapters[0]["keywords"]):
            matches.append(chapters[0]["keywords"][keyword_index])
    if len(matches) != 1 or not isinstance(matches[0].get("text"), str):
        raise ASRDocumentV2Error(f"离线修订目标{scope}:{target}无法唯一定位到文本。")
    if scope in {"overview", "chapter_block", "conclusion"} and matches[0].get(
        "type"
    ) != "paragraph":
        raise ASRDocumentV2Error(f"离线修订目标{scope}:{target}必须定位到段落。")
    matches[0]["text"] = replacement["text"]
    return f"{scope}:{target}"


def revise_task_document_v2_candidate(
    output_root: str | Path,
    task_id: str,
    *,
    revision_spec: Mapping[str, Any],
) -> tuple[AuthoritativeDocumentContentV2, Path, bool]:
    """以0 API发布新候选并切换candidate-current；旧结果组保持不变。"""

    spec = _validate_spec(revision_spec, task_id=task_id)
    prepared = prepare_document_composition_input(
        output_root, task_id, document_profile=spec["document_profile"]
    )
    loaded = _load_candidate(
        output_root, task_id, composition_input=prepared.composition_input
    )
    if loaded is None:
        raise ASRDocumentV2Error("当前不存在可离线修订的Document 2.0候选。")
    source_preview, pointer_path = loaded
    report_path = (
        pointer_path.parent
        / "revisions"
        / spec["revision_id"]
        / "revision-report.json"
    )
    if source_preview.document["content_id"] != spec["source_content_id"]:
        try:
            prior_report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            prior_report = None
        if (
            isinstance(prior_report, dict)
            and prior_report.get("schema_version") == REVISION_REPORT_SCHEMA_VERSION
            and prior_report.get("status") == "success"
            and prior_report.get("task_id") == task_id
            and prior_report.get("revision_id") == spec["revision_id"]
            and prior_report.get("source_content_id") == spec["source_content_id"]
            and prior_report.get("revised_content_id")
            == source_preview.document["content_id"]
            and prior_report.get("revised_document_sha256")
            == canonical_sha256(source_preview.document)
            and prior_report.get("revision_spec_sha256") == canonical_sha256(spec)
            and prior_report.get("api_request_count") == 0
        ):
            return _preview_source(
                source_preview, pointer_path=pointer_path
            ), report_path, True
        raise ASRDocumentV2Error("当前候选与离线修订规范锁定的source_content_id不一致。")

    document = deepcopy(source_preview.document)
    changed_paths = [_replace_text(document, item) for item in spec["replacements"]]
    document["generation"] = {
        **document["generation"],
        "mode": "document-composition-offline-publish",
        "api_request_count": 0,
    }
    document = attach_structured_document_content_id(document)
    if document["content_id"] == source_preview.document["content_id"]:
        raise ASRDocumentV2Error("离线修订没有产生新的内容身份。")
    preview = publish_document_preview(
        output_root,
        task_id,
        composition_input=prepared.composition_input,
        document=document,
        business_call_count=0,
        supplier_request_count=0,
        cache_hit_count=0,
        api_request_count=0,
    )

    report = {
        "schema_version": REVISION_REPORT_SCHEMA_VERSION,
        "status": "success",
        "revision_id": spec["revision_id"],
        "task_id": task_id,
        "reason": spec["reason"],
        "source_content_id": source_preview.document["content_id"],
        "source_document_sha256": canonical_sha256(source_preview.document),
        "revised_content_id": preview.document["content_id"],
        "revised_document_sha256": canonical_sha256(preview.document),
        "changed_paths": changed_paths,
        "api_request_count": 0,
        "revision_engine_version": REVISION_ENGINE_VERSION,
        "revision_spec_sha256": canonical_sha256(spec),
    }
    report_cache_hit = _atomic_write(report_path, _json_bytes(report))
    _write_candidate_pointer(
        pointer_path,
        task_id=task_id,
        composition_input=prepared.composition_input,
        preview=preview,
    )
    return _preview_source(preview, pointer_path=pointer_path), report_path, report_cache_hit


def load_revision_spec(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ASRDocumentV2Error("离线修订规范无法读取。") from exc
    if not isinstance(payload, dict):
        raise ASRDocumentV2Error("离线修订规范顶层必须是对象。")
    return payload
