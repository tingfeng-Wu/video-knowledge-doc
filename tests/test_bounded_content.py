"""Bounded composition: actual scoped calls, strict coverage, replay and cache."""
import copy
import dataclasses
import json

import pytest

from tests.test_document_composer_v2 import (
    _chain, _compose, _ContentTransport, _responses, SINGLE_OK, GLOBAL_OK,
)
from tests.test_timeline_builder_v2 import TASK
from video_doc import document_composer_v2 as dc
from video_doc.chapter_frames import select_chapter_frames


def _long_chain(tmp_path, count):
    from tests.test_document_composer_v2 import _synth_chain, _plan, GOOD_RESPONSE, ScriptedTransport
    from video_doc.task_workspace import initialize_task_workspace
    from video_doc.timeline_builder_v2 import build_timeline_v2
    events = [(10.0, 20.0, "甲" * 500)] + [
        (90.0 + i * 5, 93.0 + i * 5, "乙" * 500) for i in range(count - 1)
    ]
    root = _synth_chain(tmp_path, m1_events=events, duration_seconds=600.0)
    initialize_task_workspace(root, TASK)
    build_timeline_v2(TASK, output_root=root, ocr="auto")
    _plan(root, ScriptedTransport([copy.deepcopy(GOOD_RESPONSE)]), tmp_path=tmp_path)
    select_chapter_frames(TASK, output_root=root)
    return root


def test_multi_chunk_publication_replays_every_scope_and_rejects_tampered_count(tmp_path):
    root = _long_chain(tmp_path, 15)
    inputs = dc._load_content_inputs(root, TASK)
    responses = [{"paragraphs": [{"source_unit_ids": [u["unit_id"] for u in scope.ledgers[0].units],
                                  "text": "合成范围正文。"}]}
                 for scope in dc.bounded_scopes(inputs)] + [copy.deepcopy(GLOBAL_OK)]
    result = _compose(root, _ContentTransport(responses), tmp_path=tmp_path, bounded=True)
    assert result.business_call_count == 5
    assert result.content["coverage"]["totals"]["full_coverage"]
    assert dc.load_published_document_content(root, TASK) == result.content
    forged = {**result.report, "business_call_count": 3}
    with pytest.raises(dc.ContentCacheCorruptError):
        dc._validate_execution_report(forged, content=result.content, task_id=TASK)


def test_overlarge_scoped_plan_stops_before_any_supplier_request(tmp_path):
    root = _long_chain(tmp_path, 73)
    transport = _ContentTransport([])
    with pytest.raises(dc.ContentBudgetExceededError):
        _compose(root, transport, tmp_path=tmp_path, bounded=True)
    assert not transport.calls
    assert not (root / TASK / "m2/content/current.json").exists()


def test_bounded_publication_replay_and_zero_request_cache(tmp_path):
    root = _chain(tmp_path)
    select_chapter_frames(TASK, output_root=root)
    second = {"paragraphs": [{"source_unit_ids": ["c0002-u001"], "text": "第二章内容。"}]}
    transport = _ContentTransport([copy.deepcopy(SINGLE_OK), second, copy.deepcopy(GLOBAL_OK)])
    result = _compose(root, transport, tmp_path=tmp_path, bounded=True)
    assert result.business_call_count == result.supplier_request_count == 3
    assert dc.composer_version_for_tokens(result.content["generation"]["per_call_max_tokens"]) == "1.2"
    assert dc.load_published_document_content(root, TASK) == result.content
    again = _compose(root, _ContentTransport([]), tmp_path=tmp_path, bounded=True)
    assert again.cache_hit
    assert again.result_id == result.result_id


def test_existing_published_group_can_be_pinned_across_composer_change(tmp_path):
    root = _chain(tmp_path)
    select_chapter_frames(TASK, output_root=root)
    old = _compose(root, _ContentTransport(_responses()), tmp_path=tmp_path)
    new = _compose(root, _ContentTransport([]), tmp_path=tmp_path, bounded=True, preserve_published=True)
    assert new.cache_hit and new.result_id == old.result_id


def test_scopes_partition_long_chapter_without_losing_units(tmp_path):
    root = _chain(tmp_path)
    select_chapter_frames(TASK, output_root=root)
    inputs = dc._load_content_inputs(root, TASK)
    ledger = inputs.ledgers[0]
    units = tuple({**ledger.units[0], "unit_id": f"c0001-u{i:03}"} for i in range(1, 14))
    inputs = dataclasses.replace(inputs, ledgers=(dataclasses.replace(ledger, units=units),))
    scopes = dc.bounded_scopes(inputs)
    assert [len(s.ledgers[0].units) for s in scopes] == [6, 6, 1]
    assert tuple(u for scope in scopes for u in scope.ledgers[0].units) == units


def test_scoped_duplicate_reference_still_fails_and_does_not_publish(tmp_path):
    root = _chain(tmp_path)
    select_chapter_frames(TASK, output_root=root)
    bad = {"paragraphs": [copy.deepcopy(SINGLE_OK["paragraphs"][0])] * 2}
    transport = _ContentTransport([bad, bad])
    with pytest.raises(dc.ContentModelFailedError):
        _compose(root, transport, tmp_path=tmp_path, bounded=True)
    assert len(transport.calls) == 2
    assert not (root / TASK / "m2/content/current.json").exists()


@pytest.mark.parametrize("existing,expected", [(None, "fake-file-key"), ("fake-env-key", "fake-env-key")])
def test_m2_cli_reads_local_dotenv_and_preserves_environment(tmp_path, monkeypatch, existing, expected):
    import os
    from video_doc import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIDEO_DOC_LLM_API_KEY", raising=False)
    if existing:
        monkeypatch.setenv("VIDEO_DOC_LLM_API_KEY", existing)
    (tmp_path / ".env").write_text("VIDEO_DOC_LLM_API_KEY=fake-file-key\n", encoding="utf-8")
    def inspect(*args, **kwargs):
        assert os.environ["VIDEO_DOC_LLM_API_KEY"] == expected
        raise dc.RoleConfigInvalidError("offline credential probe")
    monkeypatch.setattr(cli, "compose_document_v2", inspect)
    assert cli.main(["compose-document-v2", "SYNTH01"]) == 1

