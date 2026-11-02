"""全新视频到三格式文档的通用ASR-only编排入口。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

from video_doc.asr_timeline import build_task_timeline
from video_doc.asr_transcribe import transcribe_task_audio
from video_doc.audio_extract import extract_task_audio
from video_doc.asr_document_v2 import compose_task_document_v2_candidate
from video_doc.chapter_content import (
    ChapterContentError,
    estimate_task_chapter_content_calls,
    generate_task_chapter_content_generic,
)
from video_doc.document_export import load_authoritative_chapter_content
from video_doc.document_export_v2 import publish_document_export_v2
from video_doc.formal_chaptering import plan_task_chapters_formal
from video_doc.task_workspace import (
    initialize_task_workspace,
    load_task_manifest,
    validate_task_id,
)
from video_doc.video_probe import VideoMetadata, probe_video


DEFAULT_MAX_CONTENT_CALLS = 16


class ASROnlyPipelineError(RuntimeError):
    """通用ASR-only编排在安全闸门或阶段执行中失败。"""


@dataclass(frozen=True)
class MediaTools:
    directory: Path
    ffmpeg: Path
    ffprobe: Path


@dataclass(frozen=True)
class ASROnlyRunResult:
    task_id: str
    source_video: Path
    result_group_path: Path
    result_json_path: Path
    result_markdown_path: Path
    result_docx_path: Path
    export_id: str
    content_id: str
    review_status: str
    document_profile: str
    chapter_count: int
    event_count: int
    content_planned_call_count: int
    content_business_call_count: int
    document_v2_planned_call_count: int
    document_v2_business_call_count: int
    boundary_business_call_count: int
    supplier_request_count: int
    all_llm_cache_hit: bool
    export_cache_hit: bool
    reused_authoritative_content: bool
    reused_document_v2_candidate: bool


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ASROnlyPipelineError(f"无法读取源视频：{path}") from exc
    return digest.hexdigest()


def _same_source(left: Path, right: Path) -> bool:
    try:
        if left.resolve() == right.resolve():
            return True
        if not left.is_file() or not right.is_file():
            return False
        return left.stat().st_size == right.stat().st_size and _file_sha256(
            left
        ) == _file_sha256(right)
    except OSError as exc:
        raise ASROnlyPipelineError("SOURCE_VIDEO_MISMATCH：无法核对源视频身份。") from exc


def _executable(directory: Path, name: str) -> Path:
    candidates = (directory / f"{name}.exe", directory / name)
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise ASROnlyPipelineError(
            f"MEDIA_TOOL_INVALID：{directory}中缺少{name}。"
        )
    return path.resolve()


def _validate_tool(path: Path) -> None:
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ASROnlyPipelineError(
            f"MEDIA_TOOL_INVALID：无法执行{path.name}版本探测。"
        ) from exc
    if completed.returncode != 0:
        raise ASROnlyPipelineError(
            f"MEDIA_TOOL_INVALID：{path.name}版本探测失败。"
        )


def resolve_media_tools(ffmpeg_dir: str | Path | None = None) -> MediaTools:
    """按显式目录、本地环境配置、PATH顺序解析同目录工具。"""

    selected_dir = ffmpeg_dir or os.environ.get("VIDEO_DOC_FFMPEG_DIR")
    if selected_dir is not None:
        directory = Path(selected_dir).expanduser()
        if not directory.is_dir():
            raise ASROnlyPipelineError(
                f"MEDIA_TOOL_INVALID：FFmpeg目录不存在或不可访问：{directory}"
            )
        ffmpeg = _executable(directory, "ffmpeg")
        ffprobe = _executable(directory, "ffprobe")
    else:
        ffmpeg_raw = shutil.which("ffmpeg")
        ffprobe_raw = shutil.which("ffprobe")
        if ffmpeg_raw is None or ffprobe_raw is None:
            raise ASROnlyPipelineError(
                "MEDIA_TOOL_NOT_FOUND：未同时找到ffmpeg和ffprobe；"
                "请使用--ffmpeg-dir指定安装目录。"
            )
        ffmpeg = Path(ffmpeg_raw).resolve()
        ffprobe = Path(ffprobe_raw).resolve()
        if ffmpeg.parent != ffprobe.parent:
            raise ASROnlyPipelineError(
                "MEDIA_TOOL_INVALID：ffmpeg和ffprobe必须来自同一目录。"
            )
        directory = ffmpeg.parent
    _validate_tool(ffmpeg)
    _validate_tool(ffprobe)
    return MediaTools(directory=directory.resolve(), ffmpeg=ffmpeg, ffprobe=ffprobe)


@contextmanager
def _media_tool_path(tools: MediaTools) -> Iterator[None]:
    previous = os.environ.get("PATH")
    suffix = previous or ""
    os.environ["PATH"] = str(tools.directory) + os.pathsep + suffix
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


def _resolve_source(
    output_root: str | Path,
    task_id: str,
    source_video: str | Path | None,
) -> tuple[Path, bool]:
    validate_task_id(task_id)
    task_root = Path(output_root).expanduser() / task_id
    manifest_path = task_root / "task.json"
    if not manifest_path.is_file():
        if source_video is None:
            raise ASROnlyPipelineError(
                "SOURCE_VIDEO_REQUIRED：新任务必须提供--source-video。"
            )
        source = Path(source_video).expanduser()
        if not source.is_file():
            raise ASROnlyPipelineError(f"源视频不存在：{source}")
        return source.resolve(), True

    manifest = load_task_manifest(manifest_path, expected_task_id=task_id)
    recorded = manifest.get("source_video")
    if not recorded:
        raise ASROnlyPipelineError("已有task.json未记录源视频路径。")
    source = Path(recorded).expanduser()
    if not source.is_file():
        raise ASROnlyPipelineError(f"源视频不存在：{source}")
    if source_video is not None:
        provided = Path(source_video).expanduser()
        if not _same_source(source, provided):
            raise ASROnlyPipelineError(
                "SOURCE_VIDEO_MISMATCH：提供的视频与已有任务源身份不一致。"
            )
    return source.resolve(), False


def _save_metadata_if_changed(path: Path, metadata: VideoMetadata) -> None:
    content = json.dumps(asdict(metadata), ensure_ascii=False, indent=2) + "\n"
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except (OSError, UnicodeError):
            pass
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise ASROnlyPipelineError(f"无法保存视频元数据：{path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _existing_authoritative_content(
    output_root: str | Path, task_id: str
):
    content_root = Path(output_root).expanduser() / task_id / "content"
    content_path = content_root / "content.json"
    report_path = content_root / "content-execution-report.json"
    if not content_path.exists() and not report_path.exists():
        return None
    if not content_path.is_file() or not report_path.is_file():
        raise ASROnlyPipelineError(
            "已有正式正文别名不完整；为保护产物，入口拒绝覆盖。"
        )
    return load_authoritative_chapter_content(output_root, task_id)


def run_task_asr_only(
    output_root: str | Path,
    task_id: str,
    *,
    source_video: str | Path | None = None,
    ffmpeg_dir: str | Path | None = None,
    allow_llm: bool = False,
    asr_model: str = "small",
    asr_device: str = "cpu",
    asr_compute_type: str = "int8",
    document_profile: str = "short_explainer",
    max_content_calls: int = DEFAULT_MAX_CONTENT_CALLS,
    progress: Callable[[str, str], None] | None = None,
) -> ASROnlyRunResult:
    """从新视频或已有任务恢复，生成待复核的Schema 2.0三格式文档。"""

    if (
        isinstance(max_content_calls, bool)
        or not isinstance(max_content_calls, int)
        or max_content_calls < 1
    ):
        raise ASROnlyPipelineError("max_content_calls必须是正整数。")
    notify = progress or (lambda stage, status: None)
    notify("preflight", "started")
    tools = resolve_media_tools(ffmpeg_dir)
    source, is_new = _resolve_source(output_root, task_id, source_video)
    with _media_tool_path(tools):
        metadata = probe_video(source)
        if not metadata.has_audio:
            raise ASROnlyPipelineError("源视频没有音轨，当前ASR-only入口无法继续。")
        notify("preflight", "completed")
        initialize_task_workspace(
            output_root,
            task_id,
            source_video=source if is_new else None,
        )
        notify("task_init", "completed")
        task_root = Path(output_root).expanduser() / task_id
        _save_metadata_if_changed(task_root / "metadata.json", metadata)
        audio = extract_task_audio(output_root, task_id)
        notify("audio", "cache_hit" if audio.cache_hit else "completed")
        transcript = transcribe_task_audio(
            output_root,
            task_id,
            model=asr_model,
            device=asr_device,
            compute_type=asr_compute_type,
        )
        notify("asr", "cache_hit" if transcript.cache_hit else "completed")
        timeline = build_task_timeline(output_root, task_id)
        notify("timeline", "cache_hit" if timeline.cache_hit else "completed")

        authoritative = _existing_authoritative_content(output_root, task_id)
        reused_authoritative = authoritative is not None
        boundary_business_calls = 0
        content_planned_calls = 0
        content_business_calls = 0
        supplier_requests = 0
        all_llm_cache_hit = reused_authoritative
        if authoritative is None:
            if not allow_llm:
                raise ASROnlyPipelineError(
                    "LLM_APPROVAL_REQUIRED：尚无可复用的正式正文；"
                    "如允许本次调用项目配置的LLM，请增加--allow-llm。"
                )
            boundary = plan_task_chapters_formal(output_root, task_id)
            boundary_business_calls = boundary.business_call_count
            supplier_requests += boundary.supplier_request_count
            notify(
                "chapter_plan",
                "cache_hit" if boundary.business_call_count == 0 else "completed",
            )
            plan = estimate_task_chapter_content_calls(output_root, task_id)
            content_planned_calls = plan.business_call_count
            if plan.business_call_count > max_content_calls:
                raise ASROnlyPipelineError(
                    "CONTENT_CALL_BUDGET_EXCEEDED：正文预计业务调用数"
                    f"{plan.business_call_count}超过上限{max_content_calls}。"
                )
            content = generate_task_chapter_content_generic(
                output_root,
                task_id,
                max_business_call_count=max_content_calls,
            )
            content_business_calls = plan.business_call_count - content.cache_hit_count
            supplier_requests += content.supplier_request_count
            all_llm_cache_hit = (
                boundary.business_call_count == 0 and content.all_cache_hit
            )
            notify(
                "document_content",
                "cache_hit" if content.all_cache_hit else "completed",
            )
            authoritative = load_authoritative_chapter_content(output_root, task_id)
        else:
            notify("document_content", "cache_hit")

        document_v2 = compose_task_document_v2_candidate(
            output_root,
            task_id,
            document_profile=document_profile,
            allow_llm=allow_llm,
            max_business_call_count=max_content_calls,
        )
        notify(
            "document_v2",
            "cache_hit" if document_v2.reused_candidate else "completed",
        )
        supplier_requests += document_v2.supplier_request_count
        all_llm_cache_hit = all_llm_cache_hit and document_v2.all_llm_cache_hit
        exported = publish_document_export_v2(output_root, document_v2.source)
        notify("export", "cache_hit" if exported.cache_hit else "completed")
        return ASROnlyRunResult(
            task_id=task_id,
            source_video=source,
            result_group_path=exported.group_path,
            result_json_path=exported.group_path / "result.json",
            result_markdown_path=exported.group_path / "result.md",
            result_docx_path=exported.group_path / "result.docx",
            export_id=exported.export_id,
            content_id=document_v2.source.content_id,
            review_status=document_v2.review_status,
            document_profile=document_profile,
            chapter_count=exported.manifest["chapter_count"],
            event_count=timeline.event_count,
            content_planned_call_count=content_planned_calls,
            content_business_call_count=content_business_calls,
            document_v2_planned_call_count=document_v2.planned_call_count,
            document_v2_business_call_count=document_v2.business_call_count,
            boundary_business_call_count=boundary_business_calls,
            supplier_request_count=supplier_requests,
            all_llm_cache_hit=all_llm_cache_hit,
            export_cache_hit=exported.cache_hit,
            reused_authoritative_content=reused_authoritative,
            reused_document_v2_candidate=document_v2.reused_candidate,
        )
