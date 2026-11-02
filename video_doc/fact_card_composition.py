"""阶段9-R受限正文编排：模型写正文，本地维护事实与ASR证据账本。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from video_doc.document_chapter_composition import _high_risk_tokens
from video_doc.llm_adapter import (
    LLMCallResult,
    LLMConfig,
    LLMRetryableError,
    LLMTransport,
    call_task_llm,
)


FACT_CARD_PROMPT_VERSION = "document-fact-card-prompt-1.0"
FACT_CARD_CACHE_NAMESPACE = "document-fact-card-composition-1-0"
FACT_CARD_VALIDATION_ID = "document-fact-card-validator-1-0"

CONTINUOUS_CONTENT_PROMPT_VERSION = "document-continuous-content-prompt-1.0"
CONTINUOUS_CONTENT_VALIDATION_ID = "document-continuous-content-validator-1-1"
CONTINUOUS_CONTENT_UNIT_VERSION = "continuous-asr-unit-1.0"
CONTINUOUS_CONTENT_UNIT_MAX_CHARS = 800
CONTINUOUS_CONTENT_OUTPUT_MAX_CHARS = 6000
CONTINUOUS_CONTENT_MAX_TOKENS = 1800
CONTINUOUS_CONTENT_CACHE_NAMESPACE = "document-continuous-content-1-0"
CONTINUOUS_CONTENT_COMPACT_PROMPT_VERSION = "document-continuous-content-compact-1.0"
CONTINUOUS_CONTENT_COMPACT_VALIDATION_ID = "document-continuous-content-compact-validator-1-0"
CONTINUOUS_CONTENT_COMPACT_CACHE_NAMESPACE = "document-continuous-content-compact-1-0"
CONTINUOUS_BATCH_PROMPT_VERSION = "document-continuous-batch-prompt-1.0"
CONTINUOUS_BATCH_VALIDATION_ID = "document-continuous-batch-validator-1-0"
CONTINUOUS_BATCH_CACHE_NAMESPACE = "document-continuous-batch-1-0"
CONTINUOUS_BATCH_MAX_TOKENS = 3600
CONTINUOUS_GLOBAL_PROMPT_VERSION = "document-continuous-global-prompt-1.0"
CONTINUOUS_GLOBAL_VALIDATION_ID = "document-continuous-global-validator-1-0"
CONTINUOUS_GLOBAL_CACHE_NAMESPACE = "document-continuous-global-1-0"
CONTINUOUS_GLOBAL_MAX_TOKENS = 1600
CONTINUOUS_GLOBAL_OUTPUT_MAX_CHARS = 5000

_CLAIM_TERMS = (
    "诊断",
    "确诊",
    "治疗",
    "建议",
    "必须",
    "一定",
    "必然",
    "完全",
    "因此",
    "从而",
    "促进",
    "证明",
    "意味着",
    "导致",
    "可能",
    "有助于",
)


class FactCardCompositionError(RuntimeError):
    """事实卡片原型输入、响应或本地派生失败。"""


def _fail(message: str) -> None:
    raise FactCardCompositionError(message)


def build_fact_cards(chapter_input: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把v1必覆盖字段确定性转换为事实卡片；来源账本不交给模型。"""

    chapter_id = str(chapter_input["chapter_id"])
    cards: list[dict[str, Any]] = []
    for field in chapter_input["source_fields"]:
        if not field["coverage_required"]:
            continue
        cards.append(
            {
                "fact_id": f"{chapter_id}-f{len(cards) + 1:03d}",
                "text": str(field["text"]),
                "source_content_path": str(field["source_content_path"]),
                "source_event_ids": list(field["source_event_ids"]),
            }
        )
    if not cards:
        _fail("本章没有可用于编排的v1事实卡片。")
    return cards


def build_required_facts(chapter_input: Mapping[str, Any]) -> list[dict[str, Any]]:
    """只把v1知识点作为必覆盖事实；标题本地继承，摘要不进入覆盖。"""

    chapter_id = str(chapter_input["chapter_id"])
    facts: list[dict[str, Any]] = []
    for field in chapter_input["source_fields"]:
        if field["kind"] != "key_point":
            continue
        facts.append(
            {
                "fact_id": f"{chapter_id}-f{len(facts) + 1:03d}",
                "text": str(field["text"]),
                "source_content_path": str(field["source_content_path"]),
                "source_event_ids": list(field["source_event_ids"]),
            }
        )
    if not facts:
        _fail("本章没有可用于编排的v1知识点。")
    return facts


