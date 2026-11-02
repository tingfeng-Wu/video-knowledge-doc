from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from video_doc import ocr_extractor as extractor
from video_doc.ocr_extractor import (
    OCR_V1,
    InjectedOcrFailure,
    InputFrameSetCorruptError,
    InputFrameSetMissingError,
    InputPageChangeSetMissingError,
    NullOcrEngine,
    OcrCacheCorruptError,
    OcrConcurrentRunError,
    OcrCurrentPointerCorruptError,
    OcrDegradedOutcome,
    OcrEngineUnavailableError,
    OcrExtractorError,
    OcrFailedError,
    OcrLine,
    OcrSetGroup,
    frame_canonical_text,
    normalize_line_text,
    run_ocr,
)
from video_doc.page_change_detector import ProxyFrame, detect_page_changes

TASK = "SYNTH01"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _frame_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _proxy(pattern: int, value: int = 0) -> ProxyFrame:
    pixels = np.full((8, 8), value, dtype=np.uint8)
    bits = tuple(bool((index >> pattern) & 1) for index in range(64))
    return ProxyFrame(pixels=pixels, perceptual_hash=bits)


def _frames(count: int, *, interval_ms: int = 5_000) -> list[dict[str, object]]:
    return [
        {
            "frame_id": f"fr{ordinal:06d}",
            "ordinal": ordinal,
            "role": "head" if ordinal == 1 else ("tail" if ordinal == count else "grid"),
            "planned_ms": (ordinal - 1) * interval_ms,
            "requested_ms": (ordinal - 1) * interval_ms,
            "decoded_ms": (ordinal - 1) * interval_ms,
            "is_first": ordinal == 1,
            "is_last": ordinal == count,
            "file": f"frames/fr{ordinal:06d}.png",
            "sha256": "0" * 64,
            "size_bytes": 1,
        }
        for ordinal in range(1, count + 1)
    ]


def _publish_frame_set(root: Path, values: tuple[int, ...]) -> Path:
    frames_root = root / TASK / "m2" / "frames"
    result_id = "frame-set-test00000001"
    group = frames_root / "formal-results" / result_id
    images = group / "frames"
    images.mkdir(parents=True)
    frames = _frames(len(values))
    files: dict[str, dict[str, object]] = {}
    for index, (frame, value) in enumerate(zip(frames, values)):
        path = group / str(frame["file"])
        payload = f"fake-png-{index}-{value}".encode()
        path.write_bytes(payload)
        frame["sha256"] = _sha(payload)
        frame["size_bytes"] = len(payload)
        files[str(frame["file"])] = {"sha256": frame["sha256"], "bytes": len(payload)}
    index_payload = {
        "schema_name": "FrameSet",
        "schema_version": "1.0",
        "status": "success",
        "frame_set_id": result_id,
        "task_id": TASK,
        "profile_id": "low-frequency-v1",
        "profile": {},
        "source_video": {
            "file_name": "synthetic.mp4",
            "declared_file_name": "synthetic.mp4",
            "sha256": "a" * 64,
            "size_bytes": 123,
            "container_duration_ms": (len(values) - 1) * 5_000,
            "video_stream_duration_ms": (len(values) - 1) * 5_000,
            "width": 640,
            "height": 360,
            "frame_rate": 25.0,
            "video_codec": "h264",
        },
        "sampling": {},
        "cache_identity_sha256": "b" * 64,
        "frame_count": len(frames),
        "frames": frames,
    }
    index_bytes = _frame_json_bytes(index_payload)
    (group / "frame-set.json").write_bytes(index_bytes)
    files["frame-set.json"] = {"sha256": _sha(index_bytes), "bytes": len(index_bytes)}
    manifest = {
        "schema_name": "ArtifactGroupManifest",
        "schema_version": "1.0",
        "artifact_type": "frame_set",
        "task_id": TASK,
        "result_id": result_id,
        "status": "success",
        "cache_identity_sha256": "b" * 64,
        "inputs": {},
        "versions": {},
        "frame_count": len(frames),
        "files": files,
    }
    manifest_bytes = _frame_json_bytes(manifest)
    (group / "manifest.json").write_bytes(manifest_bytes)
    current = {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "frame_set",
        "result_id": result_id,
        "group": f"formal-results/{result_id}",
        "manifest_sha256": _sha(manifest_bytes),
    }
    (frames_root / "current.json").write_bytes(_json_bytes(current))
    return group


