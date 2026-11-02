"""阶段9-R权威输入加载与DocumentCompositionInput 2.0构造。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from video_doc.asr_timeline import (
    ASRTimelineError,
    load_task_timeline,
)
from video_doc.chapter_content import (
    CHAPTER_INPUT_OWNERSHIP_POLICY,
    ChapterContentError,
    validate_chapter_content_bundle,
    validate_chapter_input_manifest,
)
from video_doc.llm_strategy import ChapterPlanError, validate_chapter_plan
from video_doc.structured_document_content import (
    DOCUMENT_CONTENT_OUTPUT_LANGUAGE,
    DOCUMENT_PROFILE_POLICY_VERSION,
    DOCUMENT_PROFILES,
    DOCUMENT_SOURCE_PATH_VERSION,
    canonical_sha256,
)
from video_doc.task_workspace import TaskWorkspaceError, validate_task_id


DOCUMENT_COMPOSITION_INPUT_SCHEMA_VERSION = "2.0"
DOCUMENT_COMPOSITION_INPUT_BUILDER_VERSION = "document-composition-input-builder-2.0"
DOCUMENT_COMPOSITION_INPUT_ID_PREFIX = "composition-input-"
_EVENT_ID_RE = re.compile(r"^a[0-9]{4,}$")


class DocumentCompositionError(RuntimeError):
    """阶段9-R权威来源或编排输入不符合冻结契约。"""


@dataclass(frozen=True)
class ProtectedFileSnapshot:
    relative_path: str
    sha256: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class AuthoritativeDocumentCompositionSources:
    task_root: Path
    stage8_group_path: Path
    stage9_group_path: Path
    input_manifest_path: Path
    timeline_path: Path
    stage8_plan: dict[str, Any]
    stage8_report: dict[str, Any]
    stage9_bundle: dict[str, Any]
    stage9_report: dict[str, Any]
    input_manifest: dict[str, Any]
    timeline: dict[str, Any]
    protected_files: tuple[ProtectedFileSnapshot, ...]


@dataclass(frozen=True)
class DocumentCompositionInputResult:
    sources: AuthoritativeDocumentCompositionSources
    document_profile: str
    composition_input: dict[str, Any]
    input_id: str


def _fail(message: str) -> None:
    raise DocumentCompositionError(message)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise DocumentCompositionError(f"权威输入文件不存在：{path}") from exc
    except OSError as exc:
        raise DocumentCompositionError(f"无法读取权威输入文件：{path}") from exc
    return digest.hexdigest()


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DocumentCompositionError(f"{description}不存在：{path}") from exc
    except OSError as exc:
        raise DocumentCompositionError(f"无法读取{description}：{path}") from exc
    except UnicodeError as exc:
        raise DocumentCompositionError(f"{description}不是有效UTF-8：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DocumentCompositionError(f"{description}不是有效JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise DocumentCompositionError(f"{description}必须是JSON对象：{path}")
    return payload


def _valid_sha256(value: Any, location: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise DocumentCompositionError(f"{location}必须是64位SHA-256。")
    return value.lower()


def _require_byte_alias(alias: Path, source: Path, description: str) -> None:
    try:
        if alias.read_bytes() != source.read_bytes():
            raise DocumentCompositionError(f"{description}与正式结果组不一致。")
    except FileNotFoundError as exc:
        raise DocumentCompositionError(f"{description}不存在：{alias}") from exc
    except OSError as exc:
        raise DocumentCompositionError(f"无法读取{description}：{alias}") from exc


def _validate_group_manifest(
    manifest: dict[str, Any],
    *,
    task_id: str,
    result_id: str,
    expected_files: set[str],
    location: str,
) -> dict[str, str]:
    files = manifest.get("files")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("task_id") != task_id
        or manifest.get("result_id") != result_id
        or not isinstance(files, dict)
        or set(files) != expected_files
    ):
        raise DocumentCompositionError(f"{location}结构、任务或结果身份无效。")
    return {
        name: _valid_sha256(files[name], f"{location}.files[{name}]")
        for name in sorted(expected_files)
    }


def _snapshot_paths(
    task_root: Path, paths: list[Path]
) -> tuple[ProtectedFileSnapshot, ...]:
    snapshots: list[ProtectedFileSnapshot] = []
    seen: set[Path] = set()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            relative = path.relative_to(task_root).as_posix()
            stat = path.stat()
        except ValueError as exc:
            raise DocumentCompositionError("保护文件必须位于当前任务目录内。") from exc
        except OSError as exc:
            raise DocumentCompositionError(f"无法读取保护文件状态：{path}") from exc
        snapshots.append(
            ProtectedFileSnapshot(
                relative_path=relative,
                sha256=_file_sha256(path),
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return tuple(snapshots)


def assert_protected_files_unchanged(
    task_root: str | Path,
    expected: tuple[ProtectedFileSnapshot, ...],
) -> None:
    """复验R2读取链文件未被改写、替换或删除。"""

    root = Path(task_root)
    paths = [root / snapshot.relative_path for snapshot in expected]
    current = _snapshot_paths(root, paths)
    if current != expected:
        raise DocumentCompositionError("阶段8/9权威输入保护清单发生变化。")


def load_authoritative_document_composition_sources(
    output_root: str | Path,
    task_id: str,
) -> AuthoritativeDocumentCompositionSources:
    """只读复验阶段8、阶段9 v1、输入manifest和timeline完整权威链。"""

    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise DocumentCompositionError(str(exc)) from exc
    task_root = Path(output_root).expanduser() / task_id
    boundaries_root = task_root / "boundaries"
    content_root = task_root / "content"
    timeline_path = task_root / "timeline" / "timeline.json"

    stage9_top_content_path = content_root / "content.json"
    stage9_top_report_path = content_root / "content-execution-report.json"
    stage9_top_bundle = _read_json(stage9_top_content_path, "阶段9 v1顶层内容")
    stage9_top_report = _read_json(stage9_top_report_path, "阶段9 v1顶层执行报告")
    stage9_result_id = stage9_top_report.get("result_id")
    if (
        stage9_top_report.get("schema_version") != "1.0"
        or stage9_top_report.get("task_id") != task_id
        or stage9_top_report.get("status") != "success"
        or not isinstance(stage9_result_id, str)
        or not stage9_result_id.startswith("content-")
    ):
        raise DocumentCompositionError("阶段9 v1顶层执行报告身份或状态无效。")

    stage9_group = content_root / "formal-results" / stage9_result_id
    stage9_group_content_path = stage9_group / "content.json"
    stage9_group_report_path = stage9_group / "execution-report.json"
    stage9_group_manifest_path = stage9_group / "manifest.json"
    stage9_bundle = _read_json(stage9_group_content_path, "阶段9 v1正式内容")
    stage9_report = _read_json(stage9_group_report_path, "阶段9 v1正式执行报告")
    stage9_manifest = _read_json(stage9_group_manifest_path, "阶段9 v1正式manifest")
    stage9_files = _validate_group_manifest(
        stage9_manifest,
        task_id=task_id,
        result_id=stage9_result_id,
        expected_files={"content.json", "execution-report.json"},
        location="阶段9 v1正式manifest",
    )
    if _file_sha256(stage9_group_content_path) != stage9_files["content.json"]:
        raise DocumentCompositionError("阶段9 v1正式content.json文件哈希不一致。")
    if _file_sha256(stage9_group_report_path) != stage9_files["execution-report.json"]:
        raise DocumentCompositionError("阶段9 v1正式execution-report.json文件哈希不一致。")
    _require_byte_alias(stage9_top_content_path, stage9_group_content_path, "阶段9 v1顶层内容")
    _require_byte_alias(stage9_top_report_path, stage9_group_report_path, "阶段9 v1顶层执行报告")
    if stage9_top_bundle != stage9_bundle or stage9_top_report != stage9_report:
        raise DocumentCompositionError("阶段9 v1顶层JSON与正式结果组不一致。")
    stage9_canonical_hash = canonical_sha256(stage9_bundle)
    if (
        stage9_report.get("content_sha256") != stage9_canonical_hash
        or stage9_result_id != f"content-{stage9_canonical_hash[:12]}"
        or stage9_report.get("chapter_count") != len(stage9_bundle.get("chapters", []))
    ):
        raise DocumentCompositionError("阶段9 v1规范哈希、结果ID或章节数不一致。")

    source_plan = stage9_bundle.get("source_chapter_plan")
    source_timeline = stage9_bundle.get("source_timeline")
    if not isinstance(source_plan, dict) or not isinstance(source_timeline, dict):
        raise DocumentCompositionError("阶段9 v1缺少分章或时间轴来源。")
    stage8_result_id = source_plan.get("result_id")
    timeline_canonical_hash = _valid_sha256(
        source_timeline.get("sha256"), "阶段9 v1时间轴规范哈希"
    )
    if (
        not isinstance(stage8_result_id, str)
        or not stage8_result_id
        or stage9_report.get("source_stage8_result_id") != stage8_result_id
    ):
        raise DocumentCompositionError("阶段9 v1执行报告与分章来源不一致。")
    input_manifest_path = (
        content_root
        / "attempts"
        / f"input-{stage8_result_id}-{timeline_canonical_hash[:12]}"
        / "chapter-input-manifest.json"
    )
    input_manifest = _read_json(input_manifest_path, "阶段9章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
        validate_chapter_input_manifest(input_manifest, timeline=timeline)
        validate_chapter_content_bundle(stage9_bundle, input_manifest=input_manifest)
    except (ASRTimelineError, ChapterContentError) as exc:
        raise DocumentCompositionError(f"阶段9 v1 Schema或输入来源复验失败：{exc}") from exc
    if stage9_report.get("input_manifest_cache_key") != input_manifest.get("cache_key"):
        raise DocumentCompositionError("阶段9 v1执行报告与输入manifest缓存身份不一致。")
    if canonical_sha256(timeline) != timeline_canonical_hash:
        raise DocumentCompositionError("当前timeline与阶段9 v1来源规范哈希不一致。")

    stage8_top_plan_path = boundaries_root / "llm.json"
    stage8_top_report_path = boundaries_root / "llm-execution-report.json"
    stage8_top_plan = _read_json(stage8_top_plan_path, "阶段8顶层ChapterPlan")
    stage8_top_report = _read_json(stage8_top_report_path, "阶段8顶层执行报告")
    stage8_group = boundaries_root / "formal-results" / stage8_result_id
    stage8_group_plan_path = stage8_group / "chapter-plan.json"
    stage8_group_report_path = stage8_group / "execution-report.json"
    stage8_group_manifest_path = stage8_group / "manifest.json"
    stage8_plan = _read_json(stage8_group_plan_path, "阶段8正式ChapterPlan")
    stage8_report = _read_json(stage8_group_report_path, "阶段8正式执行报告")
    stage8_manifest = _read_json(stage8_group_manifest_path, "阶段8正式manifest")
    stage8_files = _validate_group_manifest(
        stage8_manifest,
        task_id=task_id,
        result_id=stage8_result_id,
        expected_files={"chapter-plan.json", "execution-report.json"},
        location="阶段8正式manifest",
    )
    if _file_sha256(stage8_group_plan_path) != stage8_files["chapter-plan.json"]:
        raise DocumentCompositionError("阶段8正式chapter-plan.json文件哈希不一致。")
    if _file_sha256(stage8_group_report_path) != stage8_files["execution-report.json"]:
        raise DocumentCompositionError("阶段8正式execution-report.json文件哈希不一致。")
    _require_byte_alias(stage8_top_plan_path, stage8_group_plan_path, "阶段8顶层ChapterPlan")
    _require_byte_alias(stage8_top_report_path, stage8_group_report_path, "阶段8顶层执行报告")
    if stage8_top_plan != stage8_plan or stage8_top_report != stage8_report:
        raise DocumentCompositionError("阶段8顶层JSON与正式结果组不一致。")
    try:
        validate_chapter_plan(stage8_plan, timeline=timeline, expected_task_id=task_id)
    except ChapterPlanError as exc:
        raise DocumentCompositionError(f"阶段8正式ChapterPlan复验失败：{exc}") from exc
    stage8_canonical_hash = canonical_sha256(stage8_plan)
    provenance = stage9_bundle.get("chaptering_provenance")
    if not isinstance(provenance, dict):
        raise DocumentCompositionError("阶段9 v1缺少分章来源状态。")
    expected_stage8_id = f"{provenance.get('result_source')}-{stage8_canonical_hash[:12]}"
    if (
        stage8_result_id != expected_stage8_id
        or source_plan.get("sha256") != stage8_canonical_hash
        or stage8_report.get("final_plan_sha256") != stage8_canonical_hash
        or stage8_report.get("result_id") != stage8_result_id
        or stage8_report.get("status") not in {"success", "degraded"}
        or stage8_report.get("result_source") != provenance.get("result_source")
        or stage8_report.get("quality_level") != provenance.get("quality_level")
        or canonical_sha256(stage8_report) != provenance.get("execution_report_sha256")
    ):
        raise DocumentCompositionError("阶段8正式组、阶段9 v1来源和provenance不一致。")

    protected_paths = [
        stage8_top_plan_path,
        stage8_top_report_path,
        stage8_group_plan_path,
        stage8_group_report_path,
        stage8_group_manifest_path,
        timeline_path,
        input_manifest_path,
        stage9_top_content_path,
        stage9_top_report_path,
        stage9_group_content_path,
        stage9_group_report_path,
        stage9_group_manifest_path,
    ]
    protected_files = _snapshot_paths(task_root, protected_paths)
    return AuthoritativeDocumentCompositionSources(
        task_root=task_root,
        stage8_group_path=stage8_group,
        stage9_group_path=stage9_group,
        input_manifest_path=input_manifest_path,
        timeline_path=timeline_path,
        stage8_plan=stage8_plan,
        stage8_report=stage8_report,
        stage9_bundle=stage9_bundle,
        stage9_report=stage9_report,
        input_manifest=input_manifest,
        timeline=timeline,
        protected_files=protected_files,
    )


def _field_payload(
    *,
    path: str,
    kind: str,
    item_index: int | None,
    evidence: Mapping[str, Any],
    coverage_required: bool,
) -> dict[str, Any]:
    return {
        "source_content_path": path,
        "kind": kind,
        "item_index": item_index,
        "text": evidence["text"],
        "source_event_ids": list(evidence["source_event_ids"]),
        "coverage_required": coverage_required,
    }


def _chapter_source_fields(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    chapter_id = chapter["chapter_id"]
    fields = [
        _field_payload(
            path=f"chapters[{chapter_id}].title",
            kind="title",
            item_index=None,
            evidence=chapter["title"],
            coverage_required=True,
        ),
        _field_payload(
            path=f"chapters[{chapter_id}].summary",
            kind="summary",
            item_index=None,
            evidence=chapter["summary"],
            coverage_required=True,
        ),
    ]
    for index, evidence in enumerate(chapter["key_points"]):
        fields.append(
            _field_payload(
                path=f"chapters[{chapter_id}].key_points[{index}]",
                kind="key_point",
                item_index=index,
                evidence=evidence,
                coverage_required=True,
            )
        )
    for index, evidence in enumerate(chapter["keywords"]):
        fields.append(
            _field_payload(
                path=f"chapters[{chapter_id}].keywords[{index}]",
                kind="keyword",
                item_index=index,
                evidence=evidence,
                coverage_required=False,
            )
        )
    return fields


def _input_versions() -> dict[str, str]:
    return {
        "input_schema": DOCUMENT_COMPOSITION_INPUT_SCHEMA_VERSION,
        "input_builder": DOCUMENT_COMPOSITION_INPUT_BUILDER_VERSION,
        "profile_policy": DOCUMENT_PROFILE_POLICY_VERSION,
        "source_path": DOCUMENT_SOURCE_PATH_VERSION,
        "ownership_policy": CHAPTER_INPUT_OWNERSHIP_POLICY,
    }


def document_composition_input_identity_sha256(payload: Mapping[str, Any]) -> str:
    identity = deepcopy(dict(payload))
    identity.pop("input_id", None)
    return canonical_sha256(identity)


def _attach_input_id(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(payload))
    result.pop("input_id", None)
    result["input_id"] = (
        f"{DOCUMENT_COMPOSITION_INPUT_ID_PREFIX}"
        f"{document_composition_input_identity_sha256(result)[:12]}"
    )
    return result


def _build_composition_input(
    sources: AuthoritativeDocumentCompositionSources,
    *,
    document_profile: str,
) -> dict[str, Any]:
    if document_profile not in DOCUMENT_PROFILES:
        raise DocumentCompositionError("document_profile不属于冻结四种profile。")
    timeline_by_id = {event["id"]: event for event in sources.timeline["events"]}
    manifest_chapters = {
        chapter["chapter_id"]: chapter for chapter in sources.input_manifest["chapters"]
    }
    chapters: list[dict[str, Any]] = []
    for chapter in sources.stage9_bundle["chapters"]:
        chapter_id = chapter["chapter_id"]
        manifest_chapter = manifest_chapters[chapter_id]
        events = [
            {
                "id": timeline_by_id[event_id]["id"],
                "start": timeline_by_id[event_id]["start"],
                "end": timeline_by_id[event_id]["end"],
                "text": timeline_by_id[event_id]["text"],
            }
            for event_id in manifest_chapter["event_ids"]
        ]
        chapter_payload: dict[str, Any] = {
            "chapter_id": chapter_id,
            "index": chapter["index"],
            "start": chapter["start"],
            "end": chapter["end"],
            "planned_title": chapter["planned_title"],
            "window_count": chapter["window_count"],
            "source_fields": _chapter_source_fields(chapter),
            "events": events,
            "event_count": len(events),
        }
        chapter_payload["chapter_input_sha256"] = canonical_sha256(chapter_payload)
        chapters.append(chapter_payload)

    stage8_group = sources.stage8_group_path
    stage9_group = sources.stage9_group_path
    payload = {
        "schema_version": DOCUMENT_COMPOSITION_INPUT_SCHEMA_VERSION,
        "task_id": sources.stage9_bundle["task_id"],
        "language": DOCUMENT_CONTENT_OUTPUT_LANGUAGE,
        "strategy": sources.stage9_bundle["strategy"],
        "document_profile": document_profile,
        "source_content_bundle": {
            "schema_version": sources.stage9_bundle["schema_version"],
            "result_id": sources.stage9_report["result_id"],
            "canonical_sha256": sources.stage9_report["content_sha256"],
            "content_file_sha256": _file_sha256(stage9_group / "content.json"),
            "execution_report_file_sha256": _file_sha256(
                stage9_group / "execution-report.json"
            ),
            "manifest_file_sha256": _file_sha256(stage9_group / "manifest.json"),
            "input_manifest_file_sha256": _file_sha256(sources.input_manifest_path),
        },
        "source_chapter_plan": {
            "schema_version": sources.stage8_plan["schema_version"],
            "result_id": sources.stage8_report["result_id"],
            "canonical_sha256": sources.stage8_report["final_plan_sha256"],
            "chapter_plan_file_sha256": _file_sha256(stage8_group / "chapter-plan.json"),
            "execution_report_file_sha256": _file_sha256(
                stage8_group / "execution-report.json"
            ),
            "manifest_file_sha256": _file_sha256(stage8_group / "manifest.json"),
        },
        "source_timeline": {
            "schema_version": sources.timeline["schema_version"],
            "canonical_sha256": canonical_sha256(sources.timeline),
            "timeline_file_sha256": _file_sha256(sources.timeline_path),
            "event_count": len(sources.timeline["events"]),
        },
        "chaptering_provenance": deepcopy(
            sources.stage9_bundle["chaptering_provenance"]
        ),
        "source_generation": deepcopy(sources.stage9_bundle["generation"]),
        "ownership": deepcopy(sources.input_manifest["ownership"]),
        "chapters": chapters,
        "input_versions": _input_versions(),
    }
    return _attach_input_id(payload)


def _require_exact_keys(
    payload: Mapping[str, Any], expected: set[str], location: str
) -> None:
    if set(payload) != expected:
        raise DocumentCompositionError(f"{location}字段集合不符合Schema。")


def _validate_source_identity(payload: Mapping[str, Any]) -> None:
    source_content = payload["source_content_bundle"]
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
    if (
        source_content["schema_version"] != "1.0"
        or not isinstance(source_content["result_id"], str)
        or not source_content["result_id"].startswith("content-")
    ):
        raise DocumentCompositionError("source_content_bundle身份无效。")
    for field in (
        "canonical_sha256",
        "content_file_sha256",
        "execution_report_file_sha256",
        "manifest_file_sha256",
        "input_manifest_file_sha256",
    ):
        _valid_sha256(source_content[field], f"source_content_bundle.{field}")
    if source_content["result_id"] != (
        f"content-{source_content['canonical_sha256'][:12]}"
    ):
        raise DocumentCompositionError("source_content_bundle结果ID与规范哈希不一致。")

    source_plan = payload["source_chapter_plan"]
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
    if (
        source_plan["schema_version"] != "1.0"
        or not isinstance(source_plan["result_id"], str)
        or not source_plan["result_id"]
    ):
        raise DocumentCompositionError("source_chapter_plan身份无效。")
    for field in (
        "canonical_sha256",
        "chapter_plan_file_sha256",
        "execution_report_file_sha256",
        "manifest_file_sha256",
    ):
        _valid_sha256(source_plan[field], f"source_chapter_plan.{field}")

    source_timeline = payload["source_timeline"]
    _require_exact_keys(
        source_timeline,
        {"schema_version", "canonical_sha256", "timeline_file_sha256", "event_count"},
        "source_timeline",
    )
    if source_timeline["schema_version"] != "1.0":
        raise DocumentCompositionError("source_timeline.schema_version无效。")
    _valid_sha256(source_timeline["canonical_sha256"], "source_timeline.canonical_sha256")
    _valid_sha256(source_timeline["timeline_file_sha256"], "source_timeline.timeline_file_sha256")
    if (
        isinstance(source_timeline["event_count"], bool)
        or not isinstance(source_timeline["event_count"], int)
        or source_timeline["event_count"] < 1
    ):
        raise DocumentCompositionError("source_timeline.event_count无效。")

    provenance = payload["chaptering_provenance"]
    _require_exact_keys(
        provenance,
        {"status", "result_source", "quality_level", "execution_report_sha256"},
        "chaptering_provenance",
    )
    if (
        provenance["status"],
        provenance["result_source"],
        provenance["quality_level"],
    ) not in {
        ("success", "primary", "normal"),
        ("success", "fallback", "unknown"),
        ("degraded", "emergency", "degraded"),
    }:
        raise DocumentCompositionError("chaptering_provenance状态组合无效。")
    _valid_sha256(
        provenance["execution_report_sha256"],
        "chaptering_provenance.execution_report_sha256",
    )
    if source_plan["result_id"] != (
        f"{provenance['result_source']}-{source_plan['canonical_sha256'][:12]}"
    ):
        raise DocumentCompositionError("source_chapter_plan结果ID与provenance不一致。")

    generation = payload["source_generation"]
    if generation.get("status") != "success":
        raise DocumentCompositionError("source_generation.status必须为success。")
    for field in (
        "prompt_version",
        "merge_prompt_version",
        "content_policy_version",
        "windowing_version",
        "validator_version",
    ):
        if not isinstance(generation.get(field), str) or not generation[field].strip():
            raise DocumentCompositionError(f"source_generation.{field}无效。")
    if generation.get("output_language") != DOCUMENT_CONTENT_OUTPUT_LANGUAGE:
        raise DocumentCompositionError("source_generation.output_language必须为zh-CN。")


def validate_document_composition_input(payload: Any) -> None:
    """纯内存校验DocumentCompositionInput 2.0。"""

    if not isinstance(payload, dict):
        raise DocumentCompositionError("DocumentCompositionInput必须是对象。")
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "input_id",
            "task_id",
            "language",
            "strategy",
            "document_profile",
            "source_content_bundle",
            "source_chapter_plan",
            "source_timeline",
            "chaptering_provenance",
            "source_generation",
            "ownership",
            "chapters",
            "input_versions",
        },
        "DocumentCompositionInput",
    )
    if payload["schema_version"] != DOCUMENT_COMPOSITION_INPUT_SCHEMA_VERSION:
        raise DocumentCompositionError("DocumentCompositionInput Schema版本无效。")
    if not isinstance(payload["input_id"], str) or payload["input_id"] != (
        f"{DOCUMENT_COMPOSITION_INPUT_ID_PREFIX}"
        f"{document_composition_input_identity_sha256(payload)[:12]}"
    ):
        raise DocumentCompositionError("input_id与规范输入身份不一致。")
    if (
        not isinstance(payload["task_id"], str)
        or not payload["task_id"]
        or payload["language"] != DOCUMENT_CONTENT_OUTPUT_LANGUAGE
        or payload["strategy"] not in {"llm", "rule", "ppo"}
        or payload["document_profile"] not in DOCUMENT_PROFILES
    ):
        raise DocumentCompositionError("任务、语言、策略或profile无效。")
    if payload["input_versions"] != _input_versions():
        raise DocumentCompositionError("input_versions与当前冻结版本不一致。")
    for field in (
        "source_content_bundle",
        "source_chapter_plan",
        "source_timeline",
        "chaptering_provenance",
        "source_generation",
        "ownership",
    ):
        if not isinstance(payload[field], dict):
            raise DocumentCompositionError(f"{field}必须是对象。")
    _validate_source_identity(payload)
    if payload["source_timeline"].get("event_count") != payload["ownership"].get(
        "assigned_event_count"
    ):
        raise DocumentCompositionError("时间轴事件数与ownership不一致。")
    if payload["ownership"] != {
        "policy_version": CHAPTER_INPUT_OWNERSHIP_POLICY,
        "rule": "chapter.start <= event.start < chapter.end",
        "assigned_event_count": payload["source_timeline"]["event_count"],
        "duplicate_event_count": 0,
        "unassigned_event_count": 0,
    }:
        raise DocumentCompositionError("ownership不符合唯一归属契约。")
    chapters = payload["chapters"]
    if not isinstance(chapters, list) or not chapters:
        raise DocumentCompositionError("chapters必须是非空数组。")
    all_event_ids: list[str] = []
    all_paths: set[str] = set()
    previous_end: float | None = None
    previous_global_event_start = -1.0
    for expected_index, chapter in enumerate(chapters, start=1):
        location = f"chapters[{expected_index - 1}]"
        if not isinstance(chapter, dict):
            raise DocumentCompositionError(f"{location}必须是对象。")
        _require_exact_keys(
            chapter,
            {
                "chapter_id",
                "index",
                "start",
                "end",
                "planned_title",
                "window_count",
                "source_fields",
                "events",
                "event_count",
                "chapter_input_sha256",
            },
            location,
        )
        chapter_id = f"c{expected_index:04d}"
        if chapter["chapter_id"] != chapter_id or chapter["index"] != expected_index:
            raise DocumentCompositionError(f"{location}ID或序号不连续。")
        start = chapter["start"]
        end = chapter["end"]
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or start < 0
            or end <= start
            or (expected_index == 1 and start != 0)
            or (previous_end is not None and start != previous_end)
        ):
            raise DocumentCompositionError(f"{location}时间范围无效或不连续。")
        previous_end = float(end)
        if not isinstance(chapter["planned_title"], str) or not chapter[
            "planned_title"
        ].strip():
            raise DocumentCompositionError(f"{location}.planned_title无效。")
        if isinstance(chapter["window_count"], bool) or not isinstance(
            chapter["window_count"], int
        ) or chapter["window_count"] < 1:
            raise DocumentCompositionError(f"{location}.window_count无效。")
        events = chapter["events"]
        if not isinstance(events, list) or not events or chapter["event_count"] != len(events):
            raise DocumentCompositionError(f"{location}.events无效。")
        event_ids: list[str] = []
        previous_event_start = -1.0
        for event_index, event in enumerate(events):
            if not isinstance(event, dict) or set(event) != {"id", "start", "end", "text"}:
                raise DocumentCompositionError(f"{location}.events[{event_index}]结构无效。")
            event_id = event["id"]
            event_start = event["start"]
            event_end = event["end"]
            if (
                not isinstance(event_id, str)
                or not event_id
                or isinstance(event_start, bool)
                or isinstance(event_end, bool)
                or not isinstance(event_start, (int, float))
                or not isinstance(event_end, (int, float))
                or not math.isfinite(float(event_start))
                or not math.isfinite(float(event_end))
                or not _EVENT_ID_RE.fullmatch(event_id)
                or not start <= event_start < end
                or event_end <= event_start
                or event_start < previous_event_start
                or event_start < previous_global_event_start
                or not isinstance(event["text"], str)
                or not event["text"].strip()
            ):
                raise DocumentCompositionError(f"{location}.events[{event_index}]无效。")
            previous_event_start = float(event_start)
            previous_global_event_start = float(event_start)
            event_ids.append(event_id)
        if len(event_ids) != len(set(event_ids)):
            raise DocumentCompositionError(f"{location}事件ID重复。")
        source_fields = chapter["source_fields"]
        if not isinstance(source_fields, list) or len(source_fields) < 2:
            raise DocumentCompositionError(f"{location}.source_fields无效。")
        kinds = [field.get("kind") for field in source_fields if isinstance(field, dict)]
        if len(kinds) != len(source_fields) or kinds[:2] != ["title", "summary"]:
            raise DocumentCompositionError(f"{location}.source_fields顺序无效。")
        seen_keyword = False
        next_key_point_index = 0
        next_keyword_index = 0
        for field_index, field in enumerate(source_fields):
            field_location = f"{location}.source_fields[{field_index}]"
            if not isinstance(field, dict):
                raise DocumentCompositionError(f"{field_location}必须是对象。")
            _require_exact_keys(
                field,
                {
                    "source_content_path",
                    "kind",
                    "item_index",
                    "text",
                    "source_event_ids",
                    "coverage_required",
                },
                field_location,
            )
            kind = field["kind"]
            if kind not in {"title", "summary", "key_point", "keyword"}:
                raise DocumentCompositionError(f"{field_location}.kind无效。")
            if seen_keyword and kind != "keyword":
                raise DocumentCompositionError(f"{location}关键词后不得出现核心字段。")
            seen_keyword = seen_keyword or kind == "keyword"
            item_index = field["item_index"]
            if kind in {"title", "summary"}:
                expected_path = f"chapters[{chapter_id}].{kind}"
                if item_index is not None:
                    raise DocumentCompositionError(f"{field_location}.item_index无效。")
            else:
                if isinstance(item_index, bool) or not isinstance(item_index, int) or item_index < 0:
                    raise DocumentCompositionError(f"{field_location}.item_index无效。")
                plural = "key_points" if kind == "key_point" else "keywords"
                expected_path = f"chapters[{chapter_id}].{plural}[{item_index}]"
                expected_item_index = (
                    next_key_point_index if kind == "key_point" else next_keyword_index
                )
                if item_index != expected_item_index:
                    raise DocumentCompositionError(
                        f"{field_location}.item_index必须从0连续递增。"
                    )
                if kind == "key_point":
                    next_key_point_index += 1
                else:
                    next_keyword_index += 1
            if field["source_content_path"] != expected_path or expected_path in all_paths:
                raise DocumentCompositionError(f"{field_location}路径无效或重复。")
            all_paths.add(expected_path)
            if not isinstance(field["text"], str) or not field["text"].strip():
                raise DocumentCompositionError(f"{field_location}.text无效。")
            refs = field["source_event_ids"]
            if (
                not isinstance(refs, list)
                or not refs
                or len(refs) != len(set(refs))
                or any(ref not in event_ids for ref in refs)
                or refs != [event_id for event_id in event_ids if event_id in set(refs)]
            ):
                raise DocumentCompositionError(f"{field_location}证据ID无效或乱序。")
            if field["coverage_required"] is not (kind != "keyword"):
                raise DocumentCompositionError(f"{field_location}.coverage_required无效。")
        if next_key_point_index < 1:
            raise DocumentCompositionError(f"{location}至少需要一个v1 key_point来源。")
        without_hash = dict(chapter)
        recorded_hash = without_hash.pop("chapter_input_sha256")
        if recorded_hash != canonical_sha256(without_hash):
            raise DocumentCompositionError(f"{location}.chapter_input_sha256不一致。")
        all_event_ids.extend(event_ids)
    if (
        len(all_event_ids) != len(set(all_event_ids))
        or len(all_event_ids) != payload["source_timeline"]["event_count"]
    ):
        raise DocumentCompositionError("章节事件存在重复、遗漏或总数不一致。")


def prepare_document_composition_input(
    output_root: str | Path,
    task_id: str,
    *,
    document_profile: str,
) -> DocumentCompositionInputResult:
    """只读加载并在内存构造确定性编排输入，不创建任何产物。"""

    if document_profile not in DOCUMENT_PROFILES:
        raise DocumentCompositionError("document_profile不属于冻结四种profile。")
    sources = load_authoritative_document_composition_sources(output_root, task_id)
    payload = _build_composition_input(sources, document_profile=document_profile)
    validate_document_composition_input(payload)
    assert_protected_files_unchanged(sources.task_root, sources.protected_files)
    return DocumentCompositionInputResult(
        sources=sources,
        document_profile=document_profile,
        composition_input=payload,
        input_id=payload["input_id"],
    )
