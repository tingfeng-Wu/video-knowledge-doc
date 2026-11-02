from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from video_doc import page_change_detector as detector
from video_doc.page_change_detector import (
    PAGE_CHANGE_PROFILES,
    PAGE_CHANGE_V1,
    InputFrameSetCorruptError,
    PageChangeCacheCorruptError,
    PageChangeConcurrentRunError,
    PageChangeCurrentPointerCorruptError,
    PageChangeDetectorError,
    PageChangePublishFailedError,
    ProxyFrame,
    _candidate_limit,
    detect_page_changes,
    select_page_changes,
)


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


def _publish_frame_set(root: Path, task_id: str = "SYNTH01", values: tuple[int, ...] = (0, 255, 0, 255)) -> tuple[Path, dict[str, ProxyFrame]]:
    frames_root = root / task_id / "m2" / "frames"
    result_id = "frame-set-test00000001"
    group = frames_root / "formal-results" / result_id
    images = group / "frames"
    images.mkdir(parents=True)
    frames = _frames(len(values))
    proxies: dict[str, ProxyFrame] = {}
    files: dict[str, dict[str, object]] = {}
    for index, (frame, value) in enumerate(zip(frames, values)):
        path = group / str(frame["file"])
        payload = f"fake-png-{index}".encode()
        path.write_bytes(payload)
        frame["sha256"] = _sha(payload)
        frame["size_bytes"] = len(payload)
        files[str(frame["file"])] = {"sha256": frame["sha256"], "bytes": len(payload)}
        pixels = np.full((8, 8), value, dtype=np.uint8)
        bits = tuple(bool((bit >> (index % 6)) & 1) for bit in range(64))
        proxies[path.name] = ProxyFrame(pixels, bits)
    index_payload = {
        "schema_name": "FrameSet",
        "schema_version": "1.0",
        "status": "success",
        "frame_set_id": result_id,
        "task_id": task_id,
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
        "task_id": task_id,
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
    frames_root.mkdir(parents=True, exist_ok=True)
    (frames_root / "current.json").write_bytes(_json_bytes(current))
    return group, proxies


def _loader(proxies: dict[str, ProxyFrame]):
    def load(path: Path, width: int) -> ProxyFrame:
        assert width == 320
        return proxies[path.name]
    return load


def _tree_identity(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (_sha(path.read_bytes()), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _page_root(tmp_path: Path, task_id: str = "SYNTH01") -> Path:
    return tmp_path / task_id / "m2" / "frames" / "page-changes"


def _failure_audits(tmp_path: Path, task_id: str = "SYNTH01") -> list[Path]:
    return sorted(
        (tmp_path / task_id / "m2" / "failures" / "page-changes").glob("*/failure.json")
    )


def _assert_page_current_target_is_intact(tmp_path: Path, task_id: str = "SYNTH01") -> str:
    """复读page-changes current，确认其指向的组、manifest和登记文件都真实存在。"""

    root = _page_root(tmp_path, task_id)
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    group = root / str(pointer["group"])
    assert group.is_dir()
    manifest_path = group / "manifest.json"
    assert manifest_path.is_file()
    assert _sha(manifest_path.read_bytes()) == pointer["manifest_sha256"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result_id"] == pointer["result_id"]
    assert manifest["files"]
    for relative, identity in manifest["files"].items():
        target = group / relative
        assert target.is_file()
        assert _sha(target.read_bytes()) == identity["sha256"]
    index = json.loads((group / "page-change-set.json").read_text(encoding="utf-8"))
    assert index["page_change_set_id"] == pointer["result_id"]
    return str(pointer["result_id"])


def _patch_replace_failing_for(
    monkeypatch: pytest.MonkeyPatch, file_name: str
) -> list[Path]:
    """让模块内的``os.replace``在目标为该文件名时于替换前失败。

    只替换``page_change_detector``命名空间里的``os``，返回被写出过的临时源
    路径，便于断言临时文件确实写出过、随后又被精确清理。
    """

    seen: list[Path] = []
    real_os = detector.os

    class _ReplaceFailingOs:
        def replace(self, source: object, target: object) -> None:
            if Path(str(target)).name == file_name:
                seen.append(Path(str(source)))
                raise OSError("模拟os.replace在替换前失败。")
            real_os.replace(source, target)

        def __getattr__(self, attribute: str) -> object:
            return getattr(real_os, attribute)

    monkeypatch.setattr(detector, "os", _ReplaceFailingOs())
    return seen


def _register_test_profile() -> str:
    """注册临时profile，用于在已有正式组的任务上生成新的结果组身份。"""

    profile = replace(PAGE_CHANGE_V1, profile_id="test-quantile", adaptive_quantile=0.0)
    PAGE_CHANGE_PROFILES[profile.profile_id] = profile
    return profile.profile_id


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [(1_000, 12), (914_923, 23), (1_076_000, 27), (1_448_739, 37), (3_285_035, 83), (9_000_000, 90)],
)
def test_candidate_limit_matches_f0(duration_ms: int, expected: int) -> None:
    assert _candidate_limit(duration_ms, PAGE_CHANGE_V1) == expected


def test_identical_frames_produce_explicit_empty_result() -> None:
    frames = _frames(4)
    proxies = [_proxy(index, 50) for index in range(4)]
    comparisons, candidates, selection = select_page_changes(frames, proxies, duration_ms=15_000)
    assert candidates == []
    assert all(item["change_strength"] == 0 for item in comparisons)
    assert selection["threshold_pass_count"] == 0


def test_change_strength_and_candidate_time_are_traceable() -> None:
    frames = _frames(3, interval_ms=15_000)
    proxies = [_proxy(0, 0), _proxy(1, 255), _proxy(2, 0)]
    comparisons, candidates, selection = select_page_changes(frames, proxies, duration_ms=30_000)
    assert len(candidates) == 2
    assert [item["frame_id"] for item in candidates] == ["fr000002", "fr000003"]
    assert all(item["change_strength"] == 1.0 for item in candidates)
    assert selection["applied_threshold"] == 1.0
    assert comparisons[0]["previous_frame_id"] == "fr000001"


def test_temporal_neighbor_keeps_stronger_change() -> None:
    frames = _frames(4)
    proxies = [_proxy(0, 0), _proxy(1, 100), _proxy(2, 255), _proxy(3, 0)]
    profile = replace(PAGE_CHANGE_V1, adaptive_quantile=0.0)
    comparisons, candidates, selection = select_page_changes(
        frames, proxies, duration_ms=15_000, profile=profile
    )
    assert [item["frame_id"] for item in candidates] == ["fr000002", "fr000004"]
    assert selection["temporal_neighbor_excluded"] == 1


def test_perceptual_duplicate_is_removed_even_when_far_apart() -> None:
    frames = _frames(4, interval_ms=15_000)
    shared_hash = tuple(index % 2 == 0 for index in range(64))
    proxies = [
        ProxyFrame(np.zeros((8, 8), dtype=np.uint8), tuple(False for _ in range(64))),
        ProxyFrame(np.full((8, 8), 255, dtype=np.uint8), shared_hash),
        ProxyFrame(np.zeros((8, 8), dtype=np.uint8), tuple(index % 3 == 0 for index in range(64))),
        ProxyFrame(np.full((8, 8), 255, dtype=np.uint8), shared_hash),
    ]
    _, candidates, selection = select_page_changes(frames, proxies, duration_ms=45_000)
    assert len(candidates) == 2
    assert selection["perceptual_duplicate_excluded"] == 1


def test_detector_publishes_independent_group_and_current(tmp_path: Path) -> None:
    frame_group, proxies = _publish_frame_set(tmp_path)
    before = _tree_identity(frame_group)
    result = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    assert result.index["schema_name"] == "PageChangeSet"
    assert result.index["status"] == "success"
    assert result.index["input_frame_set"]["frame_set_id"] == frame_group.name
    assert result.group_path.parent.parent == frame_group.parent.parent / "page-changes"
    assert json.loads((result.group_path.parent.parent / "current.json").read_text())["result_id"] == result.index["page_change_set_id"]
    assert _tree_identity(frame_group) == before


def test_detector_publishes_empty_group(tmp_path: Path) -> None:
    _, proxies = _publish_frame_set(tmp_path, values=(10, 10, 10))
    result = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    assert result.index["status"] == "empty"
    assert result.index["candidate_count"] == 0
    assert result.manifest["status"] == "empty"


def test_second_run_is_zero_rewrite_cache_hit(tmp_path: Path) -> None:
    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    before = _tree_identity(first.group_path.parent.parent)
    second = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=lambda *_: pytest.fail("cache hit decoded an image"))
    assert second.cache_hit is True
    assert second.pointer_repaired is False
    assert _tree_identity(first.group_path.parent.parent) == before


def test_missing_page_current_is_repaired_without_group_rewrite(tmp_path: Path) -> None:
    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    before = _tree_identity(first.group_path)
    (first.group_path.parent.parent / "current.json").unlink()
    second = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=lambda *_: pytest.fail("repair decoded an image"))
    assert second.cache_hit is True
    assert second.pointer_repaired is True
    assert _tree_identity(first.group_path) == before


def test_missing_frame_current_hard_fails(tmp_path: Path) -> None:
    with pytest.raises(PageChangeDetectorError, match="frames current"):
        detect_page_changes("SYNTH01", output_root=tmp_path)


def test_corrupt_frame_file_hard_fails_before_detection(tmp_path: Path) -> None:
    group, proxies = _publish_frame_set(tmp_path)
    (group / "frames" / "fr000002.png").write_bytes(b"changed")
    with pytest.raises(InputFrameSetCorruptError, match="帧图片"):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))


