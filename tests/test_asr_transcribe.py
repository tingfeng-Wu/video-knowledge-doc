import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_doc.cli as cli
from video_doc.asr_transcribe import (
    ASRTranscriptionError,
    ASRTranscriptionResult,
    transcribe_task_audio,
)
from video_doc.task_workspace import initialize_task_workspace


def _prepare_task(tmp_path: Path, *, duration: float = 12.5) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01", source_video=source)
    audio_path = workspace.root / "audio" / "audio.wav"
    audio_path.write_bytes(b"fake-wav")
    (workspace.root / "audio" / "audio.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "output": {
                    "path": "audio.wav",
                    "duration_seconds": duration,
                },
            }
        ),
        encoding="utf-8",
    )
    return output_root


class FakeModel:
    def __init__(self, segments: list[SimpleNamespace], info: SimpleNamespace) -> None:
        self._segments = segments
        self._info = info
        self.calls: list[tuple[str, dict[str, object]]] = []

    def transcribe(
        self,
        audio_path: str,
        **kwargs: object,
    ) -> tuple[iter, SimpleNamespace]:
        self.calls.append((audio_path, kwargs))
        return iter(self._segments), self._info


def _factory_for(model: FakeModel, calls: list[tuple[str, str, str]]):
    def factory(name: str, device: str, compute_type: str) -> FakeModel:
        calls.append((name, device, compute_type))
        return model

    return factory


def _read_log_events(output_root: Path) -> list[dict[str, object]]:
    log_path = output_root / "DEV01" / "logs" / "asr_transcribe.jsonl"
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]


def test_transcribe_task_audio_writes_valid_timestamped_json(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [
            SimpleNamespace(start=0.2, end=2.0, text=" 第一段 "),
            SimpleNamespace(start=2.1, end=12.7, text="第二段"),
        ],
        SimpleNamespace(language="zh", language_probability=0.9876543),
    )
    factory_calls: list[tuple[str, str, str]] = []

    result = transcribe_task_audio(
        output_root,
        "DEV01",
        model_factory=_factory_for(fake_model, factory_calls),
    )

    assert factory_calls == [("small", "cpu", "int8")]
    assert fake_model.calls[0][1] == {"beam_size": 5, "vad_filter": True}
    assert result.language == "zh"
    assert result.segment_count == 2
    assert result.cache_hit is False
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert len(payload["cache_key"]) == 64
    assert payload["engine"]["model"] == "small"
    assert payload["segments"] == [
        {"id": 1, "start": 0.2, "end": 2.0, "text": "第一段"},
        {"id": 2, "start": 2.1, "end": 12.5, "text": "第二段"},
    ]
    assert not (output_root / "DEV01" / "asr" / "transcript.json.tmp").exists()
    assert [event["event"] for event in _read_log_events(output_root)] == [
        "started",
        "completed",
    ]


def test_transcribe_task_audio_uses_cache_before_loading_model(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [SimpleNamespace(start=0.2, end=2.0, text="缓存文本")],
        SimpleNamespace(language="zh", language_probability=0.9),
    )
    factory_calls: list[tuple[str, str, str]] = []
    factory = _factory_for(fake_model, factory_calls)
    first = transcribe_task_audio(output_root, "DEV01", model_factory=factory)
    original_bytes = first.output_path.read_bytes()
    original_mtime = first.output_path.stat().st_mtime_ns

    second = transcribe_task_audio(
        output_root,
        "DEV01",
        model_factory=lambda *_: pytest.fail("缓存命中时不应加载模型"),
    )

    assert second.cache_hit is True
    assert factory_calls == [("small", "cpu", "int8")]
    assert first.output_path.read_bytes() == original_bytes
    assert first.output_path.stat().st_mtime_ns == original_mtime
    assert [event["event"] for event in _read_log_events(output_root)] == [
        "started",
        "completed",
        "cache_hit",
    ]


def test_transcribe_task_audio_invalidates_cache_when_audio_changes(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [SimpleNamespace(start=0.2, end=2.0, text="文本")],
        SimpleNamespace(language="zh", language_probability=0.9),
    )
    factory_calls: list[tuple[str, str, str]] = []
    factory = _factory_for(fake_model, factory_calls)
    transcribe_task_audio(output_root, "DEV01", model_factory=factory)
    (output_root / "DEV01" / "audio" / "audio.wav").write_bytes(
        b"changed-audio-content"
    )

    result = transcribe_task_audio(output_root, "DEV01", model_factory=factory)

    assert result.cache_hit is False
    assert len(factory_calls) == 2


def test_transcribe_task_audio_invalidates_cache_when_parameters_change(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [SimpleNamespace(start=0.2, end=2.0, text="文本")],
        SimpleNamespace(language="zh", language_probability=0.9),
    )
    factory_calls: list[tuple[str, str, str]] = []
    factory = _factory_for(fake_model, factory_calls)
    transcribe_task_audio(output_root, "DEV01", model_factory=factory)

    result = transcribe_task_audio(
        output_root,
        "DEV01",
        model="base",
        model_factory=factory,
    )

    assert result.cache_hit is False
    assert factory_calls == [("small", "cpu", "int8"), ("base", "cpu", "int8")]


