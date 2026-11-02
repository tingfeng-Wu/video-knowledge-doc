from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from video_doc import frame_sampler as sampler
from video_doc.task_workspace import (
    TASK_SUBDIRECTORIES,
    TaskWorkspaceError,
    initialize_task_workspace,
)


SYNTHETIC_DURATION_SECONDS = 32
SYNTHETIC_EXPECTED_FRAMES = 8
SYNTHETIC_WIDTH = 320
SYNTHETIC_HEIGHT = 240


@pytest.fixture(scope="module")
def synthetic_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """生成一个短片夹具，避免自动测试依赖data/raw。"""

    directory = tmp_path_factory.mktemp("frame-sampler-fixture")
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


def _workspace(tmp_path: Path, task_id: str, source: Path) -> Path:
    initialize_task_workspace(tmp_path, task_id, source_video=source)
    return tmp_path


def _frames_root(output_root: Path, task_id: str) -> Path:
    return output_root / task_id / "m2" / "frames"


def _failure_audits(output_root: Path, task_id: str) -> list[Path]:
    return sorted(
        (output_root / task_id / "m2" / "failures" / "frames").glob("*/failure.json")
    )


def _assert_current_target_is_intact(output_root: Path, task_id: str) -> str:
    """复读current，确认其指向的组、manifest和全部帧文件都真实存在。"""

    pointer = sampler.load_frame_set_current(output_root, task_id)
    assert pointer is not None
    group = _frames_root(output_root, task_id) / str(pointer["group"])
    assert group.is_dir()
    manifest_path = group / "manifest.json"
    assert manifest_path.is_file()
    assert (
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        == pointer["manifest_sha256"]
    )
    index = json.loads((group / "frame-set.json").read_text(encoding="utf-8"))
    assert index["frame_set_id"] == pointer["result_id"]
    assert index["frames"]
    for frame in index["frames"]:
        assert (group / frame["file"]).is_file()
    return str(pointer["result_id"])


def _patch_replace_failing_for(
    monkeypatch: pytest.MonkeyPatch, file_name: str
) -> list[Path]:
    """让模块内的``os.replace``在目标为该文件名时于替换前失败。

    只替换``frame_sampler``命名空间里的``os``，返回被写出过的临时源路径，
    便于断言临时文件确实写出过、随后又被精确清理。
    """

    seen: list[Path] = []
    real_os = sampler.os

    class _ReplaceFailingOs:
        def replace(self, source: object, target: object) -> None:
            if Path(str(target)).name == file_name:
                seen.append(Path(str(source)))
                raise OSError("模拟os.replace在替换前失败。")
            real_os.replace(source, target)

        def __getattr__(self, attribute: str) -> object:
            return getattr(real_os, attribute)

    monkeypatch.setattr(sampler, "os", _ReplaceFailingOs())
    return seen


def _register_shorter_profile() -> str:
    """注册临时profile，用于在已有正式组的任务上生成新的结果组身份。"""

    shorter = replace(
        sampler.LOW_FREQUENCY_V1, profile_id="test-10s", base_interval_ms=10_000
    )
    sampler.SAMPLING_PROFILES[shorter.profile_id] = shorter
    return shorter.profile_id


# ---------------------------------------------------------------- 时间点规划


def test_plan_matches_frozen_low_frequency_v1_counts() -> None:
    slots, plan = sampler.plan_sampling_slots(914_922, 914_800)
    assert [slot.requested_ms for slot in slots[:3]] == [0, 5000, 10000]
    assert plan["slot_count"] == 184
    assert plan["grid_slot_count"] == 183
    assert plan["grid_slots_dropped_beyond_stream_end"] == 0
    assert plan["planned_slot_count_before_cap"] == 184
    assert plan["uniformly_resampled"] is False


def test_plan_includes_first_and_last_with_expected_roles() -> None:
    slots, _ = sampler.plan_sampling_slots(32_000, 32_000)
    assert slots[0].frame_id == "fr000001"
    assert slots[0].requested_ms == 0
    assert slots[0].role == "grid"
    assert slots[0].is_first is True
    assert slots[0].is_last is False
    assert slots[-1].role == "tail"
    assert slots[-1].requested_ms == 32_000
    assert slots[-1].is_last is True
    assert [slot.ordinal for slot in slots] == list(range(1, len(slots) + 1))


def test_plan_caps_long_video_uniformly_between_first_and_last() -> None:
    # LONG01的最后一个网格点3285000ms落在视频流末尾3284948ms之后，因此被丢弃。
    slots, plan = sampler.plan_sampling_slots(3_285_035, 3_284_948)
    assert len(slots) == 360
    assert plan["uniformly_resampled"] is True
    assert plan["grid_slot_count"] == 658
    assert plan["grid_slots_dropped_beyond_stream_end"] == 1
    assert plan["planned_slot_count_before_cap"] == 658
    assert slots[0].requested_ms == 0
    assert slots[-1].requested_ms == 3_284_948
    assert slots[-1].role == "tail"
    gaps = [
        slots[index + 1].requested_ms - slots[index].requested_ms
        for index in range(len(slots) - 1)
    ]
    assert max(gaps) - min(gaps) <= 5000


def test_plan_drops_grid_points_beyond_stream_end() -> None:
    slots, plan = sampler.plan_sampling_slots(100_000, 82_000)
    assert plan["grid_slot_count"] == 20
    assert plan["grid_slots_dropped_beyond_stream_end"] == 3
    assert [slot.requested_ms for slot in slots] == [
        0, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000,
        45000, 50000, 55000, 60000, 65000, 70000, 75000, 80000, 82000,
    ]


def test_plan_rejects_unusable_durations() -> None:
    with pytest.raises(sampler.FrameExtractionFailedError):
        sampler.plan_sampling_slots(0, 0)


def test_plan_refuses_unsupported_long_video_distribution() -> None:
    profile = replace(
        sampler.LOW_FREQUENCY_V1,
        max_sample_frames_per_video=5,
        long_video_distribution="keep_first",
    )
    with pytest.raises(sampler.FrameSamplerError):
        sampler.plan_sampling_slots(200_000, 200_000, profile)


