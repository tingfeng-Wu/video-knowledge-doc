import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from tests.test_document_chapter_composition import (
    FailIfCalledTransport,
    _composition_input,
    _workspace,
)
from video_doc.document_chapter_composition import (
    DeterministicDocumentChapterMockTransport,
    compose_document_chapters_offline_mock,
)
from video_doc.document_content_preview import (
    DOCUMENT_COMPOSITION_CONTENT_POLICY_VERSION,
    DOCUMENT_GLOBAL_CACHE_NAMESPACE,
    DOCUMENT_GLOBAL_MOCK_MODEL,
    DOCUMENT_GLOBAL_PROMPT_VERSION,
    DOCUMENT_GLOBAL_RESPONSE_SCHEMA_VERSION,
    DOCUMENT_FORMAL_CURRENT_SCHEMA_VERSION,
    DOCUMENT_FORMAL_PUBLISHER_VERSION,
    DOCUMENT_PREVIEW_MANIFEST_SCHEMA_VERSION,
    DOCUMENT_PREVIEW_PUBLISHER_VERSION,
    DOCUMENT_PREVIEW_REPORT_SCHEMA_VERSION,
    DeterministicDocumentGlobalMockTransport,
    DocumentContentPreviewError,
    assemble_structured_document_content,
    compose_document_preview_offline_mock,
    publish_accepted_document_preview,
    validate_document_preview_group,
    validate_global_composition_response,
)
from video_doc.llm_adapter import LLMConfig
from video_doc.structured_document_content import (
    StructuredDocumentContentError,
    attach_structured_document_content_id,
    validate_structured_document_content,
)


class InvalidGlobalTransport:
    def __init__(self) -> None:
        self.call_count = 0

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        del config, messages, response_format
        self.call_count += 1
        return {"invalid": "全文失败"}


