"""阶段12：`run-ocr`命令行验收。

覆盖off/空候选的真实CLI路径、成功摘要、缓存命中、auto降级退出码0、
required失败退出码1、输入缺失类型化失败、用法错误退出码2、中断传播
与错误输出安全。OCR算法与结果组语义由`tests/test_ocr_extractor.py`覆盖，
本文件只验证CLI接线与失败边界。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
from pathlib import Path

import numpy as np
import pytest

from video_doc import cli
from video_doc.ocr_extractor import OcrLine, run_ocr
from video_doc.page_change_detector import ProxyFrame, detect_page_changes

TASK = "SYNTH01"
_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _frame_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def _publish_frame_set(root: Path, values: tuple[int, ...]) -> None:
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


def _proxy_loader(values: tuple[int, ...]):
    proxies = {
        f"fr{ordinal:06d}.png": ProxyFrame(
            pixels=np.full((8, 8), value, dtype=np.uint8),
            perceptual_hash=tuple(bool((index >> (ordinal % 6)) & 1) for index in range(64)),
        )
        for ordinal, value in enumerate(values, start=1)
    }

    def load(path: Path, width: int) -> ProxyFrame:
        return proxies[path.name]

    return load


def _setup(tmp_path: Path, values: tuple[int, ...] = (0, 255, 0, 255, 0, 255, 0, 255)) -> Path:
    root = tmp_path / "outputs"
    _publish_frame_set(root, values)
    detect_page_changes(TASK, output_root=root, _proxy_loader=_proxy_loader(values))
    return root


def _candidates(root: Path) -> list[dict]:
    current = json.loads((root / TASK / "m2" / "frames" / "page-changes" / "current.json").read_text(encoding="utf-8"))
    group = root / TASK / "m2" / "frames" / "page-changes" / current["group"]
    index = json.loads((group / "page-change-set.json").read_text(encoding="utf-8"))
    return index["candidates"]


class _OneLineEngine:
    engine_id = "scripted"

    def describe_binding(self) -> dict:
        return {
            "engine_id": "scripted",
            "engine_version": "1.0",
            "runtime": "script",
            "runtime_version": "1.0",
            "model_fingerprints": {"fake.onnx": "f" * 64},
        }

    def recognize(self, image_path: Path) -> list[OcrLine]:
        return [OcrLine(text=f"text {image_path.name}", confidence=0.9, box=((10, 10), (200, 10), (200, 50), (10, 50)))]


class _AlwaysFailEngine(_OneLineEngine):
    def recognize(self, image_path: Path) -> list[OcrLine]:
        raise RuntimeError("engine boom")


def _patched_run_ocr(monkeypatch: pytest.MonkeyPatch, engine_factory) -> None:
    def wrapper(task_id, *, output_root="outputs", mode="auto"):
        return run_ocr(task_id, output_root=output_root, mode=mode, _engine_factory=engine_factory)

    monkeypatch.setattr(cli, "run_ocr", wrapper)


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_off_mode_real_cli_publishes_disabled(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    code, out, err = _invoke(["run-ocr", TASK, "--output-root", str(root), "--mode", "off"])
    assert code == 0, err
    summary = json.loads(out)
    assert summary["status"] == "disabled"
    assert summary["engine_state"] == "not_run"
    assert summary["engine_id"] is None
    assert summary["reason_codes"] == ["engine_disabled"]
    assert (root / TASK / "m2" / "ocr" / "current.json").is_file()


def test_no_candidates_real_cli_publishes_empty(tmp_path: Path) -> None:
    root = _setup(tmp_path, values=(0, 0, 0, 0, 0, 0))
    code, out, err = _invoke(["run-ocr", TASK, "--output-root", str(root)])
    assert code == 0, err
    summary = json.loads(out)
    assert summary["status"] == "empty"
    assert summary["reason_codes"] == ["no_candidate_frames"]
    assert summary["frame_count"] == 0


def test_success_summary_and_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _setup(tmp_path)
    _patched_run_ocr(monkeypatch, lambda profile: _OneLineEngine())
    argv = ["run-ocr", TASK, "--output-root", str(root)]
    code, out, err = _invoke(argv)
    assert code == 0, err
    first = json.loads(out)
    assert first["status"] == "success"
    assert first["module"] == "ocr"
    assert first["ocr_set_id"].startswith("ocr-set-")
    assert first["engine_id"] == "scripted"
    assert first["frame_count"] == len(_candidates(root))
    assert first["line_count"] == first["frame_count"]
    assert first["cache_hit"] is False
    assert first["group_relative_path"] == f"m2/ocr/formal-results/{first['ocr_set_id']}"
    code2, out2, _ = _invoke(argv)
    assert code2 == 0
    second = json.loads(out2)
    assert second["cache_hit"] is True
    assert second["ocr_set_id"] == first["ocr_set_id"]


def test_auto_engine_failure_degrades_with_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _setup(tmp_path)

    def broken(profile):
        from video_doc.ocr_extractor import OcrEngineUnavailableError

        raise OcrEngineUnavailableError("OCR_FAILED：OCR引擎依赖不可用。")

    _patched_run_ocr(monkeypatch, broken)
    code, out, err = _invoke(["run-ocr", TASK, "--output-root", str(root), "--mode", "auto"])
    assert code == 0, err
    summary = json.loads(out)
    assert summary["status"] == "degraded"
    assert summary["reason_codes"] == ["engine_unavailable"]
    assert summary["next_action"] == "continue_text_pipeline"
    assert summary["failure_id"].startswith("failure-")
    assert not (root / TASK / "m2" / "ocr" / "current.json").exists()


def test_required_frame_failure_exits_one_with_stderr_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _setup(tmp_path)
    _patched_run_ocr(monkeypatch, lambda profile: _AlwaysFailEngine())
    code, out, err = _invoke(["run-ocr", TASK, "--output-root", str(root), "--mode", "required"])
    assert code == 1
    assert out == ""
    summary = json.loads(err)
    assert summary["status"] == "failed"
    assert summary["error_code"] == "OCR_FAILED"
    assert summary["module"] == "ocr"
    assert summary["next_action"] == "continue_text_pipeline"
    assert not (root / TASK / "m2" / "ocr" / "current.json").exists()


def test_missing_input_exits_one_with_typed_code(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _publish_frame_set(root, (0, 255, 0, 255))
    code, out, err = _invoke(["run-ocr", TASK, "--output-root", str(root)])
    assert code == 1
    assert out == ""
    summary = json.loads(err)
    assert summary["error_code"] == "INPUT_PAGE_CHANGE_SET_MISSING"
    assert summary["recoverable"] is True
    assert summary["next_action"] == "run_page_change_detector"


def test_usage_error_exits_two(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    with pytest.raises(SystemExit) as info:
        _invoke(["run-ocr", TASK, "--output-root", str(root), "--mode", "sometimes"])
    assert info.value.code == 2


def test_keyboard_interrupt_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _setup(tmp_path)

    class _InterruptEngine(_OneLineEngine):
        def recognize(self, image_path: Path):
            raise KeyboardInterrupt()

    _patched_run_ocr(monkeypatch, lambda profile: _InterruptEngine())
    with pytest.raises(KeyboardInterrupt):
        _invoke(["run-ocr", TASK, "--output-root", str(root)])


def test_failure_output_has_no_absolute_paths_or_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _setup(tmp_path)
    _patched_run_ocr(monkeypatch, lambda profile: _AlwaysFailEngine())
    code, out, err = _invoke(["run-ocr", TASK, "--output-root", str(root), "--mode", "required"])
    assert code == 1
    assert str(root) not in err
    assert not _ABSOLUTE.search(err)
    assert "sk-" not in err
