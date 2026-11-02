import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_doc.audio_extract as audio_extract
from video_doc.audio_extract import AudioExtractionError, extract_task_audio
from video_doc.task_workspace import initialize_task_workspace


def _prepare_task(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    initialize_task_workspace(output_root, "DEV01", source_video=source)
    return output_root, source


def _install_successful_media_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    ffmpeg_commands: list[list[str]] = []
    monkeypatch.setattr(audio_extract.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        audio_extract,
        "probe_video",
        lambda _: SimpleNamespace(has_audio=True),
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "ffmpeg":
            ffmpeg_commands.append(command)
            Path(command[-1]).write_bytes(b"fake-wav")
            return subprocess.CompletedProcess(command, 0, "", "")
        payload = {
            "streams": [
                {
                    "codec_name": "pcm_s16le",
                    "sample_rate": "16000",
                    "channels": 1,
                }
            ],
            "format": {"duration": "12.5"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(audio_extract.subprocess, "run", fake_run)
    return ffmpeg_commands


def test_extract_task_audio_creates_valid_wav_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _ = _prepare_task(tmp_path)
    ffmpeg_commands = _install_successful_media_mocks(monkeypatch)

    result = extract_task_audio(output_root, "DEV01")

    assert result.cache_hit is False
    assert result.output_path.read_bytes() == b"fake-wav"
    assert result.metadata.sample_rate == 16_000
    assert result.metadata.channels == 1
    assert result.metadata.codec == "pcm_s16le"
    assert len(ffmpeg_commands) == 1
    command = ffmpeg_commands[0]
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0"
    assert manifest["output"]["path"] == "audio.wav"
    log_lines = (output_root / "DEV01" / "logs" / "audio_extract.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert json.loads(log_lines[0])["event"] == "generated"


def test_extract_task_audio_uses_cache_without_running_ffmpeg_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _ = _prepare_task(tmp_path)
    ffmpeg_commands = _install_successful_media_mocks(monkeypatch)
    first = extract_task_audio(output_root, "DEV01")
    original_audio = first.output_path.read_bytes()

    second = extract_task_audio(output_root, "DEV01")

    assert second.cache_hit is True
    assert len(ffmpeg_commands) == 1
    assert second.output_path.read_bytes() == original_audio
    log_lines = (output_root / "DEV01" / "logs" / "audio_extract.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert [json.loads(line)["event"] for line in log_lines] == [
        "generated",
        "cache_hit",
    ]


def test_extract_task_audio_invalidates_cache_when_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, source = _prepare_task(tmp_path)
    ffmpeg_commands = _install_successful_media_mocks(monkeypatch)
    extract_task_audio(output_root, "DEV01")
    source.write_bytes(b"changed-video-content")

    result = extract_task_audio(output_root, "DEV01")

    assert result.cache_hit is False
    assert len(ffmpeg_commands) == 2


def test_extract_task_audio_rejects_video_without_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _ = _prepare_task(tmp_path)
    monkeypatch.setattr(audio_extract.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        audio_extract,
        "probe_video",
        lambda _: SimpleNamespace(has_audio=False),
    )

    with pytest.raises(AudioExtractionError, match="源视频没有音轨"):
        extract_task_audio(output_root, "DEV01")


def test_extract_task_audio_reports_missing_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _ = _prepare_task(tmp_path)
    monkeypatch.setattr(
        audio_extract.shutil,
        "which",
        lambda name: None if name == "ffmpeg" else name,
    )

    with pytest.raises(AudioExtractionError, match="未找到FFmpeg"):
        extract_task_audio(output_root, "DEV01")


def test_extract_task_audio_removes_temporary_file_after_ffmpeg_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _ = _prepare_task(tmp_path)
    monkeypatch.setattr(audio_extract.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        audio_extract,
        "probe_video",
        lambda _: SimpleNamespace(has_audio=True),
    )

    def fail_ffmpeg(command: list[str], **kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, command, stderr="decode failed")

    monkeypatch.setattr(audio_extract.subprocess, "run", fail_ffmpeg)

    with pytest.raises(AudioExtractionError, match="FFmpeg提取音频失败"):
        extract_task_audio(output_root, "DEV01")

    audio_directory = output_root / "DEV01" / "audio"
    assert not (audio_directory / "audio.wav").exists()
    assert not (audio_directory / "audio.tmp.wav").exists()


def test_extract_task_audio_rejects_invalid_output_properties(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _ = _prepare_task(tmp_path)
    monkeypatch.setattr(audio_extract.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        audio_extract,
        "probe_video",
        lambda _: SimpleNamespace(has_audio=True),
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"fake-wav")
            return subprocess.CompletedProcess(command, 0, "", "")
        payload = {
            "streams": [
                {
                    "codec_name": "pcm_s16le",
                    "sample_rate": "48000",
                    "channels": 1,
                }
            ],
            "format": {"duration": "12.5"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(audio_extract.subprocess, "run", fake_run)

    with pytest.raises(AudioExtractionError, match="采样率为48000"):
        extract_task_audio(output_root, "DEV01")

    audio_directory = output_root / "DEV01" / "audio"
    assert not (audio_directory / "audio.wav").exists()
    assert not (audio_directory / "audio.tmp.wav").exists()