def _proxy_loader(values: tuple[int, ...]):
    proxies = {
        f"fr{ordinal:06d}.png": _proxy(ordinal % 6, value)
        for ordinal, value in enumerate(values, start=1)
    }

    def load(path: Path, width: int) -> ProxyFrame:
        assert width == 320
        return proxies[path.name]

    return load


def _setup(tmp_path: Path, values: tuple[int, ...] = (0, 255, 0, 255, 0, 255, 0, 255)) -> Path:
    """发布合成帧集并用真实F2发布页面变化组；返回output根目录。"""

    root = tmp_path / "outputs"
    _publish_frame_set(root, values)
    detect_page_changes(TASK, output_root=root, _proxy_loader=_proxy_loader(values))
    return root


def _candidates(root: Path) -> list[dict]:
    current = json.loads((root / TASK / "m2" / "frames" / "page-changes" / "current.json").read_text(encoding="utf-8"))
    group = root / TASK / "m2" / "frames" / "page-changes" / current["group"]
    index = json.loads((group / "page-change-set.json").read_text(encoding="utf-8"))
    return index["candidates"]


def _line(text: str, confidence: float, y: int, x0: int = 50, x1: int = 300, height: int = 40) -> OcrLine:
    return OcrLine(
        text=text,
        confidence=confidence,
        box=((x0, y), (x1, y), (x1, y + height), (x0, y + height)),
    )


class ScriptedEngine:
    """按帧文件名脚本化输出的测试引擎。"""

    engine_id = "scripted"

    def __init__(
        self,
        scripts: dict[str, list[OcrLine]] | None = None,
        *,
        binding: dict | None = None,
        fail_files: set[str] | None = None,
        fail_error: Exception | None = None,
    ) -> None:
        self.scripts = scripts or {}
        self.binding = binding or {
            "engine_id": "scripted",
            "engine_version": "1.0",
            "runtime": "script",
            "runtime_version": "1.0",
            "model_fingerprints": {"fake_model.onnx": "f" * 64},
        }
        self.fail_files = fail_files or set()
        self.fail_error = fail_error or RuntimeError("engine boom")
        self.calls: list[str] = []

    def describe_binding(self) -> dict:
        return dict(self.binding)

    def recognize(self, image_path: Path) -> list[OcrLine]:
        self.calls.append(image_path.name)
        if image_path.name in self.fail_files:
            raise self.fail_error
        return list(self.scripts.get(image_path.name, []))


def _factory(engine: ScriptedEngine):
    def create(profile) -> ScriptedEngine:
        assert profile.engine_id == OCR_V1.engine_id or profile.engine_id == "scripted-test"
        return engine

    return create


def _forbidden_factory(profile):
    raise AssertionError("off/空候选路径不得构造引擎")


