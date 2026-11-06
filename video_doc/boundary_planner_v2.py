"""BoundaryPlannerV2：UnifiedTimeline 2.0之上的LLM章节边界决策与不可变发布（阶段14-B）。

严格按《阶段14-B-分章策略裁决》逐条实现：

- 输入：只读已发布Timeline 2.0正式组（``load_timeline_current``＋
  ``load_published_timeline``）；``run_status=degraded``的时间轴允许分章，
  ``failed``拒绝；
- 响应契约（裁决A1）：``{"boundaries":[{"source_event_id","title"}...]}``，
  按模型原始顺序处理，不排序、不去重、不合并；模型从不输出timestamp；
- prompt（裁决B1/C/E）：只序列化transcript事件；不压缩、不截断；分章政策行
  逐字复用M1 ``chapter_policy.render_stability_policy``（不得修改该模块）；
- 校验（裁决§G/§3.2）：11个业务校验码＋规范ChapterPlan 2.0字段级校验；
- 预算（裁决F.8/§3.4）：每视频1次业务调用、至多2次供应商请求、共用同一次
  修正机会；两种失败都落空时零API发布emergency单章“完整内容”，以degraded
  成功退出；
- 发布（裁决F.6）：锁→同盘临时组→fsync写字节→manifest最后→临时校验→
  ``os.replace``→正式复验→current原子切换；失败清理与审计以磁盘current为
  唯一事实；缓存身份为``BoundaryPlannerV2CacheIdentity/1.0``；
- 角色配置（裁决F.1/F.2＋P3 §二/§三.1/§六）：三级解析、启用身份字段校验、
  fixture整体拒绝与实际prompt内容哈希比对、role_cache_identity进
  ``m2/boundaries/llm-cache/``请求缓存。

本模块14-B验收全程离线：``transport``为注入点，定向测试与DEV01复跑均零真实
网络/API；CLI默认接线``OpenAICompatibleTransport``但真实调用属挂账项，须用户
另行批准（P3 §七/§八）。M1的``plan-chapters``/``formal_chaptering``与本模块
零交集；M1与全部既有正式组只读。

实现决策（提交G1核对）：

1. ChapterPlan 2.0的``parameters``为5字段
   （result_source/quality_level/prompt_version/min_chapter_ms/boundary_alignment），
   即裁决D要求“result_source/quality_level在parameters中冗余记录”的Schema增量；
2. ``model_roles``层新增请求缓存条目``BoundaryRoleCallCache/1.0``，键集合为P3
   §六列举内容字段＋``execution_errors``（修正轮错误清单）＋审计字段
   ``created_at``＋``credential_present``＋身份镜像``backend``/``model``；
   ``execution_errors``进条目是执行报告字节级可复现的前提（报告不含时间戳）；
3. 执行报告``execution-report.json``不含``created_at``/耗时/路径：一切字段由
   内容身份、冻结语义与确定性错误清单决定，保证缓存复跑逐字节一致（裁决F.6
   “byte-identical→复用”的可判定的前提）；
4. ``MIN_CHAPTER_MS=60000``为冻结常量而非CLI参数（裁决§E.5）；
   ``DURATION_TOO_SHORT``判据``duration_ms<=60000``按裁决逐字实现（``<=``），
   与M1``duration<minimum``的旧判据不同，属14-B裁决明文；
5. task.json缺失与task_id不一致都归``SOURCE_VIDEO_MISMATCH``（裁决§3.3词表内
   最贴合项，消息不含绝对路径）；
6. 基类``BoundaryPlannerV2Error.error_code=BOUNDARY_PLAN_FAILED``沿用
   ``TIMELINE_BUILD_FAILED``先例，只用于非法内部调用（未知注入点名），不出现在
   裁决词表的正常路径上；
7. ``run_status=failed``的主时间轴与``transcript``事件数为0的时间轴都以
   ``INPUT_TIMELINE_CORRUPT``硬失败（裁决词表无专用码；failed拒绝为裁决明文，
   空主文本无法构造A1首章规则所要求的“首个主文本事件”）；
8. 冻结生成参数（model=qwen3.7-flash、temperature=0.0、enable_thinking=false、
   max_tokens=null、timeout=60.0、response_format={"type":"json_object"}、
   fallback=none、预算1/2、max_attempts=2、合同/校验/命名空间字符串）由本模块
   ``_enforce_frozen_role``逐项等值核对（裁决“冻结身份字符串”）；loader只校
   Schema形态。``model_revision``/``revision_policy``/``role_config_version``
   不冻结取值，仅进缓存身份；
9. fixture拒绝位置：loader校形态（fixture必须通过形态校验以证明Schema覆盖），
   本模块在使用点拒绝``fixture_only``/``fixture_hash_only``/内容哈希不符；
10. ``after_inputs``注入点位于锁内（与timeline不同）：14-B的audit边界从锁开始，
    该点失败会写``stage=prepare``审计且零产物；其余注入点与13-1同位；
11. current缺失但目标组完整时：仅补指针（``pointer_repaired=true``），不重新
    调用模型的前提是llm-cache命中或current早期命中；两者皆无时按裁决F.6允许
    真实调用一次（预算内），产出不同则``CACHE_CORRUPT``拒绝覆盖；
12. degraded（emergency）组在current早期命中路径按冻结语义直接复现、不重试
    模型（裁决F.6缓存命中零API适用于任何已发布组，不区分primary/emergency）；
13. transport返回形态兼容（2026-10-18真实调用验收缺陷修正，与14-C
    ``_parse_response_text``同型）：真实``OpenAICompatibleTransport.complete``
    返回content字符串（M1由``call_task_llm``内部再解析；本模块直接复用时曾
    丢失该步，导致真实链必然``RESPONSE_NOT_OBJECT``）。规范化前先对str做
    ``json.loads``；非法JSON或解析结果非对象统一落既有
    ``RESPONSE_NOT_OBJECT``语义，词汇表、缓存形态（raw_response恒为对象）与
    纠错回声字节均不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from video_doc.chapter_policy import STABILITY_POLICY_VERSION, render_stability_policy
from video_doc.frame_sampler import artifact_group_path, is_safe_result_id
from video_doc.llm_adapter import LLMConfig, LLMTransport, OpenAICompatibleTransport
from video_doc.model_roles import (
    BOUNDARY_ROLE_NAME,
    LlmRoleConfig,
    ModelRoleRegistryError,
    RegistrySnapshot,
    build_llm_config,
    compute_role_cache_identity,
    resolve_api_key,
    resolve_model_registry,
)
from video_doc.task_workspace import validate_task_id
from video_doc.timeline_builder_v2 import (
    TimelineBuilderV2Error,
    load_published_timeline,
    load_timeline_current,
)
from video_doc.unified_timeline import (
    TRANSCRIPT_EVENT_TYPE,
    UnifiedTimelineError,
    canonical_json_bytes,
    canonical_json_sha256,
)

# ---------------------------------------------------------------------------
# 冻结身份字符串（裁决§一.8/§E.5，逐字）
# ---------------------------------------------------------------------------

BOUNDARY_PLANNER_VERSION = "1.0"
CHAPTER_PLAN_SCHEMA_NAME = "ChapterPlan"
CHAPTER_PLAN_SCHEMA_VERSION = "2.0"
BOUNDARY_ARTIFACT_TYPE = "chapter_plan"
BOUNDARY_MODULE_TOKEN = "boundary_planner_v2"
BOUNDARY_ROOT_DIRECTORY = "boundaries"
BOUNDARY_GROUP_DIRECTORY = "formal-results"
BOUNDARY_LLM_CACHE_DIRECTORY = "llm-cache"
BOUNDARY_CURRENT_FILE = "current.json"
BOUNDARY_LOCK_FILE = "boundaries.lock"
BOUNDARY_PLAN_FILE = "chapter-plan.json"
BOUNDARY_RAW_FILE = "raw-response.json"
BOUNDARY_REPORT_FILE = "execution-report.json"
BOUNDARY_MANIFEST_FILE = "manifest.json"

PROMPT_VERSION = "chapter-boundary-unified-timeline-2.0"
EMERGENCY_PROMPT_VERSION = "local-emergency-single-chapter-2.0"
VALIDATION_ID = "chapter-plan-validator-2-0"
VALIDATION_RETRY_POLICY = "validation-feedback-1.0"
CACHE_NAMESPACE = "boundary-model-unified-timeline-2-0"
INPUT_CONTRACT = "UnifiedTimeline/2.0"
OUTPUT_CONTRACT = "ChapterPlan/2.0"
BOUNDARY_ALIGNMENT = "selected_transcript_event_start"
MIN_CHAPTER_MS = 60000

FROZEN_MODEL = "qwen3.7-flash"
PUBLIC_ANONYMOUS_PROFILE = "pollinations_anonymous"
PUBLIC_ANONYMOUS_MODEL = "openai"
PUBLIC_ANONYMOUS_BASE_URL = "https://text.pollinations.ai/openai"
FROZEN_RESPONSE_FORMAT: dict[str, Any] = {"type": "json_object"}
FROZEN_ALTERNATE_FALLBACK = "none"
FROZEN_MAX_ATTEMPTS = 2
FROZEN_BUSINESS_BUDGET = 1
FROZEN_SUPPLIER_BUDGET = 2
EMERGENCY_TITLE = "完整内容"
MODEL_ROLE_REVISION = "1.0"

MANIFEST_SCHEMA_NAME = "ArtifactGroupManifest"
MANIFEST_SCHEMA_VERSION = "1.0"
CURRENT_SCHEMA_NAME = "ArtifactCurrentPointer"
CURRENT_SCHEMA_VERSION = "1.0"
FAILURE_SCHEMA_NAME = "ModuleFailureAudit"
FAILURE_SCHEMA_VERSION = "1.0"
CACHE_SCHEMA_NAME = "BoundaryPlannerV2CacheIdentity"
CACHE_SCHEMA_VERSION = "1.0"
LLM_CACHE_SCHEMA_NAME = "BoundaryRoleCallCache"
LLM_CACHE_SCHEMA_VERSION = "1.0"
REPORT_SCHEMA_NAME = "BoundaryExecutionReport"
REPORT_SCHEMA_VERSION = "1.0"
PROMPT_TEMPLATE_SCHEMA_NAME = "BoundaryPromptTemplate"
PROMPT_TEMPLATE_SCHEMA_VERSION = "1.0"

RESULT_ID_PREFIX = "chapter-plan-"

# 命令级失败码（裁决§3.3）
ERROR_BOUNDARY_PLAN_FAILED = "BOUNDARY_PLAN_FAILED"
ERROR_SOURCE_VIDEO_MISMATCH = "SOURCE_VIDEO_MISMATCH"
ERROR_INPUT_TIMELINE_MISSING = "INPUT_TIMELINE_MISSING"
ERROR_INPUT_TIMELINE_CORRUPT = "INPUT_TIMELINE_CORRUPT"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_ROLE_CONFIG_MISSING = "ROLE_CONFIG_MISSING"
ERROR_ROLE_CONFIG_INVALID = "ROLE_CONFIG_INVALID"
ERROR_DURATION_TOO_SHORT = "DURATION_TOO_SHORT"
ERROR_BOUNDARY_MODEL_FAILED = "BOUNDARY_MODEL_FAILED"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"

# 业务校验码（裁决§G；只进修正反馈与执行报告errors[]，不进命令级）
BUSINESS_CODES = frozenset(
    {
        "RESPONSE_NOT_OBJECT",
        "BOUNDARIES_MISSING",
        "BOUNDARY_NOT_OBJECT",
        "TITLE_EMPTY",
        "EVENT_ID_MISSING",
        "EVENT_NOT_FOUND",
        "EVENT_NOT_BOUNDARY_ELIGIBLE",
        "FIRST_EVENT_VIOLATION",
        "DUPLICATE_EVENT_ID",
        "ORDER_VIOLATION",
        "MIN_CHAPTER_VIOLATION",
    }
)

_VALIDATION_FEEDBACK = "保持标题和章节顺序不变，只修正无效条目，返回完整JSON对象。"
_TRANSPORT_FEEDBACK_INSTRUCTION = "请只返回符合规定格式的完整JSON对象。"

_FAILURE_INJECTION_POINTS = (
    "after_inputs",
    "before_model_call",
    "after_model_call",
    "before_group_publish",
    "after_group_publish",
    "before_current",
)

_CURRENT_KEYS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "result_id",
    "group",
    "manifest_sha256",
}
_MANIFEST_KEYS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "task_id",
    "result_id",
    "status",
    "cache_identity_sha256",
    "inputs",
    "versions",
    "files",
}
_MANIFEST_INPUT_KEYS = {
    "timeline_id",
    "timeline_sha256",
    "timeline_manifest_sha256",
}
_MANIFEST_VERSION_KEYS = {
    "boundary_planner_v2",
    "chapter_plan_schema",
    "artifact_group_manifest",
    "prompt_version",
    "validation_id",
    "validation_retry_policy",
}
_PLAN_TOP_KEYS = {
    "schema_name",
    "schema_version",
    "task_id",
    "strategy",
    "source_timeline",
    "duration_ms",
    "parameters",
    "boundaries",
}
_SOURCE_TIMELINE_KEYS = {
    "schema_name",
    "schema_version",
    "timeline_id",
    "sha256",
    "event_count",
    "transcript_event_count",
}
_PLAN_PARAMETERS_KEYS = {
    "result_source",
    "quality_level",
    "prompt_version",
    "min_chapter_ms",
    "boundary_alignment",
}
_PLAN_BOUNDARY_KEYS = {"timestamp_ms", "title", "source_event_id"}
_REPORT_KEYS = {
    "schema_name",
    "schema_version",
    "task_id",
    "module",
    "status",
    "result_source",
    "quality_level",
    "timeline_id",
    "timeline_sha256",
    "duration_ms",
    "boundary_count",
    "business_call_count",
    "supplier_request_count",
    "errors",
    "credential_present",
}
_REPORT_ERROR_KEYS = {"attempt", "kind", "message", "issues"}
_REPORT_ISSUE_KEYS = {"code", "chapter_index", "title", "source_event_id", "message"}
_LLM_CACHE_KEYS = {
    "schema_name",
    "schema_version",
    "role_cache_identity_sha256",
    "messages_sha256",
    "raw_response",
    "normalized_response",
    "execution_errors",
    "supplier_request_count",
    "backend",
    "model",
    "created_at",
    "credential_present",
}
_CACHE_IDENTITY_KEYS = {
    "schema_name",
    "schema_version",
    "boundary_planner_v2",
    "chapter_plan_schema",
    "timeline_id",
    "timeline_sha256",
    "timeline_manifest_sha256",
    "role_cache_identity_sha256",
    "min_chapter_ms",
    "boundary_alignment",
    "prompt_version",
    "validation_id",
    "validation_retry_policy",
    "emergency_policy_version",
}


class BoundaryPlannerV2Error(RuntimeError):
    error_code = ERROR_BOUNDARY_PLAN_FAILED
    recoverable = False
    next_action = "none"


class SourceVideoMismatchError(BoundaryPlannerV2Error):
    error_code = ERROR_SOURCE_VIDEO_MISMATCH
    recoverable = False
    next_action = "none"


class InputTimelineMissingError(BoundaryPlannerV2Error):
    error_code = ERROR_INPUT_TIMELINE_MISSING
    recoverable = True
    next_action = "run_build_timeline_v2"


class InputTimelineCorruptError(BoundaryPlannerV2Error):
    error_code = ERROR_INPUT_TIMELINE_CORRUPT
    recoverable = False
    next_action = "none"


class CurrentPointerCorruptError(BoundaryPlannerV2Error):
    error_code = ERROR_CURRENT_POINTER_CORRUPT
    recoverable = False
    next_action = "none"


class BoundaryCacheCorruptError(BoundaryPlannerV2Error):
    error_code = ERROR_CACHE_CORRUPT
    recoverable = False
    next_action = "none"


class BoundaryConcurrentRunError(BoundaryPlannerV2Error):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "retry_after_other_run_finishes"


class RoleConfigMissingError(BoundaryPlannerV2Error):
    error_code = ERROR_ROLE_CONFIG_MISSING
    recoverable = True
    next_action = "provide_model_registry"


class RoleConfigInvalidError(BoundaryPlannerV2Error):
    error_code = ERROR_ROLE_CONFIG_INVALID
    recoverable = True
    next_action = "fix_model_registry"


class DurationTooShortError(BoundaryPlannerV2Error):
    error_code = ERROR_DURATION_TOO_SHORT
    recoverable = False
    next_action = "none"


class BoundaryModelFailedError(BoundaryPlannerV2Error):
    """模型路径耗尽预算仍未获得合法响应；只在emergency判定内部使用。"""

    error_code = ERROR_BOUNDARY_MODEL_FAILED
    recoverable = True
    next_action = "review_boundary_model_response"

    def __init__(self, message: str, *, errors: Sequence[dict[str, Any]], supplier_request_count: int) -> None:
        super().__init__(message)
        self.errors = list(errors)
        self.supplier_request_count = supplier_request_count


class BoundaryPublishFailedError(BoundaryPlannerV2Error):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = True
    next_action = "retry_or_inspect_failures"


class InjectedBoundaryFailure(BoundaryPlannerV2Error):
    recoverable = True
    next_action = "none"


class ChapterPlanValidationError(RuntimeError):
    """ChapterPlan 2.0字段级/交叉校验失败（裁决§3.2）。"""


@dataclass(frozen=True)
class TranscriptView:
    """时间轴主文本的prompt序列化视图。"""

    events: tuple[dict[str, Any], ...]
    first_event_id: str
    language_code: str
    duration_ms: int


@dataclass(frozen=True)
class BoundaryPlanResult:
    group_path: Path
    result_id: str
    plan: dict[str, Any]
    manifest: dict[str, Any]
    report: dict[str, Any]
    status: str  # success | degraded
    result_source: str  # primary | emergency
    quality_level: str  # normal | degraded
    boundary_count: int
    duration_ms: int
    timeline_id: str
    cache_hit: bool
    pointer_repaired: bool
    business_call_count: int
    supplier_request_count: int


# ---------------------------------------------------------------------------
# 通用helpers（与13-1/F1/F2/OCR逐点对齐）
# ---------------------------------------------------------------------------


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(
    path: Path, label: str, error_type: type[BoundaryPlannerV2Error]
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise error_type(f"{label}无法读取或JSON无效。") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label}必须是JSON对象。")
    return value


def _current_path_state(
    path: Path, label: str, error_type: type[BoundaryPlannerV2Error]
) -> bool:
    """current路径是普通文件时返回True，不存在时返回False；其余一律硬失败。"""

    if path.is_symlink():
        raise error_type(f"{label}存在但不是普通文件。")
    if path.is_file():
        return True
    if path.exists():
        raise error_type(f"{label}存在但不是普通文件。")
    return False


def _exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str, error_type: type
) -> None:
    if set(value) != expected:
        raise error_type(f"{label}字段集合无效。")


def _safe_relative_file(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _write_fsync(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    )


def _sanitize_echo(value: Any) -> str | None:
    """错误清单回显字段脱敏：折叠空白、限长80字符（裁决§G脱敏文本）。"""

    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    if len(collapsed) > 80:
        return collapsed[:79] + "…"
    return collapsed


def _safe_error_text(error: BaseException, api_key: str) -> str:
    text = str(error) or type(error).__name__
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    collapsed = " ".join(text.split())
    if len(collapsed) > 300:
        collapsed = collapsed[:299] + "…"
    return collapsed


# ---------------------------------------------------------------------------
# 输入：task身份、Timeline 2.0只读加载、主文本视图
# ---------------------------------------------------------------------------


def _ensure_task_identity(output_root: Path, task_id: str) -> None:
    """task.json缺失或与task_id不一致都是SOURCE_VIDEO_MISMATCH（决策5）。"""

    task_json = output_root / task_id / "task.json"
    if not task_json.is_file():
        raise SourceVideoMismatchError(
            f"{ERROR_SOURCE_VIDEO_MISMATCH}：任务工作区缺少task.json登记信息。"
        )
    record = _read_json(task_json, "task.json", SourceVideoMismatchError)
    recorded = record.get("task_id")
    if recorded != task_id:
        raise SourceVideoMismatchError(
            f"{ERROR_SOURCE_VIDEO_MISMATCH}：task.json记录的task_id与请求不一致。"
        )


def _load_timeline_inputs(output_root: Path, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """只读加载current指向的Timeline 2.0组；返回(pointer, index)。"""

    try:
        pointer = load_timeline_current(output_root, task_id)
    except TimelineBuilderV2Error as exc:
        raise InputTimelineCorruptError(f"{ERROR_INPUT_TIMELINE_CORRUPT}：{exc}") from exc
    if pointer is None:
        raise InputTimelineMissingError(
            f"{ERROR_INPUT_TIMELINE_MISSING}：任务没有已发布的Timeline 2.0 current。"
        )
    try:
        index = load_published_timeline(output_root, task_id)
    except (TimelineBuilderV2Error, UnifiedTimelineError) as exc:
        raise InputTimelineCorruptError(f"{ERROR_INPUT_TIMELINE_CORRUPT}：{exc}") from exc
    if index is None:
        raise InputTimelineMissingError(
            f"{ERROR_INPUT_TIMELINE_MISSING}：任务没有已发布的Timeline 2.0 current。"
        )
    return pointer, index


def _transcript_view(index: Mapping[str, Any]) -> TranscriptView:
    run_status = index.get("run_status")
    if run_status == "failed":
        raise InputTimelineCorruptError(
            f"{ERROR_INPUT_TIMELINE_CORRUPT}：Timeline 2.0的run_status为failed，不能作为分章输入。"
        )
    events = [
        event for event in index["events"] if event["type"] == TRANSCRIPT_EVENT_TYPE
    ]
    if not events:
        raise InputTimelineCorruptError(
            f"{ERROR_INPUT_TIMELINE_CORRUPT}：Timeline 2.0不含任何主文本事件，无法建立章节边界。"
        )
    return TranscriptView(
        events=tuple(events),
        first_event_id=str(events[0]["id"]),
        language_code=str(index["language"]["code"]),
        duration_ms=int(index["duration_ms"]),
    )


# ---------------------------------------------------------------------------
# Prompt（裁决B/C/E）
# ---------------------------------------------------------------------------


def _format_hms_ms(value_ms: int) -> str:
    hours, rest = divmod(int(value_ms), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


BOUNDARY_SYSTEM_PROMPT = (
    "你负责给知识型视频划分章节。只依据给出的完整主文本时间轴判断主题变化，"
    "只输出一个JSON对象，不要输出解释、Markdown或章节摘要。"
)

_BOUNDARY_RULES = (
    "boundaries只表示章节起点。每条边界必须同时返回一个简短标题和一个真实存在于输入的"
    "source_event_id；不得自行估算或输出timestamp。\n"
    "第一章必须选择首个主文本事件{first_event_id}；后续条目必须沿时间轴顺序严格递增，"
    "不得重复，不得选择标注为（不可选边界）的事件。\n"
    "每章（含最后一章）不得少于{min_chapter_seconds}秒。\n"
    "标题与事件ID一一对应，不得为修复顺序而交换标题。\n"
    "标题简洁并与主文本语言一致。"
)

_BOUNDARY_RESPONSE_EXAMPLE = (
    '{"boundaries":[{"source_event_id":"te000001","title":"..."},'
    '{"source_event_id":"te000042","title":"..."}]}'
)

_BOUNDARY_TIMELINE_INTRO = "完整主文本时间轴如下："


def _render_boundary_header(view: TranscriptView) -> str:
    # 裁决C.3逐字。
    return (
        f"视频时长为{view.duration_ms / 1000:.3f}秒，"
        f"主文本语言为{view.language_code}，"
        f"主文本事件共{len(view.events)}条。"
    )


def _render_boundary_timeline(view: TranscriptView) -> str:
    """裁决C.1/C.2：逐条transcript事件序列化，从不压缩或截断。"""

    lines: list[str] = []
    for event in view.events:
        payload = event["payload"]
        pause_ms = int(payload["pause_before_ms"])
        pause_part = f" pause_before={pause_ms / 1000:.3f}s" if pause_ms else ""
        marker = "" if payload["boundary_eligible"] else "（不可选边界）"
        lines.append(
            f"[{event['id']}] {_format_hms_ms(int(event['start_ms']))}-"
            f"{_format_hms_ms(int(event['end_ms']))}{pause_part} "
            f"{payload['text']}{marker}"
        )
    return "\n".join(lines)


def build_boundary_messages(view: TranscriptView) -> list[dict[str, str]]:
    """裁决E：system逐字；user按版本行→头部→分章政策行→边界规则→返回格式→时间轴。"""

    rules = _BOUNDARY_RULES.format(
        first_event_id=view.first_event_id,
        min_chapter_seconds=f"{MIN_CHAPTER_MS / 1000:.3f}",
    )
    policy = render_stability_policy(view.duration_ms / 1000.0)
    user = "\n".join(
        [
            f"提示词版本：{PROMPT_VERSION}。",
            _render_boundary_header(view),
            policy,
            rules,
            f"返回格式必须是：{_BOUNDARY_RESPONSE_EXAMPLE}。",
            _BOUNDARY_TIMELINE_INTRO,
            _render_boundary_timeline(view),
        ]
    )
    return [
        {"role": "system", "content": BOUNDARY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _prompt_template_identity() -> dict[str, Any]:
    """裁决E.4：内容哈希覆盖“渲染前模板＋版本常量”，数据值由输入canonical哈希覆盖。"""

    return {
        "schema_name": PROMPT_TEMPLATE_SCHEMA_NAME,
        "schema_version": PROMPT_TEMPLATE_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "policy_version": STABILITY_POLICY_VERSION,
        "min_chapter_ms": MIN_CHAPTER_MS,
        "boundary_alignment": BOUNDARY_ALIGNMENT,
        "emergency_policy_version": EMERGENCY_PROMPT_VERSION,
        "system": BOUNDARY_SYSTEM_PROMPT,
        "user_sections": [
            "提示词版本：{prompt_version}。",
            "视频时长为{duration_seconds:.3f}秒，主文本语言为{language}，主文本事件共{transcript_event_count}条。",
            "{policy}",
            _BOUNDARY_RULES,
            f"返回格式必须是：{_BOUNDARY_RESPONSE_EXAMPLE}。",
            _BOUNDARY_TIMELINE_INTRO,
            "[teNNNNNN] HH:MM:SS.mmm-HH:MM:SS.mmm[ pause_before=X.XXXs] 文本[（不可选边界）]",
        ],
    }


def boundary_prompt_content_sha256() -> str:
    return canonical_json_sha256(_prompt_template_identity())


def measure_boundary_egress(
    output_root: str | Path,
    task_id: str,
    *,
    model_registry: str | Path | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """run-m2入口预算门的纯增量测量面（阶段15-0裁决§一1.4第1项）。

    复用既有只读helpers构造将外发的全messages后，按裁决§一1.3度量：
    ``message_chars``＝各message content文本字符数之和（system＋user，
    ``build_boundary_messages``产物口径）；``message_bytes``＝同一文本串的
    UTF-8字节数。零写入、零API、不构造transport；成员与
    ``stage14_real_egress_preview``登记的boundary预览同构。
    ``model_registry``/``project_root``为与其他公开入口的参数面对齐，不改变
    被测文本（度量对象是messages文本本身）。
    """

    output = Path(output_root).expanduser()
    _pointer, index = _load_timeline_inputs(output, task_id)
    view = _transcript_view(index)
    messages = build_boundary_messages(view)
    return {
        "message_chars": sum(len(str(message["content"])) for message in messages),
        "message_bytes": sum(
            len(str(message["content"]).encode("utf-8")) for message in messages
        ),
        "timeline_event_count": len(view.events),
        "language_code": view.language_code,
        "duration_ms": view.duration_ms,
    }


# ---------------------------------------------------------------------------
# 响应规范化与业务校验（裁决A1/§G）
# ---------------------------------------------------------------------------


def _issue(
    code: str,
    chapter_index: int,
    *,
    title: Any = None,
    source_event_id: Any = None,
    message: str,
) -> dict[str, Any]:
    if code not in BUSINESS_CODES:
        raise RuntimeError(f"未知业务校验码：{code}")
    return {
        "code": code,
        "chapter_index": chapter_index,
        "title": _sanitize_echo(title),
        "source_event_id": _sanitize_echo(source_event_id),
        "message": message,
    }


def _event_positions(view: TranscriptView) -> dict[str, int]:
    return {str(event["id"]): position for position, event in enumerate(view.events)}


def normalize_boundary_response(
    response: Any, view: TranscriptView
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按模型原始顺序逐条规范化（裁决A1）；不排序、不去重、不合并、不补位。

    返回(规范化boundaries, 业务校验issues)。issues非空时规范化结果不可信。
    """

    if not isinstance(response, dict):
        return [], [_issue("RESPONSE_NOT_OBJECT", 0, message="模型响应不是单一JSON对象。")]
    raw_boundaries = response.get("boundaries")
    if not isinstance(raw_boundaries, list) or not raw_boundaries:
        return [], [_issue("BOUNDARIES_MISSING", 0, message="响应缺少非空boundaries数组。")]

    all_event_ids = {str(event["id"]) for event in view.events}
    eligible_by_id = {
        str(event["id"]): event for event in view.events if event["payload"]["boundary_eligible"]
    }
    positions = _event_positions(view)

    issues: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    previous_position: int | None = None
    previous_id: str | None = None

    for chapter_index, entry in enumerate(raw_boundaries, start=1):
        if not isinstance(entry, dict):
            issues.append(
                _issue(
                    "BOUNDARY_NOT_OBJECT",
                    chapter_index,
                    message="该条目不是JSON对象。",
                )
            )
            continue
        title_raw = entry.get("title")
        title = _sanitize_echo(title_raw)
        event_id_raw = entry.get("source_event_id")
        event_id = event_id_raw if isinstance(event_id_raw, str) else None

        if not isinstance(title_raw, str) or not title_raw.strip():
            issues.append(
                _issue(
                    "TITLE_EMPTY",
                    chapter_index,
                    source_event_id=event_id_raw,
                    message="标题为空或缺失。",
                )
            )
        if event_id is None:
            issues.append(
                _issue(
                    "EVENT_ID_MISSING",
                    chapter_index,
                    title=title_raw,
                    message="缺少字符串类型的source_event_id。",
                )
            )
            continue
        if event_id not in all_event_ids:
            issues.append(
                _issue(
                    "EVENT_NOT_FOUND",
                    chapter_index,
                    title=title_raw,
                    source_event_id=event_id,
                    message="source_event_id不存在于时间轴。",
                )
            )
            continue
        if event_id not in eligible_by_id:
            issues.append(
                _issue(
                    "EVENT_NOT_BOUNDARY_ELIGIBLE",
                    chapter_index,
                    title=title_raw,
                    source_event_id=event_id,
                    message="该事件存在但不是可选边界的主文本事件。",
                )
            )
            continue
        if chapter_index == 1:
            if event_id != view.first_event_id:
                issues.append(
                    _issue(
                        "FIRST_EVENT_VIOLATION",
                        chapter_index,
                        title=title_raw,
                        source_event_id=event_id,
                        message=f"第一章必须选择首个主文本事件{view.first_event_id}。",
                    )
                )
                continue
            normalized.append(
                {"timestamp_ms": 0, "title": title_raw.strip(), "source_event_id": None}
            )
            previous_position = 0
            previous_id = event_id
            continue

        position = positions[event_id]
        if previous_id is not None and event_id == previous_id:
            issues.append(
                _issue(
                    "DUPLICATE_EVENT_ID",
                    chapter_index,
                    title=title_raw,
                    source_event_id=event_id,
                    message="事件ID与前一条目重复。",
                )
            )
            continue
        if previous_position is not None and position <= previous_position:
            issues.append(
                _issue(
                    "ORDER_VIOLATION",
                    chapter_index,
                    title=title_raw,
                    source_event_id=event_id,
                    message="事件ID未沿时间轴顺序严格递增。",
                )
            )
            continue
        event = eligible_by_id[event_id]
        normalized.append(
            {
                "timestamp_ms": int(event["start_ms"]),
                "title": title_raw.strip() if isinstance(title_raw, str) else title_raw,
                "source_event_id": event_id,
            }
        )
        previous_position = position
        previous_id = event_id

    if not issues and normalized:
        starts = [int(boundary["timestamp_ms"]) for boundary in normalized]
        for chapter_index in range(len(starts)):
            end = starts[chapter_index + 1] if chapter_index + 1 < len(starts) else view.duration_ms
            length = end - starts[chapter_index]
            if length < MIN_CHAPTER_MS:
                boundary = normalized[chapter_index]
                issues.append(
                    _issue(
                        "MIN_CHAPTER_VIOLATION",
                        chapter_index + 1,
                        title=boundary["title"],
                        source_event_id=boundary["source_event_id"],
                        message=(
                            f"第{chapter_index + 1}章时长{length}ms低于最小章节时长{MIN_CHAPTER_MS}ms。"
                        ),
                    )
                )
    return normalized, issues