def test_resolve_sampling_profile_rejects_unknown_id() -> None:
    assert sampler.resolve_sampling_profile("low-frequency-v1") is sampler.LOW_FREQUENCY_V1
    with pytest.raises(sampler.FrameSamplerError):
        sampler.resolve_sampling_profile("low-frequency-v9")


# ---------------------------------------------------------------- 缓存身份


def test_cache_identity_is_deterministic_and_source_bound(synthetic_video: Path) -> None:
    tools = sampler.resolve_media_tools()
    source = sampler.describe_source_video(synthetic_video, tools)
    _, first = sampler.frame_sampler_cache_identity(sampler.LOW_FREQUENCY_V1, source)
    _, second = sampler.frame_sampler_cache_identity(sampler.LOW_FREQUENCY_V1, source)
    assert first == second
    assert sampler.frame_set_result_id(first).endswith(first[:12])

    changed_profile = replace(sampler.LOW_FREQUENCY_V1, base_interval_ms=10_000)
    _, other = sampler.frame_sampler_cache_identity(changed_profile, source)
    assert other != first

    renamed = replace(source, file_name="SYNTH01-renamed.mp4")
    _, renamed_identity = sampler.frame_sampler_cache_identity(
        sampler.LOW_FREQUENCY_V1, renamed
    )
    assert renamed_identity != first


def test_cache_identity_excludes_deferred_f2_parameters() -> None:
    """F2调参不得让已发布的帧集失效。"""

    assert set(sampler.SamplingProfile.__dataclass_fields__) == {
        "profile_id",
        "base_interval_ms",
        "max_sample_frames_per_video",
        "include_first_decodable_frame",
        "include_last_decodable_frame",
        "long_video_distribution",
        "stored_frame_resolution",
    }
    for name in sampler.LOW_FREQUENCY_V1_DEFERRED_PARAMETERS:
        assert name not in sampler.SamplingProfile.__dataclass_fields__
    assert "change_proxy_width_pixels" in sampler.LOW_FREQUENCY_V1_DEFERRED_PARAMETERS


# ---------------------------------------------------------------- 发布与缓存


def test_sample_frames_publishes_immutable_group_and_current(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)

    assert result.cache_hit is False
    assert result.pointer_repaired is False
    assert result.group_path.name == result.index["frame_set_id"]
    assert result.index["frame_count"] == SYNTHETIC_EXPECTED_FRAMES
    assert len(result.frame_paths) == SYNTHETIC_EXPECTED_FRAMES
    assert result.manifest["artifact_type"] == "frame_set"
    assert result.manifest["result_id"] == result.index["frame_set_id"]
    assert result.manifest["frame_count"] == SYNTHETIC_EXPECTED_FRAMES

    for path in result.frame_paths:
        assert path.is_file()
        assert path.suffix == ".png"
    assert sampler._png_dimensions(result.frame_paths[0]) == (
        SYNTHETIC_WIDTH,
        SYNTHETIC_HEIGHT,
    )

    frames = result.index["frames"]
    assert frames[0]["frame_id"] == "fr000001"
    assert frames[0]["decoded_ms"] == 0
    assert frames[-1]["is_last"] is True
    for frame in frames:
        assert frame["decoded_ms"] >= frame["requested_ms"] - 1
        assert len(frame["sha256"]) == 64

    pointer = json.loads(
        (_frames_root(output_root, "SYNTH01") / "current.json").read_text(encoding="utf-8")
    )
    assert pointer["schema_name"] == "ArtifactCurrentPointer"
    assert pointer["result_id"] == result.index["frame_set_id"]
    assert pointer["group"] == f"formal-results/{result.index['frame_set_id']}"


def test_sample_frames_records_source_identity_without_absolute_path(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    recorded = result.index["source_video"]
    assert recorded["file_name"] == synthetic_video.name
    assert recorded["size_bytes"] == synthetic_video.stat().st_size
    assert recorded["video_stream_duration_ms"] == SYNTHETIC_DURATION_SECONDS * 1000

    for path in (result.index_path, result.manifest_path):
        payload = path.read_text(encoding="utf-8")
        assert str(synthetic_video.parent) not in payload
        assert "tmp" not in recorded["file_name"]


def test_second_run_hits_cache_without_rewriting_group(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size) for path in first.frame_paths
    }
    manifest_before = first.manifest_path.read_bytes()

    second = sampler.sample_frames("SYNTH01", output_root=output_root)
    assert second.cache_hit is True
    assert second.pointer_repaired is False
    assert second.group_path == first.group_path
    assert second.manifest_path.read_bytes() == manifest_before
    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_size) for path in second.frame_paths
    }
    assert after == before


def test_missing_current_is_repaired_without_rewriting_group(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    current_path = _frames_root(output_root, "SYNTH01") / "current.json"
    current_path.unlink()
    manifest_before = first.manifest_path.read_bytes()

    repaired = sampler.sample_frames("SYNTH01", output_root=output_root)
    assert repaired.cache_hit is True
    assert repaired.pointer_repaired is True
    assert repaired.manifest_path.read_bytes() == manifest_before
    assert json.loads(current_path.read_text(encoding="utf-8"))["result_id"] == first.index[
        "frame_set_id"
    ]


def test_corrupt_frame_file_hard_fails_and_preserves_current(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    current_path = _frames_root(output_root, "SYNTH01") / "current.json"
    current_before = current_path.read_bytes()

    victim = result.frame_paths[3]
    original = victim.read_bytes()
    victim.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))

    with pytest.raises(sampler.FrameSetCacheCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)

    assert current_path.read_bytes() == current_before
    assert victim.read_bytes() == original[:-1] + bytes([original[-1] ^ 0xFF])
    audits = _failure_audits(output_root, "SYNTH01")
    assert audits
    audit = json.loads(audits[-1].read_text(encoding="utf-8"))
    assert audit["error_code"] == "CACHE_CORRUPT"
    assert audit["current_unchanged"] is True
    assert audit["business_call_count"] == 0
    assert audit["supplier_request_count"] == 0


