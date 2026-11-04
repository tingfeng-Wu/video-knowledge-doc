"""Build portable development packs from selected project seed tasks; zero API calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(project: Path, target: Path, task: str, editorial: str | None):
    task_root = project / "outputs" / task
    record = json.loads((task_root / "task.json").read_text(encoding="utf-8"))
    source = Path(record["source_video"])
    if not source.is_absolute():
        source = project / source
    files: dict[str, bytes] = {}
    source_rel = f"source/{task}.mp4"
    files[source_rel] = source.read_bytes()
    base = f"outputs/{task}"
    files[f"{base}/task.json"] = json_bytes(
        {"schema_version": "1.0", "task_id": task, "source_video": source_rel}
    )
    metadata = json.loads((task_root / "metadata.json").read_text(encoding="utf-8"))
    metadata["source_path"] = source_rel
    files["reference/video-metadata.json"] = json_bytes(metadata)
    transcript = json.loads((task_root / "asr/transcript.json").read_text(encoding="utf-8"))
    transcript["source_audio"]["path"] = "audio.wav"
    files["reference/asr-transcript.json"] = json_bytes(transcript)

    results = {}
    m2 = task_root / "m2"
    for pointer_path in sorted(m2.rglob("current.json")):
        if "formal-results" in pointer_path.relative_to(m2).parts:
            continue
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        group = (pointer_path.parent / pointer["group"]).resolve()
        if not group.is_relative_to(m2.resolve()) or not group.is_dir():
            raise ValueError(f"Invalid current group: {pointer_path}")
        files[f"{base}/" + pointer_path.relative_to(task_root).as_posix()] = pointer_path.read_bytes()
        for item in sorted(group.rglob("*")):
            if item.is_file():
                files[f"{base}/" + item.relative_to(task_root).as_posix()] = item.read_bytes()
        results[pointer_path.parent.relative_to(m2).as_posix()] = pointer["result_id"]
    if editorial:
        group = m2 / "editorial-revisions" / editorial
        if not group.is_dir():
            raise ValueError(f"Editorial revision not found: {editorial}")
        for item in sorted(group.rglob("*")):
            if item.is_file():
                files[f"{base}/" + item.relative_to(task_root).as_posix()] = item.read_bytes()

    sample = {
        "schema_version": "1.0", "task_id": task,
        "source_video": source_rel,
        "source_sha256": sha256(files[source_rel]),
        "source_mtime_ns": source.stat().st_mtime_ns,
        "duration_seconds": metadata["duration_seconds"],
        "width": metadata["width"], "height": metadata["height"],
        "output_root": "outputs", "results": results,
        "scenario": {"COURSE01": "course", "EXPLAINER01": "science-explainer", "TALK01": "talk"}.get(task, "video"),
        "editorial_revision": editorial,
        "transcript": "reference/asr-transcript.json",
        "notes": "Current M2 groups retain their original bytes. ASR is a path-normalized reference copy; use a new task ID for fresh processing.",
    }
    files["sample.json"] = json_bytes(sample)
    files["README.md"] = (
        f"# {task} development sample\n\n"
        "source/ contains the video. outputs/ contains the available current M2 result groups.\n"
        + ("This sample includes chapter content and document exports.\n" if "documents" in results
           else "This preprocessing sample includes frames and page changes; chapter content and exports are not included.\n") +
        "reference/ contains video metadata and ASR text. No API Key is required to read existing results.\n"
        "Use tools/unpack_dev_samples.py from the repository to unpack and set the local video path.\n"
        "The machine result and editorial revision are separate. See sample.json for their IDs.\n"
    ).encode()
    # Selected artifacts are checked before packaging, including text inside DOCX/PDF.
    for name, data in files.items():
        if name.endswith((".json", ".md", ".txt")):
            text = data.decode("utf-8")
            if re.search(r"sk-[A-Za-z0-9]{28,}|(?<![A-Za-z])[A-Za-z]:[\\/]|/home/|/Users/", text):
                raise ValueError(f"Credential-like string or workstation path in {name}")
    files["manifest.json"] = json_bytes(
        {"schema_version": "1.0", "task_id": task,
         "files": {name: sha256(data) for name, data in sorted(files.items())}}
    )
    target.mkdir(parents=True, exist_ok=True)
    archive = target / f"{task.lower()}-development-v1.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 4, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, data)
    if archive.stat().st_size >= 100 * 1024 * 1024:
        raise ValueError(f"Archive exceeds GitHub file limit: {archive.name}")
    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None
        manifest = json.loads(package.read("manifest.json"))
        assert all(sha256(package.read(name)) == digest for name, digest in manifest["files"].items())
    digest = sha256(archive.read_bytes())
    archive.with_suffix(".zip.sha256").write_bytes(f"{digest}  {archive.name}\n".encode())
    return {"task_id": task, "archive": archive.name, "sha256": digest,
            "size_bytes": archive.stat().st_size, "file_count": len(files),
            "duration_seconds": metadata["duration_seconds"],
            "source_sha256": sample["source_sha256"], "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("samples/development"))
    parser.add_argument("--case", action="append", required=True, help="TASK or TASK=editorial-id")
    args = parser.parse_args()
    cases = []
    for spec in args.case:
        task, _, editorial = spec.partition("=")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task):
            raise ValueError("Invalid task ID")
        if editorial and not re.fullmatch(r"editorial-[0-9a-f]+", editorial):
            raise ValueError("Invalid editorial ID")
        result = build(args.project_root.resolve(), args.output_dir, task, editorial or None)
        cases.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    (args.output_dir / "index.json").write_bytes(json_bytes({"schema_version": "1.0", "samples": cases}))


if __name__ == "__main__":
    main()
