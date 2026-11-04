"""Portable sample extraction: local paths, reuse, integrity and archive boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from tools.unpack_dev_samples import unpack


def package(root: Path, *, unsafe: bool = False):
    root.mkdir()
    task = "SAMPLE01"
    members = {
        "source/SAMPLE01.mp4": b"synthetic-unit-test-file",
        "outputs/SAMPLE01/task.json": json.dumps(
            {"schema_version": "1.0", "task_id": task, "source_video": "source/SAMPLE01.mp4"}
        ).encode(),
        "sample.json": json.dumps(
            {"task_id": task, "source_video": "source/SAMPLE01.mp4",
             "source_mtime_ns": 1_700_000_000_000_000_000, "results": {}}
        ).encode(),
    }
    if unsafe:
        members["../outside.txt"] = b"unexpected"
    manifest = {"files": {k: hashlib.sha256(v).hexdigest() for k, v in members.items()}}
    archive = root / "sample.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, blob in members.items():
            z.writestr(name, blob)
        z.writestr("manifest.json", json.dumps(manifest))
    return {"task_id": task, "archive": archive.name,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}


def test_unpack_rebases_local_task_and_reuses_without_writes(tmp_path):
    source = tmp_path / "packages"
    entry = package(source)
    target = tmp_path / "member-workspace"
    result = unpack(entry, source, target)
    assert result["reused"] is False
    record = json.loads((Path(result["output_root"]) / "SAMPLE01/task.json").read_text())
    assert record["source_video"] == result["source_video"]
    assert Path(record["source_video"]).is_file()
    before = {p.relative_to(target): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in target.rglob("*") if p.is_file()}
    assert unpack(entry, source, target)["reused"] is True
    after = {p.relative_to(target): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in target.rglob("*") if p.is_file()}
    assert after == before


def test_invalid_archive_hash_writes_nothing(tmp_path):
    source = tmp_path / "packages"
    entry = package(source)
    entry["sha256"] = "0" * 64
    target = tmp_path / "destination"
    with pytest.raises(ValueError, match="Archive hash mismatch"):
        unpack(entry, source, target)
    assert not target.exists()


def test_modified_existing_sample_is_preserved(tmp_path):
    source = tmp_path / "packages"
    entry = package(source)
    target = tmp_path / "destination"
    result = unpack(entry, source, target)
    video = Path(result["source_video"])
    video.write_bytes(b"member-local-change")
    with pytest.raises(ValueError, match="File hash mismatch"):
        unpack(entry, source, target)
    assert video.read_bytes() == b"member-local-change"


def test_archive_parent_path_is_rejected_before_extraction(tmp_path):
    source = tmp_path / "packages"
    entry = package(source, unsafe=True)
    target = tmp_path / "destination"
    with pytest.raises(ValueError, match="Invalid ZIP path"):
        unpack(entry, source, target)
    assert not target.exists()
    assert not (tmp_path / "outside.txt").exists()
