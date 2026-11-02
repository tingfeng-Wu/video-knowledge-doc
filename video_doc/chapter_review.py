"""阶段8：对既有方案A章节计划执行一次证据约束审阅。"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_doc.asr_timeline import ASRTimelineError, load_task_timeline
from video_doc.chapter_policy import STABILITY_POLICY_VERSION, render_stability_policy
from video_doc.llm_adapter import (
    LLMAdapterError,
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
    validate_experiment_id,
)


REVIEW_PROMPT_VERSION = "chapter-plan-evidence-review-1.0"
REVIEW_VALIDATION_VERSION = "chapter-plan-review-v1"
REVIEW_EXPERIMENT_VERSION = "evidence-review-v1"
REVIEW_CALL_ID = "review-result"
MAX_REVIEW_ISSUES = 4
_OPERATIONS = {"move", "merge", "split", "rename"}


class ChapterReviewError(RuntimeError):
    """审阅输入、响应或产物处理失败。"""


@dataclass(frozen=True)
class ChapterReviewResult:
    """一次A+Review执行结果；失败时明确回退到来源A计划。"""

    status: str
    selected_plan_path: Path
    reviewed_plan_path: Path | None
    audit_path: Path
    verdict: str | None
    issue_count: int
    attempt_count: int
    llm_cache_hit: bool
    plan_cache_hit: bool
    fallback_used: bool


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_directory(output_root: str | Path, task_id: str, source_run_id: str) -> Path:
    return (
        Path(output_root).expanduser()
        / task_id
        / "experiments"
        / "stage8"
        / "review"
        / REVIEW_EXPERIMENT_VERSION
        / source_run_id
    )


def _source_plan_path(output_root: str | Path, task_id: str, source_run_id: str) -> Path:
    return (
        Path(output_root).expanduser()
        / task_id
        / "experiments"
        / "stage8"
        / "direct"
        / STABILITY_DIRECT_EXPERIMENT_VERSION
        / source_run_id
        / "chapter-plan.json"
    )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ChapterReviewError(f"无法读取{label}。") from exc
    except json.JSONDecodeError as exc:
        raise ChapterReviewError(f"{label}不是合法JSON。") from exc
    if not isinstance(payload, dict):
        raise ChapterReviewError(f"{label}顶层必须是JSON对象。")
    return payload


def _write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        if path.is_file() and path.read_bytes() == data:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError as exc:
        raise ChapterReviewError(f"无法保存审阅产物：{path.name}") from exc
    finally:
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item, secret) for key, item in value.items()}
    return value


def _review_boundaries(source_plan: dict[str, Any], timeline: dict[str, Any]) -> list[dict[str, str]]:
    first_event_id = str(timeline["events"][0]["id"])
    result: list[dict[str, str]] = []
    for index, boundary in enumerate(source_plan["boundaries"]):
        event_id = first_event_id if index == 0 else boundary["source_event_id"]
        result.append({"source_event_id": str(event_id), "title": boundary["title"]})
    return result


def _render_timeline(timeline: dict[str, Any]) -> str:
    return "\n".join(
        f"[{event['id']}] {float(event['start']):.3f}-{float(event['end']):.3f} "
        f"{event['text']}"
        for event in timeline["events"]
    )


def build_review_messages(
    timeline: dict[str, Any],
    source_plan: dict[str, Any],
    *,
    source_plan_sha256: str | None = None,
    timeline_sha256: str | None = None,
) -> list[dict[str, str]]:
    """构造不含文件路径、方案B或预设缺陷答案的审阅提示词。"""

    minimum = float(source_plan["parameters"]["min_chapter_seconds"])
    source_boundaries = _review_boundaries(source_plan, timeline)
    source_hash = source_plan_sha256 or _canonical_sha256(source_plan)
    timeline_hash = timeline_sha256 or _canonical_sha256(timeline)
    system = (
        "你是章节计划审阅器。只依据给出的完整ASR证据检查既有计划；"
        "正确时接受，仅在一级主题边界或标题确有问题时做最小修订。"
        "只输出JSON对象，不要输出Markdown或额外解释。"
    )
    user = (
        f"审阅提示词版本：{REVIEW_PROMPT_VERSION}。\n"
        f"共用简洁分章政策版本：{STABILITY_POLICY_VERSION}。\n"
        f"来源A计划SHA-256：{source_hash}。\n"
        f"时间轴SHA-256：{timeline_hash}。\n"
        f"{render_stability_policy(float(timeline['duration_seconds']))}\n"
        "章节标题描述的是该边界开始后、直到下一边界前的下方内容。"
        "不要为了局部例子刻意分章；不能通过排序边界来重新绑定标题。\n"
        "verdict只能是accept或revise。accept时issues必须为空且"
        "revised_boundaries必须为null；revise时返回完整修订计划和1至4条issue。"
        "operation只能是move、merge、split、rename。每条issue必须给出"
        "chapter_indexes、original_event_ids、proposed_event_ids、"
        "evidence_event_ids和reason；所有事件ID必须来自ASR。"
        f"修订后每章（包括末章）至少{minimum:.3f}秒，第一章必须使用首个ASR事件。\n"
        "返回格式："
        '{"verdict":"accept","issues":[],"revised_boundaries":null}'
        "，或"
        '{"verdict":"revise","issues":[{"operation":"move",'
        '"chapter_indexes":[2],"original_event_ids":["a0010"],'
        '"proposed_event_ids":["a0012"],"evidence_event_ids":["a0011","a0012"],'
        '"reason":"..."}],"revised_boundaries":'
        '[{"source_event_id":"a0001","title":"..."}]}。\n'
        "既有A计划：\n"
        f"{json.dumps(source_boundaries, ensure_ascii=False, separators=(',', ':'))}\n"
        "完整ASR时间轴：\n"
        f"{_render_timeline(timeline)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _id_list(issue: dict[str, Any], field: str, index: int) -> list[str]:
    value = issue.get(field)
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ChapterReviewError(f"第{index}条issue的{field}必须是非空事件ID数组。")
    result = [item.strip() for item in value]
    if len(result) != len(set(result)):
        raise ChapterReviewError(f"第{index}条issue的{field}包含重复事件ID。")
    return result


def normalize_review_response(
    response: Any,
    timeline: dict[str, Any],
    source_plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验审阅响应并返回规范响应、可验证的完整ChapterPlan。"""

    if not isinstance(response, dict):
        raise ChapterReviewError("审阅响应必须是JSON对象。")
    verdict = response.get("verdict")
    issues = response.get("issues")
    revised = response.get("revised_boundaries")
    if verdict not in {"accept", "revise"}:
        raise ChapterReviewError("审阅verdict必须是accept或revise。")
    if not isinstance(issues, list):
        raise ChapterReviewError("审阅issues必须是数组。")
    if verdict == "accept":
        if issues or revised is not None:
            raise ChapterReviewError("accept时issues必须为空且不得包含修订边界。")
        normalized_response = {"verdict": "accept", "issues": [], "revised_boundaries": None}
        return normalized_response, copy.deepcopy(source_plan)
    if not 1 <= len(issues) <= MAX_REVIEW_ISSUES:
        raise ChapterReviewError("revise时必须包含1至4条issue。")
    if not isinstance(revised, list) or not revised:
        raise ChapterReviewError("revise时必须返回完整revised_boundaries。")

    event_ids = {str(event["id"]) for event in timeline["events"]}
    source_boundaries = _review_boundaries(source_plan, timeline)
    source_ids = [item["source_event_id"] for item in source_boundaries]
    try:
        normalized_boundaries = normalize_event_id_chapter_response(
            {"boundaries": revised},
            timeline,
            min_chapter_seconds=source_plan["parameters"]["min_chapter_seconds"],
        )["boundaries"]
    except ChapterPlanError as exc:
        raise ChapterReviewError(str(exc)) from exc
    revised_review = [
        {
            "source_event_id": source_ids[0] if index == 0 else boundary["source_event_id"],
            "title": boundary["title"],
        }
        for index, boundary in enumerate(normalized_boundaries)
    ]
    revised_ids = [item["source_event_id"] for item in revised_review]
    normalized_issues: list[dict[str, Any]] = []
    operations: set[str] = set()
    traced_ids: set[str] = set()
    traced_chapter_indexes: set[int] = set()
    max_chapter_index = max(len(source_boundaries), len(revised))
    for index, issue in enumerate(issues, start=1):
        if not isinstance(issue, dict):
            raise ChapterReviewError(f"第{index}条issue必须是JSON对象。")
        operation = issue.get("operation")
        if operation not in _OPERATIONS:
            raise ChapterReviewError(f"第{index}条issue操作无效。")
        chapter_indexes = issue.get("chapter_indexes")
        if not isinstance(chapter_indexes, list) or not chapter_indexes or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1 or item > max_chapter_index
            for item in chapter_indexes
        ):
            raise ChapterReviewError(f"第{index}条issue的chapter_indexes无效。")
        original_ids = _id_list(issue, "original_event_ids", index)
        proposed_ids = _id_list(issue, "proposed_event_ids", index)
        evidence_ids = _id_list(issue, "evidence_event_ids", index)
        reason = issue.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ChapterReviewError(f"第{index}条issue的reason不能为空。")
        all_ids = original_ids + proposed_ids + evidence_ids
        missing = [event_id for event_id in all_ids if event_id not in event_ids]
        if missing:
            raise ChapterReviewError(f"第{index}条issue引用不存在的事件ID：{missing[0]}。")
        if any(event_id not in source_ids for event_id in original_ids):
            raise ChapterReviewError(f"第{index}条issue的original_event_ids与来源计划不一致。")
        if any(event_id not in revised_ids for event_id in proposed_ids):
            raise ChapterReviewError(f"第{index}条issue的proposed_event_ids与修订计划不一致。")
        if operation == "merge" and len(original_ids) < 2:
            raise ChapterReviewError("merge必须引用至少两个原章节边界。")
        if operation == "split" and len(proposed_ids) < 2:
            raise ChapterReviewError("split必须引用至少两个修订章节边界。")
        operations.add(operation)
        traced_ids.update(original_ids)
        traced_ids.update(proposed_ids)
        traced_chapter_indexes.update(chapter_indexes)
        normalized_issues.append(
            {
                "operation": operation,
                "chapter_indexes": chapter_indexes,
                "original_event_ids": original_ids,
                "proposed_event_ids": proposed_ids,
                "evidence_event_ids": evidence_ids,
                "reason": reason.strip(),
            }
        )

    if revised_review == source_boundaries:
        raise ChapterReviewError("revise未实际修改章节计划。")
    if len(revised_review) < len(source_boundaries) and "merge" not in operations:
        raise ChapterReviewError("章节数减少必须由merge issue说明。")
    if len(revised_review) > len(source_boundaries) and "split" not in operations:
        raise ChapterReviewError("章节数增加必须由split issue说明。")
    changed_ids = set(source_ids).symmetric_difference(revised_ids)
    if len(revised_review) == len(source_boundaries) and changed_ids and "move" not in operations:
        raise ChapterReviewError("同章数边界变化必须由move issue说明。")
    source_titles_by_id = {
        item["source_event_id"]: item["title"] for item in source_boundaries
    }
    changed_title_indexes = {
        index
        for index, item in enumerate(revised_review, start=1)
        if item["source_event_id"] in source_titles_by_id
        and source_titles_by_id[item["source_event_id"]] != item["title"]
    }
    if not changed_title_indexes.issubset(traced_chapter_indexes):
        raise ChapterReviewError("issue的chapter_indexes未覆盖全部标题变化。")
    if not changed_ids.issubset(traced_ids):
        raise ChapterReviewError("issue未覆盖全部变化的事件ID。")

    reviewed_plan = copy.deepcopy(source_plan)
    reviewed_plan["boundaries"] = normalized_boundaries
    normalized_response = {
        "verdict": "revise",
        "issues": normalized_issues,
        "revised_boundaries": revised_review,
    }
    return normalized_response, reviewed_plan


