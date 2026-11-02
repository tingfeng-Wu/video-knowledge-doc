import hashlib
import json
from pathlib import Path

import pytest

import video_doc.cli as cli
from video_doc.asr_timeline import (
    ASRTimelineError,
    ASRTimelineResult,
    build_task_timeline,
    load_task_timeline,
    query_timeline_events,
    render_timeline_text,
)
from video_doc.task_workspace import initialize_task_workspace


def _prepare_task(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01", source_video=source)
    transcript = {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "cache_key": "a" * 64,
        "engine": {"name": "faster-whisper"},
        "source_audio": {
            "path": "audio.wav",
            "size_bytes": 100,
            "mtime_ns": 123,
            "duration_seconds": 20.0,
        },
        "language": {"code": "zh", "probability": 0.98},
        "segments": [
            {"id": 1, "start": 2.0, "end": 4.0, "text": "第一段"},
            {"id": 2, "start": 4.0, "end": 6.0, "text": "第二段"},
            {"id": 3, "start": 8.5, "end": 11.0, "text": "第三段"},
        ],
    }
    (workspace.root / "asr" / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_build_task_timeline_writes_stable_events_and_text(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)

    result = build_task_timeline(output_root, "DEV01")

    assert result.event_count == 3
    assert result.duration_seconds == 20.0
    assert result.cache_hit is False
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["source_asr"]["cache_key"] == "a" * 64
    assert len(payload["source_asr"]["sha256"]) == 64
    assert payload["events"] == [
        {
            "id": "a0001",
            "type": "asr",
            "source_segment_id": 1,
            "start": 2.0,
            "end": 4.0,
            "pause_before_seconds": 2.0,
            "text": "第一段",
        },
        {
            "id": "a0002",
            "type": "asr",
            "source_segment_id": 2,
            "start": 4.0,
            "end": 6.0,
            "pause_before_seconds": 0.0,
            "text": "第二段",
        },
        {
            "id": "a0003",
            "type": "asr",
            "source_segment_id": 3,
            "start": 8.5,
            "end": 11.0,
            "pause_before_seconds": 2.5,
            "text": "第三段",
        },
    ]
    assert result.text_path.read_text(encoding="utf-8").splitlines() == [
        "[00:00:02.000 - 00:00:04.000] [pause_before=2.000s] 第一段",
        "[00:00:04.000 - 00:00:06.000] 第二段",
        "[00:00:08.500 - 00:00:11.000] [pause_before=2.500s] 第三段",
    ]


def test_build_task_timeline_uses_cache_without_rewriting(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = build_task_timeline(output_root, "DEV01")
    json_hash = _sha256(first.json_path)
    text_hash = _sha256(first.text_path)
    json_mtime = first.json_path.stat().st_mtime_ns
    text_mtime = first.text_path.stat().st_mtime_ns

    second = build_task_timeline(output_root, "DEV01")

    assert second.cache_hit is True
    assert _sha256(second.json_path) == json_hash
    assert _sha256(second.text_path) == text_hash
    assert second.json_path.stat().st_mtime_ns == json_mtime
    assert second.text_path.stat().st_mtime_ns == text_mtime


def test_build_task_timeline_invalidates_cache_when_asr_changes(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    first = build_task_timeline(output_root, "DEV01")
    original_hash = _sha256(first.json_path)
    payload = json.loads(
        (output_root / "DEV01" / "asr" / "transcript.json").read_text(
            encoding="utf-8"
        )
    )
    payload["segments"][0]["text"] = "修改后的第一段"
    (output_root / "DEV01" / "asr" / "transcript.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    second = build_task_timeline(output_root, "DEV01")

    assert second.cache_hit is False
    assert _sha256(second.json_path) != original_hash
    timeline = load_task_timeline(output_root, "DEV01")
    assert timeline["events"][0]["text"] == "修改后的第一段"


@pytest.mark.parametrize("damaged_name", ["timeline.json", "timeline.txt"])
def test_build_task_timeline_replaces_damaged_cache(
    tmp_path: Path,
    damaged_name: str,
) -> None:
    output_root = _prepare_task(tmp_path)
    first = build_task_timeline(output_root, "DEV01")
    (first.json_path.parent / damaged_name).write_bytes(b"\xffbroken-cache")

    second = build_task_timeline(output_root, "DEV01")

    assert second.cache_hit is False
    assert load_task_timeline(output_root, "DEV01")["events"]
    assert second.text_path.read_text(encoding="utf-8").endswith("第三段\n")


def test_build_task_timeline_clamps_negative_pause_for_overlapping_segments(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    transcript_path = output_root / "DEV01" / "asr" / "transcript.json"
    payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    payload["segments"][1] = {
        "id": 2,
        "start": 3.5,
        "end": 6.0,
        "text": "重叠片段",
    }
    transcript_path.write_text(json.dumps(payload), encoding="utf-8")

    result = build_task_timeline(output_root, "DEV01")
    timeline = json.loads(result.json_path.read_text(encoding="utf-8"))

    assert timeline["events"][1]["pause_before_seconds"] == 0.0


def test_query_timeline_events_uses_half_open_overlap_rules(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    build_task_timeline(output_root, "DEV01")
    timeline = load_task_timeline(output_root, "DEV01")

    assert [event["id"] for event in query_timeline_events(timeline, 2.5, 3.0)] == [
        "a0001"
    ]
    assert [event["id"] for event in query_timeline_events(timeline, 4.0, 8.5)] == [
        "a0002"
    ]
    assert [event["id"] for event in query_timeline_events(timeline, 3.0, 9.0)] == [
        "a0001",
        "a0002",
        "a0003",
    ]
    assert query_timeline_events(timeline, 6.0, 8.5) == []


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1.0, 2.0), (2.0, 2.0), (3.0, 2.0), (0.0, 20.1)],
)
def test_query_timeline_events_rejects_invalid_ranges(
    tmp_path: Path,
    start: float,
    end: float,
) -> None:
    output_root = _prepare_task(tmp_path)
    build_task_timeline(output_root, "DEV01")
    timeline = load_task_timeline(output_root, "DEV01")

    with pytest.raises(ASRTimelineError, match="查询范围必须满足"):
        query_timeline_events(timeline, start, end)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(task_id="OTHER"), "任务ID"),
        (lambda payload: payload.update(schema_version="0.9"), "Schema版本"),
        (lambda payload: payload["segments"][0].update(id=2), "片段ID"),
        (lambda payload: payload["segments"][0].update(end=21.0), "时间范围无效"),
    ],
)
def test_build_task_timeline_rejects_invalid_asr(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    output_root = _prepare_task(tmp_path)
    transcript_path = output_root / "DEV01" / "asr" / "transcript.json"
    payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    mutate(payload)
    transcript_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ASRTimelineError, match=message):
        build_task_timeline(output_root, "DEV01")


def test_build_task_timeline_preserves_existing_outputs_on_input_failure(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    first = build_task_timeline(output_root, "DEV01")
    original_json = first.json_path.read_bytes()
    original_text = first.text_path.read_bytes()
    (output_root / "DEV01" / "asr" / "transcript.json").write_text(
        "{broken-json",
        encoding="utf-8",
    )

    with pytest.raises(ASRTimelineError, match="不是有效UTF-8 JSON"):
        build_task_timeline(output_root, "DEV01")

    assert first.json_path.read_bytes() == original_json
    assert first.text_path.read_bytes() == original_text


def test_build_timeline_cli_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "build_task_timeline",
        lambda *_: ASRTimelineResult(
            json_path=tmp_path / "timeline.json",
            text_path=tmp_path / "timeline.txt",
            event_count=3,
            duration_seconds=20.0,
            cache_hit=False,
        ),
    )
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["video-doc", "build-timeline", "DEV01", "--output-root", str(tmp_path)],
    )

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"event_count": 3' in output
    assert '"cache_hit": false' in output
    assert "ASR时间轴生成完成" in output


def test_query_timeline_cli_supports_text_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    build_task_timeline(output_root, "DEV01")
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "video-doc",
            "query-timeline",
            "DEV01",
            "--output-root",
            str(output_root),
            "--start",
            "3",
            "--end",
            "5",
            "--format",
            "json",
        ],
    )

    assert cli.main() == 0
    json_output = json.loads(capsys.readouterr().out)
    assert json_output["event_count"] == 2
    assert [event["id"] for event in json_output["events"]] == ["a0001", "a0002"]

    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "video-doc",
            "query-timeline",
            "DEV01",
            "--output-root",
            str(output_root),
            "--start",
            "6",
            "--end",
            "8",
        ],
    )
    assert cli.main() == 0
    assert capsys.readouterr().out == ""


def test_render_timeline_text_supports_empty_query() -> None:
    assert render_timeline_text([]) == ""
