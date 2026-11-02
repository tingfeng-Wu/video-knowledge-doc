"""ModelRoleRegistry 1.0最小loader与角色缓存身份计算（阶段14-B裁决F.1）。

按M2-0-P3 §二/§三.1与14-B裁决F.2实现三级解析：

1. 显式``--model-registry``文件；
2. 项目默认registry文件``config/model-roles.json``；
3. 内置安全配置：``boundary_model`` enabled=false（绝不自动真实联网）。

本模块离线、只读、不写任何产物：

- 不读取``VIDEO_DOC_LLM_*``、不调用M1 ``load_llm_config()``（P3 §3.1）。API Key
  只经角色连接档案声明的``api_key_env``环境变量名解析；本模块只返回密钥值与
  credential presence，绝不写入任何文件；
- 不发起网络请求；prompt模板与实际prompt内容哈希属于``boundary_planner_v2``，
  由调用方传入后参与``role_cache_identity``计算；
- ``Schema校验器按P3 §2.2完整实现``（14-B裁决F.1）：字段级校验适用于全部LLM角色，
  boundary_model与content_model的启用政策分别由14-B与14-C裁决。

实现决策（提交G1核对）：

1. 角色顶层、连接档案、registry顶层使用精确键集合校验；``generation``块要求
   P3 §2.2四键、允许fixture已有的可选``per_call_max_tokens``（content_model形态），
   其余未知键一律拒绝；记录字段``role_cache_identity_sha256``为可选键，不参与计算；
2. ``engine_roles``仅做浅校验（对象＋非空字符串``role_config_version``/``state``）：
   ASR/OCR引擎字段已在各自阶段文档冻结，本模块不越权重新解释；
3. ``role_cache_identity``规范对象按P3 §六列举项构造；额外纳入``model_revision``与
   ``runtime_budget``两项§2.2身份字段——§六明文“角色配置或输入任一身份项变化必须
   缓存失效”，revision与budget都是§2.2身份项；``registry_id``属来源信息而非身份项，
   不进入规范对象；
4. temperature/timeout_seconds统一量化为float、max_tokens为int或null，使同一配置
   写作``0``与``0.0``时身份哈希稳定；
5. P3 §2.1“API Key值不得进入registry”落地为递归键名扫描：任何对象出现键名
   （小写精确匹配）``api_key``/``apikey``/``secret``/``token``/``authorization``/
   ``access_key``/``password``即拒绝整个registry；``api_key_env``为声明字段，合法。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from video_doc.llm_adapter import LLMConfig
from video_doc.unified_timeline import canonical_json_bytes

REGISTRY_SCHEMA_NAME = "ModelRoleRegistry"
REGISTRY_SCHEMA_VERSION = "1.0"
ROLE_CACHE_IDENTITY_SCHEMA_NAME = "ModelRoleCacheIdentity"
ROLE_CACHE_IDENTITY_SCHEMA_VERSION = "1.0"

BOUNDARY_ROLE_NAME = "boundary_model"
DEFAULT_REGISTRY_RELATIVE_PATH = Path("config") / "model-roles.json"

RESOLUTION_EXPLICIT = "explicit"
RESOLUTION_PROJECT_DEFAULT = "project-default"
RESOLUTION_BUILTIN_DISABLED = "builtin-disabled"

_ALLOWED_LLM_BACKENDS = frozenset({"openai_compatible"})

_REGISTRY_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "registry_id",
        "fixture_only",
        "connection_profiles",
        "engine_roles",
        "llm_roles",
    }
)
_REGISTRY_OPTIONAL_KEYS = frozenset({"identity_algorithm"})
_PROFILE_KEYS = frozenset({"backend", "base_url", "api_key_env"})
_PROFILE_OPTIONAL_KEYS = frozenset({"organization_id", "project_id"})
_ROLE_KEYS = frozenset(
    {
        "role_config_version",
        "enabled",
        "connection_profile",
        "resolved_provider",
        "model",
        "model_revision",
        "revision_policy",
        "generation",
        "input_contract",
        "output_contract",
        "prompt_bundle",
        "response_format",
        "validation_id",
        "validation_retry_policy",
        "cache_namespace",
        "max_attempts_per_business_call",
        "alternate_model_fallback",
        "runtime_budget",
    }
)
_ROLE_OPTIONAL_KEYS = frozenset({"role_cache_identity_sha256"})
_RESOLVED_PROVIDER_KEYS = frozenset({"backend", "base_url"})
_GENERATION_KEYS = frozenset(
    {"temperature", "enable_thinking", "max_tokens", "timeout_seconds"}
)
_GENERATION_OPTIONAL_KEYS = frozenset({"per_call_max_tokens"})
_PROMPT_BUNDLE_KEYS = frozenset({"versions", "content_sha256", "fixture_hash_only"})
_RUNTIME_BUDGET_KEYS = frozenset(
    {"business_call_hard_limit", "supplier_request_hard_limit"}
)
_FORBIDDEN_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "token",
        "authorization",
        "access_key",
        "password",
    }
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# validation_id允许点号：P3冻结fixture的content_model值
# "continuous-content-validator-bundle-1.0"即含点号；cache_namespace是路径类
# 组件，保持不含点号的严格形态，两者不得混用一个模式。
_VALIDATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ModelRoleRegistryError(RuntimeError):
    """registry Schema、身份字段或安全约束校验失败。"""


@dataclass(frozen=True)
class ConnectionProfile:
    name: str
    backend: str
    base_url: str
    api_key_env: str


@dataclass(frozen=True)
class RoleGeneration:
    temperature: float
    enable_thinking: bool
    max_tokens: int | None
    timeout_seconds: float


@dataclass(frozen=True)
class PromptBundle:
    versions: tuple[str, ...]
    content_sha256: str
    fixture_hash_only: bool


@dataclass(frozen=True)
class LlmRoleConfig:
    """P3 §2.2全部身份字段的解析结果；禁用角色允许None形态。"""

    name: str
    role_config_version: str
    enabled: bool
    connection_profile: ConnectionProfile | None
    resolved_provider: dict[str, str] | None
    model: str | None
    model_revision: str | None
    revision_policy: str | None
    generation: RoleGeneration | None
    input_contract: str | None
    output_contract: str | None
    prompt_bundle: PromptBundle | None
    response_format: dict[str, Any] | None
    validation_id: str | None
    validation_retry_policy: str | None
    cache_namespace: str | None
    max_attempts_per_business_call: int
    alternate_model_fallback: str
    runtime_budget: dict[str, int]


@dataclass(frozen=True)
class RegistrySnapshot:
    registry_id: str
    fixture_only: bool
    source: str
    connection_profiles: dict[str, ConnectionProfile]
    engine_roles: dict[str, dict[str, Any]]
    llm_roles: dict[str, LlmRoleConfig]

    def lookup(self, role_name: str) -> LlmRoleConfig | None:
        return self.llm_roles.get(role_name)


def _fail(message: str) -> None:
    raise ModelRoleRegistryError(message)


def _exact_keys(
    value: Any,
    expected: frozenset[str],
    optional: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label}必须是JSON对象。")
    keys = set(value)
    missing = sorted(expected - keys)
    extra = sorted(keys - expected - optional)
    if missing or extra:
        _fail(
            f"{label}字段集合无效：缺少{missing or '无'}，多余{extra or '无'}。"
        )
    return dict(value)


def _nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label}必须是非空字符串。")
    return value.strip()


def _optional_str(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty_str(value, label)


def _identifier(value: Any, label: str) -> str:
    text = _nonempty_str(value, label)
    if not _IDENTIFIER_PATTERN.fullmatch(text):
        _fail(f"{label}只能包含字母、数字、下划线和连字符且以字母或数字开头。")
    return text


def _validation_id(value: Any, label: str) -> str:
    text = _nonempty_str(value, label)
    if not _VALIDATION_ID_PATTERN.fullmatch(text):
        _fail(f"{label}只能包含字母、数字、下划线、连字符和点号且以字母或数字开头。")
    return text


def _bool_field(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label}必须是布尔值。")
    return value


def _int_field(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label}必须是整数。")
    return value


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        _fail(f"{label}必须是有限数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelRoleRegistryError(f"{label}必须是有限数字。") from exc
    if not math.isfinite(number):
        _fail(f"{label}必须是有限数字。")
    return number


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        _fail(f"{label}必须是64位小写十六进制SHA-256。")
    return value


def _reject_secret_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEY_NAMES:
                _fail(f"registry禁止携带密钥材料：字段{path}.{key}。")
            _reject_secret_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_keys(item, f"{path}[{index}]")


def _load_connection_profiles(raw: Any) -> dict[str, ConnectionProfile]:
    if not isinstance(raw, dict):
        _fail("connection_profiles必须是JSON对象。")
    profiles: dict[str, ConnectionProfile] = {}
    for name, block in raw.items():
        _nonempty_str(name, "connection_profiles名称")
        value = _exact_keys(block, _PROFILE_KEYS, _PROFILE_OPTIONAL_KEYS, f"连接档案{name}")
        for field in _PROFILE_OPTIONAL_KEYS:
            _optional_str(value.get(field), f"连接档案{name}的{field}")
        backend = _nonempty_str(value["backend"], f"连接档案{name}的backend")
        if backend not in _ALLOWED_LLM_BACKENDS:
            _fail(f"连接档案{name}的backend必须是openai_compatible。")
        base_url = _nonempty_str(value["base_url"], f"连接档案{name}的base_url")
        if not base_url.startswith(("https://", "http://")):
            _fail(f"连接档案{name}的base_url必须以http(s)://开头。")
        api_key_env = _nonempty_str(value["api_key_env"], f"连接档案{name}的api_key_env")
        if not _ENV_NAME_PATTERN.fullmatch(api_key_env):
            _fail(f"连接档案{name}的api_key_env不是合法环境变量名。")
        profiles[str(name)] = ConnectionProfile(
            name=str(name),
            backend=backend,
            base_url=base_url,
            api_key_env=api_key_env,
        )
    return profiles


def _load_generation(raw: Any, label: str) -> RoleGeneration:
    value = _exact_keys(raw, _GENERATION_KEYS, _GENERATION_OPTIONAL_KEYS, f"{label}的generation")
    temperature = _finite_float(value["temperature"], f"{label}的temperature")
    if not 0 <= temperature <= 2:
        _fail(f"{label}的temperature必须在0到2之间。")
    max_tokens = value["max_tokens"]
    if max_tokens is not None:
        max_tokens = _int_field(max_tokens, f"{label}的max_tokens")
        if max_tokens < 1:
            _fail(f"{label}的max_tokens必须为正整数或null。")
    timeout = _finite_float(value["timeout_seconds"], f"{label}的timeout_seconds")
    if timeout <= 0:
        _fail(f"{label}的timeout_seconds必须大于0。")
    per_call = value.get("per_call_max_tokens")
    if per_call is not None:
        if not isinstance(per_call, dict) or not per_call:
            _fail(f"{label}的per_call_max_tokens必须是非空JSON对象。")
        for key, item in per_call.items():
            _nonempty_str(key, f"{label}的per_call_max_tokens键")
            if _int_field(item, f"{label}的per_call_max_tokens.{key}") < 1:
                _fail(f"{label}的per_call_max_tokens.{key}必须为正整数。")
    return RoleGeneration(
        temperature=temperature,
        enable_thinking=_bool_field(value["enable_thinking"], f"{label}的enable_thinking"),
        max_tokens=max_tokens,
        timeout_seconds=timeout,
    )


def _load_prompt_bundle(raw: Any, label: str) -> PromptBundle:
    value = _exact_keys(raw, _PROMPT_BUNDLE_KEYS, frozenset(), f"{label}的prompt_bundle")
    versions = value["versions"]
    if not isinstance(versions, list) or not versions:
        _fail(f"{label}的prompt_bundle.versions必须是非空数组。")
    clean_versions: list[str] = []
    for index, item in enumerate(versions, start=1):
        text = _nonempty_str(item, f"{label}的prompt_bundle.versions[{index}]")
        if text in clean_versions:
            _fail(f"{label}的prompt_bundle.versions不得包含重复版本。")
        clean_versions.append(text)
    return PromptBundle(
        versions=tuple(clean_versions),
        content_sha256=_sha256(
            value["content_sha256"], f"{label}的prompt_bundle.content_sha256"
        ),
        fixture_hash_only=_bool_field(
            value["fixture_hash_only"], f"{label}的prompt_bundle.fixture_hash_only"
        ),
    )


def _load_runtime_budget(raw: Any, label: str) -> dict[str, int]:
    value = _exact_keys(raw, _RUNTIME_BUDGET_KEYS, frozenset(), f"{label}的runtime_budget")
    business = _int_field(
        value["business_call_hard_limit"], f"{label}的business_call_hard_limit"
    )
    supplier = _int_field(
        value["supplier_request_hard_limit"], f"{label}的supplier_request_hard_limit"
    )
    if business < 0 or supplier < 0:
        _fail(f"{label}的runtime_budget不得为负。")
    return {
        "business_call_hard_limit": business,
        "supplier_request_hard_limit": supplier,
    }


def _load_llm_role(name: Any, raw: Any, profiles: Mapping[str, ConnectionProfile]) -> LlmRoleConfig:
    role_name = _nonempty_str(name, "llm_roles名称")
    value = _exact_keys(raw, _ROLE_KEYS, _ROLE_OPTIONAL_KEYS, f"角色{role_name}")
    role_config_version = _nonempty_str(
        value["role_config_version"], f"角色{role_name}的role_config_version"
    )
    if value.get("role_cache_identity_sha256") is not None:
        _sha256(
            value["role_cache_identity_sha256"],
            f"角色{role_name}的role_cache_identity_sha256",
        )
    enabled = _bool_field(value["enabled"], f"角色{role_name}的enabled")
    budget = _load_runtime_budget(value["runtime_budget"], f"角色{role_name}")
    max_attempts = _int_field(
        value["max_attempts_per_business_call"], f"角色{role_name}的max_attempts_per_business_call"
    )
    fallback = _nonempty_str(value["alternate_model_fallback"], f"角色{role_name}的alternate_model_fallback")
    cache_namespace = _identifier(value["cache_namespace"], f"角色{role_name}的cache_namespace")

    if not enabled:
        # 禁用角色沿用fixture中vision角色的None形态（P3 §2.2不适用于禁用角色）。
        for field in (
            "connection_profile",
            "resolved_provider",
            "model",
            "model_revision",
            "generation",
            "input_contract",
            "output_contract",
            "prompt_bundle",
            "response_format",
            "validation_id",
        ):
            if value[field] is not None:
                _fail(f"禁用角色{role_name}的{field}必须为null。")
        revision_policy = _optional_str(value["revision_policy"], f"角色{role_name}的revision_policy")
        retry_policy = _optional_str(
            value["validation_retry_policy"], f"角色{role_name}的validation_retry_policy"
        )
        if max_attempts < 0:
            _fail(f"禁用角色{role_name}的max_attempts_per_business_call不得为负。")
        return LlmRoleConfig(
            name=role_name,
            role_config_version=role_config_version,
            enabled=False,
            connection_profile=None,
            resolved_provider=None,
            model=None,
            model_revision=None,
            revision_policy=revision_policy,
            generation=None,
            input_contract=None,
            output_contract=None,
            prompt_bundle=None,
            response_format=None,
            validation_id=None,
            validation_retry_policy=retry_policy,
            cache_namespace=cache_namespace,
            max_attempts_per_business_call=max_attempts,
            alternate_model_fallback=fallback,
            runtime_budget=budget,
        )

    # 启用角色缺任一P3 §2.2身份字段必须失败，不退回全局默认。
    if budget["business_call_hard_limit"] < 1 or budget["supplier_request_hard_limit"] < 1:
        _fail(f"启用角色{role_name}的runtime_budget硬上限必须至少为1。")
    if budget["supplier_request_hard_limit"] < budget["business_call_hard_limit"]:
        _fail(f"启用角色{role_name}的供应商请求硬上限不得小于业务调用硬上限。")
    if max_attempts < 1:
        _fail(f"启用角色{role_name}的max_attempts_per_business_call必须至少为1。")
    profile_name = _nonempty_str(value["connection_profile"], f"角色{role_name}的connection_profile")
    profile = profiles.get(profile_name)
    if profile is None:
        _fail(f"角色{role_name}引用了不存在的连接档案{profile_name}。")
    provider = _exact_keys(
        value["resolved_provider"], _RESOLVED_PROVIDER_KEYS, frozenset(), f"角色{role_name}的resolved_provider"
    )
    if (
        provider["backend"] != profile.backend
        or provider["base_url"] != profile.base_url
    ):
        _fail(f"角色{role_name}的resolved_provider与连接档案不一致。")
    if provider["backend"] not in _ALLOWED_LLM_BACKENDS:
        _fail(f"角色{role_name}的backend必须是openai_compatible。")
    model = _nonempty_str(value["model"], f"角色{role_name}的model")
    model_revision = _optional_str(value["model_revision"], f"角色{role_name}的model_revision")
    revision_policy = _nonempty_str(value["revision_policy"], f"角色{role_name}的revision_policy")
    generation = _load_generation(value["generation"], f"角色{role_name}")
    input_contract = _nonempty_str(value["input_contract"], f"角色{role_name}的input_contract")
    output_contract = _nonempty_str(value["output_contract"], f"角色{role_name}的output_contract")
    prompt_bundle = _load_prompt_bundle(value["prompt_bundle"], f"角色{role_name}")
    response_format = value["response_format"]
    if not isinstance(response_format, dict) or not response_format:
        _fail(f"角色{role_name}的response_format必须是非空JSON对象。")
    _nonempty_str(response_format.get("type"), f"角色{role_name}的response_format.type")
    validation_id = _validation_id(value["validation_id"], f"角色{role_name}的validation_id")
    retry_policy = _nonempty_str(
        value["validation_retry_policy"], f"角色{role_name}的validation_retry_policy"
    )
    return LlmRoleConfig(
        name=role_name,
        role_config_version=role_config_version,
        enabled=True,
        connection_profile=profile,
        resolved_provider=dict(provider),
        model=model,
        model_revision=model_revision,
        revision_policy=revision_policy,
        generation=generation,
        input_contract=input_contract,
        output_contract=output_contract,
        prompt_bundle=prompt_bundle,
        response_format=dict(response_format),
        validation_id=validation_id,
        validation_retry_policy=retry_policy,
        cache_namespace=cache_namespace,
        max_attempts_per_business_call=max_attempts,
        alternate_model_fallback=fallback,
        runtime_budget=budget,
    )


def _load_engine_roles(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        _fail("engine_roles必须是JSON对象。")
    result: dict[str, dict[str, Any]] = {}
    for name, block in raw.items():
        role_name = _nonempty_str(name, "engine_roles名称")
        if not isinstance(block, dict):
            _fail(f"引擎角色{role_name}必须是JSON对象。")
        _nonempty_str(block.get("role_config_version"), f"引擎角色{role_name}的role_config_version")
        _nonempty_str(block.get("state"), f"引擎角色{role_name}的state")
        result[role_name] = dict(block)
    return result


def load_model_role_registry(path: str | Path) -> RegistrySnapshot:
    """读取并完整校验一个ModelRoleRegistry 1.0文件（P3 §二/§2.2）。"""

    registry_path = Path(path).expanduser()
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ModelRoleRegistryError("registry文件无法读取或JSON无效。") from exc
    if not isinstance(payload, dict):
        _fail("registry顶层必须是JSON对象。")
    _reject_secret_keys(payload)
    value = _exact_keys(payload, _REGISTRY_KEYS, _REGISTRY_OPTIONAL_KEYS, "registry")
    if value["schema_name"] != REGISTRY_SCHEMA_NAME:
        _fail("registry的schema_name必须是ModelRoleRegistry。")
    if value["schema_version"] != REGISTRY_SCHEMA_VERSION:
        _fail("registry的schema_version必须是1.0。")
    registry_id = _identifier(value["registry_id"], "registry_id")
    if value.get("identity_algorithm") is not None:
        _nonempty_str(value["identity_algorithm"], "identity_algorithm")
    fixture_only = _bool_field(value["fixture_only"], "fixture_only")
    if not isinstance(value["connection_profiles"], dict):
        _fail("connection_profiles必须是JSON对象。")
    if not isinstance(value["llm_roles"], dict) or not value["llm_roles"]:
        _fail("llm_roles必须是非空JSON对象。")
    profiles = _load_connection_profiles(value["connection_profiles"])
    engine_roles = _load_engine_roles(value["engine_roles"])
    parsed_roles = [
        _load_llm_role(name, raw, profiles)
        for name, raw in value["llm_roles"].items()
    ]
    llm_roles = {role.name: role for role in parsed_roles}
    return RegistrySnapshot(
        registry_id=registry_id,
        fixture_only=fixture_only,
        source="",
        connection_profiles=profiles,
        engine_roles=engine_roles,
        llm_roles=llm_roles,
    )


def builtin_disabled_registry() -> RegistrySnapshot:
    """内置安全配置（P3 §三.1第3级）：boundary_model enabled=false。"""

    role = LlmRoleConfig(
        name=BOUNDARY_ROLE_NAME,
        role_config_version="boundary-model-role-1.0",
        enabled=False,
        connection_profile=None,
        resolved_provider=None,
        model=None,
        model_revision=None,
        revision_policy="disabled",
        generation=None,
        input_contract=None,
        output_contract=None,
        prompt_bundle=None,
        response_format=None,
        validation_id=None,
        validation_retry_policy="none",
        cache_namespace="boundary-model-disabled-1-0",
        max_attempts_per_business_call=0,
        alternate_model_fallback="none",
        runtime_budget={
            "business_call_hard_limit": 0,
            "supplier_request_hard_limit": 0,
        },
    )
    return RegistrySnapshot(
        registry_id="model-role-registry-builtin-disabled",
        fixture_only=False,
        source=RESOLUTION_BUILTIN_DISABLED,
        connection_profiles={},
        engine_roles={},
        llm_roles={BOUNDARY_ROLE_NAME: role},
    )


def resolve_model_registry(
    project_root: str | Path,
    explicit_path: str | Path | None = None,
) -> RegistrySnapshot:
    """按P3 §三.1三级优先级解析registry；返回带source的快照。

    显式文件不存在或校验失败一律抛``ModelRoleRegistryError``（调用方映射为
    ROLE_CONFIG_INVALID）；只有显式路径与项目默认文件都不存在时才落到内置
    disabled配置（调用方映射为ROLE_CONFIG_MISSING）。
    """

    if explicit_path is not None:
        snapshot = load_model_role_registry(explicit_path)
        return RegistrySnapshot(
            registry_id=snapshot.registry_id,
            fixture_only=snapshot.fixture_only,
            source=RESOLUTION_EXPLICIT,
            connection_profiles=snapshot.connection_profiles,
            engine_roles=snapshot.engine_roles,
            llm_roles=snapshot.llm_roles,
        )
    default_path = Path(project_root).expanduser() / DEFAULT_REGISTRY_RELATIVE_PATH
    if default_path.is_file():
        snapshot = load_model_role_registry(default_path)
        return RegistrySnapshot(
            registry_id=snapshot.registry_id,
            fixture_only=snapshot.fixture_only,
            source=RESOLUTION_PROJECT_DEFAULT,
            connection_profiles=snapshot.connection_profiles,
            engine_roles=snapshot.engine_roles,
            llm_roles=snapshot.llm_roles,
        )
    return builtin_disabled_registry()


def resolve_api_key(role: LlmRoleConfig, environ: Mapping[str, str]) -> tuple[str, bool]:
    """只经角色档案声明的``api_key_env``读取密钥（P3 §2.1/§3.1）。"""

    if role.connection_profile is None:
        _fail("启用角色缺少connection_profile，无法解析API Key。")
    name = role.connection_profile.api_key_env
    key = str(environ.get(name, "")).strip()
    return key, bool(key)


def build_llm_config(role: LlmRoleConfig, api_key: str) -> LLMConfig:
    """把已校验角色组装为传输层配置；密钥不进入repr。"""

    if role.generation is None or role.connection_profile is None or role.model is None:
        _fail("启用角色身份字段不完整，无法构建LLM配置。")
    return LLMConfig(
        backend=role.connection_profile.backend,
        base_url=role.connection_profile.base_url,
        model=str(role.model),
        timeout_seconds=role.generation.timeout_seconds,
        api_key=api_key,
        mock_response={},
        enable_thinking=role.generation.enable_thinking,
        temperature=role.generation.temperature,
        max_tokens=role.generation.max_tokens,
    )


def role_cache_identity_payload(
    role: LlmRoleConfig,
    *,
    input_canonical_sha256: str,
    prompt_content_sha256: str,
) -> dict[str, Any]:
    """P3 §六规范对象（含实现决策3纳入的身份项），未哈希。"""

    if not role.enabled:
        _fail("禁用角色不得计算role_cache_identity。")
    _sha256(input_canonical_sha256, "input canonical SHA-256")
    _sha256(prompt_content_sha256, "实际prompt内容SHA-256")
    assert role.generation is not None and role.prompt_bundle is not None
    assert role.resolved_provider is not None
    return {
        "schema_name": ROLE_CACHE_IDENTITY_SCHEMA_NAME,
        "schema_version": ROLE_CACHE_IDENTITY_SCHEMA_VERSION,
        "registry_schema_name": REGISTRY_SCHEMA_NAME,
        "registry_schema_version": REGISTRY_SCHEMA_VERSION,
        "role": {
            "name": role.name,
            "role_config_version": role.role_config_version,
            "enabled": role.enabled,
        },
        "provider": {
            "backend": role.resolved_provider["backend"],
            "base_url": role.resolved_provider["base_url"],
            "model": role.model,
            "model_revision": role.model_revision,
            "revision_policy": role.revision_policy,
        },
        "generation": {
            "temperature": role.generation.temperature,
            "enable_thinking": role.generation.enable_thinking,
            "max_tokens": role.generation.max_tokens,
            "timeout_seconds": role.generation.timeout_seconds,
        },
        "runtime_budget": dict(role.runtime_budget),
        "input_contract": role.input_contract,
        "input_canonical_sha256": input_canonical_sha256,
        "output_contract": role.output_contract,
        "prompt_bundle": {
            "versions": list(role.prompt_bundle.versions),
            "content_sha256": prompt_content_sha256,
        },
        "response_format": dict(role.response_format or {}),
        "validation": {
            "validation_id": role.validation_id,
            "validation_retry_policy": role.validation_retry_policy,
            "max_attempts_per_business_call": role.max_attempts_per_business_call,
        },
        "cache_namespace": role.cache_namespace,
        "alternate_model_fallback": role.alternate_model_fallback,
    }


def compute_role_cache_identity(
    role: LlmRoleConfig,
    *,
    input_canonical_sha256: str,
    prompt_content_sha256: str,
) -> tuple[dict[str, Any], str]:
    """返回(规范对象, SHA-256)；对象不含API Key/时间/费用/路径（P3 §六）。"""

    payload = role_cache_identity_payload(
        role,
        input_canonical_sha256=input_canonical_sha256,
        prompt_content_sha256=prompt_content_sha256,
    )
    return payload, hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
