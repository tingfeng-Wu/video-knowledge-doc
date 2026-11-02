import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from tests.test_document_chapter_composition import _composition_input, _workspace
from video_doc.fact_card_composition import (
    CONTINUOUS_BATCH_MAX_TOKENS,
    CONTINUOUS_CONTENT_MAX_TOKENS,
    CONTINUOUS_GLOBAL_MAX_TOKENS,
    FactCardCompositionError,
    build_continuous_content_dry_run,
    build_continuous_content_payload,
    build_continuous_batch_dry_run,
    build_continuous_global_dry_run,
    build_continuous_global_payload,
    build_continuous_source_units,
    build_fact_cards,
    build_required_facts,
    compose_continuous_content_chapter_once,
    compose_continuous_batch_once,
    compose_continuous_global_once,
    derive_continuous_content_evidence,
    derive_continuous_global_evidence,
    derive_fact_card_evidence,
    review_continuous_content_response,
    build_continuous_reviewed_chapter_result,
    validate_continuous_content_response,
    validate_continuous_global_response,
    validate_fact_card_response,
)
from video_doc.llm_adapter import LLMConfig


def _response(cards: list[dict[str, object]]) -> dict[str, object]:
    return {
        "sections": [
            {
                "heading": None,
                "blocks": [
                    {
                        "type": "paragraph",
                        "fact_ids": [card["fact_id"] for card in cards],
                        "text": "样本包含14人，没有使用外部数据。ModelA需要2步，ModelB可能更快。",
                    }
                ],
            }
        ],
        "omitted_duplicates": [],
    }


def test_fact_cards_inherit_v1_ledger_deterministically() -> None:
    chapter = _composition_input()["chapters"][0]
    cards = build_fact_cards(chapter)

    assert [card["fact_id"] for card in cards] == [
        "c0001-f001",
        "c0001-f002",
        "c0001-f003",
        "c0001-f004",
    ]
    assert cards[0]["source_content_path"] == "chapters[c0001].title"
    assert cards[0]["source_event_ids"] == ["a0001"]


def test_unknown_fact_id_is_rejected() -> None:
    cards = build_fact_cards(_composition_input()["chapters"][0])
    response = _response(cards)
    response["sections"][0]["blocks"][0]["fact_ids"] = ["c9999-f001"]

    with pytest.raises(FactCardCompositionError, match="未知或章外"):
        validate_fact_card_response(response, fact_cards=cards)


def test_new_number_is_rejected_against_declared_cards() -> None:
    cards = build_fact_cards(_composition_input()["chapters"][0])
    response = _response(cards)
    response["sections"][0]["blocks"][0]["text"] = "样本包含15人。"

    with pytest.raises(FactCardCompositionError, match="数字/比例"):
        validate_fact_card_response(response, fact_cards=cards)


def test_local_derivation_uses_fact_ids_not_model_ledger() -> None:
    chapter = _composition_input()["chapters"][0]
    cards = build_fact_cards(chapter)
    response = _response(cards)

    derived = derive_fact_card_evidence(
        response, chapter_input=chapter, fact_cards=cards
    )

    block = derived["blocks"][0]
    assert block["source_content_paths"] == [
        card["source_content_path"] for card in cards
    ]
    assert block["source_event_ids"] == ["a0001", "a0002", "a0003"]
    assert all(item["status"] == "represented" for item in derived["coverage"])


def test_omitted_duplicate_requires_overlapping_evidence() -> None:
    chapter = _composition_input()["chapters"][0]
    cards = build_fact_cards(chapter)
    response = _response(cards)
    response["sections"][0]["blocks"][0]["fact_ids"] = [cards[0]["fact_id"]]
    response["sections"][0]["blocks"][0]["text"] = cards[0]["text"]
    response["omitted_duplicates"] = [
        {"fact_id": cards[1]["fact_id"], "represented_by": cards[0]["fact_id"]},
        {"fact_id": cards[2]["fact_id"], "represented_by": cards[0]["fact_id"]},
    ]

    with pytest.raises(FactCardCompositionError, match="没有重合事件证据"):
        validate_fact_card_response(response, fact_cards=cards)


def _continuous_response(
    facts: list[dict[str, object]], units: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "paragraphs": [
            {
                "source_unit_ids": [unit["unit_id"] for unit in units],
                "fact_ids": [fact["fact_id"] for fact in facts],
                "text": " ".join(str(fact["text"]) for fact in facts),
            }
        ]
    }