# ---------------------------------------------------------------------------
# ChapterPlan 2.0构造与字段级校验（裁决D/§3.2）
# ---------------------------------------------------------------------------


def _source_timeline_of(index: Mapping[str, Any], view: TranscriptView) -> dict[str, Any]:
    return {
        "schema_name": "UnifiedTimeline",
        "schema_version": "2.0",
        "timeline_id": str(index["timeline_id"]),
        "sha256": canonical_json_sha256(index),
        "event_count": len(list(index["events"])),
        "transcript_event_count": len(view.events),
    }


def build_chapter_plan_v2(
    task_id: str,
    index: Mapping[str, Any],
    view: TranscriptView,
    boundaries: Sequence[Mapping[str, Any]],
    *,
    result_source: str,
) -> dict[str, Any]:
    duration_ms = view.duration_ms
    if result_source == "primary":
        parameters = {
            "result_source": "primary",
            "quality_level": "normal",
            "prompt_version": PROMPT_VERSION,
            "min_chapter_ms": MIN_CHAPTER_MS,
            "boundary_alignment": BOUNDARY_ALIGNMENT,
        }
    elif result_source == "emergency":
        parameters = {
            "result_source": "emergency",
            "quality_level": "degraded",
            "prompt_version": EMERGENCY_PROMPT_VERSION,
            "min_chapter_ms": min(1000, duration_ms),
            "boundary_alignment": BOUNDARY_ALIGNMENT,
        }
    else:
        raise RuntimeError(f"未知result_source：{result_source}")
    return {
        "schema_name": CHAPTER_PLAN_SCHEMA_NAME,
        "schema_version": CHAPTER_PLAN_SCHEMA_VERSION,
        "task_id": task_id,
        "strategy": "llm",
        "source_timeline": _source_timeline_of(index, view),
        "duration_ms": duration_ms,
        "parameters": parameters,
        "boundaries": [
            {
                "timestamp_ms": int(boundary["timestamp_ms"]),
                "title": str(boundary["title"]),
                "source_event_id": boundary["source_event_id"],
            }
            for boundary in boundaries
        ],
    }