def _file_state(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _chapter_and_global(tmp_path: Path) -> tuple[Path, dict[str, Any], Any, dict[str, Any]]:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    chapter_result = compose_document_chapters_offline_mock(
        output_root, "DEV01", payload
    )
    global_transport = DeterministicDocumentGlobalMockTransport()
    messages = [
        {
            "role": "user",
            "content": "DOCUMENT_GLOBAL_INPUT_JSON="
            + json.dumps(
                {
                    "validated_chapters": [
                        {"chapter": chapter.chapter}
                        for chapter in chapter_result.chapters
                    ]
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
    ]
    response = global_transport.complete(
        LLMConfig(
            backend="mock",
            base_url="",
            model="fixture",
            timeout_seconds=1,
            api_key="",
            mock_response={},
        ),
        messages,
        {},
    )
    return output_root, payload, chapter_result, response


def test_r4_versions_are_frozen() -> None:
    assert DOCUMENT_GLOBAL_RESPONSE_SCHEMA_VERSION == "2.0"
    assert DOCUMENT_GLOBAL_PROMPT_VERSION == "document-global-prompt-2.0"
    assert DOCUMENT_GLOBAL_CACHE_NAMESPACE == (
        "document-global-composition-json-object-2-0"
    )
    assert DOCUMENT_GLOBAL_MOCK_MODEL == "deterministic-document-global-fixture"
    assert DOCUMENT_COMPOSITION_CONTENT_POLICY_VERSION == "document-asr-evidence-only-2.0"
    assert DOCUMENT_PREVIEW_REPORT_SCHEMA_VERSION == "1.0"
    assert DOCUMENT_PREVIEW_MANIFEST_SCHEMA_VERSION == "1.0"
    assert DOCUMENT_PREVIEW_PUBLISHER_VERSION == "document-preview-publisher-2.0"


def test_offline_mock_builds_complete_document_and_atomic_preview(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()

    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)

    assert result.content_id.startswith("document-content-")
    assert result.business_call_count == 2
    assert result.supplier_request_count == 2
    assert result.cache_hit_count == 0
    assert result.preview.cache_hit is False
    assert {path.name for path in result.preview.group_path.iterdir()} == {
        "document-content.json",
        "execution-report.json",
        "manifest.json",
    }
    assert len(result.document["chapters"]) == 1
    assert len(result.document["coverage"]["items"]) == 4
    assert [row["block_id"] for row in result.document["evidence_index"]] == [
        "overview-p001",
        "c0001-s001-p001",
    ]
    assert result.document["evidence_index"][0]["start"] == 0.0
    assert not (output_root / "DEV01" / "content" / "document-v2" / "current.json").exists()
    assert not (output_root / "DEV01" / "content" / "document-v2" / "formal-results").exists()


def test_full_rerun_hits_all_call_and_preview_caches_without_group_rewrite(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    first = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    before = _file_state(first.preview.group_path)

    second = compose_document_preview_offline_mock(
        output_root,
        "DEV01",
        payload,
        chapter_transport=FailIfCalledTransport(),
        global_transport=FailIfCalledTransport(),
    )

    assert second.all_call_cache_hit
    assert second.supplier_request_count == 0
    assert second.preview.cache_hit
    assert _file_state(second.preview.group_path) == before


def test_global_call_receives_only_validated_chapter_objects(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    call = json.loads(result.global_call.output_path.read_text(encoding="utf-8"))
    user_content = call["request"]["messages"][1]["content"]

    assert "validated_chapters" in user_content
    assert '"events"' not in user_content
    assert "source_generation" not in user_content
    assert result.global_call.backend == "mock"


def test_global_response_cannot_return_or_modify_chapters(tmp_path: Path) -> None:
    _, payload, chapter_result, response = _chapter_and_global(tmp_path)
    response["chapters"] = []

    with pytest.raises(DocumentContentPreviewError, match="字段集合"):
        validate_global_composition_response(
            response,
            composition_input=payload,
            chapters=chapter_result.chapters,
            context=__import__(
                "video_doc.document_chapter_composition", fromlist=["_chapter_context"]
            )._chapter_context(payload),
        )


def test_global_response_rejects_unvalidated_chapter_evidence(tmp_path: Path) -> None:
    _, payload, chapter_result, response = _chapter_and_global(tmp_path)
    response["overview"][0]["source_event_ids"] = ["a0004"]

    with pytest.raises(DocumentContentPreviewError, match="未进入已验证章节"):
        validate_global_composition_response(
            response,
            composition_input=payload,
            chapters=chapter_result.chapters,
            context=__import__(
                "video_doc.document_chapter_composition", fromlist=["_chapter_context"]
            )._chapter_context(payload),
        )


def test_global_high_risk_fact_drift_is_rejected(tmp_path: Path) -> None:
    _, payload, chapter_result, response = _chapter_and_global(tmp_path)
    response["overview"][0]["text"] = "样本包含15人。"

    with pytest.raises(DocumentContentPreviewError, match="数字/比例"):
        validate_global_composition_response(
            response,
            composition_input=payload,
            chapters=chapter_result.chapters,
            context=__import__(
                "video_doc.document_chapter_composition", fromlist=["_chapter_context"]
            )._chapter_context(payload),
        )


def test_optional_global_conclusion_is_supported(tmp_path: Path) -> None:
    _, payload, chapter_result, response = _chapter_and_global(tmp_path)
    response["overall_conclusion"] = [
        {
            "block_id": "conclusion-callout001",
            "type": "callout",
            "role": "conclusion",
            "text": "ModelA与ModelB的比较。",
            "content_origin": "stage9_v1",
            "source_content_paths": ["chapters[c0001].title"],
            "source_event_ids": ["a0001"],
        }
    ]

    document = assemble_structured_document_content(
        payload,
        chapter_result=chapter_result,
        global_response=response,
    )
    assert document["overall_conclusion"][0]["type"] == "callout"
    assert document["evidence_index"][-1]["block_id"] == "conclusion-callout001"


def test_evidence_index_time_is_derived_and_full_validator_rejects_tamper(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    tampered = deepcopy(result.document)
    tampered["evidence_index"][0]["end"] = 999
    tampered = attach_structured_document_content_id(tampered)
    from video_doc.document_chapter_composition import _chapter_context

    with pytest.raises(StructuredDocumentContentError, match="确定性派生"):
        validate_structured_document_content(tampered, context=_chapter_context(payload))


def test_global_failure_has_audit_and_never_publishes_preview(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    transport = InvalidGlobalTransport()

    with pytest.raises(DocumentContentPreviewError):
        compose_document_preview_offline_mock(
            output_root, "DEV01", payload, global_transport=transport
        )

    audit = (
        output_root
        / "DEV01"
        / "content"
        / "document-v2"
        / "attempts"
        / payload["input_id"]
        / "global"
        / "calls"
        / "document-global-validation-audit.json"
    )
    assert transport.call_count == 2
    assert json.loads(audit.read_text(encoding="utf-8"))["status"] == "failed"
    previews = output_root / "DEV01" / "content" / "document-v2" / "previews"
    assert not previews.exists() or not any(previews.iterdir())


def test_corrupt_preview_content_is_rejected(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    result.preview.document_path.write_bytes(
        result.preview.document_path.read_bytes() + b"\n"
    )

    with pytest.raises(DocumentContentPreviewError, match="manifest|确定性"):
        validate_document_preview_group(
            result.preview.group_path,
            composition_input=payload,
        )


def test_preview_extra_member_is_rejected(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    (result.preview.group_path / "extra.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DocumentContentPreviewError, match="额外成员"):
        validate_document_preview_group(
            result.preview.group_path,
            composition_input=payload,
        )


def test_manifest_records_full_input_versions_calls_and_file_identity(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    manifest = result.preview.manifest

    assert manifest["input"]["input_id"] == payload["input_id"]
    assert manifest["input"]["source_content_result_id"].startswith("content-")
    assert manifest["content_versions"] == result.document["content_versions"]
    assert manifest["calls"] == {
        "business_call_count": 2,
        "supplier_request_count": 2,
        "cache_hit_count": 0,
        "api_request_count": 0,
    }
    for name, record in manifest["files"].items():
        assert record["sha256"] == hashlib.sha256(
            (result.preview.group_path / name).read_bytes()
        ).hexdigest()


def test_profile_change_creates_distinct_content_and_preview(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    course = _composition_input("course")
    speech = _composition_input("speech")

    first = compose_document_preview_offline_mock(output_root, "DEV01", course)
    second = compose_document_preview_offline_mock(output_root, "DEV01", speech)

    assert first.content_id != second.content_id
    assert first.preview.group_path != second.preview.group_path
    assert first.preview.group_path.is_dir() and second.preview.group_path.is_dir()


def test_r4_explicit_configs_do_not_load_dotenv(tmp_path: Path, monkeypatch: Any) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("R4不得读取.env")

    monkeypatch.setattr("video_doc.llm_adapter.load_llm_config", forbidden)
    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    assert result.document["generation"]["api_request_count"] == 0


def test_no_temporary_group_or_current_remains_after_success(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    result = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    previews_root = result.preview.group_path.parent

    assert not any(path.name.startswith(".") for path in previews_root.iterdir())
    document_v2 = output_root / "DEV01" / "content" / "document-v2"
    assert not (document_v2 / "current.json").exists()
    assert not (document_v2 / "formal-results").exists()


def test_accepted_preview_publishes_byte_identical_formal_group_and_current(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    preview = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    preview_state = _file_state(preview.preview.group_path)

    formal = publish_accepted_document_preview(
        output_root,
        "DEV01",
        composition_input=payload,
        content_id=preview.content_id,
    )

    assert formal.cache_hit is False
    assert {
        name: identity[:2]
        for name, identity in _file_state(formal.group.group_path).items()
    } == {name: identity[:2] for name, identity in preview_state.items()}
    assert formal.current == {
        "schema_version": DOCUMENT_FORMAL_CURRENT_SCHEMA_VERSION,
        "status": "accepted",
        "task_id": "DEV01",
        "content_id": preview.content_id,
        "group": f"formal-results/{preview.content_id}",
        "document_sha256": preview.preview.execution_report["document_sha256"],
        "publisher_version": DOCUMENT_FORMAL_PUBLISHER_VERSION,
    }
    assert formal.current_path.read_text(encoding="utf-8").endswith("\n")
    assert _file_state(preview.preview.group_path) == preview_state

    before = _file_state(formal.group.group_path.parent.parent)
    cached = publish_accepted_document_preview(
        output_root,
        "DEV01",
        composition_input=payload,
        content_id=preview.content_id,
    )
    assert cached.cache_hit is True
    assert _file_state(formal.group.group_path.parent.parent) == before


def test_damaged_existing_formal_group_is_rejected_without_repair(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    preview = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    formal = publish_accepted_document_preview(
        output_root,
        "DEV01",
        composition_input=payload,
        content_id=preview.content_id,
    )
    document_path = formal.group.document_path
    document_path.write_text("{}\n", encoding="utf-8")
    damaged = document_path.read_bytes()

    with pytest.raises(DocumentContentPreviewError):
        publish_accepted_document_preview(
            output_root,
            "DEV01",
            composition_input=payload,
            content_id=preview.content_id,
        )
    assert document_path.read_bytes() == damaged
