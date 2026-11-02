import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from tests.test_document_chapter_composition import _composition_input, _workspace
from video_doc.document_chapter_composition import (
    DeterministicDocumentChapterMockTransport,
)
from video_doc.document_content_preview import (
    DeterministicDocumentGlobalMockTransport,
    DocumentContentPreviewError,
    compose_document_preview_real,
)
from video_doc.llm_adapter import LLMConfig


def _real_config() -> LLMConfig:
    return LLMConfig(
        backend="openai_compatible",
        base_url="https://example.invalid/v1/chat/completions",
        model="fake-real-model",
        timeout_seconds=1.0,
        api_key="test-only-secret",
        mock_response={},
        enable_thinking=False,
        temperature=0.0,
    )


def _mock_config(model: str) -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model=model,
        timeout_seconds=1.0,
        api_key="",
        mock_response={},
        enable_thinking=False,
        temperature=0.0,
    )


class FakeRealCompositionTransport:
    def __init__(self, *, fail_first: bool = False, fail_global: bool = False) -> None:
        self.fail_first = fail_first
        self.fail_global = fail_global
        self.call_count = 0
        self.chapter = DeterministicDocumentChapterMockTransport()
        self.global_ = DeterministicDocumentGlobalMockTransport()
        self.payloads: list[dict[str, Any]] = []
        self.response_formats: list[dict[str, Any]] = []

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        assert config.backend == "openai_compatible"
        self.call_count += 1
        self.response_formats.append(dict(response_format))
        if self.fail_first and self.call_count == 1:
            return {"invalid": "触发唯一一次结构纠错"}
        markers = (
            "DOCUMENT_CHAPTER_INPUT_JSON=",
            "DOCUMENT_CHAPTER_MERGE_INPUT_JSON=",
            "DOCUMENT_WINDOW_INPUT_JSON=",
            "DOCUMENT_GLOBAL_INPUT_JSON=",
        )
        user_content, marker = next(
            (str(message["content"]), item)
            for message in messages
            for item in markers
            if item in str(message["content"])
        )
        self.payloads.append(json.loads(user_content.split(marker, 1)[1]))
        if marker == "DOCUMENT_GLOBAL_INPUT_JSON=":
            if self.fail_global:
                return {"invalid": "全文结构失败"}
            return self.global_.complete(
                _mock_config("global-fixture"), messages, response_format
            )
        return self.chapter.complete(
            _mock_config("chapter-fixture"), messages, response_format
        )


class FailIfCalledTransport:
    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        del config, messages, response_format
        raise AssertionError("真实配置缓存命中时不得访问transport")


def test_fake_real_success_uses_shared_pipeline_and_isolated_preview(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    transport = FakeRealCompositionTransport()

    result = compose_document_preview_real(
        output_root,
        "DEV01",
        payload,
        config=_real_config(),
        transport=transport,
    )

    assert result.business_call_count == 2
    assert result.supplier_request_count == 2
    generation = result.document["generation"]
    assert generation["mode"] == "document-composition-real"
    assert generation["backend"] == "openai_compatible"
    assert generation["model"] == "fake-real-model"
    assert generation["temperature"] == 0.0
    assert generation["api_request_count"] == 2
    assert result.preview.execution_report["api_request_count"] == 2
    assert not (result.preview.group_path.parent.parent / "current.json").exists()
    assert not (result.preview.group_path.parent.parent / "formal-results").exists()
    assert set(transport.payloads[0]) == {
        "input_id",
        "document_profile",
        "chapter",
        "window_drafts",
        "versions",
    }
    assert set(transport.payloads[1]) == {
        "input_id",
        "document_profile",
        "validated_chapters",
        "versions",
    }
    assert transport.response_formats == [
        {"type": "json_object"},
        {"type": "json_object"},
    ]


def test_fake_real_allows_one_correction_and_counts_requests(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    transport = FakeRealCompositionTransport(fail_first=True)

    result = compose_document_preview_real(
        output_root,
        "DEV01",
        _composition_input(),
        config=_real_config(),
        transport=transport,
    )

    assert transport.call_count == 3
    assert result.supplier_request_count == 3
    assert result.document["generation"]["api_request_count"] == 3


def test_fake_real_partial_failure_does_not_publish_preview(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()

    with pytest.raises(DocumentContentPreviewError):
        compose_document_preview_real(
            output_root,
            "DEV01",
            payload,
            config=_real_config(),
            transport=FakeRealCompositionTransport(fail_global=True),
        )

    preview_root = output_root / "DEV01" / "content" / "document-v2" / "previews"
    assert not preview_root.exists()


def test_fake_real_rerun_is_zero_transport_and_does_not_rewrite(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    payload = _composition_input()
    first = compose_document_preview_real(
        output_root,
        "DEV01",
        payload,
        config=_real_config(),
        transport=FakeRealCompositionTransport(),
    )
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in first.preview.group_path.iterdir()
    }

    second = compose_document_preview_real(
        output_root,
        "DEV01",
        payload,
        config=_real_config(),
        transport=FailIfCalledTransport(),
    )

    assert second.all_call_cache_hit
    assert second.supplier_request_count == 0
    assert second.content_id == first.content_id
    assert second.preview.cache_hit
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in second.preview.group_path.iterdir()
    } == before


def test_real_entry_rejects_mock_backend_before_writing(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    with pytest.raises(DocumentContentPreviewError, match="openai_compatible"):
        compose_document_preview_real(
            output_root,
            "DEV01",
            _composition_input(),
            config=_mock_config("not-real"),
            transport=FailIfCalledTransport(),
        )
    assert not (output_root / "DEV01" / "content" / "document-v2").exists()
