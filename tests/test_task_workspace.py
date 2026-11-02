import json
from pathlib import Path

import pytest

from video_doc.task_workspace import (
    TASK_SCHEMA_VERSION,
    TASK_SUBDIRECTORIES,
    TaskWorkspaceError,
    initialize_task_workspace,
    load_task_manifest,
    validate_task_manifest,
)


def test_initialize_task_workspace_creates_layout_and_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")

    workspace = initialize_task_workspace(
        tmp_path / "outputs",
        "DEV01",
        source_video=source,
    )

    assert workspace.root == tmp_path / "outputs" / "DEV01"
    assert all(
        (workspace.root / directory_name).is_dir()
        for directory_name in TASK_SUBDIRECTORIES
    )
    assert load_task_manifest(
        workspace.manifest_path,
        expected_task_id="DEV01",
    ) == {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": "DEV01",
        "source_video": str(source.resolve()),
    }


def test_initialize_task_workspace_preserves_existing_outputs(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01")
    original_manifest = workspace.manifest_path.read_bytes()
    existing_artifact = workspace.root / "audio" / "keep.txt"
    existing_artifact.write_text("do not overwrite", encoding="utf-8")

    repeated_workspace = initialize_task_workspace(
        output_root,
        "DEV01",
        source_video=tmp_path / "different.mp4",
    )

    assert repeated_workspace == workspace
    assert workspace.manifest_path.read_bytes() == original_manifest
    assert existing_artifact.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.parametrize("task_id", ["../DEV01", "DEV 01", "_DEV01"])
def test_initialize_task_workspace_rejects_unsafe_task_id(
    tmp_path: Path,
    task_id: str,
) -> None:
    with pytest.raises(TaskWorkspaceError, match="任务ID只能包含"):
        initialize_task_workspace(tmp_path / "outputs", task_id)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"task_id": "DEV01", "source_video": None}, "缺少必填字段"),
        (
            {
                "schema_version": "0.9",
                "task_id": "DEV01",
                "source_video": None,
            },
            "不支持的Schema版本",
        ),
        (
            {
                "schema_version": TASK_SCHEMA_VERSION,
                "task_id": "DEV01",
                "source_video": 123,
            },
            "source_video必须是字符串或null",
        ),
    ],
)
def test_validate_task_manifest_rejects_invalid_schema(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(TaskWorkspaceError, match=message):
        validate_task_manifest(payload)


def test_existing_invalid_manifest_is_not_overwritten(tmp_path: Path) -> None:
    task_root = tmp_path / "outputs" / "DEV01"
    task_root.mkdir(parents=True)
    manifest_path = task_root / "task.json"
    invalid_content = json.dumps({"schema_version": "0.9"})
    manifest_path.write_text(invalid_content, encoding="utf-8")

    with pytest.raises(TaskWorkspaceError, match="缺少必填字段"):
        initialize_task_workspace(tmp_path / "outputs", "DEV01")

    assert manifest_path.read_text(encoding="utf-8") == invalid_content
