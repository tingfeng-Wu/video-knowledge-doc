"""Web workflow tests for task management and edited document versions."""

from __future__ import annotations

import json
import threading
import urllib.request
from copy import deepcopy
from pathlib import Path

import pytest

from video_doc.web_server import create_server
from video_doc.web_service import TaskService, WebServiceError, _latest_pipeline_failure


def _document() -> dict:
    return {
        "schema_name": "StructuredDocumentContent",
        "schema_version": "3.0",
        "task_id": "WEB01",
        "content_id": "document-content-web-test",
        "title": "原始文档标题",
        "overview": [
            {
                "block_id": "overview-p001",
                "type": "paragraph",
                "role": "overview",
                "text": "原始概述。",
            }
        ],
        "overall_conclusion": [
            {
                "block_id": "conclusion-p001",
                "type": "paragraph",
                "role": "conclusion",
                "text": "原始结论。",
            }
        ],
        "chapters": [
            {
                "index": 1,
                "chapter_id": "c0001",
                "title": "原始章节",
                "start_ms": 0,
                "end_ms": 60_000,
                "blocks": [
                    {
                        "block_id": "c0001-p001",
                        "type": "paragraph",
                        "role": "explanation",
                        "text": "原始正文。",
                    },
                    {
                        "block_id": "c0001-list001",
                        "type": "list",
                        "role": "steps",
                        "list_kind": "numbered",
                        "items": [
                            {
                                "item_id": "c0001-list001-i001",
                                "text": "第一步。",
                            }
                        ],
                    },
                    {
                        "block_id": "c0001-cmp001",
                        "type": "comparison",
                        "role": "comparison",
                        "columns": ["现象", "影响"],
                        "rows": [
                            {
                                "row_id": "c0001-cmp001-r001",
                                "dimension": "表现",
                                "cells": [{"text": "紧张"}, {"text": "回避"}],
                            }
                        ],
                    },
                ],
                "frames": [
                    {
                        "frame_id": "fr000001",
                        "decoded_ms": 5_000,
                        "caption_text": "第一张画面",
                    },
                    {
                        "frame_id": "fr000002",
                        "decoded_ms": 20_000,
                        "caption_text": "第二张画面",
                    },
                ],
            }
        ],
    }


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TaskService:
    output_root = tmp_path / "outputs"
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"not-a-real-video-but-a-valid-test-file")
    instance = TaskService(output_root)
    instance._create_task(
        task_id="WEB01",
        source_video=source,
        display_name="Web test",
        options={"ocr_mode": "off"},
        source_origin="local",
        probe=False,
    )
    document = _document()
    monkeypatch.setattr(instance, "_machine_content", lambda task_id: deepcopy(document))
    return instance


def test_task_creation_and_overview(service: TaskService) -> None:
    task = service.get_task("WEB01")
    assert task["task_id"] == "WEB01"
    assert task["status"] == "ready"
    assert task["display_name"] == "Web test"
    assert service.overview()["counts"]["total"] == 1

    service.set_archived("WEB01", True)
    assert service.list_tasks() == []
    assert [item["task_id"] for item in service.list_tasks(include_archived=True)] == ["WEB01"]


def test_edit_creates_separate_immutable_version(service: TaskService) -> None:
    initial = service.get_document("WEB01")
    editable = initial["machine"]["editable"]
    editable["title"] = "修改后的标题"
    editable["overview"][0]["text"] = "修改后的概述。"
    editable["chapters"][0]["title"] = "修改后的章节"
    editable["chapters"][0]["blocks"][0]["text"] = "修改后的正文。"
    editable["chapters"][0]["blocks"][1]["items"][0]["text"] = "修改后的步骤。"
    editable["chapters"][0]["blocks"][2]["rows"][0]["cells"][1]["text"] = "影响被修正"
    editable["chapters"][0]["selected_frame_ids"] = ["fr000001"]

    saved = service.save_edit(
        "WEB01",
        {
            "base_version": "machine",
            "version_name": "修正版本",
            "editable": editable,
        },
    )
    assert saved["kind"] == "edit"
    assert saved["editable"]["title"] == "修改后的标题"
    assert saved["stats"]["frame_count"] == 1
    assert saved["manifest"]["source_version"] == "machine"
    assert saved["manifest"]["deleted_frame_ids"] == ["fr000002"]

    reopened = service.get_document("WEB01")
    assert reopened["machine"]["editable"]["title"] == "原始文档标题"
    assert reopened["machine"]["editable"]["overview"][0]["text"] == "原始概述。"
    assert reopened["latest_edit_id"] == saved["version_id"]
    assert reopened["selected"]["editable"]["chapters"][0]["title"] == "修改后的章节"
    assert len(reopened["versions"]) == 1

    edit_root = service._task_root("WEB01") / "edits" / saved["version_id"]
    assert (edit_root / "content.json").is_file()
    assert (edit_root / "manifest.json").is_file()
    assert "content_id" not in json.loads((edit_root / "manifest.json").read_text("utf-8"))


def test_invalid_edit_is_rejected(service: TaskService) -> None:
    initial = service.get_document("WEB01")
    editable = initial["machine"]["editable"]
    editable["title"] = "   "
    with pytest.raises(WebServiceError, match="标题不能为空"):
        service.save_edit(
            "WEB01",
            {
                "base_version": "machine",
                "version_name": "invalid",
                "editable": editable,
            },
        )


def test_interrupted_runner_is_recovered_on_startup(tmp_path: Path) -> None:
    output_root = tmp_path / "outputs"
    state_root = output_root / "_web" / "tasks"
    state_root.mkdir(parents=True)
    state = {
        "task_id": "RESTART01",
        "status": "running",
        "stage": "frames",
        "stage_status": "started",
        "progress": 42,
        "events": [],
        "source_video": None,
    }
    (state_root / "RESTART01.json").write_text(json.dumps(state), encoding="utf-8")

    recovered = TaskService(output_root).get_task("RESTART01")
    assert recovered["status"] == "failed"
    assert recovered["error"]["code"] == "RUNNER_INTERRUPTED"
    assert "重新运行" in recovered["stage_message"]


def test_pipeline_failure_fallback_reads_reason_code(tmp_path: Path) -> None:
    journal = tmp_path / "m2" / "logs" / "run-m2.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "\n".join(
            (
                '{"stage": "timeline_v2", "status": "completed"}',
                '{"stage": "chapter_plan_v2", "status": "failed", '
                '"reason_codes": ["LLM_APPROVAL_REQUIRED"]}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    failure = _latest_pipeline_failure(tmp_path)
    assert failure == {
        "error_code": "LLM_APPROVAL_REQUIRED",
        "failed_stage": "chapter_plan_v2",
        "reason_codes": ["LLM_APPROVAL_REQUIRED"],
        "message": "",
    }


def test_http_server_serves_app_and_health(service: TaskService) -> None:
    server = create_server("127.0.0.1", 0, service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["status"] == "ok"

        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert "知构" in html
        assert "/assets/app.js" in html

        with urllib.request.urlopen(f"{base}/assets/app.js", timeout=5) as response:
            script = response.read().decode("utf-8")
        assert "loadDocument" in script
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
