"""单个视频任务的目录与最小数据结构。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TASK_SCHEMA_VERSION = "1.0"
TASK_SUBDIRECTORIES = (
    "audio",
    "asr",
    "frames",
    "ocr",
    "timeline",
    "boundaries",
    "llm",
    "documents",
    "logs",
)
TASK_MANIFEST_SCHEMA: dict[str, Any] = {
    "schema_version": TASK_SCHEMA_VERSION,
    "required": ("schema_version", "task_id", "source_video"),
}

_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class TaskWorkspaceError(RuntimeError):
    """任务目录初始化或Schema校验失败。"""


@dataclass(frozen=True)
class TaskWorkspace:
    """一个视频任务的关键路径。"""

    task_id: str
    root: Path
    manifest_path: Path


def validate_task_id(task_id: str) -> None:
    """确认任务ID可安全用作单层目录名。"""

    if not isinstance(task_id, str) or not _TASK_ID_PATTERN.fullmatch(task_id):
        raise TaskWorkspaceError(
            "任务ID只能包含英文字母、数字、下划线和连字符，"
            "必须以字母或数字开头，长度不超过64。"
        )


def validate_task_manifest(
    payload: Any,
    *,
    expected_task_id: str | None = None,
) -> None:
    """校验最小任务清单，允许未来增加可选字段。"""

    if not isinstance(payload, dict):
        raise TaskWorkspaceError("task.json必须是JSON对象。")

    missing = [
        field for field in TASK_MANIFEST_SCHEMA["required"] if field not in payload
    ]
    if missing:
        raise TaskWorkspaceError(f"task.json缺少必填字段：{'、'.join(missing)}")

    if payload["schema_version"] != TASK_SCHEMA_VERSION:
        raise TaskWorkspaceError(
            "不支持的Schema版本："
            f"{payload['schema_version']!r}，当前版本为{TASK_SCHEMA_VERSION}。"
        )

    task_id = payload["task_id"]
    if not isinstance(task_id, str):
        raise TaskWorkspaceError("task_id必须是字符串。")
    validate_task_id(task_id)
    if expected_task_id is not None and task_id != expected_task_id:
        raise TaskWorkspaceError(
            f"task.json中的任务ID为{task_id!r}，目录期望{expected_task_id!r}。"
        )

    source_video = payload["source_video"]
    if source_video is not None and not isinstance(source_video, str):
        raise TaskWorkspaceError("source_video必须是字符串或null。")


def load_task_manifest(
    path: str | Path,
    *,
    expected_task_id: str | None = None,
) -> dict[str, Any]:
    """读取并校验任务清单。"""

    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaskWorkspaceError(f"无法读取任务清单：{manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise TaskWorkspaceError(f"任务清单不是有效JSON：{manifest_path}") from exc

    validate_task_manifest(payload, expected_task_id=expected_task_id)
    return payload


def initialize_task_workspace(
    output_root: str | Path,
    task_id: str,
    *,
    source_video: str | Path | None = None,
) -> TaskWorkspace:
    """初始化任务目录；已有目录、清单和产物均不会被覆盖。"""

    validate_task_id(task_id)
    task_root = Path(output_root).expanduser() / task_id
    manifest_path = task_root / "task.json"

    try:
        task_root.mkdir(parents=True, exist_ok=True)
        for directory_name in TASK_SUBDIRECTORIES:
            (task_root / directory_name).mkdir(exist_ok=True)
    except OSError as exc:
        raise TaskWorkspaceError(f"无法初始化任务目录：{task_root}") from exc

    workspace = TaskWorkspace(
        task_id=task_id,
        root=task_root,
        manifest_path=manifest_path,
    )
    if manifest_path.exists():
        load_task_manifest(manifest_path, expected_task_id=task_id)
        return workspace

    source_path = (
        str(Path(source_video).expanduser().resolve())
        if source_video is not None
        else None
    )
    payload = {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": task_id,
        "source_video": source_path,
    }
    validate_task_manifest(payload, expected_task_id=task_id)

    try:
        with manifest_path.open("x", encoding="utf-8") as manifest_file:
            json.dump(payload, manifest_file, ensure_ascii=False, indent=2)
            manifest_file.write("\n")
    except FileExistsError:
        load_task_manifest(manifest_path, expected_task_id=task_id)
    except OSError as exc:
        raise TaskWorkspaceError(f"无法写入任务清单：{manifest_path}") from exc

    return workspace