def test_current_pointing_at_missing_group_hard_fails(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    current_path = _frames_root(output_root, "SYNTH01") / "current.json"
    pointer = json.loads(current_path.read_text(encoding="utf-8"))
    shutil.rmtree(result.group_path)

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    assert json.loads(current_path.read_text(encoding="utf-8")) == pointer


def test_existing_lock_reports_concurrent_run(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    locks = _frames_root(output_root, "SYNTH01").parent / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock_path = locks / "frames.lock"
    lock_path.write_text("{}", encoding="utf-8")

    with pytest.raises(sampler.ConcurrentRunError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    assert lock_path.exists()
    assert not (_frames_root(output_root, "SYNTH01") / "formal-results").exists()


def test_explicit_source_with_declared_name_but_other_content_is_rejected(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    impostor_directory = tmp_path / "elsewhere"
    impostor_directory.mkdir()
    impostor = impostor_directory / synthetic_video.name
    payload = bytearray(synthetic_video.read_bytes())
    payload[-1] ^= 0xFF
    impostor.write_bytes(bytes(payload))

    with pytest.raises(sampler.SourceVideoMismatchError):
        sampler.sample_frames("SYNTH01", output_root=output_root, source_video=impostor)


def test_explicit_source_with_other_name_is_rejected(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    other = tmp_path / "SYNTH01-copy.mp4"
    shutil.copyfile(synthetic_video, other)

    with pytest.raises(sampler.SourceVideoMismatchError):
        sampler.sample_frames("SYNTH01", output_root=output_root, source_video=other)


def test_changed_source_content_yields_new_identity_not_overwrite(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """源视频内容变化必须生成新结果组，不得覆盖已发布帧集。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    manifest_before = first.manifest_path.read_bytes()
    frame_count_before = first.index["frame_count"]

    replacement = tmp_path / synthetic_video.name
    replacement.write_bytes(synthetic_video.read_bytes() + b"\x00" * 8)
    task_manifest = output_root / "SYNTH01" / "task.json"
    manifest = json.loads(task_manifest.read_text(encoding="utf-8"))
    manifest["source_video"] = str(replacement)
    task_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    second = sampler.sample_frames("SYNTH01", output_root=output_root)
    assert second.group_path != first.group_path
    assert first.manifest_path.read_bytes() == manifest_before
    assert first.index["frame_count"] == frame_count_before
    assert json.loads(
        (_frames_root(output_root, "SYNTH01") / "current.json").read_text(encoding="utf-8")
    )["result_id"] == second.index["frame_set_id"]


@pytest.mark.parametrize(
    "point",
    [
        "after_frame_extraction",
        "after_index",
        "after_manifest",
        "before_group_publish",
        "after_group_publish",
        "before_current",
    ],
)
def test_injected_failure_never_publishes_or_switches_current(
    tmp_path: Path, synthetic_video: Path, point: str
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")

    with pytest.raises(sampler.FrameSamplerError):
        sampler.sample_frames(
            "SYNTH01", output_root=output_root, failure_injection=point
        )

    assert not (frames_root / "current.json").exists()
    groups = frames_root / "formal-results"
    assert not groups.exists() or list(groups.iterdir()) == []
    assert not list(frames_root.glob(".tmp-*"))
    audit = json.loads(_failure_audits(output_root, "SYNTH01")[-1].read_text(encoding="utf-8"))
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] in {"temp_removed", "group_removed"}


def test_unknown_failure_injection_point_is_rejected(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    with pytest.raises(sampler.FrameSamplerError):
        sampler.sample_frames(
            "SYNTH01", output_root=output_root, failure_injection="after_everything"
        )


def test_sampling_does_not_touch_m1_artifacts(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    task_root = output_root / "SYNTH01"
    task_manifest = task_root / "task.json"
    before = task_manifest.read_bytes()
    m1_directories = {
        name: sorted(path.name for path in (task_root / name).iterdir())
        for name in TASK_SUBDIRECTORIES
    }

    sampler.sample_frames("SYNTH01", output_root=output_root)

    assert task_manifest.read_bytes() == before
    assert {
        name: sorted(path.name for path in (task_root / name).iterdir())
        for name in TASK_SUBDIRECTORIES
    } == m1_directories
    assert sorted(path.name for path in task_root.iterdir()) == sorted(
        [*TASK_SUBDIRECTORIES, "m2", "task.json"]
    )
    # 成功运行不写失败审计，因此m2下不应出现空的failures目录。
    assert sorted(path.name for path in (task_root / "m2").iterdir()) == [
        ".locks",
        "frames",
    ]
    assert not (task_root / "m2" / "failures").exists()


def test_new_identity_creates_new_group_and_keeps_old_group(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    first_bytes = first.manifest_path.read_bytes()

    shorter = replace(
        sampler.LOW_FREQUENCY_V1, profile_id="test-10s", base_interval_ms=10_000
    )
    sampler.SAMPLING_PROFILES[shorter.profile_id] = shorter
    try:
        second = sampler.sample_frames("SYNTH01", output_root=output_root, profile_id="test-10s")
    finally:
        sampler.SAMPLING_PROFILES.pop("test-10s", None)

    assert second.group_path != first.group_path
    assert first.manifest_path.read_bytes() == first_bytes
    assert first.index["frame_count"] == SYNTHETIC_EXPECTED_FRAMES
    assert second.index["frame_count"] < SYNTHETIC_EXPECTED_FRAMES
    assert json.loads(
        (_frames_root(output_root, "SYNTH01") / "current.json").read_text(encoding="utf-8")
    )["result_id"] == second.index["frame_set_id"]


def test_formal_group_revalidation_runs_before_current_switch(
    tmp_path: Path, synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式组复验必须发生在current切换之前，否则复验失败会留下悬空指针。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    current_path = _frames_root(output_root, "SYNTH01") / "current.json"
    real_validate = sampler.validate_frame_set_group
    current_existed_at_revalidation: list[bool] = []

    def recording_validate(group_path: object, **kwargs: object):
        if kwargs.get("require_directory_name", True):
            current_existed_at_revalidation.append(current_path.exists())
        return real_validate(group_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sampler, "validate_frame_set_group", recording_validate)
    sampler.sample_frames("SYNTH01", output_root=output_root)

    assert current_existed_at_revalidation == [False]
    assert current_path.exists()


def test_formal_group_revalidation_failure_keeps_old_current_and_removes_new_group(
    tmp_path: Path, synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式组复验失败时，新组必须被清理，旧current必须原样保留且仍指向有效组。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")
    current_path = frames_root / "current.json"
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    current_before = current_path.read_bytes()

    real_validate = sampler.validate_frame_set_group

    def failing_validate(group_path: object, **kwargs: object):
        if kwargs.get("require_directory_name", True):
            raise sampler.FrameSetCacheCorruptError("CACHE_CORRUPT：模拟正式组复验失败。")
        return real_validate(group_path, **kwargs)  # type: ignore[arg-type]

    shorter = replace(
        sampler.LOW_FREQUENCY_V1, profile_id="test-10s", base_interval_ms=10_000
    )
    sampler.SAMPLING_PROFILES[shorter.profile_id] = shorter
    monkeypatch.setattr(sampler, "validate_frame_set_group", failing_validate)
    try:
        with pytest.raises(sampler.FrameSetCacheCorruptError):
            sampler.sample_frames("SYNTH01", output_root=output_root, profile_id="test-10s")
    finally:
        sampler.SAMPLING_PROFILES.pop("test-10s", None)

    # 旧current逐字节不变，且仍指向一个存在、可复验的正式组。
    assert current_path.read_bytes() == current_before
    assert json.loads(current_before)["result_id"] == first.index["frame_set_id"]
    assert first.group_path.is_dir()
    assert _assert_current_target_is_intact(output_root, "SYNTH01") == (
        first.index["frame_set_id"]
    )

    # 新组目录与临时目录都必须被清理，正式组目录下只剩旧组。
    groups = frames_root / "formal-results"
    assert [path.name for path in groups.iterdir()] == [first.group_path.name]
    assert not list(frames_root.glob(".tmp-*"))

    audit = json.loads(_failure_audits(output_root, "SYNTH01")[-1].read_text(encoding="utf-8"))
    assert audit["error_code"] == "CACHE_CORRUPT"
    assert audit["stage"] == "group_revalidation"
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "group_removed"


def test_publish_failure_before_current_keeps_old_current_and_removes_new_group(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """current替换前失败时，旧current逐字节不变，新组和临时目录都必须清理。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")
    current_path = frames_root / "current.json"
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    current_before = current_path.read_bytes()

    profile_id = _register_shorter_profile()
    try:
        with pytest.raises(sampler.FrameSamplerError):
            sampler.sample_frames(
                "SYNTH01",
                output_root=output_root,
                profile_id=profile_id,
                failure_injection="before_current",
            )
    finally:
        sampler.SAMPLING_PROFILES.pop(profile_id, None)

    assert current_path.read_bytes() == current_before
    assert _assert_current_target_is_intact(output_root, "SYNTH01") == (
        first.index["frame_set_id"]
    )
    assert [path.name for path in (frames_root / "formal-results").iterdir()] == [
        first.group_path.name
    ]
    assert not list(frames_root.glob(".tmp-*"))

    audit = json.loads(_failure_audits(output_root, "SYNTH01")[-1].read_text(encoding="utf-8"))
    assert audit["stage"] == "current_publish"
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "group_removed"


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_failure_after_current_replace_keeps_published_group(
    tmp_path: Path,
    synthetic_video: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    """current底层替换成功后、状态标志更新前的异常或中断不得删除新组。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")
    current_path = frames_root / "current.json"
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    real_replace_current = sampler._replace_current
    raised: list[BaseException] = []

    def replace_then_fail(target_root: Path, pointer: object) -> None:
        real_replace_current(target_root, pointer)  # type: ignore[arg-type]
        error = error_type("模拟current替换成功后、状态标志更新前失败。")
        raised.append(error)
        raise error

    profile_id = _register_shorter_profile()
    monkeypatch.setattr(sampler, "_replace_current", replace_then_fail)
    expected = sampler.FrameSetPublishFailedError if error_type is RuntimeError else error_type
    try:
        with pytest.raises(expected) as excinfo:
            sampler.sample_frames("SYNTH01", output_root=output_root, profile_id=profile_id)
    finally:
        sampler.SAMPLING_PROFILES.pop(profile_id, None)

    # current已经切到新组，新组必须保留；旧组也必须原样保留。
    pointer = json.loads(current_path.read_text(encoding="utf-8"))
    assert pointer["result_id"] != first.index["frame_set_id"]
    assert _assert_current_target_is_intact(output_root, "SYNTH01") == pointer["result_id"]
    assert first.group_path.is_dir()
    assert sorted(path.name for path in (frames_root / "formal-results").iterdir()) == sorted(
        [first.group_path.name, str(pointer["result_id"])]
    )
    assert not list(frames_root.glob(".tmp-*"))
    assert not (frames_root.parent / ".locks" / "frames.lock").exists()

    if error_type is RuntimeError:
        assert isinstance(excinfo.value, sampler.FrameSetPublishFailedError)
    else:
        # 中断必须原样重新抛出，不能被包装、替换或吞掉。
        assert excinfo.value is raised[0]

    # 普通异常与中断都必须留下失败审计，且字段符合磁盘事实。
    audit = json.loads(
        _failure_audits(output_root, "SYNTH01")[-1].read_text(encoding="utf-8")
    )
    assert audit["status"] == "failed"
    assert audit["task_id"] == "SYNTH01"
    assert audit["module"] == "frames"
    assert audit["stage"] == "current_publish"
    # 磁盘上的current已经改变，审计必须如实记录，而不是照抄内存标志。
    assert audit["current_unchanged"] is False
    assert audit["temporary_cleanup_status"] == "published"
    assert audit["safe_message"].endswith(f"（错误类型：{error_type.__name__}）")


@pytest.mark.parametrize("audit_error", [OSError, KeyboardInterrupt, SystemExit])
def test_audit_write_failure_never_replaces_the_original_interrupt(
    tmp_path: Path,
    synthetic_video: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_error: type[BaseException],
) -> None:
    """审计写入失败或写入期间再次被中断，都必须原样抛出原始中断。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    real_replace_current = sampler._replace_current
    interrupted: list[BaseException] = []

    def replace_then_interrupt(target_root: Path, pointer: object) -> None:
        real_replace_current(target_root, pointer)  # type: ignore[arg-type]
        error = KeyboardInterrupt("模拟current替换成功后发生中断。")
        interrupted.append(error)
        raise error

    def failing_audit(*args: object, **kwargs: object) -> None:
        raise audit_error("模拟审计写入失败。")

    profile_id = _register_shorter_profile()
    monkeypatch.setattr(sampler, "_replace_current", replace_then_interrupt)
    monkeypatch.setattr(sampler, "_write_failure_audit", failing_audit)
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            sampler.sample_frames("SYNTH01", output_root=output_root, profile_id=profile_id)
    finally:
        sampler.SAMPLING_PROFILES.pop(profile_id, None)

    assert excinfo.value is interrupted[0]
    # 审计没写成，但清理仍按磁盘事实完成：current已指向新组，新组必须保留。
    assert (
        _assert_current_target_is_intact(output_root, "SYNTH01")
        != first.index["frame_set_id"]
    )
    assert not _failure_audits(output_root, "SYNTH01")


def test_unparsable_current_after_replace_keeps_published_group(
    tmp_path: Path, synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """current存在但无法解析时不得猜着删组，否则可能删掉它实际指向的新组。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")
    current_path = frames_root / "current.json"
    first = sampler.sample_frames("SYNTH01", output_root=output_root)

    def replace_with_garbage(target_root: Path, pointer: object) -> None:
        (target_root / "current.json").write_bytes(b"{not-json")
        raise RuntimeError("模拟current写入后损坏且状态标志未更新。")

    profile_id = _register_shorter_profile()
    monkeypatch.setattr(sampler, "_replace_current", replace_with_garbage)
    try:
        with pytest.raises(sampler.FrameSetPublishFailedError):
            sampler.sample_frames("SYNTH01", output_root=output_root, profile_id=profile_id)
    finally:
        sampler.SAMPLING_PROFILES.pop(profile_id, None)

    # 无法确认current指向谁时保留新组，旧组也不受影响；新组本身必须完整。
    groups = sorted(path.name for path in (frames_root / "formal-results").iterdir())
    assert first.group_path.name in groups
    assert len(groups) == 2
    assert current_path.read_bytes() == b"{not-json"

    audit = json.loads(_failure_audits(output_root, "SYNTH01")[-1].read_text(encoding="utf-8"))
    assert audit["current_unchanged"] is False
    assert audit["temporary_cleanup_status"] == "published"


def test_current_replacement_failure_leaves_no_temporary_pointer(
    tmp_path: Path, synthetic_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """current替换前失败时，旧current有效、新组按既定规则清理、临时指针无残留。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")
    current_path = frames_root / "current.json"
    first = sampler.sample_frames("SYNTH01", output_root=output_root)
    current_before = current_path.read_bytes()
    entries_before = sorted(path.name for path in frames_root.iterdir())

    written = _patch_replace_failing_for(monkeypatch, "current.json")
    profile_id = _register_shorter_profile()
    try:
        with pytest.raises(sampler.FrameSetPublishFailedError):
            sampler.sample_frames("SYNTH01", output_root=output_root, profile_id=profile_id)
    finally:
        sampler.SAMPLING_PROFILES.pop(profile_id, None)

    # 临时指针确实写出过，并被_replace_current自己按精确路径清理干净。
    assert len(written) == 1
    assert written[0].name.startswith(".current.json.tmp-")
    assert not written[0].exists()
    # 旧current逐字节保持有效，指向的组、manifest和帧文件都还在。
    assert current_path.read_bytes() == current_before
    assert (
        _assert_current_target_is_intact(output_root, "SYNTH01")
        == first.index["frame_set_id"]
    )
    # 新正式组按既定规则清理；frames根目录除旧内容外无任何新增条目。
    assert sorted(path.name for path in frames_root.iterdir()) == entries_before
    assert sorted(
        path.name for path in (frames_root / "formal-results").iterdir()
    ) == [first.group_path.name]

    audit = json.loads(
        _failure_audits(output_root, "SYNTH01")[-1].read_text(encoding="utf-8")
    )
    assert audit["stage"] == "current_publish"
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "group_removed"


def test_successful_current_replacement_leaves_no_temporary_pointer(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """os.replace成功后，_replace_current的finally清理是空操作，不留临时指针。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")
    sampler.sample_frames("SYNTH01", output_root=output_root)
    # 缓存修复路径同样只重写current，必须走同一条清理逻辑。
    (frames_root / "current.json").unlink()
    sampler.sample_frames("SYNTH01", output_root=output_root)

    assert sorted(path.name for path in frames_root.iterdir()) == [
        "current.json",
        "formal-results",
    ]


def test_rejects_unknown_task_id_shape(tmp_path: Path, synthetic_video: Path) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    with pytest.raises(TaskWorkspaceError):
        sampler.sample_frames("../escape", output_root=output_root)


def test_missing_task_manifest_is_reported(tmp_path: Path, synthetic_video: Path) -> None:
    with pytest.raises(sampler.FrameSamplerError):
        sampler.sample_frames("NOPE01", output_root=tmp_path)


def _write_current_payload(frames_root: Path, payload: object) -> None:
    (frames_root / "current.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _corrupt_current_payloads(result_id: str) -> list[tuple[str, object]]:
    """requirement 3要求的全部current损坏形态。"""

    valid = {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "frame_set",
        "result_id": result_id,
        "group": f"formal-results/{result_id}",
        "manifest_sha256": "0" * 64,
    }
    return [
        ("非对象", [1, 2, 3]),
        ("字段缺失", {key: value for key, value in valid.items() if key != "group"}),
        ("字段多余", {**valid, "extra": 1}),
        ("Schema错误", {**valid, "schema_version": "9.9"}),
        ("artifact_type错误", {**valid, "artifact_type": "page_change_set"}),
        ("group错误", {**valid, "group": "formal-results/frame-set-ffffffffffff"}),
        ("manifest哈希错误", {**valid, "manifest_sha256": "f" * 64}),
    ]


@pytest.mark.parametrize("label,payload", _corrupt_current_payloads("frame-set-000000000000"))
def test_corrupt_current_forms_are_current_pointer_corrupt(
    tmp_path: Path, synthetic_video: Path, label: str, payload: object
) -> None:
    """非法JSON、非对象、字段缺失或多余、Schema错误、group错误和哈希错误统一分类。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    group_before = sorted(path.name for path in result.group_path.iterdir())
    _write_current_payload(frames_root, payload)

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    # 损坏current不允许被改写成新组指针，也不允许删除已有正式组
    assert json.loads((frames_root / "current.json").read_text(encoding="utf-8")) == payload
    assert sorted(path.name for path in result.group_path.iterdir()) == group_before


def test_non_json_current_is_current_pointer_corrupt(
    tmp_path: Path, synthetic_video: Path
) -> None:
    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    (frames_root / "current.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    assert (frames_root / "current.json").read_text(encoding="utf-8") == "{not json"
    assert result.group_path.is_dir()


def test_current_that_is_not_a_regular_file_hard_fails(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """current.json存在但不是普通文件时按CURRENT_POINTER_CORRUPT硬失败。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    (frames_root / "current.json").unlink()
    (frames_root / "current.json").mkdir()

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    assert (frames_root / "current.json").is_dir()
    assert result.group_path.is_dir()


def test_load_frame_set_current_distinguishes_missing_from_corrupt(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """公开读取入口只把“文件不存在”当作没有current。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    frames_root = _frames_root(output_root, "SYNTH01")
    assert sampler.load_frame_set_current(output_root, "SYNTH01") is None

    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    pointer = sampler.load_frame_set_current(output_root, "SYNTH01")
    assert pointer is not None
    assert pointer["result_id"] == result.index["frame_set_id"]

    (frames_root / "current.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, "SYNTH01")

    (frames_root / "current.json").unlink()
    (frames_root / "current.json").mkdir()
    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, "SYNTH01")


# ------------------------------------------- current指针文件类型与路径边界


class _RegularFileSymlinkStub:
    """模拟“指向普通文件的活符号链接”：目标可读，但自身不是普通文件。"""

    def is_file(self) -> bool:
        return True

    def is_symlink(self) -> bool:
        return True

    def exists(self) -> bool:
        return True


class _BrokenSymlinkStub:
    """模拟断链符号链接：is_file()和exists()都为False，只有is_symlink()为True。"""

    def is_file(self) -> bool:
        return False

    def is_symlink(self) -> bool:
        return True

    def exists(self) -> bool:
        return False


class _DirectoryStub:
    """模拟目录：路径存在，但不是普通文件。"""

    def is_file(self) -> bool:
        return False

    def is_symlink(self) -> bool:
        return False

    def exists(self) -> bool:
        return True


@pytest.mark.parametrize(
    "label,stub,legacy_accepts",
    [
        ("活符号链接", _RegularFileSymlinkStub(), True),
        ("断链符号链接", _BrokenSymlinkStub(), False),
        ("目录", _DirectoryStub(), False),
    ],
)
def test_current_file_type_guard_rejects_every_non_regular_shape(
    label: str, stub: object, legacy_accepts: bool
) -> None:
    """文件类型守卫必须先判符号链接，再判普通文件。

    本机默认没有创建符号链接的权限，因此对判断助手做无特权单元测试。第三个参数
    复现旧判据（``is_file()``优先）的结果：只有活符号链接会被旧实现接受，
    所以本用例在旧实现下必然失败。
    """

    legacy_result = stub.is_file() or not (  # type: ignore[attr-defined]
        stub.is_symlink() or stub.exists()  # type: ignore[attr-defined]
    )
    assert legacy_result is legacy_accepts
    with pytest.raises(sampler.CurrentPointerCorruptError, match="不是普通文件"):
        sampler._require_regular_current_file(stub, "frames current指针")  # type: ignore[arg-type]


def _force_symlink_predicate(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """把单个路径的``is_symlink``谓词替换为True，模拟活符号链接。

    本机没有创建符号链接的权限；只对这一个精确路径生效，其余路径仍走真实实现。
    """

    real_is_symlink = Path.is_symlink

    def is_symlink(self: Path) -> bool:
        return True if self == target else real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)


def test_symlinked_current_hard_fails_without_rewriting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, synthetic_video: Path
) -> None:
    """指向普通文件的活符号链接不得被当成普通current文件接受。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    current = frames_root / sampler.FRAME_CURRENT_FILE
    before = current.read_bytes()
    _force_symlink_predicate(monkeypatch, current)

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, "SYNTH01")

    assert current.read_bytes() == before
    assert result.group_path.is_dir()


def test_live_symlink_current_hard_fails_when_symlinks_are_available(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """有符号链接权限时用真实活链接复验同一行为；无权限则跳过。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    current = frames_root / sampler.FRAME_CURRENT_FILE
    elsewhere = tmp_path / "elsewhere-current.json"
    elsewhere.write_bytes(current.read_bytes())
    current.unlink()
    try:
        current.symlink_to(elsewhere)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"当前环境无法创建符号链接：{type(exc).__name__}")
    assert current.is_symlink()
    assert current.is_file()

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    assert current.is_symlink()
    assert result.group_path.is_dir()


def test_artifact_group_path_rejects_traversal(tmp_path: Path) -> None:
    """解析后的目标必须精确等于formal-results下的预期路径。"""

    frames_root = tmp_path / "frames"
    legitimate = frames_root / "formal-results" / "frame-set-000000000000"
    legitimate.mkdir(parents=True)
    assert (
        sampler.artifact_group_path(frames_root, "formal-results", "frame-set-000000000000")
        == legitimate
    )
    for escaping in ("../../evil", "..", "."):
        assert sampler.artifact_group_path(frames_root, "formal-results", escaping) is None
    # 绕回formal-results内部的写法解析后虽然仍在目录内，但不是精确的
    # resolved_parent/result_id，因此同样返回None（名称校验也拒绝它）：
    assert (
        sampler.artifact_group_path(
            frames_root, "formal-results", "../formal-results/frame-set-000000000000"
        )
        is None
    )
    assert sampler.is_safe_result_id("../formal-results/frame-set-000000000000") is False


def _force_resolved_paths(monkeypatch: pytest.MonkeyPatch, mapping: dict[Path, Path]) -> None:
    """把指定路径的``resolve``结果替换为另一个路径，模拟符号链接或目录联接。

    本机没有创建符号链接的权限，因此用解析替身做无特权测试：只对``mapping``里的
    精确路径生效，其余路径仍走真实实现。调用方需要同时映射链接目录本身和它下面
    的目标路径，才能如实复现“解析时跟随链接”的效果。
    """

    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        replacement = mapping.get(self)
        return real_resolve(self, strict) if replacement is None else replacement

    monkeypatch.setattr(Path, "resolve", resolve)


def _tree_identity(root: Path) -> dict[str, str]:
    """目录下所有文件的相对路径与内容哈希，用于断言整树未被改写。"""

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_artifact_group_path_keeps_real_directories(tmp_path: Path) -> None:
    """普通真实目录不受精确身份校验影响，仍返回原候选路径。"""

    frames_root = tmp_path / "frames"
    result_id = "frame-set-000000000000"
    legitimate = frames_root / "formal-results" / result_id
    legitimate.mkdir(parents=True)

    assert sampler.artifact_group_path(frames_root, "formal-results", result_id) == legitimate
    # 组目录尚不存在时同样返回候选路径，由调用方的存在性检查给出各自的错误码
    assert sampler.artifact_group_path(
        frames_root, "formal-results", "frame-set-ffffffffffff"
    ) == (frames_root / "formal-results" / "frame-set-ffffffffffff")


def test_group_directory_linked_outside_artifact_root_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``formal-results``自身指向artifact根目录外时必须返回None。

    旧判据只比较``resolved_candidate.parent == resolved_parent``：parent与candidate
    会一起解析到外部，父子关系依然成立，因此旧实现会放行。
    """

    frames_root = tmp_path / "frames"
    link = frames_root / "formal-results"
    link.mkdir(parents=True)
    result_id = "frame-set-000000000000"
    outside = tmp_path / "outside-formal-results"
    (outside / result_id).mkdir(parents=True)
    _force_resolved_paths(monkeypatch, {link: outside, link / result_id: outside / result_id})

    # 旧判据：解析到外部之后父子关系仍然成立
    assert (outside / result_id).parent == outside

    assert sampler.artifact_group_path(frames_root, "formal-results", result_id) is None


def test_result_group_linked_to_sibling_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """结果组目录指向同级另一个组时必须返回None。

    旧判据下解析结果仍落在同一个``formal-results``内，父子关系同样成立，
    因此旧实现会把同级另一个组当成目标组。
    """

    frames_root = tmp_path / "frames"
    formal = frames_root / "formal-results"
    formal.mkdir(parents=True)
    linked_id = "frame-set-000000000000"
    sibling_id = "frame-set-111111111111"
    (formal / linked_id).mkdir()
    (formal / sibling_id).mkdir()
    _force_resolved_paths(monkeypatch, {formal / linked_id: formal / sibling_id})

    # 旧判据：解析结果仍位于同一个formal-results内
    assert (formal / sibling_id).parent == formal

    assert sampler.artifact_group_path(frames_root, "formal-results", linked_id) is None


def test_group_directory_link_escape_is_rejected_without_rewriting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, synthetic_video: Path
) -> None:
    """``formal-results``被链接到artifact根目录外时，两个F1读取入口都拒绝且不改写。

    外部目标里放的是真实manifest且哈希匹配，旧判据会直接放行并读到它。
    """

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    result_id = str(result.index["frame_set_id"])
    manifest_bytes = (result.group_path / "manifest.json").read_bytes()

    link = frames_root / "formal-results"
    outside = tmp_path / "outside-formal-results"
    (outside / result_id).mkdir(parents=True)
    (outside / result_id / "manifest.json").write_bytes(manifest_bytes)
    outside_before = _tree_identity(outside)
    current = frames_root / sampler.FRAME_CURRENT_FILE
    current_before = current.read_bytes()
    group_before = sorted(path.name for path in result.group_path.iterdir())
    _force_resolved_paths(monkeypatch, {link: outside, link / result_id: outside / result_id})

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, "SYNTH01")
    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)

    assert current.read_bytes() == current_before
    assert sorted(path.name for path in result.group_path.iterdir()) == group_before
    assert _tree_identity(outside) == outside_before


@pytest.mark.parametrize(
    "result_id",
    [
        "../evil",
        "../../evil",
        "..",
        ".",
        ".tmp-frame-set-000000000000",
        "formal-results/../evil",
        "C:evil",
        "C:\\evil",
        "\\\\server\\share\\evil",
        "frame-set-000000000000.",
        "",
    ],
)
def test_current_result_id_escape_forms_are_rejected(
    tmp_path: Path, synthetic_video: Path, result_id: str
) -> None:
    """result_id必须是安全的单层名称，内部读取和公开读取都拒绝其余形态。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    legit_current = (frames_root / sampler.FRAME_CURRENT_FILE).read_bytes()
    assert sampler.is_safe_result_id(result_id) is False
    _write_current_payload(
        frames_root,
        {
            "schema_name": "ArtifactCurrentPointer",
            "schema_version": "1.0",
            "artifact_type": "frame_set",
            "result_id": result_id,
            "group": f"formal-results/{result_id}",
            "manifest_sha256": "0" * 64,
        },
    )
    current_before = (frames_root / sampler.FRAME_CURRENT_FILE).read_bytes()

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, "SYNTH01")

    assert (frames_root / sampler.FRAME_CURRENT_FILE).read_bytes() == current_before
    assert result.group_path.is_dir()
    # 恢复合法指针后原有正式组仍是完整的缓存命中，说明拒绝损坏指针没有破坏结果组
    (frames_root / sampler.FRAME_CURRENT_FILE).write_bytes(legit_current)
    assert _assert_current_target_is_intact(output_root, "SYNTH01") == result.index["frame_set_id"]


def test_current_group_escape_beyond_artifact_root_is_rejected(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """``..``逃逸的current：逃逸目标真实存在且哈希匹配也必须被拒绝。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    manifest_bytes = (result.group_path / "manifest.json").read_bytes()

    escaped_group = (frames_root / "formal-results" / "../../../evil").resolve()
    assert escaped_group == output_root / "SYNTH01" / "evil"  # 逃出m2，不只是越出formal-results
    escaped_group.mkdir(parents=True)
    (escaped_group / "manifest.json").write_bytes(manifest_bytes)
    escaped = {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "frame_set",
        "result_id": "../../../evil",
        "group": "formal-results/../../../evil",
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    # 复现旧判据：group字段与f-string一致即可通过，随后直接按group拼路径，逃逸组的
    # manifest会被读到且哈希匹配——本用例在旧实现下会被放行，因此是真正的回归测试。
    assert escaped["group"] == f"formal-results/{escaped['result_id']}"
    assert (escaped_group / "manifest.json").is_file()

    _write_current_payload(frames_root, escaped)
    current_before = (frames_root / sampler.FRAME_CURRENT_FILE).read_bytes()
    group_before = sorted(path.name for path in result.group_path.iterdir())

    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.sample_frames("SYNTH01", output_root=output_root)
    with pytest.raises(sampler.CurrentPointerCorruptError):
        sampler.load_frame_set_current(output_root, "SYNTH01")

    assert (frames_root / sampler.FRAME_CURRENT_FILE).read_bytes() == current_before
    assert sorted(path.name for path in result.group_path.iterdir()) == group_before
    assert (escaped_group / "manifest.json").read_bytes() == manifest_bytes


def test_load_frame_set_current_rejects_every_corrupt_form(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """公开读取入口与内部读取对损坏形态的分类必须一致。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    result = sampler.sample_frames("SYNTH01", output_root=output_root)
    frames_root = _frames_root(output_root, "SYNTH01")
    result_id = str(result.index["frame_set_id"])
    manifest_sha256 = hashlib.sha256(
        (result.group_path / "manifest.json").read_bytes()
    ).hexdigest()
    valid = {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "frame_set",
        "result_id": result_id,
        "group": f"formal-results/{result_id}",
        "manifest_sha256": manifest_sha256,
    }
    payloads = _corrupt_current_payloads(result_id) + [
        ("非字符串result_id", {**valid, "result_id": 7}),
        (
            "指向缺失组",
            {**valid, "result_id": "frame-set-ffffffffffff", "group": "formal-results/frame-set-ffffffffffff"},
        ),
        (
            "路径逃逸",
            {**valid, "result_id": "../../../evil", "group": "formal-results/../../../evil"},
        ),
    ]

    assert sampler.load_frame_set_current(output_root, "SYNTH01") == valid
    for label, payload in payloads:
        _write_current_payload(frames_root, payload)
        current_before = (frames_root / sampler.FRAME_CURRENT_FILE).read_bytes()
        with pytest.raises(sampler.CurrentPointerCorruptError):
            sampler.load_frame_set_current(output_root, "SYNTH01")
        assert (frames_root / sampler.FRAME_CURRENT_FILE).read_bytes() == current_before, label
    assert result.group_path.is_dir()


def test_ffmpeg_resolution_failure_is_a_typed_frame_sampler_error(
    tmp_path: Path, synthetic_video: Path
) -> None:
    """缺少FFmpeg时收敛为F1类型化失败，不泄漏编排层异常，也不泄漏目录。"""

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    with pytest.raises(sampler.FrameExtractionFailedError) as error:
        sampler.sample_frames(
            "SYNTH01", output_root=output_root, ffmpeg_dir=tmp_path / "no-ffmpeg-here"
        )
    message = str(error.value)
    assert message.startswith("FRAME_EXTRACTION_FAILED")
    assert str(tmp_path) not in message
    assert not (_frames_root(output_root, "SYNTH01") / "formal-results").exists()


def _failing_ffmpeg_run(real_run: object):
    """只让真正的抽帧命令失败，放行FFprobe等其它子进程。"""

    def _run(command: object, *args: object, **kwargs: object) -> object:
        if isinstance(command, (list, tuple)) and "-frames:v" in command:
            raise OSError("模拟FFmpeg执行失败。")
        return real_run(command, *args, **kwargs)  # type: ignore[operator]

    return _run


def test_ffmpeg_execution_failure_is_a_typed_frame_sampler_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, synthetic_video: Path
) -> None:
    """工具可解析但真正抽取帧时执行失败，同样收敛为F1类型化失败。

    与“工具缺失”不同，该失败发生在锁与审计边界之内，因此既要类型化，
    也要留下失败审计且不产生任何正式组或current。
    """

    output_root = _workspace(tmp_path, "SYNTH01", synthetic_video)
    monkeypatch.setattr(
        sampler.subprocess, "run", _failing_ffmpeg_run(subprocess.run)
    )

    with pytest.raises(sampler.FrameExtractionFailedError) as error:
        sampler.sample_frames("SYNTH01", output_root=output_root)

    message = str(error.value)
    assert message.startswith("FRAME_EXTRACTION_FAILED")
    assert str(tmp_path) not in message
    frames_root = _frames_root(output_root, "SYNTH01")
    assert not (frames_root / "current.json").exists()
    # 失败发生在锁内，`formal-results`会被建为空目录，但不得留下任何结果组或临时组
    published = list((frames_root / "formal-results").glob("*"))
    assert published == []
    assert list(frames_root.glob(".tmp-*")) == []
    audits = _failure_audits(output_root, "SYNTH01")
    assert len(audits) == 1
    audit_text = audits[0].read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit["error_code"] == "FRAME_EXTRACTION_FAILED"
    assert audit["module"] == sampler.FRAME_MODULE_TOKEN
    assert audit["stage"] == "frame_extraction"
    assert audit["temporary_cleanup_status"] == "temp_removed"
    assert audit["current_unchanged"] is True
    assert audit["recoverable"] is True
    assert audit["next_action"] == "continue_text_pipeline"
    assert audit["business_call_count"] == 0
    assert audit["supplier_request_count"] == 0
    assert str(tmp_path) not in audit_text