def _five_chapter_composition() -> dict[str, Any]:
    composition = _composition_input()
    template = composition["chapters"][0]
    chapters = []
    for chapter_index in range(1, 6):
        chapter = deepcopy(template)
        chapter_id = f"c{chapter_index:04d}"
        event_id_map = {
            event["id"]: f"a{(chapter_index - 1) * 4 + offset:04d}"
            for offset, event in enumerate(chapter["events"], start=1)
        }
        chapter["chapter_id"] = chapter_id
        chapter["index"] = chapter_index
        for event in chapter["events"]:
            event["id"] = event_id_map[event["id"]]
        for field in chapter["source_fields"]:
            field["source_content_path"] = field["source_content_path"].replace(
                "c0001", chapter_id
            )
            field["source_event_ids"] = [
                event_id_map[event_id] for event_id in field["source_event_ids"]
            ]
        chapters.append(chapter)
    composition["chapters"] = chapters
    return composition


def test_required_facts_only_include_v1_key_points() -> None:
    facts = build_required_facts(_composition_input()["chapters"][0])

    assert [fact["fact_id"] for fact in facts] == ["c0001-f001", "c0001-f002"]
    assert all(".key_points[" in fact["source_content_path"] for fact in facts)


def test_continuous_units_preserve_all_events_at_event_boundaries() -> None:
    chapter = _composition_input()["chapters"][0]
    max_chars = max(len(event["text"]) for event in chapter["events"])
    units = build_continuous_source_units(chapter, max_chars=max_chars)

    assert [event_id for unit in units for event_id in unit["source_event_ids"]] == [
        event["id"] for event in chapter["events"]
    ]
    assert all(len(unit["text"]) <= max_chars for unit in units)
    assert [unit["unit_id"] for unit in units] == [
        f"c0001-u{index:03d}" for index in range(1, len(units) + 1)
    ]


def test_continuous_payload_exposes_only_frozen_four_fields() -> None:
    payload, facts, units = build_continuous_content_payload(
        _composition_input(), chapter_index=1
    )

    assert set(payload) == {"title", "profile", "facts", "units"}
    assert set(payload["facts"][0]) == {"fact_id", "text"}
    assert set(payload["units"][0]) == {"unit_id", "text"}
    assert "source_event_ids" in facts[0]
    assert "source_event_ids" in units[0]


def test_minimal_response_requires_each_fact_once_and_known_adjacent_units() -> None:
    chapter = _composition_input()["chapters"][0]
    facts = build_required_facts(chapter)
    units = build_continuous_source_units(chapter)
    response = _continuous_response(facts, units)

    assert validate_continuous_content_response(
        response, required_facts=facts, source_units=units
    ) == response

    response["paragraphs"][0]["fact_ids"] = [facts[0]["fact_id"]]
    with pytest.raises(FactCardCompositionError, match="知识点未覆盖"):
        validate_continuous_content_response(
            response, required_facts=facts, source_units=units
        )


def test_minimal_response_rejects_new_high_risk_fact() -> None:
    chapter = _composition_input()["chapters"][0]
    facts = build_required_facts(chapter)
    units = build_continuous_source_units(chapter)
    response = _continuous_response(facts, units)
    response["paragraphs"][0]["text"] = "样本包含999人，因此必须治疗。"

    with pytest.raises(FactCardCompositionError, match="数字/比例"):
        validate_continuous_content_response(
            response, required_facts=facts, source_units=units
        )


def test_continuous_response_accepts_supported_entity_with_different_quotes() -> None:
    chapter = _composition_input()["chapters"][0]
    facts = build_required_facts(chapter)
    units = build_continuous_source_units(chapter)
    facts[0]["text"] += "，采用‘先开始’策略。"
    response = _continuous_response(facts, units)
    response["paragraphs"][0]["text"] += "，采用“先开始”策略。"

    assert validate_continuous_content_response(
        response, required_facts=facts, source_units=units
    ) == response

    response["paragraphs"][0]["text"] += "，采用“来源没有的新策略”。"
    with pytest.raises(FactCardCompositionError, match="引号实体"):
        validate_continuous_content_response(
            response, required_facts=facts, source_units=units
        )


