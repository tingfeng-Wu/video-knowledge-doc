"""run-m2最薄编排入口——阶段15-0设计裁决§三/§四的E0+E1+E2+E3落地层。

本模块当前落地（裁决§四，实现轮按小任务闸门拆E0+E1、E2+E3）：

- **E0**：参数面校验、运行计划（``M2RunPlan``）JSON形态与数据类、条目类型化失败
  ``M2PipelineError``的封闭码集（裁决§三.7）、输入预算策略常量
  ``m2-input-budget-policy-1.0``与纯函数判定``evaluate_input_budget``（裁决§一Q1）。
- **E1**：只读preflight``preflight_task_m2``（裁决§三.8两分支）：源身份校验复用
  ``asr_only_pipeline._resolve_source``既有语义，媒体工具三层发现复用
  ``resolve_media_tools``；全程零写入（E01 fail_before_write），不探测性写文件。
- **E2**：零写入dry-run。``_assemble_run_plan``按裁决§三.9依赖边正向推进逐阶段
  disposition（各域current指针级只读复验，复用各模块既有current读取函数；缓存身份
  全量复验由模块在普通运行的早退路径完成——P4 §5.3「编排不复制业务逻辑」），
  预算角色按§三.3呈现``input_budget``（不可测属呈现非失败）；探测媒体流
  （``probe_video``）先于任何task_init写入（审查复核OB-2约束，dry-run与普通路径皆然）。
- **E3**：十二阶段全链编排``run_task_m2``普通路径：按§三.6输出形态（stdout逐阶段
  JSON进度行＋终态单条摘要），P6 §六.3/裁决§一在每角色首个供应商请求前完成
  审批门与预算门（``measure_boundary_egress``/``build_content_composition_dry_run``
  入口测量，模块零感知）；脱敏append-only JSONL日志（``m2/logs/run-m2.jsonl``，
  LF铁律，P4 §6.8口径）；模块类型化失败原码包裹入§三.7同一envelope
  （``M2EnvelopeFailure``为包裹事实载体，不复制模块码表、不新增条目错误码）。

实现决定（裁决沉默处的实现选择，登记供G1「提问单→裁决文档→实现diff」三点一致性核对）：

1. 「源不可用」形态映射：源文件缺失/不可读/零字节与``task.json``未记录源路径，
   映射入封闭码集既有码``SOURCE_VIDEO_REQUIRED``，以``reason_codes``细化
   （``SOURCE_FILE_UNREADABLE``/``SOURCE_FILE_EMPTY``/``SOURCE_VIDEO_UNRECORDED``），
   不新增条目错误码；E01/E04的fail_before_write语义由映射保持。
2. 裁决§三.8的preflight检查面为封闭清单（源身份、媒体工具、``task.json``可解析、
   ``os.access``可写性），未含ffprobe探测；故preflight不跑``probe_video``，时长/音轨
   等输入事实的探测延后到E2计划装配（同样零写）。无音轨的硬失败沿用既有类型化族
   ``ASROnlyPipelineError``（§三.7原码包裹，该族消息内嵌码文，envelope的
   ``error_code``取模块定型属性、缺省为空串），条目层不发明``NO_AUDIO``类码。
   输出目录不可写复用既有类型化族``TaskWorkspaceError``（同样不新增条目码）。
3. E2/E3：「成功摘要九键」按P4 §5.3字面枚举8键落地
   （``status/task_id/result_ids/cache_hits/degraded/reason_codes/relative_paths/
   call_counts``）。P4 §5.3的字面键列表恰为这8项；「九键」系提问单起的计数标签、
   裁决沿用（先例：14-A §五.4「13个」计数笔误被G1按表格字面接受）。不发明第9键
   （§六.3禁止新增）。摘要各值类型（裁决只给键集与映射来源）：``status``取阶段词表
   终值``completed``；``degraded``为进入degraded状态的阶段名列表；``reason_codes``
   为模块既有词表值（含裁决§三.6字面授权的``OCR_DISABLED``）的聚合；
   ``relative_paths``为域→组相对路径映射；``cache_hits``键＝9个产物域
   （裁决§三.2/§三.9映射覆盖的阶段名），``preflight/task_init/completed``为
   编排标记不入。
4. E2 disposition语义：``cache_hit``＝该域current指针级复验通过（存在、身份有效、
   目标组manifest哈希匹配，复用各模块既有读取函数）且全部上游满足；``will_run``＝
   上游满足但本域无current（或非预算阶段）；``unmeasurable``＝预算角色阶段的上游
   未满足、度量输入不可构造（呈现``input_budget_status=unknown``，§三.3）；
   ``disabled``＝``ocr=off``（其余阶段恒非disabled）。上游未满足而本域组存在时
   记``will_run``/``unmeasurable``（普通运行将按新身份重发布）。缓存早退的最终事实
   以模块运行的身份复验为准；dry-run不复算缓存身份（P4 §5.3编排不复制业务逻辑）。
5. dry-run遇到current指针损坏：与§三.9「按P4损坏硬失败报告并停止」同口径，
   模块类型化损坏错误原码包裹入envelope并退出1；§三.3「除用法错误与preflight硬失败
   外退出0」按健康树语义理解，损坏缓存属E01 fail-before-write族的硬失败边界
   （零写入保持）。
6. 普通运行预算门的测量构造抛错分流：域输入缺失/损坏错误（模块类型化的
   ``Input*Missing/Corrupt``族）保留原码包裹（P4损坏/缺失硬失败语义）；其余意外
   抛错按裁决§一1.5「构造抛错→``INPUT_BUDGET_UNKNOWN``停止」执行条目失败。
7. E3(i)类离线端到端注入 seam：``run_task_m2``关键字参数``_transports``
   （角色→传输对象映射，CLI永不传递、不入命令面，先例：各模块``_engine_factory``/
   ``failure_injection``/``_proxy_loader``私有注入口）。普通运行缺省时：计划调用>0
   且``--allow-llm``→传None由模块构造真实传输（模块自身计数即§三.6的transport计数，
   经结果类型化字段``business_call_count``/``supplier_request_count``回传编排层）；
   未批准或计划0时注入拒绝守卫传输（其``complete``抛条目``LLM_APPROVAL_REQUIRED``），
   杜绝「计划0但身份漂移导致未批准真实请求」残余路径；守卫触发时零网络。
8. JSONL日志：``preflight``/``task_init``之前的条目失败不产生日志文件（E01/E05
   零写保持）；之前的阶段事件缓冲至task_init后落盘。每阶段记录终态事件
   （started不入日志；进度行保留started）。
9. ``measure_boundary_egress``的``model_registry``/``project_root``参数按裁决
   §一1.4字面签名保留，不参与度量（度量对象是messages文本本身）。
10. transcript域无结果组/current形态，而裁决§三.6字面把transcript列入
    ``result_ids``域清单：真实M1 ``timeline/timeline.json``顶层无``timeline_id``
    键（DEV01实测键集为duration_seconds/events/language/schema_version/
    source_asr/task_id），故域标识取``unified_timeline.compute_timeline_id``
    （代码库既有canonical口径``unified-timeline-<sha256前12位>``，对载荷确定性、
    键存在与否均先剔除该键再哈希），经``load_task_timeline``Schema校验后计算。
11. 裁决§三.6字面「ocr=off→null」按摘要呈现层采纳：``--ocr off``时成功摘要
    ``result_ids["ocr"]``恒为``null``且该域不入``relative_paths``——OCR模块对off
    照常发布disabled组（§三.6「status=completed＋reason OCR_DISABLED，模块既有
    disabled语义透传」佐证），组目录与JSONL终态事件中的组身份如实保留，仅条目
    摘要层不暴露该id。
12. ``call_counts.this_run``的cache_hit口径：模块域缓存早退时，结果类型化字段
    ``business_call_count``/``supplier_request_count``承载的是组report中持久化的
    **历史首次生成**计数（14-B/14-C实测）；裁决§三.6「本轮实数」与P6 §八分离裁定
    要求入口不重建未见证的历史数，故域``cache_hit=True``时this_run如实记0，
    本轮新发布时该字段即本轮实数照常上报。E13「缓存复跑this_run全0」据此成立。
13. 审查报告§四B2修正轮登记：裁决§一1.5字面「普通运行在每角色首个调用前打印
    一条进度JSON行并写JSONL同构事件」——带``input_budget``的stdout进度行在预算
    判定成形且停门检查（exceeded/unknown）通过后、模块调用前打印（该字面只明确
    约束stdout行）；JSONL同构事件保持在阶段终态/失败事件时点入日志（与终态的
    ``elapsed_ms``/``result_id``同事件，不在调用前另起started事件、不扩张§三.5
    事件字段集）。该选择按B2修正要求②登记为实现决定。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from video_doc import chapter_frames as _cf_mod
from video_doc import document_composer_v2 as _dc_mod
from video_doc import document_exporter_v2 as _de_mod
from video_doc import ocr_extractor as _ocr_mod
from video_doc import page_change_detector as _pcd_mod
from video_doc.asr_only_pipeline import (
    ASROnlyPipelineError,
    MediaTools,
    _file_sha256,
    _media_tool_path,
    _resolve_source,
    _save_metadata_if_changed,
    resolve_media_tools,
)
from video_doc.asr_timeline import (
    ASRTimelineError,
    build_task_timeline,
    load_task_timeline,
)
from video_doc.asr_transcribe import ASRTranscriptionError, transcribe_task_audio
from video_doc.audio_extract import AudioExtractionError, extract_task_audio
from video_doc.boundary_planner_v2 import (
    BoundaryPlannerV2Error,
    load_chapter_plan_current,
    measure_boundary_egress,
    plan_chapters_v2,
)
from video_doc.chapter_frames import ChapterFramesError, select_chapter_frames
from video_doc.document_composer_v2 import (
    DocumentComposerV2Error,
    build_content_composition_dry_run,
    compose_document_v2,
)
from video_doc.document_exporter_v2 import (
    DocumentExporterV2Error,
    export_document_v2,
)
from video_doc.frame_sampler import (
    FrameSamplerError,
    load_frame_set_current,
    sample_frames,
)
from video_doc.ocr_extractor import (
    OCR_MODES,
    OcrDegradedOutcome,
    OcrExtractorError,
    run_ocr,
)
from video_doc.page_change_detector import PageChangeDetectorError, detect_page_changes
from video_doc.task_workspace import (
    TaskWorkspaceError,
    initialize_task_workspace,
    load_task_manifest,
    validate_task_id,
)
from video_doc.timeline_builder_v2 import (
    TimelineBuilderV2Error,
    build_timeline_v2,
    load_timeline_current,
)
from video_doc.unified_timeline import compute_timeline_id
from video_doc.video_probe import VideoMetadata, VideoProbeError, probe_video

# ---------------------------------------------------------------------------
# E0：命令面词表、阶段列表与封闭失败码（裁决§三.2/§三.4/§三.7）
# ---------------------------------------------------------------------------

M2_ENTRY_ERROR_CODES = frozenset(
    {
        "SOURCE_VIDEO_REQUIRED",
        "SOURCE_VIDEO_MISMATCH",
        "MEDIA_TOOL_NOT_FOUND",
        "MEDIA_TOOL_INVALID",
        "LLM_APPROVAL_REQUIRED",
        "INPUT_BUDGET_UNKNOWN",
        "INPUT_BUDGET_EXCEEDED",
    }
)

_ENTRY_FAILURE_MESSAGES = {
    "SOURCE_VIDEO_REQUIRED": "SOURCE_VIDEO_REQUIRED：源视频参数缺失或对本入口不可用。",
    "SOURCE_VIDEO_MISMATCH": "SOURCE_VIDEO_MISMATCH：提供的视频与已有任务源身份不一致。",
    "MEDIA_TOOL_NOT_FOUND": (
        "MEDIA_TOOL_NOT_FOUND：未同时找到ffmpeg和ffprobe；请使用--ffmpeg-dir指定安装目录。"
    ),
    "MEDIA_TOOL_INVALID": "MEDIA_TOOL_INVALID：媒体工具目录、同目录约束或版本探测不满足。",
    "LLM_APPROVAL_REQUIRED": "LLM_APPROVAL_REQUIRED：存在计划内供应商调用，必须显式--allow-llm批准。",
    "INPUT_BUDGET_UNKNOWN": "INPUT_BUDGET_UNKNOWN：输入预算判定不可得，已在首个供应商请求前停止。",
    "INPUT_BUDGET_EXCEEDED": "INPUT_BUDGET_EXCEEDED：输入预算超出条目策略上限，已在首个供应商请求前停止。",
}

# 裁决§三.2完整顺序（P6 §七「至少」授权增设chapter_frames，插于chapter_plan_v2与
# document_content_v2之间）；transcript覆盖audio/asr/timeline1三子步。
M2_STAGE_SEQUENCE = (
    "preflight",
    "task_init",
    "transcript",
    "frames",
    "page_changes",
    "ocr",
    "timeline_v2",
    "chapter_plan_v2",
    "chapter_frames",
    "document_content_v2",
    "export_v2",
    "completed",
)

M2_ROLE_BY_STAGE = {
    "chapter_plan_v2": "boundary_model",
    "document_content_v2": "content_model",
}

# 裁决§三.3：dry-run逐阶段disposition封闭词表。
M2_PLAN_DISPOSITIONS = frozenset({"cache_hit", "will_run", "unmeasurable", "disabled"})

# P6 §七阶段状态词表（§三.5/§三.6的JSONL与九键摘要沿用，不新增词汇；E3接线）。
M2_STAGE_STATUSES = frozenset(
    {"started", "cache_hit", "completed", "degraded", "failed"}
)

# 裁决§三.4：--vision-caption仅接受off（默认off）；--ocr沿用既有off/auto|required词表。
M2_VISION_CAPTION_VALUES = ("off",)

# ---------------------------------------------------------------------------
# E0：输入预算条目策略（裁决§一Q1，纯 additive，不入任何缓存身份/已发布Schema）
# ---------------------------------------------------------------------------

M2_INPUT_BUDGET_POLICY_VERSION = "m2-input-budget-policy-1.0"

M2_INPUT_BUDGET_LIMITS: Mapping[str, Mapping[str, int]] = {
    "boundary_model": {"limit_message_chars": 150_000, "limit_utf8_bytes": 300_000},
    "content_model": {"limit_message_chars": 60_000, "limit_utf8_bytes": 180_000},
}

# P6 §六.2硬预算（条目仅呈现、不放大；各模块自身预算执行不变）。
# content_model条目按《阶段15-content失败修复裁决》D2/D5修订为{3,6}
# （validation-feedback-1.0一次纠错；放大决定属该裁决，条目随registry事实同步）。
M2_CALL_BUDGETS: Mapping[str, Mapping[str, int]] = {
    "boundary_model": {"business_call_hard_limit": 1, "supplier_request_hard_limit": 2},
    "content_model": {"business_call_hard_limit": 3, "supplier_request_hard_limit": 6},
    "vision_caption_model": {
        "business_call_hard_limit": 0,
        "supplier_request_hard_limit": 0,
    },
}

M2_INPUT_BUDGET_STATUSES = frozenset({"pass", "unknown", "exceeded"})


class M2PipelineError(RuntimeError):
    """run-m2条目层类型化失败；码集封闭（裁决§三.7），不复制模块码表。"""

    def __init__(
        self,
        error_code: str,
        *,
        failed_stage: str = "preflight",
        reason_codes: Sequence[str] = (),
        message: str | None = None,
    ) -> None:
        if error_code not in M2_ENTRY_ERROR_CODES:
            raise ValueError(f"条目失败码必须来自封闭集：{error_code}")
        if failed_stage not in M2_STAGE_SEQUENCE:
            raise ValueError(f"failed_stage必须来自阶段列表：{failed_stage}")
        self.error_code = error_code
        self.failed_stage = failed_stage
        self.reason_codes = tuple(reason_codes) or (error_code,)
        super().__init__(message or _ENTRY_FAILURE_MESSAGES[error_code])


def _normalize_slot_measurement(measurement: Any) -> tuple[int, int] | None:
    """把单槽全messages度量规整为(chars, bytes)或None（不可测）。

    裁决§一1.3：度量=该次业务调用全messages各条``content``文本拼接后的字符数与
    UTF-8字节数；构造不可得/形态不合约定一律按``unknown``处理，不做猜测折算。
    """

    if measurement is None:
        return None
    if not isinstance(measurement, Mapping):
        return None
    chars = measurement.get("message_chars")
    bytes_ = measurement.get("message_bytes")
    if isinstance(chars, bool) or isinstance(bytes_, bool):
        return None
    if not isinstance(chars, int) or not isinstance(bytes_, int):
        return None
    if chars < 0 or bytes_ < 0:
        return None
    return (chars, bytes_)


@dataclass(frozen=True)
class InputBudgetRoleDecision:
    """单角色在首个供应商请求前的入口预算判定记录（裁决§一1.4/1.5）。"""

    role: str
    policy_version: str
    limit_message_chars: int
    limit_utf8_bytes: int
    slot_measurements: tuple[tuple[int, int] | None, ...]
    status: str
    planned_business_calls: int
    business_call_hard_limit: int
    supplier_request_hard_limit: int

    def to_json(self) -> dict[str, Any]:
        """呈现层对象＝裁决§一1.5字面8键，不增不减（审查报告§四B1修正）。

        G1解释（审查报告§四.2，按2026-10-18页眉措辞解释先例）：
        ``measured_chars``/``measured_bytes``为按调用槽位排序的列表——不可测槽为
        ``null``、boundary单调用为单元素列表、``planned_business_calls == 0``时
        两者均为``null``（§一1.5「measured记null」字面）、空度量为空列表。
        ``role``（由阶段→角色冻结映射唯一决定）与``business_call_hard_limit``
        不入呈现层；内部dataclass字段保留不删。
        """

        if self.planned_business_calls == 0:
            measured_chars: list[int | None] | None = None
            measured_bytes: list[int | None] | None = None
        else:
            measured_chars = [
                None if slot is None else slot[0] for slot in self.slot_measurements
            ]
            measured_bytes = [
                None if slot is None else slot[1] for slot in self.slot_measurements
            ]
        return {
            "policy_version": self.policy_version,
            "limit_chars": self.limit_message_chars,
            "limit_bytes": self.limit_utf8_bytes,
            "measured_chars": measured_chars,
            "measured_bytes": measured_bytes,
            "status": self.status,
            "planned_business_calls": self.planned_business_calls,
            "supplier_request_limit": self.supplier_request_hard_limit,
        }


def evaluate_input_budget(
    role: str,
    measurements: Mapping[str, int] | None | Sequence[Mapping[str, int] | None],
    *,
    planned_business_calls: int,
) -> InputBudgetRoleDecision:
    """裁决§一Q1的入口纯函数：在首个供应商请求之前判定，不改任何模块行为。

    - ``planned_business_calls == 0``恒``pass``（E07语义）；
    - 逐槽判：任一槽超限→``exceeded``；否则任槽不可测→``unknown``；否则``pass``
      （超限优先于不可测——已能确证失败时不降级为unknown）；
    - ``exceeded``/``unknown``的停止动作（``INPUT_BUDGET_EXCEEDED``/
      ``INPUT_BUDGET_UNKNOWN``、零调用、零发布、不动current）由E3编排层执行。
    """

    if role not in M2_INPUT_BUDGET_LIMITS:
        raise ValueError(f"输入预算策略未定义角色：{role}")
    if not isinstance(planned_business_calls, int) or isinstance(
        planned_business_calls, bool
    ):
        raise ValueError("planned_business_calls必须为非负整数")
    if planned_business_calls < 0:
        raise ValueError("planned_business_calls必须为非负整数")

    limits = M2_INPUT_BUDGET_LIMITS[role]
    budgets = M2_CALL_BUDGETS[role]

    if isinstance(measurements, Mapping) or measurements is None:
        raw_slots: tuple[Any, ...] = (measurements,)
    elif isinstance(measurements, Sequence) and not isinstance(measurements, str):
        raw_slots = tuple(measurements)
    else:
        raise ValueError("measurements必须为映射、None或映射序列")

    slots = tuple(_normalize_slot_measurement(item) for item in raw_slots)

    if planned_business_calls == 0:
        status = "pass"
    elif not slots:
        status = "unknown"
    else:
        per_slot = []
        for slot in slots:
            if slot is None:
                per_slot.append("unknown")
            elif (
                slot[0] > limits["limit_message_chars"]
                or slot[1] > limits["limit_utf8_bytes"]
            ):
                per_slot.append("exceeded")
            else:
                per_slot.append("pass")
        if "exceeded" in per_slot:
            status = "exceeded"
        elif "unknown" in per_slot:
            status = "unknown"
        else:
            status = "pass"

    return InputBudgetRoleDecision(
        role=role,
        policy_version=M2_INPUT_BUDGET_POLICY_VERSION,
        limit_message_chars=limits["limit_message_chars"],
        limit_utf8_bytes=limits["limit_utf8_bytes"],
        slot_measurements=slots,
        status=status,
        planned_business_calls=planned_business_calls,
        business_call_hard_limit=budgets["business_call_hard_limit"],
        supplier_request_hard_limit=budgets["supplier_request_hard_limit"],
    )


# ---------------------------------------------------------------------------
# E0：运行计划JSON形态（裁决§三.3；数据类先行冻结，E2接线装配与输出）
# ---------------------------------------------------------------------------

M2_RUN_PLAN_SCHEMA_NAME = "M2RunPlan"
M2_RUN_PLAN_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class M2StagePlanEntry:
    """dry-run运行计划逐阶段条目：{stage, disposition, input_budget:{...}|null}。"""

    stage: str
    disposition: str
    input_budget: InputBudgetRoleDecision | None = None

    def __post_init__(self) -> None:
        if self.stage not in M2_STAGE_SEQUENCE:
            raise ValueError(f"未知阶段名：{self.stage}")
        if self.disposition not in M2_PLAN_DISPOSITIONS:
            raise ValueError(f"未知disposition：{self.disposition}")
        if self.input_budget is not None:
            expected_role = M2_ROLE_BY_STAGE.get(self.stage)
            if expected_role is None or self.input_budget.role != expected_role:
                raise ValueError(f"阶段{self.stage}不承载输入预算条目")

    def to_json(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "disposition": self.disposition,
            "input_budget": (
                None if self.input_budget is None else self.input_budget.to_json()
            ),
        }


@dataclass(frozen=True)
class M2RunPlan:
    """全新/已有任务统一形态的运行计划（裁决§三.3）。

    ``model_registry``只记basename（P6 §三：绝对路径不入正式JSON）；
    ``media_tools``只记工具文件名（P6 §三：正式产物只记工具basename）。
    """

    task_id: str
    is_new_task: bool
    dry_run: bool
    ocr_mode: str
    vision_caption: str
    allow_llm: bool
    model_registry: str | None
    source_video_sha256: str | None
    size_bytes: int | None
    duration_ms: int | None
    has_audio: bool | None
    media_tools: tuple[str, str]
    stages: tuple[M2StagePlanEntry, ...]

    def __post_init__(self) -> None:
        names = tuple(entry.stage for entry in self.stages)
        positions = [M2_STAGE_SEQUENCE.index(name) for name in names]
        if len(set(positions)) != len(positions) or positions != sorted(positions):
            raise ValueError("运行计划阶段必须按冻结顺序且不重复")
        if self.ocr_mode not in OCR_MODES:
            raise ValueError(f"未知ocr_mode：{self.ocr_mode}")
        if self.vision_caption not in M2_VISION_CAPTION_VALUES:
            raise ValueError(f"未知vision_caption：{self.vision_caption}")
        if self.model_registry is not None:
            # P6 §三：绝对路径不入正式JSON——Schema自身钉死basename口径（实现决定）
            object.__setattr__(
                self, "model_registry", Path(self.model_registry).expanduser().name
            )
        if self.media_tools:
            object.__setattr__(
                self,
                "media_tools",
                tuple(Path(str(tool)).name for tool in self.media_tools),
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_name": M2_RUN_PLAN_SCHEMA_NAME,
            "schema_version": M2_RUN_PLAN_SCHEMA_VERSION,
            "task_id": self.task_id,
            "is_new_task": self.is_new_task,
            "dry_run": self.dry_run,
            "ocr_mode": self.ocr_mode,
            "vision_caption": self.vision_caption,
            "allow_llm": self.allow_llm,
            "model_registry": self.model_registry,
            "source_video": {
                "sha256": self.source_video_sha256,
                "size_bytes": self.size_bytes,
                "duration_ms": self.duration_ms,
                "has_audio": self.has_audio,
            },
            "media_tools": {"ffmpeg": self.media_tools[0], "ffprobe": self.media_tools[1]},
            "stages": [entry.to_json() for entry in self.stages],
        }


# ---------------------------------------------------------------------------
# E1：只读preflight（裁决§三.8两分支，全部在task_init之前、零写入）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class M2PreflightResult:
    """preflight通过后的只读事实集；不含任何写入副作用。"""

    task_id: str
    is_new_task: bool
    task_root: Path
    source_video: Path
    size_bytes: int
    media_tools: MediaTools
    task_manifest: Mapping[str, Any] | None


_ASR_ONLY_ENTRY_CODES = (
    "SOURCE_VIDEO_REQUIRED",
    "SOURCE_VIDEO_MISMATCH",
    "MEDIA_TOOL_NOT_FOUND",
    "MEDIA_TOOL_INVALID",
)


def _map_asr_only_failure(
    exc: ASROnlyPipelineError, *, failed_stage: str = "preflight"
) -> M2PipelineError:
    """把复用的asr-only失败消息前缀映射为条目封闭码；未知形态原样传播。

    实现决定1：源文件不存在/不可读与task.json未记录源路径映射为
    SOURCE_VIDEO_REQUIRED的reason细化，不新增条目码。
    """

    message = str(exc)
    for code in _ASR_ONLY_ENTRY_CODES:
        if code in message:
            return M2PipelineError(code, failed_stage=failed_stage)
    if "源视频不存在" in message or "无法读取源视频" in message:
        return M2PipelineError(
            "SOURCE_VIDEO_REQUIRED",
            failed_stage=failed_stage,
            reason_codes=("SOURCE_VIDEO_REQUIRED", "SOURCE_FILE_UNREADABLE"),
        )
    if "未记录源视频路径" in message:
        return M2PipelineError(
            "SOURCE_VIDEO_REQUIRED",
            failed_stage=failed_stage,
            reason_codes=("SOURCE_VIDEO_REQUIRED", "SOURCE_VIDEO_UNRECORDED"),
        )
    raise exc


def _require_source_usable(source: Path, *, failed_stage: str = "preflight") -> int:
    """裁决§三.8：源路径必须为存在、可读、size>0的普通文件（映射实现决定1）。"""

    try:
        size = source.stat().st_size
    except OSError as exc:
        raise M2PipelineError(
            "SOURCE_VIDEO_REQUIRED",
            failed_stage=failed_stage,
            reason_codes=("SOURCE_VIDEO_REQUIRED", "SOURCE_FILE_UNREADABLE"),
        ) from exc
    if size == 0:
        raise M2PipelineError(
            "SOURCE_VIDEO_REQUIRED",
            failed_stage=failed_stage,
            reason_codes=("SOURCE_VIDEO_REQUIRED", "SOURCE_FILE_EMPTY"),
        )
    if not os.access(source, os.R_OK):
        raise M2PipelineError(
            "SOURCE_VIDEO_REQUIRED",
            failed_stage=failed_stage,
            reason_codes=("SOURCE_VIDEO_REQUIRED", "SOURCE_FILE_UNREADABLE"),
        )
    return size


def _require_output_writable(task_root: Path) -> None:
    """裁决§三.8：os.access级可写检查，禁止探测性写文件。

    实现决定2：可写性失败复用既有类型化族TaskWorkspaceError（消息不带绝对路径），
    不在封闭码集之外发明条目码。
    """

    probe = task_root
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            raise TaskWorkspaceError("任务输出目录的祖先路径不存在且无法确定可写位置。")
        probe = parent
    if not os.access(probe, os.W_OK):
        raise TaskWorkspaceError("任务输出目录不可写（os.access检查失败）。")


def preflight_task_m2(
    output_root: str | Path,
    task_id: str,
    *,
    source_video: str | Path | None = None,
    ffmpeg_dir: str | Path | None = None,
) -> M2PreflightResult:
    """执行裁决§三.8两分支preflight检查面；全程零写入、零网络、零供应商请求。"""

    validate_task_id(task_id)
    try:
        source, is_new_task = _resolve_source(output_root, task_id, source_video)
    except ASROnlyPipelineError as exc:
        raise _map_asr_only_failure(exc) from exc

    size_bytes = _require_source_usable(source)

    try:
        tools = resolve_media_tools(ffmpeg_dir)
    except ASROnlyPipelineError as exc:
        raise _map_asr_only_failure(exc) from exc

    task_root = Path(output_root).expanduser() / task_id
    _require_output_writable(task_root)

    manifest = None
    if not is_new_task:
        manifest = load_task_manifest(task_root / "task.json", expected_task_id=task_id)

    return M2PreflightResult(
        task_id=task_id,
        is_new_task=is_new_task,
        task_root=task_root,
        source_video=source,
        size_bytes=size_bytes,
        media_tools=tools,
        task_manifest=manifest,
    )


# ---------------------------------------------------------------------------
# E2：阶段依赖边表、跨域current读取与运行计划装配（裁决§三.9/§三.3）
# ---------------------------------------------------------------------------

# 裁决§三.9依赖边表（正向推进判disposition用；``ocr``不是任何阶段的阻断依赖——
# 该表把「OCR状态」列为timeline的输入形态之一，disabled/not_run/降级同为可消费态，
# 由timeline模块自身消费已发布组或记状态）。
_STAGE_DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    "task_init": (),
    "transcript": (),
    "frames": (),
    "page_changes": ("frames",),
    "ocr": ("page_changes",),
    "timeline_v2": ("transcript", "frames", "page_changes"),
    "chapter_plan_v2": ("timeline_v2",),
    "chapter_frames": ("chapter_plan_v2", "frames", "timeline_v2"),
    # 裁决§三.9字面表：content依赖plan+timeline；代表帧选组的存在性由compose在
    # 运行时的类型化失败兜底（dry-run预览构造抛错→测量折叠为unknown呈现，§三.3
    # 「不可测属呈现非失败」；普通运行在顺序上chapter_frames先于content执行）。
    "document_content_v2": ("chapter_plan_v2", "timeline_v2"),
    "export_v2": ("document_content_v2", "chapter_frames"),
}

_M2_ARTIFACT_STAGES = (
    "transcript",
    "frames",
    "page_changes",
    "ocr",
    "timeline_v2",
    "chapter_plan_v2",
    "chapter_frames",
    "document_content_v2",
    "export_v2",
)

# 各域产物根目录（相对task_root）；transcript为M1固定路径，不属于结果组形态。
_ARTIFACT_GROUP_DIRS: Mapping[str, str] = {
    "frames": "m2/frames/formal-results",
    "page_changes": "m2/frames/page-changes/formal-results",
    "ocr": "m2/ocr/formal-results",
    "timeline_v2": "m2/timelines/formal-results",
    "chapter_plan_v2": "m2/boundaries/formal-results",
    "chapter_frames": "m2/chapter-frames/formal-results",
    "document_content_v2": "m2/content/formal-results",
    "export_v2": "m2/documents/formal-results",
}


def _domain_relative(stage: str, result_id: str) -> str:
    """域→任务根相对路径（摘要与日志共用；不含绝对路径，P4 §6.8）。"""

    if stage == "transcript":
        return "timeline/timeline.json"
    return f"{_ARTIFACT_GROUP_DIRS[stage]}/{result_id}"


def _read_domain_result_id(
    stage: str, output: Path, task_root: Path, task_id: str
) -> str | None:
    """指针级复验读取域current的result_id；无current返回None，损坏硬抛。

    实现决定4/5：复用各模块既有current读取/复验函数（含result_id安全、组路径
    包含性、manifest哈希复验），不复制业务逻辑；损坏形态原异常传播，由入口按
    类型化族包裹为§三.7 envelope。
    """

    if stage == "transcript":
        if not (task_root / "timeline" / "timeline.json").is_file():
            return None
        # 实现决定10：真实M1文件无timeline_id键（DEV01实测键集）——域标识取
        # ``compute_timeline_id``（既有canonical ``unified-timeline-<12hex>``口径，
        # 对载荷确定性；裁决§三.6字面要求transcript域入result_ids）。
        return compute_timeline_id(load_task_timeline(output, task_id))
    if stage == "frames":
        pointer = load_frame_set_current(output, task_id)
        return None if pointer is None else str(pointer["result_id"])
    if stage == "page_changes":
        return _pcd_mod._load_current(
            task_root / "m2" / "frames" / "page-changes"
        )[1]
    if stage == "ocr":
        return _ocr_mod._load_current(task_root / "m2" / "ocr")[1]
    if stage == "timeline_v2":
        pointer = load_timeline_current(output, task_id)
        return None if pointer is None else str(pointer["result_id"])
    if stage == "chapter_plan_v2":
        pointer = load_chapter_plan_current(output, task_id)
        return None if pointer is None else str(pointer["result_id"])
    if stage == "chapter_frames":
        return _cf_mod._load_own_current(task_root / "m2" / "chapter-frames")[1]
    if stage == "document_content_v2":
        return _dc_mod._load_current(task_root / "m2" / "content")[1]
    if stage == "export_v2":
        return _de_mod._load_export_current(task_root / "m2" / "documents")[1]
    raise KeyError(f"未知产物域：{stage}")


def _dry_run_budget_decision(
    role: str,
    output: Path,
    task_id: str,
    model_registry: str | Path | None,
    bounded_content: bool = False,
) -> InputBudgetRoleDecision:
    """dry-run预算呈现测量：上游满足、本域无current时构造入口度量。

    裁决§三.3「不可测属呈现非失败」：dry-run中测量构造的任何抛错（含模块类型化
    缺失/损坏族）一律折叠为``unknown``呈现，不发错误码、不退出非零；普通运行的
    分流见实现决定6。``planned``缺省取角色硬限（呈现上限，不放大）。
    """

    hard_limit = M2_CALL_BUDGETS[role]["business_call_hard_limit"]
    try:
        if role == "boundary_model":
            measurement = measure_boundary_egress(
                output, task_id, model_registry=model_registry
            )
            return evaluate_input_budget(role, [measurement], planned_business_calls=1)
        preview = build_content_composition_dry_run(
            output, task_id, model_registry=model_registry,
            **({"bounded": True} if bounded_content else {}),
        )
        slots = [
            {"message_chars": call["message_chars"], "message_bytes": call["message_bytes"]}
            for call in preview["calls"]
        ]
        planned = int(preview["budget"]["expected_business_calls"])
        decision = evaluate_input_budget(role, slots, planned_business_calls=planned)
        return replace(decision, business_call_hard_limit=12, supplier_request_hard_limit=12) if bounded_content else decision
    except Exception:
        return evaluate_input_budget(role, None, planned_business_calls=hard_limit)


def _assemble_run_plan(
    preflight: M2PreflightResult,
    metadata: VideoMetadata,
    source_sha256: str,
    *,
    ocr_mode: str,
    vision_caption: str,
    allow_llm: bool,
    model_registry: str | Path | None,
    bounded_content: bool = False,
) -> M2RunPlan:
    """E2零写入运行计划装配（裁决§三.3/§三.9；调用前必须已完成媒体流探测）。"""

    output = preflight.task_root.parent
    task_root = preflight.task_root
    task_id = preflight.task_id

    entries: list[M2StagePlanEntry] = [
        M2StagePlanEntry("preflight", "cache_hit"),
        M2StagePlanEntry(
            "task_init", "will_run" if preflight.is_new_task else "cache_hit"
        ),
    ]
    satisfied: dict[str, bool] = {
        "task_init": not preflight.is_new_task,
    }
    for stage in _M2_ARTIFACT_STAGES:
        role = M2_ROLE_BY_STAGE.get(stage)
        deps_ok = all(satisfied.get(dep, False) for dep in _STAGE_DEPENDENCIES[stage])
        result_id: str | None = None
        if deps_ok:
            result_id = _read_domain_result_id(stage, output, task_root, task_id)
        if stage == "ocr" and ocr_mode == "off":
            # 裁决§三.3字面：--ocr off对应disabled；不阻断下游（§三.9消费的是状态）。
            disposition = "disabled"
            entry = M2StagePlanEntry(stage, disposition)
        elif role is None:
            disposition = "cache_hit" if result_id else "will_run"
            entry = M2StagePlanEntry(stage, disposition)
        elif not deps_ok:
            # §三.3：度量输入不可构造（上游未满足）→ unmeasurable呈现unknown。
            disposition = "unmeasurable"
            entry = M2StagePlanEntry(
                stage,
                disposition,
                evaluate_input_budget(
                    role,
                    None,
                    planned_business_calls=M2_CALL_BUDGETS[role]["business_call_hard_limit"],
                ),
            )
        elif result_id:
            disposition = "cache_hit"
            entry = M2StagePlanEntry(
                stage, disposition, evaluate_input_budget(role, None, planned_business_calls=0)
            )
        else:
            disposition = "will_run"
            entry = M2StagePlanEntry(
                stage,
                disposition,
                _dry_run_budget_decision(role, output, task_id, model_registry, **({"bounded_content": True} if bounded_content else {})),
            )
        satisfied[stage] = disposition == "cache_hit" or (
            stage == "ocr" and disposition == "disabled"
        )
        entries.append(entry)
    entries.append(M2StagePlanEntry("completed", "will_run"))

    return M2RunPlan(
        task_id=task_id,
        is_new_task=preflight.is_new_task,
        dry_run=True,
        ocr_mode=ocr_mode,
        vision_caption=vision_caption,
        allow_llm=allow_llm,
        model_registry=str(model_registry) if model_registry is not None else None,
        source_video_sha256=source_sha256,
        size_bytes=preflight.size_bytes,
        duration_ms=int(round(metadata.duration_seconds * 1000)),
        has_audio=metadata.has_audio,
        media_tools=(
            str(preflight.media_tools.ffmpeg),
            str(preflight.media_tools.ffprobe),
        ),
        stages=tuple(entries),
    )


# ---------------------------------------------------------------------------
# E3：模块类型化失败包裹、审批守卫传输、JSONL日志与全链编排
# ---------------------------------------------------------------------------

# §三.7：模块类型化失败原码包裹入同一envelope；基类族逐一列出（含被复用编排族）。
# TaskWorkspaceError不在其列——main()既有文本兜底覆盖（test_invalid_task_id先例）。
_MODULE_FAILURE_TYPES = (
    FrameSamplerError,
    PageChangeDetectorError,
    OcrExtractorError,
    TimelineBuilderV2Error,
    BoundaryPlannerV2Error,
    ChapterFramesError,
    DocumentComposerV2Error,
    DocumentExporterV2Error,
    AudioExtractionError,
    ASRTranscriptionError,
    ASRTimelineError,
    VideoProbeError,
    ASROnlyPipelineError,
)


@dataclass
class M2EnvelopeFailure(RuntimeError):
    """模块类型化失败的包裹事实载体（裁决§三.7）；不是新的失败码来源。

    继承``RuntimeError``仅为其可抛出/可捕获的形态载体；``error_code``与
    ``reason_codes``原样取自被包裹的模块类型化异常（不复制模块码表），
    条目封闭码集``M2_ENTRY_ERROR_CODES``不因本类扩容。
    非frozen：``raise``时``BaseException``需写入``__traceback__``等内部属性。
    """

    failed_stage: str
    error_code: str
    reason_codes: tuple[str, ...]
    message: str


def _module_envelope(exc: BaseException, failed_stage: str) -> M2EnvelopeFailure:
    code = str(getattr(exc, "error_code", "") or "")
    carried = getattr(exc, "reason_codes", None)
    if isinstance(carried, (tuple, list)) and carried:
        reason_codes = tuple(str(item) for item in carried)
    elif code:
        reason_codes = (code,)
    else:
        # 实现决定：无码族（如ASROnlyPipelineError）reason_codes回退为阶段名——
        # 封闭词表内取值，不发明新token；error_code留空如实（登记§三.7/实现决定2）。
        reason_codes = (failed_stage,)
    return M2EnvelopeFailure(
        failed_stage=failed_stage,
        error_code=code,
        reason_codes=reason_codes,
        message=str(exc),
    )


class _DenyApprovalTransport:
    """未批准区间与计划0区间的拒绝守卫：``complete``一进入即条目失败，零网络。

    实现决定7：杜绝「计划0但身份漂移导致未批准真实请求」的残余路径；守卫抛出的
    条目错误若被模块吸收为模型失败，结果只会是不外发的降级/应急发布，无网络副作用。
    """

    def __init__(self, failed_stage: str) -> None:
        self._failed_stage = failed_stage
        self.rejected = 0

    def complete(self, config: Any, messages: Any, response_format: Any) -> Any:
        del config, messages, response_format
        self.rejected += 1
        raise M2PipelineError("LLM_APPROVAL_REQUIRED", failed_stage=self._failed_stage)


def _pick_transport(
    role: str,
    stage: str,
    *,
    allow_llm: bool,
    transports: Mapping[str, Any] | None,
) -> Any:
    if transports is not None and role in transports:
        return transports[role]
    if allow_llm:
        return None  # 由模块构造真实传输；本轮零真实API（E4/E5须用户另行批准）
    return _DenyApprovalTransport(stage)


class _RunJournal:
    """脱敏append-only JSONL日志（裁决§三.5）；task_init之前缓冲、失败不落盘。

    实现决定8：``begin_logging``前事件只进内存——preflight/task_init之前的条目
    失败不产生日志文件（E01/E05零写保持）；每阶段记录终态事件，预算判定随该阶段
    终态/失败事件入日志（实现决定13：§一1.5「首个调用前」字面约束stdout进度行，
    带input_budget的行在模块调用前打印；JSONL同构事件保持终态时点）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._buffer: list[dict[str, Any]] = []
        self._active = False

    def event(
        self,
        stage: str,
        status: str,
        *,
        elapsed_ms: int | None = None,
        result_id: str | None = None,
        reason_codes: Sequence[str] = (),
        input_budget: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": stage,
            "status": status,
        }
        if elapsed_ms is not None:
            payload["elapsed_ms"] = int(elapsed_ms)
        if result_id is not None:
            payload["result_id"] = result_id
            payload["relative_paths"] = [_domain_relative(stage, result_id)]
        if reason_codes:
            payload["reason_codes"] = list(reason_codes)
        if input_budget is not None:
            payload["input_budget"] = dict(input_budget)
        if self._active:
            self._append(payload)
        else:
            self._buffer.append(payload)

    def begin_logging(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._active = True
        pending, self._buffer = self._buffer, []
        for payload in pending:
            self._append(payload)

    def _append(self, payload: Mapping[str, Any]) -> None:
        # LF铁律：newline=""（Windows文本模式会把\n翻成\r\n，破坏与哈希/工具的口径）。
        with open(self._path, "a", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class M2RunSummary:
    """成功摘要：P4 §5.3字面枚举8键（实现决定3；不发明第9键）。"""

    task_id: str
    result_ids: Mapping[str, Any]
    cache_hits: Mapping[str, bool]
    degraded: Sequence[str]
    reason_codes: Sequence[str]
    relative_paths: Mapping[str, str]
    call_counts: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "status": "completed",
            "task_id": self.task_id,
            "result_ids": dict(self.result_ids),
            "cache_hits": dict(self.cache_hits),
            "degraded": list(self.degraded),
            "reason_codes": list(self.reason_codes),
            "relative_paths": dict(self.relative_paths),
            "call_counts": json.loads(json.dumps(self.call_counts)),
        }


def _notify(
    progress: Callable[..., None] | None,
    stage: str,
    status: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    if progress is not None:
        progress(stage, status, payload)


# ---------------------------------------------------------------------------
# E0—E3：入口函数
# ---------------------------------------------------------------------------


def _execute_run(
    output_root: str | Path,
    preflight: M2PreflightResult,
    metadata: VideoMetadata,
    *,
    ocr_mode: str,
    model_registry: str | Path | None,
    allow_llm: bool,
    ffmpeg_dir: str | Path | None,
    progress: Callable[..., None] | None,
    journal: _RunJournal,
    transports: Mapping[str, Any] | None,
    bounded_content: bool = False,
) -> M2RunSummary:
    """E3普通路径十二阶段编排主体（在``_media_tool_path``上下文内执行）。"""

    output = preflight.task_root.parent
    task_root = preflight.task_root
    task_id = preflight.task_id

    cache_hits: dict[str, bool] = {}
    degraded: list[str] = []
    reason_codes: list[str] = []
    published: list[str] = []
    role_counts: dict[str, dict[str, int]] = {
        "boundary_model": {"business_calls": 0, "supplier_requests": 0},
        "content_model": {"business_calls": 0, "supplier_requests": 0},
    }
    budget_decisions: dict[str, InputBudgetRoleDecision] = {}
    stage = "task_init"

    def done(
        stage_name: str,
        status: str,
        t0: float,
        *,
        result_id: str | None = None,
        reasons: Sequence[str] = (),
        input_budget: InputBudgetRoleDecision | None = None,
    ) -> None:
        # B2修正：带input_budget的stdout进度行不在此打印（已前移至模块调用前，
        # 实现决定13）；done只保留终态进度行与JSONL终态事件。
        _notify(progress, stage_name, status)
        journal.event(
            stage_name,
            status,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            result_id=result_id,
            reason_codes=reasons,
            input_budget=(
                None if input_budget is None else input_budget.to_json()
            ),
        )

    try:
        t0 = time.monotonic()
        _notify(progress, "task_init", "started")
        initialize_task_workspace(
            output_root,
            task_id,
            source_video=preflight.source_video if preflight.is_new_task else None,
        )
        _save_metadata_if_changed(task_root / "metadata.json", metadata)
        journal.begin_logging()
        done("task_init", "completed", t0)

        # ---- transcript（覆盖audio/asr/timeline1三子步；进度行按子步打印，
        # JSONL按阶段级记录——裁决§三.2字面）----
        stage = "transcript"
        t0 = time.monotonic()
        _notify(progress, "transcript", "started")
        if not metadata.has_audio:
            # 沿用asr-only既有形态：条目族类型化失败，不发明NO_AUDIO码。
            raise ASROnlyPipelineError("源视频没有音轨，run-m2入口无法继续。")
        audio = extract_task_audio(output_root, task_id)
        _notify(progress, "audio", "cache_hit" if audio.cache_hit else "completed")
        asr = transcribe_task_audio(
            output_root, task_id, model="small", device="cpu", compute_type="int8"
        )
        _notify(progress, "asr", "cache_hit" if asr.cache_hit else "completed")
        timeline1 = build_task_timeline(output_root, task_id)
        _notify(progress, "timeline", "cache_hit" if timeline1.cache_hit else "completed")
        transcript_hit = bool(audio.cache_hit and asr.cache_hit and timeline1.cache_hit)
        cache_hits["transcript"] = transcript_hit
        # transcript的result_id（timeline_id）在completed阶段的权威current复读统一读取。
        done(
            "transcript",
            "cache_hit" if transcript_hit else "completed",
            t0,
        )

        # ---- frames ----
        stage = "frames"
        t0 = time.monotonic()
        _notify(progress, "frames", "started")
        frame_group = sample_frames(
            task_id, output_root=output_root, ffmpeg_dir=ffmpeg_dir
        )
        cache_hits["frames"] = bool(frame_group.cache_hit)
        if not frame_group.cache_hit:
            published.append("frames")
        done(
            "frames",
            "cache_hit" if frame_group.cache_hit else "completed",
            t0,
            result_id=frame_group.group_path.name,
        )

        # ---- page_changes ----
        stage = "page_changes"
        t0 = time.monotonic()
        _notify(progress, "page_changes", "started")
        pc_group = detect_page_changes(task_id, output_root=output_root)
        cache_hits["page_changes"] = bool(pc_group.cache_hit)
        if not pc_group.cache_hit:
            published.append("page_changes")
        done(
            "page_changes",
            "cache_hit" if pc_group.cache_hit else "completed",
            t0,
            result_id=pc_group.group_path.name,
        )

        # ---- ocr ----
        stage = "ocr"
        t0 = time.monotonic()
        _notify(progress, "ocr", "started")
        ocr_outcome = run_ocr(task_id, output_root=output_root, mode=ocr_mode)
        if isinstance(ocr_outcome, OcrDegradedOutcome):
            cache_hits["ocr"] = False
            degraded.append("ocr")
            reason_codes.extend(ocr_outcome.reason_codes)
            done("ocr", "degraded", t0, reasons=ocr_outcome.reason_codes)
        else:
            cache_hits["ocr"] = bool(ocr_outcome.cache_hit)
            if not ocr_outcome.cache_hit:
                published.append("ocr")
            off_reasons = ("OCR_DISABLED",) if ocr_mode == "off" else ()
            reason_codes.extend(off_reasons)
            done(
                "ocr",
                "cache_hit" if ocr_outcome.cache_hit else "completed",
                t0,
                result_id=ocr_outcome.group_path.name,
                reasons=off_reasons,
            )

        # ---- timeline_v2 ----
        stage = "timeline_v2"
        t0 = time.monotonic()
        _notify(progress, "timeline_v2", "started")
        tl_group = build_timeline_v2(
            task_id, output_root=output_root, ocr=ocr_mode
        )
        cache_hits["timeline_v2"] = bool(tl_group.cache_hit)
        if not tl_group.cache_hit:
            published.append("timeline_v2")
        done(
            "timeline_v2",
            "cache_hit" if tl_group.cache_hit else "completed",
            t0,
            result_id=tl_group.group_path.name,
        )

        # ---- chapter_plan_v2（boundary角色：审批门→测量门→exceeded/unknown停）----
        stage = "chapter_plan_v2"
        t0 = time.monotonic()
        _notify(progress, "chapter_plan_v2", "started")
        plan_rid = _read_domain_result_id(
            "chapter_plan_v2", output, task_root, task_id
        )
        if plan_rid is None and not allow_llm:
            raise M2PipelineError(
                "LLM_APPROVAL_REQUIRED", failed_stage="chapter_plan_v2"
            )
        if plan_rid is not None:
            boundary_decision = evaluate_input_budget(
                "boundary_model", None, planned_business_calls=0
            )
        else:
            try:
                measurement = measure_boundary_egress(
                    output, task_id, model_registry=model_registry,
                    **({"bounded": True} if bounded_content else {}),
                )
            except BoundaryPlannerV2Error as exc:
                # 实现决定6：域输入缺失/损坏保留原码包裹（P4硬失败语义）。
                raise _module_envelope(exc, "chapter_plan_v2") from exc
            except Exception:
                # 裁决§一1.5字面：构造抛错→INPUT_BUDGET_UNKNOWN停止（首请求前）。
                measurement = None
            boundary_decision = evaluate_input_budget(
                "boundary_model",
                None if measurement is None else [measurement],
                planned_business_calls=1,
            )
        budget_decisions["chapter_plan_v2"] = boundary_decision
        if boundary_decision.status == "exceeded":
            raise M2PipelineError(
                "INPUT_BUDGET_EXCEEDED", failed_stage="chapter_plan_v2"
            )
        if boundary_decision.status == "unknown":
            raise M2PipelineError(
                "INPUT_BUDGET_UNKNOWN", failed_stage="chapter_plan_v2"
            )
        # 审查报告§四B2修正（裁决§一1.5字面）：带input_budget的进度行先于角色
        # 首个供应商调用打印；JSONL同构事件保持终态时点（实现决定13）。
        _notify(
            progress,
            "chapter_plan_v2",
            "started",
            {"input_budget": boundary_decision.to_json()},
        )
        boundary_result = plan_chapters_v2(
            task_id,
            output_root=output_root,
            model_registry=model_registry,
            transport=_pick_transport(
                "boundary_model",
                "chapter_plan_v2",
                allow_llm=allow_llm,
                transports=transports,
            ),
        )
        # 实现决定12：this_run＝本轮实数（裁决§三.6）。cache_hit早退时本轮零调用，
        # 记0；结果类型化字段此时携带的是组report中持久化的历史首次生成计数，
        # 按P6 §八分离裁定不入this_run。本轮新发布时该字段即本轮实数。
        role_counts["boundary_model"] = (
            {"business_calls": 0, "supplier_requests": 0}
            if boundary_result.cache_hit
            else {
                "business_calls": int(boundary_result.business_call_count),
                "supplier_requests": int(boundary_result.supplier_request_count),
            }
        )
        cache_hits["chapter_plan_v2"] = bool(boundary_result.cache_hit)
        if not boundary_result.cache_hit:
            published.append("chapter_plan_v2")
        boundary_status = (
            "cache_hit"
            if boundary_result.cache_hit
            else ("degraded" if boundary_result.status == "degraded" else "completed")
        )
        if boundary_status == "degraded":
            degraded.append("chapter_plan_v2")
        done(
            "chapter_plan_v2",
            boundary_status,
            t0,
            result_id=boundary_result.result_id,
            input_budget=boundary_decision,
        )

        # ---- chapter_frames ----
        stage = "chapter_frames"
        t0 = time.monotonic()
        _notify(progress, "chapter_frames", "started")
        cf_result = select_chapter_frames(task_id, output_root=output_root)
        cache_hits["chapter_frames"] = bool(cf_result.cache_hit)
        if not cf_result.cache_hit:
            published.append("chapter_frames")
        done(
            "chapter_frames",
            "cache_hit" if cf_result.cache_hit else "completed",
            t0,
            result_id=cf_result.result_id,
        )

        # ---- document_content_v2（content角色：同门序）----
        stage = "document_content_v2"
        t0 = time.monotonic()
        _notify(progress, "document_content_v2", "started")
        content_rid = _read_domain_result_id(
            "document_content_v2", output, task_root, task_id
        )
        if content_rid is None and not allow_llm:
            raise M2PipelineError(
                "LLM_APPROVAL_REQUIRED", failed_stage="document_content_v2"
            )
        if content_rid is not None:
            content_decision = evaluate_input_budget(
                "content_model", None, planned_business_calls=0
            )
        else:
            preview: dict[str, Any] | None
            try:
                preview = build_content_composition_dry_run(
                    output, task_id, model_registry=model_registry,
                    **({"bounded": True} if bounded_content else {}),
                )
            except DocumentComposerV2Error as exc:
                raise _module_envelope(exc, "document_content_v2") from exc
            except Exception:
                preview = None
            if preview is None:
                content_decision = evaluate_input_budget(
                    "content_model",
                    None,
                    planned_business_calls=M2_CALL_BUDGETS["content_model"][
                        "business_call_hard_limit"
                    ],
                )
            else:
                slots = [
                    {
                        "message_chars": call["message_chars"],
                        "message_bytes": call["message_bytes"],
                    }
                    for call in preview["calls"]
                ]
                content_decision = evaluate_input_budget(
                    "content_model",
                    slots,
                    planned_business_calls=int(preview["budget"]["expected_business_calls"]),
                )
        if bounded_content:
            content_decision = replace(content_decision, business_call_hard_limit=12, supplier_request_hard_limit=12)
        budget_decisions["document_content_v2"] = content_decision
        if content_decision.status == "exceeded":
            raise M2PipelineError(
                "INPUT_BUDGET_EXCEEDED", failed_stage="document_content_v2"
            )
        if content_decision.status == "unknown":
            raise M2PipelineError(
                "INPUT_BUDGET_UNKNOWN", failed_stage="document_content_v2"
            )
        # B2修正：同boundary角色——预算行先于content首个供应商调用（决定13）。
        _notify(
            progress,
            "document_content_v2",
            "started",
            {"input_budget": content_decision.to_json()},
        )
        content_result = compose_document_v2(
            output_root,
            task_id,
            **({"bounded": True, "preserve_published": True} if bounded_content else {}),
            model_registry=model_registry,
            transport=_pick_transport(
                "content_model",
                "document_content_v2",
                allow_llm=allow_llm,
                transports=transports,
            ),
        )
        role_counts["content_model"] = (
            {"business_calls": 0, "supplier_requests": 0}
            if content_result.cache_hit
            else {
                "business_calls": int(content_result.business_call_count),
                "supplier_requests": int(content_result.supplier_request_count),
            }
        )
        cache_hits["document_content_v2"] = bool(content_result.cache_hit)
        if not content_result.cache_hit:
            published.append("document_content_v2")
        done(
            "document_content_v2",
            "cache_hit" if content_result.cache_hit else "completed",
            t0,
            result_id=content_result.result_id,
            input_budget=content_decision,
        )

        # ---- export_v2 ----
        stage = "export_v2"
        t0 = time.monotonic()
        _notify(progress, "export_v2", "started")
        export_result = export_document_v2(output_root, task_id)
        cache_hits["export_v2"] = bool(export_result.cache_hit)
        if not export_result.cache_hit:
            published.append("export_v2")
        export_reasons = tuple(export_result.degraded_reasons)
        if export_reasons:
            degraded.append("export_v2")
            reason_codes.extend(export_reasons)
        done(
            "export_v2",
            "cache_hit"
            if export_result.cache_hit
            else ("degraded" if export_reasons else "completed"),
            t0,
            result_id=export_result.result_id,
            reasons=export_reasons,
        )

        # ---- completed：权威current复读（指针级复验；损坏→failed_stage=completed）----
        stage = "completed"
        result_ids: dict[str, Any] = {}
        relative_paths: dict[str, str] = {}
        for domain in _M2_ARTIFACT_STAGES:
            if domain == "ocr" and ocr_mode == "off":
                # 裁决§三.6字面「ocr=off→null」：条目摘要的ocr域恒报null，
                # 即使模块照常发布了disabled组（组身份如实留在域内与JSONL事件，
                # 摘要层不暴露该id、不入relative_paths）。实现决定11登记。
                result_ids["ocr"] = None
                continue
            rid = _read_domain_result_id(domain, output, task_root, task_id)
            if rid is None:
                continue
            result_ids[domain] = rid
            relative_paths[domain] = _domain_relative(domain, rid)
        t0 = time.monotonic()
        done("completed", "completed", t0)
        return M2RunSummary(
            task_id=task_id,
            result_ids=result_ids,
            cache_hits=cache_hits,
            degraded=degraded,
            reason_codes=reason_codes,
            relative_paths=relative_paths,
            call_counts={
                "this_run": role_counts,
                "published_this_run": published,
            },
        )
    except M2PipelineError as exc:
        decision = budget_decisions.get(exc.failed_stage)
        journal.event(
            exc.failed_stage,
            "failed",
            reason_codes=exc.reason_codes,
            input_budget=None if decision is None else decision.to_json(),
        )
        raise
    except M2EnvelopeFailure as failure:
        journal.event(
            failure.failed_stage, "failed", reason_codes=failure.reason_codes
        )
        raise
    except _MODULE_FAILURE_TYPES as exc:
        failure = _module_envelope(exc, stage)
        journal.event(stage, "failed", reason_codes=failure.reason_codes)
        raise failure from exc


def run_task_m2(
    output_root: str | Path,
    task_id: str,
    *,
    source_video: str | Path | None = None,
    ffmpeg_dir: str | Path | None = None,
    ocr_mode: str = "auto",
    vision_caption: str = "off",
    model_registry: str | Path | None = None,
    allow_llm: bool = False,
    dry_run: bool = False,
    progress: Callable[..., None] | None = None,
    _transports: Mapping[str, Any] | None = None,
    bounded_content: bool = False,
) -> M2RunPlan | M2RunSummary:
    """run-m2公开入口（裁决§三.1落名；§四E0—E3全部接线）。

    - ``dry_run=True``：返回``M2RunPlan``（单条运行计划JSON，由调用方打印）；
      全程零写入、零transport构造、永不联网（无论``allow_llm``与否）；
    - 普通运行：返回``M2RunSummary``（P4 §5.3字面8键成功摘要）。
      本轮零真实API：``allow_llm=True``才由14-B/14-C模块构造真实传输（E4/E5
      子种子首跑须用户另行批准，§4.3/§六.5）；未批准时计划内调用>0在首个
      供应商请求前抛条目``LLM_APPROVAL_REQUIRED``。

    媒体流探测（``probe_video``）在preflight后、任何task_init写入之前完成，
    dry-run与普通路径皆然（E0+E1审查报告OB-2绑定条款）。
    """

    if ocr_mode not in OCR_MODES:
        raise ValueError(f"未知--ocr取值：{ocr_mode}")
    if vision_caption not in M2_VISION_CAPTION_VALUES:
        raise ValueError(f"--vision-caption当前仅接受off：{vision_caption}")

    _notify(progress, "preflight", "started")
    preflight = preflight_task_m2(
        output_root,
        task_id,
        source_video=source_video,
        ffmpeg_dir=ffmpeg_dir,
    )
    _notify(progress, "preflight", "completed")

    journal = _RunJournal(
        preflight.task_root / "m2" / "logs" / "run-m2.jsonl"
    )
    journal.event("preflight", "completed")

    # OB-2：探测先于一切task_init写入；同时取源内容哈希供计划/摘要。
    with _media_tool_path(preflight.media_tools):
        metadata = probe_video(preflight.source_video)
        source_sha256 = _file_sha256(preflight.source_video)

    if dry_run:
        return _assemble_run_plan(
            preflight,
            metadata,
            source_sha256,
            ocr_mode=ocr_mode,
            vision_caption=vision_caption,
            allow_llm=allow_llm,
            model_registry=model_registry,
            **({"bounded_content": True} if bounded_content else {}),
        )

    with _media_tool_path(preflight.media_tools):
        return _execute_run(
            output_root,
            preflight,
            metadata,
            ocr_mode=ocr_mode,
            model_registry=model_registry,
            allow_llm=allow_llm,
            ffmpeg_dir=ffmpeg_dir,
            progress=progress,
            journal=journal,
            transports=_transports,
            bounded_content=bounded_content,
        )
