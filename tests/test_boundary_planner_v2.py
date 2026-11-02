"""阶段14-B：BoundaryPlannerV2 定向测试（裁决A/B/C/D/E/F/G全部语义）。

离线注入ScriptedTransport，零真实网络/API；全部产物写tmp_path；DEV01用例只读
复制已发布Timeline 2.0组到tmp_path，并断言真实``outputs/DEV01``零新增写入。
覆盖：11业务校验码、prompt逐字结构、plan字段级/交叉校验、执行报告计数、
发布/缓存/指针修复/并发/中断/注入矩阵、registry使用点拒绝与冻结核对。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

import video_doc.boundary_planner_v2 as bvp
from tests.test_timeline_builder_v2 import TASK, _write_m1
from video_doc.boundary_planner_v2 import (
    BOUNDARY_GROUP_DIRECTORY,
    BOUNDARY_MANIFEST_FILE,
    BOUNDARY_PLAN_FILE,
    BOUNDARY_RAW_FILE,
    BOUNDARY_REPORT_FILE,
    EMERGENCY_PROMPT_VERSION,
    EMERGENCY_TITLE,
    MIN_CHAPTER_MS,
    PROMPT_VERSION,
    BoundaryCacheCorruptError,
    BoundaryConcurrentRunError,
    BoundaryModelFailedError,
    BoundaryPlannerV2Error,
    ChapterPlanValidationError,
    CurrentPointerCorruptError,
    DurationTooShortError,
    InjectedBoundaryFailure,
    InputTimelineCorruptError,
    InputTimelineMissingError,
    RoleConfigInvalidError,
    RoleConfigMissingError,
    SourceVideoMismatchError,
    TranscriptView,
    _BOUNDARY_RESPONSE_EXAMPLE,
    _BOUNDARY_TIMELINE_INTRO,
    _resolve_boundary_role,
    _sanitize_echo,
    _validate_execution_report,
    boundary_prompt_content_sha256,
    build_chapter_plan_v2,
    build_emergency_plan,
    build_execution_report,
    load_chapter_plan_current,
    load_published_chapter_plan,
    normalize_boundary_response,
    plan_chapters_v2,
    validate_chapter_plan_group,
    validate_chapter_plan_v2,
)
from video_doc.chapter_policy import render_stability_policy
from video_doc.model_roles import BOUNDARY_ROLE_NAME
from video_doc.task_workspace import initialize_task_workspace
from video_doc.timeline_builder_v2 import build_timeline_v2, load_published_timeline
from video_doc.unified_timeline import (
    TRANSCRIPT_EVENT_TYPE,
    canonical_json_bytes,
    canonical_json_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "docs" / "development" / "m2-p3-model-role-config-fixture.json"
TEMPLATE_PATH = REPO_ROOT / "config" / "model-roles.json"

SECRET = "sk-fake-secret-for-tests-0123456789"
ENV = {"VIDEO_DOC_LLM_API_KEY": SECRET}

# 600s视频：te000001@10s、te000002@90s、te000003@300s。
HAPPY_EVENTS = [(10.0, 30.0, "alpha"), (90.0, 120.0, "beta"), (300.0, 330.0, "gamma")]
# 100s视频：te000002@35s → 第一章35s < 60s 触发MIN_CHAPTER_VIOLATION。
MIN_EVENTS = [(10.0, 30.0, "a"), (35.0, 45.0, "b"), (90.0, 99.0, "c")]
MIN_RESPONSE = {
    "boundaries": [
        {"source_event_id": "te000001", "title": "开场"},
        {"source_event_id": "te000002", "title": "违规章"},
    ]
}
GOOD_RESPONSE = {
    "boundaries": [
        {"source_event_id": "te000001", "title": "开场"},
        {"source_event_id": "te000002", "title": "中段"},
    ]
}
ALT_RESPONSE = {
    "boundaries": [
        {"source_event_id": "te000001", "title": "新开场"},
        {"source_event_id": "te000002", "title": "新中段"},
    ]
}
SINGLE_RESPONSE = {"boundaries": [{"source_event_id": "te000001", "title": "完整内容"}]}
BAD_FIRST_RESPONSE = {
    "boundaries": [{"source_event_id": "te000002", "title": "错位"}]
}
# B1（审查报告§五修正要求1）：条目含多余字段（timestamp为与事件start_ms冲突的
# 错误值＋未知键），验证裁决A1「多余字段忽略」且timestamp_ms只来自时间轴事件。
EXTRA_FIELDS_RESPONSE = {
    "boundaries": [
        {
            "source_event_id": "te000001",
            "title": "开场",
            "timestamp": 999999,
            "unknown_key": "x",
        },
        {
            "source_event_id": "te000002",
            "title": "中段",
            "timestamp": 12345,
            "score": 0.5,
        },
    ]
}


class ScriptedTransport:
    """离线传输替身：按脚本应答并记录每次请求的消息与response_format。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, config: Any, messages: Any, response_format: Any) -> Any:
        self.calls.append(
            {
                "messages": [dict(item) for item in messages],
                "response_format": dict(response_format),
                "model": config.model,
            }
        )
        if not self.responses:
            raise AssertionError("预算外请求：传输层收到了超出脚本数量的调用")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _workspace(
    tmp_path: Path,
    *,
    events: Any = HAPPY_EVENTS,
    duration_seconds: float = 600.0,
    build: bool = True,
    task: str = TASK,
) -> Path:
    root = tmp_path / "outputs"
    initialize_task_workspace(root, task)
    _write_m1(root, task=task, events=events, duration_seconds=duration_seconds)
    if build:
        build_timeline_v2(task, output_root=root, ocr="off")
    return root


