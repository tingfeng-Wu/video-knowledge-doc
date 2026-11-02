"""阶段14-A定向测试：ChapterFrameSelection 1.0选择、说明、发布与接线。

契约唯一权威：docs/development/阶段14A-章内代表帧与确定性说明裁决.md
（§二Q1—Q6＋§三实现契约汇总）。矩阵按§3.6.1/§3.6.2组织：
S=选择、V=caption逐字节、U=交叉/加载一致性、E/N=合法形态与合法空、
P=发布工程、I=输入身份、C=CLI、DEV01沙箱双形态e2e＋真实树整树零改写。

全部离线：手造dict直接喂build纯函数；集成层用真实模块链（frames/pc/ocr经
13-1测试的`_workspace`，timeline经build_timeline_v2，plan经14-B注入
ScriptedTransport）；DEV01用例只读复制task.json＋m2树到tmp（14-B先例），
并断言真实outputs/DEV01整树零改写。零网络、零真实API、零FFmpeg。

集成/沙箱层的期望值不硬编码：由本文件内独立重写的参考实现`_ref_select`
（按裁决§3.3/§3.4文字另行成文的双录入）从实际发布的五份文档重算，与产物
逐项比对；只有手造dict单元层使用显式冻结期望。
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.test_boundary_planner_v2 import (
    ENV,
    GOOD_RESPONSE,
    HAPPY_EVENTS,
    ScriptedTransport,
    _mutate_role,
    _plan,
    _registry,
)
from tests.test_timeline_builder_v2 import TASK
from tests.test_timeline_builder_v2 import _workspace as _synth_chain
from video_doc import chapter_frames as cf
from video_doc.boundary_planner_v2 import (
    MIN_CHAPTER_MS,
    load_chapter_plan_current,
    plan_chapters_v2,
)
from video_doc.cli import main
from video_doc.task_workspace import initialize_task_workspace
from video_doc.timeline_builder_v2 import build_timeline_v2
from video_doc.unified_timeline import canonical_json_bytes, canonical_json_sha256

REPO_ROOT = Path(__file__).resolve().parents[1]
_DEV01_REAL = REPO_ROOT / "outputs" / "DEV01"

HEX_A = "a" * 64
HEX_B = "b" * 64


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 手造文档工厂（键集与裁决§〇实测冻结键集精确一致）
# ---------------------------------------------------------------------------


def _frame_entry(frame_id: str, decoded_ms: int) -> dict[str, Any]:
    return {
        "frame_id": frame_id,
        "decoded_ms": decoded_ms,
        "file": f"{frame_id}.png",
        "is_first": False,
        "is_last": False,
        "ordinal": 0,
        "planned_ms": decoded_ms,
        "requested_ms": decoded_ms,
        "role": "sampled",
        "sha256": HEX_A,
        "size_bytes": 100,
    }


def _frame_doc(entries: list[dict[str, Any]], *, task_id: str = "T1",
               frame_set_id: str = "frame-set-1") -> dict[str, Any]:
    for position, entry in enumerate(entries):
        entry["ordinal"] = position
        entry["is_first"] = position == 0
        entry["is_last"] = position == len(entries) - 1
    return {
        "cache_identity_sha256": HEX_A,
        "frame_count": len(entries),
        "frame_set_id": frame_set_id,
        "frames": entries,
        "profile": {},
        "profile_id": "low-frequency-v1",
        "sampling": {},
        "schema_name": "FrameSet",
        "schema_version": "1.0",
        "source_video": "v.mp4",
        "status": "success",
        "task_id": task_id,
    }


def _pc_candidate(candidate_id: str, frame_id: str, decoded_ms: int,
                  strength: float, *, prev: str, distance: float = 0.02) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "change_strength": strength,
        "decoded_ms": decoded_ms,
        "frame_id": frame_id,
        "ordinal": int(candidate_id[2:]) if candidate_id[2:].isdigit() else 0,
        "perceptual_distance": distance,
        "planned_ms": decoded_ms,
        "previous_frame_id": prev,
        "requested_ms": decoded_ms,
    }


def _pc_doc(candidates: list[dict[str, Any]], *, task_id: str = "T1",
            pc_set_id: str = "page-change-set-1", status: str = "success") -> dict[str, Any]:
    for position, item in enumerate(candidates):
        item["ordinal"] = position
    return {
        "cache_identity_sha256": HEX_A,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "comparison_count": 0,
        "comparisons": [],
        "input_frame_set": {},
        "page_change_set_id": pc_set_id,
        "profile": {},
        "profile_id": "page-change-v1",
        "schema_name": "PageChangeSet",
        "schema_version": "1.0",
        "selection": {},
        "status": status,
        "task_id": task_id,
    }


def _ocr_line(line_index: int, text: str) -> dict[str, Any]:
    return {"box": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
            "confidence": 0.99, "line_index": line_index, "text": text}


def _ocr_entry(candidate_id: str, frame_id: str, decoded_ms: int,
               lines: list[dict[str, Any]], *, status: str = "ok",
               duplicate_of: str | None = None,
               canonical: str | None = None) -> dict[str, Any]:
    if canonical is None:
        canonical = " ".join(
            " ".join(line["text"].split()) for line in lines if line["text"].strip()
        ).lower()
    return {
        "candidate_id": candidate_id,
        "canonical_text": canonical,
        "decoded_ms": decoded_ms,
        "duplicate_of": duplicate_of,
        "filtered_line_count": 0,
        "frame_id": frame_id,
        "line_count": len(lines),
        "lines": lines,
        "ordinal": 0,
        "reason_codes": [],
        "status": status,
        "text_sha256": _sha(candidate_id + frame_id),
    }


def _ocr_doc(entries: list[dict[str, Any]], *, task_id: str = "T1",
             ocr_set_id: str = "ocr-set-1", mode: str = "auto",
             status: str = "success") -> dict[str, Any]:
    for position, item in enumerate(entries):
        item["ordinal"] = position
    return {
        "cache_identity_sha256": HEX_A,
        "deduplicated_count": 0,
        "engine": {},
        "failed_frame_count": 0,
        "filtered_line_count": 0,
        "frame_count": len(entries),
        "frames": entries,
        "input": {},
        "line_count": sum(int(entry["line_count"]) for entry in entries),
        "mode": mode,
        "ocr_set_id": ocr_set_id,
        "profile": {},
        "profile_id": "rapidocr-v1",
        "reason_codes": [],
        "schema_name": "OCRSet",
        "schema_version": "1.0",
        "status": status,
        "task_id": task_id,
    }


def _pe_event(event_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "page_change",
        "start_ms": int(candidate["decoded_ms"]),
        "end_ms": int(candidate["decoded_ms"]),
        "source_kind": "m2",
        "source_segment_id": None,
        "payload": {
            "candidate_id": candidate["candidate_id"],
            "frame_id": candidate["frame_id"],
            "previous_frame_id": candidate["previous_frame_id"],
            "change_strength": candidate["change_strength"],
            "perceptual_distance": candidate["perceptual_distance"],
        },
        "payload_schema": "page-change-event-1.0",
        "legacy_event_id": None,
    }


def _oe_event(event_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "ocr",
        "start_ms": int(entry["decoded_ms"]),
        "end_ms": int(entry["decoded_ms"]),
        "source_kind": "m2",
        "source_segment_id": None,
        "payload": {
            "candidate_id": entry["candidate_id"],
            "frame_id": entry["frame_id"],
            "line_count": int(entry["line_count"]),
            "text": entry["canonical_text"],
            "text_sha256": entry["text_sha256"],
        },
        "payload_schema": "ocr-event-1.0",
        "legacy_event_id": None,
    }


def _timeline_doc(events: list[dict[str, Any]], *, duration_ms: int,
                  task_id: str = "T1", timeline_id: str = "unified-timeline-1",
                  fc_status: str | None = None,
                  ocr_status: str | None = None) -> dict[str, Any]:
    pe_count = sum(1 for event in events if event["type"] == "page_change")
    oe_count = sum(1 for event in events if event["type"] == "ocr")
    return {
        "schema_name": "UnifiedTimeline",
        "schema_version": "2.0",
        "task_id": task_id,
        "timeline_id": timeline_id,
        "duration_ms": duration_ms,
        "events": events,
        "modalities": {
            "transcript": {"status": "available", "reason_codes": []},
            "frame_change": {
                "status": fc_status if fc_status is not None
                else ("available" if pe_count else "empty"),
                "reason_codes": [],
            },
            "ocr": {
                "status": ocr_status if ocr_status is not None
                else ("available" if oe_count else "empty"),
                "reason_codes": [],
            },
        },
        "run_status": "success",
        "quality_level": "multimodal",
        "primary_transcript": "m1",
    }


def _plan_doc(boundaries: list[tuple[int, str, str | None]], *, duration_ms: int,
              task_id: str = "T1", timeline_id: str = "unified-timeline-1",
              result_source: str = "primary", quality_level: str = "normal") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "duration_ms": duration_ms,
        "source_timeline": {"timeline_id": timeline_id},
        "parameters": {"result_source": result_source, "quality_level": quality_level},
        "boundaries": [
            {"timestamp_ms": start, "title": title, "source_event_id": source}
            for start, title, source in boundaries
        ],
    }


def _refs(*, plan_id: str = "chapter-plan-1", timeline_id: str = "unified-timeline-1",
          frame_id: str = "frame-set-1", pc_id: str | None = None,
          ocr_id: str | None = None) -> dict[str, Any]:
    refs: dict[str, Any] = {
        "plan_ref": {"result_id": plan_id, "manifest_sha256": HEX_A},
        "timeline_ref": {"result_id": timeline_id, "manifest_sha256": HEX_B},
        "frame_set_ref": {"result_id": frame_id, "manifest_sha256": HEX_A},
    }
    if pc_id is not None:
        refs["page_change_set_ref"] = {"result_id": pc_id, "manifest_sha256": HEX_B}
    if ocr_id is not None:
        refs["ocr_set_ref"] = {"result_id": ocr_id, "manifest_sha256": HEX_A}
    return refs


def _build(plan: dict[str, Any], timeline: dict[str, Any], frame_doc: dict[str, Any],
           pc_doc: dict[str, Any] | None, ocr_doc: dict[str, Any] | None,
           refs: dict[str, Any], *, task_id: str = "T1") -> dict[str, Any]:
    return cf.build_chapter_frame_selection(
        plan,
        timeline,
        frame_doc,
        pc_doc,
        ocr_doc,
        task_id=task_id,
        plan_ref=refs["plan_ref"],
        timeline_ref=refs["timeline_ref"],
        frame_set_ref=refs["frame_set_ref"],
        page_change_set_ref=refs.get("page_change_set_ref"),
        ocr_set_ref=refs.get("ocr_set_ref"),
    )


def _fixture() -> dict[str, Any]:
    """S/U/V共用基座：6帧@0..50000，pe在fr2(.30)/fr4(.10)，oe在fr2/fr3。

    级联期望：t1=[fr2]、t2=[fr3]、t3=[fr4]，单章全部选中tier1/2/3。
    """
    frames = [_frame_entry(f"fr00000{i}", (i - 1) * 10_000) for i in range(1, 7)]
    c2 = _pc_candidate("pc000001", "fr000002", 10_000, 0.30, prev="fr000001")
    c4 = _pc_candidate("pc000002", "fr000004", 30_000, 0.10, prev="fr000003")
    e2 = _ocr_entry("oc000001", "fr000002", 10_000,
                    [_ocr_line(0, "alpha"), _ocr_line(1, "beta")])
    e3 = _ocr_entry("oc000002", "fr000003", 20_000, [_ocr_line(0, "gamma")])
    events = [_pe_event("pe000001", c2), _pe_event("pe000002", c4),
              _oe_event("oe000001", e2), _oe_event("oe000002", e3)]
    return {
        "frames": frames,
        "frame_doc": _frame_doc(frames),
        "pc_doc": _pc_doc([c2, c4]),
        "ocr_doc": _ocr_doc([e2, e3]),
        "events": events,
        "timeline": _timeline_doc(events, duration_ms=100_000),
        "plan": _plan_doc([(0, "第一章", None)], duration_ms=100_000),
        "refs": _refs(pc_id="page-change-set-1", ocr_id="ocr-set-1"),
    }


def _build_fixture(fx: dict[str, Any]) -> dict[str, Any]:
    return _build(fx["plan"], fx["timeline"], fx["frame_doc"],
                  fx["pc_doc"], fx["ocr_doc"], fx["refs"])


# ---------------------------------------------------------------------------
# 独立参考实现（按裁决§3.3/§3.4文字另行成文的双录入，供集成与沙箱层）
# ---------------------------------------------------------------------------

_FORM_BY_TIER = {1: "pe_ocr", 2: "ocr_only", 3: "pe_only", 4: "time_only"}


def _ref_clock(decoded_ms: int) -> str:
    seconds_total = int(decoded_ms) // 1000
    return (
        f"{seconds_total // 3600:02d}:{(seconds_total % 3600) // 60:02d}:"
        f"{seconds_total % 60:02d}"
    )


def _ref_excerpt(entry: dict[str, Any]) -> tuple[list[str], int, int]:
    ordered = sorted(entry["lines"], key=lambda line: line["line_index"])
    nonblank = [line["text"].strip() for line in ordered if line["text"].strip()]
    excerpt: list[str] = []
    for text in nonblank[:3]:
        rendered = text.replace("\n", " ").replace("\t", " ")
        if len(rendered) > 40:
            rendered = rendered[:40] + "…"
        excerpt.append(rendered)
    return excerpt, len(nonblank), sum(len(text) for text in nonblank)


def _ref_caption(form: str, decoded_ms: int, strength: Any,
                 excerpt: list[str]) -> str:
    time_str = _ref_clock(decoded_ms)
    ocr_part = "该帧OCR识别文本（前" + str(len(excerpt)) + "行）：" + "；".join(excerpt)
    if form == "pe_ocr":
        return (
            f"画面于{time_str}发生变化（相对前一采样帧的变化强度{float(strength):.4f}）。"
            + ocr_part
        )
    if form == "pe_only":
        return f"画面于{time_str}发生变化（相对前一采样帧的变化强度{float(strength):.4f}）。"
    if form == "ocr_only":
        return f"{time_str}采样帧。" + ocr_part
    return f"{time_str}采样帧。"


def _ref_select(plan: dict[str, Any], timeline: dict[str, Any],
                frame_doc: dict[str, Any], pc_doc: dict[str, Any] | None,
                ocr_doc: dict[str, Any] | None) -> dict[str, Any]:
    pe_by_frame = {
        event["payload"]["frame_id"]: event
        for event in timeline["events"] if event["type"] == "page_change"
    }
    oe_by_frame = {
        event["payload"]["frame_id"]: event
        for event in timeline["events"] if event["type"] == "ocr"
    }
    cand_by_id = {
        candidate["candidate_id"]: candidate
        for candidate in (pc_doc or {}).get("candidates", [])
    }
    entry_by_id = {
        entry["candidate_id"]: entry
        for entry in (ocr_doc or {}).get("frames", [])
    }
    strength_by_frame = {
        frame_id: cand_by_id[event["payload"]["candidate_id"]]["change_strength"]
        for frame_id, event in pe_by_frame.items()
    }
    duration_ms = int(plan["duration_ms"])
    boundaries = plan["boundaries"]
    tiers = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0}
    forms = {"pe_ocr": 0, "pe_only": 0, "ocr_only": 0, "time_only": 0}
    selected_total = 0
    without_frames = 0
    chapters: list[dict[str, Any]] = []
    for index, boundary in enumerate(boundaries):
        start = int(boundary["timestamp_ms"])
        end = int(boundaries[index + 1]["timestamp_ms"]) if index + 1 < len(boundaries) else duration_ms
        pool = [
            entry for entry in frame_doc["frames"]
            if start <= int(entry["decoded_ms"]) < end
        ]

        def _fid(entry: dict[str, Any]) -> str:
            return str(entry["frame_id"])

        def _time(entry: dict[str, Any]) -> int:
            return int(entry["decoded_ms"])

        t1 = sorted(
            [e for e in pool if _fid(e) in pe_by_frame and _fid(e) in oe_by_frame],
            key=lambda e: (-float(strength_by_frame[_fid(e)]), _time(e), _fid(e)),
        )
        t2_items = [e for e in pool if _fid(e) in oe_by_frame and _fid(e) not in pe_by_frame]

        def _t2_key(e: dict[str, Any]) -> tuple[int, int, int, str]:
            entry = entry_by_id[oe_by_frame[_fid(e)]["payload"]["candidate_id"]]
            _excerpt, nonblank_lines, nonblank_chars = _ref_excerpt(entry)
            return (-nonblank_lines, -nonblank_chars, _time(e), _fid(e))

        t2 = sorted(t2_items, key=_t2_key)
        t3 = sorted(
            [e for e in pool if _fid(e) in pe_by_frame and _fid(e) not in oe_by_frame],
            key=lambda e: (-float(strength_by_frame[_fid(e)]), _time(e), _fid(e)),
        )
        cascade = [(e, 1) for e in t1] + [(e, 2) for e in t2] + [(e, 3) for e in t3]
        chosen = cascade[:3]
        if not chosen and pool:
            mid = (start + end) // 2
            chosen = [
                (
                    min(pool, key=lambda e: (abs(_time(e) - mid), _time(e), _fid(e))),
                    4,
                )
            ]
        out: list[dict[str, Any]] = []
        for rank, (entry, tier) in enumerate(chosen, start=1):
            form = _FORM_BY_TIER[tier]
            strength: Any = None
            excerpt: list[str] = []
            if tier in (1, 3):
                strength = strength_by_frame[_fid(entry)]
            if tier in (1, 2):
                excerpt = _ref_excerpt(
                    entry_by_id[oe_by_frame[_fid(entry)]["payload"]["candidate_id"]]
                )[0]
            out.append(
                {
                    "frame_id": _fid(entry),
                    "rank": rank,
                    "tier": tier,
                    "decoded_ms": _time(entry),
                    "caption_form": form,
                    "caption_text": _ref_caption(form, _time(entry), strength, excerpt),
                }
            )
        out.sort(key=lambda item: (item["decoded_ms"], item["frame_id"]))
        for item in out:
            tiers[f"tier{item['tier']}"] += 1
            forms[item["caption_form"]] += 1
        selected_total += len(out)
        if not out:
            without_frames += 1
        chapters.append(
            {
                "chapter_index": index,
                "start_ms": start,
                "end_ms": end,
                "frames": out,
            }
        )
    return {
        "chapters": chapters,
        "totals": {
            "chapter_count": len(chapters),
            "selected_frame_count": selected_total,
            "chapters_without_frames": without_frames,
            "tier_counts": tiers,
            "caption_form_counts": forms,
        },
    }


def _assert_matches_reference(selection: dict[str, Any], plan: dict[str, Any],
                              timeline: dict[str, Any], frame_doc: dict[str, Any],
                              pc_doc: dict[str, Any] | None,
                              ocr_doc: dict[str, Any] | None) -> dict[str, Any]:
    ref = _ref_select(plan, timeline, frame_doc, pc_doc, ocr_doc)
    assert selection["totals"] == ref["totals"]
    assert len(selection["chapters"]) == len(ref["chapters"])
    for chapter, expected in zip(selection["chapters"], ref["chapters"]):
        assert chapter["chapter_index"] == expected["chapter_index"]
        assert (chapter["start_ms"], chapter["end_ms"]) == (expected["start_ms"], expected["end_ms"])
        observed = [
            {key: frame[key] for key in
             ("frame_id", "rank", "tier", "decoded_ms", "caption_form", "caption_text")}
            for frame in chapter["frames"]
        ]
        assert observed == expected["frames"], chapter["chapter_index"]
        for frame in chapter["frames"]:
            assert expected["start_ms"] <= frame["decoded_ms"] < expected["end_ms"]
    return ref


# ---------------------------------------------------------------------------
# 磁盘/审计助手
# ---------------------------------------------------------------------------


def _own_root(output_root: Path, task_id: str) -> Path:
    return output_root / task_id / "m2" / cf.ROOT_DIRECTORY


def _failures_dir(output_root: Path) -> Path:
    return output_root / TASK / "m2" / "failures" / cf.MODULE_TOKEN


def _only_failure(output_root: Path) -> dict[str, Any]:
    entries = list(_failures_dir(output_root).iterdir())
    assert len(entries) == 1
    return json.loads((entries[0] / "failure.json").read_text(encoding="utf-8"))


def _tree_sha_pairs(root: Path) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for entry in sorted(root.rglob("*")):
        if entry.is_file():
            data = entry.read_bytes()
            out[entry.relative_to(root).as_posix()] = (
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
    return out


def _chain(tmp_path: Path) -> Path:
    """真实链合成工作区：M1＋F1＋F2＋OCR＋Timeline 2.0(auto)＋注入primary plan。"""
    root = _synth_chain(tmp_path, m1_events=HAPPY_EVENTS, duration_seconds=600.0)
    initialize_task_workspace(root, TASK)
    build_timeline_v2(TASK, output_root=root, ocr="auto")
    _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    return root


# ---------------------------------------------------------------------------
# S矩阵：四级确定性选择（裁决Q3）
# ---------------------------------------------------------------------------


def _scene(*, frames: list[dict[str, Any]], pcs: list[dict[str, Any]] = (),
           ocrs: list[dict[str, Any]] = (), plan: dict[str, Any],
           fc: str | None = None, ocr: str | None = None) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for position, candidate in enumerate(pcs, start=1):
        events.append(_pe_event(f"pe{position:06d}", candidate))
    for position, entry in enumerate(ocrs, start=1):
        events.append(_oe_event(f"oe{position:06d}", entry))
    frame_doc = _frame_doc([copy.deepcopy(entry) for entry in frames])
    pc_doc = _pc_doc([copy.deepcopy(candidate) for candidate in pcs])
    ocr_doc = _ocr_doc([copy.deepcopy(entry) for entry in ocrs])
    timeline = _timeline_doc(
        events, duration_ms=int(plan["duration_ms"]), fc_status=fc, ocr_status=ocr
    )
    return _build(
        plan, timeline, frame_doc, pc_doc, ocr_doc,
        _refs(pc_id="page-change-set-1", ocr_id="ocr-set-1"),
    )


def _frames(*specs: tuple[str, int]) -> list[dict[str, Any]]:
    return [_frame_entry(frame_id, decoded_ms) for frame_id, decoded_ms in specs]


def _summary(chapter: dict[str, Any]) -> list[tuple[str, int, int]]:
    return [(f["frame_id"], f["rank"], f["tier"]) for f in chapter["frames"]]


def test_s1_four_tier_cascade_hit() -> None:
    fx = _fixture()
    selection = _build_fixture(fx)
    chapter = selection["chapters"][0]
    assert _summary(chapter) == [("fr000002", 1, 1), ("fr000003", 2, 2), ("fr000004", 3, 3)]
    f2, f3, f4 = chapter["frames"]
    assert f2["caption_form"] == "pe_ocr" and f2["page_change"] is not None and f2["ocr"] is not None
    assert f3["caption_form"] == "ocr_only" and f3["page_change"] is None and f3["ocr"] is not None
    assert f4["caption_form"] == "pe_only" and f4["page_change"] is not None and f4["ocr"] is None
    assert f2["caption_text"] == _ref_caption("pe_ocr", 10_000, 0.30, ["alpha", "beta"])
    assert f2["page_change"]["event_id"] == "pe000001"
    assert f2["page_change"]["candidate_id"] == "pc000001"
    assert f2["ocr"]["event_id"] == "oe000001" and f2["ocr"]["excerpt_line_count"] == 2
    totals = selection["totals"]
    assert totals["tier_counts"] == {"tier1": 1, "tier2": 1, "tier3": 1, "tier4": 0}
    assert totals["caption_form_counts"] == {
        "pe_ocr": 1, "pe_only": 1, "ocr_only": 1, "time_only": 0
    }


def test_s2_tier1_over_cap_truncates_by_strength() -> None:
    specs = [("fr000002", 10_000, 0.1), ("fr000003", 20_000, 0.2),
             ("fr000004", 30_000, 0.3), ("fr000005", 40_000, 0.4)]
    frames = _frames(*[(fid, dec) for fid, dec, _s in specs])
    pcs = [
        _pc_candidate(f"pc00000{i}", fid, dec, s, prev="fr000001")
        for i, (fid, dec, s) in enumerate(specs, start=1)
    ]
    ocrs = [
        _ocr_entry(f"oc00000{i}", fid, dec, [_ocr_line(0, f"line{i}")])
        for i, (fid, dec, _s) in enumerate(specs, start=1)
    ]
    selection = _scene(
        frames=frames, pcs=pcs, ocrs=ocrs,
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
    )
    # 降序：0.4@40000、0.3@30000、0.2@20000入选，0.1@10000被CAP=3截断。
    assert _summary(selection["chapters"][0]) == [
        ("fr000003", 3, 1), ("fr000004", 2, 1), ("fr000005", 1, 1)
    ]
    assert selection["totals"]["tier_counts"]["tier1"] == 3


def test_s3_cascade_mixes_t1_then_t2() -> None:
    frames = _frames(
        ("fr000002", 10_000), ("fr000003", 20_000), ("fr000004", 30_000),
        ("fr000005", 40_000), ("fr000006", 50_000),
    )
    pcs = [
        _pc_candidate("pc000001", "fr000002", 10_000, 0.5, prev="fr000001"),
        _pc_candidate("pc000002", "fr000003", 20_000, 0.1, prev="fr000002"),
    ]
    ocrs = [
        _ocr_entry("oc000001", "fr000002", 10_000, [_ocr_line(0, "a")]),
        _ocr_entry("oc000002", "fr000003", 20_000, [_ocr_line(0, "b")]),
        _ocr_entry("oc000003", "fr000004", 30_000, [_ocr_line(0, "c")]),
        _ocr_entry("oc000004", "fr000005", 40_000, [_ocr_line(0, "ab"), _ocr_line(1, "cd")]),
        _ocr_entry("oc000005", "fr000006", 50_000,
                   [_ocr_line(0, "abcdefgh"), _ocr_line(1, "ijklmn")]),
    ]
    selection = _scene(
        frames=frames, pcs=pcs, ocrs=ocrs,
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
    )
    # t1=[fr2(.5),fr3(.1)]；t2按(2行14字>2行4字>1行)：fr6,fr5,fr4。级联取3：fr2,fr3,fr6。
    assert _summary(selection["chapters"][0]) == [
        ("fr000002", 1, 1), ("fr000003", 2, 1), ("fr000006", 3, 2)
    ]


def test_s4_tier1_tie_break_by_decoded_then_frame_id() -> None:
    frames = _frames(("fr000002", 20_000), ("fr000003", 10_000))
    pcs = [
        _pc_candidate("pc000001", "fr000002", 20_000, 0.2, prev="fr000001"),
        _pc_candidate("pc000002", "fr000003", 10_000, 0.2, prev="fr000001"),
    ]
    ocrs = [
        _ocr_entry("oc000001", "fr000002", 20_000, [_ocr_line(0, "x")]),
        _ocr_entry("oc000002", "fr000003", 10_000, [_ocr_line(0, "x")]),
    ]
    selection = _scene(
        frames=frames, pcs=pcs, ocrs=ocrs,
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
    )
    # 同强度→decoded升序：10000先。写出仍按decoded升序。
    assert _summary(selection["chapters"][0]) == [("fr000003", 1, 1), ("fr000002", 2, 1)]


def test_s5_tier2_tie_breaks() -> None:
    frames = _frames(("fr000004", 30_000), ("fr000005", 40_000), ("fr000007", 30_000))
    ocrs = [
        _ocr_entry("oc000001", "fr000004", 30_000, [_ocr_line(0, "ef"), _ocr_line(1, "gh")]),
        _ocr_entry("oc000002", "fr000005", 40_000, [_ocr_line(0, "z")]),
        _ocr_entry("oc000003", "fr000007", 30_000, [_ocr_line(0, "ab"), _ocr_line(1, "cd")]),
    ]
    selection = _scene(
        frames=frames, ocrs=ocrs,
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
    )
    # 同行数同字符数：fr000004与fr000007同decoded→frame_id升序，然后才是fr000005(1行)。
    assert _summary(selection["chapters"][0]) == [
        ("fr000004", 1, 2), ("fr000007", 2, 2), ("fr000005", 3, 2)
    ]


def test_s6_boundary_frame_belongs_to_later_chapter() -> None:
    frames = _frames(("fr000001", 0), ("fr000002", 50_000))
    pcs = [_pc_candidate("pc000001", "fr000002", 50_000, 0.3, prev="fr000001")]
    ocrs = [_ocr_entry("oc000001", "fr000002", 50_000, [_ocr_line(0, "edge")])]
    selection = _scene(
        frames=frames, pcs=pcs, ocrs=ocrs,
        plan=_plan_doc([(0, "上", None), (50_000, "下", "te000001")], duration_ms=100_000),
    )
    assert _summary(selection["chapters"][0]) == [("fr000001", 1, 4)]
    assert _summary(selection["chapters"][1]) == [("fr000002", 1, 1)]


def test_s7_last_chapter_end_equals_duration() -> None:
    selection = _scene(
        frames=_frames(("fr000001", 0)),
        plan=_plan_doc([(0, "上", None), (40_000, "下", "te000001")], duration_ms=100_000),
    )
    assert selection["chapters"][-1]["end_ms"] == 100_000
    assert selection["chapters"][0]["end_ms"] == 40_000
    assert selection["totals"]["chapter_count"] == 2


def test_s8_tier4_fallback_nearest_to_midpoint_with_tie() -> None:
    frames = _frames(("fr000002", 10_000), ("fr000005", 45_000),
                     ("fr000006", 55_000), ("fr000007", 60_000))
    selection = _scene(
        frames=frames,
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
        fc="empty", ocr="disabled",
    )
    chapter = selection["chapters"][0]
    assert _summary(chapter) == [("fr000005", 1, 4)]
    frame = chapter["frames"][0]
    assert frame["caption_form"] == "time_only"
    assert frame["caption_text"] == "00:00:45采样帧。"
    assert frame["page_change"] is None and frame["ocr"] is None


def test_s9_empty_pool_chapter_is_legal_empty() -> None:
    selection = _scene(
        frames=_frames(("fr000001", 0)),
        plan=_plan_doc([(0, "上", None), (50_000, "下", "te000001")], duration_ms=100_000),
        fc="empty", ocr="disabled",
    )
    assert selection["status"] == "success"
    assert selection["chapters"][1]["frames"] == []
    assert selection["chapters"][1]["selected_frame_count"] == 0
    assert selection["totals"]["chapters_without_frames"] == 1
    assert selection["totals"]["selected_frame_count"] == 1


def test_s10_whole_selection_empty_still_success() -> None:
    selection = _scene(
        frames=[],
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
        fc="empty", ocr="empty",
    )
    assert selection["status"] == "success"
    assert selection["totals"]["selected_frame_count"] == 0
    assert selection["totals"]["chapters_without_frames"] == 1
    assert selection["source_frame_set"]["frame_count"] == 0


def test_s11_emergency_single_chapter_selects_normally() -> None:
    frames = _frames(*[(f"fr00000{i}", i * 10_000) for i in range(1, 6)])
    pcs = [
        _pc_candidate(f"pc00000{i}", f"fr00000{i}", i * 10_000, 0.1 * i, prev="fr000001")
        for i in range(1, 6)
    ]
    ocrs = [
        _ocr_entry(f"oc00000{i}", f"fr00000{i}", i * 10_000, [_ocr_line(0, f"t{i}")])
        for i in range(1, 6)
    ]
    plan = _plan_doc([(0, "完整内容", None)], duration_ms=100_000,
                     result_source="emergency", quality_level="degraded")
    selection = _scene(frames=frames, pcs=pcs, ocrs=ocrs, plan=plan)
    assert selection["source_plan"]["result_source"] == "emergency"
    assert selection["source_plan"]["quality_level"] == "degraded"
    assert selection["totals"]["chapter_count"] == 1
    assert _summary(selection["chapters"][0]) == [
        ("fr000003", 3, 1), ("fr000004", 2, 1), ("fr000005", 1, 1)
    ]


def test_s12_duplicate_and_failed_ocr_frames_cannot_reach_tier1_2() -> None:
    frames = _frames(("fr000002", 10_000), ("fr000003", 20_000), ("fr000004", 30_000))
    pcs = [
        _pc_candidate("pc000001", "fr000002", 10_000, 0.4, prev="fr000001"),
        _pc_candidate("pc000002", "fr000003", 20_000, 0.3, prev="fr000002"),
        _pc_candidate("pc000003", "fr000004", 30_000, 0.2, prev="fr000003"),
    ]
    ocrs = [
        _ocr_entry("oc000001", "fr000002", 10_000, [_ocr_line(0, "dup")],
                   duplicate_of="oc000003"),
        _ocr_entry("oc000002", "fr000003", 20_000, [], status="failed", canonical=""),
        _ocr_entry("oc000003", "fr000004", 30_000, [_ocr_line(0, "real")]),
    ]
    events = [_pe_event(f"pe00000{i}", c) for i, c in enumerate(pcs, start=1)]
    events.append(_oe_event("oe000001", ocrs[2]))
    timeline = _timeline_doc(events, duration_ms=100_000, ocr_status="available")
    selection = _build(
        _plan_doc([(0, "第一章", None)], duration_ms=100_000), timeline,
        _frame_doc(copy.deepcopy(frames)), _pc_doc(copy.deepcopy(pcs)),
        _ocr_doc(copy.deepcopy(ocrs)), _refs(pc_id="p", ocr_id="o"),
    )
    # 级联序：fr4(t1)→fr2(t3,0.4)→fr3(t3,0.3)；写出按decoded升序。
    assert _summary(selection["chapters"][0]) == [
        ("fr000002", 2, 3), ("fr000003", 3, 3), ("fr000004", 1, 1)
    ]
    assert selection["totals"]["tier_counts"]["tier2"] == 0
    by_id = {f["frame_id"]: f for f in selection["chapters"][0]["frames"]}
    assert by_id["fr000004"]["caption_form"] == "pe_ocr"
    assert by_id["fr000002"]["caption_form"] == "pe_only"
    assert by_id["fr000003"]["caption_form"] == "pe_only"


def test_s13_previous_frame_may_live_in_earlier_chapter() -> None:
    frames = _frames(("fr000001", 0), ("fr000003", 60_000))
    pcs = [_pc_candidate("pc000001", "fr000003", 60_000, 0.25, prev="fr000001")]
    ocrs = [_ocr_entry("oc000001", "fr000003", 60_000, [_ocr_line(0, "跨章")])]
    selection = _scene(
        frames=frames, pcs=pcs, ocrs=ocrs,
        plan=_plan_doc([(0, "上", None), (50_000, "下", "te000001")], duration_ms=100_000),
    )
    chapter = selection["chapters"][1]
    assert chapter["frames"][0]["tier"] == 1
    assert chapter["frames"][0]["page_change"]["previous_frame_id"] == "fr000001"
    assert "fr000001" not in chapter["frames"][0]["caption_text"]


# ---------------------------------------------------------------------------
# V矩阵：deterministic-caption-zh-1.0逐字节（裁决Q4）
# ---------------------------------------------------------------------------


def test_v1_pe_ocr_bytes() -> None:
    assert cf.render_caption_text(
        caption_form="pe_ocr", decoded_ms=5_000, change_strength=0.16704,
        excerpt_lines=["alpha", "beta"],
    ) == "画面于00:00:05发生变化（相对前一采样帧的变化强度0.1670）。该帧OCR识别文本（前2行）：alpha；beta"


def test_v2_pe_only_bytes_and_integer_strength() -> None:
    assert cf.render_caption_text(
        caption_form="pe_only", decoded_ms=30_000, change_strength=0.1
    ) == "画面于00:00:30发生变化（相对前一采样帧的变化强度0.1000）。"
    assert cf.render_caption_text(
        caption_form="pe_only", decoded_ms=30_000, change_strength=1
    ) == "画面于00:00:30发生变化（相对前一采样帧的变化强度1.0000）。"


def test_v3_ocr_only_bytes() -> None:
    assert cf.render_caption_text(
        caption_form="ocr_only", decoded_ms=20_000, excerpt_lines=["只有一行"]
    ) == "00:00:20采样帧。该帧OCR识别文本（前1行）：只有一行"


def test_v4_time_only_bytes() -> None:
    assert cf.render_caption_text(caption_form="time_only", decoded_ms=0) == "00:00:00采样帧。"


def test_v5_unknown_form_rejected() -> None:
    with pytest.raises(cf.SelectionValidationFailedError) as excinfo:
        cf.render_caption_text(caption_form="free_text", decoded_ms=0)
    assert excinfo.value.error_code == cf.ERROR_SELECTION_VALIDATION_FAILED


def test_v6_clock_floor() -> None:
    def clock(decoded_ms: int) -> str:
        return cf.render_caption_text(
            caption_form="pe_only", decoded_ms=decoded_ms, change_strength=0.5
        )[3:11]

    assert clock(5_999) == "00:00:05"
    assert clock(1_000) == "00:00:01"
    assert clock(3_661_500) == "01:01:01"
    assert cf.render_caption_text(caption_form="time_only", decoded_ms=3_600_000) == "01:00:00采样帧。"


def _single_ocr_frame(lines: list[dict[str, Any]], decoded_ms: int = 20_000) -> dict[str, Any]:
    entry = _ocr_entry("oc000001", "fr000002", decoded_ms, lines)
    selection = _scene(
        frames=_frames(("fr000002", decoded_ms)),
        ocrs=[entry],
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
    )
    frame = selection["chapters"][0]["frames"][0]
    assert frame["tier"] == 2 and frame["caption_form"] == "ocr_only"
    return frame


def test_v7_line_exactly_40_chars_not_truncated() -> None:
    frame = _single_ocr_frame([_ocr_line(0, "x" * 40)])
    assert frame["ocr"]["excerpt_lines"] == ["x" * 40]
    assert frame["caption_text"] == "00:00:20采样帧。该帧OCR识别文本（前1行）：" + "x" * 40


def test_v8_line_41_chars_truncated_with_ellipsis() -> None:
    frame = _single_ocr_frame([_ocr_line(0, "y" * 41)])
    assert frame["ocr"]["excerpt_lines"] == ["y" * 40 + "…"]
    assert frame["caption_text"].endswith("y" * 40 + "…")


def test_v9_more_than_three_nonblank_lines_take_first_three_in_line_index_order() -> None:
    frame = _single_ocr_frame(
        [_ocr_line(2, "z"), _ocr_line(0, "a"), _ocr_line(1, "m"),
         _ocr_line(3, "p"), _ocr_line(4, "q")]
    )
    assert frame["ocr"]["excerpt_lines"] == ["a", "m", "z"]
    assert frame["ocr"]["excerpt_line_count"] == 3
    assert "（前3行）：a；m；z" in frame["caption_text"]


def test_v10_blank_lines_skipped_but_counted_for_ranking() -> None:
    frame = _single_ocr_frame(
        [_ocr_line(0, "  "), _ocr_line(1, "x"), _ocr_line(2, "   "),
         _ocr_line(3, "y"), _ocr_line(4, "z"), _ocr_line(5, "w")]
    )
    assert frame["ocr"]["excerpt_lines"] == ["x", "y", "z"]
    assert frame["caption_text"].startswith("00:00:20采样帧。")
    assert frame["caption_text"].endswith("（前3行）：x；y；z")


def test_v11_intra_line_newline_and_tab_replaced_per_char() -> None:
    frame = _single_ocr_frame([_ocr_line(0, "a\nb\tc"), _ocr_line(1, "  pad  ")])
    assert frame["ocr"]["excerpt_lines"] == ["a b c", "pad"]


def test_v12_joiner_is_fullwidth_semicolon() -> None:
    frame = _single_ocr_frame([_ocr_line(0, "一"), _ocr_line(1, "二")])
    assert frame["caption_text"].endswith("（前2行）：一；二")


def test_v13_fixture_captions_match_reference_byte_for_byte() -> None:
    fx = _fixture()
    selection = _build_fixture(fx)
    _assert_matches_reference(
        selection, fx["plan"], fx["timeline"], fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"]
    )
    frames = selection["chapters"][0]["frames"]
    by_id = {frame["frame_id"]: frame for frame in frames}
    assert by_id["fr000003"]["caption_text"] == "00:00:20采样帧。该帧OCR识别文本（前1行）：gamma"
    assert by_id["fr000004"]["caption_text"] == (
        "画面于00:00:30发生变化（相对前一采样帧的变化强度0.1000）。"
    )


# ---------------------------------------------------------------------------
# U矩阵：事件级交叉与文档一致性（裁决Q2.3/Q2.4＋实现决定4/12）
# ---------------------------------------------------------------------------


def _mismatch(fn: Any) -> str:
    with pytest.raises(cf.InputIdentityMismatchError) as excinfo:
        fn()
    return excinfo.value.error_code


def test_u_pe_field_by_field_mismatch() -> None:
    for field, value in (
        ("change_strength", 0.9),
        ("frame_id", "fr000005"),
        ("candidate_id", "pc999999"),
        ("perceptual_distance", 0.77),
    ):
        fx = _fixture()
        fx["events"][0]["payload"][field] = value
        assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH

    fx = _fixture()
    fx["events"][0]["start_ms"] = 11_000
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH


def test_u_oe_field_by_field_mismatch() -> None:
    for field, value in (
        ("text_sha256", HEX_A),
        ("frame_id", "fr000001"),
        ("candidate_id", "oc999999"),
    ):
        fx = _fixture()
        fx["events"][2]["payload"][field] = value
        assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH

    fx = _fixture()
    fx["events"][2]["start_ms"] = 999
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH


def test_u_oe_pointing_at_duplicate_or_blank_entry_rejected() -> None:
    fx = _fixture()
    fx["ocr_doc"]["frames"][0]["duplicate_of"] = "oc000002"
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH
    fx = _fixture()
    fx["ocr_doc"]["frames"][0]["canonical_text"] = ""
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH
    fx = _fixture()
    fx["ocr_doc"]["frames"][0]["status"] = "failed"
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH


def test_u_pe_without_pc_doc_is_missing_not_mismatch() -> None:
    fx = _fixture()
    with pytest.raises(cf.InputPageChangeSetMissingError) as excinfo:
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], None, fx["ocr_doc"], fx["refs"])
    assert excinfo.value.error_code == cf.ERROR_INPUT_PAGE_CHANGE_SET_MISSING
    with pytest.raises(cf.InputOcrSetMissingError) as excinfo:
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], fx["pc_doc"], None, fx["refs"])
    assert excinfo.value.error_code == cf.ERROR_INPUT_OCR_SET_MISSING


def test_u_pe_referencing_frame_outside_set() -> None:
    fx = _fixture()
    fx["events"][0]["payload"]["frame_id"] = "fr000009"
    fx["pc_doc"]["candidates"][0]["frame_id"] = "fr000009"
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH


def test_u_frame_double_referenced_by_same_modality() -> None:
    fx = _fixture()
    extra_candidate = _pc_candidate("pc000003", "fr000002", 10_000, 0.4, prev="fr000001")
    fx["pc_doc"]["candidates"].append(extra_candidate)
    fx["pc_doc"]["candidate_count"] += 1
    fx["events"].append(_pe_event("pe000003", extra_candidate))
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH


def test_u_plan_cross_checks_with_timeline_and_refs() -> None:
    fx = _fixture()
    fx["plan"]["source_timeline"]["timeline_id"] = "other"
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH

    fx = _fixture()
    fx["plan"]["duration_ms"] = 99_999
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH

    fx = _fixture()
    fx["plan"]["task_id"] = "T2"
    assert _mismatch(lambda: _build_fixture(fx)) == cf.ERROR_INPUT_IDENTITY_MISMATCH

    fx = _fixture()
    refs = dict(fx["refs"])
    refs["timeline_ref"] = {"result_id": "mismatched", "manifest_sha256": HEX_B}
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"], refs)


def test_u_upstream_task_mismatch_via_set_checks() -> None:
    fx = _fixture()
    doc = copy.deepcopy(fx["frame_doc"])
    doc["task_id"] = "T2"
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], fx["timeline"], doc, fx["pc_doc"], fx["ocr_doc"], fx["refs"])
    doc = copy.deepcopy(fx["ocr_doc"])
    doc["task_id"] = "T2"
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], fx["pc_doc"], doc, fx["refs"])


def test_u_set_document_corruption_shapes() -> None:
    fx = _fixture()
    doc = copy.deepcopy(fx["pc_doc"])
    doc["candidate_count"] = 5
    with pytest.raises(cf.InputPageChangeSetCorruptError) as excinfo:
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], doc, fx["ocr_doc"], fx["refs"])
    assert excinfo.value.error_code == cf.ERROR_INPUT_PAGE_CHANGE_SET_CORRUPT

    doc = copy.deepcopy(fx["pc_doc"])
    doc["extra_key"] = 1
    with pytest.raises(cf.InputPageChangeSetCorruptError):
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], doc, fx["ocr_doc"], fx["refs"])

    doc = copy.deepcopy(fx["frame_doc"])
    doc["frame_count"] = 7
    with pytest.raises(cf.InputFrameSetCorruptError) as excinfo:
        _build(fx["plan"], fx["timeline"], doc, fx["pc_doc"], fx["ocr_doc"], fx["refs"])
    assert excinfo.value.error_code == cf.ERROR_INPUT_FRAME_SET_CORRUPT

    doc = copy.deepcopy(fx["frame_doc"])
    doc["frames"].append(copy.deepcopy(doc["frames"][0]))
    doc["frame_count"] = len(doc["frames"])
    with pytest.raises(cf.InputFrameSetCorruptError):
        _build(fx["plan"], fx["timeline"], doc, fx["pc_doc"], fx["ocr_doc"], fx["refs"])

    doc = copy.deepcopy(fx["ocr_doc"])
    doc["frames"][0]["line_count"] = 9
    with pytest.raises(cf.InputOcrSetCorruptError) as excinfo:
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], fx["pc_doc"], doc, fx["refs"])
    assert excinfo.value.error_code == cf.ERROR_INPUT_OCR_SET_CORRUPT

    doc = copy.deepcopy(fx["ocr_doc"])
    doc["frames"][0]["lines"].append(_ocr_line(0, "dup-index"))
    doc["frames"][0]["line_count"] = len(doc["frames"][0]["lines"])
    doc["line_count"] = sum(int(entry["line_count"]) for entry in doc["frames"])
    with pytest.raises(cf.InputOcrSetCorruptError):
        _build(fx["plan"], fx["timeline"], fx["frame_doc"], fx["pc_doc"], doc, fx["refs"])


def test_u_modality_event_invariants() -> None:
    fx = _fixture()
    no_pe = [event for event in fx["events"] if event["type"] != "page_change"]
    timeline = _timeline_doc(no_pe, duration_ms=100_000, fc_status="available")
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], timeline, fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"], fx["refs"])

    fx = _fixture()
    no_oe = [event for event in fx["events"] if event["type"] != "ocr"]
    timeline = _timeline_doc(no_oe, duration_ms=100_000, ocr_status="available")
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], timeline, fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"], fx["refs"])

    fx = _fixture()
    timeline = _timeline_doc(fx["events"], duration_ms=100_000, fc_status="empty")
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], timeline, fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"], fx["refs"])

    fx = _fixture()
    timeline = _timeline_doc(fx["events"], duration_ms=100_000, ocr_status="empty")
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], timeline, fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"], fx["refs"])

    fx = _fixture()
    timeline = _timeline_doc(fx["events"], duration_ms=100_000, fc_status="bogus")
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], timeline, fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"], fx["refs"])

    fx = _fixture()
    timeline = _timeline_doc(fx["events"], duration_ms=100_000, ocr_status="degraded")
    with pytest.raises(cf.InputIdentityMismatchError):
        _build(fx["plan"], timeline, fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"], fx["refs"])


def test_u_degraded_ocr_failed_modality_is_consumed_and_recorded() -> None:
    fx = _fixture()
    fx["timeline"]["modalities"]["ocr"]["status"] = "failed"
    fx["timeline"]["modalities"]["ocr"]["reason_codes"] = ["recognition_failed"]
    fx["ocr_doc"]["status"] = "degraded"
    selection = _build_fixture(fx)
    assert selection["ocr_modality_status"] == "failed"
    assert selection["source_ocr_set"]["status"] == "degraded"
    assert selection["source_ocr_set"]["frame_count"] == 2
    _assert_matches_reference(
        selection, fx["plan"], fx["timeline"], fx["frame_doc"], fx["pc_doc"], fx["ocr_doc"]
    )


def test_u_present_but_ignored_sets_yield_null_blocks() -> None:
    fx = _fixture()
    events = [event for event in fx["events"] if event["type"] == "page_change"]
    fx["timeline"] = _timeline_doc(events, duration_ms=100_000, ocr_status="disabled")
    selection = _build_fixture(fx)
    assert selection["source_ocr_set"] is None
    assert selection["ocr_modality_status"] == "disabled"
    assert selection["source_page_change_set"] is not None
    _, cache_sha = cf.chapter_frames_cache_identity(
        task_id="T1",
        plan_ref=fx["refs"]["plan_ref"],
        timeline_ref=fx["refs"]["timeline_ref"],
        frame_set_ref=fx["refs"]["frame_set_ref"],
        page_change_set_ref=fx["refs"]["page_change_set_ref"],
        ocr_set_ref=None,
    )
    from_document = cf._cache_identity_from_document(selection)
    assert canonical_json_sha256(from_document) == cache_sha
    assert from_document["ocr_set_id"] is None and from_document["ocr_set_manifest_sha256"] is None


# ---------------------------------------------------------------------------
# E/N矩阵补充：选择对象自校验与组盘复验单元（裁决Q3/Q4.7）
# ---------------------------------------------------------------------------


def _valid_selection() -> dict[str, Any]:
    return _build_fixture(_fixture())


def _mutate_raise(mutate: Any) -> str:
    doc = copy.deepcopy(_valid_selection())
    mutate(doc)
    with pytest.raises(cf.SelectionValidationFailedError) as excinfo:
        cf._validate_selection_document(doc)
    return excinfo.value.error_code


def test_e10_caption_text_mutation_rejected() -> None:
    def mutate(doc: dict[str, Any]) -> None:
        doc["chapters"][0]["frames"][0]["caption_text"] += "自由改写"

    assert _mutate_raise(mutate) == cf.ERROR_SELECTION_VALIDATION_FAILED


def test_e11_totals_mutation_rejected() -> None:
    assert _mutate_raise(
        lambda doc: doc["totals"].__setitem__("selected_frame_count", 99)
    ) == cf.ERROR_SELECTION_VALIDATION_FAILED
    assert _mutate_raise(
        lambda doc: doc["totals"]["tier_counts"].__setitem__("tier1", 7)
    ) == cf.ERROR_SELECTION_VALIDATION_FAILED


def test_e12_rank_and_index_violations_rejected() -> None:
    def dup_rank(doc: dict[str, Any]) -> None:
        doc["chapters"][0]["frames"][1]["rank"] = doc["chapters"][0]["frames"][0]["rank"]

    assert _mutate_raise(dup_rank) == cf.ERROR_SELECTION_VALIDATION_FAILED

    def bad_index(doc: dict[str, Any]) -> None:
        doc["chapters"][0]["chapter_index"] = 5

    assert _mutate_raise(bad_index) == cf.ERROR_SELECTION_VALIDATION_FAILED


def test_e13_decoded_outside_chapter_rejected() -> None:
    doc = copy.deepcopy(_valid_selection())
    doc["chapters"][0]["frames"][0]["decoded_ms"] = int(doc["duration_ms"])
    with pytest.raises(cf.SelectionValidationFailedError):
        cf._validate_selection_document(doc)


def test_e14_chapter_boundary_invariants_rejected() -> None:
    def first_start(doc: dict[str, Any]) -> None:
        doc["chapters"][0]["start_ms"] = 1

    assert _mutate_raise(first_start) == cf.ERROR_SELECTION_VALIDATION_FAILED

    def last_end(doc: dict[str, Any]) -> None:
        doc["chapters"][-1]["end_ms"] = int(doc["duration_ms"]) - 1

    assert _mutate_raise(last_end) == cf.ERROR_SELECTION_VALIDATION_FAILED


def test_e15_tier_form_evidence_mapping_rejected() -> None:
    def bad_tier(doc: dict[str, Any]) -> None:
        doc["chapters"][0]["frames"][0]["tier"] = 5

    assert _mutate_raise(bad_tier) == cf.ERROR_SELECTION_VALIDATION_FAILED

    def bad_form(doc: dict[str, Any]) -> None:
        doc["chapters"][0]["frames"][0]["caption_form"] = "pe_only"

    assert _mutate_raise(bad_form) == cf.ERROR_SELECTION_VALIDATION_FAILED

    def empty_excerpt(doc: dict[str, Any]) -> None:
        frame = doc["chapters"][0]["frames"][0]
        frame["ocr"]["excerpt_lines"] = []
        frame["ocr"]["excerpt_line_count"] = 0

    assert _mutate_raise(empty_excerpt) == cf.ERROR_SELECTION_VALIDATION_FAILED


def test_e16_result_source_pair_and_extra_key_rejected() -> None:
    assert _mutate_raise(
        lambda doc: doc["source_plan"].__setitem__("quality_level", "degraded")
    ) == cf.ERROR_SELECTION_VALIDATION_FAILED
    assert _mutate_raise(lambda doc: doc.__setitem__("extra", 1)) == (
        cf.ERROR_SELECTION_VALIDATION_FAILED
    )


def test_n1_legal_shapes_validate_clean() -> None:
    cf._validate_selection_document(_valid_selection())
    empty = _scene(
        frames=[],
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
        fc="empty", ocr="empty",
    )
    cf._validate_selection_document(empty)
    emergency = _build_fixture(
        {**_fixture(), "plan": _plan_doc(
            [(0, "完整内容", None)], duration_ms=100_000,
            result_source="emergency", quality_level="degraded",
        )}
    )
    cf._validate_selection_document(emergency)


def test_n2_tier4_time_only_is_legal() -> None:
    selection = _scene(
        frames=_frames(("fr000002", 45_000)),
        plan=_plan_doc([(0, "第一章", None)], duration_ms=100_000),
        fc="empty", ocr="disabled",
    )
    frame = selection["chapters"][0]["frames"][0]
    assert frame["tier"] == 4 and frame["caption_form"] == "time_only"
    assert frame["page_change"] is None and frame["ocr"] is None


# ---------------------------------------------------------------------------
# 缓存身份与组盘复验单元（裁决Q5.6/Q4.7）
# ---------------------------------------------------------------------------


def test_p1_cache_identity_exact_keyset() -> None:
    fx = _fixture()
    payload, sha = cf.chapter_frames_cache_identity(
        task_id="T1",
        plan_ref=fx["refs"]["plan_ref"],
        timeline_ref=fx["refs"]["timeline_ref"],
        frame_set_ref=fx["refs"]["frame_set_ref"],
        page_change_set_ref=fx["refs"]["page_change_set_ref"],
        ocr_set_ref=fx["refs"]["ocr_set_ref"],
    )
    assert len(payload) == 19
    assert canonical_json_sha256(payload) == sha


def test_p2_every_ref_dimension_changes_sha() -> None:
    fx = _fixture()
    hex_c = "c" * 64

    def sha_of(**overrides: Any) -> str:
        refs = copy.deepcopy(fx["refs"])
        for key, (field, value) in overrides.items():
            refs[key][field] = value
        return cf.chapter_frames_cache_identity(
            task_id="T1",
            plan_ref=refs["plan_ref"],
            timeline_ref=refs["timeline_ref"],
            frame_set_ref=refs["frame_set_ref"],
            page_change_set_ref=refs["page_change_set_ref"],
            ocr_set_ref=refs["ocr_set_ref"],
        )[1]

    base = sha_of()
    changed = [
        sha_of(plan_ref=("result_id", "chapter-plan-2")),
        sha_of(plan_ref=("manifest_sha256", hex_c)),
        sha_of(timeline_ref=("result_id", "unified-timeline-2")),
        sha_of(timeline_ref=("manifest_sha256", hex_c)),
        sha_of(frame_set_ref=("result_id", "frame-set-2")),
        sha_of(frame_set_ref=("manifest_sha256", hex_c)),
        sha_of(page_change_set_ref=("result_id", "page-change-set-2")),
        sha_of(page_change_set_ref=("manifest_sha256", hex_c)),
        sha_of(ocr_set_ref=("result_id", "ocr-set-2")),
        sha_of(ocr_set_ref=("manifest_sha256", hex_c)),
    ]
    assert base not in changed
    assert len(set(changed)) == len(changed)


def test_p3_unconsumed_domains_are_null_and_differ_from_present() -> None:
    fx = _fixture()
    stripped = cf.chapter_frames_cache_identity(
        task_id="T1",
        plan_ref=fx["refs"]["plan_ref"],
        timeline_ref=fx["refs"]["timeline_ref"],
        frame_set_ref=fx["refs"]["frame_set_ref"],
    )
    again = cf.chapter_frames_cache_identity(
        task_id="T1",
        plan_ref=fx["refs"]["plan_ref"],
        timeline_ref=fx["refs"]["timeline_ref"],
        frame_set_ref=fx["refs"]["frame_set_ref"],
    )
    assert stripped[0]["page_change_set_id"] is None
    assert stripped[0]["page_change_set_manifest_sha256"] is None
    assert stripped[0]["ocr_set_id"] is None
    assert stripped[0]["ocr_set_manifest_sha256"] is None
    assert stripped[1] == again[1]
    full = cf.chapter_frames_cache_identity(
        task_id="T1",
        plan_ref=fx["refs"]["plan_ref"],
        timeline_ref=fx["refs"]["timeline_ref"],
        frame_set_ref=fx["refs"]["frame_set_ref"],
        page_change_set_ref=fx["refs"]["page_change_set_ref"],
        ocr_set_ref=fx["refs"]["ocr_set_ref"],
    )
    assert full[1] != stripped[1]


def test_p4_result_id_is_content_hash_and_title_changes_it() -> None:
    rid = cf._compute_result_id(_valid_selection())
    assert rid.startswith(cf.RESULT_ID_PREFIX)
    suffix = rid[len(cf.RESULT_ID_PREFIX):]
    assert len(suffix) == 12
    int(suffix, 16)
    fx = _fixture()
    fx["plan"] = _plan_doc([(0, "改了标题", None)], duration_ms=100_000)
    other = cf._compute_result_id(_build_fixture(fx))
    assert other != rid


def _write_group(base: Path, selection: dict[str, Any]) -> tuple[Path, str, str]:
    result_id = cf._compute_result_id(selection)
    cache_sha = canonical_json_sha256(cf._cache_identity_from_document(selection))
    selection_bytes = canonical_json_bytes(selection)
    manifest = cf._build_manifest(
        selection, result_id=result_id, cache_sha256=cache_sha,
        selection_bytes=selection_bytes,
    )
    group = base / cf.GROUP_DIRECTORY / result_id
    group.mkdir(parents=True)
    with (group / cf.SELECTION_FILE).open("wb") as stream:
        stream.write(selection_bytes)
    with (group / cf.MANIFEST_FILE).open("wb") as stream:
        stream.write(cf._json_bytes(manifest))
    return group, result_id, cache_sha


def test_p5_group_roundtrip_and_null_domain_manifest(tmp_path: Path) -> None:
    fx = _fixture()
    events = [e for e in fx["events"] if e["type"] == "page_change"]
    fx["timeline"] = _timeline_doc(events, duration_ms=100_000, ocr_status="disabled")
    selection = _build_fixture(fx)
    group, result_id, cache_sha = _write_group(tmp_path, selection)
    loaded, manifest = cf.validate_chapter_frame_selection_group(
        group, cache_sha256=cache_sha, expected_result_id=result_id
    )
    assert loaded == selection
    assert manifest["result_id"] == result_id
    assert set(manifest["files"]) == {cf.SELECTION_FILE}
    assert manifest["inputs"]["ocr_set_id"] is None
    assert manifest["inputs"]["ocr_set_manifest_sha256"] is None
    assert manifest["inputs"]["page_change_set_id"] == "page-change-set-1"


def test_p6_group_name_mismatch_is_cache_corrupt(tmp_path: Path) -> None:
    group, _, cache_sha = _write_group(tmp_path, _valid_selection())
    moved = group.parent / "renamed-group"
    group.rename(moved)
    with pytest.raises(cf.CacheCorruptError) as excinfo:
        cf.validate_chapter_frame_selection_group(moved, cache_sha256=cache_sha)
    assert excinfo.value.error_code == cf.ERROR_CACHE_CORRUPT


def test_p7_tampered_bytes_map_by_mode(tmp_path: Path) -> None:
    group, result_id, cache_sha = _write_group(tmp_path, _valid_selection())
    path = group / cf.SELECTION_FILE
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(cf.CacheCorruptError):
        cf.validate_chapter_frame_selection_group(
            group, cache_sha256=cache_sha, expected_result_id=result_id
        )
    with pytest.raises(cf.SelectionValidationFailedError):
        cf.validate_chapter_frame_selection_group(
            group,
            cache_sha256=cache_sha,
            expected_result_id=result_id,
            publish_mode=True,
        )


def test_p8_missing_selection_file_is_cache_corrupt(tmp_path: Path) -> None:
    group, _, cache_sha = _write_group(tmp_path, _valid_selection())
    (group / cf.SELECTION_FILE).unlink()
    with pytest.raises(cf.CacheCorruptError):
        cf.validate_chapter_frame_selection_group(group, cache_sha256=cache_sha)


# ---------------------------------------------------------------------------
# P矩阵：真实合成链上的发布工程（裁决Q5.1—Q5.5；14-B先例逐字对齐）
# ---------------------------------------------------------------------------

from tests.test_boundary_planner_v2 import ALT_RESPONSE


def _current_path(root: Path) -> Path:
    return _own_root(root, TASK) / cf.CURRENT_FILE


def _read_current(root: Path) -> dict[str, Any]:
    return json.loads(_current_path(root).read_text(encoding="utf-8"))


def _chain_docs(root: Path) -> tuple[Any, ...]:
    up = cf._load_upstream(root, TASK)
    return up, up.plan, up.timeline, up.frame_doc, up.page_change_doc, up.ocr_doc


def test_p_chain_first_publish(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    up, plan, timeline, frame_doc, pc_doc, ocr_doc = _chain_docs(root)
    result = cf.select_chapter_frames(TASK, output_root=root)
    assert result.cache_hit is False and result.pointer_repaired is False
    assert result.result_id == cf._compute_result_id(result.selection)
    assert result.chapter_count == len(plan["boundaries"])
    names = sorted(path.name for path in result.group_path.iterdir())
    assert names == sorted([cf.SELECTION_FILE, cf.MANIFEST_FILE])
    current = _read_current(root)
    assert set(current) == {
        "schema_name", "schema_version", "artifact_type", "result_id", "group",
        "manifest_sha256",
    }
    assert current["artifact_type"] == cf.ARTIFACT_TYPE
    assert current["result_id"] == result.result_id
    assert current["group"] == f"{cf.GROUP_DIRECTORY}/{result.result_id}"
    assert current["manifest_sha256"] == hashlib.sha256(
        (result.group_path / cf.MANIFEST_FILE).read_bytes()
    ).hexdigest()
    cache_sha = canonical_json_sha256(cf._cache_identity_from_document(result.selection))
    assert result.manifest["cache_identity_sha256"] == cache_sha
    _assert_matches_reference(result.selection, plan, timeline, frame_doc, pc_doc, ocr_doc)
    assert up.plan["task_id"] == TASK
    assert cf.load_published_chapter_frame_selection(root, TASK) == result.selection


def test_p_chain_cache_hit_zero_rewrite(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    first = cf.select_chapter_frames(TASK, output_root=root)
    snapshot = _tree_sha_pairs(root)
    second = cf.select_chapter_frames(TASK, output_root=root)
    assert second.cache_hit is True and second.pointer_repaired is False
    assert second.result_id == first.result_id
    assert _tree_sha_pairs(root) == snapshot
    assert not _failures_dir(root).exists()


def test_p_chain_pointer_only_repair(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    first = cf.select_chapter_frames(TASK, output_root=root)
    current_path = _current_path(root)
    current_path.unlink()
    group_snapshot = _tree_sha_pairs(first.group_path)
    second = cf.select_chapter_frames(TASK, output_root=root)
    assert second.cache_hit is True and second.pointer_repaired is True
    assert second.result_id == first.result_id
    assert _tree_sha_pairs(first.group_path) == group_snapshot
    assert _read_current(root)["result_id"] == first.result_id
    assert not _failures_dir(root).exists()


def test_p_chain_upstream_change_republishes_new_group(tmp_path: Path) -> None:
    # 14-B先例（test_boundary_planner_v2 双身份重发布）：plan的缓存身份只含请求侧
    # （registry/timeline），必须变更model_revision才能让ALT_RESPONSE真正生效。
    root = _chain(tmp_path)
    first = cf.select_chapter_frames(TASK, output_root=root)
    old_snapshot = _tree_sha_pairs(first.group_path)
    registry = _registry(
        tmp_path, mutate_role=_mutate_role("model_revision", "rev-2"), name="rev2.json")
    _plan(root, ScriptedTransport([ALT_RESPONSE]), registry=registry, tmp_path=tmp_path)
    second = cf.select_chapter_frames(TASK, output_root=root)
    assert second.cache_hit is False
    assert second.result_id != first.result_id
    assert _tree_sha_pairs(first.group_path) == old_snapshot
    assert _read_current(root)["result_id"] == second.result_id
    up, plan, timeline, frame_doc, pc_doc, ocr_doc = _chain_docs(root)
    _assert_matches_reference(second.selection, plan, timeline, frame_doc, pc_doc, ocr_doc)


def test_p_chain_tampered_current_group_is_cache_corrupt(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    first = cf.select_chapter_frames(TASK, output_root=root)
    target = first.group_path / cf.SELECTION_FILE
    target.write_bytes(target.read_bytes() + b" ")
    current_bytes = _current_path(root).read_bytes()
    with pytest.raises(cf.CacheCorruptError):
        cf.select_chapter_frames(TASK, output_root=root)
    assert _current_path(root).read_bytes() == current_bytes
    assert target.read_bytes().endswith(b" ")
    audit = _only_failure(root)
    assert audit["stage"] == "current_validation"
    assert audit["error_code"] == cf.ERROR_CACHE_CORRUPT
    assert audit["previous_current_id"] == first.result_id
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "not_created"


def test_p_chain_same_id_different_bytes_never_rewritten(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    up = cf._load_upstream(root, TASK)
    selection = cf.build_chapter_frame_selection(
        up.plan, up.timeline, up.frame_doc, up.page_change_doc, up.ocr_doc,
        task_id=TASK, plan_ref=up.plan_pointer, timeline_ref=up.timeline_pointer,
        frame_set_ref=up.frame_pointer, page_change_set_ref=up.page_change_pointer,
        ocr_set_ref=up.ocr_pointer,
    )
    result_id = cf._compute_result_id(selection)
    junk_group = _own_root(root, TASK) / cf.GROUP_DIRECTORY / result_id
    junk_group.mkdir(parents=True)
    junk = b'{"tampered": true}\n'
    (junk_group / cf.SELECTION_FILE).write_bytes(junk)
    with pytest.raises(cf.CacheCorruptError):
        cf.select_chapter_frames(TASK, output_root=root)
    assert (junk_group / cf.SELECTION_FILE).read_bytes() == junk
    assert not _current_path(root).exists()
    audit = _only_failure(root)
    assert audit["stage"] == "group_publish"
    assert audit["error_code"] == cf.ERROR_CACHE_CORRUPT
    assert audit["temporary_cleanup_status"] == "not_created"


def test_p_chain_concurrent_run_zero_writes(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    locks = root / TASK / "m2" / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock_path = locks / cf.LOCK_FILE
    lock_path.write_bytes(b'{"holder": "other"}\n')
    with pytest.raises(cf.ConcurrentRunError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_CONCURRENT_RUN
    assert excinfo.value.recoverable is True
    assert not _own_root(root, TASK).exists()
    assert not _failures_dir(root).exists()
    assert lock_path.read_bytes() == b'{"holder": "other"}\n'


def test_p_inject_after_inputs_zero_artifact(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    with pytest.raises(cf.InjectedChapterFramesFailure):
        cf.select_chapter_frames(TASK, output_root=root, failure_injection="after_inputs")
    assert not _own_root(root, TASK).exists()
    audit = _only_failure(root)
    assert audit["stage"] == "prepare"
    assert audit["error_code"] == cf.ERROR_PUBLISH_FAILED
    assert audit["temporary_cleanup_status"] == "not_created"
    assert audit["current_unchanged"] is True
    assert audit["business_call_count"] == 0 and audit["supplier_request_count"] == 0
    assert audit["cache_identity_sha256"] and isinstance(audit["cache_identity_sha256"], str)
    assert set(audit["input_ids"]) == {
        "task_id", "plan_result_id", "timeline_id", "frame_set_id",
        "page_change_set_id", "ocr_set_id", "result_id",
    }


def test_p_inject_before_group_publish_temp_removed(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    with pytest.raises(cf.InjectedChapterFramesFailure):
        cf.select_chapter_frames(
            TASK, output_root=root, failure_injection="before_group_publish")
    groups = _own_root(root, TASK) / cf.GROUP_DIRECTORY
    assert groups.is_dir() and list(groups.iterdir()) == []
    assert not _current_path(root).exists()
    audit = _only_failure(root)
    assert audit["stage"] == "group_publish"
    assert audit["temporary_cleanup_status"] == "temp_removed"


def test_p_inject_after_group_publish_group_removed(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    with pytest.raises(cf.InjectedChapterFramesFailure):
        cf.select_chapter_frames(
            TASK, output_root=root, failure_injection="after_group_publish")
    groups = _own_root(root, TASK) / cf.GROUP_DIRECTORY
    assert groups.is_dir() and list(groups.iterdir()) == []
    assert not _current_path(root).exists()
    audit = _only_failure(root)
    assert audit["stage"] == "group_publish"
    assert audit["temporary_cleanup_status"] == "group_removed"


def test_p_inject_before_current_group_removed(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    with pytest.raises(cf.InjectedChapterFramesFailure):
        cf.select_chapter_frames(TASK, output_root=root, failure_injection="before_current")
    groups = _own_root(root, TASK) / cf.GROUP_DIRECTORY
    assert groups.is_dir() and list(groups.iterdir()) == []
    audit = _only_failure(root)
    assert audit["stage"] == "current_publish"
    assert audit["temporary_cleanup_status"] == "group_removed"


def test_p_keyboard_interrupt_publish_failed_audit(tmp_path: Path, monkeypatch) -> None:
    root = _chain(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cf, "_replace_current", _boom)
    with pytest.raises(KeyboardInterrupt):
        cf.select_chapter_frames(TASK, output_root=root)
    groups = _own_root(root, TASK) / cf.GROUP_DIRECTORY
    assert groups.is_dir() and list(groups.iterdir()) == []
    assert not _current_path(root).exists()
    audit = _only_failure(root)
    assert audit["stage"] == "current_publish"
    assert audit["error_code"] == cf.ERROR_PUBLISH_FAILED
    assert audit["temporary_cleanup_status"] == "group_removed"


def test_p_unparseable_current_keeps_group(tmp_path: Path, monkeypatch) -> None:
    root = _chain(tmp_path)
    own = _own_root(root, TASK)

    def _corrupt_then_interrupt(root_arg: Path, payload: Any) -> None:
        (root_arg / cf.CURRENT_FILE).write_bytes(b"not-json\n")
        raise KeyboardInterrupt

    monkeypatch.setattr(cf, "_replace_current", _corrupt_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        cf.select_chapter_frames(TASK, output_root=root)
    groups = own / cf.GROUP_DIRECTORY
    surviving = list(groups.iterdir())
    assert len(surviving) == 1 and surviving[0].name.startswith(cf.RESULT_ID_PREFIX)
    assert (own / cf.CURRENT_FILE).read_bytes() == b"not-json\n"
    audit = _only_failure(root)
    assert audit["stage"] == "current_publish"


def test_p_chain_emergency_plan_single_chapter(tmp_path: Path) -> None:
    root = _synth_chain(tmp_path, m1_events=HAPPY_EVENTS, duration_seconds=600.0)
    initialize_task_workspace(root, TASK)
    build_timeline_v2(TASK, output_root=root, ocr="auto")
    _plan(root, ScriptedTransport([RuntimeError("boom-1"), RuntimeError("boom-2")]),
          tmp_path=tmp_path)
    plan_pointer = load_chapter_plan_current(root, TASK)
    assert plan_pointer is not None
    result = cf.select_chapter_frames(TASK, output_root=root)
    assert result.chapter_count == 1
    assert result.selection["source_plan"]["result_source"] == "emergency"
    assert result.selection["source_plan"]["quality_level"] == "degraded"
    assert result.selection["totals"]["selected_frame_count"] <= cf.MAX_FRAMES_PER_CHAPTER
    up, plan, timeline, frame_doc, pc_doc, ocr_doc = _chain_docs(root)
    _assert_matches_reference(result.selection, plan, timeline, frame_doc, pc_doc, ocr_doc)


def test_p_load_published_none_without_current(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    assert cf.load_published_chapter_frame_selection(root, TASK) is None
    cf.select_chapter_frames(TASK, output_root=root)
    assert cf.load_published_chapter_frame_selection(root, TASK) is not None


def test_p_unknown_injection_point_rejected(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    with pytest.raises(cf.ChapterFramesError):
        cf.select_chapter_frames(TASK, output_root=root, failure_injection="before_model")
    assert not _own_root(root, TASK).exists()


# ---------------------------------------------------------------------------
# I矩阵：输入身份/缺失/损坏与锁前零写入（裁决Q2.1/Q5.8）
# ---------------------------------------------------------------------------

import os


def test_i_task_json_missing_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    with pytest.raises(cf.InputTaskMissingError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_TASK_MISSING
    assert not (root / TASK).exists()


def test_i_task_json_identity_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    initialize_task_workspace(root, TASK)
    task_json = root / TASK / "task.json"
    record = json.loads(task_json.read_text(encoding="utf-8"))
    record["task_id"] = "OTHER99"
    with task_json.open("w", encoding="utf-8", newline="") as stream:
        json.dump(record, stream, ensure_ascii=False)
    with pytest.raises(cf.InputTaskMissingError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_TASK_MISSING
    assert not (root / TASK / "m2" / cf.ROOT_DIRECTORY).exists()


def test_i_invalid_task_id_rejected_before_any_read(tmp_path: Path) -> None:
    from video_doc.task_workspace import TaskWorkspaceError

    root = tmp_path / "outputs"
    with pytest.raises(TaskWorkspaceError):
        cf.select_chapter_frames("bad id", output_root=root)
    assert not (root / "bad id").exists()


def _chain_without(tmp_path: Path, *, plan: bool, timeline: bool) -> Path:
    root = _synth_chain(tmp_path, m1_events=HAPPY_EVENTS, duration_seconds=600.0)
    initialize_task_workspace(root, TASK)
    if timeline:
        build_timeline_v2(TASK, output_root=root, ocr="auto")
    if plan:
        _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    return root


def test_i_plan_missing_zero_writes(tmp_path: Path) -> None:
    root = _chain_without(tmp_path, plan=False, timeline=True)
    with pytest.raises(cf.InputChapterPlanMissingError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_CHAPTER_PLAN_MISSING
    assert excinfo.value.domain == "boundaries"
    assert excinfo.value.next_action == "run_plan_chapters_v2"
    assert not _own_root(root, TASK).exists()
    assert not _failures_dir(root).exists()


def test_i_timeline_missing(tmp_path: Path) -> None:
    root = _chain_without(tmp_path, plan=False, timeline=False)
    with pytest.raises(cf.InputTimelineMissingError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_TIMELINE_MISSING
    assert excinfo.value.domain == "timelines"
    assert not _own_root(root, TASK).exists()


def test_i_frames_current_missing(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    m2 = _current_path(root).parents[1]
    (m2 / cf.FRAME_MODULE_TOKEN / cf.CURRENT_FILE).unlink()
    with pytest.raises(cf.InputFrameSetMissingError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_FRAME_SET_MISSING
    assert excinfo.value.domain == "frames"
    assert not _own_root(root, TASK).exists()
    assert not _failures_dir(root).exists()


def test_i_plan_current_garbage_is_corrupt(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    boundaries_current = _current_path(root).parents[1] / "boundaries" / cf.CURRENT_FILE
    original = boundaries_current.read_bytes()
    boundaries_current.write_bytes(b"{not json\n")
    with pytest.raises(cf.InputChapterPlanCorruptError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_CHAPTER_PLAN_CORRUPT
    assert excinfo.value.domain == "boundaries"
    boundaries_current.write_bytes(original)
    assert cf.load_chapter_plan_current(root, TASK) is not None


def _tamper_group_doc(root: Path, domain: Path, pointer: dict[str, Any], doc_name: str) -> None:
    group = domain / cf.GROUP_DIRECTORY / str(pointer["result_id"])
    path = group / doc_name
    path.write_bytes(path.read_bytes() + b" ")


def test_i_frame_doc_tampered_is_corrupt(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    frames_root = _current_path(root).parents[1] / cf.FRAME_MODULE_TOKEN
    pointer = json.loads(
        (frames_root / cf.CURRENT_FILE).read_text(encoding="utf-8"))
    _tamper_group_doc(root, frames_root, pointer, "frame-set.json")
    with pytest.raises(cf.InputFrameSetCorruptError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_FRAME_SET_CORRUPT
    assert excinfo.value.domain == "frames"


def test_i_timeline_ocr_off_success_ignores_ocr_domain(tmp_path: Path) -> None:
    # Timeline为模态唯一权威：ocr=off必须在plan发布前生效——事后重建timeline会改变
    # 其result_id，使既有plan的source_timeline引用在交叉校验中判为CACHE_CORRUPT。
    root = _synth_chain(tmp_path, m1_events=HAPPY_EVENTS, duration_seconds=600.0)
    initialize_task_workspace(root, TASK)
    build_timeline_v2(TASK, output_root=root, ocr="off")
    _plan(root, ScriptedTransport([GOOD_RESPONSE]), tmp_path=tmp_path)
    ocr_current = _current_path(root).parents[1] / cf.OCR_ROOT_DIRECTORY / cf.CURRENT_FILE
    original = ocr_current.read_bytes()
    result = cf.select_chapter_frames(TASK, output_root=root)
    assert result.cache_hit is False
    assert result.selection["source_ocr_set"] is None
    assert result.selection["ocr_modality_status"] == "disabled"
    assert all(
        frame["ocr"] is None
        for chapter in result.selection["chapters"] for frame in chapter["frames"]
    )
    assert ocr_current.read_bytes() == original
    up, plan, timeline, frame_doc, pc_doc, ocr_doc = _chain_docs(root)
    assert ocr_doc is None
    _assert_matches_reference(result.selection, plan, timeline, frame_doc, pc_doc, ocr_doc)


def test_i_pe_present_but_page_changes_current_missing(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    from video_doc.timeline_builder_v2 import load_published_timeline

    timeline = load_published_timeline(root, TASK)
    assert timeline is not None
    assert any(event["type"] == "page_change" for event in timeline["events"]), (
        "合成链必须包含pe事件，否则该用例失去意义"
    )
    pc_root = _current_path(root).parents[1] / cf.FRAME_MODULE_TOKEN / cf.PAGE_CHANGE_ROOT_DIRECTORY
    (pc_root / cf.CURRENT_FILE).unlink()
    with pytest.raises(cf.InputPageChangeSetMissingError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_PAGE_CHANGE_SET_MISSING
    assert excinfo.value.domain == "page-changes"


def test_i_oe_present_but_ocr_current_missing(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    from video_doc.timeline_builder_v2 import load_published_timeline

    timeline = load_published_timeline(root, TASK)
    assert timeline is not None
    assert any(event["type"] == "ocr" for event in timeline["events"]), (
        "合成链必须包含oe事件，否则该用例失去意义"
    )
    ocr_root = _current_path(root).parents[1] / cf.OCR_ROOT_DIRECTORY
    (ocr_root / cf.CURRENT_FILE).unlink()
    with pytest.raises(cf.InputOcrSetMissingError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_INPUT_OCR_SET_MISSING
    assert excinfo.value.domain == "ocr"


def test_i_own_current_garbage_keeps_group(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    first = cf.select_chapter_frames(TASK, output_root=root)
    _current_path(root).write_bytes(b"~~~\n")
    with pytest.raises(cf.CurrentPointerCorruptError) as excinfo:
        cf.select_chapter_frames(TASK, output_root=root)
    assert excinfo.value.error_code == cf.ERROR_CURRENT_POINTER_CORRUPT
    assert excinfo.value.domain == cf.ROOT_DIRECTORY
    assert first.group_path.is_dir()
    audit = _only_failure(root)
    assert audit["stage"] == "current_validation"
    assert audit["error_code"] == cf.ERROR_CURRENT_POINTER_CORRUPT
    assert audit["current_unchanged"] is True


def test_i_current_symlink_hard_fails(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    first = cf.select_chapter_frames(TASK, output_root=root)
    own = _own_root(root, TASK)
    current = own / cf.CURRENT_FILE
    try:
        os.symlink(first.group_path / cf.MANIFEST_FILE, current)
    except (OSError, NotImplementedError):
        pytest.skip("本机无符号链接权限（预期skip）")
    with pytest.raises(cf.CurrentPointerCorruptError):
        cf.select_chapter_frames(TASK, output_root=root)


# ---------------------------------------------------------------------------
# C矩阵：CLI接线（裁决Q5.7；单条JSON、退出码0/1/2、KI/SX原样传播）
# ---------------------------------------------------------------------------

_CLI_OK_KEYS = {
    "task_id", "module", "status", "result_id", "chapter_count",
    "selected_frame_count", "chapters_without_frames", "cache_hit",
    "pointer_repaired", "group_relative_path",
}
_CLI_FAIL_KEYS = {
    "task_id", "module", "status", "error_code", "safe_message",
    "recoverable", "next_action", "domain",
}


def _cli_status(payload_text: str) -> dict[str, Any]:
    return json.loads(payload_text)


def test_c_success_stdout_and_cache_replay(tmp_path: Path, capsys) -> None:
    root = _chain(tmp_path)
    argv = ["select-chapter-frames", TASK, "--output-root", str(root)]
    assert main(argv) == 0
    ok = _cli_status(capsys.readouterr().out)
    assert set(ok) == _CLI_OK_KEYS
    assert ok["module"] == cf.MODULE_TOKEN and ok["status"] == "success"
    assert ok["cache_hit"] is False and ok["pointer_repaired"] is False
    assert ok["group_relative_path"] == (
        f"m2/{cf.ROOT_DIRECTORY}/{cf.GROUP_DIRECTORY}/{ok['result_id']}"
    )
    assert main(argv) == 0
    replay = _cli_status(capsys.readouterr().out)
    assert replay["cache_hit"] is True and replay["result_id"] == ok["result_id"]


def test_c_plan_missing_failure_json(tmp_path: Path, capsys) -> None:
    root = _chain_without(tmp_path, plan=False, timeline=True)
    assert main(["select-chapter-frames", TASK, "--output-root", str(root)]) == 1
    err = _cli_status(capsys.readouterr().err)
    assert set(err) == _CLI_FAIL_KEYS
    assert err["module"] == cf.MODULE_TOKEN and err["status"] == "failed"
    assert err["error_code"] == cf.ERROR_INPUT_CHAPTER_PLAN_MISSING
    assert err["domain"] == "boundaries"
    assert err["recoverable"] is True and err["next_action"] == "run_plan_chapters_v2"
    assert str(root) not in err["safe_message"]


def test_c_usage_error_exit_2(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["select-chapter-frames"])
    assert excinfo.value.code == 2


def test_c_keyboard_interrupt_propagates(tmp_path: Path, monkeypatch) -> None:
    root = _chain(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("video_doc.cli.select_chapter_frames", _boom)
    with pytest.raises(KeyboardInterrupt):
        main(["select-chapter-frames", TASK, "--output-root", str(root)])


def test_c_current_corrupt_domain_exposed(tmp_path: Path, capsys) -> None:
    root = _chain(tmp_path)
    cf.select_chapter_frames(TASK, output_root=root)
    _current_path(root).write_bytes(b"garbage\n")
    capsys.readouterr()
    assert main(["select-chapter-frames", TASK, "--output-root", str(root)]) == 1
    err = _cli_status(capsys.readouterr().err)
    assert err["error_code"] == cf.ERROR_CURRENT_POINTER_CORRUPT
    assert err["domain"] == cf.ROOT_DIRECTORY


# ---------------------------------------------------------------------------
# DEV01真实沙箱双形态e2e＋真实树整树零改写（裁决Q6.2；14-B先例）
# ---------------------------------------------------------------------------

from video_doc.timeline_builder_v2 import load_published_timeline
from video_doc.unified_timeline import TRANSCRIPT_EVENT_TYPE

_DEV01_M2 = _DEV01_REAL / "m2"


def _dev01_ready() -> bool:
    return (
        (_DEV01_REAL / "task.json").is_file()
        and (_DEV01_M2 / "timelines" / cf.CURRENT_FILE).is_file()
        and (_DEV01_M2 / cf.FRAME_MODULE_TOKEN / cf.CURRENT_FILE).is_file()
        and (_DEV01_M2 / cf.OCR_ROOT_DIRECTORY / cf.CURRENT_FILE).is_file()
    )


_DEV01_REASON = "真实outputs/DEV01的五域正式组不齐全（task/timeline/frame-set/ocr-set）"


def _dev01_sandbox(tmp_path: Path) -> Path:
    sandbox_outputs = tmp_path / "outputs"
    sandbox = sandbox_outputs / "DEV01"
    sandbox.mkdir(parents=True)
    shutil.copy2(_DEV01_REAL / "task.json", sandbox / "task.json")
    for name in ("timelines", cf.FRAME_MODULE_TOKEN, cf.OCR_ROOT_DIRECTORY):
        source = _DEV01_M2 / name
        assert source.is_dir(), f"真实DEV01缺少m2/{name}"
        shutil.copytree(source, sandbox / "m2" / name)
    return sandbox_outputs


def _dev01_injected_plan(sandbox_outputs: Path, *, emergency: bool) -> list[dict[str, Any]]:
    if emergency:
        plan_chapters_v2(
            "DEV01",
            output_root=sandbox_outputs,
            transport=ScriptedTransport([RuntimeError("boom-1"), RuntimeError("boom-2")]),
            environ=ENV,
        )
        return [{"source_event_id": None, "title": "完整内容"}]
    index = load_published_timeline(sandbox_outputs, "DEV01")
    assert index is not None
    duration_ms = int(index["duration_ms"])
    eligible = [
        event
        for event in index["events"]
        if event["type"] == TRANSCRIPT_EVENT_TYPE and event["payload"]["boundary_eligible"]
    ]
    assert eligible, "DEV01真实Timeline没有可用boundary_eligible事件"
    boundaries = [{"source_event_id": eligible[0]["id"], "title": "章节一"}]
    for event in eligible[1:]:
        start = int(event["start_ms"])
        if start >= MIN_CHAPTER_MS and duration_ms - start >= MIN_CHAPTER_MS:
            boundaries.append({"source_event_id": event["id"], "title": "章节二"})
            break
    plan_result = plan_chapters_v2(
        "DEV01",
        output_root=sandbox_outputs,
        transport=ScriptedTransport([{"boundaries": boundaries}]),
        environ=ENV,
    )
    assert plan_result.status == "success"
    return boundaries


@pytest.mark.skipif(not _dev01_ready(), reason=_DEV01_REASON)
def test_dev01_sandbox_primary_real_tree_e2e(tmp_path: Path) -> None:
    sandbox_outputs = _dev01_sandbox(tmp_path)
    real_snapshot = _tree_sha_pairs(_DEV01_REAL)
    boundaries = _dev01_injected_plan(sandbox_outputs, emergency=False)
    result = cf.select_chapter_frames("DEV01", output_root=sandbox_outputs)
    assert result.cache_hit is False and result.chapter_count == len(boundaries)
    up = cf._load_upstream(sandbox_outputs, "DEV01")
    _assert_matches_reference(
        result.selection, up.plan, up.timeline, up.frame_doc,
        up.page_change_doc, up.ocr_doc,
    )
    assert result.selection["source_plan"]["result_source"] == "primary"
    assert 1 <= result.selected_frame_count <= cf.MAX_FRAMES_PER_CHAPTER * len(boundaries)
    group_snapshot = _tree_sha_pairs(result.group_path)
    current_bytes = (
        sandbox_outputs / "DEV01" / "m2" / cf.ROOT_DIRECTORY / cf.CURRENT_FILE
    ).read_bytes()
    second = cf.select_chapter_frames("DEV01", output_root=sandbox_outputs)
    assert second.cache_hit is True and second.result_id == result.result_id
    assert _tree_sha_pairs(second.group_path) == group_snapshot
    assert (
        sandbox_outputs / "DEV01" / "m2" / cf.ROOT_DIRECTORY / cf.CURRENT_FILE
    ).read_bytes() == current_bytes
    assert cf.load_published_chapter_frame_selection(sandbox_outputs, "DEV01") == result.selection
    # 2026-10-18真实调用验收开始后真实m2域可合法存在；沙箱泄漏防护=上面的整树零改写快照。
    assert _tree_sha_pairs(_DEV01_REAL) == real_snapshot


@pytest.mark.skipif(not _dev01_ready(), reason=_DEV01_REASON)
def test_dev01_sandbox_emergency_real_tree_e2e(tmp_path: Path) -> None:
    sandbox_outputs = _dev01_sandbox(tmp_path)
    real_snapshot = _tree_sha_pairs(_DEV01_REAL)
    _dev01_injected_plan(sandbox_outputs, emergency=True)
    result = cf.select_chapter_frames("DEV01", output_root=sandbox_outputs)
    assert result.chapter_count == 1
    assert result.selection["source_plan"]["result_source"] == "emergency"
    assert result.selection["source_plan"]["quality_level"] == "degraded"
    assert 1 <= result.selected_frame_count <= cf.MAX_FRAMES_PER_CHAPTER
    up = cf._load_upstream(sandbox_outputs, "DEV01")
    _assert_matches_reference(
        result.selection, up.plan, up.timeline, up.frame_doc,
        up.page_change_doc, up.ocr_doc,
    )
    # 同上：真实域缺席断言由零改写快照取代（2026-10-18真实调用验收）。
    assert _tree_sha_pairs(_DEV01_REAL) == real_snapshot


@pytest.mark.skipif(not _dev01_ready(), reason=_DEV01_REASON)
def test_dev01_real_cli_missing_plan_zero_rewrite(capsys) -> None:
    if (_DEV01_M2 / "boundaries" / cf.CURRENT_FILE).exists():
        pytest.skip("真实DEV01已发布plan；本失败路径零改写用例届时由G1另行复跑")
    outputs_root = REPO_ROOT / "outputs"
    snapshot = _tree_sha_pairs(outputs_root)
    assert main(["select-chapter-frames", "DEV01", "--output-root", str(outputs_root)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["module"] == cf.MODULE_TOKEN
    assert err["error_code"] == cf.ERROR_INPUT_CHAPTER_PLAN_MISSING
    assert err["domain"] == "boundaries"
    assert _tree_sha_pairs(outputs_root) == snapshot
    assert not (_DEV01_M2 / cf.ROOT_DIRECTORY).exists()
    assert not (_DEV01_M2 / "failures" / cf.MODULE_TOKEN).exists()