def _tree_identity(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (_sha(path.read_bytes()), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _ocr_root(root: Path) -> Path:
    return root / TASK / "m2" / "ocr"


def _failures(root: Path) -> list[dict]:
    base = root / TASK / "m2" / "failures" / "ocr"
    if not base.is_dir():
        return []
    return [json.loads((entry / "failure.json").read_text(encoding="utf-8")) for entry in sorted(base.iterdir())]


def test_success_publishes_group_and_current(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    assert len(candidates) >= 2
    first = f"{candidates[0]['frame_id']}.png"
    second = f"{candidates[1]['frame_id']}.png"
    scripts = {
        first: [_line("第三章 内存管理", 0.99, 40), _line("Memory Management", 0.98, 120)],
        second: [_line("3.1 分页机制", 0.97, 40)],
    }
    engine = ScriptedEngine(scripts)
    result = run_ocr(TASK, output_root=root, mode="auto", _engine_factory=_factory(engine))
    assert isinstance(result, OcrSetGroup)
    assert result.index["status"] == "success"
    assert result.index["mode"] == "auto"
    assert result.index["ocr_set_id"].startswith("ocr-set-")
    assert result.index["frame_count"] == len(candidates)
    assert result.index["line_count"] == 3
    assert result.index["failed_frame_count"] == 0
    current = json.loads((_ocr_root(root) / "current.json").read_text(encoding="utf-8"))
    assert current["result_id"] == result.index["ocr_set_id"]
    assert current["artifact_type"] == "ocr_set"
    entry = result.index["frames"][0]
    assert entry["status"] == "ok"
    assert [line["text"] for line in entry["lines"]] == ["第三章 内存管理", "Memory Management"]
    assert all(len(line["box"]) == 4 for line in entry["lines"])
    assert engine.calls == [f"{candidate['frame_id']}.png" for candidate in candidates]


def test_reading_order_is_reordered_deterministically(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    first = f"{candidates[0]['frame_id']}.png"
    engine = ScriptedEngine({first: [_line("bottom line", 0.9, 200), _line("top line", 0.9, 20)]})
    result = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine))
    lines = result.index["frames"][0]["lines"]
    assert [line["text"] for line in lines] == ["top line", "bottom line"]
    assert [line["line_index"] for line in lines] == [1, 2]


def test_box_out_of_bounds_auto_marks_frame_failed_and_degrades(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    first = f"{candidates[0]['frame_id']}.png"
    bad = OcrLine(text="x", confidence=0.9, box=((0, 0), (9999, 0), (9999, 40), (0, 40)))
    engine = ScriptedEngine({first: [bad]})
    result = run_ocr(TASK, output_root=root, mode="auto", _engine_factory=_factory(engine))
    assert isinstance(result, OcrSetGroup)
    assert result.index["status"] == "degraded"
    assert "box_out_of_bounds" in result.index["reason_codes"]
    assert result.index["frames"][0]["status"] == "failed"
    assert result.index["frames"][0]["reason_codes"] == ["box_out_of_bounds"]
    assert result.index["failed_frame_count"] == 1


def test_box_out_of_bounds_required_raises_and_publishes_nothing(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    first = f"{candidates[0]['frame_id']}.png"
    bad = OcrLine(text="x", confidence=0.9, box=((0, 0), (9999, 0), (9999, 40), (0, 40)))
    engine = ScriptedEngine({first: [bad]})
    with pytest.raises(OcrFailedError) as info:
        run_ocr(TASK, output_root=root, mode="required", _engine_factory=_factory(engine))
    assert info.value.error_code == "OCR_FAILED"
    assert not (_ocr_root(root) / "formal-results").exists() or not any((_ocr_root(root) / "formal-results").iterdir())
    assert not (_ocr_root(root) / "current.json").exists()
    audits = _failures(root)
    assert len(audits) == 1
    assert audits[0]["error_code"] == "OCR_FAILED"
    assert audits[0]["current_unchanged"] is True


def test_low_score_lines_filtered_and_recorded(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    first = f"{candidates[0]['frame_id']}.png"
    engine = ScriptedEngine({first: [_line("good", 0.9, 20), _line("bad", 0.3, 100)]})
    result = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine))
    entry = result.index["frames"][0]
    assert entry["line_count"] == 1
    assert entry["filtered_line_count"] == 1
    assert entry["reason_codes"] == ["low_score_filtered"]
    assert "low_score_filtered" in result.index["reason_codes"]
    assert result.index["filtered_line_count"] == 1


def test_all_lines_filtered_yields_empty_status(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    scripts = {
        f"{candidate['frame_id']}.png": [_line("faint", 0.1, 20)]
        for candidate in candidates
    }
    engine = ScriptedEngine(scripts)
    result = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine))
    assert result.index["status"] == "empty"
    assert "no_text_detected" in result.index["reason_codes"]
    assert result.index["line_count"] == 0
    assert result.index["frame_count"] == len(candidates)


def test_duplicate_text_only_marked_never_deleted(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    assert len(candidates) >= 2
    first = f"{candidates[0]['frame_id']}.png"
    second = f"{candidates[1]['frame_id']}.png"
    engine = ScriptedEngine({
        first: [_line("Hello   World", 0.95, 20)],
        second: [_line("hello world", 0.9, 20)],
    })
    result = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine))
    frames = result.index["frames"]
    assert frames[0]["duplicate_of"] is None
    assert frames[1]["duplicate_of"] == frames[0]["frame_id"]
    # 原文与行数完整保留，只标记关系
    assert frames[1]["lines"][0]["text"] == "hello world"
    assert frames[1]["line_count"] == 1
    assert frames[0]["text_sha256"] == frames[1]["text_sha256"]
    assert result.index["deduplicated_count"] == 1


def test_normalization_keeps_single_spaces_and_casefolds() -> None:
    assert normalize_line_text("  Hello   World　２０２６ ") == "hello world 2026"
    assert frame_canonical_text(["ＡＢ", "cd  EF"]) == "ab\ncd ef"
    assert normalize_line_text("   ") == ""


def test_empty_text_frames_do_not_participate_in_dedup(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    assert len(candidates) >= 2
    engine = ScriptedEngine({})
    result = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine))
    assert result.index["status"] == "empty"
    assert all(frame["duplicate_of"] is None for frame in result.index["frames"])
    assert result.index["deduplicated_count"] == 0


