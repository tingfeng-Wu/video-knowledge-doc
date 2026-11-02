"""阶段14-B：ModelRoleRegistry 1.0加载、三级解析与role_cache_identity定向测试。

覆盖裁决F.2/F.4与P3 §二/§三.1/§六：启用身份字段强校验、禁用None形态、密钥
材料递归拒绝、backend/provider一致性、三级解析顺序、内置禁用配置零联网语义、
API Key只经声明env解析、role_cache_identity纳入model_revision与runtime_budget
而排除registry_id。全部离线，不触碰网络与真实outputs。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from video_doc.llm_adapter import LLMConfig
from video_doc.model_roles import (
    BOUNDARY_ROLE_NAME,
    ModelRoleRegistryError,
    builtin_disabled_registry,
    build_llm_config,
    compute_role_cache_identity,
    load_model_role_registry,
    resolve_api_key,
    resolve_model_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "docs" / "development" / "m2-p3-model-role-config-fixture.json"
TEMPLATE_PATH = REPO_ROOT / "config" / "model-roles.json"

INPUT_SHA = "a" * 64
PROMPT_SHA = "b" * 64


def _template_payload() -> dict[str, Any]:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _write_registry(tmp_path: Path, payload: dict[str, Any], name: str = "roles.json") -> Path:
    path = tmp_path / name
    with path.open("w", encoding="utf-8", newline="") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return path


def _load_dict(tmp_path: Path, payload: dict[str, Any]) -> Any:
    return load_model_role_registry(_write_registry(tmp_path, payload))


def _boundary_role(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["llm_roles"][BOUNDARY_ROLE_NAME]


def _role_variant(tmp_path: Path, **mutate: Any):
    """返回基于仓库模板、修改boundary_model顶层字段的加载后角色。"""

    payload = _template_payload()
    _boundary_role(payload).update(mutate)
    snapshot = _load_dict(tmp_path, payload)
    return snapshot.lookup(BOUNDARY_ROLE_NAME)


def _identity_sha(role: Any, *, input_sha: str = INPUT_SHA, prompt_sha: str = PROMPT_SHA) -> str:
    return compute_role_cache_identity(
        role, input_canonical_sha256=input_sha, prompt_content_sha256=prompt_sha
    )[1]


# ---------------------------------------------------------------------------
# 仓库内文件本体
# ---------------------------------------------------------------------------


def test_repo_template_and_p3_fixture_load() -> None:
    snapshot = load_model_role_registry(TEMPLATE_PATH)
    assert snapshot.fixture_only is False
    role = snapshot.lookup(BOUNDARY_ROLE_NAME)
    assert role is not None and role.enabled and role.model == "qwen3.7-flash"
    assert role.runtime_budget == {"business_call_hard_limit": 1, "supplier_request_hard_limit": 2}
    assert role.cache_namespace == "boundary-model-unified-timeline-2-0"
    fixture = load_model_role_registry(FIXTURE_PATH)
    assert fixture.fixture_only is True
    assert set(fixture.llm_roles) == {"boundary_model", "content_model", "vision_caption_model"}
    assert fixture.lookup("content_model").generation is not None
    assert fixture.lookup("vision_caption_model").enabled is False


def test_builtin_disabled_registry_never_enables_calls() -> None:
    snapshot = builtin_disabled_registry()
    assert snapshot.source == "builtin-disabled"
    role = snapshot.lookup(BOUNDARY_ROLE_NAME)
    assert role is not None
    assert role.enabled is False
    assert role.connection_profile is None
    assert role.runtime_budget["business_call_hard_limit"] == 0
    assert role.runtime_budget["supplier_request_hard_limit"] == 0
    with pytest.raises(ModelRoleRegistryError, match="禁用角色"):
        compute_role_cache_identity(
            role, input_canonical_sha256=INPUT_SHA, prompt_content_sha256=PROMPT_SHA
        )


# ---------------------------------------------------------------------------
# 强校验：密钥、字段集合、身份字段、provider一致性
# ---------------------------------------------------------------------------


def test_forbidden_secret_key_names_rejected_recursively(tmp_path: Path) -> None:
    payload = _template_payload()
    payload["engine_roles"]["probe"] = {
        "role_config_version": "x",
        "state": "deferred",
        "nested": [{"Api_Key": "nope"}],
    }
    with pytest.raises(ModelRoleRegistryError, match="密钥材料"):
        _load_dict(tmp_path, payload)


def test_registry_extra_or_missing_top_keys_rejected(tmp_path: Path) -> None:
    payload = _template_payload()
    payload["unexpected"] = 1
    with pytest.raises(ModelRoleRegistryError, match="字段集合"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    del payload["registry_id"]
    with pytest.raises(ModelRoleRegistryError, match="registry_id"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    payload["schema_version"] = "1.1"
    with pytest.raises(ModelRoleRegistryError, match="schema_version"):
        _load_dict(tmp_path, payload)


def test_enabled_role_missing_identity_field_fails_without_global_default(tmp_path: Path) -> None:
    payload = _template_payload()
    del _boundary_role(payload)["validation_id"]
    with pytest.raises(ModelRoleRegistryError, match="validation_id"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["model"] = None
    with pytest.raises(ModelRoleRegistryError, match="model"):
        _load_dict(tmp_path, payload)


def test_disabled_role_none_shape_enforced(tmp_path: Path) -> None:
    payload = _template_payload()
    role = _boundary_role(payload)
    role["enabled"] = False
    role["connection_profile"] = None
    role["resolved_provider"] = None
    role["model"] = None
    role["generation"] = None
    role["input_contract"] = None
    role["output_contract"] = None
    role["prompt_bundle"] = None
    role["response_format"] = None
    role["validation_id"] = None
    role["revision_policy"] = "disabled"
    role["validation_retry_policy"] = "none"
    role["max_attempts_per_business_call"] = 0
    role["cache_namespace"] = "boundary-model-disabled-1-0"
    role["runtime_budget"] = {"business_call_hard_limit": 0, "supplier_request_hard_limit": 0}
    snapshot = _load_dict(tmp_path, payload)
    assert snapshot.lookup(BOUNDARY_ROLE_NAME).enabled is False
    payload2 = copy.deepcopy(payload)
    _boundary_role(payload2)["model"] = "sneaky"
    with pytest.raises(ModelRoleRegistryError, match="必须为null"):
        _load_dict(tmp_path, payload2)


def test_resolved_provider_mismatch_and_backend_and_url(tmp_path: Path) -> None:
    payload = _template_payload()
    _boundary_role(payload)["resolved_provider"] = {
        "backend": "openai_compatible",
        "base_url": "https://elsewhere.example/chat",
    }
    with pytest.raises(ModelRoleRegistryError, match="resolved_provider"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    payload["connection_profiles"]["bailian_default"]["backend"] = "anthropic_native"
    with pytest.raises(ModelRoleRegistryError, match="openai_compatible"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    payload["connection_profiles"]["bailian_default"]["base_url"] = "ftp://bad"
    with pytest.raises(ModelRoleRegistryError, match="http"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["connection_profile"] = "ghost_profile"
    with pytest.raises(ModelRoleRegistryError, match="不存在"):
        _load_dict(tmp_path, payload)


def test_runtime_budget_and_attempt_rules(tmp_path: Path) -> None:
    payload = _template_payload()
    _boundary_role(payload)["runtime_budget"] = {
        "business_call_hard_limit": 2,
        "supplier_request_hard_limit": 1,
    }
    with pytest.raises(ModelRoleRegistryError, match="不得小于"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["runtime_budget"] = {
        "business_call_hard_limit": 0,
        "supplier_request_hard_limit": 0,
    }
    with pytest.raises(ModelRoleRegistryError, match="至少为1"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["max_attempts_per_business_call"] = 0
    with pytest.raises(ModelRoleRegistryError, match="至少为1"):
        _load_dict(tmp_path, payload)


def test_prompt_bundle_and_generation_shapes(tmp_path: Path) -> None:
    payload = _template_payload()
    _boundary_role(payload)["prompt_bundle"]["versions"] = []
    with pytest.raises(ModelRoleRegistryError, match="versions"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["prompt_bundle"]["versions"] = ["v1", "v1"]
    with pytest.raises(ModelRoleRegistryError, match="重复"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["prompt_bundle"]["content_sha256"] = "XYZ"
    with pytest.raises(ModelRoleRegistryError, match="SHA-256"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["generation"]["temperature"] = 3.0
    with pytest.raises(ModelRoleRegistryError, match="temperature"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["response_format"] = {}
    with pytest.raises(ModelRoleRegistryError, match="response_format"):
        _load_dict(tmp_path, payload)


def test_optional_record_fields_accept_missing_or_valid(tmp_path: Path) -> None:
    payload = _template_payload()
    assert "role_cache_identity_sha256" not in _boundary_role(payload)
    _load_dict(tmp_path, payload)  # 缺省可选键不得崩溃（.get修复回归）
    payload = _template_payload()
    _boundary_role(payload)["role_cache_identity_sha256"] = "f" * 64
    _load_dict(tmp_path, payload)
    payload = _template_payload()
    del payload["identity_algorithm"]  # 可选顶层键缺失同样不得崩溃
    _load_dict(tmp_path, payload)
    payload = _template_payload()
    payload["connection_profiles"]["bailian_default"]["organization_id"] = "org-1"
    _load_dict(tmp_path, payload)  # profile可选键缺省与给出都必须通过
    payload = _template_payload()
    _boundary_role(payload)["role_cache_identity_sha256"] = "nothex"
    with pytest.raises(ModelRoleRegistryError, match="role_cache_identity_sha256"):
        _load_dict(tmp_path, payload)


def test_validation_id_allows_dots_namespace_does_not(tmp_path: Path) -> None:
    # P3冻结fixture的content_model.validation_id含点号，loader必须能加载它。
    payload = _template_payload()
    _boundary_role(payload)["validation_id"] = "validator.bundle-1.0"
    _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["validation_id"] = ".leading-dot"
    with pytest.raises(ModelRoleRegistryError, match="validation_id"):
        _load_dict(tmp_path, payload)
    payload = _template_payload()
    _boundary_role(payload)["cache_namespace"] = "ns.with-dot"
    with pytest.raises(ModelRoleRegistryError, match="cache_namespace"):
        _load_dict(tmp_path, payload)


# ---------------------------------------------------------------------------
# 三级解析
# ---------------------------------------------------------------------------


def test_three_level_resolution_order(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    snapshot = resolve_model_registry(empty)
    assert snapshot.source == "builtin-disabled"
    assert snapshot.lookup(BOUNDARY_ROLE_NAME).enabled is False

    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    _write_registry(project / "config", _template_payload(), "model-roles.json")
    snapshot = resolve_model_registry(project)
    assert snapshot.source == "project-default"

    other = _template_payload()
    other["registry_id"] = "model-role-registry-explicit-wins"
    explicit = _write_registry(tmp_path, other, "explicit.json")
    snapshot = resolve_model_registry(project, explicit)
    assert snapshot.source == "explicit"
    assert snapshot.registry_id == "model-role-registry-explicit-wins"

    with pytest.raises(ModelRoleRegistryError):
        resolve_model_registry(project, tmp_path / "does-not-exist.json")


def test_resolve_api_key_only_declared_env() -> None:
    snapshot = load_model_role_registry(TEMPLATE_PATH)
    role = snapshot.lookup(BOUNDARY_ROLE_NAME)
    assert role is not None
    key, present = resolve_api_key(
        role, {"VIDEO_DOC_LLM_API_KEY": " declared-key ", "OTHER_KEY": "x"}
    )
    assert (key, present) == ("declared-key", True)
    key, present = resolve_api_key(role, {"OTHER_KEY": "x"})
    assert key == "" and present is False
    with pytest.raises(ModelRoleRegistryError):
        resolve_api_key(builtin_disabled_registry().lookup(BOUNDARY_ROLE_NAME), {})


def test_build_llm_config_maps_frozen_generation() -> None:
    snapshot = load_model_role_registry(TEMPLATE_PATH)
    role = snapshot.lookup(BOUNDARY_ROLE_NAME)
    assert role is not None
    config = build_llm_config(role, "sk-x")
    assert isinstance(config, LLMConfig)
    assert config.model == "qwen3.7-flash"
    assert config.temperature == 0.0
    assert config.enable_thinking is False
    assert config.max_tokens is None
    assert config.timeout_seconds == 60.0
    assert config.mock_response == {}
    assert "sk-x" not in repr(config)
    with pytest.raises(ModelRoleRegistryError):
        build_llm_config(builtin_disabled_registry().lookup(BOUNDARY_ROLE_NAME), "sk-x")


# ---------------------------------------------------------------------------
# role_cache_identity（裁决F.4＋实现决策3/4/5）
# ---------------------------------------------------------------------------


def test_role_cache_identity_deterministic_and_scope(tmp_path: Path) -> None:
    role = _role_variant(tmp_path)
    payload_a, sha_a = compute_role_cache_identity(
        role, input_canonical_sha256=INPUT_SHA, prompt_content_sha256=PROMPT_SHA
    )
    payload_b, sha_b = compute_role_cache_identity(
        role, input_canonical_sha256=INPUT_SHA, prompt_content_sha256=PROMPT_SHA
    )
    assert sha_a == sha_b and payload_a == payload_b and len(sha_a) == 64
    # 纳入项：model_revision、runtime_budget（决策5/实现决策3）；排除registry_id与API Key。
    assert payload_a["schema_name"] == "ModelRoleCacheIdentity"
    assert payload_a["provider"]["model_revision"] is None
    assert payload_a["runtime_budget"] == {
        "business_call_hard_limit": 1,
        "supplier_request_hard_limit": 2,
    }
    dump = json.dumps(payload_a)
    assert "registry_id" not in dump
    assert "sk-" not in dump
    assert payload_a["prompt_bundle"]["content_sha256"] == PROMPT_SHA


def test_role_cache_identity_invalidation_items(tmp_path: Path) -> None:
    base = _role_variant(tmp_path)
    base_sha = _identity_sha(base)

    # model_revision、generation、runtime_budget、输入哈希、prompt内容哈希
    # 任一变化都必须使role_cache_identity失效（P3 §六）。
    assert _identity_sha(_role_variant(tmp_path, model_revision="rev-2026-10")) != base_sha
    mutated = _template_payload()
    _boundary_role(mutated)["generation"]["temperature"] = 0.5
    hot = _load_dict(tmp_path, mutated).lookup(BOUNDARY_ROLE_NAME)
    assert _identity_sha(hot) != base_sha
    budget = _role_variant(
        tmp_path,
        runtime_budget={"business_call_hard_limit": 1, "supplier_request_hard_limit": 3},
    )
    assert _identity_sha(budget) != base_sha
    assert _identity_sha(base, input_sha="c" * 64) != base_sha
    assert _identity_sha(base, prompt_sha="d" * 64) != base_sha


def test_registry_id_difference_does_not_change_identity(tmp_path: Path) -> None:
    payload_a = _template_payload()
    payload_a["registry_id"] = "model-role-registry-alpha"
    payload_b = _template_payload()
    payload_b["registry_id"] = "model-role-registry-beta"
    role_a = _load_dict(tmp_path, payload_a).lookup(BOUNDARY_ROLE_NAME)
    role_b = _load_dict(tmp_path, payload_b).lookup(BOUNDARY_ROLE_NAME)
    _, sha_a = compute_role_cache_identity(role_a, input_canonical_sha256=INPUT_SHA, prompt_content_sha256=PROMPT_SHA)
    _, sha_b = compute_role_cache_identity(role_b, input_canonical_sha256=INPUT_SHA, prompt_content_sha256=PROMPT_SHA)
    assert sha_a == sha_b


def test_role_cache_identity_rejects_malformed_hashes(tmp_path: Path) -> None:
    role = _role_variant(tmp_path)
    with pytest.raises(ModelRoleRegistryError, match="SHA-256"):
        compute_role_cache_identity(role, input_canonical_sha256="zz", prompt_content_sha256=PROMPT_SHA)