def build_emergency_plan(
    task_id: str, index: Mapping[str, Any], view: TranscriptView
) -> dict[str, Any]:
    """裁决D：emergency单章对象逐字段实现；模型ID可为null。"""

    return build_chapter_plan_v2(
        task_id,
        index,
        view,
        [{"timestamp_ms": 0, "title": EMERGENCY_TITLE, "source_event_id": None}],
        result_source="emergency",
    )


def _validate_plan_shape(plan: Any) -> None:
    if not isinstance(plan, dict):
        raise ChapterPlanValidationError("ChapterPlan必须是JSON对象。")
    _exact_keys(plan, _PLAN_TOP_KEYS, "ChapterPlan", ChapterPlanValidationError)
    if plan["schema_name"] != CHAPTER_PLAN_SCHEMA_NAME or plan["schema_version"] != CHAPTER_PLAN_SCHEMA_VERSION:
        raise ChapterPlanValidationError("ChapterPlan的schema_name/schema_version无效。")
    if not isinstance(plan["task_id"], str) or not plan["task_id"].strip():
        raise ChapterPlanValidationError("ChapterPlan的task_id无效。")
    if plan["strategy"] != "llm":
        raise ChapterPlanValidationError("ChapterPlan的strategy必须为llm。")
    if not _is_int(plan["duration_ms"]) or plan["duration_ms"] <= 0:
        raise ChapterPlanValidationError("ChapterPlan的duration_ms必须是正整数。")

    source = plan["source_timeline"]
    _exact_keys(source, _SOURCE_TIMELINE_KEYS, "source_timeline", ChapterPlanValidationError)
    if source["schema_name"] != "UnifiedTimeline" or source["schema_version"] != "2.0":
        raise ChapterPlanValidationError("source_timeline的schema_name/schema_version无效。")
    if not isinstance(source["timeline_id"], str) or not source["timeline_id"].strip():
        raise ChapterPlanValidationError("source_timeline的timeline_id无效。")
    if not _sha256_hex(source["sha256"]):
        raise ChapterPlanValidationError("source_timeline的sha256必须是64位十六进制。")
    if not _is_int(source["event_count"]) or source["event_count"] <= 0:
        raise ChapterPlanValidationError("source_timeline的event_count必须是正整数。")
    if (
        not _is_int(source["transcript_event_count"])
        or source["transcript_event_count"] <= 0
    ):
        raise ChapterPlanValidationError("source_timeline的transcript_event_count必须是正整数。")

    parameters = plan["parameters"]
    _exact_keys(
        parameters, _PLAN_PARAMETERS_KEYS, "parameters", ChapterPlanValidationError
    )
    if parameters["boundary_alignment"] != BOUNDARY_ALIGNMENT:
        raise ChapterPlanValidationError("parameters的boundary_alignment无效。")
    combination = (parameters["result_source"], parameters["quality_level"])
    if combination == ("primary", "normal"):
        if parameters["min_chapter_ms"] != MIN_CHAPTER_MS:
            raise ChapterPlanValidationError("primary结果的min_chapter_ms必须为60000。")
        if parameters["prompt_version"] != PROMPT_VERSION:
            raise ChapterPlanValidationError("primary结果的prompt_version无效。")
    elif combination == ("emergency", "degraded"):
        expected_min = min(1000, int(plan["duration_ms"]))
        if parameters["min_chapter_ms"] != expected_min:
            raise ChapterPlanValidationError("emergency结果的min_chapter_ms无效。")
        if parameters["prompt_version"] != EMERGENCY_PROMPT_VERSION:
            raise ChapterPlanValidationError("emergency结果的prompt_version无效。")
    else:
        raise ChapterPlanValidationError("parameters的result_source/quality_level组合无效。")

    boundaries = plan["boundaries"]
    if not isinstance(boundaries, list) or not boundaries:
        raise ChapterPlanValidationError("boundaries必须是非空数组。")
    previous_ts: int | None = None
    for position, boundary in enumerate(boundaries):
        label = f"boundaries[{position + 1}]"
        if not isinstance(boundary, dict):
            raise ChapterPlanValidationError(f"{label}必须是JSON对象。")
        _exact_keys(boundary, _PLAN_BOUNDARY_KEYS, label, ChapterPlanValidationError)
        timestamp = boundary["timestamp_ms"]
        title = boundary["title"]
        event_id = boundary["source_event_id"]
        if not _is_int(timestamp) or timestamp < 0:
            raise ChapterPlanValidationError(f"{label}的timestamp_ms必须是非负整数毫秒。")
        if not isinstance(title, str) or not title.strip() or title != title.strip():
            raise ChapterPlanValidationError(f"{label}的title必须是非空且无首尾空白的字符串。")
        if position == 0:
            if timestamp != 0 or event_id is not None:
                raise ChapterPlanValidationError(
                    f"{label}必须为timestamp_ms=0且source_event_id=null。"
                )
        else:
            if not isinstance(event_id, str) or not event_id.strip():
                raise ChapterPlanValidationError(f"{label}的source_event_id必须是非空字符串。")
            duration = int(plan["duration_ms"])
            if not 0 < timestamp < duration:
                raise ChapterPlanValidationError(f"{label}的timestamp_ms必须位于(0, duration_ms)。")
            if previous_ts is not None and timestamp <= previous_ts:
                raise ChapterPlanValidationError(f"{label}的timestamp_ms必须严格递增。")
        previous_ts = timestamp
    min_ms = int(parameters["min_chapter_ms"])
    starts = [int(boundary["timestamp_ms"]) for boundary in boundaries]
    duration = int(plan["duration_ms"])
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else duration
        if end - start < min_ms:
            raise ChapterPlanValidationError(f"第{position + 1}章低于最小章节时长。")


