import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import video_doc.cli as cli
import video_doc.chapter_content as chapter_content_module
from video_doc.chapter_content import (
    CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
    CHAPTER_CONTENT_OUTPUT_LANGUAGE,
    CHAPTER_CONTENT_POLICY_VERSION,
    CHAPTER_CONTENT_PROMPT_VERSION,
    CHAPTER_CONTENT_VALIDATOR_VERSION,
    CHAPTER_INPUT_OWNERSHIP_POLICY,
    CHAPTER_WINDOWING_VERSION,
    ChapterContentError,
    generate_task_chapter_content,
    generate_task_chapter_content_single_window_mock,
    generate_task_chapter_content_windowed_mock,
    prepare_task_chapter_inputs,
    split_chapter_events,
    validate_chapter_content_bundle,
    validate_chapter_input_manifest,
)
from video_doc.llm_adapter import LLMConfig
from video_doc.task_workspace import initialize_task_workspace


def _real_config(*, model: str = "qwen3.7-flash") -> LLMConfig:
    return LLMConfig(
        backend="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        model=model,
        timeout_seconds=60.0,
        api_key="p9-5-test-secret",
        mock_response={},
        enable_thinking=False,
        temperature=0.0,
    )


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timeline() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "duration_seconds": 180.0,
        "language": {"code": "zh", "probability": 0.99},
        "source_asr": {
            "schema_version": "1.0",
            "cache_key": "a" * 64,
            "sha256": "b" * 64,
        },
        "events": [
            {
                "id": "a0001",
                "type": "asr",
                "source_segment_id": 1,
                "start": 5.0,
                "end": 70.0,
                "pause_before_seconds": 5.0,
                "text": "第一章开始，片段跨过六十秒边界",
            },
            {
                "id": "a0002",
                "type": "asr",
                "source_segment_id": 2,
                "start": 60.0,
                "end": 80.0,
                "pause_before_seconds": 0.0,
                "text": "第二章从边界精确开始",
            },
            {
                "id": "a0003",
                "type": "asr",
                "source_segment_id": 3,
                "start": 120.0,
                "end": 130.0,
                "pause_before_seconds": 40.0,
                "text": "第三章开始",
            },
            {
                "id": "a0004",
                "type": "asr",
                "source_segment_id": 4,
                "start": 160.0,
                "end": 170.0,
                "pause_before_seconds": 30.0,
                "text": "第三章结束",
            },
        ],
    }


