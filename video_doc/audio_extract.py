"""使用FFmpeg为单个视频任务提取ASR音频。"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from video_doc.task_workspace import (
    TaskWorkspaceError,
    load_task_manifest,
    validate_task_id,
)
from video_doc.video_probe import VideoProbeError, probe_video


AUDIO_SCHEMA_VERSION = "1.0"
TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1
TARGET_CODEC = "pcm_s16le"


class AudioExtractionError(RuntimeError):
    """音频提取、校验或缓存读取失败。"""


@dataclass(frozen=True)
class AudioMetadata:
    """FFprobe返回的音频属性。"""

    duration_seconds: float
    sample_rate: int
    channels: int
    codec: str


@dataclass(frozen=True)
class AudioExtractionResult:
    """一次音频提取或缓存命中的结果。"""

    output_path: Path
    manifest_path: Path
    metadata: AudioMetadata
    cache_hit: bool


def _source_fingerprint(source: Path) -> dict[str, Any]:
    try:
        stat = source.stat()
    except OSError as exc:
        raise AudioExtractionError(f"无法读取源视频信息：{source}") from exc
    return {
        "path": str(source.resolve()),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _target_parameters() -> dict[str, Any]:
    return {
        "sample_rate": TARGET_SAMPLE_RATE,
        "channels": TARGET_CHANNELS,
        "codec": TARGET_CODEC,
    }


def _remove_temporary_files(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _append_audio_log(
    task_root: Path,
    task_id: str,
    result: AudioExtractionResult,
) -> None:
    log_path = task_root / "logs" / "audio_extract.jsonl"
    event = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "event": "cache_hit" if result.cache_hit else "generated",
        "task_id": task_id,
        "audio_path": str(result.output_path.resolve()),
        **asdict(result.metadata),
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise AudioExtractionError(f"无法写入音频提取日志：{log_path}") from exc


def _probe_audio(audio_path: Path, ffprobe_path: str) -> AudioMetadata:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,duration:format=duration",
        "-of",
        "json",
        str(audio_path),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "未知错误").strip()
        raise AudioExtractionError(f"FFprobe无法校验音频：{details}") from exc

    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        stream = streams[0]
        duration_raw = stream.get("duration") or payload.get("format", {}).get(
            "duration"
        )
        metadata = AudioMetadata(
            duration_seconds=round(float(duration_raw), 3),
            sample_rate=int(stream["sample_rate"]),
            channels=int(stream["channels"]),
            codec=str(stream["codec_name"]),
        )
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AudioExtractionError("FFprobe未返回有效的音频属性。") from exc

    if metadata.duration_seconds <= 0:
        raise AudioExtractionError("输出音频时长无效。")
    if metadata.sample_rate != TARGET_SAMPLE_RATE:
        raise AudioExtractionError(
            f"输出音频采样率为{metadata.sample_rate} Hz，期望{TARGET_SAMPLE_RATE} Hz。"
        )
    if metadata.channels != TARGET_CHANNELS:
        raise AudioExtractionError(
            f"输出音频声道数为{metadata.channels}，期望{TARGET_CHANNELS}。"
        )
    if metadata.codec != TARGET_CODEC:
        raise AudioExtractionError(
            f"输出音频编码为{metadata.codec}，期望{TARGET_CODEC}。"
        )
    return metadata


def _load_cached_result(
    audio_path: Path,
    audio_manifest_path: Path,
    *,
    source: dict[str, Any],
    parameters: dict[str, Any],
    ffprobe_path: str,
) -> AudioExtractionResult | None:
    if not audio_path.is_file() or not audio_manifest_path.is_file():
        return None
    try:
        payload = json.loads(audio_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != AUDIO_SCHEMA_VERSION:
        return None
    if payload.get("source") != source or payload.get("parameters") != parameters:
        return None
    try:
        metadata = _probe_audio(audio_path, ffprobe_path)
    except AudioExtractionError:
        return None
    return AudioExtractionResult(
        output_path=audio_path,
        manifest_path=audio_manifest_path,
        metadata=metadata,
        cache_hit=True,
    )


def extract_task_audio(
    output_root: str | Path,
    task_id: str,
) -> AudioExtractionResult:
    """提取任务音频；缓存有效时不重复调用FFmpeg。"""

    validate_task_id(task_id)
    task_root = Path(output_root).expanduser() / task_id
    task_manifest_path = task_root / "task.json"
    try:
        task_manifest = load_task_manifest(
            task_manifest_path,
            expected_task_id=task_id,
        )
    except TaskWorkspaceError as exc:
        raise AudioExtractionError(str(exc)) from exc

    source_raw = task_manifest["source_video"]
    if not source_raw:
        raise AudioExtractionError("task.json未记录源视频路径。")
    source = Path(source_raw).expanduser()
    if not source.is_file():
        raise AudioExtractionError(f"源视频不存在：{source}")

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path is None:
        raise AudioExtractionError("未找到FFmpeg，请确认ffmpeg已加入PATH。")
    if ffprobe_path is None:
        raise AudioExtractionError("未找到FFprobe，请确认ffprobe已加入PATH。")

    audio_directory = task_root / "audio"
    audio_path = audio_directory / "audio.wav"
    audio_manifest_path = audio_directory / "audio.json"
    temporary_audio_path = audio_directory / "audio.tmp.wav"
    temporary_manifest_path = audio_directory / "audio.json.tmp"
    source_fingerprint = _source_fingerprint(source)
    parameters = _target_parameters()

    cached_result = _load_cached_result(
        audio_path,
        audio_manifest_path,
        source=source_fingerprint,
        parameters=parameters,
        ffprobe_path=ffprobe_path,
    )
    if cached_result is not None:
        _append_audio_log(task_root, task_id, cached_result)
        return cached_result

    try:
        source_metadata = probe_video(source)
    except VideoProbeError as exc:
        raise AudioExtractionError(str(exc)) from exc
    if not source_metadata.has_audio:
        raise AudioExtractionError("源视频没有音轨，无法提取ASR音频。")

    try:
        audio_directory.mkdir(parents=True, exist_ok=True)
        _remove_temporary_files(temporary_audio_path, temporary_manifest_path)
    except OSError as exc:
        raise AudioExtractionError(f"无法准备音频目录：{audio_directory}") from exc

    command = [
        ffmpeg_path,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-c:a",
        TARGET_CODEC,
        str(temporary_audio_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        metadata = _probe_audio(temporary_audio_path, ffprobe_path)
        manifest = {
            "schema_version": AUDIO_SCHEMA_VERSION,
            "source": source_fingerprint,
            "parameters": parameters,
            "output": {
                "path": audio_path.name,
                **asdict(metadata),
            },
        }
        temporary_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_audio_path.replace(audio_path)
        temporary_manifest_path.replace(audio_manifest_path)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "未知错误").strip()
        raise AudioExtractionError(f"FFmpeg提取音频失败：{details}") from exc
    except OSError as exc:
        raise AudioExtractionError(f"无法保存音频产物：{audio_path}") from exc
    finally:
        _remove_temporary_files(temporary_audio_path, temporary_manifest_path)

    extraction_result = AudioExtractionResult(
        output_path=audio_path,
        manifest_path=audio_manifest_path,
        metadata=metadata,
        cache_hit=False,
    )
    _append_audio_log(task_root, task_id, extraction_result)
    return extraction_result
