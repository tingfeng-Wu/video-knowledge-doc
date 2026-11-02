from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

from tests.test_document_chapter_composition import _composition_input, _workspace
from tests.test_document_content_preview import _chapter_and_global, _file_state
from video_doc.document_chapter_composition import compose_document_chapters_offline_mock
from video_doc.document_content_preview import (
    DOCUMENT_PREVIEW_FAILURE_AUDIT_SCHEMA_VERSION,
    DeterministicDocumentGlobalMockTransport,
    DocumentContentPreviewError,
    InjectedDocumentContentPreviewFailure,
    assemble_structured_document_content,
    compose_document_preview_offline_mock,
    publish_document_preview,
)
from video_doc.llm_adapter import LLMConfig
from video_doc.structured_document_content import (
    StructuredDocumentContentError,
    validate_structured_document_content,
)


def _snapshot_outside_document_v2(output_root: Path) -> dict[str, tuple[str, int, int]]:
    task_root = output_root / "DEV01"
    return {
        path.relative_to(task_root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in task_root.rglob("*")
        if path.is_file()
        and "content/document-v2/" not in path.relative_to(task_root).as_posix()
        and not path.relative_to(task_root).as_posix().startswith("logs/")
    }


def _assembled_document(
    output_root: Path, payload: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    chapter_result = compose_document_chapters_offline_mock(
        output_root, "DEV01", payload
    )
    titles = [chapter.chapter["title"] for chapter in chapter_result.chapters]
    response = DeterministicDocumentGlobalMockTransport().complete(
        LLMConfig(
            backend="mock",
            base_url="",
            model="fixture",
            timeout_seconds=1,
            api_key="",
            mock_response={},
        ),
        [
            {
                "role": "user",
                "content": "DOCUMENT_GLOBAL_INPUT_JSON="
                + json.dumps(
                    {"validated_chapters": [{"chapter": {"title": title}} for title in titles]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ],
        {},
    )
    return chapter_result, assemble_structured_document_content(
        payload,
        chapter_result=chapter_result,
        global_response=response,
    )


def _stage10_pure_consumer_fixture(document: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """模拟阶段10只接收一个2.0对象，不读取ASR、文件或LLM配置。"""

    validate_structured_document_content(document)
    payload = deepcopy(dict(document))
    canonical_result_json = (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    render_input = {
        "content_id": payload["content_id"],
        "language": payload["language"],
        "document_profile": payload["document_profile"],
        "title": payload["title"],
        "overview": payload["overview"],
        "chapters": payload["chapters"],
        "overall_conclusion": payload["overall_conclusion"],
        "evidence_index": payload["evidence_index"],
        "chaptering_provenance": payload["chaptering_provenance"],
    }
    return canonical_result_json, render_input


def test_r5_failure_audit_version_is_frozen() -> None:
    assert DOCUMENT_PREVIEW_FAILURE_AUDIT_SCHEMA_VERSION == "1.0"


@pytest.mark.parametrize(
    "failure_point",
    [
        "after_document_content",
        "after_execution_report",
        "after_manifest",
        "before_group_publish",
        "after_group_publish",
    ],
)
def test_preview_failure_injection_rolls_back_new_group_and_preserves_old_group(
    tmp_path: Path,
    failure_point: str,
) -> None:
    output_root = _workspace(tmp_path)
    first = compose_document_preview_offline_mock(
        output_root, "DEV01", _composition_input("course")
    )
    old_state = _file_state(first.preview.group_path)
    payload = _composition_input("speech")
    chapter_result, document = _assembled_document(output_root, payload)

    with pytest.raises(InjectedDocumentContentPreviewFailure, match=failure_point):
        publish_document_preview(
            output_root,
            "DEV01",
            composition_input=payload,
            document=document,
            business_call_count=chapter_result.business_call_count + 1,
            supplier_request_count=chapter_result.supplier_request_count + 1,
            cache_hit_count=chapter_result.cache_hit_count,
            failure_injection=failure_point,
        )

    assert _file_state(first.preview.group_path) == old_state
    assert not (
        first.preview.group_path.parent / document["content_id"]
    ).exists()
    assert not list(first.preview.group_path.parent.glob(".*.tmp-*"))
    document_v2 = output_root / "DEV01" / "content" / "document-v2"
    assert not (document_v2 / "current.json").exists()
    assert not (document_v2 / "formal-results").exists()
    audits = list((document_v2 / "failures").glob("*/failure.json"))
    assert len(audits) == 1
    audit_text = audits[0].read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit["schema_version"] == "1.0"
    assert audit["status"] == "failed"
    assert audit["content_id"] == document["content_id"]
    assert audit["failure_stage"] in {
        "document-content.json",
        "execution-report.json",
        "manifest.json",
        "group_publish",
    }
    assert str(output_root) not in audit_text
    assert document["overview"][0]["text"] not in audit_text


def test_unknown_preview_failure_point_is_rejected_before_preview_write(
    tmp_path: Path,
) -> None:
    output_root, payload, chapter_result, response = _chapter_and_global(tmp_path)
    document = assemble_structured_document_content(
        payload,
        chapter_result=chapter_result,
        global_response=response,
    )

    with pytest.raises(DocumentContentPreviewError, match="未知"):
        publish_document_preview(
            output_root,
            "DEV01",
            composition_input=payload,
            document=document,
            business_call_count=2,
            supplier_request_count=2,
            cache_hit_count=0,
            failure_injection="unknown",
        )

    document_v2 = output_root / "DEV01" / "content" / "document-v2"
    assert not (document_v2 / "previews").exists()
    assert not (document_v2 / "failures").exists()


def test_corrupt_preview_cache_hard_fails_without_repair_or_rewrite(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    first = compose_document_preview_offline_mock(output_root, "DEV01", payload)
    first.preview.document_path.write_bytes(b"{}\n")
    corrupt_state = _file_state(first.preview.group_path)

    with pytest.raises(DocumentContentPreviewError):
        compose_document_preview_offline_mock(output_root, "DEV01", payload)

    assert _file_state(first.preview.group_path) == corrupt_state
    failures = list(
        (output_root / "DEV01" / "content" / "document-v2" / "failures").glob(
            "*/failure.json"
        )
    )
    assert len(failures) == 1
    failure = json.loads(failures[0].read_text(encoding="utf-8"))
    assert failure["failure_stage"] == "cache_validation"
    assert "{}" not in failure["message"]


def test_success_failure_and_cache_paths_preserve_all_legacy_artifacts(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    task_root = output_root / "DEV01"
    legacy_documents = task_root / "documents"
    legacy_documents.mkdir(parents=True, exist_ok=True)
    (legacy_documents / "current.json").write_text(
        '{"export_id":"export-frozen"}\n', encoding="utf-8"
    )
    legacy_group = legacy_documents / "formal-results" / "export-frozen"
    legacy_group.mkdir(parents=True)
    (legacy_group / "manifest.json").write_text("frozen\n", encoding="utf-8")
    before = _snapshot_outside_document_v2(output_root)

    first = compose_document_preview_offline_mock(
        output_root, "DEV01", _composition_input("course")
    )
    second = compose_document_preview_offline_mock(
        output_root, "DEV01", _composition_input("course")
    )
    with pytest.raises(InjectedDocumentContentPreviewFailure):
        compose_document_preview_offline_mock(
            output_root,
            "DEV01",
            _composition_input("speech"),
            preview_failure_injection="after_manifest",
        )

    assert first.preview.cache_hit is False
    assert second.preview.cache_hit is True
    assert _snapshot_outside_document_v2(output_root) == before
    assert (legacy_documents / "current.json").read_text(encoding="utf-8") == (
        '{"export_id":"export-frozen"}\n'
    )


def test_stage10_fixture_consumes_only_complete_v2_object_without_io_or_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _workspace(tmp_path)
    result = compose_document_preview_offline_mock(
        output_root, "DEV01", _composition_input()
    )
    before = _file_state(output_root / "DEV01")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("阶段10纯消费夹具不得读取LLM配置")

    monkeypatch.setattr("video_doc.llm_adapter.load_llm_config", forbidden)
    first_json, first_view = _stage10_pure_consumer_fixture(result.document)
    second_json, second_view = _stage10_pure_consumer_fixture(result.document)

    assert first_json == second_json
    assert first_view == second_view
    assert json.loads(first_json)["content_id"] == result.content_id
    assert set(first_view) == {
        "content_id",
        "language",
        "document_profile",
        "title",
        "overview",
        "chapters",
        "overall_conclusion",
        "evidence_index",
        "chaptering_provenance",
    }
    assert first_view["chapters"] == result.document["chapters"]
    assert _file_state(output_root / "DEV01") == before


def test_stage10_fixture_rejects_invalid_object_without_asr_context(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    result = compose_document_preview_offline_mock(
        output_root, "DEV01", _composition_input()
    )
    tampered = deepcopy(result.document)
    tampered["content_id"] = "document-content-000000000000"

    with pytest.raises(StructuredDocumentContentError, match="内容身份"):
        _stage10_pure_consumer_fixture(tampered)
