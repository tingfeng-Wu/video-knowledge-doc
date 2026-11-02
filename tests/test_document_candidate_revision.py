from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from video_doc import document_candidate_revision as revision
from video_doc.asr_document_v2 import ASRDocumentV2Error
from video_doc.document_content_preview import DocumentPreviewGroup
from video_doc.structured_document_content import attach_structured_document_content_id


def _document() -> dict:
    return attach_structured_document_content_id(
        {
            "generation": {"mode": "document-composition-real", "api_request_count": 2},
            "overview": [{"type": "paragraph", "text": "旧概述"}],
            "chapters": [
                {
                    "chapter_id": "c0001",
                    "title": {"text": "旧章标题"},
                    "keywords": [{"text": "旧关键词"}],
                    "sections": [
                        {"blocks": [{"block_id": "c0001-s001-p001", "type": "paragraph", "text": "旧正文"}]}
                    ]
                }
            ],
            "overall_conclusion": [
                {"block_id": "conclusion-p001", "type": "paragraph", "text": "旧结论"}
            ],
        }
    )


def _spec(source_content_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "revision_id": "review-r1",
        "task_id": "SYNTH01",
        "document_profile": "course",
        "source_content_id": source_content_id,
        "reason": "人工复核后的有限文本修订",
        "replacements": [
            {"scope": "overview", "target": "0", "text": "新概述"},
            {"scope": "chapter_block", "target": "c0001-s001-p001", "text": "新正文"},
            {"scope": "conclusion", "target": "conclusion-p001", "text": "新结论"},
        ],
    }


def test_replace_text_only_changes_selected_paragraphs() -> None:
    document = _document()
    before = deepcopy(document)
    for item in _spec(document["content_id"])["replacements"]:
        revision._replace_text(document, item)
    assert document["overview"][0]["text"] == "新概述"
    assert document["chapters"][0]["sections"][0]["blocks"][0]["text"] == "新正文"
    assert document["overall_conclusion"][0]["text"] == "新结论"
    before["overview"][0]["text"] = "新概述"
    before["chapters"][0]["sections"][0]["blocks"][0]["text"] = "新正文"
    before["overall_conclusion"][0]["text"] = "新结论"
    assert document == before


def test_replace_text_supports_title_and_keyword_fields() -> None:
    document = _document()
    revision._replace_text(
        document, {"scope": "chapter_title", "target": "c0001", "text": "新章标题"}
    )
    revision._replace_text(
        document,
        {"scope": "chapter_keyword", "target": "c0001:0", "text": "新关键词"},
    )
    assert document["chapters"][0]["title"]["text"] == "新章标题"
    assert document["chapters"][0]["keywords"][0]["text"] == "新关键词"


def test_revision_locks_source_and_publishes_zero_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_document = _document()
    composition_input = {"input_id": "input-1", "document_profile": "course"}
    source_preview = DocumentPreviewGroup(
        group_path=tmp_path / source_document["content_id"],
        document_path=tmp_path / "source.json",
        execution_report_path=tmp_path / "report.json",
        manifest_path=tmp_path / "manifest.json",
        document=source_document,
        execution_report={},
        manifest={},
        cache_hit=False,
    )
    monkeypatch.setattr(
        revision,
        "prepare_document_composition_input",
        lambda *args, **kwargs: type(
            "Prepared", (), {"composition_input": composition_input}
        )(),
    )
    monkeypatch.setattr(
        revision,
        "_load_candidate",
        lambda *args, **kwargs: (source_preview, tmp_path / "candidate-current.json"),
    )
    published: dict = {}

    def fake_publish(*args, **kwargs):
        published.update(kwargs)
        document = kwargs["document"]
        return DocumentPreviewGroup(
            group_path=tmp_path / document["content_id"],
            document_path=tmp_path / "new.json",
            execution_report_path=tmp_path / "new-report.json",
            manifest_path=tmp_path / "new-manifest.json",
            document=document,
            execution_report={},
            manifest={},
            cache_hit=False,
        )

    monkeypatch.setattr(revision, "publish_document_preview", fake_publish)
    monkeypatch.setattr(revision, "_write_candidate_pointer", lambda *args, **kwargs: False)
    monkeypatch.setattr(revision, "_preview_source", lambda preview, **kwargs: preview.document)
    source, report_path, cache_hit = revision.revise_task_document_v2_candidate(
        tmp_path, "SYNTH01", revision_spec=_spec(source_document["content_id"])
    )
    assert source["content_id"] != source_document["content_id"]
    assert source["generation"]["mode"] == "document-composition-offline-publish"
    assert source["generation"]["api_request_count"] == 0
    assert published["business_call_count"] == 0
    assert published["supplier_request_count"] == 0
    assert published["api_request_count"] == 0
    assert report_path.is_file()
    assert cache_hit is False

    bad_spec = _spec("document-content-000000000000")
    with pytest.raises(ASRDocumentV2Error, match="source_content_id"):
        revision.revise_task_document_v2_candidate(
            tmp_path, "SYNTH01", revision_spec=bad_spec
        )