def test_connective_is_review_flag_not_hard_failure() -> None:
    chapter = _composition_input()["chapters"][0]
    facts = build_required_facts(chapter)
    units = build_continuous_source_units(chapter)
    response = _continuous_response(facts, units)
    response["paragraphs"][0]["text"] = (
        " ".join(str(fact["text"]) for fact in facts) + "，从而形成连续说明。"
    )

    assert validate_continuous_content_response(
        response, required_facts=facts, source_units=units
    ) == response
    assert review_continuous_content_response(
        response, required_facts=facts, source_units=units
    ) == [
        {"paragraph_index": 1, "category": "人工复核表达", "phrase": "从而"}
    ]


def test_continuous_evidence_and_dry_run_are_fully_local() -> None:
    composition = _composition_input()
    chapter = composition["chapters"][0]
    payload, facts, units = build_continuous_content_payload(
        composition, chapter_index=1
    )
    response = _continuous_response(facts, units)

    derived = derive_continuous_content_evidence(
        response,
        chapter_input=chapter,
        required_facts=facts,
        source_units=units,
    )
    dry_run = build_continuous_content_dry_run(composition, chapter_index=1)

    assert derived["blocks"][0]["source_event_ids"] == [
        event["id"] for event in chapter["events"]
    ]
    assert [item["source_content_path"] for item in derived["coverage"]] == [
        fact["source_content_path"] for fact in facts
    ]
    assert dry_run["payload"] == payload
    assert dry_run["max_tokens"] == CONTINUOUS_CONTENT_MAX_TOKENS
    assert dry_run["api_request_count"] == 0
    assert dry_run["payload_chars"] > 0


def _reviewed_candidate(composition: dict[str, Any]) -> dict[str, Any]:
    payload, facts, units = build_continuous_content_payload(
        composition, chapter_index=1
    )
    return {
        "chapters": [
            {
                "chapter_id": "c0001",
                "title": payload["title"],
                "paragraphs": _continuous_response(facts, units)["paragraphs"],
            }
        ]
    }


def test_continuous_global_dry_run_and_local_2_0_adapter() -> None:
    from video_doc.document_content_preview import assemble_structured_document_content

    composition = _composition_input()
    candidate = _reviewed_candidate(composition)
    dry_run = build_continuous_global_dry_run(
        candidate, document_profile=composition["document_profile"]
    )
    payload = build_continuous_global_payload(
        candidate, document_profile=composition["document_profile"]
    )
    paragraph = candidate["chapters"][0]["paragraphs"][0]["text"]
    response = {
        "title": candidate["chapters"][0]["title"],
        "overview": [paragraph],
        "conclusions": [paragraph],
    }

    assert dry_run["payload"] == payload
    assert dry_run["max_tokens"] == CONTINUOUS_GLOBAL_MAX_TOKENS
    assert dry_run["api_request_count"] == 0
    assert validate_continuous_global_response(response, payload=payload) == response
    chapter_result = build_continuous_reviewed_chapter_result(composition, candidate)
    global_response = derive_continuous_global_evidence(
        response, payload=payload, chapter_result=chapter_result
    )
    document = assemble_structured_document_content(
        composition,
        chapter_result=chapter_result,
        global_response=global_response,
        generation_mode="document-composition-real",
        generation_backend="openai_compatible",
        generation_model="fixture",
        generation_api_request_count=3,
    )
    assert len(document["chapters"]) == 1
    assert len(document["coverage"]["items"]) == 4


class _OneShotGlobalTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        payload = json.loads(
            str(messages[-1]["content"]).split("CONTINUOUS_GLOBAL_INPUT_JSON=", 1)[1]
        )
        self.calls.append({"max_tokens": config.max_tokens, "payload": payload})
        text = payload["chapters"][0]["paragraphs"][0]
        return {
            "title": payload["chapters"][0]["title"],
            "overview": [text],
            "conclusions": [text],
        }


def test_continuous_global_real_entry_is_one_shot(tmp_path: Path) -> None:
    output_root = _workspace(tmp_path)
    composition = _composition_input()
    candidate = _reviewed_candidate(composition)
    transport = _OneShotGlobalTransport()
    config = LLMConfig(
        backend="openai_compatible",
        base_url="https://example.invalid/v1",
        model="fixture",
        timeout_seconds=1,
        api_key="secret",
        mock_response={},
    )

    result = compose_continuous_global_once(
        output_root,
        "DEV01",
        candidate,
        document_profile=composition["document_profile"],
        input_id=composition["input_id"],
        config=config,
        transport=transport,
    )

    assert result.attempt_count == 1
    assert len(transport.calls) == 1
    assert transport.calls[0]["max_tokens"] == CONTINUOUS_GLOBAL_MAX_TOKENS
    assert not (output_root / "DEV01" / "content" / "document-v2" / "previews").exists()


