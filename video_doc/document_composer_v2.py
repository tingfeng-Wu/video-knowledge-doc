"""阶段14-C：M2文档正文组合（DocumentComposerV2 / StructuredDocumentContent 3.0）。

契约唯一权威：``docs/development/阶段14CD-M2文档链裁决.md``（Q1/Q2/Q3/Q4/Q6逐项
裁决）＋``docs/development/阶段15-content失败修复裁决.md``（D1–D3修订失败取证、
纠错重试与报告透明语义；E4三种子0/3实证驱动）。本模块按裁决§四逐条零偏离落地；
下列编号实现决定供G1「提问单→裁决文档→实现diff」三点一致性核对。发布/缓存/中断/
审计工程逐字沿14-B（boundary_planner_v2）与14-A（chapter_frames）先例，不新发明。

实现决定（编号供G1审查核对）：
1. 入口签名按裁决Q6.2字面落地``compose_document_v2(output_root, task_id, *, ...)``
   （output_root为首位参），其余注入参数（project_root/environ/failure_injection）
   沿14-B先例补齐为关键字参数。
2. 上游current复验失败沿用14-A先例的域专属错误码（boundaries→
   INPUT_CHAPTER_PLAN_MISSING/CORRUPT、timelines→INPUT_TIMELINE_MISSING/CORRUPT、
   chapter-frames→INPUT_CHAPTER_FRAME_SELECTION_MISSING/CORRUPT、frames→
   INPUT_FRAME_SET_MISSING/CORRUPT）；仅本域自身current用CURRENT_POINTER_CORRUPT。
   task.json缺失、不可读与task_id不符一律INPUT_TASK_MISSING（14-A先例；本模块
   无SOURCE_VIDEO_MISMATCH，裁决Q6.10）。
3. chapter-frames域没有公开的指针读取入口，故本模块自带对
   ``m2/chapter-frames/current.json``的严格指针读取（result_id＋
   manifest_sha256进缓存身份；裁决Q4.1"14-A loader为帧与说明唯一权威"约束的是
   选择对象内容——一律经``load_published_chapter_frame_selection``读取并复验，
   指针字节身份不在其覆盖范围）。
4. 执行报告为裁决Q6.11枚举的12键（无schema_name、无created_at）；报告只在首次
   发布时写入，缓存命中与指针修复复用既不重写也不比对报告内容（裁决Q6.8"报告
   永不重写"）。报告versions{}子对象字段裁决未枚举，落地为generation的版本镜像
   （确定性字符串），列入G1增量清单。
5. 缓存命中路径的确定性文件逐字节比较（裁决Q6.8）按文件语义落地：
   document-content.json与manifest.json逐字节强一致（manifest逐项哈希又钉死
   raw-responses与report的磁盘字节）；raw-responses.json含每次运行的
   cache_hit/计数事实字段，不可能是跨运行不变量，故其一致性经由"逐条原始响应
   重放业务校验且规范化结果与既有正文块逐字节吻合"的组复验保证——对篡改同样
   硬失败（CACHE_CORRUPT），不放宽任何语义。
6. primary计划若只有1章（boundaries恰1条），batch调用同样跳过——与emergency
   单章同样2次调用、generation.prompt_versions.batch=null、
   validator_versions.batch=null、raw-responses含2条目（裁决把跳过条件定义为
   "章数是否为1"，不按result_source）。
7. frames指针的result_id/manifest_sha256取实时frames current（活权威）；
   selection.source_frame_set记录的组身份与实时指针不一致时
   INPUT_IDENTITY_MISMATCH（裁决Q4.3绑定式的自然延伸；组字节不可变，正常
   情况下逐字节相等）。
8. ``content_role_identity_sha256``按registry的identity_algorithm
   （"sha256(canonical_json(role_without_role_cache_identity_sha256))"）落地：
   对已校验的LlmRoleConfig做``dataclasses.asdict``后剔除
   ``role_cache_identity_sha256``字段（如存在）再取规范JSON的SHA-256——与
   registry JSON中的角色字典语义等价（registry文件本身不含该计算字段）。
9. 逐调用role_cache_identity的prompt_content_sha256＝该调用槽位"渲染前模板
   身份对象"的规范JSON SHA-256（对象含call_slot/prompt_version/validation_id/
   unit_policy_version/system/user_prefix）；registry中
   prompt_bundle.content_sha256＝[single,batch,global]三个模板身份对象数组的
   规范JSON SHA-256（裁决Q6.3字面"三个渲染前模板串数组"，模板身份对象是"串＋
   版本＋标记"的完整渲染前形态，落地为数组对象并在实现时真实计算，非fixture）。
10. llm-cache的normalized_response存各槽位"合并前已验证形状"（single为
    {"paragraphs":[…]}、batch为{"chapters":[…]}、global为
    {"title","overview","conclusions"}）；≤4段合并是发布装配阶段的确定性推导，
    不进缓存对象。
11. M1 global校验中的5000字符总长上限与硬承诺检查没有对应的裁决冻结码
    （裁决Q2.4把15码清单定为唯一封闭词表），不发明新码；相应约束保留在
    system提示词层，结构校验只执行15码覆盖的形态检查。
12. generation.mode：调用方注入``transport``对象⇒"content-composer-v2-mock"
    且generation.backend="mock"；未注入（CLI默认接线
    OpenAICompatibleTransport）⇒"content-composer-v2-real"且backend取角色
    connection_profile.backend。raw-responses每条目attempt_count＝该槽位成功
    所消耗的真实尝试次数（1..2，修复裁决D3.1；缓存命中恒为1）。
    （历史：max_attempts=1时代恒为1，2026-10-28修复裁决修订。）
13. te事件→章节归属按``start_ms``落在半开区间[start,end)（末章end=
    duration_ms）；章序与章号c0001起按plan.boundaries位置1基编号。这是裁决
    Q2.2"章内te按时间轴序聚合"与boundary_alignment=
    selected_transcript_event_start的确定性展开。
14. 传输层返回JSON字符串时先解析为dict再进业务校验（真实
    OpenAICompatibleTransport返回choices[0].message.content字符串）；解析失败
    记kind="transport"，与14-B对传输异常的处理同型；业务15码与结构形态失败
    记kind="validation"。errors明细只进失败审计safe_message（脱敏、截断、
    绝不含模型原始响应正文）。
15. CONTENT_MODEL_FAILED为响应侧唯一命令级收敛（裁决Q2.5）；CONTENT_BUDGET_
    EXCEEDED仅守卫实现缺陷（预算3/3按构造先于任何transport.complete检查，
    llm-cache命中计0次供应商请求）；CONTENT_VALIDATION_FAILED只出现在
    "已通过业务校验的响应经确定性装配后仍通不过structured-document-content-
    validator-3.0"的内部一致性断言。二者与PUBLISH_FAILED并列于Q6.10词表。
16. 失败注入点沿14-B全部6点（after_inputs/before_model_call/after_model_call/
    before_group_publish/after_group_publish/before_current）；
    before_model_call/after_model_call作用于整个三调用组合阶段的边界（裁决未
    定义逐槽注入点，注入异常同样收敛为PUBLISH_FAILED审计码）。
17. 缓存命中路径的CLI调用计数读既有execution-report.json的字段值
    （裁决Q6.8"CLI摘要的调用计数读report"）；缓存命中时
    supplier_request_count可为0。
18. messages_sha256按裁决Q6.8"既有P3 §7.1 canonical口径"＝messages列表的
    规范JSON SHA-256（canonical_json_sha256），不引入新哈希形态。
19. 外发payload紧凑串＝json.dumps(payload, ensure_ascii=False,
    separators=(",", ":"))（键按构造序），message_chars＝user+system内容字符
    总长，payload_chars＝紧凑串长度；两者是审计/观测字段，不进缓存身份。
20. M1``canonical_sha256``按裁决Q1.2纯函数导入复用（不修改M1）；M1
    ``_cap_document_paragraphs``的合并机制（相邻段落以单个换行"\n"连接、
    source_unit_ids并集保序）在本模块内以同构实现落地——M1该函数硬绑定
    fact_ids字段，跨版本导入私有函数属修改面扩大，复制机制＋换行连接为裁决
    Q2.5的字面要求。
21. structured-document-content-validator-3.0是本模块私有校验器（裁决Q1.1"落
    在composer自持校验器"）；对14-A帧投影做逐字节6键比较（裁决Q4.2"帧＝14-A
    字段投影冻结6键，逐字节"），并复检decoded_ms∈[start,end)、rank=1..n排列、
    tier∈{1..4}、tier↔caption_form冻结映射（导入复用chapter_frames的
    TIER_TO_FORM常量，单一事实源）。
22. content组manifest的`files`按14-B先例登记除manifest.json外的三个组文件
    （manifest自身哈希自引用不可构造）；裁决Q6.4"files=四文件"解释为组文件集
    为四文件。组复验据此检查files清单恰为这三文件。
23. 零API预览``build_content_composition_dry_run``中global调用的payload依赖
    此前业务调用的已验证正文，预览以"章标题＋空paragraphs"的结构形态列出
    （char计数因此低估global正文部分），逐项在返回中附note字段说明；single与
    batch预览为精确外发形态。M1先例把global预览作为组合后独立函数
    （fact_card_composition.py:484），本模块按裁决Q2.9"逐调用"清单在同一返回
    内覆盖三槽位。
24. 纠错重试（修复裁决D2）：每槽位至多2次供应商请求，反馈消息形态与14-B
    逐点同构——校验失败＝原messages＋assistant回声（仅当响应解析为dict）＋
    user纠错消息（issues清单JSON＋_CONTENT_VALIDATION_FEEDBACK）；传输/解析
    失败＝原messages＋user消息（脱敏错误文本＋_TRANSPORT_FEEDBACK_INSTRUCTION，
    与14-B同串）。两反馈常量为模块级、不进_prompt_template_identity，故
    prompt_bundle.content_sha256与三prompt版本串逐字节不变（修复裁决D2.3）。
25. 失败取证（修复裁决D1）：ContentModelFailedError携带failed_attempts载荷，
    失败审计组在failure.json（Schema不动）旁增补model-responses.json
    （ContentFailedResponses/1.0，逐失败尝试的raw_response原样值＋脱敏message＋
    issues；传输异常时raw_response=null）。写入与failure.json同一尽力而为
    包裹，取证失败不覆盖原始异常；仅CONTENT_MODEL_FAILED产生该文件。
26. 报告透明性（修复裁决D3.2）：成功组execution-report.errors[]记录全部被
    纠正的第1次失败（{attempt,kind,message,issues}四键、attempt恒为1）；
    计数不变式live=business−cache_hits、live≤supplier≤2×live、
    len(errors)==supplier−live。既有旧形态报告（errors=[]、supplier==live）
    满足不变式，复验向后兼容。
27. 缓存条目messages_sha256恒为该槽位attempt-1消息的规范哈希（14-B先例，
    修复裁决D2.5）：纠错消息不入缓存身份；失败尝试不写llm-cache。

零真实API：本模块的默认接线仅在CLI显式调用时构建OpenAICompatibleTransport；
14-C离线验收全部经注入transport完成，真实content_model调用按裁决Q7.6挂账
（2026-10-28修复裁决D6.4：三种子重跑为新的挂账，需用户§4.3新批准）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from video_doc.boundary_planner_v2 import (
    BoundaryPlannerV2Error,
    load_chapter_plan_current,
    load_published_chapter_plan,
)
from video_doc.chapter_frames import (
    CAPTION_TEMPLATE_VERSION,
    ChapterFramesError,
    SELECTION_POLICY_VERSION,
    TIER_TO_FORM,
    load_published_chapter_frame_selection,
)
from video_doc.frame_sampler import (
    FRAME_MODULE_TOKEN,
    FrameSamplerError,
    artifact_group_path,
    is_safe_result_id,
    load_frame_set_current,
)
from video_doc.llm_adapter import (
    LLMConfig,
    LLMTransport,
    OpenAICompatibleTransport,
)
from video_doc.model_roles import (
    LlmRoleConfig,
    ModelRoleRegistryError,
    RegistrySnapshot,
    build_llm_config,
    compute_role_cache_identity,
    resolve_api_key,
    resolve_model_registry,
)
from video_doc.structured_document_content import canonical_sha256
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
# 冻结身份字符串（裁决Q1/Q2/Q6.3，逐字）
# ---------------------------------------------------------------------------

# 二次失败形态裁决N3：composer版本1.1（N1 single信封规范化＋N2 batch上限8192）。
# 组复验按generation.composer_version查HISTORY，1.0旧组保持合法（N3.1–N3.5）。
COMPOSER_VERSION = "1.1"
CONTENT_SCHEMA_NAME = "StructuredDocumentContent"
CONTENT_SCHEMA_VERSION = "3.0"
ARTIFACT_TYPE = "document_content"
MODULE_TOKEN = "document_composer_v2"
ROOT_DIRECTORY = "content"
GROUP_DIRECTORY = "formal-results"
LLM_CACHE_DIRECTORY = "llm-cache"
CURRENT_FILE = "current.json"
LOCK_FILE = "content.lock"
CONTENT_FILE = "document-content.json"
RAW_RESPONSES_FILE = "raw-responses.json"
REPORT_FILE = "execution-report.json"
MANIFEST_FILE = "manifest.json"

CONTENT_VALIDATOR_VERSION = "structured-document-content-validator-3.0"
CONTENT_POLICY_VERSION = "document-timeline-evidence-only-1.0"
UNIT_POLICY_VERSION = "continuous-te-unit-1.0"
BLOCK_POLICY_VERSION = "m2-document-block-policy-1.0"
COVERAGE_POLICY_VERSION = "m2-unit-coverage-1.0"

ROLE_NAME = "content_model"
# 修复裁决D2.1：角色配置2.0（validation-feedback-1.0一次纠错、预算{3,6}）。
ROLE_CONFIG_VERSION = "content-model-role-2.0"
INPUT_CONTRACT = "M2ContentCompositionInput/1.0"
OUTPUT_CONTRACT = "StructuredDocumentContent/3.0"
VALIDATION_SUITE_ID = "document-content-validator-suite-2-0"
VALIDATION_RETRY_POLICY = "validation-feedback-1.0"
CACHE_NAMESPACE = "content-model-unified-timeline-2-0"
FROZEN_MODEL = "qwen3.7-flash"
FROZEN_RESPONSE_FORMAT: dict[str, Any] = {"type": "json_object"}
FROZEN_ALTERNATE_FALLBACK = "none"
FROZEN_MAX_ATTEMPTS = 2
FROZEN_BUSINESS_BUDGET = 3
FROZEN_SUPPLIER_BUDGET = 6

CALL_SINGLE = "single"
CALL_BATCH = "batch"
CALL_GLOBAL = "global"
CALL_SLOTS = (CALL_SINGLE, CALL_BATCH, CALL_GLOBAL)

PROMPT_VERSIONS = {
    CALL_SINGLE: "document-content-single-unified-timeline-2.0",
    CALL_BATCH: "document-content-batch-unified-timeline-2.0",
    CALL_GLOBAL: "document-content-global-unified-timeline-2.0",
}
VALIDATION_IDS = {
    CALL_SINGLE: "document-content-single-validator-2-0",
    CALL_BATCH: "document-content-batch-validator-2-0",
    CALL_GLOBAL: "document-content-global-validator-2-0",
}
# 二次失败形态裁决N2/N3：逐调用上限按composer版本冻结；组复验以旧值核旧组。
PER_CALL_MAX_TOKENS_HISTORY: dict[str, dict[str, int]] = {
    "1.0": {CALL_SINGLE: 1800, CALL_BATCH: 3600, CALL_GLOBAL: 1600},
    "1.1": {CALL_SINGLE: 1800, CALL_BATCH: 8192, CALL_GLOBAL: 1600},
    "1.2": {CALL_SINGLE: 4096, CALL_BATCH: 4096, CALL_GLOBAL: 1600},
}
PER_CALL_MAX_TOKENS = PER_CALL_MAX_TOKENS_HISTORY[COMPOSER_VERSION]


def composer_version_for_tokens(per_call_max_tokens: Any) -> str | None:
    """二次失败形态裁决N3版本判别：由generation.per_call_max_tokens在HISTORY
    值集上冻结反查组的composer版本（两版本映射互异、单射）；未知映射返回None。"""

    if not isinstance(per_call_max_tokens, dict):
        return None
    for version, frozen in PER_CALL_MAX_TOKENS_HISTORY.items():
        if per_call_max_tokens == frozen:
            return version
    return None

UNIT_MAX_CHARS = 800
PARAGRAPH_RAW_MAX = 8
PER_CHAPTER_PARAGRAPH_MAX = 4
TITLE_MAX_CHARS = 100

MODE_REAL = "content-composer-v2-real"
MODE_MOCK = "content-composer-v2-mock"

BLOCK_TYPE = "paragraph"
BLOCK_ORIGIN = "timeline2_continuous"
BLOCK_ROLES = frozenset({"explanation", "overview", "conclusion"})
CHAPTER_BLOCK_ROLE = "explanation"
OVERVIEW_BLOCK_ROLE = "overview"
CONCLUSION_BLOCK_ROLE = "conclusion"

MANIFEST_SCHEMA_NAME = "ArtifactGroupManifest"
MANIFEST_SCHEMA_VERSION = "1.0"
CURRENT_SCHEMA_NAME = "ArtifactCurrentPointer"
CURRENT_SCHEMA_VERSION = "1.0"
REPORT_SCHEMA_VERSION = "1.0"
FAILURE_SCHEMA_NAME = "ModuleFailureAudit"
FAILURE_SCHEMA_VERSION = "1.0"
CACHE_SCHEMA_NAME = "DocumentComposerV2CacheIdentity"
CACHE_SCHEMA_VERSION = "1.0"
LLM_CACHE_SCHEMA_NAME = "ContentRoleCallCache"
LLM_CACHE_SCHEMA_VERSION = "1.0"
RAW_DOC_SCHEMA_NAME = "ContentRawResponses"
RAW_DOC_SCHEMA_VERSION = "1.0"
PROMPT_TEMPLATE_SCHEMA_NAME = "DocumentContentPromptTemplate"
PROMPT_TEMPLATE_SCHEMA_VERSION = "1.0"
# 修复裁决D1：失败取证文件（仅CONTENT_MODEL_FAILED失败组产生）。
MODEL_RESPONSES_FILE = "model-responses.json"
FAILED_DOC_SCHEMA_NAME = "ContentFailedResponses"
FAILED_DOC_SCHEMA_VERSION = "1.0"
_FAILED_ATTEMPT_KEYS = {
    "call_slot",
    "attempt",
    "kind",
    "message",
    "issues",
    "raw_response",
}

RESULT_ID_PREFIX = "document-content-"

# 命令级失败码（裁决Q6.10，恰20个）
ERROR_INPUT_TASK_MISSING = "INPUT_TASK_MISSING"
ERROR_INPUT_CHAPTER_PLAN_MISSING = "INPUT_CHAPTER_PLAN_MISSING"
ERROR_INPUT_CHAPTER_PLAN_CORRUPT = "INPUT_CHAPTER_PLAN_CORRUPT"
ERROR_INPUT_TIMELINE_MISSING = "INPUT_TIMELINE_MISSING"
ERROR_INPUT_TIMELINE_CORRUPT = "INPUT_TIMELINE_CORRUPT"
ERROR_INPUT_CHAPTER_FRAME_SELECTION_MISSING = "INPUT_CHAPTER_FRAME_SELECTION_MISSING"
ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT = "INPUT_CHAPTER_FRAME_SELECTION_CORRUPT"
ERROR_INPUT_FRAME_SET_MISSING = "INPUT_FRAME_SET_MISSING"
ERROR_INPUT_FRAME_SET_CORRUPT = "INPUT_FRAME_SET_CORRUPT"
ERROR_INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
ERROR_UNIT_CONSTRUCTION_FAILED = "UNIT_CONSTRUCTION_FAILED"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_ROLE_CONFIG_MISSING = "ROLE_CONFIG_MISSING"
ERROR_ROLE_CONFIG_INVALID = "ROLE_CONFIG_INVALID"
ERROR_CONTENT_MODEL_FAILED = "CONTENT_MODEL_FAILED"
ERROR_CONTENT_BUDGET_EXCEEDED = "CONTENT_BUDGET_EXCEEDED"
ERROR_CONTENT_VALIDATION_FAILED = "CONTENT_VALIDATION_FAILED"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"

COMMAND_CODES = frozenset(
    {
        ERROR_INPUT_TASK_MISSING,
        ERROR_INPUT_CHAPTER_PLAN_MISSING,
        ERROR_INPUT_CHAPTER_PLAN_CORRUPT,
        ERROR_INPUT_TIMELINE_MISSING,
        ERROR_INPUT_TIMELINE_CORRUPT,
        ERROR_INPUT_CHAPTER_FRAME_SELECTION_MISSING,
        ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT,
        ERROR_INPUT_FRAME_SET_MISSING,
        ERROR_INPUT_FRAME_SET_CORRUPT,
        ERROR_INPUT_IDENTITY_MISMATCH,
        ERROR_UNIT_CONSTRUCTION_FAILED,
        ERROR_CURRENT_POINTER_CORRUPT,
        ERROR_CACHE_CORRUPT,
        ERROR_CONCURRENT_RUN,
        ERROR_ROLE_CONFIG_MISSING,
        ERROR_ROLE_CONFIG_INVALID,
        ERROR_CONTENT_MODEL_FAILED,
        ERROR_CONTENT_BUDGET_EXCEEDED,
        ERROR_CONTENT_VALIDATION_FAILED,
        ERROR_PUBLISH_FAILED,
    }
)

# 业务校验码（裁决Q2.4，恰15个；只进失败审计与组复验，命令级收敛
# CONTENT_MODEL_FAILED）
BUSINESS_CODES = frozenset(
    {
        "RESPONSE_NOT_OBJECT",
        "RESPONSE_KEYS_INVALID",
        "PARAGRAPHS_MISSING",
        "PARAGRAPH_NOT_OBJECT",
        "PARAGRAPH_TEXT_EMPTY",
        "PARAGRAPH_COUNT_EXCEEDED",
        "UNIT_ID_NOT_FOUND",
        "UNIT_CROSS_CHAPTER",
        "UNIT_ORDER_VIOLATION",
        "UNIT_DUPLICATE_REFERENCE",
        "UNIT_COVERAGE_INCOMPLETE",
        "BATCH_CHAPTERS_MISMATCH",
        "GLOBAL_TITLE_INVALID",
        "OVERVIEW_COUNT_INVALID",
        "CONCLUSIONS_COUNT_INVALID",
    }
)

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
    "plan_result_id",
    "plan_manifest_sha256",
    "timeline_id",
    "timeline_manifest_sha256",
    "chapter_frame_selection_result_id",
    "chapter_frame_selection_manifest_sha256",
    "frame_set_id",
    "frame_set_manifest_sha256",
}
_MANIFEST_VERSION_KEYS = {
    "document_composer_v2",
    "content_schema",
    "content_policy_version",
    "unit_policy_version",
    "prompt_version_single",
    "prompt_version_batch",
    "prompt_version_global",
    "validation_id_single",
    "validation_id_batch",
    "validation_id_global",
}
_GROUP_FILE_NAMES = {CONTENT_FILE, RAW_RESPONSES_FILE, REPORT_FILE, MANIFEST_FILE}

# StructuredDocumentContent 3.0精确键集（裁决Q1.2/§四.1，逐字）
_CONTENT_TOP_KEYS = {
    "schema_name",
    "schema_version",
    "task_id",
    "language",
    "source_language",
    "title",
    "strategy",
    "overview",
    "overall_conclusion",
    "chapters",
    "coverage",
    "generation",
    "content_versions",
    "source_plan",
    "source_timeline",
    "source_chapter_frame_selection",
    "source_frame_set",
    "content_id",
}
_BLOCK_KEYS = {
    "block_id",
    "type",
    "role",
    "text",
    "content_origin",
    "source_unit_ids",
    "source_event_ids",
    "source_chapter_ids",
}
_CHAPTER_KEYS = {
    "chapter_id",
    "index",
    "start_ms",
    "end_ms",
    "title",
    "blocks",
    "frames",
    "units_referenced",
}
_FRAME_PROJECTION_KEYS = (
    "frame_id",
    "rank",
    "tier",
    "decoded_ms",
    "caption_form",
    "caption_text",
)
_FRAME_PROJECTION_KEY_SET = set(_FRAME_PROJECTION_KEYS)
_COVERAGE_KEYS = {"policy_version", "chapters", "totals"}
_COVERAGE_CHAPTER_KEYS = {
    "chapter_id",
    "unit_count",
    "covered_unit_count",
    "event_count",
    "covered_event_count",
}
_COVERAGE_TOTALS_KEYS = {
    "unit_count",
    "covered_unit_count",
    "event_count",
    "covered_event_count",
    "full_coverage",
}
_GENERATION_KEYS = {
    "status",
    "mode",
    "backend",
    "model",
    "temperature",
    "enable_thinking",
    "prompt_versions",
    "content_policy_version",
    "unit_policy_version",
    "validator_versions",
    "validator_version",
    "per_call_max_tokens",
}
_GENERATION_VERSION_MAP_KEYS = {"single", "batch", "global"}
_CONTENT_VERSIONS_KEYS = {
    "schema",
    "content_policy",
    "unit_policy",
    "block_policy",
    "validator",
}
_SOURCE_PLAN_KEYS = {
    "result_id",
    "manifest_sha256",
    "result_source",
    "quality_level",
    "boundary_count",
    "strategy",
}
_SOURCE_TIMELINE_KEYS = {
    "timeline_id",
    "sha256",
    "manifest_sha256",
    "event_count",
    "transcript_event_count",
}
_SOURCE_SELECTION_KEYS = {
    "result_id",
    "manifest_sha256",
    "selection_policy_version",
    "caption_template_version",
}
_SOURCE_FRAME_SET_KEYS = {"frame_set_id", "manifest_sha256", "frame_count"}
_RAW_DOC_KEYS = {"schema_name", "schema_version", "calls"}
_RAW_CALL_KEYS = {
    "call_slot",
    "prompt_version",
    "validation_id",
    "raw_response",
    "cache_hit",
    "attempt_count",
    "payload_chars",
    "message_chars",
    "max_tokens",
}
# 裁决Q6.11枚举恰12键：无schema_name、无created_at（实现决定4）
_REPORT_KEYS_1_0 = {
    "schema_version",
    "status",
    "task_id",
    "content_id",
    "plan_result_source",
    "plan_quality_level",
    "chapter_count",
    "business_call_count",
    "supplier_request_count",
    "cache_hit_count",
    "errors",
    "versions",
}
# 二次失败形态裁决N1/N3.4：1.1组报告新增第13键response_normalizations；
# 1.0组必须缺席该键（版本判别键集，D3向后兼容先例同构）。
_REPORT_KEYS_1_1 = _REPORT_KEYS_1_0 | {"response_normalizations"}
_REPORT_KEYS_BY_COMPOSER = {"1.0": _REPORT_KEYS_1_0, "1.1": _REPORT_KEYS_1_1, "1.2": _REPORT_KEYS_1_1}
_REPORT_KEYS = _REPORT_KEYS_1_1
# N1透明性记录冻结键集与信封名。
_NORMALIZATION_RECORD_KEYS = {"call_slot", "attempt", "envelope", "chapter_id"}
SINGLE_ENVELOPE_NORMALIZATION = "chapters-single"
_LLM_CACHE_KEYS = {
    "schema_name",
    "schema_version",
    "call_slot",
    "role_cache_identity_sha256",
    "messages_sha256",
    "raw_response",
    "normalized_response",
    "supplier_request_count",
    "backend",
    "model",
    "created_at",
}
_CACHE_IDENTITY_KEYS = {
    "schema_name",
    "schema_version",
    "task_id",
    "composer_version",
    "content_schema",
    "content_policy_version",
    "unit_policy_version",
    "prompt_versions",
    "validation_ids",
    "per_call_max_tokens",
    "paragraph_merge_max",
    "plan_result_id",
    "plan_manifest_sha256",
    "timeline_id",
    "timeline_manifest_sha256",
    "chapter_frame_selection_result_id",
    "chapter_frame_selection_manifest_sha256",
    "frame_set_id",
    "frame_set_manifest_sha256",
    "content_role_identity_sha256",
}

# 响应形态精确键集（裁决Q2.3响应契约）
_SINGLE_RESPONSE_KEYS = {"paragraphs"}
_BATCH_RESPONSE_KEYS = {"chapters"}
_BATCH_CHAPTER_KEYS = {"chapter_id", "paragraphs"}
_PARAGRAPH_KEYS = {"source_unit_ids", "text"}
_GLOBAL_RESPONSE_KEYS = {"title", "overview", "conclusions"}

_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_CHAPTER_ID_RE = re.compile(r"^c[0-9]{4}$")
_UNIT_ID_RE = re.compile(r"^c[0-9]{4}-u[0-9]{3}$")
_TE_EVENT_RE = re.compile(r"^te[0-9]{6}$")
_BLOCK_ID_RE = re.compile(r"^(?:c[0-9]{4}|overview|conclusion)-p[0-9]{3}$")


class DocumentComposerV2Error(RuntimeError):
    """14-C命令级类型化失败基类；error_code必须属于Q6.10的20码词表。"""

    error_code = ERROR_PUBLISH_FAILED
    recoverable = False
    next_action = "none"
    domain: str | None = None


class InputTaskMissingError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_TASK_MISSING
    recoverable = True
    next_action = "run_init_task"


class InputChapterPlanMissingError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_CHAPTER_PLAN_MISSING
    recoverable = True
    next_action = "run_plan_chapters_v2"


class InputChapterPlanCorruptError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_CHAPTER_PLAN_CORRUPT
    recoverable = False
    next_action = "none"


class InputTimelineMissingError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_TIMELINE_MISSING
    recoverable = True
    next_action = "run_build_timeline_v2"


class InputTimelineCorruptError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_TIMELINE_CORRUPT
    recoverable = False
    next_action = "none"


class InputChapterFrameSelectionMissingError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_CHAPTER_FRAME_SELECTION_MISSING
    recoverable = True
    next_action = "run_select_chapter_frames"


class InputChapterFrameSelectionCorruptError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT
    recoverable = False
    next_action = "none"


class InputFrameSetMissingError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_FRAME_SET_MISSING
    recoverable = True
    next_action = "run_sample_frames"


class InputFrameSetCorruptError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_FRAME_SET_CORRUPT
    recoverable = False
    next_action = "none"


class InputIdentityMismatchError(DocumentComposerV2Error):
    error_code = ERROR_INPUT_IDENTITY_MISMATCH
    recoverable = False
    next_action = "none"


class UnitConstructionFailedError(DocumentComposerV2Error):
    error_code = ERROR_UNIT_CONSTRUCTION_FAILED
    recoverable = False
    next_action = "none"


class CurrentPointerCorruptError(DocumentComposerV2Error):
    error_code = ERROR_CURRENT_POINTER_CORRUPT
    recoverable = False
    next_action = "none"


class ContentCacheCorruptError(DocumentComposerV2Error):
    error_code = ERROR_CACHE_CORRUPT
    recoverable = False
    next_action = "none"


class ContentConcurrentRunError(DocumentComposerV2Error):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "retry_after_other_run_finishes"


class RoleConfigMissingError(DocumentComposerV2Error):
    error_code = ERROR_ROLE_CONFIG_MISSING
    recoverable = True
    next_action = "provide_model_registry"


class RoleConfigInvalidError(DocumentComposerV2Error):
    error_code = ERROR_ROLE_CONFIG_INVALID
    recoverable = True
    next_action = "fix_model_registry"


class ContentModelFailedError(DocumentComposerV2Error):
    """响应侧唯一命令级收敛（裁决Q2.5）：传输失败或15码校验失败，零发布。

    ``failed_attempts``为逐失败尝试的取证载荷（修复裁决D1/D3.4），由失败审计
    落盘为``model-responses.json``；raw_response保存transport返回原样值。
    """

    error_code = ERROR_CONTENT_MODEL_FAILED
    recoverable = True
    next_action = "review_content_model_response"

    def __init__(
        self,
        message: str,
        *,
        errors: Sequence[Mapping[str, Any]],
        failed_attempts: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(message)
        self.errors = [dict(item) for item in errors]
        self.failed_attempts = [dict(item) for item in failed_attempts]


class ContentBudgetExceededError(DocumentComposerV2Error):
    error_code = ERROR_CONTENT_BUDGET_EXCEEDED
    recoverable = False
    next_action = "none"


class ContentValidationFailedError(DocumentComposerV2Error):
    error_code = ERROR_CONTENT_VALIDATION_FAILED
    recoverable = False
    next_action = "none"


class ContentPublishFailedError(DocumentComposerV2Error):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = True
    next_action = "retry_or_inspect_failures"


class InjectedContentFailure(DocumentComposerV2Error):
    recoverable = True
    next_action = "none"


class StructuredContentValidationError(RuntimeError):
    """structured-document-content-validator-3.0内部失败（装配一致性断言）。"""


@dataclass(frozen=True)
class ChapterLedger:
    """一单元的连续te主文本账本（裁决Q2.2 continuous-te-unit-1.0）。"""

    chapter_id: str
    chapter_index: int  # 1基
    start_ms: int
    end_ms: int
    title: str
    units: tuple[dict[str, Any], ...]  # {unit_id, text, source_event_ids}
    events: tuple[dict[str, Any], ...]  # 章内te事件（时间轴序）


@dataclass(frozen=True)
class ContentInputs:
    """锁前只读输入快照（纯内存，供三调用payload与复验共用）。"""

    task_id: str
    timeline_pointer: dict[str, Any]
    timeline_index: dict[str, Any]
    plan_pointer: dict[str, Any]
    plan: dict[str, Any]
    selection_pointer: dict[str, Any]
    selection: dict[str, Any]
    frames_pointer: dict[str, Any]
    ledgers: tuple[ChapterLedger, ...]


@dataclass(frozen=True)
class DocumentCompositionResult:
    group_path: Path
    result_id: str
    content: dict[str, Any]
    manifest: dict[str, Any]
    report: dict[str, Any]
    chapter_count: int
    plan_result_source: str
    plan_quality_level: str
    business_call_count: int
    supplier_request_count: int
    cache_hit_count: int
    cache_hit: bool
    pointer_repaired: bool


# ---------------------------------------------------------------------------
# 通用helpers（与14-B/14-A/F1逐点对齐，复制适配新基类）
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
    path: Path, label: str, error_type: type[DocumentComposerV2Error]
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise error_type(f"{label}无法读取或JSON无效。") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label}必须是JSON对象。")
    return value


def _current_path_state(
    path: Path, label: str, error_type: type[DocumentComposerV2Error]
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
    """错误清单回显字段脱敏：折叠空白、限长80字符。"""

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


def _compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 输入：task身份、四个上游域只读加载、身份链绑定（裁决Q4.1/Q4.3）
# ---------------------------------------------------------------------------


def _ensure_task_identity(output_root: Path, task_id: str) -> None:
    """task.json缺失/不可读/task_id不符一律INPUT_TASK_MISSING（实现决定2）。"""

    task_json = output_root / task_id / "task.json"
    if not _current_path_state(task_json, "task.json", InputTaskMissingError):
        raise InputTaskMissingError(f"{ERROR_INPUT_TASK_MISSING}：任务工作区缺少task.json登记信息。")
    record = _read_json(task_json, "task.json", InputTaskMissingError)
    if record.get("task_id") != task_id:
        raise InputTaskMissingError(
            f"{ERROR_INPUT_TASK_MISSING}：task.json记录的task_id与请求不一致。"
        )


def _m2_root(output_root: Path, task_id: str) -> Path:
    return output_root / task_id / "m2"


def _load_timeline_inputs(output_root: Path, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
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


def _load_plan_inputs(
    output_root: Path, task_id: str, timeline_index: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        pointer = load_chapter_plan_current(output_root, task_id)
    except BoundaryPlannerV2Error as exc:
        raise InputChapterPlanCorruptError(f"{ERROR_INPUT_CHAPTER_PLAN_CORRUPT}：{exc}") from exc
    if pointer is None:
        raise InputChapterPlanMissingError(
            f"{ERROR_INPUT_CHAPTER_PLAN_MISSING}：任务没有已发布的ChapterPlan 2.0 current。"
        )
    try:
        plan = load_published_chapter_plan(output_root, task_id, timeline_index=timeline_index)
    except BoundaryPlannerV2Error as exc:
        raise InputChapterPlanCorruptError(f"{ERROR_INPUT_CHAPTER_PLAN_CORRUPT}：{exc}") from exc
    if plan is None:
        raise InputChapterPlanMissingError(
            f"{ERROR_INPUT_CHAPTER_PLAN_MISSING}：任务没有已发布的ChapterPlan 2.0 current。"
        )
    return pointer, plan


def _load_selection_pointer(m2_root: Path, task_id: str) -> dict[str, Any]:
    """自带chapter-frames current严格读取（实现决定3）。"""

    root = m2_root / "chapter-frames"
    path = root / CURRENT_FILE
    if not _current_path_state(path, "chapter-frames current", InputChapterFrameSelectionCorruptError):
        raise InputChapterFrameSelectionMissingError(
            f"{ERROR_INPUT_CHAPTER_FRAME_SELECTION_MISSING}：任务没有已发布的14-A代表帧current。"
        )
    pointer = _read_json(path, "chapter-frames current", InputChapterFrameSelectionCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "chapter-frames current", InputChapterFrameSelectionCorruptError)
    if (
        pointer["schema_name"] != CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != "chapter_frame_selection"
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{GROUP_DIRECTORY}/{pointer['result_id']}"
        or not _sha256_hex(pointer["manifest_sha256"])
    ):
        raise InputChapterFrameSelectionCorruptError(
            f"{ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT}：chapter-frames current身份无效。"
        )
    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise InputChapterFrameSelectionCorruptError(
            f"{ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT}：chapter-frames current的组路径越界。"
        )
    manifest_path = group / MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise InputChapterFrameSelectionCorruptError(
            f"{ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT}：chapter-frames current目标缺失或损坏。"
        )
    return pointer


def _load_selection(
    output_root: Path, task_id: str
) -> dict[str, Any]:
    try:
        selection = load_published_chapter_frame_selection(output_root, task_id)
    except ChapterFramesError as exc:
        raise InputChapterFrameSelectionCorruptError(
            f"{ERROR_INPUT_CHAPTER_FRAME_SELECTION_CORRUPT}：{exc}"
        ) from exc
    if selection is None:
        raise InputChapterFrameSelectionMissingError(
            f"{ERROR_INPUT_CHAPTER_FRAME_SELECTION_MISSING}：任务没有已发布的14-A代表帧current。"
        )
    return selection


def _load_frames_pointer(output_root: Path, task_id: str) -> dict[str, Any]:
    try:
        pointer = load_frame_set_current(output_root, task_id)
    except FrameSamplerError as exc:
        raise InputFrameSetCorruptError(f"{ERROR_INPUT_FRAME_SET_CORRUPT}：{exc}") from exc
    if pointer is None:
        raise InputFrameSetMissingError(
            f"{ERROR_INPUT_FRAME_SET_MISSING}：任务没有已发布的帧集current。"
        )
    return pointer


def _chapter_intervals(plan: Mapping[str, Any]) -> list[tuple[int, int]]:
    """与14-A同构：boundary timestamp_ms半开区间，末章end=duration_ms。"""

    boundaries = plan["boundaries"]
    duration = int(plan["duration_ms"])
    intervals: list[tuple[int, int]] = []
    for position, boundary in enumerate(boundaries):
        start = int(boundary["timestamp_ms"])
        end = (
            int(boundaries[position + 1]["timestamp_ms"])
            if position + 1 < len(boundaries)
            else duration
        )
        intervals.append((start, end))
    return intervals


def _build_ledgers(timeline_index: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[ChapterLedger, ...]:
    """continuous-te-unit-1.0账本（裁决Q2.2；实现决定13）。

    章内te事件按时间轴序聚合，800字符/单元上限，unit_id=cNNNN-uMMM；单事件
    文本strip后为空或超过800字符→UNIT_CONSTRUCTION_FAILED硬失败，无静默截断。
    """

    if timeline_index.get("run_status") == "failed":
        raise InputTimelineCorruptError(
            f"{ERROR_INPUT_TIMELINE_CORRUPT}：Timeline 2.0的run_status为failed，不能作为正文组合输入。"
        )
    events = [event for event in timeline_index["events"] if event["type"] == TRANSCRIPT_EVENT_TYPE]
    if not events:
        raise InputTimelineCorruptError(
            f"{ERROR_INPUT_TIMELINE_CORRUPT}：Timeline 2.0不含任何主文本事件，无法生成正文。"
        )
    intervals = _chapter_intervals(plan)
    boundaries = plan["boundaries"]
    ledgers: list[ChapterLedger] = []
    for position, (start_ms, end_ms) in enumerate(intervals):
        chapter_index = position + 1
        chapter_id = f"c{chapter_index:04d}"
        title = str(boundaries[position]["title"]).strip()
        chapter_events = [event for event in events if start_ms <= int(event["start_ms"]) < end_ms]
        units: list[dict[str, Any]] = []
        pending_events: list[dict[str, Any]] = []
        for event in chapter_events:
            text = str(event["payload"]["text"]).strip()
            if not text:
                raise UnitConstructionFailedError(
                    f"{ERROR_UNIT_CONSTRUCTION_FAILED}：章{chapter_id}事件{event['id']}主文本为空。"
                )
            if len(text) > UNIT_MAX_CHARS:
                raise UnitConstructionFailedError(
                    f"{ERROR_UNIT_CONSTRUCTION_FAILED}：章{chapter_id}事件{event['id']}单事件文本"
                    f"{len(text)}字符超过{UNIT_MAX_CHARS}上限，禁止静默截断。"
                )
            if pending_events:
                candidate = " ".join(
                    [str(item["payload"]["text"]).strip() for item in pending_events] + [text]
                )
                if len(candidate) > UNIT_MAX_CHARS:
                    units.append(_flush_unit(chapter_id, len(units) + 1, pending_events))
                    pending_events = [event]
                    continue
            pending_events.append(event)
        if pending_events:
            units.append(_flush_unit(chapter_id, len(units) + 1, pending_events))
        # 展平不重不漏不乱序自检（与M1 continuous-asr-unit-1.0同构的本地账本核对）
        flattened = [event_id for unit in units for event_id in unit["source_event_ids"]]
        expected = [event["id"] for event in chapter_events]
        if flattened != expected or len(set(flattened)) != len(flattened):
            raise UnitConstructionFailedError(
                f"{ERROR_UNIT_CONSTRUCTION_FAILED}：章{chapter_id}单元账本展平与事件序列不一致。"
            )
        for unit in units:
            if len(str(unit["text"])) > UNIT_MAX_CHARS:
                raise UnitConstructionFailedError(
                    f"{ERROR_UNIT_CONSTRUCTION_FAILED}：章{chapter_id}单元{unit['unit_id']}聚合超"
                    f"{UNIT_MAX_CHARS}字符。"
                )
        ledgers.append(
            ChapterLedger(
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                start_ms=start_ms,
                end_ms=end_ms,
                title=title,
                units=tuple(units),
                events=tuple(chapter_events),
            )
        )
    if any(not ledger.units for ledger in ledgers):
        empty = [ledger.chapter_id for ledger in ledgers if not ledger.units]
        raise UnitConstructionFailedError(
            f"{ERROR_UNIT_CONSTRUCTION_FAILED}：章{empty[0]}区间内没有主文本事件，无法构成连续单元。"
        )
    return tuple(ledgers)


def _flush_unit(chapter_id: str, ordinal: int, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "unit_id": f"{chapter_id}-u{ordinal:03d}",
        "text": " ".join(str(event["payload"]["text"]).strip() for event in events),
        "source_event_ids": [str(event["id"]) for event in events],
    }


def _load_content_inputs(output_root: Path, task_id: str) -> ContentInputs:
    """锁前零写入输入加载＋身份链绑定（裁决Q4.3五对象task_id一致等）。"""

    output = Path(output_root).expanduser()
    m2_root = _m2_root(output, task_id)
    _ensure_task_identity(output, task_id)
    timeline_pointer, timeline_index = _load_timeline_inputs(output, task_id)
    plan_pointer, plan = _load_plan_inputs(output, task_id, timeline_index)
    selection_pointer = _load_selection_pointer(m2_root, task_id)
    selection = _load_selection(output, task_id)
    frames_pointer = _load_frames_pointer(output, task_id)

    for label, doc in (("timeline", timeline_index), ("plan", plan), ("selection", selection)):
        if doc.get("task_id") != task_id:
            raise InputIdentityMismatchError(
                f"{ERROR_INPUT_IDENTITY_MISMATCH}：{label}文档task_id与请求不一致。"
            )
    if str(timeline_index["task_id"]) != task_id or str(timeline_pointer["result_id"]) != str(
        timeline_index["timeline_id"]
    ):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：timelines current与时间轴文档身份不一致。"
        )
    if str(plan["source_timeline"]["timeline_id"]) != str(timeline_index["timeline_id"]):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：章节计划绑定的时间轴与current不符。"
        )
    source_plan = selection["source_plan"]
    if source_plan["result_id"] != str(plan_pointer["result_id"]):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：14-A绑定的计划组与boundaries current不符。"
        )
    if source_plan["manifest_sha256"] != str(plan_pointer["manifest_sha256"]):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：14-A绑定的计划manifest哈希与current不符。"
        )
    if selection["source_timeline"]["timeline_id"] != str(timeline_pointer["result_id"]):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：14-A绑定的时间轴与timelines current不符。"
        )
    source_frame_set = selection["source_frame_set"]
    if source_frame_set["frame_set_id"] != str(frames_pointer["result_id"]):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：14-A绑定的帧集与frames current不符。"
        )
    if source_frame_set["manifest_sha256"] != str(frames_pointer["manifest_sha256"]):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：14-A绑定的帧集manifest哈希与current不符。"
        )
    if str(selection_pointer["result_id"]) != _selection_result_id(selection):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：chapter-frames current与选择对象内容身份不符。"
        )
    ledgers = _build_ledgers(timeline_index, plan)
    return ContentInputs(
        task_id=task_id,
        timeline_pointer=dict(timeline_pointer),
        timeline_index=dict(timeline_index),
        plan_pointer=dict(plan_pointer),
        plan=dict(plan),
        selection_pointer=dict(selection_pointer),
        selection=dict(selection),
        frames_pointer=dict(frames_pointer),
        ledgers=ledgers,
    )


def _selection_result_id(selection: Mapping[str, Any]) -> str:
    """与14-A相同的确定性内容身份复算（前缀+规范JSON SHA-256前12位）。"""

    return "chapter-frame-selection-" + _sha256_bytes(canonical_json_bytes(selection))[:12]


# ---------------------------------------------------------------------------
# 提示词（裁决Q2.3冻结要点；零few-shot，system＋user两消息）
# ---------------------------------------------------------------------------

DOCUMENT_CONTENT_SINGLE_BATCH_SYSTEM = (
    "仅依据给定的连续主文本单元写成可连续阅读的简体中文正文。不得使用外部知识，"
    "不得新增或强化数字、实体、诊断、否定、建议、因果和结论。标题仅供理解，不要返回标题；"
    "不要机械逐单元复述。"
    "段落规则：每段文本非空；段落顺序与单元顺序一致；每段的source_unit_ids只引用直接支持本段的单元，"
    "按给定顺序升序且相邻连续；每个unit_id必须在全部段落中恰好出现一次；不得跨章引用单元；"
    "每章段落总数至多8段。"
    "只返回单个JSON对象。若输入顶层含units（单章），返回"
    '{"paragraphs":[{"source_unit_ids":["c0001-u001"],"text":"连续正文"}]}；'
    "若输入顶层含chapters（多章），返回"
    '{"chapters":[{"chapter_id":"c0002","paragraphs":[{"source_unit_ids":["c0002-u001"],"text":"连续正文"}]}]}，'
    "各章禁止混用信息。"
    "不得返回事件ID、时间、来源路径、coverage或证据索引。"
)

DOCUMENT_CONTENT_GLOBAL_SYSTEM = (
    "只根据给定的各章已验证正文生成全文级中文字段，不得补充外部知识，不得改写或返回章节。"
    '只返回单个JSON对象，字段必须严格为：{"title":"全文标题","overview":["概述第1段","可选第2段"],'
    '"conclusions":["核心结论第1段","可选第2段"]}。'
    "title非空且不超过100字符；overview必须1至2段；conclusions必须1至2段；全部文本必须非空。"
    "不得返回ID、时间、证据或Markdown。"
)

_USER_TASK_INSTRUCTION = "请仅依据以下给定输入返回完整JSON对象。"
_USER_PREFIXES = {
    CALL_SINGLE: "DOCUMENT_CONTENT_SINGLE_INPUT_JSON=",
    CALL_BATCH: "DOCUMENT_CONTENT_BATCH_INPUT_JSON=",
    CALL_GLOBAL: "DOCUMENT_CONTENT_GLOBAL_INPUT_JSON=",
}

# 纠错反馈常量（修复裁决D2.2/D2.4；实现决定24）：模块级、不进prompt模板身份，
# prompt_bundle.content_sha256与三prompt版本串不受影响。
_CONTENT_VALIDATION_FEEDBACK = (
    "保持全部unit_id覆盖与顺序要求不变，只修正未通过校验的部分，返回完整JSON对象。"
)
_TRANSPORT_FEEDBACK_INSTRUCTION = "请只返回符合规定格式的完整JSON对象。"

# 成功报告errors[]条目键集（修复裁决D3.2；14-B _REPORT_ERROR_KEYS同构）。
_REPORT_ERROR_KEYS = {"attempt", "kind", "message", "issues"}
_REPORT_ISSUE_KEYS = {"code", "detail"}


def _system_prompt(slot: str) -> str:
    if slot == CALL_GLOBAL:
        return DOCUMENT_CONTENT_GLOBAL_SYSTEM
    return DOCUMENT_CONTENT_SINGLE_BATCH_SYSTEM


def _prompt_template_identity(slot: str) -> dict[str, Any]:
    return {
        "schema_name": PROMPT_TEMPLATE_SCHEMA_NAME,
        "schema_version": PROMPT_TEMPLATE_SCHEMA_VERSION,
        "call_slot": slot,
        "prompt_version": PROMPT_VERSIONS[slot],
        "validation_id": VALIDATION_IDS[slot],
        "unit_policy_version": UNIT_POLICY_VERSION,
        "system": _system_prompt(slot),
        "user_prefix": _USER_PREFIXES[slot],
    }


def content_prompt_content_sha256(slot: str) -> str:
    """逐调用模板身份SHA-256（实现决定9）。"""

    return canonical_json_sha256(_prompt_template_identity(slot))


def content_prompt_bundle_sha256() -> str:
    """registry prompt_bundle.content_sha256期望值（实现决定9）。"""

    return canonical_json_sha256(
        [_prompt_template_identity(slot) for slot in CALL_SLOTS]
    )


def build_single_payload(ledger: ChapterLedger) -> dict[str, Any]:
    return {
        "title": ledger.title,
        "units": [
            {"unit_id": unit["unit_id"], "text": unit["text"]} for unit in ledger.units
        ],
    }


def build_batch_payload(ledgers: Sequence[ChapterLedger]) -> dict[str, Any]:
    return {
        "chapters": [
            {
                "chapter_id": ledger.chapter_id,
                "title": ledger.title,
                "units": [
                    {"unit_id": unit["unit_id"], "text": unit["text"]} for unit in ledger.units
                ],
            }
            for ledger in ledgers
        ]
    }


def build_global_payload(
    ledgers: Sequence[ChapterLedger],
    chapter_paragraphs: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    return {
        "chapters": [
            {
                "chapter_id": ledger.chapter_id,
                "title": ledger.title,
                "paragraphs": [str(item["text"]) for item in chapter_paragraphs[ledger.chapter_id]],
            }
            for ledger in ledgers
        ]
    }


def build_slot_messages(slot: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    user = f"{_USER_TASK_INSTRUCTION}\n{_USER_PREFIXES[slot]}{_compact_json(payload)}"
    return [
        {"role": "system", "content": _system_prompt(slot)},
        {"role": "user", "content": user},
    ]


def _payload_metrics(payload: Mapping[str, Any], messages: Sequence[Mapping[str, str]]) -> tuple[int, int]:
    payload_chars = len(_compact_json(payload))
    message_chars = sum(len(str(message["content"])) for message in messages)
    return payload_chars, message_chars


# ---------------------------------------------------------------------------
# 响应规范化与业务校验（裁决Q2.4的15码封闭词表）
# ---------------------------------------------------------------------------


def _issue(code: str, detail: Any = None) -> dict[str, Any]:
    if code not in BUSINESS_CODES:
        raise AssertionError(f"业务校验码越出冻结词表：{code}")
    record: dict[str, Any] = {"code": code}
    echo = _sanitize_echo(detail)
    if echo is not None:
        record["detail"] = echo
    return record


def _issue_message(issues: Sequence[Mapping[str, Any]]) -> str:
    codes = ",".join(str(item.get("code")) for item in issues)
    return f"响应未通过业务校验：[{codes}]"


def _normalize_paragraph_list(
    paragraphs: Any,
    ledger: ChapterLedger,
    issues: list[dict[str, Any]],
    *,
    strict_exact_units: bool,
) -> list[dict[str, Any]] | None:
    """校验一个章作用域的段落列表并返回规范化形；不追加致命异常，只累积issues。

    检查项（全部映射到15码封闭词表）：段落对象形态、文本非空、数量≤8、
    unit_id引用（未知/跨章/重复/覆盖/顺序）。strict_exact_units为True时按
    整章账本要求"恰好一次且按序全覆盖"。
    """

    if not isinstance(paragraphs, list) or not paragraphs:
        issues.append(_issue("PARAGRAPHS_MISSING", "paragraphs"))
        return None
    if len(paragraphs) > PARAGRAPH_RAW_MAX:
        issues.append(_issue("PARAGRAPH_COUNT_EXCEEDED", len(paragraphs)))
        return None
    unit_ids = [str(unit["unit_id"]) for unit in ledger.units]
    position = {unit_id: index for index, unit_id in enumerate(unit_ids)}
    clean: list[dict[str, Any]] = []
    referenced: list[str] = []
    fatal = False
    for item in paragraphs:
        if not isinstance(item, dict) or set(item) != _PARAGRAPH_KEYS:
            issues.append(_issue("PARAGRAPH_NOT_OBJECT", "paragraph"))
            fatal = True
            continue
        text = item["text"]
        if not isinstance(text, str) or not text.strip():
            issues.append(_issue("PARAGRAPH_TEXT_EMPTY", "text"))
            fatal = True
            continue
        source_unit_ids = item["source_unit_ids"]
        if (
            not isinstance(source_unit_ids, list)
            or not source_unit_ids
            or not all(isinstance(entry, str) and entry for entry in source_unit_ids)
        ):
            # 空引用与非字符串引用都是段落形态失败（无专属冻结码，实现决定11）
            issues.append(_issue("PARAGRAPH_NOT_OBJECT", "source_unit_ids"))
            fatal = True
            continue
        clean.append(
            {
                "source_unit_ids": list(source_unit_ids),
                "text": text.strip(),
            }
        )
        referenced.extend(source_unit_ids)
    if fatal:
        return None
    seen: set[str] = set()
    for entry in referenced:
        if entry in seen:
            issues.append(_issue("UNIT_DUPLICATE_REFERENCE", entry))
            return None
        seen.add(entry)
    cross = [
        entry
        for entry in referenced
        if entry not in position and _CHAPTER_ID_RE.fullmatch(str(entry).split("-")[0])
    ]
    unknown = [entry for entry in referenced if entry not in position]
    if strict_exact_units:
        for entry in unknown:
            chapter_part = str(entry).split("-u")[0]
            if _CHAPTER_ID_RE.fullmatch(chapter_part):
                issues.append(_issue("UNIT_CROSS_CHAPTER", entry))
            else:
                issues.append(_issue("UNIT_ID_NOT_FOUND", entry))
        if any(str(item["code"]) != "UNIT_CROSS_CHAPTER" and str(item["code"]) != "UNIT_ID_NOT_FOUND" for item in issues):
            return None
        if issues:
            return None
        if seen != set(unit_ids):
            missing = sorted(set(unit_ids) - seen)
            issues.append(_issue("UNIT_COVERAGE_INCOMPLETE", missing[0] if missing else "units"))
            return None
        if referenced != unit_ids:
            issues.append(_issue("UNIT_ORDER_VIOLATION", "units"))
            return None
        return clean
    # 非精确模式（batch逐章复用同一检查）：未知/跨章按存在域判定，随后覆盖与顺序
    for entry in unknown:
        chapter_part = str(entry).split("-u")[0]
        if _CHAPTER_ID_RE.fullmatch(chapter_part):
            issues.append(_issue("UNIT_CROSS_CHAPTER", entry))
        else:
            issues.append(_issue("UNIT_ID_NOT_FOUND", entry))
    if unknown:
        return None
    if seen != set(unit_ids):
        missing = sorted(set(unit_ids) - seen)
        issues.append(_issue("UNIT_COVERAGE_INCOMPLETE", missing[0] if missing else "units"))
        return None
    if referenced != unit_ids:
        issues.append(_issue("UNIT_ORDER_VIOLATION", "units"))
        return None
    return clean


def _coerce_single_envelope(
    response: Any, ledger: ChapterLedger
) -> tuple[Any, dict[str, Any] | None]:
    """二次失败形态裁决N1：single槽batch信封的窄口径确定性强制转换。

    当且仅当响应键集恰{"chapters"}、其值为长度1的list、唯一条目键集恰
    _BATCH_CHAPTER_KEYS、且chapter_id与single槽账本一致时，转换为
    {"paragraphs": …}；转换后仍走全部既有严格校验。返回(可能被转换的响应,
    审计记录或None)。纯函数、确定性，实时路径与raw重放路径同构。
    """
    if not isinstance(response, dict) or set(response) != _BATCH_RESPONSE_KEYS:
        return response, None
    chapters = response["chapters"]
    if not isinstance(chapters, list) or len(chapters) != 1:
        return response, None
    entry = chapters[0]
    if not isinstance(entry, dict) or set(entry) != _BATCH_CHAPTER_KEYS:
        return response, None
    if entry["chapter_id"] != ledger.chapter_id:
        return response, None
    return (
        {"paragraphs": entry["paragraphs"]},
        {
            "call_slot": CALL_SINGLE,
            "envelope": SINGLE_ENVELOPE_NORMALIZATION,
            "chapter_id": str(entry["chapter_id"]),
        },
    )


def single_envelope_normalization(
    response: Any, ledger: ChapterLedger, attempt: int
) -> dict[str, Any] | None:
    """N1审计记录推导（组复验重推导与成功路径记录共用）。"""
    _coerced, record = _coerce_single_envelope(response, ledger)
    if record is None:
        return None
    return {**record, "attempt": int(attempt)}


def normalize_single_response(
    response: Any, ledger: ChapterLedger
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(response, dict):
        return None, [_issue("RESPONSE_NOT_OBJECT", type(response).__name__)]
    response, _normalization = _coerce_single_envelope(response, ledger)
    if set(response) != _SINGLE_RESPONSE_KEYS:
        return None, [_issue("RESPONSE_KEYS_INVALID", "paragraphs")]
    issues: list[dict[str, Any]] = []
    paragraphs = _normalize_paragraph_list(
        response["paragraphs"], ledger, issues, strict_exact_units=True
    )
    if paragraphs is None:
        return None, issues
    return {"paragraphs": paragraphs}, []


def normalize_batch_response(
    response: Any, ledgers: Sequence[ChapterLedger]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(response, dict):
        return None, [_issue("RESPONSE_NOT_OBJECT", type(response).__name__)]
    if set(response) != _BATCH_RESPONSE_KEYS:
        return None, [_issue("RESPONSE_KEYS_INVALID", "chapters")]
    chapters = response["chapters"]
    if not isinstance(chapters, list) or not chapters:
        return None, [_issue("PARAGRAPHS_MISSING", "chapters")]
    expected_ids = [ledger.chapter_id for ledger in ledgers]
    ledger_by_id = {ledger.chapter_id: ledger for ledger in ledgers}
    issues: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    for item in chapters:
        if not isinstance(item, dict) or set(item) != _BATCH_CHAPTER_KEYS:
            issues.append(_issue("BATCH_CHAPTERS_MISMATCH", "chapter"))
            return None, issues
        chapter_id = item["chapter_id"]
        if not isinstance(chapter_id, str) or chapter_id not in ledger_by_id:
            issues.append(_issue("BATCH_CHAPTERS_MISMATCH", chapter_id))
            return None, issues
        paragraphs = _normalize_paragraph_list(
            item["paragraphs"], ledger_by_id[chapter_id], issues, strict_exact_units=True
        )
        if paragraphs is None:
            return None, issues
        result.append({"chapter_id": chapter_id, "paragraphs": paragraphs})
    if [item["chapter_id"] for item in result] != expected_ids:
        issues.append(_issue("BATCH_CHAPTERS_MISMATCH", "order"))
        return None, issues
    return {"chapters": result}, []


def normalize_global_response(
    response: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(response, dict):
        return None, [_issue("RESPONSE_NOT_OBJECT", type(response).__name__)]
    if set(response) != _GLOBAL_RESPONSE_KEYS:
        return None, [_issue("RESPONSE_KEYS_INVALID", "title/overview/conclusions")]
    issues: list[dict[str, Any]] = []
    title = response["title"]
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX_CHARS:
        issues.append(_issue("GLOBAL_TITLE_INVALID", "title"))
    for field, code, upper in (
        ("overview", "OVERVIEW_COUNT_INVALID", 2),
        ("conclusions", "CONCLUSIONS_COUNT_INVALID", 2),
    ):
        value = response[field]
        if not isinstance(value, list) or not 1 <= len(value) <= upper:
            issues.append(_issue(code, field))
        else:
            for entry in value:
                if not isinstance(entry, str):
                    issues.append(_issue("PARAGRAPH_NOT_OBJECT", field))
                elif not entry.strip():
                    issues.append(_issue("PARAGRAPH_TEXT_EMPTY", field))
    if issues:
        return None, issues
    return (
        {
            "title": title.strip(),
            "overview": [str(entry).strip() for entry in response["overview"]],
            "conclusions": [str(entry).strip() for entry in response["conclusions"]],
        },
        [],
    )


_SLOT_LEDGER_SCOPE: dict[str, str] = {CALL_SINGLE: "first", CALL_BATCH: "rest", CALL_GLOBAL: "global"}


def normalize_slot_response(
    slot: str, response: Any, inputs: ContentInputs
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """按槽位作用域规范化（single=首章账本；batch=其余章；global=全文）。"""

    if slot == CALL_SINGLE:
        return normalize_single_response(response, inputs.ledgers[0])
    if slot == CALL_BATCH:
        return normalize_batch_response(response, inputs.ledgers[1:])
    return normalize_global_response(response)


def slot_business_valid(
    slot: str, raw_response: Any, inputs: ContentInputs
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    return normalize_slot_response(slot, raw_response, inputs)


# ---------------------------------------------------------------------------
# 段落合并（裁决Q2.5：>4段按M1 _cap_document_paragraphs机制合并至≤4，
# 文本以单个换行连接、source_unit_ids并集保序；实现决定20）
# ---------------------------------------------------------------------------


def _merge_paragraphs_to_cap(
    paragraphs: Sequence[Mapping[str, Any]], *, max_blocks: int = PER_CHAPTER_PARAGRAPH_MAX
) -> list[dict[str, Any]]:
    normalized = [
        {
            "source_unit_ids": list(item["source_unit_ids"]),
            "text": str(item["text"]).strip(),
        }
        for item in paragraphs
    ]
    while len(normalized) > max_blocks:
        merge_at = min(
            range(len(normalized) - 1),
            key=lambda index: (
                len(normalized[index]["text"]) + len(normalized[index + 1]["text"]),
                index,
            ),
        )
        left, right = normalized[merge_at : merge_at + 2]
        merged = {
            "source_unit_ids": list(
                dict.fromkeys(left["source_unit_ids"] + right["source_unit_ids"])
            ),
            "text": f"{left['text']}\n{right['text']}",
        }
        normalized[merge_at : merge_at + 2] = [merged]
    return normalized


# ---------------------------------------------------------------------------
# 正文装配与structured-document-content-validator-3.0（裁决Q1.2/Q4）
# ---------------------------------------------------------------------------


def _project_frames(selection_chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    """14-A帧6键投影，保序逐字节（裁决Q4.2）。"""

    return [
        {key: frame[key] for key in _FRAME_PROJECTION_KEYS}
        for frame in selection_chapter["frames"]
    ]


def assemble_document_content(
    inputs: ContentInputs,
    single_normalized: Mapping[str, Any],
    batch_normalized: Mapping[str, Any] | None,
    global_normalized: Mapping[str, Any],
    *,
    mode: str,
    backend: str,
    per_call_max_tokens: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """三调用已验证输出＋四个上游绑定 → 无content_id的StructuredDocumentContent 3.0。

    纯确定性装配：帧＝14-A投影照录（禁止任何重算/改动/换序/跨章移动，裁决Q4.2）；
    block的source_event_ids＝引用单元te并集（时间轴升序去重）。
    """

    # 二次失败形态裁决N3：装配嵌入的per_call_max_tokens——新组用当前冻结常量，
    # 组复验重放传入旧组存储值，保证旧组重装配逐字节复现（含content_id）。
    effective_max_tokens = (
        dict(PER_CALL_MAX_TOKENS) if per_call_max_tokens is None else dict(per_call_max_tokens)
    )
    plan = inputs.plan
    selection = inputs.selection
    ledgers = inputs.ledgers
    chapter_paragraph_sources: dict[str, list[dict[str, Any]]] = {}
    chapter_paragraph_sources[ledgers[0].chapter_id] = list(single_normalized["paragraphs"])
    if batch_normalized is not None:
        for item in batch_normalized["chapters"]:
            chapter_paragraph_sources[str(item["chapter_id"])] = list(item["paragraphs"])

    timeline_events = inputs.timeline_index["events"]
    event_count = len(timeline_events)
    transcript_count = sum(
        1 for event in timeline_events if event["type"] == TRANSCRIPT_EVENT_TYPE
    )
    all_chapter_ids = [ledger.chapter_id for ledger in ledgers]

    chapters: list[dict[str, Any]] = []
    coverage_chapters: list[dict[str, Any]] = []
    totals_units = 0
    totals_covered_units = 0
    totals_events = 0
    for position, ledger in enumerate(ledgers):
        merged = _merge_paragraphs_to_cap(chapter_paragraph_sources[ledger.chapter_id])
        unit_by_id = {str(unit["unit_id"]): unit for unit in ledger.units}
        blocks: list[dict[str, Any]] = []
        for block_position, paragraph in enumerate(merged, start=1):
            event_ids: list[str] = []
            for unit_id in paragraph["source_unit_ids"]:
                for event_id in unit_by_id[unit_id]["source_event_ids"]:
                    if event_id not in event_ids:
                        event_ids.append(event_id)
            blocks.append(
                {
                    "block_id": f"{ledger.chapter_id}-p{block_position:03d}",
                    "type": BLOCK_TYPE,
                    "role": CHAPTER_BLOCK_ROLE,
                    "text": paragraph["text"],
                    "content_origin": BLOCK_ORIGIN,
                    "source_unit_ids": list(paragraph["source_unit_ids"]),
                    "source_event_ids": event_ids,
                    "source_chapter_ids": [ledger.chapter_id],
                }
            )
        selection_chapter = selection["chapters"][position]
        frames = _project_frames(selection_chapter)
        chapters.append(
            {
                "chapter_id": ledger.chapter_id,
                "index": ledger.chapter_index,
                "start_ms": ledger.start_ms,
                "end_ms": ledger.end_ms,
                "title": ledger.title,
                "blocks": blocks,
                "frames": frames,
                "units_referenced": len(ledger.units),
            }
        )
        covered_units = sum(len(block["source_unit_ids"]) for block in blocks)
        coverage_chapters.append(
            {
                "chapter_id": ledger.chapter_id,
                "unit_count": len(ledger.units),
                "covered_unit_count": covered_units,
                "event_count": len(ledger.events),
                "covered_event_count": len(ledger.events),
            }
        )
        totals_units += len(ledger.units)
        totals_covered_units += covered_units
        totals_events += len(ledger.events)

    overview_blocks = [
        {
            "block_id": f"overview-p{position:03d}",
            "type": BLOCK_TYPE,
            "role": OVERVIEW_BLOCK_ROLE,
            "text": text,
            "content_origin": BLOCK_ORIGIN,
            "source_unit_ids": [],
            "source_event_ids": [],
            "source_chapter_ids": list(all_chapter_ids),
        }
        for position, text in enumerate(global_normalized["overview"], start=1)
    ]
    conclusion_blocks = [
        {
            "block_id": f"conclusion-p{position:03d}",
            "type": BLOCK_TYPE,
            "role": CONCLUSION_BLOCK_ROLE,
            "text": text,
            "content_origin": BLOCK_ORIGIN,
            "source_unit_ids": [],
            "source_event_ids": [],
            "source_chapter_ids": list(all_chapter_ids),
        }
        for position, text in enumerate(global_normalized["conclusions"], start=1)
    ]

    batch_skipped = len(ledgers) == 1
    source_plan = selection["source_plan"]
    source_timeline_doc = plan["source_timeline"]

    content: dict[str, Any] = {
        "schema_name": CONTENT_SCHEMA_NAME,
        "schema_version": CONTENT_SCHEMA_VERSION,
        "task_id": inputs.task_id,
        "language": "zh-CN",
        "source_language": str(inputs.timeline_index["language"]["code"]),
        "title": global_normalized["title"],
        "strategy": "llm",
        "overview": overview_blocks,
        "overall_conclusion": conclusion_blocks,
        "chapters": chapters,
        "coverage": {
            "policy_version": COVERAGE_POLICY_VERSION,
            "chapters": coverage_chapters,
            "totals": {
                "unit_count": totals_units,
                "covered_unit_count": totals_covered_units,
                "event_count": totals_events,
                "covered_event_count": totals_events,
                "full_coverage": totals_units == totals_covered_units,
            },
        },
        "generation": {
            "status": "success",
            "mode": mode,
            "backend": backend,
            "model": FROZEN_MODEL,
            "temperature": 0.0,
            "enable_thinking": False,
            "prompt_versions": {
                "single": PROMPT_VERSIONS[CALL_SINGLE],
                "batch": None if batch_skipped else PROMPT_VERSIONS[CALL_BATCH],
                "global": PROMPT_VERSIONS[CALL_GLOBAL],
            },
            "content_policy_version": CONTENT_POLICY_VERSION,
            "unit_policy_version": UNIT_POLICY_VERSION,
            "validator_versions": {
                "single": VALIDATION_IDS[CALL_SINGLE],
                "batch": None if batch_skipped else VALIDATION_IDS[CALL_BATCH],
                "global": VALIDATION_IDS[CALL_GLOBAL],
            },
            "validator_version": CONTENT_VALIDATOR_VERSION,
            "per_call_max_tokens": effective_max_tokens,
        },
        "content_versions": {
            "schema": CONTENT_SCHEMA_VERSION,
            "content_policy": CONTENT_POLICY_VERSION,
            "unit_policy": UNIT_POLICY_VERSION,
            "block_policy": BLOCK_POLICY_VERSION,
            "validator": CONTENT_VALIDATOR_VERSION,
        },
        "source_plan": {
            "result_id": str(source_plan["result_id"]),
            "manifest_sha256": str(source_plan["manifest_sha256"]),
            "result_source": str(source_plan["result_source"]),
            "quality_level": str(source_plan["quality_level"]),
            "boundary_count": int(source_plan["boundary_count"]),
            "strategy": str(plan["strategy"]),
        },
        "source_timeline": {
            "timeline_id": str(source_timeline_doc["timeline_id"]),
            "sha256": str(source_timeline_doc["sha256"]),
            "manifest_sha256": str(inputs.timeline_pointer["manifest_sha256"]),
            "event_count": event_count,
            "transcript_event_count": transcript_count,
        },
        "source_chapter_frame_selection": {
            "result_id": str(inputs.selection_pointer["result_id"]),
            "manifest_sha256": str(inputs.selection_pointer["manifest_sha256"]),
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "caption_template_version": CAPTION_TEMPLATE_VERSION,
        },
        "source_frame_set": {
            "frame_set_id": str(selection["source_frame_set"]["frame_set_id"]),
            "manifest_sha256": str(selection["source_frame_set"]["manifest_sha256"]),
            "frame_count": int(selection["source_frame_set"]["frame_count"]),
        },
    }
    content["content_id"] = _compute_content_id(content)
    validate_structured_document_content_v3(
        content,
        plan=plan,
        timeline_index=inputs.timeline_index,
        selection=selection,
        expected_task_id=inputs.task_id,
    )
    return content


def _compute_content_id(payload: Mapping[str, Any]) -> str:
    """content_id=result_id=document-content-＋（剔除content_id后规范JSON）SHA-256前12位。"""

    body = {key: value for key, value in payload.items() if key != "content_id"}
    return RESULT_ID_PREFIX + canonical_sha256(body)[:12]


# ---------------------------------------------------------------------------
# structured-document-content-validator-3.0（composer自持，裁决Q1.1/Q4.4）
# ---------------------------------------------------------------------------


def _vfail(message: str) -> None:
    raise StructuredContentValidationError(message)


def validate_structured_document_content_v3(
    content: Any,
    *,
    plan: Mapping[str, Any],
    timeline_index: Mapping[str, Any],
    selection: Mapping[str, Any],
    expected_task_id: str | None = None,
) -> None:
    """全量复验：精确键集、ID形态、账本重推导、帧投影、coverage重算、
    content_id重算、四源绑定与14-A事件级交叉核对（裁决Q1/Q4全部结构语义）。"""

    if not isinstance(content, dict):
        _vfail("正文必须是JSON对象。")
    _keys = set(content)
    if _keys != _CONTENT_TOP_KEYS:
        _vfail("StructuredDocumentContent 3.0顶层字段集合无效。")
    if content["schema_name"] != CONTENT_SCHEMA_NAME or content["schema_version"] != CONTENT_SCHEMA_VERSION:
        _vfail("正文Schema身份无效。")
    task_id = content["task_id"]
    if not isinstance(task_id, str) or not task_id.strip():
        _vfail("task_id必须是非空字符串。")
    if expected_task_id is not None and task_id != expected_task_id:
        _vfail("正文task_id与请求不一致。")
    if content["language"] != "zh-CN":
        _vfail("language必须固定为zh-CN。")
    source_language = timeline_index.get("language")
    if not isinstance(source_language, dict) or content["source_language"] != source_language.get("code"):
        _vfail("source_language与时间轴语言不符。")
    title = content["title"]
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX_CHARS:
        _vfail("title必须strip非空且不超过100字符。")
    if title != title.strip():
        _vfail("title必须已strip。")
    if content["strategy"] != "llm":
        _vfail("strategy必须为llm。")

    # 账本重推导（与装配路径同一纯函数）
    ledgers = _build_ledgers(timeline_index, plan)
    chapter_count = len(ledgers)
    chapters = content["chapters"]
    if not isinstance(chapters, list) or len(chapters) != chapter_count:
        _vfail("章数与章节计划不符。")
    boundaries = plan["boundaries"]
    for position, ledger in enumerate(ledgers):
        chapter = chapters[position]
        if not isinstance(chapter, dict) or set(chapter) != _CHAPTER_KEYS:
            _vfail(f"第{position + 1}章字段集合无效。")
        if chapter["chapter_id"] != ledger.chapter_id or chapter["index"] != ledger.chapter_index:
            _vfail(f"第{position + 1}章编号与计划位置不一致。")
        if chapter["start_ms"] != ledger.start_ms or chapter["end_ms"] != ledger.end_ms:
            _vfail(f"章{ledger.chapter_id}区间与计划边界重推导不一致。")
        if chapter["title"] != ledger.title or chapter["title"] != str(boundaries[position]["title"]).strip():
            _vfail(f"章{ledger.chapter_id}标题与计划不符。")
        if not _is_int(chapter["units_referenced"]) or chapter["units_referenced"] != len(ledger.units):
            _vfail(f"章{ledger.chapter_id} units_referenced与账本重推导不符。")
        blocks = chapter["blocks"]
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= PER_CHAPTER_PARAGRAPH_MAX:
            _vfail(f"章{ledger.chapter_id}正文块数必须在1..4。")
        unit_by_id = {str(unit["unit_id"]): unit for unit in ledger.units}
        unit_ids = [str(unit["unit_id"]) for unit in ledger.units]
        flattened_units: list[str] = []
        for block_position, block in enumerate(blocks, start=1):
            _validate_block(
                block,
                expected_block_id=f"{ledger.chapter_id}-p{block_position:03d}",
                expected_role=CHAPTER_BLOCK_ROLE,
                expected_chapter_ids=[ledger.chapter_id],
            )
            for unit_id in block["source_unit_ids"]:
                if unit_id not in unit_by_id:
                    _vfail(f"章{ledger.chapter_id}块引用未知单元{unit_id}。")
                if unit_id in flattened_units:
                    _vfail(f"章{ledger.chapter_id}单元{unit_id}被重复覆盖。")
                flattened_units.append(unit_id)
            if block["source_event_ids"] != _sorted_timeline_events(timeline_index, chapter_events_of(block, unit_by_id)):
                _vfail(f"章{ledger.chapter_id}块source_event_ids与引用单元te并集（时间轴升序去重）不符。")
        if flattened_units != unit_ids:
            _vfail(f"章{ledger.chapter_id}单元覆盖必须恰一次且按账本顺序。")
        frames = chapter["frames"]
        expected_frames = _project_frames(selection["chapters"][position])
        if frames != expected_frames:
            _vfail(f"章{ledger.chapter_id}帧必须逐字节等于14-A投影（禁止重算/改动/换序/跨章移动）。")
        seen_frame_ids: set[str] = set()
        seen_ranks: list[int] = []
        for frame in frames:
            if set(frame) != _FRAME_PROJECTION_KEY_SET:
                _vfail(f"章{ledger.chapter_id}帧投影字段集合无效。")
            frame_id = frame["frame_id"]
            if frame_id in seen_frame_ids:
                _vfail(f"章{ledger.chapter_id}帧ID在章内重复。")
            seen_frame_ids.add(str(frame_id))
            if not _is_int(frame["rank"]) or not _is_int(frame["tier"]) or frame["tier"] not in TIER_TO_FORM:
                _vfail(f"章{ledger.chapter_id}帧rank/tier形态无效。")
            if TIER_TO_FORM[frame["tier"]] != frame["caption_form"]:
                _vfail(f"章{ledger.chapter_id}帧tier与caption_form冻结映射不符。")
            if not _is_int(frame["decoded_ms"]):
                _vfail(f"章{ledger.chapter_id}帧decoded_ms必须为整数毫秒。")
            if not ledger.start_ms <= frame["decoded_ms"] < ledger.end_ms:
                _vfail(f"章{ledger.chapter_id}帧{frame_id}的decoded_ms不在章区间内。")
            if not isinstance(frame["caption_text"], str) or not frame["caption_text"]:
                _vfail(f"章{ledger.chapter_id}帧{frame_id}说明缺失。")
            seen_ranks.append(frame["rank"])
        if sorted(seen_ranks) != list(range(1, len(frames) + 1)):
            _vfail(f"章{ledger.chapter_id}帧rank必须是1..n排列。")

    _validate_global_blocks(content["overview"], "overview", OVERVIEW_BLOCK_ROLE, [ledger.chapter_id for ledger in ledgers])
    _validate_global_blocks(
        content["overall_conclusion"], "conclusion", CONCLUSION_BLOCK_ROLE, [ledger.chapter_id for ledger in ledgers]
    )

    coverage = content["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != _COVERAGE_KEYS:
        _vfail("coverage字段集合无效。")
    if coverage["policy_version"] != COVERAGE_POLICY_VERSION:
        _vfail("coverage.policy_version无效。")
    coverage_chapters = coverage["chapters"]
    if not isinstance(coverage_chapters, list) or len(coverage_chapters) != chapter_count:
        _vfail("coverage章节记录与章数不符。")
    totals_units = 0
    totals_events = 0
    for position, ledger in enumerate(ledgers):
        record = coverage_chapters[position]
        if not isinstance(record, dict) or set(record) != _COVERAGE_CHAPTER_KEYS:
            _vfail("coverage章节记录字段集合无效。")
        if record["chapter_id"] != ledger.chapter_id:
            _vfail("coverage章节记录顺序与章不符。")
        if record["unit_count"] != len(ledger.units) or record["covered_unit_count"] != len(ledger.units):
            _vfail(f"章{ledger.chapter_id} coverage单元计数与账本不符。")
        if record["event_count"] != len(ledger.events) or record["covered_event_count"] != len(ledger.events):
            _vfail(f"章{ledger.chapter_id} coverage事件计数与账本不符。")
        totals_units += len(ledger.units)
        totals_events += len(ledger.events)
    totals = coverage["totals"]
    if not isinstance(totals, dict) or set(totals) != _COVERAGE_TOTALS_KEYS:
        _vfail("coverage.totals字段集合无效。")
    if totals["unit_count"] != totals_units or totals["covered_unit_count"] != totals_units:
        _vfail("coverage.totals单元计数不符。")
    if totals["event_count"] != totals_events or totals["covered_event_count"] != totals_events:
        _vfail("coverage.totals事件计数不符。")
    if totals["full_coverage"] is not True:
        _vfail("coverage.totals.full_coverage必须为true（裁决Q1.2硬约束）。")

    generation = content["generation"]
    if not isinstance(generation, dict) or set(generation) != _GENERATION_KEYS:
        _vfail("generation字段集合无效。")
    if generation["status"] != "success":
        _vfail("generation.status必须为success。")
    if generation["mode"] not in (MODE_REAL, MODE_MOCK):
        _vfail("generation.mode无效。")
    if not isinstance(generation["backend"], str) or not generation["backend"]:
        _vfail("generation.backend必须是非空字符串。")
    if generation["model"] != FROZEN_MODEL:
        _vfail("generation.model必须为qwen3.7-flash。")
    if generation["temperature"] != 0.0 or generation["enable_thinking"] is not False:
        _vfail("generation采样身份与冻结角色不符。")
    batch_skipped = chapter_count == 1
    for map_name, expected_map, allowed_null in (
        ("prompt_versions", PROMPT_VERSIONS, True),
        ("validator_versions", VALIDATION_IDS, True),
    ):
        value = generation[map_name]
        if not isinstance(value, dict) or set(value) != _GENERATION_VERSION_MAP_KEYS:
            _vfail(f"generation.{map_name}字段集合无效。")
        if value["single"] != expected_map[CALL_SINGLE] or value["global"] != expected_map[CALL_GLOBAL]:
            _vfail(f"generation.{map_name}与冻结版本不符。")
        if allowed_null:
            if batch_skipped:
                if value["batch"] is not None:
                    _vfail(f"batch跳过时generation.{map_name}.batch必须为null。")
            elif value["batch"] != expected_map[CALL_BATCH]:
                _vfail(f"generation.{map_name}.batch与冻结版本不符。")
    if generation["content_policy_version"] != CONTENT_POLICY_VERSION:
        _vfail("generation.content_policy_version无效。")
    if generation["unit_policy_version"] != UNIT_POLICY_VERSION:
        _vfail("generation.unit_policy_version无效。")
    if generation["validator_version"] != CONTENT_VALIDATOR_VERSION:
        _vfail("generation.validator_version无效。")
    # 二次失败形态裁决N3.1：per_call_max_tokens必须恰为某冻结版本的映射
    # （反查即组版本判别；旧组{1800,3600,1600}→1.0保持合法）。
    if composer_version_for_tokens(generation["per_call_max_tokens"]) is None:
        _vfail("generation.per_call_max_tokens不是任何冻结版本的调用级上限映射。")

    versions = content["content_versions"]
    if not isinstance(versions, dict) or set(versions) != _CONTENT_VERSIONS_KEYS:
        _vfail("content_versions字段集合无效。")
    if versions != {
        "schema": CONTENT_SCHEMA_VERSION,
        "content_policy": CONTENT_POLICY_VERSION,
        "unit_policy": UNIT_POLICY_VERSION,
        "block_policy": BLOCK_POLICY_VERSION,
        "validator": CONTENT_VALIDATOR_VERSION,
    }:
        _vfail("content_versions与冻结版本族不符。")

    source_plan = content["source_plan"]
    if not isinstance(source_plan, dict) or set(source_plan) != _SOURCE_PLAN_KEYS:
        _vfail("source_plan字段集合无效。")
    plan_source = plan["parameters"]["result_source"]
    plan_quality = plan["parameters"]["quality_level"]
    legal_pairs = {("primary", "normal"), ("emergency", "degraded")}
    if (source_plan["result_source"], source_plan["quality_level"]) not in legal_pairs:
        _vfail("source_plan的(result_source,quality_level)不在合法对。")
    if source_plan["result_source"] != plan_source or source_plan["quality_level"] != plan_quality:
        _vfail("source_plan与所消费计划的parameters不符。")
    if source_plan["boundary_count"] != len(boundaries):
        _vfail("source_plan.boundary_count与计划边界数不符。")
    if source_plan["strategy"] != plan["strategy"] or source_plan["strategy"] != "llm":
        _vfail("source_plan.strategy无效。")
    if not _sha256_hex(source_plan["manifest_sha256"]) or not isinstance(source_plan["result_id"], str):
        _vfail("source_plan组身份形态无效。")
    selection_source_plan = selection["source_plan"]
    if (
        source_plan["result_id"] != selection_source_plan["result_id"]
        or source_plan["manifest_sha256"] != selection_source_plan["manifest_sha256"]
        or source_plan["result_source"] != selection_source_plan["result_source"]
        or source_plan["quality_level"] != selection_source_plan["quality_level"]
        or source_plan["boundary_count"] != selection_source_plan["boundary_count"]
    ):
        _vfail("source_plan与14-A选择对象绑定的计划身份不符。")

    source_timeline = content["source_timeline"]
    if not isinstance(source_timeline, dict) or set(source_timeline) != _SOURCE_TIMELINE_KEYS:
        _vfail("source_timeline字段集合无效。")
    if source_timeline["timeline_id"] != timeline_index["timeline_id"]:
        _vfail("source_timeline.timeline_id与时间轴不符。")
    if source_timeline["sha256"] != plan["source_timeline"]["sha256"]:
        _vfail("source_timeline.sha256与计划绑定不符。")
    events_all = timeline_index["events"]
    if source_timeline["event_count"] != len(events_all):
        _vfail("source_timeline.event_count与时间轴不符。")
    if source_timeline["transcript_event_count"] != sum(
        1 for event in events_all if event["type"] == TRANSCRIPT_EVENT_TYPE
    ):
        _vfail("source_timeline.transcript_event_count与时间轴不符。")
    if not _sha256_hex(source_timeline["manifest_sha256"]):
        _vfail("source_timeline.manifest_sha256形态无效。")
    if timeline_index["timeline_id"] != selection["source_timeline"]["timeline_id"]:
        _vfail("时间轴与14-A绑定不符。")

    source_selection = content["source_chapter_frame_selection"]
    if not isinstance(source_selection, dict) or set(source_selection) != _SOURCE_SELECTION_KEYS:
        _vfail("source_chapter_frame_selection字段集合无效。")
    if source_selection["selection_policy_version"] != SELECTION_POLICY_VERSION:
        _vfail("选择策略版本无效。")
    if source_selection["caption_template_version"] != CAPTION_TEMPLATE_VERSION:
        _vfail("说明模板版本无效。")
    if source_selection["result_id"] != _selection_result_id(selection):
        _vfail("source_chapter_frame_selection.result_id与选择对象内容身份不符。")
    if not _sha256_hex(source_selection["manifest_sha256"]):
        _vfail("选择组manifest哈希形态无效。")

    source_frames = content["source_frame_set"]
    if not isinstance(source_frames, dict) or set(source_frames) != _SOURCE_FRAME_SET_KEYS:
        _vfail("source_frame_set字段集合无效。")
    if source_frames != {
        "frame_set_id": selection["source_frame_set"]["frame_set_id"],
        "manifest_sha256": selection["source_frame_set"]["manifest_sha256"],
        "frame_count": selection["source_frame_set"]["frame_count"],
    }:
        _vfail("source_frame_set与14-A绑定不符。")

    content_id = content["content_id"]
    if (
        not isinstance(content_id, str)
        or len(content_id) != len(RESULT_ID_PREFIX) + 12
        or not content_id.startswith(RESULT_ID_PREFIX)
        or _ID_RE.fullmatch(content_id[len(RESULT_ID_PREFIX):]) is None
    ):
        _vfail("content_id必须形如document-content-＋12位十六进制。")
    if content_id != _compute_content_id(content):
        _vfail("content_id与剔除content_id后规范JSON的SHA-256前12位不一致。")


def _sorted_timeline_events(timeline_index: Mapping[str, Any], event_ids: Sequence[str]) -> list[str]:
    order = {
        str(event["id"]): index
        for index, event in enumerate(timeline_index["events"])
        if event["type"] == TRANSCRIPT_EVENT_TYPE
    }
    return sorted({str(item) for item in event_ids}, key=lambda value: order.get(value, 1 << 40))


def chapter_events_of(block: Mapping[str, Any], unit_by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    events: list[str] = []
    for unit_id in block["source_unit_ids"]:
        for event_id in unit_by_id[unit_id]["source_event_ids"]:
            if event_id not in events:
                events.append(event_id)
    return events


def _validate_block(
    block: Any,
    *,
    expected_block_id: str,
    expected_role: str,
    expected_chapter_ids: Sequence[str],
) -> None:
    if not isinstance(block, dict) or set(block) != _BLOCK_KEYS:
        _vfail(f"块{expected_block_id}字段集合无效。")
    if block["block_id"] != expected_block_id or _BLOCK_ID_RE.fullmatch(str(block["block_id"])) is None:
        _vfail(f"块ID必须形如{expected_block_id}且连续编号。")
    if block["type"] != BLOCK_TYPE:
        _vfail(f"块{expected_block_id} type必须为paragraph。")
    if block["role"] != expected_role or block["role"] not in BLOCK_ROLES:
        _vfail(f"块{expected_block_id} role无效。")
    text = block["text"]
    if not isinstance(text, str) or not text.strip():
        _vfail(f"块{expected_block_id} text必须strip非空。")
    if block["content_origin"] != BLOCK_ORIGIN:
        _vfail(f"块{expected_block_id} content_origin无效。")
    for field in ("source_unit_ids", "source_event_ids", "source_chapter_ids"):
        value = block[field]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            _vfail(f"块{expected_block_id} {field}必须是非空字符串数组。")
        if len(set(value)) != len(value):
            _vfail(f"块{expected_block_id} {field}存在重复项。")
    if block["source_chapter_ids"] != list(expected_chapter_ids):
        _vfail(f"块{expected_block_id} source_chapter_ids无效。")
    for event_id in block["source_event_ids"]:
        if _TE_EVENT_RE.fullmatch(event_id) is None:
            _vfail(f"块{expected_block_id}证据事件必须全部是teNNNNNN（pe/oe/M1 aNNNN一律禁止，裁决Q1.3）。")


def _validate_global_blocks(
    blocks: Any, id_stem: str, expected_role: str, all_chapter_ids: Sequence[str]
) -> None:
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 2:
        _vfail(f"{_global_stem_hint(id_stem)}必须为1..2块。")
        return
    for position, block in enumerate(blocks, start=1):
        _validate_block(
            block,
            expected_block_id=f"{id_stem}-p{position:03d}",
            expected_role=expected_role,
            expected_chapter_ids=all_chapter_ids,
        )
        if block["source_unit_ids"] or block["source_event_ids"]:
            _vfail(f"{id_stem}块不得引用单元或事件。")


def _global_stem_hint(stem: str) -> str:
    return "overview（概述）" if stem == "overview" else "overall_conclusion（结论）"


# ---------------------------------------------------------------------------
# 缓存身份（裁决Q4.1：DocumentComposerV2CacheIdentity恰20键）
# ---------------------------------------------------------------------------


def _content_cache_identity(
    inputs: ContentInputs, content_role_identity_sha256: str
) -> tuple[dict[str, Any], str]:
    identity = {
        "schema_name": CACHE_SCHEMA_NAME,
        "schema_version": CACHE_SCHEMA_VERSION,
        "task_id": inputs.task_id,
        "composer_version": COMPOSER_VERSION,
        "content_schema": CONTENT_SCHEMA_VERSION,
        "content_policy_version": CONTENT_POLICY_VERSION,
        "unit_policy_version": UNIT_POLICY_VERSION,
        "prompt_versions": dict(PROMPT_VERSIONS),
        "validation_ids": dict(VALIDATION_IDS),
        "per_call_max_tokens": dict(PER_CALL_MAX_TOKENS),
        "paragraph_merge_max": PER_CHAPTER_PARAGRAPH_MAX,
        "plan_result_id": str(inputs.plan_pointer["result_id"]),
        "plan_manifest_sha256": str(inputs.plan_pointer["manifest_sha256"]),
        "timeline_id": str(inputs.timeline_pointer["result_id"]),
        "timeline_manifest_sha256": str(inputs.timeline_pointer["manifest_sha256"]),
        "chapter_frame_selection_result_id": str(inputs.selection_pointer["result_id"]),
        "chapter_frame_selection_manifest_sha256": str(inputs.selection_pointer["manifest_sha256"]),
        "frame_set_id": str(inputs.frames_pointer["result_id"]),
        "frame_set_manifest_sha256": str(inputs.frames_pointer["manifest_sha256"]),
        "content_role_identity_sha256": content_role_identity_sha256,
    }
    if set(identity) != _CACHE_IDENTITY_KEYS:
        raise AssertionError("缓存身份键集偏离冻结20键。")
    return identity, canonical_json_sha256(identity)


# ---------------------------------------------------------------------------
# content_model角色解析与冻结值核对（裁决Q6.3；实现决定8/9）
# ---------------------------------------------------------------------------


def _role_config_identity_sha256(role: LlmRoleConfig) -> str:
    body = dataclasses.asdict(role)
    body.pop("role_cache_identity_sha256", None)
    return canonical_json_sha256(body)


def _enforce_frozen_role(role: LlmRoleConfig) -> None:
    """逐字段核对裁决Q6.3冻结角色；任一不符ROLE_CONFIG_INVALID。"""

    def bad(field: str, actual: Any) -> None:
        raise RoleConfigInvalidError(
            f"{ERROR_ROLE_CONFIG_INVALID}：content_model角色字段{field}={actual!r}偏离裁决Q6.3冻结值。"
        )

    if role.name != ROLE_NAME:
        bad("name", role.name)
    if role.role_config_version != ROLE_CONFIG_VERSION:
        bad("role_config_version", role.role_config_version)
    if role.enabled is not True:
        bad("enabled", role.enabled)
    if role.revision_policy != "provider_model_name":
        bad("revision_policy", role.revision_policy)
    if role.model != FROZEN_MODEL:
        bad("model", role.model)
    profile = role.connection_profile
    if profile is None or profile.name != "bailian_default" or profile.backend != "openai_compatible":
        bad("connection_profile", profile)
    assert profile is not None
    if profile.api_key_env != "VIDEO_DOC_LLM_API_KEY":
        bad("connection_profile.api_key_env", profile.api_key_env)
    if role.resolved_provider != {"backend": "openai_compatible", "base_url": profile.base_url}:
        bad("resolved_provider", role.resolved_provider)
    generation = role.generation
    if (
        generation is None
        or generation.temperature != 0.0
        or generation.enable_thinking is not False
        or generation.max_tokens is not None
        or generation.timeout_seconds != 60.0
    ):
        bad("generation", generation)
    if role.input_contract != INPUT_CONTRACT:
        bad("input_contract", role.input_contract)
    if role.output_contract != OUTPUT_CONTRACT:
        bad("output_contract", role.output_contract)
    bundle = role.prompt_bundle
    if bundle is None or bundle.fixture_hash_only is not False:
        bad("prompt_bundle", bundle)
    assert bundle is not None
    if list(bundle.versions) != [PROMPT_VERSIONS[slot] for slot in CALL_SLOTS]:
        bad("prompt_bundle.versions", bundle.versions)
    if bundle.content_sha256 != content_prompt_bundle_sha256():
        bad("prompt_bundle.content_sha256", bundle.content_sha256)
    if role.response_format != FROZEN_RESPONSE_FORMAT:
        bad("response_format", role.response_format)
    if role.validation_id != VALIDATION_SUITE_ID:
        bad("validation_id", role.validation_id)
    if role.validation_retry_policy != VALIDATION_RETRY_POLICY:
        bad("validation_retry_policy", role.validation_retry_policy)
    if role.cache_namespace != CACHE_NAMESPACE:
        bad("cache_namespace", role.cache_namespace)
    if role.max_attempts_per_business_call != FROZEN_MAX_ATTEMPTS:
        bad("max_attempts_per_business_call", role.max_attempts_per_business_call)
    if role.alternate_model_fallback != FROZEN_ALTERNATE_FALLBACK:
        bad("alternate_model_fallback", role.alternate_model_fallback)
    if role.runtime_budget != {
        "business_call_hard_limit": FROZEN_BUSINESS_BUDGET,
        "supplier_request_hard_limit": FROZEN_SUPPLIER_BUDGET,
    }:
        bad("runtime_budget", role.runtime_budget)


def _resolve_content_role(
    project_root: Path, explicit_registry_path: str | Path | None
) -> tuple[LlmRoleConfig, RegistrySnapshot]:
    try:
        snapshot = resolve_model_registry(project_root, explicit_registry_path)
    except ModelRoleRegistryError as exc:
        raise RoleConfigInvalidError(
            f"{ERROR_ROLE_CONFIG_INVALID}：模型角色registry校验失败：{_safe_error_text(exc, '')}"
        ) from exc
    if snapshot.fixture_only:
        raise RoleConfigInvalidError(
            f"{ERROR_ROLE_CONFIG_INVALID}：fixture_only=true的registry不得用于正式组合。"
        )
    role = snapshot.lookup(ROLE_NAME)
    if role is None:
        raise RoleConfigMissingError(
            f"{ERROR_ROLE_CONFIG_MISSING}：registry（{snapshot.source}）不含content_model角色。"
        )
    if role.enabled is not True:
        raise RoleConfigMissingError(
            f"{ERROR_ROLE_CONFIG_MISSING}：content_model角色未启用（source={snapshot.source}）。"
        )
    _enforce_frozen_role(role)
    assert role.prompt_bundle is not None and role.generation is not None
    if role.prompt_bundle.fixture_hash_only is not False:
        raise RoleConfigInvalidError(
            f"{ERROR_ROLE_CONFIG_INVALID}：content_model的prompt_bundle.fixture_hash_only必须为false。"
        )
    return role, snapshot


def _slot_role_for_call(role: LlmRoleConfig, slot: str) -> LlmRoleConfig:
    """实现决定9：逐调用身份用max_tokens=槽位冻结值的角色副本。"""

    assert role.generation is not None
    return dataclasses.replace(
        role, generation=dataclasses.replace(role.generation, max_tokens=PER_CALL_MAX_TOKENS[slot])
    )


def _slot_input_canonical_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json_bytes(payload))


# ---------------------------------------------------------------------------
# 执行报告与原始响应文档（裁决Q6.11/Q2.6；实现决定4/12）
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report_versions(content: Mapping[str, Any]) -> dict[str, Any]:
    """报告versions子对象＝generation版本镜像（实现决定4，列入G1增量清单）。"""

    generation = content["generation"]
    # 二次失败形态裁决N3：composer_version由per_call_max_tokens冻结反查导出，
    # 旧组1.0报告重建恒等（generation键集冻结12键、不含composer_version字段）。
    derived_version = composer_version_for_tokens(generation["per_call_max_tokens"])
    if derived_version is None:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：generation.per_call_max_tokens不是任何冻结版本的调用级上限映射。"
        )
    return {
        "composer_version": derived_version,
        "content_schema": content["schema_version"],
        "content_policy_version": generation["content_policy_version"],
        "unit_policy_version": generation["unit_policy_version"],
        "block_policy_version": content["content_versions"]["block_policy"],
        "coverage_policy_version": content["coverage"]["policy_version"],
        "validator_version": generation["validator_version"],
        "prompt_versions": dict(generation["prompt_versions"]),
        "validator_versions": dict(generation["validator_versions"]),
        "per_call_max_tokens": dict(generation["per_call_max_tokens"]),
    }


def _build_execution_report(
    inputs: ContentInputs,
    content: Mapping[str, Any],
    *,
    business_call_count: int,
    supplier_request_count: int,
    cache_hit_count: int,
    errors: Sequence[Mapping[str, Any]] = (),
    response_normalizations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    # 修复裁决D3.2：errors[]如实记录被成功纠正的第1次失败（14-B透明性先例）。
    # 二次失败形态裁决N1：response_normalizations如实记录被发布响应上的信封转换。
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "success",
        "task_id": inputs.task_id,
        "content_id": content["content_id"],
        "plan_result_source": content["source_plan"]["result_source"],
        "plan_quality_level": content["source_plan"]["quality_level"],
        "chapter_count": len(content["chapters"]),
        "business_call_count": int(business_call_count),
        "supplier_request_count": int(supplier_request_count),
        "cache_hit_count": int(cache_hit_count),
        "errors": [dict(error) for error in errors],
        "response_normalizations": [dict(item) for item in response_normalizations],
        "versions": _report_versions(content),
    }


def _validate_execution_report(
    report: Any,
    *,
    content: Mapping[str, Any],
    task_id: str,
) -> tuple[int, int, int]:
    """组复验用：按composer版本恰12/13键、成功语义与正文一致，
    返回(业务次数,供应商次数,缓存命中次数)。二次失败形态裁决N3.4。"""

    generation = content["generation"]
    composer_version = composer_version_for_tokens(generation["per_call_max_tokens"])
    if composer_version is None:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：正文per_call_max_tokens不在冻结版本映射集内。"
        )
    expected_keys = _REPORT_KEYS_BY_COMPOSER[composer_version]
    if not isinstance(report, dict) or set(report) != expected_keys:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：execution-report.json字段集合偏离"
            f"composer {composer_version}冻结键集。"
        )
    if composer_version in ("1.1", "1.2"):
        normalizations = report["response_normalizations"]
        if not isinstance(normalizations, list):
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：response_normalizations必须是数组。"
            )
        for item in normalizations:
            if not isinstance(item, dict) or set(item) != _NORMALIZATION_RECORD_KEYS:
                raise ContentCacheCorruptError(
                    f"{ERROR_CACHE_CORRUPT}：response_normalizations条目键集无效。"
                )
            if (
                item["call_slot"] != CALL_SINGLE
                or item["envelope"] != SINGLE_ENVELOPE_NORMALIZATION
                or not isinstance(item["chapter_id"], str)
                or not item["chapter_id"]
                or not _is_int(item["attempt"])
                or not 1 <= item["attempt"] <= FROZEN_MAX_ATTEMPTS
            ):
                raise ContentCacheCorruptError(
                    f"{ERROR_CACHE_CORRUPT}：response_normalizations条目值无效。"
                )
    if (
        report["schema_version"] != REPORT_SCHEMA_VERSION
        or report["status"] != "success"
        or report["task_id"] != task_id
        or report["content_id"] != content["content_id"]
        or report["plan_result_source"] != content["source_plan"]["result_source"]
        or report["plan_quality_level"] != content["source_plan"]["quality_level"]
        or report["chapter_count"] != len(content["chapters"])
        or report["versions"] != _report_versions(content)
    ):
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：execution-report.json与正文事实不一致。"
        )
    counts = (
        report["business_call_count"],
        report["supplier_request_count"],
        report["cache_hit_count"],
    )
    if not all(_is_int(value) and value >= 0 for value in counts):
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：execution-report.json调用计数必须为非负整数。"
        )
    business, supplier, hits = counts
    expected_calls = 2 if len(content["chapters"]) == 1 else 3
    if composer_version == "1.2":
        expected_calls = 1 + sum((chapter["unit_count"] + 5) // 6 for chapter in content["coverage"]["chapters"])
        if supplier > 12:
            raise ContentCacheCorruptError("CONTENT_CACHE_CORRUPT: scoped supplier budget exceeded")
    # 修复裁决D3.2计数不变式：live=business−hits、live≤supplier≤2×live、
    # len(errors)==supplier−live（旧形态errors=[]且supplier==live天然满足）。
    if business != expected_calls or not 0 <= hits <= business:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：execution-report.json调用计数与章数/预算语义不符。"
        )
    live = business - hits
    if not live <= supplier <= live * FROZEN_MAX_ATTEMPTS:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：execution-report.json供应商计数超出纠错重试预算语义。"
        )
    errors = report["errors"]
    if not isinstance(errors, list) or len(errors) != supplier - live:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：execution-report.json错误记录数与供应商计数不变式不符。"
        )
    for error in errors:
        if not isinstance(error, dict) or set(error) != _REPORT_ERROR_KEYS:
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：execution-report.json错误记录字段集合无效。"
            )
        if error["attempt"] != 1 or error["kind"] not in ("transport", "validation"):
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：execution-report.json错误记录attempt/kind无效。"
            )
        if not isinstance(error["message"], str) or not error["message"]:
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：execution-report.json错误记录message无效。"
            )
        issues = error["issues"]
        if not isinstance(issues, list):
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：execution-report.json错误记录issues必须是数组。"
            )
        for item in issues:
            if not isinstance(item, dict) or not set(item) <= _REPORT_ISSUE_KEYS or "code" not in item:
                raise ContentCacheCorruptError(
                    f"{ERROR_CACHE_CORRUPT}：execution-report.json的issues条目键集无效。"
                )
            if item["code"] not in BUSINESS_CODES:
                raise ContentCacheCorruptError(
                    f"{ERROR_CACHE_CORRUPT}：execution-report.json的issues条目含未知业务码。"
                )
            if "detail" in item and not isinstance(item["detail"], str):
                raise ContentCacheCorruptError(
                    f"{ERROR_CACHE_CORRUPT}：execution-report.json的issues条目detail无效。"
                )
    return business, supplier, hits


def _build_raw_responses_doc(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for call in calls:
        if set(call) != _RAW_CALL_KEYS:
            raise AssertionError("raw-responses条目键集偏离冻结9键。")
        entries.append(
            {
                "call_slot": call["call_slot"],
                "prompt_version": call["prompt_version"],
                "validation_id": call["validation_id"],
                "raw_response": call["raw_response"],
                "cache_hit": bool(call["cache_hit"]),
                "attempt_count": int(call["attempt_count"]),
                "payload_chars": int(call["payload_chars"]),
                "message_chars": int(call["message_chars"]),
                "max_tokens": int(call["max_tokens"]),
            }
        )
    return {
        "schema_name": RAW_DOC_SCHEMA_NAME,
        "schema_version": RAW_DOC_SCHEMA_VERSION,
        "calls": entries,
    }


def _validate_raw_responses_doc(
    document: Any,
    *,
    content: Mapping[str, Any],
    inputs: ContentInputs,
) -> list[dict[str, Any]]:
    """实现决定5：逐条原始响应重放业务校验，规范化后确定性合并必须逐字节复现既有正文块。
    二次失败形态裁决N1：返回从raw重推导的single信封规范化清单，供组复验与报告比对。"""

    if not isinstance(document, dict) or set(document) != _RAW_DOC_KEYS:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：raw-responses.json字段集合偏离冻结3键。"
        )
    if document["schema_name"] != RAW_DOC_SCHEMA_NAME or document["schema_version"] != RAW_DOC_SCHEMA_VERSION:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses.json文档身份无效。")
    calls = document["calls"]
    chapter_count = len(inputs.ledgers)
    bounded = composer_version_for_tokens(content["generation"]["per_call_max_tokens"]) == "1.2"
    scopes = bounded_scopes(inputs) if bounded else []
    expected_calls = len(scopes) + 1 if bounded else (2 if chapter_count == 1 else 3)
    if not isinstance(calls, list) or len(calls) != expected_calls:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：raw-responses.json调用条目数与章数语义不符。"
        )
    slot_order = ([CALL_SINGLE] * len(scopes) + [CALL_GLOBAL]) if bounded else [CALL_SINGLE] + ([CALL_BATCH] if chapter_count > 1 else []) + [CALL_GLOBAL]
    bounded_results = []
    # 二次失败形态裁决N3.2：raw条目max_tokens按组反查版本核对，旧组3600保持合法。
    group_version = composer_version_for_tokens(content["generation"]["per_call_max_tokens"])
    if group_version is None:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：raw-responses复验时正文per_call_max_tokens不在冻结映射集内。"
        )
    expected_max_tokens = PER_CALL_MAX_TOKENS_HISTORY[group_version]
    normalized_by_slot: dict[str, Any] = {}
    derived_normalizations: list[dict[str, Any]] = []
    for call_index, (entry, slot) in enumerate(zip(calls, slot_order)):
        scope = scopes[call_index] if bounded and slot == CALL_SINGLE else inputs
        if not isinstance(entry, dict) or set(entry) != _RAW_CALL_KEYS:
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses条目字段集合无效。")
        # 修复裁决D3.1：attempt_count＝成功所耗真实尝试（1..2）；缓存命中恒为1。
        attempt_count = entry["attempt_count"]
        if entry["call_slot"] != slot:
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses条目调用形态无效。")
        if not _is_int(attempt_count) or not 1 <= attempt_count <= FROZEN_MAX_ATTEMPTS:
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses条目attempt_count越界。")
        if entry["cache_hit"] and attempt_count != 1:
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：raw-responses缓存命中条目的attempt_count必须为1。"
            )
        if (
            entry["prompt_version"] != PROMPT_VERSIONS[slot]
            or entry["validation_id"] != VALIDATION_IDS[slot]
            or entry["max_tokens"] != expected_max_tokens[slot]
        ):
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses条目冻结版本不符。")
        if entry["cache_hit"] is not bool(entry["cache_hit"]):
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses条目cache_hit形态无效。")
        if not _is_int(entry["payload_chars"]) or not _is_int(entry["message_chars"]):
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses条目观测字段形态无效。")
        parsed, parse_error = _parse_response_text(entry["raw_response"])
        if parse_error is not None:
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：raw-responses条目重放解析失败：{parse_error}"
            )
        normalized, issues = normalize_slot_response(slot, parsed, scope)
        if normalized is None or issues:
            raise ContentCacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：raw-responses条目重放业务校验失败：{_issue_message(issues)}"
            )
        if slot == CALL_SINGLE:
            record = single_envelope_normalization(parsed, scope.ledgers[0], attempt_count)
            if record is not None:
                derived_normalizations.append(record)
        normalized_by_slot[slot] = normalized
        bounded_results.append(normalized)
    if bounded:
        responses = iter(bounded_results)
        single, batch, final = _bounded_calls(inputs, lambda *args: next(responses))
        normalized_by_slot = {CALL_SINGLE: single, CALL_GLOBAL: final}
        if batch is not None:
            normalized_by_slot[CALL_BATCH] = batch
    recomputed = assemble_document_content(
        inputs,
        normalized_by_slot[CALL_SINGLE],
        normalized_by_slot.get(CALL_BATCH),
        normalized_by_slot[CALL_GLOBAL],
        mode=str(content["generation"]["mode"]),
        backend=str(content["generation"]["backend"]),
        per_call_max_tokens=expected_max_tokens,
    )
    if canonical_json_bytes(recomputed) != canonical_json_bytes(content):
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：raw-responses重放装配与既有正文逐字节不一致。"
        )
    return derived_normalizations


# ---------------------------------------------------------------------------
# 响应文本解析（实现决定14）与逐调用llm-cache（裁决Q6.8）
# ---------------------------------------------------------------------------


def _parse_response_text(raw: Any) -> tuple[Any, str | None]:
    if not isinstance(raw, str):
        return raw, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"响应文本不是合法JSON：{exc.msg}"
    if not isinstance(parsed, dict):
        return None, "响应JSON顶层必须是对象。"
    return parsed, None


def _content_llm_cache_path(m2_root: Path, role_cache_identity_sha256: str) -> Path:
    return m2_root / ROOT_DIRECTORY / LLM_CACHE_DIRECTORY / f"{role_cache_identity_sha256}.json"


def _validate_llm_cache_entry(
    entry: Any,
    *,
    slot: str,
    role_cache_identity_sha256: str,
    messages_sha256: str,
    inputs: ContentInputs,
) -> tuple[Any, dict[str, Any]]:
    if not isinstance(entry, dict) or set(entry) != _LLM_CACHE_KEYS:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：content llm-cache条目字段集合偏离冻结11键。"
        )
    if (
        entry["schema_name"] != LLM_CACHE_SCHEMA_NAME
        or entry["schema_version"] != LLM_CACHE_SCHEMA_VERSION
        or entry["call_slot"] != slot
        or entry["role_cache_identity_sha256"] != role_cache_identity_sha256
        or entry["messages_sha256"] != messages_sha256
        or entry["model"] != FROZEN_MODEL
    ):
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：content llm-cache条目身份与本次调用不符。"
        )
    if not isinstance(entry["backend"], str) or not entry["backend"]:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：content llm-cache条目backend无效。")
    if not _is_int(entry["supplier_request_count"]) or entry["supplier_request_count"] != 1:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：content llm-cache条目supplier_request_count必须为1。"
        )
    if not isinstance(entry["created_at"], str) or not entry["created_at"]:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：content llm-cache条目created_at无效。")
    parsed, parse_error = _parse_response_text(entry["raw_response"])
    if parse_error is not None:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：content llm-cache原始响应不可解析：{parse_error}")
    normalized, issues = normalize_slot_response(slot, parsed, inputs)
    if normalized is None or issues:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：content llm-cache响应重放业务校验失败：{_issue_message(issues)}"
        )
    if not isinstance(entry["normalized_response"], dict) or canonical_json_bytes(
        entry["normalized_response"]
    ) != canonical_json_bytes(normalized):
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：content llm-cache规范化响应与重放结果逐字节不一致。"
        )
    return parsed, normalized


def _write_llm_cache_entry(
    path: Path,
    *,
    slot: str,
    role_cache_identity_sha256: str,
    messages_sha256: str,
    raw_response: Any,
    normalized_response: Mapping[str, Any],
    backend: str,
) -> None:
    entry = {
        "schema_name": LLM_CACHE_SCHEMA_NAME,
        "schema_version": LLM_CACHE_SCHEMA_VERSION,
        "call_slot": slot,
        "role_cache_identity_sha256": role_cache_identity_sha256,
        "messages_sha256": messages_sha256,
        "raw_response": raw_response,
        "normalized_response": dict(normalized_response),
        "supplier_request_count": 1,
        "backend": backend,
        "model": FROZEN_MODEL,
        "created_at": _iso_now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.json.tmp-{uuid.uuid4()}")
    try:
        _write_fsync(temporary, _json_bytes(entry))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 公开入口接线辅助
# ---------------------------------------------------------------------------


def _domain_root(m2_root: Path) -> Path:
    return m2_root / ROOT_DIRECTORY


def _group_path(m2_root: Path, result_id: str) -> Path:
    if not is_safe_result_id(result_id):
        raise ContentPublishFailedError(f"{ERROR_PUBLISH_FAILED}：result_id不是安全的单层目录名。")
    return _domain_root(m2_root) / GROUP_DIRECTORY / result_id


# ---------------------------------------------------------------------------
# 结果组manifest与组复验（裁决Q6.4；实现决定5/22）
# ---------------------------------------------------------------------------


def _build_content_manifest(
    inputs: ContentInputs,
    content: Mapping[str, Any],
    *,
    result_id: str,
    cache_sha256: str,
    file_bytes: Mapping[str, bytes],
) -> dict[str, Any]:
    generation = content["generation"]
    return {
        "schema_name": MANIFEST_SCHEMA_NAME,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "task_id": inputs.task_id,
        "result_id": result_id,
        "status": "success",
        "cache_identity_sha256": cache_sha256,
        "inputs": {
            "plan_result_id": str(inputs.plan_pointer["result_id"]),
            "plan_manifest_sha256": str(inputs.plan_pointer["manifest_sha256"]),
            "timeline_id": str(inputs.timeline_pointer["result_id"]),
            "timeline_manifest_sha256": str(inputs.timeline_pointer["manifest_sha256"]),
            "chapter_frame_selection_result_id": str(inputs.selection_pointer["result_id"]),
            "chapter_frame_selection_manifest_sha256": str(inputs.selection_pointer["manifest_sha256"]),
            "frame_set_id": str(inputs.frames_pointer["result_id"]),
            "frame_set_manifest_sha256": str(inputs.frames_pointer["manifest_sha256"]),
        },
        "versions": {
            # 二次失败形态裁决N3：composer_version由冻结反查导出，旧组manifest重建恒等。
            "document_composer_v2": _report_versions(content)["composer_version"],
            "content_schema": content["schema_version"],
            "content_policy_version": generation["content_policy_version"],
            "unit_policy_version": generation["unit_policy_version"],
            "prompt_version_single": generation["prompt_versions"]["single"],
            "prompt_version_batch": generation["prompt_versions"]["batch"],
            "prompt_version_global": generation["prompt_versions"]["global"],
            "validation_id_single": generation["validator_versions"]["single"],
            "validation_id_batch": generation["validator_versions"]["batch"],
            "validation_id_global": generation["validator_versions"]["global"],
        },
        "files": {
            name: {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
            for name, payload in sorted(file_bytes.items())
        },
    }


def _synthetic_inputs_for_validation(
    content: Mapping[str, Any],
    plan: Mapping[str, Any],
    timeline_index: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> ContentInputs:
    """组复验专用输入快照：指针身份取自正文自身source_*绑定（实现决定22配套）。"""

    source_plan = content["source_plan"]
    source_timeline = content["source_timeline"]
    source_selection = content["source_chapter_frame_selection"]
    source_frames = content["source_frame_set"]
    return ContentInputs(
        task_id=str(content["task_id"]),
        timeline_pointer={
            "result_id": str(source_timeline["timeline_id"]),
            "manifest_sha256": str(source_timeline["manifest_sha256"]),
        },
        timeline_index=dict(timeline_index),
        plan_pointer={
            "result_id": str(source_plan["result_id"]),
            "manifest_sha256": str(source_plan["manifest_sha256"]),
        },
        plan=dict(plan),
        selection_pointer={
            "result_id": str(source_selection["result_id"]),
            "manifest_sha256": str(source_selection["manifest_sha256"]),
        },
        selection=dict(selection),
        frames_pointer={
            "result_id": str(source_frames["frame_set_id"]),
            "manifest_sha256": str(source_frames["manifest_sha256"]),
        },
        ledgers=_build_ledgers(timeline_index, plan),
    )


def validate_document_content_group(
    group_path: str | Path,
    *,
    cache_sha256: str,
    plan: Mapping[str, Any],
    timeline_index: Mapping[str, Any],
    selection: Mapping[str, Any],
    expected_result_id: str | None = None,
    require_directory_name: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """复验正文正式组（裁决Q6.8确定性口径＋实现决定5）：成员恰四文件、
    正文规范字节、validator-3.0全量、raw逐条重放业务＋重装配逐字节、
    报告12键一致、manifest重建比对＋逐文件磁盘哈希。"""

    group = Path(group_path)
    if not group.is_dir():
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文结果组目录不存在。")
    content_path = group / CONTENT_FILE
    manifest_path = group / MANIFEST_FILE
    report_path = group / REPORT_FILE
    raw_path = group / RAW_RESPONSES_FILE
    for path in (content_path, manifest_path, report_path, raw_path):
        if not path.is_file():
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文结果组缺少必需文件。")

    content_bytes = content_path.read_bytes()
    try:
        content = json.loads(content_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文无法读取或JSON无效。") from exc
    if not isinstance(content, dict):
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文必须是JSON对象。")
    if content_bytes != canonical_json_bytes(content):
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文不是确定的规范JSON字节。")
    synthetic = _synthetic_inputs_for_validation(content, plan, timeline_index, selection)
    try:
        validate_structured_document_content_v3(
            content,
            plan=plan,
            timeline_index=timeline_index,
            selection=selection,
            expected_task_id=synthetic.task_id,
        )
    except StructuredContentValidationError as exc:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文无效：{exc}") from exc

    member_names = {entry.name for entry in group.iterdir()}
    if member_names != _GROUP_FILE_NAMES:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文结果组成员无效。")

    raw_bytes = raw_path.read_bytes()
    try:
        raw_document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses无法读取或JSON无效。") from exc
    if raw_bytes != _json_bytes(raw_document):
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：raw-responses不是确定的序列化字节。")
    derived_normalizations = _validate_raw_responses_doc(
        raw_document, content=content, inputs=synthetic
    )

    report = _read_json(report_path, "execution-report", ContentCacheCorruptError)
    _validate_execution_report(report, content=content, task_id=synthetic.task_id)
    # 二次失败形态裁决N1（verified not trusted）：报告response_normalizations
    # 必须与raw重推导清单逐条一致；1.0组该字段缺席且推导必为空。
    reported_normalizations = report.get("response_normalizations", [])
    if [dict(item) for item in reported_normalizations] != derived_normalizations:
        raise ContentCacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：execution-report.json的response_normalizations"
            f"与raw-responses重推导清单不一致。"
        )

    result_id = _compute_content_id(content)
    if content["content_id"] != result_id:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文content_id与重算不符。")
    if require_directory_name and group.name != result_id:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文结果组目录名与内容身份不符。")
    if expected_result_id is not None and result_id != expected_result_id:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：正文结果身份与预期不符。")

    file_bytes = {
        CONTENT_FILE: content_bytes,
        RAW_RESPONSES_FILE: raw_bytes,
        REPORT_FILE: report_path.read_bytes(),
    }
    expected_manifest = _build_content_manifest(
        synthetic,
        content,
        result_id=result_id,
        cache_sha256=cache_sha256,
        file_bytes=file_bytes,
    )
    manifest = _read_json(manifest_path, "content manifest", ContentCacheCorruptError)
    _exact_keys(manifest, _MANIFEST_KEYS, "content manifest", ContentCacheCorruptError)
    _exact_keys(manifest["inputs"], _MANIFEST_INPUT_KEYS, "manifest.inputs", ContentCacheCorruptError)
    _exact_keys(manifest["versions"], _MANIFEST_VERSION_KEYS, "manifest.versions", ContentCacheCorruptError)
    if manifest != expected_manifest:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：content manifest与组内容不一致。")
    if set(manifest["files"]) != {CONTENT_FILE, RAW_RESPONSES_FILE, REPORT_FILE}:
        raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：content manifest文件清单无效。")
    for name, record in manifest["files"].items():
        if not _safe_relative_file(name) or set(record) != {"sha256", "bytes"}:
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：content manifest文件记录无效。")
        path = group / PurePosixPath(name)
        if not path.is_file() or _file_sha256(path) != record["sha256"]:
            raise ContentCacheCorruptError(f"{ERROR_CACHE_CORRUPT}：content manifest记录的哈希与磁盘不符。")
    return content, manifest, report


# ---------------------------------------------------------------------------
# 本域current、锁、注入、审计（14-B逐字平移）
# ---------------------------------------------------------------------------


def _current_payload(result_id: str, manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": CURRENT_SCHEMA_NAME,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "result_id": result_id,
        "group": f"{GROUP_DIRECTORY}/{result_id}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
    }


def _load_current(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = root / CURRENT_FILE
    if not _current_path_state(path, "content current", CurrentPointerCorruptError):
        return None, None
    pointer = _read_json(path, "content current", CurrentPointerCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "content current", CurrentPointerCorruptError)
    if (
        pointer["schema_name"] != CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise CurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：content current身份无效。")
    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：content current的组路径越出正式组目录。"
        )
    manifest_path = group / MANIFEST_FILE
    if not manifest_path.is_file() or _file_sha256(manifest_path) != pointer["manifest_sha256"]:
        raise CurrentPointerCorruptError("CURRENT_POINTER_CORRUPT：content current目标缺失或损坏。")
    return pointer, str(pointer["result_id"])


def _replace_current(root: Path, payload: Mapping[str, Any]) -> None:
    temporary = root / f".{CURRENT_FILE}.tmp-{uuid.uuid4().hex}"
    try:
        _write_fsync(temporary, _json_bytes(payload))
        os.replace(temporary, root / CURRENT_FILE)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _disk_current_identity(root: Path) -> tuple[str, str] | None:
    try:
        raw = (root / CURRENT_FILE).read_bytes()
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
    if not (root / CURRENT_FILE).exists():
        return True
    identity = _disk_current_identity(root)
    return identity is not None and identity[0] != result_id


@contextmanager
def _module_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    locks = m2_root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / LOCK_FILE
    payload = _json_bytes(
        {
            "schema_name": "ModuleLock",
            "schema_version": "1.0",
            "task_id": task_id,
            "module": MODULE_TOKEN,
            "pid": os.getpid(),
        }
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise ContentConcurrentRunError("CONCURRENT_RUN：content锁已存在。") from exc
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
        raise InjectedContentFailure(f"注入失败：{point}")


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
) -> tuple[str, Path]:
    failure_id = (
        f"failure-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    )
    root = m2_root / "failures" / MODULE_TOKEN / failure_id
    root.mkdir(parents=True, exist_ok=False)
    code = getattr(error, "error_code", ERROR_PUBLISH_FAILED)
    message = (
        str(error)
        if isinstance(error, DocumentComposerV2Error)
        else f"正文组合失败：{type(error).__name__}"
    )
    payload = {
        "schema_name": FAILURE_SCHEMA_NAME,
        "schema_version": FAILURE_SCHEMA_VERSION,
        "failure_id": failure_id,
        "task_id": task_id,
        "module": MODULE_TOKEN,
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
    return failure_id, root


# ---------------------------------------------------------------------------
# 逐调用执行（裁决Q3三调用拓扑＋修复裁决D2纠错重试；预算守卫先于每次complete）
# ---------------------------------------------------------------------------


def bounded_scopes(inputs: ContentInputs) -> list[ContentInputs]:
    """bounded-chapter-units-1.0: up to six consecutive units per call.

    Scope reconstruction is deterministic and used for raw-response replay.
    Chapters never share a request. All original unit/event IDs are retained.
    """
    scopes = []
    for ledger in inputs.ledgers:
        for offset in range(0, len(ledger.units), 6):
            part = dataclasses.replace(ledger, units=ledger.units[offset:offset + 6])
            scopes.append(dataclasses.replace(inputs, ledgers=(part,)))
    return scopes


def _bounded_calls(inputs: ContentInputs, run: Any) -> tuple[dict | None, dict | None, dict | None]:
    paragraphs = {ledger.chapter_id: [] for ledger in inputs.ledgers}
    for scope in bounded_scopes(inputs):
        result = run(CALL_SINGLE, build_single_payload(scope.ledgers[0]), scope)
        if result is None:
            return None, None, None
        paragraphs[scope.ledgers[0].chapter_id].extend(result["paragraphs"])
    # Merge only within a chapter after every scoped response passes coverage.
    paragraphs = {key: _merge_paragraphs_to_cap(value) for key, value in paragraphs.items()}
    single = {"paragraphs": paragraphs[inputs.ledgers[0].chapter_id]}
    batch = {"chapters": [{"chapter_id": ledger.chapter_id, "paragraphs": paragraphs[ledger.chapter_id]}
                           for ledger in inputs.ledgers[1:]]} if len(inputs.ledgers) > 1 else None
    final = run(CALL_GLOBAL, build_global_payload(inputs.ledgers, paragraphs), inputs)
    return single, batch, final


def _run_content_slot(
    *,
    slot: str,
    payload: Mapping[str, Any],
    inputs: ContentInputs,
    role: LlmRoleConfig,
    env: Mapping[str, str],
    transport: LLMTransport,
    mock_mode: bool,
    backend: str,
    m2_root: Path,
    counters: dict[str, int],
    errors: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    failed_attempts: list[dict[str, Any]],
    normalizations: list[dict[str, Any]],
    bounded: bool = False,
) -> dict[str, Any] | None:
    """单个业务调用槽位：llm-cache命中计0供应商请求；至多2次供应商请求
    （修复裁决D2，14-B同构反馈纠错）；失败累积errors/failed_attempts返回None。
    成功路径上single信封规范化记录进normalizations（二次失败形态裁决N1）。"""

    counters["business"] += 1
    slot_role = _slot_role_for_call(role, slot)
    token_limits = PER_CALL_MAX_TOKENS_HISTORY["1.2"] if bounded else PER_CALL_MAX_TOKENS
    if bounded:
        slot_role = dataclasses.replace(slot_role, generation=dataclasses.replace(slot_role.generation, max_tokens=token_limits[slot]))
    messages = build_slot_messages(slot, payload)
    messages_sha256 = canonical_json_sha256(messages)
    payload_chars, message_chars = _payload_metrics(payload, messages)
    _, role_call_sha256 = compute_role_cache_identity(
        slot_role,
        input_canonical_sha256=_slot_input_canonical_sha256(payload),
        prompt_content_sha256=content_prompt_content_sha256(slot),
    )
    entry_path = _content_llm_cache_path(m2_root, role_call_sha256)

    def record(raw_response: Any, *, cache_hit: bool, attempt_count: int) -> None:
        calls.append(
            {
                "call_slot": slot,
                "prompt_version": PROMPT_VERSIONS[slot],
                "validation_id": VALIDATION_IDS[slot],
                "raw_response": raw_response,
                "cache_hit": cache_hit,
                "attempt_count": attempt_count,
                "payload_chars": payload_chars,
                "message_chars": message_chars,
                "max_tokens": token_limits[slot],
            }
        )

    def note_normalization(raw_response: Any, attempt_count: int) -> None:
        if slot != CALL_SINGLE:
            return
        parsed, parse_error = _parse_response_text(raw_response)
        if parse_error is not None:
            return
        record_item = single_envelope_normalization(parsed, inputs.ledgers[0], attempt_count)
        if record_item is not None:
            normalizations.append(record_item)

    if _current_path_state(entry_path, "content llm-cache条目", ContentCacheCorruptError):
        entry = _read_json(entry_path, "content llm-cache条目", ContentCacheCorruptError)
        _, normalized = _validate_llm_cache_entry(
            entry,
            slot=slot,
            role_cache_identity_sha256=role_call_sha256,
            messages_sha256=messages_sha256,
            inputs=inputs,
        )
        counters["cache_hits"] += 1
        record(entry["raw_response"], cache_hit=True, attempt_count=1)
        note_normalization(entry["raw_response"], 1)
        return normalized

    # 业务调用预算守卫（重试不新增业务调用，修复裁决D2.4）
    if counters["business"] > (12 if bounded else FROZEN_BUSINESS_BUDGET):
        raise ContentBudgetExceededError(
            f"{ERROR_CONTENT_BUDGET_EXCEEDED}：第{counters['business']}次业务调用"
            f"超过冻结预算{FROZEN_BUSINESS_BUDGET}，零发布。"
        )
    assert role.connection_profile is not None
    api_key, _credential_present = resolve_api_key(role, env)
    if not api_key:
        raise RoleConfigInvalidError(
            f"{ERROR_ROLE_CONFIG_INVALID}：启用角色要求API Key，"
            f"但声明的环境变量{role.connection_profile.api_key_env}未设置。"
        )
    config = build_llm_config(slot_role, api_key)
    max_attempts = int(role.max_attempts_per_business_call)

    def note_failure(
        attempt: int, kind: str, message: str, issues: list[dict[str, Any]], raw_response: Any
    ) -> None:
        errors.append({"attempt": attempt, "kind": kind, "message": message, "issues": issues})
        failed_attempts.append(
            {
                "call_slot": slot,
                "attempt": attempt,
                "kind": kind,
                "message": message,
                "issues": [dict(item) for item in issues],
                "raw_response": raw_response,
            }
        )

    current_messages = messages
    for attempt in range(1, max_attempts + 1):
        # 供应商预算守卫：先于每一次transport.complete（裁决Q3.4/实现决定15）
        if counters["supplier"] + 1 > (12 if bounded else FROZEN_SUPPLIER_BUDGET):
            raise ContentBudgetExceededError(
                f"{ERROR_CONTENT_BUDGET_EXCEEDED}：第{counters['supplier'] + 1}次供应商请求"
                f"超过冻结预算{FROZEN_SUPPLIER_BUDGET}，拒绝请求并零发布。"
            )
        counters["supplier"] += 1
        try:
            raw_response = transport.complete(config, current_messages, role.response_format or {})
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # 二次失败形态裁决N7：审批守卫停止不是模块失败——原样传播，
            # 不记errors、不纠错重试（守卫在transport层零网络抛出，重试无意义
            # 且supplier计数会失真）。冻结入口码duck-typing判别，避免对
            # m2_pipeline的循环依赖（实现决定28）。
            if getattr(exc, "error_code", None) == "LLM_APPROVAL_REQUIRED":
                raise
            safe = _safe_error_text(exc, api_key)
            note_failure(attempt, "transport", safe, [], None)
            if attempt >= max_attempts:
                return None
            current_messages = list(messages) + [
                {
                    "role": "user",
                    "content": f"上一次调用未获得合法JSON响应：{safe}。{_TRANSPORT_FEEDBACK_INSTRUCTION}",
                }
            ]
            continue
        parsed, parse_error = _parse_response_text(raw_response)
        if parse_error is not None:
            note_failure(attempt, "transport", parse_error, [], raw_response)
            if attempt >= max_attempts:
                return None
            current_messages = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        f"上一次调用未获得合法JSON响应：{parse_error}。"
                        f"{_TRANSPORT_FEEDBACK_INSTRUCTION}"
                    ),
                }
            ]
            continue
        normalized, issues = normalize_slot_response(slot, parsed, inputs)
        if normalized is None or issues:
            note_failure(attempt, "validation", _issue_message(issues), list(issues), raw_response)
            if attempt >= max_attempts:
                return None
            feedback = list(messages)
            if isinstance(parsed, dict):
                feedback.append(
                    {"role": "assistant", "content": _compact_json(parsed)}
                )
            issues_json = _compact_json(issues)
            feedback.append(
                {
                    "role": "user",
                    "content": (
                        f"上一次响应未通过业务校验，错误清单：{issues_json}。"
                        f"{_CONTENT_VALIDATION_FEEDBACK}"
                    ),
                }
            )
            current_messages = feedback
            continue
        _write_llm_cache_entry(
            entry_path,
            slot=slot,
            role_cache_identity_sha256=role_call_sha256,
            messages_sha256=messages_sha256,
            raw_response=raw_response,
            normalized_response=normalized,
            backend=backend,
        )
        record(raw_response, cache_hit=False, attempt_count=attempt)
        note_normalization(raw_response, attempt)
        return normalized
    return None


# ---------------------------------------------------------------------------
# 顶层编排（裁决Q6.2/Q6.7/Q6.8六件套；14-B逐字平移）
# ---------------------------------------------------------------------------


def _result_from_group(
    group: Path,
    result_id: str,
    content: Mapping[str, Any],
    manifest: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    cache_hit: bool,
    pointer_repaired: bool,
) -> DocumentCompositionResult:
    return DocumentCompositionResult(
        group_path=group,
        result_id=result_id,
        content=dict(content),
        manifest=dict(manifest),
        report=dict(report),
        chapter_count=len(content["chapters"]),
        plan_result_source=str(content["source_plan"]["result_source"]),
        plan_quality_level=str(content["source_plan"]["quality_level"]),
        business_call_count=int(report["business_call_count"]),
        supplier_request_count=int(report["supplier_request_count"]),
        cache_hit_count=int(report["cache_hit_count"]),
        cache_hit=cache_hit,
        pointer_repaired=pointer_repaired,
    )


def compose_document_v2(
    output_root: str | Path,
    task_id: str,
    *,
    model_registry: str | Path | None = None,
    transport: LLMTransport | None = None,
    project_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    failure_injection: str | None = None,
    bounded: bool = False,
    preserve_published: bool = False,
) -> DocumentCompositionResult:
    """组合并发布StructuredDocumentContent 3.0不可变结果组（裁决Q3/Q6全部语义）。

    - 全程只读M1与上游14-A/14-B/13/12/11正式组；产物仅写入``<output_root>/<task_id>/m2/content/``；
    - ``transport``为注入点；默认接线``OpenAICompatibleTransport``，14-C离线验收不使用（裁决Q7.6挂账）；
    - emergency计划照常消费（裁决Q6.9，无CLI旗无拒绝模式）。
    """

    if failure_injection is not None and failure_injection not in _FAILURE_INJECTION_POINTS:
        raise DocumentComposerV2Error("未知的正文组合失败注入点。")
    validate_task_id(task_id)
    output = Path(output_root).expanduser()
    m2_root = _m2_root(output, task_id)
    root = m2_root / ROOT_DIRECTORY
    groups_dir = root / GROUP_DIRECTORY
    env: Mapping[str, str] = os.environ if environ is None else environ
    mock_mode = transport is not None
    active_transport: LLMTransport = transport if transport is not None else OpenAICompatibleTransport()
    mode = MODE_MOCK if mock_mode else MODE_REAL

    # --- 锁前：零写入、零审计（14-B先例；UNIT_CONSTRUCTION_FAILED亦前置） ---
    inputs = _load_content_inputs(output, task_id)
    backend = "mock" if mock_mode else ""
    stage = "prepare"
    role: LlmRoleConfig | None = None

    counters = {"business": 0, "supplier": 0, "cache_hits": 0}
    errors: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    failed_attempts: list[dict[str, Any]] = []  # 修复裁决D1取证载荷
    normalizations: list[dict[str, Any]] = []  # 二次失败形态裁决N1审计记录
    cache_sha256: str | None = None
    result_id: str | None = None
    previous_current_id: str | None = None
    initial_current_identity: tuple[str, str] | None = None
    group: Path | None = None
    temporary: Path | None = None
    created_group = False
    cleanup_status = "not_created"

    with _module_lock(m2_root, task_id):
        try:
            stage = "after_inputs"
            initial_current_identity = _disk_current_identity(root)
            _inject(failure_injection, "after_inputs")

            stage = "role_config"
            project = (
                Path(project_root)
                if project_root is not None
                else Path(__file__).resolve().parent.parent
            )
            role, _snapshot = _resolve_content_role(project, model_registry)
            content_role_identity_sha256 = _role_config_identity_sha256(role)
            _, cache_sha256 = _content_cache_identity(inputs, content_role_identity_sha256)
            if bounded:
                identity, _ = _content_cache_identity(inputs, content_role_identity_sha256)
                identity["composer_version"] = "1.2"
                identity["per_call_max_tokens"] = PER_CALL_MAX_TOKENS_HISTORY["1.2"]
                cache_sha256 = canonical_json_sha256(identity)
            if not mock_mode:
                assert role.connection_profile is not None
                backend = str(role.connection_profile.backend)

            stage = "current_validation"
            current_pointer, previous_current_id = _load_current(root)
            if current_pointer is not None:
                candidate = artifact_group_path(
                    root, GROUP_DIRECTORY, str(current_pointer["result_id"])
                )
                if candidate is None:
                    raise CurrentPointerCorruptError(
                        "CURRENT_POINTER_CORRUPT：content current的组路径越界。"
                    )
                manifest_probe = _read_json(
                    candidate / MANIFEST_FILE, "content manifest", ContentCacheCorruptError
                )
                if (
                    isinstance(manifest_probe.get("cache_identity_sha256"), str)
                    and (manifest_probe["cache_identity_sha256"] == cache_sha256 or preserve_published)
                ):
                    content, manifest, report = validate_document_content_group(
                        candidate,
                        cache_sha256=str(manifest_probe["cache_identity_sha256"]),
                        plan=inputs.plan,
                        timeline_index=inputs.timeline_index,
                        selection=inputs.selection,
                    )
                    return _result_from_group(
                        candidate,
                        str(current_pointer["result_id"]),
                        content,
                        manifest,
                        report,
                        cache_hit=True,
                        pointer_repaired=False,
                    )

            stage = "model_calls"
            _inject(failure_injection, "before_model_call")
            ledgers = inputs.ledgers
            normalized_by_slot: dict[str, Any] = {}
            chapter_paragraphs: dict[str, list[dict[str, Any]]] = {}
            if bounded:
                if len(bounded_scopes(inputs)) + 1 > 12:
                    raise ContentBudgetExceededError("CONTENT_BUDGET_EXCEEDED: scoped plan exceeds 12 calls; zero requests")
                def run(slot, payload, scope):
                    return _run_content_slot(slot=slot, payload=payload, inputs=scope, role=role,
                        env=env, transport=active_transport, mock_mode=mock_mode, backend=backend,
                        m2_root=m2_root, counters=counters, errors=errors, calls=calls,
                        failed_attempts=failed_attempts, normalizations=normalizations, bounded=True)
                single, batch, final = _bounded_calls(inputs, run)
                if single is not None:
                    normalized_by_slot[CALL_SINGLE] = single
                if batch is not None:
                    normalized_by_slot[CALL_BATCH] = batch
                if final is not None:
                    normalized_by_slot[CALL_GLOBAL] = final
            else:
                single = _run_content_slot(
                    slot=CALL_SINGLE,
                    payload=build_single_payload(ledgers[0]),
                    inputs=inputs,
                    role=role,
                    env=env,
                    transport=active_transport,
                    mock_mode=mock_mode,
                    backend=backend,
                    m2_root=m2_root,
                    counters=counters,
                    errors=errors,
                    calls=calls,
                    failed_attempts=failed_attempts,
                    normalizations=normalizations,
                )
                batch = None
                if single is not None:
                    normalized_by_slot[CALL_SINGLE] = single
                    chapter_paragraphs[ledgers[0].chapter_id] = list(single["paragraphs"])
                    if len(ledgers) > 1:
                        batch = _run_content_slot(
                            slot=CALL_BATCH,
                            payload=build_batch_payload(ledgers[1:]),
                            inputs=inputs,
                            role=role,
                            env=env,
                            transport=active_transport,
                            mock_mode=mock_mode,
                            backend=backend,
                            m2_root=m2_root,
                            counters=counters,
                            errors=errors,
                            calls=calls,
                            failed_attempts=failed_attempts,
                            normalizations=normalizations,
                        )
                        if batch is not None:
                            normalized_by_slot[CALL_BATCH] = batch
                            for item in batch["chapters"]:
                                chapter_paragraphs[str(item["chapter_id"])] = list(item["paragraphs"])
                    global_normalized = None
                    if batch is not None or len(ledgers) == 1:
                        global_normalized = _run_content_slot(
                            slot=CALL_GLOBAL,
                            payload=build_global_payload(ledgers, chapter_paragraphs),
                            inputs=inputs,
                            role=role,
                            env=env,
                            transport=active_transport,
                            mock_mode=mock_mode,
                            backend=backend,
                            m2_root=m2_root,
                            counters=counters,
                            errors=errors,
                            calls=calls,
                            failed_attempts=failed_attempts,
                            normalizations=normalizations,
                        )
                        if global_normalized is not None:
                            normalized_by_slot[CALL_GLOBAL] = global_normalized
            _inject(failure_injection, "after_model_call")
            if (
                single is None
                or (len(ledgers) > 1 and batch is None)
                or CALL_GLOBAL not in normalized_by_slot
            ):
                summary = "; ".join(
                    f"{item['kind']}:{item['message']}" for item in errors
                ) or "无可用响应"
                raise ContentModelFailedError(
                    f"{ERROR_CONTENT_MODEL_FAILED}：三调用组合失败（{summary}），零发布。",
                    errors=errors,
                    failed_attempts=failed_attempts,
                )

            stage = "assembly"
            try:
                content = assemble_document_content(
                    inputs,
                    normalized_by_slot[CALL_SINGLE],
                    normalized_by_slot.get(CALL_BATCH),
                    normalized_by_slot[CALL_GLOBAL],
                    mode=mode,
                    backend=backend,
                    per_call_max_tokens=PER_CALL_MAX_TOKENS_HISTORY["1.2"] if bounded else None,
                )
            except StructuredContentValidationError as exc:
                raise ContentValidationFailedError(
                    f"{ERROR_CONTENT_VALIDATION_FAILED}：已通过业务校验的响应经确定性装配后"
                    f"仍通不过validator-3.0（实现缺陷级）：{exc}"
                ) from exc

            stage = "group_publish"
            result_id = content["content_id"]
            raw_document = _build_raw_responses_doc(calls)
            report = _build_execution_report(
                inputs,
                content,
                business_call_count=counters["business"],
                supplier_request_count=counters["supplier"],
                cache_hit_count=counters["cache_hits"],
                errors=errors,
                response_normalizations=normalizations,
            )
            file_bytes: dict[str, bytes] = {
                CONTENT_FILE: canonical_json_bytes(content),
                RAW_RESPONSES_FILE: _json_bytes(raw_document),
                REPORT_FILE: _json_bytes(report),
            }
            manifest = _build_content_manifest(
                inputs,
                content,
                result_id=result_id,
                cache_sha256=cache_sha256 or "",
                file_bytes=file_bytes,
            )
            manifest_bytes = _json_bytes(manifest)
            group = _group_path(m2_root, result_id)

            if group.exists():
                # 裁决Q6.8＋实现决定5：正文与manifest逐字节强一致；
                # raw/report的既有磁盘字节由完整组复验（含raw重放）钉死。
                content_file = group / CONTENT_FILE
                if (
                    not content_file.is_file()
                    or content_file.read_bytes() != file_bytes[CONTENT_FILE]
                ):
                    raise ContentCacheCorruptError(
                        f"{ERROR_CACHE_CORRUPT}：同身份正文组内容与本次发布不一致。"
                    )
                validate_document_content_group(
                    group,
                    cache_sha256=cache_sha256 or "",
                    plan=inputs.plan,
                    timeline_index=inputs.timeline_index,
                    selection=inputs.selection,
                    expected_result_id=result_id,
                )
                reused = True
            else:
                reused = False
                groups_dir.mkdir(parents=True, exist_ok=True)
                temporary = groups_dir / f".tmp-{result_id}-{uuid.uuid4().hex}"
                temporary.mkdir(exist_ok=False)
                cleanup_status = "created"
                for name, payload in file_bytes.items():
                    _write_fsync(temporary / name, payload)
                # manifest最后写（与F1/F2/OCR/13-1/14-B/14-A一致）。
                _write_fsync(temporary / MANIFEST_FILE, manifest_bytes)
                validate_document_content_group(
                    temporary,
                    cache_sha256=cache_sha256 or "",
                    plan=inputs.plan,
                    timeline_index=inputs.timeline_index,
                    selection=inputs.selection,
                    expected_result_id=result_id,
                    require_directory_name=False,
                )
                _inject(failure_injection, "before_group_publish")
                os.replace(temporary, group)
                created_group = True
                cleanup_status = "published"
                _inject(failure_injection, "after_group_publish")
                validate_document_content_group(
                    group,
                    cache_sha256=cache_sha256 or "",
                    plan=inputs.plan,
                    timeline_index=inputs.timeline_index,
                    selection=inputs.selection,
                    expected_result_id=result_id,
                )

            # pointer_repaired仅在复用既有同身份组但current缺失/指向他组时置真（14-B F.6先例）。
            pointer_repaired = reused and previous_current_id != result_id
            if previous_current_id != result_id:
                stage = "current_publish"
                if not reused:
                    _inject(failure_injection, "before_current")
                _replace_current(root, _current_payload(result_id, manifest_bytes))
            return _result_from_group(
                group,
                result_id,
                content,
                manifest,
                report,
                cache_hit=reused,
                pointer_repaired=pointer_repaired,
            )
        except BaseException as exc:
            current_identity = _disk_current_identity(root)
            if (
                result_id is not None
                and created_group
                and group is not None
                and group.exists()
                and _may_discard_published_group(root, result_id)
            ):
                shutil.rmtree(group, ignore_errors=True)
                cleanup_status = "group_removed"
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
                cleanup_status = "temp_removed"
            # 二次失败形态裁决N7：审批守卫停止＝非模块失败——完成清理后原样
            # 传播，不写失败审计（F3前置边界无审计先例），杜绝把零网络的
            # 审批停止误分类为CONTENT_MODEL_FAILED并污染失败域。
            if getattr(exc, "error_code", None) == "LLM_APPROVAL_REQUIRED":
                raise
            try:
                failure_id, failure_root = _write_failure(
                    m2_root,
                    task_id=task_id,
                    stage=stage,
                    error=exc,
                    input_ids={
                        "task_id": task_id,
                        "plan_result_id": str(inputs.plan_pointer["result_id"]),
                        "timeline_id": str(inputs.timeline_pointer["result_id"]),
                        "chapter_frame_selection_result_id": str(
                            inputs.selection_pointer["result_id"]
                        ),
                        "frame_set_id": str(inputs.frames_pointer["result_id"]),
                        "content_role_identity_sha256": (
                            _role_config_identity_sha256(role) if role is not None else None
                        ),
                        "result_id": result_id,
                    },
                    cache_sha256=cache_sha256,
                    previous_current_id=previous_current_id,
                    cleanup_status=cleanup_status,
                    current_unchanged=current_identity == initial_current_identity,
                    business_call_count=counters["business"],
                    supplier_request_count=counters["supplier"],
                )
                # 修复裁决D1：仅CONTENT_MODEL_FAILED失败组增补原始响应取证文件；
                # 尽力而为，取证写入失败不覆盖原始异常。
                evidence = getattr(exc, "failed_attempts", None)
                if evidence:
                    document = {
                        "schema_name": FAILED_DOC_SCHEMA_NAME,
                        "schema_version": FAILED_DOC_SCHEMA_VERSION,
                        "failure_id": failure_id,
                        "task_id": task_id,
                        "attempts": [
                            {key: attempt[key] for key in sorted(_FAILED_ATTEMPT_KEYS)}
                            for attempt in evidence
                        ],
                    }
                    _write_fsync(failure_root / MODEL_RESPONSES_FILE, _json_bytes(document))
            except BaseException:
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, DocumentComposerV2Error):
                raise
            raise ContentPublishFailedError(
                f"{ERROR_PUBLISH_FAILED}：无法发布StructuredDocumentContent 3.0结果组："
                f"{type(exc).__name__}"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)


# ---------------------------------------------------------------------------
# 公开消费入口与零API预览（裁决Q6.2/Q2.9）
# ---------------------------------------------------------------------------


def load_published_document_content(
    output_root: str | Path, task_id: str
) -> dict[str, Any] | None:
    """只读加载current指向的StructuredDocumentContent 3.0正文（供14-D与阶段15消费）。"""

    validate_task_id(task_id)
    output = Path(output_root).expanduser()
    inputs = _load_content_inputs(output, task_id)
    root = _m2_root(output, task_id) / ROOT_DIRECTORY
    pointer, _ = _load_current(root)
    if pointer is None:
        return None
    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise CurrentPointerCorruptError(
            "CURRENT_POINTER_CORRUPT：content current的组路径越界。"
        )
    manifest_probe = _read_json(group / MANIFEST_FILE, "content manifest", ContentCacheCorruptError)
    content, _, _ = validate_document_content_group(
        group,
        cache_sha256=str(manifest_probe.get("cache_identity_sha256", "")),
        plan=inputs.plan,
        timeline_index=inputs.timeline_index,
        selection=inputs.selection,
        expected_result_id=str(pointer["result_id"]),
    )
    return content


_DRY_RUN_SCHEMA_NAME = "DocumentComposerDryRun"
_DRY_RUN_SCHEMA_VERSION = "1.0"
_SEND_FIELDS_FROZEN = "plan章标题＋te事件文本（聚合为units）＋（global时）已生成正文"
_NOT_SENT_FIELDS_FROZEN = (
    "pe/oe事件、caption、OCR行、帧文件、document_profile、task_id、任何路径/密钥"
)


def build_content_composition_dry_run(
    output_root: str | Path,
    task_id: str,
    *,
    model_registry: str | Path | None = None,
    project_root: str | Path | None = None,
    bounded: bool = False,
) -> dict[str, Any]:
    """零API、零写入、不发布的逐调用外发预览（裁决Q2.9，供挂账审批与15-0接线）。

    global调用的正文由各章已验证产出决定，预览仅列将外发的章标题结构
    （实现决定23）；其余键按Q2.9枚举逐项给出。
    """

    validate_task_id(task_id)
    output = Path(output_root).expanduser()
    inputs = _load_content_inputs(output, task_id)
    project = (
        Path(project_root)
        if project_root is not None
        else Path(__file__).resolve().parent.parent
    )
    role, snapshot = _resolve_content_role(project, model_registry)
    ledgers = inputs.ledgers

    def entry(slot: str, payload: Mapping[str, Any], unit_count: int, **extra: Any) -> dict[str, Any]:
        messages = build_slot_messages(slot, payload)
        payload_chars, message_chars = _payload_metrics(payload, messages)
        # 阶段15-0裁决§一1.4第2项：纯增量键message_bytes＝同文本串的UTF-8字节数（§一1.3定义，
        # 与message_chars同一messages产物口径；预览dict属工具面，不入已裁Schema）。
        message_bytes = sum(
            len(str(message["content"]).encode("utf-8")) for message in messages
        )
        item: dict[str, Any] = {
            "call_slot": slot,
            "prompt_version": PROMPT_VERSIONS[slot],
            "validation_id": VALIDATION_IDS[slot],
            "payload": payload,
            "message_chars": message_chars,
            "message_bytes": message_bytes,
            "payload_chars": payload_chars,
            "unit_count": unit_count,
            "max_tokens": PER_CALL_MAX_TOKENS[slot],
        }
        item.update(extra)
        return item

    calls_preview = [
        entry(CALL_SINGLE, build_single_payload(ledgers[0]), len(ledgers[0].units))
    ]
    if len(ledgers) > 1:
        calls_preview.append(
            entry(
                CALL_BATCH,
                build_batch_payload(ledgers[1:]),
                sum(len(item.units) for item in ledgers[1:]),
            )
        )
    structural_global_payload = {
        "chapters": [
            {"chapter_id": item.chapter_id, "title": item.title, "paragraphs": []}
            for item in ledgers
        ]
    }
    calls_preview.append(
        entry(
            CALL_GLOBAL,
            structural_global_payload,
            0,
            note="global调用的各章正文由此前业务调用产出，预览仅列将外发的章标题结构（实现决定23）",
        )
    )
    if bounded:
        calls_preview = [entry(CALL_SINGLE, build_single_payload(scope.ledgers[0]), len(scope.ledgers[0].units)) for scope in bounded_scopes(inputs)] + [calls_preview[-1]]
        for call in calls_preview:
            call["max_tokens"] = PER_CALL_MAX_TOKENS_HISTORY["1.2"][call["call_slot"]]
    assert role.connection_profile is not None
    return {
        "schema_name": _DRY_RUN_SCHEMA_NAME,
        "schema_version": _DRY_RUN_SCHEMA_VERSION,
        "task_id": task_id,
        "model": str(role.model),
        "backend": str(role.connection_profile.backend),
        "registry_source": str(snapshot.source),
        "calls": calls_preview,
        "send_fields": _SEND_FIELDS_FROZEN,
        "not_sent_fields": _NOT_SENT_FIELDS_FROZEN,
        "budget": {
            "business_call_hard_limit": 12 if bounded else FROZEN_BUSINESS_BUDGET,
            "supplier_request_hard_limit": 12 if bounded else FROZEN_SUPPLIER_BUDGET,
            "expected_business_calls": len(calls_preview),
            "max_attempts_per_business_call": FROZEN_MAX_ATTEMPTS,
            "validation_retry_policy": VALIDATION_RETRY_POLICY,
        },
        "api_request_count": 0,
        "zero_write": True,
    }


