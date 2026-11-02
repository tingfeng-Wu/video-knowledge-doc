"""阶段14-C定向测试：DocumentComposerV2三调用正文组合与StructuredDocumentContent 3.0发布。

契约唯一权威：docs/development/阶段14CD-M2文档链裁决.md（Q1—Q7＋实现决定1—23）。
矩阵组织：R=账本/payload/prompt/响应规范化纯函数；A=装配与validator-3.0；
I=输入身份/缺失/损坏与绑定核验（锁前零写入）；M=三调用拓扑/应急两调用/无重试/
预算守卫/llm-cache；P=发布六件套/早退缓存/复用与pointer_repaired/注入矩阵/
中断/锁；G=组复验篡改；D=零API dry-run；C=CLI compose-document-v2接线。

全部离线：集成层用真实模块链（13-1合成工作区＋build_timeline_v2＋14-B注入
plan＋14-A select_chapter_frames），正文三槽位用ScriptedTransport脚本应答；
零网络、零真实API、零FFmpeg；M1与真实outputs只读不触碰。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.test_boundary_planner_v2 import (
    ENV,
    GOOD_RESPONSE,
    HAPPY_EVENTS,
    SECRET,
    ScriptedTransport,
    _plan,
)
from tests.test_boundary_planner_v2 import _registry as _boundary_registry
from tests.test_timeline_builder_v2 import TASK
from tests.test_timeline_builder_v2 import _workspace as _synth_chain
from video_doc import chapter_frames as cf
from video_doc import document_composer_v2 as dc
from video_doc.boundary_planner_v2 import load_chapter_plan_current
from video_doc.cli import main
from video_doc.task_workspace import TaskWorkspaceError, initialize_task_workspace
from video_doc.timeline_builder_v2 import build_timeline_v2
from video_doc.unified_timeline import TRANSCRIPT_EVENT_TYPE, canonical_json_bytes

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "config" / "model-roles.json"

# 冻结角色哈希复核锚点（裁决Q6.3成文值；registry内已钉入）
CONTENT_PROMPT_BUNDLE_SHA256 = (
    "8fc35480c7bb0fe6282874f331de73765465faace8a37f46d8e473c77e74e6a2"
)

# HAPPY链账本：c0001=[te000001]"alpha"；c0002=[te000002,te000003]"beta gamma"（合并一单元）
SINGLE_OK = {"paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "开场讲解第一段。"}]}
BATCH_OK = {
    "chapters": [
        {
            "chapter_id": "c0002",
            "paragraphs": [{"source_unit_ids": ["c0002-u001"], "text": "中段与结尾讲解。"}],
        }
    ]
}
GLOBAL_OK = {
    "title": "测试视频结构化文档",
    "overview": ["三段内容依次展开。"],
    "conclusions": ["全文两章即可覆盖。"],
}
EMERG_SINGLE = {
    "paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "完整内容讲解一段。"}]
}
EMERG_GLOBAL = {
    "title": "应急单章文档",
    "overview": ["全片归入单章。"],
    "conclusions": ["应急口径不宣称分章成功。"],
}


def _responses() -> list[Any]:
    return [copy.deepcopy(SINGLE_OK), copy.deepcopy(BATCH_OK), copy.deepcopy(GLOBAL_OK)]


def _emerg_responses() -> list[Any]:
    return [copy.deepcopy(EMERG_SINGLE), copy.deepcopy(EMERG_GLOBAL)]


class _ContentTransport(ScriptedTransport):
    """记录逐调用config.max_tokens/temperature的脚本传输替身。"""

    def complete(self, config: Any, messages: Any, response_format: Any) -> Any:
        item = super().complete(config, messages, response_format)
        self.calls[-1]["max_tokens"] = config.max_tokens
        self.calls[-1]["temperature"] = config.temperature
        return item


# ---------------------------------------------------------------------------
# 链与registry工厂
# ---------------------------------------------------------------------------


def _chain(tmp_path: Path) -> Path:
    """合成M1＋Timeline 2.0(auto)＋14-B primary plan；未跑14-A。"""
    root = _synth_chain(tmp_path, m1_events=HAPPY_EVENTS, duration_seconds=600.0)
    initialize_task_workspace(root, TASK)
    build_timeline_v2(TASK, output_root=root, ocr="auto")
    _plan(root, ScriptedTransport([copy.deepcopy(GOOD_RESPONSE)]), tmp_path=tmp_path)
    return root


def _chain_emerg(tmp_path: Path) -> Path:
    """同链但14-B走两次传输失败→emergency单章plan。"""
    root = _synth_chain(tmp_path, m1_events=HAPPY_EVENTS, duration_seconds=600.0)
    initialize_task_workspace(root, TASK)
    build_timeline_v2(TASK, output_root=root, ocr="auto")
    _plan(
        root,
        ScriptedTransport([RuntimeError("boom-1"), RuntimeError("boom-2")]),
        tmp_path=tmp_path,
    )
    pointer = load_chapter_plan_current(root, TASK)
    assert pointer is not None
    return root


def _chain_full(tmp_path: Path) -> Path:
    root = _chain(tmp_path)
    cf.select_chapter_frames(TASK, output_root=root)
    return root


def _chain_full_emerg(tmp_path: Path) -> Path:
    root = _chain_emerg(tmp_path)
    cf.select_chapter_frames(TASK, output_root=root)
    return root


def _content_registry(
    tmp_path: Path,
    *,
    mutate: Any = None,
    drop_role: bool = False,
    fixture_only: bool | None = None,
    name: str = "content-registry.json",
) -> Path:
    payload = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    if drop_role:
        payload["llm_roles"].pop("content_model")
    elif mutate is not None:
        mutate(payload["llm_roles"]["content_model"])
    if fixture_only is not None:
        payload["fixture_only"] = fixture_only
    path = tmp_path / name
    with path.open("w", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return path


def _compose(
    root: Path,
    transport: ScriptedTransport,
    *,
    tmp_path: Path | None = None,
    registry: Path | None = None,
    environ: Any = ENV,
    **kwargs: Any,
) -> dc.DocumentCompositionResult:
    if registry is None:
        base = tmp_path if tmp_path is not None else root
        registry = _content_registry(base)
    return dc.compose_document_v2(
        root,
        TASK,
        model_registry=registry,
        transport=transport,
        environ=environ,
        **kwargs,
    )


def _m2_root(root: Path) -> Path:
    return root / TASK / "m2"


def _own_root(root: Path) -> Path:
    return _m2_root(root) / dc.ROOT_DIRECTORY


def _failures_dir(root: Path) -> Path:
    return _m2_root(root) / "failures" / dc.MODULE_TOKEN


def _only_failure(root: Path) -> dict[str, Any]:
    entries = list(_failures_dir(root).iterdir())
    assert len(entries) == 1
    return json.loads((entries[0] / "failure.json").read_text(encoding="utf-8"))


def _only_failure_evidence(root: Path) -> dict[str, Any]:
    """修复裁决D1：CONTENT_MODEL_FAILED失败组的model-responses.json取证文件。"""
    entries = list(_failures_dir(root).iterdir())
    assert len(entries) == 1
    return json.loads(
        (entries[0] / dc.MODEL_RESPONSES_FILE).read_text(encoding="utf-8")
    )


def _tree_sha_pairs(root: Path) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for entry in sorted(root.rglob("*")):
        if entry.is_file():
            data = entry.read_bytes()
            out[entry.relative_to(root).as_posix()] = (len(data), hashlib.sha256(data).hexdigest())
    return out


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_report_bytes(group: Path, report: Any) -> None:
    _write_json(group / dc.REPORT_FILE, report)


def _write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)


def _content_inputs(root: Path) -> dc.ContentInputs:
    return dc._load_content_inputs(Path(root), TASK)


# ---------------------------------------------------------------------------
# R矩阵：账本构建、payload装配、prompt模板与响应规范化（裁决Q2/Q3）
# ---------------------------------------------------------------------------


def _hand_timeline(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"run_status": "success", "events": events}


def _hand_event(index: int, start_ms: int, text: str) -> dict[str, Any]:
    return {
        "id": f"te{index:06d}",
        "type": TRANSCRIPT_EVENT_TYPE,
        "start_ms": start_ms,
        "end_ms": start_ms + 1000,
        "payload": {"text": text},
    }


def _hand_plan(boundaries: list[tuple[int, str]], duration_ms: int) -> dict[str, Any]:
    return {
        "duration_ms": duration_ms,
        "boundaries": [
            {"timestamp_ms": start, "title": title, "source_event_id": None}
            for start, title in boundaries
        ],
    }


def test_r_ledgers_partition_and_merge_units() -> None:
    timeline = _hand_timeline(
        [_hand_event(1, 0, "aa"), _hand_event(2, 40_000, "bb"), _hand_event(3, 80_000, "cc")]
    )
    plan = _hand_plan([(0, "甲"), (50_000, "乙")], 100_000)
    ledgers = dc._build_ledgers(timeline, plan)
    assert [item.chapter_id for item in ledgers] == ["c0001", "c0002"]
    assert ledgers[0].units[0]["text"] == "aa bb"
    assert ledgers[0].units[0]["source_event_ids"] == ["te000001", "te000002"]
    assert ledgers[1].units[0]["source_event_ids"] == ["te000003"]
    assert ledgers[1].units[0]["text"] == "cc"
    assert ledgers[0].start_ms == 0 and ledgers[0].end_ms == 50_000
    assert ledgers[1].start_ms == 50_000 and ledgers[1].end_ms == 100_000


def test_r_ledgers_unit_800_cap_no_silent_truncation() -> None:
    events = [_hand_event(i + 1, i * 1000, "x" * 300) for i in range(4)]
    timeline = _hand_timeline(events)
    plan = _hand_plan([(0, "甲")], 10_000)
    ledgers = dc._build_ledgers(timeline, plan)
    assert len(ledgers[0].units) == 2
    for unit in ledgers[0].units:
        assert len(unit["text"]) <= dc.UNIT_MAX_CHARS
    # 贪心合并：300+1+300=601≤800成u001；再加一事件902>800另起u002
    flat = [eid for unit in ledgers[0].units for eid in unit["source_event_ids"]]
    assert flat == ["te000001", "te000002", "te000003", "te000004"]
    assert ledgers[0].units[0]["source_event_ids"] == ["te000001", "te000002"]
    assert ledgers[0].units[0]["text"] == f"{'x' * 300} {'x' * 300}"
    assert ledgers[0].units[1]["source_event_ids"] == ["te000003", "te000004"]


def test_r_ledgers_overlong_single_event_hard_fail() -> None:
    timeline = _hand_timeline([_hand_event(1, 0, "y" * (dc.UNIT_MAX_CHARS + 1))])
    plan = _hand_plan([(0, "甲")], 10_000)
    with pytest.raises(dc.UnitConstructionFailedError) as excinfo:
        dc._build_ledgers(timeline, plan)
    assert excinfo.value.error_code == dc.ERROR_UNIT_CONSTRUCTION_FAILED


def test_r_ledgers_blank_event_hard_fail() -> None:
    timeline = _hand_timeline([_hand_event(1, 0, "   ")])
    plan = _hand_plan([(0, "甲")], 10_000)
    with pytest.raises(dc.UnitConstructionFailedError):
        dc._build_ledgers(timeline, plan)


def test_r_ledgers_rejects_failed_run_status_and_no_te() -> None:
    plan = _hand_plan([(0, "甲")], 10_000)
    failed = {"run_status": "failed", "events": [_hand_event(1, 0, "aa")]}
    with pytest.raises(dc.InputTimelineCorruptError):
        dc._build_ledgers(failed, plan)
    empty = {"run_status": "success", "events": []}
    with pytest.raises(dc.InputTimelineCorruptError):
        dc._build_ledgers(empty, plan)


def test_r_payload_builders_exact_shape() -> None:
    timeline = _hand_timeline([_hand_event(1, 0, "aa"), _hand_event(2, 60_000, "bb")])
    plan = _hand_plan([(0, "甲"), (50_000, "乙")], 100_000)
    ledgers = dc._build_ledgers(timeline, plan)
    single = dc.build_single_payload(ledgers[0])
    assert single == {"title": "甲", "units": [{"unit_id": "c0001-u001", "text": "aa"}]}
    batch = dc.build_batch_payload(ledgers[1:])
    assert batch == {
        "chapters": [
            {
                "chapter_id": "c0002",
                "title": "乙",
                "units": [{"unit_id": "c0002-u001", "text": "bb"}],
            }
        ]
    }
    paragraphs = {"c0001": [{"text": "T1"}], "c0002": [{"text": "T2"}]}
    glob = dc.build_global_payload(ledgers, paragraphs)
    assert glob == {
        "chapters": [
            {"chapter_id": "c0001", "title": "甲", "paragraphs": ["T1"]},
            {"chapter_id": "c0002", "title": "乙", "paragraphs": ["T2"]},
        ]
    }


def test_r_prompt_hashes_frozen() -> None:
    assert dc.content_prompt_bundle_sha256() == CONTENT_PROMPT_BUNDLE_SHA256
    per_slot = {slot: dc.content_prompt_content_sha256(slot) for slot in dc.CALL_SLOTS}
    assert len(set(per_slot.values())) == 3
    assert all(len(value) == 64 for value in per_slot.values())


def test_r_messages_deterministic_two_templates() -> None:
    payload = {"title": "甲", "units": [{"unit_id": "c0001-u001", "text": "aa"}]}
    first = dc.build_slot_messages(dc.CALL_SINGLE, payload)
    second = dc.build_slot_messages(dc.CALL_SINGLE, payload)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert [item["role"] for item in first] == ["system", "user"]
    assert first[0]["content"] == dc.DOCUMENT_CONTENT_SINGLE_BATCH_SYSTEM
    assert dc.build_slot_messages(dc.CALL_GLOBAL, payload)[0]["content"] == (
        dc.DOCUMENT_CONTENT_GLOBAL_SYSTEM
    )
    assert first[1]["content"].startswith(dc._USER_TASK_INSTRUCTION)
    assert "c0001-u001" in first[1]["content"]


@pytest.mark.parametrize(
    ("slot", "response", "code"),
    [
        (dc.CALL_SINGLE, "not-a-dict", "RESPONSE_NOT_OBJECT"),
        (dc.CALL_SINGLE, {"paragraphs": [], "extra": 1}, "RESPONSE_KEYS_INVALID"),
        (dc.CALL_SINGLE, {"paragraphs": []}, "PARAGRAPHS_MISSING"),
        (dc.CALL_SINGLE, {"paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "  "}]}, "PARAGRAPH_TEXT_EMPTY"),
        (dc.CALL_SINGLE, {"paragraphs": [{"source_unit_ids": ["c99-u001"], "text": "好"}]}, "UNIT_ID_NOT_FOUND"),
        (dc.CALL_SINGLE, {"paragraphs": [{"source_unit_ids": ["c0002-u001"], "text": "好"}]}, "UNIT_CROSS_CHAPTER"),
        (
            dc.CALL_SINGLE,
            {"paragraphs": [
                {"source_unit_ids": ["c0001-u002"], "text": "后"},
                {"source_unit_ids": ["c0001-u001"], "text": "先"},
            ]},
            "UNIT_ORDER_VIOLATION",
        ),
        (
            dc.CALL_SINGLE,
            {"paragraphs": [
                {"source_unit_ids": ["c0001-u001"], "text": "一"},
                {"source_unit_ids": ["c0001-u001"], "text": "重复"},
            ]},
            "UNIT_DUPLICATE_REFERENCE",
        ),
        (
            dc.CALL_SINGLE,
            {"paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "只覆盖首单元"}]},
            "UNIT_COVERAGE_INCOMPLETE",
        ),
        (dc.CALL_BATCH, {"chapters": []}, "PARAGRAPHS_MISSING"),
        (dc.CALL_BATCH, {"chapters": [{"chapter_id": "c9999", "paragraphs": []}]}, "BATCH_CHAPTERS_MISMATCH"),
        (dc.CALL_GLOBAL, {"title": "", "overview": ["有"], "conclusions": ["有"]}, "GLOBAL_TITLE_INVALID"),
        (
            dc.CALL_GLOBAL,
            {"title": "题", "overview": ["一", "二", "三"], "conclusions": ["结"]},
            "OVERVIEW_COUNT_INVALID",
        ),
        (
            dc.CALL_GLOBAL,
            {"title": "题", "overview": ["述"], "conclusions": []},
            "CONCLUSIONS_COUNT_INVALID",
        ),
        (
            dc.CALL_GLOBAL,
            {"title": "题" * (dc.TITLE_MAX_CHARS + 1), "overview": ["述"], "conclusions": ["结"]},
            "GLOBAL_TITLE_INVALID",
        ),
    ],
)
def test_r_normalize_business_codes(slot: str, response: Any, code: str) -> None:
    timeline = _hand_timeline(
        [
            _hand_event(1, 0, "a" * 300),
            _hand_event(2, 1000, "b" * 300),
            _hand_event(3, 2000, "c" * 300),
            _hand_event(4, 60_000, "d"),
        ]
    )
    plan = _hand_plan([(0, "甲"), (50_000, "乙")], 100_000)
    ledgers = dc._build_ledgers(timeline, plan)
    # c0001需两单元（u001/u002）方能触发顺序/覆盖类码
    assert [len(item.units) for item in ledgers] == [2, 1]
    if slot == dc.CALL_SINGLE:
        normalized, issues = dc.normalize_single_response(response, ledgers[0])
    elif slot == dc.CALL_BATCH:
        normalized, issues = dc.normalize_batch_response(response, ledgers[1:])
    else:
        normalized, issues = dc.normalize_global_response(response)
    assert normalized is None
    assert code in {item["code"] for item in issues}
    assert code in dc.BUSINESS_CODES


def test_r_normalize_happy_paths() -> None:
    timeline = _hand_timeline([_hand_event(1, 0, "aa"), _hand_event(2, 60_000, "bb")])
    plan = _hand_plan([(0, "甲"), (50_000, "乙")], 100_000)
    ledgers = dc._build_ledgers(timeline, plan)
    single, issues = dc.normalize_single_response(
        {"paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": " 讲解 "}]}, ledgers[0]
    )
    assert issues == [] and single["paragraphs"][0]["text"] == "讲解"
    batch, issues = dc.normalize_batch_response(
        {"chapters": [{"chapter_id": "c0002", "paragraphs": [{"source_unit_ids": ["c0002-u001"], "text": "乙章"}]}]},
        ledgers[1:],
    )
    assert issues == [] and batch["chapters"][0]["chapter_id"] == "c0002"
    glob, issues = dc.normalize_global_response(
        {"title": " 总标题 ", "overview": ["述"], "conclusions": ["结"]}
    )
    assert issues == [] and glob["title"] == "总标题"


def test_r_merge_paragraphs_to_cap_pairwise() -> None:
    paragraphs = [
        {"source_unit_ids": [f"c0001-u{index:03d}"], "text": f"段落{index}"}
        for index in range(1, 7)
    ]
    merged = dc._merge_paragraphs_to_cap(paragraphs, max_blocks=4)
    assert len(merged) == 4
    units = [uid for item in merged for uid in item["source_unit_ids"]]
    assert units == [f"c0001-u{index:03d}" for index in range(1, 7)]
    assert any("\n" in item["text"] for item in merged)


def test_r_merge_prefers_min_adjacent_pair() -> None:
    paragraphs = [
        {"source_unit_ids": ["a"], "text": "x" * 100},
        {"source_unit_ids": ["b"], "text": "y"},
        {"source_unit_ids": ["c"], "text": "z" * 100},
    ]
    merged = dc._merge_paragraphs_to_cap(paragraphs, max_blocks=2)
    # a+b与b+c并集大小相同（101），min tie取最早相邻对
    assert len(merged) == 2
    assert merged[0]["source_unit_ids"] == ["a", "b"]
    assert merged[1]["source_unit_ids"] == ["c"]


# ---------------------------------------------------------------------------
# A矩阵：确定性装配与validator-3.0交叉复验（裁决Q1/Q4）
# ---------------------------------------------------------------------------


def _assemble_on(tmp_path: Path, emergency: bool = False) -> tuple[dc.ContentInputs, dict[str, Any]]:
    root = _chain_full_emerg(tmp_path) if emergency else _chain_full(tmp_path)
    inputs = _content_inputs(root)
    responses = _emerg_responses() if emergency else _responses()
    normalized = {
        dc.CALL_SINGLE: dc.normalize_slot_response(dc.CALL_SINGLE, responses[0], inputs)[0],
        dc.CALL_GLOBAL: dc.normalize_slot_response(dc.CALL_GLOBAL, responses[-1], inputs)[0],
    }
    batch = None
    if len(inputs.ledgers) > 1:
        batch = dc.normalize_slot_response(dc.CALL_BATCH, responses[1], inputs)[0]
    content = dc.assemble_document_content(
        inputs,
        normalized[dc.CALL_SINGLE],
        batch,
        normalized[dc.CALL_GLOBAL],
        mode=dc.MODE_MOCK,
        backend="mock",
    )
    return inputs, content


def test_a_assemble_shape_happy(tmp_path: Path) -> None:
    inputs, content = _assemble_on(tmp_path)
    assert set(content) == dc._CONTENT_TOP_KEYS
    assert content["schema_name"] == dc.CONTENT_SCHEMA_NAME
    assert content["schema_version"] == dc.CONTENT_SCHEMA_VERSION
    assert content["content_id"].startswith(dc.RESULT_ID_PREFIX)
    assert content["task_id"] == TASK
    assert content["title"] == GLOBAL_OK["title"]
    assert [chapter["chapter_id"] for chapter in content["chapters"]] == ["c0001", "c0002"]
    chapter = content["chapters"][0]
    assert set(chapter) == dc._CHAPTER_KEYS
    block = chapter["blocks"][0]
    assert set(block) == dc._BLOCK_KEYS
    assert block["type"] == dc.BLOCK_TYPE and block["content_origin"] == dc.BLOCK_ORIGIN
    assert block["source_event_ids"] == ["te000001"]
    assert content["overview"][0]["block_id"] == "overview-p001"
    assert content["overall_conclusion"][0]["block_id"] == "conclusion-p001"
    dc.validate_structured_document_content_v3(
        content,
        plan=inputs.plan,
        timeline_index=inputs.timeline_index,
        selection=inputs.selection,
        expected_task_id=TASK,
    )


def test_a_assemble_frames_projection_from_selection(tmp_path: Path) -> None:
    inputs, content = _assemble_on(tmp_path)
    for position, chapter in enumerate(content["chapters"]):
        projected = dc._project_frames(inputs.selection["chapters"][position])
        assert chapter["frames"] == projected
        for frame in chapter["frames"]:
            assert set(frame) == dc._FRAME_PROJECTION_KEY_SET


def test_a_assemble_content_id_reproducible(tmp_path: Path) -> None:
    inputs, content = _assemble_on(tmp_path)
    stripped = {key: value for key, value in content.items() if key != "content_id"}
    assert dc._compute_content_id(stripped) == content["content_id"]


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        (("chapters", 0, "index"), 7, "index"),
        (("chapters", 0, "blocks", 0, "block_id"), "c0099-p001", "block_id"),
        (("coverage", "totals", "units"), 99, "coverage"),
        (("chapters", 0, "frames"), [], "帧"),
        (("title",), "  ", "title"),
    ],
    ids=["chapter-index", "block-id", "coverage-totals", "frames-dropped", "blank-title"],
)
def test_a_validator_rejects_tampering(tmp_path: Path, path: Any, value: Any, needle: str) -> None:
    inputs, content = _assemble_on(tmp_path)
    if path == ("title",):
        content["title"] = "  "
    elif path == ("chapters", 0, "frames"):
        content["chapters"][0]["frames"] = []
    else:
        target: Any = content
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    with pytest.raises(dc.StructuredContentValidationError):
        dc.validate_structured_document_content_v3(
            content,
            plan=inputs.plan,
            timeline_index=inputs.timeline_index,
            selection=inputs.selection,
            expected_task_id=TASK,
        )


def test_a_emergency_assembles_single_chapter(tmp_path: Path) -> None:
    inputs, content = _assemble_on(tmp_path, emergency=True)
    assert len(content["chapters"]) == 1
    assert content["source_plan"]["result_source"] == "emergency"
    assert content["source_plan"]["quality_level"] == "degraded"
    assert content["chapters"][0]["blocks"][0]["source_event_ids"] == [
        "te000001",
        "te000002",
        "te000003",
    ]
    dc.validate_structured_document_content_v3(
        content,
        plan=inputs.plan,
        timeline_index=inputs.timeline_index,
        selection=inputs.selection,
        expected_task_id=TASK,
    )


# ---------------------------------------------------------------------------
# I矩阵：输入缺失/损坏/绑定错配，锁前零写入（裁决Q4.3/Q5；实现决定2/3）
# ---------------------------------------------------------------------------


def _assert_zero_writes(root: Path) -> None:
    assert not _own_root(root).exists()
    assert not _failures_dir(root).exists()


def _assert_not_published(root: Path) -> None:
    # formal-results父目录在发布准备时预建，清理后允许作为空目录存在；
    # 不得有组目录或.tmp残留，current必须缺席。
    groups = _own_root(root) / dc.GROUP_DIRECTORY
    if groups.exists():
        assert [path.name for path in groups.iterdir()] == []
    assert not _current_file(root, dc.ROOT_DIRECTORY).exists()


def _current_file(root: Path, domain: str) -> Path:
    return _m2_root(root) / domain / "current.json"


@pytest.mark.parametrize(
    ("remove", "cls", "code"),
    [
        ("task", dc.InputTaskMissingError, dc.ERROR_INPUT_TASK_MISSING),
        ("timelines", dc.InputTimelineMissingError, dc.ERROR_INPUT_TIMELINE_MISSING),
        ("boundaries", dc.InputChapterPlanMissingError, dc.ERROR_INPUT_CHAPTER_PLAN_MISSING),
        (
            "chapter-frames",
            dc.InputChapterFrameSelectionMissingError,
            dc.ERROR_INPUT_CHAPTER_FRAME_SELECTION_MISSING,
        ),
        ("frames", dc.InputFrameSetMissingError, dc.ERROR_INPUT_FRAME_SET_MISSING),
    ],
)
def test_i_missing_upstream_current(tmp_path: Path, remove: str, cls: type, code: str) -> None:
    root = _chain_full(tmp_path)
    if remove == "task":
        (root / TASK / "task.json").unlink()
    else:
        _current_file(root, remove).unlink()
    transport = ScriptedTransport(_responses())
    with pytest.raises(cls) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert excinfo.value.error_code == code
    assert transport.calls == []
    _assert_zero_writes(root)


@pytest.mark.parametrize(
    ("domain", "cls", "code"),
    [
        ("timelines", dc.InputTimelineCorruptError, dc.ERROR_INPUT_TIMELINE_CORRUPT),
        ("boundaries", dc.InputChapterPlanCorruptError, dc.ERROR_INPUT_CHAPTER_PLAN_CORRUPT),
        (
            "chapter-frames",
            dc.InputChapterFrameSelectionCorruptError,
            dc.ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT,
        ),
        ("frames", dc.InputFrameSetCorruptError, dc.ERROR_INPUT_FRAME_SET_CORRUPT),
    ],
)
def test_i_corrupt_upstream_current(tmp_path: Path, domain: str, cls: type, code: str) -> None:
    root = _chain_full(tmp_path)
    _current_file(root, domain).write_bytes(b"not-json\n")
    transport = ScriptedTransport(_responses())
    with pytest.raises(cls) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert excinfo.value.error_code == code
    assert transport.calls == []
    _assert_zero_writes(root)


def test_i_task_record_identity_mismatch(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    _write_json(root / TASK / "task.json", {"task_id": "OTHER01"})
    with pytest.raises(dc.InputTaskMissingError):
        _compose(root, ScriptedTransport(_responses()), tmp_path=tmp_path)
    _assert_zero_writes(root)


def test_i_invalid_task_id_rejected_before_any_read(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    snapshot = _tree_sha_pairs(root)
    with pytest.raises(TaskWorkspaceError):
        dc.compose_document_v2(root, "..\\evil", transport=ScriptedTransport([]))
    assert _tree_sha_pairs(root) == snapshot


def test_i_selection_doc_tampered_is_corrupt(tmp_path: Path) -> None:
    # composer只读frames current指针，帧集成员内容归exporter校验；
    # 篡改被14-A loader逐文件sha复核的selection组主文档才触发CORRUPT。
    root = _chain_full(tmp_path)
    pointer = _read_json(_current_file(root, "chapter-frames"))
    group = _m2_root(root) / "chapter-frames" / pointer["group"]
    (group / "chapter-frame-selection.json").write_bytes(b'{"tampered": true}\n')
    with pytest.raises(dc.InputChapterFrameSelectionCorruptError) as excinfo:
        _compose(root, ScriptedTransport(_responses()), tmp_path=tmp_path)
    assert excinfo.value.error_code == dc.ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT
    _assert_zero_writes(root)


def _patch_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeline: Any = None,
    plan: Any = None,
    selection: Any = None,
    selection_pointer: Any = None,
    frames_pointer: Any = None,
) -> None:
    if timeline is not None:
        monkeypatch.setattr(dc, "_load_timeline_inputs", lambda *a, **k: timeline)
    if plan is not None:
        monkeypatch.setattr(dc, "_load_plan_inputs", lambda *a, **k: plan)
    if selection is not None:
        monkeypatch.setattr(dc, "_load_selection", lambda *a, **k: selection)
    if selection_pointer is not None:
        monkeypatch.setattr(dc, "_load_selection_pointer", lambda *a, **k: selection_pointer)
    if frames_pointer is not None:
        monkeypatch.setattr(dc, "_load_frames_pointer", lambda *a, **k: frames_pointer)


def test_i_binding_mismatch_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _chain_full(tmp_path)
    inputs = _content_inputs(root)
    variants: list[tuple[str, dict[str, Any]]] = []

    doc = copy.deepcopy(inputs.timeline_index)
    doc["task_id"] = "OTHER01"
    # 同时stub plan加载器，避免真实_load_plan_inputs拿坏timeline_index先触发上游CORRUPT
    variants.append(
        (
            "timeline task_id",
            {"timeline": (inputs.timeline_pointer, doc), "plan": (inputs.plan_pointer, copy.deepcopy(inputs.plan))},
        )
    )

    doc = copy.deepcopy(inputs.plan)
    doc["task_id"] = "OTHER01"
    variants.append(("plan task_id", {"plan": (inputs.plan_pointer, doc)}))

    doc = copy.deepcopy(inputs.selection)
    doc["task_id"] = "OTHER01"
    variants.append(("selection task_id", {"selection": doc}))

    pointer = copy.deepcopy(inputs.timeline_pointer)
    pointer["result_id"] = "unified-timeline-000000000000"
    variants.append(
        ("timeline current vs index", {"timeline": (pointer, inputs.timeline_index)})
    )

    doc = copy.deepcopy(inputs.plan)
    doc["source_timeline"]["timeline_id"] = "unified-timeline-111111111111"
    variants.append(("plan bound timeline", {"plan": (inputs.plan_pointer, doc)}))

    doc = copy.deepcopy(inputs.selection)
    doc["source_plan"]["result_id"] = "chapter-plan-ffffffffffff"
    variants.append(("selection bound plan id", {"selection": doc}))

    doc = copy.deepcopy(inputs.selection)
    doc["source_plan"]["manifest_sha256"] = "f" * 64
    variants.append(("selection bound plan manifest", {"selection": doc}))

    doc = copy.deepcopy(inputs.selection)
    doc["source_timeline"]["timeline_id"] = "unified-timeline-222222222222"
    variants.append(("selection bound timeline", {"selection": doc}))

    doc = copy.deepcopy(inputs.selection)
    doc["source_frame_set"]["frame_set_id"] = "frame-set-eeeeeeeeeeee"
    variants.append(("selection bound frame set id", {"selection": doc}))

    doc = copy.deepcopy(inputs.selection)
    doc["source_frame_set"]["manifest_sha256"] = "e" * 64
    variants.append(("selection bound frame set manifest", {"selection": doc}))

    pointer = copy.deepcopy(inputs.selection_pointer)
    pointer["result_id"] = "chapter-frame-selection-0123456789ab"
    variants.append(
        (
            "selection current vs content id",
            {
                "selection": copy.deepcopy(inputs.selection),
                "selection_pointer": pointer,
            },
        )
    )

    for label, patch in variants:
        _patch_inputs(monkeypatch, **patch)
        transport = ScriptedTransport(_responses())
        with pytest.raises(dc.InputIdentityMismatchError) as excinfo:
            _compose(root, transport, tmp_path=tmp_path)
        assert excinfo.value.error_code == dc.ERROR_INPUT_IDENTITY_MISMATCH, label
        assert transport.calls == [], label
        _assert_zero_writes(root)
        monkeypatch.undo()  # 恢复全部加载器，下一变体重新打补丁


# ---------------------------------------------------------------------------
# M矩阵：三调用拓扑、应急两调用、无重试、预算守卫与llm-cache（裁决Q2/Q3）
# ---------------------------------------------------------------------------


def test_m_primary_three_calls_topology(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    transport = _ContentTransport(_responses())
    result = _compose(root, transport, tmp_path=tmp_path)
    assert result.business_call_count == 3
    assert result.supplier_request_count == 3
    assert result.cache_hit_count == 0
    assert result.cache_hit is False and result.pointer_repaired is False
    assert result.chapter_count == 2
    assert len(transport.calls) == 3
    for call in transport.calls:
        assert call["model"] == dc.FROZEN_MODEL
        assert call["response_format"] == {"type": "json_object"}
        assert call["temperature"] == 0.0
    assert [call["max_tokens"] for call in transport.calls] == [1800, 8192, 1600]
    assert "开场" in transport.calls[0]["messages"][1]["content"]
    assert "c0002" in transport.calls[1]["messages"][1]["content"]
    assert "开场讲解第一段。" in transport.calls[2]["messages"][1]["content"]
    entries = list((_own_root(root) / dc.LLM_CACHE_DIRECTORY).glob("*.json"))
    assert len(entries) == 3
    raw_doc = _read_json(result.group_path / dc.RAW_RESPONSES_FILE)
    assert [item["call_slot"] for item in raw_doc["calls"]] == list(dc.CALL_SLOTS)
    assert all(item["cache_hit"] is False and item["attempt_count"] == 1 for item in raw_doc["calls"])
    report = _read_json(result.group_path / dc.REPORT_FILE)
    assert report["business_call_count"] == 3
    assert report["supplier_request_count"] == 3
    assert report["cache_hit_count"] == 0
    assert set(result.manifest) == dc._MANIFEST_KEYS
    assert result.manifest["status"] == "success"


def test_m_emergency_two_calls(tmp_path: Path) -> None:
    root = _chain_full_emerg(tmp_path)
    transport = _ContentTransport(_emerg_responses())
    result = _compose(root, transport, tmp_path=tmp_path)
    assert len(transport.calls) == 2
    assert result.business_call_count == 2
    assert result.supplier_request_count == 2
    assert result.cache_hit_count == 0
    assert result.chapter_count == 1
    assert result.plan_result_source == "emergency"
    assert result.plan_quality_level == "degraded"
    raw_doc = _read_json(result.group_path / dc.RAW_RESPONSES_FILE)
    assert [item["call_slot"] for item in raw_doc["calls"]] == [dc.CALL_SINGLE, dc.CALL_GLOBAL]
    entries = list((_own_root(root) / dc.LLM_CACHE_DIRECTORY).glob("*.json"))
    assert len(entries) == 2


def test_m_transport_failures_retry_then_fail(tmp_path: Path) -> None:
    """修复裁决D2：首槽传输失败→纠错重试一次→仍失败即硬失败；短路后续调用。"""
    root = _chain_full(tmp_path)
    transport = ScriptedTransport([RuntimeError("net-1"), RuntimeError("net-2")])
    with pytest.raises(dc.ContentModelFailedError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert len(transport.calls) == 2  # 至多一次纠错（14-B同构）
    assert [item["attempt"] for item in excinfo.value.errors] == [1, 2]
    assert all(item["kind"] == "transport" for item in excinfo.value.errors)
    # 第2次消息＝原messages＋user纠错消息（脱敏错误文本＋固定指令）
    retry_messages = transport.calls[1]["messages"]
    assert len(retry_messages) == 3
    assert retry_messages[0] == transport.calls[0]["messages"][0]
    assert retry_messages[1] == transport.calls[0]["messages"][1]
    assert retry_messages[2]["role"] == "user"
    assert "net-1" in retry_messages[2]["content"]
    assert dc._TRANSPORT_FEEDBACK_INSTRUCTION in retry_messages[2]["content"]
    audit = _only_failure(root)
    assert audit["stage"] == "model_calls"
    assert audit["error_code"] == dc.ERROR_CONTENT_MODEL_FAILED
    assert audit["business_call_count"] == 1
    assert audit["supplier_request_count"] == 2
    assert audit["current_unchanged"] is True
    # 修复裁决D1：取证文件双条目，传输异常raw_response=null
    evidence = _only_failure_evidence(root)
    assert evidence["schema_name"] == dc.FAILED_DOC_SCHEMA_NAME
    assert evidence["failure_id"] == audit["failure_id"]
    assert [(item["attempt"], item["kind"]) for item in evidence["attempts"]] == [
        (1, "transport"),
        (2, "transport"),
    ]
    assert all(item["raw_response"] is None for item in evidence["attempts"])
    _assert_not_published(root)


def test_m_validation_retry_success_transparent(tmp_path: Path) -> None:
    """修复裁决D2/D3：首槽校验失败→纠错重试成功→发布；报告/raw如实记录纠错轮。"""
    root = _chain_full(tmp_path)
    bad_single = {"paragraphs": [{"source_unit_ids": ["c99-u001"], "text": "坏"}]}
    transport = ScriptedTransport(
        [copy.deepcopy(bad_single), copy.deepcopy(SINGLE_OK),
         copy.deepcopy(BATCH_OK), copy.deepcopy(GLOBAL_OK)]
    )
    result = _compose(root, transport, tmp_path=tmp_path)
    assert len(transport.calls) == 4
    assert result.business_call_count == 3
    assert result.supplier_request_count == 4
    assert result.cache_hit_count == 0
    # 纠错消息＝原messages＋assistant回声＋user错误清单指令（14-B同构）
    retry_messages = transport.calls[1]["messages"]
    assert len(retry_messages) == 4
    assert retry_messages[2]["role"] == "assistant"
    assert json.loads(retry_messages[2]["content"]) == bad_single
    assert retry_messages[3]["role"] == "user"
    assert "UNIT_ID_NOT_FOUND" in retry_messages[3]["content"]
    assert dc._CONTENT_VALIDATION_FEEDBACK in retry_messages[3]["content"]
    # raw-responses：single槽attempt_count=2，其余1（修复裁决D3.1）
    raw_doc = _read_json(result.group_path / dc.RAW_RESPONSES_FILE)
    assert [item["attempt_count"] for item in raw_doc["calls"]] == [2, 1, 1]
    # 报告透明性：恰1条attempt=1的validation错误记录（修复裁决D3.2）
    report = _read_json(result.group_path / dc.REPORT_FILE)
    assert len(report["errors"]) == 1
    error = report["errors"][0]
    assert set(error) == dc._REPORT_ERROR_KEYS
    assert error["attempt"] == 1 and error["kind"] == "validation"
    assert error["issues"][0]["code"] == "UNIT_ID_NOT_FOUND"
    # llm-cache只存成功响应：single条目raw为SINGLE_OK而非bad_single
    cache_dir = _own_root(root) / dc.LLM_CACHE_DIRECTORY
    entries = [_read_json(path) for path in cache_dir.glob("*.json")]
    assert len(entries) == 3
    single_entry = next(item for item in entries if item["call_slot"] == dc.CALL_SINGLE)
    assert single_entry["raw_response"] == SINGLE_OK
    assert single_entry["supplier_request_count"] == 1
    # 无失败组（成功轮不产生审计）
    assert not _failures_dir(root).exists()


def test_m_validation_failure_twice_not_cached(tmp_path: Path) -> None:
    """修复裁决D1/D2：同一槽两次校验失败→硬失败；失败响应一律不写llm-cache。"""
    root = _chain_full(tmp_path)
    bad_1 = {"paragraphs": [{"source_unit_ids": ["c99-u001"], "text": "坏"}]}
    bad_2 = {"title": "多返回了标题", "paragraphs": [
        {"source_unit_ids": ["c0001-u001"], "text": "键集不符。"}]}
    transport = ScriptedTransport([copy.deepcopy(bad_1), copy.deepcopy(bad_2)])
    with pytest.raises(dc.ContentModelFailedError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert len(transport.calls) == 2  # 首槽两次失败短路batch/global
    assert [item["kind"] for item in excinfo.value.errors] == ["validation", "validation"]
    assert excinfo.value.errors[0]["issues"][0]["code"] == "UNIT_ID_NOT_FOUND"
    assert excinfo.value.errors[1]["issues"][0]["code"] == "RESPONSE_KEYS_INVALID"
    cache_dir = _own_root(root) / dc.LLM_CACHE_DIRECTORY
    assert not cache_dir.exists()
    audit = _only_failure(root)
    assert audit["supplier_request_count"] == 2
    evidence = _only_failure_evidence(root)
    assert [(item["call_slot"], item["attempt"]) for item in evidence["attempts"]] == [
        (dc.CALL_SINGLE, 1),
        (dc.CALL_SINGLE, 2),
    ]
    # 取证保存transport返回原样值（修复裁决D1：不截断不改写）
    assert evidence["attempts"][0]["raw_response"] == bad_1
    assert evidence["attempts"][1]["raw_response"] == bad_2
    _assert_not_published(root)


def test_m_batch_failure_does_not_degrade_to_emergency(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    bad_batch = {"chapters": [{"chapter_id": "c9999", "paragraphs": []}]}
    transport = ScriptedTransport([copy.deepcopy(SINGLE_OK), bad_batch, copy.deepcopy(GLOBAL_OK)])
    with pytest.raises(dc.ContentModelFailedError):
        _compose(root, transport, tmp_path=tmp_path)
    _assert_not_published(root)


def test_m_global_failure_fails_whole_composition(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    bad_global = {"title": "题", "overview": [], "conclusions": ["结"]}
    transport = ScriptedTransport([copy.deepcopy(SINGLE_OK), copy.deepcopy(BATCH_OK), bad_global])
    with pytest.raises(dc.ContentModelFailedError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert any("OVERVIEW_COUNT_INVALID" in str(item["issues"]) for item in excinfo.value.errors)
    _assert_not_published(root)


def test_m_failure_evidence_redacts_secret(tmp_path: Path) -> None:
    """修复裁决D1：取证message走脱敏口径，api_key涂抹为[REDACTED]。"""
    root = _chain_full(tmp_path)
    transport = ScriptedTransport(
        [RuntimeError(f"上游泄露 {SECRET} 结束"), RuntimeError(f"再次泄露 {SECRET}")]
    )
    with pytest.raises(dc.ContentModelFailedError):
        _compose(root, transport, tmp_path=tmp_path)
    entries = list(_failures_dir(root).iterdir())
    raw_text = (entries[0] / dc.MODEL_RESPONSES_FILE).read_text(encoding="utf-8")
    evidence = json.loads(raw_text)
    assert SECRET not in raw_text
    assert all("[REDACTED]" in item["message"] for item in evidence["attempts"])
    failure_text = (entries[0] / "failure.json").read_text(encoding="utf-8")
    assert SECRET not in failure_text


def test_m_budget_guard_supplier_exceeded(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    inputs = _content_inputs(root)
    role, _ = dc._resolve_content_role(REPO_ROOT, TEMPLATE_PATH)
    transport = ScriptedTransport(_responses())
    counters = {"business": 3, "supplier": dc.FROZEN_SUPPLIER_BUDGET, "cache_hits": 0}
    ledger = inputs.ledgers[0]
    with pytest.raises(dc.ContentBudgetExceededError) as excinfo:
        dc._run_content_slot(
            slot=dc.CALL_SINGLE,
            payload=dc.build_single_payload(ledger),
            inputs=inputs,
            role=role,
            env=ENV,
            transport=transport,
            mock_mode=True,
            backend="mock",
            m2_root=_m2_root(root),
            counters=counters,
            errors=[],
            calls=[],
            failed_attempts=[],
            normalizations=[],
        )
    assert excinfo.value.error_code == dc.ERROR_CONTENT_BUDGET_EXCEEDED
    assert transport.calls == []
    assert dc.FROZEN_SUPPLIER_BUDGET == 6  # 修复裁决D2.1：3槽×2次


def test_m_budget_guard_business_exceeded(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    inputs = _content_inputs(root)
    role, _ = dc._resolve_content_role(REPO_ROOT, TEMPLATE_PATH)
    transport = ScriptedTransport(_responses())
    counters = {"business": dc.FROZEN_BUSINESS_BUDGET + 1, "supplier": 0, "cache_hits": 0}
    ledger = inputs.ledgers[0]
    with pytest.raises(dc.ContentBudgetExceededError):
        dc._run_content_slot(
            slot=dc.CALL_SINGLE,
            payload=dc.build_single_payload(ledger),
            inputs=inputs,
            role=role,
            env=ENV,
            transport=transport,
            mock_mode=True,
            backend="mock",
            m2_root=_m2_root(root),
            counters=counters,
            errors=[],
            calls=[],
            failed_attempts=[],
            normalizations=[],
        )
    assert transport.calls == []


def test_m_api_key_missing_role_invalid(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    transport = ScriptedTransport(_responses())
    with pytest.raises(dc.RoleConfigInvalidError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path, environ={})
    assert excinfo.value.error_code == dc.ERROR_ROLE_CONFIG_INVALID
    assert transport.calls == []
    audit = _only_failure(root)
    assert audit["stage"] == "model_calls"
    assert not (_own_root(root) / dc.LLM_CACHE_DIRECTORY).exists()
    _assert_not_published(root)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda role: role.__setitem__("model", "qwen9-max"), "model"),
        (lambda role: role.__setitem__("validation_retry_policy", "no-retry-1.0"), "validation_retry_policy"),
        (lambda role: role.__setitem__("cache_namespace", "other-ns"), "cache_namespace"),
        (
            lambda role: role.__setitem__("max_attempts_per_business_call", 1),
            "max_attempts_per_business_call",
        ),
        (
            lambda role: role.__setitem__("role_config_version", "content-model-role-1.0"),
            "role_config_version",
        ),
        (
            lambda role: role.__setitem__(
                "runtime_budget",
                {"business_call_hard_limit": 3, "supplier_request_hard_limit": 3},
            ),
            "runtime_budget",
        ),
        (
            lambda role: role["prompt_bundle"].__setitem__("content_sha256", "0" * 64),
            "prompt_bundle.content_sha256",
        ),
        (lambda role: role.__setitem__("input_contract", "X/1.0"), "input_contract"),
        (lambda role: role.__setitem__("response_format", {"type": "text"}), "response_format"),
        (
            lambda role: role["generation"].__setitem__("temperature", 0.7),
            "generation",
        ),
    ],
)
def test_m_role_config_invalid_matrix(tmp_path: Path, mutate: Any, needle: str) -> None:
    root = _chain_full(tmp_path)
    registry = _content_registry(tmp_path, mutate=mutate)
    transport = ScriptedTransport(_responses())
    with pytest.raises(dc.RoleConfigInvalidError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path, registry=registry)
    assert excinfo.value.error_code == dc.ERROR_ROLE_CONFIG_INVALID
    assert needle in str(excinfo.value)
    assert transport.calls == []
    audit = _only_failure(root)
    assert audit["stage"] == "role_config"
    assert not _own_root(root).exists()


def test_m_role_config_missing_and_disabled(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    missing = _content_registry(tmp_path, drop_role=True, name="missing.json")
    with pytest.raises(dc.RoleConfigMissingError) as excinfo:
        _compose(root, ScriptedTransport([]), tmp_path=tmp_path, registry=missing)
    assert excinfo.value.error_code == dc.ERROR_ROLE_CONFIG_MISSING
    def _disable(role: dict) -> None:
        # model_roles禁用角色规则（P3 §2.2不适用）：十个身份字段全部置null
        role["enabled"] = False
        for field in (
            "connection_profile",
            "resolved_provider",
            "model",
            "model_revision",
            "generation",
            "input_contract",
            "output_contract",
            "prompt_bundle",
            "response_format",
            "validation_id",
        ):
            role[field] = None

    disabled = _content_registry(tmp_path, mutate=_disable, name="disabled.json")
    with pytest.raises(dc.RoleConfigMissingError):
        _compose(root, ScriptedTransport([]), tmp_path=tmp_path, registry=disabled)
    assert not _own_root(root).exists()


def test_m_fixture_only_registry_rejected(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    registry = _content_registry(tmp_path, fixture_only=True, name="fx.json")
    with pytest.raises(dc.RoleConfigInvalidError):
        _compose(root, ScriptedTransport(_responses()), tmp_path=tmp_path, registry=registry)


def test_m_llm_cache_replay_rebuilds_identical_group(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    first = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    content_bytes = (first.group_path / dc.CONTENT_FILE).read_bytes()
    result_id = first.result_id
    shutil.rmtree(first.group_path)
    _current_file(root, dc.ROOT_DIRECTORY).unlink()
    second = _compose(root, _ContentTransport([]), tmp_path=tmp_path)
    assert second.result_id == result_id
    assert second.cache_hit is False  # 组已删，重建发布
    assert second.business_call_count == 3
    assert second.supplier_request_count == 0
    assert second.cache_hit_count == 3
    assert (second.group_path / dc.CONTENT_FILE).read_bytes() == content_bytes
    raw_doc = _read_json(second.group_path / dc.RAW_RESPONSES_FILE)
    assert all(item["cache_hit"] is True for item in raw_doc["calls"])


@pytest.mark.parametrize("shape", ["garbage", "extra-key", "messages-mismatch", "raw-unparsable"])
def test_m_llm_cache_corrupt_matrix(tmp_path: Path, shape: str) -> None:
    root = _chain_full(tmp_path)
    first = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    shutil.rmtree(first.group_path)
    (root / TASK / "m2" / dc.ROOT_DIRECTORY / dc.CURRENT_FILE).unlink()
    cache_dir = _own_root(root) / dc.LLM_CACHE_DIRECTORY
    target = None
    for path in cache_dir.glob("*.json"):
        entry = _read_json(path)
        if entry["call_slot"] == dc.CALL_SINGLE:
            target = path
            break
    assert target is not None
    if shape == "garbage":
        target.write_bytes(b"{oops\n")
    elif shape == "extra-key":
        entry = _read_json(target)
        entry["surprise"] = 1
        _write_json(target, entry)
    elif shape == "messages-mismatch":
        entry = _read_json(target)
        entry["messages_sha256"] = "0" * 64
        _write_json(target, entry)
    else:
        entry = _read_json(target)
        entry["raw_response"] = "{未闭合"
        _write_json(target, entry)
    with pytest.raises(dc.ContentCacheCorruptError) as excinfo:
        _compose(root, _ContentTransport([]), tmp_path=tmp_path)
    assert excinfo.value.error_code == dc.ERROR_CACHE_CORRUPT
    audit = _only_failure(root)
    assert audit["stage"] == "model_calls"
    assert not (root / TASK / "m2" / dc.ROOT_DIRECTORY / dc.GROUP_DIRECTORY / first.result_id).exists()


# ---------------------------------------------------------------------------
# P矩阵：发布六件套、早退缓存、复用与pointer_repaired、注入矩阵、中断与锁（裁决Q6.7/Q6.8）
# ---------------------------------------------------------------------------


def _lock_file(root: Path) -> Path:
    return _m2_root(root) / ".locks" / dc.LOCK_FILE


def test_p_first_publish_group_layout_and_pointer(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    result = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    assert result.group_path.name == result.result_id
    assert result.result_id == result.content["content_id"]
    names = {path.name for path in result.group_path.iterdir()}
    assert names == {
        dc.CONTENT_FILE,
        dc.RAW_RESPONSES_FILE,
        dc.REPORT_FILE,
        dc.MANIFEST_FILE,
    }
    manifest = _read_json(result.group_path / dc.MANIFEST_FILE)
    assert set(manifest["files"]) == {dc.CONTENT_FILE, dc.RAW_RESPONSES_FILE, dc.REPORT_FILE}
    pointer = _read_json(_current_file(root, dc.ROOT_DIRECTORY))
    assert set(pointer) == dc._CURRENT_KEYS
    assert pointer["result_id"] == result.result_id
    assert pointer["group"] == f"{dc.GROUP_DIRECTORY}/{result.result_id}"
    manifest_bytes = (result.group_path / dc.MANIFEST_FILE).read_bytes()
    assert pointer["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert list((_own_root(root) / dc.GROUP_DIRECTORY).iterdir()) == [result.group_path]
    assert not _lock_file(root).exists()
    assert not _failures_dir(root).exists()
    residue = [path.name for path in _own_root(root).rglob("*") if ".tmp-" in path.name]
    assert residue == []


def test_p_early_hit_whole_tree_zero_writes(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    first = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    snapshot = _tree_sha_pairs(root)
    transport = _ContentTransport([])
    second = _compose(root, transport, tmp_path=tmp_path)
    assert transport.calls == []
    assert second.cache_hit is True
    assert second.pointer_repaired is False
    assert second.result_id == first.result_id
    assert _tree_sha_pairs(root) == snapshot
    assert not _failures_dir(root).exists()


def test_p_group_reuse_pointer_repaired(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    first = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    _current_file(root, dc.ROOT_DIRECTORY).unlink()
    transport = _ContentTransport([])
    second = _compose(root, transport, tmp_path=tmp_path)
    assert transport.calls == []
    assert second.result_id == first.result_id
    assert second.cache_hit is True
    assert second.pointer_repaired is True
    assert second.content == first.content
    assert list((_own_root(root) / dc.GROUP_DIRECTORY).iterdir()) == [second.group_path]


@pytest.mark.parametrize(
    "shape",
    ["garbage", "extra-key", "group-mismatch", "unsafe-id", "dangling", "manifest-tamper"],
)
def test_p_content_current_corrupt_matrix(tmp_path: Path, shape: str) -> None:
    root = _chain_full(tmp_path)
    first = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    current = _current_file(root, dc.ROOT_DIRECTORY)
    current_bytes = current.read_bytes()
    if shape == "garbage":
        current.write_bytes(b"{broken\n")
    else:
        pointer = _read_json(current)
        if shape == "extra-key":
            pointer["surprise"] = 1
        elif shape == "group-mismatch":
            pointer["group"] = f"{dc.GROUP_DIRECTORY}/other"
        elif shape == "unsafe-id":
            pointer["result_id"] = "../evil"
            pointer["group"] = "formal-results/../evil"
        elif shape == "dangling":
            pointer["result_id"] = "content-000000000000"
            pointer["group"] = f"{dc.GROUP_DIRECTORY}/content-000000000000"
        else:
            (first.group_path / dc.MANIFEST_FILE).write_bytes(b'{"tampered": true}\n')
        if shape != "manifest-tamper":
            _write_json(current, pointer)
    tampered_bytes = current.read_bytes()
    transport = _ContentTransport([])
    with pytest.raises(dc.CurrentPointerCorruptError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert excinfo.value.error_code == dc.ERROR_CURRENT_POINTER_CORRUPT
    assert transport.calls == []
    audit = _only_failure(root)
    assert audit["stage"] == "current_validation"
    assert audit["temporary_cleanup_status"] == "not_created"
    assert audit["current_unchanged"] is True
    assert current.read_bytes() == tampered_bytes
    assert (first.group_path / dc.CONTENT_FILE).is_file()


def test_p_cache_identity_change_new_group_old_immutable(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    first = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    old_group = _own_root(root) / dc.GROUP_DIRECTORY / first.result_id
    old_snapshot = _tree_sha_pairs(old_group)
    # plan标题来自模型响应、不进14-B缓存身份；改以ocr="off"对照组重建整条上游身份链
    build_timeline_v2(TASK, output_root=root, ocr="off")
    _plan(root, ScriptedTransport([copy.deepcopy(GOOD_RESPONSE)]), tmp_path=tmp_path)
    cf.select_chapter_frames(TASK, output_root=root)
    second = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    assert second.result_id != first.result_id
    assert second.cache_hit is False
    groups = sorted(path.name for path in (_own_root(root) / dc.GROUP_DIRECTORY).iterdir())
    assert groups == sorted([first.result_id, second.result_id])
    assert _tree_sha_pairs(old_group) == old_snapshot
    pointer = _read_json(_current_file(root, dc.ROOT_DIRECTORY))
    assert pointer["result_id"] == second.result_id


@pytest.mark.parametrize(
    ("point", "stage", "cleanup", "llm_entries", "calls"),
    [
        ("after_inputs", "after_inputs", "not_created", None, 0),
        ("before_model_call", "model_calls", "not_created", 0, 0),
        ("after_model_call", "model_calls", "not_created", 3, 3),
        ("before_group_publish", "group_publish", "temp_removed", 3, 3),
        ("after_group_publish", "group_publish", "group_removed", 3, 3),
        ("before_current", "current_publish", "group_removed", 3, 3),
    ],
)
def test_p_injection_matrix(
    tmp_path: Path, point: str, stage: str, cleanup: str, llm_entries: int | None, calls: int
) -> None:
    root = _chain_full(tmp_path)
    transport = _ContentTransport(_responses())
    with pytest.raises(dc.InjectedContentFailure) as excinfo:
        _compose(root, transport, tmp_path=tmp_path, failure_injection=point)
    assert excinfo.value.error_code == dc.ERROR_PUBLISH_FAILED
    assert len(transport.calls) == calls
    audit = _only_failure(root)
    assert audit["stage"] == stage
    assert audit["temporary_cleanup_status"] == cleanup
    assert audit["error_code"] == dc.ERROR_PUBLISH_FAILED
    assert audit["business_call_count"] == calls
    assert audit["supplier_request_count"] == calls
    assert audit["current_unchanged"] is True
    _assert_not_published(root)
    if llm_entries is None:
        assert not _own_root(root).exists()
    else:
        cache_dir = _own_root(root) / dc.LLM_CACHE_DIRECTORY
        assert len(list(cache_dir.glob("*.json"))) == llm_entries


def test_p_keyboard_interrupt_discards_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _chain_full(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(dc, "_replace_current", _boom)
    with pytest.raises(KeyboardInterrupt):
        _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    audit = _only_failure(root)
    assert audit["stage"] == "current_publish"
    assert audit["error_code"] == dc.ERROR_PUBLISH_FAILED
    assert "KeyboardInterrupt" in audit["safe_message"]
    assert audit["temporary_cleanup_status"] == "group_removed"
    assert audit["current_unchanged"] is True
    _assert_not_published(root)
    assert not _lock_file(root).exists()
    residue = [path.name for path in _own_root(root).rglob("*") if ".tmp-" in path.name]
    assert residue == []


def test_p_lock_conflict_no_audit_then_success(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    locks = _m2_root(root) / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    _lock_file(root).write_text("{}", encoding="utf-8", newline="")
    transport = _ContentTransport(_responses())
    with pytest.raises(dc.ContentConcurrentRunError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert excinfo.value.error_code == dc.ERROR_CONCURRENT_RUN
    assert transport.calls == []
    assert not _failures_dir(root).exists()
    assert not _own_root(root).exists()
    _lock_file(root).unlink()
    result = _compose(root, transport, tmp_path=tmp_path)
    assert result.cache_hit is False
    assert result.business_call_count == 3


def test_p_unknown_injection_point_plain_error(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    snapshot = _tree_sha_pairs(root)
    with pytest.raises(dc.DocumentComposerV2Error) as excinfo:
        _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path, failure_injection="bogus")
    assert type(excinfo.value) is dc.DocumentComposerV2Error
    assert _tree_sha_pairs(root) == snapshot


# ---------------------------------------------------------------------------
# G矩阵：组完整复验与只读消费入口（裁决Q6.2）
# ---------------------------------------------------------------------------


def test_g_load_published_roundtrip(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    assert dc.load_published_document_content(root, TASK) is None
    result = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    loaded = dc.load_published_document_content(root, TASK)
    assert loaded == result.content
    assert loaded["content_id"] == result.result_id


@pytest.mark.parametrize(
    "shape",
    [
        "content-bytes",
        "raw-deleted",
        "report-tamper",
        "report-errors-tamper",
        "report-supplier-tamper",
        "raw-attempt-tamper",
    ],
)
def test_g_group_tamper_cache_corrupt(tmp_path: Path, shape: str) -> None:
    root = _chain_full(tmp_path)
    first = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    if shape == "content-bytes":
        (first.group_path / dc.CONTENT_FILE).write_bytes(b'{"tampered": 1}\n')
    elif shape == "raw-deleted":
        (first.group_path / dc.RAW_RESPONSES_FILE).unlink()
    elif shape == "report-errors-tamper":
        # 修复裁决D3.2：errors条数必须等于supplier−live，注入伪记录→CACHE_CORRUPT
        report = _read_json(first.group_path / dc.REPORT_FILE)
        report["errors"] = [
            {"attempt": 1, "kind": "validation", "message": "伪造。", "issues": []}
        ]
        _write_report_bytes(first.group_path, report)
    elif shape == "report-supplier-tamper":
        # 供应商计数越出[live, 2×live]→CACHE_CORRUPT
        report = _read_json(first.group_path / dc.REPORT_FILE)
        report["supplier_request_count"] = 7
        _write_report_bytes(first.group_path, report)
    elif shape == "raw-attempt-tamper":
        # 修复裁决D3.1：attempt_count越界→CACHE_CORRUPT
        raw_doc = _read_json(first.group_path / dc.RAW_RESPONSES_FILE)
        raw_doc["calls"][0]["attempt_count"] = 3
        (first.group_path / dc.RAW_RESPONSES_FILE).write_bytes(
            canonical_json_bytes(raw_doc) + b"\n"
        )
    else:
        (first.group_path / dc.REPORT_FILE).write_bytes(b"garbage\n")
    transport = _ContentTransport([])
    with pytest.raises(dc.ContentCacheCorruptError) as excinfo:
        _compose(root, transport, tmp_path=tmp_path)
    assert excinfo.value.error_code == dc.ERROR_CACHE_CORRUPT
    assert transport.calls == []
    audit = _only_failure(root)
    assert audit["stage"] == "current_validation"
    assert audit["current_unchanged"] is True
    assert first.group_path.is_dir()
    if shape == "content-bytes":
        with pytest.raises(dc.ContentCacheCorruptError):
            dc.load_published_document_content(root, TASK)


# ---------------------------------------------------------------------------
# D矩阵：零API逐调用外发预览（裁决Q2.9；实现决定23）
# ---------------------------------------------------------------------------


def test_d_dry_run_primary_zero_write(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    snapshot = _tree_sha_pairs(root)
    preview = dc.build_content_composition_dry_run(root, TASK)
    inputs = _content_inputs(root)
    assert preview["schema_name"] == "DocumentComposerDryRun"
    assert preview["schema_version"] == "1.0"
    assert preview["api_request_count"] == 0
    assert preview["zero_write"] is True
    assert [item["call_slot"] for item in preview["calls"]] == list(dc.CALL_SLOTS)
    assert [item["unit_count"] for item in preview["calls"]] == [1, 1, 0]
    assert [item["max_tokens"] for item in preview["calls"]] == [1800, 8192, 1600]
    assert [item["prompt_version"] for item in preview["calls"]] == [
        dc.PROMPT_VERSIONS[slot] for slot in dc.CALL_SLOTS
    ]
    assert preview["calls"][0]["payload"] == dc.build_single_payload(inputs.ledgers[0])
    assert preview["calls"][1]["payload"] == dc.build_batch_payload(inputs.ledgers[1:])
    assert preview["calls"][2]["payload"]["chapters"][0]["paragraphs"] == []
    assert preview["budget"]["expected_business_calls"] == 3
    assert preview["budget"]["business_call_hard_limit"] == 3
    # 修复裁决D2/D5：供应商硬上限6、一次纠错、validation-feedback-1.0
    assert preview["budget"]["supplier_request_hard_limit"] == 6
    assert preview["budget"]["max_attempts_per_business_call"] == 2
    assert preview["budget"]["validation_retry_policy"] == "validation-feedback-1.0"
    assert preview["model"] == dc.FROZEN_MODEL
    assert preview["backend"] == "openai_compatible"
    assert "章标题" in preview["send_fields"]
    assert "帧文件" in preview["not_sent_fields"]
    assert _tree_sha_pairs(root) == snapshot
    assert not _own_root(root).exists()


def test_d_dry_run_emergency_two_calls(tmp_path: Path) -> None:
    root = _chain_full_emerg(tmp_path)
    preview = dc.build_content_composition_dry_run(root, TASK)
    assert [item["call_slot"] for item in preview["calls"]] == [dc.CALL_SINGLE, dc.CALL_GLOBAL]
    assert [item["unit_count"] for item in preview["calls"]] == [1, 0]
    assert preview["budget"]["expected_business_calls"] == 2


def test_d_dry_run_missing_upstream_zero_write(tmp_path: Path) -> None:
    root = _chain_full(tmp_path)
    _current_file(root, "chapter-frames").unlink()
    snapshot = _tree_sha_pairs(root)
    with pytest.raises(dc.InputChapterFrameSelectionMissingError):
        dc.build_content_composition_dry_run(root, TASK)
    assert _tree_sha_pairs(root) == snapshot
    assert not _own_root(root).exists()


def test_d_dry_run_slot_message_bytes_measure(tmp_path: Path) -> None:
    """阶段15-0裁决§一1.4第2项：逐槽增补``message_bytes``＝同文本UTF-8字节数。

    度量口径为§一1.3：全messages各条content文本拼接（system＋user，
    ``build_slot_messages``产物），禁止以payload字符数替代；纯增量键，其余
    既有键与取值逐字不动（预览dict属工具面，不入已裁Schema）。
    """

    root = _chain_full(tmp_path)
    snapshot = _tree_sha_pairs(root)
    preview = dc.build_content_composition_dry_run(root, TASK)
    inputs = _content_inputs(root)
    payloads = {
        dc.CALL_SINGLE: dc.build_single_payload(inputs.ledgers[0]),
        dc.CALL_BATCH: dc.build_batch_payload(inputs.ledgers[1:]),
        dc.CALL_GLOBAL: {
            "chapters": [
                {"chapter_id": item.chapter_id, "title": item.title, "paragraphs": []}
                for item in inputs.ledgers
            ]
        },
    }
    for item in preview["calls"]:
        messages = dc.build_slot_messages(item["call_slot"], payloads[item["call_slot"]])
        expected_bytes = sum(
            len(str(message["content"]).encode("utf-8")) for message in messages
        )
        expected_chars = sum(len(str(message["content"])) for message in messages)
        assert item["message_bytes"] == expected_bytes
        assert item["message_chars"] == expected_chars
        assert item["message_bytes"] >= item["message_chars"]
    assert set(preview["calls"][0]) == {
        "call_slot",
        "prompt_version",
        "validation_id",
        "payload",
        "message_chars",
        "message_bytes",
        "payload_chars",
        "unit_count",
        "max_tokens",
    }
    assert _tree_sha_pairs(root) == snapshot


# ---------------------------------------------------------------------------
# C矩阵：CLI compose-document-v2接线（成功stdout 14键、失败stderr脱敏、用法2、中断传播）
# ---------------------------------------------------------------------------

_CLI_OK_KEYS = {
    "task_id", "module", "status", "result_id", "content_id", "chapter_count",
    "plan_result_source", "plan_quality_level", "business_call_count",
    "supplier_request_count", "cache_hit_count", "cache_hit",
    "pointer_repaired", "group_relative_path",
}
_CLI_FAIL_KEYS = {
    "task_id", "module", "status", "error_code", "safe_message",
    "recoverable", "next_action",
}


def _cli_status(payload_text: str) -> dict[str, Any]:
    return json.loads(payload_text)


def test_c_success_stdout_and_cache_replay(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _chain_full(tmp_path)
    monkeypatch.setattr(
        dc, "OpenAICompatibleTransport", lambda *a, **k: _ContentTransport(_responses())
    )
    monkeypatch.setenv("VIDEO_DOC_LLM_API_KEY", SECRET)
    argv = ["compose-document-v2", TASK, "--output-root", str(root)]
    assert main(argv) == 0
    ok = _cli_status(capsys.readouterr().out)
    assert set(ok) == _CLI_OK_KEYS
    assert ok["module"] == dc.MODULE_TOKEN and ok["status"] == "success"
    assert ok["chapter_count"] == 2
    assert ok["plan_result_source"] == "primary" and ok["plan_quality_level"] == "normal"
    assert ok["business_call_count"] == 3 and ok["supplier_request_count"] == 3
    assert ok["cache_hit"] is False and ok["pointer_repaired"] is False
    assert ok["group_relative_path"] == (
        f"m2/{dc.ROOT_DIRECTORY}/{dc.GROUP_DIRECTORY}/{ok['result_id']}"
    )
    assert main(argv) == 0
    replay = _cli_status(capsys.readouterr().out)
    assert replay["cache_hit"] is True and replay["result_id"] == ok["result_id"]


def test_c_missing_selection_failure_json(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _chain_full(tmp_path)
    _current_file(root, "chapter-frames").unlink()
    monkeypatch.setenv("VIDEO_DOC_LLM_API_KEY", SECRET)
    assert main(["compose-document-v2", TASK, "--output-root", str(root)]) == 1
    err = _cli_status(capsys.readouterr().err)
    assert set(err) == _CLI_FAIL_KEYS
    assert err["module"] == dc.MODULE_TOKEN and err["status"] == "failed"
    assert err["error_code"] == dc.ERROR_INPUT_CHAPTER_FRAME_SELECTION_MISSING
    assert str(root) not in err["safe_message"]


def test_c_usage_error_exit_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["compose-document-v2"])
    assert excinfo.value.code == 2


def test_c_keyboard_interrupt_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _chain_full(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("video_doc.cli.compose_document_v2", _boom)
    with pytest.raises(KeyboardInterrupt):
        main(["compose-document-v2", TASK, "--output-root", str(root)])


# ---------------------------------------------------------------------------
# N矩阵：single信封规范化＋batch 8192＋composer版本感知复验
# （二次失败形态裁决N1/N2/N3；契约docs/development/阶段15-content二次失败形态裁决.md）
# ---------------------------------------------------------------------------

# 形态A取证同构：single槽收到batch信封（章id与single账本c0001一致）
SINGLE_ENVELOPE = {
    "chapters": [
        {
            "chapter_id": "c0001",
            "paragraphs": [
                {"source_unit_ids": ["c0001-u001"], "text": "开场讲解第一段。"}
            ],
        }
    ]
}


def _envelope_responses() -> list[Any]:
    return [copy.deepcopy(SINGLE_ENVELOPE), copy.deepcopy(BATCH_OK), copy.deepcopy(GLOBAL_OK)]


def test_n_max_tokens_history_and_reverse_lookup() -> None:
    # N2/N3：HISTORY映射、当前常量取1.1、batch=8192、反查单射
    assert dc.COMPOSER_VERSION == "1.1"
    assert dc.PER_CALL_MAX_TOKENS == dc.PER_CALL_MAX_TOKENS_HISTORY["1.1"]
    assert dc.PER_CALL_MAX_TOKENS[dc.CALL_BATCH] == 8192
    assert dc.PER_CALL_MAX_TOKENS[dc.CALL_SINGLE] == 1800
    assert dc.PER_CALL_MAX_TOKENS[dc.CALL_GLOBAL] == 1600
    assert dc.PER_CALL_MAX_TOKENS_HISTORY["1.0"][dc.CALL_BATCH] == 3600
    assert dc.composer_version_for_tokens(
        {dc.CALL_SINGLE: 1800, dc.CALL_BATCH: 3600, dc.CALL_GLOBAL: 1600}
    ) == "1.0"
    assert dc.composer_version_for_tokens(dict(dc.PER_CALL_MAX_TOKENS)) == "1.1"
    assert dc.composer_version_for_tokens(
        {dc.CALL_SINGLE: 1800, dc.CALL_BATCH: 9999, dc.CALL_GLOBAL: 1600}
    ) is None
    assert dc.composer_version_for_tokens("nope") is None
    assert dc.composer_version_for_tokens(None) is None


def test_n_report_keys_by_version() -> None:
    # N3.4：1.0=12键且无response_normalizations；1.1=13键且含之
    assert "response_normalizations" not in dc._REPORT_KEYS_1_0
    assert "response_normalizations" in dc._REPORT_KEYS_1_1
    assert len(dc._REPORT_KEYS_1_1) == len(dc._REPORT_KEYS_1_0) + 1
    assert dc._REPORT_KEYS_BY_COMPOSER["1.0"] is dc._REPORT_KEYS_1_0
    assert dc._REPORT_KEYS_BY_COMPOSER["1.1"] is dc._REPORT_KEYS_1_1


def test_n_normalize_single_envelope_matches_paragraphs(tmp_path: Path) -> None:
    # N1纯函数：chapters信封规范化后与直给paragraphs逐字节等价
    root = _chain_full(tmp_path)
    ledger = _content_inputs(root).ledgers[0]
    assert ledger.chapter_id == "c0001"
    norm_env, issues_env = dc.normalize_single_response(copy.deepcopy(SINGLE_ENVELOPE), ledger)
    norm_ok, issues_ok = dc.normalize_single_response(copy.deepcopy(SINGLE_OK), ledger)
    assert issues_env == [] and issues_ok == []
    assert norm_env == norm_ok
    rec = dc.single_envelope_normalization(copy.deepcopy(SINGLE_ENVELOPE), ledger, 1)
    assert rec == {
        "call_slot": "single",
        "envelope": "chapters-single",
        "chapter_id": "c0001",
        "attempt": 1,
    }
    assert dc.single_envelope_normalization(copy.deepcopy(SINGLE_OK), ledger, 1) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"chapters": [
            {"chapter_id": "c0001", "paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "甲。"}]},
            {"chapter_id": "c0002", "paragraphs": [{"source_unit_ids": ["c0002-u001"], "text": "乙。"}]},
        ]},
        {"chapters": [
            {"chapter_id": "c9999", "paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "甲。"}]},
        ]},
        {"chapters": [
            {"chapter_id": "c0001", "paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "甲。"}]},
        ], "extra": 1},
        {"chapters": [
            {"chapter_id": "c0001", "title": "x", "paragraphs": [{"source_unit_ids": ["c0001-u001"], "text": "甲。"}]},
        ]},
    ],
    ids=["multi_chapter", "chapter_id_mismatch", "envelope_extra_key", "chapter_extra_key"],
)
def test_n_normalize_single_envelope_rejects(tmp_path: Path, bad: dict[str, Any]) -> None:
    # N1窄口径：四类非精确形态一律按现状RESPONSE_KEYS_INVALID拒绝，不强制转换
    root = _chain_full(tmp_path)
    ledger = _content_inputs(root).ledgers[0]
    coerced, record = dc._coerce_single_envelope(copy.deepcopy(bad), ledger)
    assert record is None and coerced == bad
    norm, issues = dc.normalize_single_response(copy.deepcopy(bad), ledger)
    assert norm is None
    assert [item["code"] for item in issues] == ["RESPONSE_KEYS_INVALID"]


def test_n_compose_envelope_publishes_identical_content(tmp_path: Path) -> None:
    # N1端到端：chapters信封single发布成功，正文与paragraphs直给逐字节一致，
    # 报告记录response_normalizations，组复验重推导一致（verified not trusted）
    (tmp_path / "env").mkdir()
    (tmp_path / "ok").mkdir()
    root_env = _chain_full(tmp_path / "env")
    root_ok = _chain_full(tmp_path / "ok")
    res_env = _compose(root_env, ScriptedTransport(_envelope_responses()), tmp_path=tmp_path / "env")
    res_ok = _compose(root_ok, ScriptedTransport(_responses()), tmp_path=tmp_path / "ok")
    assert res_env.supplier_request_count == 3 and res_env.business_call_count == 3
    env_bytes = (res_env.group_path / dc.CONTENT_FILE).read_bytes()
    ok_bytes = (res_ok.group_path / dc.CONTENT_FILE).read_bytes()
    assert env_bytes == ok_bytes
    report = _read_json(res_env.group_path / dc.REPORT_FILE)
    assert set(report) == dc._REPORT_KEYS_1_1
    assert report["response_normalizations"] == [
        {"call_slot": "single", "attempt": 1, "envelope": "chapters-single", "chapter_id": "c0001"}
    ]
    assert report["errors"] == []
    inputs_env = _content_inputs(root_env)
    dc.validate_document_content_group(
        res_env.group_path,
        cache_sha256=str(_read_json(res_env.group_path / dc.MANIFEST_FILE)["cache_identity_sha256"]),
        plan=inputs_env.plan,
        timeline_index=inputs_env.timeline_index,
        selection=inputs_env.selection,
        expected_result_id=res_env.result_id,
    )
    report_ok = _read_json(res_ok.group_path / dc.REPORT_FILE)
    assert report_ok["response_normalizations"] == []


def test_n_envelope_normalization_on_attempt2(tmp_path: Path) -> None:
    # N1：attempt-1非法键集失败、attempt-2 chapters信封被规范化成功
    root = _chain_full(tmp_path)
    responses = [
        {"unexpected": 1},
        copy.deepcopy(SINGLE_ENVELOPE),
        copy.deepcopy(BATCH_OK),
        copy.deepcopy(GLOBAL_OK),
    ]
    result = _compose(root, ScriptedTransport(responses), tmp_path=tmp_path)
    assert result.business_call_count == 3
    assert result.supplier_request_count == 4
    report = _read_json(result.group_path / dc.REPORT_FILE)
    assert report["response_normalizations"] == [
        {"call_slot": "single", "attempt": 2, "envelope": "chapters-single", "chapter_id": "c0001"}
    ]
    assert len(report["errors"]) == 1
    assert report["errors"][0]["attempt"] == 1
    assert report["errors"][0]["kind"] == "validation"
    live = report["business_call_count"] - report["cache_hit_count"]
    assert live == 3 and live <= report["supplier_request_count"] <= 2 * live
    assert len(report["errors"]) == report["supplier_request_count"] - live


def test_n_envelope_normalization_on_cache_hit(tmp_path: Path) -> None:
    # N1：发布后删current与组、保留llm-cache，重组走逐调用缓存命中，
    # 仍从缓存raw重记response_normalizations(attempt=1)、0供应商请求、正文逐字节一致
    root = _chain_full(tmp_path)
    first = _compose(root, ScriptedTransport(_envelope_responses()), tmp_path=tmp_path)
    assert first.supplier_request_count == 3
    first_content_bytes = (first.group_path / dc.CONTENT_FILE).read_bytes()
    own = _own_root(root)
    os.remove(own / dc.CURRENT_FILE)
    shutil.rmtree(first.group_path)
    second = _compose(root, ScriptedTransport([]), tmp_path=tmp_path)
    assert second.cache_hit_count == 3 and second.supplier_request_count == 0
    assert (second.group_path / dc.CONTENT_FILE).read_bytes() == first_content_bytes
    report = _read_json(second.group_path / dc.REPORT_FILE)
    assert report["response_normalizations"] == [
        {"call_slot": "single", "attempt": 1, "envelope": "chapters-single", "chapter_id": "c0001"}
    ]


def test_n_report_keyset_enforced_for_11_group(tmp_path: Path) -> None:
    # N3.4：1.1组报告删除response_normalizations键→键集偏离冻结13键→CACHE_CORRUPT
    root = _chain_full(tmp_path)
    result = _compose(root, ScriptedTransport(_responses()), tmp_path=tmp_path)
    group = result.group_path
    report = _read_json(group / dc.REPORT_FILE)
    broken = {k: v for k, v in report.items() if k != "response_normalizations"}
    _write_report_bytes(group, broken)
    inputs = _content_inputs(root)
    with pytest.raises(dc.ContentCacheCorruptError):
        dc.validate_document_content_group(
            group,
            cache_sha256=str(_read_json(group / dc.MANIFEST_FILE)["cache_identity_sha256"]),
            plan=inputs.plan,
            timeline_index=inputs.timeline_index,
            selection=inputs.selection,
            expected_result_id=result.result_id,
        )


def test_n_real_legacy_10_group_revalidates() -> None:
    # N3向后兼容：真实COURSE01/DEV01 composer-1.0旧组（batch 3600）仍复验通过
    outputs = REPO_ROOT / "outputs"
    checked = 0
    for task, expected in (
        ("COURSE01", "document-content-db15c28ea8e6"),
        ("DEV01", "document-content-4aa2d5d37b9f"),
    ):
        current = outputs / task / "m2" / "content" / "current.json"
        if not current.is_file():
            continue
        content = dc.load_published_document_content(outputs, task)
        assert content is not None
        assert content["content_id"] == expected
        assert content["generation"]["per_call_max_tokens"][dc.CALL_BATCH] == 3600
        assert dc.composer_version_for_tokens(content["generation"]["per_call_max_tokens"]) == "1.0"
        checked += 1
    if checked == 0:
        pytest.skip("真实outputs content旧组不可用")


def test_n7_approval_error_propagates_from_slot_without_retry(tmp_path: Path) -> None:
    # N7：审批守卫异常在槽位层原样传播——不记errors、不纠错重试、不算传输失败
    from video_doc.m2_pipeline import M2PipelineError

    root = _chain_full(tmp_path)
    inputs = _content_inputs(root)
    role, _ = dc._resolve_content_role(REPO_ROOT, TEMPLATE_PATH)
    approval = M2PipelineError(
        "LLM_APPROVAL_REQUIRED", failed_stage="document_content_v2"
    )
    transport = ScriptedTransport([approval])
    counters = {"business": 0, "supplier": 0, "cache_hits": 0}
    errors: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    norms: list[dict[str, Any]] = []
    with pytest.raises(M2PipelineError) as exc:
        dc._run_content_slot(
            slot=dc.CALL_SINGLE,
            payload=dc.build_single_payload(inputs.ledgers[0]),
            inputs=inputs,
            role=role,
            env=ENV,
            transport=transport,
            mock_mode=True,
            backend="mock",
            m2_root=_m2_root(root),
            counters=counters,
            errors=errors,
            calls=calls,
            failed_attempts=failed,
            normalizations=norms,
        )
    assert exc.value.error_code == "LLM_APPROVAL_REQUIRED"
    assert errors == [] and failed == [] and calls == [] and norms == []
    assert len(transport.calls) == 1  # 单次尝试、无第二次守卫抛出


def test_n7_compose_approval_stop_writes_no_failure_group(tmp_path: Path) -> None:
    # N7：compose级审批停止→原样传播、零失败组、零发布、current不建
    from video_doc.m2_pipeline import M2PipelineError

    root = _chain_full(tmp_path)
    approval = M2PipelineError(
        "LLM_APPROVAL_REQUIRED", failed_stage="document_content_v2"
    )
    transport = ScriptedTransport([approval])
    with pytest.raises(M2PipelineError) as exc:
        _compose(root, transport, tmp_path=tmp_path)
    assert exc.value.error_code == "LLM_APPROVAL_REQUIRED"
    failures = _failures_dir(root)
    assert not failures.exists() or list(failures.iterdir()) == []
    own = _own_root(root)
    formal = own / "formal-results"
    assert not formal.exists() or list(formal.iterdir()) == []
    assert not (own / dc.CURRENT_FILE).exists()
