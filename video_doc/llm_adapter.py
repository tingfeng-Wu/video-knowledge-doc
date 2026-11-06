"""供应商隔离的结构化LLM调用、缓存与日志。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import time
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from video_doc.task_workspace import (
    TaskWorkspaceError,
    load_task_manifest,
    validate_task_id,
)


LLM_SCHEMA_VERSION = "1.0"
DEFAULT_BACKEND = "mock"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_TEMPERATURE = 0.0
VALIDATION_RETRY_POLICY_VERSION = "validation-feedback-1.0"
DEFAULT_MOCK_RESPONSE: dict[str, Any] = {
    "mock": True,
    "message": "local mock response",
}
SUPPORTED_BACKENDS = ("mock", "openai_compatible")
_CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MESSAGE_ROLES = {"system", "user", "assistant"}
_PUBLIC_ENDPOINT_LOCK = threading.Lock()
_PUBLIC_ENDPOINT_LAST_REQUEST = 0.0
_PUBLIC_ENDPOINT_MIN_INTERVAL_SECONDS = 31.0


class LLMAdapterError(RuntimeError):
    """LLM配置、调用、校验或保存失败。"""


class LLMRetryableError(LLMAdapterError):
    """允许由适配器重试一次的临时调用错误。"""


class LLMNonRetryableError(LLMAdapterError):
    """无需重试的确定性调用错误。"""


@dataclass(frozen=True)
class LLMConfig:
    """从环境变量加载的LLM配置；密钥不会出现在repr中。"""

    backend: str
    base_url: str
    model: str
    timeout_seconds: float
    api_key: str = field(repr=False)
    mock_response: dict[str, Any] = field(repr=False)
    enable_thinking: bool | None = None
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int | None = None

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "backend": self.backend,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
        }
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        return payload


@dataclass(frozen=True)
class LLMCallResult:
    """一次结构化LLM调用的关键结果。"""

    output_path: Path
    response: dict[str, Any]
    backend: str
    model: str
    attempt_count: int
    cache_hit: bool
    raw_response: dict[str, Any] | None = None


class LLMTransport(Protocol):
    """具体供应商传输实现必须满足的最小协议。"""

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any: ...


ResponseValidator = Callable[[dict[str, Any]], dict[str, Any]]


def _load_dotenv_file(dotenv_path: str | Path | None) -> None:
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError as exc:
        raise LLMAdapterError(
            "未安装python-dotenv，请先安装项目运行依赖。"
        ) from exc

    if dotenv_path is None:
        discovered = find_dotenv(usecwd=True)
        if discovered:
            load_dotenv(discovered, override=False)
        return
    path = Path(dotenv_path).expanduser()
    if path.is_file():
        load_dotenv(path, override=False)


def _parse_mock_response(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return dict(DEFAULT_MOCK_RESPONSE)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMAdapterError(
            "VIDEO_DOC_LLM_MOCK_RESPONSE必须是合法JSON对象。"
        ) from exc
    if not isinstance(payload, dict):
        raise LLMAdapterError("VIDEO_DOC_LLM_MOCK_RESPONSE顶层必须是JSON对象。")
    return payload


def _parse_optional_boolean(raw: str, *, variable_name: str) -> bool | None:
    normalized = raw.strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise LLMAdapterError(f"{variable_name}必须是true或false。")


def _parse_temperature(raw: str) -> float:
    try:
        temperature = float(raw.strip())
    except ValueError as exc:
        raise LLMAdapterError("VIDEO_DOC_LLM_TEMPERATURE必须是0到2之间的数字。") from exc
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise LLMAdapterError("VIDEO_DOC_LLM_TEMPERATURE必须是0到2之间的数字。")
    return temperature


def load_llm_config(
    *, dotenv_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> LLMConfig:
    """加载.env和环境变量；已有系统环境变量优先于.env。"""

    if environ is None:
        _load_dotenv_file(dotenv_path)
        values: Mapping[str, str] = os.environ
    else:
        values = environ

    backend = values.get("VIDEO_DOC_LLM_BACKEND", DEFAULT_BACKEND).strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        raise LLMAdapterError(
            "VIDEO_DOC_LLM_BACKEND必须是mock或openai_compatible。"
        )
    timeout_raw = values.get(
        "VIDEO_DOC_LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)
    ).strip()
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError as exc:
        raise LLMAdapterError("VIDEO_DOC_LLM_TIMEOUT_SECONDS必须是正数。") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise LLMAdapterError("VIDEO_DOC_LLM_TIMEOUT_SECONDS必须是正数。")

    base_url = values.get("VIDEO_DOC_LLM_BASE_URL", "").strip()
    api_key = values.get("VIDEO_DOC_LLM_API_KEY", "").strip()
    model = values.get("VIDEO_DOC_LLM_MODEL", "").strip()
    mock_response = _parse_mock_response(
        values.get("VIDEO_DOC_LLM_MOCK_RESPONSE", "")
    )
    enable_thinking = _parse_optional_boolean(
        values.get("VIDEO_DOC_LLM_ENABLE_THINKING", ""),
        variable_name="VIDEO_DOC_LLM_ENABLE_THINKING",
    )
    temperature = _parse_temperature(
        values.get("VIDEO_DOC_LLM_TEMPERATURE", str(DEFAULT_TEMPERATURE))
    )
    if backend == "openai_compatible":
        if not base_url.startswith(("https://", "http://")):
            raise LLMAdapterError(
                "真实LLM模式需要有效的VIDEO_DOC_LLM_BASE_URL。"
            )
        if not api_key:
            raise LLMAdapterError(
                "真实LLM模式缺少VIDEO_DOC_LLM_API_KEY。"
            )
        if not model:
            raise LLMAdapterError("真实LLM模式缺少VIDEO_DOC_LLM_MODEL。")

    return LLMConfig(
        backend=backend,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        mock_response=mock_response,
        enable_thinking=enable_thinking,
        temperature=temperature,
    )


def validate_call_id(call_id: str) -> None:
    if not isinstance(call_id, str) or not _CALL_ID_PATTERN.fullmatch(call_id):
        raise LLMAdapterError(
            "call_id只能包含英文字母、数字、下划线和连字符，"
            "必须以字母或数字开头，长度不超过64。"
        )


def _validated_artifact_subdir(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts:
        raise LLMAdapterError("LLM产物子目录必须是任务目录内的相对路径。")
    for part in path.parts:
        if part in {".", ".."} or not _CALL_ID_PATTERN.fullmatch(part):
            raise LLMAdapterError("LLM产物子目录包含不安全的路径片段。")
    return path


def _validated_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise LLMAdapterError("LLM messages必须是非空数组。")
    normalized: list[dict[str, str]] = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            raise LLMAdapterError(f"第{index}条LLM消息必须是JSON对象。")
        role = message.get("role")
        content = message.get("content")
        if role not in _MESSAGE_ROLES:
            raise LLMAdapterError(f"第{index}条LLM消息role无效。")
        if not isinstance(content, str) or not content.strip():
            raise LLMAdapterError(f"第{index}条LLM消息content不能为空。")
        normalized.append({"role": role, "content": content.strip()})
    return normalized


def _validated_response_format(response_format: Any) -> dict[str, Any]:
    if response_format is None:
        return {"type": "json_object"}
    if not isinstance(response_format, dict) or not response_format:
        raise LLMAdapterError("response_format必须是非空JSON对象。")
    try:
        json.dumps(response_format, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise LLMAdapterError("response_format包含不可序列化内容。") from exc
    return response_format


def load_llm_request_file(path: str | Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    request_path = Path(path).expanduser()
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LLMAdapterError(f"无法读取LLM请求文件：{request_path}") from exc
    except json.JSONDecodeError as exc:
        raise LLMAdapterError(f"LLM请求文件不是合法JSON：{request_path}") from exc
    if not isinstance(payload, dict):
        raise LLMAdapterError("LLM请求文件顶层必须是JSON对象。")
    return (
        _validated_messages(payload.get("messages")),
        _validated_response_format(payload.get("response_format")),
    )


class MockLLMTransport:
    """确定性的本地结构化响应，不访问网络。"""

    def __init__(self, response: Any | None = None) -> None:
        self.response = response

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        del messages, response_format
        return config.mock_response if self.response is None else self.response


class OpenAICompatibleTransport:
    """隔离OpenAI-compatible Chat Completions HTTP格式。"""

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        anonymous_endpoint = (
            urlsplit(config.base_url).hostname == "text.pollinations.ai"
        )
        global _PUBLIC_ENDPOINT_LAST_REQUEST
        if anonymous_endpoint:
            with _PUBLIC_ENDPOINT_LOCK:
                wait_seconds = _PUBLIC_ENDPOINT_MIN_INTERVAL_SECONDS - (
                    time.monotonic() - _PUBLIC_ENDPOINT_LAST_REQUEST
                )
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                _PUBLIC_ENDPOINT_LAST_REQUEST = time.monotonic()
        request_payload: dict[str, Any] = {
            "model": config.model,
            "messages": list(messages),
            "temperature": config.temperature,
        }
        if anonymous_endpoint:
            request_payload["reasoning_effort"] = "low"
            system_text = "\n".join(
                str(message.get("content", ""))
                for message in messages
                if message.get("role") == "system"
            )
            if "source_unit_ids" in system_text and "paragraphs" in system_text:
                compact_instruction = (
                    "\n\n匿名端点输出长度有限。请用简洁总结代替逐句转写；"
                    "每段不超过180个汉字，每批最多2段，仍须覆盖全部"
                    "source_unit_ids并保持给定JSON结构。"
                )
                for message in request_payload["messages"]:
                    if message.get("role") == "system":
                        message["content"] = str(message["content"]) + compact_instruction
                        break
        if not anonymous_endpoint:
            request_payload["response_format"] = dict(response_format)
            if config.enable_thinking is not None:
                request_payload["enable_thinking"] = config.enable_thinking
            if config.max_tokens is not None:
                request_payload["max_tokens"] = config.max_tokens
        body = json.dumps(
            request_payload,
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if not anonymous_endpoint:
            headers["Authorization"] = f"Bearer {config.api_key}"
        request = urllib.request.Request(
            config.base_url,
            data=body,
            headers=headers,
            method="POST",
        )
        raw_payload = ""
        for request_attempt in range(2):
            try:
                with urllib.request.urlopen(
                    request, timeout=config.timeout_seconds
                ) as response:
                    raw_payload = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                error_detail = _http_error_detail(exc)
                if (
                    anonymous_endpoint
                    and exc.code == 402
                    and request_attempt == 0
                ):
                    retry_after = _public_retry_after_seconds(exc.headers)
                    time.sleep(retry_after)
                    continue
                if exc.code in {408, 409, 429} or exc.code >= 500:
                    raise LLMRetryableError(
                        f"LLM服务暂时不可用（HTTP {exc.code}）：{error_detail}"
                    ) from exc
                raise LLMNonRetryableError(
                    f"LLM请求被拒绝（HTTP {exc.code}）：{error_detail}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                raise LLMRetryableError("LLM网络请求失败或超时。") from exc
            except OSError as exc:
                raise LLMRetryableError("LLM网络请求失败。") from exc

        try:
            payload = json.loads(raw_payload)
            return payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMRetryableError("LLM服务响应格式无效。") from exc


def _public_retry_after_seconds(headers: Any) -> float:
    value = headers.get("Retry-After", "") if headers is not None else ""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = _PUBLIC_ENDPOINT_MIN_INTERVAL_SECONDS
    return min(45.0, max(1.0, seconds))


def _http_error_detail(error: urllib.error.HTTPError) -> str:
    try:
        payload = error.read()
        text = payload.decode("utf-8", errors="replace").strip()
    except (OSError, ValueError, AttributeError):
        return "无错误详情"
    return " ".join(text.split())[:300] or "无错误详情"


def _default_transport(config: LLMConfig) -> LLMTransport:
    if config.backend == "mock":
        return MockLLMTransport()
    return OpenAICompatibleTransport()


def _structured_response(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise LLMRetryableError("LLM未返回非空的结构化JSON对象。")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise LLMRetryableError("LLM返回内容不是合法JSON。") from exc
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested:
            raise LLMRetryableError("LLM返回内容不是合法JSON。") from nested
    if not isinstance(payload, dict):
        raise LLMRetryableError("LLM返回JSON顶层不是对象。")
    return payload


def _domain_validated_response(
    response: dict[str, Any],
    response_validator: ResponseValidator | None,
) -> dict[str, Any]:
    if response_validator is None:
        return response
    try:
        normalized = response_validator(response)
    except LLMAdapterError:
        raise
    except Exception as exc:
        raise LLMAdapterError("LLM业务响应校验器执行失败。") from exc
    if not isinstance(normalized, dict):
        raise LLMAdapterError("LLM业务响应校验器必须返回JSON对象。")
    return normalized


def _build_cache_key(
    config: LLMConfig,
    messages: list[dict[str, str]],
    response_format: dict[str, Any],
    validation_id: str | None,
    cache_namespace: str | None,
    max_attempts: int = 2,
) -> str:
    cache_input = {
        "schema_version": LLM_SCHEMA_VERSION,
        "provider": config.public_dict(),
        "messages": messages,
        "response_format": response_format,
        "mock_response": config.mock_response if config.backend == "mock" else None,
    }
    if validation_id is not None:
        cache_input["validation_id"] = validation_id
        cache_input["retry_policy_version"] = VALIDATION_RETRY_POLICY_VERSION
    if cache_namespace is not None:
        cache_input["cache_namespace"] = cache_namespace
    if max_attempts != 2:
        cache_input["max_attempts"] = max_attempts
    encoded = json.dumps(
        cache_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _remove_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _safe_error(error: BaseException, api_key: str) -> str:
    message = str(error)
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message


def _redact_audit_value(value: Any, api_key: str) -> Any:
    """递归清理失败审计中的密钥，保留模型原始响应结构。"""

    if isinstance(value, str):
        return value.replace(api_key, "[REDACTED]") if api_key else value
    if isinstance(value, list):
        return [_redact_audit_value(item, api_key) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _redact_audit_value(item, api_key)
            for key, item in value.items()
        }
    return value


def _write_validation_audit(
    path: Path,
    *,
    task_id: str,
    call_id: str,
    cache_key: str,
    provider: dict[str, Any],
    validation_id: str,
    status: str,
    attempts: list[dict[str, Any]],
) -> None:
    temporary_path = path.with_name(f"{path.name}.tmp")
    payload = {
        "schema_version": LLM_SCHEMA_VERSION,
        "task_id": task_id,
        "call_id": call_id,
        "cache_key": cache_key,
        "provider": provider,
        "validation_id": validation_id,
        "status": status,
        "attempts": attempts,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _remove_temporary_file(temporary_path)
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        raise LLMAdapterError(f"无法保存LLM校验审计：{path}") from exc
    finally:
        _remove_temporary_file(temporary_path)


def _append_llm_log(
    task_root: Path,
    *,
    event: str,
    task_id: str,
    call_id: str,
    cache_key: str,
    provider: dict[str, Any],
    elapsed_seconds: float,
    output_path: Path,
    attempt_count: int,
    error: str | None = None,
) -> None:
    log_path = task_root / "logs" / "llm_calls.jsonl"
    payload: dict[str, Any] = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "event": event,
        "task_id": task_id,
        "call_id": call_id,
        "cache_key": cache_key,
        "provider": provider,
        "attempt_count": attempt_count,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "output_path": str(output_path.resolve()),
    }
    if error is not None:
        payload["error"] = error
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise LLMAdapterError(f"无法写入LLM日志：{log_path}") from exc


def _load_cached_result(
    output_path: Path,
    *,
    task_id: str,
    call_id: str,
    cache_key: str,
    provider: dict[str, Any],
    request_payload: dict[str, Any],
    response_validator: ResponseValidator | None,
) -> LLMCallResult | None:
    if not output_path.is_file():
        return None
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != LLM_SCHEMA_VERSION:
            return None
        if payload.get("task_id") != task_id or payload.get("call_id") != call_id:
            return None
        if payload.get("cache_key") != cache_key:
            return None
        if payload.get("provider") != provider:
            return None
        if payload.get("request") != request_payload:
            return None
        response = payload.get("response")
        raw_response = payload.get("raw_response")
        attempt_count = payload.get("attempt_count")
        if not isinstance(response, dict):
            return None
        if response_validator is not None:
            if not isinstance(raw_response, dict):
                return None
            if _domain_validated_response(raw_response, response_validator) != response:
                return None
        elif raw_response is not None:
            return None
        if not isinstance(attempt_count, int) or not 1 <= attempt_count <= 2:
            return None
    except (LLMAdapterError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return LLMCallResult(
        output_path=output_path,
        response=response,
        backend=str(provider["backend"]),
        model=str(provider["model"]),
        attempt_count=attempt_count,
        cache_hit=True,
        raw_response=raw_response,
    )


def call_task_llm(
    output_root: str | Path,
    task_id: str,
    *,
    call_id: str,
    messages: list[dict[str, str]],
    response_format: dict[str, Any] | None = None,
    config: LLMConfig | None = None,
    transport: LLMTransport | None = None,
    dotenv_path: str | Path | None = None,
    response_validator: ResponseValidator | None = None,
    validation_id: str | None = None,
    artifact_subdir: str | Path = "llm",
    cache_namespace: str | None = None,
    save_validation_audit: bool = False,
    max_attempts: int = 2,
) -> LLMCallResult:
    """调用结构化LLM；缓存有效时不访问transport，成功时原子写入。"""

    validate_task_id(task_id)
    validate_call_id(call_id)
    normalized_messages = _validated_messages(messages)
    normalized_format = _validated_response_format(response_format)
    if (response_validator is None) != (validation_id is None):
        raise LLMAdapterError(
            "response_validator和validation_id必须同时提供。"
        )
    if validation_id is not None:
        validate_call_id(validation_id)
    if cache_namespace is not None:
        validate_call_id(cache_namespace)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 2:
        raise LLMAdapterError("max_attempts必须是1或2。")
    normalized_artifact_subdir = _validated_artifact_subdir(artifact_subdir)
    resolved_config = config or load_llm_config(dotenv_path=dotenv_path)
    task_root = Path(output_root).expanduser() / task_id
    try:
        load_task_manifest(task_root / "task.json", expected_task_id=task_id)
    except TaskWorkspaceError as exc:
        raise LLMAdapterError(str(exc)) from exc

    provider = resolved_config.public_dict()
    request_payload = {
        "messages": normalized_messages,
        "response_format": normalized_format,
    }
    if validation_id is not None:
        request_payload["validation_id"] = validation_id
        request_payload["retry_policy_version"] = VALIDATION_RETRY_POLICY_VERSION
    if cache_namespace is not None:
        request_payload["cache_namespace"] = cache_namespace
    if max_attempts != 2:
        request_payload["max_attempts"] = max_attempts
    cache_key = _build_cache_key(
        resolved_config,
        normalized_messages,
        normalized_format,
        validation_id,
        cache_namespace,
        max_attempts,
    )
    output_path = task_root / normalized_artifact_subdir / f"{call_id}.json"
    validation_audit_path = output_path.with_name(
        f"{call_id}-validation-audit.json"
    )
    temporary_path = output_path.with_name(f"{output_path.name}.tmp")
    started_at = time.perf_counter()

    cached_result = _load_cached_result(
        output_path,
        task_id=task_id,
        call_id=call_id,
        cache_key=cache_key,
        provider=provider,
        request_payload=request_payload,
        response_validator=response_validator,
    )
    if cached_result is not None:
        _append_llm_log(
            task_root,
            event="cache_hit",
            task_id=task_id,
            call_id=call_id,
            cache_key=cache_key,
            provider=provider,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            attempt_count=0,
        )
        return cached_result

    selected_transport = transport or _default_transport(resolved_config)
    attempt_count = 0
    _append_llm_log(
        task_root,
        event="started",
        task_id=task_id,
        call_id=call_id,
        cache_key=cache_key,
        provider=provider,
        elapsed_seconds=0.0,
        output_path=output_path,
        attempt_count=1,
    )
    try:
        successful_raw_response: dict[str, Any] | None = None
        validation_attempts: list[dict[str, Any]] = []
        attempt_messages = list(normalized_messages)
        while attempt_count < max_attempts:
            attempt_count += 1
            raw_response: Any = None
            structured_response: dict[str, Any] | None = None
            try:
                raw_response = selected_transport.complete(
                    resolved_config, attempt_messages, normalized_format
                )
                structured_response = _structured_response(raw_response)
                response = _domain_validated_response(
                    structured_response,
                    response_validator,
                )
                if response_validator is not None:
                    successful_raw_response = structured_response
                break
            except LLMRetryableError as exc:
                if save_validation_audit and validation_id is not None:
                    validation_attempts.append(
                        {
                            "attempt": attempt_count,
                            "raw_response": _redact_audit_value(
                                structured_response
                                if structured_response is not None
                                else raw_response,
                                resolved_config.api_key,
                            ),
                            "error": _safe_error(exc, resolved_config.api_key),
                        }
                    )
                    _write_validation_audit(
                        validation_audit_path,
                        task_id=task_id,
                        call_id=call_id,
                        cache_key=cache_key,
                        provider=provider,
                        validation_id=validation_id,
                        status=(
                            "failed" if attempt_count >= max_attempts else "retrying"
                        ),
                        attempts=validation_attempts,
                    )
                if attempt_count >= max_attempts:
                    raise
                correction = (
                    "上次响应未通过业务校验："
                    f"{_safe_error(exc, resolved_config.api_key)}"
                    "请只修正该问题，重新返回符合原格式的完整JSON对象。"
                )
                attempt_messages = list(normalized_messages)
                if structured_response is not None:
                    attempt_messages.append(
                        {
                            "role": "assistant",
                            "content": json.dumps(
                                structured_response,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    )
                attempt_messages.append({"role": "user", "content": correction})
                _append_llm_log(
                    task_root,
                    event="retrying",
                    task_id=task_id,
                    call_id=call_id,
                    cache_key=cache_key,
                    provider=provider,
                    elapsed_seconds=time.perf_counter() - started_at,
                    output_path=output_path,
                    attempt_count=attempt_count + 1,
                    error=_safe_error(exc, resolved_config.api_key),
                )

        if save_validation_audit and validation_attempts and validation_id is not None:
            _write_validation_audit(
                validation_audit_path,
                task_id=task_id,
                call_id=call_id,
                cache_key=cache_key,
                provider=provider,
                validation_id=validation_id,
                status="completed_after_retry",
                attempts=validation_attempts,
            )

        payload = {
            "schema_version": LLM_SCHEMA_VERSION,
            "task_id": task_id,
            "call_id": call_id,
            "cache_key": cache_key,
            "provider": provider,
            "request": request_payload,
            "response": response,
            "attempt_count": attempt_count,
        }
        if successful_raw_response is not None:
            payload["raw_response"] = successful_raw_response
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _remove_temporary_file(temporary_path)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(output_path)
        except OSError as exc:
            raise LLMAdapterError(f"无法保存LLM结果：{output_path}") from exc
        finally:
            _remove_temporary_file(temporary_path)

        result = LLMCallResult(
            output_path=output_path,
            response=response,
            backend=resolved_config.backend,
            model=resolved_config.model,
            attempt_count=attempt_count,
            cache_hit=False,
            raw_response=successful_raw_response,
        )
        _append_llm_log(
            task_root,
            event="completed",
            task_id=task_id,
            call_id=call_id,
            cache_key=cache_key,
            provider=provider,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            attempt_count=attempt_count,
        )
        return result
    except KeyboardInterrupt as exc:
        error = LLMAdapterError("LLM调用被中断。")
        _remove_temporary_file(temporary_path)
        _append_llm_log(
            task_root,
            event="failed",
            task_id=task_id,
            call_id=call_id,
            cache_key=cache_key,
            provider=provider,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            attempt_count=attempt_count,
            error=str(error),
        )
        raise error from exc
    except LLMAdapterError as exc:
        _remove_temporary_file(temporary_path)
        safe_message = _safe_error(exc, resolved_config.api_key)
        _append_llm_log(
            task_root,
            event="failed",
            task_id=task_id,
            call_id=call_id,
            cache_key=cache_key,
            provider=provider,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            attempt_count=attempt_count,
            error=safe_message,
        )
        if safe_message != str(exc):
            raise type(exc)(safe_message) from exc
        raise
    except Exception as exc:
        _remove_temporary_file(temporary_path)
        wrapped = LLMAdapterError(f"LLM调用失败：{_safe_error(exc, resolved_config.api_key)}")
        _append_llm_log(
            task_root,
            event="failed",
            task_id=task_id,
            call_id=call_id,
            cache_key=cache_key,
            provider=provider,
            elapsed_seconds=time.perf_counter() - started_at,
            output_path=output_path,
            attempt_count=attempt_count,
            error=str(wrapped),
        )
        raise wrapped from exc
