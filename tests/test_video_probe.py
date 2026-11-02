from pathlib import Path
import subprocess

import pytest

from video_doc.video_probe import VideoProbeError, parse_ffprobe_payload, probe_video


def test_parse_ffprobe_payload(tmp_path: Path) -> None:
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"fake-video")
    payload = {
        "format": {"duration": "123.456"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
            },
        ],
    }

    metadata = parse_ffprobe_payload(payload, source)

    assert metadata.file_name == "sample.mp4"
    assert metadata.duration_seconds == 123.456
    assert metadata.width == 1920
    assert metadata.height == 1080
    assert round(metadata.frame_rate or 0, 3) == 29.97
    assert metadata.video_codec == "h264"
    assert metadata.has_audio is True
    assert metadata.audio_codec == "aac"
    assert metadata.audio_sample_rate == 48000


def test_parse_ffprobe_payload_rejects_payload_without_video_stream(
    tmp_path: Path,
) -> None:
    source = tmp_path / "audio-only.mp4"
    source.write_bytes(b"fake-audio")
    payload = {
        "format": {"duration": "10.0"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
            }
        ],
    }

    with pytest.raises(VideoProbeError, match="没有检测到视频流"):
        parse_ffprobe_payload(payload, source)


def test_parse_ffprobe_payload_accepts_video_without_audio(tmp_path: Path) -> None:
    source = tmp_path / "silent.mp4"
    source.write_bytes(b"fake-silent-video")
    payload = {
        "format": {"duration": "30.0"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "25/1",
            }
        ],
    }

    metadata = parse_ffprobe_payload(payload, source)

    assert metadata.has_audio is False
    assert metadata.audio_codec is None
    assert metadata.audio_sample_rate is None


def test_probe_video_rejects_missing_path(tmp_path: Path) -> None:
    missing_video = tmp_path / "missing.mp4"

    with pytest.raises(VideoProbeError, match="视频文件不存在"):
        probe_video(missing_video)


def test_probe_video_reports_missing_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"fake-video")
    monkeypatch.setattr("video_doc.video_probe.shutil.which", lambda _: None)

    with pytest.raises(VideoProbeError, match="未找到 FFprobe"):
        probe_video(source)


def test_probe_video_reports_ffprobe_failure_for_corrupt_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "corrupt.mp4"
    source.write_bytes(b"not-a-real-video")
    monkeypatch.setattr(
        "video_doc.video_probe.shutil.which",
        lambda _: "ffprobe",
    )

    def fail_ffprobe(*args: object, **kwargs: object) -> None:
        command = args[0] if args else "ffprobe"
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            stderr="Invalid data found when processing input",
        )

    monkeypatch.setattr("video_doc.video_probe.subprocess.run", fail_ffprobe)

    with pytest.raises(
        VideoProbeError,
        match="FFprobe 无法读取该视频：Invalid data found",
    ):
        probe_video(source)