def test_corrupt_page_cache_hard_fails_without_overwrite(tmp_path: Path) -> None:
    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    current_before = (first.group_path.parent.parent / "current.json").read_bytes()
    first.index_path.write_bytes(b"{}\n")
    with pytest.raises(PageChangeCacheCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    assert (first.group_path.parent.parent / "current.json").read_bytes() == current_before


def test_page_current_pointing_at_missing_group_hard_fails(tmp_path: Path) -> None:
    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    current_path = first.group_path.parent.parent / "current.json"
    current = json.loads(current_path.read_text())
    current["result_id"] = "page-change-set-missing"
    current["group"] = "formal-results/page-change-set-missing"
    current_path.write_bytes(_json_bytes(current))
    with pytest.raises(PageChangeCurrentPointerCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))


def test_existing_page_change_lock_reports_concurrent_run(tmp_path: Path) -> None:
    _, proxies = _publish_frame_set(tmp_path)
    locks = tmp_path / "SYNTH01" / "m2" / ".locks"
    locks.mkdir(parents=True)
    (locks / "page-changes.lock").write_text("held")
    with pytest.raises(PageChangeConcurrentRunError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))


@pytest.mark.parametrize(
    "point",
    ["after_detection", "after_index", "after_manifest", "before_group_publish", "after_group_publish", "before_current"],
)
def test_injected_failure_does_not_publish_or_switch_current(tmp_path: Path, point: str) -> None:
    frame_group, proxies = _publish_frame_set(tmp_path)
    frame_before = _tree_identity(frame_group)
    with pytest.raises(PageChangeDetectorError):
        detect_page_changes("SYNTH01", output_root=tmp_path, failure_injection=point, _proxy_loader=_loader(proxies))
    root = frame_group.parent.parent / "page-changes"
    assert not (root / "current.json").exists()
    assert not list((root / "formal-results").glob("page-change-set-*")) if (root / "formal-results").exists() else True
    assert _tree_identity(frame_group) == frame_before
    failures = list((tmp_path / "SYNTH01" / "m2" / "failures" / "page-changes").glob("*/failure.json"))
    assert len(failures) == 1
    assert json.loads(failures[0].read_text())["current_unchanged"] is True