def _plan(timeline: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": "DEV01",
        "strategy": "llm",
        "source_timeline": {
            "schema_version": "1.0",
            "sha256": _canonical_sha256(timeline),
            "event_count": 4,
        },
        "duration_seconds": 180.0,
        "parameters": {
            "prompt_version": "chapter-boundary-event-id-stability-2.0",
            "min_chapter_seconds": 60.0,
            "boundary_alignment": "selected_asr_event_start",
            "formal_policy_version": "stage8-frozen-a-primary-b-fallback-1.0",
            "formal_result_source": "primary",
        },
        "boundaries": [
            {"timestamp": 0.0, "title": "第一章", "source_event_id": None},
            {"timestamp": 60.0, "title": "第二章", "source_event_id": "a0002"},
            {"timestamp": 120.0, "title": "第三章", "source_event_id": "a0003"},
        ],
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_formal_task(
    tmp_path: Path,
    *,
    result_source: str = "primary",
    quality_level: str = "normal",
) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    output_root = tmp_path / "outputs"
    workspace = initialize_task_workspace(output_root, "DEV01", source_video=source)
    timeline = _timeline()
    plan = _plan(timeline)
    plan["parameters"]["formal_result_source"] = result_source
    plan_sha = _canonical_sha256(plan)
    result_id = f"{result_source}-{plan_sha[:12]}"
    report = {
        "schema_version": "1.0",
        "formal_policy_version": "stage8-frozen-a-primary-b-fallback-1.0",
        "task_id": "DEV01",
        "status": "success",
        "result_source": result_source,
        "quality_level": quality_level,
        "business_call_count": 1,
        "supplier_request_count": 1,
        "primary_strategy": "direct-event-id-v2",
        "fallback_strategy": "two-stage-stability-1.0",
        "emergency_strategy": "local-emergency-single-chapter-1.0",
        "errors": [],
        "final_plan_sha256": plan_sha,
        "result_id": result_id,
    }
    _write_json(workspace.root / "timeline" / "timeline.json", timeline)
    _write_json(workspace.root / "boundaries" / "llm.json", plan)
    _write_json(
        workspace.root / "boundaries" / "llm-execution-report.json", report
    )
    group = workspace.root / "boundaries" / "formal-results" / result_id
    group_plan = group / "chapter-plan.json"
    group_report = group / "execution-report.json"
    _write_json(group_plan, plan)
    _write_json(group_report, report)
    _write_json(
        group / "manifest.json",
        {
            "schema_version": "1.0",
            "formal_policy_version": "stage8-frozen-a-primary-b-fallback-1.0",
            "result_id": result_id,
            "task_id": "DEV01",
            "files": {
                "chapter-plan.json": hashlib.sha256(group_plan.read_bytes()).hexdigest(),
                "execution-report.json": hashlib.sha256(
                    group_report.read_bytes()
                ).hexdigest(),
            },
        },
    )
    return output_root


def _bundle(manifest: dict[str, Any]) -> dict[str, Any]:
    chapters = []
    for expected in manifest["chapters"]:
        ids = expected["event_ids"]
        chapters.append(
            {
                "chapter_id": expected["chapter_id"],
                "index": expected["index"],
                "start": expected["start"],
                "end": expected["end"],
                "planned_title": expected["planned_title"],
                "title": {"text": "章节标题", "source_event_ids": [ids[0]]},
                "summary": {"text": "章节摘要", "source_event_ids": ids},
                "key_points": [
                    {"text": "知识点一", "source_event_ids": [ids[0]]},
                    {"text": "知识点二", "source_event_ids": [ids[-1]]},
                    {"text": "知识点三", "source_event_ids": ids},
                ],
                "keywords": [
                    {"text": "关键词一", "source_event_ids": [ids[0]]},
                    {"text": "关键词二", "source_event_ids": [ids[-1]]},
                    {"text": "关键词三", "source_event_ids": ids},
                ],
                "source_event_ids": ids,
                "window_count": 1,
            }
        )
    return {
        "schema_version": "1.0",
        "task_id": manifest["task_id"],
        "strategy": manifest["strategy"],
        "source_chapter_plan": manifest["source_chapter_plan"],
        "source_timeline": manifest["source_timeline"],
        "chaptering_provenance": manifest["chaptering_provenance"],
        "generation": {
            "status": "success",
            "output_language": CHAPTER_CONTENT_OUTPUT_LANGUAGE,
            "prompt_version": CHAPTER_CONTENT_PROMPT_VERSION,
            "merge_prompt_version": CHAPTER_CONTENT_MERGE_PROMPT_VERSION,
            "content_policy_version": CHAPTER_CONTENT_POLICY_VERSION,
            "windowing_version": CHAPTER_WINDOWING_VERSION,
            "validator_version": CHAPTER_CONTENT_VALIDATOR_VERSION,
        },
        "chapters": chapters,
    }


def _mock_config() -> LLMConfig:
    return LLMConfig(
        backend="mock",
        base_url="",
        model="stage9-test-mock",
        timeout_seconds=60.0,
        api_key="",
        mock_response={"mode": "stage9-test"},
        enable_thinking=None,
        temperature=0.0,
    )


def _window_response(ids: list[str]) -> dict[str, Any]:
    return {
        "title": {"text": "标题", "source_event_ids": [ids[0]]},
        "summary": {"text": "摘要", "source_event_ids": ids},
        "key_points": [
            {"text": "知识点一", "source_event_ids": [ids[0]]},
            {"text": "知识点二", "source_event_ids": [ids[-1]]},
            {"text": "知识点三", "source_event_ids": ids},
        ],
        "keywords": [
            {"text": "关键词一", "source_event_ids": [ids[0]]},
            {"text": "关键词二", "source_event_ids": [ids[-1]]},
            {"text": "关键词三", "source_event_ids": ids},
        ],
    }


def test_validator_v1_2_sorts_only_valid_unique_chapter_ids() -> None:
    response = _window_response(["a0001", "a0002", "a0003"])
    response["summary"]["source_event_ids"] = ["a0003", "a0001", "a0002"]
    response["keywords"][1]["source_event_ids"] = ["a0003", "a0001"]
    original_text = response["keywords"][1]["text"]

    normalized = chapter_content_module._normalize_window_response(
        response,
        allowed_ids=["a0001", "a0002", "a0003"],
    )

    assert normalized["summary"]["source_event_ids"] == [
        "a0001",
        "a0002",
        "a0003",
    ]
    assert normalized["keywords"][1]["source_event_ids"] == ["a0001", "a0003"]
    assert normalized["keywords"][1]["text"] == original_text
    assert normalized["source_event_ids"] == ["a0001", "a0002", "a0003"]
    assert response["summary"]["source_event_ids"] == ["a0003", "a0001", "a0002"]


def test_validator_v1_2_still_rejects_duplicate_ids() -> None:
    response = _window_response(["a0001", "a0002", "a0003"])
    response["keywords"][1]["source_event_ids"] = ["a0002", "a0002"]

    with pytest.raises(ChapterContentError, match="重复引用"):
        chapter_content_module._normalize_window_response(
            response,
            allowed_ids=["a0001", "a0002", "a0003"],
        )


def test_validator_v1_2_still_rejects_unknown_or_cross_chapter_ids() -> None:
    response = _window_response(["a0001", "a0002", "a0003"])
    response["keywords"][1]["source_event_ids"] = ["a9999"]

    with pytest.raises(ChapterContentError, match="章外或未知"):
        chapter_content_module._normalize_window_response(
            response,
            allowed_ids=["a0001", "a0002", "a0003"],
        )


def test_r3_zh_cn_versions_and_prompt_contract() -> None:
    chapter = {"chapter_id": "c0001", "planned_title": "English source title"}
    events = [{"id": "a0001", "start": 0.0, "end": 1.0, "text": "English ASR"}]

    window_messages = chapter_content_module._build_window_messages(chapter, events)
    merge_messages = chapter_content_module._build_merge_messages(
        chapter,
        [
            {
                "window_id": "c0001-w001",
                "title": {"text": "中文标题", "source_event_ids": ["a0001"]},
                "summary": {"text": "中文摘要", "source_event_ids": ["a0001"]},
                "key_points": [],
                "keywords": [],
                "source_event_ids": ["a0001"],
            }
        ],
    )

    assert CHAPTER_CONTENT_OUTPUT_LANGUAGE == "zh-CN"
    assert CHAPTER_CONTENT_PROMPT_VERSION == "chapter-content-window-1.1"
    assert CHAPTER_CONTENT_MERGE_PROMPT_VERSION == "chapter-content-merge-1.1"
    assert CHAPTER_CONTENT_POLICY_VERSION == "asr-evidence-only-zh-cn-1.1"
    assert CHAPTER_CONTENT_VALIDATOR_VERSION == "chapter-content-validator-1.2"
    assert "固定使用简体中文" in window_messages[1]["content"]
    assert "输入ASR或planned_title的语言不得改变输出语言" in window_messages[1]["content"]
    assert "固定使用简体中文" in merge_messages[1]["content"]
    assert "各窗口或planned_title的语言不得改变输出语言" in merge_messages[1]["content"]
    assert (
        chapter_content_module.LEGACY_CHAPTER_CONTENT_VALIDATOR_VERSION
        != CHAPTER_CONTENT_VALIDATOR_VERSION
    )


@pytest.mark.parametrize(
    ("field", "expected_error"),
    [
        ("title", "title.text必须使用简体中文"),
        ("summary", "summary.text必须使用简体中文"),
        ("key_points", r"key_points\[1\].text必须使用简体中文"),
    ],
)
def test_validator_v1_2_rejects_non_chinese_core_text(
    field: str,
    expected_error: str,
) -> None:
    response = _window_response(["a0001", "a0002", "a0003"])
    if field == "key_points":
        response[field][0]["text"] = "English key point"
    else:
        response[field]["text"] = "English text"

    with pytest.raises(ChapterContentError, match=expected_error):
        chapter_content_module._normalize_window_response(
            response,
            allowed_ids=["a0001", "a0002", "a0003"],
        )


def test_validator_v1_2_allows_ascii_proper_noun_keyword() -> None:
    response = _window_response(["a0001", "a0002", "a0003"])
    response["keywords"][0]["text"] = "Viktor Frankl"

    normalized = chapter_content_module._normalize_window_response(
        response,
        allowed_ids=["a0001", "a0002", "a0003"],
    )

    assert normalized["keywords"][0]["text"] == "Viktor Frankl"


def test_validator_v1_2_rejects_english_sentence_as_keyword() -> None:
    response = _window_response(["a0001", "a0002", "a0003"])
    response["keywords"][0]["text"] = "This is a complete English keyword sentence"

    with pytest.raises(ChapterContentError, match=r"keywords\[1\].text必须使用简体中文"):
        chapter_content_module._normalize_window_response(
            response,
            allowed_ids=["a0001", "a0002", "a0003"],
        )


def test_legacy_validator_replays_pre_r3_english_content() -> None:
    response = {
        "title": {"text": "English title", "source_event_ids": ["a0001"]},
        "summary": {"text": "English summary", "source_event_ids": ["a0001"]},
        "key_points": [
            {"text": "English point one", "source_event_ids": ["a0001"]},
            {"text": "English point two", "source_event_ids": ["a0001"]},
            {"text": "English point three", "source_event_ids": ["a0001"]},
        ],
        "keywords": [
            {"text": "keyword one", "source_event_ids": ["a0001"]},
            {"text": "keyword two", "source_event_ids": ["a0001"]},
            {"text": "keyword three", "source_event_ids": ["a0001"]},
        ],
    }

    normalized = chapter_content_module._normalize_window_response_legacy(
        response,
        allowed_ids=["a0001"],
    )

    assert normalized["title"]["text"] == "English title"


class SequenceTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[Any] = []

    def complete(self, config: object, messages: object, response_format: object) -> Any:
        self.calls.append((config, messages, response_format))
        if not self.responses:
            raise AssertionError("缓存命中时不应访问transport")
        return self.responses.pop(0)


def test_prepare_inputs_assigns_overlapping_asr_by_start_exactly_once(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)

    result = prepare_task_chapter_inputs(output_root, "DEV01")

    assert result.chapter_count == 3
    assert result.event_count == 4
    assert result.result_source == "primary"
    assert result.quality_level == "normal"
    assert result.cache_hit is False
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["ownership"] == {
        "policy_version": CHAPTER_INPUT_OWNERSHIP_POLICY,
        "rule": "chapter.start <= event.start < chapter.end",
        "assigned_event_count": 4,
        "duplicate_event_count": 0,
        "unassigned_event_count": 0,
    }
    assert [(chapter["start"], chapter["end"]) for chapter in manifest["chapters"]] == [
        (0.0, 60.0),
        (60.0, 120.0),
        (120.0, 180.0),
    ]
    assert [chapter["event_ids"] for chapter in manifest["chapters"]] == [
        ["a0001"],
        ["a0002"],
        ["a0003", "a0004"],
    ]
    all_ids = [
        event_id
        for chapter in manifest["chapters"]
        for event_id in chapter["event_ids"]
    ]
    assert all_ids == ["a0001", "a0002", "a0003", "a0004"]
    assert len(all_ids) == len(set(all_ids))


def test_prepare_inputs_uses_cache_without_rewriting(tmp_path: Path) -> None:
    output_root = _prepare_formal_task(tmp_path)
    first = prepare_task_chapter_inputs(output_root, "DEV01")
    before = first.manifest_path.read_bytes()
    mtime = first.manifest_path.stat().st_mtime_ns

    second = prepare_task_chapter_inputs(output_root, "DEV01")

    assert second.cache_hit is True
    assert second.manifest_path.read_bytes() == before
    assert second.manifest_path.stat().st_mtime_ns == mtime


@pytest.mark.parametrize(
    ("result_source", "quality_level"),
    [("primary", "normal"), ("fallback", "unknown"), ("emergency", "degraded")],
)
def test_prepare_inputs_inherits_stage8_provenance(
    tmp_path: Path,
    result_source: str,
    quality_level: str,
) -> None:
    output_root = _prepare_formal_task(
        tmp_path, result_source=result_source, quality_level=quality_level
    )

    result = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert manifest["chaptering_provenance"]["result_source"] == result_source
    assert manifest["chaptering_provenance"]["quality_level"] == quality_level


def test_prepare_inputs_rejects_damaged_formal_group_hash(tmp_path: Path) -> None:
    output_root = _prepare_formal_task(tmp_path)
    report = json.loads(
        (output_root / "DEV01" / "boundaries" / "llm-execution-report.json").read_text(
            encoding="utf-8"
        )
    )
    group_plan = (
        output_root
        / "DEV01"
        / "boundaries"
        / "formal-results"
        / report["result_id"]
        / "chapter-plan.json"
    )
    group_plan.write_text("{}", encoding="utf-8")

    with pytest.raises(ChapterContentError, match="文件哈希不一致"):
        prepare_task_chapter_inputs(output_root, "DEV01")

    assert not (output_root / "DEV01" / "content").exists()


def test_prepare_inputs_rejects_mixed_top_level_report(tmp_path: Path) -> None:
    output_root = _prepare_formal_task(tmp_path)
    report_path = output_root / "DEV01" / "boundaries" / "llm-execution-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["quality_level"] = "unknown"
    _write_json(report_path, report)

    with pytest.raises(ChapterContentError, match="来源与质量级别"):
        prepare_task_chapter_inputs(output_root, "DEV01")


def test_validate_content_bundle_accepts_only_chapter_local_evidence(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    result = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    bundle = _bundle(manifest)

    validate_chapter_content_bundle(bundle, input_manifest=manifest)

    bundle["chapters"][0]["summary"]["source_event_ids"] = ["a0002"]
    with pytest.raises(ChapterContentError, match="章外或未知"):
        validate_chapter_content_bundle(bundle, input_manifest=manifest)


def test_validate_content_bundle_v1_2_requires_zh_cn_generation_metadata(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    result = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    bundle = _bundle(manifest)
    bundle["generation"].pop("output_language")

    with pytest.raises(ChapterContentError, match="output_language必须为zh-CN"):
        validate_chapter_content_bundle(bundle, input_manifest=manifest)


def test_validate_content_bundle_rejects_wrong_reference_union(tmp_path: Path) -> None:
    output_root = _prepare_formal_task(tmp_path)
    result = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    bundle = _bundle(manifest)
    bundle["chapters"][2]["source_event_ids"] = ["a0003"]

    with pytest.raises(ChapterContentError, match="有序去重并集"):
        validate_chapter_content_bundle(bundle, input_manifest=manifest)


def test_validate_input_manifest_rejects_duplicate_event_assignment(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    result = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["chapters"][1]["event_ids"] = ["a0001"]
    manifest["chapters"][1]["first_event_id"] = "a0001"
    manifest["chapters"][1]["last_event_id"] = "a0001"
    manifest["chapters"][1]["event_count"] = 1
    without_key = dict(manifest)
    without_key.pop("cache_key")
    manifest["cache_key"] = _canonical_sha256(without_key)

    with pytest.raises(ChapterContentError, match="遗漏或重复"):
        validate_chapter_input_manifest(manifest)


def test_validate_input_manifest_rejects_empty_chapter(tmp_path: Path) -> None:
    output_root = _prepare_formal_task(tmp_path)
    result = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["chapters"][1]["event_ids"] = []
    manifest["chapters"][1]["event_count"] = 0
    manifest["chapters"][1]["first_event_id"] = None
    manifest["chapters"][1]["last_event_id"] = None
    without_key = dict(manifest)
    without_key.pop("cache_key")
    manifest["cache_key"] = _canonical_sha256(without_key)

    with pytest.raises(ChapterContentError, match="事件ID数组无效"):
        validate_chapter_input_manifest(manifest)


def test_prepare_chapter_inputs_cli_reports_offline_manifest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "video-doc",
            "prepare-chapter-inputs",
            "DEV01",
            "--output-root",
            str(output_root),
        ],
    )

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"chapter_count": 3' in output
    assert '"event_count": 4' in output
    assert '"result_source": "primary"' in output
    assert "章节输入准备完成" in output


def test_single_window_mock_generates_isolated_normalized_preview(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    transport = SequenceTransport(
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003", "a0004"]),
    )

    result = generate_task_chapter_content_single_window_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=transport,
    )

    assert result.chapter_count == 3
    assert result.supplier_request_count == 3
    assert result.cache_hit_count == 0
    assert result.all_cache_hit is False
    preview = json.loads(result.preview_path.read_text(encoding="utf-8"))
    assert preview["generation"]["mode"] == "mock"
    assert preview["generation"]["output_language"] == "zh-CN"
    assert preview["generation"]["prompt_version"] == CHAPTER_CONTENT_PROMPT_VERSION
    assert preview["generation"]["content_policy_version"] == CHAPTER_CONTENT_POLICY_VERSION
    assert preview["generation"]["validator_version"] == CHAPTER_CONTENT_VALIDATOR_VERSION
    assert [chapter["source_event_ids"] for chapter in preview["chapters"]] == [
        ["a0001"],
        ["a0002"],
        ["a0003", "a0004"],
    ]
    expected_ids = [["a0001"], ["a0002"], ["a0003", "a0004"]]
    for call, allowed in zip(transport.calls, expected_ids):
        messages = call[1]
        serialized = messages[-1]["content"].split("CHAPTER_INPUT_JSON=", 1)[1]
        chapter_input = json.loads(serialized)
        assert [event["id"] for event in chapter_input["events"]] == allowed
        assert not (set(expected_ids[0] + expected_ids[1] + expected_ids[2]) - set(allowed)) & {
            event["id"] for event in chapter_input["events"]
        }
    first_window = json.loads(
        (
            result.attempt_path
            / "windows"
            / "c0001"
            / "c0001-w001.json"
        ).read_text(encoding="utf-8")
    )
    assert "raw_response" in first_window
    assert first_window["raw_response"] == _window_response(["a0001"])
    assert first_window["response"]["source_event_ids"] == ["a0001"]


def test_single_window_mock_corrects_invalid_cross_chapter_reference_once(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    invalid = _window_response(["a0001"])
    invalid["summary"]["source_event_ids"] = ["a0002"]
    transport = SequenceTransport(
        invalid,
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003", "a0004"]),
    )

    result = generate_task_chapter_content_single_window_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=transport,
    )

    assert result.supplier_request_count == 4
    assert len(transport.calls) == 4
    retry_messages = transport.calls[1][1]
    assert any("章外或未知" in message["content"] for message in retry_messages)
    audit = json.loads(
        (
            result.attempt_path
            / "windows"
            / "c0001"
            / "c0001-w001-validation-audit.json"
        ).read_text(encoding="utf-8")
    )
    assert audit["status"] == "completed_after_retry"
    assert len(audit["attempts"]) == 1


def test_single_window_mock_second_invalid_response_fails_without_preview(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    invalid = _window_response(["a0001"])
    invalid["summary"]["source_event_ids"] = ["a0002"]
    transport = SequenceTransport(invalid, invalid)

    with pytest.raises(ChapterContentError, match="章外或未知"):
        generate_task_chapter_content_single_window_mock(
            output_root,
            "DEV01",
            config=_mock_config(),
            transport=transport,
        )

    attempt_reports = list(
        (output_root / "DEV01" / "content" / "attempts").glob(
            "single-window-*/attempt-report.json"
        )
    )
    assert len(attempt_reports) == 1
    report = json.loads(attempt_reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["failed_chapter_id"] == "c0001"
    assert report["supplier_request_count"] == 2
    assert not (attempt_reports[0].parent / "single-window-preview.json").exists()


def test_single_window_mock_cache_avoids_all_transport_calls(tmp_path: Path) -> None:
    output_root = _prepare_formal_task(tmp_path)
    first = generate_task_chapter_content_single_window_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(
            _window_response(["a0001"]),
            _window_response(["a0002"]),
            _window_response(["a0003", "a0004"]),
        ),
    )
    preview_hash = hashlib.sha256(first.preview_path.read_bytes()).hexdigest()
    preview_mtime = first.preview_path.stat().st_mtime_ns

    second = generate_task_chapter_content_single_window_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(),
    )

    assert second.supplier_request_count == 0
    assert second.cache_hit_count == 3
    assert second.all_cache_hit is True
    assert hashlib.sha256(second.preview_path.read_bytes()).hexdigest() == preview_hash
    assert second.preview_path.stat().st_mtime_ns == preview_mtime


@pytest.mark.parametrize(
    ("constant_name", "old_value"),
    [
        ("CHAPTER_CONTENT_OUTPUT_LANGUAGE", "en"),
        ("CHAPTER_CONTENT_PROMPT_VERSION", "chapter-content-window-1.0"),
        ("CHAPTER_CONTENT_POLICY_VERSION", "asr-evidence-only-1.0"),
        ("CHAPTER_CONTENT_VALIDATOR_VERSION", "chapter-content-validator-1.1"),
    ],
)
def test_r3_language_and_version_changes_cannot_hit_current_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    constant_name: str,
    old_value: str,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    responses = (
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003", "a0004"]),
    )
    current = generate_task_chapter_content_single_window_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(*responses),
    )
    monkeypatch.setattr(chapter_content_module, constant_name, old_value)

    drifted = generate_task_chapter_content_single_window_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(*responses),
    )

    assert drifted.attempt_path != current.attempt_path
    assert drifted.supplier_request_count == 3
    assert drifted.cache_hit_count == 0