def build_continuous_source_units(
    chapter_input: Mapping[str, Any],
    *,
    max_chars: int = CONTINUOUS_CONTENT_UNIT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """按事件边界把完整章内ASR确定性切成连续内容单元。"""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        _fail("内容单元字符上限必须是正整数。")
    chapter_id = str(chapter_input["chapter_id"])
    events = chapter_input["events"]
    if not isinstance(events, list) or not events:
        _fail("本章没有可用于编排的ASR事件。")
    units: list[dict[str, Any]] = []
    pending: list[Mapping[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        text = " ".join(str(event["text"]).strip() for event in pending)
        units.append(
            {
                "unit_id": f"{chapter_id}-u{len(units) + 1:03d}",
                "text": text,
                "source_event_ids": [str(event["id"]) for event in pending],
                "start": pending[0]["start"],
                "end": pending[-1]["end"],
            }
        )
        pending.clear()

    for event in events:
        event_text = str(event["text"]).strip()
        if not event_text:
            _fail("ASR事件text不能为空。")
        if len(event_text) > max_chars:
            _fail(f"单个ASR事件超过内容单元字符上限：{event['id']}。")
        candidate = " ".join(
            [str(item["text"]).strip() for item in pending] + [event_text]
        )
        if pending and len(candidate) > max_chars:
            flush()
        pending.append(event)
    flush()
    flattened = [event_id for unit in units for event_id in unit["source_event_ids"]]
    expected = [str(event["id"]) for event in events]
    if flattened != expected or len(flattened) != len(set(flattened)):
        _fail("连续内容单元发生ASR事件遗漏、重复或乱序。")
    if any(len(unit["text"]) > max_chars for unit in units):
        _fail("连续内容单元超过字符上限。")
    return units


def build_continuous_content_payload(
    composition_input: Mapping[str, Any],
    *,
    chapter_index: int,
    unit_max_chars: int = CONTINUOUS_CONTENT_UNIT_MAX_CHARS,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """构造模型可见的四字段载荷，并保留本地事实/单元账本。"""

    chapters = composition_input["chapters"]
    if (
        isinstance(chapter_index, bool)
        or not isinstance(chapter_index, int)
        or not 1 <= chapter_index <= len(chapters)
    ):
        _fail("chapter_index超出范围。")
    chapter = chapters[chapter_index - 1]
    facts = build_required_facts(chapter)
    units = build_continuous_source_units(chapter, max_chars=unit_max_chars)
    title_fields = [
        field for field in chapter["source_fields"] if field["kind"] == "title"
    ]
    if len(title_fields) != 1:
        _fail("本章必须恰好有一个本地继承的v1标题。")
    payload = {
        "title": str(title_fields[0]["text"]),
        "profile": str(composition_input["document_profile"]),
        "facts": [
            {"fact_id": fact["fact_id"], "text": fact["text"]} for fact in facts
        ],
        "units": [
            {"unit_id": unit["unit_id"], "text": unit["text"]} for unit in units
        ],
    }
    return payload, facts, units


def continuous_content_messages(
    payload: Mapping[str, Any], *, compact_recovery: bool = False
) -> list[dict[str, str]]:
    """冻结请求A将使用的消息；不读取配置或构造transport。"""

    if set(payload) != {"title", "profile", "facts", "units"}:
        _fail("连续正文外发载荷必须只含title、profile、facts和units。")
    compact_rule = (
        "最多返回5段，全部正文合计不超过1400个汉字；综合相近内容，"
        "不要沿着units逐句扩写。"
        if compact_recovery
        else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "仅依据给定facts和连续ASR units写成可连续阅读的简体中文章节正文。"
                "不得使用外部知识，不得新增或强化数字、实体、诊断、否定、建议、因果和结论。"
                "标题仅供理解，不要返回标题；不要机械逐条复述facts。只返回单个JSON对象："
                '{"paragraphs":[{"source_unit_ids":["c0001-u001"],'
                '"fact_ids":["c0001-f001"],"text":"连续正文"}]}。'
                "每个fact_id必须在全部段落中恰好出现一次；source_unit_ids只引用直接支持本段的单元。"
                "不得返回事件ID、来源路径、coverage、时间或证据索引。"
                + compact_rule
            ),
        },
        {
            "role": "user",
            "content": "CONTINUOUS_CONTENT_INPUT_JSON="
            + json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")),
        },
    ]


def build_continuous_content_dry_run(
    composition_input: Mapping[str, Any],
    *,
    chapter_index: int,
    unit_max_chars: int = CONTINUOUS_CONTENT_UNIT_MAX_CHARS,
) -> dict[str, Any]:
    """返回精确的零API请求预览；调用方只能检查或展示，不会写产物。"""

    payload, facts, units = build_continuous_content_payload(
        composition_input,
        chapter_index=chapter_index,
        unit_max_chars=unit_max_chars,
    )
    messages = continuous_content_messages(payload)
    serialized_payload = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )
    return {
        "prompt_version": CONTINUOUS_CONTENT_PROMPT_VERSION,
        "unit_version": CONTINUOUS_CONTENT_UNIT_VERSION,
        "validation_id": CONTINUOUS_CONTENT_VALIDATION_ID,
        "payload": payload,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": CONTINUOUS_CONTENT_MAX_TOKENS,
        "payload_chars": len(serialized_payload),
        "message_chars": sum(len(message["content"]) for message in messages),
        "fact_count": len(facts),
        "unit_count": len(units),
        "api_request_count": 0,
    }


def build_continuous_batch_payload(
    composition_input: Mapping[str, Any],
    *,
    chapter_indices: Sequence[int] = (2, 3, 4, 5),
) -> tuple[
    dict[str, Any],
    dict[str, tuple[Mapping[str, Any], list[dict[str, Any]], list[dict[str, Any]]]],
]:
    """构造第2—5章单次批量载荷和本地账本。"""

    if not chapter_indices or len(chapter_indices) != len(set(chapter_indices)):
        _fail("批量章节索引必须非空且唯一。")
    ledgers: dict[
        str, tuple[Mapping[str, Any], list[dict[str, Any]], list[dict[str, Any]]]
    ] = {}
    chapters: list[dict[str, Any]] = []
    for chapter_index in chapter_indices:
        chapter_payload, facts, units = build_continuous_content_payload(
            composition_input,
            chapter_index=chapter_index,
        )
        chapter = composition_input["chapters"][chapter_index - 1]
        chapter_id = str(chapter["chapter_id"])
        chapters.append(
            {
                "chapter_id": chapter_id,
                "title": chapter_payload["title"],
                "facts": chapter_payload["facts"],
                "units": chapter_payload["units"],
            }
        )
        ledgers[chapter_id] = (chapter, facts, units)
    return {
        "profile": str(composition_input["document_profile"]),
        "chapters": chapters,
    }, ledgers


def continuous_batch_messages(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    if set(payload) != {"profile", "chapters"}:
        _fail("批量外发载荷必须只含profile和chapters。")
    return [
        {
            "role": "system",
            "content": (
                "分别依据每章给定的facts和连续ASR units写成可连续阅读的简体中文正文。"
                "各章禁止混用信息，不得使用外部知识，不得新增或强化数字、实体、诊断、建议和结论。"
                "不要机械逐条复述facts。只返回单个JSON对象："
                '{"chapters":[{"chapter_id":"c0002","paragraphs":['
                '{"source_unit_ids":["c0002-u001"],"fact_ids":["c0002-f001"],'
                '"text":"连续正文"}]}]}。'
                "每章每个fact_id必须恰好出现一次；source_unit_ids只引用本章直接支持该段的单元。"
                "不得返回标题、事件ID、来源路径、coverage、时间或证据索引。"
            ),
        },
        {
            "role": "user",
            "content": "CONTINUOUS_BATCH_INPUT_JSON="
            + json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")),
        },
    ]


def validate_continuous_batch_response(
    response: Any,
    *,
    ledgers: Mapping[
        str, tuple[Mapping[str, Any], Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]
    ],
) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) != {"chapters"}:
        _fail("批量响应必须是只含chapters的JSON对象。")
    chapters = response["chapters"]
    if not isinstance(chapters, list):
        _fail("批量响应chapters必须是数组。")
    expected_ids = list(ledgers)
    actual_ids = [item.get("chapter_id") if isinstance(item, dict) else None for item in chapters]
    if actual_ids != expected_ids:
        _fail("批量响应章节必须完整且按输入顺序返回。")
    normalized: list[dict[str, Any]] = []
    for item in chapters:
        if set(item) != {"chapter_id", "paragraphs"}:
            _fail("批量响应章节字段无效。")
        chapter_id = str(item["chapter_id"])
        _, facts, units = ledgers[chapter_id]
        validated = validate_continuous_content_response(
            {"paragraphs": item["paragraphs"]},
            required_facts=facts,
            source_units=units,
        )
        normalized.append(
            {"chapter_id": chapter_id, "paragraphs": validated["paragraphs"]}
        )
    return {"chapters": normalized}


def derive_continuous_batch_evidence(
    response: Mapping[str, Any],
    *,
    ledgers: Mapping[
        str, tuple[Mapping[str, Any], Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]
    ],
) -> dict[str, Any]:
    normalized = validate_continuous_batch_response(response, ledgers=ledgers)
    chapters: list[dict[str, Any]] = []
    for item in normalized["chapters"]:
        chapter_id = item["chapter_id"]
        chapter, facts, units = ledgers[chapter_id]
        chapters.append(
            {
                "chapter_id": chapter_id,
                **derive_continuous_content_evidence(
                    {"paragraphs": item["paragraphs"]},
                    chapter_input=chapter,
                    required_facts=facts,
                    source_units=units,
                ),
            }
        )
    return {"chapters": chapters}


def build_continuous_global_payload(
    candidate: Mapping[str, Any], *, document_profile: str
) -> dict[str, Any]:
    """请求C只暴露五章已验证标题与正文，不暴露ASR或内部证据。"""

    chapters = candidate.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        _fail("全文候选chapters必须是非空数组。")
    visible: list[dict[str, Any]] = []
    for index, chapter in enumerate(chapters, start=1):
        expected_id = f"c{index:04d}"
        if not isinstance(chapter, dict) or chapter.get("chapter_id") != expected_id:
            _fail("全文候选章节必须完整且连续。")
        title = chapter.get("title")
        paragraphs = chapter.get("paragraphs")
        if not isinstance(title, str) or not title.strip():
            _fail(f"{expected_id}标题不能为空。")
        if not isinstance(paragraphs, list) or not paragraphs:
            _fail(f"{expected_id}正文不能为空。")
        texts = [item.get("text") if isinstance(item, dict) else None for item in paragraphs]
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            _fail(f"{expected_id}正文段落无效。")
        visible.append(
            {
                "chapter_id": expected_id,
                "title": title.strip(),
                "paragraphs": [str(text).strip() for text in texts],
            }
        )
    return {"profile": str(document_profile), "chapters": visible}


def continuous_global_messages(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "只根据给定的五章已验证正文生成全文级中文字段，不得补充外部知识，"
                "不得改写或返回章节。只返回单个JSON对象，字段必须严格为："
                '{"title":"全文标题","overview":["概述第1段","可选第2段"],'
                '"conclusions":["核心结论第1段","可选第2段"]}。'
                "overview必须1至2段，conclusions必须1至2段；不得返回ID、时间、证据或Markdown。"
            ),
        },
        {
            "role": "user",
            "content": "CONTINUOUS_GLOBAL_INPUT_JSON="
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def validate_continuous_global_response(
    response: Any, *, payload: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) != {
        "title",
        "overview",
        "conclusions",
    }:
        _fail("请求C响应字段无效。")
    title = response["title"]
    overview = response["overview"]
    conclusions = response["conclusions"]
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
        _fail("请求C标题为空或过长。")
    if not isinstance(overview, list) or not 1 <= len(overview) <= 2:
        _fail("请求C概述必须为1至2段。")
    if not isinstance(conclusions, list) or not 1 <= len(conclusions) <= 2:
        _fail("请求C核心结论必须为1至2段。")
    texts = [title, *overview, *conclusions]
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        _fail("请求C全文字段不得包含空文本。")
    normalized = {
        "title": title.strip(),
        "overview": [str(text).strip() for text in overview],
        "conclusions": [str(text).strip() for text in conclusions],
    }
    if sum(len(text) for text in texts) > CONTINUOUS_GLOBAL_OUTPUT_MAX_CHARS:
        _fail("请求C全文字段超过字符上限。")
    # 展平五章正文；模型不提供也不可信任任何证据ID。
    source_text = "\n".join(
        str(item)
        for chapter in payload["chapters"]
        for item in [chapter["title"], *chapter["paragraphs"]]
    )
    for text in texts:
        _validate_continuous_hard_claims(str(text), [{"text": source_text}])
    return normalized


def build_continuous_global_dry_run(
    candidate: Mapping[str, Any], *, document_profile: str
) -> dict[str, Any]:
    payload = build_continuous_global_payload(
        candidate, document_profile=document_profile
    )
    messages = continuous_global_messages(payload)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "prompt_version": CONTINUOUS_GLOBAL_PROMPT_VERSION,
        "validation_id": CONTINUOUS_GLOBAL_VALIDATION_ID,
        "payload": payload,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": CONTINUOUS_GLOBAL_MAX_TOKENS,
        "payload_chars": len(serialized),
        "message_chars": sum(len(item["content"]) for item in messages),
        "chapter_count": len(payload["chapters"]),
        "paragraph_count": sum(len(item["paragraphs"]) for item in payload["chapters"]),
        "api_request_count": 0,
    }