def _registry(
    tmp_path: Path,
    *,
    mutate_role: Any = None,
    mutate_profile: Any = None,
    name: str = "registry.json",
) -> Path:
    payload = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    if mutate_role is not None:
        mutate_role(payload["llm_roles"][BOUNDARY_ROLE_NAME])
    if mutate_profile is not None:
        mutate_profile(payload["connection_profiles"]["bailian_default"])
    path = tmp_path / name
    with path.open("w", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return path


def _plan(
    root: Path,
    transport: ScriptedTransport,
    *,
    tmp_path: Path | None = None,
    registry: Path | None = None,
    environ: Any = ENV,
    **kwargs: Any,
) -> bvp.BoundaryPlanResult:
    if registry is None:
        registry = _registry(tmp_path if tmp_path is not None else root)
    return plan_chapters_v2(
        TASK,
        output_root=root,
        model_registry=registry,
        transport=transport,
        environ=environ,
        **kwargs,
    )


def _boundaries_root(root: Path) -> Path:
    return root / TASK / "m2" / "boundaries"


def _failures_dir(root: Path) -> Path:
    return root / TASK / "m2" / "failures" / "boundary_planner_v2"


def _tree_snapshot(root: Path) -> dict[str, str]:
    return {
        entry.relative_to(root).as_posix(): entry.stat().st_size
        for entry in sorted(root.rglob("*"))
        if entry.is_file()
    }


def _tree_sha_snapshot(root: Path) -> dict[str, str]:
    return {
        entry.relative_to(root).as_posix(): bvp._file_sha256(entry)
        for entry in sorted(root.rglob("*"))
        if entry.is_file()
    }


def _only_failure(root: Path) -> dict[str, Any]:
    entries = list(_failures_dir(root).iterdir())
    assert len(entries) == 1
    return json.loads((entries[0] / "failure.json").read_text(encoding="utf-8"))


def _event(
    index: int,
    start_ms: int,
    end_ms: int,
    text: str,
    *,
    pause_ms: int = 0,
    eligible: bool = True,
    event_type: str = TRANSCRIPT_EVENT_TYPE,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "pause_before_ms": pause_ms,
        "boundary_eligible": eligible,
    }
    if event_type != TRANSCRIPT_EVENT_TYPE:
        payload = {}
    return {
        "id": f"te{index:06d}" if event_type == TRANSCRIPT_EVENT_TYPE else f"pe{index:06d}",
        "type": event_type,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "payload": payload,
    }


def _fake_view(segments: list[tuple[int, int, str, bool]], *, duration_ms: int = 100_000) -> TranscriptView:
    events = tuple(
        _event(i, start, end, text, eligible=eligible)
        for i, (start, end, text, eligible) in enumerate(segments, start=1)
    )
    return TranscriptView(
        events=events,
        first_event_id=str(events[0]["id"]),
        language_code="zh-cn",
        duration_ms=duration_ms,
    )


def _fake_index(events: list[dict[str, Any]], *, duration_ms: int = 300_000) -> dict[str, Any]:
    return {
        "task_id": TASK,
        "timeline_id": "timeline-fake-0001",
        "run_status": "success",
        "quality_level": "normal",
        "duration_ms": duration_ms,
        "language": {"code": "zh-cn"},
        "events": events,
    }


# ---------------------------------------------------------------------------
# 业务规范化与11个校验码（裁决A1/§G）
# ---------------------------------------------------------------------------


def test_normalize_not_object_and_boundaries_missing() -> None:
    view = _fake_view([(0, 30_000, "a", True), (60_000, 90_000, "b", True)])
    _, issues = normalize_boundary_response([{"source_event_id": "te000001", "title": "x"}], view)
    assert [i["code"] for i in issues] == ["RESPONSE_NOT_OBJECT"]
    _, issues = normalize_boundary_response({}, view)
    assert [i["code"] for i in issues] == ["BOUNDARIES_MISSING"]
    _, issues = normalize_boundary_response({"boundaries": []}, view)
    assert issues[0]["code"] == "BOUNDARIES_MISSING"


def test_normalize_entry_shape_codes() -> None:
    view = _fake_view(
        [
            (0, 30_000, "a", True),
            (60_000, 90_000, "b", True),
            (95_000, 99_000, "ghost", False),
        ]
    )
    _, issues = normalize_boundary_response({"boundaries": ["not-a-dict"]}, view)
    assert [i["code"] for i in issues] == ["BOUNDARY_NOT_OBJECT"]
    _, issues = normalize_boundary_response(
        {"boundaries": [{"source_event_id": "te000001", "title": "   "}]}, view
    )
    assert [i["code"] for i in issues] == ["TITLE_EMPTY"]
    _, issues = normalize_boundary_response({"boundaries": [{"title": "有标题"}]}, view)
    assert [i["code"] for i in issues] == ["EVENT_ID_MISSING"]
    _, issues = normalize_boundary_response(
        {"boundaries": [{"source_event_id": "te999999", "title": "不存在"}]}, view
    )
    assert [i["code"] for i in issues] == ["EVENT_NOT_FOUND"]
    # 首位即不可选：在eligibility检查处continue，FIRST检查不再执行（裁决A1逐条顺序）。
    _, issues = normalize_boundary_response(
        {"boundaries": [{"source_event_id": "te000003", "title": "不可选"}]}, view
    )
    assert [i["code"] for i in issues] == ["EVENT_NOT_BOUNDARY_ELIGIBLE"]
    # 非首位的不可选事件（首位合法）才是纯EVENT_NOT_BOUNDARY_ELIGIBLE：
    view2 = _fake_view(
        [(0, 30_000, "a", True), (60_000, 90_000, "b", False), (95_000, 99_000, "c", True)]
    )
    _, issues = normalize_boundary_response(
        {"boundaries": [
            {"source_event_id": "te000001", "title": "开场"},
            {"source_event_id": "te000002", "title": "不可选"},
        ]},
        view2,
    )
    assert [i["code"] for i in issues] == ["EVENT_NOT_BOUNDARY_ELIGIBLE"]


def test_normalize_first_duplicate_order_and_min() -> None:
    view = _fake_view(
        [(0, 30_000, "a", True), (70_000, 80_000, "b", True), (90_000, 99_000, "c", True)],
        duration_ms=100_000,
    )
    _, issues = normalize_boundary_response(BAD_FIRST_RESPONSE, view)
    assert [i["code"] for i in issues] == ["FIRST_EVENT_VIOLATION"]
    _, issues = normalize_boundary_response(
        {"boundaries": [
            {"source_event_id": "te000001", "title": "开场"},
            {"source_event_id": "te000001", "title": "重复"},
        ]},
        view,
    )
    assert [i["code"] for i in issues] == ["DUPLICATE_EVENT_ID"]
    normalized, issues = normalize_boundary_response(
        {"boundaries": [
            {"source_event_id": "te000001", "title": "开场"},
            {"source_event_id": "te000003", "title": "乱序前"},
            {"source_event_id": "te000002", "title": "乱序后"},
        ]},
        view,
    )
    assert [i["code"] for i in issues] == ["ORDER_VIOLATION"]
    assert issues[0]["chapter_index"] == 3
    # 裁决A1：不重排——规范化结果保持原始顺序（首章＋te000003）。
    assert [b["source_event_id"] for b in normalized] == [None, "te000003"]
    # min校验只在全部条目规范化干净后按章节起点计算（裁决A1）：
    # 专用视图te000002@35s使第一章35000ms<60000ms。
    min_view = _fake_view(
        [(0, 30_000, "a", True), (35_000, 45_000, "b", True), (90_000, 99_000, "c", True)],
        duration_ms=100_000,
    )
    normalized, issues = normalize_boundary_response(MIN_RESPONSE, min_view)
    assert [i["code"] for i in issues] == ["MIN_CHAPTER_VIOLATION"]
    assert issues[0]["chapter_index"] == 1
    assert "35000" in issues[0]["message"] and "60000" in issues[0]["message"]
    # 同一响应在te000002@70s的视图下违规落在末章（chapter_index=2）。
    _, issues = normalize_boundary_response(MIN_RESPONSE, view)
    assert [i["code"] for i in issues] == ["MIN_CHAPTER_VIOLATION"]
    assert issues[0]["chapter_index"] == 2


def test_normalize_title_strip_and_echo_sanitization() -> None:
    view = _fake_view([(0, 30_000, "a", True), (70_000, 80_000, "b", True)])
    normalized, issues = normalize_boundary_response(
        {"boundaries": [{"source_event_id": "te000001", "title": "  带 空白\n标题 "}]}, view
    )
    assert normalized[0]["title"] == "带 空白\n标题"
    assert not issues
    long_title = "x" * 300
    _, issues = normalize_boundary_response(
        {"boundaries": [{"source_event_id": "te999999", "title": long_title}]}, view
    )
    assert len(issues[0]["title"]) == 80 and issues[0]["title"].endswith("…")
    assert _sanitize_echo("  a\n b\t  c ") == "a b c"
    assert _sanitize_echo("y" * 200) == "y" * 79 + "…"
    assert _sanitize_echo(123) is None


def test_normalize_ignores_extra_fields_a1() -> None:
    """B1修正要求1：裁决A1/§3.5.1定向项——条目多余字段忽略、零issues、normalized不落。"""
    view = _fake_view(
        [(10_000, 30_000, "alpha", True), (90_000, 120_000, "beta", True)],
        duration_ms=600_000,
    )
    normalized, issues = normalize_boundary_response(EXTRA_FIELDS_RESPONSE, view)
    assert issues == []
    # timestamp_ms只来自时间轴事件start_ms（首条恒0），注入的错误timestamp被忽略。
    assert normalized == [
        {"timestamp_ms": 0, "title": "开场", "source_event_id": None},
        {"timestamp_ms": 90_000, "title": "中段", "source_event_id": "te000002"},
    ]
    for entry in normalized:
        assert set(entry) == {"timestamp_ms", "title", "source_event_id"}


# ---------------------------------------------------------------------------
# Prompt（裁决B/C/E逐字）
# ---------------------------------------------------------------------------


def test_prompt_structure_and_lines() -> None:
    view = _fake_view(
        [
            (16_110, 20_500, "hello world", True),
            (20_500, 30_250, "second part", True),
            (31_000, 45_000, "third part", True),
            (31_000, 44_000, "dup", False),
        ],
        duration_ms=100_000,
    )
    view = TranscriptView(
        events=tuple(
            {
                **event,
                "payload": {
                    **event["payload"],
                    "pause_before_ms": pause,
                },
            }
            for event, pause in zip(view.events, (16_110, 0, 750, 0))
        ),
        first_event_id=view.first_event_id,
        language_code=view.language_code,
        duration_ms=view.duration_ms,
    )
    messages = bvp.build_boundary_messages(view)
    assert messages[0] == {"role": "system", "content": bvp.BOUNDARY_SYSTEM_PROMPT}
    user = messages[1]["content"]
    lines = user.splitlines()
    assert lines[0] == f"提示词版本：{PROMPT_VERSION}。"
    assert lines[1] == "视频时长为100.000秒，主文本语言为zh-cn，主文本事件共4条。"
    policy = render_stability_policy(100.0)
    assert f"\n{policy}\n" in f"\n{user}\n"
    assert user.index("视频时长为") < user.index(policy.splitlines()[0]) < user.index("第一章必须选择")
    assert "第一章必须选择首个主文本事件te000001" in user
    assert "每章（含最后一章）不得少于60.000秒" in user
    assert f"返回格式必须是：{_BOUNDARY_RESPONSE_EXAMPLE}。" in user
    assert "te000042" in user
    timeline_start = user.index(_BOUNDARY_TIMELINE_INTRO)
    assert user.index("te000001") < user.index("返回格式必须是") < timeline_start
    timeline_lines = user[timeline_start:].splitlines()[1:]
    assert timeline_lines == [
        "[te000001] 00:00:16.110-00:00:20.500 pause_before=16.110s hello world",
        "[te000002] 00:00:20.500-00:00:30.250 second part",
        "[te000003] 00:00:31.000-00:00:45.000 pause_before=0.750s third part",
        "[te000004] 00:00:31.000-00:00:44.000 dup（不可选边界）",
    ]
    # 裁决B1/C.2：只序列化transcript、全量不截断。
    assert "（不可选边界）" in user
    for text in ("hello world", "second part", "third part", "dup"):
        assert text in user


def test_prompt_content_hash_matches_template_record() -> None:
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    recorded = template["llm_roles"][BOUNDARY_ROLE_NAME]["prompt_bundle"]["content_sha256"]
    assert boundary_prompt_content_sha256() == recorded


# ---------------------------------------------------------------------------
# run-m2入口预算测量面（阶段15-0裁决§一1.4第1项，纯增量公开函数）
# ---------------------------------------------------------------------------


def test_measure_boundary_egress_members_and_zero_write(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    before = _tree_sha_snapshot(root)
    pointer, index = bvp._load_timeline_inputs(root, TASK)
    assert pointer is not None
    view = bvp._transcript_view(index)
    messages = bvp.build_boundary_messages(view)
    expected_chars = sum(len(str(message["content"])) for message in messages)
    expected_bytes = sum(
        len(str(message["content"]).encode("utf-8")) for message in messages
    )
    measurement = bvp.measure_boundary_egress(root, TASK)
    assert set(measurement) == {
        "message_chars",
        "message_bytes",
        "timeline_event_count",
        "language_code",
        "duration_ms",
    }
    assert measurement["message_chars"] == expected_chars > 0
    assert measurement["message_bytes"] == expected_bytes >= expected_chars
    assert measurement["timeline_event_count"] == len(view.events) == 3
    assert measurement["language_code"] == view.language_code
    assert measurement["duration_ms"] == view.duration_ms
    # 参数面对齐不影响被测文本（度量对象是messages文本本身）。
    assert bvp.measure_boundary_egress(
        root, TASK, model_registry=TEMPLATE_PATH, project_root=REPO_ROOT
    ) == measurement
    assert _tree_sha_snapshot(root) == before


def test_measure_boundary_egress_missing_timeline_typed(tmp_path: Path) -> None:
    root = _workspace(tmp_path, build=False)
    before = _tree_sha_snapshot(root)
    with pytest.raises(bvp.InputTimelineMissingError):
        bvp.measure_boundary_egress(root, TASK)
    assert _tree_sha_snapshot(root) == before


# ---------------------------------------------------------------------------
# registry使用点拒绝与冻结核对（裁决F.2/§一.8；决策9）
# ---------------------------------------------------------------------------


def test_resolve_role_accepts_repo_template() -> None:
    snapshot, role = _resolve_boundary_role(REPO_ROOT, None)
    assert snapshot.source == "project-default"
    assert role.enabled and role.model == "qwen3.7-flash"


def test_resolve_role_rejects_builtin_and_fixture(tmp_path: Path) -> None:
    with pytest.raises(RoleConfigMissingError):
        _resolve_boundary_role(tmp_path, None)
    with pytest.raises(RoleConfigInvalidError, match="fixture_only"):
        _resolve_boundary_role(REPO_ROOT, FIXTURE_PATH)
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8", newline="")
    with pytest.raises(RoleConfigInvalidError, match="registry无效"):
        _resolve_boundary_role(REPO_ROOT, broken)


def _mutate_role(field: str, value: Any):
    def _apply(role: dict[str, Any]) -> None:
        role[field] = value
    return _apply


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (_mutate_role("model", "qwen9-max"), "model必须为"),
        (_mutate_role("input_contract", "UnifiedTimeline/1.0"), "input_contract"),
        (_mutate_role("output_contract", "ChapterPlan/1.0"), "output_contract"),
        (_mutate_role("validation_id", "other-validator"), "validation_id"),
        (_mutate_role("validation_retry_policy", "rfb-2.0"), "validation_retry_policy"),
        (_mutate_role("cache_namespace", "boundary-model-other-namespace"), "cache_namespace"),
        (_mutate_role("alternate_model_fallback", "backup-model"), "alternate_model_fallback"),
        (_mutate_role("max_attempts_per_business_call", 1), "max_attempts_per_business_call"),
        (
            _mutate_role("runtime_budget", {"business_call_hard_limit": 2, "supplier_request_hard_limit": 3}),
            "runtime_budget",
        ),
        (
            _mutate_role("response_format", {"type": "text"}),
            "response_format",
        ),
    ],
)
def test_frozen_role_violations_rejected(tmp_path: Path, mutation: Any, needle: str) -> None:
    registry = _registry(tmp_path, mutate_role=mutation)
    with pytest.raises(RoleConfigInvalidError, match=needle):
        _resolve_boundary_role(REPO_ROOT, registry)