def test_group_revalidation_runs_before_current_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式组复验必须发生在current切换之前，否则复验失败会留下悬空指针。"""

    _, proxies = _publish_frame_set(tmp_path)
    current_path = _page_root(tmp_path) / "current.json"
    real_validate = detector.validate_page_change_group
    current_existed_at_revalidation: list[bool] = []

    def recording_validate(group_path: object, **kwargs: object):
        if kwargs.get("require_directory_name", True):
            current_existed_at_revalidation.append(current_path.exists())
        return real_validate(group_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(detector, "validate_page_change_group", recording_validate)
    detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))

    assert current_existed_at_revalidation == [False]
    assert current_path.exists()


def test_group_revalidation_failure_keeps_old_current_and_removes_new_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式组复验失败时，新组必须清理，旧current必须原样保留且仍指向有效组。"""

    _, proxies = _publish_frame_set(tmp_path)
    loader = _loader(proxies)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)
    root = _page_root(tmp_path)
    current_path = root / "current.json"
    current_before = current_path.read_bytes()
    group_before = _tree_identity(first.group_path)

    real_validate = detector.validate_page_change_group

    def failing_validate(group_path: object, **kwargs: object):
        if kwargs.get("require_directory_name", True):
            raise PageChangeCacheCorruptError("CACHE_CORRUPT：模拟正式组复验失败。")
        return real_validate(group_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(detector, "validate_page_change_group", failing_validate)
    profile_id = _register_test_profile()
    try:
        with pytest.raises(PageChangeCacheCorruptError):
            detect_page_changes(
                "SYNTH01",
                output_root=tmp_path,
                profile_id=profile_id,
                _proxy_loader=loader,
            )
    finally:
        PAGE_CHANGE_PROFILES.pop(profile_id, None)

    assert current_path.read_bytes() == current_before
    assert _assert_page_current_target_is_intact(tmp_path) == (
        first.index["page_change_set_id"]
    )
    assert _tree_identity(first.group_path) == group_before
    assert [path.name for path in (root / "formal-results").iterdir()] == [
        first.group_path.name
    ]
    assert not list((root / "formal-results").glob(".tmp-*"))

    audit = json.loads(_failure_audits(tmp_path)[-1].read_text(encoding="utf-8"))
    assert audit["stage"] == "group_revalidation"
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "group_removed"


def test_publish_failure_before_current_keeps_old_current_and_removes_new_group(
    tmp_path: Path,
) -> None:
    """current替换前失败时，旧current逐字节不变，新组和临时目录都必须清理。"""

    _, proxies = _publish_frame_set(tmp_path)
    loader = _loader(proxies)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)
    root = _page_root(tmp_path)
    current_path = root / "current.json"
    current_before = current_path.read_bytes()
    group_before = _tree_identity(first.group_path)

    profile_id = _register_test_profile()
    try:
        with pytest.raises(PageChangeDetectorError):
            detect_page_changes(
                "SYNTH01",
                output_root=tmp_path,
                profile_id=profile_id,
                failure_injection="before_current",
                _proxy_loader=loader,
            )
    finally:
        PAGE_CHANGE_PROFILES.pop(profile_id, None)

    assert current_path.read_bytes() == current_before
    assert _assert_page_current_target_is_intact(tmp_path) == (
        first.index["page_change_set_id"]
    )
    assert _tree_identity(first.group_path) == group_before
    assert [path.name for path in (root / "formal-results").iterdir()] == [
        first.group_path.name
    ]
    assert not list((root / "formal-results").glob(".tmp-*"))

    audit = json.loads(_failure_audits(tmp_path)[-1].read_text(encoding="utf-8"))
    assert audit["stage"] == "current_publish"
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "group_removed"


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_failure_after_current_replace_keeps_published_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    """current底层替换成功后、状态标志更新前的异常或中断不得删除新组。"""

    _, proxies = _publish_frame_set(tmp_path)
    loader = _loader(proxies)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)
    root = _page_root(tmp_path)
    group_before = _tree_identity(first.group_path)
    real_replace_current = detector._replace_current
    raised: list[BaseException] = []

    def replace_then_fail(target_root: Path, payload: object) -> None:
        real_replace_current(target_root, payload)  # type: ignore[arg-type]
        error = error_type("模拟current替换成功后、状态标志更新前失败。")
        raised.append(error)
        raise error

    monkeypatch.setattr(detector, "_replace_current", replace_then_fail)
    profile_id = _register_test_profile()
    expected = PageChangePublishFailedError if error_type is RuntimeError else error_type
    try:
        with pytest.raises(expected) as excinfo:
            detect_page_changes(
                "SYNTH01",
                output_root=tmp_path,
                profile_id=profile_id,
                _proxy_loader=loader,
            )
    finally:
        PAGE_CHANGE_PROFILES.pop(profile_id, None)

    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    assert pointer["result_id"] != first.index["page_change_set_id"]
    assert _assert_page_current_target_is_intact(tmp_path) == pointer["result_id"]
    assert _tree_identity(first.group_path) == group_before
    assert sorted(path.name for path in (root / "formal-results").iterdir()) == sorted(
        [first.group_path.name, str(pointer["result_id"])]
    )
    assert not list((root / "formal-results").glob(".tmp-*"))
    assert not (tmp_path / "SYNTH01" / "m2" / ".locks" / "page-changes.lock").exists()

    if error_type is RuntimeError:
        assert isinstance(excinfo.value, PageChangePublishFailedError)
    else:
        # 中断必须原样重新抛出，不能被包装、替换或吞掉。
        assert excinfo.value is raised[0]

    # 普通异常与中断都必须留下失败审计，且字段符合磁盘事实。
    audit = json.loads(_failure_audits(tmp_path)[-1].read_text(encoding="utf-8"))
    assert audit["task_id"] == "SYNTH01"
    assert audit["module"] == "page-changes"
    assert audit["stage"] == "current_publish"
    # 磁盘上的current已经改变，审计必须如实记录，而不是照抄内存标志。
    assert audit["current_unchanged"] is False
    assert audit["temporary_cleanup_status"] == "published"


