from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_doc.asr_only_pipeline as pipeline
from video_doc.asr_document_v2 import (
    _cap_document_paragraphs,
    _has_recorded_json_parse_failure,
    _load_global_conclusion_recovery,
)
from video_doc.chapter_content import (
    ChapterContentError,
    _normalize_generic_shape_variants,
    _normalize_merge_response,
)


def _patch_local_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, authoritative=None
) -> list[str]:
    calls: list[str] = []
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    tools = SimpleNamespace(directory=tmp_path, ffmpeg=tmp_path / "ffmpeg.exe", ffprobe=tmp_path / "ffprobe.exe")
    monkeypatch.setattr(pipeline, "resolve_media_tools", lambda value=None: tools)
    monkeypatch.setattr(pipeline, "_media_tool_path", lambda value: nullcontext())
    monkeypatch.setattr(pipeline, "_resolve_source", lambda *args: (source, True))
    monkeypatch.setattr(
        pipeline,
        "probe_video",
        lambda value: SimpleNamespace(has_audio=True),
    )
    monkeypatch.setattr(
        pipeline,
        "initialize_task_workspace",
        lambda *args, **kwargs: calls.append("init"),
    )
    monkeypatch.setattr(
        pipeline,
        "_save_metadata_if_changed",
        lambda *args, **kwargs: calls.append("metadata"),
    )
    monkeypatch.setattr(
        pipeline,
        "extract_task_audio",
        lambda *args, **kwargs: calls.append("audio") or SimpleNamespace(cache_hit=True),
    )
    monkeypatch.setattr(
        pipeline,
        "transcribe_task_audio",
        lambda *args, **kwargs: calls.append("asr") or SimpleNamespace(cache_hit=True),
    )
    monkeypatch.setattr(
        pipeline,
        "build_task_timeline",
        lambda *args, **kwargs: calls.append("timeline") or SimpleNamespace(event_count=20, cache_hit=True),
    )
    monkeypatch.setattr(
        pipeline,
        "_existing_authoritative_content",
        lambda *args: authoritative,
    )
    return calls


def _export(tmp_path: Path):
    group = tmp_path / "outputs" / "NEW01" / "documents" / "formal-results" / "export-abc"
    return SimpleNamespace(
        group_path=group,
        export_id="export-abc",
        manifest={"chapter_count": 2},
        cache_hit=False,
    )


def _document_v2(source: object, *, reused: bool = False):
    return SimpleNamespace(
        source=SimpleNamespace(content_id="document-content-abc123def456"),
        review_status="needs_review",
        planned_call_count=3,
        business_call_count=0 if reused else 3,
        supplier_request_count=0 if reused else 3,
        all_llm_cache_hit=reused,
        reused_candidate=reused,
    )


