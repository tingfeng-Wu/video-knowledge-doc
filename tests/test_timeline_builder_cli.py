"""阶段13-2：`build-timeline-v2` CLI接线定向测试。

只测试CLI层：成功摘要、缓存命中、off/auto/required开关语义、类型化失败
（stderr JSON、退出码1）、argparse用法错误（退出码2）、失败摘要脱敏。
fixture复用tests/test_timeline_builder_v2.py的`_workspace`等助手，
不重复实现，不触碰真实outputs、不调用真实引擎或API。
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.test_timeline_builder_v2 import (
    TASK,
    _workspace,
)
from video_doc import cli

_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _invoke_allow_exit(argv: list[str]) -> tuple[int, str, str]:
    """argparse用法错误经SystemExit退出；在重定向内捕获以便检查stderr。"""

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _argv(root: Path, ocr: str) -> list[str]:
    return ["build-timeline-v2", TASK, "--output-root", str(root), "--ocr", ocr]


# ---------------------------------------------------------------------------
# 成功路径与缓存
# ---------------------------------------------------------------------------


def test_cli_auto_success_summary_and_cache_hit(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    argv = _argv(root, "auto")
    code, out, err = _invoke(argv)
    assert code == 0, err
    assert err.strip() == ""  # 成功摘要只走stdout
    summary: dict[str, Any] = json.loads(out)
    assert summary["task_id"] == TASK
    assert summary["module"] == "timelines"
    assert summary["status"] == "success"
    timeline_id = summary["timeline_id"]
    assert isinstance(timeline_id, str) and timeline_id.startswith(
        "unified-timeline-"
    )
    assert summary["quality_level"] == "multimodal"
    assert summary["ocr_request"] == "auto"
    assert isinstance(summary["duration_ms"], int)
    counts = summary["event_counts"]
    assert set(counts) == {"total", "transcript", "page_change", "ocr"}
    assert counts["total"] == counts["transcript"] + counts["page_change"] + counts["ocr"]
    assert counts["transcript"] >= 1
    statuses = summary["modality_statuses"]
    assert set(statuses) == {"transcript", "frame_change", "ocr"}
    assert all(v == "available" for v in statuses.values())
    assert (
        summary["group_relative_path"]
        == f"m2/timelines/formal-results/{timeline_id}"
    )
    assert summary["cache_hit"] is False
    assert summary["pointer_repaired"] is False
    assert (root / TASK / "m2" / "timelines" / "current.json").is_file()

    # 同参数复跑：缓存命中、同一timeline_id。
    code2, out2, err2 = _invoke(argv)
    assert code2 == 0, err2
    second = json.loads(out2)
    assert second["cache_hit"] is True
    assert second["timeline_id"] == timeline_id


# ---------------------------------------------------------------------------
# --ocr开关语义
# ---------------------------------------------------------------------------


def test_cli_off_disables_ocr_keeps_text(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    code, out, err = _invoke(_argv(root, "off"))
    assert code == 0, err
    off = json.loads(out)
    assert off["ocr_request"] == "off"
    assert off["modality_statuses"]["ocr"] == "disabled"
    assert off["event_counts"]["ocr"] == 0

    code2, out2, err2 = _invoke(_argv(root, "auto"))
    assert code2 == 0, err2
    auto = json.loads(out2)
    # 开关不改变文本事件数量。
    assert auto["event_counts"]["transcript"] == off["event_counts"]["transcript"]


def test_cli_required_without_ocr_exits_one(tmp_path: Path) -> None:
    root = _workspace(tmp_path, publish_ocr=False)
    code, out, err = _invoke(_argv(root, "required"))
    assert code == 1, out
    failure = json.loads(err)
    assert failure["task_id"] == TASK
    assert failure["module"] == "timelines"
    assert failure["status"] == "failed"
    assert failure["error_code"] == "INPUT_OCR_SET_MISSING"
    assert failure["recoverable"] is True
    assert failure["next_action"] == "run_ocr"
    # stdout不得出现成功JSON。
    assert out.strip() == "" or "timeline_id" not in out


def test_cli_missing_m1_timeline_exits_one(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir(parents=True)
    code, out, err = _invoke(_argv(root, "auto"))
    assert code == 1, out
    failure = json.loads(err)
    assert failure["module"] == "timelines"
    assert failure["status"] == "failed"
    assert failure["error_code"] == "INPUT_ASR_TIMELINE_MISSING"
    assert failure["recoverable"] is True
    assert failure["next_action"] == "run_m1_pipeline"


# ---------------------------------------------------------------------------
# text_only降级
# ---------------------------------------------------------------------------


def test_cli_text_only_when_no_visual_sources(tmp_path: Path) -> None:
    root = _workspace(tmp_path, publish_frames=False)
    code, out, err = _invoke(_argv(root, "auto"))
    assert code == 0, err
    summary = json.loads(out)
    assert summary["quality_level"] == "text_only"
    assert summary["modality_statuses"] == {
        "transcript": "available",
        "frame_change": "not_run",
        "ocr": "not_run",
    }
    counts = summary["event_counts"]
    assert counts["page_change"] == 0
    assert counts["ocr"] == 0
    assert counts["transcript"] >= 1


# ---------------------------------------------------------------------------
# 用法错误（argparse）
# ---------------------------------------------------------------------------


def test_cli_bad_ocr_choice_exits_two(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    code, out, err = _invoke_allow_exit(_argv(root, "bogus"))
    assert code == 2
    assert "bogus" in err  # argparse回显非法choice
    assert '"error_code"' not in err  # 不是类型化失败JSON


def test_cli_missing_task_id_exits_two() -> None:
    code, out, err = _invoke_allow_exit(["build-timeline-v2"])
    assert code == 2


# ---------------------------------------------------------------------------
# 失败摘要脱敏
# ---------------------------------------------------------------------------


def test_cli_failure_output_has_no_absolute_paths(tmp_path: Path) -> None:
    root = _workspace(tmp_path, publish_ocr=False)
    code, out, err = _invoke(_argv(root, "required"))
    assert code == 1, out
    assert str(root) not in err
    assert _ABSOLUTE.search(err) is None
    lowered = err.lower()
    for forbidden in ("api", "key", "secret", "token"):
        assert forbidden not in lowered