@pytest.mark.parametrize("audit_error", [OSError, KeyboardInterrupt, SystemExit])
def test_audit_write_failure_never_replaces_the_original_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_error: type[BaseException]
) -> None:
    """审计写入失败或写入期间再次被中断，都必须原样抛出原始中断。"""

    _, proxies = _publish_frame_set(tmp_path)
    loader = _loader(proxies)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)
    real_replace_current = detector._replace_current
    interrupted: list[BaseException] = []

    def replace_then_interrupt(target_root: Path, payload: object) -> None:
        real_replace_current(target_root, payload)  # type: ignore[arg-type]
        error = KeyboardInterrupt("模拟current替换成功后发生中断。")
        interrupted.append(error)
        raise error

    def failing_audit(*args: object, **kwargs: object) -> None:
        raise audit_error("模拟审计写入失败。")

    monkeypatch.setattr(detector, "_replace_current", replace_then_interrupt)
    monkeypatch.setattr(detector, "_write_failure", failing_audit)
    profile_id = _register_test_profile()
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            detect_page_changes(
                "SYNTH01",
                output_root=tmp_path,
                profile_id=profile_id,
                _proxy_loader=loader,
            )
    finally:
        PAGE_CHANGE_PROFILES.pop(profile_id, None)

    assert excinfo.value is interrupted[0]
    # 审计没写成，但清理仍按磁盘事实完成：current已指向新组，新组必须保留。
    assert (
        _assert_page_current_target_is_intact(tmp_path)
        != first.index["page_change_set_id"]
    )
    assert not _failure_audits(tmp_path)


