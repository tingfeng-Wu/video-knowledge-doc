"""run-m2 CLI接线定向测试（阶段15-0 E0+E1）：argparse矩阵、envelope形态与退出码。

裁决§三.7：条目失败stderr单条JSON、退出1；argparse用法错误由argparse自行退出2；
E2/E3未接线的``NotImplementedError``按F3约定原样traceback传播（本测试显式断言该
过渡形态，防止被误当作已实现的成功路径）。全程零网络、零真实FFmpeg依赖、零正式目录写。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from video_doc import cli
from video_doc import m2_pipeline
from video_doc.asr_only_pipeline import MediaTools
from video_doc.task_workspace import initialize_task_workspace

TASK_ID = "M2CLI01"


@pytest.fixture(autouse=True)
def _clean_ffmpeg_env(monkeypatch):
    monkeypatch.delenv("VIDEO_DOC_FFMPEG_DIR", raising=False)


def _snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        snapshot[rel] = "dir" if path.is_dir() else hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return snapshot


def _stub_tools(monkeypatch, tmp_path: Path) -> None:
    directory = tmp_path / "stub_ff"
    directory.mkdir(exist_ok=True)
    monkeypatch.setattr(
        m2_pipeline,
        "resolve_media_tools",
        lambda ffmpeg_dir=None: MediaTools(
            directory=directory,
            ffmpeg=directory / "ffmpeg.exe",
            ffprobe=directory / "ffprobe.exe",
        ),
    )


def _read_capsys(capsys):
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert set(payload) == {
        "status",
        "task_id",
        "failed_stage",
        "error_code",
        "reason_codes",
        "relative_paths",
        "message",
    }
    return payload, captured.out


@pytest.mark.parametrize(
    "extra",
    [
        ["--ocr", "empty"],
        ["--ocr", "off", "--vision-caption", "on"],
        [],
    ],
)
def test_usage_errors_exit_2(tmp_path, capsys, extra):
    argv = ["run-m2", TASK_ID, "--output-root", str(tmp_path)]
    if not extra:
        argv = ["run-m2", "--output-root", str(tmp_path)]  # 缺task_id位置参数
    with pytest.raises(SystemExit) as exc:
        cli.main(argv + extra if extra else argv)
    assert exc.value.code == 2


def test_new_task_missing_source_envelope_exit_1_and_zero_write(tmp_path, capsys):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    before = _snapshot(tmp_path)
    code = cli.main(["run-m2", TASK_ID, "--output-root", str(output_root)])
    assert code == 1
    payload, out = _read_capsys(capsys)
    assert payload["status"] == "failed"
    assert payload["task_id"] == TASK_ID
    assert payload["failed_stage"] == "preflight"
    assert payload["error_code"] == "SOURCE_VIDEO_REQUIRED"
    assert payload["relative_paths"] == []
    assert json.loads(out.splitlines()[0]) == {
        "stage": "preflight",
        "status": "started",
    }
    assert _snapshot(tmp_path) == before
    assert not (output_root / TASK_ID).exists()


def test_missing_ffmpeg_dir_envelope_media_tool_invalid_no_path_leak(
    tmp_path, capsys
):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    source = tmp_path / "src.mp4"
    source.write_bytes(b"x" * 16)
    before = _snapshot(tmp_path)
    code = cli.main(
        [
            "run-m2",
            TASK_ID,
            "--output-root",
            str(output_root),
            "--source-video",
            str(source),
            "--ffmpeg-dir",
            str(tmp_path / "no-such-dir"),
        ]
    )
    assert code == 1
    payload, _ = _read_capsys(capsys)
    assert payload["error_code"] == "MEDIA_TOOL_INVALID"
    assert str(tmp_path) not in payload["message"]
    assert "<path>" not in payload["message"]  # 条目消息本身即无路径，非靠兜底替换
    assert _snapshot(output_root) == {}


def test_existing_task_mismatch_envelope_and_task_json_unchanged(tmp_path, capsys):
    output_root = tmp_path / "outputs"
    source_a = tmp_path / "a.mp4"
    source_a.write_bytes(b"x" * 32)
    source_b = tmp_path / "b.mp4"
    source_b.write_bytes(b"y" * 32)
    initialize_task_workspace(output_root, TASK_ID, source_video=source_a)
    task_json = output_root / TASK_ID / "task.json"
    task_json_before = task_json.read_bytes()
    before = _snapshot(output_root)
    code = cli.main(
        [
            "run-m2",
            TASK_ID,
            "--output-root",
            str(output_root),
            "--source-video",
            str(source_b),
        ]
    )
    assert code == 1
    payload, _ = _read_capsys(capsys)
    assert payload["error_code"] == "SOURCE_VIDEO_MISMATCH"
    assert task_json.read_bytes() == task_json_before
    assert _snapshot(output_root) == before
    assert not (output_root / TASK_ID / "m2").exists()


def test_invalid_task_id_falls_back_to_plain_failure_line(tmp_path, capsys):
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    code = cli.main(["run-m2", "bad id!", "--output-root", str(output_root)])
    assert code == 1
    captured = capsys.readouterr()
    # TaskWorkspaceError属既有类型化族：main()文本兜底，不保证JSON
    assert captured.err.startswith("处理失败：")
    assert _snapshot(output_root) == {}


def _stub_probe(monkeypatch, *, has_audio: bool = True) -> None:
    from video_doc.video_probe import VideoMetadata

    def fake_probe(path):
        p = Path(path)
        return VideoMetadata(
            source_path=str(p),
            file_name=p.name,
            file_size_bytes=p.stat().st_size,
            duration_seconds=10.0,
            width=640,
            height=360,
            frame_rate=25.0,
            video_codec="h264",
            has_audio=has_audio,
            audio_codec="aac",
            audio_sample_rate=16000,
        )

    monkeypatch.setattr(m2_pipeline, "probe_video", fake_probe)


def _json_docs(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    docs: list[dict] = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos] in " \r\n\t":
            pos += 1
        if pos >= len(text):
            break
        obj, pos = decoder.raw_decode(text, pos)
        docs.append(obj)
    return docs


def test_dry_run_prints_progress_lines_then_single_plan_json_exit_0(
    tmp_path, capsys, monkeypatch
):
    """E2（§三.3/E02/E03）：dry-run进度行＋单条运行计划JSON、退出0、零写入。"""

    output_root = tmp_path / "outputs"
    output_root.mkdir()
    source = tmp_path / "src.mp4"
    source.write_bytes(b"x" * 16)
    _stub_tools(monkeypatch, tmp_path)
    _stub_probe(monkeypatch)
    before = _snapshot(tmp_path)
    code = cli.main(
        [
            "run-m2",
            TASK_ID,
            "--output-root",
            str(output_root),
            "--source-video",
            str(source),
            "--dry-run",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    docs = _json_docs(captured.out)
    progress_lines = docs[:-1]
    assert progress_lines[:2] == [
        {"stage": "preflight", "status": "started"},
        {"stage": "preflight", "status": "completed"},
    ]
    plan = docs[-1]
    assert plan["schema_name"] == "M2RunPlan"
    assert plan["dry_run"] is True
    assert [entry["stage"] for entry in plan["stages"]] == list(
        m2_pipeline.M2_STAGE_SEQUENCE
    )
    assert _snapshot(tmp_path) == before
    assert not (output_root / TASK_ID).exists()
    # JSONL日志在dry-run不产生（实现决定8/§三.5：task_init之前零落盘）。
    assert not (output_root / TASK_ID / "m2" / "logs" / "run-m2.jsonl").exists()


def test_normal_run_module_typed_failure_wrapped_into_envelope(
    tmp_path, capsys, monkeypatch
):
    """E3（§三.7）：模块类型化失败原码包裹入同一7键envelope、退出1。

    无音轨形态沿用asr-only条目族（无``error_code``属性）：``error_code``如实为空串、
    reason_codes回退为阶段名（登记实现决定）；task_init写入先行、transcript之前
    无任何m2产物（失败不发布、不切current）。
    """

    output_root = tmp_path / "outputs"
    output_root.mkdir()
    source = tmp_path / "src.mp4"
    source.write_bytes(b"x" * 16)
    _stub_tools(monkeypatch, tmp_path)
    _stub_probe(monkeypatch, has_audio=False)
    code = cli.main(
        [
            "run-m2",
            TASK_ID,
            "--output-root",
            str(output_root),
            "--source-video",
            str(source),
        ]
    )
    assert code == 1
    payload, out = _read_capsys(capsys)
    assert payload["status"] == "failed"
    assert payload["failed_stage"] == "transcript"
    assert payload["error_code"] == ""
    assert payload["reason_codes"] == ["transcript"]
    assert payload["relative_paths"] == []
    assert str(tmp_path) not in payload["message"]
    assert (output_root / TASK_ID / "task.json").is_file()
    # m2下仅日志目录：失败不发布任何结果组、不切任何current。
    assert sorted(p.name for p in (output_root / TASK_ID / "m2").iterdir()) == ["logs"]
    docs = _json_docs(out)
    stage_statuses = [(d["stage"], d["status"]) for d in docs]
    assert stage_statuses[:2] == [
        ("preflight", "started"),
        ("preflight", "completed"),
    ]
    assert ("task_init", "started") in stage_statuses
    assert ("transcript", "started") in stage_statuses
    # JSONL失败事件已落盘（task_init后），不发布任何结果组。
    log_path = output_root / TASK_ID / "m2" / "logs" / "run-m2.jsonl"
    assert log_path.is_file()
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["stage"] == "transcript"
    assert events[-1]["status"] == "failed"
    assert log_path.read_bytes().count(b"\r") == 0  # LF铁律
