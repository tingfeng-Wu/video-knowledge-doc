"""将ASR转写转换为稳定、可检索的文本时间轴。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_doc.asr_transcribe import ASR_SCHEMA_VERSION
from video_doc.task_workspace import (
    TaskWorkspaceError,
    load_task_manifest,
    validate_task_id,
)


TIMELINE_SCHEMA_VERSION = "1.0"


class ASRTimelineError(RuntimeError):
    """时间轴输入、生成、查询或输出校验失败。"""


@dataclass(frozen=True)
class ASRTimelineResult:
    """一次ASR时间轴生成的关键结果。"""

    json_path: Path
    text_path: Path
    event_count: int
    duration_seconds: float
    cache_hit: bool


def _remove_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ASRTimelineError(f"{description}不存在：{path}") from exc
    except OSError as exc:
        raise ASRTimelineError(f"无法读取{description}：{path}") from exc
    except UnicodeError as exc:
        raise ASRTimelineError(f"{description}不是有效UTF-8文本：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ASRTimelineError(f"{description}不是有效JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ASRTimelineError(f"{description}必须是JSON对象：{path}")
    return payload


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ASRTimelineError(f"{field}必须是有效数字。") from exc
    if not math.isfinite(number):
        raise ASRTimelineError(f"{field}必须是有限数字。")
    return number


def _validate_language(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ASRTimelineError("ASR语言信息必须是JSON对象。")
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ASRTimelineError("ASR语言代码无效。")
    probability = _finite_number(payload.get("probability"), "ASR语言置信度")
    if not 0 <= probability <= 1:
        raise ASRTimelineError("ASR语言置信度不在0到1之间。")
    return {"code": code.strip(), "probability": round(probability, 6)}


def _build_events(raw_segments: Any, duration: float) -> list[dict[str, Any]]:
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ASRTimelineError("ASR结果必须包含至少一个文本片段。")

    events: list[dict[str, Any]] = []
    previous_start = -1.0
    previous_end = 0.0
    for index, segment in enumerate(raw_segments, start=1):
        if not isinstance(segment, dict):
            raise ASRTimelineError(f"ASR片段{index}必须是JSON对象。")
        if segment.get("id") != index:
            raise ASRTimelineError("ASR片段ID必须从1开始连续递增。")
        start = _finite_number(segment.get("start"), f"ASR片段{index}起点")
        end = _finite_number(segment.get("end"), f"ASR片段{index}终点")
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ASRTimelineError(f"ASR片段{index}文本不能为空。")
        if start < 0 or end <= start or end > duration:
            raise ASRTimelineError(
                f"ASR片段{index}时间范围无效：{start:.3f}—{end:.3f}秒。"
            )
        if start < previous_start or end < previous_end:
            raise ASRTimelineError("ASR片段时间戳未按顺序递增。")

        events.append(
            {
                "id": f"a{index:04d}",
                "type": "asr",
                "source_segment_id": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "pause_before_seconds": round(max(0.0, start - previous_end), 3),
                "text": text.strip(),
            }
        )
        previous_start = start
        previous_end = end
    return events


def _load_asr_source(
    task_root: Path,
    task_id: str,
) -> tuple[dict[str, Any], str]:
    transcript_path = task_root / "asr" / "transcript.json"
    try:
        transcript_bytes = transcript_path.read_bytes()
    except FileNotFoundError as exc:
        raise ASRTimelineError(f"ASR结果不存在：{transcript_path}") from exc
    except OSError as exc:
        raise ASRTimelineError(f"无法读取ASR结果：{transcript_path}") from exc
    try:
        payload = json.loads(transcript_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ASRTimelineError(f"ASR结果不是有效UTF-8 JSON：{transcript_path}") from exc
    if not isinstance(payload, dict):
        raise ASRTimelineError("ASR结果必须是JSON对象。")
    if payload.get("schema_version") != ASR_SCHEMA_VERSION:
        raise ASRTimelineError(
            "不支持的ASR Schema版本："
            f"{payload.get('schema_version')!r}，当前版本为{ASR_SCHEMA_VERSION}。"
        )
    if payload.get("task_id") != task_id:
        raise ASRTimelineError("ASR结果中的任务ID与当前任务不一致。")
    cache_key = payload.get("cache_key")
    if not isinstance(cache_key, str) or len(cache_key) != 64:
        raise ASRTimelineError("ASR结果缺少有效缓存键。")
    return payload, hashlib.sha256(transcript_bytes).hexdigest()


def _build_timeline_payload(
    task_id: str,
    transcript: dict[str, Any],
    transcript_sha256: str,
) -> dict[str, Any]:
    source_audio = transcript.get("source_audio")
    if not isinstance(source_audio, dict):
        raise ASRTimelineError("ASR结果缺少音频来源信息。")
    duration = _finite_number(
        source_audio.get("duration_seconds"),
        "ASR音频时长",
    )
    if duration <= 0:
        raise ASRTimelineError("ASR音频时长必须大于0。")
    duration = round(duration, 3)

    return {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "task_id": task_id,
        "duration_seconds": duration,
        "language": _validate_language(transcript.get("language")),
        "source_asr": {
            "schema_version": transcript["schema_version"],
            "cache_key": transcript["cache_key"],
            "sha256": transcript_sha256,
        },
        "events": _build_events(transcript.get("segments"), duration),
    }


def validate_timeline_payload(
    payload: Any,
    *,
    expected_task_id: str | None = None,
) -> None:
    """校验已生成的ASR-only时间轴。"""

    if not isinstance(payload, dict):
        raise ASRTimelineError("时间轴必须是JSON对象。")
    if payload.get("schema_version") != TIMELINE_SCHEMA_VERSION:
        raise ASRTimelineError(
            "不支持的时间轴Schema版本："
            f"{payload.get('schema_version')!r}，当前版本为{TIMELINE_SCHEMA_VERSION}。"
        )
    task_id = payload.get("task_id")
    if not isinstance(task_id, str):
        raise ASRTimelineError("时间轴任务ID无效。")
    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise ASRTimelineError(str(exc)) from exc
    if expected_task_id is not None and task_id != expected_task_id:
        raise ASRTimelineError("时间轴中的任务ID与当前任务不一致。")

    duration = _finite_number(payload.get("duration_seconds"), "时间轴时长")
    if duration <= 0:
        raise ASRTimelineError("时间轴时长必须大于0。")
    _validate_language(payload.get("language"))
    source_asr = payload.get("source_asr")
    if not isinstance(source_asr, dict):
        raise ASRTimelineError("时间轴缺少ASR来源信息。")
    if source_asr.get("schema_version") != ASR_SCHEMA_VERSION:
        raise ASRTimelineError("时间轴引用的ASR Schema版本无效。")
    if not isinstance(source_asr.get("cache_key"), str):
        raise ASRTimelineError("时间轴引用的ASR缓存键无效。")
    sha256 = source_asr.get("sha256")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ASRTimelineError("时间轴引用的ASR哈希无效。")

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise ASRTimelineError("时间轴必须包含至少一个事件。")
    previous_start = -1.0
    previous_end = -1.0
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise ASRTimelineError(f"时间轴事件{index}必须是JSON对象。")
        if event.get("id") != f"a{index:04d}":
            raise ASRTimelineError("时间轴事件ID不连续。")
        if event.get("type") != "asr" or event.get("source_segment_id") != index:
            raise ASRTimelineError("时间轴事件来源信息无效。")
        start = _finite_number(event.get("start"), f"时间轴事件{index}起点")
        end = _finite_number(event.get("end"), f"时间轴事件{index}终点")
        pause = _finite_number(
            event.get("pause_before_seconds"),
            f"时间轴事件{index}停顿",
        )
        text = event.get("text")
        if start < 0 or end <= start or end > duration:
            raise ASRTimelineError(f"时间轴事件{index}时间范围无效。")
        if start < previous_start or end < previous_end:
            raise ASRTimelineError("时间轴事件未按顺序递增。")
        if pause < 0 or not isinstance(text, str) or not text.strip():
            raise ASRTimelineError(f"时间轴事件{index}内容无效。")
        expected_pause = round(max(0.0, start - (0.0 if index == 1 else previous_end)), 3)
        if round(pause, 3) != expected_pause:
            raise ASRTimelineError(f"时间轴事件{index}停顿信息不一致。")
        previous_start = start
        previous_end = end


def _format_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def render_timeline_text(events: list[dict[str, Any]]) -> str:
    """将时间轴事件导出为适合人工和LLM读取的稳定文本。"""

    lines: list[str] = []
    for event in events:
        pause = float(event["pause_before_seconds"])
        pause_text = f" [pause_before={pause:.3f}s]" if pause > 0 else ""
        lines.append(
            f"[{_format_timestamp(float(event['start']))} - "
            f"{_format_timestamp(float(event['end']))}]"
            f"{pause_text} {event['text']}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def _cached_timeline_matches(
    json_path: Path,
    text_path: Path,
    expected_payload: dict[str, Any],
    expected_text: str,
) -> bool:
    if not json_path.is_file() or not text_path.is_file():
        return False
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        validate_timeline_payload(
            payload,
            expected_task_id=expected_payload["task_id"],
        )
        text = text_path.read_text(encoding="utf-8")
    except (ASRTimelineError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == expected_payload and text == expected_text


def build_task_timeline(
    output_root: str | Path,
    task_id: str,
) -> ASRTimelineResult:
    """从任务ASR生成JSON和可读文本；有效缓存存在时不改写。"""

    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise ASRTimelineError(str(exc)) from exc
    task_root = Path(output_root).expanduser() / task_id
    try:
        load_task_manifest(task_root / "task.json", expected_task_id=task_id)
    except TaskWorkspaceError as exc:
        raise ASRTimelineError(str(exc)) from exc

    transcript, transcript_sha256 = _load_asr_source(task_root, task_id)
    payload = _build_timeline_payload(task_id, transcript, transcript_sha256)
    validate_timeline_payload(payload, expected_task_id=task_id)
    text = render_timeline_text(payload["events"])

    timeline_directory = task_root / "timeline"
    json_path = timeline_directory / "timeline.json"
    text_path = timeline_directory / "timeline.txt"
    if _cached_timeline_matches(json_path, text_path, payload, text):
        return ASRTimelineResult(
            json_path=json_path,
            text_path=text_path,
            event_count=len(payload["events"]),
            duration_seconds=payload["duration_seconds"],
            cache_hit=True,
        )

    json_temporary_path = timeline_directory / "timeline.json.tmp"
    text_temporary_path = timeline_directory / "timeline.txt.tmp"
    try:
        timeline_directory.mkdir(parents=True, exist_ok=True)
        _remove_temporary_file(json_temporary_path)
        _remove_temporary_file(text_temporary_path)
        json_temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        text_temporary_path.write_text(text, encoding="utf-8")
        text_temporary_path.replace(text_path)
        json_temporary_path.replace(json_path)
    except OSError as exc:
        raise ASRTimelineError(f"无法保存时间轴：{timeline_directory}") from exc
    finally:
        _remove_temporary_file(json_temporary_path)
        _remove_temporary_file(text_temporary_path)

    return ASRTimelineResult(
        json_path=json_path,
        text_path=text_path,
        event_count=len(payload["events"]),
        duration_seconds=payload["duration_seconds"],
        cache_hit=False,
    )


def load_task_timeline(
    output_root: str | Path,
    task_id: str,
) -> dict[str, Any]:
    """读取并校验一个任务的时间轴。"""

    try:
        validate_task_id(task_id)
    except TaskWorkspaceError as exc:
        raise ASRTimelineError(str(exc)) from exc
    path = Path(output_root).expanduser() / task_id / "timeline" / "timeline.json"
    payload = _read_json(path, "时间轴")
    validate_timeline_payload(payload, expected_task_id=task_id)
    return payload


def query_timeline_events(
    timeline: dict[str, Any],
    start: float,
    end: float,
) -> list[dict[str, Any]]:
    """返回与半开区间[start, end)相交的ASR事件。"""

    validate_timeline_payload(timeline)
    query_start = _finite_number(start, "查询起点")
    query_end = _finite_number(end, "查询终点")
    duration = float(timeline["duration_seconds"])
    if query_start < 0 or query_end <= query_start or query_end > duration:
        raise ASRTimelineError(
            f"查询范围必须满足0 <= start < end <= {duration:.3f}。"
        )
    return [
        dict(event)
        for event in timeline["events"]
        if float(event["end"]) > query_start and float(event["start"]) < query_end
    ]