def test_unparsable_current_after_replace_keeps_published_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """current存在但无法解析时不得猜着删组，否则可能删掉它实际指向的新组。"""

    _, proxies = _publish_frame_set(tmp_path)
    loader = _loader(proxies)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)
    root = _page_root(tmp_path)
    current_path = root / "current.json"

    def replace_with_garbage(target_root: Path, payload: object) -> None:
        (target_root / "current.json").write_bytes(b"{not-json")
        raise RuntimeError("模拟current写入后损坏且状态标志未更新。")

    monkeypatch.setattr(detector, "_replace_current", replace_with_garbage)
    profile_id = _register_test_profile()
    try:
        with pytest.raises(PageChangePublishFailedError):
            detect_page_changes(
                "SYNTH01",
                output_root=tmp_path,
                profile_id=profile_id,
                _proxy_loader=loader,
            )
    finally:
        PAGE_CHANGE_PROFILES.pop(profile_id, None)

    groups = sorted(path.name for path in (root / "formal-results").iterdir())
    assert first.group_path.name in groups
    assert len(groups) == 2
    assert current_path.read_bytes() == b"{not-json"

    audit = json.loads(_failure_audits(tmp_path)[-1].read_text(encoding="utf-8"))
    assert audit["current_unchanged"] is False
    assert audit["temporary_cleanup_status"] == "published"