def test_transcribe_task_audio_replaces_damaged_cache(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [SimpleNamespace(start=0.2, end=2.0, text="文本")],
        SimpleNamespace(language="zh", language_probability=0.9),
    )
    factory_calls: list[tuple[str, str, str]] = []
    factory = _factory_for(fake_model, factory_calls)
    first = transcribe_task_audio(output_root, "DEV01", model_factory=factory)
    first.output_path.write_text("{broken-json", encoding="utf-8")

    second = transcribe_task_audio(output_root, "DEV01", model_factory=factory)

    assert second.cache_hit is False
    assert len(factory_calls) == 2
    assert json.loads(second.output_path.read_text(encoding="utf-8"))["segments"]


def test_transcribe_task_audio_rejects_invalid_cached_timestamps(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [SimpleNamespace(start=0.2, end=2.0, text="文本")],
        SimpleNamespace(language="zh", language_probability=0.9),
    )
    factory_calls: list[tuple[str, str, str]] = []
    factory = _factory_for(fake_model, factory_calls)
    first = transcribe_task_audio(output_root, "DEV01", model_factory=factory)
    payload = json.loads(first.output_path.read_text(encoding="utf-8"))
    payload["segments"][0]["end"] = 13.0
    first.output_path.write_text(json.dumps(payload), encoding="utf-8")

    second = transcribe_task_audio(output_root, "DEV01", model_factory=factory)

    assert second.cache_hit is False
    assert len(factory_calls) == 2


def test_transcribe_task_audio_rejects_empty_result(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [SimpleNamespace(start=0.0, end=1.0, text="   ")],
        SimpleNamespace(language="zh", language_probability=0.9),
    )

    with pytest.raises(ASRTranscriptionError, match="未生成任何非空文本片段"):
        transcribe_task_audio(
            output_root,
            "DEV01",
            model_factory=_factory_for(fake_model, []),
        )

    assert not (output_root / "DEV01" / "asr" / "transcript.json").exists()


def test_transcribe_task_audio_rejects_non_increasing_timestamps(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [
            SimpleNamespace(start=2.0, end=4.0, text="第一段"),
            SimpleNamespace(start=1.0, end=5.0, text="第二段"),
        ],
        SimpleNamespace(language="zh", language_probability=0.9),
    )

    with pytest.raises(ASRTranscriptionError, match="未按顺序递增"):
        transcribe_task_audio(
            output_root,
            "DEV01",
            model_factory=_factory_for(fake_model, []),
        )


def test_transcribe_task_audio_rejects_timestamp_beyond_duration(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    fake_model = FakeModel(
        [SimpleNamespace(start=10.0, end=13.1, text="越界")],
        SimpleNamespace(language="zh", language_probability=0.9),
    )

    with pytest.raises(ASRTranscriptionError, match="超过音频时长"):
        transcribe_task_audio(
            output_root,
            "DEV01",
            model_factory=_factory_for(fake_model, []),
        )


def test_transcribe_task_audio_preserves_previous_output_on_failure(
    tmp_path: Path,
) -> None:
    output_root = _prepare_task(tmp_path)
    output_path = output_root / "DEV01" / "asr" / "transcript.json"
    output_path.write_text("previous-result", encoding="utf-8")

    class FailingModel:
        def transcribe(self, *_: object, **__: object) -> None:
            raise RuntimeError("inference stopped")

    with pytest.raises(ASRTranscriptionError, match="ASR转写失败"):
        transcribe_task_audio(
            output_root,
            "DEV01",
            model_factory=lambda *_: FailingModel(),
        )

    assert output_path.read_text(encoding="utf-8") == "previous-result"
    assert not output_path.with_name("transcript.json.tmp").exists()
    assert [event["event"] for event in _read_log_events(output_root)] == [
        "started",
        "failed",
    ]
    assert "inference stopped" in str(_read_log_events(output_root)[-1]["error"])


def test_transcribe_task_audio_reports_model_load_failure(tmp_path: Path) -> None:
    output_root = _prepare_task(tmp_path)

    def fail_factory(*_: object) -> None:
        raise RuntimeError("model unavailable")

    with pytest.raises(ASRTranscriptionError, match="无法加载ASR模型"):
        transcribe_task_audio(output_root, "DEV01", model_factory=fail_factory)


def test_transcribe_task_audio_requires_extracted_audio(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    initialize_task_workspace(output_root, "DEV01", source_video=source)

    with pytest.raises(ASRTranscriptionError, match="ASR音频不存在"):
        transcribe_task_audio(output_root, "DEV01", model_factory=lambda *_: None)


def test_transcribe_cli_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "transcript.json"
    monkeypatch.setattr(
        cli,
        "transcribe_task_audio",
        lambda *args, **kwargs: ASRTranscriptionResult(
            output_path=output_path,
            language="zh",
            language_probability=0.98,
            segment_count=3,
            duration_seconds=12.5,
            cache_hit=False,
        ),
    )
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["video-doc", "transcribe", "DEV01", "--output-root", str(tmp_path)],
    )

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"segment_count": 3' in output
    assert '"cache_hit": false' in output
    assert "ASR转写完成" in output