def test_single_window_p9_2_rejects_non_mock_backend_before_generation(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    real_config = LLMConfig(
        backend="openai_compatible",
        base_url="https://example.invalid",
        model="forbidden",
        timeout_seconds=60.0,
        api_key="secret",
        mock_response={},
    )

    with pytest.raises(ChapterContentError, match="只允许mock backend"):
        generate_task_chapter_content_single_window_mock(
            output_root,
            "DEV01",
            config=real_config,
            transport=SequenceTransport(),
        )

    assert not (output_root / "DEV01" / "content").exists()


def test_generate_chapter_content_mock_cli_is_offline_and_cacheable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "video-doc",
            "generate-chapter-content-mock",
            "DEV01",
            "--output-root",
            str(output_root),
        ],
    )

    assert cli.main() == 0
    first_output = capsys.readouterr().out
    assert '"mode": "mock"' in first_output
    assert '"supplier_request_count": 3' in first_output
    assert "单窗mock内容生成完成" in first_output

    assert cli.main() == 0
    second_output = capsys.readouterr().out
    assert '"supplier_request_count": 0' in second_output
    assert '"cache_hit_count": 3' in second_output
    assert '"all_cache_hit": true' in second_output


def test_split_chapter_events_is_deterministic_without_overlap_or_omission() -> None:
    events = [
        {"id": f"a{index:04d}", "start": float(index), "end": index + 0.5, "text": str(index)}
        for index in range(1, 5)
    ]

    first = split_chapter_events(events, max_window_events=2, max_window_chars=1000)
    second = split_chapter_events(events, max_window_events=2, max_window_chars=1000)

    assert first == second
    assert [window["event_ids"] for window in first] == [
        ["a0001", "a0002"],
        ["a0003", "a0004"],
    ]
    flattened = [event_id for window in first for event_id in window["event_ids"]]
    assert flattened == ["a0001", "a0002", "a0003", "a0004"]
    assert len(flattened) == len(set(flattened))