def test_current_replacement_failure_leaves_no_temporary_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """current替换前失败时，旧current有效、新组按既定规则清理、临时指针无残留。"""

    _, proxies = _publish_frame_set(tmp_path)
    loader = _loader(proxies)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)
    root = _page_root(tmp_path)
    current_path = root / "current.json"
    current_before = current_path.read_bytes()
    group_before = _tree_identity(first.group_path)
    entries_before = sorted(path.name for path in root.iterdir())

    written = _patch_replace_failing_for(monkeypatch, "current.json")
    profile_id = _register_test_profile()
    try:
        with pytest.raises(PageChangePublishFailedError):
            detect_page_changes(
                "SYNTH01",
                output_root=tmp_path,
                profile_id=profile_id,
                _proxy_loader=loader,
            )
    finally:
        PAGE_CHANGE_PROFILES.pop(profile_id, None)

    # 临时指针确实写出过，并被_replace_current自己按精确路径清理干净。
    assert len(written) == 1
    assert written[0].name.startswith(".current.json.tmp-")
    assert not written[0].exists()
    # 旧current逐字节保持有效，指向的组与登记文件都还在且未改写。
    assert current_path.read_bytes() == current_before
    assert _assert_page_current_target_is_intact(tmp_path) == first.index["page_change_set_id"]
    assert _tree_identity(first.group_path) == group_before
    # 新正式组按既定规则清理；根目录除旧内容外无任何新增条目。
    assert sorted(path.name for path in root.iterdir()) == entries_before
    assert sorted(
        path.name for path in (root / "formal-results").iterdir()
    ) == [first.group_path.name]

    audit = json.loads(_failure_audits(tmp_path)[-1].read_text(encoding="utf-8"))
    assert audit["stage"] == "current_publish"
    assert audit["current_unchanged"] is True
    assert audit["temporary_cleanup_status"] == "group_removed"


def test_successful_current_replacement_leaves_no_temporary_pointer(
    tmp_path: Path,
) -> None:
    """os.replace成功后，_replace_current的finally清理是空操作，不留临时指针。"""

    _, proxies = _publish_frame_set(tmp_path)
    loader = _loader(proxies)
    detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)
    root = _page_root(tmp_path)
    # 缓存修复路径同样只重写current，必须走同一条清理逻辑。
    (root / "current.json").unlink()
    detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=loader)

    assert sorted(path.name for path in root.iterdir()) == [
        "current.json",
        "formal-results",
    ]


def test_cache_identity_changes_with_detector_profile_only(tmp_path: Path) -> None:
    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    # Published identity proves F2 config is independent from the unchanged frame-set ID.
    assert first.index["input_frame_set"]["frame_set_id"] == "frame-set-test00000001"
    assert first.index["profile"]["proxy_width_pixels"] == 320
    assert first.index["cache_identity_sha256"] != first.index["input_frame_set"]["manifest_sha256"]


def test_unknown_profile_and_failure_point_are_rejected(tmp_path: Path) -> None:
    _publish_frame_set(tmp_path)
    with pytest.raises(PageChangeDetectorError, match="profile"):
        detect_page_changes("SYNTH01", output_root=tmp_path, profile_id="unknown")
    with pytest.raises(PageChangeDetectorError, match="失败注入"):
        detect_page_changes("SYNTH01", output_root=tmp_path, failure_injection="unknown")


