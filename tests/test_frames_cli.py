"""阶段11-F3：`sample-frames`与`detect-page-changes`命令行验收。

覆盖首次运行、缓存命中零改写、current缺失修复、损坏正式组、损坏current、
非普通文件current、FFmpeg缺失、FFmpeg执行失败、中断传播、并发锁，以及退出码、
错误输出安全性和成功JSON摘要。

本文件只验证CLI接线和失败边界；采样与页面变化算法本身由
`tests/test_frame_sampler.py`和`tests/test_page_change_detector.py`覆盖。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from video_doc import cli
from video_doc import frame_sampler as sampler
from video_doc.task_workspace import initialize_task_workspace


SYNTHETIC_DURATION_SECONDS = 32
SYNTHETIC_WIDTH = 320
SYNTHETIC_HEIGHT = 240
SYNTHETIC_EXPECTED_FRAMES = 8
TASK_ID = "SYNTH01"


@pytest.fixture(scope="module")
def synthetic_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """生成一个短片夹具，避免自动测试依赖data/raw。"""

    directory = tmp_path_factory.mktemp("frames-cli-fixture")
    path = directory / "SYNTH01.mp4"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={SYNTHETIC_WIDTH}x{SYNTHETIC_HEIGHT}"
        f":rate=10:duration={SYNTHETIC_DURATION_SECONDS}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-y",
        str(path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"本机FFmpeg无法生成合成视频夹具：{exc}")
    return path


class Published(NamedTuple):
    output_root: Path
    summary: dict[str, object]


@pytest.fixture(scope="module")
def published(
    tmp_path_factory: pytest.TempPathFactory, synthetic_video: Path
) -> Published:
    """通过CLI完成一次真实首次发布，后续用例复制它以避免重复抽帧。"""

    output_root = tmp_path_factory.mktemp("frames-cli-published") / "outputs"
    assert (
        cli.main(
            [
                "init-task",
                TASK_ID,
                "--output-root",
                str(output_root),
                "--source-video",
                str(synthetic_video),
            ]
        )
        == 0
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(["sample-frames", TASK_ID, "--output-root", str(output_root)])
    assert code == 0, buffer.getvalue()
    return Published(output_root=output_root, summary=json.loads(buffer.getvalue()))


# ---------------------------------------------------------------- 通用助手


def _run_cli(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    """执行CLI并返回(退出码, stdout, stderr)；用法错误按argparse的退出码处理。"""

    try:
        code = cli.main(list(argv))
    except SystemExit as exc:  # argparse用法错误
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _copy_task(tmp_path: Path, output_root: Path) -> Path:
    """复制已发布的任务目录，避免每个用例重复执行真实抽帧。"""

    destination = tmp_path / "outputs"
    shutil.copytree(output_root / TASK_ID, destination / TASK_ID)
    return destination


def _frames_root(output_root: Path) -> Path:
    return output_root / TASK_ID / "m2" / "frames"


def _page_changes_root(output_root: Path) -> Path:
    return _frames_root(output_root) / "page-changes"


def _current_path(output_root: Path) -> Path:
    return _frames_root(output_root) / "current.json"


def _page_changes_current_path(output_root: Path) -> Path:
    return _page_changes_root(output_root) / "current.json"


def _failure_audits(output_root: Path, module: str = "frames") -> list[Path]:
    return sorted(
        (output_root / TASK_ID / "m2" / "failures" / module).glob("*/failure.json")
    )


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    """记录“相对路径 → (字节数, mtime_ns)”，用于判定零改写。"""

    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_pointer(output_root: Path) -> dict[str, object]:
    pointer = sampler.load_frame_set_current(output_root, TASK_ID)
    assert pointer is not None
    return pointer


def _write_pointer(output_root: Path, pointer: object) -> None:
    _current_path(output_root).write_text(
        json.dumps(pointer, ensure_ascii=False), encoding="utf-8"
    )


def _assert_current_target_is_intact(output_root: Path) -> str:
    """复读current，确认其指向的组、manifest和全部帧文件都真实存在。"""

    pointer = _load_pointer(output_root)
    group = _frames_root(output_root) / str(pointer["group"])
    assert group.is_dir()
    manifest_path = group / "manifest.json"
    assert manifest_path.is_file()
    assert _sha256(manifest_path) == pointer["manifest_sha256"]
    index = json.loads((group / "frame-set.json").read_text(encoding="utf-8"))
    assert index["frame_set_id"] == pointer["result_id"]
    assert index["frames"]
    for frame in index["frames"]:
        assert (group / frame["file"]).is_file()
    return str(pointer["result_id"])


def _assert_safe_output(text: str, *roots: Path) -> None:
    """错误输出不得包含绝对路径、本地根目录或API凭据。"""

    for root in roots:
        assert str(root) not in text
        assert str(root.resolve()) not in text
    assert not re.search(r"[A-Za-z]:[\\/]", text)
    assert "sk-" not in text


def _assert_failure(
    code: int,
    out: str,
    err: str,
    *,
    error_code: str,
    output_root: Path,
) -> dict[str, object]:
    assert code == 1
    assert out == ""
    status = json.loads(err)
    assert status["status"] == "failed"
    assert status["error_code"] == error_code
    assert status["task_id"] == TASK_ID
    assert isinstance(status["safe_message"], str) and status["safe_message"]
    assert isinstance(status["recoverable"], bool)
    assert status["next_action"]
    _assert_safe_output(err, output_root)
    return status


def _register_shorter_profile() -> str:
    """注册临时profile，用于在已有正式组的任务上生成新的结果组身份。"""

    shorter = replace(
        sampler.LOW_FREQUENCY_V1, profile_id="test-cli-10s", base_interval_ms=10_000
    )
    sampler.SAMPLING_PROFILES[shorter.profile_id] = shorter
    return shorter.profile_id


# ---------------------------------------------------------------- 首次运行


def test_usage_errors_exit_with_two(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ("sample-frames",),
        ("sample-frames", TASK_ID, "--profile", "not-a-profile"),
        ("detect-page-changes",),
    ):
        code, out, err = _run_cli(capsys, *argv)
        assert code == 2, argv
        assert out == ""
        assert err


def test_first_run_publishes_frame_set(published: Published) -> None:
    output_root = published.output_root
    summary = published.summary

    assert summary["status"] == "ok"
    assert summary["module"] == "frames"
    assert summary["task_id"] == TASK_ID
    assert str(summary["frame_set_id"]).startswith("frame-set-")
    assert summary["profile_id"] == sampler.LOW_FREQUENCY_V1.profile_id
    assert summary["frame_count"] == SYNTHETIC_EXPECTED_FRAMES
    assert summary["cache_hit"] is False
    assert summary["pointer_repaired"] is False
    assert summary["group_relative_path"] == (
        f"m2/frames/formal-results/{summary['frame_set_id']}"
    )

    group = _frames_root(output_root) / "formal-results" / str(summary["frame_set_id"])
    assert group.is_dir()
    assert {entry.name for entry in group.iterdir()} == {
        "frame-set.json",
        "manifest.json",
        "frames",
    }
    assert len(list((group / "frames").glob("*.png"))) == SYNTHETIC_EXPECTED_FRAMES
    assert _assert_current_target_is_intact(output_root) == summary["frame_set_id"]
    # 成功路径不写失败审计，也不残留临时目录或锁
    assert _failure_audits(output_root) == []
    assert not list((_frames_root(output_root) / "formal-results").glob(".tmp-*"))
    assert not (_frames_root(output_root).parent / ".locks" / "frames.lock").exists()


# ---------------------------------------------------------------- 缓存与修复


def test_repeat_run_is_cache_hit_with_zero_rewrite(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    before = _tree_state(output_root / TASK_ID / "m2")
    current_sha = _sha256(_current_path(output_root))

    code, out, err = _run_cli(
        capsys, "sample-frames", TASK_ID, "--output-root", str(output_root)
    )

    assert (code, err) == (0, "")
    summary = json.loads(out)
    assert summary["cache_hit"] is True
    assert summary["pointer_repaired"] is False
    assert summary["frame_set_id"] == _load_pointer(output_root)["result_id"]
    assert _tree_state(output_root / TASK_ID / "m2") == before
    assert _sha256(_current_path(output_root)) == current_sha
    assert _failure_audits(output_root) == []


def test_missing_current_is_repaired_without_rewriting_group(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    current_before = _current_path(output_root).read_bytes()
    group = _frames_root(output_root) / "formal-results" / str(
        _load_pointer(output_root)["result_id"]
    )
    group_before = _tree_state(group)
    _current_path(output_root).unlink()

    code, out, err = _run_cli(
        capsys, "sample-frames", TASK_ID, "--output-root", str(output_root)
    )

    assert (code, err) == (0, "")
    summary = json.loads(out)
    assert summary["cache_hit"] is True
    assert summary["pointer_repaired"] is True
    assert _current_path(output_root).read_bytes() == current_before
    assert _tree_state(group) == group_before
    assert _assert_current_target_is_intact(output_root) == summary["frame_set_id"]
    assert _failure_audits(output_root) == []


# ---------------------------------------------------------------- 损坏current


def _corrupt_pointer_cases() -> list[tuple[str, object]]:
    """构造requirement 3列出的全部损坏形态。"""

    valid = {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "frame_set",
        "result_id": "frame-set-000000000000",
        "group": "formal-results/frame-set-000000000000",
        "manifest_sha256": "0" * 64,
    }
    missing_key = {key: value for key, value in valid.items() if key != "group"}
    extra_key = {**valid, "extra": 1}
    bad_schema = {**valid, "schema_version": "9.9"}
    bad_artifact = {**valid, "artifact_type": "page_change_set"}
    bad_group = {**valid, "group": "formal-results/frame-set-ffffffffffff"}
    bad_hash = {**valid, "manifest_sha256": "f" * 64}
    return [
        ("非法JSON", "{not json"),
        ("非对象", [1, 2, 3]),
        ("字段缺失", missing_key),
        ("字段多余", extra_key),
        ("Schema错误", bad_schema),
        ("artifact_type错误", bad_artifact),
        ("group错误", bad_group),
        ("manifest哈希错误", bad_hash),
    ]


@pytest.mark.parametrize("label,payload", _corrupt_pointer_cases())
def test_corrupt_current_hard_fails(
    tmp_path: Path,
    published: Published,
    capsys: pytest.CaptureFixture[str],
    label: str,
    payload: object,
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    group = _frames_root(output_root) / "formal-results" / str(
        _load_pointer(output_root)["result_id"]
    )
    group_before = _tree_state(group)
    if isinstance(payload, str):
        _current_path(output_root).write_text(payload, encoding="utf-8")
    else:
        _write_pointer(output_root, payload)
    current_before = _current_path(output_root).read_bytes()

    code, out, err = _run_cli(
        capsys, "sample-frames", TASK_ID, "--output-root", str(output_root)
    )

    _assert_failure(
        code, out, err, error_code="CURRENT_POINTER_CORRUPT", output_root=output_root
    )
    assert _current_path(output_root).read_bytes() == current_before
    assert _tree_state(group) == group_before
    assert not list((_frames_root(output_root) / "formal-results").glob(".tmp-*"))


def test_current_pointing_at_missing_group_hard_fails(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    pointer = _load_pointer(output_root)
    result_id = "frame-set-ffffffffffff"
    pointer["result_id"] = result_id
    pointer["group"] = f"formal-results/{result_id}"
    _write_pointer(output_root, pointer)

    code, out, err = _run_cli(
        capsys, "sample-frames", TASK_ID, "--output-root", str(output_root)
    )

    _assert_failure(
        code, out, err, error_code="CURRENT_POINTER_CORRUPT", output_root=output_root
    )
    # 损坏指针按原样保留，不猜着修复；已发布的正式组仍然完好
    assert json.loads(_current_path(output_root).read_text(encoding="utf-8")) == pointer
    assert (
        _frames_root(output_root) / "formal-results" / str(published.summary["frame_set_id"])
    ).is_dir()
    assert not list((_frames_root(output_root) / "formal-results").glob(".tmp-*"))


def test_current_that_is_not_a_regular_file_hard_fails(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    pointer = _load_pointer(output_root)
    group = _frames_root(output_root) / str(pointer["group"])
    group_before = _tree_state(group)
    _current_path(output_root).unlink()
    _current_path(output_root).mkdir()

    code, out, err = _run_cli(
        capsys, "sample-frames", TASK_ID, "--output-root", str(output_root)
    )

    _assert_failure(
        code, out, err, error_code="CURRENT_POINTER_CORRUPT", output_root=output_root
    )
    # 非普通文件按损坏保留原样，既不当成“没有current”也不删除，也不新建组
    assert _current_path(output_root).is_dir()
    assert _tree_state(group) == group_before
    assert not list((_frames_root(output_root) / "formal-results").glob(".tmp-*"))
    assert len(list((_frames_root(output_root) / "formal-results").iterdir())) == 1


def test_load_frame_set_current_reports_corrupt_pointer_as_hard_failure(
    tmp_path: Path, published: Published
) -> None:
    """公开读取入口不得把损坏指针降级为“没有current”。"""

    output_root = _copy_task(tmp_path, published.output_root)
    _current_path(output_root).write_text("{not json", encoding="utf-8")

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, TASK_ID)

    _current_path(output_root).unlink()
    _current_path(output_root).mkdir()
    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, TASK_ID)

    assert sampler.load_frame_set_current(output_root, "OTHER01") is None


# ---------------------------------------------------------------- 损坏正式组


def test_corrupt_frame_file_hard_fails_and_preserves_current(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    pointer = _load_pointer(output_root)
    group = _frames_root(output_root) / str(pointer["group"])
    victim = sorted((group / "frames").glob("*.png"))[3]
    original = victim.read_bytes()
    victim.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
    current_before = _current_path(output_root).read_bytes()

    code, out, err = _run_cli(
        capsys, "sample-frames", TASK_ID, "--output-root", str(output_root)
    )

    _assert_failure(code, out, err, error_code="CACHE_CORRUPT", output_root=output_root)
    assert victim.read_bytes() == original[:-1] + bytes([original[-1] ^ 0xFF])
    assert _current_path(output_root).read_bytes() == current_before


# ---------------------------------------------------------------- FFmpeg边界


def test_missing_ffmpeg_directory_is_a_typed_failure(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    current_before = _current_path(output_root).read_bytes()
    absent = tmp_path / "no-such-ffmpeg"

    code, out, err = _run_cli(
        capsys,
        "sample-frames",
        TASK_ID,
        "--output-root",
        str(output_root),
        "--ffmpeg-dir",
        str(absent),
    )

    status = _assert_failure(
        code, out, err, error_code="FRAME_EXTRACTION_FAILED", output_root=output_root
    )
    # 类型化失败必须说明怎么恢复，且不含任何目录
    assert status["recoverable"] is True
    assert status["next_action"] == "continue_text_pipeline"
    assert "--ffmpeg-dir" in str(status["safe_message"])
    assert _current_path(output_root).read_bytes() == current_before


def test_ffmpeg_directory_without_tools_is_a_typed_failure(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    empty = tmp_path / "empty-ffmpeg"
    empty.mkdir()

    code, out, err = _run_cli(
        capsys,
        "sample-frames",
        TASK_ID,
        "--output-root",
        str(output_root),
        "--ffmpeg-dir",
        str(empty),
    )

    _assert_failure(
        code, out, err, error_code="FRAME_EXTRACTION_FAILED", output_root=output_root
    )


def test_ffmpeg_execution_failure_is_a_typed_cli_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    published: Published,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """工具可解析但执行失败：退出码1、安全JSON，且不发布新组、不切换current。"""

    output_root = _copy_task(tmp_path, published.output_root)
    profile_id = _register_shorter_profile()
    old_result_id = str(published.summary["frame_set_id"])
    current_before = _current_path(output_root).read_bytes()
    group_before = _tree_state(_frames_root(output_root) / "formal-results")
    real_run = subprocess.run

    def _failing_run(command: object, *args: object, **kwargs: object) -> object:
        if isinstance(command, (list, tuple)) and "-frames:v" in command:
            raise OSError("模拟FFmpeg执行失败。")
        return real_run(command, *args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(sampler.subprocess, "run", _failing_run)

    code, out, err = _run_cli(
        capsys,
        "sample-frames",
        TASK_ID,
        "--output-root",
        str(output_root),
        "--profile",
        profile_id,
    )

    status = _assert_failure(
        code, out, err, error_code="FRAME_EXTRACTION_FAILED", output_root=output_root
    )
    assert status["recoverable"] is True
    assert status["next_action"] == "continue_text_pipeline"
    # 执行失败发生在锁与审计边界之内：必须留下失败审计，且既有正式组与current不被改写
    audits = _failure_audits(output_root)
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["stage"] == "frame_extraction"
    assert audit["error_code"] == "FRAME_EXTRACTION_FAILED"
    assert audit["previous_current_id"] == old_result_id
    assert audit["current_unchanged"] is True
    assert _current_path(output_root).read_bytes() == current_before
    assert _assert_current_target_is_intact(output_root) == old_result_id
    assert _tree_state(_frames_root(output_root) / "formal-results") == group_before
    _assert_safe_output(err, output_root)


def test_resolve_media_tools_failure_is_a_frame_sampler_error(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """模块边界：FFmpeg解析失败不得泄漏编排层异常类型，也不得泄漏目录。"""

    initialize_task_workspace(tmp_path, TASK_ID, source_video=synthetic_video)
    with pytest.raises(sampler.FrameExtractionFailedError) as error:
        sampler.sample_frames(
            TASK_ID, output_root=tmp_path, ffmpeg_dir="no-such-ffmpeg-dir"
        )
    assert str(error.value).startswith("FRAME_EXTRACTION_FAILED")
    assert "no-such-ffmpeg-dir" not in str(error.value)
    assert not (_frames_root(tmp_path) / "formal-results").exists()


# ---------------------------------------------------------------- 并发锁


def test_existing_lock_blocks_cli_without_touching_results(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    locks = output_root / TASK_ID / "m2" / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock_path = locks / "frames.lock"
    lock_path.write_text("{}", encoding="utf-8")
    before = _tree_state(_frames_root(output_root))

    code, out, err = _run_cli(
        capsys, "sample-frames", TASK_ID, "--output-root", str(output_root)
    )

    status = _assert_failure(
        code, out, err, error_code="CONCURRENT_RUN", output_root=output_root
    )
    assert status["recoverable"] is True
    assert status["next_action"] == "await_user_confirmation"
    assert lock_path.exists()
    assert _tree_state(_frames_root(output_root)) == before
    assert _failure_audits(output_root) == []


# ---------------------------------------------------------------- 中断传播


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_interrupt_during_current_publish_propagates_unchanged(
    tmp_path: Path,
    published: Published,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    """CLI不得吞掉中断；中断后旧current仍指向完好的正式组。"""

    output_root = _copy_task(tmp_path, published.output_root)
    profile_id = _register_shorter_profile()
    current_before = _current_path(output_root).read_bytes()
    old_result_id = published.summary["frame_set_id"]

    def _interrupting_replace(frames_root: Path, pointer: object) -> None:
        raise error_type("模拟发布过程中断。")

    try:
        monkeypatch.setattr(sampler, "_replace_current", _interrupting_replace)
        with pytest.raises(error_type):
            cli.main(
                [
                    "sample-frames",
                    TASK_ID,
                    "--output-root",
                    str(output_root),
                    "--profile",
                    profile_id,
                ]
            )
    finally:
        sampler.SAMPLING_PROFILES.pop(profile_id, None)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert _current_path(output_root).read_bytes() == current_before
    assert _assert_current_target_is_intact(output_root) == old_result_id
    # 本次新建但未被current引用的组已按磁盘事实回收
    assert not list((_frames_root(output_root) / "formal-results").glob(".tmp-*"))
    assert len(list((_frames_root(output_root) / "formal-results").iterdir())) == 1

    audits = _failure_audits(output_root)
    assert audits
    audit = json.loads(audits[-1].read_text(encoding="utf-8"))
    assert audit["stage"] == "current_publish"
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "group_removed"
    assert audit["previous_current_id"] == old_result_id
    _assert_safe_output(json.dumps(audit, ensure_ascii=False), output_root)


# ---------------------------------------------------------------- 页面变化


def test_detect_page_changes_first_run_and_cache_hit(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)

    code, out, err = _run_cli(
        capsys, "detect-page-changes", TASK_ID, "--output-root", str(output_root)
    )

    assert (code, err) == (0, "")
    summary = json.loads(out)
    assert summary["status"] == "ok"
    assert summary["module"] == "page-changes"
    assert str(summary["page_change_set_id"]).startswith("page-change-set-")
    assert summary["frame_set_id"] == published.summary["frame_set_id"]
    assert summary["comparison_count"] == SYNTHETIC_EXPECTED_FRAMES - 1
    assert summary["candidate_count"] <= summary["comparison_count"]
    assert summary["cache_hit"] is False
    assert summary["group_relative_path"] == (
        f"m2/frames/page-changes/formal-results/{summary['page_change_set_id']}"
    )
    current = _page_changes_current_path(output_root)
    assert current.is_file()
    assert json.loads(current.read_text(encoding="utf-8"))["result_id"] == (
        summary["page_change_set_id"]
    )
    assert _failure_audits(output_root, "page-changes") == []

    group = _page_changes_root(output_root) / "formal-results" / str(
        summary["page_change_set_id"]
    )
    group_before = _tree_state(group)
    current_before = current.read_bytes()

    code, out, err = _run_cli(
        capsys, "detect-page-changes", TASK_ID, "--output-root", str(output_root)
    )
    assert (code, err) == (0, "")
    repeated = json.loads(out)
    assert repeated["cache_hit"] is True
    assert repeated["pointer_repaired"] is False
    assert repeated["page_change_set_id"] == summary["page_change_set_id"]
    assert _tree_state(group) == group_before
    assert current.read_bytes() == current_before


def test_detect_page_changes_missing_current_is_repaired(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    assert (
        _run_cli(
            capsys, "detect-page-changes", TASK_ID, "--output-root", str(output_root)
        )[0]
        == 0
    )
    current_before = _page_changes_current_path(output_root).read_bytes()
    _page_changes_current_path(output_root).unlink()

    code, out, err = _run_cli(
        capsys, "detect-page-changes", TASK_ID, "--output-root", str(output_root)
    )

    assert (code, err) == (0, "")
    summary = json.loads(out)
    assert summary["cache_hit"] is True
    assert summary["pointer_repaired"] is True
    assert _page_changes_current_path(output_root).read_bytes() == current_before


def test_detect_page_changes_corrupt_current_hard_fails(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    assert (
        _run_cli(
            capsys, "detect-page-changes", TASK_ID, "--output-root", str(output_root)
        )[0]
        == 0
    )
    current = _page_changes_current_path(output_root)
    group = _only_page_change_group(output_root)
    group_before = _tree_state(group)
    current.unlink()
    current.mkdir()

    code, out, err = _run_cli(
        capsys, "detect-page-changes", TASK_ID, "--output-root", str(output_root)
    )

    _assert_failure(
        code, out, err, error_code="CURRENT_POINTER_CORRUPT", output_root=output_root
    )
    assert current.is_dir()
    assert _tree_state(group) == group_before
    assert not list(group.parent.glob(".tmp-*"))


def test_detect_page_changes_corrupt_input_frame_set_hard_fails(
    tmp_path: Path, published: Published, capsys: pytest.CaptureFixture[str]
) -> None:
    output_root = _copy_task(tmp_path, published.output_root)
    _current_path(output_root).write_text("{not json", encoding="utf-8")

    code, out, err = _run_cli(
        capsys, "detect-page-changes", TASK_ID, "--output-root", str(output_root)
    )
    _assert_failure(
        code, out, err, error_code="INPUT_FRAME_SET_CORRUPT", output_root=output_root
    )

    other = _copy_task(tmp_path / "directory-current", published.output_root)
    _current_path(other).unlink()
    _current_path(other).mkdir()
    code, out, err = _run_cli(
        capsys, "detect-page-changes", TASK_ID, "--output-root", str(other)
    )
    _assert_failure(
        code, out, err, error_code="INPUT_FRAME_SET_CORRUPT", output_root=other
    )
    assert _current_path(other).is_dir()


def _only_page_change_group(output_root: Path) -> Path:
    """返回页面变化正式组目录；此时只应存在一个正式组。"""

    entries = [
        entry
        for entry in (_page_changes_root(output_root) / "formal-results").iterdir()
        if entry.is_dir()
    ]
    assert len(entries) == 1
    return entries[0]