def test_split_chapter_events_keeps_single_oversize_event_whole() -> None:
    event = {"id": "a0001", "start": 1.0, "end": 2.0, "text": "很长" * 100}

    windows = split_chapter_events(
        [event], max_window_events=1, max_window_chars=10
    )

    assert len(windows) == 1
    assert windows[0]["event_ids"] == ["a0001"]
    assert windows[0]["oversize_single_event"] is True


def test_windowed_mock_merges_only_normalized_results_and_publishes_atomic_group(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    transport = SequenceTransport(
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003"]),
        _window_response(["a0004"]),
        _window_response(["a0003", "a0004"]),
    )

    result = generate_task_chapter_content_windowed_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=transport,
        max_window_events=1,
        max_window_chars=10000,
    )

    assert result.chapter_count == 3
    assert result.window_count == 4
    assert result.merge_count == 1
    assert result.supplier_request_count == 5
    assert result.cache_hit_count == 0
    assert result.result_group_cache_hit is False
    content = json.loads(result.content_path.read_text(encoding="utf-8"))
    assert [chapter["window_count"] for chapter in content["chapters"]] == [1, 1, 2]
    assert content["chapters"][2]["source_event_ids"] == ["a0003", "a0004"]
    merge_messages = transport.calls[-1][1]
    merge_json = merge_messages[-1]["content"].split(
        "CHAPTER_MERGE_INPUT_JSON=", 1
    )[1]
    merge_input = json.loads(merge_json)
    assert set(merge_input) == {"chapter_id", "planned_title", "windows"}
    assert all("events" not in window for window in merge_input["windows"])
    assert [window["source_event_ids"] for window in merge_input["windows"]] == [
        ["a0003"],
        ["a0004"],
    ]
    manifest = json.loads(
        (result.result_group_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert {path.name for path in result.result_group_path.iterdir()} == {
        "content.json",
        "execution-report.json",
        "manifest.json",
    }
    for filename, expected_hash in manifest["files"].items():
        assert hashlib.sha256(
            (result.result_group_path / filename).read_bytes()
        ).hexdigest() == expected_hash


def test_windowed_mock_merge_correction_requires_evidence_from_every_window(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    invalid_merge = _window_response(["a0003"])
    transport = SequenceTransport(
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003"]),
        _window_response(["a0004"]),
        invalid_merge,
        _window_response(["a0003", "a0004"]),
    )

    result = generate_task_chapter_content_windowed_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=transport,
        max_window_events=1,
        max_window_chars=10000,
    )

    assert result.supplier_request_count == 6
    audit = json.loads(
        (
            result.attempt_path
            / "merges"
            / "c0003-merge-validation-audit.json"
        ).read_text(encoding="utf-8")
    )
    assert audit["status"] == "completed_after_retry"
    assert "第2个窗口" in audit["attempts"][0]["error"]


def test_windowed_mock_partial_window_failure_does_not_publish_result_group(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    invalid = _window_response(["a0004"])
    invalid["summary"]["source_event_ids"] = ["a0003"]
    transport = SequenceTransport(
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003"]),
        invalid,
        invalid,
    )

    with pytest.raises(ChapterContentError, match="章外或未知"):
        generate_task_chapter_content_windowed_mock(
            output_root,
            "DEV01",
            config=_mock_config(),
            transport=transport,
            max_window_events=1,
            max_window_chars=10000,
        )

    assert not (output_root / "DEV01" / "content" / "mock-results").exists()
    reports = list(
        (output_root / "DEV01" / "content" / "attempts").glob(
            "windowed-*/attempt-report.json"
        )
    )
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["failed_call_id"] == "c0003-w002"
    assert len(report["completed_calls"]) == 3


def test_windowed_mock_merge_failure_does_not_publish_result_group(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    invalid_merge = _window_response(["a0003"])
    transport = SequenceTransport(
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003"]),
        _window_response(["a0004"]),
        invalid_merge,
        invalid_merge,
    )

    with pytest.raises(ChapterContentError, match="第2个窗口"):
        generate_task_chapter_content_windowed_mock(
            output_root,
            "DEV01",
            config=_mock_config(),
            transport=transport,
            max_window_events=1,
            max_window_chars=10000,
        )

    assert not (output_root / "DEV01" / "content" / "mock-results").exists()


def test_windowed_mock_reuses_all_call_and_result_group_caches(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    first = generate_task_chapter_content_windowed_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(
            _window_response(["a0001"]),
            _window_response(["a0002"]),
            _window_response(["a0003"]),
            _window_response(["a0004"]),
            _window_response(["a0003", "a0004"]),
        ),
        max_window_events=1,
        max_window_chars=10000,
    )
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.result_group_path.iterdir()
    }
    mtimes = {path.name: path.stat().st_mtime_ns for path in first.result_group_path.iterdir()}

    second = generate_task_chapter_content_windowed_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(),
        max_window_events=1,
        max_window_chars=10000,
    )

    assert second.supplier_request_count == 0
    assert second.cache_hit_count == 5
    assert second.all_cache_hit is True
    assert second.result_group_cache_hit is True
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.result_group_path.iterdir()
    } == hashes
    assert {
        path.name: path.stat().st_mtime_ns for path in second.result_group_path.iterdir()
    } == mtimes


def test_windowed_mock_rejects_damaged_existing_result_group(tmp_path: Path) -> None:
    output_root = _prepare_formal_task(tmp_path)
    first = generate_task_chapter_content_windowed_mock(
        output_root,
        "DEV01",
        config=_mock_config(),
        transport=SequenceTransport(
            _window_response(["a0001"]),
            _window_response(["a0002"]),
            _window_response(["a0003"]),
            _window_response(["a0004"]),
            _window_response(["a0003", "a0004"]),
        ),
        max_window_events=1,
        max_window_chars=10000,
    )
    first.content_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ChapterContentError, match="不完整、损坏或发生内容混用"):
        generate_task_chapter_content_windowed_mock(
            output_root,
            "DEV01",
            config=_mock_config(),
            transport=SequenceTransport(),
            max_window_events=1,
            max_window_chars=10000,
        )

    assert first.content_path.read_text(encoding="utf-8") == "{}"