def test_page_current_that_is_not_a_regular_file_hard_fails(tmp_path: Path) -> None:
    """current.json存在但不是普通文件时按CURRENT_POINTER_CORRUPT硬失败。"""

    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    current_path = first.group_path.parent.parent / "current.json"
    current_path.unlink()
    current_path.mkdir()

    with pytest.raises(PageChangeCurrentPointerCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    # 非普通文件按损坏保留原样，既不当成“没有current”也不删除
    assert current_path.is_dir()


def _force_symlink_predicate(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """把单个路径的``is_symlink``谓词替换为True，模拟活符号链接。

    本机没有创建符号链接的权限；只对这一个精确路径生效，其余路径仍走真实实现。
    """

    real_is_symlink = Path.is_symlink

    def is_symlink(self: Path) -> bool:
        return True if self == target else real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)


def test_page_current_live_symlink_hard_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """指向普通文件的活符号链接不得被当成普通current文件接受。"""

    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    current_path = first.group_path.parent.parent / "current.json"
    before = current_path.read_bytes()
    _force_symlink_predicate(monkeypatch, current_path)

    with pytest.raises(PageChangeCurrentPointerCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    assert current_path.read_bytes() == before
    assert first.group_path.is_dir()
    assert _assert_page_current_target_is_intact(tmp_path) == first.index["page_change_set_id"]


def test_input_frames_current_live_symlink_hard_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F1当前帧集指针是活符号链接时按输入帧集损坏硬失败，不误报为缺失。"""

    _, proxies = _publish_frame_set(tmp_path)
    current_path = tmp_path / "SYNTH01" / "m2" / "frames" / "current.json"
    before = current_path.read_bytes()
    _force_symlink_predicate(monkeypatch, current_path)

    with pytest.raises(InputFrameSetCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    assert current_path.read_bytes() == before
    assert not _page_root(tmp_path).exists()


def test_page_current_group_escape_is_rejected(tmp_path: Path) -> None:
    """页面变化current用``..``逃出formal-results时按CURRENT_POINTER_CORRUPT硬失败。"""

    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    root = _page_root(tmp_path)
    manifest_bytes = (first.group_path / "manifest.json").read_bytes()

    escaped_group = (root / "formal-results" / "../../../evil").resolve()
    assert escaped_group == tmp_path / "SYNTH01" / "m2" / "evil"
    escaped_group.mkdir(parents=True)
    (escaped_group / "manifest.json").write_bytes(manifest_bytes)
    escaped = {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "page_change_set",
        "result_id": "../../../evil",
        "group": "formal-results/../../../evil",
        "manifest_sha256": _sha(manifest_bytes),
    }
    # 复现旧判据：group字段与f-string一致即可通过，逃逸组的manifest随后会被读到
    # 且哈希匹配——本用例在旧实现下会被放行，因此是真正的回归测试。
    assert escaped["group"] == f"formal-results/{escaped['result_id']}"
    assert (escaped_group / "manifest.json").is_file()
    (root / "current.json").write_bytes(_json_bytes(escaped))
    current_before = (root / "current.json").read_bytes()

    with pytest.raises(PageChangeCurrentPointerCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))

    assert (root / "current.json").read_bytes() == current_before
    assert (escaped_group / "manifest.json").read_bytes() == manifest_bytes
    # 原有正式组未被改写，且没有发布新组
    assert [path.name for path in (root / "formal-results").iterdir()] == [first.group_path.name]


def test_input_frames_current_group_escape_is_rejected(tmp_path: Path) -> None:
    """F1输入的current用``..``逃出formal-results时按输入帧集损坏硬失败。"""

    group, proxies = _publish_frame_set(tmp_path)
    frames_root = tmp_path / "SYNTH01" / "m2" / "frames"
    manifest_bytes = (group / "manifest.json").read_bytes()

    escaped_group = (frames_root / "formal-results" / "../../../evil").resolve()
    assert escaped_group == tmp_path / "SYNTH01" / "evil"
    escaped_group.mkdir(parents=True)
    (escaped_group / "manifest.json").write_bytes(manifest_bytes)
    escaped = {
        "schema_name": "ArtifactCurrentPointer",
        "schema_version": "1.0",
        "artifact_type": "frame_set",
        "result_id": "../../../evil",
        "group": "formal-results/../../../evil",
        "manifest_sha256": _sha(manifest_bytes),
    }
    assert escaped["group"] == f"formal-results/{escaped['result_id']}"
    assert (escaped_group / "manifest.json").is_file()
    (frames_root / "current.json").write_bytes(_json_bytes(escaped))
    before = _tree_identity(tmp_path)

    with pytest.raises(InputFrameSetCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))

    # 输入帧集校验发生在锁与审计边界之前，因此整树不得有任何改写
    assert _tree_identity(tmp_path) == before
    assert not _page_root(tmp_path).exists()


@pytest.mark.parametrize(
    "result_id",
    ["../evil", "..", ".", "C:\\evil", "formal-results/../evil", "frame-set-test00000001."],
)
def test_input_frames_current_unsafe_result_id_is_rejected(
    tmp_path: Path, result_id: str
) -> None:
    """F1输入帧集current的result_id必须是安全的单层名称。"""

    group, proxies = _publish_frame_set(tmp_path)
    frames_root = tmp_path / "SYNTH01" / "m2" / "frames"
    assert detector.is_safe_result_id(result_id) is False
    (frames_root / "current.json").write_bytes(
        _json_bytes(
            {
                "schema_name": "ArtifactCurrentPointer",
                "schema_version": "1.0",
                "artifact_type": "frame_set",
                "result_id": result_id,
                "group": f"formal-results/{result_id}",
                "manifest_sha256": _sha((group / "manifest.json").read_bytes()),
            }
        )
    )
    before = _tree_identity(tmp_path)

    with pytest.raises(InputFrameSetCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))

    assert _tree_identity(tmp_path) == before
    assert group.is_dir()


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


def test_page_group_directory_linked_outside_root_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """page-changes的``formal-results``指向artifact根目录外时硬失败且不改写。

    外部目标里放的是真实manifest且哈希匹配；旧判据只比较父子关系，会直接放行。
    """

    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    root = _page_root(tmp_path)
    link = root / detector.PAGE_CHANGE_GROUP_DIRECTORY
    result_id = str(first.index["page_change_set_id"])
    manifest_bytes = (first.group_path / "manifest.json").read_bytes()
    outside = tmp_path / "outside-page-formal-results"
    (outside / result_id).mkdir(parents=True)
    (outside / result_id / "manifest.json").write_bytes(manifest_bytes)
    outside_before = _tree_identity(outside)
    current_before = (root / "current.json").read_bytes()
    _force_resolved_paths(monkeypatch, {link: outside, link / result_id: outside / result_id})

    # 旧判据：解析到外部之后父子关系仍然成立
    assert (outside / result_id).parent == outside

    with pytest.raises(PageChangeCurrentPointerCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))

    assert (root / "current.json").read_bytes() == current_before
    assert [path.name for path in link.iterdir()] == [first.group_path.name]
    assert _tree_identity(outside) == outside_before


def test_page_result_group_linked_to_sibling_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """页面变化结果组指向同级另一个组时硬失败且不改写。

    同级目标里放的是一份完整副本（manifest哈希匹配），旧判据下解析结果仍落在同一个
    ``formal-results``内，父子关系成立，因此旧实现会读到副本并继续，不会报这个错。
    """

    _, proxies = _publish_frame_set(tmp_path)
    first = detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    root = _page_root(tmp_path)
    formal = root / detector.PAGE_CHANGE_GROUP_DIRECTORY
    linked = formal / str(first.index["page_change_set_id"])
    sibling = formal / "page-change-set-111111111111"
    shutil.copytree(linked, sibling)
    sibling_before = _tree_identity(sibling)
    current_before = (root / "current.json").read_bytes()
    _force_resolved_paths(monkeypatch, {linked: sibling})

    # 旧判据：解析结果仍位于同一个formal-results内
    assert sibling.parent == formal

    with pytest.raises(PageChangeCurrentPointerCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))

    assert (root / "current.json").read_bytes() == current_before
    assert sorted(path.name for path in formal.iterdir()) == sorted(
        [first.group_path.name, sibling.name]
    )
    assert _tree_identity(sibling) == sibling_before


def test_input_frames_current_that_is_not_a_regular_file_hard_fails(tmp_path: Path) -> None:
    """F1的current.json是目录时按输入帧集损坏硬失败，不误报为缺失。"""

    _, proxies = _publish_frame_set(tmp_path)
    current_path = tmp_path / "SYNTH01" / "m2" / "frames" / "current.json"
    current_path.unlink()
    current_path.mkdir()

    with pytest.raises(InputFrameSetCorruptError):
        detect_page_changes("SYNTH01", output_root=tmp_path, _proxy_loader=_loader(proxies))
    assert current_path.is_dir()
