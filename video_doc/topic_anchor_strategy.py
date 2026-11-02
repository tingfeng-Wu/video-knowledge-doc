"""阶段8主题锚点两阶段分章实验（严格 primary 路径）。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_doc.asr_timeline import ASRTimelineError, load_task_timeline
from video_doc.chapter_policy import STABILITY_POLICY_VERSION, render_stability_policy
from video_doc.llm_adapter import (
    LLMAdapterError,
    LLMConfig,
    LLMNonRetryableError,
    LLMRetryableError,
    LLMTransport,
    call_task_llm,
    load_llm_config,
)
from video_doc.llm_strategy import (
    CHAPTER_PLAN_SCHEMA_VERSION,
    ChapterPlanError,
    validate_chapter_plan,
    validate_experiment_id,
)


STRATEGY_VERSION = "two-stage-anchor-2.0"
TOPIC_PROMPT_VERSION = "chapter-topic-anchor-2.0"
BOUNDARY_PROMPT_VERSION = "chapter-boundary-anchor-2.0"
TOPIC_VALIDATION_VERSION = "chapter-topic-anchor-v1"
BOUNDARY_VALIDATION_VERSION = "chapter-boundary-anchor-v1"
MIN_CHAPTER_SECONDS = 60.0
MAX_DURATION_SECONDS = 1800.0
OUTLINE_CALL_ID = "stage8-anchor-topic-outline"
ALIGNMENT_CALL_ID = "stage8-anchor-boundary-alignment"
CORE_FILES = (
    "topic-outline.json",
    "boundary-alignment.json",
    "quality-sidecar.json",
    "chapter-plan.json",
)


class TopicAnchorError(ChapterPlanError):
    """主题锚点实验失败，携带稳定失败类型和审计路径。"""

    def __init__(
        self,
        message: str,
        *,
        failure_type: str = "failed",
        audit_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.audit_path = audit_path


@dataclass(frozen=True)
class TopicAnchorResult:
    run_directory: Path
    plan_path: Path
    boundary_count: int
    duration_seconds: float
    business_call_count: int
    provider_request_count: int
    correction_count: int
    llm_cache_hit_count: int
    artifact_cache_hit: bool
    status: str = "success"
    result_source: str = "primary"
    quality_level: str = "normal"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TopicAnchorError(f"{field}不能为空。", failure_type="structure_error")
    return value.strip()


def _event_index(timeline: dict[str, Any]) -> dict[str, int]:
    return {str(event["id"]): index for index, event in enumerate(timeline["events"])}


def _timeline_sha256(timeline: dict[str, Any]) -> str:
    return _sha256_value(timeline)


def _run_root(output_root: str | Path, task_id: str) -> Path:
    return (
        Path(output_root).expanduser()
        / task_id
        / "experiments"
        / "stage8"
        / "two-stage"
        / "anchor-v2"
    )


def _render_timeline(timeline: dict[str, Any]) -> str:
    return "\n".join(
        f"[{event['id']}] start={float(event['start']):.3f}s "
        f"end={float(event['end']):.3f}s text={event['text']}"
        for event in timeline["events"]
    )


def build_topic_anchor_messages(timeline: dict[str, Any]) -> list[dict[str, str]]:
    duration = float(timeline["duration_seconds"])
    system = (
        "你只识别知识型视频按叙事顺序展开的一级主题，并为每个主题选择一个已经持续"
        "展开该主题的内部ASR锚点。锚点不是章节起点，不得选择首次提及、过渡、回顾或"
        "下一主题内容。不要输出边界或粗略秒数，只返回JSON对象。"
    )
    user = (
        f"提示词版本：{TOPIC_PROMPT_VERSION}。"
        f"{render_stability_policy(duration)}\n"
        "第一主题覆盖视频开头，最后主题覆盖视频结尾。topic_id必须从t0001连续递增；"
        "title、scope、anchor_reason均不得为空；anchor_event_id只能引用时间轴现有事件且"
        "必须严格递增。anchor_reason要说明此处为何已经位于主题内部。只返回："
        '{"topics":[{"topic_id":"t0001","title":"...","scope":"...",'
        '"anchor_event_id":"a0001","anchor_reason":"..."}]}。\n'
        "完整ASR时间轴：\n"
        f"{_render_timeline(timeline)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_topic_anchor_response(
    response: Any, timeline: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise TopicAnchorError("主题响应必须是JSON对象。", failure_type="structure_error")
    raw_topics = response.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise TopicAnchorError("主题响应必须包含非空topics数组。", failure_type="structure_error")
    indices = _event_index(timeline)
    topics: list[dict[str, str]] = []
    previous_index = -1
    for number, raw in enumerate(raw_topics, start=1):
        if not isinstance(raw, dict):
            raise TopicAnchorError(f"第{number}个主题必须是对象。", failure_type="structure_error")
        expected_id = f"t{number:04d}"
        if raw.get("topic_id") != expected_id:
            raise TopicAnchorError("topic_id必须连续且顺序正确。", failure_type="structure_error")
        anchor_id = _nonempty(raw.get("anchor_event_id"), f"{expected_id}锚点")
        if anchor_id not in indices:
            raise TopicAnchorError(f"{expected_id}锚点事件不存在。", failure_type="structure_error")
        anchor_index = indices[anchor_id]
        if anchor_index <= previous_index:
            raise TopicAnchorError("主题锚点不得重复或逆序。", failure_type="structure_error")
        topics.append(
            {
                "topic_id": expected_id,
                "title": _nonempty(raw.get("title"), f"{expected_id}标题"),
                "scope": _nonempty(raw.get("scope"), f"{expected_id}范围"),
                "anchor_event_id": anchor_id,
                "anchor_reason": _nonempty(
                    raw.get("anchor_reason"), f"{expected_id}锚点理由"
                ),
            }
        )
        previous_index = anchor_index
    return {"topics": topics}


def build_anchor_intervals(
    outline: dict[str, Any], timeline: dict[str, Any]
) -> list[dict[str, Any]]:
    topics = outline.get("topics")
    if not isinstance(topics, list) or not topics:
        raise TopicAnchorError("主题结果无效。", failure_type="structure_error")
    indices = _event_index(timeline)
    events = timeline["events"]
    intervals: list[dict[str, Any]] = []
    for left, right in zip(topics, topics[1:]):
        left_index = indices[str(left["anchor_event_id"])]
        right_index = indices[str(right["anchor_event_id"])]
        if left_index >= right_index:
            raise TopicAnchorError("相邻主题锚点顺序无效。", failure_type="structure_error")
        intervals.append(
            {
                "transition_id": f"{left['topic_id']}_to_{right['topic_id']}",
                "previous_topic": left,
                "new_topic": right,
                "start_anchor_event_id": left["anchor_event_id"],
                "end_anchor_event_id": right["anchor_event_id"],
                "events": [
                    {
                        "id": event["id"],
                        "start": event["start"],
                        "end": event["end"],
                        "text": event["text"],
                    }
                    for event in events[left_index : right_index + 1]
                ],
            }
        )
    return intervals


def build_anchor_alignment_messages(
    intervals: list[dict[str, Any]],
) -> list[dict[str, str]]:
    system = (
        "你只判断相邻一级主题之间的转换。对每个transition返回boundary、merge、"
        "uncertain或invalid_anchor之一。boundary必须是新主题首次明确引入；merge必须有"
        "双侧正面证据；证据不足用uncertain；锚点不在主题内部用invalid_anchor。不得新增"
        "主题、拆章、重排或改写标题和范围。只返回JSON对象。"
    )
    user = (
        f"提示词版本：{BOUNDARY_PROMPT_VERSION}。每条结果必须固定包含transition_id、"
        "decision、source_event_id、previous_evidence_ids、new_evidence_ids、"
        "invalid_anchor_topic_ids和reason。boundary两侧证据各1至4条，new_evidence_ids"
        "必须包含source_event_id；merge两侧证据各1至4条；uncertain的证据和无效锚点"
        "数组均为空；invalid_anchor列出一至两个相邻topic_id及锚点错误证据。只返回："
        '{"transitions":[{"transition_id":"t0001_to_t0002",'
        '"decision":"boundary","source_event_id":"a0001",'
        '"previous_evidence_ids":["a0001"],"new_evidence_ids":["a0002"],'
        '"invalid_anchor_topic_ids":[],"reason":"..."}]}。\n'
        "相邻锚点区间：\n"
        + json.dumps(intervals, ensure_ascii=False, separators=(",", ":"))
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _evidence_ids(
    raw: dict[str, Any], field: str, number: int, allowed: set[str]
) -> list[str]:
    value = raw.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TopicAnchorError(f"第{number}条{field}必须是事件ID数组。", failure_type="structure_error")
    if len(value) != len(set(value)):
        raise TopicAnchorError(f"第{number}条{field}不得重复。", failure_type="structure_error")
    if any(item not in allowed for item in value):
        raise TopicAnchorError(f"第{number}条{field}包含区间外事件。", failure_type="structure_error")
    return list(value)


def normalize_anchor_alignment_response(
    response: Any,
    outline: dict[str, Any],
    intervals: list[dict[str, Any]],
    timeline: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise TopicAnchorError("边界响应必须是JSON对象。", failure_type="structure_error")
    raw_items = response.get("transitions")
    if not isinstance(raw_items, list) or len(raw_items) != len(intervals):
        raise TopicAnchorError("transition数量必须与相邻主题对一致。", failure_type="structure_error")
    indices = _event_index(timeline)
    normalized: list[dict[str, Any]] = []
    for number, (raw, interval) in enumerate(zip(raw_items, intervals), start=1):
        if not isinstance(raw, dict):
            raise TopicAnchorError(f"第{number}条transition必须是对象。", failure_type="structure_error")
        if raw.get("transition_id") != interval["transition_id"]:
            raise TopicAnchorError("transition缺失、重复或顺序错误。", failure_type="structure_error")
        decision = raw.get("decision")
        if decision not in {"boundary", "merge", "uncertain", "invalid_anchor"}:
            raise TopicAnchorError(f"第{number}条decision无效。", failure_type="structure_error")
        allowed = {str(event["id"]) for event in interval["events"]}
        previous = _evidence_ids(raw, "previous_evidence_ids", number, allowed)
        new = _evidence_ids(raw, "new_evidence_ids", number, allowed)
        invalid = raw.get("invalid_anchor_topic_ids")
        if not isinstance(invalid, list) or any(not isinstance(item, str) for item in invalid):
            raise TopicAnchorError("invalid_anchor_topic_ids必须是字符串数组。", failure_type="structure_error")
        if len(invalid) != len(set(invalid)):
            raise TopicAnchorError("invalid_anchor_topic_ids不得重复。", failure_type="structure_error")
        source = raw.get("source_event_id")
        reason = _nonempty(raw.get("reason"), f"第{number}条决定理由")
        pair = {
            interval["previous_topic"]["topic_id"],
            interval["new_topic"]["topic_id"],
        }
        if decision == "boundary":
            if not isinstance(source, str) or source not in allowed:
                raise TopicAnchorError("boundary必须引用锚点区间内事件。", failure_type="structure_error")
            if not 1 <= len(previous) <= 4 or not 1 <= len(new) <= 4:
                raise TopicAnchorError("boundary双侧证据各需1至4条。", failure_type="structure_error")
            if source not in new:
                raise TopicAnchorError("新主题证据必须包含所选边界。", failure_type="structure_error")
            if invalid:
                raise TopicAnchorError("boundary不得声明无效锚点。", failure_type="structure_error")
            source_index = indices[source]
            if any(indices[item] >= source_index for item in previous):
                raise TopicAnchorError("前一主题证据必须全部早于边界。", failure_type="structure_error")
            if any(indices[item] < source_index for item in new):
                raise TopicAnchorError("新主题证据不得早于边界。", failure_type="structure_error")
        elif decision == "merge":
            if source is not None or invalid:
                raise TopicAnchorError("merge不得包含边界或无效锚点。", failure_type="structure_error")
            if not 1 <= len(previous) <= 4 or not 1 <= len(new) <= 4:
                raise TopicAnchorError("merge必须提供双侧1至4条证据。", failure_type="structure_error")
        elif decision == "uncertain":
            if source is not None or previous or new or invalid:
                raise TopicAnchorError("uncertain不得伪造边界、证据或无效锚点。", failure_type="structure_error")
        else:
            if source is not None or not 1 <= len(invalid) <= 2 or not set(invalid) <= pair:
                raise TopicAnchorError("invalid_anchor必须指出相邻的一至两个主题。", failure_type="structure_error")
            if not previous and not new:
                raise TopicAnchorError("invalid_anchor必须提供锚点错误证据。", failure_type="structure_error")
        normalized.append(
            {
                "transition_id": interval["transition_id"],
                "decision": decision,
                "source_event_id": source,
                "previous_evidence_ids": previous,
                "new_evidence_ids": new,
                "invalid_anchor_topic_ids": list(invalid),
                "reason": reason,
            }
        )
    return {"transitions": normalized}


def _merged_groups(
    outline: dict[str, Any], alignment: dict[str, Any]
) -> list[dict[str, Any]]:
    topics = outline["topics"]
    groups: list[dict[str, Any]] = [
        {"topics": [topics[0]], "boundary": None}
    ]
    for transition, topic in zip(alignment["transitions"], topics[1:]):
        if transition["decision"] == "merge":
            groups[-1]["topics"].append(topic)
        else:
            groups.append({"topics": [topic], "boundary": transition})
    return groups


def build_anchor_chapter_plan(
    task_id: str,
    run_id: str,
    timeline: dict[str, Any],
    outline: dict[str, Any],
    alignment: dict[str, Any],
) -> dict[str, Any]:
    decisions = [item["decision"] for item in alignment["transitions"]]
    if "uncertain" in decisions:
        raise TopicAnchorError("主方案包含uncertain决定。", failure_type="uncertain")
    if "invalid_anchor" in decisions:
        raise TopicAnchorError("主方案包含invalid_anchor决定。", failure_type="invalid_anchor")
    events = {str(event["id"]): event for event in timeline["events"]}
    boundaries: list[dict[str, Any]] = []
    for index, group in enumerate(_merged_groups(outline, alignment)):
        titles = [topic["title"] for topic in group["topics"]]
        title = titles[0] if len(titles) == 1 else " / ".join(titles) + "（待阶段9重写）"
        boundary = group["boundary"]
        event_id = None if index == 0 else boundary["source_event_id"]
        timestamp = 0.0 if event_id is None else round(float(events[event_id]["start"]), 3)
        boundaries.append(
            {"timestamp": timestamp, "title": title, "source_event_id": event_id}
        )
    plan = {
        "schema_version": CHAPTER_PLAN_SCHEMA_VERSION,
        "task_id": task_id,
        "strategy": "llm",
        "source_timeline": {
            "schema_version": timeline["schema_version"],
            "sha256": _timeline_sha256(timeline),
            "event_count": len(timeline["events"]),
        },
        "duration_seconds": round(float(timeline["duration_seconds"]), 3),
        "parameters": {
            "pipeline": STRATEGY_VERSION,
            "experiment_id": run_id,
            "prompt_version": f"{TOPIC_PROMPT_VERSION}+{BOUNDARY_PROMPT_VERSION}",
            "topic_validation_version": TOPIC_VALIDATION_VERSION,
            "boundary_validation_version": BOUNDARY_VALIDATION_VERSION,
            "chapter_policy_version": STABILITY_POLICY_VERSION,
            "min_chapter_seconds": MIN_CHAPTER_SECONDS,
            "boundary_alignment": "selected_asr_event_start",
        },
        "boundaries": boundaries,
    }
    try:
        validate_chapter_plan(plan, timeline=timeline, expected_task_id=task_id)
    except ChapterPlanError as exc:
        raise TopicAnchorError(
            str(exc), failure_type="chapter_plan_validation_failed"
        ) from exc
    return plan


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _artifact_payloads(
    task_id: str,
    run_id: str,
    timeline: dict[str, Any],
    outline: dict[str, Any],
    intervals: list[dict[str, Any]],
    alignment: dict[str, Any],
    plan: dict[str, Any],
    stats: dict[str, int],
    provider: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    common = {
        "task_id": task_id,
        "run_id": run_id,
        "strategy_version": STRATEGY_VERSION,
        "timeline_sha256": _timeline_sha256(timeline),
        "provider": provider,
    }
    outline_payload = {**common, "prompt_version": TOPIC_PROMPT_VERSION, **outline}
    alignment_payload = {
        **common,
        "prompt_version": BOUNDARY_PROMPT_VERSION,
        "intervals": intervals,
        **alignment,
    }
    merged = any(item["decision"] == "merge" for item in alignment["transitions"])
    sidecar = {
        **common,
        "quality_status": "merged" if merged else "passed",
        "topics": outline["topics"],
        "transitions": alignment["transitions"],
        "reference_evaluation": None,
        "chapter_plan_sha256": _sha256_value(plan),
    }
    report = {
        **common,
        "mode": "experiment",
        "status": "success",
        "result_source": "primary",
        "quality_level": "normal",
        "failure_reason": None,
        "selected_plan": "chapter-plan.json",
        "selected_plan_sha256": _sha256_value(plan),
        **stats,
    }
    audit = {
        **common,
        "status": "passed",
        "topic_validation_version": TOPIC_VALIDATION_VERSION,
        "boundary_validation_version": BOUNDARY_VALIDATION_VERSION,
        "checks": ["outline", "anchor_intervals", "alignment_evidence", "chapter_plan"],
    }
    return {
        "topic-outline.json": outline_payload,
        "boundary-alignment.json": alignment_payload,
        "quality-sidecar.json": sidecar,
        "chapter-plan.json": plan,
        "execution-report.json": report,
        "validation-audit.json": audit,
    }


def _publish_success_group(target: Path, payloads: dict[str, dict[str, Any]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
    quarantine: Path | None = None
    try:
        stage.mkdir()
        for name, payload in payloads.items():
            _write_json(stage / name, payload)
        manifest = {
            "schema_version": "1.0",
            "status": "success",
            "task_id": payloads["chapter-plan.json"]["task_id"],
            "run_id": payloads["topic-outline.json"]["run_id"],
            "strategy_version": STRATEGY_VERSION,
            "timeline_sha256": payloads["topic-outline.json"]["timeline_sha256"],
            "provider": payloads["topic-outline.json"]["provider"],
            "versions": {
                "topic_prompt": TOPIC_PROMPT_VERSION,
                "boundary_prompt": BOUNDARY_PROMPT_VERSION,
                "topic_validation": TOPIC_VALIDATION_VERSION,
                "boundary_validation": BOUNDARY_VALIDATION_VERSION,
            },
            "files": {name: _sha256_file(stage / name) for name in CORE_FILES},
        }
        _write_json(stage / "artifact-manifest.json", manifest)
        if target.exists():
            quarantine = target.parent / f".{target.name}.invalid-{uuid.uuid4().hex}"
            os.replace(target, quarantine)
        os.replace(stage, target)
    except OSError as exc:
        if quarantine is not None and quarantine.exists() and not target.exists():
            os.replace(quarantine, target)
        raise TopicAnchorError("无法原子发布anchor-v2成功产物组。", failure_type="artifact_publish_failed") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _load_cached_group(
    target: Path,
    timeline: dict[str, Any],
    task_id: str,
    run_id: str,
    provider: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        required = [*CORE_FILES, "artifact-manifest.json", "execution-report.json", "validation-audit.json"]
        if not target.is_dir() or any(not (target / name).is_file() for name in required):
            return None
        manifest = json.loads((target / "artifact-manifest.json").read_text(encoding="utf-8"))
        expected_versions = {
            "topic_prompt": TOPIC_PROMPT_VERSION,
            "boundary_prompt": BOUNDARY_PROMPT_VERSION,
            "topic_validation": TOPIC_VALIDATION_VERSION,
            "boundary_validation": BOUNDARY_VALIDATION_VERSION,
        }
        if (
            manifest.get("status") != "success"
            or manifest.get("task_id") != task_id
            or manifest.get("run_id") != run_id
            or manifest.get("strategy_version") != STRATEGY_VERSION
            or manifest.get("timeline_sha256") != _timeline_sha256(timeline)
            or manifest.get("provider") != provider
            or manifest.get("versions") != expected_versions
        ):
            return None
        if any(manifest.get("files", {}).get(name) != _sha256_file(target / name) for name in CORE_FILES):
            return None
        outline_payload = json.loads((target / "topic-outline.json").read_text(encoding="utf-8"))
        alignment_payload = json.loads((target / "boundary-alignment.json").read_text(encoding="utf-8"))
        sidecar = json.loads((target / "quality-sidecar.json").read_text(encoding="utf-8"))
        plan = json.loads((target / "chapter-plan.json").read_text(encoding="utf-8"))
        for payload in (outline_payload, alignment_payload, sidecar):
            if (
                payload.get("task_id") != task_id
                or payload.get("run_id") != run_id
                or payload.get("strategy_version") != STRATEGY_VERSION
                or payload.get("timeline_sha256") != _timeline_sha256(timeline)
                or payload.get("provider") != provider
            ):
                return None
        outline = {"topics": outline_payload["topics"]}
        alignment = {"transitions": alignment_payload["transitions"]}
        normalized_outline = normalize_topic_anchor_response(outline, timeline)
        intervals = build_anchor_intervals(normalized_outline, timeline)
        normalized_alignment = normalize_anchor_alignment_response(
            alignment, normalized_outline, intervals, timeline
        )
        if alignment_payload.get("intervals") != intervals:
            return None
        if sidecar.get("topics") != normalized_outline["topics"]:
            return None
        if sidecar.get("transitions") != normalized_alignment["transitions"]:
            return None
        rebuilt = build_anchor_chapter_plan(
            task_id, run_id, timeline, normalized_outline, normalized_alignment
        )
        if rebuilt != plan or sidecar.get("chapter_plan_sha256") != _sha256_value(plan):
            return None
        if sidecar.get("quality_status") not in {"passed", "merged"}:
            return None
        return plan
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TopicAnchorError, ChapterPlanError):
        return None


def _write_failure(
    root: Path,
    task_id: str,
    run_id: str,
    timeline: dict[str, Any],
    failure_type: str,
    message: str,
    stats: dict[str, int],
) -> Path:
    digest = hashlib.sha256(f"{failure_type}:{message}".encode("utf-8")).hexdigest()[:12]
    failure_dir = root / "failures" / run_id / digest
    failure_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "task_id": task_id,
        "run_id": run_id,
        "strategy_version": STRATEGY_VERSION,
        "timeline_sha256": _timeline_sha256(timeline),
        "mode": "experiment",
        "status": "failed",
        "result_source": "none",
        "quality_level": "unknown",
        "failure_type": failure_type,
        "failure_reason": message,
        "recovery_triggered": False,
        "fallback_triggered": False,
        "emergency_triggered": False,
        "chapter_plan_created": False,
        **stats,
    }
    _write_json(failure_dir / "execution-report.json", payload)
    _write_json(failure_dir / "validation-audit.json", payload)
    return failure_dir / "execution-report.json"


def plan_task_chapters_anchor_v2(
    output_root: str | Path,
    task_id: str,
    *,
    run_id: str,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
) -> TopicAnchorResult:
    """执行严格实验primary；失败不恢复、不fallback且不生成ChapterPlan。"""

    validate_experiment_id(run_id)
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise TopicAnchorError(str(exc), failure_type="invalid_timeline") from exc
    duration = float(timeline["duration_seconds"])
    if duration > MAX_DURATION_SECONDS:
        raise TopicAnchorError(
            "unsupported_duration：anchor-v2当前只验收30分钟以内视频。",
            failure_type="unsupported_duration",
        )
    resolved_config = config or load_llm_config(dotenv_path=dotenv_path)
    provider = resolved_config.public_dict()
    if resolved_config.temperature != 0.0 or resolved_config.enable_thinking is not False:
        raise TopicAnchorError(
            "anchor-v2实验要求temperature=0且不得启用thinking。",
            failure_type="invalid_model_parameters",
        )
    root = _run_root(output_root, task_id)
    target = root / run_id
    cached_plan = _load_cached_group(target, timeline, task_id, run_id, provider)
    if cached_plan is not None:
        return TopicAnchorResult(
            run_directory=target,
            plan_path=target / "chapter-plan.json",
            boundary_count=len(cached_plan["boundaries"]),
            duration_seconds=duration,
            business_call_count=0,
            provider_request_count=0,
            correction_count=0,
            llm_cache_hit_count=2,
            artifact_cache_hit=True,
        )

    business_calls = 0
    provider_requests = 0
    corrections = 0
    cache_hits = 0
    stats = lambda: {
        "business_call_count": business_calls,
        "provider_request_count": provider_requests,
        "correction_count": corrections,
        "llm_cache_hit_count": cache_hits,
    }
    call_subdir = (
        Path("experiments") / "stage8" / "two-stage" / "anchor-v2" / "calls" / run_id
    )
    cache_namespace = f"anchor_v2_{run_id}_{_timeline_sha256(timeline)[:12]}"
    try:
        def validate_outline(response: dict[str, Any]) -> dict[str, Any]:
            try:
                return normalize_topic_anchor_response(response, timeline)
            except TopicAnchorError as exc:
                raise LLMRetryableError(str(exc)) from exc

        business_calls += 1
        outline_result = call_task_llm(
            output_root,
            task_id,
            call_id=OUTLINE_CALL_ID,
            messages=build_topic_anchor_messages(timeline),
            response_format={"type": "json_object"},
            config=resolved_config,
            transport=transport,
            dotenv_path=dotenv_path,
            response_validator=validate_outline,
            validation_id=TOPIC_VALIDATION_VERSION,
            artifact_subdir=call_subdir,
            cache_namespace=cache_namespace,
            save_validation_audit=True,
        )
        cache_hits += int(outline_result.cache_hit)
        if not outline_result.cache_hit:
            provider_requests += outline_result.attempt_count
            corrections += max(0, outline_result.attempt_count - 1)
        outline = outline_result.response
        intervals = build_anchor_intervals(outline, timeline)

        def validate_alignment(response: dict[str, Any]) -> dict[str, Any]:
            try:
                normalized = normalize_anchor_alignment_response(
                    response, outline, intervals, timeline
                )
                build_anchor_chapter_plan(task_id, run_id, timeline, outline, normalized)
                return normalized
            except TopicAnchorError as exc:
                if exc.failure_type in {"uncertain", "invalid_anchor"}:
                    return normalize_anchor_alignment_response(
                        response, outline, intervals, timeline
                    )
                raise LLMRetryableError(str(exc)) from exc
            except ChapterPlanError as exc:
                raise LLMRetryableError(str(exc)) from exc

        business_calls += 1
        alignment_result = call_task_llm(
            output_root,
            task_id,
            call_id=ALIGNMENT_CALL_ID,
            messages=build_anchor_alignment_messages(intervals),
            response_format={"type": "json_object"},
            config=resolved_config,
            transport=transport,
            dotenv_path=dotenv_path,
            response_validator=validate_alignment,
            validation_id=BOUNDARY_VALIDATION_VERSION,
            artifact_subdir=call_subdir,
            cache_namespace=cache_namespace,
            save_validation_audit=True,
        )
        cache_hits += int(alignment_result.cache_hit)
        if not alignment_result.cache_hit:
            provider_requests += alignment_result.attempt_count
            corrections += max(0, alignment_result.attempt_count - 1)
        alignment = alignment_result.response
        plan = build_anchor_chapter_plan(task_id, run_id, timeline, outline, alignment)
        payloads = _artifact_payloads(
            task_id,
            run_id,
            timeline,
            outline,
            intervals,
            alignment,
            plan,
            stats(),
            provider,
        )
        _publish_success_group(target, payloads)
        verified = _load_cached_group(
            target, timeline, task_id, run_id, provider
        )
        if verified is None:
            raise TopicAnchorError("发布后的成功产物组复验失败。", failure_type="artifact_publish_failed")
        return TopicAnchorResult(
            run_directory=target,
            plan_path=target / "chapter-plan.json",
            boundary_count=len(plan["boundaries"]),
            duration_seconds=duration,
            artifact_cache_hit=False,
            **stats(),
        )
    except TopicAnchorError as exc:
        audit = _write_failure(root, task_id, run_id, timeline, exc.failure_type, str(exc), stats())
        exc.audit_path = audit
        raise
    except LLMAdapterError as exc:
        message = str(exc)
        provider_requests += 1 if isinstance(exc, LLMNonRetryableError) else 2
        current_audits = list((Path(output_root).expanduser() / task_id / call_subdir).glob("*-validation-audit.json"))
        if current_audits:
            corrections += 1
        audit = _write_failure(root, task_id, run_id, timeline, "llm_call_failed", message, stats())
        raise TopicAnchorError(message, failure_type="llm_call_failed", audit_path=audit) from exc
