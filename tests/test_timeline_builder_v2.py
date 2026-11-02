"""阶段13-1/13-2：TimelineBuilderV2 定向测试。

覆盖P5阶段13-2故障矩阵：同毫秒多模态事件、OCR关闭/为空/失败、输入current
错配、缓存与发布失败；以及13-1核心语义（modalities映射、稳定命名空间、文本
事件ID不随视觉输入重编号、缓存命中零改写、指针修复、并发、中断审计、loader、
CLI接线、M1零改写）。

全部使用合成fixture（tmp_path）：M1时间轴 + 真实F1帧集结构 + 真实F2页面变化
+ 真实OCR（ScriptedEngine），不访问网络、不触碰真实outputs、不运行真实引擎。
fixture层（`_workspace`及其依赖）跨M1/F1/F2/OCR四套冻结契约，由主会话建立并
验证；具体故障用例在其上展开。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.test_ocr_extractor import (
    TASK,
    ScriptedEngine,
    _candidates,
    _factory,
    _line,
    _proxy_loader,
    _publish_frame_set,
)
from tests.test_unified_timeline import make_m1_timeline
from video_doc.ocr_extractor import run_ocr
from video_doc.page_change_detector import detect_page_changes
from video_doc.timeline_builder_v2 import (
    OCR_REQUESTS,
    TIMELINE_GROUP_DIRECTORY,
    TIMELINE_INDEX_FILE,
    TIMELINE_LOCK_FILE,
    TIMELINE_MANIFEST_FILE,
    TIMELINE_MODULE_TOKEN,
    TIMELINE_ROOT_DIRECTORY,
    InjectedTimelineFailure,
    InputFrameSetCorruptError,
    InputFrameSetMissingError,
    InputOcrSetCorruptError,
    InputOcrSetMissingError,
    InputPageChangeSetMissingError,
    RequiredOcrUnavailableError,
    TimelineCacheCorruptError,
    TimelineConcurrentRunError,
    TimelineCurrentPointerCorruptError,
    build_timeline_v2,
    load_published_timeline,
    load_timeline_current,
)

# 8帧@0,5000,...,35000ms；values交替触发页面变化 → 候选为fr000002..fr000008。
DEFAULT_VALUES = (0, 255, 0, 255, 0, 255, 0, 255)
# 候选帧文件名（供OCR脚本命中）。
CANDIDATE_FILES = [f"fr{i:06d}.png" for i in range(2, 9)]


def _write_m1(root: Path, *, task: str, events: Any = None, duration_seconds: float = 60.0) -> dict[str, Any]:
    tl = make_m1_timeline(events=events, duration_seconds=duration_seconds, task_id=task)
    d = root / task / "timeline"
    d.mkdir(parents=True, exist_ok=True)
    (d / "timeline.json").write_text(
        json.dumps(tl, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tl


def _default_ocr_scripts() -> dict[str, list]:
    """为每个候选帧脚本化一行文本，使OCR集合status=success且产生oe事件。"""

    return {
        name: [_line(f"text {name}", 0.99, 50)]
        for name in CANDIDATE_FILES
    }


def _workspace(
    tmp_path: Path,
    *,
    m1_events: Any = None,
    duration_seconds: float = 60.0,
    values: tuple[int, ...] = DEFAULT_VALUES,
    publish_frames: bool = True,
    publish_page_changes: bool = True,
    publish_ocr: bool = True,
    ocr_scripts: dict[str, list] | None = None,
    ocr_engine: ScriptedEngine | None = None,
    ocr_mode: str = "auto",
    task: str = TASK,
) -> Path:
    """组装含M1时间轴与F1/F2/OCR已发布组的合成工作区，返回output根目录。

    各publish_*开关用于构造缺链/未运行场景；ocr_scripts=None时用默认脚本
    （每候选一行文本）。``ocr_engine``显式给定则直接用它跑run_ocr（用于
    degraded/失败帧/自定义binding场景），优先于ocr_scripts。返回的root即
    build_timeline_v2的output_root。
    """

    root = tmp_path / "outputs"
    _write_m1(root, task=task, events=m1_events, duration_seconds=duration_seconds)
    if publish_frames:
        _publish_frame_set(root, values)
        if publish_page_changes:
            detect_page_changes(task, output_root=root, _proxy_loader=_proxy_loader(values))
            if publish_ocr:
                engine = (
                    ocr_engine
                    if ocr_engine is not None
                    else ScriptedEngine(
                        _default_ocr_scripts() if ocr_scripts is None else ocr_scripts
                    )
                )
                run_ocr(task, output_root=root, mode=ocr_mode, _engine_factory=_factory(engine))
    return root


def _failures(root: Path, task: str = TASK) -> list[dict[str, Any]]:
    """读取timelines模块已写的失败审计（按failure_id排序）。"""

    base = root / task / "m2" / "failures" / TIMELINE_MODULE_TOKEN
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir()):
        fp = entry / "failure.json"
        if fp.is_file():
            out.append(json.loads(fp.read_text(encoding="utf-8")))
    return out


def _lock_path(root: Path, task: str = TASK) -> Path:
    return root / task / "m2" / ".locks" / TIMELINE_LOCK_FILE


def _candidate_files(root: Path) -> list[str]:
    """实际页面变化候选对应的帧文件名（供OCR脚本/失败注入命中）。"""

    return [f"{c['frame_id']}.png" for c in _candidates(root)]


def _counts(index: dict[str, Any]) -> dict[str, int]:
    c = {"transcript": 0, "page_change": 0, "ocr": 0}
    for e in index["events"]:
        c[e["type"]] += 1
    return c


def _ocr_index(root: Path, task: str = TASK) -> dict[str, Any] | None:
    """读取已发布OCR集合index（无current时返回None）。"""

    cur = root / task / "m2" / "ocr" / "current.json"
    if not cur.is_file():
        return None
    pointer = json.loads(cur.read_text(encoding="utf-8"))
    group = root / task / "m2" / "ocr" / pointer["group"]
    return json.loads((group / "ocr-set.json").read_text(encoding="utf-8"))


def _expected_oe_frames(root: Path, task: str = TASK) -> list[dict[str, Any]]:
    """OCR集合中应产出oe事件的帧：status=ok、非duplicate、canonical_text非空。"""

    index = _ocr_index(root, task)
    if index is None:
        return []
    return [
        f
        for f in index["frames"]
        if f["status"] == "ok"
        and f["duplicate_of"] is None
        and isinstance(f["canonical_text"], str)
        and f["canonical_text"].strip()
    ]


def _text_events(index: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in index["events"] if e["type"] == "transcript"]


def _statuses(index: dict[str, Any]) -> dict[str, str]:
    return {k: v["status"] for k, v in index["modalities"].items()}


def _timelines_root(root: Path, task: str = TASK) -> Path:
    return root / task / "m2" / TIMELINE_ROOT_DIRECTORY


def _group_dir(root: Path, timeline_id: str, task: str = TASK) -> Path:
    return _timelines_root(root, task) / TIMELINE_GROUP_DIRECTORY / timeline_id


# ---------------------------------------------------------------------------
# 核心正常路径（主会话建立并验证fixture）
# ---------------------------------------------------------------------------


def test_happy_auto_all_available_multimodal(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    res = build_timeline_v2(TASK, output_root=root, ocr="auto")
    idx = res.index
    assert _statuses(idx) == {
        "transcript": "available",
        "frame_change": "available",
        "ocr": "available",
    }
    assert idx["run_status"] == "success"
    assert idx["quality_level"] == "multimodal"
    counts = _counts(idx)
    candidates = _candidates(root)
    expected_oe = _expected_oe_frames(root)
    # pe事件数=页面变化候选数；oe事件数=OCR集合中ok且非重复且有文本的帧数。
    assert counts["page_change"] == len(candidates)
    assert counts["ocr"] == len(expected_oe)
    assert counts["ocr"] >= 1  # fixture确保至少一个oe事件
    assert res.manifest["event_counts"]["total"] == sum(counts.values())
    # 可追溯性：每个pe的candidate_id来自候选集，每个oe的frame_id来自应产出帧集。
    cand_ids = {c["candidate_id"] for c in candidates}
    oe_frame_ids = {f["frame_id"] for f in expected_oe}
    for e in idx["events"]:
        if e["type"] == "page_change":
            assert e["payload"]["candidate_id"] in cand_ids
            assert e["source_segment_id"] == f"pc:{e['payload']['candidate_id']}"
        elif e["type"] == "ocr":
            assert e["payload"]["frame_id"] in oe_frame_ids
            assert e["source_segment_id"] == f"oc:{e['payload']['frame_id']}"
    assert not res.cache_hit and not res.pointer_repaired


def test_off_disables_ocr_and_keeps_text_ids(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    auto = build_timeline_v2(TASK, output_root=root, ocr="auto").index
    off = build_timeline_v2(TASK, output_root=root, ocr="off").index
    assert _statuses(off)["ocr"] == "disabled"
    assert off["modalities"]["ocr"]["reason_codes"] == ["NOT_ENABLED"]
    assert _counts(off)["ocr"] == 0
    # P5阶段闸门：OCR开关不改变文本事件（逐字节相同）。
    assert _text_events(off) == _text_events(auto)
    assert off["timeline_id"] != auto["timeline_id"]


def test_required_matches_auto_content_when_ocr_present(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    auto = build_timeline_v2(TASK, output_root=root, ocr="auto")
    req = build_timeline_v2(TASK, output_root=root, ocr="required")
    # auto与required消费同一OCR集合 → 内容逐字节相同 → 同一timeline_id → 缓存命中。
    assert req.index["timeline_id"] == auto.index["timeline_id"]
    assert req.cache_hit is True


def test_no_ocr_current_auto_is_not_run(tmp_path: Path) -> None:
    root = _workspace(tmp_path, publish_ocr=False)
    idx = build_timeline_v2(TASK, output_root=root, ocr="auto").index
    assert _statuses(idx) == {
        "transcript": "available",
        "frame_change": "available",
        "ocr": "not_run",
    }
    assert idx["quality_level"] == "multimodal"
    assert _counts(idx)["ocr"] == 0


def test_required_without_ocr_current_hard_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path, publish_ocr=False)
    with pytest.raises(InputOcrSetMissingError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="required")
    assert exc.value.error_code == "INPUT_OCR_SET_MISSING"


def test_no_frames_at_all_is_text_only(tmp_path: Path) -> None:
    root = _workspace(tmp_path, publish_frames=False)
    idx = build_timeline_v2(TASK, output_root=root, ocr="auto").index
    assert _statuses(idx) == {
        "transcript": "available",
        "frame_change": "not_run",
        "ocr": "not_run",
    }
    assert idx["run_status"] == "success"
    assert idx["quality_level"] == "text_only"
    assert _counts(idx) == {"transcript": len(_text_events(idx)), "page_change": 0, "ocr": 0}


def test_frames_without_page_changes_is_frame_change_not_run(tmp_path: Path) -> None:
    root = _workspace(tmp_path, publish_page_changes=False)
    idx = build_timeline_v2(TASK, output_root=root, ocr="auto").index
    assert _statuses(idx)["frame_change"] == "not_run"
    assert _statuses(idx)["ocr"] == "not_run"


def test_cache_hit_is_zero_rewrite(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = build_timeline_v2(TASK, output_root=root, ocr="auto")

    def snap() -> dict[str, tuple[int, str]]:
        out = {}
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                p = Path(dp) / fn
                out[p.relative_to(root).as_posix()] = (
                    p.stat().st_size,
                    __import__("hashlib").sha256(p.read_bytes()).hexdigest(),
                )
        return out

    before = snap()
    second = build_timeline_v2(TASK, output_root=root, ocr="auto")
    after = snap()
    assert second.cache_hit is True
    assert second.index["timeline_id"] == first.index["timeline_id"]
    assert before == after  # 零改写


def test_loaders_roundtrip_published_group(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    res = build_timeline_v2(TASK, output_root=root, ocr="auto")
    pointer = load_timeline_current(root, TASK)
    assert pointer is not None and pointer["result_id"] == res.index["timeline_id"]
    loaded = load_published_timeline(root, TASK)
    assert loaded == res.index
    # 无current时loader返回None
    empty_root = _workspace(tmp_path / "e", publish_frames=False)
    assert load_timeline_current(empty_root, TASK) is None
    assert load_published_timeline(empty_root, TASK) is None


def test_publish_only_touches_timelines_dir(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    def snap() -> set[str]:
        return {
            (Path(dp) / fn).relative_to(root).as_posix()
            for dp, _dn, fns in os.walk(root)
            for fn in fns
        }

    before = snap()
    build_timeline_v2(TASK, output_root=root, ocr="auto")
    after = snap()
    added = after - before
    assert added and all(a.startswith(f"{TASK}/m2/{TIMELINE_ROOT_DIRECTORY}/") for a in added)
    assert before - after == set()  # 未删除任何既有文件（M1/F1/F2/OCR零改写）


def test_same_ms_orders_transcript_then_page_change_then_ocr(tmp_path: Path) -> None:
    # M1事件@5.0s=5000ms与fr000002候选(decoded_ms=5000)对齐 → 同毫秒三模态。
    root = _workspace(tmp_path, m1_events=[(5.0, 8.0, "aligned")], duration_seconds=60.0)
    idx = build_timeline_v2(TASK, output_root=root, ocr="auto").index
    at_5000 = [e for e in idx["events"] if e["start_ms"] == 5000]
    types_in_order = [e["type"] for e in at_5000]
    assert types_in_order == ["transcript", "page_change", "ocr"], types_in_order
    # 全局排序：sort key单调不减
    prio = {"transcript": 0, "page_change": 1, "ocr": 2}
    keys = [(e["start_ms"], prio[e["type"]], e["id"]) for e in idx["events"]]
    assert keys == sorted(keys)


def _dumps_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _ocr_current(root: Path, task: str = TASK) -> Path:
    return root / task / "m2" / "ocr" / "current.json"


def _pc_current(root: Path, task: str = TASK) -> Path:
    return root / task / "m2" / "frames" / "page-changes" / "current.json"


def _frames_current(root: Path, task: str = TASK) -> Path:
    return root / task / "m2" / "frames" / "current.json"


def _timeline_current(root: Path, task: str = TASK) -> Path:
    return _timelines_root(root, task) / "current.json"


def _assert_no_timeline_published(root: Path, task: str = TASK) -> None:
    tr = _timelines_root(root, task)
    assert not (tr / "current.json").exists()
    fr = tr / TIMELINE_GROUP_DIRECTORY
    if fr.is_dir():
        leftover = [
            e.name
            for e in fr.iterdir()
            if e.name.startswith("unified-timeline-") or e.name.startswith(".tmp-")
        ]
        assert not leftover, leftover


def _degraded_engine(root_probe: Path) -> ScriptedEngine:
    """构造使首个候选帧识别失败的引擎 → OCR集合status=degraded。"""

    first_candidate = _candidate_files(root_probe)[0]
    return ScriptedEngine(
        _default_ocr_scripts(),
        fail_files={first_candidate},
        fail_error=RuntimeError("engine boom"),
    )


# ---------------------------------------------------------------------------
# 13-2 故障矩阵：OCR为空/失败、required边界
# ---------------------------------------------------------------------------


def test_ocr_empty_set_maps_to_empty_modality(tmp_path: Path) -> None:
    # 空脚本 → 所有候选帧0行 → OCR集合status=empty（reason no_text_detected）。
    root = _workspace(tmp_path, ocr_scripts={})
    idx = build_timeline_v2(TASK, output_root=root, ocr="auto").index
    assert _statuses(idx) == {
        "transcript": "available",
        "frame_change": "available",
        "ocr": "empty",
    }
    assert idx["modalities"]["ocr"]["reason_codes"] == ["no_text_detected"]
    assert _counts(idx)["ocr"] == 0
    assert idx["run_status"] == "success"
    assert idx["quality_level"] == "multimodal"  # frame_change仍available


def test_required_with_empty_ocr_succeeds(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ocr_scripts={})
    idx = build_timeline_v2(TASK, output_root=root, ocr="required").index
    assert _statuses(idx)["ocr"] == "empty"  # empty是required可接受状态


def test_ocr_degraded_maps_to_failed_and_keeps_partial_events(tmp_path: Path) -> None:
    probe = _workspace(tmp_path / "probe", publish_ocr=False)
    engine = _degraded_engine(probe)
    root = _workspace(tmp_path, ocr_engine=engine)
    res = build_timeline_v2(TASK, output_root=root, ocr="auto")
    idx = res.index
    assert _statuses(idx)["ocr"] == "failed"
    assert "recognition_failed" in idx["modalities"]["ocr"]["reason_codes"]
    # degraded → run_status降级、quality退回text_only（P2 §七冻结表）。
    assert idx["run_status"] == "degraded"
    assert idx["quality_level"] == "text_only"
    # frame_change不受影响，仍有pe事件；oe事件保留成功帧（部分）。
    assert _statuses(idx)["frame_change"] == "available"
    assert _counts(idx)["page_change"] == len(_candidates(root))
    expected_oe = _expected_oe_frames(root)
    assert _counts(idx)["ocr"] == len(expected_oe)
    assert _counts(idx)["ocr"] >= 1
    # 失败帧不得产出oe事件
    failed_ids = {
        f["frame_id"] for f in _ocr_index(root)["frames"] if f["status"] == "failed"
    }
    assert failed_ids, "fixture应至少失败一帧"
    for e in idx["events"]:
        if e["type"] == "ocr":
            assert e["payload"]["frame_id"] not in failed_ids


def test_required_with_degraded_ocr_hard_fails(tmp_path: Path) -> None:
    probe = _workspace(tmp_path / "probe", publish_ocr=False)
    engine = _degraded_engine(probe)
    root = _workspace(tmp_path, ocr_engine=engine)
    with pytest.raises(RequiredOcrUnavailableError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="required")
    assert exc.value.error_code == "REQUIRED_OCR_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 13-2 故障矩阵：输入current错配 / 链完整性
# ---------------------------------------------------------------------------


def test_ocr_current_manifest_sha_mismatch_hard_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    cur = _ocr_current(root)
    pointer = json.loads(cur.read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = "0" * 64  # 指向错误manifest哈希
    cur.write_bytes(_dumps_bytes(pointer))
    with pytest.raises(InputOcrSetCorruptError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "INPUT_OCR_SET_CORRUPT"


def test_ocr_group_content_tamper_hard_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    index = _ocr_index(root)
    # 篡改首帧canonical_text（validate_ocr_group会检出text_sha256不一致）
    for frame in index["frames"]:
        if frame["status"] == "ok" and frame["canonical_text"]:
            frame["canonical_text"] = "tampered text"
            break
    cur = json.loads(_ocr_current(root).read_text(encoding="utf-8"))
    group = root / TASK / "m2" / "ocr" / cur["group"]
    (group / "ocr-set.json").write_bytes(_dumps_bytes(index))
    with pytest.raises(InputOcrSetCorruptError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "INPUT_OCR_SET_CORRUPT"


def test_ocr_current_without_pc_current_hard_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _pc_current(root).unlink()  # OCR存在但页面变化current缺失
    with pytest.raises(InputPageChangeSetMissingError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "INPUT_PAGE_CHANGE_SET_MISSING"


def test_pc_current_without_frames_current_hard_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _frames_current(root).unlink()  # 页面变化存在但帧集current缺失
    with pytest.raises(InputFrameSetMissingError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "INPUT_FRAME_SET_MISSING"


def test_frames_current_target_missing_hard_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    cur = _frames_current(root)
    pointer = json.loads(cur.read_text(encoding="utf-8"))
    pointer["result_id"] = "frame-set-doesnotexist"
    pointer["group"] = "formal-results/frame-set-doesnotexist"
    cur.write_bytes(_dumps_bytes(pointer))
    with pytest.raises(InputFrameSetCorruptError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "INPUT_FRAME_SET_CORRUPT"


def test_off_ignores_dangling_ocr_current(tmp_path: Path) -> None:
    # 决策：--ocr off不读取OCR，即便OCR current悬空(缺pc)也不阻断纯文本+帧构建。
    root = _workspace(tmp_path)
    _pc_current(root).unlink()
    idx = build_timeline_v2(TASK, output_root=root, ocr="off").index
    assert _statuses(idx)["ocr"] == "disabled"
    assert _statuses(idx)["transcript"] == "available"


# ---------------------------------------------------------------------------
# 13-2 故障矩阵：缓存损坏 / current损坏 / 指针修复 / 并发
# ---------------------------------------------------------------------------


def test_cache_corrupt_index_not_overwritten(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    res = build_timeline_v2(TASK, output_root=root, ocr="auto")
    timeline_id = res.index["timeline_id"]
    index_file = _group_dir(root, timeline_id) / TIMELINE_INDEX_FILE
    index_file.write_bytes(b"{ this is not json")
    before_bytes = index_file.read_bytes()
    with pytest.raises(TimelineCacheCorruptError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "CACHE_CORRUPT"
    # 损坏组不得被覆盖；current不得改写；审计已写。
    assert index_file.read_bytes() == before_bytes
    assert json.loads(_timeline_current(root).read_text(encoding="utf-8"))["result_id"] == timeline_id
    audits = _failures(root)
    assert len(audits) == 1 and audits[0]["module"] == TIMELINE_MODULE_TOKEN


def test_current_pointer_corrupt_hard_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    build_timeline_v2(TASK, output_root=root, ocr="auto")
    _timeline_current(root).write_bytes(b"{ broken pointer")
    with pytest.raises(TimelineCurrentPointerCorruptError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "CURRENT_POINTER_CORRUPT"


def test_missing_current_repaired_from_intact_group(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = build_timeline_v2(TASK, output_root=root, ocr="auto")
    timeline_id = first.index["timeline_id"]
    _timeline_current(root).unlink()  # 组完整但current缺失
    repaired = build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert repaired.cache_hit is True
    assert repaired.pointer_repaired is True
    assert repaired.index["timeline_id"] == timeline_id
    assert json.loads(_timeline_current(root).read_text(encoding="utf-8"))["result_id"] == timeline_id


def test_concurrent_run_lock_blocks(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    lock = _lock_path(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b'{"held":"by-other"}')
    with pytest.raises(TimelineConcurrentRunError) as exc:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    assert exc.value.error_code == "CONCURRENT_RUN"
    assert lock.is_file()  # 不得自动删除既有锁
    _assert_no_timeline_published(root)


# ---------------------------------------------------------------------------
# 13-2 故障矩阵：发布失败注入（清理与审计不变量）
# ---------------------------------------------------------------------------


def test_after_inputs_injection_fails_before_lock_no_audit(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(InjectedTimelineFailure):
        build_timeline_v2(TASK, output_root=root, ocr="auto", failure_injection="after_inputs")
    # after_inputs在锁与审计边界之前 → 无审计、无发布。
    assert _failures(root) == []
    _assert_no_timeline_published(root)


@pytest.mark.parametrize(
    "point",
    ["after_index", "after_manifest", "before_group_publish", "after_group_publish", "before_current"],
)
def test_post_lock_injection_cleans_up_and_audits(tmp_path: Path, point: str) -> None:
    root = _workspace(tmp_path)
    with pytest.raises(InjectedTimelineFailure):
        build_timeline_v2(TASK, output_root=root, ocr="auto", failure_injection=point)
    # 失败不切current、不留悬空组或临时目录、写一条审计且current_unchanged为真。
    _assert_no_timeline_published(root)
    audits = _failures(root)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["module"] == TIMELINE_MODULE_TOKEN
    assert audit["error_code"] == "TIMELINE_BUILD_FAILED"
    assert audit["current_unchanged"] is True
    assert audit["business_call_count"] == 0 and audit["supplier_request_count"] == 0


def test_existing_current_unchanged_when_injection_fails(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    good = build_timeline_v2(TASK, output_root=root, ocr="off")  # 先发布一个off时间轴
    good_id = good.index["timeline_id"]
    good_current = _timeline_current(root).read_bytes()
    # 另起一个会成功的auto构建，但在切current前注入失败 → 既有off current必须不变。
    with pytest.raises(InjectedTimelineFailure):
        build_timeline_v2(TASK, output_root=root, ocr="auto", failure_injection="before_current")
    assert _timeline_current(root).read_bytes() == good_current
    assert json.loads(_timeline_current(root).read_text(encoding="utf-8"))["result_id"] == good_id
    audit = _failures(root)[-1]
    assert audit["current_unchanged"] is True
    assert audit["previous_current_id"] == good_id


def test_keyboard_interrupt_audits_and_reraises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path)

    def fake_inject(selected, point):
        if selected == point == "after_index":
            raise KeyboardInterrupt()

    monkeypatch.setattr("video_doc.timeline_builder_v2._inject", fake_inject)
    with pytest.raises(KeyboardInterrupt):
        build_timeline_v2(TASK, output_root=root, ocr="auto", failure_injection="after_index")
    _assert_no_timeline_published(root)
    audits = _failures(root)
    assert len(audits) == 1 and audits[0]["current_unchanged"] is True


# ---------------------------------------------------------------------------
# 13-1 语义：文本事件ID不随视觉输入重编号（P5阶段闸门）
# ---------------------------------------------------------------------------


def test_text_events_identical_across_visual_inputs(tmp_path: Path) -> None:
    text_only = build_timeline_v2(
        TASK, output_root=_workspace(tmp_path / "t", publish_frames=False), ocr="auto"
    ).index
    full = build_timeline_v2(TASK, output_root=_workspace(tmp_path / "f"), ocr="auto").index
    # 视觉输入从“无”到“帧+页面变化+OCR”，文本事件必须逐字节不变、ID不重编号。
    assert _text_events(full) == _text_events(text_only)
    assert [e["id"] for e in _text_events(full)] == [
        f"te{i:06d}" for i in range(1, len(_text_events(full)) + 1)
    ]
    assert _counts(full)["page_change"] > 0 and _counts(full)["ocr"] > 0
    assert _counts(text_only)["page_change"] == 0 and _counts(text_only)["ocr"] == 0


def test_beyond_duration_visual_candidates_skipped_with_reason(tmp_path: Path) -> None:
    # 候选在5000/15000/25000ms；M1时长设为20000ms且文本事件都在其中，
    # 则25000ms候选(decoded_ms+1>duration)被跳过并记reason，5000/15000保留。
    root = _workspace(
        tmp_path,
        m1_events=[(1.0, 5.0, "a"), (6.0, 10.0, "b"), (11.0, 15.0, "c")],
        duration_seconds=20.0,
    )
    idx = build_timeline_v2(TASK, output_root=root, ocr="auto").index
    duration_ms = idx["duration_ms"]
    assert duration_ms == 20000
    pe_starts = sorted(e["start_ms"] for e in idx["events"] if e["type"] == "page_change")
    assert pe_starts == [5000, 15000]  # 25000被时长截断
    assert 25000 not in pe_starts
    for e in idx["events"]:
        if e["type"] in ("page_change", "ocr"):
            assert e["end_ms"] <= duration_ms
    assert "page_change_beyond_duration" in idx["modalities"]["frame_change"]["reason_codes"]
    assert "ocr_beyond_duration" in idx["modalities"]["ocr"]["reason_codes"]
    # frame_change/ocr仍available（有保留事件），文本事件3条不受影响。
    assert _statuses(idx)["frame_change"] == "available"
    assert _counts(idx)["transcript"] == 3