def test_generation_and_prompt_bundle_drift_rejected(tmp_path: Path) -> None:
    for field, value in [
        ("temperature", 0.5),
        ("enable_thinking", True),
        ("max_tokens", 1024),
        ("timeout_seconds", 30.0),
    ]:
        registry = _registry(
            tmp_path,
            mutate_role=_mutate_role("generation", {
                "temperature": 0.0,
                "enable_thinking": False,
                "max_tokens": None,
                "timeout_seconds": 60.0,
                **({field: value}),
            }),
            name=f"gen-{field}.json",
        )
        with pytest.raises(RoleConfigInvalidError):
            _resolve_boundary_role(REPO_ROOT, registry)
    registry = _registry(
        tmp_path,
        mutate_role=_mutate_role("prompt_bundle", {
            "versions": ["chapter-boundary-unified-timeline-2.0"],
            "content_sha256": "0" * 64,
            "fixture_hash_only": False,
        }),
        name="hash-drift.json",
    )
    with pytest.raises(RoleConfigInvalidError, match="实际prompt内容"):
        _resolve_boundary_role(REPO_ROOT, registry)
    registry = _registry(
        tmp_path,
        mutate_role=_mutate_role("prompt_bundle", {
            "versions": ["some-other-version"],
            "content_sha256": boundary_prompt_content_sha256(),
            "fixture_hash_only": False,
        }),
        name="version-drift.json",
    )
    with pytest.raises(RoleConfigInvalidError, match="缺少冻结提示词版本"):
        _resolve_boundary_role(REPO_ROOT, registry)
    registry = _registry(
        tmp_path,
        mutate_role=_mutate_role("prompt_bundle", {
            "versions": ["chapter-boundary-unified-timeline-2.0"],
            "content_sha256": boundary_prompt_content_sha256(),
            "fixture_hash_only": True,
        }),
        name="hash-only.json",
    )
    with pytest.raises(RoleConfigInvalidError, match="fixture_hash_only"):
        _resolve_boundary_role(REPO_ROOT, registry)


