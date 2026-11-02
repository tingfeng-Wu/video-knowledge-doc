"""阶段14-D：M2三格式导出（DocumentExporterV2 / 自包含导出组）。

契约唯一权威：``docs/development/阶段14CD-M2文档链裁决.md``（Q5/Q6/Q7逐项裁决）。
本模块按裁决逐条零偏离落地；下列E-编号实现决定供G1「提问单→裁决文档→实现diff」
三点一致性核对。发布/缓存/中断/审计工程逐字沿14-C（document_composer_v2）与
14-B/14-A/F1先例；DOCX字节确定性与样式/页面/页眉页脚机器沿M1
document_export.py同构机制在M2独立实现（裁决Q5.6「不import M1渲染代码」）。

实现决定（E-编号供G1审查核对）：
E1. 公开入口按裁决Q6.2字面``export_document_v2(output_root, task_id)``；
    ``failure_injection``按14-B/14-C先例补为仅测试用的关键字参数，不影响生产路径。
E2. 上游域复验失败沿用域专属错误码：content→INPUT_CONTENT_MISSING/CORRUPT、
    frames→INPUT_FRAME_SET_MISSING/CORRUPT（14-C实现决定2先例）；本域
    documents current用CURRENT_POINTER_CORRUPT；task.json缺失/不可读/task_id
    不符一律INPUT_TASK_MISSING。本模块无SOURCE_VIDEO_MISMATCH（裁决Q6.10）。
E3. exporter对正文组做「导出形态复验」：顶层/章/块/帧6键/source_plan/
    source_frame_set精确键集、规范字节确定性、正文组manifest三文件哈希钉住；
    不重跑14-C全链validator——裁决Q6.6把导出输入契约定义为content＋frames
    两对象，全链语义复验属composer域职责。精确键集常量从
    document_composer_v2导入复用（裁决Q6.2「上游导入复用允许」，单一事实源）。
E4. content引用的frame_id在帧集索引无登记条目＝组级不一致→
    INPUT_FRAME_SET_CORRUPT硬失败；文件缺失/不可读/哈希不符才是裁决Q5.4的
    单帧降级形态（image_missing/image_unreadable/image_hash_mismatch）。
    组级失败不降级。
E5. image_embed_failed的帧**不**复制进导出组（images/＝成功嵌入帧集合），
    使组内恒成立「引用集＝images文件∪降级reason集」且校验重渲染与磁盘字节
    逐字节可复现。渲染采用迭代：每轮python-docx异常剔除对应帧后重渲染，
    至无新增失败收敛（确定性输入→确定性不动点）。
E6. 导出组manifest的``files``登记除manifest.json外的全部组文件
    （result.json/result.md/result.docx与每个``images/<frame_id>.png``），
    沿14-C实现决定22先例；裁决Q5.7「files含全部images条目」。
E7. 裁决Q5.7要求manifest.versions＝渲染器版本族＋document_exporter_v2，
    未枚举键名；落地冻结为7键{document_exporter_v2, document_view,
    structured_document_json, structured_document_markdown,
    structured_document_docx, word_style, image_embed_policy}，列入G1增量清单。
E8. 导出组成员集合恒含``images/``目录（0帧亦存在），使成员集、复用比对与
    校验只有一种确定形态；空目录不入manifest.files（files只登记文件）。
E9. ``python_docx``运行时版本＝importlib.metadata.version("python-docx")，
    进导出缓存身份（裁决Q6.6）但不进DOCX字节——DOCX字节确定性由M1同构的
    ZIP归档规化＋core properties钉定承载；库版本变化→身份变化→新组，
    不污染旧不可变组。
E10. degraded_reasons词汇与顺序冻结：image_missing/image_unreadable/
     image_hash_mismatch按「章序×decoded_ms升序、首见去重」的引用迭代序；
     image_embed_failed按渲染迭代轮次发现序追加。校验器按集合级不变式复验
     并保留存储顺序（E5使该顺序本身确定）。
E11. 渲染模板版式文本（全部进m2-document-view-1.0身份）：应急告示取裁决
     Q3.9冻结措辞逐字；DOCX页眉沿M1字面``f"{task_id} | 知识型视频结构化文档"``、
     页脚「第 <PAGE字段> 页」沿M1同构；MD章标题``## <index>. <title>``、
     节标题「概述」「结论」。
E12. 正文组的raw-responses.json与execution-report.json在导出复验中只按
     manifest记录做哈希钉住、不重放内容——字节防篡改，语义重放属composer
     组复验职责（裁决Q6.8分工）。
E13. 导出为零API本地模块：ModuleFailureAudit的business_call_count/
     supplier_request_count恒为0（P4§九17字段形态不变）。
E14. 缓存命中路径的image_count/image_omitted_count/status从既有组manifest
     读取，不重新渲染（14-C实现决定17「计数读组事实」同型）。
E15. 失败注入点为无模型阶段的4点（after_inputs/before_group_publish/
     after_group_publish/before_current），14-C决定16的语义切分；
     after_model_call/before_model_call在本模块不存在。

零真实API、零模型接触：本模块不读取任何角色配置；vision红线由「设计性关闭」
承载（裁决Q8.3）。M1目录与上游阶段11/12/13/14-A/14-B/14-C正式组只读。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Container, Iterator, Mapping, Sequence

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor

from video_doc.document_composer_v2 import (
    ARTIFACT_TYPE as CONTENT_ARTIFACT_TYPE,
    CONTENT_FILE,
    CONTENT_SCHEMA_NAME,
    CONTENT_SCHEMA_VERSION,
    CURRENT_FILE,
    CURRENT_SCHEMA_NAME,
    CURRENT_SCHEMA_VERSION,
    FAILURE_SCHEMA_NAME,
    FAILURE_SCHEMA_VERSION,
    GROUP_DIRECTORY,
    MANIFEST_FILE,
    MANIFEST_SCHEMA_NAME,
    MANIFEST_SCHEMA_VERSION,
    RAW_RESPONSES_FILE,
    REPORT_FILE,
    RESULT_ID_PREFIX as CONTENT_RESULT_ID_PREFIX,
    ROOT_DIRECTORY,
    _BLOCK_KEYS,
    _CHAPTER_KEYS,
    _CONTENT_TOP_KEYS,
    _CURRENT_KEYS,
    _FRAME_PROJECTION_KEY_SET,
    _MANIFEST_INPUT_KEYS,
    _MANIFEST_KEYS,
    _SOURCE_FRAME_SET_KEYS,
    _SOURCE_PLAN_KEYS,
)
from video_doc.frame_sampler import (
    FRAME_ARTIFACT_TYPE,
    FRAME_IMAGE_DIRECTORY,
    FRAME_INDEX_FILE,
    FRAME_MANIFEST_FILE,
    FRAME_MODULE_TOKEN,
    FRAME_SET_MANIFEST_SCHEMA_NAME,
    FRAME_SET_MANIFEST_SCHEMA_VERSION,
    FRAME_SET_SCHEMA_NAME,
    FRAME_SET_SCHEMA_VERSION,
    FrameSamplerError,
    artifact_group_path,
    is_safe_result_id,
    load_frame_set_current,
)
from video_doc.task_workspace import validate_task_id
from video_doc.unified_timeline import canonical_json_bytes, canonical_json_sha256

# ---------------------------------------------------------------------------
# 冻结身份字符串（裁决Q5/Q6，逐字）
# ---------------------------------------------------------------------------

EXPORTER_VERSION = "1.0"
EXPORT_ARTIFACT_TYPE = "document_export"
EXPORT_MODULE_TOKEN = "document_exporter_v2"
EXPORT_ROOT_DIRECTORY = "documents"
LOCK_FILE = "documents.lock"

RESULT_FILE = "result.json"
MARKDOWN_FILE = "result.md"
DOCX_FILE = "result.docx"
IMAGES_DIRECTORY = "images"
IMAGE_SUFFIX = ".png"

VIEW_VERSION = "m2-document-view-1.0"
JSON_RENDERER_VERSION = "m2-structured-document-json-1.0"
MARKDOWN_RENDERER_VERSION = "m2-structured-document-markdown-1.0"
DOCX_RENDERER_VERSION = "m2-structured-document-docx-1.1"
WORD_STYLE_VERSION = "m2-compact-reference-guide-1.0"
IMAGE_EMBED_POLICY_VERSION = "m2-document-image-embed-policy-1.0"

CACHE_SCHEMA_NAME = "DocumentExporterV2CacheIdentity"
CACHE_SCHEMA_VERSION = "1.0"

RESULT_ID_PREFIX = "export-"

# 裁决Q3.9冻结应急告示（逐字，进m2-document-view-1.0身份）
EMERGENCY_NOTICE = (
    "本文档由单章应急计划生成：分章模型调用失败，"
    "全部内容归入单章「完整内容」，不宣称分章成功。"
)

REASON_IMAGE_MISSING = "image_missing"
REASON_IMAGE_UNREADABLE = "image_unreadable"
REASON_IMAGE_HASH_MISMATCH = "image_hash_mismatch"
REASON_IMAGE_EMBED_FAILED = "image_embed_failed"
DEGRADED_REASON_KINDS = (
    REASON_IMAGE_MISSING,
    REASON_IMAGE_UNREADABLE,
    REASON_IMAGE_HASH_MISMATCH,
    REASON_IMAGE_EMBED_FAILED,
)

DOCX_SUBJECT = "知识型视频结构化文档"
DOCX_HEADER_SUFFIX = "| 知识型视频结构化文档"
SECTION_HEADING_OVERVIEW = "概述"
SECTION_HEADING_CONCLUSION = "结论"
DOCX_IMAGE_CAPTION_STYLE = "M2 Image Caption"
DOCX_NOTICE_STYLE = "Document Status"
IMAGE_EMBED_WIDTH = Cm(15.0)

# 命令级失败码（裁决Q6.10，恰11个）
ERROR_INPUT_TASK_MISSING = "INPUT_TASK_MISSING"
ERROR_INPUT_CONTENT_MISSING = "INPUT_CONTENT_MISSING"
ERROR_INPUT_CONTENT_CORRUPT = "INPUT_CONTENT_CORRUPT"
ERROR_INPUT_FRAME_SET_MISSING = "INPUT_FRAME_SET_MISSING"
ERROR_INPUT_FRAME_SET_CORRUPT = "INPUT_FRAME_SET_CORRUPT"
ERROR_INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
ERROR_CURRENT_POINTER_CORRUPT = "CURRENT_POINTER_CORRUPT"
ERROR_CACHE_CORRUPT = "CACHE_CORRUPT"
ERROR_CONCURRENT_RUN = "CONCURRENT_RUN"
ERROR_EXPORT_VALIDATION_FAILED = "EXPORT_VALIDATION_FAILED"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"

COMMAND_CODES = frozenset(
    {
        ERROR_INPUT_TASK_MISSING,
        ERROR_INPUT_CONTENT_MISSING,
        ERROR_INPUT_CONTENT_CORRUPT,
        ERROR_INPUT_FRAME_SET_MISSING,
        ERROR_INPUT_FRAME_SET_CORRUPT,
        ERROR_INPUT_IDENTITY_MISMATCH,
        ERROR_CURRENT_POINTER_CORRUPT,
        ERROR_CACHE_CORRUPT,
        ERROR_CONCURRENT_RUN,
        ERROR_EXPORT_VALIDATION_FAILED,
        ERROR_PUBLISH_FAILED,
    }
)

_EXPORT_INJECTION_POINTS = (
    "after_inputs",
    "before_group_publish",
    "after_group_publish",
    "before_current",
)

# 导出组manifest＝ArtifactGroupManifest 1.0＋degraded_reasons（裁决Q5.7，
# status词汇增量提交G1）
_EXPORT_MANIFEST_KEYS = _MANIFEST_KEYS | {"degraded_reasons"}
_EXPORT_MANIFEST_INPUT_KEYS = {
    "content_id",
    "content_manifest_sha256",
    "frame_set_id",
    "frame_set_manifest_sha256",
}
_EXPORT_MANIFEST_VERSION_KEYS = {
    "document_exporter_v2",
    "document_view",
    "structured_document_json",
    "structured_document_markdown",
    "structured_document_docx",
    "word_style",
    "image_embed_policy",
}
# 裁决Q6.6恰16键（逐字）
_EXPORT_CACHE_KEYS = {
    "schema_name",
    "schema_version",
    "task_id",
    "exporter_version",
    "content_id",
    "content_manifest_sha256",
    "content_document_sha256",
    "frame_set_id",
    "frame_set_manifest_sha256",
    "view_version",
    "json_renderer",
    "markdown_renderer",
    "docx_renderer",
    "word_style",
    "image_embed_policy",
    "python_docx",
}
# 裁决Q6.11 export成功摘要恰12键
_SUMMARY_KEYS = {
    "task_id",
    "module",
    "status",
    "result_id",
    "content_id",
    "chapter_count",
    "image_count",
    "image_omitted_count",
    "degraded_reasons",
    "cache_hit",
    "pointer_repaired",
    "group_relative_path",
}

_EXPORT_GROUP_FILE_NAMES = {RESULT_FILE, MARKDOWN_FILE, DOCX_FILE, MANIFEST_FILE, IMAGES_DIRECTORY}

# 帧集索引/manifest/帧条目精确键集（沿stage12 OCR输入复验先例，只读消费）
_FRAME_INDEX_KEYS = {
    "schema_name",
    "schema_version",
    "status",
    "frame_set_id",
    "task_id",
    "profile_id",
    "profile",
    "source_video",
    "sampling",
    "cache_identity_sha256",
    "frame_count",
    "frames",
}
_FRAME_ENTRY_KEYS = {
    "frame_id",
    "ordinal",
    "role",
    "planned_ms",
    "requested_ms",
    "decoded_ms",
    "is_first",
    "is_last",
    "file",
    "sha256",
    "size_bytes",
}
_FRAME_MANIFEST_KEYS = {
    "schema_name",
    "schema_version",
    "artifact_type",
    "task_id",
    "result_id",
    "status",
    "cache_identity_sha256",
    "inputs",
    "versions",
    "frame_count",
    "files",
}


# ---------------------------------------------------------------------------
# 类型化失败（基类字段沿14-B/14-C；11码恰与裁决Q6.10词表对齐）
# ---------------------------------------------------------------------------


class DocumentExporterV2Error(RuntimeError):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = False
    next_action = "none"


class InputTaskMissingError(DocumentExporterV2Error):
    error_code = ERROR_INPUT_TASK_MISSING
    recoverable = True
    next_action = "run_init_task"


class InputContentMissingError(DocumentExporterV2Error):
    error_code = ERROR_INPUT_CONTENT_MISSING
    recoverable = True
    next_action = "run_compose_document_v2"


class InputContentCorruptError(DocumentExporterV2Error):
    error_code = ERROR_INPUT_CONTENT_CORRUPT
    recoverable = False
    next_action = "none"


class InputFrameSetMissingError(DocumentExporterV2Error):
    error_code = ERROR_INPUT_FRAME_SET_MISSING
    recoverable = True
    next_action = "run_sample_frames"


class InputFrameSetCorruptError(DocumentExporterV2Error):
    error_code = ERROR_INPUT_FRAME_SET_CORRUPT
    recoverable = False
    next_action = "none"


class InputIdentityMismatchError(DocumentExporterV2Error):
    error_code = ERROR_INPUT_IDENTITY_MISMATCH
    recoverable = False
    next_action = "none"


class CurrentPointerCorruptError(DocumentExporterV2Error):
    error_code = ERROR_CURRENT_POINTER_CORRUPT
    recoverable = False
    next_action = "none"


class CacheCorruptError(DocumentExporterV2Error):
    error_code = ERROR_CACHE_CORRUPT
    recoverable = False
    next_action = "none"


class ConcurrentRunError(DocumentExporterV2Error):
    error_code = ERROR_CONCURRENT_RUN
    recoverable = True
    next_action = "retry_later"


class ExportValidationFailedError(DocumentExporterV2Error):
    error_code = ERROR_EXPORT_VALIDATION_FAILED
    recoverable = False
    next_action = "none"


class PublishFailedError(DocumentExporterV2Error):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = False
    next_action = "none"


class InjectedExportFailure(DocumentExporterV2Error):
    error_code = ERROR_PUBLISH_FAILED
    recoverable = False
    next_action = "none"


@dataclass(frozen=True)
class ExportInputs:
    """锁前只读输入快照（content＋frames两绑定对象，裁决Q6.6）。"""

    task_id: str
    content: dict[str, Any]
    content_bytes: bytes
    content_manifest_sha256: str
    frames_group: Path
    frame_files: Mapping[str, Mapping[str, Any]]
    frame_set_id: str
    frame_set_manifest_sha256: str
    result_source: str
    chapter_count: int


@dataclass(frozen=True)
class DocumentExportResult:
    """导出发布结果；``summary()``产出裁决Q6.11的精确12键CLI摘要。"""

    group_path: Path
    result_id: str
    content: Mapping[str, Any]
    manifest: Mapping[str, Any]
    status: str
    chapter_count: int
    image_count: int
    image_omitted_count: int
    degraded_reasons: tuple[str, ...]
    cache_hit: bool
    pointer_repaired: bool

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": str(self.content["task_id"]),
            "module": EXPORT_MODULE_TOKEN,
            "status": self.status,
            "result_id": self.result_id,
            "content_id": str(self.content["content_id"]),
            "chapter_count": self.chapter_count,
            "image_count": self.image_count,
            "image_omitted_count": self.image_omitted_count,
            "degraded_reasons": list(self.degraded_reasons),
            "cache_hit": self.cache_hit,
            "pointer_repaired": self.pointer_repaired,
            "group_relative_path": (
                f"m2/{EXPORT_ROOT_DIRECTORY}/{GROUP_DIRECTORY}/{self.result_id}"
            ),
        }


# ---------------------------------------------------------------------------
# 通用helpers（与14-C/14-B/F1逐点对齐，复制适配新基类）
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
    path: Path, label: str, error_type: type[DocumentExporterV2Error]
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise error_type(f"{label}无法读取或JSON无效。") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label}必须是JSON对象。")
    return value


def _current_path_state(
    path: Path, label: str, error_type: type[DocumentExporterV2Error]
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


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    )


def _reason(kind: str, frame_id: str) -> str:
    return f"{kind}:{frame_id}"


def _python_docx_version() -> str:
    try:
        return str(metadata.version("python-docx"))
    except metadata.PackageNotFoundError as exc:  # pragma: no cover - 依赖缺失
        raise ExportValidationFailedError(
            "python-docx运行时不可用。"
        ) from exc


def _image_file_name(frame_id: str) -> str:
    return f"{frame_id}{IMAGE_SUFFIX}"


def _image_relative_path(frame_id: str) -> str:
    return f"{IMAGES_DIRECTORY}/{_image_file_name(frame_id)}"


def _sorted_chapter_frames(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    """章内帧按decoded_ms升序、frame_id决胜（裁决Q5.6冻结位置语义，E11）。"""

    return sorted(
        [dict(frame) for frame in chapter["frames"]],
        key=lambda frame: (int(frame["decoded_ms"]), str(frame["frame_id"])),
    )


def _referenced_frame_ids(content: Mapping[str, Any]) -> list[str]:
    """引用迭代序＝章序×decoded_ms升序×首见去重（E10）。"""

    ordered: list[str] = []
    seen: set[str] = set()
    for chapter in content["chapters"]:
        for frame in _sorted_chapter_frames(chapter):
            frame_id = str(frame["frame_id"])
            if frame_id not in seen:
                seen.add(frame_id)
                ordered.append(frame_id)
    return ordered


# ---------------------------------------------------------------------------
# Markdown渲染器（m2-structured-document-markdown-1.0，E11/E14）
# ---------------------------------------------------------------------------


def render_export_markdown(
    content: Mapping[str, Any], embedded: Container[str]
) -> bytes:
    """由内容对象＋嵌入帧集合确定性渲染Markdown字节；不执行文件写入。"""

    lines: list[str] = []
    lines.append(f"# {content['title']}")
    lines.append("")
    if content["source_plan"]["result_source"] == "emergency":
        lines.append(EMERGENCY_NOTICE)
        lines.append("")
    lines.append(f"## {SECTION_HEADING_OVERVIEW}")
    lines.append("")
    for block in content["overview"]:
        lines.append(str(block["text"]))
        lines.append("")
    for chapter in content["chapters"]:
        lines.append(f"## {chapter['index']}. {chapter['title']}")
        lines.append("")
        for block in chapter["blocks"]:
            lines.append(str(block["text"]))
            lines.append("")
        for frame in _sorted_chapter_frames(chapter):
            frame_id = str(frame["frame_id"])
            if frame_id in embedded:
                lines.append(f"![{frame_id}]({_image_relative_path(frame_id)})")
                lines.append("")
            lines.append(str(frame["caption_text"]))
            lines.append("")
    lines.append(f"## {SECTION_HEADING_CONCLUSION}")
    lines.append("")
    for block in content["overall_conclusion"]:
        lines.append(str(block["text"]))
        lines.append("")
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# DOCX渲染器（m2-structured-document-docx-1.1＋m2-compact-reference-guide-1.0；
# M1 document_export.py样式/页面/页眉页脚/归档规化机制的M2独立实现，裁决Q5.6）
# ---------------------------------------------------------------------------


def _set_style_font(style: Any, font_name: str, east_asia: str, size: float) -> None:
    style.font.name = font_name
    style.font.size = Pt(size)
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:ascii"), font_name)
    fonts.set(qn("w:hAnsi"), font_name)
    fonts.set(qn("w:eastAsia"), east_asia)


def _configure_docx_styles(document: Any) -> None:
    styles = document.styles
    normal = styles["Normal"]
    _set_style_font(normal, "Calibri", "微软雅黑", 11)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.widow_control = True

    title = styles["Title"]
    _set_style_font(title, "Calibri", "微软雅黑", 24)
    title.font.bold = True
    title.font.color.rgb = RGBColor.from_string("0B2545")
    title.paragraph_format.space_before = Pt(0)
    title.paragraph_format.space_after = Pt(12)
    title.paragraph_format.keep_with_next = True

    for name, size, color, before, after in (
        ("Heading 1", 16, "2E74B5", 18, 10),
        ("Heading 2", 13, "2E74B5", 14, 7),
        ("Heading 3", 12, "1F4D78", 10, 5),
    ):
        style = styles[name]
        _set_style_font(style, "Calibri", "微软雅黑", size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.keep_together = True

    custom_styles = (
        (DOCX_NOTICE_STYLE, 10, "1F3A5F", 4, 8, 1.15),
        (DOCX_IMAGE_CAPTION_STYLE, 9, "555555", 0, 6, 1.0),
    )
    for name, size, color, before, after, spacing in custom_styles:
        style = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = normal
        _set_style_font(style, "Calibri", "微软雅黑", size)
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = spacing
        style.paragraph_format.widow_control = True

    for style_name in ("Header", "Footer"):
        style = styles[style_name]
        _set_style_font(style, "Calibri", "微软雅黑", 9)
        style.font.color.rgb = RGBColor.from_string("666666")


def _set_docx_page_geometry(document: Any) -> None:
    for section in document.sections:
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
        section.top_margin = Inches(1)
        section.right_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.header_distance = Inches(0.492)
        section.footer_distance = Inches(0.492)


def _add_page_field(paragraph: Any) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    display = OxmlElement("w:t")
    display.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instruction, separate, display, end):
        run._r.append(element)


def _configure_docx_header_footer(document: Any, task_id: str) -> None:
    for section in document.sections:
        header = section.header.paragraphs[0]
        header.style = document.styles["Header"]
        header.text = f"{task_id} {DOCX_HEADER_SUFFIX}"
        footer = section.footer.paragraphs[0]
        footer.style = document.styles["Footer"]
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer.add_run("第 ")
        _add_page_field(footer)
        footer.add_run(" 页")


def _normalize_docx_archive(payload: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(payload), "r")
    destination_buffer = io.BytesIO()
    with source, zipfile.ZipFile(
        destination_buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as destination:
        for name in sorted(source.namelist()):
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            destination.writestr(info, source.read(name))
    return destination_buffer.getvalue()


def render_export_docx(
    content: Mapping[str, Any], embedded: Mapping[str, bytes]
) -> tuple[bytes, list[str]]:
    """由内容对象＋帧字节映射确定性渲染DOCX字节；不执行文件写入。

    单帧``add_picture``异常时省略该图片、保留caption段并把frame_id计入返回值
    （裁决Q5.4/5.3；调用方按E5剪枝后重渲染至收敛）。
    """

    failed: list[str] = []
    task_id = str(content["task_id"])
    document = Document()
    _set_docx_page_geometry(document)
    _configure_docx_styles(document)
    _configure_docx_header_footer(document, task_id)
    core = document.core_properties
    core.title = str(content["title"])
    core.subject = DOCX_SUBJECT
    core.identifier = f"m2-content-sha256:{content['content_id']}"
    core.author = "video-doc"
    core.last_modified_by = "video-doc"
    core.created = datetime(2000, 1, 1)
    core.modified = datetime(2000, 1, 1)
    core.revision = 1

    title_paragraph = document.add_paragraph(style="Title")
    title_paragraph.add_run(str(content["title"]))
    if content["source_plan"]["result_source"] == "emergency":
        notice = document.add_paragraph(style=DOCX_NOTICE_STYLE)
        notice.add_run(EMERGENCY_NOTICE)

    def _heading(text: str) -> None:
        paragraph = document.add_paragraph(style="Heading 1")
        paragraph.add_run(text)

    def _body(text: str) -> None:
        paragraph = document.add_paragraph(style="Normal")
        paragraph.add_run(text)

    _heading(SECTION_HEADING_OVERVIEW)
    for block in content["overview"]:
        _body(str(block["text"]))
    for chapter in content["chapters"]:
        _heading(f"{chapter['index']}. {chapter['title']}")
        for block in chapter["blocks"]:
            _body(str(block["text"]))
        for frame in _sorted_chapter_frames(chapter):
            frame_id = str(frame["frame_id"])
            blob = embedded.get(frame_id)
            if blob is not None:
                image_paragraph = document.add_paragraph(style="Normal")
                # 阶段15-0裁决§二Q2排版修复：图像段与caption段保持同页相邻
                # （最小版式语义；样式、段落数量/顺序/内容与E5剪枝全部不动）。
                image_paragraph.paragraph_format.keep_with_next = True
                run = image_paragraph.add_run()
                try:
                    shape = run.add_picture(io.BytesIO(blob), width=IMAGE_EMBED_WIDTH)
                    shape._inline.docPr.set("descr", frame_id)
                except Exception:
                    failed.append(frame_id)
                    image_paragraph._p.getparent().remove(image_paragraph._p)
            caption = document.add_paragraph(style=DOCX_IMAGE_CAPTION_STYLE)
            caption.paragraph_format.keep_together = True
            caption.add_run(str(frame["caption_text"]))
    _heading(SECTION_HEADING_CONCLUSION)
    for block in content["overall_conclusion"]:
        _body(str(block["text"]))

    buffer = io.BytesIO()
    document.save(buffer)
    return _normalize_docx_archive(buffer.getvalue()), failed
# ---------------------------------------------------------------------------
# 导出形态正文复验（实现决定E3：键集＋来源身份＋确定性字节；不重跑validator-3.0）
# ---------------------------------------------------------------------------


def _validate_export_content_shape(
    content: Any, *, task_id: str, error_type: type[DocumentExporterV2Error]
) -> None:
    if not isinstance(content, dict):
        raise error_type("StructuredDocumentContent必须是JSON对象。")
    _exact_keys(content, _CONTENT_TOP_KEYS, "StructuredDocumentContent", error_type)
    if (
        content["schema_name"] != CONTENT_SCHEMA_NAME
        or content["schema_version"] != CONTENT_SCHEMA_VERSION
        or content["task_id"] != task_id
    ):
        raise error_type("正文Schema或任务身份无效。")
    content_id = content["content_id"]
    if (
        not isinstance(content_id, str)
        or not content_id.startswith(CONTENT_RESULT_ID_PREFIX)
        or len(content_id) != len(CONTENT_RESULT_ID_PREFIX) + 12
        or any(
            ch not in "0123456789abcdef"
            for ch in content_id[len(CONTENT_RESULT_ID_PREFIX) :]
        )
    ):
        raise error_type("正文content_id形态无效。")
    source_plan = content["source_plan"]
    if not isinstance(source_plan, dict):
        raise error_type("正文source_plan无效。")
    _exact_keys(source_plan, _SOURCE_PLAN_KEYS, "source_plan", error_type)
    if not _is_nonempty_str(source_plan["result_source"]):
        raise error_type("正文source_plan.result_source无效。")
    source_frames = content["source_frame_set"]
    if not isinstance(source_frames, dict):
        raise error_type("正文source_frame_set无效。")
    _exact_keys(source_frames, _SOURCE_FRAME_SET_KEYS, "source_frame_set", error_type)
    frame_count = source_frames["frame_count"]
    if not _is_int(frame_count) or frame_count < 1:
        raise error_type("正文source_frame_set.frame_count无效。")
    chapters = content["chapters"]
    if not isinstance(chapters, list) or len(chapters) < 1:
        raise error_type("正文chapters无效。")
    for key in ("overview", "overall_conclusion"):
        blocks = content[key]
        if not isinstance(blocks, list):
            raise error_type(f"正文{key}无效。")
        for block in blocks:
            _validate_block(block, error_type)
    seen_indexes: list[int] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise error_type("正文章节无效。")
        _exact_keys(chapter, _CHAPTER_KEYS, "正文章节", error_type)
        index = chapter["index"]
        if not _is_int(index):
            raise error_type("正文章节index无效。")
        seen_indexes.append(index)
        if not _is_nonempty_str(chapter["title"]):
            raise error_type("正文章节title无效。")
        if not _is_int(chapter["start_ms"]) or not _is_int(chapter["end_ms"]):
            raise error_type("正文章节时间边界无效。")
        blocks = chapter["blocks"]
        if not isinstance(blocks, list):
            raise error_type("正文章节blocks无效。")
        for block in blocks:
            _validate_block(block, error_type)
        frames = chapter["frames"]
        if not isinstance(frames, list):
            raise error_type("正文章节frames无效。")
        for frame in frames:
            if not isinstance(frame, dict):
                raise error_type("正文帧投影条目无效。")
            _exact_keys(frame, _FRAME_PROJECTION_KEY_SET, "正文帧投影条目", error_type)
            if (
                not _is_nonempty_str(frame["frame_id"])
                or not _is_int(frame["rank"])
                or not _is_int(frame["tier"])
                or not _is_int(frame["decoded_ms"])
                or not _is_nonempty_str(frame["caption_form"])
                or not _is_nonempty_str(frame["caption_text"])
            ):
                raise error_type("正文帧投影条目字段无效。")
        decoded_values = [int(frame["decoded_ms"]) for frame in frames]
        if len(decoded_values) != len(set(decoded_values)):
            raise error_type("正文章节frames存在重复decoded_ms。")
    if sorted(seen_indexes) != list(range(1, len(seen_indexes) + 1)):
        raise error_type("正文章节index必须为1..N连续整数。")


def _validate_block(block: Any, error_type: type[DocumentExporterV2Error]) -> None:
    if not isinstance(block, dict):
        raise error_type("正文内容块无效。")
    _exact_keys(block, _BLOCK_KEYS, "正文内容块", error_type)
    if (
        not _is_nonempty_str(block["block_id"])
        or not _is_nonempty_str(block["type"])
        or not _is_nonempty_str(block["text"])
    ):
        raise error_type("正文内容块字段无效。")


# ---------------------------------------------------------------------------
# 锁前输入加载（content＋frames绑定；实现决定E2/E3/E4）
# ---------------------------------------------------------------------------


def _load_task_record(output_root: Path, task_id: str) -> None:
    """task.json缺失/不可读/task_id不符一律INPUT_TASK_MISSING（14-C实现决定2同型）。"""

    task_json = output_root / task_id / "task.json"
    if not _current_path_state(task_json, "task.json", InputTaskMissingError):
        raise InputTaskMissingError(
            f"{ERROR_INPUT_TASK_MISSING}：任务工作区缺少task.json登记信息。"
        )
    record = _read_json(task_json, "task.json", InputTaskMissingError)
    if record.get("task_id") != task_id:
        raise InputTaskMissingError(
            f"{ERROR_INPUT_TASK_MISSING}：task.json记录的task_id与请求不一致。"
        )


def _load_content_current(content_root: Path) -> dict[str, Any] | None:
    path = content_root / CURRENT_FILE
    if not _current_path_state(path, "content current", InputContentCorruptError):
        return None
    pointer = _read_json(path, "content current", InputContentCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "content current", InputContentCorruptError)
    if (
        pointer["schema_name"] != CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != CONTENT_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：content current身份无效。"
        )
    group = artifact_group_path(content_root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：content current的组路径越出正式组目录。"
        )
    manifest_path = group / MANIFEST_FILE
    if (
        not manifest_path.is_file()
        or _file_sha256(manifest_path) != pointer["manifest_sha256"]
    ):
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：content current目标缺失或损坏。"
        )
    return pointer


def _load_content_inputs(
    m2_root: Path, task_id: str
) -> tuple[dict[str, Any], bytes, str, dict[str, Any]]:
    """加载正文导出组：指针、成员、键集形态、确定性字节与三文件哈希钉住（E3/E12）。"""

    content_root = m2_root / ROOT_DIRECTORY
    pointer = _load_content_current(content_root)
    if pointer is None:
        raise InputContentMissingError(
            f"{ERROR_INPUT_CONTENT_MISSING}：任务没有已发布的StructuredDocumentContent current。"
        )
    result_id = str(pointer["result_id"])
    group = artifact_group_path(content_root, GROUP_DIRECTORY, result_id)
    if group is None or not group.is_dir():
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：content current指向的组缺失或越界。"
        )
    members = {entry.name for entry in group.iterdir()}
    if members != {CONTENT_FILE, RAW_RESPONSES_FILE, REPORT_FILE, MANIFEST_FILE}:
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文结果组成员集合无效。"
        )
    content_bytes = (group / CONTENT_FILE).read_bytes()
    try:
        content = json.loads(content_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文无法读取或JSON无效。"
        ) from exc
    _validate_export_content_shape(content, task_id=task_id, error_type=InputContentCorruptError)
    if content["content_id"] != result_id:
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文content_id与current结果组不一致。"
        )
    if content_bytes != canonical_json_bytes(content):
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文JSON序列化不确定。"
        )
    manifest_path = group / MANIFEST_FILE
    manifest_bytes = manifest_path.read_bytes()
    if _sha256_bytes(manifest_bytes) != pointer["manifest_sha256"]:
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文manifest哈希不匹配。"
        )
    manifest = _read_json(manifest_path, "content manifest", InputContentCorruptError)
    _exact_keys(manifest, _MANIFEST_KEYS, "content manifest", InputContentCorruptError)
    if (
        manifest["schema_name"] != MANIFEST_SCHEMA_NAME
        or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION
        or manifest["artifact_type"] != CONTENT_ARTIFACT_TYPE
        or manifest["task_id"] != task_id
        or manifest["result_id"] != result_id
    ):
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文manifest身份无效。"
        )
    inputs_map = manifest["inputs"]
    if not isinstance(inputs_map, dict):
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文manifest.inputs无效。"
        )
    _exact_keys(inputs_map, _MANIFEST_INPUT_KEYS, "manifest.inputs", InputContentCorruptError)
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != {
        CONTENT_FILE,
        RAW_RESPONSES_FILE,
        REPORT_FILE,
    }:
        raise InputContentCorruptError(
            f"{ERROR_INPUT_CONTENT_CORRUPT}：正文manifest文件清单无效。"
        )
    for name in (CONTENT_FILE, RAW_RESPONSES_FILE, REPORT_FILE):
        record = files[name]
        if not isinstance(record, dict) or set(record) != {"sha256", "bytes"}:
            raise InputContentCorruptError(
                f"{ERROR_INPUT_CONTENT_CORRUPT}：正文manifest文件记录无效。"
            )
        payload = (group / name).read_bytes()
        if record.get("sha256") != _sha256_bytes(payload) or record.get("bytes") != len(payload):
            raise InputContentCorruptError(
                f"{ERROR_INPUT_CONTENT_CORRUPT}：正文组文件磁盘哈希与manifest不一致。"
            )
    return content, content_bytes, str(pointer["manifest_sha256"]), inputs_map


def _load_frame_inputs(
    output_root: Path, task_id: str
) -> tuple[dict[str, Any], Path, dict[str, dict[str, Any]], str]:
    """只读复验帧集组（沿阶段12 OCR``_load_frame_set``电池），返回index、组路径、
    frame_id→帧条目映射与frames manifest哈希（实现决定E4）。"""

    frames_root = output_root / task_id / "m2" / FRAME_MODULE_TOKEN
    try:
        pointer = load_frame_set_current(output_root, task_id)
    except FrameSamplerError as exc:
        raise InputFrameSetCorruptError(f"{ERROR_INPUT_FRAME_SET_CORRUPT}：{exc}") from exc
    if pointer is None:
        raise InputFrameSetMissingError(
            f"{ERROR_INPUT_FRAME_SET_MISSING}：任务没有已发布的frame set current。"
        )
    result_id = str(pointer["result_id"])
    group = artifact_group_path(frames_root, GROUP_DIRECTORY, result_id)
    if group is None or not group.is_dir():
        raise InputFrameSetCorruptError(
            f"{ERROR_INPUT_FRAME_SET_CORRUPT}：frames current指向的组缺失或越界。"
        )
    members = {entry.name for entry in group.iterdir()}
    if members != {FRAME_INDEX_FILE, FRAME_MANIFEST_FILE, FRAME_IMAGE_DIRECTORY}:
        raise InputFrameSetCorruptError(
            f"{ERROR_INPUT_FRAME_SET_CORRUPT}：帧集成员集合无效。"
        )
    manifest_path = group / FRAME_MANIFEST_FILE
    manifest_bytes = manifest_path.read_bytes()
    if _sha256_bytes(manifest_bytes) != pointer["manifest_sha256"]:
        raise InputFrameSetCorruptError(
            f"{ERROR_INPUT_FRAME_SET_CORRUPT}：frames manifest哈希不匹配。"
        )
    manifest = _read_json(manifest_path, "frames manifest", InputFrameSetCorruptError)
    index_path = group / FRAME_INDEX_FILE
    index = _read_json(index_path, "frame set", InputFrameSetCorruptError)
    _exact_keys(index, _FRAME_INDEX_KEYS, "frame set", InputFrameSetCorruptError)
    _exact_keys(manifest, _FRAME_MANIFEST_KEYS, "frames manifest", InputFrameSetCorruptError)
    if (
        index["schema_name"] != FRAME_SET_SCHEMA_NAME
        or index["schema_version"] != FRAME_SET_SCHEMA_VERSION
        or index["status"] != "success"
        or index["frame_set_id"] != result_id
        or index["task_id"] != task_id
        or manifest["schema_name"] != FRAME_SET_MANIFEST_SCHEMA_NAME
        or manifest["schema_version"] != FRAME_SET_MANIFEST_SCHEMA_VERSION
        or manifest["artifact_type"] != FRAME_ARTIFACT_TYPE
        or manifest["result_id"] != result_id
        or manifest["task_id"] != task_id
    ):
        raise InputFrameSetCorruptError(
            f"{ERROR_INPUT_FRAME_SET_CORRUPT}：帧集Schema或来源身份无效。"
        )
    frame_json = lambda payload: (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if index_path.read_bytes() != frame_json(index) or manifest_bytes != frame_json(manifest):
        raise InputFrameSetCorruptError(
            f"{ERROR_INPUT_FRAME_SET_CORRUPT}：帧集JSON序列化不确定。"
        )
    frames = index["frames"]
    if not isinstance(frames, list) or index["frame_count"] != len(frames) or len(frames) < 1:
        raise InputFrameSetCorruptError(
            f"{ERROR_INPUT_FRAME_SET_CORRUPT}：帧数量无效。"
        )
    previous_decoded_ms = -1
    file_names: set[str] = set()
    frame_files: dict[str, dict[str, Any]] = {}
    for ordinal, frame in enumerate(frames, start=1):
        if not isinstance(frame, dict):
            raise InputFrameSetCorruptError(
                f"{ERROR_INPUT_FRAME_SET_CORRUPT}：帧条目无效。"
            )
        _exact_keys(frame, _FRAME_ENTRY_KEYS, "帧条目", InputFrameSetCorruptError)
        expected_file = f"{FRAME_IMAGE_DIRECTORY}/fr{ordinal:06d}.png"
        if (
            frame["ordinal"] != ordinal
            or frame["frame_id"] != f"fr{ordinal:06d}"
            or frame["file"] != expected_file
            or not _safe_relative_file(frame["file"])
            or not _is_int(frame["decoded_ms"])
            or frame["decoded_ms"] <= previous_decoded_ms
            or frame["frame_id"] in frame_files
        ):
            raise InputFrameSetCorruptError(
                f"{ERROR_INPUT_FRAME_SET_CORRUPT}：帧身份、路径或时间顺序无效。"
            )
        frame_files[str(frame["frame_id"])] = frame
        file_names.add(str(frame["file"]))
        previous_decoded_ms = frame["decoded_ms"]
    manifest_files = manifest["files"]
    if (
        not isinstance(manifest_files, dict)
        or set(manifest_files) != file_names | {FRAME_INDEX_FILE}
    ):
        raise InputFrameSetCorruptError(
            f"{ERROR_INPUT_FRAME_SET_CORRUPT}：帧集manifest文件清单无效。"
        )
    for frame in frames:
        recorded = manifest_files.get(str(frame["file"]))
        if (
            not isinstance(recorded, dict)
            or recorded.get("sha256") != frame["sha256"]
            or recorded.get("bytes") != frame["size_bytes"]
        ):
            raise InputFrameSetCorruptError(
                f"{ERROR_INPUT_FRAME_SET_CORRUPT}：manifest与帧条目身份不一致。"
            )
    return index, group, frame_files, str(pointer["manifest_sha256"])


def _load_export_inputs(output: Path, task_id: str) -> ExportInputs:
    """content＋frames绑定加载（裁决Q6.6两输入契约；E2/E3/E4）。"""

    m2_root = output / task_id / "m2"
    content, content_bytes, content_manifest_sha256, _content_inputs_map = (
        _load_content_inputs(m2_root, task_id)
    )
    frames_index, frames_group, frame_files, frame_set_manifest_sha256 = (
        _load_frame_inputs(output, task_id)
    )
    source_frames = content["source_frame_set"]
    if (
        str(source_frames["frame_set_id"]) != frames_index["frame_set_id"]
        or str(source_frames["manifest_sha256"]) != frame_set_manifest_sha256
    ):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：正文source_frame_set与frames current绑定不一致。"
        )
    if int(source_frames["frame_count"]) != int(frames_index["frame_count"]):
        raise InputIdentityMismatchError(
            f"{ERROR_INPUT_IDENTITY_MISMATCH}：正文绑定帧数与帧集frame_count不一致。"
        )
    for chapter in content["chapters"]:
        for frame in chapter["frames"]:
            if str(frame["frame_id"]) not in frame_files:
                raise InputFrameSetCorruptError(
                    f"{ERROR_INPUT_FRAME_SET_CORRUPT}：正文引用的frame_id未在帧集索引登记"
                    f"（{frame['frame_id']}）。"
                )
    return ExportInputs(
        task_id=task_id,
        content=content,
        content_bytes=content_bytes,
        content_manifest_sha256=content_manifest_sha256,
        frames_group=frames_group,
        frame_files=frame_files,
        frame_set_id=str(frames_index["frame_set_id"]),
        frame_set_manifest_sha256=frame_set_manifest_sha256,
        result_source=str(content["source_plan"]["result_source"]),
        chapter_count=len(content["chapters"]),
    )


def _collect_frame_bytes(
    inputs: ExportInputs, referenced: Sequence[str]
) -> tuple[dict[str, bytes], list[str]]:
    """逐引用帧读取磁盘字节并核验哈希；返回可嵌入映射与采集降级reason（E4/E10）。"""

    embedded: dict[str, bytes] = {}
    reasons: list[str] = []
    for frame_id in referenced:
        entry = inputs.frame_files[frame_id]
        path = inputs.frames_group / str(entry["file"])
        if not path.is_file():
            reasons.append(_reason(REASON_IMAGE_MISSING, frame_id))
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            reasons.append(_reason(REASON_IMAGE_UNREADABLE, frame_id))
            continue
        if _sha256_bytes(payload) != entry["sha256"]:
            reasons.append(_reason(REASON_IMAGE_HASH_MISMATCH, frame_id))
            continue
        embedded[frame_id] = payload
    return embedded, reasons


def _export_cache_identity(inputs: ExportInputs) -> tuple[dict[str, Any], str]:
    """裁决Q6.6恰16键导出缓存身份（E9：python_docx进身份不进字节）。"""

    identity = {
        "schema_name": CACHE_SCHEMA_NAME,
        "schema_version": CACHE_SCHEMA_VERSION,
        "task_id": inputs.task_id,
        "exporter_version": EXPORTER_VERSION,
        "content_id": str(inputs.content["content_id"]),
        "content_manifest_sha256": inputs.content_manifest_sha256,
        "content_document_sha256": _sha256_bytes(inputs.content_bytes),
        "frame_set_id": inputs.frame_set_id,
        "frame_set_manifest_sha256": inputs.frame_set_manifest_sha256,
        "view_version": VIEW_VERSION,
        "json_renderer": JSON_RENDERER_VERSION,
        "markdown_renderer": MARKDOWN_RENDERER_VERSION,
        "docx_renderer": DOCX_RENDERER_VERSION,
        "word_style": WORD_STYLE_VERSION,
        "image_embed_policy": IMAGE_EMBED_POLICY_VERSION,
        "python_docx": _python_docx_version(),
    }
    if set(identity) != _EXPORT_CACHE_KEYS:
        raise AssertionError("导出缓存身份键集偏离冻结16键。")
    return identity, canonical_json_sha256(identity)
# ---------------------------------------------------------------------------
# 导出组manifest构建与组复验（裁决Q5.7/Q6.8；实现决定E5/E6/E10/E12）
# ---------------------------------------------------------------------------


def _build_export_manifest(
    inputs: ExportInputs,
    *,
    result_id: str,
    cache_sha256: str,
    status: str,
    degraded_reasons: Sequence[str],
    file_bytes: Mapping[str, bytes],
) -> dict[str, Any]:
    return {
        "schema_name": MANIFEST_SCHEMA_NAME,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact_type": EXPORT_ARTIFACT_TYPE,
        "task_id": inputs.task_id,
        "result_id": result_id,
        "status": status,
        "cache_identity_sha256": cache_sha256,
        "inputs": {
            "content_id": str(inputs.content["content_id"]),
            "content_manifest_sha256": inputs.content_manifest_sha256,
            "frame_set_id": inputs.frame_set_id,
            "frame_set_manifest_sha256": inputs.frame_set_manifest_sha256,
        },
        "versions": {
            "document_exporter_v2": EXPORTER_VERSION,
            "document_view": VIEW_VERSION,
            "structured_document_json": JSON_RENDERER_VERSION,
            "structured_document_markdown": MARKDOWN_RENDERER_VERSION,
            "structured_document_docx": DOCX_RENDERER_VERSION,
            "word_style": WORD_STYLE_VERSION,
            "image_embed_policy": IMAGE_EMBED_POLICY_VERSION,
        },
        "files": {
            name: {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
            for name, payload in sorted(file_bytes.items())
        },
        "degraded_reasons": list(degraded_reasons),
    }


def _parse_degraded_reason(
    value: Any, error_type: type[DocumentExporterV2Error]
) -> tuple[str, str]:
    if not isinstance(value, str) or ":" not in value:
        raise error_type("degraded_reasons条目形态无效。")
    kind, _, frame_id = value.partition(":")
    if kind not in DEGRADED_REASON_KINDS or not frame_id:
        raise error_type("degraded_reasons词汇无效。")
    if len(frame_id) != 8 or not frame_id.startswith("fr"):
        raise error_type("degraded_reasons帧标识无效。")
    if any(ch not in "0123456789" for ch in frame_id[2:]):
        raise error_type("degraded_reasons帧标识无效。")
    return kind, frame_id


def validate_document_export_group(
    group_path: str | Path,
    *,
    task_id: str,
    cache_sha256: str,
    content_manifest_sha256: str,
    frame_set_id: str,
    frame_set_manifest_sha256: str,
    expected_result_id: str | None = None,
    require_directory_name: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """复验导出正式组（裁决Q6.8确定性口径＋E5/E6/E10）：成员恰五、manifest规范
    字节与钉住身份、result.json逐字节＝正文规范字节、渲染器对磁盘字节重渲染
    逐字节一致（DOCX收敛轮重放零新增失败）、引用集＝images∪降级不变式、
    逐文件磁盘哈希。"""

    group = Path(group_path)
    if not group.is_dir():
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出结果组目录不存在。")
    members = {entry.name for entry in group.iterdir()}
    if members != _EXPORT_GROUP_FILE_NAMES:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出结果组成员集合无效。")
    images_dir = group / IMAGES_DIRECTORY
    if not images_dir.is_dir():
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出结果组缺少images目录。")
    image_entries = sorted(entry.name for entry in images_dir.iterdir())
    present_ids: list[str] = []
    for name in image_entries:
        # 成员名＝"fr"＋6位数字（8字符，与_parse_degraded_reason同口径）＋".png"
        if not name.endswith(IMAGE_SUFFIX) or len(name) != 12:
            raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：images目录含非法成员。")
        frame_id = name[: -len(IMAGE_SUFFIX)]
        if not frame_id.startswith("fr") or any(
            ch not in "0123456789" for ch in frame_id[2:]
        ):
            raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：images目录含非法成员。")
        if not (images_dir / name).is_file():
            raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：images成员不是普通文件。")
        present_ids.append(frame_id)

    manifest_path = group / MANIFEST_FILE
    manifest_bytes = manifest_path.read_bytes()
    manifest = _read_json(manifest_path, "export manifest", CacheCorruptError)
    _exact_keys(manifest, _EXPORT_MANIFEST_KEYS, "export manifest", CacheCorruptError)
    if manifest_bytes != _json_bytes(manifest):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出manifest序列化不确定。")
    if (
        manifest["schema_name"] != MANIFEST_SCHEMA_NAME
        or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION
        or manifest["artifact_type"] != EXPORT_ARTIFACT_TYPE
        or manifest["task_id"] != task_id
        or not is_safe_result_id(manifest["result_id"])
        or manifest["cache_identity_sha256"] != cache_sha256
        or manifest["status"] not in {"success", "degraded"}
    ):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出manifest身份无效。")
    if require_directory_name and group.name != manifest["result_id"]:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出组目录名与manifest不一致。")
    if expected_result_id is not None and manifest["result_id"] != expected_result_id:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出组result_id与期望不一致。")
    if require_directory_name:
        if not (group.parent.name == GROUP_DIRECTORY and is_safe_result_id(group.name)):
            raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出组目录位置无效。")

    manifest_inputs = manifest["inputs"]
    if not isinstance(manifest_inputs, dict):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出manifest.inputs无效。")
    _exact_keys(manifest_inputs, _EXPORT_MANIFEST_INPUT_KEYS, "manifest.inputs", CacheCorruptError)
    if (
        manifest_inputs["content_manifest_sha256"] != content_manifest_sha256
        or manifest_inputs["frame_set_id"] != frame_set_id
        or manifest_inputs["frame_set_manifest_sha256"] != frame_set_manifest_sha256
    ):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出绑定身份与本次请求不一致。")

    manifest_versions = manifest["versions"]
    if not isinstance(manifest_versions, dict):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出manifest.versions无效。")
    _exact_keys(
        manifest_versions, _EXPORT_MANIFEST_VERSION_KEYS, "manifest.versions", CacheCorruptError
    )
    if manifest_versions != {
        "document_exporter_v2": EXPORTER_VERSION,
        "document_view": VIEW_VERSION,
        "structured_document_json": JSON_RENDERER_VERSION,
        "structured_document_markdown": MARKDOWN_RENDERER_VERSION,
        "structured_document_docx": DOCX_RENDERER_VERSION,
        "word_style": WORD_STYLE_VERSION,
        "image_embed_policy": IMAGE_EMBED_POLICY_VERSION,
    }:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出版本族与冻结值不一致。")

    files = manifest["files"]
    if not isinstance(files, dict):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出manifest.files无效。")
    expected_names = {RESULT_FILE, MARKDOWN_FILE, DOCX_FILE} | {
        _image_relative_path(frame_id) for frame_id in present_ids
    }
    if set(files) != expected_names:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出文件清单与磁盘成员不一致。")
    for name in files:
        record = files[name]
        if not isinstance(record, dict) or set(record) != {"sha256", "bytes"}:
            raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出文件记录无效。")
        payload = (group / name).read_bytes()
        if record.get("sha256") != _sha256_bytes(payload) or record.get("bytes") != len(payload):
            raise CacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：导出组文件磁盘哈希与manifest不一致。"
            )

    result_bytes = (group / RESULT_FILE).read_bytes()
    try:
        content = json.loads(result_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：result.json无法读取或JSON无效。") from exc
    _validate_export_content_shape(content, task_id=task_id, error_type=CacheCorruptError)
    if result_bytes != canonical_json_bytes(content):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：result.json序列化不确定。")
    if str(content["content_id"]) != str(manifest_inputs["content_id"]):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：result.json与manifest绑定content_id不一致。")
    source_frames = content["source_frame_set"]
    if (
        str(source_frames["frame_set_id"]) != frame_set_id
        or str(source_frames["manifest_sha256"]) != frame_set_manifest_sha256
    ):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：result.json帧集绑定与请求不一致。")

    degraded = manifest["degraded_reasons"]
    if not isinstance(degraded, list):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：degraded_reasons必须是列表。")
    parsed_reasons = [_parse_degraded_reason(item, CacheCorruptError) for item in degraded]
    reason_ids = [frame_id for _kind, frame_id in parsed_reasons]
    if len(set(reason_ids)) != len(reason_ids):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：degraded_reasons含重复帧。")
    if (manifest["status"] == "degraded") != (len(degraded) > 0):
        raise CacheCorruptError(f"{ERROR_CACHE_CORRUPT}：导出status与degraded_reasons不一致。")

    referenced = _referenced_frame_ids(content)
    referenced_set = set(referenced)
    present_set = set(present_ids)
    reason_set = set(reason_ids)
    if (
        present_set & reason_set
        or referenced_set != present_set | reason_set
    ):
        raise CacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：引用帧集合与images∪降级集合不变式不一致。"
        )
    kinds = [kind for kind, _fid in parsed_reasons]
    collection_frames = [fid for kind, fid in parsed_reasons if kind != REASON_IMAGE_EMBED_FAILED]
    expected_collection = [fid for fid in referenced if fid in set(collection_frames)]
    if collection_frames != expected_collection:
        raise CacheCorruptError(
            f"{ERROR_CACHE_CORRUPT}：采集类degraded_reasons顺序偏离引用迭代序。"
        )
    if REASON_IMAGE_EMBED_FAILED in kinds:
        first_embed = kinds.index(REASON_IMAGE_EMBED_FAILED)
        if any(kind != REASON_IMAGE_EMBED_FAILED for kind in kinds[first_embed:]):
            raise CacheCorruptError(
                f"{ERROR_CACHE_CORRUPT}：image_embed_failed类reason未连续排在采集类之后。"
            )

    markdown_bytes = (group / MARKDOWN_FILE).read_bytes()
    if markdown_bytes != render_export_markdown(content, embedded=present_set):
        raise ExportValidationFailedError(
            f"{ERROR_EXPORT_VALIDATION_FAILED}：result.md重渲染与磁盘字节不一致。"
        )
    embedded_map: dict[str, bytes] = {}
    for frame_id in present_ids:
        embedded_map[frame_id] = (images_dir / _image_file_name(frame_id)).read_bytes()
    docx_bytes, failed = render_export_docx(content, embedded=embedded_map)
    if failed:
        raise ExportValidationFailedError(
            f"{ERROR_EXPORT_VALIDATION_FAILED}：result.docx重渲染出现新增嵌入失败"
            f"（{failed[0]}），发布未收敛至不动点。"
        )
    if docx_bytes != (group / DOCX_FILE).read_bytes():
        raise ExportValidationFailedError(
            f"{ERROR_EXPORT_VALIDATION_FAILED}：result.docx重渲染与磁盘字节不一致。"
        )
    return content, manifest
# ---------------------------------------------------------------------------
# current指针、锁、审计与发布机器（逐字沿14-C document_composer_v2先例）
# ---------------------------------------------------------------------------


def _current_payload(result_id: str, manifest_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_name": CURRENT_SCHEMA_NAME,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "artifact_type": EXPORT_ARTIFACT_TYPE,
        "result_id": result_id,
        "group": f"{GROUP_DIRECTORY}/{result_id}",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
    }


def _load_export_current(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = root / CURRENT_FILE
    if not _current_path_state(path, "documents current", CurrentPointerCorruptError):
        return None, None
    pointer = _read_json(path, "documents current", CurrentPointerCorruptError)
    _exact_keys(pointer, _CURRENT_KEYS, "documents current", CurrentPointerCorruptError)
    if (
        pointer["schema_name"] != CURRENT_SCHEMA_NAME
        or pointer["schema_version"] != CURRENT_SCHEMA_VERSION
        or pointer["artifact_type"] != EXPORT_ARTIFACT_TYPE
        or not is_safe_result_id(pointer["result_id"])
        or pointer["group"] != f"{GROUP_DIRECTORY}/{pointer['result_id']}"
    ):
        raise CurrentPointerCorruptError(
            f"{ERROR_CURRENT_POINTER_CORRUPT}：documents current身份无效。"
        )
    group = artifact_group_path(root, GROUP_DIRECTORY, str(pointer["result_id"]))
    if group is None:
        raise CurrentPointerCorruptError(
            f"{ERROR_CURRENT_POINTER_CORRUPT}：documents current的组路径越出正式组目录。"
        )
    manifest_path = group / MANIFEST_FILE
    if (
        not manifest_path.is_file()
        or _file_sha256(manifest_path) != pointer["manifest_sha256"]
    ):
        raise CurrentPointerCorruptError(
            f"{ERROR_CURRENT_POINTER_CORRUPT}：documents current目标缺失或损坏。"
        )
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
def _export_lock(m2_root: Path, task_id: str) -> Iterator[None]:
    locks = m2_root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / LOCK_FILE
    payload = _json_bytes(
        {
            "schema_name": "ModuleLock",
            "schema_version": "1.0",
            "task_id": task_id,
            "module": EXPORT_MODULE_TOKEN,
            "pid": os.getpid(),
        }
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise ConcurrentRunError(f"{ERROR_CONCURRENT_RUN}：documents锁已存在。") from exc
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
        raise InjectedExportFailure(f"注入失败：{point}")


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
) -> str:
    failure_id = (
        f"failure-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    )
    root = m2_root / "failures" / EXPORT_MODULE_TOKEN / failure_id
    root.mkdir(parents=True, exist_ok=False)
    code = getattr(error, "error_code", ERROR_PUBLISH_FAILED)
    message = (
        str(error)
        if isinstance(error, DocumentExporterV2Error)
        else f"导出失败：{type(error).__name__}"
    )
    payload = {
        "schema_name": FAILURE_SCHEMA_NAME,
        "schema_version": FAILURE_SCHEMA_VERSION,
        "failure_id": failure_id,
        "task_id": task_id,
        "module": EXPORT_MODULE_TOKEN,
        "stage": stage,
        "error_code": code,
        "safe_message": message,
        "input_ids": dict(input_ids),
        "cache_identity_sha256": cache_sha256,
        "previous_current_id": previous_current_id,
        "temporary_cleanup_status": cleanup_status,
        "business_call_count": 0,
        "supplier_request_count": 0,
        "current_unchanged": current_unchanged,
        "recoverable": bool(getattr(error, "recoverable", False)),
        "next_action": str(getattr(error, "next_action", "none")),
    }
    _write_fsync(root / "failure.json", _json_bytes(payload))
    return failure_id


def _counts_from_manifest(manifest: Mapping[str, Any]) -> tuple[int, int]:
    image_count = sum(1 for name in manifest["files"] if name.startswith(IMAGES_DIRECTORY + "/"))
    return image_count, len(manifest["degraded_reasons"])


# ---------------------------------------------------------------------------
# 公开入口（裁决Q6.1/Q6.2；E1/E5/E14）
# ---------------------------------------------------------------------------


def export_document_v2(
    output_root: str | Path,
    task_id: str,
    *,
    failure_injection: str | None = None,
) -> DocumentExportResult:
    """导出并发布自包含M2三格式文档正式组（裁决Q5/Q6全部语义）。

    - 全程只读M1与上游content/frames正式组；产物仅写入``<output_root>/<task_id>/m2/documents/``；
    - 零API零模型：正文与帧集来自已发布current（E9/E13）；
    - 单帧嵌入失败按E5剪枝重渲染至不动点，组仍发布并切current（degraded语义，裁决Q5.4）。
    """

    if failure_injection is not None and failure_injection not in _EXPORT_INJECTION_POINTS:
        raise DocumentExporterV2Error("未知的导出失败注入点。")
    validate_task_id(task_id)
    output = Path(output_root).expanduser()
    m2_root = output / task_id / "m2"

    # --- 锁前：零写入、零审计（14-C先例；输入读取与绑定核验全部前置） ---
    _load_task_record(output, task_id)
    inputs = _load_export_inputs(output, task_id)

    root = m2_root / EXPORT_ROOT_DIRECTORY
    groups_dir = root / GROUP_DIRECTORY

    cache_sha256: str | None = None
    result_id: str | None = None
    previous_current_id: str | None = None
    initial_current_identity: tuple[str, str] | None = None
    group: Path | None = None
    temporary: Path | None = None
    created_group = False
    cleanup_status = "not_created"
    stage = "prepare"

    with _export_lock(m2_root, task_id):
        try:
            stage = "after_inputs"
            initial_current_identity = _disk_current_identity(root)
            _inject(failure_injection, "after_inputs")

            stage = "cache_identity"
            _, cache_sha256 = _export_cache_identity(inputs)

            stage = "current_validation"
            current_pointer, previous_current_id = _load_export_current(root)
            if current_pointer is not None:
                candidate = artifact_group_path(
                    root, GROUP_DIRECTORY, str(current_pointer["result_id"])
                )
                if candidate is None:
                    raise CurrentPointerCorruptError(
                        f"{ERROR_CURRENT_POINTER_CORRUPT}：documents current的组路径越界。"
                    )
                manifest_probe = _read_json(
                    candidate / MANIFEST_FILE, "export manifest", CacheCorruptError
                )
                if (
                    isinstance(manifest_probe.get("cache_identity_sha256"), str)
                    and manifest_probe["cache_identity_sha256"] == cache_sha256
                ):
                    content, manifest = validate_document_export_group(
                        candidate,
                        task_id=task_id,
                        cache_sha256=cache_sha256 or "",
                        content_manifest_sha256=inputs.content_manifest_sha256,
                        frame_set_id=inputs.frame_set_id,
                        frame_set_manifest_sha256=inputs.frame_set_manifest_sha256,
                    )
                    image_count, omitted_count = _counts_from_manifest(manifest)
                    return DocumentExportResult(
                        group_path=candidate,
                        result_id=str(current_pointer["result_id"]),
                        content=content,
                        manifest=manifest,
                        status=str(manifest["status"]),
                        chapter_count=inputs.chapter_count,
                        image_count=image_count,
                        image_omitted_count=omitted_count,
                        degraded_reasons=tuple(manifest["degraded_reasons"]),
                        cache_hit=True,
                        pointer_repaired=False,
                    )

            stage = "render"
            referenced = _referenced_frame_ids(inputs.content)
            embedded_map, degraded = _collect_frame_bytes(inputs, referenced)
            while True:
                docx_bytes, failed = render_export_docx(inputs.content, embedded=embedded_map)
                if not failed:
                    break
                new_failures = [frame_id for frame_id in failed if frame_id in embedded_map]
                if not new_failures:
                    raise ExportValidationFailedError(
                        f"{ERROR_EXPORT_VALIDATION_FAILED}：DOCX渲染迭代未收敛（E5不动点前提被破坏）。"
                    )
                for frame_id in new_failures:
                    degraded.append(_reason(REASON_IMAGE_EMBED_FAILED, frame_id))
                    embedded_map.pop(frame_id)
            markdown_bytes = render_export_markdown(inputs.content, embedded=set(embedded_map))

            stage = "group_publish"
            result_id = RESULT_ID_PREFIX + str(cache_sha256)[:12]
            status = "degraded" if degraded else "success"
            file_bytes: dict[str, bytes] = {
                RESULT_FILE: inputs.content_bytes,
                MARKDOWN_FILE: markdown_bytes,
                DOCX_FILE: docx_bytes,
            }
            for frame_id, blob in embedded_map.items():
                file_bytes[_image_relative_path(frame_id)] = blob
            manifest = _build_export_manifest(
                inputs,
                result_id=result_id,
                cache_sha256=cache_sha256 or "",
                status=status,
                degraded_reasons=degraded,
                file_bytes=file_bytes,
            )
            manifest_bytes = _json_bytes(manifest)
            group = root / GROUP_DIRECTORY / result_id

            def _validate_target(target: Path, *, require_name: bool) -> tuple[dict[str, Any], dict[str, Any]]:
                return validate_document_export_group(
                    target,
                    task_id=task_id,
                    cache_sha256=cache_sha256 or "",
                    content_manifest_sha256=inputs.content_manifest_sha256,
                    frame_set_id=inputs.frame_set_id,
                    frame_set_manifest_sha256=inputs.frame_set_manifest_sha256,
                    expected_result_id=result_id,
                    require_directory_name=require_name,
                )

            if group.exists():
                # 裁决Q6.8：result.json与manifest逐字节强一致；其余文件由完整
                # 组复验（重渲染逐字节）钉死既有磁盘字节。
                result_file = group / RESULT_FILE
                if (
                    not result_file.is_file()
                    or result_file.read_bytes() != file_bytes[RESULT_FILE]
                ):
                    raise CacheCorruptError(
                        f"{ERROR_CACHE_CORRUPT}：同身份导出组内容与本次发布不一致。"
                    )
                content, manifest = _validate_target(group, require_name=True)
                reused = True
            else:
                reused = False
                groups_dir.mkdir(parents=True, exist_ok=True)
                temporary = groups_dir / f".tmp-{result_id}-{uuid.uuid4().hex}"
                temporary.mkdir(exist_ok=False)
                (temporary / IMAGES_DIRECTORY).mkdir(exist_ok=False)
                cleanup_status = "created"
                for name, payload in sorted(file_bytes.items()):
                    _write_fsync(temporary / name, payload)
                # manifest最后写（与F1/F2/OCR/13-1/14-B/14-A/14-C一致）。
                _write_fsync(temporary / MANIFEST_FILE, manifest_bytes)
                _validate_target(temporary, require_name=False)
                _inject(failure_injection, "before_group_publish")
                os.replace(temporary, group)
                created_group = True
                cleanup_status = "published"
                _inject(failure_injection, "after_group_publish")
                _validate_target(group, require_name=True)

            if reused:
                image_count, omitted_count = _counts_from_manifest(manifest)
                status = str(manifest["status"])
                degraded = list(manifest["degraded_reasons"])
            else:
                image_count = len(embedded_map)
                omitted_count = len(degraded)
                content = inputs.content

            # pointer_repaired仅在复用既有同身份组但current缺失/指向他组时置真（F.6先例）。
            pointer_repaired = reused and previous_current_id != result_id
            if previous_current_id != result_id:
                stage = "current_publish"
                if not reused:
                    _inject(failure_injection, "before_current")
                _replace_current(root, _current_payload(result_id, manifest_bytes))
            return DocumentExportResult(
                group_path=group,
                result_id=result_id,
                content=content,
                manifest=manifest,
                status=status,
                chapter_count=inputs.chapter_count,
                image_count=image_count,
                image_omitted_count=omitted_count,
                degraded_reasons=tuple(degraded),
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
            try:
                _write_failure(
                    m2_root,
                    task_id=task_id,
                    stage=stage,
                    error=exc,
                    input_ids={
                        "task_id": task_id,
                        "content_id": str(inputs.content["content_id"]),
                        "frame_set_id": inputs.frame_set_id,
                        "result_id": result_id,
                    },
                    cache_sha256=cache_sha256,
                    previous_current_id=previous_current_id,
                    cleanup_status=cleanup_status,
                    current_unchanged=current_identity == initial_current_identity,
                )
            except BaseException:
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, DocumentExporterV2Error):
                raise
            raise PublishFailedError(
                f"{ERROR_PUBLISH_FAILED}：无法发布M2文档导出组：{type(exc).__name__}"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
