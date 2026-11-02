"""阶段8多次分章实验的可读对比材料。"""

from __future__ import annotations

import json
import re
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from video_doc.asr_timeline import ASRTimelineError, validate_timeline_payload
from video_doc.llm_strategy import (
    STABILITY_DIRECT_EXPERIMENT_VERSION,
    ChapterPlanError,
    experiment_artifact_subdir,
    validate_chapter_plan,
)


DEFAULT_RUN_IDS = ("run-01", "run-02", "run-03")
SCHEMES = ("direct", "two-stage")


class StabilityComparisonError(RuntimeError):
    """稳定性实验产物或对比材料无效。"""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StabilityComparisonError(f"无法读取实验JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise StabilityComparisonError(f"实验JSON顶层必须是对象：{path}")
    return payload


def load_experiment_plans(
    task_root: str | Path,
    task_id: str,
    *,
    run_ids: Iterable[str] = DEFAULT_RUN_IDS,
) -> dict[str, dict[str, dict[str, Any]]]:
    """读取并校验两套方案的指定实验运行。"""

    root = Path(task_root)
    plans: dict[str, dict[str, dict[str, Any]]] = {}
    for scheme in SCHEMES:
        scheme_plans: dict[str, dict[str, Any]] = {}
        for run_id in run_ids:
            path = root / experiment_artifact_subdir(
                scheme,
                run_id,
                experiment_version=(
                    STABILITY_DIRECT_EXPERIMENT_VERSION
                    if scheme == "direct"
                    else None
                ),
            ) / "chapter-plan.json"
            plan = _load_json(path)
            try:
                validate_chapter_plan(plan, expected_task_id=task_id)
            except ChapterPlanError as exc:
                raise StabilityComparisonError(f"实验ChapterPlan无效：{path}") from exc
            if plan["parameters"].get("experiment_id") != run_id:
                raise StabilityComparisonError(f"实验运行ID不一致：{path}")
            if scheme == "direct" and plan["parameters"].get(
                "experiment_version"
            ) != STABILITY_DIRECT_EXPERIMENT_VERSION:
                raise StabilityComparisonError(f"方案A实验版本不一致：{path}")
            scheme_plans[run_id] = plan
        plans[scheme] = scheme_plans
    return plans


def _timestamp(seconds: float) -> str:
    total = int(round(seconds))
    minutes, second = divmod(total, 60)
    return f"{minutes:02d}:{second:02d}"


def _chapter_durations(plan: dict[str, Any]) -> list[float]:
    starts = [float(boundary["timestamp"]) for boundary in plan["boundaries"]]
    ends = starts[1:] + [float(plan["duration_seconds"])]
    return [end - start for start, end in zip(starts, ends)]


def _cluster_scheme(
    scheme_plans: dict[str, dict[str, Any]],
    *,
    tolerance_seconds: float,
) -> list[dict[str, Any]]:
    records = sorted(
        (
            {
                "run_id": run_id,
                "timestamp": float(boundary["timestamp"]),
                "event_id": str(boundary["source_event_id"]),
                "title": str(boundary["title"]),
            }
            for run_id, plan in scheme_plans.items()
            for boundary in plan["boundaries"][1:]
        ),
        key=lambda item: (item["timestamp"], item["run_id"]),
    )
    clusters: list[dict[str, Any]] = []
    for record in records:
        candidates: list[tuple[float, dict[str, Any]]] = []
        for cluster in clusters:
            if record["run_id"] in cluster["run_ids"]:
                continue
            distance = abs(record["timestamp"] - float(cluster["median_timestamp"]))
            if distance <= tolerance_seconds:
                candidates.append((distance, cluster))
        if candidates:
            cluster = min(candidates, key=lambda item: item[0])[1]
            cluster["members"].append(record)
            cluster["run_ids"].add(record["run_id"])
            cluster["median_timestamp"] = median(
                member["timestamp"] for member in cluster["members"]
            )
        else:
            clusters.append(
                {
                    "members": [record],
                    "run_ids": {record["run_id"]},
                    "median_timestamp": record["timestamp"],
                }
            )
    for cluster in clusters:
        representative = min(
            cluster["members"],
            key=lambda member: (
                abs(member["timestamp"] - cluster["median_timestamp"]),
                member["timestamp"],
                member["run_id"],
            ),
        )
        cluster["representative"] = representative
        cluster["run_ids"] = sorted(cluster["run_ids"])
    return clusters


def _boundary_table(plan: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for index, boundary in enumerate(plan["boundaries"], start=1):
        timestamp = float(boundary["timestamp"])
        rows.append(
            f"| {index} | {_timestamp(timestamp)} ({timestamp:.2f}s) | "
            f"{boundary.get('source_event_id') or '—'} | {boundary['title']} |"
        )
    return rows


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _event_line(event: dict[str, Any]) -> str:
    return (
        f"{_timestamp(float(event['start']))} `[{event['id']}]` "
        f"{_clean_text(event['text'])}"
    )


def _chapter_cards(plan: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    """把每章明确写成从哪个片段开始、到哪个片段结束。"""

    index_by_id = {str(event["id"]): index for index, event in enumerate(events)}
    starts: list[int] = []
    for index, boundary in enumerate(plan["boundaries"]):
        event_id = boundary.get("source_event_id")
        starts.append(0 if index == 0 else index_by_id[str(event_id)])

    lines: list[str] = []
    for index, (boundary, start_index) in enumerate(
        zip(plan["boundaries"], starts), start=1
    ):
        end_index = starts[index] - 1 if index < len(starts) else len(events) - 1
        start_event = events[start_index]
        end_event = events[end_index]
        start_seconds = float(boundary["timestamp"])
        end_seconds = (
            float(plan["boundaries"][index]["timestamp"])
            if index < len(starts)
            else float(plan["duration_seconds"])
        )
        lines.extend(
            [
                f"#### 第{index}章：{boundary['title']}",
                "",
                f"- **本章范围：** {_timestamp(start_seconds)}—{_timestamp(end_seconds)}；"
                f"从`{start_event['id']}`开始，到`{end_event['id']}`结束；"
                f"约{end_seconds - start_seconds:.1f}秒。",
                "- **本章从下面这段原文开始（包含）：**",
                "",
                f"> {_event_line(start_event)}",
                "",
                "- **本章到下面这段原文结束（包含）：**",
                "",
                f"> {_event_line(end_event)}",
                "",
            ]
        )
    return lines


def _context_text(events: list[dict[str, Any]], event_id: str) -> str:
    index_by_id = {str(event["id"]): index for index, event in enumerate(events)}
    index = index_by_id[event_id]
    selected = events[max(0, index - 2) : min(len(events), index + 3)]
    return " / ".join(
        f"{event['id']} {re.sub(r'\s+', ' ', str(event['text'])).strip()}"
        for event in selected
    )


def _boundary_context_lines(
    events: list[dict[str, Any]], event_id: str
) -> list[str]:
    index_by_id = {str(event["id"]): index for index, event in enumerate(events)}
    index = index_by_id[event_id]
    previous = events[index - 1] if index > 0 else None
    current = events[index]
    following = events[index + 1] if index + 1 < len(events) else None
    lines = [
        f"- 上一段（仍属于上一章）：{_event_line(previous)}"
        if previous
        else "- 上一段：无（视频开头）",
        "- **分章线在这里：下一段开始进入新章。**",
        f"- **新章第一段：{_event_line(current)}**",
    ]
    if following:
        lines.append(f"- 后一段（继续属于新章）：{_event_line(following)}")
    return lines


def _flush_paragraph(lines: list[str], fragments: list[str]) -> None:
    if fragments:
        lines.append(" ".join(fragments))
        lines.append("")
        fragments.clear()


def build_stability_comparison(
    timeline: dict[str, Any],
    plans: dict[str, dict[str, dict[str, Any]]],
    *,
    tolerance_seconds: float = 30.0,
) -> str:
    """生成不使用AI参考真值的单视频稳定性对比Markdown。"""

    try:
        validate_timeline_payload(timeline)
    except ASRTimelineError as exc:
        raise StabilityComparisonError(str(exc)) from exc
    if set(plans) != set(SCHEMES):
        raise StabilityComparisonError("必须同时提供direct和two-stage实验计划。")
    task_id = str(timeline["task_id"])
    for scheme_plans in plans.values():
        for plan in scheme_plans.values():
            try:
                validate_chapter_plan(
                    plan, timeline=timeline, expected_task_id=task_id
                )
            except ChapterPlanError as exc:
                raise StabilityComparisonError("实验计划与时间轴不一致。") from exc

    clusters = {
        scheme: _cluster_scheme(
            plans[scheme], tolerance_seconds=tolerance_seconds
        )
        for scheme in SCHEMES
    }
    labels = {"direct": "方案A（直接分章）", "two-stage": "方案B（两阶段）"}
    lines = [
        f"# {task_id}阶段8稳定性分章对比",
        "",
        "> 本文只机械整理ASR与六次实验结果，不使用AI参考章节，也不构成人工真值。",
        "> 共识表示同一方案至少2次运行在±30秒内出现边界，不代表边界一定正确。",
        "",
        "## 怎么阅读这份材料",
        "",
        "- 分章标记永远放在它所对应原文的**上方**；标记下面紧接的第一段ASR就是新章开头。",
        "- 每章都按“从某段开始（包含）—到某段结束（包含）”列出，不需要凭时间或事件ID猜归属。",
        "- 时间和事件ID只用于追溯；真正判断边界时，请直接读粗体的“新章第一段”及其前后原文。",
        "- 建议先看第二节的章节卡片，再看第四节的边界原文，最后需要连续语境时再看第五节全文。",
        "",
        "## 一、运行总览",
        "",
        "| 方案 | 运行 | 章节数 | 少于90秒的章节 |",
        "|---|---|---:|---:|",
    ]
    for scheme in SCHEMES:
        for run_id, plan in plans[scheme].items():
            durations = _chapter_durations(plan)
            short_count = sum(duration < 90.0 for duration in durations)
            lines.append(
                f"| {labels[scheme]} | {run_id} | {len(durations)} | {short_count} |"
            )

    events = list(timeline["events"])
    lines.extend(["", "## 二、六次运行的章节卡片", ""])
    for scheme in SCHEMES:
        for run_id, plan in plans[scheme].items():
            lines.extend(
                [
                    f"### {labels[scheme]} · {run_id}",
                    "",
                    "下面每张卡片的内容范围是前闭后开：本章包含起点原文，下一章从下一张卡片的起点原文开始。",
                    "",
                    *_chapter_cards(plan, events),
                ]
            )

    lines.extend(["## 三、共识边界", ""])
    marker_map: dict[str, list[str]] = {}
    for scheme in SCHEMES:
        lines.extend([f"### {labels[scheme]}", ""])
        consensus = [cluster for cluster in clusters[scheme] if len(cluster["run_ids"]) >= 2]
        if not consensus:
            lines.extend(["无至少两次重复出现的边界。", ""])
            continue
        for index, cluster in enumerate(consensus, start=1):
            representative = cluster["representative"]
            run_text = "、".join(cluster["run_ids"])
            lines.extend(
                [
                    f"#### 共识{index}：{_timestamp(float(cluster['median_timestamp']))}附近",
                    "",
                    f"出现于：{run_text}；新章标题：{representative['title']}。",
                    "",
                    *_boundary_context_lines(events, representative["event_id"]),
                    "",
                ]
            )
            marker_map.setdefault(representative["event_id"], []).append(
                f"> **{labels[scheme]}共识边界**：{run_text}，"
                f"{_timestamp(float(cluster['median_timestamp']))}附近，"
                f"{representative['title']}"
            )
        lines.append("")

    lines.extend(["## 四、需要重点比较的边界原文", ""])
    conflict_count = 0
    other = {"direct": "two-stage", "two-stage": "direct"}
    for scheme in SCHEMES:
        other_consensus = [
            cluster
            for cluster in clusters[other[scheme]]
            if len(cluster["run_ids"]) >= 2
        ]
        for cluster in clusters[scheme]:
            unmatched = not any(
                abs(float(cluster["median_timestamp"]) - float(candidate["median_timestamp"]))
                <= tolerance_seconds
                for candidate in other_consensus
            )
            if len(cluster["run_ids"]) == 1 or unmatched:
                conflict_count += 1
                representative = cluster["representative"]
                lines.extend(
                    [
                        f"### 冲突{conflict_count}：{labels[scheme]} "
                        f"{_timestamp(float(cluster['median_timestamp']))}",
                        "",
                        f"出现于：{'、'.join(cluster['run_ids'])}；新章标题："
                        f"{representative['title']}。",
                        "",
                        *_boundary_context_lines(events, representative["event_id"]),
                        "",
                    ]
                )
    if conflict_count == 0:
        lines.extend(["没有需要单列的单次或跨方案冲突边界。", ""])

    lines.extend(
        [
            "## 五、带边界标记的完整ASR正文",
            "",
            "每条ASR单独一行。若某行上方有分章标记，该行就是相应新章的第一段原文；标记上方最后一行仍属于上一章。",
            "",
        ]
    )
    for event in events:
        event_id = str(event["id"])
        if event_id in marker_map:
            lines.extend(marker_map[event_id])
            lines.append("> ↓ **下面这一行是新章第一段**")
            lines.append("")
        lines.append(_event_line(event))
        lines.append("")

    lines.extend(
        [
            "## 六、人工判断问题",
            "",
            "对每套方案分别判断：",
            "",
            "1. 整体章节数是过少、合适还是过多；",
            "2. 是否漏掉明显的一级主题转换；",
            "3. 是否把例子、解释或过渡句错误拆成新章。",
            "",
        ]
    )
    return "\n".join(lines)


def _match_boundaries(
    first: dict[str, Any], second: dict[str, Any], *, tolerance_seconds: float
) -> tuple[int, list[float], int]:
    first_times = [float(item["timestamp"]) for item in first["boundaries"][1:]]
    second_times = [float(item["timestamp"]) for item in second["boundaries"][1:]]
    candidates = sorted(
        (abs(left - right), left_index, right_index)
        for left_index, left in enumerate(first_times)
        for right_index, right in enumerate(second_times)
        if abs(left - right) <= tolerance_seconds
    )
    used_left: set[int] = set()
    used_right: set[int] = set()
    differences: list[float] = []
    for difference, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        differences.append(difference)
    denominator = max(len(first_times), len(second_times))
    return len(differences), differences, denominator


def calculate_scheme_statistics(
    scheme_plans: dict[str, dict[str, Any]], *, tolerance_seconds: float = 30.0
) -> dict[str, Any]:
    chapter_counts = [len(plan["boundaries"]) for plan in scheme_plans.values()]
    durations = [
        duration
        for plan in scheme_plans.values()
        for duration in _chapter_durations(plan)
    ]
    pair_rates: list[float] = []
    all_differences: list[float] = []
    for (_, first), (_, second) in combinations(scheme_plans.items(), 2):
        matched, differences, denominator = _match_boundaries(
            first, second, tolerance_seconds=tolerance_seconds
        )
        pair_rates.append(1.0 if denominator == 0 else matched / denominator)
        all_differences.extend(differences)
    clusters = _cluster_scheme(
        scheme_plans, tolerance_seconds=tolerance_seconds
    )
    consensus = [
        {
            "timestamp": round(float(cluster["median_timestamp"]), 3),
            "run_ids": list(cluster["run_ids"]),
            "event_id": cluster["representative"]["event_id"],
            "title": cluster["representative"]["title"],
        }
        for cluster in clusters
        if len(cluster["run_ids"]) >= 2
    ]
    return {
        "valid_runs": len(scheme_plans),
        "chapter_count": {
            "min": min(chapter_counts),
            "median": median(chapter_counts),
            "max": max(chapter_counts),
            "runs": dict(zip(scheme_plans, chapter_counts)),
        },
        "short_chapters": {
            "count": sum(duration < 90.0 for duration in durations),
            "total": len(durations),
            "ratio": round(
                sum(duration < 90.0 for duration in durations) / len(durations), 6
            ),
        },
        "pairwise_boundary_agreement": round(sum(pair_rates) / len(pair_rates), 6),
        "matched_boundary_drift_seconds": {
            "median": round(median(all_differences), 3) if all_differences else None,
            "max": round(max(all_differences), 3) if all_differences else None,
        },
        "consensus_boundaries": consensus,
    }


def build_stability_summary(
    task_payloads: dict[str, tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]]]]
) -> tuple[dict[str, Any], str]:
    """生成四视频统计原始数据和不自动判胜的用户总览。"""

    summary: dict[str, Any] = {
        "schema_version": "1.0",
        "tolerance_seconds": 30.0,
        "short_chapter_threshold_seconds": 90.0,
        "decision": None,
        "tasks": {},
    }
    labels = {"direct": "方案A", "two-stage": "方案B"}
    lines = [
        "# 阶段8多视频稳定性统计",
        "",
        "> 本页只呈现结构和稳定性，不合成总分、不自动选择方案。语义好坏请结合四份ASR对比材料判断。",
        "",
        "## 一、逐视频统计",
        "",
        "| 视频 | 方案 | 章节数（三次） | 中位数 | 偏短章 | 成对边界一致率 | 匹配漂移（中位/最大） |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for task_id, (timeline, plans) in task_payloads.items():
        task_stats: dict[str, Any] = {}
        for scheme in SCHEMES:
            stats = calculate_scheme_statistics(plans[scheme])
            task_stats[scheme] = stats
            counts = stats["chapter_count"]["runs"]
            count_text = "/".join(str(counts[run_id]) for run_id in DEFAULT_RUN_IDS)
            short = stats["short_chapters"]
            drift = stats["matched_boundary_drift_seconds"]
            drift_text = (
                "—"
                if drift["median"] is None
                else f"{drift['median']:.1f}s/{drift['max']:.1f}s"
            )
            lines.append(
                f"| {task_id} | {labels[scheme]} | {count_text} | "
                f"{stats['chapter_count']['median']:g} | "
                f"{short['count']}/{short['total']} ({short['ratio']:.1%}) | "
                f"{stats['pairwise_boundary_agreement']:.1%} | {drift_text} |"
            )
        summary["tasks"][task_id] = {
            "duration_seconds": float(timeline["duration_seconds"]),
            "event_count": len(timeline["events"]),
            "schemes": task_stats,
        }
    lines.extend(
        [
            "",
            "成对边界一致率：三组运行两两比较，在±30秒内进行一对一匹配；匹配数除以两次运行中较多的非零边界数，再取三组平均。它只衡量稳定性，不代表边界正确。",
            "",
            "## 二、人工判断入口",
            "",
        ]
    )
    for task_id in task_payloads:
        lines.append(f"- [{task_id}章节卡片与完整ASR](./{task_id}/comparison.md)")
    lines.extend(
        [
            "",
            "请分别判断A、B：章节数是否过少/合适/过多，是否漏掉一级主题，是否把例子或过渡误拆为新章。两套都不好时可直接记为共同失败，不必二选一。",
            "",
        ]
    )
    return summary, "\n".join(lines)


def write_stability_summary(
    output_root: str | Path, task_ids: Iterable[str]
) -> tuple[Path, Path]:
    output_root = Path(output_root)
    task_payloads: dict[
        str, tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]]]
    ] = {}
    for task_id in task_ids:
        task_root = output_root / task_id
        timeline = _load_json(task_root / "timeline" / "timeline.json")
        task_payloads[task_id] = (
            timeline,
            load_experiment_plans(task_root, task_id),
        )
    payload, markdown = build_stability_summary(task_payloads)
    summary_root = output_root / "stage8_stability"
    json_path = summary_root / "summary.json"
    markdown_path = summary_root / "summary.md"
    summary_root.mkdir(parents=True, exist_ok=True)
    for path, content in (
        (json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"),
        (markdown_path, markdown),
    ):
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return json_path, markdown_path


def write_stability_comparison(
    output_root: str | Path,
    task_id: str,
    *,
    run_ids: Iterable[str] = DEFAULT_RUN_IDS,
) -> Path:
    output_root = Path(output_root)
    task_root = output_root / task_id
    timeline = _load_json(task_root / "timeline" / "timeline.json")
    plans = load_experiment_plans(task_root, task_id, run_ids=run_ids)
    content = build_stability_comparison(timeline, plans)
    output_path = output_root / "stage8_stability" / task_id / "comparison.md"
    temporary_path = output_path.with_name(f"{output_path.name}.tmp")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(content, encoding="utf-8")
        temporary_path.replace(output_path)
    except OSError as exc:
        raise StabilityComparisonError(f"无法保存稳定性对比材料：{output_path}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path
