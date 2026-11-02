"""M1纵向链路的零API缓存复验入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from video_doc.asr_timeline import build_task_timeline
from video_doc.asr_transcribe import ASRTranscriptionError, transcribe_task_audio
from video_doc.audio_extract import extract_task_audio
from video_doc.chapter_content import (
    prepare_task_chapter_inputs,
)
from video_doc.document_composition import prepare_document_composition_input
from video_doc.document_content_preview import publish_accepted_document_preview
from video_doc.document_export_v2 import (
    load_authoritative_document_content_v2,
    publish_document_export_v2,
)


class M1PipelineError(RuntimeError):
    """M1缓存复验没有满足冻结验收条件。"""


@dataclass(frozen=True)
class M1RunResult:
    task_id: str
    audio_cache_hit: bool
    asr_cache_hit: bool
    timeline_cache_hit: bool
    chapter_inputs_cache_hit: bool
    stage9_v1_validated: bool
    stage9_v2_cache_hit: bool
    export_cache_hit: bool
    content_id: str
    export_id: str
    result_group_path: Path
    result_json_path: Path
    result_markdown_path: Path
    result_docx_path: Path
    chapter_count: int
    event_count: int
    api_request_count: int


def _cache_required(stage: str, cache_hit: bool) -> None:
    if not cache_hit:
        raise M1PipelineError(
            f"run-m1只允许复验已有冻结缓存；{stage}未命中缓存，已停止。"
        )


def _block_asr_model_load(model: str, device: str, compute_type: str) -> NoReturn:
    del model, device, compute_type
    raise ASRTranscriptionError(
        "run-m1禁止加载ASR模型；现有ASR缓存无效或参数不匹配。"
    )


def run_task_m1(output_root: str | Path, task_id: str) -> M1RunResult:
    """用已有冻结缓存复验M1全链，并返回唯一三格式正式结果。"""

    audio = extract_task_audio(output_root, task_id)
    _cache_required("音频", audio.cache_hit)

    asr = transcribe_task_audio(
        output_root,
        task_id,
        model="small",
        device="cpu",
        compute_type="int8",
        model_factory=_block_asr_model_load,
    )
    _cache_required("ASR", asr.cache_hit)

    timeline = build_task_timeline(output_root, task_id)
    _cache_required("ASR时间轴", timeline.cache_hit)

    chapter_inputs = prepare_task_chapter_inputs(output_root, task_id)
    _cache_required("阶段8正式分章到阶段9章内输入", chapter_inputs.cache_hit)

    accepted = load_authoritative_document_content_v2(output_root, task_id)
    composition = prepare_document_composition_input(
        output_root,
        task_id,
        document_profile=accepted.document["document_profile"],
    )
    stage9_v2 = publish_accepted_document_preview(
        output_root,
        task_id,
        composition_input=composition.composition_input,
        content_id=accepted.content_id,
    )
    _cache_required("阶段9-R已接受2.0正式内容", stage9_v2.cache_hit)

    reloaded = load_authoritative_document_content_v2(output_root, task_id)
    if reloaded.content_id != accepted.content_id:
        raise M1PipelineError("阶段9-R权威内容指针在缓存复验中发生漂移。")
    exported = publish_document_export_v2(output_root, reloaded)
    _cache_required("阶段10三格式正式结果", exported.cache_hit)

    return M1RunResult(
        task_id=task_id,
        audio_cache_hit=audio.cache_hit,
        asr_cache_hit=asr.cache_hit,
        timeline_cache_hit=timeline.cache_hit,
        chapter_inputs_cache_hit=chapter_inputs.cache_hit,
        stage9_v1_validated=True,
        stage9_v2_cache_hit=stage9_v2.cache_hit,
        export_cache_hit=exported.cache_hit,
        content_id=reloaded.content_id,
        export_id=exported.export_id,
        result_group_path=exported.group_path,
        result_json_path=exported.group_path / "result.json",
        result_markdown_path=exported.group_path / "result.md",
        result_docx_path=exported.group_path / "result.docx",
        chapter_count=exported.manifest["chapter_count"],
        event_count=chapter_inputs.event_count,
        api_request_count=0,
    )
