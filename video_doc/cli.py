"""项目的命令行入口。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from video_doc.asr_timeline import (
    ASRTimelineError,
    build_task_timeline,
    load_task_timeline,
    query_timeline_events,
    render_timeline_text,
)
from video_doc.asr_transcribe import ASRTranscriptionError, transcribe_task_audio
from video_doc.asr_document_v2 import ASRDocumentV2Error
from video_doc.asr_only_pipeline import ASROnlyPipelineError, run_task_asr_only
from video_doc.audio_extract import AudioExtractionError, extract_task_audio
from video_doc.chapter_review import ChapterReviewError, review_task_chapters
from video_doc.chapter_review_v2 import review_task_chapters_v2
from video_doc.chapter_content import (
    assemble_task_chapter_content_offline,
    ChapterContentError,
    generate_task_chapter_content,
    generate_task_chapter_content_single_window_mock,
    generate_task_chapter_content_windowed_mock,
    prepare_task_chapter_content_semantic_repair_candidate,
    prepare_task_chapter_inputs,
    publish_task_chapter_content_semantic_repair,
    reconcile_task_chapter_content_audits,
    recover_task_chapter_content_candidate_offline,
)
from video_doc.document_export import (
    DocumentExportError,
)
from video_doc.document_composition import DocumentCompositionError
from video_doc.document_content_preview import DocumentContentPreviewError
from video_doc.document_export_v2 import (
    DocumentExportV2Error,
    load_authoritative_document_content_v2,
    publish_document_export_v2,
)
from video_doc.formal_chaptering import plan_task_chapters_formal
from video_doc.fact_card_composition import FactCardCompositionError
from video_doc.frame_sampler import (
    FRAME_GROUP_DIRECTORY,
    FRAME_MODULE_TOKEN,
    LOW_FREQUENCY_V1,
    SAMPLING_PROFILES,
    FrameSamplerError,
    sample_frames,
)
from video_doc.llm_adapter import (
    LLMAdapterError,
    call_task_llm,
    load_llm_request_file,
)
from video_doc.llm_strategy import (
    DEFAULT_MIN_CHAPTER_SECONDS,
    STABILITY_MIN_CHAPTER_SECONDS,
    ChapterPlanError,
    plan_task_chapters,
)
from video_doc.m1_pipeline import M1PipelineError, run_task_m1
from video_doc.m2_pipeline import (
    M2EnvelopeFailure,
    M2PipelineError,
    run_task_m2,
)
from video_doc.page_change_detector import (
    PAGE_CHANGE_GROUP_DIRECTORY,
    PAGE_CHANGE_MODULE_TOKEN,
    PAGE_CHANGE_ROOT_DIRECTORY,
    PageChangeDetectorError,
    detect_page_changes,
)
from video_doc.ocr_extractor import (
    OCR_GROUP_DIRECTORY,
    OCR_MODES,
    OCR_MODULE_TOKEN,
    OCR_ROOT_DIRECTORY,
    OcrDegradedOutcome,
    OcrExtractorError,
    run_ocr,
)
from video_doc.boundary_planner_v2 import (
    BOUNDARY_GROUP_DIRECTORY,
    BOUNDARY_MODULE_TOKEN,
    BOUNDARY_ROOT_DIRECTORY,
    BoundaryPlannerV2Error,
    plan_chapters_v2,
)
from video_doc.chapter_frames import (
    GROUP_DIRECTORY as CHAPTER_FRAMES_GROUP_DIRECTORY,
    MODULE_TOKEN as CHAPTER_FRAMES_MODULE_TOKEN,
    ROOT_DIRECTORY as CHAPTER_FRAMES_ROOT_DIRECTORY,
    ChapterFramesError,
    select_chapter_frames,
)
from video_doc.document_composer_v2 import (
    GROUP_DIRECTORY as CONTENT_GROUP_DIRECTORY,
    MODULE_TOKEN as CONTENT_MODULE_TOKEN,
    ROOT_DIRECTORY as CONTENT_ROOT_DIRECTORY,
    DocumentComposerV2Error,
    compose_document_v2,
)
from video_doc.document_exporter_v2 import (
    EXPORT_MODULE_TOKEN,
    EXPORT_ROOT_DIRECTORY,
    DocumentExporterV2Error,
    export_document_v2,
)
from video_doc.timeline_builder_v2 import (
    OCR_REQUESTS,
    TIMELINE_GROUP_DIRECTORY,
    TIMELINE_MODULE_TOKEN,
    TIMELINE_ROOT_DIRECTORY,
    TimelineBuilderV2Error,
    build_timeline_v2,
)
from video_doc.task_workspace import (
    TaskWorkspaceError,
    initialize_task_workspace,
    load_task_manifest,
)
from video_doc.topic_anchor_strategy import (
    TopicAnchorError,
    plan_task_chapters_anchor_v2,
)
from video_doc.two_stage_llm_strategy import (
    DEFAULT_ALIGNMENT_WINDOW_SECONDS,
    plan_task_chapters_two_stage,
)
from video_doc.video_probe import VideoProbeError, probe_video, save_metadata


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知识型视频结构化文档生成系统")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check-env", help="检查当前开发环境")

    inspect_parser = subparsers.add_parser("inspect", help="读取本地视频基础信息")
    inspect_parser.add_argument("video", help="本地视频文件路径")
    inspect_parser.add_argument("--output", help="元数据 JSON 输出路径")

    init_parser = subparsers.add_parser("init-task", help="初始化单个视频任务目录")
    init_parser.add_argument("task_id", help="任务ID，例如DEV01或V01")
    init_parser.add_argument(
        "--output-root",
        default="outputs",
        help="任务输出根目录，默认：outputs",
    )
    init_parser.add_argument("--source-video", help="对应的本地视频路径")

    audio_parser = subparsers.add_parser(
        "extract-audio",
        help="为单个视频任务提取16 kHz单声道WAV",
    )
    audio_parser.add_argument("task_id", help="已经初始化的任务ID")
    audio_parser.add_argument(
        "--output-root",
        default="outputs",
        help="任务输出根目录，默认：outputs",
    )

    asr_parser = subparsers.add_parser(
        "transcribe",
        help="使用faster-whisper转写单个视频任务",
    )
    asr_parser.add_argument("task_id", help="已经提取音频的任务ID")
    asr_parser.add_argument(
        "--output-root",
        default="outputs",
        help="任务输出根目录，默认：outputs",
    )
    asr_parser.add_argument("--model", default="small", help="模型名称，默认：small")
    asr_parser.add_argument("--device", default="cpu", help="推理设备，默认：cpu")
    asr_parser.add_argument(
        "--compute-type",
        default="int8",
        help="计算精度，默认：int8",
    )

    timeline_parser = subparsers.add_parser(
        "build-timeline",
        help="从ASR结果生成结构化时间轴和可读文本",
    )
    timeline_parser.add_argument("task_id", help="已经完成ASR转写的任务ID")
    timeline_parser.add_argument(
        "--output-root",
        default="outputs",
        help="任务输出根目录，默认：outputs",
    )

    query_parser = subparsers.add_parser(
        "query-timeline",
        help="按半开时间区间[start, end)查询时间轴",
    )
    query_parser.add_argument("task_id", help="已经生成时间轴的任务ID")
    query_parser.add_argument(
        "--output-root",
        default="outputs",
        help="任务输出根目录，默认：outputs",
    )
    query_parser.add_argument("--start", type=float, required=True, help="查询起点（秒）")
    query_parser.add_argument("--end", type=float, required=True, help="查询终点（秒）")
    query_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="输出格式，默认：text",
    )

    llm_parser = subparsers.add_parser(
        "call-llm",
        help="执行一次可缓存的结构化LLM调用",
    )
    llm_parser.add_argument("task_id", help="已经初始化的任务ID")
    llm_parser.add_argument(
        "--output-root",
        default="outputs",
        help="任务输出根目录，默认：outputs",
    )
    llm_parser.add_argument("--call-id", required=True, help="本次调用的稳定ID")
    llm_parser.add_argument(
        "--request-file",
        required=True,
        help="包含messages和可选response_format的JSON文件",
    )

    chapter_parser = subparsers.add_parser(
        "plan-chapters",
        help="使用完整ASR时间轴直接生成LLM章节计划",
    )
    chapter_parser.add_argument("task_id", help="已经生成时间轴的任务ID")
    chapter_parser.add_argument(
        "--output-root",
        default="outputs",
        help="任务输出根目录，默认：outputs",
    )
    chapter_parser.add_argument(
        "--min-chapter-seconds",
        type=float,
        default=None,
        help=(
            f"最小章节时长；冻结A正式运行和稳定性实验固定"
            f"{STABILITY_MIN_CHAPTER_SECONDS:g}秒，独立旧流程默认"
            f"{DEFAULT_MIN_CHAPTER_SECONDS:g}秒"
        ),
    )
    chapter_parser.add_argument(
        "--pipeline",
        choices=("direct", "two-stage"),
        default="direct",
        help="分章流程；two-stage生成独立候选结果，默认：direct",
    )
    chapter_parser.add_argument(
        "--alignment-window-seconds",
        type=float,
        default=DEFAULT_ALIGNMENT_WINDOW_SECONDS,
        help=f"两阶段边界定位窗口半径，默认：{DEFAULT_ALIGNMENT_WINDOW_SECONDS:g}秒",
    )
    chapter_parser.add_argument(
        "--experiment-id",
        help="阶段8独立实验运行ID，例如run-01；提供后不会覆盖正式分章产物",
    )

    review_parser = subparsers.add_parser(
        "review-chapters",
        help="审阅一份阶段8方案A事件ID章节计划",
    )
    review_parser.add_argument("task_id", help="已经完成方案A实验的任务ID")
    review_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    review_parser.add_argument(
        "--source-run-id", required=True, help="来源方案A运行ID，例如run-01"
    )
    review_v2_parser = subparsers.add_parser(
        "review-chapters-v2",
        help="使用简化v2契约审阅一份阶段8方案A事件ID章节计划",
    )
    review_v2_parser.add_argument("task_id", help="已经完成方案A实验的任务ID")
    review_v2_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    review_v2_parser.add_argument(
        "--source-run-id", required=True, help="来源方案A运行ID，例如run-01"
    )
    anchor_parser = subparsers.add_parser(
        "plan-chapters-anchor-v2",
        help="运行阶段8主题锚点严格实验primary路径",
    )
    anchor_parser.add_argument("task_id", help="已经生成时间轴的任务ID")
    anchor_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    anchor_parser.add_argument(
        "--run-id", required=True, help="阶段8独立运行ID，例如run-01"
    )
    content_input_parser = subparsers.add_parser(
        "prepare-chapter-inputs",
        help="校验正式分章结果并离线准备逐章ASR输入manifest",
    )
    content_input_parser.add_argument("task_id", help="已经完成正式分章的任务ID")
    content_input_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    content_mock_parser = subparsers.add_parser(
        "generate-chapter-content-mock",
        help="P9-2使用确定性本地mock逐章生成单窗内容预览",
    )
    content_mock_parser.add_argument("task_id", help="已经准备章节输入的任务ID")
    content_mock_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    content_windowed_mock_parser = subparsers.add_parser(
        "generate-chapter-content-windowed-mock",
        help="P9-3确定性分窗、离线mock生成与章内合并",
    )
    content_windowed_mock_parser.add_argument(
        "task_id", help="已经准备章节输入的任务ID"
    )
    content_windowed_mock_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    real_content_parser = subparsers.add_parser(
        "generate-chapter-content",
        help="使用P9-4冻结配置生成正式章节内容",
    )
    real_content_parser.add_argument("task_id", help="P9-5固定为DEV01")
    real_content_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    reconciliation_parser = subparsers.add_parser(
        "reconcile-chapter-content-audits",
        help="R2-2离线记录陈旧失败审计与后续成功调用的取代关系",
    )
    reconciliation_parser.add_argument("task_id", help="R2-2固定为DEV01")
    reconciliation_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    candidate_parser = subparsers.add_parser(
        "recover-chapter-content-candidate-offline",
        help="R2-3从冻结失败审计生成第五章离线规范化候选",
    )
    candidate_parser.add_argument("task_id", help="R2-3固定为DEV01")
    candidate_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    formal_recovery_parser = subparsers.add_parser(
        "assemble-chapter-content-offline",
        help="R2-4组装冻结五章来源并发布正式章节内容",
    )
    formal_recovery_parser.add_argument("task_id", help="R2-4固定为DEV01")
    formal_recovery_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    semantic_repair_parser = subparsers.add_parser(
        "prepare-chapter-content-semantic-repair",
        help="R3-2生成冻结单字段语义修复候选，不发布正式结果",
    )
    semantic_repair_parser.add_argument("task_id", help="R3-2固定为DEV01")
    semantic_repair_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    semantic_repair_publish_parser = subparsers.add_parser(
        "publish-chapter-content-semantic-repair",
        help="R3-3复验冻结候选并离线发布新正式章节内容",
    )
    semantic_repair_publish_parser.add_argument("task_id", help="R3-3固定为DEV01")
    semantic_repair_publish_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    export_parser = subparsers.add_parser(
        "export-document",
        help="从阶段9唯一权威内容离线导出JSON、Markdown和Word结果组",
    )
    export_parser.add_argument("task_id", help="已经完成正式章节内容的任务ID")
    export_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    m1_parser = subparsers.add_parser(
        "run-m1",
        help="零API复验M1已有缓存并返回JSON、Markdown和Word正式结果",
    )
    m1_parser.add_argument("task_id", help="已经完成M1正式验收的任务ID")
    m1_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    run_parser = subparsers.add_parser(
        "run",
        help="从全新视频或已有缓存运行当前ASR-only链路并导出三种格式",
    )
    run_parser.add_argument("task_id", help="新建或继续处理的任务ID")
    run_parser.add_argument("--source-video", help="新任务必填的本地视频路径")
    run_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    run_parser.add_argument(
        "--ffmpeg-dir", help="同时包含ffmpeg和ffprobe的本地目录"
    )
    run_parser.add_argument(
        "--allow-llm",
        action="store_true",
        help="允许本次运行在正式正文缓存缺失时访问项目LLM",
    )
    run_parser.add_argument("--asr-model", default="small", help="ASR模型，默认：small")
    run_parser.add_argument("--asr-device", default="cpu", help="ASR设备，默认：cpu")
    run_parser.add_argument(
        "--asr-compute-type", default="int8", help="ASR计算精度，默认：int8"
    )
    run_parser.add_argument(
        "--max-content-calls",
        type=int,
        default=16,
        help="正文业务调用硬上限，默认：16",
    )
    run_parser.add_argument(
        "--document-profile",
        choices=("short_explainer", "course", "academic_report", "speech"),
        default="short_explainer",
        help="2.0文档组织类型，默认：short_explainer",
    )
    sample_frames_parser = subparsers.add_parser(
        "sample-frames",
        help="按版本化profile低频抽取视频帧并原子发布不可变帧集",
    )
    sample_frames_parser.add_argument("task_id", help="已经初始化并记录源视频的任务ID")
    sample_frames_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    sample_frames_parser.add_argument(
        "--profile",
        choices=sorted(SAMPLING_PROFILES),
        default=LOW_FREQUENCY_V1.profile_id,
        help=f"取帧profile，默认：{LOW_FREQUENCY_V1.profile_id}",
    )
    sample_frames_parser.add_argument(
        "--ffmpeg-dir", help="同时包含ffmpeg和ffprobe的本地目录"
    )
    page_change_parser = subparsers.add_parser(
        "detect-page-changes",
        help="只读正式帧集，发布页面变化强度、筛选与去重候选",
    )
    page_change_parser.add_argument("task_id", help="已经发布正式帧集的任务ID")
    page_change_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    run_ocr_parser = subparsers.add_parser(
        "run-ocr",
        help="只对正式页面变化候选帧运行OCR并原子发布不可变OCRSet",
    )
    run_ocr_parser.add_argument("task_id", help="已经发布正式页面变化集合的任务ID")
    run_ocr_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    run_ocr_parser.add_argument(
        "--mode",
        choices=list(OCR_MODES),
        default="auto",
        help="off跳过引擎；auto失败只降级、文本链继续；required失败停止当前命令，默认：auto",
    )
    build_timeline_parser = subparsers.add_parser(
        "build-timeline-v2",
        help="只读已发布上游来源，组合并原子发布不可变UnifiedTimeline 2.0",
    )
    build_timeline_parser.add_argument(
        "task_id", help="已经发布M1时间轴（及可选帧/页面变化/OCR集合）的任务ID"
    )
    build_timeline_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    build_timeline_parser.add_argument(
        "--ocr",
        choices=list(OCR_REQUESTS),
        default="auto",
        help=(
            "对已发布OCR集合的消费方式：off不读取并记disabled；"
            "auto消费或记not_run；required必须存在且状态为success/empty，默认：auto"
        ),
    )
    plan_chapters_v2_parser = subparsers.add_parser(
        "plan-chapters-v2",
        help="只读已发布Timeline 2.0，经boundary_model决策并原子发布不可变ChapterPlan 2.0",
    )
    plan_chapters_v2_parser.add_argument(
        "task_id", help="已经发布UnifiedTimeline 2.0正式组的任务ID"
    )
    plan_chapters_v2_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    plan_chapters_v2_parser.add_argument(
        "--model-registry",
        default=None,
        help="ModelRoleRegistry 1.0文件路径；缺省时依次尝试config/model-roles.json与内置禁用配置",
    )
    select_chapter_frames_parser = subparsers.add_parser(
        "select-chapter-frames",
        help="只读已发布的ChapterPlan/Timeline/frame-set（按条件page-change-set与ocr-set），"
        "选择章内代表帧、渲染确定性说明并原子发布不可变ChapterFrameSelection组",
    )
    select_chapter_frames_parser.add_argument(
        "task_id", help="已经发布ChapterPlan 2.0正式组的任务ID"
    )
    select_chapter_frames_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    compose_document_v2_parser = subparsers.add_parser(
        "compose-document-v2",
        help="只读已发布ChapterPlan/Timeline/ChapterFrameSelection，经content_model组合"
        "并原子发布不可变StructuredDocumentContent 3.0",
    )
    compose_document_v2_parser.add_argument(
        "task_id", help="已经发布ChapterFrameSelection正式组的任务ID"
    )
    compose_document_v2_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    compose_document_v2_parser.add_argument(
        "--model-registry",
        default=None,
        help="ModelRoleRegistry 1.0文件路径；缺省时依次尝试config/model-roles.json与内置禁用配置",
    )
    compose_document_v2_parser.add_argument("--bounded-content", action="store_true", help="逐章每6单元分批，正文至多12次供应商请求；复用已发布正文")
    export_document_v2_parser = subparsers.add_parser(
        "export-document-v2",
        help="只读已发布StructuredDocumentContent与frame set，渲染JSON/Markdown/Word三格式"
        "并原子发布自包含导出组",
    )
    export_document_v2_parser.add_argument(
        "task_id", help="已经发布StructuredDocumentContent正式组的任务ID"
    )
    export_document_v2_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    run_m2_parser = subparsers.add_parser(
        "run-m2",
        help="P6新鲜视频一键M2入口：preflight→task_init→transcript→frames→page_changes→"
        "ocr→timeline_v2→chapter_plan_v2→chapter_frames→document_content_v2→export_v2全链；"
        "阶段15-0全链编排已接线（E0–E4）：审批守卫（真实LLM调用需--allow-llm）、"
        "输入预算条目、JSONL事件与失败信封",
    )
    run_m2_parser.add_argument("--bounded-content", action="store_true", help="启用有界分章正文生成并复用已发布正文")
    run_m2_parser.add_argument(
        "task_id", help="任务ID；task.json不存在时按全新任务预检，存在时按已有任务预检"
    )
    run_m2_parser.add_argument(
        "--source-video",
        default=None,
        help="全新任务必须提供的源视频路径；已有任务可省略（按task.json登记身份为准），提供时仅核对不重绑",
    )
    run_m2_parser.add_argument(
        "--output-root", default="outputs", help="任务输出根目录，默认：outputs"
    )
    run_m2_parser.add_argument(
        "--ffmpeg-dir",
        default=None,
        help="显式FFmpeg安装目录；缺省时依次尝试VIDEO_DOC_FFMPEG_DIR与PATH",
    )
    run_m2_parser.add_argument(
        "--ocr",
        choices=list(OCR_MODES),
        default="auto",
        help="OCR模式：驱动run_ocr并以同值透传build-timeline-v2（裁决§三.4一参数两用）",
    )
    run_m2_parser.add_argument(
        "--vision-caption",
        choices=["off"],
        default="off",
        help="视觉说明角色；当前契约仅接受off（恒0预算，registry无启用路径）",
    )
    run_m2_parser.add_argument(
        "--model-registry",
        default=None,
        help="ModelRoleRegistry 1.0文件路径；缺省时沿用既有回退链",
    )
    run_m2_parser.add_argument(
        "--allow-llm",
        action="store_true",
        help="显式批准计划内供应商请求；缺省时在任何计划调用>0的角色首个请求前停止",
    )
    run_m2_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出单条运行计划JSON：零写入、零网络、除preflight硬失败外用0退出",
    )
    return parser


def _check_environment() -> int:
    status = {
        "python": sys.version.split()[0],
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if status["ffmpeg"] is None or status["ffprobe"] is None:
        print("\n环境尚未就绪：后续视频处理需要安装 FFmpeg。", file=sys.stderr)
        return 1
    print("\n基础环境检查通过。")
    return 0


def _inspect_video(video: str, output: str | None) -> int:
    metadata = probe_video(video)
    print(json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2))

    if output:
        destination = save_metadata(metadata, Path(output))
        print(f"\n元数据已保存：{destination.resolve()}")
    return 0


def _initialize_task(task_id: str, output_root: str, source_video: str | None) -> int:
    workspace = initialize_task_workspace(
        output_root,
        task_id,
        source_video=source_video,
    )
    manifest = load_task_manifest(
        workspace.manifest_path,
        expected_task_id=task_id,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\n任务目录已就绪：{workspace.root.resolve()}")
    return 0


def _extract_audio(task_id: str, output_root: str) -> int:
    result = extract_task_audio(output_root, task_id)
    status = {
        "task_id": task_id,
        "audio_path": str(result.output_path.resolve()),
        "sample_rate": result.metadata.sample_rate,
        "channels": result.metadata.channels,
        "codec": result.metadata.codec,
        "duration_seconds": result.metadata.duration_seconds,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n音频缓存已命中。" if result.cache_hit else "\n音频提取完成。")
    return 0


def _transcribe(
    task_id: str,
    output_root: str,
    model: str,
    device: str,
    compute_type: str,
) -> int:
    result = transcribe_task_audio(
        output_root,
        task_id,
        model=model,
        device=device,
        compute_type=compute_type,
    )
    status = {
        "task_id": task_id,
        "transcript_path": str(result.output_path.resolve()),
        "language": result.language,
        "language_probability": result.language_probability,
        "segment_count": result.segment_count,
        "duration_seconds": result.duration_seconds,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\nASR缓存已命中。" if result.cache_hit else "\nASR转写完成。")
    return 0


def _build_timeline(task_id: str, output_root: str) -> int:
    result = build_task_timeline(output_root, task_id)
    status = {
        "task_id": task_id,
        "timeline_path": str(result.json_path.resolve()),
        "text_path": str(result.text_path.resolve()),
        "event_count": result.event_count,
        "duration_seconds": result.duration_seconds,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n时间轴缓存已命中。" if result.cache_hit else "\nASR时间轴生成完成。")
    return 0


def _query_timeline(
    task_id: str,
    output_root: str,
    start: float,
    end: float,
    output_format: str,
) -> int:
    timeline = load_task_timeline(output_root, task_id)
    events = query_timeline_events(timeline, start, end)
    if output_format == "json":
        payload = {
            "task_id": task_id,
            "start": start,
            "end": end,
            "event_count": len(events),
            "events": events,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_timeline_text(events), end="")
    return 0


def _call_llm(
    task_id: str,
    output_root: str,
    call_id: str,
    request_file: str,
) -> int:
    messages, response_format = load_llm_request_file(request_file)
    result = call_task_llm(
        output_root,
        task_id,
        call_id=call_id,
        messages=messages,
        response_format=response_format,
    )
    status = {
        "task_id": task_id,
        "call_id": call_id,
        "result_path": str(result.output_path.resolve()),
        "backend": result.backend,
        "model": result.model,
        "attempt_count": result.attempt_count,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\nLLM缓存已命中。" if result.cache_hit else "\nLLM调用完成。")
    return 0


def _prepare_chapter_inputs(task_id: str, output_root: str) -> int:
    result = prepare_task_chapter_inputs(output_root, task_id)
    status = {
        "task_id": task_id,
        "manifest_path": str(result.manifest_path.resolve()),
        "result_group_path": str(result.result_group_path.resolve()),
        "chapter_count": result.chapter_count,
        "event_count": result.event_count,
        "result_source": result.result_source,
        "quality_level": result.quality_level,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n章节输入缓存已命中。" if result.cache_hit else "\n章节输入准备完成。")
    return 0


def _generate_chapter_content_mock(task_id: str, output_root: str) -> int:
    result = generate_task_chapter_content_single_window_mock(output_root, task_id)
    status = {
        "task_id": task_id,
        "mode": "mock",
        "preview_path": str(result.preview_path.resolve()),
        "attempt_report_path": str(result.attempt_report_path.resolve()),
        "chapter_count": result.chapter_count,
        "supplier_request_count": result.supplier_request_count,
        "cache_hit_count": result.cache_hit_count,
        "all_cache_hit": result.all_cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n单窗mock内容缓存已全部命中。" if result.all_cache_hit else "\n单窗mock内容生成完成。")
    return 0


def _generate_chapter_content_windowed_mock(task_id: str, output_root: str) -> int:
    result = generate_task_chapter_content_windowed_mock(output_root, task_id)
    status = {
        "task_id": task_id,
        "mode": "mock-windowed",
        "result_group_path": str(result.result_group_path.resolve()),
        "content_path": str(result.content_path.resolve()),
        "execution_report_path": str(result.execution_report_path.resolve()),
        "chapter_count": result.chapter_count,
        "window_count": result.window_count,
        "merge_count": result.merge_count,
        "supplier_request_count": result.supplier_request_count,
        "cache_hit_count": result.cache_hit_count,
        "all_cache_hit": result.all_cache_hit,
        "result_group_cache_hit": result.result_group_cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n分窗mock内容与结果组缓存已命中。" if result.all_cache_hit else "\n分窗mock内容生成与结果组发布完成。")
    return 0


def _generate_chapter_content(task_id: str, output_root: str) -> int:
    result = generate_task_chapter_content(output_root, task_id)
    status = {
        "task_id": task_id,
        "mode": "real-windowed",
        "result_group_path": str(result.result_group_path.resolve()),
        "content_path": str(result.content_path.resolve()),
        "execution_report_path": str(result.execution_report_path.resolve()),
        "chapter_count": result.chapter_count,
        "window_count": result.window_count,
        "merge_count": result.merge_count,
        "supplier_request_count": result.supplier_request_count,
        "cache_hit_count": result.cache_hit_count,
        "all_cache_hit": result.all_cache_hit,
        "result_group_cache_hit": result.result_group_cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print(
        "\n正式章节内容与结果组缓存已命中。"
        if result.all_cache_hit
        else "\n正式章节内容生成与结果组发布完成。"
    )
    return 0


def _reconcile_chapter_content_audits(task_id: str, output_root: str) -> int:
    result = reconcile_task_chapter_content_audits(output_root, task_id)
    status = {
        "task_id": task_id,
        "mode": "offline-audit-reconciliation",
        "reconciliation_path": str(result.reconciliation_path.resolve()),
        "completed_call_count": result.completed_call_count,
        "inspected_audit_count": result.inspected_audit_count,
        "superseded_count": result.superseded_count,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n陈旧失败审计关系缓存已命中。" if result.cache_hit else "\n陈旧失败审计关系记录完成。")
    return 0


def _recover_chapter_content_candidate_offline(
    task_id: str, output_root: str
) -> int:
    result = recover_task_chapter_content_candidate_offline(output_root, task_id)
    status = {
        "task_id": task_id,
        "mode": "offline-candidate",
        "candidate_path": str(result.candidate_path.resolve()),
        "recovery_audit_path": str(result.recovery_audit_path.resolve()),
        "changed_field_count": result.changed_field_count,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n第五章离线候选缓存已命中。" if result.cache_hit else "\n第五章离线候选生成完成。")
    return 0


def _assemble_chapter_content_offline(task_id: str, output_root: str) -> int:
    result = assemble_task_chapter_content_offline(output_root, task_id)
    status = {
        "task_id": task_id,
        "mode": "real-windowed-offline-recovery",
        "result_id": result.result_id,
        "result_group_path": str(result.result_group_path.resolve()),
        "content_path": str(result.content_path.resolve()),
        "execution_report_path": str(result.execution_report_path.resolve()),
        "chapter_count": result.chapter_count,
        "api_request_count": 0,
        "result_group_cache_hit": result.result_group_cache_hit,
        "top_level_content_cache_hit": result.top_level_content_cache_hit,
        "top_level_report_cache_hit": result.top_level_report_cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n正式章节内容离线恢复缓存已命中。" if result.result_group_cache_hit else "\n正式章节内容离线恢复发布完成。")
    return 0


def _prepare_chapter_content_semantic_repair(
    task_id: str, output_root: str
) -> int:
    result = prepare_task_chapter_content_semantic_repair_candidate(
        output_root, task_id
    )
    status = {
        "task_id": task_id,
        "mode": "offline-semantic-repair-candidate",
        "candidate_path": str(result.candidate_path.resolve()),
        "repair_audit_path": str(result.repair_audit_path.resolve()),
        "manifest_path": str(result.manifest_path.resolve()),
        "changed_text_path_count": result.changed_text_path_count,
        "api_request_count": result.api_request_count,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n单字段语义修复候选缓存已命中。" if result.cache_hit else "\n单字段语义修复候选生成完成。")
    return 0


def _publish_chapter_content_semantic_repair(
    task_id: str, output_root: str
) -> int:
    result = publish_task_chapter_content_semantic_repair(output_root, task_id)
    status = {
        "task_id": task_id,
        "mode": "offline-semantic-repair-formal-publish",
        "result_id": result.result_id,
        "result_group_path": str(result.result_group_path.resolve()),
        "content_path": str(result.content_path.resolve()),
        "execution_report_path": str(result.execution_report_path.resolve()),
        "chapter_count": result.chapter_count,
        "api_request_count": result.api_request_count,
        "result_group_cache_hit": result.result_group_cache_hit,
        "top_level_content_cache_hit": result.top_level_content_cache_hit,
        "top_level_report_cache_hit": result.top_level_report_cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print(
        "\n语义修复正式结果与顶层别名缓存已命中。"
        if result.result_group_cache_hit
        and result.top_level_content_cache_hit
        and result.top_level_report_cache_hit
        else "\n语义修复正式结果离线发布完成。"
    )
    return 0


def _export_document(task_id: str, output_root: str) -> int:
    source = load_authoritative_document_content_v2(output_root, task_id)
    result = publish_document_export_v2(output_root, source)
    status = {
        "task_id": task_id,
        "mode": "offline-three-format-export",
        "export_id": result.export_id,
        "cache_key": result.cache_key,
        "result_group_path": str(result.group_path.resolve()),
        "result_json_path": str((result.group_path / "result.json").resolve()),
        "result_markdown_path": str((result.group_path / "result.md").resolve()),
        "result_docx_path": str((result.group_path / "result.docx").resolve()),
        "manifest_path": str((result.group_path / "manifest.json").resolve()),
        "current_path": str(result.current_path.resolve()),
        "chapter_count": result.manifest["chapter_count"],
        "block_count": result.manifest["block_count"],
        "semantic_projection_sha256": result.manifest[
            "semantic_projection_sha256"
        ],
        "api_request_count": 0,
        "cache_hit": result.cache_hit,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n三格式文档结果组缓存已命中。" if result.cache_hit else "\n三格式文档结果组导出完成。")
    return 0


def _run_m1(task_id: str, output_root: str) -> int:
    result = run_task_m1(output_root, task_id)
    status = {
        "task_id": result.task_id,
        "mode": "zero-api-cache-replay",
        "content_id": result.content_id,
        "export_id": result.export_id,
        "result_group_path": str(result.result_group_path.resolve()),
        "result_json_path": str(result.result_json_path.resolve()),
        "result_markdown_path": str(result.result_markdown_path.resolve()),
        "result_docx_path": str(result.result_docx_path.resolve()),
        "chapter_count": result.chapter_count,
        "event_count": result.event_count,
        "cache_hits": {
            "audio": result.audio_cache_hit,
            "asr": result.asr_cache_hit,
            "timeline": result.timeline_cache_hit,
            "chapter_inputs": result.chapter_inputs_cache_hit,
            "stage9_v1_validated": result.stage9_v1_validated,
            "stage9_v2": result.stage9_v2_cache_hit,
            "export": result.export_cache_hit,
        },
        "api_request_count": result.api_request_count,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\nM1全链零API缓存复验通过。")
    return 0


def _run_asr_only(args: argparse.Namespace) -> int:
    def progress(stage: str, state: str) -> None:
        print(
            json.dumps(
                {"stage": stage, "status": state},
                ensure_ascii=False,
            ),
            flush=True,
        )

    result = run_task_asr_only(
        args.output_root,
        args.task_id,
        source_video=args.source_video,
        ffmpeg_dir=args.ffmpeg_dir,
        allow_llm=args.allow_llm,
        asr_model=args.asr_model,
        asr_device=args.asr_device,
        asr_compute_type=args.asr_compute_type,
        document_profile=args.document_profile,
        max_content_calls=args.max_content_calls,
        progress=progress,
    )
    status = {
        "task_id": result.task_id,
        "mode": "generic-asr-only-2.0",
        "source_video": str(result.source_video),
        "export_id": result.export_id,
        "content_id": result.content_id,
        "review_status": result.review_status,
        "document_profile": result.document_profile,
        "result_group_path": str(result.result_group_path.resolve()),
        "result_json_path": str(result.result_json_path.resolve()),
        "result_markdown_path": str(result.result_markdown_path.resolve()),
        "result_docx_path": str(result.result_docx_path.resolve()),
        "chapter_count": result.chapter_count,
        "event_count": result.event_count,
        "runtime_calls": {
            "boundary_business": result.boundary_business_call_count,
            "content_business": result.content_business_call_count,
            "document_v2_business": result.document_v2_business_call_count,
            "supplier_requests": result.supplier_request_count,
        },
        "content_planned_call_count": result.content_planned_call_count,
        "document_v2_planned_call_count": result.document_v2_planned_call_count,
        "all_llm_cache_hit": result.all_llm_cache_hit,
        "export_cache_hit": result.export_cache_hit,
        "reused_authoritative_content": result.reused_authoritative_content,
        "reused_document_v2_candidate": result.reused_document_v2_candidate,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\nASR-only一键链路完成，已生成2.0 JSON、Markdown和Word待复核候选。")
    return 0


def _run_m2(args: argparse.Namespace) -> int:
    """run-m2处理器（裁决§三.6/§三.7）。

    - stdout逐阶段JSON进度行（预算角色在首个调用前追加带``input_budget``的行）；
    - ``--dry-run``：stdout单条运行计划JSON（``M2RunPlan``形态），退出0；
    - 普通运行成功：stdout单条成功摘要JSON（P4 §5.3字面8键）；
    - 条目失败（``M2PipelineError``）与模块类型化失败的原码包裹
      （``M2EnvelopeFailure``）：stderr单条7键JSON envelope、退出1。
    """

    def progress(
        stage: str, state: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        data: dict[str, Any] = {"stage": stage, "status": state}
        if payload:
            data.update(payload)
        print(json.dumps(data, ensure_ascii=False), flush=True)

    try:
        result = run_task_m2(
            args.output_root,
            args.task_id,
            source_video=args.source_video,
            ffmpeg_dir=args.ffmpeg_dir,
            ocr_mode=args.ocr,
            vision_caption=args.vision_caption,
            model_registry=args.model_registry,
            allow_llm=args.allow_llm,
            dry_run=args.dry_run,
            **({"bounded_content": True} if args.bounded_content else {}),
            progress=progress,
        )
    except M2PipelineError as exc:
        _print_status(
            {
                "status": "failed",
                "task_id": args.task_id,
                "failed_stage": exc.failed_stage,
                "error_code": exc.error_code,
                "reason_codes": list(exc.reason_codes),
                "relative_paths": [],
                "message": _safe_cli_message(str(exc), (args.output_root,)),
            },
            failed=True,
        )
        return 1
    except M2EnvelopeFailure as failure:
        _print_status(
            {
                "status": "failed",
                "task_id": args.task_id,
                "failed_stage": failure.failed_stage,
                "error_code": failure.error_code,
                "reason_codes": list(failure.reason_codes),
                "relative_paths": [],
                "message": _safe_cli_message(failure.message, (args.output_root,)),
            },
            failed=True,
        )
        return 1
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
    return 0


_ABSOLUTE_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/][^\s，。；：、（）]*"
    r"|\\\\[^\s，。；：、（）]*"
)


def _safe_cli_message(message: str, roots: Sequence[str | Path] = ()) -> str:
    """按P4失败边界要求输出错误信息：先精确替换已知根目录，再抹掉剩余绝对路径。"""

    text = message
    for root in roots:
        for form in {str(root), str(Path(root).expanduser())}:
            if form:
                text = text.replace(form, "<path>")
    return _ABSOLUTE_PATH_PATTERN.sub("<path>", text)


def _module_failure_status(
    task_id: str,
    module: str,
    error: BaseException,
    roots: Sequence[str | Path],
) -> dict[str, Any]:
    """只汇报模块已经定型的失败事实，不复制任何业务逻辑。"""

    status = {
        "task_id": task_id,
        "module": module,
        "status": "failed",
        "error_code": str(getattr(error, "error_code", "")),
        "safe_message": _safe_cli_message(str(error), roots),
        "recoverable": bool(getattr(error, "recoverable", False)),
        "next_action": str(getattr(error, "next_action", "none")),
    }
    domain = getattr(error, "domain", None)
    if domain is not None:
        # 仅chapter_frames的current类失败携带结构化domain（裁决Q5.8）。
        status["domain"] = str(domain)
    return status


def _print_status(status: Mapping[str, Any], *, failed: bool) -> None:
    print(
        json.dumps(status, ensure_ascii=False, indent=2),
        file=sys.stderr if failed else sys.stdout,
    )


def _sample_frames(
    task_id: str,
    output_root: str,
    profile_id: str,
    ffmpeg_dir: str | None,
) -> int:
    try:
        result = sample_frames(
            task_id,
            output_root=output_root,
            profile_id=profile_id,
            ffmpeg_dir=ffmpeg_dir,
        )
    except FrameSamplerError as exc:
        # KeyboardInterrupt和SystemExit不是FrameSamplerError，按模块约定原样传播。
        _print_status(
            _module_failure_status(task_id, FRAME_MODULE_TOKEN, exc, (output_root,)),
            failed=True,
        )
        return 1
    frame_set_id = str(result.index["frame_set_id"])
    status = {
        "task_id": task_id,
        "module": FRAME_MODULE_TOKEN,
        "status": "ok",
        "frame_set_id": frame_set_id,
        "profile_id": str(result.index["profile_id"]),
        "frame_count": int(result.index["frame_count"]),
        "group_relative_path": (
            f"m2/{FRAME_MODULE_TOKEN}/{FRAME_GROUP_DIRECTORY}/{frame_set_id}"
        ),
        "cache_hit": bool(result.cache_hit),
        "pointer_repaired": bool(result.pointer_repaired),
    }
    _print_status(status, failed=False)
    return 0


def _detect_page_changes(task_id: str, output_root: str) -> int:
    try:
        result = detect_page_changes(task_id, output_root=output_root)
    except PageChangeDetectorError as exc:
        # KeyboardInterrupt和SystemExit不是PageChangeDetectorError，原样传播。
        _print_status(
            _module_failure_status(
                task_id, PAGE_CHANGE_MODULE_TOKEN, exc, (output_root,)
            ),
            failed=True,
        )
        return 1
    page_change_set_id = str(result.index["page_change_set_id"])
    status = {
        "task_id": task_id,
        "module": PAGE_CHANGE_MODULE_TOKEN,
        "status": "ok",
        "page_change_set_id": page_change_set_id,
        "profile_id": str(result.index["profile_id"]),
        "frame_set_id": str(result.index["input_frame_set"]["frame_set_id"]),
        "comparison_count": int(result.index["comparison_count"]),
        "candidate_count": int(result.index["candidate_count"]),
        "group_relative_path": (
            f"m2/{FRAME_MODULE_TOKEN}/{PAGE_CHANGE_ROOT_DIRECTORY}/"
            f"{PAGE_CHANGE_GROUP_DIRECTORY}/{page_change_set_id}"
        ),
        "cache_hit": bool(result.cache_hit),
        "pointer_repaired": bool(result.pointer_repaired),
    }
    _print_status(status, failed=False)
    return 0


def _run_ocr(task_id: str, output_root: str, mode: str) -> int:
    try:
        result = run_ocr(task_id, output_root=output_root, mode=mode)
    except OcrExtractorError as exc:
        # KeyboardInterrupt和SystemExit不是OcrExtractorError，按模块约定原样传播。
        _print_status(
            _module_failure_status(task_id, OCR_MODULE_TOKEN, exc, (output_root,)),
            failed=True,
        )
        return 1
    if isinstance(result, OcrDegradedOutcome):
        # auto引擎级失败：不发布新组、current不变、审计已写；文本链继续，退出码0。
        _print_status(
            {
                "task_id": result.task_id,
                "module": OCR_MODULE_TOKEN,
                "status": "degraded",
                "mode": result.mode,
                "reason_codes": list(result.reason_codes),
                "failure_id": result.failure_id,
                "error_code": result.error_code,
                "recoverable": True,
                "next_action": "continue_text_pipeline",
            },
            failed=False,
        )
        return 0
    ocr_set_id = str(result.index["ocr_set_id"])
    engine = result.index["engine"]
    status = {
        "task_id": task_id,
        "module": OCR_MODULE_TOKEN,
        "status": str(result.index["status"]),
        "ocr_set_id": ocr_set_id,
        "mode": str(result.index["mode"]),
        "profile_id": str(result.index["profile_id"]),
        "engine_id": engine.get("engine_id"),
        "engine_state": engine.get("state"),
        "frame_count": int(result.index["frame_count"]),
        "line_count": int(result.index["line_count"]),
        "deduplicated_count": int(result.index["deduplicated_count"]),
        "reason_codes": list(result.index["reason_codes"]),
        "group_relative_path": (
            f"m2/{OCR_ROOT_DIRECTORY}/{OCR_GROUP_DIRECTORY}/{ocr_set_id}"
        ),
        "cache_hit": bool(result.cache_hit),
        "pointer_repaired": bool(result.pointer_repaired),
    }
    _print_status(status, failed=False)
    return 0


def _build_timeline_v2(task_id: str, output_root: str, ocr: str) -> int:
    try:
        result = build_timeline_v2(task_id, output_root=output_root, ocr=ocr)
    except TimelineBuilderV2Error as exc:
        # KeyboardInterrupt和SystemExit不是TimelineBuilderV2Error，按模块约定原样传播。
        _print_status(
            _module_failure_status(task_id, TIMELINE_MODULE_TOKEN, exc, (output_root,)),
            failed=True,
        )
        return 1
    index = result.index
    timeline_id = str(index["timeline_id"])
    event_counts = result.manifest["event_counts"]
    modalities = index["modalities"]
    status = {
        "task_id": task_id,
        "module": TIMELINE_MODULE_TOKEN,
        "status": str(index["run_status"]),
        "timeline_id": timeline_id,
        "quality_level": str(index["quality_level"]),
        "ocr_request": ocr,
        "duration_ms": int(index["duration_ms"]),
        "event_counts": {
            "total": int(event_counts["total"]),
            "transcript": int(event_counts["transcript"]),
            "page_change": int(event_counts["page_change"]),
            "ocr": int(event_counts["ocr"]),
        },
        "modality_statuses": {
            name: str(modalities[name]["status"])
            for name in ("transcript", "frame_change", "ocr")
        },
        "group_relative_path": (
            f"m2/{TIMELINE_ROOT_DIRECTORY}/{TIMELINE_GROUP_DIRECTORY}/{timeline_id}"
        ),
        "cache_hit": bool(result.cache_hit),
        "pointer_repaired": bool(result.pointer_repaired),
    }
    _print_status(status, failed=False)
    return 0


def _plan_chapters_v2(task_id: str, output_root: str, model_registry: str | None) -> int:
    try:
        result = plan_chapters_v2(
            task_id, output_root=output_root, model_registry=model_registry
        )
    except BoundaryPlannerV2Error as exc:
        # KeyboardInterrupt和SystemExit不是BoundaryPlannerV2Error，按模块约定原样传播。
        _print_status(
            _module_failure_status(task_id, BOUNDARY_MODULE_TOKEN, exc, (output_root,)),
            failed=True,
        )
        return 1
    status = {
        "task_id": task_id,
        "module": BOUNDARY_MODULE_TOKEN,
        "status": result.status,
        "result_id": result.result_id,
        "result_source": result.result_source,
        "quality_level": result.quality_level,
        "boundary_count": int(result.boundary_count),
        "duration_ms": int(result.duration_ms),
        "timeline_id": result.timeline_id,
        "business_call_count": int(result.business_call_count),
        "supplier_request_count": int(result.supplier_request_count),
        "cache_hit": bool(result.cache_hit),
        "pointer_repaired": bool(result.pointer_repaired),
        "group_relative_path": (
            f"m2/{BOUNDARY_ROOT_DIRECTORY}/{BOUNDARY_GROUP_DIRECTORY}/{result.result_id}"
        ),
    }
    _print_status(status, failed=False)
    return 0


def _select_chapter_frames(task_id: str, output_root: str) -> int:
    try:
        result = select_chapter_frames(task_id, output_root=output_root)
    except ChapterFramesError as exc:
        # KeyboardInterrupt和SystemExit不是ChapterFramesError，按模块约定原样传播。
        _print_status(
            _module_failure_status(
                task_id, CHAPTER_FRAMES_MODULE_TOKEN, exc, (output_root,)
            ),
            failed=True,
        )
        return 1
    status = {
        "task_id": task_id,
        "module": CHAPTER_FRAMES_MODULE_TOKEN,
        "status": "success",
        "result_id": result.result_id,
        "chapter_count": int(result.chapter_count),
        "selected_frame_count": int(result.selected_frame_count),
        "chapters_without_frames": int(result.chapters_without_frames),
        "cache_hit": bool(result.cache_hit),
        "pointer_repaired": bool(result.pointer_repaired),
        "group_relative_path": (
            f"m2/{CHAPTER_FRAMES_ROOT_DIRECTORY}/"
            f"{CHAPTER_FRAMES_GROUP_DIRECTORY}/{result.result_id}"
        ),
    }
    _print_status(status, failed=False)
    return 0


def _compose_document_v2(task_id: str, output_root: str, model_registry: str | None, bounded_content: bool = False) -> int:
    try:
        result = compose_document_v2(
            output_root, task_id, model_registry=model_registry,
            **({"bounded": True, "preserve_published": True} if bounded_content else {}),
        )
    except DocumentComposerV2Error as exc:
        # KeyboardInterrupt和SystemExit不是DocumentComposerV2Error，按模块约定原样传播。
        _print_status(
            _module_failure_status(task_id, CONTENT_MODULE_TOKEN, exc, (output_root,)),
            failed=True,
        )
        return 1
    status = {
        "task_id": task_id,
        "module": CONTENT_MODULE_TOKEN,
        "status": "success",
        "result_id": result.result_id,
        "content_id": str(result.content["content_id"]),
        "chapter_count": int(result.chapter_count),
        "plan_result_source": result.plan_result_source,
        "plan_quality_level": result.plan_quality_level,
        "business_call_count": int(result.business_call_count),
        "supplier_request_count": int(result.supplier_request_count),
        "cache_hit_count": int(result.cache_hit_count),
        "cache_hit": bool(result.cache_hit),
        "pointer_repaired": bool(result.pointer_repaired),
        "group_relative_path": (
            f"m2/{CONTENT_ROOT_DIRECTORY}/{CONTENT_GROUP_DIRECTORY}/{result.result_id}"
        ),
    }
    _print_status(status, failed=False)
    return 0


def _export_document_v2(task_id: str, output_root: str) -> int:
    try:
        result = export_document_v2(output_root, task_id)
    except DocumentExporterV2Error as exc:
        # KeyboardInterrupt和SystemExit不是DocumentExporterV2Error，按模块约定原样传播。
        _print_status(
            _module_failure_status(task_id, EXPORT_MODULE_TOKEN, exc, (output_root,)),
            failed=True,
        )
        return 1
    _print_status(result.summary(), failed=False)
    return 0


def _plan_chapters(
    task_id: str,
    output_root: str,
    min_chapter_seconds: float | None,
    pipeline: str,
    alignment_window_seconds: float,
    experiment_id: str | None,
) -> int:
    if pipeline == "two-stage":
        result = plan_task_chapters_two_stage(
            output_root,
            task_id,
            min_chapter_seconds=min_chapter_seconds,
            window_seconds=alignment_window_seconds,
            experiment_id=experiment_id,
        )
    elif experiment_id is not None:
        result = plan_task_chapters(
            output_root,
            task_id,
            min_chapter_seconds=min_chapter_seconds,
            experiment_id=experiment_id,
        )
    else:
        if min_chapter_seconds is not None and (
            abs(min_chapter_seconds - STABILITY_MIN_CHAPTER_SECONDS) > 1e-9
        ):
            raise ChapterPlanError(
                f"冻结A正式入口的最小章节时长固定为"
                f"{STABILITY_MIN_CHAPTER_SECONDS:g}秒。"
            )
        result = plan_task_chapters_formal(output_root, task_id)
    status = {
        "task_id": task_id,
        "strategy": "llm",
        "pipeline": pipeline,
        "experiment_id": experiment_id,
        "chapter_plan_path": str(result.plan_path.resolve()),
        "boundary_count": result.boundary_count,
        "duration_seconds": result.duration_seconds,
        "min_chapter_seconds": result.min_chapter_seconds,
        "plan_cache_hit": result.plan_cache_hit,
    }
    if hasattr(result, "result_source"):
        status.update(
            {
                "status": result.status,
                "result_source": result.result_source,
                "quality_level": result.quality_level,
                "business_call_count": result.business_call_count,
                "supplier_request_count": result.supplier_request_count,
                "execution_report_path": str(
                    result.execution_report_path.resolve()
                ),
                "result_group_path": str(result.result_group_path.resolve()),
            }
        )
    else:
        status.update(
            {
                "attempt_count": result.attempt_count,
                "llm_cache_hit": result.llm_cache_hit,
            }
        )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    fully_cached = result.plan_cache_hit and (
        result.business_call_count == 0
        if hasattr(result, "business_call_count")
        else result.llm_cache_hit
    )
    print(
        "\nLLM章节计划缓存已命中。"
        if fully_cached
        else "\nLLM章节计划生成完成。"
    )
    return 0


def _review_chapters(task_id: str, output_root: str, source_run_id: str) -> int:
    result = review_task_chapters(
        output_root, task_id, source_run_id=source_run_id
    )
    status = {
        "task_id": task_id,
        "source_run_id": source_run_id,
        "status": result.status,
        "selected_plan_path": str(result.selected_plan_path.resolve()),
        "reviewed_plan_path": (
            str(result.reviewed_plan_path.resolve())
            if result.reviewed_plan_path is not None
            else None
        ),
        "audit_path": str(result.audit_path.resolve()),
        "verdict": result.verdict,
        "issue_count": result.issue_count,
        "attempt_count": result.attempt_count,
        "llm_cache_hit": result.llm_cache_hit,
        "plan_cache_hit": result.plan_cache_hit,
        "fallback_used": result.fallback_used,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print("\n审阅完成。" if result.status == "completed" else "\n审阅失败，已回退来源A计划。")
    return 0 if result.status == "completed" else 1


def _review_chapters_v2(task_id: str, output_root: str, source_run_id: str) -> int:
    result = review_task_chapters_v2(
        output_root, task_id, source_run_id=source_run_id
    )
    status = {
        "task_id": task_id,
        "source_run_id": source_run_id,
        "review_version": "v2",
        "status": result.status,
        "selected_plan_path": str(result.selected_plan_path.resolve()),
        "reviewed_plan_path": (
            str(result.reviewed_plan_path.resolve())
            if result.reviewed_plan_path is not None
            else None
        ),
        "audit_path": str(result.audit_path.resolve()),
        "verdict": result.verdict,
        "issue_count": result.issue_count,
        "attempt_count": result.attempt_count,
        "llm_cache_hit": result.llm_cache_hit,
        "plan_cache_hit": result.plan_cache_hit,
        "fallback_used": result.fallback_used,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    print(
        "\nReview v2审阅完成。"
        if result.status == "completed"
        else "\nReview v2审阅失败，已回退来源A计划。"
    )
    return 0 if result.status == "completed" else 1


def _plan_chapters_anchor_v2(task_id: str, output_root: str, run_id: str) -> int:
    try:
        result = plan_task_chapters_anchor_v2(
            output_root, task_id, run_id=run_id
        )
    except TopicAnchorError as exc:
        status = {
            "task_id": task_id,
            "run_id": run_id,
            "mode": "experiment",
            "status": "failed",
            "result_source": "none",
            "quality_level": "unknown",
            "failure_type": exc.failure_type,
            "failure_reason": str(exc),
            "audit_path": str(exc.audit_path.resolve()) if exc.audit_path else None,
            "recovery_triggered": False,
            "fallback_triggered": False,
            "emergency_triggered": False,
        }
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 1
    status = {
        "task_id": task_id,
        "run_id": run_id,
        "mode": "experiment",
        "status": result.status,
        "result_source": result.result_source,
        "quality_level": result.quality_level,
        "chapter_plan_path": str(result.plan_path.resolve()),
        "boundary_count": result.boundary_count,
        "duration_seconds": result.duration_seconds,
        "business_call_count": result.business_call_count,
        "provider_request_count": result.provider_request_count,
        "correction_count": result.correction_count,
        "llm_cache_hit_count": result.llm_cache_hit_count,
        "artifact_cache_hit": result.artifact_cache_hit,
        "recovery_triggered": False,
        "fallback_triggered": False,
        "emergency_triggered": False,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command in ("run-m2", "plan-chapters-v2", "compose-document-v2"):
        # Team quickstart uses a local .env; exported environment always wins.
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env", override=False)
    try:
        if args.command == "check-env":
            return _check_environment()
        if args.command == "inspect":
            return _inspect_video(args.video, args.output)
        if args.command == "init-task":
            return _initialize_task(
                args.task_id,
                args.output_root,
                args.source_video,
            )
        if args.command == "extract-audio":
            return _extract_audio(args.task_id, args.output_root)
        if args.command == "transcribe":
            return _transcribe(
                args.task_id,
                args.output_root,
                args.model,
                args.device,
                args.compute_type,
            )
        if args.command == "build-timeline":
            return _build_timeline(args.task_id, args.output_root)
        if args.command == "query-timeline":
            return _query_timeline(
                args.task_id,
                args.output_root,
                args.start,
                args.end,
                args.format,
            )
        if args.command == "call-llm":
            return _call_llm(
                args.task_id,
                args.output_root,
                args.call_id,
                args.request_file,
            )
        if args.command == "plan-chapters":
            return _plan_chapters(
                args.task_id,
                args.output_root,
                args.min_chapter_seconds,
                args.pipeline,
                args.alignment_window_seconds,
                args.experiment_id,
            )
        if args.command == "review-chapters":
            return _review_chapters(
                args.task_id, args.output_root, args.source_run_id
            )
        if args.command == "review-chapters-v2":
            return _review_chapters_v2(
                args.task_id, args.output_root, args.source_run_id
            )
        if args.command == "plan-chapters-anchor-v2":
            return _plan_chapters_anchor_v2(
                args.task_id, args.output_root, args.run_id
            )
        if args.command == "prepare-chapter-inputs":
            return _prepare_chapter_inputs(args.task_id, args.output_root)
        if args.command == "generate-chapter-content-mock":
            return _generate_chapter_content_mock(args.task_id, args.output_root)
        if args.command == "generate-chapter-content-windowed-mock":
            return _generate_chapter_content_windowed_mock(
                args.task_id, args.output_root
            )
        if args.command == "generate-chapter-content":
            return _generate_chapter_content(args.task_id, args.output_root)
        if args.command == "reconcile-chapter-content-audits":
            return _reconcile_chapter_content_audits(
                args.task_id, args.output_root
            )
        if args.command == "recover-chapter-content-candidate-offline":
            return _recover_chapter_content_candidate_offline(
                args.task_id, args.output_root
            )
        if args.command == "assemble-chapter-content-offline":
            return _assemble_chapter_content_offline(
                args.task_id, args.output_root
            )
        if args.command == "prepare-chapter-content-semantic-repair":
            return _prepare_chapter_content_semantic_repair(
                args.task_id, args.output_root
            )
        if args.command == "publish-chapter-content-semantic-repair":
            return _publish_chapter_content_semantic_repair(
                args.task_id, args.output_root
            )
        if args.command == "export-document":
            return _export_document(args.task_id, args.output_root)
        if args.command == "run-m1":
            return _run_m1(args.task_id, args.output_root)
        if args.command == "run":
            return _run_asr_only(args)
        if args.command == "sample-frames":
            return _sample_frames(
                args.task_id,
                args.output_root,
                args.profile,
                args.ffmpeg_dir,
            )
        if args.command == "detect-page-changes":
            return _detect_page_changes(args.task_id, args.output_root)
        if args.command == "run-ocr":
            return _run_ocr(args.task_id, args.output_root, args.mode)
        if args.command == "build-timeline-v2":
            return _build_timeline_v2(args.task_id, args.output_root, args.ocr)
        if args.command == "plan-chapters-v2":
            return _plan_chapters_v2(args.task_id, args.output_root, args.model_registry)
        if args.command == "select-chapter-frames":
            return _select_chapter_frames(args.task_id, args.output_root)
        if args.command == "compose-document-v2":
            return _compose_document_v2(
                args.task_id, args.output_root, args.model_registry, args.bounded_content
            )
        if args.command == "export-document-v2":
            return _export_document_v2(args.task_id, args.output_root)
        if args.command == "run-m2":
            return _run_m2(args)
    except (
        ASROnlyPipelineError,
        ASRDocumentV2Error,
        ASRTimelineError,
        ASRTranscriptionError,
        AudioExtractionError,
        ChapterContentError,
        DocumentExportError,
        DocumentExportV2Error,
        DocumentCompositionError,
        DocumentContentPreviewError,
        FactCardCompositionError,
        ChapterReviewError,
        ChapterPlanError,
        LLMAdapterError,
        M1PipelineError,
        TaskWorkspaceError,
        VideoProbeError,
    ) as exc:
        print(f"处理失败：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
