import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

import video_doc.cli as cli
import video_doc.llm_adapter as llm_adapter
from video_doc.llm_adapter import (
    LLMAdapterError,
    LLMCallResult,
    LLMConfig,
    LLMNonRetryableError,
    LLMRetryableError,
    OpenAICompatibleTransport,
    call_task_llm,
    load_llm_config,
)
from video_doc.task_workspace import initialize_task_workspace


MESSAGES = [{"role": "user", "content": "只返回JSON"}]


def _prepare_task(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    initialize_task_workspace(output_root, "DEV01", source_video=source)
    return output_root


def _mock_config(**changes: Any) -> LLMConfig:
    values: dict[str, Any] = {
        "backend": "mock",
        "base_url": "",
        "model": "",
        "timeout_seconds": 60.0,
        "api_key": "",
        "mock_response": {"mock": True},
        "enable_thinking": None,
    }
    values.update(changes)
    return LLMConfig(**values)


def _read_events(output_root: Path) -> list[dict[str, Any]]:
    path = output_root / "DEV01" / "logs" / "llm_calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class SequenceTransport:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[LLMConfig, object, object]] = []

    def complete(self, config: LLMConfig, messages: object, response_format: object) -> Any:
        self.calls.append((config, messages, response_format))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_load_config_defaults_to_mock() -> None:
    config = load_llm_config(environ={})
    assert config.backend == "mock"
    assert config.timeout_seconds == 60.0
    assert config.temperature == 0.0
    assert config.mock_response["mock"] is True


def test_dotenv_loads_values_but_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "VIDEO_DOC_LLM_BACKEND=mock\nVIDEO_DOC_LLM_TIMEOUT_SECONDS=12\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIDEO_DOC_LLM_TIMEOUT_SECONDS", "25")
    config = load_llm_config(dotenv_path=dotenv_path)
    assert config.timeout_seconds == 25.0


@pytest.mark.parametrize("value", ["0", "-1", "nan", "abc"])
def test_config_rejects_invalid_timeout(value: str) -> None:
    with pytest.raises(LLMAdapterError, match="必须是正数"):
        load_llm_config(
            environ={
                "VIDEO_DOC_LLM_BACKEND": "mock",
                "VIDEO_DOC_LLM_TIMEOUT_SECONDS": value,
            }
        )


def test_real_config_requires_url_key_and_model() -> None:
    with pytest.raises(LLMAdapterError, match="BASE_URL"):
        load_llm_config(environ={"VIDEO_DOC_LLM_BACKEND": "openai_compatible"})


def test_config_repr_does_not_expose_key() -> None:
    secret = "top-secret-key"
    config = _mock_config(api_key=secret)
    assert secret not in repr(config)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("false", False), ("0", False), ("", None)],
)
def test_config_parses_optional_thinking_switch(
    raw: str, expected: bool | None
) -> None:
    config = load_llm_config(
        environ={
            "VIDEO_DOC_LLM_BACKEND": "mock",
            "VIDEO_DOC_LLM_ENABLE_THINKING": raw,
        }
    )
    assert config.enable_thinking is expected


def test_config_rejects_invalid_thinking_switch() -> None:
    with pytest.raises(LLMAdapterError, match="ENABLE_THINKING"):
        load_llm_config(
            environ={
                "VIDEO_DOC_LLM_BACKEND": "mock",
                "VIDEO_DOC_LLM_ENABLE_THINKING": "sometimes",
            }
        )


@pytest.mark.parametrize("value", ["-0.1", "2.1", "nan", "abc"])
def test_config_rejects_invalid_temperature(value: str) -> None:
    with pytest.raises(LLMAdapterError, match="TEMPERATURE"):
        load_llm_config(
            environ={
                "VIDEO_DOC_LLM_BACKEND": "mock",
                "VIDEO_DOC_LLM_TEMPERATURE": value,
            }
        )