def test_new_task_runs_current_asr_only_chain_and_exports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_local_stages(monkeypatch, tmp_path)
    authoritative = object()
    monkeypatch.setattr(
        pipeline,
        "plan_task_chapters_formal",
        lambda *args, **kwargs: calls.append("boundaries")
        or SimpleNamespace(business_call_count=1, supplier_request_count=1),
    )
    monkeypatch.setattr(
        pipeline,
        "estimate_task_chapter_content_calls",
        lambda *args, **kwargs: SimpleNamespace(business_call_count=13),
    )
    monkeypatch.setattr(
        pipeline,
        "generate_task_chapter_content_generic",
        lambda *args, **kwargs: calls.append("content")
        or SimpleNamespace(
            supplier_request_count=13, all_cache_hit=False, cache_hit_count=0
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "load_authoritative_chapter_content",
        lambda *args: authoritative,
    )
    monkeypatch.setattr(
        pipeline, "compose_task_document_v2_candidate",
        lambda *args, **kwargs: calls.append("document_v2") or _document_v2(authoritative),
    )
    monkeypatch.setattr(
        pipeline, "publish_document_export_v2",
        lambda *args: calls.append("export") or _export(tmp_path),
    )

    result = pipeline.run_task_asr_only(
        tmp_path / "outputs",
        "NEW01",
        source_video=tmp_path / "source.mp4",
        allow_llm=True,
    )

    assert calls == [
        "init",
        "metadata",
        "audio",
        "asr",
        "timeline",
        "boundaries",
        "content",
        "document_v2",
        "export",
    ]
    assert result.chapter_count == 2
    assert result.content_planned_call_count == 13
    assert result.content_business_call_count == 13
    assert result.supplier_request_count == 17
    assert result.review_status == "needs_review"
    assert result.result_docx_path.name == "result.docx"


def test_existing_authoritative_content_replays_without_llm_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    authoritative = object()
    calls = _patch_local_stages(monkeypatch, tmp_path, authoritative=authoritative)
    monkeypatch.setattr(
        pipeline,
        "plan_task_chapters_formal",
        lambda *args, **kwargs: pytest.fail("不得调用分章LLM"),
    )
    monkeypatch.setattr(
        pipeline,
        "generate_task_chapter_content_generic",
        lambda *args, **kwargs: pytest.fail("不得调用正文LLM"),
    )
    monkeypatch.setattr(
        pipeline, "compose_task_document_v2_candidate",
        lambda *args, **kwargs: calls.append("document_v2") or _document_v2(authoritative, reused=True),
    )
    monkeypatch.setattr(
        pipeline, "publish_document_export_v2",
        lambda *args: calls.append("export") or _export(tmp_path),
    )

    result = pipeline.run_task_asr_only(tmp_path, "NEW01")

    assert result.reused_authoritative_content is True
    assert result.all_llm_cache_hit is True
    assert result.supplier_request_count == 0
    assert calls[-1] == "export"


def test_missing_content_requires_explicit_llm_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_local_stages(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline,
        "plan_task_chapters_formal",
        lambda *args, **kwargs: pytest.fail("不得在授权前调用LLM"),
    )

    with pytest.raises(pipeline.ASROnlyPipelineError, match="LLM_APPROVAL_REQUIRED"):
        pipeline.run_task_asr_only(tmp_path, "NEW01")


def test_content_budget_stops_before_first_content_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_local_stages(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline,
        "plan_task_chapters_formal",
        lambda *args, **kwargs: SimpleNamespace(
            business_call_count=0, supplier_request_count=0
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "estimate_task_chapter_content_calls",
        lambda *args, **kwargs: SimpleNamespace(business_call_count=13),
    )
    monkeypatch.setattr(
        pipeline,
        "generate_task_chapter_content_generic",
        lambda *args, **kwargs: pytest.fail("预算超限不得调用正文LLM"),
    )

    with pytest.raises(
        pipeline.ASROnlyPipelineError, match="CONTENT_CALL_BUDGET_EXCEEDED"
    ):
        pipeline.run_task_asr_only(
            tmp_path, "NEW01", allow_llm=True, max_content_calls=12
        )


def test_resolve_source_rejects_rebinding_existing_task(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "outputs"
    original = tmp_path / "original.mp4"
    other = tmp_path / "other.mp4"
    original.write_bytes(b"original")
    other.write_bytes(b"different")
    pipeline.initialize_task_workspace(
        output_root, "NEW01", source_video=original
    )

    with pytest.raises(pipeline.ASROnlyPipelineError, match="SOURCE_VIDEO_MISMATCH"):
        pipeline._resolve_source(output_root, "NEW01", other)


def test_incomplete_authoritative_alias_is_not_treated_as_cache_miss(
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "NEW01" / "content"
    content_root.mkdir(parents=True)
    (content_root / "content.json").write_text("{}", encoding="utf-8")

    with pytest.raises(pipeline.ASROnlyPipelineError, match="拒绝覆盖"):
        pipeline._existing_authoritative_content(tmp_path, "NEW01")


def _merge_response() -> dict:
    field = lambda text, event: {"text": text, "source_event_ids": [event]}
    return {
        "title": field("标题", "a0001"),
        "summary": field("摘要", "a0002"),
        "key_points": [
            field("知识点一", "a0001"),
            field("知识点二", "a0002"),
            field("知识点三", "a0003"),
        ],
        "keywords": [
            field("关键词一", "a0001"),
            field("关键词二", "a0002"),
            field("关键词三", "a0003"),
        ],
    }


def test_merge_normalizer_drops_only_known_redundant_wrapper_fields() -> None:
    response = _merge_response()
    response["chapter_id"] = "c0001"
    response["source_event_ids"] = ["untrusted-extra-value"]

    normalized = _normalize_merge_response(
        response,
        expected_chapter_id="c0001",
        chapter_event_ids=["a0001", "a0002", "a0003"],
        window_results=[
            {"source_event_ids": ["a0001", "a0002"]},
            {"source_event_ids": ["a0003"]},
        ],
    )

    assert normalized["source_event_ids"] == ["a0001", "a0002", "a0003"]


def test_merge_normalizer_still_rejects_unknown_or_wrong_wrapper_fields() -> None:
    response = _merge_response()
    response["unexpected"] = True
    with pytest.raises(ChapterContentError, match="未知额外字段"):
        _normalize_merge_response(
            response,
            expected_chapter_id="c0001",
            chapter_event_ids=["a0001", "a0002", "a0003"],
            window_results=[{"source_event_ids": ["a0001", "a0002", "a0003"]}],
        )

    response = _merge_response()
    response["chapter_id"] = "c9999"
    with pytest.raises(ChapterContentError, match="当前章节不一致"):
        _normalize_merge_response(
            response,
            expected_chapter_id="c0001",
            chapter_event_ids=["a0001", "a0002", "a0003"],
            window_results=[{"source_event_ids": ["a0001", "a0002", "a0003"]}],
        )


def test_generic_shape_compat_is_bounded_and_keeps_evidence_validation() -> None:
    response = _merge_response()
    response["key_points"] = [
        [f"知识点{index}", f"a{index:04d}"] for index in range(1, 8)
    ]
    response["keywords"] = [
        [f"关键词{index}", f"a{index:04d}"] for index in range(1, 11)
    ]
    response["keywords"][4] = ["无证据关键词", "c0001"]

    repaired = _normalize_generic_shape_variants(
        response, allowed_ids=[f"a{index:04d}" for index in range(1, 11)]
    )

    assert len(repaired["key_points"]) == 6
    assert len(repaired["keywords"]) == 8
    assert all(item["text"] != "无证据关键词" for item in repaired["keywords"])
    assert repaired["key_points"][0] == {
        "text": "知识点1",
        "source_event_ids": ["a0001"],
    }


def test_document_v2_paragraph_cap_merges_adjacent_ledgers_without_loss() -> None:
    paragraphs = [
        {
            "source_unit_ids": [f"c0001-u{index:03d}"],
            "fact_ids": [f"c0001-f{index:03d}"],
            "text": "段落" * index,
        }
        for index in range(1, 6)
    ]

    capped = _cap_document_paragraphs(paragraphs)

    assert len(capped) == 4
    assert [item for block in capped for item in block["fact_ids"]] == [
        f"c0001-f{index:03d}" for index in range(1, 6)
    ]
    assert [item for block in capped for item in block["source_unit_ids"]] == [
        f"c0001-u{index:03d}" for index in range(1, 6)
    ]


def test_recorded_json_parse_failure_selects_compact_resume(tmp_path: Path) -> None:
    audit = (
        tmp_path
        / "LONG01"
        / "content"
        / "document-v2"
        / "attempts"
        / "composition-input-abc"
        / "continuous-content-v1"
        / "c0004"
        / "calls"
        / "continuous-content-c0004-validation-audit.json"
    )
    audit.parent.mkdir(parents=True)
    audit.write_text(
        '{"status":"failed","attempts":[{"error":"LLM返回内容不是合法JSON。"}]}',
        encoding="utf-8",
    )

    assert _has_recorded_json_parse_failure(
        tmp_path,
        "LONG01",
        input_id="composition-input-abc",
        chapter_id="c0004",
    )


def test_global_three_conclusion_failure_is_recovered_offline(tmp_path: Path) -> None:
    audit = (
        tmp_path
        / "LONG01"
        / "content"
        / "document-v2"
        / "attempts"
        / "composition-input-abc"
        / "continuous-content-v1"
        / "global"
        / "calls"
        / "continuous-content-global-validation-audit.json"
    )
    audit.parent.mkdir(parents=True)
    audit.write_text(
        json.dumps(
            {
                "status": "failed",
                "attempts": [
                    {
                        "error": "请求C核心结论必须为1至2段。",
                        "raw_response": {
                            "title": "标题",
                            "overview": ["甲乙"],
                            "conclusions": ["甲", "乙", "丙"],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload = {
        "profile": "short_explainer",
        "chapters": [
            {"chapter_id": "c0001", "title": "标题", "paragraphs": ["甲乙丙"]}
        ],
    }

    recovered = _load_global_conclusion_recovery(
        tmp_path,
        "LONG01",
        input_id="composition-input-abc",
        payload=payload,
    )

    assert recovered["conclusions"] == ["甲 乙", "丙"]
