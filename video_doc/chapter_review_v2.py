"""阶段8：Review v2简化契约与本地确定性差异审计。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from video_doc.chapter_policy import STABILITY_POLICY_VERSION, render_stability_policy
from video_doc.chapter_review import (
    MAX_REVIEW_ISSUES,
    ChapterReviewError,
    ChapterReviewResult,
    _canonical_sha256,
    _file_sha256,
    _load_inputs,
    _redact,
    _review_boundaries,
    _write_json_if_changed,
)
from video_doc.llm_adapter import (
    LLMConfig,
    LLMTransport,
    call_task_llm,
    load_llm_config,
)
from video_doc.llm_strategy import (
    STABILITY_DIRECT_EXPERIMENT_VERSION,
    ChapterPlanError,
    normalize_event_id_chapter_response,
    validate_chapter_plan,
)


REVIEW_V2_PROMPT_VERSION = "chapter-plan-evidence-review-2.0"
REVIEW_V2_VALIDATION_VERSION = "chapter-plan-review-v2"
REVIEW_V2_EXPERIMENT_VERSION = "evidence-review-v2"
REVIEW_V2_CALL_ID = "review-result"
_TOP_LEVEL_FIELDS = {"verdict", "issues", "revised_boundaries"}
_ISSUE_FIELDS = {"evidence_event_ids", "reason"}
_SHORT_REASON_MARKERS = (
    "章节过短",
    "低于最短",
    "少于最短",
    "不满足最短",
    "未达到最短",
)


def _review_v2_directory(
    output_root: str | Path, task_id: str, source_run_id: str
) -> Path:
    return (
        Path(output_root).expanduser()
        / task_id
        / "experiments"
        / "stage8"
        / "review"
        / REVIEW_V2_EXPERIMENT_VERSION
        / source_run_id
    )


def _render_timeline(timeline: dict[str, Any]) -> str:
    return "\n".join(
        f"[{event['id']}] {float(event['start']):.3f}-{float(event['end']):.3f} "
        f"{event['text']}"
        for event in timeline["events"]
    )


def build_review_v2_messages(
    timeline: dict[str, Any],
    source_plan: dict[str, Any],
    *,
    source_plan_sha256: str | None = None,
    timeline_sha256: str | None = None,
) -> list[dict[str, str]]:
    """构造只要求最终计划、证据和理由的v2审阅提示词。"""

    minimum = float(source_plan["parameters"]["min_chapter_seconds"])
    source_boundaries = _review_boundaries(source_plan, timeline)
    source_hash = source_plan_sha256 or _canonical_sha256(source_plan)
    timeline_hash = timeline_sha256 or _canonical_sha256(timeline)
    system = (
        "你是章节计划审阅器。只依据给出的完整ASR证据检查既有计划；"
        "正确时接受，仅在一级主题边界或标题确有问题时做最小修订。"
        "只输出JSON对象，不要输出Markdown或额外解释。"
    )
    examples = (
        "示例1（接受）："
        '{"verdict":"accept","issues":[],"revised_boundaries":null}\n'
        "示例2（只删除一个边界）："
        '{"verdict":"revise","issues":[{"evidence_event_ids":["x0019","x0020"],'
        '"reason":"两段仍属同一一级主题"}],"revised_boundaries":['
        '{"source_event_id":"x0001","title":"背景"},'
        '{"source_event_id":"x0030","title":"方法"}]}\n'
        "示例3（只替换一个边界）："
        '{"verdict":"revise","issues":[{"evidence_event_ids":["x0019","x0020","x0021"],'
        '"reason":"新主题从更早的明确引入句开始"}],"revised_boundaries":['
        '{"source_event_id":"x0001","title":"背景"},'
        '{"source_event_id":"x0020","title":"方法"}]}\n'
        "示例4（同时删除旧边界并新增更准确边界）："
        '{"verdict":"revise","issues":[{"evidence_event_ids":["x0018","x0019","x0020"],'
        '"reason":"旧边界仍是过渡句，下一段才明确引入一级主题"}],'
        '"revised_boundaries":[{"source_event_id":"x0001","title":"背景"},'
        '{"source_event_id":"x0020","title":"新主题"}]}'
    )
    user = (
        f"审阅提示词版本：{REVIEW_V2_PROMPT_VERSION}。\n"
        f"共用简洁分章政策版本：{STABILITY_POLICY_VERSION}。\n"
        f"来源A计划SHA-256：{source_hash}。\n"
        f"时间轴SHA-256：{timeline_hash}。\n"
        f"{render_stability_policy(float(timeline['duration_seconds']))}\n"
        "章节标题描述的是该边界开始后、直到下一边界前的下方内容。"
        "不要为局部例子或短过渡刻意分章，也不能排序边界后重新绑定标题。\n"
        "只输出完整最终计划，不要结构化描述原计划与最终计划之间的差异。"
        "evidence_event_ids只用于证明reason，不表示最终边界列表。"
        "无法确定是否需要修改时优先accept，不要为了体现审阅作用而强行修改。\n"
        "verdict只能是accept或revise。accept时issues必须为空且"
        "revised_boundaries必须为null；revise时返回完整修订计划和1至4条issue。"
        "每条issue只能包含evidence_event_ids和reason，证据必须来自ASR。"
        f"修订后每章（包括末章）至少{minimum:.3f}秒，第一章必须使用首个ASR事件。"
        "不得声称某章违反最短时长，除非给出的起止时间确实支持。\n"
        f"{examples}\n"
        "既有A计划：\n"
        f"{json.dumps(source_boundaries, ensure_ascii=False, separators=(',', ':'))}\n"
        "完整ASR时间轴：\n"
        f"{_render_timeline(timeline)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _normalize_issue(
    issue: Any, index: int, event_ids: set[str]
) -> dict[str, Any]:
    if not isinstance(issue, dict):
        raise ChapterReviewError(f"第{index}条issue必须是JSON对象。")
    unknown = set(issue) - _ISSUE_FIELDS
    missing_fields = _ISSUE_FIELDS - set(issue)
    if unknown or missing_fields:
        raise ChapterReviewError(
            f"第{index}条issue只能包含evidence_event_ids和reason。"
        )
    evidence = issue["evidence_event_ids"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
        raise ChapterReviewError(f"第{index}条issue必须包含1至8个证据事件ID。")
    if any(not isinstance(item, str) or not item.strip() for item in evidence):
        raise ChapterReviewError(f"第{index}条issue包含无效证据事件ID。")
    normalized_evidence = [item.strip() for item in evidence]
    if len(normalized_evidence) != len(set(normalized_evidence)):
        raise ChapterReviewError(f"第{index}条issue包含重复证据事件ID。")
    missing = [event_id for event_id in normalized_evidence if event_id not in event_ids]
    if missing:
        raise ChapterReviewError(f"第{index}条issue引用不存在的事件ID：{missing[0]}。")
    reason = issue["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ChapterReviewError(f"第{index}条issue的reason不能为空。")
    return {"evidence_event_ids": normalized_evidence, "reason": reason.strip()}


def _difference_audit(
    source_boundaries: list[dict[str, str]],
    revised_boundaries: list[dict[str, str]],
    timeline: dict[str, Any],
    issues: list[dict[str, Any]],
    minimum: float,
) -> dict[str, Any]:
    source_by_id = {item["source_event_id"]: item["title"] for item in source_boundaries}
    revised_by_id = {item["source_event_id"]: item["title"] for item in revised_boundaries}
    source_ids = list(source_by_id)
    revised_ids = list(revised_by_id)
    removed = [event_id for event_id in source_ids if event_id not in revised_by_id]
    added = [event_id for event_id in revised_ids if event_id not in source_by_id]
    retained = [event_id for event_id in source_ids if event_id in revised_by_id]
    renamed = [
        {
            "source_event_id": event_id,
            "original_title": source_by_id[event_id],
            "revised_title": revised_by_id[event_id],
        }
        for event_id in retained
        if source_by_id[event_id] != revised_by_id[event_id]
    ]
    delta = len(revised_boundaries) - len(source_boundaries)
    change_kinds = sum((bool(removed or added), bool(renamed)))
    if renamed and not removed and not added:
        labels = ["title-change"]
    elif delta == 0 and removed and added and not renamed:
        labels = ["boundary-replacement"]
    elif delta < 0 and change_kinds == 1:
        labels = ["chapter-count-decrease"]
    elif delta > 0 and change_kinds == 1:
        labels = ["chapter-count-increase"]
    else:
        labels = ["compound-change"]

    positions = {
        str(event["id"]): position for position, event in enumerate(timeline["events"])
    }
    all_evidence = {
        event_id for issue in issues for event_id in issue["evidence_event_ids"]
    }
    changed_event_ids = removed + added + [item["source_event_id"] for item in renamed]
    uncovered = [
        event_id
        for event_id in changed_event_ids
        if not any(abs(positions[event_id] - positions[evidence_id]) <= 2 for evidence_id in all_evidence)
    ]
    if uncovered:
        raise ChapterReviewError(f"issue证据未覆盖变化位置：{uncovered[0]}。")

    reason_warnings: list[dict[str, Any]] = []
    for index, issue in enumerate(issues, start=1):
        if any(marker in issue["reason"] for marker in _SHORT_REASON_MARKERS):
            reason_warnings.append(
                {
                    "issue_index": index,
                    "code": "short-chapter-claim-conflicts-with-valid-plan",
                    "message": (
                        f"来源计划已通过每章至少{minimum:.3f}秒的硬校验，"
                        "该理由仍声称章节过短，需人工复核其语义。"
                    ),
                }
            )
    return {
        "removed": removed,
        "added": added,
        "retained": retained,
        "renamed": renamed,
        "chapter_count_delta": delta,
        "labels": labels,
        "reason_warnings": reason_warnings,
    }


def normalize_review_v2_response(
    response: Any,
    timeline: dict[str, Any],
    source_plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """校验v2响应，并返回最终计划和本地确定性差异。"""

    if not isinstance(response, dict):
        raise ChapterReviewError("审阅响应必须是JSON对象。")
    if set(response) != _TOP_LEVEL_FIELDS:
        raise ChapterReviewError(
            "v2响应只能包含verdict、issues和revised_boundaries。"
        )
    verdict = response["verdict"]
    issues = response["issues"]
    revised = response["revised_boundaries"]
    if verdict not in {"accept", "revise"}:
        raise ChapterReviewError("审阅verdict必须是accept或revise。")
    if not isinstance(issues, list):
        raise ChapterReviewError("审阅issues必须是数组。")
    if verdict == "accept":
        if issues or revised is not None:
            raise ChapterReviewError("accept时issues必须为空且修订边界必须为null。")
        difference = {
            "removed": [],
            "added": [],
            "retained": [
                item["source_event_id"] for item in _review_boundaries(source_plan, timeline)
            ],
            "renamed": [],
            "chapter_count_delta": 0,
            "labels": [],
            "reason_warnings": [],
        }
        return copy.deepcopy(response), copy.deepcopy(source_plan), difference
    if not 1 <= len(issues) <= MAX_REVIEW_ISSUES:
        raise ChapterReviewError("revise时必须包含1至4条issue。")
    if not isinstance(revised, list) or not revised:
        raise ChapterReviewError("revise时必须返回完整revised_boundaries。")

    event_ids = {str(event["id"]) for event in timeline["events"]}
    normalized_issues = [
        _normalize_issue(issue, index, event_ids)
        for index, issue in enumerate(issues, start=1)
    ]
    try:
        normalized_boundaries = normalize_event_id_chapter_response(
            {"boundaries": revised},
            timeline,
            min_chapter_seconds=source_plan["parameters"]["min_chapter_seconds"],
        )["boundaries"]
    except ChapterPlanError as exc:
        raise ChapterReviewError(str(exc)) from exc

    first_event_id = str(timeline["events"][0]["id"])
    revised_review = [
        {
            "source_event_id": (
                first_event_id if index == 0 else str(boundary["source_event_id"])
            ),
            "title": str(boundary["title"]),
        }
        for index, boundary in enumerate(normalized_boundaries)
    ]
    source_boundaries = _review_boundaries(source_plan, timeline)
    if revised_review == source_boundaries:
        raise ChapterReviewError("revise未实际修改章节计划。")
    difference = _difference_audit(
        source_boundaries,
        revised_review,
        timeline,
        normalized_issues,
        float(source_plan["parameters"]["min_chapter_seconds"]),
    )
    reviewed_plan = copy.deepcopy(source_plan)
    reviewed_plan["boundaries"] = normalized_boundaries
    normalized_response = {
        "verdict": "revise",
        "issues": normalized_issues,
        "revised_boundaries": revised_review,
    }
    return normalized_response, reviewed_plan, difference


def review_task_chapters_v2(
    output_root: str | Path,
    task_id: str,
    *,
    source_run_id: str,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
) -> ChapterReviewResult:
    """使用v2契约审阅来源A；非法响应保存审计并回退。"""

    source_plan, timeline, source_path, source_hash, timeline_hash = _load_inputs(
        output_root, task_id, source_run_id
    )
    resolved_config = config or load_llm_config(dotenv_path=dotenv_path)
    review_dir = _review_v2_directory(output_root, task_id, source_run_id)
    reviewed_path = review_dir / "chapter-plan.reviewed.json"
    audit_path = review_dir / "review-audit.json"
    failure_path = review_dir / "review-failure.json"
    messages = build_review_v2_messages(
        timeline,
        source_plan,
        source_plan_sha256=source_hash,
        timeline_sha256=timeline_hash,
    )
    call_result = call_task_llm(
        output_root,
        task_id,
        call_id=REVIEW_V2_CALL_ID,
        messages=messages,
        response_format={"type": "json_object"},
        config=resolved_config,
        transport=transport,
        response_validator=lambda response: response,
        validation_id=REVIEW_V2_VALIDATION_VERSION,
        artifact_subdir=(
            Path("experiments")
            / "stage8"
            / "review"
            / REVIEW_V2_EXPERIMENT_VERSION
            / source_run_id
        ),
        cache_namespace=f"review-v2-{source_run_id}",
    )
    try:
        normalized, reviewed_plan, difference = normalize_review_v2_response(
            call_result.response, timeline, source_plan
        )
        reviewed_plan["parameters"] = {
            **reviewed_plan["parameters"],
            "review_prompt_version": REVIEW_V2_PROMPT_VERSION,
            "review_validation_version": REVIEW_V2_VALIDATION_VERSION,
            "review_experiment_version": REVIEW_V2_EXPERIMENT_VERSION,
            "review_source_experiment_id": source_run_id,
            "review_source_plan_sha256": source_hash,
            "review_verdict": normalized["verdict"],
        }
        validate_chapter_plan(reviewed_plan, timeline=timeline, expected_task_id=task_id)
        plan_cache_hit = _write_json_if_changed(reviewed_path, reviewed_plan)
        audit = {
            "schema_version": "1.0",
            "task_id": task_id,
            "status": "completed",
            "verdict": normalized["verdict"],
            "issues": normalized["issues"],
            "difference": difference,
            "source": {
                "experiment_id": source_run_id,
                "experiment_version": STABILITY_DIRECT_EXPERIMENT_VERSION,
                "plan_sha256": source_hash,
                "timeline_sha256": timeline_hash,
            },
            "review": {
                "prompt_version": REVIEW_V2_PROMPT_VERSION,
                "validation_version": REVIEW_V2_VALIDATION_VERSION,
                "experiment_version": REVIEW_V2_EXPERIMENT_VERSION,
                "chapter_policy_version": STABILITY_POLICY_VERSION,
                "reviewed_plan_sha256": _canonical_sha256(reviewed_plan),
            },
        }
        _write_json_if_changed(audit_path, audit)
        try:
            failure_path.unlink(missing_ok=True)
        except OSError as exc:
            raise ChapterReviewError("无法清理旧的v2审阅失败审计。") from exc
        return ChapterReviewResult(
            status="completed",
            selected_plan_path=reviewed_path,
            reviewed_plan_path=reviewed_path,
            audit_path=audit_path,
            verdict=normalized["verdict"],
            issue_count=len(normalized["issues"]),
            attempt_count=call_result.attempt_count,
            llm_cache_hit=call_result.cache_hit,
            plan_cache_hit=plan_cache_hit,
            fallback_used=False,
        )
    except (ChapterPlanError, ChapterReviewError) as exc:
        try:
            reviewed_path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            raise ChapterReviewError("无法移除无效的v2 reviewed计划。") from unlink_exc
        failure = {
            "schema_version": "1.0",
            "task_id": task_id,
            "status": "failed",
            "fallback": "source-a-plan",
            "error": str(exc),
            "response": _redact(call_result.response, resolved_config.api_key),
            "source": {
                "experiment_id": source_run_id,
                "experiment_version": STABILITY_DIRECT_EXPERIMENT_VERSION,
                "plan_sha256": source_hash,
                "timeline_sha256": timeline_hash,
            },
            "review": {
                "prompt_version": REVIEW_V2_PROMPT_VERSION,
                "validation_version": REVIEW_V2_VALIDATION_VERSION,
                "experiment_version": REVIEW_V2_EXPERIMENT_VERSION,
            },
        }
        _write_json_if_changed(failure_path, failure)
        return ChapterReviewResult(
            status="failed",
            selected_plan_path=source_path,
            reviewed_plan_path=None,
            audit_path=failure_path,
            verdict=None,
            issue_count=0,
            attempt_count=call_result.attempt_count,
            llm_cache_hit=call_result.cache_hit,
            plan_cache_hit=False,
            fallback_used=True,
        )