def _cross_validate_plan_with_timeline(
    plan: Mapping[str, Any],
    index: Mapping[str, Any],
    view: TranscriptView,
    *,
    expected_task_id: str | None,
) -> None:
    if expected_task_id is not None and plan["task_id"] != expected_task_id:
        raise ChapterPlanValidationError("ChapterPlan的task_id与请求任务不一致。")
    if plan["task_id"] != index["task_id"]:
        raise ChapterPlanValidationError("ChapterPlan的task_id与时间轴不一致。")
    source = plan["source_timeline"]
    if source["timeline_id"] != index["timeline_id"]:
        raise ChapterPlanValidationError("source_timeline的timeline_id与时间轴不一致。")
    if source["sha256"] != canonical_json_sha256(index):
        raise ChapterPlanValidationError("source_timeline的sha256与时间轴规范哈希不一致。")
    if source["event_count"] != len(list(index["events"])):
        raise ChapterPlanValidationError("source_timeline的event_count与时间轴不一致。")
    if source["transcript_event_count"] != len(view.events):
        raise ChapterPlanValidationError("source_timeline的transcript_event_count与时间轴不一致。")
    if plan["duration_ms"] != index["duration_ms"]:
        raise ChapterPlanValidationError("ChapterPlan的duration_ms与时间轴不一致。")

    eligible_by_id = {
        str(event["id"]): event for event in view.events if event["payload"]["boundary_eligible"]
    }
    transcript_ids = {str(event["id"]) for event in view.events}
    all_ids = {str(event["id"]) for event in index["events"]}
    for position, boundary in enumerate(plan["boundaries"]):
        if position == 0:
            continue
        event_id = boundary["source_event_id"]
        if event_id not in all_ids:
            raise ChapterPlanValidationError(f"第{position + 1}章引用了不存在的事件ID。")
        if event_id not in transcript_ids or event_id not in eligible_by_id:
            raise ChapterPlanValidationError(
                f"第{position + 1}章引用的事件不是可选边界的主文本事件。"
            )
        if boundary["timestamp_ms"] != int(eligible_by_id[event_id]["start_ms"]):
            raise ChapterPlanValidationError(f"第{position + 1}章的timestamp_ms与事件起点不符。")


def validate_chapter_plan_v2(
    plan: Any,
    *,
    timeline_index: Mapping[str, Any] | None = None,
    view: TranscriptView | None = None,
    expected_task_id: str | None = None,
) -> None:
    """裁决§3.2：字段级校验；提供时间轴时执行完整交叉校验。"""

    _validate_plan_shape(plan)
    if timeline_index is not None:
        if view is None:
            view = _transcript_view(timeline_index)
        _cross_validate_plan_with_timeline(
            plan, timeline_index, view, expected_task_id=expected_task_id
        )


# ---------------------------------------------------------------------------
# 缓存身份（裁决§F.5）与执行报告
# ---------------------------------------------------------------------------