def test_generate_windowed_mock_cli_publishes_nonformal_mock_group(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "video-doc",
            "generate-chapter-content-windowed-mock",
            "DEV01",
            "--output-root",
            str(output_root),
        ],
    )

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"mode": "mock-windowed"' in output
    assert '"chapter_count": 3' in output
    assert '"window_count": 3' in output
    assert '"merge_count": 0' in output
    assert not (output_root / "DEV01" / "content" / "content.json").exists()


def test_real_windowed_path_reuses_pipeline_and_publishes_formal_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    prepared = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        chapter_content_module,
        "P9_5_STAGE8_RESULT_ID",
        manifest["source_chapter_plan"]["result_id"],
    )
    transport = SequenceTransport(
        _window_response(["a0001"]),
        _window_response(["a0002"]),
        _window_response(["a0003", "a0004"]),
    )

    result = generate_task_chapter_content(
        output_root,
        "DEV01",
        config=_real_config(),
        transport=transport,
    )

    assert result.chapter_count == 3
    assert result.window_count == 3
    assert result.merge_count == 0
    assert result.supplier_request_count == 3
    assert result.result_group_path.parent.name == "formal-results"
    bundle = json.loads(result.content_path.read_text(encoding="utf-8"))
    assert bundle["generation"]["mode"] == "real-windowed"
    assert bundle["chaptering_provenance"] == manifest["chaptering_provenance"]
    assert json.loads(
        (output_root / "DEV01" / "content" / "content.json").read_text(
            encoding="utf-8"
        )
    ) == bundle
    assert (
        output_root / "DEV01" / "content" / "content-execution-report.json"
    ).is_file()
    all_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (output_root / "DEV01" / "content").rglob("*.json")
    )
    assert "p9-5-test-secret" not in all_text


def test_real_windowed_path_second_run_is_zero_transport_and_preserves_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    prepared = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        chapter_content_module,
        "P9_5_STAGE8_RESULT_ID",
        manifest["source_chapter_plan"]["result_id"],
    )
    first = generate_task_chapter_content(
        output_root,
        "DEV01",
        config=_real_config(),
        transport=SequenceTransport(
            _window_response(["a0001"]),
            _window_response(["a0002"]),
            _window_response(["a0003", "a0004"]),
        ),
    )
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.result_group_path.iterdir()
    }
    mtimes = {
        path.name: path.stat().st_mtime_ns
        for path in first.result_group_path.iterdir()
    }

    second = generate_task_chapter_content(
        output_root,
        "DEV01",
        config=_real_config(),
        transport=SequenceTransport(),
    )

    assert second.supplier_request_count == 0
    assert second.cache_hit_count == 3
    assert second.all_cache_hit is True
    assert second.result_group_cache_hit is True
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.result_group_path.iterdir()
    } == hashes
    assert {
        path.name: path.stat().st_mtime_ns
        for path in second.result_group_path.iterdir()
    } == mtimes


def test_real_windowed_path_failure_does_not_publish_formal_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    prepared = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        chapter_content_module,
        "P9_5_STAGE8_RESULT_ID",
        manifest["source_chapter_plan"]["result_id"],
    )
    invalid = _window_response(["a0001"])
    invalid["summary"]["source_event_ids"] = ["a0002"]

    with pytest.raises(ChapterContentError, match="章外或未知"):
        generate_task_chapter_content(
            output_root,
            "DEV01",
            config=_real_config(),
            transport=SequenceTransport(invalid, invalid),
        )

    content_root = output_root / "DEV01" / "content"
    assert not (content_root / "formal-results").exists()
    assert not (content_root / "content.json").exists()
    assert not (content_root / "content-execution-report.json").exists()


def test_real_windowed_path_rejects_public_config_drift_before_transport(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)

    with pytest.raises(ChapterContentError, match="公开配置"):
        generate_task_chapter_content(
            output_root,
            "DEV01",
            config=_real_config(model="changed-model"),
            transport=SequenceTransport(),
        )

    assert not (output_root / "DEV01" / "content").exists()