def test_off_mode_publishes_disabled_without_engine(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    result = run_ocr(TASK, output_root=root, mode="off", _engine_factory=_forbidden_factory)
    assert isinstance(result, OcrSetGroup)
    assert result.index["status"] == "disabled"
    assert result.index["reason_codes"] == ["engine_disabled"]
    assert result.index["frames"] == []
    assert result.index["engine"] == {"state": "not_run", "reason": "disabled"}
    assert (_ocr_root(root) / "current.json").is_file()


def test_no_candidates_publishes_empty_without_engine(tmp_path: Path) -> None:
    root = _setup(tmp_path, values=(0, 0, 0, 0, 0, 0))
    assert _candidates(root) == []
    result = run_ocr(TASK, output_root=root, mode="auto", _engine_factory=_forbidden_factory)
    assert isinstance(result, OcrSetGroup)
    assert result.index["status"] == "empty"
    assert result.index["reason_codes"] == ["no_candidate_frames"]
    assert result.index["engine"] == {"state": "not_run", "reason": "no_candidate_frames"}
    assert result.index["frames"] == []


def test_auto_frame_failure_publishes_degraded_group(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    assert len(candidates) >= 2
    first = f"{candidates[0]['frame_id']}.png"
    engine = ScriptedEngine(
        {f"{candidates[1]['frame_id']}.png": [_line("ok text", 0.9, 20)]},
        fail_files={first},
    )
    result = run_ocr(TASK, output_root=root, mode="auto", _engine_factory=_factory(engine))
    assert result.index["status"] == "degraded"
    assert result.index["frames"][0]["status"] == "failed"
    assert result.index["frames"][0]["reason_codes"] == ["recognition_failed"]
    assert result.index["frames"][1]["status"] == "ok"
    assert result.index["line_count"] == 1
    assert (_ocr_root(root) / "current.json").is_file()


def test_auto_engine_unavailable_returns_degraded_and_audits(tmp_path: Path) -> None:
    root = _setup(tmp_path)

    def broken_factory(profile):
        raise OcrEngineUnavailableError("OCR_FAILED：OCR引擎依赖不可用。")

    result = run_ocr(TASK, output_root=root, mode="auto", _engine_factory=broken_factory)
    assert isinstance(result, OcrDegradedOutcome)
    assert result.status == "degraded"
    assert result.reason_codes == ("engine_unavailable",)
    assert not (_ocr_root(root) / "formal-results").exists()
    assert not (_ocr_root(root) / "current.json").exists()
    audits = _failures(root)
    assert len(audits) == 1
    assert audits[0]["stage"] == "engine_init"
    assert audits[0]["error_code"] == "OCR_FAILED"
    assert audits[0]["current_unchanged"] is True
    assert audits[0]["failure_id"] == result.failure_id


def test_auto_lazy_init_failure_returns_degraded(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    engine = ScriptedEngine({}, fail_error=OcrEngineUnavailableError("OCR_FAILED：OCR引擎初始化失败。"))
    candidates = _candidates(root)
    engine.fail_files = {f"{candidates[0]['frame_id']}.png"}
    result = run_ocr(TASK, output_root=root, mode="auto", _engine_factory=_factory(engine))
    assert isinstance(result, OcrDegradedOutcome)
    assert not (_ocr_root(root) / "formal-results").exists()
    audits = _failures(root)
    assert len(audits) == 1 and audits[0]["stage"] == "engine_init"


def test_required_engine_unavailable_raises(tmp_path: Path) -> None:
    root = _setup(tmp_path)

    def broken_factory(profile):
        raise OcrEngineUnavailableError("OCR_FAILED：OCR引擎依赖不可用。")

    with pytest.raises(OcrEngineUnavailableError):
        run_ocr(TASK, output_root=root, mode="required", _engine_factory=broken_factory)
    assert not (_ocr_root(root) / "current.json").exists()
    assert len(_failures(root)) == 1


def test_cache_hit_is_zero_rewrite(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    first = f"{candidates[0]['frame_id']}.png"
    engine = ScriptedEngine({first: [_line("cached text", 0.9, 20)]})
    factory = _factory(engine)
    first_run = run_ocr(TASK, output_root=root, _engine_factory=factory)
    assert first_run.cache_hit is False
    before = _tree_identity(root / TASK)
    engine.calls.clear()
    second = ScriptedEngine({first: [_line("different text", 0.9, 20)]})
    second_run = run_ocr(TASK, output_root=root, _engine_factory=_factory(second))
    assert second_run.cache_hit is True
    assert second_run.pointer_repaired is False
    assert second_run.index["ocr_set_id"] == first_run.index["ocr_set_id"]
    assert second.calls == []
    assert _tree_identity(root / TASK) == before


def test_missing_current_is_repaired_from_intact_group(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    engine = ScriptedEngine({})
    first_run = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine))
    group_before = _tree_identity(first_run.group_path)
    (_ocr_root(root) / "current.json").unlink()
    repaired = run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))
    assert repaired.cache_hit is True
    assert repaired.pointer_repaired is True
    assert repaired.index["ocr_set_id"] == first_run.index["ocr_set_id"]
    assert _tree_identity(repaired.group_path) == group_before


def test_binding_change_creates_new_group_and_keeps_old(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    engine_a = ScriptedEngine({}, binding={
        "engine_id": "scripted", "engine_version": "1.0", "runtime": "script",
        "runtime_version": "1.0", "model_fingerprints": {"m.onnx": "a" * 64},
    })
    engine_b = ScriptedEngine({}, binding={
        "engine_id": "scripted", "engine_version": "1.0", "runtime": "script",
        "runtime_version": "1.0", "model_fingerprints": {"m.onnx": "b" * 64},
    })
    run_a = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine_a))
    identity_a = _tree_identity(root / TASK / "m2" / "ocr" / "formal-results")
    run_b = run_ocr(TASK, output_root=root, _engine_factory=_factory(engine_b))
    assert run_b.index["ocr_set_id"] != run_a.index["ocr_set_id"]
    assert run_b.cache_hit is False
    groups = {path.name for path in (root / TASK / "m2" / "ocr" / "formal-results").iterdir()}
    assert groups == {run_a.index["ocr_set_id"], run_b.index["ocr_set_id"]}
    # 旧组身份未被改写
    for key, value in identity_a.items():
        assert _tree_identity(root / TASK / "m2" / "ocr" / "formal-results")[key] == value
    current = json.loads((_ocr_root(root) / "current.json").read_text(encoding="utf-8"))
    assert current["result_id"] == run_b.index["ocr_set_id"]