def test_openai_compatible_transport_sends_timeout_and_parses_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": '{"ok": true}'}}]}
            ).encode("utf-8")

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(llm_adapter.urllib.request, "urlopen", fake_urlopen)
    config = _mock_config(
        backend="openai_compatible",
        base_url="https://example.invalid/v1/chat/completions",
        model="example-model",
        api_key="secret-key",
        timeout_seconds=17.0,
        enable_thinking=False,
        max_tokens=1800,
    )
    result = OpenAICompatibleTransport().complete(
        config,
        MESSAGES,
        {"type": "json_object"},
    )
    request = captured["request"]
    assert result == '{"ok": true}'
    assert captured["timeout"] == 17.0
    assert request.full_url == config.base_url
    assert request.get_header("Authorization") == "Bearer secret-key"
    sent = json.loads(request.data.decode("utf-8"))
    assert sent["model"] == "example-model"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["enable_thinking"] is False
    assert sent["temperature"] == 0.0
    assert sent["max_tokens"] == 1800


def test_anonymous_public_transport_omits_unsupported_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {"message": {"content": '```json\n{"ok": true}\n```'}}
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(llm_adapter.urllib.request, "urlopen", fake_urlopen)
    config = _mock_config(
        backend="openai_compatible",
        base_url="https://text.pollinations.ai/openai",
        model="openai",
        api_key="anonymous",
        enable_thinking=False,
        max_tokens=1800,
    )
    result = OpenAICompatibleTransport().complete(
        config,
        MESSAGES,
        {"type": "json_object"},
    )

    request = captured["request"]
    sent = json.loads(request.data.decode("utf-8"))
    assert request.get_header("Authorization") is None
    assert "response_format" not in sent
    assert "enable_thinking" not in sent
    assert "max_tokens" not in sent
    assert llm_adapter._structured_response(result) == {"ok": True}