def boundary_planner_cache_identity(
    *,
    timeline_id: str,
    timeline_sha256: str,
    timeline_manifest_sha256: str,
    role_cache_identity_sha256: str,
) -> tuple[dict[str, Any], str]:
    payload = {
        "schema_name": CACHE_SCHEMA_NAME,
        "schema_version": CACHE_SCHEMA_VERSION,
        "boundary_planner_v2": BOUNDARY_PLANNER_VERSION,
        "chapter_plan_schema": CHAPTER_PLAN_SCHEMA_VERSION,
        "timeline_id": timeline_id,
        "timeline_sha256": timeline_sha256,
        "timeline_manifest_sha256": timeline_manifest_sha256,
        "role_cache_identity_sha256": role_cache_identity_sha256,
        "min_chapter_ms": MIN_CHAPTER_MS,
        "boundary_alignment": BOUNDARY_ALIGNMENT,
        "prompt_version": PROMPT_VERSION,
        "validation_id": VALIDATION_ID,
        "validation_retry_policy": VALIDATION_RETRY_POLICY,
        "emergency_policy_version": EMERGENCY_PROMPT_VERSION,
    }
    return payload, canonical_json_sha256(payload)


def build_execution_report(
    task_id: str,
    plan: Mapping[str, Any],
    *,
    timeline_sha256: str,
    business_call_count: int,
    supplier_request_count: int,
    errors: Sequence[Mapping[str, Any]],
    credential_present: bool,
) -> dict[str, Any]:
    """裁决报告决策3：全部字段由内容身份与确定性错误清单决定，不含时间戳。"""

    parameters = plan["parameters"]
    degraded = parameters["result_source"] == "emergency"
    return {
        "schema_name": REPORT_SCHEMA_NAME,
        "schema_version": REPORT_SCHEMA_VERSION,
        "task_id": task_id,
        "module": BOUNDARY_MODULE_TOKEN,
        "status": "degraded" if degraded else "success",
        "result_source": parameters["result_source"],
        "quality_level": parameters["quality_level"],
        "timeline_id": plan["source_timeline"]["timeline_id"],
        "timeline_sha256": timeline_sha256,
        "duration_ms": plan["duration_ms"],
        "boundary_count": len(plan["boundaries"]),
        "business_call_count": business_call_count,
        "supplier_request_count": supplier_request_count,
        "errors": [dict(error) for error in errors],
        "credential_present": credential_present,
    }


def _validate_execution_report(report: Any, plan: Mapping[str, Any]) -> None:
    if not isinstance(report, dict):
        raise ChapterPlanValidationError("execution-report必须是JSON对象。")
    _exact_keys(report, _REPORT_KEYS, "execution-report", ChapterPlanValidationError)
    if report["schema_name"] != REPORT_SCHEMA_NAME or report["schema_version"] != REPORT_SCHEMA_VERSION:
        raise ChapterPlanValidationError("execution-report的schema_name/schema_version无效。")
    if report["module"] != BOUNDARY_MODULE_TOKEN:
        raise ChapterPlanValidationError("execution-report的module无效。")
    if report["task_id"] != plan["task_id"]:
        raise ChapterPlanValidationError("execution-report的task_id与章节计划不一致。")
    parameters = plan["parameters"]
    expected_status = "degraded" if parameters["result_source"] == "emergency" else "success"
    if report["status"] != expected_status:
        raise ChapterPlanValidationError("execution-report的status与result_source不符。")
    if (
        report["result_source"] != parameters["result_source"]
        or report["quality_level"] != parameters["quality_level"]
    ):
        raise ChapterPlanValidationError("execution-report的结果三元组与parameters不一致。")
    if report["timeline_id"] != plan["source_timeline"]["timeline_id"]:
        raise ChapterPlanValidationError("execution-report的timeline_id不一致。")
    if not _sha256_hex(report["timeline_sha256"]):
        raise ChapterPlanValidationError("execution-report的timeline_sha256无效。")
    if report["duration_ms"] != plan["duration_ms"]:
        raise ChapterPlanValidationError("execution-report的duration_ms不一致。")
    if report["boundary_count"] != len(plan["boundaries"]):
        raise ChapterPlanValidationError("execution-report的boundary_count不一致。")
    if not _is_int(report["business_call_count"]) or report["business_call_count"] not in (0, 1):
        raise ChapterPlanValidationError("execution-report的business_call_count无效。")
    if not _is_int(report["supplier_request_count"]) or not 0 <= report["supplier_request_count"] <= 2:
        raise ChapterPlanValidationError("execution-report的supplier_request_count无效。")
    if parameters["result_source"] == "primary":
        if report["business_call_count"] != 1 or report["supplier_request_count"] not in (1, 2):
            raise ChapterPlanValidationError("primary结果的调用计数无效。")
        if report["supplier_request_count"] == 1 and report["errors"]:
            raise ChapterPlanValidationError("一次成功的primary结果不应带错误记录。")
        if report["supplier_request_count"] == 2 and len(report["errors"]) != 1:
            raise ChapterPlanValidationError("修正轮primary结果应恰有一条错误记录。")
    else:
        if report["business_call_count"] != 1 or report["supplier_request_count"] != 2:
            raise ChapterPlanValidationError("emergency结果的调用计数无效。")
        if len(report["errors"]) != 2:
            raise ChapterPlanValidationError("emergency结果应恰有两条失败记录。")
    if not isinstance(report["credential_present"], bool):
        raise ChapterPlanValidationError("execution-report的credential_present必须是布尔值。")
    if not isinstance(report["errors"], list):
        raise ChapterPlanValidationError("execution-report的errors必须是数组。")
    for error in report["errors"]:
        if not isinstance(error, dict):
            raise ChapterPlanValidationError("execution-report的错误记录必须是JSON对象。")
        _exact_keys(error, _REPORT_ERROR_KEYS, "错误记录", ChapterPlanValidationError)
        if not _is_int(error["attempt"]) or error["attempt"] not in (1, 2):
            raise ChapterPlanValidationError("错误记录的attempt无效。")
        if error["kind"] not in ("transport", "validation"):
            raise ChapterPlanValidationError("错误记录的kind无效。")
        if not isinstance(error["message"], str) or not error["message"]:
            raise ChapterPlanValidationError("错误记录的message必须是非空字符串。")
        if not isinstance(error["issues"], list):
            raise ChapterPlanValidationError("错误记录的issues必须是数组。")
        for item in error["issues"]:
            if not isinstance(item, dict):
                raise ChapterPlanValidationError("issues条目必须是JSON对象。")
            _exact_keys(item, _REPORT_ISSUE_KEYS, "issues条目", ChapterPlanValidationError)
            if item["code"] not in BUSINESS_CODES:
                raise ChapterPlanValidationError("issues条目包含未知业务校验码。")
            if not _is_int(item["chapter_index"]) or item["chapter_index"] < 0:
                raise ChapterPlanValidationError("issues条目的chapter_index无效。")
            for field in ("title", "source_event_id"):
                if item[field] is not None and not isinstance(item[field], str):
                    raise ChapterPlanValidationError(f"issues条目的{field}无效。")
            if not isinstance(item["message"], str) or not item["message"]:
                raise ChapterPlanValidationError("issues条目的message必须是非空字符串。")


# ---------------------------------------------------------------------------
# registry解析与冻结值核对（裁决F.2/§一.8）
# ---------------------------------------------------------------------------


def _enforce_frozen_role(role: LlmRoleConfig) -> None:
    if role.input_contract != INPUT_CONTRACT:
        raise RoleConfigInvalidError(f"boundary_model的input_contract必须为{INPUT_CONTRACT}。")
    if role.output_contract != OUTPUT_CONTRACT:
        raise RoleConfigInvalidError(f"boundary_model的output_contract必须为{OUTPUT_CONTRACT}。")
    if role.validation_id != VALIDATION_ID:
        raise RoleConfigInvalidError("boundary_model的validation_id与冻结校验器身份不符。")
    if role.validation_retry_policy != VALIDATION_RETRY_POLICY:
        raise RoleConfigInvalidError("boundary_model的validation_retry_policy与冻结值不符。")
    if role.cache_namespace != CACHE_NAMESPACE:
        raise RoleConfigInvalidError("boundary_model的cache_namespace与冻结值不符。")
    if role.alternate_model_fallback != FROZEN_ALTERNATE_FALLBACK:
        raise RoleConfigInvalidError("boundary_model必须保持alternate_model_fallback=none。")
    if role.max_attempts_per_business_call != FROZEN_MAX_ATTEMPTS:
        raise RoleConfigInvalidError("boundary_model的max_attempts_per_business_call必须为2。")
    if role.runtime_budget != {
        "business_call_hard_limit": FROZEN_BUSINESS_BUDGET,
        "supplier_request_hard_limit": FROZEN_SUPPLIER_BUDGET,
    }:
        raise RoleConfigInvalidError("boundary_model的runtime_budget必须为1次业务调用/2次供应商请求。")
    public_anonymous = (
        role.connection_profile is not None
        and role.connection_profile.name == PUBLIC_ANONYMOUS_PROFILE
        and role.connection_profile.base_url == PUBLIC_ANONYMOUS_BASE_URL
    )
    allowed_models = (
        {FROZEN_MODEL, PUBLIC_ANONYMOUS_MODEL}
        if public_anonymous
        else {FROZEN_MODEL}
    )
    if role.model not in allowed_models:
        raise RoleConfigInvalidError(
            f"boundary_model的model必须为{', '.join(sorted(allowed_models))}。"
        )
    generation = role.generation
    assert generation is not None
    if generation.temperature != 0.0:
        raise RoleConfigInvalidError("boundary_model的temperature必须为0.0。")
    if generation.enable_thinking is not False:
        raise RoleConfigInvalidError("boundary_model必须enable_thinking=false。")
    if generation.max_tokens is not None:
        raise RoleConfigInvalidError("boundary_model的max_tokens必须为null。")
    if generation.timeout_seconds != 60.0:
        raise RoleConfigInvalidError("boundary_model的timeout_seconds必须为60.0。")
    if role.response_format != FROZEN_RESPONSE_FORMAT:
        raise RoleConfigInvalidError("boundary_model的response_format必须为json_object。")
    bundle = role.prompt_bundle
    assert bundle is not None
    if PROMPT_VERSION not in bundle.versions:
        raise RoleConfigInvalidError("boundary_model的prompt_bundle.versions缺少冻结提示词版本。")


def _resolve_boundary_role(
    project_root: Path,
    explicit_path: str | Path | None,
) -> tuple[RegistrySnapshot, LlmRoleConfig]:
    try:
        snapshot = resolve_model_registry(project_root, explicit_path)
    except ModelRoleRegistryError as exc:
        raise RoleConfigInvalidError(f"模型角色registry无效：{exc}") from exc
    if snapshot.fixture_only:
        raise RoleConfigInvalidError("fixture_only的registry不得用于正式调用（P3 §六）。")
    role = snapshot.lookup(BOUNDARY_ROLE_NAME)
    if role is None or not role.enabled:
        raise RoleConfigMissingError(
            "未提供可用的boundary_model角色配置（内置安全配置默认禁用，绝不自动真实联网）。"
        )
    assert role.connection_profile is not None
    _enforce_frozen_role(role)
    expected_sha = boundary_prompt_content_sha256()
    assert role.prompt_bundle is not None
    if role.prompt_bundle.fixture_hash_only:
        raise RoleConfigInvalidError(
            "prompt_bundle标记fixture_hash_only，不得用于正式调用（P3 §六）。"
        )
    if role.prompt_bundle.content_sha256 != expected_sha:
        raise RoleConfigInvalidError(
            "prompt_bundle记录的content_sha256与实际prompt内容不一致（P3 §六）。"
        )
    return snapshot, role