# ---------------------------------------------------------------------------
# ChapterPlan 2.0字段级/交叉校验（裁决D/§3.2）
# ---------------------------------------------------------------------------


def _fake_plan_inputs() -> tuple[dict[str, Any], TranscriptView]:
    events = [
        _event(1, 0, 30_000, "aa"),
        _event(2, 120_000, 150_000, "bb"),
        _event(3, 200_000, 210_000, "cc", event_type="page_change"),
    ]
    index = _fake_index(events)
    view = TranscriptView(
        events=tuple(e for e in events if e["type"] == TRANSCRIPT_EVENT_TYPE),
        first_event_id="te000001",
        language_code="zh-cn",
        duration_ms=300_000,
    )
    return index, view


def _primary_plan() -> dict[str, Any]:
    index, view = _fake_plan_inputs()
    boundaries = [
        {"timestamp_ms": 0, "title": "开场", "source_event_id": None},
        {"timestamp_ms": 120_000, "title": "中段", "source_event_id": "te000002"},
    ]
    return build_chapter_plan_v2(TASK, index, view, boundaries, result_source="primary")


def test_plan_shape_valid_and_emergency() -> None:
    plan = _primary_plan()
    validate_chapter_plan_v2(plan)
    index, view = _fake_plan_inputs()
    emergency = build_emergency_plan(TASK, index, view)
    validate_chapter_plan_v2(emergency, timeline_index=index, view=view, expected_task_id=TASK)
    assert emergency["boundaries"] == [
        {"timestamp_ms": 0, "title": EMERGENCY_TITLE, "source_event_id": None}
    ]
    parameters = emergency["parameters"]
    assert parameters["result_source"] == "emergency"
    assert parameters["quality_level"] == "degraded"
    assert parameters["prompt_version"] == EMERGENCY_PROMPT_VERSION
    assert parameters["min_chapter_ms"] == min(1000, view.duration_ms)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda p: p.update({"extra": 1}), "字段集合无效"),
        (lambda p: p["boundaries"][1].__setitem__("timestamp_ms", 120_000.5), "非负整数毫秒"),
        (lambda p: p["boundaries"][0].__setitem__("source_event_id", "te000001"), "source_event_id=null"),
        (lambda p: p["boundaries"][1].__setitem__("title", " 带空白 "), "首尾空白"),
        (lambda p: p.__setitem__("boundaries", []), "非空数组"),
        (lambda p: p["parameters"].__setitem__("quality_level", "degraded"), "组合无效"),
        (lambda p: p["boundaries"][1].__setitem__("timestamp_ms", 10_000), "低于最小章节时长"),
        (lambda p: p["boundaries"][1].__setitem__("timestamp_ms", 300_000), "必须位于"),
    ],
)
def test_plan_shape_tampering(mutate: Any, needle: str) -> None:
    plan = _primary_plan()
    mutate(plan)
    with pytest.raises(ChapterPlanValidationError, match=needle):
        validate_chapter_plan_v2(plan)


def test_plan_cross_validation() -> None:
    index, view = _fake_plan_inputs()
    plan = _primary_plan()
    validate_chapter_plan_v2(plan, timeline_index=index, view=view, expected_task_id=TASK)
    tampered = copy.deepcopy(plan)
    tampered["boundaries"][1]["source_event_id"] = "te000001"
    with pytest.raises(ChapterPlanValidationError, match="与事件起点不符"):
        validate_chapter_plan_v2(tampered, timeline_index=index, view=view)
    tampered = copy.deepcopy(plan)
    tampered["boundaries"][1]["source_event_id"] = "pe000003"  # 存在但非主文本可选边界
    with pytest.raises(ChapterPlanValidationError, match="不是可选边界"):
        validate_chapter_plan_v2(tampered, timeline_index=index, view=view)
    tampered = copy.deepcopy(plan)
    tampered["boundaries"][1]["source_event_id"] = "te999999"
    with pytest.raises(ChapterPlanValidationError, match="不存在的事件ID"):
        validate_chapter_plan_v2(tampered, timeline_index=index, view=view)
    tampered = copy.deepcopy(plan)
    tampered["source_timeline"]["sha256"] = "f" * 64
    with pytest.raises(ChapterPlanValidationError, match="规范哈希不一致"):
        validate_chapter_plan_v2(tampered, timeline_index=index, view=view)
    tampered = copy.deepcopy(plan)
    tampered["source_timeline"]["event_count"] = 99
    with pytest.raises(ChapterPlanValidationError, match="event_count"):
        validate_chapter_plan_v2(tampered, timeline_index=index, view=view)
    tampered = copy.deepcopy(plan)
    tampered["task_id"] = "OTHERID"
    with pytest.raises(ChapterPlanValidationError, match="task_id"):
        validate_chapter_plan_v2(
            tampered, timeline_index=index, view=view, expected_task_id=TASK
        )


# ---------------------------------------------------------------------------
# 执行报告计数（决策2/3）
# ---------------------------------------------------------------------------


