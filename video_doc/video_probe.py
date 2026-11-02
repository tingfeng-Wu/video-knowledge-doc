"""使用 FFprobe 读取视频的基础信息。"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SUPPORTED_VIDEO_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".m4v",
}


class VideoProbeError(RuntimeError):
    """视频信息读取失败。"""


@dataclass(frozen=True)
class VideoMetadata:
    """后续处理流程共用的视频基础信息。"""

    source_path: str
    file_name: str
    file_size_bytes: int
    duration_seconds: float
    width: int | None
    height: int | None
    frame_rate: float | None
    video_codec: str | None
    has_audio: bool
    audio_codec: str | None
    audio_sample_rate: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_fraction(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = value.split("/", maxsplit=1)
        denominator_value = float(denominator)
        if denominator_value == 0:
            return None
        return float(numerator) / denominator_value
    except (TypeError, ValueError):
        return None


def parse_ffprobe_payload(payload: dict[str, Any], source: Path) -> VideoMetadata:
    """把 FFprobe JSON 转换为项目内部统一结构。"""

    streams = payload.get("streams", [])
    format_info = payload.get("format", {})
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )

    if video_stream is None:
        raise VideoProbeError("文件中没有检测到视频流。")

    duration_raw = format_info.get("duration") or video_stream.get("duration")
    try:
        duration_seconds = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise VideoProbeError("FFprobe 未返回有效的视频时长。") from exc

    sample_rate_raw = audio_stream.get("sample_rate") if audio_stream else None
    try:
        sample_rate = int(sample_rate_raw) if sample_rate_raw else None
    except (TypeError, ValueError):
        sample_rate = None

    return VideoMetadata(
        source_path=str(source.resolve()),
        file_name=source.name,
        file_size_bytes=source.stat().st_size,
        duration_seconds=round(duration_seconds, 3),
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        frame_rate=_parse_fraction(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        ),
        video_codec=video_stream.get("codec_name"),
        has_audio=audio_stream is not None,
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
        audio_sample_rate=sample_rate,
    )


def probe_video(video_path: str | Path) -> VideoMetadata:
    """调用 FFprobe 读取视频元数据。"""

    source = Path(video_path).expanduser()
    if not source.is_file():
        raise VideoProbeError(f"视频文件不存在：{source}")
    if source.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
        supported = "、".join(sorted(SUPPORTED_VIDEO_SUFFIXES))
        raise VideoProbeError(f"暂不支持 {source.suffix or '无后缀'} 文件，支持：{supported}")

    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path is None:
        raise VideoProbeError(
            "未找到 FFprobe。请先安装 FFmpeg，并确认 ffprobe 命令已经加入 PATH。"
        )

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(source),
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
        raise VideoProbeError(f"FFprobe 无法读取该视频：{details}") from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VideoProbeError("FFprobe 返回了无法解析的结果。") from exc

    return parse_ffprobe_payload(payload, source)


def save_metadata(metadata: VideoMetadata, output_path: str | Path) -> Path:
    """把视频元数据保存为 UTF-8 JSON。"""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination

