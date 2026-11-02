from pathlib import Path
from types import SimpleNamespace

import pytest

import video_doc.m1_pipeline as pipeline


def _cached_dependencies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    calls: list[str] = []

    def record(name: str, result: SimpleNamespace):
        def fake(*args, **kwargs):
            del args, kwargs
            calls.append(name)
            return result

        return fake

    group = tmp_path / "DEV01" / "documents" / "formal-results" / "export-abc"
    source = SimpleNamespace(
        content_id="document-content-123456789abc",
        document={"document_profile": "teaching_video"},
    )
    monkeypatch.setattr(
        pipeline, "extract_task_audio", record("audio", SimpleNamespace(cache_hit=True))
    )
    monkeypatch.setattr(
        pipeline,
        "transcribe_task_audio",
        record("asr", SimpleNamespace(cache_hit=True)),
    )
    monkeypatch.setattr(
        pipeline, "build_task_timeline", record("timeline", SimpleNamespace(cache_hit=True))
    )
    monkeypatch.setattr(
        pipeline,
        "prepare_task_chapter_inputs",
        record("chapter_inputs", SimpleNamespace(cache_hit=True, event_count=262)),
    )
    monkeypatch.setattr(
        pipeline,
        "load_authoritative_document_content_v2",
        record("load_v2", source),
    )
    monkeypatch.setattr(
        pipeline,
        "prepare_document_composition_input",
        record("composition", SimpleNamespace(composition_input={"input_id": "x"})),
    )
    monkeypatch.setattr(
        pipeline,
        "publish_accepted_document_preview",
        record("stage9_v2", SimpleNamespace(cache_hit=True)),
    )
    monkeypatch.setattr(
        pipeline,
        "publish_document_export_v2",
        record(
            "export",
            SimpleNamespace(
                cache_hit=True,
                export_id="export-abc",
                group_path=group,
                manifest={"chapter_count": 5},
            ),
        ),
    )
    return calls


def test_run_task_m1_replays_only_cached_local_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _cached_dependencies(monkeypatch, tmp_path)

    result = pipeline.run_task_m1(tmp_path, "DEV01")

    assert calls == [
        "audio",
        "asr",
        "timeline",
        "chapter_inputs",
        "load_v2",
        "composition",
        "stage9_v2",
        "load_v2",
        "export",
    ]
    assert result.api_request_count == 0
    assert result.export_cache_hit is True
    assert result.chapter_count == 5
    assert result.event_count == 262
    assert result.result_docx_path == result.result_group_path / "result.docx"


def test_run_task_m1_stops_at_first_cache_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _cached_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(
        pipeline,
        "extract_task_audio",
        lambda *args, **kwargs: SimpleNamespace(cache_hit=False),
    )

    with pytest.raises(pipeline.M1PipelineError, match="音频未命中缓存"):
        pipeline.run_task_m1(tmp_path, "DEV01")

    assert calls == []


def test_asr_model_load_is_structurally_blocked() -> None:
    with pytest.raises(pipeline.ASRTranscriptionError, match="禁止加载ASR模型"):
        pipeline._block_asr_model_load("small", "cpu", "int8")