def test_tampered_group_is_cache_corrupt_and_not_overwritten(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    first = run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))
    index_path = first.index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["line_count"] = 999
    index_path.write_bytes(_json_bytes(payload))
    tampered_bytes = index_path.read_bytes()
    with pytest.raises(OcrCacheCorruptError):
        run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))
    assert index_path.read_bytes() == tampered_bytes


def test_corrupt_current_pointer_hard_fails(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))
    (_ocr_root(root) / "current.json").write_bytes(b"{not json")
    with pytest.raises(OcrCurrentPointerCorruptError):
        run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))


def test_current_directory_hard_fails(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    (_ocr_root(root)).mkdir(parents=True, exist_ok=True)
    (_ocr_root(root) / "current.json").mkdir()
    with pytest.raises(OcrCurrentPointerCorruptError):
        run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))


def test_concurrent_lock_blocks(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    locks = root / TASK / "m2" / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "ocr.lock").write_bytes(b"{}")
    with pytest.raises(OcrConcurrentRunError):
        run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))


def test_missing_frame_set_input(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    (root / TASK).mkdir(parents=True)
    with pytest.raises(InputFrameSetMissingError):
        run_ocr(TASK, output_root=root, _engine_factory=_forbidden_factory)


def test_missing_page_change_input(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _publish_frame_set(root, (0, 255, 0, 255))
    with pytest.raises(InputPageChangeSetMissingError):
        run_ocr(TASK, output_root=root, _engine_factory=_forbidden_factory)


def test_candidate_image_corrupt_hard_fails(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    frame_group = root / TASK / "m2" / "frames" / "formal-results" / "frame-set-test00000001"
    target = frame_group / "frames" / f"{candidates[0]['frame_id']}.png"
    target.write_bytes(b"rotten")
    with pytest.raises(InputFrameSetCorruptError):
        run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine({})))


def test_failure_injection_before_current_cleans_group_and_audits(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    with pytest.raises(InjectedOcrFailure):
        run_ocr(
            TASK,
            output_root=root,
            failure_injection="before_current",
            _engine_factory=_factory(ScriptedEngine({})),
        )
    formal = _ocr_root(root) / "formal-results"
    assert not formal.exists() or list(formal.iterdir()) == []
    assert not (_ocr_root(root) / "current.json").exists()
    assert list(_ocr_root(root).glob(".tmp-*")) == []
    if formal.exists():
        assert list(formal.glob(".tmp-*")) == []
    audits = _failures(root)
    assert len(audits) == 1
    assert audits[0]["stage"] == "current_publish"
    assert audits[0]["current_unchanged"] is True
    assert audits[0]["temporary_cleanup_status"] == "group_removed"


def test_keyboard_interrupt_propagates_and_audits(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    candidates = _candidates(root)
    engine = ScriptedEngine(
        {},
        fail_files={f"{candidates[0]['frame_id']}.png"},
        fail_error=KeyboardInterrupt(),
    )
    with pytest.raises(KeyboardInterrupt):
        run_ocr(TASK, output_root=root, _engine_factory=_factory(engine))
    audits = _failures(root)
    assert len(audits) == 1
    assert audits[0]["current_unchanged"] is True
    assert not (_ocr_root(root) / "current.json").exists()


def test_inputs_are_not_rewritten_by_successful_run(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    protected = root / TASK / "m2" / "frames"
    before = _tree_identity(protected)
    candidates = _candidates(root)
    scripts = {f"{candidate['frame_id']}.png": [_line("text", 0.9, 20)] for candidate in candidates}
    run_ocr(TASK, output_root=root, _engine_factory=_factory(ScriptedEngine(scripts)))
    assert _tree_identity(protected) == before


def test_invalid_arguments_rejected(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    with pytest.raises(OcrExtractorError):
        run_ocr(TASK, output_root=root, mode="sometimes", _engine_factory=_forbidden_factory)
    with pytest.raises(OcrExtractorError):
        run_ocr(TASK, output_root=root, profile_id="ocr-v9", _engine_factory=_forbidden_factory)
    with pytest.raises(OcrExtractorError):
        run_ocr(TASK, output_root=root, failure_injection="after_publish", _engine_factory=_forbidden_factory)


def test_null_engine_is_empty_and_test_only() -> None:
    engine = NullOcrEngine()
    assert engine.recognize(Path("whatever.png")) == []
    binding = engine.describe_binding()
    assert binding["engine_id"] == "null"
    assert binding["model_fingerprints"] == {}


def test_rapidocr_binding_matches_installed_package() -> None:
    pytest.importorskip("rapidocr")
    binding = extractor._rapidocr_binding()
    assert binding["engine_id"] == "rapidocr-onnxruntime"
    assert binding["runtime"] == "onnxruntime"
    assert binding["engine_version"]
    assert binding["model_fingerprints"]
    assert all(len(value) == 64 for value in binding["model_fingerprints"].values())


def test_off_and_auto_have_distinct_cache_identities(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    off = run_ocr(TASK, output_root=root, mode="off", _engine_factory=_forbidden_factory)
    auto = run_ocr(TASK, output_root=root, mode="auto", _engine_factory=_factory(ScriptedEngine({})))
    assert off.index["ocr_set_id"] != auto.index["ocr_set_id"]
    current = json.loads((_ocr_root(root) / "current.json").read_text(encoding="utf-8"))
    assert current["result_id"] == auto.index["ocr_set_id"]