def _report(plan: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return build_execution_report(
        plan["task_id"],
        plan,
        timeline_sha256="a" * 64,
        business_call_count=kw.pop("business_call_count", 1),
        supplier_request_count=kw.pop("supplier_request_count", 1),
        errors=kw.pop("errors", []),
        credential_present=kw.pop("credential_present", True),
    )


def _error_item(attempt: int, kind: str) -> dict[str, Any]:
    return {"attempt": attempt, "kind": kind, "message": "m", "issues": []}


def test_report_counter_rules() -> None:
    plan = _primary_plan()
    _validate_execution_report(_report(plan), plan)
    _validate_execution_report(
        _report(plan, supplier_request_count=2, errors=[_error_item(1, "validation")]), plan
    )
    index, view = _fake_plan_inputs()
    emergency = build_emergency_plan(TASK, index, view)
    _validate_execution_report(
        _report(
            emergency,
            supplier_request_count=2,
            errors=[_error_item(1, "validation"), _error_item(2, "transport")],
        ),
        emergency,
    )
    with pytest.raises(ChapterPlanValidationError, match="不应带错误"):
        _validate_execution_report(
            _report(plan, supplier_request_count=1, errors=[_error_item(1, "transport")]), plan
        )
    with pytest.raises(ChapterPlanValidationError, match="恰有一条"):
        _validate_execution_report(_report(plan, supplier_request_count=2, errors=[]), plan)
    with pytest.raises(ChapterPlanValidationError, match="恰有两条失败记录"):
        _validate_execution_report(_report(emergency, supplier_request_count=2, errors=[_error_item(1, "transport")]), emergency)
    with pytest.raises(ChapterPlanValidationError, match="调用计数无效"):
        _validate_execution_report(_report(emergency, supplier_request_count=1, errors=[_error_item(1, "transport"), _error_item(2, "transport")]), emergency)
    with pytest.raises(ChapterPlanValidationError, match="status与result_source"):
        bad = _report(plan)
        bad["status"] = "degraded"
        _validate_execution_report(bad, plan)
    with pytest.raises(ChapterPlanValidationError, match="字段集合无效"):
        bad = _report(plan)
        bad["created_at"] = "x"
        _validate_execution_report(bad, plan)
    with pytest.raises(ChapterPlanValidationError, match="未知业务校验码"):
        item = _error_item(1, "validation")
        item["issues"] = [{"code": "NOT_A_CODE", "chapter_index": 0, "title": None, "source_event_id": None, "message": "x"}]
        _validate_execution_report(
            _report(plan, supplier_request_count=2, errors=[item]), plan
        )


# ---------------------------------------------------------------------------
# e2e：发布、缓存、复用、修复、失效
# ---------------------------------------------------------------------------


def test_publish_happy_rerun_and_isolation(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    timelines_snapshot = _tree_sha_snapshot(root / TASK / "m2" / "timelines")
    transport = ScriptedTransport([GOOD_RESPONSE])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "success" and result.result_source == "primary"
    assert result.business_call_count == 1 and result.supplier_request_count == 1
    assert result.cache_hit is False and result.pointer_repaired is False
    assert result.boundary_count == 2
    assert result.result_id.startswith("chapter-plan-")
    root_b = _boundaries_root(root)
    group = root_b / "formal-results" / result.result_id
    assert {p.name for p in group.iterdir()} == {
        BOUNDARY_PLAN_FILE,
        BOUNDARY_RAW_FILE,
        BOUNDARY_REPORT_FILE,
        BOUNDARY_MANIFEST_FILE,
    }
    assert transport.calls[0]["model"] == "qwen3.7-flash"
    assert transport.calls[0]["response_format"] == {"type": "json_object"}
    current = json.loads((root_b / "current.json").read_text(encoding="utf-8"))
    assert set(current) == {
        "schema_name", "schema_version", "artifact_type", "result_id", "group", "manifest_sha256",
    }
    assert current["artifact_type"] == "chapter_plan"
    assert (root_b / "llm-cache").is_dir()
    before = _tree_sha_snapshot(root_b)
    rerun = _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert rerun.cache_hit is True and rerun.result_id == result.result_id
    assert _tree_sha_snapshot(root_b) == before
    assert _tree_sha_snapshot(root / TASK / "m2" / "timelines") == timelines_snapshot
    assert not _failures_dir(root).exists()


def test_correction_flow_and_replay(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    transport = ScriptedTransport([BAD_FIRST_RESPONSE, GOOD_RESPONSE])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.business_call_count == 1 and result.supplier_request_count == 2
    assert result.report["errors"][0]["kind"] == "validation"
    assert result.report["errors"][0]["issues"][0]["code"] == "FIRST_EVENT_VIOLATION"
    second = transport.calls[1]["messages"]
    assert second[-2] == {
        "role": "assistant",
        "content": json.dumps(BAD_FIRST_RESPONSE, ensure_ascii=False, separators=(",", ":")),
    }
    assert second[-1]["role"] == "user"
    assert second[-1]["content"].startswith("上一次响应未通过业务校验，错误清单：")
    assert second[-1]["content"].endswith("保持标题和章节顺序不变，只修正无效条目，返回完整JSON对象。")
    entry_path = next((_boundaries_root(root) / "llm-cache").glob("*.json"))
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    assert entry["supplier_request_count"] == 2 and len(entry["execution_errors"]) == 1
    assert (result.group_path / BOUNDARY_RAW_FILE).read_bytes() == bvp._json_bytes(GOOD_RESPONSE)
    report_bytes = (result.group_path / BOUNDARY_REPORT_FILE).read_bytes()
    rerun = _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert rerun.cache_hit is True and rerun.supplier_request_count == 2
    assert (rerun.group_path / BOUNDARY_REPORT_FILE).read_bytes() == report_bytes


def test_transport_feedback_without_assistant_echo(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    failure = RuntimeError(f"HTTP 401 unauthorized key {SECRET}")
    transport = ScriptedTransport([failure, GOOD_RESPONSE])
    result = _plan(root, transport, tmp_path=tmp_path)
    second = transport.calls[1]["messages"]
    assert len(second) == 3 and second[-1]["role"] == "user"
    assert second[-1]["content"].startswith("上一次调用未获得合法JSON响应：HTTP 401")
    assert second[-1]["content"].endswith("请只返回符合规定格式的完整JSON对象。")
    group_bytes = b"".join(p.read_bytes() for p in result.group_path.iterdir())
    assert SECRET.encode() not in group_bytes
    assert b"[REDACTED]" in group_bytes


def test_emergency_double_transport_failure(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    transport = ScriptedTransport([RuntimeError("boom-1"), RuntimeError("boom-2")])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "degraded" and result.quality_level == "degraded"
    assert result.boundary_count == 1 and len(transport.calls) == 2
    assert {p.name for p in result.group_path.iterdir()} == {
        BOUNDARY_PLAN_FILE, BOUNDARY_REPORT_FILE, BOUNDARY_MANIFEST_FILE,
    }
    assert not (result.group_path / BOUNDARY_RAW_FILE).exists()
    assert not (_boundaries_root(root) / "llm-cache").exists()
    errors = result.report["errors"]
    assert [e["kind"] for e in errors] == ["transport", "transport"]
    rerun = _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert rerun.cache_hit is True and rerun.status == "degraded"  # 决策12：不重试


def test_emergency_mixed_validation_then_transport(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    transport = ScriptedTransport([BAD_FIRST_RESPONSE, RuntimeError("net down")])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "degraded"
    assert [e["kind"] for e in result.report["errors"]] == ["validation", "transport"]
    assert len(result.report["errors"][0]["issues"]) == 1
    assert result.report["errors"][0]["issues"][0]["code"] == "FIRST_EVENT_VIOLATION"


def test_validation_twice_then_emergency_and_budget_cap(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    transport = ScriptedTransport([BAD_FIRST_RESPONSE] * 5)
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "degraded"
    assert len(transport.calls) == 2 and len(transport.responses) == 3


def test_single_chapter_publish(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    result = _plan(root, ScriptedTransport([SINGLE_RESPONSE]), tmp_path=tmp_path)
    assert result.boundary_count == 1 and result.status == "success"


def test_extra_fields_ignored_publish_and_replay_a1(tmp_path: Path) -> None:
    """B1修正要求1端到端：多余字段响应一次通过发布，raw逐字节保留，组复验与回放一致。"""
    root = _workspace(tmp_path)
    transport = ScriptedTransport([EXTRA_FIELDS_RESPONSE])
    result = _plan(root, transport, tmp_path=tmp_path)
    # 零issues：首轮即通过业务校验，不触发纠错轮，预算1业务调用/1供应商请求。
    assert result.status == "success" and result.result_source == "primary"
    assert result.business_call_count == 1 and result.supplier_request_count == 1
    assert result.report["errors"] == []
    assert result.boundary_count == 2
    # raw-response.json逐字节保留多余字段（原始响应原样落盘）。
    raw_bytes = (result.group_path / BOUNDARY_RAW_FILE).read_bytes()
    assert raw_bytes == bvp._json_bytes(EXTRA_FIELDS_RESPONSE)
    raw = json.loads(raw_bytes.decode("utf-8"))
    assert raw["boundaries"][0]["timestamp"] == 999999
    assert raw["boundaries"][0]["unknown_key"] == "x"
    assert raw["boundaries"][1]["timestamp"] == 12345
    assert raw["boundaries"][1]["score"] == 0.5
    # chapter-plan.json不含任何多余字段；timestamp_ms来自事件start_ms而非注入值。
    plan_bytes = (result.group_path / BOUNDARY_PLAN_FILE).read_bytes()
    assert b"unknown_key" not in plan_bytes and b'"timestamp"' not in plan_bytes
    assert result.plan["boundaries"] == [
        {"timestamp_ms": 0, "title": "开场", "source_event_id": None},
        {"timestamp_ms": 90_000, "title": "中段", "source_event_id": "te000002"},
    ]
    # validate_chapter_plan_group对含多余字段raw的发布组直接复验通过。
    manifest = json.loads(
        (result.group_path / BOUNDARY_MANIFEST_FILE).read_text(encoding="utf-8")
    )
    plan_again, manifest_again, report_again = validate_chapter_plan_group(
        result.group_path,
        cache_sha256=str(manifest["cache_identity_sha256"]),
        timeline_index=load_published_timeline(root, TASK),
        timeline_manifest_sha256=str(manifest["inputs"]["timeline_manifest_sha256"]),
        expected_result_id=result.result_id,
    )
    assert plan_again == result.plan
    assert manifest_again == manifest and report_again == result.report
    # 缓存回放一致：命中零真实请求，发布树逐字节不变，无失败审计。
    before = _tree_sha_snapshot(_boundaries_root(root))
    rerun = _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert rerun.cache_hit is True and rerun.result_id == result.result_id
    assert _tree_sha_snapshot(_boundaries_root(root)) == before
    assert not _failures_dir(root).exists()


def test_pointer_repair_via_llm_cache(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    current_path = _boundaries_root(root) / "current.json"
    current_path.unlink()
    repaired = _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert repaired.cache_hit is True and repaired.pointer_repaired is True
    assert repaired.result_id == first.result_id
    assert repaired.business_call_count == 1 and repaired.supplier_request_count == 1


def test_fresh_call_reuses_byte_identical_group(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    before = _tree_sha_snapshot(_boundaries_root(root) / "formal-results")
    (_boundaries_root(root) / "current.json").unlink()
    shutil.rmtree(_boundaries_root(root) / "llm-cache")
    transport = ScriptedTransport([GOOD_RESPONSE])
    rerun = _plan(root, transport, tmp_path=tmp_path)
    assert len(transport.calls) == 1
    assert rerun.cache_hit is True and rerun.pointer_repaired is True
    assert _tree_sha_snapshot(_boundaries_root(root) / "formal-results") == before


def test_identity_change_publishes_new_group(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    old_snapshot = _tree_sha_snapshot(_boundaries_root(root) / "formal-results")
    registry = _registry(tmp_path, mutate_role=_mutate_role("model_revision", "rev-2"), name="rev2.json")
    second = _plan(root, ScriptedTransport([ALT_RESPONSE]), registry=registry, tmp_path=tmp_path)
    assert second.result_id != first.result_id and second.cache_hit is False
    current = json.loads((_boundaries_root(root) / "current.json").read_text(encoding="utf-8"))
    assert current["result_id"] == second.result_id
    for name, sha in old_snapshot.items():
        assert bvp._file_sha256(_boundaries_root(root) / "formal-results" / name) == sha
    assert len(list((_boundaries_root(root) / "llm-cache").glob("*.json"))) == 2


def test_same_plan_different_identity_is_cache_corrupt(tmp_path: Path) -> None:
    # 裁决F.6逐字：同result_id组存在但manifest字节不同→CACHE_CORRUPT，不复用不覆盖。
    root = _workspace(tmp_path)
    first = _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    group_before = _tree_sha_snapshot(first.group_path)
    (_boundaries_root(root) / "current.json").unlink()
    shutil.rmtree(_boundaries_root(root) / "llm-cache")
    registry = _registry(tmp_path, mutate_role=_mutate_role("model_revision", "rev-collide"), name="collide.json")
    with pytest.raises(bvp.BoundaryCacheCorruptError) as excinfo:
        _plan(root, ScriptedTransport([GOOD_RESPONSE]), registry=registry, tmp_path=tmp_path)
    assert excinfo.value.error_code == "CACHE_CORRUPT"
    assert _tree_sha_snapshot(first.group_path) == group_before
    assert not (_boundaries_root(root) / "current.json").exists()
    audit = _only_failure(root)
    assert audit["stage"] == "group_publish" and audit["error_code"] == "CACHE_CORRUPT"


def test_current_only_repair_via_early_hit(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    current_path = _boundaries_root(root) / "current.json"
    current_path.unlink()
    shutil.rmtree(_boundaries_root(root) / "llm-cache")
    transport = ScriptedTransport([GOOD_RESPONSE])
    repaired = _plan(root, transport, tmp_path=tmp_path)
    assert repaired.pointer_repaired is True and repaired.cache_hit is True


# ---------------------------------------------------------------------------
# e2e失败矩阵（裁决§3.3；决策4/5/6/7/10）
# ---------------------------------------------------------------------------


def test_duration_too_short_precheck_zero_write(tmp_path: Path) -> None:
    root = _workspace(tmp_path, events=[(5.0, 20.0, "short")], duration_seconds=60.0)
    with pytest.raises(DurationTooShortError):
        _plan(root, ScriptedTransport([SINGLE_RESPONSE]), tmp_path=tmp_path)
    assert not _boundaries_root(root).exists()
    assert not _failures_dir(root).exists()
    # .locks目录本身可能已由build-timeline-v2创建；只要求boundaries锁文件不存在。
    assert not (root / TASK / "m2" / ".locks" / "boundaries.lock").exists()


def test_input_timeline_missing(tmp_path: Path) -> None:
    root = _workspace(tmp_path, build=False)
    with pytest.raises(InputTimelineMissingError) as excinfo:
        _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert excinfo.value.next_action == "run_build_timeline_v2"
    assert not _boundaries_root(root).exists()


def test_task_identity_mismatch(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    task_json = root / TASK / "task.json"
    task_json.unlink()
    with pytest.raises(SourceVideoMismatchError):
        _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    with task_json.open("w", encoding="utf-8", newline="") as stream:
        json.dump(
            {"schema_version": "1.0", "task_id": "OTHERID", "source_video": None}, stream
        )
    with pytest.raises(SourceVideoMismatchError):
        _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert not _boundaries_root(root).exists()
    assert not _failures_dir(root).exists()


def test_concurrent_run_keeps_lock(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    locks = root / TASK / "m2" / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "boundaries.lock").write_text("x", encoding="utf-8", newline="")
    with pytest.raises(BoundaryConcurrentRunError):
        _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert (locks / "boundaries.lock").is_file()
    assert not _boundaries_root(root).exists()
    assert not _failures_dir(root).exists()  # 锁获取失败在try之外：零审计


def test_role_missing_e2e_writes_audit(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    empty_project = tmp_path / "empty-project"
    empty_project.mkdir()
    with pytest.raises(RoleConfigMissingError):
        plan_chapters_v2(
            TASK, output_root=root, project_root=empty_project,
            transport=ScriptedTransport([]), environ=ENV,
        )
    audit = _only_failure(root)
    assert audit["stage"] == "role_config" and audit["error_code"] == "ROLE_CONFIG_MISSING"


def test_missing_api_key_at_issuance(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    transport = ScriptedTransport([GOOD_RESPONSE])
    with pytest.raises(RoleConfigInvalidError, match="VIDEO_DOC_LLM_API_KEY"):
        _plan(root, transport, tmp_path=tmp_path, environ={})
    assert len(transport.calls) == 0
    audit = _only_failure(root)
    assert audit["stage"] == "model_call" and audit["error_code"] == "ROLE_CONFIG_INVALID"


def test_keyboard_interrupt_audit_and_cleanup(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    transport = ScriptedTransport([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        _plan(root, transport, tmp_path=tmp_path)
    audit = _only_failure(root)
    assert audit["stage"] == "model_call"
    assert audit["business_call_count"] == 1 and audit["supplier_request_count"] == 1
    assert audit["current_unchanged"] is True
    assert not (_boundaries_root(root) / "formal-results").exists()
    assert not (_boundaries_root(root) / "current.json").exists()
    assert not (root / TASK / "m2" / ".locks" / "boundaries.lock").exists()


_INJECTION_STAGES = {
    "after_inputs": "prepare",
    "before_model_call": "model_call",
    "after_model_call": "model_call",
    "before_group_publish": "group_publish",
    "after_group_publish": "group_publish",
    "before_current": "current_publish",
}


@pytest.mark.parametrize("point", sorted(_INJECTION_STAGES))
def test_failure_injection_matrix(tmp_path: Path, point: str) -> None:
    root = _workspace(tmp_path)
    transport = ScriptedTransport([GOOD_RESPONSE])
    with pytest.raises(InjectedBoundaryFailure) as excinfo:
        _plan(root, transport, tmp_path=tmp_path, failure_injection=point)
    assert excinfo.value.error_code == "BOUNDARY_PLAN_FAILED"
    root_b = _boundaries_root(root)
    groups = root_b / BOUNDARY_GROUP_DIRECTORY
    # 空目录检查不做点前缀豁免：临时组(.tmp-*)若残留同样必须失败。
    assert not groups.exists() or list(groups.iterdir()) == []
    assert not (root_b / "current.json").exists()
    audit = _only_failure(root)
    assert audit["stage"] == _INJECTION_STAGES[point]
    assert audit["current_unchanged"] is True
    assert not (root / TASK / "m2" / ".locks" / "boundaries.lock").exists()


def test_unknown_injection_point_rejected(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(BoundaryPlannerV2Error, match="未知的分章失败注入点"):
        _plan(root, ScriptedTransport([]), tmp_path=tmp_path, failure_injection="bogus")


def _run_with_corruption(tmp_path: Path, mutate: Any) -> bvp.BoundaryPlannerV2Error:
    root = _workspace(tmp_path)
    _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    mutate(root)
    transport = ScriptedTransport([GOOD_RESPONSE])
    with pytest.raises(BoundaryPlannerV2Error) as excinfo:
        _plan(root, transport, tmp_path=tmp_path)
    return excinfo.value


def test_corrupt_group_plan_bytes(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        plan_path = _boundaries_root(root) / "formal-results"
        group = next(plan_path.glob("chapter-plan-*"))
        (group / BOUNDARY_PLAN_FILE).write_bytes(b" " + (group / BOUNDARY_PLAN_FILE).read_bytes())

    error = _run_with_corruption(tmp_path, mutate)
    assert error.error_code == "CACHE_CORRUPT"


def test_corrupt_current_manifest_sha(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        current = _boundaries_root(root) / "current.json"
        pointer = json.loads(current.read_text(encoding="utf-8"))
        pointer["manifest_sha256"] = "0" * 64
        with current.open("w", encoding="utf-8", newline="") as stream:
            json.dump(pointer, stream)

    error = _run_with_corruption(tmp_path, mutate)
    assert error.error_code == "CURRENT_POINTER_CORRUPT"


def test_corrupt_llm_cache_entry(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        (_boundaries_root(root) / "current.json").unlink()
        entry = next((_boundaries_root(root) / "llm-cache").glob("*.json"))
        entry.write_bytes(b"{oops")

    error = _run_with_corruption(tmp_path, mutate)
    assert error.error_code == "CACHE_CORRUPT"


def test_tampered_llm_cache_normalized(tmp_path: Path) -> None:
    def mutate(root: Path) -> None:
        (_boundaries_root(root) / "current.json").unlink()
        entry_path = next((_boundaries_root(root) / "llm-cache").glob("*.json"))
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        entry["normalized_response"]["boundaries"][1]["title"] = "篡改"
        with entry_path.open("w", encoding="utf-8", newline="") as stream:
            json.dump(entry, stream, ensure_ascii=False)

    error = _run_with_corruption(tmp_path, mutate)
    assert error.error_code == "CACHE_CORRUPT"
    assert "回放" in str(error) or "缓存" in str(error)


def test_llm_cache_messages_drift(tmp_path: Path) -> None:
    # 上游时间轴内容相同但role变化时llm-cache文件名随role_cache变化，
    # 而messages_sha256不符仅在条目身份与文件名一致时才走到复读——构造直接改写。
    def mutate(root: Path) -> None:
        (_boundaries_root(root) / "current.json").unlink()
        entry_path = next((_boundaries_root(root) / "llm-cache").glob("*.json"))
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        entry["messages_sha256"] = "e" * 64
        with entry_path.open("w", encoding="utf-8", newline="") as stream:
            json.dump(entry, stream, ensure_ascii=False)

    error = _run_with_corruption(tmp_path, mutate)
    assert error.error_code == "CACHE_CORRUPT"


def test_failed_and_empty_timeline_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path)
    index, view = _fake_plan_inputs()
    failed = dict(index, run_status="failed")
    monkeypatch.setattr(bvp, "load_published_timeline", lambda *a, **k: failed)
    with pytest.raises(InputTimelineCorruptError, match="run_status"):
        _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    empty = dict(index, events=[_event(1, 0, 5_000, "pe", event_type="page_change")])
    monkeypatch.setattr(bvp, "load_published_timeline", lambda *a, **k: empty)
    with pytest.raises(InputTimelineCorruptError, match="主文本事件"):
        _plan(root, ScriptedTransport([]), tmp_path=tmp_path)


def test_degraded_timeline_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path)
    index, view = _fake_plan_inputs()
    degraded = dict(index, run_status="degraded")
    monkeypatch.setattr(bvp, "load_published_timeline", lambda *a, **k: degraded)
    boundaries = [
        {"source_event_id": "te000001", "title": "开场"},
        {"source_event_id": "te000002", "title": "中段"},
    ]
    transport = ScriptedTransport([{"boundaries": boundaries}])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "success" and result.boundary_count == 2
    assert result.timeline_id == "timeline-fake-0001"


def test_current_symlink_hard_fail(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    current_path = _boundaries_root(root) / "current.json"
    target = first.group_path / BOUNDARY_MANIFEST_FILE
    try:
        current_path.unlink()
        os.symlink(target, current_path)
    except (OSError, NotImplementedError):
        pytest.skip("本机无符号链接权限")
    with pytest.raises(CurrentPointerCorruptError):
        _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)


def test_loaders_roundtrip(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    pointer = load_chapter_plan_current(root, TASK)
    assert pointer is not None and pointer["result_id"] == first.result_id
    plan = load_published_chapter_plan(root, TASK)
    assert plan == first.plan
    manifest = first.group_path / BOUNDARY_MANIFEST_FILE
    data = bytearray(manifest.read_bytes())
    data[-2] ^= 0x01
    manifest.write_bytes(bytes(data))
    with pytest.raises(CurrentPointerCorruptError):
        load_chapter_plan_current(root, TASK)


# ---------------------------------------------------------------------------
# DEV01真实条件e2e：只读复制正式Timeline 2.0组＋注入传输，零真实API
# ---------------------------------------------------------------------------


_DEV01_REAL = REPO_ROOT / "outputs" / "DEV01"


@pytest.mark.skipif(
    not (_DEV01_REAL / "task.json").is_file()
    or not (_DEV01_REAL / "m2" / "timelines" / "current.json").is_file(),
    reason="真实outputs/DEV01的Timeline 2.0正式组不存在",
)
def test_dev01_injected_e2e(tmp_path: Path) -> None:
    sandbox = tmp_path / "outputs" / "DEV01"
    sandbox.mkdir(parents=True)
    shutil.copy2(_DEV01_REAL / "task.json", sandbox / "task.json")
    shutil.copytree(_DEV01_REAL / "m2" / "timelines", sandbox / "m2" / "timelines")
    real_snapshot = _tree_sha_snapshot(_DEV01_REAL / "m2")
    root = tmp_path / "outputs"
    index = load_published_timeline(root, "DEV01")
    assert index is not None
    duration_ms = int(index["duration_ms"])
    eligible = [
        event
        for event in index["events"]
        if event["type"] == TRANSCRIPT_EVENT_TYPE and event["payload"]["boundary_eligible"]
    ]
    boundaries = [{"source_event_id": eligible[0]["id"], "title": "章节一"}]
    for event in eligible[1:]:
        start = int(event["start_ms"])
        if start >= MIN_CHAPTER_MS and duration_ms - start >= MIN_CHAPTER_MS:
            boundaries.append({"source_event_id": event["id"], "title": "章节二"})
            break
    transport = ScriptedTransport([{"boundaries": boundaries}])
    result = plan_chapters_v2(
        "DEV01",
        output_root=root,
        transport=transport,
        environ=ENV,
    )
    assert result.status == "success"
    assert result.boundary_count == len(boundaries)
    assert result.duration_ms == duration_ms
    before = _tree_sha_snapshot(root / "DEV01" / "m2" / "boundaries")
    rerun = plan_chapters_v2(
        "DEV01", output_root=root, transport=ScriptedTransport([]), environ=ENV
    )
    assert rerun.cache_hit is True and len(transport.calls) == 1
    assert _tree_sha_snapshot(root / "DEV01" / "m2" / "boundaries") == before
    # 2026-10-18真实调用验收后该不变量的正确形态：真实m2树逐项零改写（而非目录不存在）。
    assert _tree_sha_snapshot(_DEV01_REAL / "m2") == real_snapshot


# ---------------------------------------------------------------------------
# 实现决定13（2026-10-18真实调用验收缺陷修正）：transport返回content字符串形态
# ---------------------------------------------------------------------------


def test_real_transport_string_json_publishes_primary(tmp_path: Path) -> None:
    """真实OpenAICompatibleTransport返回content字符串；解析后必须走通primary原路径。"""

    root = _workspace(tmp_path)
    transport = ScriptedTransport([json.dumps(GOOD_RESPONSE, ensure_ascii=False)])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "success" and result.result_source == "primary"
    assert result.business_call_count == 1 and result.supplier_request_count == 1
    group = _boundaries_root(root) / "formal-results" / result.result_id
    assert (group / BOUNDARY_RAW_FILE).read_bytes() == bvp._json_bytes(GOOD_RESPONSE)
    entry_path = next((_boundaries_root(root) / "llm-cache").glob("*.json"))
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    assert entry["raw_response"] == GOOD_RESPONSE
    before = _tree_sha_snapshot(_boundaries_root(root))
    rerun = _plan(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert rerun.cache_hit is True and rerun.result_id == result.result_id
    assert _tree_sha_snapshot(_boundaries_root(root)) == before


def test_real_transport_string_malformed_json_validation_feedback(tmp_path: Path) -> None:
    """非法JSON字符串落RESPONSE_NOT_OBJECT校验归类；纠错回声不含assistant回显。"""

    root = _workspace(tmp_path)
    transport = ScriptedTransport(["这不是JSON", json.dumps(GOOD_RESPONSE)])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "success" and result.supplier_request_count == 2
    first_error = result.report["errors"][0]
    assert first_error["kind"] == "validation"
    assert first_error["issues"][0]["code"] == "RESPONSE_NOT_OBJECT"
    second = transport.calls[1]["messages"]
    assert second[-1]["role"] == "user"
    assert all(item["role"] != "assistant" for item in second)


def test_real_transport_string_json_list_double_fail_emergency(tmp_path: Path) -> None:
    """解析为非对象（list/str标量）同落RESPONSE_NOT_OBJECT；两次耗尽预算→emergency。"""

    root = _workspace(tmp_path)
    transport = ScriptedTransport(['[{"boundaries": []}]', '"只是一个字符串"'])
    result = _plan(root, transport, tmp_path=tmp_path)
    assert result.status == "degraded" and result.result_source == "emergency"
    assert result.supplier_request_count == 2
    codes = [err["issues"][0]["code"] for err in result.report["errors"]]
    assert codes == ["RESPONSE_NOT_OBJECT", "RESPONSE_NOT_OBJECT"]