class _OneShotContinuousTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        payload = json.loads(
            str(messages[-1]["content"]).split("CONTINUOUS_CONTENT_INPUT_JSON=", 1)[1]
        )
        self.calls.append(
            {
                "max_tokens": config.max_tokens,
                "payload": payload,
                "response_format": dict(response_format),
            }
        )
        return {
            "paragraphs": [
                {
                    "source_unit_ids": [item["unit_id"] for item in payload["units"]],
                    "fact_ids": [item["fact_id"] for item in payload["facts"]],
                    "text": " ".join(item["text"] for item in payload["facts"]),
                }
            ]
        }


def test_continuous_real_entry_is_one_shot_and_never_publishes_preview(
    tmp_path: Path,
) -> None:
    output_root = _workspace(tmp_path)
    transport = _OneShotContinuousTransport()
    config = LLMConfig(
        backend="openai_compatible",
        base_url="https://example.invalid/v1/chat/completions",
        model="fake-model",
        timeout_seconds=1.0,
        api_key="test-only-secret",
        mock_response={},
        enable_thinking=False,
        temperature=0.0,
    )

    result, facts, units, derived = compose_continuous_content_chapter_once(
        output_root,
        "DEV01",
        _composition_input(),
        chapter_index=1,
        config=config,
        transport=transport,
    )

    assert result.attempt_count == 1
    assert len(transport.calls) == 1
    assert transport.calls[0]["max_tokens"] == CONTINUOUS_CONTENT_MAX_TOKENS
    assert set(transport.calls[0]["payload"]) == {"title", "profile", "facts", "units"}
    assert len(facts) == 2
    assert units
    assert derived["coverage"]
    document_v2 = output_root / "DEV01" / "content" / "document-v2"
    assert not (document_v2 / "previews").exists()
    assert not (document_v2 / "formal-results").exists()
    assert not (document_v2 / "current.json").exists()


class _OneShotBatchTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        config: LLMConfig,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any],
    ) -> Any:
        payload = json.loads(
            str(messages[-1]["content"]).split("CONTINUOUS_BATCH_INPUT_JSON=", 1)[1]
        )
        self.calls.append({"max_tokens": config.max_tokens, "payload": payload})
        return {
            "chapters": [
                {
                    "chapter_id": chapter["chapter_id"],
                    "paragraphs": [
                        {
                            "source_unit_ids": [unit["unit_id"] for unit in chapter["units"]],
                            "fact_ids": [fact["fact_id"] for fact in chapter["facts"]],
                            "text": " ".join(fact["text"] for fact in chapter["facts"]),
                        }
                    ],
                }
                for chapter in payload["chapters"]
            ]
        }


def test_continuous_batch_is_four_chapters_and_one_shot(tmp_path: Path) -> None:
    composition = _five_chapter_composition()
    dry_run = build_continuous_batch_dry_run(composition)
    assert [chapter["chapter_id"] for chapter in dry_run["payload"]["chapters"]] == [
        "c0002",
        "c0003",
        "c0004",
        "c0005",
    ]
    assert dry_run["max_tokens"] == CONTINUOUS_BATCH_MAX_TOKENS

    output_root = _workspace(tmp_path)
    transport = _OneShotBatchTransport()
    config = LLMConfig(
        backend="openai_compatible",
        base_url="https://example.invalid/v1/chat/completions",
        model="fake-model",
        timeout_seconds=1.0,
        api_key="test-only-secret",
        mock_response={},
        enable_thinking=False,
        temperature=0.0,
    )
    result, derived = compose_continuous_batch_once(
        output_root,
        "DEV01",
        composition,
        config=config,
        transport=transport,
    )

    assert result.attempt_count == 1
    assert len(transport.calls) == 1
    assert transport.calls[0]["max_tokens"] == CONTINUOUS_BATCH_MAX_TOKENS
    assert [chapter["chapter_id"] for chapter in derived["chapters"]] == [
        "c0002",
        "c0003",
        "c0004",
        "c0005",
    ]
    document_v2 = output_root / "DEV01" / "content" / "document-v2"
    assert not (document_v2 / "previews").exists()
    assert not (document_v2 / "formal-results").exists()
    assert not (document_v2 / "current.json").exists()