def test_max_attempts_one_never_retries_validation_failure(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport({"value": -1}, {"value": 2})

    def validator(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["value"] != 2:
            raise LLMRetryableError("value必须等于2")
        return payload

    with pytest.raises(LLMRetryableError, match="value必须等于2"):
        call_task_llm(
            output_root,
            "DEV01",
            call_id="one-shot-validation",
            messages=MESSAGES,
            config=_mock_config(),
            transport=transport,
            response_validator=validator,
            validation_id="one-shot-validator",
            save_validation_audit=True,
            max_attempts=1,
        )

    assert len(transport.calls) == 1


def test_temperature_change_invalidates_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    call_task_llm(
        output_root,
        "DEV01",
        call_id="temperature",
        messages=MESSAGES,
        config=_mock_config(temperature=0.0),
        transport=SequenceTransport({"value": 1}),
    )
    transport = SequenceTransport({"value": 2})
    result = call_task_llm(
        output_root,
        "DEV01",
        call_id="temperature",
        messages=MESSAGES,
        config=_mock_config(temperature=0.2),
        transport=transport,
    )

    assert result.cache_hit is False
    assert len(transport.calls) == 1
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["provider"]["temperature"] == 0.2


def test_business_retry_includes_invalid_response_and_validation_feedback(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport({"value": -1}, {"value": 2})

    def validate(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("value") != 2:
            raise LLMRetryableError("value必须等于2。")
        return response

    result = call_task_llm(
        output_root,
        "DEV01",
        call_id="feedback-retry",
        messages=MESSAGES,
        config=_mock_config(),
        transport=transport,
        response_validator=validate,
        validation_id="feedback-validation",
    )

    assert result.attempt_count == 2
    assert len(transport.calls) == 2
    retry_messages = transport.calls[1][1]
    assert retry_messages[:-2] == MESSAGES
    assert json.loads(retry_messages[-2]["content"]) == {"value": -1}
    assert "value必须等于2" in retry_messages[-1]["content"]
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["request"]["retry_policy_version"] == "validation-feedback-1.0"


def test_validation_audit_preserves_two_invalid_responses_and_redacts_key(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    secret = "never-save-this-key"
    responses = (
        {"value": -1, "echo": secret},
        {"value": -2, "echo": secret},
    )

    def validate(response: dict[str, Any]) -> dict[str, Any]:
        raise LLMRetryableError(f"value {response['value']} invalid {secret}")

    with pytest.raises(LLMRetryableError):
        call_task_llm(
            output_root,
            "DEV01",
            call_id="audited-failure",
            messages=MESSAGES,
            config=_mock_config(api_key=secret),
            transport=SequenceTransport(*responses),
            response_validator=validate,
            validation_id="audited-validation",
            artifact_subdir="experiments/stage8/direct/event-id-v2/run-01",
            cache_namespace="event-id-v2-run-01",
            save_validation_audit=True,
        )

    audit_path = (
        output_root
        / "DEV01"
        / "experiments"
        / "stage8"
        / "direct"
        / "event-id-v2"
        / "run-01"
        / "audited-failure-validation-audit.json"
    )
    audit_text = audit_path.read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit["status"] == "failed"
    assert [attempt["attempt"] for attempt in audit["attempts"]] == [1, 2]
    assert [attempt["raw_response"]["value"] for attempt in audit["attempts"]] == [
        -1,
        -2,
    ]
    assert secret not in audit_text
    assert "[REDACTED]" in audit_text
    assert not audit_path.with_name(f"{audit_path.name}.tmp").exists()


def test_artifact_subdir_and_namespace_isolate_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    first = call_task_llm(
        output_root,
        "DEV01",
        call_id="experiment-call",
        messages=MESSAGES,
        config=_mock_config(),
        transport=SequenceTransport({"run": 1}),
        artifact_subdir="experiments/stage8/direct/run-01",
        cache_namespace="run-01",
    )
    cached = call_task_llm(
        output_root,
        "DEV01",
        call_id="experiment-call",
        messages=MESSAGES,
        config=_mock_config(),
        transport=SequenceTransport(),
        artifact_subdir="experiments/stage8/direct/run-01",
        cache_namespace="run-01",
    )
    second = call_task_llm(
        output_root,
        "DEV01",
        call_id="experiment-call",
        messages=MESSAGES,
        config=_mock_config(),
        transport=SequenceTransport({"run": 2}),
        artifact_subdir="experiments/stage8/direct/run-02",
        cache_namespace="run-02",
    )

    assert first.output_path != second.output_path
    assert cached.cache_hit is True
    assert second.cache_hit is False
    assert first.response == {"run": 1}
    assert second.response == {"run": 2}


def test_artifact_subdir_rejects_escape(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    with pytest.raises(LLMAdapterError, match="子目录"):
        call_task_llm(
            output_root,
            "DEV01",
            call_id="unsafe",
            messages=MESSAGES,
            config=_mock_config(),
            artifact_subdir="../outside",
        )


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, LLMNonRetryableError), (500, LLMRetryableError)],
)
def test_openai_compatible_transport_classifies_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error_type: type[LLMAdapterError],
) -> None:
    def fake_urlopen(*args: object, **kwargs: object) -> None:
        raise urllib.error.HTTPError(
            "https://example.invalid", status, "failed", None, None
        )

    monkeypatch.setattr(llm_adapter.urllib.request, "urlopen", fake_urlopen)
    config = _mock_config(
        backend="openai_compatible",
        base_url="https://example.invalid/v1/chat/completions",
        model="example-model",
        api_key="secret-key",
    )
    with pytest.raises(error_type):
        OpenAICompatibleTransport().complete(
            config,
            MESSAGES,
            {"type": "json_object"},
        )


def test_mock_writes_structured_result_and_logs(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    result = call_task_llm(
        output_root,
        "DEV01",
        call_id="smoke",
        messages=MESSAGES,
        config=_mock_config(mock_response={"ok": True}),
    )
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert result.response == {"ok": True}
    assert result.raw_response is None
    assert result.attempt_count == 1
    assert result.cache_hit is False
    assert payload["schema_version"] == "1.0"
    assert payload["response"] == {"ok": True}
    assert "raw_response" not in payload
    assert [event["event"] for event in _read_events(output_root)] == [
        "started",
        "completed",
    ]
    assert not result.output_path.with_name("smoke.json.tmp").exists()


def test_cache_hit_skips_transport_and_preserves_file(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    config = _mock_config(mock_response={"ok": True})
    transport = SequenceTransport({"ok": True})
    first = call_task_llm(
        output_root,
        "DEV01",
        call_id="smoke",
        messages=MESSAGES,
        config=config,
        transport=transport,
    )
    original_bytes = first.output_path.read_bytes()
    original_mtime = first.output_path.stat().st_mtime_ns
    second = call_task_llm(
        output_root,
        "DEV01",
        call_id="smoke",
        messages=MESSAGES,
        config=config,
        transport=SequenceTransport(AssertionError("缓存命中时不应调用transport")),
    )
    assert second.cache_hit is True
    assert len(transport.calls) == 1
    assert first.output_path.read_bytes() == original_bytes
    assert first.output_path.stat().st_mtime_ns == original_mtime
    assert [event["event"] for event in _read_events(output_root)][-1] == "cache_hit"


def test_changed_messages_invalidate_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    config = _mock_config()
    call_task_llm(
        output_root,
        "DEV01",
        call_id="smoke",
        messages=MESSAGES,
        config=config,
    )
    transport = SequenceTransport({"changed": True})
    result = call_task_llm(
        output_root,
        "DEV01",
        call_id="smoke",
        messages=[{"role": "user", "content": "changed"}],
        config=config,
        transport=transport,
    )
    assert result.cache_hit is False
    assert len(transport.calls) == 1


def test_damaged_cache_is_replaced(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    output_path = output_root / "DEV01" / "llm" / "smoke.json"
    output_path.write_text("{broken", encoding="utf-8")
    result = call_task_llm(
        output_root,
        "DEV01",
        call_id="smoke",
        messages=MESSAGES,
        config=_mock_config(),
    )
    assert result.cache_hit is False
    assert json.loads(output_path.read_text(encoding="utf-8"))["response"]


def test_retryable_failure_retries_once_then_succeeds(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport(LLMRetryableError("timeout"), '{"ok": true}')
    result = call_task_llm(
        output_root,
        "DEV01",
        call_id="retry",
        messages=MESSAGES,
        config=_mock_config(),
        transport=transport,
    )
    assert result.attempt_count == 2
    assert len(transport.calls) == 2
    assert [event["event"] for event in _read_events(output_root)] == [
        "started",
        "retrying",
        "completed",
    ]


def test_invalid_json_retries_only_once(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport("not-json", "still-not-json")
    with pytest.raises(LLMRetryableError, match="不是合法JSON"):
        call_task_llm(
            output_root,
            "DEV01",
            call_id="invalid",
            messages=MESSAGES,
            config=_mock_config(),
            transport=transport,
        )
    assert len(transport.calls) == 2
    assert [event["event"] for event in _read_events(output_root)] == [
        "started",
        "retrying",
        "failed",
    ]


def test_non_retryable_failure_is_not_retried(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    transport = SequenceTransport(LLMNonRetryableError("unauthorized"))
    with pytest.raises(LLMNonRetryableError, match="unauthorized"):
        call_task_llm(
            output_root,
            "DEV01",
            call_id="auth",
            messages=MESSAGES,
            config=_mock_config(),
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_failure_preserves_existing_output_and_redacts_key(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    output_path = output_root / "DEV01" / "llm" / "safe.json"
    output_path.write_text("previous-result", encoding="utf-8")
    secret = "never-log-this-secret"
    transport = SequenceTransport(LLMNonRetryableError(f"bad {secret}"))
    with pytest.raises(LLMNonRetryableError) as caught:
        call_task_llm(
            output_root,
            "DEV01",
            call_id="safe",
            messages=MESSAGES,
            config=_mock_config(api_key=secret),
            transport=transport,
        )
    assert output_path.read_text(encoding="utf-8") == "previous-result"
    assert secret not in str(caught.value)
    assert secret not in (
        output_root / "DEV01" / "logs" / "llm_calls.jsonl"
    ).read_text(encoding="utf-8")


def test_rejects_unsafe_call_id(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    with pytest.raises(LLMAdapterError, match="call_id"):
        call_task_llm(
            output_root,
            "DEV01",
            call_id="../escape",
            messages=MESSAGES,
            config=_mock_config(),
        )


def test_cli_prints_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({"messages": MESSAGES}), encoding="utf-8")
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        cli,
        "call_task_llm",
        lambda *args, **kwargs: LLMCallResult(
            output_path=result_path,
            response={"ok": True},
            backend="mock",
            model="",
            attempt_count=1,
            cache_hit=False,
        ),
    )
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "video-doc",
            "call-llm",
            "DEV01",
            "--output-root",
            str(tmp_path),
            "--call-id",
            "smoke",
            "--request-file",
            str(request_path),
        ],
    )
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"backend": "mock"' in output
    assert '"cache_hit": false' in output
    assert "LLM调用完成" in output
