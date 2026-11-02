"""阶段8冻结后的正式LLM分章编排与显式降级。"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_doc.asr_timeline import ASRTimelineError, load_task_timeline
from video_doc.llm_adapter import LLMConfig, LLMTransport
from video_doc.llm_strategy import (
    STABILITY_MIN_CHAPTER_SECONDS,
    ChapterPlanError,
    ChapterPlanResult,
    plan_task_chapters,
    validate_chapter_plan,
)
from video_doc.two_stage_llm_strategy import plan_task_chapters_two_stage


FORMAL_EXECUTION_SCHEMA_VERSION = "1.0"
FORMAL_POLICY_VERSION = "stage8-frozen-a-primary-b-fallback-1.0"
PRIMARY_RUN_ID = "formal-primary-v1"
FALLBACK_RUN_ID = "formal-fallback-v1"
EMERGENCY_PROMPT_VERSION = "local-emergency-single-chapter-1.0"


@dataclass(frozen=True)
class FormalChapterResult:
    """正式分章最终采用结果及其来源。"""

    plan_path: Path
    execution_report_path: Path
    result_group_path: Path
    boundary_count: int
    duration_seconds: float
    min_chapter_seconds: float
    status: str
    result_source: str
    quality_level: str
    business_call_count: int
    supplier_request_count: int
    plan_cache_hit: bool


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


def _write_json_atomic(path: Path, payload: Any) -> bool:
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
        raise ChapterPlanError(f"无法原子保存正式分章产物：{path}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def _copy_atomic(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f"{destination.name}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    except OSError as exc:
        raise ChapterPlanError(f"无法保存正式分章attempt副本：{destination}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _snapshot_attempt(
    task_root: Path,
    *,
    name: str,
    source_dir: Path | None,
    status: str,
    error: str | None,
    result: ChapterPlanResult | None,
) -> Path:
    attempt_root = task_root / "boundaries" / "attempts" / name
    copied: list[dict[str, Any]] = []
    if source_dir is not None and source_dir.is_dir():
        for source in sorted(source_dir.iterdir()):
            if not source.is_file() or source.name.endswith(".tmp"):
                continue
            if status == "failed" and source.name == "chapter-plan.json":
                continue
            destination = attempt_root / "artifacts" / source.name
            _copy_atomic(source, destination)
            copied.append(
                {
                    "name": source.name,
                    "sha256": _file_sha256(destination),
                    "size_bytes": destination.stat().st_size,
                }
            )
    report: dict[str, Any] = {
        "schema_version": FORMAL_EXECUTION_SCHEMA_VERSION,
        "formal_policy_version": FORMAL_POLICY_VERSION,
        "attempt": name,
        "status": status,
        "source_directory": (
            str(source_dir.resolve()) if source_dir is not None else None
        ),
        "copied_artifacts": copied,
    }
    if result is not None:
        report.update(
            {
                "boundary_count": result.boundary_count,
                "supplier_request_count": (
                    0 if result.llm_cache_hit else result.attempt_count
                ),
                "llm_cache_hit": result.llm_cache_hit,
                "plan_cache_hit": result.plan_cache_hit,
            }
        )
    if error is not None:
        report["error"] = error
    _write_json_atomic(attempt_root / "attempt-report.json", report)
    return attempt_root


def _recorded_supplier_requests(source_dir: Path) -> int:
    """从成功调用或失败审计中统计已记录的供应商请求数。"""

    attempts_by_call: dict[str, int] = {}
    if not source_dir.is_dir():
        return 0
    for path in source_dir.glob("*.json"):
        if path.name in {"chapter-plan.json", "attempt-report.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        count = payload.get("attempt_count")
        audit_attempts = payload.get("attempts")
        if isinstance(audit_attempts, list):
            count = len(audit_attempts)
        if isinstance(count, int) and count >= 0:
            attempts_by_call[call_id] = max(attempts_by_call.get(call_id, 0), count)
    return sum(attempts_by_call.values())


def _formalized_plan(plan: dict[str, Any], *, result_source: str) -> dict[str, Any]:
    formal = json.loads(json.dumps(plan, ensure_ascii=False))
    parameters = formal["parameters"]
    parameters.pop("experiment_id", None)
    parameters.pop("experiment_version", None)
    parameters["formal_policy_version"] = FORMAL_POLICY_VERSION
    parameters["formal_result_source"] = result_source
    return formal


def _emergency_plan(task_id: str, timeline: dict[str, Any]) -> dict[str, Any]:
    duration = round(float(timeline["duration_seconds"]), 3)
    minimum = min(1.0, duration)
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "strategy": "llm",
        "source_timeline": {
            "schema_version": timeline["schema_version"],
            "sha256": _canonical_sha256(timeline),
            "event_count": len(timeline["events"]),
        },
        "duration_seconds": duration,
        "parameters": {
            "prompt_version": EMERGENCY_PROMPT_VERSION,
            "min_chapter_seconds": minimum,
            "boundary_alignment": "selected_asr_event_start",
            "formal_policy_version": FORMAL_POLICY_VERSION,
            "formal_result_source": "emergency",
        },
        "boundaries": [
            {
                "timestamp": 0.0,
                "title": "完整内容",
                "source_event_id": None,
            }
        ],
    }


def _publish_result_group(
    task_root: Path,
    *,
    task_id: str,
    plan: dict[str, Any],
    report: dict[str, Any],
    timeline: dict[str, Any],
) -> tuple[Path, Path, Path, bool]:
    validate_chapter_plan(plan, timeline=timeline, expected_task_id=task_id)
    plan_sha256 = _canonical_sha256(plan)
    result_id = f"{report['result_source']}-{plan_sha256[:12]}"
    group = task_root / "boundaries" / "formal-results" / result_id
    group_plan = group / "chapter-plan.json"
    group_report = group / "execution-report.json"
    group_manifest = group / "manifest.json"
    group_report_payload = dict(report)
    group_report_payload["final_plan_sha256"] = plan_sha256
    group_report_payload["result_id"] = result_id
    if group.exists():
        try:
            manifest = json.loads(group_manifest.read_text(encoding="utf-8"))
            stored_plan = json.loads(group_plan.read_text(encoding="utf-8"))
            stored_report = json.loads(group_report.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChapterPlanError(f"正式分章结果组不完整或损坏：{group}") from exc
        expected_files = manifest.get("files")
        if (
            manifest.get("result_id") != result_id
            or manifest.get("task_id") != task_id
            or stored_plan != plan
            or not isinstance(stored_report, dict)
            or stored_report.get("result_id") != result_id
            or stored_report.get("task_id") != task_id
            or stored_report.get("final_plan_sha256") != plan_sha256
            or not isinstance(expected_files, dict)
            or expected_files.get("chapter-plan.json") != _file_sha256(group_plan)
            or expected_files.get("execution-report.json") != _file_sha256(group_report)
        ):
            raise ChapterPlanError(f"正式分章结果组哈希或内容不一致：{group}")
        authoritative_report_payload = stored_report
    else:
        _write_json_atomic(group_plan, plan)
        _write_json_atomic(group_report, group_report_payload)
        manifest = {
            "schema_version": FORMAL_EXECUTION_SCHEMA_VERSION,
            "formal_policy_version": FORMAL_POLICY_VERSION,
            "result_id": result_id,
            "task_id": task_id,
            "files": {
                "chapter-plan.json": _file_sha256(group_plan),
                "execution-report.json": _file_sha256(group_report),
            },
        }
        _write_json_atomic(group_manifest, manifest)
        authoritative_report_payload = group_report_payload

    final_plan = task_root / "boundaries" / "llm.json"
    final_report = task_root / "boundaries" / "llm-execution-report.json"
    plan_cache_hit = _write_json_atomic(final_plan, plan)
    _write_json_atomic(final_report, authoritative_report_payload)
    return final_plan, final_report, group, plan_cache_hit


def _experiment_source_dir(
    task_root: Path, *, pipeline: str, run_id: str
) -> Path:
    if pipeline == "direct":
        return (
            task_root
            / "experiments"
            / "stage8"
            / "direct"
            / "event-id-v2"
            / run_id
        )
    return task_root / "experiments" / "stage8" / "two-stage" / run_id


def plan_task_chapters_formal(
    output_root: str | Path,
    task_id: str,
    *,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
) -> FormalChapterResult:
    """执行冻结A主方案，技术失败时运行B，最终保证单章应急结果。"""

    try:
        timeline = load_task_timeline(output_root, task_id)
    except ASRTimelineError as exc:
        raise ChapterPlanError(str(exc)) from exc
    task_root = Path(output_root).expanduser() / task_id
    errors: list[dict[str, str]] = []
    business_calls = 0
    supplier_requests = 0
    selected_plan: dict[str, Any] | None = None
    selected_source = ""
    quality_level = ""

    primary_result: ChapterPlanResult | None = None
    primary_source = _experiment_source_dir(
        task_root, pipeline="direct", run_id=PRIMARY_RUN_ID
    )
    try:
        primary_result = plan_task_chapters(
            output_root,
            task_id,
            min_chapter_seconds=STABILITY_MIN_CHAPTER_SECONDS,
            config=config,
            transport=transport,
            dotenv_path=dotenv_path,
            experiment_id=PRIMARY_RUN_ID,
        )
        business_calls += 0 if primary_result.llm_cache_hit else 1
        supplier_requests += (
            0 if primary_result.llm_cache_hit else primary_result.attempt_count
        )
        raw_plan = json.loads(primary_result.plan_path.read_text(encoding="utf-8"))
        selected_plan = _formalized_plan(raw_plan, result_source="primary")
        selected_source = "primary"
        quality_level = "normal"
        _snapshot_attempt(
            task_root,
            name="primary",
            source_dir=primary_source,
            status="success",
            error=None,
            result=primary_result,
        )
    except (ChapterPlanError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        business_calls += 1
        supplier_requests += _recorded_supplier_requests(primary_source)
        errors.append({"path": "primary", "error": str(exc)})
        _snapshot_attempt(
            task_root,
            name="primary",
            source_dir=primary_source,
            status="failed",
            error=str(exc),
            result=None,
        )

    fallback_result: ChapterPlanResult | None = None
    if selected_plan is None:
        fallback_source = _experiment_source_dir(
            task_root, pipeline="two-stage", run_id=FALLBACK_RUN_ID
        )
        try:
            fallback_result = plan_task_chapters_two_stage(
                output_root,
                task_id,
                min_chapter_seconds=STABILITY_MIN_CHAPTER_SECONDS,
                config=config,
                transport=transport,
                dotenv_path=dotenv_path,
                experiment_id=FALLBACK_RUN_ID,
                save_validation_audit=True,
            )
            business_calls += 0 if fallback_result.llm_cache_hit else 2
            supplier_requests += (
                0 if fallback_result.llm_cache_hit else fallback_result.attempt_count
            )
            raw_plan = json.loads(
                fallback_result.plan_path.read_text(encoding="utf-8")
            )
            selected_plan = _formalized_plan(raw_plan, result_source="fallback")
            selected_source = "fallback"
            quality_level = "unknown"
            _snapshot_attempt(
                task_root,
                name="fallback",
                source_dir=fallback_source,
                status="success",
                error=None,
                result=fallback_result,
            )
        except (ChapterPlanError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            business_calls += 2
            supplier_requests += _recorded_supplier_requests(fallback_source)
            errors.append({"path": "fallback", "error": str(exc)})
            _snapshot_attempt(
                task_root,
                name="fallback",
                source_dir=fallback_source,
                status="failed",
                error=str(exc),
                result=None,
            )

    if selected_plan is None:
        selected_plan = _emergency_plan(task_id, timeline)
        selected_source = "emergency"
        quality_level = "degraded"
        emergency_root = task_root / "boundaries" / "attempts" / "emergency"
        _write_json_atomic(emergency_root / "chapter-plan.json", selected_plan)
        _write_json_atomic(
            emergency_root / "attempt-report.json",
            {
                "schema_version": FORMAL_EXECUTION_SCHEMA_VERSION,
                "formal_policy_version": FORMAL_POLICY_VERSION,
                "attempt": "emergency",
                "status": "success",
                "quality_level": "degraded",
                "api_calls": 0,
            },
        )

    report = {
        "schema_version": FORMAL_EXECUTION_SCHEMA_VERSION,
        "formal_policy_version": FORMAL_POLICY_VERSION,
        "task_id": task_id,
        "status": "success",
        "result_source": selected_source,
        "quality_level": quality_level,
        "business_call_count": business_calls,
        "supplier_request_count": supplier_requests,
        "primary_strategy": "direct-event-id-v2",
        "fallback_strategy": "two-stage-stability-1.0",
        "emergency_strategy": EMERGENCY_PROMPT_VERSION,
        "errors": errors,
    }
    final_plan, final_report, result_group, plan_cache_hit = _publish_result_group(
        task_root,
        task_id=task_id,
        plan=selected_plan,
        report=report,
        timeline=timeline,
    )
    return FormalChapterResult(
        plan_path=final_plan,
        execution_report_path=final_report,
        result_group_path=result_group,
        boundary_count=len(selected_plan["boundaries"]),
        duration_seconds=float(selected_plan["duration_seconds"]),
        min_chapter_seconds=float(
            selected_plan["parameters"]["min_chapter_seconds"]
        ),
        status="success",
        result_source=selected_source,
        quality_level=quality_level,
        business_call_count=business_calls,
        supplier_request_count=supplier_requests,
        plan_cache_hit=plan_cache_hit,
    )