# ---------------------------------------------------------------------------
# llm请求缓存（裁决F.4）
# ---------------------------------------------------------------------------


def _validate_llm_cache_entry(
    entry: Any,
    *,
    role_cache_sha256: str,
    messages_sha256: str,
    model: str,
    backend: str,
) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目必须是JSON对象。")
    _exact_keys(entry, _LLM_CACHE_KEYS, "boundary llm缓存条目", BoundaryCacheCorruptError)
    if entry["schema_name"] != LLM_CACHE_SCHEMA_NAME or entry["schema_version"] != LLM_CACHE_SCHEMA_VERSION:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目Schema无效。")
    if entry["role_cache_identity_sha256"] != role_cache_sha256:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目身份与文件名不一致。")
    if entry["messages_sha256"] != messages_sha256:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目的messages_sha256不一致。")
    if entry["backend"] != backend or entry["model"] != model:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目的backend/model与角色不一致。")
    if not isinstance(entry["raw_response"], dict):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目缺少原始响应对象。")
    if not isinstance(entry["normalized_response"], dict):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目缺少规范化章节计划。")
    if not isinstance(entry["execution_errors"], list):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目的errors无效。")
    if not _is_int(entry["supplier_request_count"]) or not 1 <= entry["supplier_request_count"] <= 2:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目的请求计数无效。")
    if not isinstance(entry["created_at"], str) or not entry["created_at"]:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目缺少created_at。")
    if not isinstance(entry["credential_present"], bool):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存条目的credential_present无效。")
    return dict(entry)


def _replay_llm_cache(
    entry: Mapping[str, Any], task_id: str, index: Mapping[str, Any], view: TranscriptView
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], int]:
    """缓存读回复放业务校验，要求与normalized_response完全一致（裁决F.4）。"""

    raw = entry["raw_response"]
    normalized, issues = normalize_boundary_response(raw, view)
    if issues:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：boundary llm缓存的原始响应不再通过业务校验。")
    replayed = build_chapter_plan_v2(task_id, index, view, normalized, result_source="primary")
    try:
        validate_chapter_plan_v2(replayed, timeline_index=index, view=view, expected_task_id=task_id)
    except ChapterPlanValidationError as exc:
        raise BoundaryCacheCorruptError(f"CACHE_CORRUPT：缓存回放的章节计划无效：{exc}") from exc
    stored = entry["normalized_response"]
    try:
        validate_chapter_plan_v2(stored, timeline_index=index, view=view, expected_task_id=task_id)
    except ChapterPlanValidationError as exc:
        raise BoundaryCacheCorruptError(f"CACHE_CORRUPT：缓存中的章节计划无效：{exc}") from exc
    if canonical_json_bytes(stored) != canonical_json_bytes(replayed):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：缓存回放与normalized_response不一致。")
    return replayed, dict(raw), [dict(item) for item in entry["execution_errors"]], int(
        entry["supplier_request_count"]
    )


def _write_llm_cache_entry(
    boundaries_root: Path,
    *,
    role_cache_sha256: str,
    messages_sha256: str,
    raw_response: Mapping[str, Any],
    normalized_response: Mapping[str, Any],
    execution_errors: Sequence[Mapping[str, Any]],
    supplier_request_count: int,
    backend: str,
    model: str,
    credential_present: bool,
) -> None:
    cache_dir = boundaries_root / BOUNDARY_LLM_CACHE_DIRECTORY
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_name": LLM_CACHE_SCHEMA_NAME,
        "schema_version": LLM_CACHE_SCHEMA_VERSION,
        "role_cache_identity_sha256": role_cache_sha256,
        "messages_sha256": messages_sha256,
        "raw_response": dict(raw_response),
        "normalized_response": dict(normalized_response),
        "execution_errors": [dict(item) for item in execution_errors],
        "supplier_request_count": supplier_request_count,
        "backend": backend,
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "credential_present": credential_present,
    }
    target = cache_dir / f"{role_cache_sha256}.json"
    temporary = cache_dir / f".{role_cache_sha256}.json.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(payload))
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# manifest、结果组复验、current、锁、审计
# ---------------------------------------------------------------------------


def _compute_result_id(plan: Mapping[str, Any]) -> str:
    return RESULT_ID_PREFIX + _sha256_bytes(canonical_json_bytes(plan))[:12]