def build_continuous_batch_dry_run(
    composition_input: Mapping[str, Any],
) -> dict[str, Any]:
    payload, ledgers = build_continuous_batch_payload(composition_input)
    messages = continuous_batch_messages(payload)
    serialized_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "prompt_version": CONTINUOUS_BATCH_PROMPT_VERSION,
        "validation_id": CONTINUOUS_BATCH_VALIDATION_ID,
        "payload": payload,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": CONTINUOUS_BATCH_MAX_TOKENS,
        "payload_chars": len(serialized_payload),
        "message_chars": sum(len(message["content"]) for message in messages),
        "chapter_count": len(ledgers),
        "fact_count": sum(len(facts) for _, facts, _ in ledgers.values()),
        "unit_count": sum(len(units) for _, _, units in ledgers.values()),
        "api_request_count": 0,
    }


def fact_card_messages(
    chapter_input: Mapping[str, Any],
    fact_cards: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    payload = {
        "chapter_id": chapter_input["chapter_id"],
        "planned_title": chapter_input["planned_title"],
        "fact_cards": [
            {"fact_id": card["fact_id"], "text": card["text"]}
            for card in fact_cards
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "只把给定事实卡片编排成可连续阅读的中文正文，不得使用外部知识，"
                "不得增加诊断、结论、建议、背景、因果或事实强度。模型不得返回来源路径、"
                "事件ID、coverage、时间范围或证据索引。只返回单个JSON对象："
                '{"sections":[{"heading":null,"blocks":['
                '{"type":"paragraph","fact_ids":["c0001-f001"],"text":"连续正文"}'
                ']}],"omitted_duplicates":['
                '{"fact_id":"c0001-f002","represented_by":"c0001-f001"}]}。'
                "本原型heading固定为null，只使用paragraph；每个事实卡片必须且只能被正文块引用一次，"
                "或在确属重复时列入omitted_duplicates一次。连接语只能改善衔接，不能承载新事实。"
            ),
        },
        {
            "role": "user",
            "content": "FACT_CARD_INPUT_JSON="
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _validate_block_claims(text: str, cards: Sequence[Mapping[str, Any]]) -> None:
    source_text = "\n".join(str(card["text"]) for card in cards)
    output_tokens = _high_risk_tokens(text)
    source_tokens = _high_risk_tokens(source_text)
    for category, tokens in output_tokens.items():
        unsupported = tokens - source_tokens[category]
        if unsupported:
            _fail(f"正文出现事实卡片未支持的{category}：{sorted(unsupported)}。")
    unsupported_terms = [
        term for term in _CLAIM_TERMS if term in text and term not in source_text
    ]
    if unsupported_terms:
        _fail(f"正文出现事实卡片未支持的判断词：{unsupported_terms}。")


def _validate_continuous_hard_claims(
    text: str,
    sources: Sequence[Mapping[str, Any]],
) -> None:
    """连续正文只硬拦截可确定比较的数字与显式实体漂移。"""

    source_text = "\n".join(str(source["text"]) for source in sources)
    output_tokens = _high_risk_tokens(text)
    source_tokens = _high_risk_tokens(source_text)
    for category in ("数字/比例", "拉丁实体", "引号实体"):
        unsupported = output_tokens[category] - source_tokens[category]
        if category == "引号实体":
            # 引号样式不是事实身份的一部分；只要引号内原文确实存在于来源即可。
            unsupported = {token for token in unsupported if token not in source_text}
        if unsupported:
            _fail(f"正文出现来源未支持的{category}：{sorted(unsupported)}。")


def _continuous_review_flags(
    text: str,
    sources: Sequence[Mapping[str, Any]],
    *,
    paragraph_index: int,
) -> list[dict[str, Any]]:
    """把因果、否定、模态和建议词降为人工复核提示，不判整章失败。"""

    source_text = "\n".join(str(source["text"]) for source in sources)
    output_tokens = _high_risk_tokens(text)
    source_tokens = _high_risk_tokens(source_text)
    phrases = set(
        output_tokens["否定/模态/建议"] - source_tokens["否定/模态/建议"]
    )
    phrases.update(
        term for term in _CLAIM_TERMS if term in text and term not in source_text
    )
    return [
        {
            "paragraph_index": paragraph_index,
            "category": "人工复核表达",
            "phrase": phrase,
        }
        for phrase in sorted(phrases)
    ]


def validate_fact_card_response(
    response: Any,
    *,
    fact_cards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) != {
        "sections",
        "omitted_duplicates",
    }:
        _fail("事实卡片响应必须是只含sections和omitted_duplicates的JSON对象。")
    cards = {str(card["fact_id"]): card for card in fact_cards}
    chapter_id = str(fact_cards[0]["fact_id"]).rsplit("-f", 1)[0]
    sections = response["sections"]
    if not isinstance(sections, list) or not 1 <= len(sections) <= 4:
        _fail("sections必须包含1至4项。")
    used_by: dict[str, str] = {}
    block_ids: list[str] = []
    for section_index, section in enumerate(sections, start=1):
        if not isinstance(section, dict) or set(section) != {"heading", "blocks"}:
            _fail("section字段无效。")
        if section["heading"] is not None:
            _fail("第1章最小原型不允许模型生成小标题。")
        blocks = section["blocks"]
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 4:
            _fail("每节blocks必须包含1至4项。")
        for block_index, block in enumerate(blocks, start=1):
            if not isinstance(block, dict) or set(block) != {"type", "fact_ids", "text"}:
                _fail("正文块字段无效。")
            if block["type"] != "paragraph":
                _fail("第1章最小原型只允许paragraph。")
            text = block["text"]
            fact_ids = block["fact_ids"]
            if not isinstance(text, str) or not text.strip():
                _fail("正文块text不能为空。")
            if (
                not isinstance(fact_ids, list)
                or not fact_ids
                or len(fact_ids) != len(set(fact_ids))
            ):
                _fail("正文块fact_ids必须非空且唯一。")
            unknown = [fact_id for fact_id in fact_ids if fact_id not in cards]
            if unknown:
                _fail(f"正文块引用未知或章外fact_id：{unknown}。")
            block_id = f"{chapter_id}-s{section_index:03d}-p{block_index:03d}"
            block_ids.append(block_id)
            for fact_id in fact_ids:
                if fact_id in used_by:
                    _fail("同一事实卡片不得被多个正文块重复消费。")
                used_by[fact_id] = block_id
            _validate_block_claims(text, [cards[fact_id] for fact_id in fact_ids])

    omitted = response["omitted_duplicates"]
    if not isinstance(omitted, list):
        _fail("omitted_duplicates必须是数组。")
    omitted_by: dict[str, str] = {}
    for item in omitted:
        if not isinstance(item, dict) or set(item) != {"fact_id", "represented_by"}:
            _fail("omitted_duplicates项字段无效。")
        fact_id = item["fact_id"]
        represented_by = item["represented_by"]
        if fact_id not in cards or represented_by not in used_by or fact_id in omitted_by:
            _fail("重复省略引用未知、未呈现或重复fact_id。")
        if fact_id in used_by or fact_id == represented_by:
            _fail("已呈现事实不得同时标为重复省略。")
        if not set(cards[fact_id]["source_event_ids"]).intersection(
            cards[represented_by]["source_event_ids"]
        ):
            _fail("重复省略的两张事实卡片没有重合事件证据。")
        omitted_by[fact_id] = represented_by
    missing = set(cards) - set(used_by) - set(omitted_by)
    if missing:
        _fail(f"事实卡片未覆盖：{sorted(missing)}。")
    return dict(response)


def derive_fact_card_evidence(
    response: Mapping[str, Any],
    *,
    chapter_input: Mapping[str, Any],
    fact_cards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """只由fact_id本地派生来源、事件、时间和coverage。"""

    validate_fact_card_response(response, fact_cards=fact_cards)
    cards = {str(card["fact_id"]): card for card in fact_cards}
    event_times = {
        str(event["id"]): (event["start"], event["end"])
        for event in chapter_input["events"]
    }
    represented: dict[str, str] = {}
    blocks: list[dict[str, Any]] = []
    for section_index, section in enumerate(response["sections"], start=1):
        for block_index, block in enumerate(section["blocks"], start=1):
            block_id = f"{chapter_input['chapter_id']}-s{section_index:03d}-p{block_index:03d}"
            fact_ids = list(block["fact_ids"])
            paths = [cards[fact_id]["source_content_path"] for fact_id in fact_ids]
            event_ids: list[str] = []
            for fact_id in fact_ids:
                represented[fact_id] = block_id
                for event_id in cards[fact_id]["source_event_ids"]:
                    if event_id not in event_ids:
                        event_ids.append(event_id)
            starts = [event_times[event_id][0] for event_id in event_ids]
            ends = [event_times[event_id][1] for event_id in event_ids]
            blocks.append(
                {
                    "block_id": block_id,
                    "type": "paragraph",
                    "fact_ids": fact_ids,
                    "text": block["text"],
                    "source_content_paths": paths,
                    "source_event_ids": event_ids,
                    "start": min(starts),
                    "end": max(ends),
                }
            )
    omitted = {
        item["fact_id"]: item["represented_by"]
        for item in response["omitted_duplicates"]
    }
    coverage = []
    for card in fact_cards:
        fact_id = card["fact_id"]
        represented_fact = omitted.get(fact_id, fact_id)
        coverage.append(
            {
                "fact_id": fact_id,
                "status": "omitted_duplicate" if fact_id in omitted else "represented",
                "target_block_id": represented[represented_fact],
            }
        )
    return {"blocks": blocks, "coverage": coverage}


def validate_continuous_content_response(
    response: Any,
    *,
    required_facts: Sequence[Mapping[str, Any]],
    source_units: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """校验最小章级响应；不要求固定段落数量或措辞风格。"""

    if not isinstance(response, dict) or set(response) != {"paragraphs"}:
        _fail("连续正文响应必须是只含paragraphs的JSON对象。")
    paragraphs = response["paragraphs"]
    if not isinstance(paragraphs, list) or not paragraphs:
        _fail("paragraphs必须是非空数组。")
    facts = {str(fact["fact_id"]): fact for fact in required_facts}
    units = {str(unit["unit_id"]): unit for unit in source_units}
    unit_order = {unit_id: index for index, unit_id in enumerate(units)}
    used_facts: list[str] = []
    total_chars = 0
    normalized: list[dict[str, Any]] = []
    for index, paragraph in enumerate(paragraphs, start=1):
        if not isinstance(paragraph, dict) or set(paragraph) != {
            "source_unit_ids",
            "fact_ids",
            "text",
        }:
            _fail(f"paragraphs[{index}]字段无效。")
        text = paragraph["text"]
        fact_ids = paragraph["fact_ids"]
        unit_ids = paragraph["source_unit_ids"]
        if not isinstance(text, str) or not text.strip():
            _fail(f"paragraphs[{index}].text不能为空。")
        if not isinstance(fact_ids, list) or len(fact_ids) != len(set(fact_ids)):
            _fail(f"paragraphs[{index}].fact_ids必须是唯一数组。")
        if (
            not isinstance(unit_ids, list)
            or not unit_ids
            or len(unit_ids) != len(set(unit_ids))
        ):
            _fail(f"paragraphs[{index}].source_unit_ids必须是非空唯一数组。")
        unknown_facts = [fact_id for fact_id in fact_ids if fact_id not in facts]
        unknown_units = [unit_id for unit_id in unit_ids if unit_id not in units]
        if unknown_facts or unknown_units:
            _fail(
                f"paragraphs[{index}]引用未知或章外ID："
                f"facts={unknown_facts}, units={unknown_units}。"
            )
        positions = [unit_order[unit_id] for unit_id in unit_ids]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            _fail(f"paragraphs[{index}].source_unit_ids必须按相邻单元顺序排列。")
        duplicated_facts = [fact_id for fact_id in fact_ids if fact_id in used_facts]
        if duplicated_facts:
            _fail(f"必覆盖fact_id不得重复使用：{duplicated_facts}。")
        used_facts.extend(fact_ids)
        total_chars += len(text.strip())
        _validate_continuous_hard_claims(
            text,
            [facts[fact_id] for fact_id in fact_ids]
            + [units[unit_id] for unit_id in unit_ids],
        )
        normalized.append(
            {
                "source_unit_ids": list(unit_ids),
                "fact_ids": list(fact_ids),
                "text": text.strip(),
            }
        )
    missing = [fact_id for fact_id in facts if fact_id not in used_facts]
    if missing:
        _fail(f"v1知识点未覆盖：{missing}。")
    if total_chars > CONTINUOUS_CONTENT_OUTPUT_MAX_CHARS:
        _fail("连续正文超过总字符上限。")
    return {"paragraphs": normalized}


def review_continuous_content_response(
    response: Any,
    *,
    required_facts: Sequence[Mapping[str, Any]],
    source_units: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """在硬校验通过后生成非阻断人工复核标记。"""

    normalized = validate_continuous_content_response(
        response,
        required_facts=required_facts,
        source_units=source_units,
    )
    facts = {str(fact["fact_id"]): fact for fact in required_facts}
    units = {str(unit["unit_id"]): unit for unit in source_units}
    flags: list[dict[str, Any]] = []
    for index, paragraph in enumerate(normalized["paragraphs"], start=1):
        flags.extend(
            _continuous_review_flags(
                paragraph["text"],
                [facts[fact_id] for fact_id in paragraph["fact_ids"]]
                + [units[unit_id] for unit_id in paragraph["source_unit_ids"]],
                paragraph_index=index,
            )
        )
    return flags


def derive_continuous_content_evidence(
    response: Mapping[str, Any],
    *,
    chapter_input: Mapping[str, Any],
    required_facts: Sequence[Mapping[str, Any]],
    source_units: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """仅由本地fact/unit账本派生块证据、时间和v1知识点coverage。"""

    normalized = validate_continuous_content_response(
        response,
        required_facts=required_facts,
        source_units=source_units,
    )
    facts = {str(fact["fact_id"]): fact for fact in required_facts}
    units = {str(unit["unit_id"]): unit for unit in source_units}
    chapter_event_order = {
        str(event["id"]): index for index, event in enumerate(chapter_input["events"])
    }
    event_times = {
        str(event["id"]): (event["start"], event["end"])
        for event in chapter_input["events"]
    }
    represented: dict[str, str] = {}
    blocks: list[dict[str, Any]] = []
    for index, paragraph in enumerate(normalized["paragraphs"], start=1):
        block_id = f"{chapter_input['chapter_id']}-s001-p{index:03d}"
        fact_ids = list(paragraph["fact_ids"])
        unit_ids = list(paragraph["source_unit_ids"])
        event_ids = {
            event_id
            for fact_id in fact_ids
            for event_id in facts[fact_id]["source_event_ids"]
        }
        event_ids.update(
            event_id
            for unit_id in unit_ids
            for event_id in units[unit_id]["source_event_ids"]
        )
        ordered_event_ids = sorted(event_ids, key=chapter_event_order.__getitem__)
        for fact_id in fact_ids:
            represented[fact_id] = block_id
        blocks.append(
            {
                "block_id": block_id,
                "type": "paragraph",
                "fact_ids": fact_ids,
                "source_unit_ids": unit_ids,
                "text": paragraph["text"],
                "source_content_paths": [
                    facts[fact_id]["source_content_path"] for fact_id in fact_ids
                ],
                "source_event_ids": ordered_event_ids,
                "start": min(event_times[event_id][0] for event_id in ordered_event_ids),
                "end": max(event_times[event_id][1] for event_id in ordered_event_ids),
            }
        )
    coverage = [
        {
            "fact_id": fact["fact_id"],
            "source_content_path": fact["source_content_path"],
            "status": "represented",
            "target_block_id": represented[fact["fact_id"]],
        }
        for fact in required_facts
    ]
    return {
        "blocks": blocks,
        "coverage": coverage,
        "review_flags": review_continuous_content_response(
            normalized,
            required_facts=required_facts,
            source_units=source_units,
        ),
    }


def build_continuous_reviewed_chapter_result(
    composition_input: Mapping[str, Any],
    candidate: Mapping[str, Any],
):
    """把已离线复核的最小段落确定性适配为2.0章级对象。"""

    from video_doc.document_chapter_composition import (
        OfflineChapterCompositionResult,
        ValidatedChapterComposition,
    )

    chapters = candidate.get("chapters")
    if not isinstance(chapters, list) or len(chapters) != len(composition_input["chapters"]):
        _fail("离线候选章节数与编排输入不一致。")
    validated_chapters = []
    for index, (chapter_input, candidate_chapter) in enumerate(
        zip(composition_input["chapters"], chapters), start=1
    ):
        payload, facts, units = build_continuous_content_payload(
            composition_input, chapter_index=index
        )
        if (
            candidate_chapter.get("chapter_id") != chapter_input["chapter_id"]
            or candidate_chapter.get("title") != payload["title"]
        ):
            _fail("离线候选章节身份或标题与权威输入不一致。")
        derived = derive_continuous_content_evidence(
            {"paragraphs": candidate_chapter["paragraphs"]},
            chapter_input=chapter_input,
            required_facts=facts,
            source_units=units,
        )
        title_field = next(
            field for field in chapter_input["source_fields"] if field["kind"] == "title"
        )
        keyword_fields = [
            field for field in chapter_input["source_fields"] if field["kind"] == "keyword"
        ][:6]
        event_order = {
            str(event["id"]): position
            for position, event in enumerate(chapter_input["events"])
        }
        blocks: list[dict[str, Any]] = []
        block_evidence: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        fact_targets = {
            item["source_content_path"]: item["target_block_id"]
            for item in derived["coverage"]
        }
        for block_index, item in enumerate(derived["blocks"]):
            paths = list(item["source_content_paths"])
            event_ids = list(item["source_event_ids"])
            if block_index == 0:
                paths.insert(0, str(title_field["source_content_path"]))
                event_ids = sorted(
                    set(event_ids) | set(title_field["source_event_ids"]),
                    key=event_order.__getitem__,
                )
            block = {
                "block_id": item["block_id"],
                "type": "paragraph",
                "role": "explanation",
                "text": item["text"],
                "content_origin": "stage9_v1",
                "source_content_paths": paths,
                "source_event_ids": event_ids,
            }
            blocks.append(block)
            block_evidence[block["block_id"]] = (tuple(paths), tuple(event_ids))
        block_ids = [block["block_id"] for block in blocks]
        coverage_items: list[dict[str, Any]] = []
        for field in chapter_input["source_fields"]:
            if not field["coverage_required"]:
                continue
            path = str(field["source_content_path"])
            if field["kind"] == "title":
                status, targets = "represented", [block_ids[0]]
            elif field["kind"] == "summary":
                status, targets = "omitted_duplicate", block_ids
            else:
                status, targets = "represented", [fact_targets[path]]
            coverage_items.append(
                {
                    "source_content_path": path,
                    "status": status,
                    "target_block_ids": targets,
                }
            )
        evidence = lambda field: {
            "text": field["text"],
            "content_origin": "stage9_v1",
            "source_content_paths": [field["source_content_path"]],
            "source_event_ids": list(field["source_event_ids"]),
        }
        chapter = {
            "chapter_id": chapter_input["chapter_id"],
            "index": chapter_input["index"],
            "start": chapter_input["start"],
            "end": chapter_input["end"],
            "planned_title": chapter_input["planned_title"],
            "title": evidence(title_field),
            "keywords": [evidence(field) for field in keyword_fields],
            "sections": [
                {
                    "section_id": f"{chapter_input['chapter_id']}-s001",
                    "heading": None,
                    "blocks": blocks,
                }
            ],
        }
        validated_chapters.append(
            ValidatedChapterComposition(
                chapter=chapter,
                coverage={"items": coverage_items},
                block_evidence=block_evidence,
            )
        )
    return OfflineChapterCompositionResult(
        input_id=str(composition_input["input_id"]),
        chapters=tuple(validated_chapters),
        business_call_count=2,
        supplier_request_count=2,
        total_attempt_count=2,
        cache_hit_count=0,
        all_cache_hit=False,
        call_paths=(),
    )


def compose_continuous_content_chapter_once(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    chapter_index: int,
    config: LLMConfig,
    transport: LLMTransport,
    compact_recovery: bool = False,
) -> tuple[
    LLMCallResult,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """执行一次受限章级请求；供应商请求硬上限为1且不生成preview。"""

    if config.backend != "openai_compatible":
        _fail("连续正文真实请求必须使用openai_compatible配置。")
    payload, facts, units = build_continuous_content_payload(
        composition_input,
        chapter_index=chapter_index,
    )
    chapter = composition_input["chapters"][chapter_index - 1]

    def validator(response: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_continuous_content_response(
                response,
                required_facts=facts,
                source_units=units,
            )
        except FactCardCompositionError as exc:
            raise LLMRetryableError(str(exc)) from exc

    bounded_config = replace(config, max_tokens=CONTINUOUS_CONTENT_MAX_TOKENS)
    result = call_task_llm(
        output_root,
        task_id,
        call_id=(
            f"continuous-content-{chapter['chapter_id']}-compact"
            if compact_recovery
            else f"continuous-content-{chapter['chapter_id']}"
        ),
        messages=continuous_content_messages(
            payload, compact_recovery=compact_recovery
        ),
        response_format={"type": "json_object"},
        config=bounded_config,
        transport=transport,
        response_validator=validator,
        validation_id=(
            CONTINUOUS_CONTENT_COMPACT_VALIDATION_ID
            if compact_recovery
            else CONTINUOUS_CONTENT_VALIDATION_ID
        ),
        artifact_subdir=(
            Path("content")
            / "document-v2"
            / "attempts"
            / composition_input["input_id"]
            / "continuous-content-v1"
            / chapter["chapter_id"]
            / (
                Path("compact-recovery") / "calls"
                if compact_recovery
                else Path("calls")
            )
        ),
        cache_namespace=(
            CONTINUOUS_CONTENT_COMPACT_CACHE_NAMESPACE
            if compact_recovery
            else CONTINUOUS_CONTENT_CACHE_NAMESPACE
        ),
        save_validation_audit=True,
        max_attempts=1,
    )
    derived = derive_continuous_content_evidence(
        result.response,
        chapter_input=chapter,
        required_facts=facts,
        source_units=units,
    )
    return result, facts, units, derived


def compose_continuous_batch_once(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    config: LLMConfig,
    transport: LLMTransport,
) -> tuple[LLMCallResult, dict[str, Any]]:
    """一次生成第2—5章；无重试且不生成preview。"""

    if config.backend != "openai_compatible":
        _fail("连续正文批量真实请求必须使用openai_compatible配置。")
    payload, ledgers = build_continuous_batch_payload(composition_input)

    def validator(response: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_continuous_batch_response(response, ledgers=ledgers)
        except FactCardCompositionError as exc:
            raise LLMRetryableError(str(exc)) from exc

    result = call_task_llm(
        output_root,
        task_id,
        call_id="continuous-content-c0002-c0005",
        messages=continuous_batch_messages(payload),
        response_format={"type": "json_object"},
        config=replace(config, max_tokens=CONTINUOUS_BATCH_MAX_TOKENS),
        transport=transport,
        response_validator=validator,
        validation_id=CONTINUOUS_BATCH_VALIDATION_ID,
        artifact_subdir=(
            Path("content")
            / "document-v2"
            / "attempts"
            / composition_input["input_id"]
            / "continuous-content-v1"
            / "batch-c0002-c0005"
            / "calls"
        ),
        cache_namespace=CONTINUOUS_BATCH_CACHE_NAMESPACE,
        save_validation_audit=True,
        max_attempts=1,
    )
    return result, derive_continuous_batch_evidence(result.response, ledgers=ledgers)


def compose_continuous_global_once(
    output_root: str | Path,
    task_id: str,
    candidate: Mapping[str, Any],
    *,
    document_profile: str,
    input_id: str,
    config: LLMConfig,
    transport: LLMTransport,
) -> LLMCallResult:
    """请求C：只发送已验证章节正文，一次请求、禁止重试。"""

    if config.backend != "openai_compatible":
        _fail("连续正文全文真实请求必须使用openai_compatible配置。")
    payload = build_continuous_global_payload(
        candidate, document_profile=document_profile
    )

    def validator(response: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_continuous_global_response(response, payload=payload)
        except FactCardCompositionError as exc:
            raise LLMRetryableError(str(exc)) from exc

    return call_task_llm(
        output_root,
        task_id,
        call_id="continuous-content-global",
        messages=continuous_global_messages(payload),
        response_format={"type": "json_object"},
        config=replace(config, max_tokens=CONTINUOUS_GLOBAL_MAX_TOKENS),
        transport=transport,
        response_validator=validator,
        validation_id=CONTINUOUS_GLOBAL_VALIDATION_ID,
        artifact_subdir=(
            Path("content")
            / "document-v2"
            / "attempts"
            / input_id
            / "continuous-content-v1"
            / "global"
            / "calls"
        ),
        cache_namespace=CONTINUOUS_GLOBAL_CACHE_NAMESPACE,
        save_validation_audit=True,
        max_attempts=1,
    )


def derive_continuous_global_evidence(
    response: Mapping[str, Any], *, payload: Mapping[str, Any], chapter_result: Any
) -> dict[str, Any]:
    """把请求C的纯文本响应映射为本地证据化的2.0全文字段。"""

    normalized = validate_continuous_global_response(response, payload=payload)

    def ordered_unique(groups: Sequence[Sequence[str]]) -> list[str]:
        seen: set[str] = set()
        values: list[str] = []
        for group in groups:
            for item in group:
                if item not in seen:
                    seen.add(item)
                    values.append(item)
        return values

    title_paths = ordered_unique(
        [chapter.chapter["title"]["source_content_paths"] for chapter in chapter_result.chapters]
    )
    title_events = ordered_unique(
        [chapter.chapter["title"]["source_event_ids"] for chapter in chapter_result.chapters]
    )
    body_paths = ordered_unique(
        [
            list(paths)
            for chapter in chapter_result.chapters
            for paths, _ in chapter.block_evidence.values()
        ]
    )
    body_events = ordered_unique(
        [
            list(events)
            for chapter in chapter_result.chapters
            for _, events in chapter.block_evidence.values()
        ]
    )
    # DEV/正式事件ID固定零填充；跨段证据有重叠时必须恢复全局时间顺序。
    title_events.sort()
    body_events.sort()
    title = {
        "text": normalized["title"],
        "content_origin": "stage9_v1",
        "source_content_paths": title_paths,
        "source_event_ids": title_events,
    }
    evidence = {
        "content_origin": "stage9_v1",
        "source_content_paths": body_paths,
        "source_event_ids": body_events,
    }
    overview = [
        {
            "block_id": f"overview-p{index:03d}",
            "type": "paragraph",
            "role": "overview",
            "text": text,
            **evidence,
        }
        for index, text in enumerate(normalized["overview"], start=1)
    ]
    conclusions = [
        {
            "block_id": f"conclusion-p{index:03d}",
            "type": "paragraph",
            "role": "conclusion",
            "text": text,
            **evidence,
        }
        for index, text in enumerate(normalized["conclusions"], start=1)
    ]
    return {
        "schema_version": "2.0",
        "title": title,
        "overview": overview,
        "overall_conclusion": conclusions,
    }


def compose_fact_card_chapter_once(
    output_root: str | Path,
    task_id: str,
    composition_input: Mapping[str, Any],
    *,
    chapter_index: int,
    config: LLMConfig,
    transport: LLMTransport,
) -> tuple[LLMCallResult, list[dict[str, Any]], dict[str, Any]]:
    chapter = composition_input["chapters"][chapter_index - 1]
    cards = build_fact_cards(chapter)

    def validator(response: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_fact_card_response(response, fact_cards=cards)
        except FactCardCompositionError as exc:
            raise LLMRetryableError(str(exc)) from exc

    result = call_task_llm(
        output_root,
        task_id,
        call_id=f"fact-card-{chapter['chapter_id']}",
        messages=fact_card_messages(chapter, cards),
        response_format={"type": "json_object"},
        config=config,
        transport=transport,
        response_validator=validator,
        validation_id=FACT_CARD_VALIDATION_ID,
        artifact_subdir=(
            Path("content")
            / "document-v2"
            / "attempts"
            / composition_input["input_id"]
            / "fact-card-prototype"
            / chapter["chapter_id"]
            / "calls"
        ),
        cache_namespace=FACT_CARD_CACHE_NAMESPACE,
        save_validation_audit=True,
    )
    derived = derive_fact_card_evidence(
        result.response, chapter_input=chapter, fact_cards=cards
    )
    return result, cards, derived
