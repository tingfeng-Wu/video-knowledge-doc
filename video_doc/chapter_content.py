"""阶段9章节内容契约与逐章ASR输入准备。"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from video_doc.asr_timeline import ASRTimelineError, load_task_timeline
from video_doc.llm_adapter import (
    LLMAdapterError,
    LLMCallResult,
    LLMConfig,
    LLMRetryableError,
    LLMTransport,
    call_task_llm,
    load_llm_config,
)
from video_doc.llm_strategy import ChapterPlanError, validate_chapter_plan
from video_doc.task_workspace import TaskWorkspaceError, validate_task_id


CHAPTER_CONTENT_SCHEMA_VERSION = "1.0"
CHAPTER_INPUT_SCHEMA_VERSION = "1.0"
CHAPTER_INPUT_OWNERSHIP_POLICY = "asr-event-start-half-open-1.0"
CHAPTER_CONTENT_OUTPUT_LANGUAGE = "zh-CN"
CHAPTER_CONTENT_POLICY_VERSION = "asr-evidence-only-zh-cn-1.1"
CHAPTER_WINDOWING_VERSION = "chapter-window-by-event-start-1.0"
CHAPTER_CONTENT_VALIDATOR_VERSION = "chapter-content-validator-1.2"
CHAPTER_CONTENT_VALIDATION_ID = "chapter-content-validator-v1-2"
CHAPTER_CONTENT_PROMPT_VERSION = "chapter-content-window-1.1"
CHAPTER_CONTENT_MERGE_PROMPT_VERSION = "chapter-content-merge-1.1"
LEGACY_CHAPTER_CONTENT_POLICY_VERSION = "asr-evidence-only-1.0"
LEGACY_CHAPTER_CONTENT_VALIDATOR_VERSION = "chapter-content-validator-1.1"
LEGACY_CHAPTER_CONTENT_PROMPT_VERSION = "chapter-content-window-1.0"
LEGACY_CHAPTER_CONTENT_MERGE_PROMPT_VERSION = "chapter-content-merge-1.0"
CHAPTER_SINGLE_WINDOW_VERSION = "stage9-single-window-mock-1.0"
CHAPTER_WINDOWED_MOCK_VERSION = "stage9-windowed-mock-1.0"
CHAPTER_WINDOWED_REAL_VERSION = "stage9-windowed-real-1.0"
CHAPTER_MERGE_VALIDATION_ID = "chapter-content-merge-validator-v1-3"
MAX_WINDOW_CHARS = 12000
MAX_WINDOW_EVENTS = 160
P9_5_TASK_ID = "DEV01"
P9_5_STAGE8_RESULT_ID = "primary-7d30ef2c3279"
P9_5_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
P9_5_MODEL = "qwen3.7-flash"
P9_5_REAL_ATTEMPT_ID = (
    "windowed-real-windowed-primary-7d30ef2c3279-e5948ab208ff"
)
AUDIT_RECONCILIATION_VERSION = "chapter-content-audit-reconciliation-1.0"
OFFLINE_CANDIDATE_VERSION = "chapter-content-offline-candidate-1.0"
P9_5_RECONCILIATION_RELATIVE_PATH = (
    "content/recoveries/audit-reconciliation-0a2de9b915d3/reconciliation.json"
)
P9_5_RECONCILIATION_SHA256 = (
    "c026996c9c7d0fd332d945653e9c5f032ec207379a9d340028985eaeaf3f0caa"
)
P9_5_OFFLINE_CANDIDATE_RELATIVE_PATH = (
    "content/recoveries/offline-candidate-eeff5b6d5113"
)
P9_5_OFFLINE_CANDIDATE_HASHES = {
    "candidate.json": "121f1e4dcb4bb0d851b30e9ce2028c33113c8369f75e9271489774643fb83b58",
    "manifest.json": "5b8f3426743dbcebb1cd309629cf3e95b3d6de7f2eac7577c32aa467fecade32",
    "recovery-audit.json": "ed47f7cae715834951c11cfc9988b558208751a3ecab136b2289b44d0fd47713",
}
OFFLINE_FORMAL_RECOVERY_VERSION = "chapter-content-offline-formal-recovery-1.0"
SEMANTIC_REPAIR_VERSION = "chapter-content-semantic-repair-1.0"
P9_5_R3_SOURCE_CONTENT_RESULT_ID = "content-05e8f25d7674"
P9_5_R3_SOURCE_HASHES = {
    "content.json": "ee629613b4833e17c9b4c12537b515f2d198ae8390b8f8c208eacb457f6f1a22",
    "execution-report.json": "f42e2a3190e3d99c072b02d0681479a94a107a4b9fdd7b1d2db5f2a704a74b98",
    "manifest.json": "fd826f0f664b7d5be1da9d8ed3b84caf847f56c25de96a3fcdcfff950d0dc41c",
}
P9_5_R3_EVIDENCE_AUDIT_RELATIVE_PATH = (
    "docs/development/阶段9-P9-5-R2-5人工语义验收.md"
)
P9_5_R3_EVIDENCE_AUDIT_SHA256 = (
    "974d141085948700a5eaa8da78f25c9e8db817a8adec8c1c3d2c39c90177d5b7"
)
P9_5_R3_TARGET_CHAPTER_ID = "c0001"
P9_5_R3_TARGET_KEY_POINT_INDEX = 2
P9_5_R3_BEFORE_TEXT = (
    "焦虑障碍非常普遍，全球约七分之一的人口受其困扰，每年治疗成本超过420亿美元。"
)
P9_5_R3_AFTER_TEXT = (
    "焦虑障碍非常普遍，全球每14人中约有1人受其困扰，每年治疗成本超过420亿美元。"
)
P9_5_R3_EVIDENCE_IDS = ["a0040", "a0042", "a0044"]
P9_5_R3_REQUIRED_EVIDENCE_TEXT = {
    "a0040": "And what I can tell you is that anxiety is very common,",
    "a0042": "Right now, one in 14 people around the world",
    "a0044": "And each year, it costs over 42 billion dollars",
}
P9_5_R3_REPAIR_ID = "semantic-repair-d46996a2e873"
P9_5_R3_REPAIR_RELATIVE_PATH = (
    f"content/semantic-repairs/{P9_5_R3_REPAIR_ID}"
)
P9_5_R3_REPAIR_HASHES = {
    "candidate.json": "1c80e3ca4a0cf7edc0a0001f1d4142dab3f6ebed38f3f4cce668532621e97fe9",
    "repair-audit.json": "0be36cba2b46e46e382e5afa573b79ad24a7f4bc61457cad8acef15bf6e94f90",
    "manifest.json": "8688627deb18151ad758b01072057a2d44032c2b22077209c2f948c76083b1a1",
}
P9_5_R2_FROZEN_HASHES = {
    "attempt-report.json": "49d88b1098b29517b8ff4d160ab8a3a4cf9397365abad77fbe80b1baaa0c2dc5",
    "window-manifest.json": "eb73faa7ea795f83d1b2373bfdbeb8edf11e24a60dec5a01187787e078a07686",
    "windows/c0001/c0001-w001-validation-audit.json": "77eb4e0fe69df856df106b1924fcae92427139b9ef3685c67e4967c79bf0dda4",
    "windows/c0001/c0001-w001.json": "1cf3fea0ae431dbdee4fd6e1f3e9f0b9f2df17a8d364f60a478152fecd0cb3bc",
    "windows/c0002/c0002-w001-validation-audit.json": "d7530558dcf2e474120e8b7219d518040eda759b98b5bb5c43df8e08fe31026d",
    "windows/c0002/c0002-w001.json": "1ebb6ef68f1b02fb0df762e8eae56d52e58b4d4ba0be72ceb9091a8865423399",
    "windows/c0003/c0003-w001-validation-audit.json": "7c14deb6ffac58d6d862b93d1e6582f3976904d3f3db8a65e732d932dc75bb1c",
    "windows/c0003/c0003-w001.json": "ed50162f60a49490695e98bde4156de8c66b51d0cdc7c679f49fc8ae974ddd75",
    "windows/c0004/c0004-w001-validation-audit.json": "37d241ec5a72ed8c3f19972add6c922c0cfda7eb477ca68427b34c42c7ff8a25",
    "windows/c0004/c0004-w001.json": "4add813a0572163f57ef429c143f103da815147ffd72c013f9c578ba1c6030c0",
    "windows/c0005/c0005-w001-validation-audit.json": "7720c6383ad141182f57746ac69dd4900a7632976a30c7b57f933d7785ffc8b5",
}

_PROVENANCE_PAIRS = {
    "primary": "normal",
    "fallback": "unknown",
    "emergency": "degraded",
}


class ChapterContentError(RuntimeError):
    """阶段9输入、Schema或输出准备失败。"""


@dataclass(frozen=True)
class ChapterInputPreparationResult:
    """一次离线章节输入准备的结果。"""

    manifest_path: Path
    result_group_path: Path
    chapter_count: int
    event_count: int
    result_source: str
    quality_level: str
    cache_hit: bool


@dataclass(frozen=True)
class SingleWindowContentResult:
    """P9-2单窗mock内容生成结果。"""

    preview_path: Path
    attempt_report_path: Path
    attempt_path: Path
    chapter_count: int
    supplier_request_count: int
    cache_hit_count: int
    all_cache_hit: bool


@dataclass(frozen=True)
class WindowedMockContentResult:
    """P9-3分窗与合并mock结果。"""

    result_group_path: Path
    content_path: Path
    execution_report_path: Path
    attempt_path: Path
    chapter_count: int
    window_count: int
    merge_count: int
    supplier_request_count: int
    cache_hit_count: int
    all_cache_hit: bool
    result_group_cache_hit: bool


@dataclass(frozen=True)
class ChapterContentCallPlan:
    """正式正文生成前可离线计算的调用规模。"""

    chapter_count: int
    window_count: int
    merge_count: int
    business_call_count: int
    windows_by_chapter: tuple[int, ...]


@dataclass(frozen=True)
class AuditReconciliationResult:
    """R2-2陈旧失败审计关系产物。"""

    reconciliation_path: Path
    recovery_path: Path
    completed_call_count: int
    inspected_audit_count: int
    superseded_count: int
    cache_hit: bool


@dataclass(frozen=True)
class OfflineCandidateResult:
    """R2-3单章离线规范化候选结果。"""

    candidate_path: Path
    recovery_audit_path: Path
    recovery_path: Path
    changed_field_count: int
    cache_hit: bool


@dataclass(frozen=True)
class OfflineFormalRecoveryResult:
    """R2-4完整五章离线正式发布结果。"""

    result_group_path: Path
    content_path: Path
    execution_report_path: Path
    top_level_content_path: Path
    top_level_report_path: Path
    result_id: str
    chapter_count: int
    result_group_cache_hit: bool
    top_level_content_cache_hit: bool
    top_level_report_cache_hit: bool


@dataclass(frozen=True)
class SemanticRepairCandidateResult:
    """R3-2离线单字段语义修复候选。"""

    candidate_path: Path
    repair_audit_path: Path
    manifest_path: Path
    repair_path: Path
    changed_text_path_count: int
    api_request_count: int
    cache_hit: bool


@dataclass(frozen=True)
class SemanticRepairFormalResult:
    """R3-3离线语义修复正式发布结果。"""

    result_group_path: Path
    content_path: Path
    execution_report_path: Path
    top_level_content_path: Path
    top_level_report_path: Path
    result_id: str
    chapter_count: int
    api_request_count: int
    result_group_cache_hit: bool
    top_level_content_cache_hit: bool
    top_level_report_cache_hit: bool


@dataclass(frozen=True)
class _FormalChapterInputs:
    timeline: dict[str, Any]
    plan: dict[str, Any]
    report: dict[str, Any]
    result_group_path: Path
    plan_sha256: str
    timeline_sha256: str
    execution_report_sha256: str


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChapterContentError(f"{description}不存在：{path}") from exc
    except OSError as exc:
        raise ChapterContentError(f"无法读取{description}：{path}") from exc
    except UnicodeError as exc:
        raise ChapterContentError(f"{description}不是有效UTF-8文本：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ChapterContentError(f"{description}不是有效JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ChapterContentError(f"{description}必须是JSON对象：{path}")
    return payload


def _valid_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise ChapterContentError(f"{field}必须是64位SHA-256。")
    return value.lower()


def _load_formal_chapter_inputs(
    output_root: str | Path,
    task_id: str,
) -> _FormalChapterInputs:
    try:
        validate_task_id(task_id)
        timeline = load_task_timeline(output_root, task_id)
    except (TaskWorkspaceError, ASRTimelineError) as exc:
        raise ChapterContentError(str(exc)) from exc

    task_root = Path(output_root).expanduser() / task_id
    plan_path = task_root / "boundaries" / "llm.json"
    report_path = task_root / "boundaries" / "llm-execution-report.json"
    plan = _read_json(plan_path, "正式ChapterPlan")
    report = _read_json(report_path, "正式分章执行报告")
    try:
        validate_chapter_plan(plan, timeline=timeline, expected_task_id=task_id)
    except ChapterPlanError as exc:
        raise ChapterContentError(f"正式ChapterPlan无效：{exc}") from exc

    if report.get("schema_version") != "1.0":
        raise ChapterContentError("正式分章执行报告Schema版本无效。")
    if report.get("task_id") != task_id or report.get("status") != "success":
        raise ChapterContentError("正式分章执行报告任务或状态无效。")
    result_source = report.get("result_source")
    quality_level = report.get("quality_level")
    if _PROVENANCE_PAIRS.get(result_source) != quality_level:
        raise ChapterContentError("正式分章来源与质量级别不一致。")

    plan_sha256 = _canonical_sha256(plan)
    if report.get("final_plan_sha256") != plan_sha256:
        raise ChapterContentError("正式分章执行报告引用的计划哈希不一致。")
    result_id = report.get("result_id")
    expected_result_id = f"{result_source}-{plan_sha256[:12]}"
    if result_id != expected_result_id:
        raise ChapterContentError("正式分章结果ID与计划或来源不一致。")

    group = task_root / "boundaries" / "formal-results" / expected_result_id
    group_plan_path = group / "chapter-plan.json"
    group_report_path = group / "execution-report.json"
    group_manifest_path = group / "manifest.json"
    group_plan = _read_json(group_plan_path, "正式结果组ChapterPlan")
    group_report = _read_json(group_report_path, "正式结果组执行报告")
    manifest = _read_json(group_manifest_path, "正式结果组manifest")
    files = manifest.get("files")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("task_id") != task_id
        or manifest.get("result_id") != expected_result_id
        or not isinstance(files, dict)
        or set(files) != {"chapter-plan.json", "execution-report.json"}
    ):
        raise ChapterContentError("正式分章结果组manifest结构或来源无效。")
    for filename, path in (
        ("chapter-plan.json", group_plan_path),
        ("execution-report.json", group_report_path),
    ):
        expected_hash = _valid_sha256(files.get(filename), f"{filename}文件哈希")
        if _file_sha256(path) != expected_hash:
            raise ChapterContentError(f"正式分章结果组文件哈希不一致：{filename}")
    if group_plan != plan or group_report != report:
        raise ChapterContentError("顶层正式分章产物与不可混用结果组不一致。")

    timeline_sha256 = _canonical_sha256(timeline)
    source_timeline = plan.get("source_timeline")
    if (
        not isinstance(source_timeline, dict)
        or source_timeline.get("sha256") != timeline_sha256
    ):
        raise ChapterContentError("正式ChapterPlan引用的时间轴哈希不一致。")
    return _FormalChapterInputs(
        timeline=timeline,
        plan=plan,
        report=report,
        result_group_path=group,
        plan_sha256=plan_sha256,
        timeline_sha256=timeline_sha256,
        execution_report_sha256=_canonical_sha256(report),
    )


def _chapter_intervals(plan: dict[str, Any]) -> list[dict[str, Any]]:
    boundaries = plan["boundaries"]
    duration = float(plan["duration_seconds"])
    intervals: list[dict[str, Any]] = []
    for offset, boundary in enumerate(boundaries):
        start = float(boundary["timestamp"])
        end = (
            float(boundaries[offset + 1]["timestamp"])
            if offset + 1 < len(boundaries)
            else duration
        )
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ChapterContentError("ChapterPlan无法构造合法的章节半开区间。")
        intervals.append(
            {
                "chapter_id": f"c{offset + 1:04d}",
                "index": offset + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "planned_title": boundary["title"].strip(),
                "events": [],
            }
        )
    return intervals


def _assign_events_once(
    timeline: dict[str, Any],
    intervals: list[dict[str, Any]],
) -> None:
    chapter_index = 0
    assigned_ids: list[str] = []
    for event in timeline["events"]:
        event_start = float(event["start"])
        while (
            chapter_index + 1 < len(intervals)
            and event_start >= float(intervals[chapter_index]["end"])
        ):
            chapter_index += 1
        chapter = intervals[chapter_index]
        if not (float(chapter["start"]) <= event_start < float(chapter["end"])):
            raise ChapterContentError(f"ASR事件无法唯一归属章节：{event['id']}")
        chapter["events"].append(event)
        assigned_ids.append(event["id"])

    timeline_ids = [event["id"] for event in timeline["events"]]
    if assigned_ids != timeline_ids or len(set(assigned_ids)) != len(assigned_ids):
        raise ChapterContentError("ASR事件唯一归属审计失败。")
    empty = [chapter["chapter_id"] for chapter in intervals if not chapter["events"]]
    if empty:
        raise ChapterContentError(f"章节没有章内ASR事件：{'、'.join(empty)}")


def _manifest_payload(inputs: _FormalChapterInputs) -> dict[str, Any]:
    intervals = _chapter_intervals(inputs.plan)
    _assign_events_once(inputs.timeline, intervals)
    chapters: list[dict[str, Any]] = []
    for chapter in intervals:
        prompt_events = [
            {
                "id": event["id"],
                "start": event["start"],
                "end": event["end"],
                "text": event["text"],
            }
            for event in chapter["events"]
        ]
        event_ids = [event["id"] for event in prompt_events]
        chapters.append(
            {
                "chapter_id": chapter["chapter_id"],
                "index": chapter["index"],
                "start": chapter["start"],
                "end": chapter["end"],
                "planned_title": chapter["planned_title"],
                "event_count": len(event_ids),
                "first_event_id": event_ids[0],
                "last_event_id": event_ids[-1],
                "event_ids": event_ids,
                "input_sha256": _canonical_sha256(prompt_events),
            }
        )
    payload = {
        "schema_version": CHAPTER_INPUT_SCHEMA_VERSION,
        "task_id": inputs.plan["task_id"],
        "strategy": inputs.plan["strategy"],
        "source_chapter_plan": {
            "schema_version": inputs.plan["schema_version"],
            "sha256": inputs.plan_sha256,
            "result_id": inputs.report["result_id"],
        },
        "source_timeline": {
            "schema_version": inputs.timeline["schema_version"],
            "sha256": inputs.timeline_sha256,
            "event_count": len(inputs.timeline["events"]),
        },
        "chaptering_provenance": {
            "status": inputs.report["status"],
            "result_source": inputs.report["result_source"],
            "quality_level": inputs.report["quality_level"],
            "execution_report_sha256": inputs.execution_report_sha256,
        },
        "ownership": {
            "policy_version": CHAPTER_INPUT_OWNERSHIP_POLICY,
            "rule": "chapter.start <= event.start < chapter.end",
            "assigned_event_count": len(inputs.timeline["events"]),
            "duplicate_event_count": 0,
            "unassigned_event_count": 0,
        },
        "chapters": chapters,
    }
    payload["cache_key"] = _canonical_sha256(payload)
    validate_chapter_input_manifest(payload, timeline=inputs.timeline)
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> bool:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return True
        except (OSError, UnicodeError):
            pass
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise ChapterContentError(f"无法原子保存阶段9 JSON产物：{path}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def prepare_task_chapter_inputs(
    output_root: str | Path,
    task_id: str,
) -> ChapterInputPreparationResult:
    """校验正式阶段8结果并生成可缓存的离线逐章输入manifest。"""

    inputs = _load_formal_chapter_inputs(output_root, task_id)
    payload = _manifest_payload(inputs)
    attempt_id = (
        f"input-{inputs.report['result_id']}-{inputs.timeline_sha256[:12]}"
    )
    manifest_path = (
        Path(output_root).expanduser()
        / task_id
        / "content"
        / "attempts"
        / attempt_id
        / "chapter-input-manifest.json"
    )
    cache_hit = _write_json_atomic(manifest_path, payload)
    return ChapterInputPreparationResult(
        manifest_path=manifest_path,
        result_group_path=inputs.result_group_path,
        chapter_count=len(payload["chapters"]),
        event_count=payload["source_timeline"]["event_count"],
        result_source=payload["chaptering_provenance"]["result_source"],
        quality_level=payload["chaptering_provenance"]["quality_level"],
        cache_hit=cache_hit,
    )


def _evidence_field(
    value: Any,
    *,
    field: str,
    allowed_ids: list[str],
) -> tuple[str, list[str]]:
    if not isinstance(value, dict):
        raise ChapterContentError(f"{field}必须是JSON对象。")
    text = value.get("text")
    ids = value.get("source_event_ids")
    if not isinstance(text, str) or not text.strip():
        raise ChapterContentError(f"{field}.text不能为空。")
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
        raise ChapterContentError(f"{field}.source_event_ids必须是非空字符串数组。")
    if len(set(ids)) != len(ids):
        raise ChapterContentError(f"{field}包含重复引用事件ID。")
    order = {event_id: index for index, event_id in enumerate(allowed_ids)}
    if any(event_id not in order for event_id in ids):
        raise ChapterContentError(f"{field}引用了章外或未知ASR事件ID。")
    normalized_ids = sorted(ids, key=order.__getitem__)
    return text.strip(), normalized_ids


def _contains_cjk(text: str) -> bool:
    return any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in text
    )


def _validate_zh_cn_text(text: str, *, field: str, allow_term: bool = False) -> None:
    if _contains_cjk(text):
        return
    if allow_term and len(text) <= 40 and 1 <= len(text.split()) <= 5:
        return
    raise ChapterContentError(f"{field}.text必须使用简体中文。")


def _normalize_window_response_core(
    response: dict[str, Any],
    *,
    allowed_ids: list[str],
    enforce_zh_cn: bool,
) -> dict[str, Any]:
    if set(response) != {"title", "summary", "key_points", "keywords"}:
        raise ChapterContentError(
            "窗口响应只能包含title、summary、key_points和keywords。"
        )
    normalized: dict[str, Any] = {}
    cited: set[str] = set()
    for field in ("title", "summary"):
        text, ids = _evidence_field(
            response.get(field), field=field, allowed_ids=allowed_ids
        )
        if enforce_zh_cn:
            _validate_zh_cn_text(text, field=field)
        normalized[field] = {"text": text, "source_event_ids": ids}
        cited.update(ids)
    for field, minimum, maximum in (
        ("key_points", 3, 6),
        ("keywords", 3, 8),
    ):
        items = response.get(field)
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            raise ChapterContentError(
                f"{field}数量必须在{minimum}到{maximum}之间。"
            )
        normalized_items: list[dict[str, Any]] = []
        normalized_texts: list[str] = []
        for index, item in enumerate(items, start=1):
            text, ids = _evidence_field(
                item,
                field=f"{field}[{index}]",
                allowed_ids=allowed_ids,
            )
            if enforce_zh_cn:
                _validate_zh_cn_text(
                    text,
                    field=f"{field}[{index}]",
                    allow_term=field == "keywords",
                )
            normalized_items.append({"text": text, "source_event_ids": ids})
            normalized_texts.append(text.casefold())
            cited.update(ids)
        if len(set(normalized_texts)) != len(normalized_texts):
            raise ChapterContentError(f"{field}包含完全重复内容。")
        normalized[field] = normalized_items
    normalized["source_event_ids"] = [
        event_id for event_id in allowed_ids if event_id in cited
    ]
    return normalized


def _normalize_window_response(
    response: dict[str, Any],
    *,
    allowed_ids: list[str],
) -> dict[str, Any]:
    """Validator 1.2：保留证据校验并强制核心内容为简体中文。"""

    return _normalize_window_response_core(
        response,
        allowed_ids=allowed_ids,
        enforce_zh_cn=True,
    )


def _normalize_generic_shape_variants(
    response: dict[str, Any], *, allowed_ids: list[str] | None = None
) -> dict[str, Any]:
    """只做有界形态修复，并删除无可用证据的关键词。"""

    normalized = dict(response)
    for field, maximum in (("key_points", 6), ("keywords", 8)):
        items = normalized.get(field)
        if not isinstance(items, list):
            continue
        repaired: list[Any] = []
        for item in items:
            if (
                isinstance(item, list)
                and len(item) >= 2
                and isinstance(item[0], str)
                and all(isinstance(event_id, str) for event_id in item[1:])
            ):
                repaired.append(
                    {"text": item[0], "source_event_ids": list(item[1:])}
                )
            else:
                repaired.append(item)
        if field == "keywords" and allowed_ids is not None:
            allowed = set(allowed_ids)
            repaired = [
                item
                for item in repaired
                if not isinstance(item, dict)
                or not isinstance(item.get("source_event_ids"), list)
                or (
                    bool(item["source_event_ids"])
                    and all(event_id in allowed for event_id in item["source_event_ids"])
                )
            ]
        normalized[field] = repaired[:maximum]
    return normalized


def _normalize_window_response_legacy(
    response: dict[str, Any],
    *,
    allowed_ids: list[str],
) -> dict[str, Any]:
    """只用于重放R2冻结的Validator 1.1历史产物。"""

    return _normalize_window_response_core(
        response,
        allowed_ids=allowed_ids,
        enforce_zh_cn=False,
    )


def _verify_frozen_attempt(
    task_root: Path,
    *,
    attempt_id: str,
    expected_hashes: Mapping[str, str],
) -> Path:
    attempt_root = task_root / "content" / "attempts" / attempt_id
    actual_files = {
        path.relative_to(attempt_root).as_posix(): path
        for path in attempt_root.rglob("*")
        if path.is_file()
    }
    if set(actual_files) != set(expected_hashes):
        raise ChapterContentError("R2冻结attempt文件集合与冻结清单不一致。")
    for relative, expected in expected_hashes.items():
        if _file_sha256(actual_files[relative]) != expected:
            raise ChapterContentError(f"R2冻结文件哈希不一致：{relative}")
    return attempt_root


def _reconcile_attempt_audits(
    task_root: Path,
    *,
    attempt_id: str,
    expected_hashes: Mapping[str, str],
) -> AuditReconciliationResult:
    """只检查已完成调用，另写关系产物，不改写历史attempt。"""

    attempt_root = _verify_frozen_attempt(
        task_root, attempt_id=attempt_id, expected_hashes=expected_hashes
    )

    report = _read_json(attempt_root / "attempt-report.json", "R2源attempt报告")
    window_manifest = _read_json(
        attempt_root / "window-manifest.json", "R2源窗口manifest"
    )
    if (
        report.get("task_id") != task_root.name
        or report.get("status") != "failed"
        or report.get("attempt_version") != CHAPTER_WINDOWED_REAL_VERSION
        or window_manifest.get("task_id") != task_root.name
    ):
        raise ChapterContentError("R2源attempt任务、状态或版本无效。")
    completed_calls = report.get("completed_calls")
    if not isinstance(completed_calls, list):
        raise ChapterContentError("R2源attempt缺少completed_calls。")

    allowed_by_call: dict[str, list[str]] = {}
    chapters = window_manifest.get("chapters")
    if not isinstance(chapters, list):
        raise ChapterContentError("R2源窗口manifest章节无效。")
    for chapter in chapters:
        if not isinstance(chapter, dict) or not isinstance(chapter.get("windows"), list):
            raise ChapterContentError("R2源窗口manifest结构无效。")
        for window in chapter["windows"]:
            if not isinstance(window, dict):
                raise ChapterContentError("R2源窗口记录无效。")
            call_id = window.get("window_id")
            event_ids = window.get("event_ids")
            if (
                not isinstance(call_id, str)
                or not isinstance(event_ids, list)
                or not event_ids
                or any(not isinstance(item, str) for item in event_ids)
            ):
                raise ChapterContentError("R2源窗口ID或事件ID无效。")
            allowed_by_call[call_id] = event_ids

    inspected_audits = 0
    superseded: list[dict[str, Any]] = []
    for completed in completed_calls:
        if (
            not isinstance(completed, dict)
            or completed.get("kind") != "window"
            or not isinstance(completed.get("call_id"), str)
            or not isinstance(completed.get("chapter_id"), str)
        ):
            raise ChapterContentError("R2 completed_calls记录无效。")
        call_id = completed["call_id"]
        chapter_id = completed["chapter_id"]
        if call_id not in allowed_by_call:
            raise ChapterContentError("R2完成调用不在窗口manifest中。")
        call_path = attempt_root / "windows" / chapter_id / f"{call_id}.json"
        recorded_path = completed.get("output_path")
        if not isinstance(recorded_path, str) or Path(recorded_path).resolve() != call_path.resolve():
            raise ChapterContentError("R2完成调用输出路径与冻结attempt不一致。")
        call = _read_json(call_path, "R2成功调用缓存")
        raw_response = call.get("raw_response")
        if (
            call.get("task_id") != task_root.name
            or call.get("call_id") != call_id
            or not isinstance(raw_response, dict)
            or call.get("response")
            != _normalize_window_response_legacy(
                raw_response, allowed_ids=allowed_by_call[call_id]
            )
        ):
            raise ChapterContentError("R2成功调用缓存身份或规范化结果无效。")
        audit_path = call_path.with_name(f"{call_id}-validation-audit.json")
        if not audit_path.is_file():
            continue
        inspected_audits += 1
        audit = _read_json(audit_path, "R2历史校验审计")
        if (
            audit.get("task_id") != task_root.name
            or audit.get("call_id") != call_id
            or audit.get("cache_key") != call.get("cache_key")
            or audit.get("provider") != call.get("provider")
        ):
            raise ChapterContentError("R2历史审计与成功调用缓存不一致。")
        if audit.get("status") == "failed":
            superseded.append(
                {
                    "call_id": call_id,
                    "chapter_id": chapter_id,
                    "relationship": "superseded_by_later_success",
                    "historical_audit_path": str(
                        audit_path.relative_to(task_root).as_posix()
                    ),
                    "historical_audit_sha256": _file_sha256(audit_path),
                    "successful_call_path": str(
                        call_path.relative_to(task_root).as_posix()
                    ),
                    "successful_call_sha256": _file_sha256(call_path),
                }
            )
        elif audit.get("status") != "completed_after_retry":
            raise ChapterContentError("R2历史审计状态无法解释。")

    if not superseded:
        raise ChapterContentError("R2未发现可建立取代关系的陈旧失败审计。")
    payload = {
        "schema_version": "1.0",
        "reconciliation_version": AUDIT_RECONCILIATION_VERSION,
        "task_id": task_root.name,
        "source_attempt_id": attempt_id,
        "source_attempt_report_sha256": _file_sha256(
            attempt_root / "attempt-report.json"
        ),
        "source_window_manifest_sha256": _file_sha256(
            attempt_root / "window-manifest.json"
        ),
        "completed_call_count": len(completed_calls),
        "inspected_audit_count": inspected_audits,
        "superseded_count": len(superseded),
        "failed_call_id_not_inspected": report.get("failed_call_id"),
        "relationships": superseded,
    }
    payload["cache_key"] = _canonical_sha256(payload)
    recovery_id = f"audit-reconciliation-{payload['cache_key'][:12]}"
    recovery_root = task_root / "content" / "recoveries" / recovery_id
    reconciliation_path = recovery_root / "reconciliation.json"
    cache_hit = _write_json_atomic(reconciliation_path, payload)
    return AuditReconciliationResult(
        reconciliation_path=reconciliation_path,
        recovery_path=recovery_root,
        completed_call_count=len(completed_calls),
        inspected_audit_count=inspected_audits,
        superseded_count=len(superseded),
        cache_hit=cache_hit,
    )


def reconcile_task_chapter_content_audits(
    output_root: str | Path,
    task_id: str,
) -> AuditReconciliationResult:
    """R2-2固定DEV01离线审计协调入口；不读取失败调用响应。"""

    if task_id != P9_5_TASK_ID:
        raise ChapterContentError("R2-2只允许任务DEV01。")
    task_root = Path(output_root).expanduser() / task_id
    return _reconcile_attempt_audits(
        task_root,
        attempt_id=P9_5_REAL_ATTEMPT_ID,
        expected_hashes=P9_5_R2_FROZEN_HASHES,
    )


def _evidence_order_changes(
    raw: dict[str, Any],
    normalized: dict[str, Any],
) -> list[str]:
    """证明规范化只改变证据ID顺序，不改变文本、项顺序或ID集合。"""

    changes: list[str] = []
    for field in ("title", "summary"):
        raw_item = raw[field]
        normalized_item = normalized[field]
        if raw_item["text"] != normalized_item["text"]:
            raise ChapterContentError(f"R2-3不允许修改{field}.text。")
        if sorted(raw_item["source_event_ids"]) != sorted(
            normalized_item["source_event_ids"]
        ):
            raise ChapterContentError(f"R2-3不允许修改{field}引用集合。")
        if raw_item["source_event_ids"] != normalized_item["source_event_ids"]:
            changes.append(f"{field}.source_event_ids")
    for field in ("key_points", "keywords"):
        if len(raw[field]) != len(normalized[field]):
            raise ChapterContentError(f"R2-3不允许修改{field}数量。")
        for index, (raw_item, normalized_item) in enumerate(
            zip(raw[field], normalized[field], strict=True), start=1
        ):
            if raw_item["text"] != normalized_item["text"]:
                raise ChapterContentError(f"R2-3不允许修改{field}[{index}].text。")
            if sorted(raw_item["source_event_ids"]) != sorted(
                normalized_item["source_event_ids"]
            ):
                raise ChapterContentError(
                    f"R2-3不允许修改{field}[{index}]引用集合。"
                )
            if raw_item["source_event_ids"] != normalized_item["source_event_ids"]:
                changes.append(f"{field}[{index}].source_event_ids")
    return changes


def _publish_offline_candidate_group(
    task_root: Path,
    *,
    candidate: dict[str, Any],
    recovery_audit: dict[str, Any],
) -> tuple[Path, bool]:
    seed = {"candidate": candidate, "recovery_audit": recovery_audit}
    recovery_id = f"offline-candidate-{_canonical_sha256(seed)[:12]}"
    group = task_root / "content" / "recoveries" / recovery_id
    candidate_path = group / "candidate.json"
    audit_path = group / "recovery-audit.json"
    manifest_path = group / "manifest.json"
    if group.exists():
        names = {path.name for path in group.iterdir() if path.is_file()}
        manifest = _read_json(manifest_path, "R2-3候选manifest")
        files = manifest.get("files")
        if (
            names != {"candidate.json", "recovery-audit.json", "manifest.json"}
            or manifest.get("schema_version") != "1.0"
            or manifest.get("recovery_id") != recovery_id
            or manifest.get("task_id") != task_root.name
            or not isinstance(files, dict)
            or files.get("candidate.json") != _file_sha256(candidate_path)
            or files.get("recovery-audit.json") != _file_sha256(audit_path)
            or _read_json(candidate_path, "R2-3候选") != candidate
            or _read_json(audit_path, "R2-3恢复审计") != recovery_audit
        ):
            raise ChapterContentError("R2-3候选结果组损坏或发生内容混用。")
        return group, True
    temporary = group.with_name(f"{group.name}.tmp")
    if temporary.exists():
        raise ChapterContentError("R2-3存在未清理的候选临时目录。")
    try:
        temporary.mkdir(parents=True, exist_ok=False)
        temporary_candidate = temporary / "candidate.json"
        temporary_audit = temporary / "recovery-audit.json"
        _write_json_atomic(temporary_candidate, candidate)
        _write_json_atomic(temporary_audit, recovery_audit)
        _write_json_atomic(
            temporary / "manifest.json",
            {
                "schema_version": "1.0",
                "recovery_id": recovery_id,
                "task_id": task_root.name,
                "files": {
                    "candidate.json": _file_sha256(temporary_candidate),
                    "recovery-audit.json": _file_sha256(temporary_audit),
                },
            },
        )
        temporary.replace(group)
    except OSError as exc:
        raise ChapterContentError("无法原子发布R2-3离线候选组。") from exc
    finally:
        if temporary.exists():
            try:
                shutil.rmtree(temporary)
            except OSError:
                pass
    return group, False


def _recover_failed_window_candidate_offline(
    task_root: Path,
    *,
    attempt_id: str,
    expected_hashes: Mapping[str, str],
    reconciliation_path: Path,
    reconciliation_sha256: str,
    chapter_id: str,
    call_id: str,
    source_attempt_number: int,
    expected_changed_fields: list[str],
) -> OfflineCandidateResult:
    attempt_root = _verify_frozen_attempt(
        task_root, attempt_id=attempt_id, expected_hashes=expected_hashes
    )
    if _file_sha256(reconciliation_path) != reconciliation_sha256:
        raise ChapterContentError("R2-3审计协调关系哈希不一致。")
    reconciliation = _read_json(reconciliation_path, "R2-3审计协调关系")
    if (
        reconciliation.get("source_attempt_id") != attempt_id
        or reconciliation.get("failed_call_id_not_inspected") != call_id
    ):
        raise ChapterContentError("R2-3审计协调关系与失败调用不一致。")

    window_manifest = _read_json(
        attempt_root / "window-manifest.json", "R2-3源窗口manifest"
    )
    allowed_ids: list[str] | None = None
    for chapter in window_manifest.get("chapters", []):
        if isinstance(chapter, dict) and chapter.get("chapter_id") == chapter_id:
            for window in chapter.get("windows", []):
                if isinstance(window, dict) and window.get("window_id") == call_id:
                    ids = window.get("event_ids")
                    if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                        allowed_ids = ids
    if not allowed_ids:
        raise ChapterContentError("R2-3无法在冻结窗口manifest中定位失败调用。")

    source_audit_path = (
        attempt_root
        / "windows"
        / chapter_id
        / f"{call_id}-validation-audit.json"
    )
    source_audit = _read_json(source_audit_path, "R2-3第五章失败审计")
    attempts = source_audit.get("attempts")
    if (
        source_audit.get("task_id") != task_root.name
        or source_audit.get("call_id") != call_id
        or source_audit.get("status") != "failed"
        or not isinstance(attempts, list)
        or len(attempts) < source_attempt_number
    ):
        raise ChapterContentError("R2-3失败审计身份、状态或尝试记录无效。")
    source_attempt = attempts[source_attempt_number - 1]
    raw_response = source_attempt.get("raw_response") if isinstance(source_attempt, dict) else None
    if not isinstance(raw_response, dict):
        raise ChapterContentError("R2-3指定尝试没有可离线恢复的JSON对象。")
    normalized = _normalize_window_response_legacy(
        raw_response, allowed_ids=allowed_ids
    )
    changed_fields = _evidence_order_changes(raw_response, normalized)
    if changed_fields != expected_changed_fields:
        raise ChapterContentError("R2-3实际规范化字段与冻结预期不一致。")

    raw_sha256 = _canonical_sha256(raw_response)
    candidate = {
        "schema_version": "1.0",
        "recovery_version": OFFLINE_CANDIDATE_VERSION,
        "task_id": task_root.name,
        "chapter_id": chapter_id,
        "call_id": call_id,
        "validator_version": LEGACY_CHAPTER_CONTENT_VALIDATOR_VERSION,
        "source_attempt_number": source_attempt_number,
        "source_raw_response_sha256": raw_sha256,
        "response": normalized,
    }
    recovery_audit = {
        "schema_version": "1.0",
        "recovery_version": OFFLINE_CANDIDATE_VERSION,
        "task_id": task_root.name,
        "chapter_id": chapter_id,
        "call_id": call_id,
        "source_attempt_id": attempt_id,
        "source_validation_audit_path": str(
            source_audit_path.relative_to(task_root).as_posix()
        ),
        "source_validation_audit_sha256": _file_sha256(source_audit_path),
        "source_attempt_number": source_attempt_number,
        "source_raw_response_sha256": raw_sha256,
        "changed_fields": changed_fields,
        "invariants": {
            "text_unchanged": True,
            "item_order_unchanged": True,
            "evidence_sets_unchanged": True,
            "only_event_id_order_normalized": True,
            "api_request_count": 0,
        },
    }
    group, cache_hit = _publish_offline_candidate_group(
        task_root, candidate=candidate, recovery_audit=recovery_audit
    )
    return OfflineCandidateResult(
        candidate_path=group / "candidate.json",
        recovery_audit_path=group / "recovery-audit.json",
        recovery_path=group,
        changed_field_count=len(changed_fields),
        cache_hit=cache_hit,
    )


def recover_task_chapter_content_candidate_offline(
    output_root: str | Path,
    task_id: str,
) -> OfflineCandidateResult:
    """R2-3固定第五章离线候选入口；不写调用缓存或正式结果。"""

    if task_id != P9_5_TASK_ID:
        raise ChapterContentError("R2-3只允许任务DEV01。")
    task_root = Path(output_root).expanduser() / task_id
    return _recover_failed_window_candidate_offline(
        task_root,
        attempt_id=P9_5_REAL_ATTEMPT_ID,
        expected_hashes=P9_5_R2_FROZEN_HASHES,
        reconciliation_path=task_root / P9_5_RECONCILIATION_RELATIVE_PATH,
        reconciliation_sha256=P9_5_RECONCILIATION_SHA256,
        chapter_id="c0005",
        call_id="c0005-w001",
        source_attempt_number=2,
        expected_changed_fields=["keywords[2].source_event_ids"],
    )


def _verify_frozen_group(
    group: Path,
    *,
    expected_hashes: Mapping[str, str],
    description: str,
) -> None:
    actual = {path.name: path for path in group.iterdir() if path.is_file()}
    if set(actual) != set(expected_hashes):
        raise ChapterContentError(f"{description}文件集合与冻结清单不一致。")
    for name, expected in expected_hashes.items():
        if _file_sha256(actual[name]) != expected:
            raise ChapterContentError(f"{description}文件哈希不一致：{name}")


def _build_offline_recovered_bundle(
    manifest: dict[str, Any],
    *,
    responses: Mapping[str, dict[str, Any]],
    provider: Mapping[str, Any],
    source_attempt_id: str,
    reconciliation_sha256: str,
    candidate_manifest_sha256: str,
) -> dict[str, Any]:
    expected_ids = [chapter["chapter_id"] for chapter in manifest["chapters"]]
    if set(responses) != set(expected_ids):
        raise ChapterContentError("R2-4章节响应集合存在缺失、未知或混用。")
    chapters: list[dict[str, Any]] = []
    for expected in manifest["chapters"]:
        chapter_id = expected["chapter_id"]
        response = responses[chapter_id]
        normalized = _normalize_window_response_legacy(
            {
                field: response[field]
                for field in ("title", "summary", "key_points", "keywords")
            },
            allowed_ids=expected["event_ids"],
        )
        if normalized != response:
            raise ChapterContentError(f"R2-4章节{chapter_id}不是Validator 1.1规范结果。")
        chapters.append(
            {
                "chapter_id": chapter_id,
                "index": expected["index"],
                "start": expected["start"],
                "end": expected["end"],
                "planned_title": expected["planned_title"],
                "title": response["title"],
                "summary": response["summary"],
                "key_points": response["key_points"],
                "keywords": response["keywords"],
                "source_event_ids": response["source_event_ids"],
                "window_count": 1,
            }
        )
    bundle = {
        "schema_version": CHAPTER_CONTENT_SCHEMA_VERSION,
        "task_id": manifest["task_id"],
        "strategy": manifest["strategy"],
        "source_chapter_plan": manifest["source_chapter_plan"],
        "source_timeline": manifest["source_timeline"],
        "chaptering_provenance": manifest["chaptering_provenance"],
        "generation": {
            "status": "success",
            "mode": "real-windowed-offline-recovery",
            "backend": provider.get("backend"),
            "model": provider.get("model"),
            "prompt_version": LEGACY_CHAPTER_CONTENT_PROMPT_VERSION,
            "merge_prompt_version": LEGACY_CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
            "content_policy_version": LEGACY_CHAPTER_CONTENT_POLICY_VERSION,
            "windowing_version": CHAPTER_WINDOWING_VERSION,
            "validator_version": LEGACY_CHAPTER_CONTENT_VALIDATOR_VERSION,
            "recovery_version": OFFLINE_FORMAL_RECOVERY_VERSION,
            "source_attempt_id": source_attempt_id,
            "reconciliation_sha256": reconciliation_sha256,
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "api_request_count": 0,
        },
        "chapters": chapters,
    }
    validate_chapter_content_bundle(bundle, input_manifest=manifest)
    return bundle


def assemble_task_chapter_content_offline(
    output_root: str | Path,
    task_id: str,
) -> OfflineFormalRecoveryResult:
    """R2-4组装冻结的五章来源并发布正式结果；不调用LLM。"""

    if task_id != P9_5_TASK_ID:
        raise ChapterContentError("R2-4只允许任务DEV01。")
    task_root = Path(output_root).expanduser() / task_id
    attempt_root = _verify_frozen_attempt(
        task_root,
        attempt_id=P9_5_REAL_ATTEMPT_ID,
        expected_hashes=P9_5_R2_FROZEN_HASHES,
    )
    reconciliation_path = task_root / P9_5_RECONCILIATION_RELATIVE_PATH
    if _file_sha256(reconciliation_path) != P9_5_RECONCILIATION_SHA256:
        raise ChapterContentError("R2-4审计协调关系哈希不一致。")
    candidate_group = task_root / P9_5_OFFLINE_CANDIDATE_RELATIVE_PATH
    _verify_frozen_group(
        candidate_group,
        expected_hashes=P9_5_OFFLINE_CANDIDATE_HASHES,
        description="R2-4第五章候选组",
    )

    preparation = prepare_task_chapter_inputs(output_root, task_id)
    manifest = _read_json(preparation.manifest_path, "R2-4章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterContentError(str(exc)) from exc
    validate_chapter_input_manifest(manifest, timeline=timeline)
    window_manifest = _read_json(
        attempt_root / "window-manifest.json", "R2-4窗口manifest"
    )
    allowed_by_call = {
        window["window_id"]: window["event_ids"]
        for chapter in window_manifest["chapters"]
        for window in chapter["windows"]
    }

    responses: dict[str, dict[str, Any]] = {}
    provider: dict[str, Any] | None = None
    for index in range(1, 5):
        chapter_id = f"c{index:04d}"
        call_id = f"{chapter_id}-w001"
        call_path = attempt_root / "windows" / chapter_id / f"{call_id}.json"
        call = _read_json(call_path, f"R2-4第{index}章成功调用")
        raw = call.get("raw_response")
        current_provider = call.get("provider")
        if (
            call.get("task_id") != task_id
            or call.get("call_id") != call_id
            or not isinstance(raw, dict)
            or not isinstance(current_provider, dict)
        ):
            raise ChapterContentError(f"R2-4第{index}章调用身份或原始响应无效。")
        normalized = _normalize_window_response_legacy(
            raw, allowed_ids=allowed_by_call[call_id]
        )
        if call.get("response") != normalized:
            raise ChapterContentError(f"R2-4第{index}章成功调用规范化结果无效。")
        if provider is None:
            provider = current_provider
        elif current_provider != provider:
            raise ChapterContentError("R2-4前四章公开provider发生混用。")
        responses[chapter_id] = normalized

    candidate = _read_json(candidate_group / "candidate.json", "R2-4第五章候选")
    candidate_response = candidate.get("response")
    if (
        candidate.get("task_id") != task_id
        or candidate.get("chapter_id") != "c0005"
        or candidate.get("call_id") != "c0005-w001"
        or candidate.get("validator_version")
        != LEGACY_CHAPTER_CONTENT_VALIDATOR_VERSION
        or not isinstance(candidate_response, dict)
    ):
        raise ChapterContentError("R2-4第五章候选身份、版本或响应无效。")
    responses["c0005"] = candidate_response
    if provider is None:
        raise ChapterContentError("R2-4缺少真实provider来源。")
    bundle = _build_offline_recovered_bundle(
        manifest,
        responses=responses,
        provider=provider,
        source_attempt_id=P9_5_REAL_ATTEMPT_ID,
        reconciliation_sha256=P9_5_RECONCILIATION_SHA256,
        candidate_manifest_sha256=P9_5_OFFLINE_CANDIDATE_HASHES["manifest.json"],
    )
    content_sha256 = _canonical_sha256(bundle)
    result_id = f"content-{content_sha256[:12]}"
    execution_report = {
        "schema_version": "1.0",
        "recovery_version": OFFLINE_FORMAL_RECOVERY_VERSION,
        "task_id": task_id,
        "status": "success",
        "mode": "real-windowed-offline-recovery",
        "result_id": result_id,
        "content_sha256": content_sha256,
        "source_stage8_result_id": manifest["source_chapter_plan"]["result_id"],
        "input_manifest_cache_key": manifest["cache_key"],
        "source_attempt_id": P9_5_REAL_ATTEMPT_ID,
        "reconciliation_sha256": P9_5_RECONCILIATION_SHA256,
        "candidate_manifest_sha256": P9_5_OFFLINE_CANDIDATE_HASHES["manifest.json"],
        "chapter_count": 5,
        "successful_llm_call_artifact_count": 4,
        "offline_candidate_count": 1,
        "historical_supplier_request_count": 11,
        "recovery_api_request_count": 0,
    }
    group, group_cache_hit = _publish_content_result_group(
        task_root,
        bundle=bundle,
        execution_report=execution_report,
        result_directory="formal-results",
        description="正式",
    )
    top_content = task_root / "content" / "content.json"
    top_report = task_root / "content" / "content-execution-report.json"
    content_cache_hit = _write_json_atomic(top_content, bundle)
    report_cache_hit = _write_json_atomic(top_report, execution_report)
    return OfflineFormalRecoveryResult(
        result_group_path=group,
        content_path=group / "content.json",
        execution_report_path=group / "execution-report.json",
        top_level_content_path=top_content,
        top_level_report_path=top_report,
        result_id=result_id,
        chapter_count=5,
        result_group_cache_hit=group_cache_hit,
        top_level_content_cache_hit=content_cache_hit,
        top_level_report_cache_hit=report_cache_hit,
    )


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chapter_text_map(bundle: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    chapters = bundle.get("chapters")
    if not isinstance(chapters, list):
        raise ChapterContentError("R3-2源内容缺少章节数组。")
    for chapter in chapters:
        if not isinstance(chapter, dict) or not isinstance(chapter.get("chapter_id"), str):
            raise ChapterContentError("R3-2源章节结构无效。")
        chapter_id = chapter["chapter_id"]
        for field in ("title", "summary"):
            value = chapter.get(field)
            if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                raise ChapterContentError(f"R3-2源章节{field}无效。")
            result[f"chapters[{chapter_id}].{field}.text"] = value["text"]
        for field in ("key_points", "keywords"):
            items = chapter.get(field)
            if not isinstance(items, list):
                raise ChapterContentError(f"R3-2源章节{field}无效。")
            for index, item in enumerate(items):
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    raise ChapterContentError(f"R3-2源章节{field}[{index}]无效。")
                result[f"chapters[{chapter_id}].{field}[{index}].text"] = item["text"]
    return result


def _validate_semantic_repair_diff(
    source_bundle: dict[str, Any],
    repaired_bundle: dict[str, Any],
    *,
    target_chapter_id: str,
    target_key_point_index: int,
    before_text: str,
) -> list[str]:
    target_path = (
        f"chapters[{target_chapter_id}].key_points[{target_key_point_index}].text"
    )
    source_texts = _chapter_text_map(source_bundle)
    repaired_texts = _chapter_text_map(repaired_bundle)
    changed_paths = [
        path for path in source_texts if source_texts[path] != repaired_texts.get(path)
    ]
    if changed_paths != [target_path] or set(source_texts) != set(repaired_texts):
        raise ChapterContentError("R3-2正文变化超出唯一字段白名单。")
    invariant_copy = deepcopy(repaired_bundle)
    invariant_copy["generation"] = deepcopy(source_bundle["generation"])
    invariant_chapter = next(
        (
            chapter
            for chapter in invariant_copy["chapters"]
            if chapter["chapter_id"] == target_chapter_id
        ),
        None,
    )
    if invariant_chapter is None:
        raise ChapterContentError("R3-2候选缺少目标章节。")
    invariant_chapter["key_points"][target_key_point_index]["text"] = before_text
    if invariant_copy != source_bundle:
        raise ChapterContentError("R3-2候选存在未声明的正文或结构变化。")
    return changed_paths


def _publish_semantic_repair_candidate(
    task_root: Path,
    *,
    repair_id: str,
    candidate: dict[str, Any],
    repair_audit: dict[str, Any],
) -> tuple[Path, bool]:
    group = task_root / "content" / "semantic-repairs" / repair_id
    candidate_path = group / "candidate.json"
    audit_path = group / "repair-audit.json"
    manifest_path = group / "manifest.json"
    if group.exists():
        names = {path.name for path in group.iterdir() if path.is_file()}
        manifest = _read_json(manifest_path, "R3-2候选manifest")
        files = manifest.get("files")
        if (
            names != {"candidate.json", "repair-audit.json", "manifest.json"}
            or manifest.get("schema_version") != "1.0"
            or manifest.get("repair_id") != repair_id
            or manifest.get("task_id") != task_root.name
            or not isinstance(files, dict)
            or files.get("candidate.json") != _file_sha256(candidate_path)
            or files.get("repair-audit.json") != _file_sha256(audit_path)
            or _read_json(candidate_path, "R3-2候选") != candidate
            or _read_json(audit_path, "R3-2修复审计") != repair_audit
        ):
            raise ChapterContentError("R3-2候选结果组损坏或发生内容混用。")
        return group, True

    temporary = group.with_name(f"{group.name}.tmp")
    if temporary.exists():
        raise ChapterContentError("R3-2存在未清理的候选临时目录。")
    try:
        temporary.mkdir(parents=True, exist_ok=False)
        temporary_candidate = temporary / "candidate.json"
        temporary_audit = temporary / "repair-audit.json"
        _write_json_atomic(temporary_candidate, candidate)
        _write_json_atomic(temporary_audit, repair_audit)
        _write_json_atomic(
            temporary / "manifest.json",
            {
                "schema_version": "1.0",
                "repair_version": SEMANTIC_REPAIR_VERSION,
                "repair_id": repair_id,
                "task_id": task_root.name,
                "files": {
                    "candidate.json": _file_sha256(temporary_candidate),
                    "repair-audit.json": _file_sha256(temporary_audit),
                },
            },
        )
        temporary.replace(group)
    except OSError as exc:
        raise ChapterContentError("无法原子发布R3-2语义修复候选组。") from exc
    finally:
        if temporary.exists():
            try:
                shutil.rmtree(temporary)
            except OSError:
                pass
    return group, False


def _prepare_semantic_repair_candidate(
    output_root: str | Path,
    task_id: str,
    *,
    source_result_id: str,
    source_hashes: Mapping[str, str],
    evidence_audit_path: Path,
    evidence_audit_sha256: str,
    target_chapter_id: str,
    target_key_point_index: int,
    before_text: str,
    after_text: str,
    evidence_ids: list[str],
    required_evidence_text: Mapping[str, str],
) -> SemanticRepairCandidateResult:
    task_root = Path(output_root).expanduser() / task_id
    source_group = task_root / "content" / "formal-results" / source_result_id
    _verify_frozen_group(
        source_group,
        expected_hashes=source_hashes,
        description="R3-2源正式结果组",
    )
    if _file_sha256(evidence_audit_path) != evidence_audit_sha256:
        raise ChapterContentError("R3-2人工语义验收文档哈希不一致。")

    source_bundle = _read_json(source_group / "content.json", "R3-2源正式内容")
    source_report = _read_json(
        source_group / "execution-report.json", "R3-2源正式执行报告"
    )
    source_manifest = _read_json(source_group / "manifest.json", "R3-2源manifest")
    if (
        source_report.get("result_id") != source_result_id
        or source_report.get("status") != "success"
        or source_manifest.get("result_id") != source_result_id
        or source_bundle.get("task_id") != task_id
    ):
        raise ChapterContentError("R3-2源正式结果身份或状态不一致。")

    preparation = prepare_task_chapter_inputs(output_root, task_id)
    input_manifest = _read_json(preparation.manifest_path, "R3-2章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterContentError(str(exc)) from exc
    validate_chapter_input_manifest(input_manifest, timeline=timeline)
    validate_chapter_content_bundle(source_bundle, input_manifest=input_manifest)

    chapters = source_bundle.get("chapters")
    matches = [
        chapter
        for chapter in chapters
        if isinstance(chapter, dict) and chapter.get("chapter_id") == target_chapter_id
    ]
    if len(matches) != 1:
        raise ChapterContentError("R3-2无法唯一定位目标章节。")
    source_chapter = matches[0]
    key_points = source_chapter.get("key_points")
    if (
        not isinstance(target_key_point_index, int)
        or target_key_point_index < 0
        or not isinstance(key_points, list)
        or target_key_point_index >= len(key_points)
        or not isinstance(key_points[target_key_point_index], dict)
    ):
        raise ChapterContentError("R3-2目标知识点位置无效。")
    source_item = key_points[target_key_point_index]
    if source_item.get("text") != before_text:
        raise ChapterContentError("R3-2目标修改前全文不一致。")
    if source_item.get("source_event_ids") != evidence_ids:
        raise ChapterContentError("R3-2目标证据ID或顺序不一致。")
    if not after_text or after_text == before_text:
        raise ChapterContentError("R3-2目标修改后全文无效。")

    timeline_events = {str(event.get("id")): event for event in timeline["events"]}
    evidence_events: list[dict[str, Any]] = []
    for event_id in evidence_ids:
        event = timeline_events.get(event_id)
        if (
            not isinstance(event, dict)
            or event.get("text") != required_evidence_text.get(event_id)
        ):
            raise ChapterContentError(f"R3-2冻结证据事件不一致：{event_id}")
        evidence_events.append(
            {
                "id": event_id,
                "start": event["start"],
                "end": event["end"],
                "text": event["text"],
            }
        )

    repaired_bundle = deepcopy(source_bundle)
    repaired_matches = [
        chapter
        for chapter in repaired_bundle["chapters"]
        if chapter["chapter_id"] == target_chapter_id
    ]
    repaired_matches[0]["key_points"][target_key_point_index]["text"] = after_text
    source_generation = deepcopy(source_bundle["generation"])
    repaired_bundle["generation"] = {
        **source_generation,
        "mode": "real-windowed-offline-recovery-with-semantic-repair",
        "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
        "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
        "source_validator_version": source_generation.get("validator_version"),
        "semantic_repair": {
            "repair_version": SEMANTIC_REPAIR_VERSION,
            "source_content_result_id": source_result_id,
            "target_path": (
                f"chapters[{target_chapter_id}].key_points[{target_key_point_index}].text"
            ),
            "api_request_count": 0,
        },
    }
    validate_chapter_content_bundle(repaired_bundle, input_manifest=input_manifest)

    target_path = (
        f"chapters[{target_chapter_id}].key_points[{target_key_point_index}].text"
    )
    changed_paths = _validate_semantic_repair_diff(
        source_bundle,
        repaired_bundle,
        target_chapter_id=target_chapter_id,
        target_key_point_index=target_key_point_index,
        before_text=before_text,
    )

    source_group_relative = str(source_group.relative_to(task_root).as_posix())
    audit_relative = P9_5_R3_EVIDENCE_AUDIT_RELATIVE_PATH
    repair_seed = {
        "repair_version": SEMANTIC_REPAIR_VERSION,
        "task_id": task_id,
        "source_result_id": source_result_id,
        "source_hashes": dict(source_hashes),
        "evidence_audit_sha256": evidence_audit_sha256,
        "target_path": target_path,
        "before_text_sha256": _text_sha256(before_text),
        "after_text_sha256": _text_sha256(after_text),
        "evidence_ids": evidence_ids,
        "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
        "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
    }
    repair_id = f"semantic-repair-{_canonical_sha256(repair_seed)[:12]}"
    repaired_content_sha256 = _canonical_sha256(repaired_bundle)
    candidate = {
        "schema_version": "1.0",
        "repair_version": SEMANTIC_REPAIR_VERSION,
        "repair_id": repair_id,
        "task_id": task_id,
        "source_content_result_id": source_result_id,
        "source_content_sha256": source_hashes["content.json"],
        "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
        "repaired_content_sha256": repaired_content_sha256,
        "content": repaired_bundle,
    }
    repair_audit = {
        "schema_version": "1.0",
        "repair_version": SEMANTIC_REPAIR_VERSION,
        "repair_id": repair_id,
        "task_id": task_id,
        "status": "candidate",
        "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
        "source": {
            "result_id": source_result_id,
            "result_group_path": source_group_relative,
            "files": dict(source_hashes),
            "evidence_audit_path": audit_relative,
            "evidence_audit_sha256": evidence_audit_sha256,
        },
        "correction": {
            "target_path": target_path,
            "before_text": before_text,
            "before_text_sha256": _text_sha256(before_text),
            "after_text": after_text,
            "after_text_sha256": _text_sha256(after_text),
            "source_event_ids": evidence_ids,
            "evidence_events": evidence_events,
        },
        "invariants": {
            "changed_text_paths": changed_paths,
            "changed_text_path_count": len(changed_paths),
            "source_event_ids_unchanged": True,
            "item_order_unchanged": True,
            "chapter_boundaries_unchanged": True,
            "all_other_content_unchanged": True,
            "stage8_provenance_unchanged": (
                repaired_bundle["chaptering_provenance"]
                == source_bundle["chaptering_provenance"]
            ),
            "api_request_count": 0,
        },
        "repaired_content_sha256": repaired_content_sha256,
    }
    group, cache_hit = _publish_semantic_repair_candidate(
        task_root,
        repair_id=repair_id,
        candidate=candidate,
        repair_audit=repair_audit,
    )
    return SemanticRepairCandidateResult(
        candidate_path=group / "candidate.json",
        repair_audit_path=group / "repair-audit.json",
        manifest_path=group / "manifest.json",
        repair_path=group,
        changed_text_path_count=1,
        api_request_count=0,
        cache_hit=cache_hit,
    )


def prepare_task_chapter_content_semantic_repair_candidate(
    output_root: str | Path,
    task_id: str,
) -> SemanticRepairCandidateResult:
    """R3-2固定DEV01单字段语义修复候选；不发布正式结果。"""

    if task_id != P9_5_TASK_ID:
        raise ChapterContentError("R3-2只允许任务DEV01。")
    project_root = Path(__file__).resolve().parent.parent
    return _prepare_semantic_repair_candidate(
        output_root,
        task_id,
        source_result_id=P9_5_R3_SOURCE_CONTENT_RESULT_ID,
        source_hashes=P9_5_R3_SOURCE_HASHES,
        evidence_audit_path=project_root / P9_5_R3_EVIDENCE_AUDIT_RELATIVE_PATH,
        evidence_audit_sha256=P9_5_R3_EVIDENCE_AUDIT_SHA256,
        target_chapter_id=P9_5_R3_TARGET_CHAPTER_ID,
        target_key_point_index=P9_5_R3_TARGET_KEY_POINT_INDEX,
        before_text=P9_5_R3_BEFORE_TEXT,
        after_text=P9_5_R3_AFTER_TEXT,
        evidence_ids=P9_5_R3_EVIDENCE_IDS,
        required_evidence_text=P9_5_R3_REQUIRED_EVIDENCE_TEXT,
    )


def _classify_semantic_repair_aliases(
    task_root: Path,
    *,
    source_bundle: dict[str, Any],
    source_report: dict[str, Any],
    repaired_bundle: dict[str, Any],
    repaired_report: dict[str, Any],
) -> str:
    content_path = task_root / "content" / "content.json"
    report_path = task_root / "content" / "content-execution-report.json"
    current_content = _read_json(content_path, "R3-3顶层内容别名")
    current_report = _read_json(report_path, "R3-3顶层执行报告别名")
    if current_content == repaired_bundle and current_report == repaired_report:
        return "repaired"
    if current_content == source_bundle and current_report == source_report:
        return "source"
    raise ChapterContentError("R3-3顶层别名与冻结源组或目标新组不一致。")


def _switch_semantic_repair_aliases(
    task_root: Path,
    *,
    source_bundle: dict[str, Any],
    source_report: dict[str, Any],
    repaired_bundle: dict[str, Any],
    repaired_report: dict[str, Any],
    alias_state: str,
) -> tuple[bool, bool]:
    if alias_state == "repaired":
        return True, True
    if alias_state != "source":
        raise ChapterContentError("R3-3顶层别名状态无效。")
    content_path = task_root / "content" / "content.json"
    report_path = task_root / "content" / "content-execution-report.json"
    content_written = False
    try:
        content_cache_hit = _write_json_atomic(content_path, repaired_bundle)
        content_written = not content_cache_hit
        report_cache_hit = _write_json_atomic(report_path, repaired_report)
    except ChapterContentError:
        if content_written:
            _write_json_atomic(content_path, source_bundle)
        _write_json_atomic(report_path, source_report)
        raise
    if (
        _read_json(content_path, "R3-3切换后顶层内容别名") != repaired_bundle
        or _read_json(report_path, "R3-3切换后顶层报告别名") != repaired_report
    ):
        _write_json_atomic(content_path, source_bundle)
        _write_json_atomic(report_path, source_report)
        raise ChapterContentError("R3-3顶层别名切换后复验失败，已恢复冻结源别名。")
    return content_cache_hit, report_cache_hit


def _publish_semantic_repair_formal_result(
    output_root: str | Path,
    task_id: str,
    *,
    source_result_id: str,
    source_hashes: Mapping[str, str],
    repair_relative_path: str,
    repair_hashes: Mapping[str, str],
    evidence_audit_path: Path,
    evidence_audit_sha256: str,
    target_chapter_id: str,
    target_key_point_index: int,
    before_text: str,
    after_text: str,
    evidence_ids: list[str],
    required_evidence_text: Mapping[str, str],
) -> SemanticRepairFormalResult:
    task_root = Path(output_root).expanduser() / task_id
    candidate_result = _prepare_semantic_repair_candidate(
        output_root,
        task_id,
        source_result_id=source_result_id,
        source_hashes=source_hashes,
        evidence_audit_path=evidence_audit_path,
        evidence_audit_sha256=evidence_audit_sha256,
        target_chapter_id=target_chapter_id,
        target_key_point_index=target_key_point_index,
        before_text=before_text,
        after_text=after_text,
        evidence_ids=evidence_ids,
        required_evidence_text=required_evidence_text,
    )
    repair_group = task_root / repair_relative_path
    if candidate_result.repair_path.resolve() != repair_group.resolve():
        raise ChapterContentError("R3-3候选目录与冻结路径不一致。")
    _verify_frozen_group(
        repair_group,
        expected_hashes=repair_hashes,
        description="R3-3语义修复候选组",
    )

    candidate = _read_json(repair_group / "candidate.json", "R3-3语义修复候选")
    audit = _read_json(repair_group / "repair-audit.json", "R3-3语义修复审计")
    repair_manifest = _read_json(repair_group / "manifest.json", "R3-3候选manifest")
    repaired_bundle = candidate.get("content")
    repair_id = candidate.get("repair_id")
    repaired_content_sha256 = candidate.get("repaired_content_sha256")
    expected_target_path = (
        f"chapters[{target_chapter_id}].key_points[{target_key_point_index}].text"
    )
    if (
        candidate.get("schema_version") != "1.0"
        or candidate.get("repair_version") != SEMANTIC_REPAIR_VERSION
        or candidate.get("task_id") != task_id
        or candidate.get("source_content_result_id") != source_result_id
        or candidate.get("source_content_sha256") != source_hashes["content.json"]
        or candidate.get("output_language") != CHAPTER_CONTENT_OUTPUT_LANGUAGE
        or not isinstance(repaired_bundle, dict)
        or not isinstance(repair_id, str)
        or repair_manifest.get("repair_id") != repair_id
        or repair_manifest.get("repair_version") != SEMANTIC_REPAIR_VERSION
        or audit.get("repair_id") != repair_id
        or audit.get("status") != "candidate"
        or audit.get("output_language") != CHAPTER_CONTENT_OUTPUT_LANGUAGE
        or audit.get("correction", {}).get("target_path") != expected_target_path
        or audit.get("correction", {}).get("before_text") != before_text
        or audit.get("correction", {}).get("after_text") != after_text
        or audit.get("correction", {}).get("source_event_ids") != evidence_ids
        or repaired_content_sha256 != _canonical_sha256(repaired_bundle)
        or audit.get("repaired_content_sha256") != repaired_content_sha256
    ):
        raise ChapterContentError("R3-3候选身份、修复规格或内容哈希无效。")
    expected_invariants = {
        "changed_text_paths": [expected_target_path],
        "changed_text_path_count": 1,
        "source_event_ids_unchanged": True,
        "item_order_unchanged": True,
        "chapter_boundaries_unchanged": True,
        "all_other_content_unchanged": True,
        "stage8_provenance_unchanged": True,
        "api_request_count": 0,
    }
    if audit.get("invariants") != expected_invariants:
        raise ChapterContentError("R3-3候选不满足唯一差异与0 API不变量。")

    source_group = task_root / "content" / "formal-results" / source_result_id
    _verify_frozen_group(
        source_group,
        expected_hashes=source_hashes,
        description="R3-3源正式结果组",
    )
    if _file_sha256(evidence_audit_path) != evidence_audit_sha256:
        raise ChapterContentError("R3-3人工语义验收文档哈希不一致。")
    source_bundle = _read_json(source_group / "content.json", "R3-3源正式内容")
    source_report = _read_json(
        source_group / "execution-report.json", "R3-3源正式执行报告"
    )
    preparation = prepare_task_chapter_inputs(output_root, task_id)
    input_manifest = _read_json(preparation.manifest_path, "R3-3章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterContentError(str(exc)) from exc
    validate_chapter_input_manifest(input_manifest, timeline=timeline)
    validate_chapter_content_bundle(repaired_bundle, input_manifest=input_manifest)
    changed_paths = _validate_semantic_repair_diff(
        source_bundle,
        repaired_bundle,
        target_chapter_id=target_chapter_id,
        target_key_point_index=target_key_point_index,
        before_text=before_text,
    )
    if changed_paths != [expected_target_path]:
        raise ChapterContentError("R3-3候选正文变化与冻结白名单不一致。")
    target_chapter = next(
        chapter
        for chapter in repaired_bundle["chapters"]
        if chapter["chapter_id"] == target_chapter_id
    )
    target_item = target_chapter["key_points"][target_key_point_index]
    if target_item != {"text": after_text, "source_event_ids": evidence_ids}:
        raise ChapterContentError("R3-3目标知识点正文或证据ID不一致。")

    result_id = f"content-{repaired_content_sha256[:12]}"
    if result_id == source_result_id:
        raise ChapterContentError("R3-3新结果ID不得沿用源结果ID。")
    execution_report = {
        "schema_version": "1.0",
        "repair_version": SEMANTIC_REPAIR_VERSION,
        "task_id": task_id,
        "status": "success",
        "mode": "real-windowed-offline-recovery-with-semantic-repair",
        "result_id": result_id,
        "content_sha256": repaired_content_sha256,
        "source_content_result_id": source_result_id,
        "source_content_hashes": dict(source_hashes),
        "semantic_repair_id": repair_id,
        "semantic_repair_candidate_sha256": repair_hashes["candidate.json"],
        "semantic_repair_audit_sha256": repair_hashes["repair-audit.json"],
        "semantic_repair_manifest_sha256": repair_hashes["manifest.json"],
        "source_stage8_result_id": input_manifest["source_chapter_plan"]["result_id"],
        "input_manifest_cache_key": input_manifest["cache_key"],
        "chapter_count": len(repaired_bundle["chapters"]),
        "historical_supplier_request_count": source_report.get(
            "historical_supplier_request_count"
        ),
        "repair_api_request_count": 0,
    }
    alias_state = _classify_semantic_repair_aliases(
        task_root,
        source_bundle=source_bundle,
        source_report=source_report,
        repaired_bundle=repaired_bundle,
        repaired_report=execution_report,
    )
    group, group_cache_hit = _publish_content_result_group(
        task_root,
        bundle=repaired_bundle,
        execution_report=execution_report,
        result_directory="formal-results",
        description="R3-3正式语义修复",
    )
    try:
        content_cache_hit, report_cache_hit = _switch_semantic_repair_aliases(
            task_root,
            source_bundle=source_bundle,
            source_report=source_report,
            repaired_bundle=repaired_bundle,
            repaired_report=execution_report,
            alias_state=alias_state,
        )
    except ChapterContentError:
        if not group_cache_hit and group.is_dir():
            try:
                shutil.rmtree(group)
            except OSError:
                pass
        raise
    return SemanticRepairFormalResult(
        result_group_path=group,
        content_path=group / "content.json",
        execution_report_path=group / "execution-report.json",
        top_level_content_path=task_root / "content" / "content.json",
        top_level_report_path=(
            task_root / "content" / "content-execution-report.json"
        ),
        result_id=result_id,
        chapter_count=len(repaired_bundle["chapters"]),
        api_request_count=0,
        result_group_cache_hit=group_cache_hit,
        top_level_content_cache_hit=content_cache_hit,
        top_level_report_cache_hit=report_cache_hit,
    )


def publish_task_chapter_content_semantic_repair(
    output_root: str | Path,
    task_id: str,
) -> SemanticRepairFormalResult:
    """R3-3复验冻结候选并离线发布新正式结果；不调用LLM。"""

    if task_id != P9_5_TASK_ID:
        raise ChapterContentError("R3-3只允许任务DEV01。")
    project_root = Path(__file__).resolve().parent.parent
    return _publish_semantic_repair_formal_result(
        output_root,
        task_id,
        source_result_id=P9_5_R3_SOURCE_CONTENT_RESULT_ID,
        source_hashes=P9_5_R3_SOURCE_HASHES,
        repair_relative_path=P9_5_R3_REPAIR_RELATIVE_PATH,
        repair_hashes=P9_5_R3_REPAIR_HASHES,
        evidence_audit_path=project_root / P9_5_R3_EVIDENCE_AUDIT_RELATIVE_PATH,
        evidence_audit_sha256=P9_5_R3_EVIDENCE_AUDIT_SHA256,
        target_chapter_id=P9_5_R3_TARGET_CHAPTER_ID,
        target_key_point_index=P9_5_R3_TARGET_KEY_POINT_INDEX,
        before_text=P9_5_R3_BEFORE_TEXT,
        after_text=P9_5_R3_AFTER_TEXT,
        evidence_ids=P9_5_R3_EVIDENCE_IDS,
        required_evidence_text=P9_5_R3_REQUIRED_EVIDENCE_TEXT,
    )


def _chapter_prompt_events(
    timeline: dict[str, Any],
    chapter: dict[str, Any],
) -> list[dict[str, Any]]:
    events_by_id = {str(event["id"]): event for event in timeline["events"]}
    try:
        return [
            {
                "id": events_by_id[event_id]["id"],
                "start": events_by_id[event_id]["start"],
                "end": events_by_id[event_id]["end"],
                "text": events_by_id[event_id]["text"],
            }
            for event_id in chapter["event_ids"]
        ]
    except KeyError as exc:
        raise ChapterContentError("章节输入manifest包含未知时间轴事件ID。") from exc


def _build_window_messages(
    chapter: dict[str, Any],
    prompt_events: list[dict[str, Any]],
) -> list[dict[str, str]]:
    input_payload = {
        "chapter_id": chapter["chapter_id"],
        "planned_title": chapter["planned_title"],
        "events": prompt_events,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是知识型视频章节内容整理器。只能依据本次提供的章内ASR事件，"
                "不得补充外部知识、不得评价或修改章节边界。只返回JSON对象。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"提示词版本：{CHAPTER_CONTENT_PROMPT_VERSION}。"
                f"内容政策：{CHAPTER_CONTENT_POLICY_VERSION}。\n"
                "固定使用简体中文生成最终title、summary和每条key_points。"
                "keywords原则上使用简体中文；人名、机构名、缩写或专有名词可保留必要拉丁字母。"
                "输入ASR或planned_title的语言不得改变输出语言。"
                "生成简洁标题、摘要、3至6条知识点和3至8个关键词。"
                "title、summary、每条key_points和每个keywords都必须包含"
                "非空source_event_ids；ID只能取自输入events并按原顺序排列。"
                "不得输出时间范围或其他字段。返回结构为"
                '{"title":{"text":"...","source_event_ids":["a0001"]},'
                '"summary":{"text":"...","source_event_ids":["a0001"]},'
                '"key_points":[{"text":"...","source_event_ids":["a0001"]}],'
                '"keywords":[{"text":"...","source_event_ids":["a0001"]}]}。\n'
                "CHAPTER_INPUT_JSON="
                + json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


class DeterministicChapterMockTransport:
    """从章内提示词生成确定性本地响应，仅用于P9-2离线验收。"""

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        del config, response_format
        content = str(messages[-1]["content"])
        merge_marker = "CHAPTER_MERGE_INPUT_JSON="
        if merge_marker in content:
            try:
                merge_input = json.loads(content.split(merge_marker, 1)[1])
                windows = merge_input["windows"]
                title = str(merge_input["planned_title"]).strip()
                if not isinstance(windows, list) or not windows:
                    raise ValueError("windows为空")
                evidence_by_window = [
                    list(window["source_event_ids"]) for window in windows
                ]
                if any(not ids for ids in evidence_by_window):
                    raise ValueError("窗口证据为空")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LLMRetryableError("离线mock无法解析章节合并输入。") from exc
            ordered_ids: list[str] = []
            for ids in evidence_by_window:
                for event_id in ids:
                    if event_id not in ordered_ids:
                        ordered_ids.append(event_id)
            chosen_ids = [
                ordered_ids[0],
                ordered_ids[len(ordered_ids) // 2],
                ordered_ids[-1],
            ]
            return {
                "title": {
                    "text": f"离线mock标题：{title}",
                    "source_event_ids": [ordered_ids[0]],
                },
                "summary": {
                    "text": "离线mock多窗合并摘要",
                    "source_event_ids": [ids[0] for ids in evidence_by_window],
                },
                "key_points": [
                    {
                        "text": f"离线mock合并知识点{index}",
                        "source_event_ids": [event_id],
                    }
                    for index, event_id in enumerate(chosen_ids, start=1)
                ],
                "keywords": [
                    {
                        "text": f"mock-merged-keyword-{index}",
                        "source_event_ids": [event_id],
                    }
                    for index, event_id in enumerate(chosen_ids, start=1)
                ],
            }

        marker = "CHAPTER_INPUT_JSON="
        if marker not in content:
            raise LLMRetryableError("离线mock提示词缺少章节输入。")
        try:
            chapter_input = json.loads(content.split(marker, 1)[1])
            events = chapter_input["events"]
            title = str(chapter_input["planned_title"]).strip()
            if not isinstance(events, list) or not events:
                raise ValueError("events为空")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMRetryableError("离线mock无法解析章节输入。") from exc
        chosen = [events[0], events[len(events) // 2], events[-1]]
        return {
            "title": {
                "text": f"离线mock标题：{title}",
                "source_event_ids": [events[0]["id"]],
            },
            "summary": {
                "text": f"离线mock摘要：{events[0]['text']}",
                "source_event_ids": [events[0]["id"], events[-1]["id"]]
                if len(events) > 1
                else [events[0]["id"]],
            },
            "key_points": [
                {
                    "text": f"离线mock知识点{index}：{event['text']}",
                    "source_event_ids": [event["id"]],
                }
                for index, event in enumerate(chosen, start=1)
            ],
            "keywords": [
                {
                    "text": f"mock-keyword-{index}",
                    "source_event_ids": [event["id"]],
                }
                for index, event in enumerate(chosen, start=1)
            ],
        }


def split_chapter_events(
    events: list[dict[str, Any]],
    *,
    max_window_chars: int = MAX_WINDOW_CHARS,
    max_window_events: int = MAX_WINDOW_EVENTS,
) -> list[dict[str, Any]]:
    """按完整事件确定性分窗，不重叠、不截断事件。"""

    if not isinstance(max_window_chars, int) or max_window_chars < 1:
        raise ChapterContentError("max_window_chars必须是正整数。")
    if not isinstance(max_window_events, int) or max_window_events < 1:
        raise ChapterContentError("max_window_events必须是正整数。")
    if not isinstance(events, list) or not events:
        raise ChapterContentError("章节分窗输入必须包含至少一个事件。")
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        candidate = current + [event]
        candidate_chars = len(
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        )
        if current and (
            len(candidate) > max_window_events or candidate_chars > max_window_chars
        ):
            windows.append(current)
            current = [event]
        else:
            current = candidate
    if current:
        windows.append(current)

    result: list[dict[str, Any]] = []
    flattened_ids: list[str] = []
    for index, window_events in enumerate(windows, start=1):
        serialized = json.dumps(
            window_events, ensure_ascii=False, separators=(",", ":")
        )
        event_ids = [str(event["id"]) for event in window_events]
        flattened_ids.extend(event_ids)
        result.append(
            {
                "window_index": index,
                "event_count": len(window_events),
                "event_ids": event_ids,
                "input_sha256": _canonical_sha256(window_events),
                "serialized_chars": len(serialized),
                "oversize_single_event": (
                    len(window_events) == 1 and len(serialized) > max_window_chars
                ),
                "events": window_events,
            }
        )
    expected_ids = [str(event["id"]) for event in events]
    if flattened_ids != expected_ids or len(set(flattened_ids)) != len(flattened_ids):
        raise ChapterContentError("确定性分窗发生事件遗漏、重复或乱序。")
    return result


def _build_merge_messages(
    chapter: dict[str, Any],
    window_results: list[dict[str, Any]],
) -> list[dict[str, str]]:
    merge_input = {
        "chapter_id": chapter["chapter_id"],
        "planned_title": chapter["planned_title"],
        "windows": window_results,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是知识型视频章节内容合并器。只能合并本章已校验的窗口结果，"
                "不得补充外部知识、不得读取其他章节、不得修改章节边界。只返回JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"合并提示词版本：{CHAPTER_CONTENT_MERGE_PROMPT_VERSION}。"
                f"内容政策：{CHAPTER_CONTENT_POLICY_VERSION}。\n"
                "固定使用简体中文生成最终title、summary和每条key_points。"
                "keywords原则上使用简体中文；人名、机构名、缩写或专有名词可保留必要拉丁字母。"
                "各窗口或planned_title的语言不得改变输出语言。"
                "按窗口顺序去重并生成一个标题、摘要、3至6条知识点和3至8个关键词。"
                "引用ID只能来自各窗口source_event_ids的并集，并且最终内容至少引用每个"
                "窗口的一条证据。返回字段和单窗结构完全相同，不得输出其他字段。\n"
                "CHAPTER_MERGE_INPUT_JSON="
                + json.dumps(merge_input, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


def _normalize_merge_response(
    response: dict[str, Any],
    *,
    expected_chapter_id: str,
    chapter_event_ids: list[str],
    window_results: list[dict[str, Any]],
    generic_shape_compat: bool = False,
) -> dict[str, Any]:
    extras = set(response) - {"title", "summary", "key_points", "keywords"}
    if extras - {"chapter_id", "source_event_ids"}:
        raise ChapterContentError("窗口响应包含未知额外字段。")
    if "chapter_id" in response and response["chapter_id"] != expected_chapter_id:
        raise ChapterContentError("合并响应chapter_id与当前章节不一致。")
    core_response = {
        field: response[field]
        for field in ("title", "summary", "key_points", "keywords")
        if field in response
    }
    allowed_set = {
        event_id
        for window in window_results
        for event_id in window["source_event_ids"]
    }
    allowed_ids = [event_id for event_id in chapter_event_ids if event_id in allowed_set]
    if generic_shape_compat:
        core_response = _normalize_generic_shape_variants(
            core_response, allowed_ids=allowed_ids
        )
    normalized = _normalize_window_response(core_response, allowed_ids=allowed_ids)
    merged_ids = set(normalized["source_event_ids"])
    for index, window in enumerate(window_results, start=1):
        if not merged_ids.intersection(window["source_event_ids"]):
            raise ChapterContentError(f"合并结果未引用第{index}个窗口的任何证据。")
    return normalized


def validate_chapter_input_manifest(
    payload: Any,
    *,
    timeline: dict[str, Any] | None = None,
) -> None:
    """校验离线章节输入manifest及事件唯一归属。"""

    if not isinstance(payload, dict):
        raise ChapterContentError("章节输入manifest必须是JSON对象。")
    if payload.get("schema_version") != CHAPTER_INPUT_SCHEMA_VERSION:
        raise ChapterContentError("章节输入manifest Schema版本无效。")
    task_id = payload.get("task_id")
    strategy = payload.get("strategy")
    if not isinstance(task_id, str) or not isinstance(strategy, str) or not strategy:
        raise ChapterContentError("章节输入manifest任务或策略无效。")
    source_plan = payload.get("source_chapter_plan")
    source_timeline = payload.get("source_timeline")
    provenance = payload.get("chaptering_provenance")
    ownership = payload.get("ownership")
    if not isinstance(source_plan, dict) or source_plan.get("schema_version") != "1.0":
        raise ChapterContentError("章节输入manifest缺少有效ChapterPlan来源。")
    _valid_sha256(source_plan.get("sha256"), "ChapterPlan来源哈希")
    if not isinstance(source_plan.get("result_id"), str):
        raise ChapterContentError("章节输入manifest结果ID无效。")
    if not isinstance(source_timeline, dict) or source_timeline.get("schema_version") != "1.0":
        raise ChapterContentError("章节输入manifest缺少有效时间轴来源。")
    _valid_sha256(source_timeline.get("sha256"), "时间轴来源哈希")
    event_count = source_timeline.get("event_count")
    if not isinstance(event_count, int) or event_count < 1:
        raise ChapterContentError("章节输入manifest事件数无效。")
    if not isinstance(provenance, dict) or provenance.get("status") != "success":
        raise ChapterContentError("章节输入manifest分章来源状态无效。")
    if _PROVENANCE_PAIRS.get(provenance.get("result_source")) != provenance.get(
        "quality_level"
    ):
        raise ChapterContentError("章节输入manifest来源与质量级别不一致。")
    _valid_sha256(provenance.get("execution_report_sha256"), "执行报告来源哈希")
    if ownership != {
        "policy_version": CHAPTER_INPUT_OWNERSHIP_POLICY,
        "rule": "chapter.start <= event.start < chapter.end",
        "assigned_event_count": event_count,
        "duplicate_event_count": 0,
        "unassigned_event_count": 0,
    }:
        raise ChapterContentError("章节输入manifest事件归属审计无效。")

    chapters = payload.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ChapterContentError("章节输入manifest必须包含非空章节数组。")
    all_event_ids: list[str] = []
    previous_end: float | None = None
    timeline_by_id = (
        {str(event["id"]): event for event in timeline["events"]}
        if timeline is not None
        else None
    )
    for index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict):
            raise ChapterContentError(f"输入章节{index}必须是JSON对象。")
        if chapter.get("chapter_id") != f"c{index:04d}" or chapter.get("index") != index:
            raise ChapterContentError("输入章节ID或序号不连续。")
        start = chapter.get("start")
        end = chapter.get("end")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(end) <= float(start)
        ):
            raise ChapterContentError(f"输入章节{index}时间范围无效。")
        if index == 1 and float(start) != 0.0:
            raise ChapterContentError("首个输入章节必须从0.0秒开始。")
        if previous_end is not None and float(start) != previous_end:
            raise ChapterContentError("输入章节区间不连续。")
        previous_end = float(end)
        if not isinstance(chapter.get("planned_title"), str) or not chapter[
            "planned_title"
        ].strip():
            raise ChapterContentError(f"输入章节{index}计划标题为空。")
        ids = chapter.get("event_ids")
        if not isinstance(ids, list) or not ids or any(
            not isinstance(event_id, str) for event_id in ids
        ):
            raise ChapterContentError(f"输入章节{index}事件ID数组无效。")
        if (
            chapter.get("event_count") != len(ids)
            or chapter.get("first_event_id") != ids[0]
            or chapter.get("last_event_id") != ids[-1]
        ):
            raise ChapterContentError(f"输入章节{index}事件摘要不一致。")
        _valid_sha256(chapter.get("input_sha256"), f"输入章节{index}哈希")
        if timeline_by_id is not None:
            try:
                events = [timeline_by_id[event_id] for event_id in ids]
            except KeyError as exc:
                raise ChapterContentError(
                    f"输入章节{index}包含未知时间轴事件ID。"
                ) from exc
            if any(
                not (float(start) <= float(event["start"]) < float(end))
                for event in events
            ):
                raise ChapterContentError(f"输入章节{index}包含章外ASR事件。")
            prompt_events = [
                {
                    "id": event["id"],
                    "start": event["start"],
                    "end": event["end"],
                    "text": event["text"],
                }
                for event in events
            ]
            if _canonical_sha256(prompt_events) != chapter["input_sha256"]:
                raise ChapterContentError(f"输入章节{index}内容哈希不一致。")
        all_event_ids.extend(ids)
    if len(all_event_ids) != event_count or len(set(all_event_ids)) != event_count:
        raise ChapterContentError("章节输入manifest存在事件遗漏或重复。")
    if timeline is not None:
        timeline_ids = [str(event["id"]) for event in timeline["events"]]
        if all_event_ids != timeline_ids or previous_end != float(
            timeline["duration_seconds"]
        ):
            raise ChapterContentError("章节输入manifest未完整覆盖时间轴。")
        if source_timeline["sha256"] != _canonical_sha256(timeline):
            raise ChapterContentError("章节输入manifest引用的时间轴哈希不一致。")
    cache_key = payload.get("cache_key")
    without_cache_key = dict(payload)
    without_cache_key.pop("cache_key", None)
    if cache_key != _canonical_sha256(without_cache_key):
        raise ChapterContentError("章节输入manifest缓存键不一致。")


def validate_chapter_content_bundle(
    payload: Any,
    *,
    input_manifest: dict[str, Any],
) -> None:
    """按离线输入manifest校验ChapterContentBundle 1.0。"""

    if not isinstance(payload, dict):
        raise ChapterContentError("ChapterContentBundle必须是JSON对象。")
    validate_chapter_input_manifest(input_manifest)
    if payload.get("schema_version") != CHAPTER_CONTENT_SCHEMA_VERSION:
        raise ChapterContentError("ChapterContentBundle Schema版本无效。")
    for field in (
        "task_id",
        "strategy",
        "source_chapter_plan",
        "source_timeline",
        "chaptering_provenance",
    ):
        if payload.get(field) != input_manifest.get(field):
            raise ChapterContentError(f"ChapterContentBundle的{field}与输入manifest不一致。")
    generation = payload.get("generation")
    if not isinstance(generation, dict) or generation.get("status") != "success":
        raise ChapterContentError("内容生成状态必须为success。")
    for field in (
        "prompt_version",
        "merge_prompt_version",
        "content_policy_version",
        "windowing_version",
        "validator_version",
    ):
        if not isinstance(generation.get(field), str) or not generation[field].strip():
            raise ChapterContentError(f"generation.{field}无效。")
    enforce_zh_cn = (
        generation.get("validator_version") == CHAPTER_CONTENT_VALIDATOR_VERSION
    )
    if enforce_zh_cn and generation.get("output_language") != CHAPTER_CONTENT_OUTPUT_LANGUAGE:
        raise ChapterContentError("generation.output_language必须为zh-CN。")

    chapters = payload.get("chapters")
    expected_chapters = input_manifest.get("chapters")
    if not isinstance(chapters, list) or not isinstance(expected_chapters, list):
        raise ChapterContentError("章节内容或输入章节必须是数组。")
    if len(chapters) != len(expected_chapters):
        raise ChapterContentError("章节内容数量与输入manifest不一致。")
    for chapter, expected in zip(chapters, expected_chapters):
        if not isinstance(chapter, dict):
            raise ChapterContentError("章节内容必须是JSON对象。")
        for field in ("chapter_id", "index", "start", "end", "planned_title"):
            if chapter.get(field) != expected.get(field):
                raise ChapterContentError(f"章节{field}与输入manifest不一致。")
        window_count = chapter.get("window_count")
        if not isinstance(window_count, int) or window_count < 1:
            raise ChapterContentError("章节window_count必须是正整数。")
        allowed_ids = expected["event_ids"]
        cited: set[str] = set()
        for field in ("title", "summary"):
            text, ids = _evidence_field(
                chapter.get(field), field=field, allowed_ids=allowed_ids
            )
            if enforce_zh_cn:
                _validate_zh_cn_text(text, field=field)
            cited.update(ids)
        for field, minimum, maximum in (
            ("key_points", 3, 6),
            ("keywords", 3, 8),
        ):
            items = chapter.get(field)
            if not isinstance(items, list) or not minimum <= len(items) <= maximum:
                raise ChapterContentError(
                    f"{field}数量必须在{minimum}到{maximum}之间。"
                )
            item_texts: list[str] = []
            for index, item in enumerate(items, start=1):
                text, ids = _evidence_field(
                    item,
                    field=f"{field}[{index}]",
                    allowed_ids=allowed_ids,
                )
                if enforce_zh_cn:
                    _validate_zh_cn_text(
                        text,
                        field=f"{field}[{index}]",
                        allow_term=field == "keywords",
                    )
                item_texts.append(text.casefold())
                cited.update(ids)
            if len(set(item_texts)) != len(item_texts):
                raise ChapterContentError(f"{field}包含完全重复内容。")
        expected_union = [event_id for event_id in allowed_ids if event_id in cited]
        if chapter.get("source_event_ids") != expected_union:
            raise ChapterContentError("章级source_event_ids不是字段引用的有序去重并集。")


def _failed_attempt_count(audit_path: Path) -> int:
    if not audit_path.is_file():
        return 0
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    attempts = payload.get("attempts") if isinstance(payload, dict) else None
    return len(attempts) if isinstance(attempts, list) else 0


def generate_task_chapter_content_single_window_mock(
    output_root: str | Path,
    task_id: str,
    *,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
) -> SingleWindowContentResult:
    """P9-2仅用mock逐章生成单窗预览；不发布正式内容。"""

    resolved_config = config or LLMConfig(
        backend="mock",
        base_url="",
        model="stage9-deterministic-mock",
        timeout_seconds=60.0,
        api_key="",
        mock_response={"mode": CHAPTER_SINGLE_WINDOW_VERSION},
        enable_thinking=None,
        temperature=0.0,
    )
    if resolved_config.backend != "mock":
        raise ChapterContentError("P9-2只允许mock backend，禁止真实API配置。")
    selected_transport = transport or DeterministicChapterMockTransport()
    preparation = prepare_task_chapter_inputs(output_root, task_id)
    manifest = _read_json(preparation.manifest_path, "章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterContentError(str(exc)) from exc
    validate_chapter_input_manifest(manifest, timeline=timeline)

    attempt_seed = {
        "version": CHAPTER_SINGLE_WINDOW_VERSION,
        "input_cache_key": manifest["cache_key"],
        "provider": resolved_config.public_dict(),
        "mock_response": resolved_config.mock_response,
        "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
        "prompt_version": CHAPTER_CONTENT_PROMPT_VERSION,
        "content_policy_version": CHAPTER_CONTENT_POLICY_VERSION,
        "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
    }
    attempt_hash = _canonical_sha256(attempt_seed)
    attempt_id = (
        f"single-window-{manifest['source_chapter_plan']['result_id']}-"
        f"{attempt_hash[:12]}"
    )
    task_root = Path(output_root).expanduser() / task_id
    attempt_root = task_root / "content" / "attempts" / attempt_id
    preview_path = attempt_root / "single-window-preview.json"
    report_path = attempt_root / "attempt-report.json"
    cache_namespace = f"stage9-p9-2-{attempt_hash[:12]}"
    completed: list[dict[str, Any]] = []
    normalized_chapters: list[dict[str, Any]] = []
    supplier_requests = 0
    cache_hits = 0

    for chapter in manifest["chapters"]:
        prompt_events = _chapter_prompt_events(timeline, chapter)
        serialized = json.dumps(
            prompt_events, ensure_ascii=False, separators=(",", ":")
        )
        if len(prompt_events) > MAX_WINDOW_EVENTS or len(serialized) > MAX_WINDOW_CHARS:
            error = (
                f"章节{chapter['chapter_id']}超过P9-2单窗上限，"
                "必须等待P9-3分窗实现。"
            )
            _write_json_atomic(
                report_path,
                {
                    "schema_version": "1.0",
                    "attempt_version": CHAPTER_SINGLE_WINDOW_VERSION,
                    "task_id": task_id,
                    "status": "failed",
                    "mode": "mock",
                    "error": error,
                    "completed_chapters": completed,
                    "supplier_request_count": supplier_requests,
                    "cache_hit_count": cache_hits,
                },
            )
            raise ChapterContentError(error)
        allowed_ids = chapter["event_ids"]

        def validate_response(response: dict[str, Any]) -> dict[str, Any]:
            try:
                return _normalize_window_response(response, allowed_ids=allowed_ids)
            except ChapterContentError as exc:
                raise LLMRetryableError(f"章节窗口内容结构无效：{exc}") from exc

        call_id = f"{chapter['chapter_id']}-w001"
        artifact_subdir = (
            Path("content")
            / "attempts"
            / attempt_id
            / "windows"
            / chapter["chapter_id"]
        )
        try:
            call_result: LLMCallResult = call_task_llm(
                output_root,
                task_id,
                call_id=call_id,
                messages=_build_window_messages(chapter, prompt_events),
                response_format={"type": "json_object"},
                config=resolved_config,
                transport=selected_transport,
                response_validator=validate_response,
                validation_id=CHAPTER_CONTENT_VALIDATION_ID,
                artifact_subdir=artifact_subdir,
                cache_namespace=cache_namespace,
                save_validation_audit=True,
            )
        except LLMAdapterError as exc:
            audit_path = (
                task_root
                / artifact_subdir
                / f"{call_id}-validation-audit.json"
            )
            supplier_requests += _failed_attempt_count(audit_path)
            error = str(exc)
            _write_json_atomic(
                report_path,
                {
                    "schema_version": "1.0",
                    "attempt_version": CHAPTER_SINGLE_WINDOW_VERSION,
                    "task_id": task_id,
                    "status": "failed",
                    "mode": "mock",
                    "failed_chapter_id": chapter["chapter_id"],
                    "error": error,
                    "completed_chapters": completed,
                    "supplier_request_count": supplier_requests,
                    "cache_hit_count": cache_hits,
                },
            )
            raise ChapterContentError(error) from exc
        if call_result.cache_hit:
            cache_hits += 1
        else:
            supplier_requests += call_result.attempt_count
        completed.append(
            {
                "chapter_id": chapter["chapter_id"],
                "call_id": call_id,
                "output_path": str(call_result.output_path.resolve()),
                "attempt_count": 0 if call_result.cache_hit else call_result.attempt_count,
                "cache_hit": call_result.cache_hit,
            }
        )
        normalized_chapters.append(
            {
                "chapter_id": chapter["chapter_id"],
                "index": chapter["index"],
                "start": chapter["start"],
                "end": chapter["end"],
                "planned_title": chapter["planned_title"],
                "title": call_result.response["title"],
                "summary": call_result.response["summary"],
                "key_points": call_result.response["key_points"],
                "keywords": call_result.response["keywords"],
                "source_event_ids": call_result.response["source_event_ids"],
                "window_count": 1,
            }
        )

    bundle = {
        "schema_version": CHAPTER_CONTENT_SCHEMA_VERSION,
        "task_id": manifest["task_id"],
        "strategy": manifest["strategy"],
        "source_chapter_plan": manifest["source_chapter_plan"],
        "source_timeline": manifest["source_timeline"],
        "chaptering_provenance": manifest["chaptering_provenance"],
        "generation": {
            "status": "success",
            "mode": "mock",
            "backend": resolved_config.backend,
            "model": resolved_config.model,
            "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
            "prompt_version": CHAPTER_CONTENT_PROMPT_VERSION,
            "merge_prompt_version": CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
            "content_policy_version": CHAPTER_CONTENT_POLICY_VERSION,
            "windowing_version": CHAPTER_WINDOWING_VERSION,
            "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
        },
        "chapters": normalized_chapters,
    }
    validate_chapter_content_bundle(bundle, input_manifest=manifest)
    _write_json_atomic(preview_path, bundle)
    report = {
        "schema_version": "1.0",
        "attempt_version": CHAPTER_SINGLE_WINDOW_VERSION,
        "task_id": task_id,
        "status": "success",
        "mode": "mock",
        "input_manifest_path": str(preparation.manifest_path.resolve()),
        "input_manifest_cache_key": manifest["cache_key"],
        "preview_path": str(preview_path.resolve()),
        "preview_sha256": _canonical_sha256(bundle),
        "chapter_count": len(normalized_chapters),
        "supplier_request_count": supplier_requests,
        "cache_hit_count": cache_hits,
        "chapters": completed,
    }
    _write_json_atomic(report_path, report)
    return SingleWindowContentResult(
        preview_path=preview_path,
        attempt_report_path=report_path,
        attempt_path=attempt_root,
        chapter_count=len(normalized_chapters),
        supplier_request_count=supplier_requests,
        cache_hit_count=cache_hits,
        all_cache_hit=cache_hits == len(normalized_chapters),
    )


def _publish_content_result_group(
    task_root: Path,
    *,
    bundle: dict[str, Any],
    execution_report: dict[str, Any],
    result_directory: str,
    description: str,
) -> tuple[Path, bool]:
    result_id = execution_report["result_id"]
    group = task_root / "content" / result_directory / result_id
    content_path = group / "content.json"
    report_path = group / "execution-report.json"
    manifest_path = group / "manifest.json"
    if group.exists():
        try:
            names = {path.name for path in group.iterdir() if path.is_file()}
            stored_content = _read_json(content_path, f"{description}结果组内容")
            stored_report = _read_json(report_path, f"{description}结果组执行报告")
            manifest = _read_json(manifest_path, f"{description}结果组manifest")
        except OSError as exc:
            raise ChapterContentError(f"无法读取{description}结果组：{group}") from exc
        files = manifest.get("files")
        if (
            names != {"content.json", "execution-report.json", "manifest.json"}
            or manifest.get("schema_version") != "1.0"
            or manifest.get("result_id") != result_id
            or manifest.get("task_id") != bundle["task_id"]
            or not isinstance(files, dict)
            or set(files) != {"content.json", "execution-report.json"}
            or files.get("content.json") != _file_sha256(content_path)
            or files.get("execution-report.json") != _file_sha256(report_path)
            or stored_content != bundle
            or stored_report != execution_report
        ):
            raise ChapterContentError(
                f"{description}结果组不完整、损坏或发生内容混用：{group}"
            )
        return group, True

    temporary = group.with_name(f"{group.name}.tmp")
    if temporary.exists():
        raise ChapterContentError(
            f"存在未清理的{description}结果组临时目录：{temporary}"
        )
    try:
        temporary.mkdir(parents=True, exist_ok=False)
        temporary_content = temporary / "content.json"
        temporary_report = temporary / "execution-report.json"
        _write_json_atomic(temporary_content, bundle)
        _write_json_atomic(temporary_report, execution_report)
        _write_json_atomic(
            temporary / "manifest.json",
            {
                "schema_version": "1.0",
                "result_id": result_id,
                "task_id": bundle["task_id"],
                "files": {
                    "content.json": _file_sha256(temporary_content),
                    "execution-report.json": _file_sha256(temporary_report),
                },
            },
        )
        temporary.replace(group)
    except OSError as exc:
        raise ChapterContentError(f"无法原子发布{description}结果组：{group}") from exc
    finally:
        if temporary.exists():
            try:
                shutil.rmtree(temporary)
            except OSError:
                pass
    return group, False


def _generate_task_chapter_content_windowed(
    output_root: str | Path,
    task_id: str,
    *,
    config: LLMConfig,
    transport: LLMTransport,
    mode: str,
    attempt_version: str,
    result_directory: str,
    result_prefix: str,
    publish_formal_aliases: bool,
    max_window_chars: int = MAX_WINDOW_CHARS,
    max_window_events: int = MAX_WINDOW_EVENTS,
    max_business_call_count: int | None = None,
    max_attempts: int = 2,
) -> WindowedMockContentResult:
    """共用的确定性分窗、生成、合并、缓存与结果组发布流水线。"""

    if not isinstance(max_window_chars, int) or max_window_chars < 1:
        raise ChapterContentError("max_window_chars必须是正整数。")
    if not isinstance(max_window_events, int) or max_window_events < 1:
        raise ChapterContentError("max_window_events必须是正整数。")
    if (
        max_business_call_count is not None
        and (
            isinstance(max_business_call_count, bool)
            or not isinstance(max_business_call_count, int)
            or max_business_call_count < 1
        )
    ):
        raise ChapterContentError("max_business_call_count必须是正整数或null。")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 2:
        raise ChapterContentError("max_attempts必须是1或2。")
    resolved_config = config
    selected_transport = transport
    preparation = prepare_task_chapter_inputs(output_root, task_id)
    manifest = _read_json(preparation.manifest_path, "章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterContentError(str(exc)) from exc
    validate_chapter_input_manifest(manifest, timeline=timeline)

    attempt_seed = {
        "version": attempt_version,
        "mode": mode,
        "input_cache_key": manifest["cache_key"],
        "provider": resolved_config.public_dict(),
        "mock_response": resolved_config.mock_response,
        "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
        "prompt_version": CHAPTER_CONTENT_PROMPT_VERSION,
        "merge_prompt_version": CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
        "content_policy_version": CHAPTER_CONTENT_POLICY_VERSION,
        "windowing_version": CHAPTER_WINDOWING_VERSION,
        "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
        "max_window_chars": max_window_chars,
        "max_window_events": max_window_events,
    }
    attempt_hash = _canonical_sha256(attempt_seed)
    attempt_id = (
        f"windowed-{mode}-{manifest['source_chapter_plan']['result_id']}-"
        f"{attempt_hash[:12]}"
    )
    task_root = Path(output_root).expanduser() / task_id
    attempt_root = task_root / "content" / "attempts" / attempt_id
    attempt_report_path = attempt_root / "attempt-report.json"
    window_manifest_path = attempt_root / "window-manifest.json"
    cache_namespace = f"stage9-{attempt_hash[:12]}"

    chapter_windows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    window_manifest_chapters: list[dict[str, Any]] = []
    total_windows = 0
    for chapter in manifest["chapters"]:
        prompt_events = _chapter_prompt_events(timeline, chapter)
        windows = split_chapter_events(
            prompt_events,
            max_window_chars=max_window_chars,
            max_window_events=max_window_events,
        )
        total_windows += len(windows)
        window_manifest_chapters.append(
            {
                "chapter_id": chapter["chapter_id"],
                "chapter_input_sha256": chapter["input_sha256"],
                "window_count": len(windows),
                "windows": [
                    {
                        "window_id": f"{chapter['chapter_id']}-w{window['window_index']:03d}",
                        "window_index": window["window_index"],
                        "event_count": window["event_count"],
                        "event_ids": window["event_ids"],
                        "input_sha256": window["input_sha256"],
                        "serialized_chars": window["serialized_chars"],
                        "oversize_single_event": window["oversize_single_event"],
                    }
                    for window in windows
                ],
            }
        )
        chapter_windows.append((chapter, windows))
    planned_business_calls = total_windows + sum(
        1 for _, windows in chapter_windows if len(windows) > 1
    )
    if (
        max_business_call_count is not None
        and planned_business_calls > max_business_call_count
    ):
        raise ChapterContentError(
            "正文预计业务调用数"
            f"{planned_business_calls}超过本次上限{max_business_call_count}；"
            "已在首个正文请求前停止。"
        )
    window_manifest = {
        "schema_version": "1.0",
        "windowing_version": CHAPTER_WINDOWING_VERSION,
        "task_id": task_id,
        "input_manifest_cache_key": manifest["cache_key"],
        "max_window_chars": max_window_chars,
        "max_window_events": max_window_events,
        "chapter_count": len(chapter_windows),
        "window_count": total_windows,
        "chapters": window_manifest_chapters,
    }
    window_manifest["cache_key"] = _canonical_sha256(window_manifest)
    _write_json_atomic(window_manifest_path, window_manifest)

    completed_calls: list[dict[str, Any]] = []
    normalized_chapters: list[dict[str, Any]] = []
    supplier_requests = 0
    cache_hits = 0
    merge_count = 0

    def fail(
        *,
        error: str,
        failed_chapter_id: str,
        failed_call_id: str | None = None,
    ) -> None:
        _write_json_atomic(
            attempt_report_path,
            {
                "schema_version": "1.0",
                "attempt_version": attempt_version,
                "task_id": task_id,
                "status": "failed",
                "mode": mode,
                "failed_chapter_id": failed_chapter_id,
                "failed_call_id": failed_call_id,
                "error": error,
                "completed_calls": completed_calls,
                "supplier_request_count": supplier_requests,
                "cache_hit_count": cache_hits,
            },
        )
        raise ChapterContentError(error)

    for chapter, windows in chapter_windows:
        window_results: list[dict[str, Any]] = []
        for window in windows:
            window_id = f"{chapter['chapter_id']}-w{window['window_index']:03d}"

            def validate_window(response: dict[str, Any]) -> dict[str, Any]:
                try:
                    if mode == "generic-asr-only":
                        response = _normalize_generic_shape_variants(
                            response, allowed_ids=window["event_ids"]
                        )
                    return _normalize_window_response(
                        response, allowed_ids=window["event_ids"]
                    )
                except ChapterContentError as exc:
                    raise LLMRetryableError(
                        f"章节窗口内容结构无效：{exc}"
                    ) from exc

            artifact_subdir = (
                Path("content")
                / "attempts"
                / attempt_id
                / "windows"
                / chapter["chapter_id"]
            )
            try:
                result = call_task_llm(
                    output_root,
                    task_id,
                    call_id=window_id,
                    messages=_build_window_messages(chapter, window["events"]),
                    response_format={"type": "json_object"},
                    config=resolved_config,
                    transport=selected_transport,
                    response_validator=validate_window,
                    validation_id=CHAPTER_CONTENT_VALIDATION_ID,
                    artifact_subdir=artifact_subdir,
                    cache_namespace=cache_namespace,
                    save_validation_audit=True,
                    max_attempts=max_attempts,
                )
            except LLMAdapterError as exc:
                audit_path = task_root / artifact_subdir / f"{window_id}-validation-audit.json"
                supplier_requests += _failed_attempt_count(audit_path)
                fail(
                    error=str(exc),
                    failed_chapter_id=chapter["chapter_id"],
                    failed_call_id=window_id,
                )
            if result.cache_hit:
                cache_hits += 1
            else:
                supplier_requests += result.attempt_count
            completed_calls.append(
                {
                    "call_id": window_id,
                    "kind": "window",
                    "chapter_id": chapter["chapter_id"],
                    "cache_hit": result.cache_hit,
                    "attempt_count": 0 if result.cache_hit else result.attempt_count,
                    "output_path": str(result.output_path.resolve()),
                }
            )
            window_results.append(
                {
                    "window_id": window_id,
                    "title": result.response["title"],
                    "summary": result.response["summary"],
                    "key_points": result.response["key_points"],
                    "keywords": result.response["keywords"],
                    "source_event_ids": result.response["source_event_ids"],
                }
            )

        final_response: dict[str, Any]
        if len(window_results) == 1:
            final_response = {
                field: window_results[0][field]
                for field in (
                    "title",
                    "summary",
                    "key_points",
                    "keywords",
                    "source_event_ids",
                )
            }
        else:
            merge_count += 1
            merge_call_id = f"{chapter['chapter_id']}-merge"

            def validate_merge(response: dict[str, Any]) -> dict[str, Any]:
                try:
                    return _normalize_merge_response(
                        response,
                        expected_chapter_id=chapter["chapter_id"],
                        chapter_event_ids=chapter["event_ids"],
                        window_results=window_results,
                        generic_shape_compat=mode == "generic-asr-only",
                    )
                except ChapterContentError as exc:
                    raise LLMRetryableError(
                        f"章节窗口合并结构无效：{exc}"
                    ) from exc

            merge_subdir = (
                Path("content") / "attempts" / attempt_id / "merges"
            )
            try:
                merge_result = call_task_llm(
                    output_root,
                    task_id,
                    call_id=merge_call_id,
                    messages=_build_merge_messages(chapter, window_results),
                    response_format={"type": "json_object"},
                    config=resolved_config,
                    transport=selected_transport,
                    response_validator=validate_merge,
                    validation_id=CHAPTER_MERGE_VALIDATION_ID,
                    artifact_subdir=merge_subdir,
                    cache_namespace=cache_namespace,
                    save_validation_audit=True,
                    max_attempts=max_attempts,
                )
            except LLMAdapterError as exc:
                audit_path = task_root / merge_subdir / f"{merge_call_id}-validation-audit.json"
                supplier_requests += _failed_attempt_count(audit_path)
                fail(
                    error=str(exc),
                    failed_chapter_id=chapter["chapter_id"],
                    failed_call_id=merge_call_id,
                )
            if merge_result.cache_hit:
                cache_hits += 1
            else:
                supplier_requests += merge_result.attempt_count
            completed_calls.append(
                {
                    "call_id": merge_call_id,
                    "kind": "merge",
                    "chapter_id": chapter["chapter_id"],
                    "cache_hit": merge_result.cache_hit,
                    "attempt_count": (
                        0 if merge_result.cache_hit else merge_result.attempt_count
                    ),
                    "output_path": str(merge_result.output_path.resolve()),
                }
            )
            final_response = merge_result.response

        normalized_chapters.append(
            {
                "chapter_id": chapter["chapter_id"],
                "index": chapter["index"],
                "start": chapter["start"],
                "end": chapter["end"],
                "planned_title": chapter["planned_title"],
                "title": final_response["title"],
                "summary": final_response["summary"],
                "key_points": final_response["key_points"],
                "keywords": final_response["keywords"],
                "source_event_ids": final_response["source_event_ids"],
                "window_count": len(window_results),
            }
        )

    bundle = {
        "schema_version": CHAPTER_CONTENT_SCHEMA_VERSION,
        "task_id": manifest["task_id"],
        "strategy": manifest["strategy"],
        "source_chapter_plan": manifest["source_chapter_plan"],
        "source_timeline": manifest["source_timeline"],
        "chaptering_provenance": manifest["chaptering_provenance"],
        "generation": {
            "status": "success",
            "mode": mode,
            "backend": resolved_config.backend,
            "model": resolved_config.model,
            "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
            "prompt_version": CHAPTER_CONTENT_PROMPT_VERSION,
            "merge_prompt_version": CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
            "content_policy_version": CHAPTER_CONTENT_POLICY_VERSION,
            "windowing_version": CHAPTER_WINDOWING_VERSION,
            "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
            "max_window_chars": max_window_chars,
            "max_window_events": max_window_events,
        },
        "chapters": normalized_chapters,
    }
    validate_chapter_content_bundle(bundle, input_manifest=manifest)
    content_sha256 = _canonical_sha256(bundle)
    result_id = f"{result_prefix}-{content_sha256[:12]}"
    deterministic_report = {
        "schema_version": "1.0",
        "attempt_version": attempt_version,
        "task_id": task_id,
        "status": "success",
        "mode": mode,
        "result_id": result_id,
        "content_sha256": content_sha256,
        "source_stage8_result_id": manifest["source_chapter_plan"]["result_id"],
        "input_manifest_cache_key": manifest["cache_key"],
        "window_manifest_sha256": _canonical_sha256(window_manifest),
        "chapter_count": len(normalized_chapters),
        "window_count": total_windows,
        "merge_count": merge_count,
        "planned_call_count": total_windows + merge_count,
    }
    result_group, group_cache_hit = _publish_content_result_group(
        task_root,
        bundle=bundle,
        execution_report=deterministic_report,
        result_directory=result_directory,
        description="正式" if publish_formal_aliases else "mock",
    )
    if publish_formal_aliases:
        _write_json_atomic(task_root / "content" / "content.json", bundle)
        _write_json_atomic(
            task_root / "content" / "content-execution-report.json",
            deterministic_report,
        )
    _write_json_atomic(
        attempt_report_path,
        {
            "schema_version": "1.0",
            "attempt_version": attempt_version,
            "task_id": task_id,
            "status": "success",
            "mode": mode,
            "result_id": result_id,
            "result_group_path": str(result_group.resolve()),
            "window_manifest_path": str(window_manifest_path.resolve()),
            "chapter_count": len(normalized_chapters),
            "window_count": total_windows,
            "merge_count": merge_count,
            "supplier_request_count": supplier_requests,
            "cache_hit_count": cache_hits,
            "result_group_cache_hit": group_cache_hit,
            "completed_calls": completed_calls,
        },
    )
    total_calls = total_windows + merge_count
    return WindowedMockContentResult(
        result_group_path=result_group,
        content_path=result_group / "content.json",
        execution_report_path=result_group / "execution-report.json",
        attempt_path=attempt_root,
        chapter_count=len(normalized_chapters),
        window_count=total_windows,
        merge_count=merge_count,
        supplier_request_count=supplier_requests,
        cache_hit_count=cache_hits,
        all_cache_hit=cache_hits == total_calls,
        result_group_cache_hit=group_cache_hit,
    )


def generate_task_chapter_content_windowed_mock(
    output_root: str | Path,
    task_id: str,
    *,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    max_window_chars: int = MAX_WINDOW_CHARS,
    max_window_events: int = MAX_WINDOW_EVENTS,
) -> WindowedMockContentResult:
    """P9-3离线mock入口；真实配置在任何内容生成前拒绝。"""

    resolved_config = config or LLMConfig(
        backend="mock",
        base_url="",
        model="stage9-windowed-deterministic-mock",
        timeout_seconds=60.0,
        api_key="",
        mock_response={"mode": CHAPTER_WINDOWED_MOCK_VERSION},
        enable_thinking=None,
        temperature=0.0,
    )
    if resolved_config.backend != "mock":
        raise ChapterContentError("P9-3只允许mock backend，禁止真实API配置。")
    return _generate_task_chapter_content_windowed(
        output_root,
        task_id,
        config=resolved_config,
        transport=transport or DeterministicChapterMockTransport(),
        mode="mock-windowed",
        attempt_version=CHAPTER_WINDOWED_MOCK_VERSION,
        result_directory="mock-results",
        result_prefix="mock",
        publish_formal_aliases=False,
        max_window_chars=max_window_chars,
        max_window_events=max_window_events,
    )


def generate_task_chapter_content(
    output_root: str | Path,
    task_id: str,
    *,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
) -> WindowedMockContentResult:
    """P9-5冻结DEV01真实入口，复用P9-3全部业务流水线。"""

    if task_id != P9_5_TASK_ID:
        raise ChapterContentError("P9-5真实验收只允许任务DEV01。")
    resolved_config = config or load_llm_config(dotenv_path=dotenv_path)
    if resolved_config.backend != "openai_compatible":
        raise ChapterContentError("P9-5真实入口需要openai_compatible backend。")
    if (
        resolved_config.base_url != P9_5_BASE_URL
        or resolved_config.model != P9_5_MODEL
        or resolved_config.temperature != 0.0
        or resolved_config.enable_thinking is not False
    ):
        raise ChapterContentError("P9-5真实公开配置与前置审计冻结值不一致。")
    preparation = prepare_task_chapter_inputs(output_root, task_id)
    manifest = _read_json(preparation.manifest_path, "章节输入manifest")
    if manifest["source_chapter_plan"]["result_id"] != P9_5_STAGE8_RESULT_ID:
        raise ChapterContentError("P9-5阶段8正式结果ID与前置审计不一致。")
    selected_transport = transport
    if selected_transport is None:
        from video_doc.llm_adapter import OpenAICompatibleTransport

        selected_transport = OpenAICompatibleTransport()
    return _generate_task_chapter_content_windowed(
        output_root,
        task_id,
        config=resolved_config,
        transport=selected_transport,
        mode="real-windowed",
        attempt_version=CHAPTER_WINDOWED_REAL_VERSION,
        result_directory="formal-results",
        result_prefix="content",
        publish_formal_aliases=True,
    )


def estimate_task_chapter_content_calls(
    output_root: str | Path,
    task_id: str,
    *,
    max_window_chars: int = MAX_WINDOW_CHARS,
    max_window_events: int = MAX_WINDOW_EVENTS,
) -> ChapterContentCallPlan:
    """不调用LLM，计算当前ASR-only正文分窗所需业务槽位。"""

    preparation = prepare_task_chapter_inputs(output_root, task_id)
    manifest = _read_json(preparation.manifest_path, "章节输入manifest")
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterContentError(str(exc)) from exc
    validate_chapter_input_manifest(manifest, timeline=timeline)
    windows_by_chapter = tuple(
        len(
            split_chapter_events(
                _chapter_prompt_events(timeline, chapter),
                max_window_chars=max_window_chars,
                max_window_events=max_window_events,
            )
        )
        for chapter in manifest["chapters"]
    )
    window_count = sum(windows_by_chapter)
    merge_count = sum(count > 1 for count in windows_by_chapter)
    return ChapterContentCallPlan(
        chapter_count=len(windows_by_chapter),
        window_count=window_count,
        merge_count=merge_count,
        business_call_count=window_count + merge_count,
        windows_by_chapter=windows_by_chapter,
    )


def generate_task_chapter_content_generic(
    output_root: str | Path,
    task_id: str,
    *,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
    max_business_call_count: int = 16,
) -> WindowedMockContentResult:
    """通用ASR-only正文入口；不改变冻结的DEV01 P9-5入口。"""

    resolved_config = config or load_llm_config(dotenv_path=dotenv_path)
    if resolved_config.backend != "openai_compatible":
        raise ChapterContentError("通用ASR-only正文入口需要openai_compatible backend。")
    if (
        resolved_config.base_url != P9_5_BASE_URL
        or resolved_config.model != P9_5_MODEL
        or resolved_config.temperature != 0.0
        or resolved_config.enable_thinking is not False
    ):
        raise ChapterContentError("通用ASR-only正文配置与当前冻结供应商配置不一致。")
    selected_transport = transport
    if selected_transport is None:
        from video_doc.llm_adapter import OpenAICompatibleTransport

        selected_transport = OpenAICompatibleTransport()
    return _generate_task_chapter_content_windowed(
        output_root,
        task_id,
        config=resolved_config,
        transport=selected_transport,
        mode="generic-asr-only",
        attempt_version="generic-asr-only-content-1.0",
        result_directory="formal-results",
        result_prefix="content",
        publish_formal_aliases=True,
        max_business_call_count=max_business_call_count,
        max_attempts=2,
    )
