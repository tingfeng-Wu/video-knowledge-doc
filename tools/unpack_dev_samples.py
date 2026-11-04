"""Unpack repository development samples and print their local data paths."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile


REPO = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify_tree(root: Path, manifest: dict, package: zipfile.ZipFile, sample: dict):
    task_file = f"outputs/{sample['task_id']}/task.json"
    for name, expected in manifest["files"].items():
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or invalid file: {path}")
        if name == task_file:
            record = json.loads(package.read(name))
            record["source_video"] = str((root / sample["source_video"]).resolve())
            if json.loads(path.read_text(encoding="utf-8")) != record:
                raise ValueError(f"Task record differs from sample: {path}")
        elif digest(path.read_bytes()) != expected:
            raise ValueError(f"File hash mismatch: {path}")


def unpack(entry: dict, source: Path, destination: Path):
    task = entry["task_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task):
        raise ValueError("Invalid task ID")
    archive = source / entry["archive"]
    if archive.parent.resolve() != source.resolve():
        raise ValueError("Invalid archive path")
    if digest(archive.read_bytes()) != entry["sha256"]:
        raise ValueError(f"Archive hash mismatch: {archive.name}")
    final = destination / task
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP entries")
        for info in package.infolist():
            parts = PurePosixPath(info.filename)
            if parts.is_absolute() or ".." in parts.parts or "\\" in info.filename or ":" in info.filename:
                raise ValueError(f"Invalid ZIP path: {info.filename}")
            kind = (info.external_attr >> 16) & 0o170000
            if kind not in (0, 0o100000, 0o040000):
                raise ValueError(f"Non-regular ZIP entry: {info.filename}")
        manifest = json.loads(package.read("manifest.json"))
        if set(names) != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("ZIP members differ from manifest")
        sample = json.loads(package.read("sample.json"))
        if sample["task_id"] != task:
            raise ValueError("Sample identity mismatch")
        if final.exists():
            if final.is_symlink() or not final.is_dir():
                raise ValueError(f"Invalid destination: {final}")
            verify_tree(final, manifest, package, sample)
            reused = True
        else:
            destination.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=f".{task}-", dir=destination))
            for name, expected in manifest["files"].items():
                data = package.read(name)
                if digest(data) != expected:
                    raise ValueError(f"ZIP member hash mismatch: {name}")
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (stage / "manifest.json").write_bytes(package.read("manifest.json"))
            # task.json is local workspace metadata, not an immutable M2 result member.
            record_path = stage / "outputs" / task / "task.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["source_video"] = str((final / sample["source_video"]).resolve())
            record_path.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            video = stage / sample["source_video"]
            os.utime(video, ns=(sample["source_mtime_ns"], sample["source_mtime_ns"]))
            stage.rename(final)
            verify_tree(final, manifest, package, sample)
            reused = False
    return {"task_id": task, "reused": reused, "sample_root": str(final),
            "source_video": str(final / sample["source_video"]),
            "output_root": str(final / "outputs"), "results": sample["results"],
            "editorial_revision": sample.get("editorial_revision")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", help="Only unpack this task (repeatable); default: all")
    parser.add_argument("--destination", type=Path, default=REPO / "data/tmp/development-samples")
    args = parser.parse_args()
    source = REPO / "samples/development"
    index = json.loads((source / "index.json").read_text(encoding="utf-8"))
    available = {item["task_id"]: item for item in index["samples"]}
    selected = args.case or list(available)
    if set(selected) - set(available):
        parser.error("Unknown sample; choose from " + ", ".join(available))
    for task in selected:
        result = unpack(available[task], source, args.destination.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
