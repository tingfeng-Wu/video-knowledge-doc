"""阶段15-0 E3(i)：run-m2离线全链沙箱e2e（裁决§四E3验收契约）。

矩阵：F=首跑全链（十二阶段进度行/8键摘要/JSONL/input_budget呈现/三格式产物）；
R=第二次全缓存复跑（E12/E13：this_run全0＋published空＋除append-only日志行外
整树零改写）；D=全树dry-run呈现（零写）；A=审批门（boundary/content两处，首个
供应商请求前停止、零发布）；X=条目预算门E08（exceeded）/E09（unknown）与
content侧exceeded。

离线形态：audio/asr/timeline1/F1帧集（依赖FFmpeg/whisper的四个上游子步）用
编排层命名空间stub（幂等：文件/域current存在即cache_hit）；F2（注入proxy
替身）、OCR（mode=off真实disabled组）、Timeline 2.0、14-B、14-A、14-C、14-D
全部真实模块；两个预算角色用ScriptedTransport脚本应答。零真实API、零网络、
零真实FFmpeg；M1与真实outputs只读不触碰。帧集在合成发布后替换为合法PNG字节
（先例test_document_exporter_v2._republish_frames_with_real_pngs），保证导出
嵌入不触发REASON_IMAGE_EMBED_FAILED剪枝。
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_boundary_planner_v2 import (
    GOOD_RESPONSE,
    HAPPY_EVENTS,
    SECRET,
    ScriptedTransport,
)
from tests.test_document_composer_v2 import _ContentTransport, _responses
from tests.test_document_exporter_v2 import _republish_frames_with_real_pngs
from tests.test_ocr_extractor import TASK, _publish_frame_set
from tests.test_timeline_builder_v2 import DEFAULT_VALUES, _proxy_loader
from tests.test_unified_timeline import make_m1_timeline
from video_doc import m2_pipeline
from video_doc.asr_only_pipeline import MediaTools
from video_doc.asr_timeline import load_task_timeline
from video_doc.m2_pipeline import (
    M2_INPUT_BUDGET_POLICY_VERSION,
    M2PipelineError,
    M2_STAGE_SEQUENCE,
    run_task_m2,
)
from video_doc.page_change_detector import (
    detect_page_changes as _real_detect_page_changes,
)
from video_doc.unified_timeline import compute_timeline_id

_SUMMARY_KEYS = {
    "status",
    "task_id",
    "result_ids",
    "cache_hits",
    "degraded",
    "reason_codes",
    "relative_paths",
    "call_counts",
}
_BUDGET_KEYS = {
    "policy_version",
    "limit_chars",
    "limit_bytes",
    "measured_chars",
    "measured_bytes",
    "status",
    "planned_business_calls",
    "supplier_request_limit",
}
_PUBLISHED_ORDER = [
    "frames",
    "page_changes",
    "ocr",
    "timeline_v2",
    "chapter_plan_v2",
    "chapter_frames",
    "document_content_v2",
    "export_v2",
]


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = (
            "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
    return out


def _log_path(output_root: Path) -> Path:
    return output_root / TASK / "m2" / "logs" / "run-m2.jsonl"


def _read_jsonl(output_root: Path) -> list[dict]:
    raw = _log_path(output_root).read_bytes()
    assert raw.count(b"\r") == 0  # LF铁律
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


def _diff(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {key for key in set(before) | set(after) if before.get(key) != after.get(key)}


class _Sandbox:
    """tmp_path内的合成全链沙箱：stub四个FFmpeg依赖子步，其余全部真实模块。"""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.output_root = tmp_path / "outputs"
        self.output_root.mkdir()
        self.source = tmp_path / "synthetic.mp4"
        self.source.write_bytes(b"M2E2E-SYNTHETIC-SOURCE" * 32)
        self.progress: list[tuple[str, str, dict | None]] = []
        self._audio_ran = False
        self._asr_ran = False

        tools_dir = tmp_path / "stub_ff"
        tools_dir.mkdir()
        monkeypatch.setattr(
            m2_pipeline,
            "resolve_media_tools",
            lambda ffmpeg_dir=None: MediaTools(
                directory=tools_dir,
                ffmpeg=tools_dir / "ffmpeg.exe",
                ffprobe=tools_dir / "ffprobe.exe",
            ),
        )
        monkeypatch.setattr(m2_pipeline, "probe_video", self._fake_probe)
        monkeypatch.setattr(m2_pipeline, "extract_task_audio", self._fake_audio)
        monkeypatch.setattr(m2_pipeline, "transcribe_task_audio", self._fake_asr)
        monkeypatch.setattr(m2_pipeline, "build_task_timeline", self._fake_timeline1)
        monkeypatch.setattr(m2_pipeline, "sample_frames", self._fake_frames)
        monkeypatch.setattr(m2_pipeline, "detect_page_changes", self._fake_page_changes)
        monkeypatch.setenv("VIDEO_DOC_LLM_API_KEY", SECRET)

    # ---- stubs（幂等，供全缓存复跑） ----

    def _fake_probe(self, path):
        from video_doc.video_probe import VideoMetadata

        p = Path(path)
        return VideoMetadata(
            source_path=str(p),
            file_name=p.name,
            file_size_bytes=p.stat().st_size,
            duration_seconds=600.0,
            width=640,
            height=360,
            frame_rate=25.0,
            video_codec="h264",
            has_audio=True,
            audio_codec="aac",
            audio_sample_rate=16000,
        )

    def _fake_audio(self, output_root, task_id, **kwargs):
        del output_root, task_id, kwargs
        hit = self._audio_ran
        self._audio_ran = True
        return SimpleNamespace(cache_hit=hit)

    def _fake_asr(self, output_root, task_id, **kwargs):
        del output_root, task_id, kwargs
        hit = self._asr_ran
        self._asr_ran = True
        return SimpleNamespace(cache_hit=hit)

    def _fake_timeline1(self, output_root, task_id, **kwargs):
        del kwargs
        timeline_dir = Path(output_root) / task_id / "timeline"
        json_path = timeline_dir / "timeline.json"
        hit = json_path.is_file()
        if not hit:
            payload = make_m1_timeline(
                events=HAPPY_EVENTS, task_id=TASK, duration_seconds=600.0
            )
            timeline_dir.mkdir(parents=True, exist_ok=True)
            with open(json_path, "w", encoding="utf-8", newline="") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return SimpleNamespace(cache_hit=hit)

    def _fake_frames(self, task_id, *, output_root=None, **kwargs):
        del kwargs
        root = Path(output_root)
        assert task_id == TASK
        frames_root = root / TASK / "m2" / "frames"
        current = frames_root / "current.json"
        if current.is_file():
            pointer = json.loads(current.read_text(encoding="utf-8"))
            return SimpleNamespace(
                cache_hit=True, group_path=frames_root / str(pointer["group"])
            )
        group = _publish_frame_set(root, DEFAULT_VALUES)
        _republish_frames_with_real_pngs(root)
        return SimpleNamespace(cache_hit=False, group_path=group)

    def _fake_page_changes(self, task_id, *, output_root=None, **kwargs):
        del kwargs
        return _real_detect_page_changes(
            task_id,
            output_root=output_root,
            _proxy_loader=_proxy_loader(DEFAULT_VALUES),
        )

    # ---- 运行helpers ----

    def fresh_transports(
        self,
        *,
        boundary: ScriptedTransport | None = None,
        content: _ContentTransport | None = None,
    ) -> tuple[ScriptedTransport, _ContentTransport, dict]:
        bt = boundary if boundary is not None else ScriptedTransport(
            [copy.deepcopy(GOOD_RESPONSE)]
        )
        ct = content if content is not None else _ContentTransport(_responses())
        return bt, ct, {"boundary_model": bt, "content_model": ct}

    def run(self, **kwargs):
        kwargs.setdefault("ocr_mode", "off")
        kwargs.setdefault("progress", self._collect_progress)
        return run_task_m2(self.output_root, TASK, **kwargs)

    def _collect_progress(self, stage: str, status: str, payload=None):
        self.progress.append((stage, status, payload))

    def task_root(self) -> Path:
        return self.output_root / TASK


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    return _Sandbox(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# F：首跑全链
# ---------------------------------------------------------------------------


def test_e2e_bounded_content_keeps_boundary_budget_measurable(sandbox):
    _boundary_t, _content_t, transports = sandbox.fresh_transports()
    summary = sandbox.run(
        source_video=sandbox.source,
        allow_llm=True,
        bounded_content=True,
        _transports=transports,
    )
    assert summary.task_id == TASK
    assert not any(
        status == "failed" and stage == "chapter_plan_v2"
        for stage, status, _payload in sandbox.progress
    )


def test_e2e_first_run_full_chain_summary_progress_and_jsonl(sandbox):
    boundary_t, content_t, transports = sandbox.fresh_transports()
    summary = sandbox.run(
        source_video=sandbox.source, allow_llm=True, _transports=transports
    )
    payload = summary.to_json()

    # 8键字面摘要（实现决定3）
    assert set(payload) == _SUMMARY_KEYS
    assert payload["status"] == "completed"
    assert payload["task_id"] == TASK

    hits = payload["cache_hits"]
    assert set(hits) == set(m2_pipeline._M2_ARTIFACT_STAGES)
    assert all(value is False for value in hits.values())

    # 裁决§三.6字面＋实现决定10/11
    rid = payload["result_ids"]
    assert set(rid) == set(m2_pipeline._M2_ARTIFACT_STAGES)
    assert rid["ocr"] is None  # ocr=off→null（组身份仍在域内与JSONL）
    expected_transcript_id = compute_timeline_id(
        load_task_timeline(sandbox.output_root, TASK)
    )
    assert rid["transcript"] == expected_transcript_id
    assert expected_transcript_id.startswith("unified-timeline-")
    assert rid["frames"] == "frame-set-test00000001"

    rel = payload["relative_paths"]
    assert rel["transcript"] == "timeline/timeline.json"
    assert "ocr" not in rel
    for domain, relative in rel.items():
        assert (sandbox.task_root() / relative).exists()

    assert payload["degraded"] == []
    assert payload["reason_codes"] == ["OCR_DISABLED"]

    # call_counts：首跑本轮实数非0（E13对照面），published按执行顺序
    counts = payload["call_counts"]
    assert counts["this_run"]["boundary_model"] == {
        "business_calls": 1,
        "supplier_requests": 1,
    }
    assert counts["this_run"]["content_model"] == {
        "business_calls": 3,
        "supplier_requests": 3,
    }
    assert counts["published_this_run"] == _PUBLISHED_ORDER
    assert len(boundary_t.calls) == 1
    assert len(content_t.calls) == 3

    # 进度行：十二阶段全序列终态行（audio/asr/timeline子步行不入该序列）
    terminal = [
        stage
        for (stage, status, _p) in sandbox.progress
        if status in {"cache_hit", "completed", "degraded"}
        and stage in M2_STAGE_SEQUENCE
    ]
    assert terminal == list(M2_STAGE_SEQUENCE)
    substeps = [
        stage for (stage, status, _p) in sandbox.progress if stage in ("audio", "asr", "timeline")
    ]
    assert substeps == ["audio", "asr", "timeline"]

    # input_budget呈现（§一1.5字面8键）
    budgets = {
        stage: payload_line["input_budget"]
        for (stage, _s, payload_line) in sandbox.progress
        if payload_line
    }
    assert set(budgets) == {"chapter_plan_v2", "document_content_v2"}
    boundary_budget = budgets["chapter_plan_v2"]
    assert set(boundary_budget) == _BUDGET_KEYS
    assert boundary_budget["policy_version"] == M2_INPUT_BUDGET_POLICY_VERSION
    assert boundary_budget["status"] == "pass"
    assert boundary_budget["planned_business_calls"] == 1
    assert boundary_budget["limit_chars"] == 150_000
    assert boundary_budget["limit_bytes"] == 300_000
    assert boundary_budget["supplier_request_limit"] == 2
    assert len(boundary_budget["measured_chars"]) == 1
    assert 0 < boundary_budget["measured_chars"][0] < 150_000
    assert len(boundary_budget["measured_bytes"]) == 1
    content_budget = budgets["document_content_v2"]
    assert content_budget["status"] == "pass"
    assert content_budget["planned_business_calls"] == 3
    assert content_budget["limit_chars"] == 60_000
    assert content_budget["limit_bytes"] == 180_000
    assert len(content_budget["measured_chars"]) == 3
    assert all(0 < chars <= 60_000 for chars in content_budget["measured_chars"])

    # JSONL：无started事件、终态覆盖十二阶段、脱敏、LF
    events = _read_jsonl(sandbox.output_root)
    assert all(event["status"] != "started" for event in events)
    assert [event["stage"] for event in events][0] == "preflight"
    assert [event["stage"] for event in events][-1] == "completed"
    by_stage = {event["stage"]: event for event in events}
    assert by_stage["chapter_plan_v2"]["result_id"] == rid["chapter_plan_v2"]
    assert set(by_stage["chapter_plan_v2"]["input_budget"]) == _BUDGET_KEYS
    assert by_stage["document_content_v2"]["input_budget"]["planned_business_calls"] == 3
    # ocr off：摘要null但组身份如实留在JSONL终态事件（实现决定11）
    assert isinstance(by_stage["ocr"]["result_id"], str) and by_stage["ocr"]["result_id"]
    assert by_stage["ocr"]["reason_codes"] == ["OCR_DISABLED"]
    raw_text = _log_path(sandbox.output_root).read_text(encoding="utf-8")
    assert str(sandbox.tmp_path) not in raw_text

    # 三格式产物真实落盘且无图像剪枝（真PNG帧集）
    docs_group = sandbox.task_root() / "m2/documents/formal-results" / rid["export_v2"]
    assert (docs_group / "result.json").is_file()
    assert (docs_group / "result.md").is_file()
    assert (docs_group / "result.docx").is_file()


def test_e2e_budget_progress_line_precedes_first_supplier_request(sandbox):
    """B2修正钉死（裁决§一1.5字面）：带input_budget的进度行先于该角色首个供应商请求。

    传输代理在首次``complete``被调用的时刻快照已打印的进度行序列：该时刻必须已
    存在本阶段带input_budget的行，且本阶段终态行尚未出现（行在调用前而非done时）。
    """
    _bt, _ct, transports_base = sandbox.fresh_transports()
    snapshots: dict[str, list] = {}

    class _ProbingTransport:
        def __init__(self, inner, role: str) -> None:
            self._inner = inner
            self._role = role

        def complete(self, config, messages, response_format):
            if self._role not in snapshots:
                snapshots[self._role] = list(sandbox.progress)
            return self._inner.complete(config, messages, response_format)

    transports = {
        role: _ProbingTransport(inner, role) for role, inner in transports_base.items()
    }
    summary = sandbox.run(
        source_video=sandbox.source, allow_llm=True, _transports=transports
    )
    assert summary.to_json()["status"] == "completed"

    for role, stage in (
        ("boundary_model", "chapter_plan_v2"),
        ("content_model", "document_content_v2"),
    ):
        seen = snapshots[role]  # 首个供应商请求时刻的进度行快照
        assert stage in {s for (s, _st, _p) in seen}, f"{role}缺{stage}进度行"
        budget_lines = [
            (s, st, p)
            for (s, st, p) in seen
            if s == stage and p is not None and "input_budget" in p
        ]
        assert budget_lines, f"{role}首个调用前未打印带input_budget的行"
        assert budget_lines[0][2]["input_budget"].keys() >= {"status", "policy_version"}
        assert not any(
            s == stage and st in {"completed", "cache_hit", "degraded"}
            for (s, st, _p) in seen
        ), f"{stage}终态行先于首个供应商请求出现（时序倒置）"


# ---------------------------------------------------------------------------
# R：第二次全缓存复跑（E12/E13）
# ---------------------------------------------------------------------------


def test_e2e_second_run_all_cache_hit_zero_calls_append_only_log(sandbox):
    _bt1, _ct1, transports1 = sandbox.fresh_transports()
    first = sandbox.run(
        source_video=sandbox.source, allow_llm=True, _transports=transports1
    ).to_json()

    # 空脚本传输：任何供应商请求都会触发AssertionError（预算外请求守卫）
    bt2, ct2, transports2 = sandbox.fresh_transports(
        boundary=ScriptedTransport([]), content=_ContentTransport([])
    )
    before = _snapshot(sandbox.output_root)
    log_bytes_before = _log_path(sandbox.output_root).read_bytes()

    second = sandbox.run(allow_llm=True, _transports=transports2).to_json()

    assert all(second["cache_hits"].values())
    assert second["call_counts"]["this_run"] == {
        "boundary_model": {"business_calls": 0, "supplier_requests": 0},
        "content_model": {"business_calls": 0, "supplier_requests": 0},
    }
    assert second["call_counts"]["published_this_run"] == []
    assert second["result_ids"] == first["result_ids"]
    assert second["relative_paths"] == first["relative_paths"]
    assert len(bt2.calls) == 0 and len(ct2.calls) == 0

    # 唯一允许写入：append-only日志行；其余整树零改写
    after = _snapshot(sandbox.output_root)
    assert _diff(before, after) == {f"{TASK}/m2/logs/run-m2.jsonl"}
    log_after = _log_path(sandbox.output_root).read_bytes()
    assert log_after.startswith(log_bytes_before)
    assert log_after.count(b"\r") == 0
    events = _read_jsonl(sandbox.output_root)
    assert events[-1]["stage"] == "completed" and events[-1]["status"] == "completed"


# ---------------------------------------------------------------------------
# D：全树dry-run呈现（零写、永不联网）
# ---------------------------------------------------------------------------


def test_e2e_dry_run_on_full_tree_reports_cache_hit_without_writes(sandbox):
    _bt1, _ct1, transports1 = sandbox.fresh_transports()
    sandbox.run(
        source_video=sandbox.source, allow_llm=True, _transports=transports1
    )
    before = _snapshot(sandbox.tmp_path)
    bt2, ct2, transports2 = sandbox.fresh_transports(
        boundary=ScriptedTransport([]), content=_ContentTransport([])
    )
    plan = sandbox.run(
        allow_llm=True, _transports=transports2, dry_run=True
    ).to_json()

    by_stage = {entry["stage"]: entry for entry in plan["stages"]}
    assert by_stage["preflight"]["disposition"] == "cache_hit"
    assert by_stage["task_init"]["disposition"] == "cache_hit"
    for domain in m2_pipeline._M2_ARTIFACT_STAGES:
        expected = "disabled" if domain == "ocr" else "cache_hit"
        assert by_stage[domain]["disposition"] == expected, domain
    assert by_stage["completed"]["disposition"] == "will_run"
    # 已发布预算域：planned 0呈现（§一1.5「measured记null」）
    for stage in ("chapter_plan_v2", "document_content_v2"):
        budget = by_stage[stage]["input_budget"]
        assert set(budget) == _BUDGET_KEYS
        assert budget["planned_business_calls"] == 0
        assert budget["measured_chars"] is None
        assert budget["measured_bytes"] is None
        assert budget["status"] == "pass"
    assert plan["dry_run"] is True
    assert len(bt2.calls) == 0 and len(ct2.calls) == 0
    assert _snapshot(sandbox.tmp_path) == before


# ---------------------------------------------------------------------------
# A：审批门（首个供应商请求前停止）
# ---------------------------------------------------------------------------


def test_e2e_boundary_stage_requires_approval_before_measurement(sandbox, monkeypatch):
    measure_calls: list[str] = []

    def spy_measure(output_root, task_id, **kwargs):
        del output_root, kwargs
        measure_calls.append(task_id)
        return {"message_chars": 10, "message_bytes": 10}

    monkeypatch.setattr(m2_pipeline, "measure_boundary_egress", spy_measure)
    bt, ct, transports = sandbox.fresh_transports(
        boundary=ScriptedTransport([]), content=_ContentTransport([])
    )
    with pytest.raises(M2PipelineError) as exc:
        sandbox.run(source_video=sandbox.source, allow_llm=False, _transports=transports)
    assert exc.value.error_code == "LLM_APPROVAL_REQUIRED"
    assert exc.value.failed_stage == "chapter_plan_v2"
    assert measure_calls == []  # 门序：pointer→审批→测量（审批先于测量）
    task_root = sandbox.task_root()
    assert (task_root / "m2/timelines/current.json").is_file()
    assert not (task_root / "m2/boundaries").exists()
    assert not (task_root / "m2/chapter-frames").exists()
    assert not (task_root / "m2/documents").exists()
    assert len(bt.calls) == 0 and len(ct.calls) == 0
    events = _read_jsonl(sandbox.output_root)
    failed = events[-1]
    assert failed["stage"] == "chapter_plan_v2" and failed["status"] == "failed"
    assert failed["reason_codes"] == ["LLM_APPROVAL_REQUIRED"]
    assert "input_budget" not in failed  # 审批停止先于判定


def test_e2e_content_stage_requires_approval_when_content_absent(sandbox):
    _bt1, _ct1, transports1 = sandbox.fresh_transports()
    sandbox.run(
        source_video=sandbox.source, allow_llm=True, _transports=transports1
    )
    task_root = sandbox.task_root()
    # 沙箱内删除本轮自产content/documents域，构造「上游齐备、content缺current」形态
    shutil.rmtree(task_root / "m2/content")
    shutil.rmtree(task_root / "m2/documents")
    bt2, ct2, transports2 = sandbox.fresh_transports(
        boundary=ScriptedTransport([]), content=_ContentTransport([])
    )
    with pytest.raises(M2PipelineError) as exc:
        sandbox.run(allow_llm=False, _transports=transports2)
    assert exc.value.error_code == "LLM_APPROVAL_REQUIRED"
    assert exc.value.failed_stage == "document_content_v2"
    assert not (task_root / "m2/content").exists()
    assert not (task_root / "m2/documents").exists()
    assert len(bt2.calls) == 0 and len(ct2.calls) == 0
    events = _read_jsonl(sandbox.output_root)
    assert events[-1]["stage"] == "document_content_v2"
    assert events[-1]["status"] == "failed"


# ---------------------------------------------------------------------------
# X：条目预算门E08/E09与content侧exceeded（首个供应商请求前停止、零发布）
# ---------------------------------------------------------------------------


def test_e2e_boundary_input_budget_exceeded_stops_before_first_request(
    sandbox, monkeypatch
):
    monkeypatch.setattr(
        m2_pipeline,
        "measure_boundary_egress",
        lambda *args, **kwargs: {"message_chars": 150_001, "message_bytes": 900_000},
    )
    bt, ct, transports = sandbox.fresh_transports()
    with pytest.raises(M2PipelineError) as exc:
        sandbox.run(source_video=sandbox.source, allow_llm=True, _transports=transports)
    assert exc.value.error_code == "INPUT_BUDGET_EXCEEDED"
    assert exc.value.failed_stage == "chapter_plan_v2"
    assert len(bt.calls) == 0 and len(ct.calls) == 0
    task_root = sandbox.task_root()
    assert (task_root / "m2/timelines/current.json").is_file()
    assert not (task_root / "m2/boundaries").exists()
    assert not (task_root / "m2/documents").exists()
    events = _read_jsonl(sandbox.output_root)
    failed = events[-1]
    assert failed["stage"] == "chapter_plan_v2" and failed["status"] == "failed"
    assert failed["input_budget"]["status"] == "exceeded"
    assert failed["input_budget"]["measured_chars"] == [150_001]
    assert failed["input_budget"]["measured_bytes"] == [900_000]


def test_e2e_boundary_input_budget_unknown_on_measurement_error(sandbox, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("injected-measurement-boom")

    monkeypatch.setattr(m2_pipeline, "measure_boundary_egress", boom)
    bt, ct, transports = sandbox.fresh_transports()
    with pytest.raises(M2PipelineError) as exc:
        sandbox.run(source_video=sandbox.source, allow_llm=True, _transports=transports)
    assert exc.value.error_code == "INPUT_BUDGET_UNKNOWN"
    assert exc.value.failed_stage == "chapter_plan_v2"
    assert len(bt.calls) == 0 and len(ct.calls) == 0
    assert not (sandbox.task_root() / "m2/boundaries").exists()
    failed = _read_jsonl(sandbox.output_root)[-1]
    assert failed["status"] == "failed"
    assert failed["input_budget"]["status"] == "unknown"
    assert failed["input_budget"]["measured_chars"] == [None]


def test_e2e_content_input_budget_exceeded_stops_after_chapter_frames(
    sandbox, monkeypatch
):
    preview = {
        "calls": [
            {"message_chars": 60_001, "message_bytes": 100} for _ in range(3)
        ],
        "budget": {"expected_business_calls": 3},
    }
    monkeypatch.setattr(
        m2_pipeline,
        "build_content_composition_dry_run",
        lambda *args, **kwargs: preview,
    )
    bt, ct, transports = sandbox.fresh_transports()
    with pytest.raises(M2PipelineError) as exc:
        sandbox.run(source_video=sandbox.source, allow_llm=True, _transports=transports)
    assert exc.value.error_code == "INPUT_BUDGET_EXCEEDED"
    assert exc.value.failed_stage == "document_content_v2"
    assert len(bt.calls) == 1  # boundary门在前且通过，计划照常发布
    assert len(ct.calls) == 0  # content在首个供应商请求前停止
    task_root = sandbox.task_root()
    assert (task_root / "m2/boundaries/current.json").is_file()
    assert (task_root / "m2/chapter-frames/current.json").is_file()
    assert not (task_root / "m2/content").exists()
    assert not (task_root / "m2/documents").exists()
    failed = _read_jsonl(sandbox.output_root)[-1]
    assert failed["stage"] == "document_content_v2" and failed["status"] == "failed"
    assert failed["input_budget"]["status"] == "exceeded"
    assert failed["input_budget"]["measured_chars"] == [60_001] * 3


def test_e2e_content_identity_drift_denied_cleanly_without_failure_group(
    sandbox, monkeypatch
):
    """N7/N4：已发布content组＋composer身份漂移＋未批准→干净LLM_APPROVAL_REQUIRED
    停止（复现DEV01/COURSE01在1.0→1.1升级后的真实形态）：不重试、零失败组、
    既有组逐字节不变、唯一写入=run-m2.jsonl追加。回退对照：撤销N7传播后本用例
    以CONTENT_MODEL_FAILED失败并写失败组。"""
    from video_doc import document_composer_v2 as composer_mod

    _bt1, _ct1, transports1 = sandbox.fresh_transports()
    first = sandbox.run(
        source_video=sandbox.source, allow_llm=True, _transports=transports1
    ).to_json()
    content_rid = first["result_ids"]["document_content_v2"]
    group = sandbox.task_root() / "m2" / "content" / "formal-results" / content_rid
    content_before = (group / "document-content.json").read_bytes()
    manifest_before = (group / "manifest.json").read_bytes()

    # 制造与COURSE01真实形态一致的N4漂移：batch逐调用上限漂移→组身份＋batch
    # llm-cache身份同时失配（single/global身份不变可回放）→batch槽触发拒绝守卫。
    # （只漂移COMPOSER_VERSION是另一形态：三槽全回放后卡在同身份组复验，不触守卫。）
    monkeypatch.setattr(
        composer_mod,
        "PER_CALL_MAX_TOKENS",
        {
            composer_mod.CALL_SINGLE: 1800,
            composer_mod.CALL_BATCH: 7777,
            composer_mod.CALL_GLOBAL: 1600,
        },
    )
    before = _snapshot(sandbox.output_root)
    with pytest.raises(M2PipelineError) as exc:
        sandbox.run(allow_llm=False)  # 不注入transports→_DenyApprovalTransport
    assert exc.value.error_code == "LLM_APPROVAL_REQUIRED"
    assert exc.value.failed_stage == "document_content_v2"

    after = _snapshot(sandbox.output_root)
    assert _diff(before, after) == {f"{TASK}/m2/logs/run-m2.jsonl"}
    assert (group / "document-content.json").read_bytes() == content_before
    assert (group / "manifest.json").read_bytes() == manifest_before
    failures = sandbox.task_root() / "m2" / "failures" / "document_composer_v2"
    assert not failures.exists() or list(failures.iterdir()) == []
    events = _read_jsonl(sandbox.output_root)
    assert events[-1]["stage"] == "document_content_v2"
    assert events[-1]["status"] == "failed"
