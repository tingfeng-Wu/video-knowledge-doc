"""阶段14-B：``plan-chapters-v2`` CLI接线定向测试。

沿用F3输出约定：成功stdout单条JSON（14键）退出0；typed失败stderr同类JSON
（7键）退出1；argparse用法错误由argparse打印usage退出2；KeyboardInterrupt
原样传播。默认传输接线经monkeypatch替身，全程离线零真实网络/API。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import video_doc.boundary_planner_v2 as bvp
import video_doc.cli as cli_module
from tests.test_boundary_planner_v2 import (
    GOOD_RESPONSE,
    SECRET,
    TASK,
    ScriptedTransport,
    _workspace,
)
from video_doc.boundary_planner_v2 import BoundaryPlannerV2Error
from video_doc.cli import main

SUCCESS_KEYS = {
    "task_id",
    "module",
    "status",
    "result_id",
    "result_source",
    "quality_level",
    "boundary_count",
    "duration_ms",
    "timeline_id",
    "business_call_count",
    "supplier_request_count",
    "cache_hit",
    "pointer_repaired",
    "group_relative_path",
}
FAILURE_KEYS = {
    "task_id",
    "module",
    "status",
    "error_code",
    "safe_message",
    "recoverable",
    "next_action",
}


def _argv(tmp_path, *extra: str) -> list[str]:
    return ["plan-chapters-v2", TASK, "--output-root", str(tmp_path / "outputs"), *extra]


@pytest.fixture()
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_DOC_LLM_API_KEY", SECRET)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, responses: list[Any]) -> ScriptedTransport:
    transport = ScriptedTransport(responses)
    monkeypatch.setattr(bvp, "OpenAICompatibleTransport", lambda: transport)
    return transport


def test_cli_success_then_cache_hit(tmp_path, capsys, offline_env, monkeypatch) -> None:
    _workspace(tmp_path)
    transport = _patch_transport(monkeypatch, [GOOD_RESPONSE])
    assert main(_argv(tmp_path)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    status = json.loads(captured.out)
    assert set(status) == SUCCESS_KEYS
    assert status["module"] == "boundary_planner_v2"
    assert status["status"] == "success"
    assert status["result_source"] == "primary"
    assert status["quality_level"] == "normal"
    assert status["boundary_count"] == 2
    assert status["business_call_count"] == 1
    assert status["supplier_request_count"] == 1
    assert status["cache_hit"] is False
    assert status["pointer_repaired"] is False
    assert status["group_relative_path"] == (
        f"m2/boundaries/formal-results/{status['result_id']}"
    )
    assert len(transport.calls) == 1

    # 复用轮：任何传输层调用都会AssertionError，早命中路径必须零调用。
    _patch_transport(monkeypatch, [])
    assert main(_argv(tmp_path)) == 0
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["cache_hit"] is True and rerun["result_id"] == status["result_id"]
    assert (tmp_path / "outputs" / TASK / status["group_relative_path"]).is_dir()


def test_cli_emergency_degraded_exit0(tmp_path, capsys, offline_env, monkeypatch) -> None:
    _workspace(tmp_path)
    _patch_transport(monkeypatch, [RuntimeError("boom-1"), RuntimeError("boom-2")])
    assert main(_argv(tmp_path)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "degraded"
    assert status["result_source"] == "emergency"
    assert status["quality_level"] == "degraded"
    assert status["boundary_count"] == 1
    assert status["supplier_request_count"] == 2


def test_cli_missing_timeline_exit1_stderr_json(tmp_path, capsys, offline_env, monkeypatch) -> None:
    root = _workspace(tmp_path, build=False)
    _patch_transport(monkeypatch, [])
    assert main(_argv(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert set(failure) == FAILURE_KEYS
    assert failure["module"] == "boundary_planner_v2"
    assert failure["status"] == "failed"
    assert failure["error_code"] == "INPUT_TIMELINE_MISSING"
    assert failure["recoverable"] is True
    assert failure["next_action"] == "run_build_timeline_v2"
    assert str(root) not in failure["safe_message"]


def test_cli_missing_api_key_exit1(tmp_path, capsys, monkeypatch) -> None:
    _workspace(tmp_path)
    # Isolate this missing-credential case from a developer's local .env.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("VIDEO_DOC_LLM_API_KEY", raising=False)
    transport = _patch_transport(monkeypatch, [GOOD_RESPONSE])
    assert main(_argv(tmp_path)) == 1
    failure = json.loads(capsys.readouterr().err)
    assert failure["error_code"] == "ROLE_CONFIG_INVALID"
    assert len(transport.calls) == 0


def test_cli_bad_registry_argument_exit1(tmp_path, capsys, offline_env, monkeypatch) -> None:
    _workspace(tmp_path)
    _patch_transport(monkeypatch, [GOOD_RESPONSE])
    code = main(_argv(tmp_path, "--model-registry", str(tmp_path / "ghost.json")))
    assert code == 1
    failure = json.loads(capsys.readouterr().err)
    assert failure["error_code"] == "ROLE_CONFIG_INVALID"
    assert failure["recoverable"] is True
    assert failure["next_action"] == "fix_model_registry"


def test_cli_typed_base_error_maps_to_failed(tmp_path, capsys, monkeypatch) -> None:
    def raiser(*args: Any, **kwargs: Any) -> Any:
        raise BoundaryPlannerV2Error("内部测试失败")

    monkeypatch.setattr(cli_module, "plan_chapters_v2", raiser)
    assert main(_argv(tmp_path)) == 1
    failure = json.loads(capsys.readouterr().err)
    assert failure["error_code"] == "BOUNDARY_PLAN_FAILED"
    assert failure["recoverable"] is False
    assert failure["next_action"] == "none"


def test_cli_keyboard_interrupt_propagates(tmp_path, monkeypatch) -> None:
    def raiser(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "plan_chapters_v2", raiser)
    with pytest.raises(KeyboardInterrupt):
        main(_argv(tmp_path))


def test_cli_usage_error_exit2(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err
