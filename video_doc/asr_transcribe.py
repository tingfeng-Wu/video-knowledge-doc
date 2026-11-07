"""使用faster-whisper为单个任务生成带时间戳的转写。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from video_doc.task_workspace import (
    TaskWorkspaceError,
    load_task_manifest,
    validate_task_id,
)


ASR_SCHEMA_VERSION = "1.0"
DEFAULT_MODEL = "small"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_BEAM_SIZE = 5
DEFAULT_VAD_FILTER = True
TIMESTAMP_TOLERANCE_SECONDS = 0.5


class ASRTranscriptionError(RuntimeError):
    """ASR输入、模型推理或输出校验失败。"""


@dataclass(frozen=True)
class ASRTranscriptionResult:
    """一次ASR转写的关键结果。"""

    output_path: Path
    language: str
    language_probability: float
    segment_count: int
    duration_seconds: float
    cache_hit: bool


ModelFactory = Callable[[str, str, str], Any]


def _remove_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _engine_version() -> str:
    try:
        import faster_whisper

        return faster_whisper.__version__
    except (ImportError, AttributeError):
        return "unknown"


def _engine_parameters(
    model: str,
    device: str,
    compute_type: str,
    beam_size: int,
) -> dict[str, Any]:
    return {
        "name": "faster-whisper",
        "version": _engine_version(),
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "beam_size": beam_size,
        "vad_filter": DEFAULT_VAD_FILTER,
    }


def _build_cache_key(source: dict[str, Any], engine: dict[str, Any]) -> str:
    cache_input = {
        "schema_version": ASR_SCHEMA_VERSION,
        "source_audio": source,
        "engine": engine,
    }
    encoded = json.dumps(
        cache_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _append_asr_log(
    task_root: Path,
    *,
    event: str,
    task_id: str,
    cache_key: str,
    engine: dict[str, Any],
    elapsed_seconds: float,
    output_path: Path,
    segment_count: int | None = None,
    error: str | None = None,
) -> None:
    log_path = task_root / "logs" / "asr_transcribe.jsonl"
    payload: dict[str, Any] = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "event": event,
        "task_id": task_id,
        "cache_key": cache_key,
        "engine": engine,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "output_path": str(output_path.resolve()),
    }
    if segment_count is not None:
        payload["segment_count"] = segment_count
    if error is not None:
        payload["error"] = error
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise ASRTranscriptionError(f"无法写入ASR日志：{log_path}") from exc


def _append_failure_log_safely(
    task_root: Path,
    *,
    task_id: str,
    cache_key: str,
    engine: dict[str, Any],
    started_at: float,
    output_path: Path,
    error: BaseException,
) -> None:
    try:
        _append_asr_log(
            task_root,
            event="failed",
            task_id=task_id,
            cache_key=cache_key,
            engine=engine,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            error=str(error),
        )
    except ASRTranscriptionError:
        pass


def _load_audio_input(task_root: Path) -> tuple[Path, float, dict[str, Any]]:
    audio_path = task_root / "audio" / "audio.wav"
    audio_manifest_path = task_root / "audio" / "audio.json"
    if not audio_path.is_file():
        raise ASRTranscriptionError(f"ASR音频不存在：{audio_path}")
    try:
        audio_manifest = json.loads(audio_manifest_path.read_text(encoding="utf-8"))
        duration = float(audio_manifest["output"]["duration_seconds"])
        stat = audio_path.stat()
    except FileNotFoundError as exc:
        raise ASRTranscriptionError(f"音频参数文件不存在：{audio_manifest_path}") from exc
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ASRTranscriptionError(f"音频参数文件无效：{audio_manifest_path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ASRTranscriptionError("音频参数中的时长无效。")
    source = {
        "path": str(audio_path.resolve()),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration_seconds": round(duration, 3),
    }
    return audio_path, duration, source


def _default_model_factory(model: str, device: str, compute_type: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ASRTranscriptionError(
            "未安装faster-whisper，请先安装项目运行依赖。"
        ) from exc
    try:
        local_model_path = os.environ.get("VIDEO_DOC_ASR_MODEL_PATH", "").strip()
        model_source = local_model_path or model
        return WhisperModel(model_source, device=device, compute_type=compute_type)
    except Exception as exc:
        raise ASRTranscriptionError(f"无法加载ASR模型{model!r}：{exc}") from exc


def _validated_language_values(
    language: Any,
    probability_raw: Any,
) -> tuple[str, float]:
    try:
        probability = float(probability_raw)
    except (TypeError, ValueError) as exc:
        raise ASRTranscriptionError("ASR未返回有效的语言置信度。") from exc
    if not isinstance(language, str) or not language.strip():
        raise ASRTranscriptionError("ASR未返回有效的语言代码。")
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ASRTranscriptionError("ASR返回的语言置信度不在0到1之间。")
    return language.strip(), round(probability, 6)


def _validated_language(info: Any) -> tuple[str, float]:
    return _validated_language_values(
        getattr(info, "language", None),
        getattr(info, "language_probability", None),
    )


def _segment_field(raw_segment: Any, field: str, default: Any = None) -> Any:
    if isinstance(raw_segment, dict):
        return raw_segment.get(field, default)
    return getattr(raw_segment, field, default)


def _validated_segments(raw_segments: Any, duration: float) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    previous_start = -1.0
    previous_end = -1.0
    try:
        for raw_segment in raw_segments:
            text = str(_segment_field(raw_segment, "text", "")).strip()
            if not text:
                continue
            start = float(_segment_field(raw_segment, "start"))
            end = float(_segment_field(raw_segment, "end"))
            if not math.isfinite(start) or not math.isfinite(end):
                raise ASRTranscriptionError("ASR片段包含非有限时间戳。")
            if start < 0 or end <= start:
                raise ASRTranscriptionError(
                    f"ASR片段时间范围无效：{start:.3f}—{end:.3f}秒。"
                )
            if start < previous_start or end < previous_end:
                raise ASRTranscriptionError("ASR片段时间戳未按顺序递增。")
            if end > duration + TIMESTAMP_TOLERANCE_SECONDS:
                raise ASRTranscriptionError(
                    f"ASR片段末尾{end:.3f}秒超过音频时长{duration:.3f}秒。"
                )
            start = min(start, duration)
            end = min(end, duration)
            if end <= start:
                raise ASRTranscriptionError("ASR片段吸附到音频末尾后时长无效。")
            segments.append(
                {
                    "id": len(segments) + 1,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": text,
                }
            )
            previous_start = start
            previous_end = end
    except ASRTranscriptionError:
        raise
    except Exception as exc:
        raise ASRTranscriptionError(f"无法读取ASR分段结果：{exc}") from exc
    if not segments:
        raise ASRTranscriptionError("ASR未生成任何非空文本片段。")
    return segments


def _load_cached_result(
    output_path: Path,
    *,
    task_id: str,
    duration: float,
    source: dict[str, Any],
    engine: dict[str, Any],
    cache_key: str,
) -> ASRTranscriptionResult | None:
    if not output_path.is_file():
        return None
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != ASR_SCHEMA_VERSION:
            return None
        if payload.get("task_id") != task_id:
            return None
        if payload.get("cache_key") != cache_key:
            return None
        if payload.get("source_audio") != source:
            return None
        if payload.get("engine") != engine:
            return None

        language_payload = payload.get("language")
        if not isinstance(language_payload, dict):
            return None
        language, language_probability = _validated_language_values(
            language_payload.get("code"),
            language_payload.get("probability"),
        )
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list):
            return None
        segments = _validated_segments(raw_segments, duration)
        if len(segments) != len(raw_segments):
            return None
        for expected_id, (raw_segment, normalized_segment) in enumerate(
            zip(raw_segments, segments, strict=True),
            start=1,
        ):
            if not isinstance(raw_segment, dict):
                return None
            if raw_segment.get("id") != expected_id:
                return None
            if raw_segment != normalized_segment:
                return None
    except (
        ASRTranscriptionError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None

    return ASRTranscriptionResult(
        output_path=output_path,
        language=language,
        language_probability=language_probability,
        segment_count=len(segments),
        duration_seconds=duration,
        cache_hit=True,
    )


def transcribe_task_audio(
    output_root: str | Path,
    task_id: str,
    *,
    model: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
    beam_size: int = DEFAULT_BEAM_SIZE,
    model_factory: ModelFactory | None = None,
) -> ASRTranscriptionResult:
    """转写任务音频；缓存有效时不加载模型，成功时原子写入JSON。"""

    validate_task_id(task_id)
    model = model.strip()
    device = device.strip()
    compute_type = compute_type.strip()
    if not model:
        raise ASRTranscriptionError("ASR模型名称不能为空。")
    if not device:
        raise ASRTranscriptionError("ASR设备名称不能为空。")
    if not compute_type:
        raise ASRTranscriptionError("ASR计算精度不能为空。")
    if beam_size < 1:
        raise ASRTranscriptionError("beam_size必须大于等于1。")

    task_root = Path(output_root).expanduser() / task_id
    try:
        load_task_manifest(task_root / "task.json", expected_task_id=task_id)
    except TaskWorkspaceError as exc:
        raise ASRTranscriptionError(str(exc)) from exc
    audio_path, duration, source = _load_audio_input(task_root)

    engine = _engine_parameters(model, device, compute_type, beam_size)
    cache_key = _build_cache_key(source, engine)
    asr_directory = task_root / "asr"
    output_path = asr_directory / "transcript.json"
    temporary_path = asr_directory / "transcript.json.tmp"
    started_at = time.perf_counter()

    cached_result = _load_cached_result(
        output_path,
        task_id=task_id,
        duration=duration,
        source=source,
        engine=engine,
        cache_key=cache_key,
    )
    if cached_result is not None:
        _append_asr_log(
            task_root,
            event="cache_hit",
            task_id=task_id,
            cache_key=cache_key,
            engine=engine,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            segment_count=cached_result.segment_count,
        )
        return cached_result

    try:
        _append_asr_log(
            task_root,
            event="started",
            task_id=task_id,
            cache_key=cache_key,
            engine=engine,
            elapsed_seconds=0.0,
            output_path=output_path,
        )
        factory = model_factory or _default_model_factory
        try:
            asr_model = factory(model, device, compute_type)
        except ASRTranscriptionError:
            raise
        except Exception as exc:
            raise ASRTranscriptionError(
                f"无法加载ASR模型{model!r}：{exc}"
            ) from exc
        try:
            raw_segments, info = asr_model.transcribe(
                str(audio_path),
                beam_size=beam_size,
                vad_filter=DEFAULT_VAD_FILTER,
            )
            language, language_probability = _validated_language(info)
            segments = _validated_segments(raw_segments, duration)
        except ASRTranscriptionError:
            raise
        except Exception as exc:
            raise ASRTranscriptionError(f"ASR转写失败：{exc}") from exc

        payload = {
            "schema_version": ASR_SCHEMA_VERSION,
            "task_id": task_id,
            "cache_key": cache_key,
            "engine": engine,
            "source_audio": source,
            "language": {
                "code": language,
                "probability": language_probability,
            },
            "segments": segments,
        }
        try:
            asr_directory.mkdir(parents=True, exist_ok=True)
            _remove_temporary_file(temporary_path)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(output_path)
        except OSError as exc:
            raise ASRTranscriptionError(f"无法保存ASR结果：{output_path}") from exc
        finally:
            _remove_temporary_file(temporary_path)

        result = ASRTranscriptionResult(
            output_path=output_path,
            language=language,
            language_probability=language_probability,
            segment_count=len(segments),
            duration_seconds=duration,
            cache_hit=False,
        )
        _append_asr_log(
            task_root,
            event="completed",
            task_id=task_id,
            cache_key=cache_key,
            engine=engine,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            segment_count=result.segment_count,
        )
        return result
    except KeyboardInterrupt as exc:
        _append_failure_log_safely(
            task_root,
            task_id=task_id,
            cache_key=cache_key,
            engine=engine,
            started_at=started_at,
            output_path=output_path,
            error=exc,
        )
        raise ASRTranscriptionError("ASR转写被中断。") from exc
    except ASRTranscriptionError as exc:
        _append_failure_log_safely(
            task_root,
            task_id=task_id,
            cache_key=cache_key,
            engine=engine,
            started_at=started_at,
            output_path=output_path,
            error=exc,
        )
        raise
