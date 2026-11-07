"""Local web workflow for task management, document preview, editing and export.

The module intentionally keeps the existing command-line pipeline and immutable
machine result groups untouched. Web-only task metadata lives under
``<output_root>/_web`` and edited document versions live under
``<output_root>/<task_id>/edits``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence
from uuid import uuid4

from video_doc.document_composer_v2 import load_published_document_content
from video_doc.document_exporter_v2 import (
    _collect_frame_bytes,
    _load_export_inputs,
    _load_frame_inputs,
    _referenced_frame_ids,
    export_document_v2,
    render_export_docx,
    render_export_markdown,
)
from video_doc.task_workspace import (
    TaskWorkspaceError,
    initialize_task_workspace,
    load_task_manifest,
    validate_task_id,
)
from video_doc.video_probe import VideoProbeError, probe_video, save_metadata


TASK_STATE_SCHEMA_VERSION = "1.0"
EDIT_SCHEMA_VERSION = "2.0"
DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
EDIT_ID_PATTERN = re.compile(r"^editorial-[0-9a-f]{12}$")

STAGE_SEQUENCE: tuple[tuple[str, str, int], ...] = (
    ("preflight", "检查视频与运行环境", 4),
    ("task_init", "建立任务目录", 8),
    ("transcript", "提取语音内容", 14),
    ("audio", "提取音频", 19),
    ("asr", "语音识别", 27),
    ("timeline", "生成文本时间轴", 33),
    ("frames", "抽取视频画面", 41),
    ("page_changes", "检测页面变化", 48),
    ("ocr", "识别画面文字", 55),
    ("timeline_v2", "对齐语音与画面", 63),
    ("chapter_plan_v2", "规划文档章节", 70),
    ("chapter_frames", "选择章节配图", 77),
    ("document_content_v2", "生成图文正文", 87),
    ("export_v2", "生成可下载文档", 95),
    ("completed", "处理完成", 100),
)
STAGE_LABELS = {stage: label for stage, label, _ in STAGE_SEQUENCE}
STAGE_PROGRESS = {stage: progress for stage, _, progress in STAGE_SEQUENCE}
ACTIVE_STATUSES = {"queued", "running", "cancelling"}

_ERROR_MESSAGES = {
    "LLM_APPROVAL_REQUIRED": "当前缓存不足以完成生成，需要授权模型调用后重试。",
    "INPUT_BUDGET_EXCEEDED": "本次处理超过模型输入预算，请缩小视频或调整预算配置。",
    "INPUT_BUDGET_UNKNOWN": "无法估算模型输入预算，处理已在调用前停止。",
    "SOURCE_VIDEO_REQUIRED": "源视频不存在、不可读或未登记，请重新选择视频。",
    "SOURCE_VIDEO_MISMATCH": "当前源视频与任务登记的视频不一致。",
    "MEDIA_TOOL_NOT_FOUND": "未找到 FFmpeg/FFprobe，请安装后加入 PATH 或指定目录。",
    "MEDIA_TOOL_INVALID": "FFmpeg/FFprobe 不可执行，请检查安装目录。",
}


class WebServiceError(RuntimeError):
    """A user-facing API error with an HTTP status and stable code."""

    def __init__(self, message: str, *, status: int = 400, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Any) -> None:
    _write_bytes_atomic(path, _json_bytes(payload))


def _safe_group(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise WebServiceError("任务结果路径越界。", status=409, code="UNSAFE_RESULT_PATH")
    return candidate


def _compact_error(message: str, limit: int = 1200) -> str:
    text = " ".join(message.replace("\x00", "").split())
    return text[:limit]


def _latest_pipeline_failure(task_root: Path) -> dict[str, Any] | None:
    """Read the last structured failure from the CLI journal as a fallback."""

    journal = task_root / "m2" / "logs" / "run-m2.jsonl"
    if not journal.is_file():
        return None
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("status") != "failed":
            continue
        reason_codes = event.get("reason_codes")
        code = (
            str(reason_codes[0])
            if isinstance(reason_codes, list) and reason_codes
            else "PROCESS_FAILED"
        )
        return {
            "error_code": code,
            "failed_stage": event.get("stage", "unknown"),
            "reason_codes": reason_codes if isinstance(reason_codes, list) else [],
            "message": "",
        }
    return None


def _time_text(milliseconds: int | float | None) -> str:
    total_seconds = max(0, int(float(milliseconds or 0) // 1000))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _item_text(item: Mapping[str, Any]) -> str:
    if isinstance(item.get("text"), str):
        return item["text"]
    evidence = item.get("evidence")
    if isinstance(evidence, Mapping) and isinstance(evidence.get("text"), str):
        return evidence["text"]
    return ""


def _set_item_text(item: dict[str, Any], text: str) -> None:
    if isinstance(item.get("text"), str):
        item["text"] = text
        return
    evidence = item.get("evidence")
    if isinstance(evidence, dict) and isinstance(evidence.get("text"), str):
        evidence["text"] = text
        return
    item["text"] = text


def _editable_block(block: Mapping[str, Any], *, chapter_id: str | None = None) -> dict[str, Any]:
    block_type = str(block.get("type", "paragraph"))
    payload: dict[str, Any] = {
        "block_id": str(block.get("block_id", "")),
        "type": block_type,
        "role": str(block.get("role", "")),
        "chapter_id": chapter_id,
    }
    if block_type in {"paragraph", "callout"}:
        payload["text"] = str(block.get("text", ""))
    elif block_type == "list":
        payload["list_kind"] = str(block.get("list_kind", "bullet"))
        payload["items"] = [
            {
                "item_id": str(item.get("item_id", f"item-{index}")),
                "text": _item_text(item),
            }
            for index, item in enumerate(block.get("items", []), start=1)
            if isinstance(item, Mapping)
        ]
    elif block_type == "comparison":
        payload["columns"] = [str(value) for value in block.get("columns", [])]
        payload["rows"] = [
            {
                "row_id": str(row.get("row_id", f"row-{index}")),
                "dimension": str(row.get("dimension", "")),
                "cells": [
                    {"text": _item_text(cell)}
                    for cell in row.get("cells", [])
                    if isinstance(cell, Mapping)
                ],
            }
            for index, row in enumerate(block.get("rows", []), start=1)
            if isinstance(row, Mapping)
        ]
    else:
        payload["text"] = str(block.get("text", ""))
    return payload


def _renderable_edit(content: Mapping[str, Any]) -> dict[str, Any]:
    rendered = deepcopy(content)
    sections = [rendered["overview"], rendered["overall_conclusion"]]
    sections.extend(chapter["blocks"] for chapter in rendered["chapters"])
    for blocks in sections:
        for block in blocks:
            if block.get("type") == "list":
                block["text"] = "\n".join(
                    f"{index}. {item['text']}" if block.get("list_kind") == "numbered" else f"- {item['text']}"
                    for index, item in enumerate(block["items"], 1)
                )
            elif block.get("type") == "comparison":
                columns = block["columns"]
                block["text"] = "\n".join(
                    f"{row['dimension']}: " + " | ".join(
                        f"{column}: {cell['text']}" for column, cell in zip(columns, row["cells"])
                    )
                    for row in block["rows"]
                )
    return rendered


def _editable_document(content: Mapping[str, Any]) -> dict[str, Any]:
    chapters: list[dict[str, Any]] = []
    for chapter in content.get("chapters", []):
        if not isinstance(chapter, Mapping):
            continue
        chapter_id = str(chapter.get("chapter_id", ""))
        frames = []
        for frame in chapter.get("frames", []):
            if not isinstance(frame, Mapping):
                continue
            frame_id = str(frame.get("frame_id", ""))
            frames.append(
                {
                    "frame_id": frame_id,
                    "decoded_ms": int(frame.get("decoded_ms", 0)),
                    "decoded_text": _time_text(frame.get("decoded_ms", 0)),
                    "caption_text": str(frame.get("caption_text", "")),
                    "image_url": f"/api/tasks/{content.get('task_id', '')}/frames/{frame_id}",
                }
            )
        chapters.append(
            {
                "index": int(chapter.get("index", len(chapters) + 1)),
                "chapter_id": chapter_id,
                "title": str(chapter.get("title", "")),
                "start_ms": int(chapter.get("start_ms", 0)),
                "end_ms": int(chapter.get("end_ms", 0)),
                "start_text": _time_text(chapter.get("start_ms", 0)),
                "end_text": _time_text(chapter.get("end_ms", 0)),
                "blocks": [
                    _editable_block(block, chapter_id=chapter_id)
                    for block in chapter.get("blocks", [])
                    if isinstance(block, Mapping)
                ],
                "frames": frames,
            }
        )
    return {
        "title": str(content.get("title", "")),
        "overview": [
            _editable_block(block)
            for block in content.get("overview", [])
            if isinstance(block, Mapping)
        ],
        "overall_conclusion": [
            _editable_block(block)
            for block in content.get("overall_conclusion", [])
            if isinstance(block, Mapping)
        ],
        "chapters": chapters,
        "keywords": [
            _item_text(item)
            for item in content.get("global_keywords", [])
            if isinstance(item, Mapping) and _item_text(item)
        ],
    }


def _validate_text(value: Any, location: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise WebServiceError(f"{location}必须是文本。", code="INVALID_EDIT")
    if len(value) > 100_000:
        raise WebServiceError(f"{location}超过100000字符限制。", code="INVALID_EDIT")
    if not allow_empty and not value.strip():
        raise WebServiceError(f"{location}不能为空。", code="INVALID_EDIT")
    return value


def _apply_block_edit(block: dict[str, Any], edit: Mapping[str, Any]) -> None:
    block_type = str(block.get("type", "paragraph"))
    if block_type in {"paragraph", "callout"} or "text" in edit:
        if "text" in edit:
            block["text"] = _validate_text(edit["text"], "正文")
    if block_type == "list" and isinstance(edit.get("items"), list):
        originals = [
            item for item in block.get("items", []) if isinstance(item, dict)
        ]
        by_id = {str(item.get("item_id", "")): item for item in originals}
        for index, item_edit in enumerate(edit["items"]):
            if not isinstance(item_edit, Mapping):
                continue
            item = by_id.get(str(item_edit.get("item_id", "")))
            if item is None and index < len(originals):
                item = originals[index]
            if item is not None and "text" in item_edit:
                _set_item_text(item, _validate_text(item_edit["text"], "列表项"))
    if block_type == "comparison":
        if isinstance(edit.get("columns"), list):
            columns = block.get("columns", [])
            for index, value in enumerate(edit["columns"]):
                if index < len(columns):
                    columns[index] = _validate_text(value, "对比表列名")
        if isinstance(edit.get("rows"), list):
            originals = [
                row for row in block.get("rows", []) if isinstance(row, dict)
            ]
            by_id = {str(row.get("row_id", "")): row for row in originals}
            for index, row_edit in enumerate(edit["rows"]):
                if not isinstance(row_edit, Mapping):
                    continue
                row = by_id.get(str(row_edit.get("row_id", "")))
                if row is None and index < len(originals):
                    row = originals[index]
                if row is None:
                    continue
                if "dimension" in row_edit:
                    row["dimension"] = _validate_text(row_edit["dimension"], "对比表维度")
                cells = [cell for cell in row.get("cells", []) if isinstance(cell, dict)]
                for cell_index, cell_edit in enumerate(row_edit.get("cells", [])):
                    if (
                        cell_index < len(cells)
                        and isinstance(cell_edit, Mapping)
                        and "text" in cell_edit
                    ):
                        _set_item_text(
                            cells[cell_index],
                            _validate_text(cell_edit["text"], "对比表单元格"),
                        )


def _apply_blocks(
    originals: Sequence[Mapping[str, Any]], edits: Any, location: str
) -> None:
    if not isinstance(edits, list):
        return
    mutable = [block for block in originals if isinstance(block, dict)]
    by_id = {str(block.get("block_id", "")): block for block in mutable}
    for index, edit in enumerate(edits):
        if not isinstance(edit, Mapping):
            continue
        block = by_id.get(str(edit.get("block_id", "")))
        if block is None and index < len(mutable):
            block = mutable[index]
        if block is not None:
            _apply_block_edit(block, edit)
        if not block:
            raise WebServiceError(f"{location}包含无法匹配的正文块。", code="INVALID_EDIT")


def _apply_editable(content: Mapping[str, Any], editable: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(content)
    result["title"] = _validate_text(editable.get("title"), "文档标题", allow_empty=False)
    _apply_blocks(result.get("overview", []), editable.get("overview"), "概述")
    _apply_blocks(
        result.get("overall_conclusion", []),
        editable.get("overall_conclusion"),
        "全文结论",
    )

    edits = editable.get("chapters")
    if not isinstance(edits, list):
        raise WebServiceError("章节编辑数据无效。", code="INVALID_EDIT")
    chapters = [chapter for chapter in result.get("chapters", []) if isinstance(chapter, dict)]
    by_id = {str(chapter.get("chapter_id", "")): chapter for chapter in chapters}
    for index, chapter_edit in enumerate(edits):
        if not isinstance(chapter_edit, Mapping):
            continue
        chapter = by_id.get(str(chapter_edit.get("chapter_id", "")))
        if chapter is None and index < len(chapters):
            chapter = chapters[index]
        if chapter is None:
            raise WebServiceError("章节编辑数据包含无法匹配的章节。", code="INVALID_EDIT")
        if "title" in chapter_edit:
            chapter["title"] = _validate_text(
                chapter_edit["title"], "章节标题", allow_empty=False
            )
        _apply_blocks(chapter.get("blocks", []), chapter_edit.get("blocks"), "章节正文")
        selected = chapter_edit.get("selected_frame_ids")
        if isinstance(selected, list):
            original_frames = [
                frame for frame in chapter.get("frames", []) if isinstance(frame, dict)
            ]
            allowed = {str(frame.get("frame_id", "")) for frame in original_frames}
            selected_ids = [str(frame_id) for frame_id in selected]
            if len(selected_ids) != len(set(selected_ids)) or any(
                frame_id not in allowed for frame_id in selected_ids
            ):
                raise WebServiceError("配图选择包含无效或重复的画面。", code="INVALID_EDIT")
            chapter["frames"] = [
                frame
                for frame in original_frames
                if str(frame.get("frame_id", "")) in set(selected_ids)
            ]
    return result


class TaskService:
    """Thread-safe task and document workflow used by the local HTTP server."""

    def __init__(
        self,
        output_root: str | Path = "outputs",
        *,
        project_root: str | Path | None = None,
        max_upload_bytes: int | None = None,
    ) -> None:
        self.output_root = Path(output_root).expanduser().resolve()
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else Path(__file__).resolve().parent.parent
        )
        self.web_root = self.output_root / "_web"
        self.state_root = self.web_root / "tasks"
        self.upload_root = self.output_root / "_uploads"
        self.max_upload_bytes = max_upload_bytes or int(
            os.environ.get("VIDEO_DOC_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        )
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._recover_interrupted_tasks()

    # ------------------------------------------------------------------
    # Task metadata and discovery
    # ------------------------------------------------------------------
    def _task_root(self, task_id: str) -> Path:
        try:
            validate_task_id(task_id)
        except TaskWorkspaceError as exc:
            raise WebServiceError(str(exc), code="INVALID_TASK_ID") from exc
        return self.output_root / task_id

    def _state_path(self, task_id: str) -> Path:
        return self.state_root / f"{task_id}.json"

    def _default_state(self, task_id: str, source_video: str | None) -> dict[str, Any]:
        now = _utc_now()
        return {
            "schema_name": "VideoDocumentWebTask",
            "schema_version": TASK_STATE_SCHEMA_VERSION,
            "task_id": task_id,
            "display_name": Path(source_video).stem if source_video else task_id,
            "source_video": source_video,
            "source_origin": "local",
            "created_at": now,
            "updated_at": now,
            "status": "ready",
            "stage": "ready",
            "stage_status": "ready",
            "stage_message": "等待开始生成",
            "progress": 0,
            "options": {
                "ocr_mode": "auto",
                "bounded_content": True,
                "allow_llm": False,
                "ffmpeg_dir": "",
            },
            "archived": False,
            "run_count": 0,
            "error": None,
            "probe_error": None,
            "events": [],
        }

    def _load_state(self, task_id: str) -> dict[str, Any]:
        task_root = self._task_root(task_id)
        manifest: Mapping[str, Any] | None = None
        if (task_root / "task.json").is_file():
            manifest = load_task_manifest(task_root / "task.json", expected_task_id=task_id)
        source = str(manifest["source_video"]) if manifest and manifest.get("source_video") else None
        state = self._default_state(task_id, source)
        path = self._state_path(task_id)
        if path.is_file():
            try:
                stored = _read_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                raise WebServiceError(
                    f"任务状态文件损坏：{path}",
                    status=409,
                    code="TASK_STATE_CORRUPT",
                ) from exc
            if not isinstance(stored, dict) or stored.get("task_id") != task_id:
                raise WebServiceError(
                    "任务状态文件与任务ID不一致。",
                    status=409,
                    code="TASK_STATE_CORRUPT",
                )
            state.update(stored)
        if source:
            state["source_video"] = source
        return state

    def _save_state(self, state: Mapping[str, Any]) -> None:
        path = self._state_path(str(state["task_id"]))
        payload = dict(state)
        payload["updated_at"] = _utc_now()
        _write_json_atomic(path, payload)

    def _recover_interrupted_tasks(self) -> None:
        """Mark tasks left active by a previous server process as recoverable failures."""

        if not self.state_root.is_dir():
            return
        with self._lock:
            for path in self.state_root.glob("*.json"):
                try:
                    state = _read_json(path)
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(state, dict) or state.get("status") not in ACTIVE_STATUSES:
                    continue
                state["status"] = "failed"
                state["stage_status"] = "failed"
                state["stage_message"] = "服务重启前任务已中断，可以重新运行"
                state["finished_at"] = _utc_now()
                state["error"] = {
                    "code": "RUNNER_INTERRUPTED",
                    "message": "服务重启前任务已中断，可以重新运行。",
                    "detail": "已发布的阶段缓存会继续复用。",
                    "failed_stage": state.get("stage", "preflight"),
                }
                self._event(state, "failed", "检测到上次运行在服务重启时中断")
                self._save_state(state)

    def _event(self, state: dict[str, Any], kind: str, message: str, **data: Any) -> None:
        event = {"timestamp": _utc_now(), "kind": kind, "message": message}
        event.update(data)
        events = list(state.get("events", []))
        events.append(event)
        state["events"] = events[-80:]

    def _discover_task_ids(self) -> set[str]:
        ids: set[str] = set()
        if self.state_root.is_dir():
            for path in self.state_root.glob("*.json"):
                if path.stem and path.name != "index.json":
                    ids.add(path.stem)
        if self.output_root.is_dir():
            for path in self.output_root.iterdir():
                if not path.is_dir() or path.name.startswith("_"):
                    continue
                if (path / "task.json").is_file():
                    ids.add(path.name)
        return ids

    def _peek_document(self, task_id: str) -> dict[str, Any]:
        task_root = self._task_root(task_id)
        m2_root = task_root / "m2"
        result: dict[str, Any] = {
            "has_content": False,
            "has_export": False,
            "content_id": None,
            "export_id": None,
            "title": None,
            "chapter_count": 0,
        }
        content_root = m2_root / "content"
        content_current = content_root / "current.json"
        if content_current.is_file():
            try:
                pointer = _read_json(content_current)
                group = _safe_group(content_root, str(pointer["group"]))
                content = _read_json(group / "document-content.json")
                result.update(
                    has_content=True,
                    content_id=pointer.get("result_id") or content.get("content_id"),
                    title=content.get("title"),
                    chapter_count=len(content.get("chapters", [])),
                )
            except Exception:
                pass
        document_root = m2_root / "documents"
        document_current = document_root / "current.json"
        if document_current.is_file():
            try:
                pointer = _read_json(document_current)
                group = _safe_group(document_root, str(pointer["group"]))
                result_file = group / "result.json"
                if result_file.is_file():
                    exported = _read_json(result_file)
                    result.update(
                        has_export=True,
                        export_id=pointer.get("result_id"),
                        title=result.get("title") or exported.get("title"),
                        chapter_count=len(exported.get("chapters", [])),
                    )
            except Exception:
                pass
        return result

    def _task_metadata(self, task_id: str) -> dict[str, Any]:
        path = self._task_root(task_id) / "metadata.json"
        if not path.is_file():
            return {}
        try:
            value = _read_json(path)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _task_summary(self, task_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        current = state or self._load_state(task_id)
        peek = self._peek_document(task_id)
        metadata = self._task_metadata(task_id)
        result = dict(current)
        result["document"] = peek
        result["metadata"] = metadata
        if current.get("status") == "ready" and (peek["has_content"] or peek["has_export"]):
            result["status"] = "completed"
            result["stage"] = "completed"
            result["stage_status"] = "completed"
            result["progress"] = 100
            result["stage_message"] = (
                "文档已生成，可预览、编辑和下载"
                if peek["has_export"]
                else "已有机器正文，可在下载时重新导出"
            )
        return result

    def list_tasks(
        self,
        *,
        status: str | None = None,
        search: str = "",
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        with self._lock:
            for task_id in self._discover_task_ids():
                try:
                    state = self._load_state(task_id)
                    if state.get("archived") and not include_archived:
                        continue
                    if status and status != "all":
                        if status == "active":
                            if state.get("status") not in ACTIVE_STATUSES:
                                continue
                        elif state.get("status") != status:
                            continue
                    summary = self._task_summary(task_id, state)
                    haystack = " ".join(
                        str(summary.get(key, ""))
                        for key in ("task_id", "display_name", "source_video")
                    ).lower()
                    if search and search.lower() not in haystack:
                        continue
                    tasks.append(summary)
                except Exception:
                    # A damaged unrelated task must not make the whole task list unusable.
                    continue
        tasks.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return tasks

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(task_id)
            summary = self._task_summary(task_id, state)
            summary["edits"] = self._list_edit_manifests(task_id)
            return summary

    def overview(self) -> dict[str, Any]:
        tasks = self.list_tasks(include_archived=False)
        statuses = {status: 0 for status in ("ready", "queued", "running", "completed", "failed", "cancelled")}
        for task in tasks:
            status = str(task.get("status", "ready"))
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "counts": {
                "total": len(tasks),
                "active": sum(statuses.get(key, 0) for key in ACTIVE_STATUSES),
                "completed": statuses.get("completed", 0),
                "needs_attention": statuses.get("failed", 0) + statuses.get("cancelled", 0),
                "ready": statuses.get("ready", 0),
            },
            "statuses": statuses,
            "recent": tasks[:6],
            "system": self.environment_status(),
        }

    def environment_status(self) -> dict[str, Any]:
        llm_key_ready = self._llm_key_configured()
        return {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
            "output_root": str(self.output_root),
            "model_config": (self.project_root / "config" / "model-roles.json").is_file(),
            "llm_key_ready": llm_key_ready,
            "llm_provider": "configured_api",
        }

    def _llm_key_configured(self) -> bool:
        if os.environ.get("VIDEO_DOC_LLM_API_KEY", "").strip():
            return True
        env_path = self.project_root / ".env"
        if not env_path.is_file():
            return False
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                name, marker, value = line.partition("=")
                if marker and name.strip() == "VIDEO_DOC_LLM_API_KEY":
                    return bool(value.strip().strip("\"'"))
        except OSError:
            return False
        return False

    # ------------------------------------------------------------------
    # Task creation
    # ------------------------------------------------------------------
    def _next_task_id(self) -> str:
        prefix = datetime.now().strftime("VID-%Y%m%d-%H%M%S")
        candidate = prefix
        index = 2
        while (self.output_root / candidate).exists() or self._state_path(candidate).exists():
            candidate = f"{prefix}-{index}"
            index += 1
        return candidate

    def _normalize_options(self, options: Mapping[str, Any] | None) -> dict[str, Any]:
        raw = dict(options or {})
        ocr_mode = str(raw.get("ocr_mode", "auto"))
        if ocr_mode not in {"auto", "off", "required"}:
            raise WebServiceError("OCR模式无效。", code="INVALID_OPTIONS")
        return {
            "ocr_mode": ocr_mode,
            "bounded_content": bool(raw.get("bounded_content", True)),
            "allow_llm": raw.get("allow_llm") is True,
            "ffmpeg_dir": str(raw.get("ffmpeg_dir", "")).strip(),
        }

    def _create_task(
        self,
        *,
        task_id: str | None,
        source_video: str | Path,
        display_name: str | None,
        options: Mapping[str, Any] | None,
        source_origin: str,
        probe: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            resolved_task_id = task_id.strip() if isinstance(task_id, str) and task_id.strip() else self._next_task_id()
            task_root = self._task_root(resolved_task_id)
            if task_root.exists():
                raise WebServiceError("任务ID已存在，请换一个ID。", status=409, code="TASK_EXISTS")
            source = Path(source_video).expanduser()
            if not source.is_file():
                raise WebServiceError("视频文件不存在或不可读。", code="SOURCE_NOT_FOUND")
            if source.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
                raise WebServiceError(
                    "暂不支持该视频格式，请选择 MP4、MKV、MOV、AVI、WEBM 或 M4V。",
                    code="UNSUPPORTED_VIDEO",
                )
            if source.stat().st_size <= 0:
                raise WebServiceError("视频文件为空。", code="EMPTY_VIDEO")

            workspace = initialize_task_workspace(
                self.output_root, resolved_task_id, source_video=source
            )
            state = self._default_state(resolved_task_id, str(source.resolve()))
            state["display_name"] = (
                display_name.strip()
                if isinstance(display_name, str) and display_name.strip()
                else source.stem
            )[:120]
            state["source_origin"] = source_origin
            state["options"] = self._normalize_options(options)
            self._event(state, "created", "任务已创建")
            if probe:
                try:
                    metadata = probe_video(source)
                    save_metadata(metadata, workspace.root / "metadata.json")
                except VideoProbeError as exc:
                    state["probe_error"] = str(exc)
                    self._event(state, "probe_failed", "未能读取视频信息", detail=str(exc))
            self._save_state(state)
            return self._task_summary(resolved_task_id, state)

    def create_local_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        source = payload.get("source_video")
        if not isinstance(source, str) or not source.strip():
            raise WebServiceError("请填写本地视频路径。", code="SOURCE_REQUIRED")
        return self._create_task(
            task_id=payload.get("task_id") if isinstance(payload.get("task_id"), str) else None,
            source_video=source,
            display_name=payload.get("display_name") if isinstance(payload.get("display_name"), str) else None,
            options=payload.get("options") if isinstance(payload.get("options"), Mapping) else None,
            source_origin="local",
        )

    def create_uploaded_task(
        self,
        *,
        task_id: str | None,
        filename: str,
        content_length: int,
        stream: BinaryIO,
        display_name: str | None,
        options: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if content_length <= 0:
            raise WebServiceError("上传文件为空。", code="EMPTY_UPLOAD")
        if content_length > self.max_upload_bytes:
            raise WebServiceError(
                f"视频超过上传上限（{self.max_upload_bytes} 字节）。",
                status=413,
                code="UPLOAD_TOO_LARGE",
            )
        with self._lock:
            resolved_task_id = task_id.strip() if isinstance(task_id, str) and task_id.strip() else self._next_task_id()
            task_root = self._task_root(resolved_task_id)
            if task_root.exists():
                raise WebServiceError("任务ID已存在，请换一个ID。", status=409, code="TASK_EXISTS")

        safe_name = Path(filename.replace("\\", "/")).name
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", safe_name).strip(" .")
        if not safe_name:
            safe_name = "source-video.mp4"
        if Path(safe_name).suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
            raise WebServiceError(
                "上传文件不是支持的视频格式。",
                code="UNSUPPORTED_VIDEO",
            )
        destination_dir = self.upload_root / resolved_task_id
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / safe_name
        temporary = destination_dir / f".{safe_name}.{uuid4().hex}.upload"
        received = 0
        try:
            with temporary.open("wb") as target:
                while received < content_length:
                    chunk = stream.read(min(1024 * 1024, content_length - received))
                    if not chunk:
                        break
                    target.write(chunk)
                    received += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            if received != content_length:
                raise WebServiceError("上传中断，文件不完整。", code="UPLOAD_INCOMPLETE")
            os.replace(temporary, destination)
            return self._create_task(
                task_id=resolved_task_id,
                source_video=destination,
                display_name=display_name or Path(safe_name).stem,
                options=options,
                source_origin="upload",
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            if destination_dir.is_dir() and not any(destination_dir.iterdir()):
                destination_dir.rmdir()
            raise

    # ------------------------------------------------------------------
    # Running the existing pipeline
    # ------------------------------------------------------------------
    def _runner_command(self, state: Mapping[str, Any]) -> list[str]:
        options = state.get("options") or {}
        command = [
            sys.executable,
            "-m",
            "video_doc.cli",
            "run-m2",
            str(state["task_id"]),
            "--output-root",
            str(self.output_root),
            "--source-video",
            str(state["source_video"]),
            "--ocr",
            str(options.get("ocr_mode", "auto")),
        ]
        if options.get("bounded_content", True):
            command.append("--bounded-content")
        if options.get("allow_llm"):
            command.append("--allow-llm")
        ffmpeg_dir = str(options.get("ffmpeg_dir", "")).strip()
        if ffmpeg_dir:
            command.extend(["--ffmpeg-dir", ffmpeg_dir])
        return command

    def start_task(self, task_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(task_id)
            if state.get("status") in ACTIVE_STATUSES or task_id in self._processes:
                raise WebServiceError("任务正在运行。", status=409, code="TASK_ALREADY_RUNNING")
            if not state.get("source_video"):
                raise WebServiceError("任务没有登记源视频。", code="SOURCE_REQUIRED")
            if not Path(str(state["source_video"])).is_file():
                raise WebServiceError("源视频已不存在，请重新导入。", code="SOURCE_NOT_FOUND")
            state["options"] = self._normalize_options(
                {
                    **(state.get("options") or {}),
                    **(payload if isinstance(payload, Mapping) else {}),
                    "allow_llm": isinstance(payload, Mapping) and payload.get("allow_llm") is True,
                }
            )
            state["status"] = "queued"
            state["stage"] = "preflight"
            state["stage_status"] = "queued"
            state["stage_message"] = "任务已进入队列"
            state["progress"] = 1
            state["error"] = None
            state["cancel_requested"] = False
            state["run_count"] = int(state.get("run_count", 0)) + 1
            state["last_started_at"] = _utc_now()
            state["finished_at"] = None
            self._event(state, "queued", f"开始第 {state['run_count']} 次生成")
            self._save_state(state)

            command = self._runner_command(state)
            thread = threading.Thread(
                target=self._run_process,
                args=(task_id, command),
                name=f"video-doc-{task_id}",
                daemon=True,
            )
            self._threads[task_id] = thread
            thread.start()
            return self._task_summary(task_id, state)

    def _run_process(self, task_id: str, command: list[str]) -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        local_asr_model = self.project_root / "data" / "tmp" / "models" / "faster-whisper-small"
        if local_asr_model.is_dir() and not env.get("VIDEO_DOC_ASR_MODEL_PATH"):
            env["VIDEO_DOC_ASR_MODEL_PATH"] = str(local_asr_model)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process: subprocess.Popen[str] | None = None
        last_stage = "preflight"
        error_payload: dict[str, Any] | None = None
        output_tail: list[str] = []
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
            with self._lock:
                self._processes[task_id] = process
                state = self._load_state(task_id)
                if state.get("cancel_requested"):
                    process.terminate()
                state["status"] = "running"
                state["stage_status"] = "running"
                state["stage_message"] = STAGE_LABELS.get(last_stage, "正在处理")
                state["progress"] = max(2, int(state.get("progress", 1)))
                self._event(state, "started", "生成进程已启动")
                self._save_state(state)

            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                output_tail.append(line)
                output_tail = output_tail[-100:]
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("status") == "failed":
                    error_payload = event
                    continue
                stage = event.get("stage")
                if not isinstance(stage, str) or stage not in STAGE_LABELS:
                    continue
                last_stage = stage
                event_status = str(event.get("status", "running"))
                with self._lock:
                    state = self._load_state(task_id)
                    state["stage"] = stage
                    state["stage_status"] = event_status
                    state["stage_message"] = STAGE_LABELS[stage]
                    state["progress"] = max(
                        int(state.get("progress", 0)),
                        STAGE_PROGRESS.get(stage, 1),
                    )
                    if event.get("reason_codes"):
                        state["reason_codes"] = list(event["reason_codes"])
                    if stage == "audio" and event_status in {"completed", "cache_hit"}:
                        last_stage = "asr"
                        state["stage"] = "asr"
                        state["stage_status"] = "started"
                        state["stage_message"] = "正在加载本地语音识别模型并转写"
                        state["progress"] = max(int(state.get("progress", 0)), 22)
                    self._event(
                        state,
                        "stage",
                        state["stage_message"],
                        stage=state["stage"],
                        stage_status=state["stage_status"],
                        result_id=event.get("result_id"),
                    )
                    self._save_state(state)
            return_code = process.wait()

            with self._lock:
                state = self._load_state(task_id)
                cancelled = bool(state.get("cancel_requested"))
                finished_at = _utc_now()
                state["finished_at"] = finished_at
                state["stage"] = "completed" if return_code == 0 and not cancelled else state.get("stage", last_stage)
                if cancelled:
                    state["status"] = "cancelled"
                    state["stage_status"] = "cancelled"
                    state["stage_message"] = "任务已取消，可重新运行"
                    self._event(state, "cancelled", "任务已取消")
                elif return_code == 0:
                    state["status"] = "completed"
                    state["stage"] = "completed"
                    state["stage_status"] = "completed"
                    state["stage_message"] = "文档已生成，可预览、编辑和下载"
                    state["progress"] = 100
                    state["error"] = None
                    state["completed_at"] = finished_at
                    peek = self._peek_document(task_id)
                    if peek.get("chapter_count"):
                        state["chapter_count"] = peek["chapter_count"]
                    if peek.get("title"):
                        state["document_title"] = peek["title"]
                    self._event(state, "completed", "全部处理阶段完成")
                else:
                    code = "PROCESS_FAILED"
                    detail = ""
                    failed_stage = last_stage
                    if error_payload is None:
                        error_payload = _latest_pipeline_failure(self._task_root(task_id))
                    if error_payload:
                        code = str(error_payload.get("error_code") or code)
                        detail = str(error_payload.get("message") or "")
                        failed_stage = str(error_payload.get("failed_stage") or failed_stage)
                    if code == "LLM_APPROVAL_REQUIRED":
                        detail = "处理链在发起章节规划模型请求前等待显式授权。"
                        if not self._llm_key_configured():
                            detail += " 当前未检测到 VIDEO_DOC_LLM_API_KEY，请先在本机 .env 中配置。"
                    if not detail:
                        detail = next(
                            (
                                line
                                for line in reversed(output_tail)
                                if line and not line.startswith("{")
                            ),
                            "生成进程异常退出。",
                        )
                    friendly = _ERROR_MESSAGES.get(code, "生成失败，请查看任务详情后重试。")
                    state["status"] = "failed"
                    state["stage"] = failed_stage
                    state["stage_status"] = "failed"
                    state["stage_message"] = friendly
                    state["error"] = {
                        "code": code,
                        "message": friendly,
                        "detail": _compact_error(detail),
                        "failed_stage": failed_stage,
                    }
                    self._event(
                        state,
                        "failed",
                        friendly,
                        error_code=code,
                        failed_stage=failed_stage,
                    )
                self._save_state(state)
        except Exception as exc:
            with self._lock:
                state = self._load_state(task_id)
                if state.get("status") not in {"cancelled", "completed"}:
                    state["status"] = "failed"
                    state["stage_status"] = "failed"
                    state["stage_message"] = "生成进程启动或读取失败"
                    state["finished_at"] = _utc_now()
                    state["error"] = {
                        "code": "RUNNER_ERROR",
                        "message": "生成进程启动或读取失败。",
                        "detail": _compact_error(str(exc)),
                        "failed_stage": state.get("stage", "preflight"),
                    }
                    self._event(state, "failed", "生成进程异常", detail=_compact_error(str(exc)))
                    self._save_state(state)
        finally:
            with self._lock:
                self._processes.pop(task_id, None)
                self._threads.pop(task_id, None)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(task_id)
            if state.get("status") not in ACTIVE_STATUSES and task_id not in self._processes:
                raise WebServiceError("任务当前不在运行。", status=409, code="TASK_NOT_RUNNING")
            state["cancel_requested"] = True
            state["status"] = "cancelling"
            state["stage_status"] = "cancelling"
            state["stage_message"] = "正在停止生成进程"
            self._event(state, "cancelling", "已请求取消任务")
            self._save_state(state)
            process = self._processes.get(task_id)
            if process is not None and process.poll() is None:
                process.terminate()
            return self._task_summary(task_id, state)

    def set_archived(self, task_id: str, archived: bool) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(task_id)
            if archived and state.get("status") in ACTIVE_STATUSES:
                raise WebServiceError("运行中的任务不能归档。", status=409, code="TASK_RUNNING")
            state["archived"] = archived
            self._event(state, "archived" if archived else "restored", "任务已归档" if archived else "任务已恢复")
            self._save_state(state)
            return self._task_summary(task_id, state)

    def shutdown(self) -> None:
        """Terminate child CLI processes when the local web server is stopped."""

        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.poll() is None:
                process.terminate()

    # ------------------------------------------------------------------
    # Machine and edited document versions
    # ------------------------------------------------------------------
    def _edit_root(self, task_id: str) -> Path:
        return self._task_root(task_id) / "m2" / "editorial-revisions"

    def _edit_path(self, task_id: str, edit_id: str) -> Path:
        if not EDIT_ID_PATTERN.fullmatch(edit_id):
            raise WebServiceError("编辑版本ID无效。", code="INVALID_EDIT_ID")
        path = self._edit_root(task_id) / edit_id
        if not path.is_dir():
            raise WebServiceError("编辑版本不存在。", status=404, code="EDIT_NOT_FOUND")
        return path

    def _latest_edit_id(self, task_id: str) -> str | None:
        manifests = self._list_edit_manifests(task_id)
        return str(manifests[0]["edit_id"]) if manifests else None

    def _list_edit_manifests(self, task_id: str) -> list[dict[str, Any]]:
        root = self._edit_root(task_id)
        versions: list[dict[str, Any]] = []
        if not root.is_dir():
            return versions
        for path in root.iterdir():
            if not path.is_dir() or not EDIT_ID_PATTERN.fullmatch(path.name):
                continue
            manifest_path = path / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                _, manifest = self._edit_content(task_id, path.name)
                versions.append(manifest)
            except WebServiceError:
                continue
        versions.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return versions

    def _machine_content(self, task_id: str) -> dict[str, Any]:
        try:
            content = load_published_document_content(self.output_root, task_id)
        except Exception as exc:
            raise WebServiceError(
                "机器正文未通过完整性复验，暂不能预览。",
                status=409,
                code="CONTENT_INVALID",
            ) from exc
        if content is None:
            raise WebServiceError(
                "任务还没有生成文档正文。",
                status=404,
                code="DOCUMENT_NOT_READY",
            )
        return content

    def _edit_content(self, task_id: str, edit_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        root = self._edit_path(task_id, edit_id)
        try:
            manifest = _read_json(root / "manifest.json")
            raw = (root / "result.json").read_bytes()
            record = manifest["files"]["result.json"]
            expected = record["sha256"] if isinstance(record, dict) else record
            if manifest["revision_id"] != edit_id or _sha256_bytes(raw) != expected:
                raise ValueError("revision checksum mismatch")
            revision = json.loads(raw)
            schema = (revision["schema_name"], revision["schema_version"])
            if revision["task_id"] != task_id or schema not in {
                ("OfflineEditorialRevision", "1.0"),
                ("EditorialRevision", EDIT_SCHEMA_VERSION),
            }:
                raise ValueError("unsupported editorial revision")
            content = revision["edited_document"]
            if not isinstance(content, dict):
                raise ValueError("invalid editorial document")
            return content, {
                **manifest,
                "edit_id": edit_id,
                "version_name": manifest.get("version_name", edit_id),
                "source_content_id": revision["source_content_id"],
                "source_version": revision.get("source_version", "machine"),
                "source_export_id": revision.get("source_export_id"),
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WebServiceError("Invalid editorial revision.", status=409, code="EDIT_CORRUPT") from exc

    def _document_payload(
        self,
        task_id: str,
        *,
        version_id: str,
        kind: str,
        content: Mapping[str, Any],
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        editable = _editable_document(content)
        # The content task ID controls preview URLs and must not leak another task.
        for chapter in editable["chapters"]:
            for frame in chapter["frames"]:
                frame["image_url"] = f"/api/tasks/{task_id}/frames/{frame['frame_id']}?version={version_id}"
        return {
            "version_id": version_id,
            "kind": kind,
            "source_content_id": content.get("content_id"),
            "manifest": dict(manifest or {}),
            "editable": editable,
            "stats": {
                "chapter_count": len(editable["chapters"]),
                "block_count": sum(len(chapter["blocks"]) for chapter in editable["chapters"]),
                "frame_count": sum(len(chapter["frames"]) for chapter in editable["chapters"]),
            },
        }

    def get_document(self, task_id: str, version_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            state = self._load_state(task_id)
            machine = self._machine_content(task_id)
            latest_edit_id = self._latest_edit_id(task_id)
            versions = self._list_edit_manifests(task_id)
            machine_payload = self._document_payload(
                task_id,
                version_id="machine",
                kind="machine",
                content=machine,
                manifest={"content_id": machine.get("content_id")},
            )
            selected = machine_payload
            edit_id = (
                latest_edit_id
                if version_id == "latest"
                else version_id if version_id != "machine" else None
            )
            if edit_id:
                edit_content, edit_manifest = self._edit_content(task_id, edit_id)
                selected = self._document_payload(
                    task_id,
                    version_id=edit_id,
                    kind="edit",
                    content=edit_content,
                    manifest=edit_manifest,
                )
            return {
                "task": self._task_summary(task_id, state),
                "machine": machine_payload,
                "latest_edit_id": latest_edit_id,
                "selected": selected,
                "versions": versions,
            }

    def save_edit(self, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        editable = payload.get("editable")
        if not isinstance(editable, Mapping):
            raise WebServiceError("编辑内容无效。", code="INVALID_EDIT")
        base_version = payload.get("base_version")
        if not isinstance(base_version, str) or not base_version:
            base_version = "machine"
        with self._lock:
            if base_version == "machine":
                machine = self._machine_content(task_id)
                base_content: dict[str, Any] = deepcopy(machine)
                base_manifest: Mapping[str, Any] = {"content_id": machine.get("content_id")}
            else:
                base_content, base_manifest = self._edit_content(task_id, base_version)
            edited = _apply_editable(base_content, editable)
            version_name = str(payload.get("version_name") or "").strip()
            if not version_name:
                version_name = f"编辑版本 {datetime.now().strftime('%m-%d %H:%M')}"
            version_name = version_name[:80]
            edited.pop("generation", None)
            edited.pop("coverage", None)
            revision = {
                "schema_name": "EditorialRevision",
                "schema_version": EDIT_SCHEMA_VERSION,
                "task_id": task_id,
                "source_version": base_version,
                "source_export_id": base_manifest.get("source_export_id"),
                "version_name": version_name,
                "source_content_id": base_manifest.get("source_content_id") or base_content.get("content_id"),
                "source_json_sha256": _sha256_bytes(_json_bytes(base_content)),
                "edited_document": edited,
                "deleted_frame_ids": sorted(set(_referenced_frame_ids(base_content)) - set(_referenced_frame_ids(edited))),
            }
            request_sha256 = _sha256_bytes(_json_bytes(revision))
            edit_id = "editorial-" + request_sha256[:12]
            edited["content_id"] = edit_id
            destination = self._edit_root(task_id) / edit_id
            if not destination.exists():
                self._write_edit_group(task_id, destination, revision, version_name, request_sha256)
            saved, manifest = self._edit_content(task_id, edit_id)
            if saved.get("content_id") != edit_id or manifest.get("request_sha256") != request_sha256:
                raise WebServiceError("Editorial revision conflict.", status=409, code="EDIT_CONFLICT")
            state = self._load_state(task_id)
            state["last_edited_at"] = manifest["created_at"]
            self._event(state, "edited", f"已保存{version_name}", edit_id=edit_id)
            self._save_state(state)
            return self._document_payload(
                task_id,
                version_id=edit_id,
                kind="edit",
                content=saved,
                manifest=manifest,
            )

    # ------------------------------------------------------------------
    # Exports and frame images
    # ------------------------------------------------------------------
    def _export_machine(self, task_id: str) -> tuple[Path, str]:
        try:
            result = export_document_v2(self.output_root, task_id)
        except Exception as exc:
            raise WebServiceError(
                f"机器版导出失败：{_compact_error(str(exc))}",
                status=409,
                code="MACHINE_EXPORT_FAILED",
            ) from exc
        return result.group_path, str(result.result_id)

    def _export_edit(self, task_id: str, edit_id: str) -> tuple[Path, dict[str, Any]]:
        _, manifest = self._edit_content(task_id, edit_id)
        return self._edit_path(task_id, edit_id), manifest

    def _write_edit_group(
        self, task_id: str, root: Path, revision: dict[str, Any], version_name: str, request_sha256: str,
    ) -> None:
        content = revision["edited_document"]
        referenced = _referenced_frame_ids(content)
        embedded = {}
        degraded = []
        if revision["source_version"] != "machine":
            for frame_id in referenced:
                image, _ = self.frame_file(task_id, frame_id, revision["source_version"])
                embedded[frame_id] = image.read_bytes()
        elif referenced:
            try:
                inputs = _load_export_inputs(self.output_root, task_id)
            except Exception as exc:
                raise WebServiceError(
                    f"无法读取导出所需的原始画面：{_compact_error(str(exc))}",
                    status=409,
                    code="EXPORT_INPUT_INVALID",
                ) from exc
            embedded, degraded = _collect_frame_bytes(inputs, referenced)
        effective = deepcopy(content)
        while True:
            missing = [frame_id for frame_id in referenced if frame_id not in embedded]
            if missing:
                for chapter in effective.get("chapters", []):
                    chapter["frames"] = [
                        frame
                        for frame in chapter.get("frames", [])
                        if str(frame.get("frame_id")) in embedded
                    ]
                for frame_id in missing:
                    degraded.append(f"IMAGE_UNAVAILABLE:{frame_id}")
                referenced = _referenced_frame_ids(effective)
            docx_bytes, failed = render_export_docx(_renderable_edit(effective), embedded=embedded)
            if not failed:
                break
            changed = False
            for frame_id in failed:
                if frame_id in embedded:
                    embedded.pop(frame_id, None)
                    degraded.append(f"IMAGE_EMBED_FAILED:{frame_id}")
                    changed = True
            if not changed:
                raise WebServiceError(
                    "Word 渲染未能处理所选配图，请删除异常配图后重试。",
                    status=409,
                    code="EDIT_EXPORT_FAILED",
                )
            for chapter in effective.get("chapters", []):
                chapter["frames"] = [
                    frame
                    for frame in chapter.get("frames", [])
                    if str(frame.get("frame_id")) in embedded
                ]
            referenced = _referenced_frame_ids(effective)

        markdown_bytes = render_export_markdown(_renderable_edit(effective), embedded=set(embedded))
        revision["edited_document"] = effective
        json_bytes = _json_bytes(revision)
        manifest = {
            "schema_name": "EditorialRevisionManifest",
            "schema_version": EDIT_SCHEMA_VERSION,
            "task_id": task_id,
            "revision_id": root.name,
            "request_sha256": request_sha256,
            "version_name": version_name,
            "created_at": _utc_now(),
            "status": "degraded" if degraded else "success",
            "degraded_reasons": degraded,
            "files": {
                "result.json": {"sha256": _sha256_bytes(json_bytes), "bytes": len(json_bytes)},
                "result.md": {"sha256": _sha256_bytes(markdown_bytes), "bytes": len(markdown_bytes)},
                "result.docx": {"sha256": _sha256_bytes(docx_bytes), "bytes": len(docx_bytes)},
            },
            "images": sorted(embedded),
            "deleted_frame_ids": revision.get("deleted_frame_ids", []),
        }
        temporary = root.with_name(f".tmp-{uuid4().hex[:12]}")
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir(parents=True)
        try:
            (temporary / "result.json").write_bytes(json_bytes)
            (temporary / "result.md").write_bytes(markdown_bytes)
            (temporary / "result.docx").write_bytes(docx_bytes)
            images_root = temporary / "images"
            images_root.mkdir()
            for frame_id, payload in embedded.items():
                filename = f"images/{frame_id}.png"
                (temporary / filename).write_bytes(payload)
                manifest["files"][filename] = {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
            (temporary / "manifest.json").write_bytes(_json_bytes(manifest))
            try:
                os.rename(temporary, root)
            except OSError:
                if not root.is_dir():
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def prepare_export(self, task_id: str, version_id: str) -> dict[str, Any]:
        if version_id == "machine":
            group, result_id = self._export_machine(task_id)
            files = sorted(path.name for path in group.glob("result.*"))
            return {"version_id": "machine", "export_id": result_id, "files": files}
        group, manifest = self._export_edit(task_id, version_id)
        return {
            "version_id": version_id,
            "edit_id": version_id,
            "status": manifest.get("status"),
            "files": ["result.json", "result.md", "result.docx"],
        }

    @staticmethod
    def _revision_file(root: Path, manifest: Mapping[str, Any], name: str) -> Path:
        path = root / name
        try:
            record = manifest["files"][name]
            expected = record["sha256"] if isinstance(record, dict) else record
            if _sha256_bytes(path.read_bytes()) != expected:
                raise ValueError("file checksum mismatch")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WebServiceError("编辑版本文件缺失或损坏。", status=409, code="EDIT_CORRUPT") from exc
        return path

    def export_file(self, task_id: str, version_id: str, fmt: str) -> tuple[Path, str, str]:
        if fmt not in {"json", "md", "docx"}:
            raise WebServiceError("不支持的下载格式。", code="INVALID_FORMAT")
        if version_id == "machine":
            group, _ = self._export_machine(task_id)
            source = group / f"result.{fmt}"
            label = "machine"
        else:
            group, manifest = self._export_edit(task_id, version_id)
            source = self._revision_file(group, manifest, f"result.{fmt}")
            label = version_id
        if not source.is_file():
            raise WebServiceError("导出文件不存在。", status=404, code="EXPORT_NOT_FOUND")
        suffix = source.suffix
        download_name = f"{task_id}-{label}{suffix}"
        mime = {
            ".json": "application/json; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(suffix, mimetypes.guess_type(source.name)[0] or "application/octet-stream")
        return source, download_name, mime

    def frame_file(self, task_id: str, frame_id: str, version_id: str = "machine") -> tuple[Path, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", frame_id):
            raise WebServiceError("画面ID无效。", code="INVALID_FRAME_ID")
        if version_id != "machine":
            root, manifest = self._export_edit(task_id, version_id)
            path = self._revision_file(root, manifest, f"images/{frame_id}.png")
            return path, "image/png"
        try:
            _, group, files, _ = _load_frame_inputs(self.output_root, task_id)
            entry = files.get(frame_id)
            if entry is None:
                raise KeyError(frame_id)
            path = _safe_group(group, str(entry["file"]))
        except Exception as exc:
            raise WebServiceError("画面不存在。", status=404, code="FRAME_NOT_FOUND") from exc
        if not path.is_file():
            raise WebServiceError("画面文件不存在。", status=404, code="FRAME_NOT_FOUND")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return path, mime


__all__ = ["TaskService", "WebServiceError"]