def _load_inputs(
    output_root: str | Path, task_id: str, source_run_id: str
) -> tuple[dict[str, Any], dict[str, Any], Path, str, str]:
    validate_experiment_id(source_run_id)
    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterReviewError(str(exc)) from exc
    source_path = _source_plan_path(output_root, task_id, source_run_id)
    source_plan = _read_json(source_path, "来源A计划")
    try:
        validate_chapter_plan(source_plan, timeline=timeline, expected_task_id=task_id)
    except ChapterPlanError as exc:
        raise ChapterReviewError(f"来源A计划校验失败：{exc}") from exc
    parameters = source_plan.get("parameters", {})
    if parameters.get("experiment_id") != source_run_id or parameters.get(
        "experiment_version"
    ) != STABILITY_DIRECT_EXPERIMENT_VERSION:
        raise ChapterReviewError("来源计划不是指定run的事件ID版方案A。")
    timeline_hash = _canonical_sha256(timeline)
    if source_plan["source_timeline"]["sha256"] != timeline_hash:
        raise ChapterReviewError("来源A计划的时间轴哈希与当前时间轴不一致。")
    return source_plan, timeline, source_path, _file_sha256(source_path), timeline_hash


def review_task_chapters(
    output_root: str | Path,
    task_id: str,
    *,
    source_run_id: str,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
) -> ChapterReviewResult:
    """审阅一份既有A计划；业务响应无效时保存审计并安全回退。"""

    source_plan, timeline, source_path, source_hash, timeline_hash = _load_inputs(
        output_root, task_id, source_run_id
    )
    resolved_config = config or load_llm_config(dotenv_path=dotenv_path)
    review_dir = _review_directory(output_root, task_id, source_run_id)
    reviewed_path = review_dir / "chapter-plan.reviewed.json"
    audit_path = review_dir / "review-audit.json"
    failure_path = review_dir / "review-failure.json"
    messages = build_review_messages(
        timeline,
        source_plan,
        source_plan_sha256=source_hash,
        timeline_sha256=timeline_hash,
    )
    cache_namespace = f"review-v1-{source_run_id}"
    call_result = call_task_llm(
        output_root,
        task_id,
        call_id=REVIEW_CALL_ID,
        messages=messages,
        response_format={"type": "json_object"},
        config=resolved_config,
        transport=transport,
        response_validator=lambda response: response,
        validation_id=REVIEW_VALIDATION_VERSION,
        artifact_subdir=(
            Path("experiments")
            / "stage8"
            / "review"
            / REVIEW_EXPERIMENT_VERSION
            / source_run_id
        ),
        cache_namespace=cache_namespace,
    )
    try:
        normalized, reviewed_plan = normalize_review_response(
            call_result.response, timeline, source_plan
        )
        reviewed_plan["parameters"] = {
            **reviewed_plan["parameters"],
            "review_prompt_version": REVIEW_PROMPT_VERSION,
            "review_validation_version": REVIEW_VALIDATION_VERSION,
            "review_experiment_version": REVIEW_EXPERIMENT_VERSION,
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
            "source": {
                "experiment_id": source_run_id,
                "experiment_version": STABILITY_DIRECT_EXPERIMENT_VERSION,
                "plan_sha256": source_hash,
                "timeline_sha256": timeline_hash,
            },
            "review": {
                "prompt_version": REVIEW_PROMPT_VERSION,
                "validation_version": REVIEW_VALIDATION_VERSION,
                "experiment_version": REVIEW_EXPERIMENT_VERSION,
                "chapter_policy_version": STABILITY_POLICY_VERSION,
                "reviewed_plan_sha256": _canonical_sha256(reviewed_plan),
            },
        }
        _write_json_if_changed(audit_path, audit)
        try:
            failure_path.unlink(missing_ok=True)
        except OSError as exc:
            raise ChapterReviewError("无法清理旧的审阅失败审计。") from exc
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
            raise ChapterReviewError("无法移除无效的reviewed计划。") from unlink_exc
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
                "prompt_version": REVIEW_PROMPT_VERSION,
                "validation_version": REVIEW_VALIDATION_VERSION,
                "experiment_version": REVIEW_EXPERIMENT_VERSION,
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