def _prepare_reconciliation_attempt(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    task_root = tmp_path / "outputs" / "DEV01"
    attempt_id = "windowed-real-test"
    attempt = task_root / "content" / "attempts" / attempt_id
    call_path = attempt / "windows" / "c0001" / "c0001-w001.json"
    audit_path = call_path.with_name("c0001-w001-validation-audit.json")
    failed_audit = (
        attempt / "windows" / "c0002" / "c0002-w001-validation-audit.json"
    )
    call_path.parent.mkdir(parents=True)
    failed_audit.parent.mkdir(parents=True)
    raw = _window_response(["a0001", "a0002", "a0003"])
    raw["summary"]["source_event_ids"] = ["a0003", "a0001"]
    normalized = chapter_content_module._normalize_window_response(
        raw, allowed_ids=["a0001", "a0002", "a0003"]
    )
    provider = {"backend": "openai_compatible", "model": "test"}
    call_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "DEV01",
                "call_id": "c0001-w001",
                "cache_key": "a" * 64,
                "provider": provider,
                "request": {},
                "response": normalized,
                "attempt_count": 1,
                "raw_response": raw,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "DEV01",
                "call_id": "c0001-w001",
                "cache_key": "a" * 64,
                "provider": provider,
                "validation_id": "old",
                "status": "failed",
                "attempts": [{"attempt": 1, "error": "old failure"}],
            }
        ),
        encoding="utf-8",
    )
    failed_audit.write_text("this failed-call audit must not be parsed", encoding="utf-8")
    (attempt / "window-manifest.json").write_text(
        json.dumps(
            {
                "task_id": "DEV01",
                "chapters": [
                    {
                        "chapter_id": "c0001",
                        "windows": [
                            {
                                "window_id": "c0001-w001",
                                "event_ids": ["a0001", "a0002", "a0003"],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (attempt / "attempt-report.json").write_text(
        json.dumps(
            {
                "task_id": "DEV01",
                "status": "failed",
                "attempt_version": chapter_content_module.CHAPTER_WINDOWED_REAL_VERSION,
                "failed_call_id": "c0002-w001",
                "completed_calls": [
                    {
                        "kind": "window",
                        "call_id": "c0001-w001",
                        "chapter_id": "c0001",
                        "output_path": str(call_path.resolve()),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    expected = {
        path.relative_to(attempt).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in attempt.rglob("*")
        if path.is_file()
    }
    return task_root, attempt_id, expected


def test_r2_reconciliation_records_superseded_audit_without_reading_failed_call(
    tmp_path: Path,
) -> None:
    task_root, attempt_id, expected = _prepare_reconciliation_attempt(tmp_path)

    result = chapter_content_module._reconcile_attempt_audits(
        task_root,
        attempt_id=attempt_id,
        expected_hashes=expected,
    )

    assert result.completed_call_count == 1
    assert result.inspected_audit_count == 1
    assert result.superseded_count == 1
    payload = json.loads(result.reconciliation_path.read_text(encoding="utf-8"))
    assert payload["failed_call_id_not_inspected"] == "c0002-w001"
    assert payload["relationships"][0]["call_id"] == "c0001-w001"
    assert (
        payload["relationships"][0]["relationship"]
        == "superseded_by_later_success"
    )


def test_r2_reconciliation_cache_does_not_rewrite(tmp_path: Path) -> None:
    task_root, attempt_id, expected = _prepare_reconciliation_attempt(tmp_path)
    first = chapter_content_module._reconcile_attempt_audits(
        task_root, attempt_id=attempt_id, expected_hashes=expected
    )
    before = first.reconciliation_path.read_bytes()
    mtime = first.reconciliation_path.stat().st_mtime_ns

    second = chapter_content_module._reconcile_attempt_audits(
        task_root, attempt_id=attempt_id, expected_hashes=expected
    )

    assert second.cache_hit is True
    assert second.reconciliation_path.read_bytes() == before
    assert second.reconciliation_path.stat().st_mtime_ns == mtime


def test_r2_reconciliation_rejects_frozen_hash_drift(tmp_path: Path) -> None:
    task_root, attempt_id, expected = _prepare_reconciliation_attempt(tmp_path)
    attempt = task_root / "content" / "attempts" / attempt_id
    (attempt / "attempt-report.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ChapterContentError, match="冻结文件哈希不一致"):
        chapter_content_module._reconcile_attempt_audits(
            task_root, attempt_id=attempt_id, expected_hashes=expected
        )


def _prepare_offline_candidate_source(
    tmp_path: Path,
    *,
    unknown_id: bool = False,
    extra_order_change: bool = False,
) -> tuple[Path, str, dict[str, str], Path, str]:
    task_root = tmp_path / "outputs" / "DEV01"
    attempt_id = "windowed-real-candidate-test"
    attempt = task_root / "content" / "attempts" / attempt_id
    audit_path = attempt / "windows" / "c0005" / "c0005-w001-validation-audit.json"
    audit_path.parent.mkdir(parents=True)
    raw = _window_response(["a0001", "a0002", "a0003"])
    raw["keywords"][1]["source_event_ids"] = [
        "a9999" if unknown_id else "a0003",
        "a0001",
    ]
    if extra_order_change:
        raw["summary"]["source_event_ids"] = ["a0003", "a0001"]
    audit_path.write_text(
        json.dumps(
            {
                "task_id": "DEV01",
                "call_id": "c0005-w001",
                "status": "failed",
                "attempts": [
                    {"attempt": 1, "raw_response": None, "error": "old"},
                    {
                        "attempt": 2,
                        "raw_response": raw,
                        "error": "引用事件ID未按时间轴顺序排列",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (attempt / "window-manifest.json").write_text(
        json.dumps(
            {
                "task_id": "DEV01",
                "chapters": [
                    {
                        "chapter_id": "c0005",
                        "windows": [
                            {
                                "window_id": "c0005-w001",
                                "event_ids": ["a0001", "a0002", "a0003"],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    expected = {
        path.relative_to(attempt).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in attempt.rglob("*")
        if path.is_file()
    }
    reconciliation_path = task_root / "content" / "recoveries" / "prior" / "reconciliation.json"
    reconciliation_path.parent.mkdir(parents=True)
    reconciliation_path.write_text(
        json.dumps(
            {
                "source_attempt_id": attempt_id,
                "failed_call_id_not_inspected": "c0005-w001",
            }
        ),
        encoding="utf-8",
    )
    reconciliation_sha256 = hashlib.sha256(reconciliation_path.read_bytes()).hexdigest()
    return task_root, attempt_id, expected, reconciliation_path, reconciliation_sha256


def test_r2_offline_candidate_changes_only_expected_evidence_order(
    tmp_path: Path,
) -> None:
    task_root, attempt_id, expected, reconciliation_path, reconciliation_hash = (
        _prepare_offline_candidate_source(tmp_path)
    )

    result = chapter_content_module._recover_failed_window_candidate_offline(
        task_root,
        attempt_id=attempt_id,
        expected_hashes=expected,
        reconciliation_path=reconciliation_path,
        reconciliation_sha256=reconciliation_hash,
        chapter_id="c0005",
        call_id="c0005-w001",
        source_attempt_number=2,
        expected_changed_fields=["keywords[2].source_event_ids"],
    )

    assert result.changed_field_count == 1
    assert {path.name for path in result.recovery_path.iterdir()} == {
        "candidate.json",
        "recovery-audit.json",
        "manifest.json",
    }
    candidate = json.loads(result.candidate_path.read_text(encoding="utf-8"))
    audit = json.loads(result.recovery_audit_path.read_text(encoding="utf-8"))
    assert candidate["response"]["keywords"][1]["source_event_ids"] == [
        "a0001",
        "a0003",
    ]
    assert audit["changed_fields"] == ["keywords[2].source_event_ids"]
    assert audit["invariants"]["api_request_count"] == 0
    assert not (task_root / "content" / "formal-results").exists()


def test_r2_offline_candidate_cache_does_not_rewrite(tmp_path: Path) -> None:
    task_root, attempt_id, expected, reconciliation_path, reconciliation_hash = (
        _prepare_offline_candidate_source(tmp_path)
    )
    arguments = {
        "attempt_id": attempt_id,
        "expected_hashes": expected,
        "reconciliation_path": reconciliation_path,
        "reconciliation_sha256": reconciliation_hash,
        "chapter_id": "c0005",
        "call_id": "c0005-w001",
        "source_attempt_number": 2,
        "expected_changed_fields": ["keywords[2].source_event_ids"],
    }
    first = chapter_content_module._recover_failed_window_candidate_offline(
        task_root, **arguments
    )
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.recovery_path.iterdir()
    }
    mtimes = {
        path.name: path.stat().st_mtime_ns for path in first.recovery_path.iterdir()
    }

    second = chapter_content_module._recover_failed_window_candidate_offline(
        task_root, **arguments
    )

    assert second.cache_hit is True
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.recovery_path.iterdir()
    } == hashes
    assert {
        path.name: path.stat().st_mtime_ns for path in second.recovery_path.iterdir()
    } == mtimes


def test_r2_offline_candidate_rejects_unexpected_additional_change(
    tmp_path: Path,
) -> None:
    task_root, attempt_id, expected, reconciliation_path, reconciliation_hash = (
        _prepare_offline_candidate_source(tmp_path, extra_order_change=True)
    )

    with pytest.raises(ChapterContentError, match="实际规范化字段"):
        chapter_content_module._recover_failed_window_candidate_offline(
            task_root,
            attempt_id=attempt_id,
            expected_hashes=expected,
            reconciliation_path=reconciliation_path,
            reconciliation_sha256=reconciliation_hash,
            chapter_id="c0005",
            call_id="c0005-w001",
            source_attempt_number=2,
            expected_changed_fields=["keywords[2].source_event_ids"],
        )

    assert not (task_root / "content" / "recoveries" / "offline-candidate").exists()


def test_r2_offline_candidate_rejects_unknown_evidence_id(tmp_path: Path) -> None:
    task_root, attempt_id, expected, reconciliation_path, reconciliation_hash = (
        _prepare_offline_candidate_source(tmp_path, unknown_id=True)
    )

    with pytest.raises(ChapterContentError, match="章外或未知"):
        chapter_content_module._recover_failed_window_candidate_offline(
            task_root,
            attempt_id=attempt_id,
            expected_hashes=expected,
            reconciliation_path=reconciliation_path,
            reconciliation_sha256=reconciliation_hash,
            chapter_id="c0005",
            call_id="c0005-w001",
            source_attempt_number=2,
            expected_changed_fields=["keywords[2].source_event_ids"],
        )


def test_r2_build_offline_recovered_bundle_validates_all_chapters(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    prepared = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    responses = {
        chapter["chapter_id"]: chapter_content_module._normalize_window_response(
            _window_response(chapter["event_ids"]),
            allowed_ids=chapter["event_ids"],
        )
        for chapter in manifest["chapters"]
    }

    bundle = chapter_content_module._build_offline_recovered_bundle(
        manifest,
        responses=responses,
        provider={"backend": "openai_compatible", "model": "test"},
        source_attempt_id="attempt",
        reconciliation_sha256="a" * 64,
        candidate_manifest_sha256="b" * 64,
    )

    assert len(bundle["chapters"]) == 3
    assert bundle["generation"]["mode"] == "real-windowed-offline-recovery"
    assert bundle["generation"]["api_request_count"] == 0
    assert bundle["generation"]["prompt_version"] == "chapter-content-window-1.0"
    assert bundle["generation"]["content_policy_version"] == "asr-evidence-only-1.0"
    assert bundle["generation"]["validator_version"] == "chapter-content-validator-1.1"
    assert bundle["chaptering_provenance"] == manifest["chaptering_provenance"]


def test_r2_build_offline_recovered_bundle_rejects_missing_chapter(
    tmp_path: Path,
) -> None:
    output_root = _prepare_formal_task(tmp_path)
    prepared = prepare_task_chapter_inputs(output_root, "DEV01")
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    responses = {
        chapter["chapter_id"]: chapter_content_module._normalize_window_response(
            _window_response(chapter["event_ids"]),
            allowed_ids=chapter["event_ids"],
        )
        for chapter in manifest["chapters"][:-1]
    }

    with pytest.raises(ChapterContentError, match="缺失、未知或混用"):
        chapter_content_module._build_offline_recovered_bundle(
            manifest,
            responses=responses,
            provider={"backend": "openai_compatible", "model": "test"},
            source_attempt_id="attempt",
            reconciliation_sha256="a" * 64,
            candidate_manifest_sha256="b" * 64,
        )


def test_r2_verify_frozen_group_rejects_damage(tmp_path: Path) -> None:
    group = tmp_path / "candidate"
    group.mkdir()
    candidate = group / "candidate.json"
    candidate.write_text("{}", encoding="utf-8")
    expected = {"candidate.json": hashlib.sha256(candidate.read_bytes()).hexdigest()}
    candidate.write_text('{"changed":true}', encoding="utf-8")

    with pytest.raises(ChapterContentError, match="文件哈希不一致"):
        chapter_content_module._verify_frozen_group(
            group, expected_hashes=expected, description="测试候选"
        )


def _prepare_r3_semantic_repair_source(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any]]:
    output_root = _prepare_formal_task(tmp_path)
    prepared = prepare_task_chapter_inputs(output_root, "DEV01")
    input_manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    source_bundle = _bundle(input_manifest)
    source_bundle["generation"].update(
        {
            "mode": "real-windowed-offline-recovery",
            "prompt_version": "chapter-content-window-1.0",
            "merge_prompt_version": "chapter-content-merge-1.0",
            "content_policy_version": "asr-evidence-only-1.0",
            "validator_version": "chapter-content-validator-1.1",
        }
    )
    source_bundle["generation"].pop("output_language")
    source_bundle["chapters"][0]["key_points"][2]["text"] = "错误数字"
    source_result_id = "content-test-source"
    source_group = (
        output_root
        / "DEV01"
        / "content"
        / "formal-results"
        / source_result_id
    )
    content_path = source_group / "content.json"
    report_path = source_group / "execution-report.json"
    _write_json(content_path, source_bundle)
    _write_json(
        report_path,
        {
            "schema_version": "1.0",
            "result_id": source_result_id,
            "task_id": "DEV01",
            "status": "success",
        },
    )
    _write_json(
        source_group / "manifest.json",
        {
            "schema_version": "1.0",
            "result_id": source_result_id,
            "task_id": "DEV01",
            "files": {
                "content.json": hashlib.sha256(content_path.read_bytes()).hexdigest(),
                "execution-report.json": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            },
        },
    )
    source_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_group.iterdir()
    }
    evidence_audit_path = tmp_path / "r2-5-audit.md"
    evidence_audit_path.write_text("frozen semantic audit", encoding="utf-8")
    arguments = {
        "source_result_id": source_result_id,
        "source_hashes": source_hashes,
        "evidence_audit_path": evidence_audit_path,
        "evidence_audit_sha256": hashlib.sha256(
            evidence_audit_path.read_bytes()
        ).hexdigest(),
        "target_chapter_id": "c0001",
        "target_key_point_index": 2,
        "before_text": "错误数字",
        "after_text": "正确数字为每14人中约有1人",
        "evidence_ids": ["a0001"],
        "required_evidence_text": {
            "a0001": "第一章开始，片段跨过六十秒边界"
        },
    }
    return output_root, arguments


def test_r3_dev01_frozen_source_hash_constants_are_exact() -> None:
    assert chapter_content_module.P9_5_R3_SOURCE_HASHES == {
        "content.json": "ee629613b4833e17c9b4c12537b515f2d198ae8390b8f8c208eacb457f6f1a22",
        "execution-report.json": "f42e2a3190e3d99c072b02d0681479a94a107a4b9fdd7b1d2db5f2a704a74b98",
        "manifest.json": "fd826f0f664b7d5be1da9d8ed3b84caf847f56c25de96a3fcdcfff950d0dc41c",
    }
    assert chapter_content_module.P9_5_R3_REPAIR_ID == "semantic-repair-d46996a2e873"
    assert chapter_content_module.P9_5_R3_REPAIR_HASHES == {
        "candidate.json": "1c80e3ca4a0cf7edc0a0001f1d4142dab3f6ebed38f3f4cce668532621e97fe9",
        "repair-audit.json": "0be36cba2b46e46e382e5afa573b79ad24a7f4bc61457cad8acef15bf6e94f90",
        "manifest.json": "8688627deb18151ad758b01072057a2d44032c2b22077209c2f948c76083b1a1",
    }


def test_r3_semantic_repair_candidate_is_atomic_exact_and_cacheable(
    tmp_path: Path,
) -> None:
    output_root, arguments = _prepare_r3_semantic_repair_source(tmp_path)
    formal_root = output_root / "DEV01" / "content" / "formal-results"
    source_hashes_before = {
        str(path.relative_to(formal_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in formal_root.rglob("*")
        if path.is_file()
    }

    first = chapter_content_module._prepare_semantic_repair_candidate(
        output_root, "DEV01", **arguments
    )

    assert first.cache_hit is False
    assert first.changed_text_path_count == 1
    assert first.api_request_count == 0
    assert {path.name for path in first.repair_path.iterdir()} == {
        "candidate.json",
        "repair-audit.json",
        "manifest.json",
    }
    candidate = json.loads(first.candidate_path.read_text(encoding="utf-8"))
    audit = json.loads(first.repair_audit_path.read_text(encoding="utf-8"))
    repaired = candidate["content"]
    assert repaired["chapters"][0]["key_points"][2] == {
        "text": "正确数字为每14人中约有1人",
        "source_event_ids": ["a0001"],
    }
    assert repaired["generation"]["prompt_version"] == "chapter-content-window-1.0"
    assert repaired["generation"]["validator_version"] == "chapter-content-validator-1.2"
    assert repaired["generation"]["source_validator_version"] == "chapter-content-validator-1.1"
    assert repaired["generation"]["output_language"] == "zh-CN"
    assert audit["invariants"]["changed_text_paths"] == [
        "chapters[c0001].key_points[2].text"
    ]
    assert audit["invariants"]["api_request_count"] == 0
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.repair_path.iterdir()
    }
    mtimes = {path.name: path.stat().st_mtime_ns for path in first.repair_path.iterdir()}

    second = chapter_content_module._prepare_semantic_repair_candidate(
        output_root, "DEV01", **arguments
    )

    assert second.cache_hit is True
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.repair_path.iterdir()
    } == hashes
    assert {
        path.name: path.stat().st_mtime_ns for path in second.repair_path.iterdir()
    } == mtimes
    assert {
        str(path.relative_to(formal_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in formal_root.rglob("*")
        if path.is_file()
    } == source_hashes_before
    assert not (output_root / "DEV01" / "content" / "content.json").exists()


@pytest.mark.parametrize(
    "failure_kind",
    ["source_hash", "audit_hash", "before_text", "evidence_text"],
)
def test_r3_semantic_repair_rejects_frozen_input_drift(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    output_root, arguments = _prepare_r3_semantic_repair_source(tmp_path)
    if failure_kind == "source_hash":
        arguments["source_hashes"] = dict(arguments["source_hashes"])
        arguments["source_hashes"]["content.json"] = "0" * 64
    elif failure_kind == "audit_hash":
        arguments["evidence_audit_sha256"] = "0" * 64
    elif failure_kind == "before_text":
        arguments["before_text"] = "错误的冻结原文"
    else:
        arguments["required_evidence_text"] = {"a0001": "错误证据"}

    with pytest.raises(ChapterContentError):
        chapter_content_module._prepare_semantic_repair_candidate(
            output_root, "DEV01", **arguments
        )

    repair_root = output_root / "DEV01" / "content" / "semantic-repairs"
    assert not repair_root.exists() or not list(repair_root.iterdir())


def test_r3_semantic_repair_diff_rejects_second_content_change() -> None:
    source = {
        "generation": {"validator_version": "old"},
        "chapters": [
            {
                "chapter_id": "c0001",
                "title": {"text": "标题"},
                "summary": {"text": "摘要"},
                "key_points": [{"text": "错误数字"}, {"text": "知识点二"}],
                "keywords": [{"text": "关键词"}],
            }
        ],
    }
    repaired = json.loads(json.dumps(source, ensure_ascii=False))
    repaired["generation"] = {"validator_version": "new"}
    repaired["chapters"][0]["key_points"][0]["text"] = "正确数字"
    repaired["chapters"][0]["summary"]["text"] = "被意外修改的摘要"

    with pytest.raises(ChapterContentError, match="超出唯一字段白名单"):
        chapter_content_module._validate_semantic_repair_diff(
            source,
            repaired,
            target_chapter_id="c0001",
            target_key_point_index=0,
            before_text="错误数字",
        )


def test_r3_semantic_repair_rejects_damaged_cached_group(tmp_path: Path) -> None:
    output_root, arguments = _prepare_r3_semantic_repair_source(tmp_path)
    result = chapter_content_module._prepare_semantic_repair_candidate(
        output_root, "DEV01", **arguments
    )
    result.candidate_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ChapterContentError, match="损坏或发生内容混用"):
        chapter_content_module._prepare_semantic_repair_candidate(
            output_root, "DEV01", **arguments
        )


def _prepare_r3_formal_publish_case(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    output_root, arguments = _prepare_r3_semantic_repair_source(tmp_path)
    candidate_result = chapter_content_module._prepare_semantic_repair_candidate(
        output_root, "DEV01", **arguments
    )
    task_root = output_root / "DEV01"
    source_group = (
        task_root
        / "content"
        / "formal-results"
        / arguments["source_result_id"]
    )
    source_bundle = json.loads(
        (source_group / "content.json").read_text(encoding="utf-8")
    )
    source_report = json.loads(
        (source_group / "execution-report.json").read_text(encoding="utf-8")
    )
    _write_json(task_root / "content" / "content.json", source_bundle)
    _write_json(
        task_root / "content" / "content-execution-report.json", source_report
    )
    publish_arguments = {
        **arguments,
        "repair_relative_path": str(
            candidate_result.repair_path.relative_to(task_root).as_posix()
        ),
        "repair_hashes": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in candidate_result.repair_path.iterdir()
            if path.is_file()
        },
    }
    return output_root, publish_arguments, source_bundle, source_report


def test_r3_formal_publish_creates_new_group_and_switches_aliases(
    tmp_path: Path,
) -> None:
    output_root, arguments, source_bundle, source_report = (
        _prepare_r3_formal_publish_case(tmp_path)
    )
    task_root = output_root / "DEV01"
    source_group = (
        task_root
        / "content"
        / "formal-results"
        / arguments["source_result_id"]
    )
    source_hashes_before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_group.iterdir()
        if path.is_file()
    }

    result = chapter_content_module._publish_semantic_repair_formal_result(
        output_root, "DEV01", **arguments
    )

    assert result.result_id != arguments["source_result_id"]
    assert result.result_id == "content-" + json.loads(
        (
            task_root
            / arguments["repair_relative_path"]
            / "candidate.json"
        ).read_text(encoding="utf-8")
    )["repaired_content_sha256"][:12]
    assert result.api_request_count == 0
    assert result.result_group_cache_hit is False
    assert result.top_level_content_cache_hit is False
    assert result.top_level_report_cache_hit is False
    assert {path.name for path in result.result_group_path.iterdir()} == {
        "content.json",
        "execution-report.json",
        "manifest.json",
    }
    repaired = json.loads(result.content_path.read_text(encoding="utf-8"))
    report = json.loads(result.execution_report_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (result.result_group_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert repaired["chapters"][0]["key_points"][2] == {
        "text": arguments["after_text"],
        "source_event_ids": arguments["evidence_ids"],
    }
    assert repaired["chaptering_provenance"] == source_bundle["chaptering_provenance"]
    assert report["repair_api_request_count"] == 0
    assert report["source_content_result_id"] == arguments["source_result_id"]
    assert report["semantic_repair_manifest_sha256"] == arguments["repair_hashes"][
        "manifest.json"
    ]
    assert manifest["files"] == {
        "content.json": hashlib.sha256(result.content_path.read_bytes()).hexdigest(),
        "execution-report.json": hashlib.sha256(
            result.execution_report_path.read_bytes()
        ).hexdigest(),
    }
    assert json.loads(result.top_level_content_path.read_text(encoding="utf-8")) == repaired
    assert json.loads(result.top_level_report_path.read_text(encoding="utf-8")) == report
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_group.iterdir()
        if path.is_file()
    } == source_hashes_before
    assert source_bundle != repaired
    assert source_report != report


def test_r3_formal_publish_rejects_candidate_hash_drift_before_writes(
    tmp_path: Path,
) -> None:
    output_root, arguments, source_bundle, source_report = (
        _prepare_r3_formal_publish_case(tmp_path)
    )
    task_root = output_root / "DEV01"
    repair_group = task_root / arguments["repair_relative_path"]
    (repair_group / "repair-audit.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ChapterContentError):
        chapter_content_module._publish_semantic_repair_formal_result(
            output_root, "DEV01", **arguments
        )

    assert json.loads(
        (task_root / "content" / "content.json").read_text(encoding="utf-8")
    ) == source_bundle
    assert json.loads(
        (task_root / "content" / "content-execution-report.json").read_text(
            encoding="utf-8"
        )
    ) == source_report
    formal_groups = list((task_root / "content" / "formal-results").iterdir())
    assert [path.name for path in formal_groups] == [arguments["source_result_id"]]


def test_r3_formal_publish_rejects_mixed_aliases_before_new_group(
    tmp_path: Path,
) -> None:
    output_root, arguments, _, source_report = _prepare_r3_formal_publish_case(
        tmp_path
    )
    task_root = output_root / "DEV01"
    _write_json(task_root / "content" / "content.json", {"mixed": True})

    with pytest.raises(ChapterContentError, match="顶层别名"):
        chapter_content_module._publish_semantic_repair_formal_result(
            output_root, "DEV01", **arguments
        )

    assert json.loads(
        (task_root / "content" / "content.json").read_text(encoding="utf-8")
    ) == {"mixed": True}
    assert json.loads(
        (task_root / "content" / "content-execution-report.json").read_text(
            encoding="utf-8"
        )
    ) == source_report
    formal_groups = list((task_root / "content" / "formal-results").iterdir())
    assert [path.name for path in formal_groups] == [arguments["source_result_id"]]


def test_r3_formal_publish_rolls_back_alias_and_new_group_on_switch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, arguments, source_bundle, source_report = (
        _prepare_r3_formal_publish_case(tmp_path)
    )
    task_root = output_root / "DEV01"
    original_write = chapter_content_module._write_json_atomic
    failed = False

    def fail_new_report_once(path: Path, payload: dict[str, Any]) -> bool:
        nonlocal failed
        if (
            path.name == "content-execution-report.json"
            and payload.get("mode")
            == "real-windowed-offline-recovery-with-semantic-repair"
            and not failed
        ):
            failed = True
            raise ChapterContentError("模拟报告别名切换失败")
        return original_write(path, payload)

    monkeypatch.setattr(
        chapter_content_module, "_write_json_atomic", fail_new_report_once
    )

    with pytest.raises(ChapterContentError, match="模拟报告别名切换失败"):
        chapter_content_module._publish_semantic_repair_formal_result(
            output_root, "DEV01", **arguments
        )

    assert json.loads(
        (task_root / "content" / "content.json").read_text(encoding="utf-8")
    ) == source_bundle
    assert json.loads(
        (task_root / "content" / "content-execution-report.json").read_text(
            encoding="utf-8"
        )
    ) == source_report
    formal_groups = list((task_root / "content" / "formal-results").iterdir())
    assert [path.name for path in formal_groups] == [arguments["source_result_id"]]


def test_r3_formal_publish_rejects_damaged_existing_new_group(
    tmp_path: Path,
) -> None:
    output_root, arguments, source_bundle, source_report = (
        _prepare_r3_formal_publish_case(tmp_path)
    )
    task_root = output_root / "DEV01"
    candidate = json.loads(
        (task_root / arguments["repair_relative_path"] / "candidate.json").read_text(
            encoding="utf-8"
        )
    )
    result_id = "content-" + candidate["repaired_content_sha256"][:12]
    damaged_group = task_root / "content" / "formal-results" / result_id
    _write_json(damaged_group / "content.json", {"damaged": True})

    with pytest.raises(ChapterContentError, match="结果组"):
        chapter_content_module._publish_semantic_repair_formal_result(
            output_root, "DEV01", **arguments
        )

    assert json.loads(
        (task_root / "content" / "content.json").read_text(encoding="utf-8")
    ) == source_bundle
    assert json.loads(
        (task_root / "content" / "content-execution-report.json").read_text(
            encoding="utf-8"
        )
    ) == source_report


def test_r3_formal_publish_cli_reports_zero_api(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    result_group = tmp_path / "formal-results" / "content-new"
    result_group.mkdir(parents=True)
    result = chapter_content_module.SemanticRepairFormalResult(
        result_group_path=result_group,
        content_path=result_group / "content.json",
        execution_report_path=result_group / "execution-report.json",
        top_level_content_path=tmp_path / "content.json",
        top_level_report_path=tmp_path / "content-execution-report.json",
        result_id="content-new",
        chapter_count=5,
        api_request_count=0,
        result_group_cache_hit=False,
        top_level_content_cache_hit=False,
        top_level_report_cache_hit=False,
    )
    monkeypatch.setattr(
        cli, "publish_task_chapter_content_semantic_repair", lambda *_: result
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "video-doc",
            "publish-chapter-content-semantic-repair",
            "DEV01",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert '"result_id": "content-new"' in output
    assert '"api_request_count": 0' in output