def _build_manifest(
    plan: Mapping[str, Any],
    *,
    result_id: str,
    cache_sha256: str,
    timeline_id: str,
    timeline_sha256: str,
    timeline_manifest_sha256: str,
    file_bytes: Mapping[str, bytes],
) -> dict[str, Any]:
    parameters = plan["parameters"]
    status = "degraded" if parameters["result_source"] == "emergency" else "success"
    return {
        "schema_name": MANIFEST_SCHEMA_NAME,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact_type": BOUNDARY_ARTIFACT_TYPE,
        "task_id": plan["task_id"],
        "result_id": result_id,
        "status": status,
        "cache_identity_sha256": cache_sha256,
        "inputs": {
            "timeline_id": timeline_id,
            "timeline_sha256": timeline_sha256,
            "timeline_manifest_sha256": timeline_manifest_sha256,
        },
        "versions": {
            "boundary_planner_v2": BOUNDARY_PLANNER_VERSION,
            "chapter_plan_schema": CHAPTER_PLAN_SCHEMA_VERSION,
            "artifact_group_manifest": MANIFEST_SCHEMA_VERSION,
            "prompt_version": parameters["prompt_version"],
            "validation_id": VALIDATION_ID,
            "validation_retry_policy": VALIDATION_RETRY_POLICY,
        },
        "files": {
            name: {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
            for name, payload in sorted(file_bytes.items())
        },
    }


def _group_file_names(*, emergency: bool) -> set[str]:
    if emergency:
        return {BOUNDARY_PLAN_FILE, BOUNDARY_REPORT_FILE, BOUNDARY_MANIFEST_FILE}
    return {
        BOUNDARY_PLAN_FILE,
        BOUNDARY_RAW_FILE,
        BOUNDARY_REPORT_FILE,
        BOUNDARY_MANIFEST_FILE,
    }


def validate_chapter_plan_group(
    group_path: str | Path,
    *,
    cache_sha256: str,
    timeline_index: Mapping[str, Any],
    timeline_manifest_sha256: str,
    expected_result_id: str | None = None,
    require_directory_name: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """复验正式组：成员、plan/raw/report/manifest交叉一致＋缓存身份匹配。"""

    group = Path(group_path)
    if not group.is_dir():
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划结果组目录不存在。")
    view = _transcript_view(timeline_index)
    plan_path = group / BOUNDARY_PLAN_FILE
    manifest_path = group / BOUNDARY_MANIFEST_FILE
    report_path = group / BOUNDARY_REPORT_FILE
    raw_path = group / BOUNDARY_RAW_FILE
    for path in (plan_path, manifest_path, report_path):
        if not path.is_file():
            raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划结果组缺少必需文件。")

    plan_bytes = plan_path.read_bytes()
    try:
        plan = json.loads(plan_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划无法读取或JSON无效。") from exc
    if not isinstance(plan, dict):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划必须是JSON对象。")
    if plan_bytes != canonical_json_bytes(plan):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划不是确定的规范JSON字节。")
    try:
        validate_chapter_plan_v2(plan, timeline_index=timeline_index, view=view)
    except ChapterPlanValidationError as exc:
        raise BoundaryCacheCorruptError(f"CACHE_CORRUPT：章节计划无效：{exc}") from exc

    emergency = plan["parameters"]["result_source"] == "emergency"
    member_names = {entry.name for entry in group.iterdir()}
    if member_names != _group_file_names(emergency=emergency):
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划结果组成员无效。")

    raw: dict[str, Any] | None = None
    if not emergency:
        raw_bytes = raw_path.read_bytes()
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise BoundaryCacheCorruptError("CACHE_CORRUPT：原始响应无法读取或JSON无效。") from exc
        if not isinstance(raw, dict):
            raise BoundaryCacheCorruptError("CACHE_CORRUPT：原始响应必须是JSON对象。")
        if raw_bytes != _json_bytes(raw):
            raise BoundaryCacheCorruptError("CACHE_CORRUPT：原始响应不是确定的序列化字节。")
        _, issues = normalize_boundary_response(raw, view)
        if issues:
            raise BoundaryCacheCorruptError("CACHE_CORRUPT：原始响应不再通过业务校验。")

    report = _read_json(report_path, "execution-report", BoundaryCacheCorruptError)
    timeline_sha256 = canonical_json_sha256(timeline_index)
    try:
        _validate_execution_report(report, plan)
    except ChapterPlanValidationError as exc:
        raise BoundaryCacheCorruptError(f"CACHE_CORRUPT：执行报告无效：{exc}") from exc
    if report["timeline_sha256"] != timeline_sha256:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：执行报告的时间轴哈希与当前上游不符。")

    result_id = _compute_result_id(plan)
    if require_directory_name and group.name != result_id:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划结果组目录名与内容身份不符。")
    if expected_result_id is not None and result_id != expected_result_id:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：章节计划结果身份与预期不符。")

    file_bytes = {BOUNDARY_PLAN_FILE: plan_bytes, BOUNDARY_REPORT_FILE: report_path.read_bytes()}
    if raw is not None:
        file_bytes[BOUNDARY_RAW_FILE] = raw_bytes
    expected_manifest = _build_manifest(
        plan,
        result_id=result_id,
        cache_sha256=cache_sha256,
        timeline_id=str(timeline_index["timeline_id"]),
        timeline_sha256=timeline_sha256,
        timeline_manifest_sha256=timeline_manifest_sha256,
        file_bytes=file_bytes,
    )
    manifest = _read_json(manifest_path, "chapter manifest", BoundaryCacheCorruptError)
    _exact_keys(manifest, _MANIFEST_KEYS, "chapter manifest", BoundaryCacheCorruptError)
    if manifest != expected_manifest:
        raise BoundaryCacheCorruptError("CACHE_CORRUPT：chapter manifest与组内容不一致。")
    for name, record in manifest["files"].items():
        if not _safe_relative_file(name) or set(record) != {"sha256", "bytes"}:
            raise BoundaryCacheCorruptError("CACHE_CORRUPT：chapter manifest文件记录无效。")
        path = group / PurePosixPath(name)
        if not path.is_file() or _file_sha256(path) != record["sha256"]:
            raise BoundaryCacheCorruptError("CACHE_CORRUPT：chapter manifest记录的哈希与磁盘不符。")
    return plan, manifest, report


def _current_payload(result_id: str, manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": CURRENT_SCHEMA_NAME,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "artifact_type": BOUNDARY_ARTIFACT_TYPE,
        "result_id": result_id,
        "group": f"{BOUNDARY_GROUP_DIRECTORY}/{result_id}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
    }


def _load_current(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = root / BOUNDARY_CURRENT_FILE
    if not _current_path_state(path, "boundaries current", CurrentPointerCorruptError):
        return None, None
    pointer = _read_json(path, "boundaries current", CurrentPointerCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "boundaries current", CurrentPointerCorruptError)
    if (
        pointer["schema_name"] != CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != BOUNDARY_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{BOUNDARY_GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：boundaries current身份无效。"
        )
    group = artifact_group_path(root, BOUNDARY_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：boundaries current的组路径越出正式组目录。"
        )
    manifest_path = group / BOUNDARY_MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：boundaries current目标缺失或损坏。"
        )
    return pointer, str(pointer["result_id"])


def load_chapter_plan_current(output_root: str | Path, task_id: str) -> dict[str, Any] | None:
    """公开的current指针读取入口；复验失败硬抛。"""

    validate_task_id(task_id)
    root = Path(output_root).expanduser() / task_id / "m2" / BOUNDARY_ROOT_DIRECTORY
    pointer, _ = _load_current(root)
    return pointer


def load_published_chapter_plan(
    output_root: str | Path,
    task_id: str,
    *,
    timeline_index: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """只读加载current指向的ChapterPlan 2.0正式组（供后续阶段消费）。"""

    pointer = load_chapter_plan_current(output_root, task_id)
    if pointer is None:
        return None
    root = Path(output_root).expanduser() / task_id / "m2" / BOUNDARY_ROOT_DIRECTORY
    group = artifact_group_path(root, BOUNDARY_GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：boundaries current的组路径越界。"
        )
    if timeline_index is None:
        loaded = load_published_timeline(output_root, task_id)
        if loaded is None:
            raise InputTimelineMissingError(
                f"{ERROR_INPUT_TIMELINE_MISSING}：任务没有已发布的Timeline 2.0 current。"
            )
        timeline_index = loaded
    manifest_path = group / BOUNDARY_MANIFEST_FILE
    manifest = _read_json(manifest_path, "chapter manifest", BoundaryCacheCorruptError)
    timeline_manifest_sha256 = str(
        manifest["inputs"].get("timeline_manifest_sha256", "")
    ) if isinstance(manifest.get("inputs"), dict) else ""
    plan, _, _ = validate_chapter_plan_group(
        group,
        cache_sha256=str(manifest["cache_identity_sha256"]),
        timeline_index=timeline_index,
        timeline_manifest_sha256=timeline_manifest_sha256,
    )
    return plan


def _replace_current(root: Path, payload: Mapping[str, Any]) -> None:
    temporary = root / f".{BOUNDARY_CURRENT_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(payload))
        os.replace(temporary, root / BOUNDARY_CURRENT_FILE)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _disk_current_identity(root: Path) -> tuple[str, str] | None:
    try:
        raw = (root / BOUNDARY_CURRENT_FILE).read_bytes()
    except OSError:
        return None
    try:
        pointer = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(pointer, dict):
        return None
    result_id = pointer.get("result_id")
    manifest_sha256 = pointer.get("manifest_sha256")
    if not is_safe_result_id(result_id) or not isinstance(manifest_sha256, str):
        return None
    return str(result_id), manifest_sha256


def _may_discard_published_group(root: Path, result_id: str) -> bool:
    if not (root / BOUNDARY_CURRENT_FILE).exists():
        return True
    identity = _disk_current_identity(root)
    return identity is not None and identity[0] != result_id


@contextmanager
def _module_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    locks = m2_root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / BOUNDARY_LOCK_FILE
    payload = _json_bytes(
        {
            "schema_name": "ModuleLock",
            "schema_version": "1.0",
            "task_id": task_id,
            "module": BOUNDARY_MODULE_TOKEN,
            "pid": os.getpid(),
        }
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise BoundaryConcurrentRunError("CONCURRENT_RUN：boundaries锁已存在。") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        path.unlink(missing_ok=True)


def _inject(selected: str | None, point: str) -> None:
    if selected == point:
        raise InjectedBoundaryFailure(f"注入失败：{point}")


def _write_failure(
    m2_root: Path,
    *,
    task_id: str,
    stage: str,
    error: BaseException,
    input_ids: Mapping[str, Any],
    cache_sha256: str | None,
    previous_current_id: str | None,
    cleanup_status: str,
    current_unchanged: bool,
    business_call_count: int,
    supplier_request_count: int,
) -> str:
    failure_id = (
        f"failure-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    )
    root = m2_root / "failures" / BOUNDARY_MODULE_TOKEN / failure_id
    root.mkdir(parents=True, exist_ok=False)
    code = getattr(error, "error_code", ERROR_BOUNDARY_PLAN_FAILED)
    message = (
        str(error)
        if isinstance(error, BoundaryPlannerV2Error)
        else f"章节边界决策失败：{type(error).__name__}"
    )
    payload = {
        "schema_name": FAILURE_SCHEMA_NAME,
        "schema_version": FAILURE_SCHEMA_VERSION,
        "failure_id": failure_id,
        "task_id": task_id,
        "module": BOUNDARY_MODULE_TOKEN,
        "stage": stage,
        "error_code": code,
        "safe_message": message,
        "input_ids": dict(input_ids),
        "cache_identity_sha256": cache_sha256,
        "previous_current_id": previous_current_id,
        "temporary_cleanup_status": cleanup_status,
        "business_call_count": business_call_count,
        "supplier_request_count": supplier_request_count,
        "current_unchanged": current_unchanged,
        "recoverable": bool(getattr(error, "recoverable", False)),
        "next_action": str(getattr(error, "next_action", "none")),
    }
    _write_fsync(root / "failure.json", _json_bytes(payload))
    return failure_id


# ---------------------------------------------------------------------------
# 模型业务调用（裁决F.8/§G：1次业务调用、至多2次请求、共用同一修正机会）
# ---------------------------------------------------------------------------


def _coerce_response_object(raw: Any) -> Any:
    """实现决定13：str形态先解析为JSON，其余原样交给业务规范化。

    非法JSON返回原str（非dict→既有RESPONSE_NOT_OBJECT）；解析出的非对象
    （list/标量）由``normalize_boundary_response``统一落同一错误码。
    """

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _run_boundary_model_call(
    *,
    transport: LLMTransport,
    config: LLMConfig,
    messages: list[dict[str, str]],
    response_format: Mapping[str, Any],
    view: TranscriptView,
    max_attempts: int,
    supplier_budget: int,
    api_key: str,
    progress: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], int]:
    """返回(boundaries, raw_response, errors, supplier_request_count)；失败抛BoundaryModelFailedError。

    ``progress``字典在每次发出请求前更新已发请求数：``KeyboardInterrupt``从
    ``transport.complete``内部抛出时不经过返回值路径，审计只能靠它读到真实计数。
    """

    errors: list[dict[str, Any]] = []
    supplier_used = 0
    current_messages = list(messages)
    for attempt in range(1, max_attempts + 1):
        if supplier_used >= supplier_budget:
            break
        supplier_used += 1
        if progress is not None:
            progress["supplier_request_count"] = supplier_used
        try:
            raw = transport.complete(config, current_messages, dict(response_format))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 - 任何传输层异常都记为transport失败
            safe = _safe_error_text(exc, api_key)
            errors.append(
                {
                    "attempt": attempt,
                    "kind": "transport",
                    "message": safe,
                    "issues": [],
                }
            )
            if attempt >= max_attempts or supplier_used >= supplier_budget:
                break
            current_messages = list(messages) + [
                {
                    "role": "user",
                    "content": f"上一次调用未获得合法JSON响应：{safe}。{_TRANSPORT_FEEDBACK_INSTRUCTION}",
                }
            ]
            continue
        parsed = _coerce_response_object(raw)
        boundaries, issues = normalize_boundary_response(parsed, view)
        if not issues:
            assert isinstance(parsed, dict)
            return boundaries, parsed, errors, supplier_used
        errors.append(
            {
                "attempt": attempt,
                "kind": "validation",
                "message": "上一次响应未通过业务校验。",
                "issues": [dict(item) for item in issues],
            }
        )
        if attempt >= max_attempts or supplier_used >= supplier_budget:
            break
        feedback = list(messages)
        if isinstance(parsed, dict):
            feedback.append(
                {
                    "role": "assistant",
                    "content": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
                }
            )
        issues_json = json.dumps(issues, ensure_ascii=False, separators=(",", ":"))
        feedback.append(
            {
                "role": "user",
                "content": f"上一次响应未通过业务校验，错误清单：{issues_json}。{_VALIDATION_FEEDBACK}",
            }
        )
        current_messages = feedback
    raise BoundaryModelFailedError(
        "boundary_model在预算内未产生合法响应，转用emergency单章结果。",
        errors=errors,
        supplier_request_count=supplier_used,
    )


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------


def plan_chapters_v2(
    task_id: str,
    *,
    output_root: str | Path = "outputs",
    model_registry: str | Path | None = None,
    project_root: str | Path | None = None,
    transport: LLMTransport | None = None,
    environ: Mapping[str, str] | None = None,
    failure_injection: str | None = None,
) -> BoundaryPlanResult:
    """规划并发布ChapterPlan 2.0不可变结果组（裁决F/§3.4全部语义）。

    - 全程只读M1与既有正式组；产物仅写入``<output_root>/<task_id>/m2/boundaries/``；
    - ``transport``为注入点；默认接线``OpenAICompatibleTransport``，14-B验收不使用；
    - ``project_root``默认为仓库根（``config/model-roles.json``所在项目目录）。
    """

    if failure_injection is not None and failure_injection not in _FAILURE_INJECTION_POINTS:
        raise BoundaryPlannerV2Error("未知的分章失败注入点。")
    validate_task_id(task_id)
    output = Path(output_root).expanduser()
    m2_root = output / task_id / "m2"
    root = m2_root / BOUNDARY_ROOT_DIRECTORY
    groups_dir = root / BOUNDARY_GROUP_DIRECTORY
    env: Mapping[str, str] = os.environ if environ is None else environ
    active_transport: LLMTransport = transport if transport is not None else OpenAICompatibleTransport()

    # --- 锁前：零写入、零审计（与F3前置策略一致） ---
    _ensure_task_identity(output, task_id)
    timeline_pointer, timeline_index = _load_timeline_inputs(output, task_id)
    timeline_id = str(timeline_index["timeline_id"])
    timeline_sha256 = canonical_json_sha256(timeline_index)
    timeline_manifest_sha256 = str(timeline_pointer["manifest_sha256"])
    view = _transcript_view(timeline_index)
    duration_ms = view.duration_ms
    if duration_ms <= MIN_CHAPTER_MS:
        raise DurationTooShortError(
            f"{ERROR_DURATION_TOO_SHORT}：视频时长{duration_ms}ms不超过最小章节时长{MIN_CHAPTER_MS}ms，不发布任何产物。"
        )

    stage = "prepare"
    role_cache_sha256: str | None = None
    cache_sha256: str | None = None
    result_id: str | None = None
    previous_current_id: str | None = None
    initial_current_identity: tuple[str, str] | None = None
    business_call_count = 0
    supplier_request_count = 0
    model_progress = {"supplier_request_count": 0}
    created_group = False
    temporary: Path | None = None
    cleanup_status = "not_created"

    with _module_lock(m2_root, task_id):
        try:
            initial_current_identity = _disk_current_identity(root)
            _inject(failure_injection, "after_inputs")

            stage = "role_config"
            _, role = _resolve_boundary_role(
                Path(project_root) if project_root is not None else Path(__file__).resolve().parent.parent,
                model_registry,
            )
            prompt_sha256 = boundary_prompt_content_sha256()
            _, role_cache_sha256 = compute_role_cache_identity(
                role,
                input_canonical_sha256=timeline_sha256,
                prompt_content_sha256=prompt_sha256,
            )
            _, cache_sha256 = boundary_planner_cache_identity(
                timeline_id=timeline_id,
                timeline_sha256=timeline_sha256,
                timeline_manifest_sha256=timeline_manifest_sha256,
                role_cache_identity_sha256=role_cache_sha256,
            )

            stage = "current_validation"
            current_pointer, previous_current_id = _load_current(root)
            if current_pointer is not None:
                candidate = artifact_group_path(
                    root, BOUNDARY_GROUP_DIRECTORY, str(current_pointer["result_id"])
                )
                if candidate is None:
                    raise CurrentPointerCorruptError(
                        "CURRENT_POINTER_CORRUPT：boundaries current的组路径越界。"
                    )
                manifest_probe = _read_json(
                    candidate / BOUNDARY_MANIFEST_FILE,
                    "chapter manifest",
                    BoundaryCacheCorruptError,
                )
                if (
                    isinstance(manifest_probe.get("cache_identity_sha256"), str)
                    and manifest_probe["cache_identity_sha256"] == cache_sha256
                ):
                    plan, manifest, report = validate_chapter_plan_group(
                        candidate,
                        cache_sha256=cache_sha256 or "",
                        timeline_index=timeline_index,
                        timeline_manifest_sha256=timeline_manifest_sha256,
                    )
                    return _result_from_group(
                        candidate,
                        str(current_pointer["result_id"]),
                        plan,
                        manifest,
                        report,
                        cache_hit=True,
                        pointer_repaired=False,
                        duration_ms=duration_ms,
                        timeline_id=timeline_id,
                    )

            stage = "llm_cache_load"
            assert role_cache_sha256 is not None and cache_sha256 is not None
            messages = build_boundary_messages(view)
            messages_sha256 = canonical_json_sha256(messages)
            entry_path = root / BOUNDARY_LLM_CACHE_DIRECTORY / f"{role_cache_sha256}.json"
            plan: dict[str, Any]
            raw_response: dict[str, Any] | None
            errors: list[dict[str, Any]]
            credential_present = False
            if _current_path_state(entry_path, "boundary llm缓存条目", BoundaryCacheCorruptError):
                entry = _read_json(entry_path, "boundary llm缓存条目", BoundaryCacheCorruptError)
                checked = _validate_llm_cache_entry(
                    entry,
                    role_cache_sha256=role_cache_sha256,
                    messages_sha256=messages_sha256,
                    model=str(role.model),
                    backend=str(role.connection_profile.backend if role.connection_profile else ""),
                )
                plan, raw_response, errors, supplier_request_count = _replay_llm_cache(
                    checked, task_id, timeline_index, view
                )
                business_call_count = 1
                credential_present = bool(checked["credential_present"])
                result_source = "primary"
            else:
                stage = "model_call"
                assert role.connection_profile is not None
                api_key, credential_present = resolve_api_key(role, env)
                if not api_key:
                    raise RoleConfigInvalidError(
                        f"启用角色要求API Key，但声明的环境变量{role.connection_profile.api_key_env}未设置。"
                    )
                config = build_llm_config(role, api_key)
                _inject(failure_injection, "before_model_call")
                business_call_count = 1
                try:
                    boundaries, model_raw, errors, supplier_request_count = _run_boundary_model_call(
                        transport=active_transport,
                        config=config,
                        messages=messages,
                        response_format=role.response_format or {},
                        view=view,
                        max_attempts=role.max_attempts_per_business_call,
                        supplier_budget=role.runtime_budget["supplier_request_hard_limit"],
                        api_key=api_key,
                        progress=model_progress,
                    )
                except BoundaryModelFailedError as exhausted:
                    # §3.4：两种失败都落空→零追加API，发布emergency单章（degraded成功）。
                    errors = exhausted.errors
                    supplier_request_count = exhausted.supplier_request_count
                    plan = build_emergency_plan(task_id, timeline_index, view)
                    raw_response = None
                    result_source = "emergency"
                else:
                    plan = build_chapter_plan_v2(
                        task_id,
                        timeline_index,
                        view,
                        boundaries,
                        result_source="primary",
                    )
                    raw_response = model_raw
                    result_source = "primary"
                    _write_llm_cache_entry(
                        root,
                        role_cache_sha256=role_cache_sha256,
                        messages_sha256=messages_sha256,
                        raw_response=raw_response or {},
                        normalized_response=plan,
                        execution_errors=errors,
                        supplier_request_count=supplier_request_count,
                        backend=str(role.connection_profile.backend),
                        model=str(role.model),
                        credential_present=credential_present,
                    )
                _inject(failure_injection, "after_model_call")

            stage = "group_publish"
            try:
                validate_chapter_plan_v2(
                    plan, timeline_index=timeline_index, view=view, expected_task_id=task_id
                )
            except ChapterPlanValidationError as exc:
                raise BoundaryPublishFailedError(f"内部错误：将要发布的章节计划无效：{exc}") from exc
            result_id = _compute_result_id(plan)
            report = build_execution_report(
                task_id,
                plan,
                timeline_sha256=timeline_sha256,
                business_call_count=business_call_count,
                supplier_request_count=supplier_request_count,
                errors=errors,
                credential_present=credential_present,
            )
            plan_bytes = canonical_json_bytes(plan)
            file_bytes: dict[str, bytes] = {BOUNDARY_PLAN_FILE: plan_bytes}
            if result_source == "primary" and raw_response is not None:
                file_bytes[BOUNDARY_RAW_FILE] = _json_bytes(raw_response)
            report_bytes = _json_bytes(report)
            file_bytes[BOUNDARY_REPORT_FILE] = report_bytes
            manifest = _build_manifest(
                plan,
                result_id=result_id,
                cache_sha256=cache_sha256,
                timeline_id=timeline_id,
                timeline_sha256=timeline_sha256,
                timeline_manifest_sha256=timeline_manifest_sha256,
                file_bytes=file_bytes,
            )
            manifest_bytes = _json_bytes(manifest)
            group = groups_dir / result_id

            if group.exists():
                # F.6：既有同身份组必须逐字节一致才复用；任何差异都是CACHE_CORRUPT。
                for name, payload in {**file_bytes, BOUNDARY_MANIFEST_FILE: manifest_bytes}.items():
                    path = group / name
                    if not path.is_file() or path.read_bytes() != payload:
                        raise BoundaryCacheCorruptError(
                            "CACHE_CORRUPT：同身份章节计划组已存在但内容与本次发布不一致。"
                        )
                reused = True
            else:
                reused = False
                groups_dir.mkdir(parents=True, exist_ok=True)
                temporary = groups_dir / f".tmp-{result_id}-{uuid.uuid4().hex}"
                temporary.mkdir(exist_ok=False)
                cleanup_status = "created"
                for name in (BOUNDARY_PLAN_FILE, BOUNDARY_RAW_FILE, BOUNDARY_REPORT_FILE):
                    if name in file_bytes:
                        _write_fsync(temporary / name, file_bytes[name])
                # manifest最后写（与F1/F2/OCR/13-1一致）。
                _write_fsync(temporary / BOUNDARY_MANIFEST_FILE, manifest_bytes)
                validate_chapter_plan_group(
                    temporary,
                    cache_sha256=cache_sha256,
                    timeline_index=timeline_index,
                    timeline_manifest_sha256=timeline_manifest_sha256,
                    expected_result_id=result_id,
                    require_directory_name=False,
                )
                _inject(failure_injection, "before_group_publish")
                os.replace(temporary, group)
                created_group = True
                cleanup_status = "published"
                _inject(failure_injection, "after_group_publish")
                validate_chapter_plan_group(
                    group,
                    cache_sha256=cache_sha256,
                    timeline_index=timeline_index,
                    timeline_manifest_sha256=timeline_manifest_sha256,
                    expected_result_id=result_id,
                )

            # 决策：pointer_repaired仅在“复用既有同身份组但current缺失/指向他组”时置真
            # （裁决F.6的原子指针修复）；首次正常发布切换current不算“修复”。
            pointer_repaired = reused and previous_current_id != result_id
            if previous_current_id != result_id:
                stage = "current_publish"
                if not reused:
                    _inject(failure_injection, "before_current")
                _replace_current(root, _current_payload(result_id, manifest_bytes))
            return _result_from_group(
                group,
                result_id,
                plan,
                manifest,
                report,
                cache_hit=reused,
                pointer_repaired=pointer_repaired,
                duration_ms=duration_ms,
                timeline_id=timeline_id,
            )
        except BaseException as exc:
            current_identity = _disk_current_identity(root)
            if (
                result_id is not None
                and created_group
                and group.exists()
                and _may_discard_published_group(root, result_id)
            ):
                shutil.rmtree(group, ignore_errors=True)
                cleanup_status = "group_removed"
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
                cleanup_status = "temp_removed"
            try:
                _write_failure(
                    m2_root,
                    task_id=task_id,
                    stage=stage,
                    error=exc,
                    input_ids={
                        "task_id": task_id,
                        "timeline_id": timeline_id,
                        "timeline_sha256": timeline_sha256,
                        "role_cache_identity_sha256": role_cache_sha256,
                        "result_id": result_id,
                    },
                    cache_sha256=cache_sha256,
                    previous_current_id=previous_current_id,
                    cleanup_status=cleanup_status,
                    current_unchanged=current_identity == initial_current_identity,
                    business_call_count=business_call_count,
                    supplier_request_count=max(
                        supplier_request_count, model_progress["supplier_request_count"]
                    ),
                )
            except BaseException:
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, BoundaryPlannerV2Error):
                raise
            raise BoundaryPublishFailedError(
                f"无法发布ChapterPlan 2.0结果组：{type(exc).__name__}"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)


def _result_from_group(
    group: Path,
    result_id: str,
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    cache_hit: bool,
    pointer_repaired: bool,
    duration_ms: int,
    timeline_id: str,
) -> BoundaryPlanResult:
    parameters = plan["parameters"]
    return BoundaryPlanResult(
        group_path=group,
        result_id=result_id,
        plan=dict(plan),
        manifest=dict(manifest),
        report=dict(report),
        status=str(report["status"]),
        result_source=str(parameters["result_source"]),
        quality_level=str(parameters["quality_level"]),
        boundary_count=len(plan["boundaries"]),
        duration_ms=duration_ms,
        timeline_id=timeline_id,
        cache_hit=cache_hit,
        pointer_repaired=pointer_repaired,
        business_call_count=int(report["business_call_count"]),
        supplier_request_count=int(report["supplier_request_count"]),
    )
