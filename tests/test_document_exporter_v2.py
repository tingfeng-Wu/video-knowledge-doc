"""阶段14-D定向测试：DocumentExporterV2三格式自包含导出与发布。

契约唯一权威：docs/development/阶段14CD-M2文档链裁决.md（Q5/Q6/Q7.11—Q7.14＋
实现决定E1—E14）。矩阵组织：E=成功发布五件套/三格式内容/字节确定性；
C=缓存早退/复用与pointer_repaired/身份变化新组；D=四类图像降级reason与
E5剪枝不动点；I=输入缺失/损坏/绑定核验（锁前零写入）；G=导出组复验篡改；
P=发布注入矩阵/中断/锁；M=emergency形态消费；X=CLI export-document-v2。

全部离线：链用真实模块（13-1合成工作区＋build_timeline_v2＋14-B注入plan＋
14-A select＋14-C注入正文compose），export本身零API零模型（E9/E13）；
零网络、零FFmpeg；M1与真实outputs只读不触碰。
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest
from docx import Document as DocxDocument

from tests.test_boundary_planner_v2 import ENV, ScriptedTransport
from tests.test_chapter_frames import (
    _DEV01_M2,
    _DEV01_REAL,
    _DEV01_REASON,
    _dev01_injected_plan,
    _dev01_ready,
    _dev01_sandbox,
)
from tests.test_document_composer_v2 import (
    TASK,
    _chain,
    _chain_emerg,
    _compose,
    _content_registry,
    _ContentTransport,
    _emerg_responses,
    _m2_root,
    _read_json,
    _responses,
    _tree_sha_pairs,
    _write_json,
)
from video_doc import chapter_frames as cf
from video_doc import document_composer_v2 as dc
from video_doc import document_exporter_v2 as de
from video_doc.cli import main
from video_doc.task_workspace import TaskWorkspaceError
from video_doc.unified_timeline import canonical_json_bytes


# ---------------------------------------------------------------------------
# 链工厂与export域helpers
# ---------------------------------------------------------------------------


def _doc_root(root: Path) -> Path:
    return _m2_root(root) / de.EXPORT_ROOT_DIRECTORY


def _groups_root(root: Path) -> Path:
    return _doc_root(root) / de.GROUP_DIRECTORY


def _export_current_file(root: Path) -> Path:
    return _doc_root(root) / de.CURRENT_FILE


def _failures_dir(root: Path) -> Path:
    return _m2_root(root) / "failures" / de.EXPORT_MODULE_TOKEN


def _only_failure(root: Path) -> dict[str, Any]:
    entries = list(_failures_dir(root).iterdir())
    assert len(entries) == 1
    return json.loads((entries[0] / "failure.json").read_text(encoding="utf-8"))


def _assert_zero_export_writes(root: Path) -> None:
    assert not _doc_root(root).exists()
    assert not _failures_dir(root).exists()


def _assert_not_published(root: Path) -> None:
    # formal-results父目录在发布准备时预建，清理后允许作为空目录存在；
    # 不得有组目录或.tmp残留，current必须缺席。
    groups = _groups_root(root)
    if groups.exists():
        assert [path.name for path in groups.iterdir()] == []
    assert not _export_current_file(root).exists()


def _assert_no_temp_residue(root: Path) -> None:
    residue = [path.name for path in _doc_root(root).rglob("*") if ".tmp-" in path.name]
    assert residue == []


def _content_group(root: Path) -> Path:
    pointer = _read_json(_m2_root(root) / dc.ROOT_DIRECTORY / de.CURRENT_FILE)
    return _m2_root(root) / dc.ROOT_DIRECTORY / pointer["group"]


def _content_file(root: Path) -> Path:
    return _content_group(root) / dc.CONTENT_FILE


def _frames_group(root: Path) -> Path:
    pointer = _read_json(_m2_root(root) / "frames" / de.CURRENT_FILE)
    return _m2_root(root) / "frames" / pointer["group"]


def _png_bytes(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """生成合法最小PNG（python-docx可解码；14-D要求真实嵌入，E12硬验收）。"""

    raw_rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def _chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw_rows, 9))
        + _chunk(b"IEND", b"")
    )


def _republish_frames_with_real_pngs(root: Path) -> None:
    """把链fixture帧集的图像字节替换为合法PNG并同步frame-set/manifest/current哈希；
    frame_id/ordinal/decoded_ms等身份字段逐字不变，结果组目录名不变。"""

    frames_root = _m2_root(root) / "frames"
    pointer_path = frames_root / de.CURRENT_FILE
    pointer = _read_json(pointer_path)
    group = frames_root / pointer["group"]
    index_path = group / "frame-set.json"
    manifest_path = group / "manifest.json"
    index = _read_json(index_path)
    for frame in index["frames"]:
        ordinal = int(frame["ordinal"])
        payload = _png_bytes(2 + ordinal, 2, (ordinal % 256, (ordinal * 7) % 256, 90))
        (group / str(frame["file"])).write_bytes(payload)
        frame["sha256"] = hashlib.sha256(payload).hexdigest()
        frame["size_bytes"] = len(payload)
    index_bytes = (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    index_path.write_bytes(index_bytes)
    manifest = _read_json(manifest_path)
    manifest["files"] = {
        str(frame["file"]): {"sha256": frame["sha256"], "bytes": frame["size_bytes"]}
        for frame in index["frames"]
    }
    manifest["files"]["frame-set.json"] = {
        "sha256": hashlib.sha256(index_bytes).hexdigest(),
        "bytes": len(index_bytes),
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    pointer["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    _write_json(pointer_path, pointer)


def _chain_export(tmp_path: Path, *, emergency: bool = False) -> Path:
    """14-D专用链：真PNG帧重发布之后才select/compose，使绑定哈希全程一致。"""

    root = _chain_emerg(tmp_path) if emergency else _chain(tmp_path)
    _republish_frames_with_real_pngs(root)
    cf.select_chapter_frames(TASK, output_root=root)
    return root


def _exported(tmp_path: Path) -> tuple[Path, dict[str, Any], de.DocumentExportResult]:
    root = _chain_export(tmp_path)
    _compose(root, ScriptedTransport(_responses()), tmp_path=tmp_path)
    content = _read_json(_content_file(root))
    export = de.export_document_v2(root, TASK)
    return root, content, export


def _exported_fresh(tmp_path: Path) -> tuple[Path, dict[str, Any], de.DocumentExportResult]:
    """导出一次后删除组与current，为“重新发布”类用例准备现场。"""

    root, content, export = _exported(tmp_path)
    shutil.rmtree(export.group_path)
    _export_current_file(root).unlink()
    return root, content, export


def _referenced(content: dict[str, Any]) -> list[str]:
    return de._referenced_frame_ids(content)


# ---------------------------------------------------------------------------
# E矩阵：成功发布五件套、三格式内容、字节确定性
# ---------------------------------------------------------------------------


def test_e_publish_group_members_and_pointer(tmp_path: Path) -> None:
    root, content, export = _exported(tmp_path)
    assert export.status == "success"
    assert export.cache_hit is False and export.pointer_repaired is False
    assert export.chapter_count == len(content["chapters"]) == 2
    referenced = _referenced(content)
    assert len(referenced) > 0
    assert export.image_count == len(referenced)
    assert export.image_omitted_count == 0
    assert export.degraded_reasons == ()
    _, cache_sha = de._export_cache_identity(de._load_export_inputs(root, TASK))
    assert export.result_id == de.RESULT_ID_PREFIX + str(cache_sha)[:12]
    group = export.group_path
    assert group.name == export.result_id
    assert {entry.name for entry in group.iterdir()} == de._EXPORT_GROUP_FILE_NAMES
    # result.json逐字节＝正文规范字节（裁决Q5.6）
    assert (group / de.RESULT_FILE).read_bytes() == _content_file(root).read_bytes()
    manifest = _read_json(group / de.MANIFEST_FILE)
    assert set(manifest) == de._EXPORT_MANIFEST_KEYS
    assert manifest["artifact_type"] == de.EXPORT_ARTIFACT_TYPE
    assert manifest["status"] == "success"
    assert manifest["cache_identity_sha256"] == cache_sha
    assert manifest["degraded_reasons"] == []
    assert set(manifest["inputs"]) == de._EXPORT_MANIFEST_INPUT_KEYS
    assert set(manifest["versions"]) == de._EXPORT_MANIFEST_VERSION_KEYS
    expected_files = {de.RESULT_FILE, de.MARKDOWN_FILE, de.DOCX_FILE} | {
        de._image_relative_path(frame_id) for frame_id in referenced
    }
    assert set(manifest["files"]) == expected_files
    for name, record in manifest["files"].items():
        payload = (group / name).read_bytes()
        assert record == {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    current = _read_json(_export_current_file(root))
    assert set(current) == de._CURRENT_KEYS
    assert current["artifact_type"] == de.EXPORT_ARTIFACT_TYPE
    assert current["result_id"] == export.result_id
    assert current["group"] == f"{de.GROUP_DIRECTORY}/{export.result_id}"
    assert not _failures_dir(root).exists()
    assert not (_m2_root(root) / ".locks" / de.LOCK_FILE).exists()
    _assert_no_temp_residue(root)
    # 导出绑定＝正文绑定帧集实时哈希链一致
    assert manifest["inputs"]["content_id"] == content["content_id"]
    assert manifest["inputs"]["frame_set_id"] == content["source_frame_set"]["frame_set_id"]


def test_e_markdown_structure(tmp_path: Path) -> None:
    root, content, export = _exported(tmp_path)
    text = (export.group_path / de.MARKDOWN_FILE).read_text(encoding="utf-8")
    assert text.startswith(f"# {content['title']}\n")
    assert f"## {de.SECTION_HEADING_OVERVIEW}" in text
    assert f"## {de.SECTION_HEADING_CONCLUSION}" in text
    assert content["overview"][0]["text"] in text
    assert content["overall_conclusion"][0]["text"] in text
    assert content["chapters"][0]["blocks"][0]["text"] in text
    for chapter in content["chapters"]:
        for frame in de._sorted_chapter_frames(chapter):
            assert frame["caption_text"] in text
    assert de.EMERGENCY_NOTICE not in text
    # 重渲染逐字节钉死（E6）
    present = {
        path.name[: -len(de.IMAGE_SUFFIX)]
        for path in (export.group_path / de.IMAGES_DIRECTORY).iterdir()
    }
    assert (export.group_path / de.MARKDOWN_FILE).read_bytes() == de.render_export_markdown(
        content, embedded=present
    )


def test_e_docx_reopens_with_images_and_captions(tmp_path: Path) -> None:
    root, content, export = _exported(tmp_path)
    document = DocxDocument(io.BytesIO((export.group_path / de.DOCX_FILE).read_bytes()))
    assert len(document.inline_shapes) == export.image_count
    caption_paragraphs = [
        paragraph
        for paragraph in document.paragraphs
        if paragraph.style.name == de.DOCX_IMAGE_CAPTION_STYLE
    ]
    assert len(caption_paragraphs) == export.image_count
    all_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert content["title"] in all_text
    for chapter in content["chapters"]:
        assert chapter["title"] in all_text
    assert de.SECTION_HEADING_OVERVIEW in all_text
    assert de.SECTION_HEADING_CONCLUSION in all_text
    assert de.EMERGENCY_NOTICE not in all_text


def test_q2_docx_keep_attributes_and_renderer_version(tmp_path: Path) -> None:
    """阶段15-0裁决§二Q2：图像段keep_with_next、caption段keep_together、版本值递增。

    最小版式语义（同文件标题样式既有先例形态）；样式定义、段落数量/顺序/内容、
    IMAGE_EMBED_WIDTH、descr、E5剪枝与Q5.4 caption一律保留全部不动；
    EXPORTER_VERSION维持"1.0"；缓存身份16键集合形状不变仅docx_renderer取值变。
    """

    assert de.DOCX_RENDERER_VERSION == "m2-structured-document-docx-1.1"
    assert de.EXPORTER_VERSION == "1.0"
    root, content, export = _exported(tmp_path)
    assert export.image_count > 0
    document = DocxDocument(
        io.BytesIO((export.group_path / de.DOCX_FILE).read_bytes())
    )
    image_paragraphs = [
        paragraph for paragraph in document.paragraphs if paragraph._p.xpath(".//w:drawing")
    ]
    assert len(image_paragraphs) == export.image_count
    for paragraph in image_paragraphs:
        assert paragraph.paragraph_format.keep_with_next is True
        assert paragraph.paragraph_format.keep_together is None
    caption_paragraphs = [
        paragraph
        for paragraph in document.paragraphs
        if paragraph.style.name == de.DOCX_IMAGE_CAPTION_STYLE
    ]
    assert len(caption_paragraphs) == export.image_count
    for paragraph in caption_paragraphs:
        assert paragraph.paragraph_format.keep_together is True
        assert paragraph.paragraph_format.keep_with_next is None
    # 普通正文段零波及：两属性只落在图像/caption段对上。
    body_paragraphs = [
        paragraph
        for paragraph in document.paragraphs
        if paragraph.style.name == "Normal" and not paragraph._p.xpath(".//w:drawing")
    ]
    assert body_paragraphs
    for paragraph in body_paragraphs:
        assert paragraph.paragraph_format.keep_with_next is None
        assert paragraph.paragraph_format.keep_together is None
    # caption文本逐字在位（Q5.4一律保留，两属性不改内容/数量/顺序）。
    caption_texts = {paragraph.text for paragraph in caption_paragraphs}
    for chapter in content["chapters"]:
        for frame in de._sorted_chapter_frames(chapter):
            assert frame["caption_text"] in caption_texts


def test_e_docx_bytes_deterministic_rebuild(tmp_path: Path) -> None:
    root, _content, export = _exported(tmp_path)
    first_bytes = {
        name: (export.group_path / name).read_bytes()
        for name in (de.RESULT_FILE, de.MARKDOWN_FILE, de.DOCX_FILE, de.MANIFEST_FILE)
    }
    shutil.rmtree(export.group_path)
    _export_current_file(root).unlink()
    second = de.export_document_v2(root, TASK)
    assert second.result_id == export.result_id
    assert second.cache_hit is False  # 组已删，重建发布
    for name, payload in first_bytes.items():
        assert (second.group_path / name).read_bytes() == payload, name
    images = sorted((export.group_path / de.IMAGES_DIRECTORY).iterdir())
    images2 = sorted((second.group_path / de.IMAGES_DIRECTORY).iterdir())
    assert [path.name for path in images2] == [path.name for path in images]
    for old, new in zip(images, images2):
        assert old.read_bytes() == new.read_bytes()


# ---------------------------------------------------------------------------
# C矩阵：缓存早退、复用、pointer_repaired与身份变化
# ---------------------------------------------------------------------------


def test_c_second_export_early_hit_zero_rewrite(tmp_path: Path) -> None:
    root, _content, first = _exported(tmp_path)
    before = _tree_sha_pairs(_doc_root(root))
    second = de.export_document_v2(root, TASK)
    assert second.cache_hit is True
    assert second.pointer_repaired is False
    assert second.result_id == first.result_id
    assert second.status == "success"
    assert _tree_sha_pairs(_doc_root(root)) == before
    assert not _failures_dir(root).exists()


def test_c_reuse_after_current_unlink_sets_pointer_repaired(tmp_path: Path) -> None:
    root, _content, first = _exported(tmp_path)
    _export_current_file(root).unlink()
    before = _tree_sha_pairs(first.group_path)
    second = de.export_document_v2(root, TASK)
    assert second.cache_hit is True
    assert second.pointer_repaired is True
    assert second.result_id == first.result_id
    assert _tree_sha_pairs(first.group_path) == before
    current = _read_json(_export_current_file(root))
    assert current["result_id"] == first.result_id


def test_c_cache_identity_change_new_group_old_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 上游身份变化（timeline/plan/content链重建）已由14-C p_cache覆盖；
    # 14-D专用链帧绑定同步会牵动page-change组身份复验，不在此重复重建全链。
    # 本用例注入导出缓存身份变化，只验export层发布行为：新组共存、旧组只读、current切换。
    root, _content, first = _exported(tmp_path)
    old_snapshot = _tree_sha_pairs(first.group_path)
    alternate_sha = hashlib.sha256(b"alternate-export-identity").hexdigest()
    real_identity = de._export_cache_identity

    def _patched(inputs: de.ExportInputs) -> tuple[dict[str, Any], str]:
        identity, _sha = real_identity(inputs)
        return identity, alternate_sha

    monkeypatch.setattr(de, "_export_cache_identity", _patched)
    second = de.export_document_v2(root, TASK)
    assert second.result_id == de.RESULT_ID_PREFIX + hashlib.sha256(
        b"alternate-export-identity"
    ).hexdigest()[:12]
    assert second.result_id != first.result_id
    assert second.cache_hit is False
    groups = sorted(path.name for path in _groups_root(root).iterdir())
    assert groups == sorted([first.result_id, second.result_id])
    assert _tree_sha_pairs(first.group_path) == old_snapshot
    current = _read_json(_export_current_file(root))
    assert current["result_id"] == second.result_id
    second_manifest = _read_json(second.group_path / de.MANIFEST_FILE)
    assert second_manifest["cache_identity_sha256"] == hashlib.sha256(
        b"alternate-export-identity"
    ).hexdigest()
    # 第三轮同注入身份：早退复用新组
    third = de.export_document_v2(root, TASK)
    assert third.cache_hit is True and third.result_id == second.result_id


def test_c_cache_identity_exact_sixteen_keys(tmp_path: Path) -> None:
    root, _content, _export = _exported(tmp_path)
    identity, sha = de._export_cache_identity(de._load_export_inputs(root, TASK))
    assert set(identity) == de._EXPORT_CACHE_KEYS
    assert len(de._EXPORT_CACHE_KEYS) == 16
    assert sha == hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


# ---------------------------------------------------------------------------
# D矩阵：四类图像降级reason、E5剪枝不动点、caption保留、degraded仍发布
# ---------------------------------------------------------------------------


def _drop_frame_file(root: Path, content: dict[str, Any]) -> str:
    """删除帧集内首个被引用帧的磁盘文件，返回其frame_id。"""

    victim = _referenced(content)[0]
    frames_group = _frames_group(root)
    index = _read_json(frames_group / "frame-set.json")
    for frame in index["frames"]:
        if frame["frame_id"] == victim:
            (frames_group / frame["file"]).unlink()
    return victim


def test_d_image_missing_degrades_but_publishes(tmp_path: Path) -> None:
    root, content, export = _exported_fresh(tmp_path)
    victim = _drop_frame_file(root, content)
    result = de.export_document_v2(root, TASK)
    assert result.status == "degraded"
    assert result.degraded_reasons == (f"{de.REASON_IMAGE_MISSING}:{victim}",)
    assert result.image_omitted_count == 1
    assert result.image_count == len(_referenced(content)) - 1
    # degraded照常发布并切current（裁决Q5.4）
    current = _read_json(_export_current_file(root))
    assert current["result_id"] == result.result_id
    manifest = _read_json(result.group_path / de.MANIFEST_FILE)
    assert manifest["status"] == "degraded"
    assert manifest["degraded_reasons"] == [f"{de.REASON_IMAGE_MISSING}:{victim}"]
    assert de._image_relative_path(victim) not in manifest["files"]
    assert not (result.group_path / de.IMAGES_DIRECTORY / f"{victim}{de.IMAGE_SUFFIX}").exists()
    # caption文本保留于md与docx（Q5.4 captions kept）
    victim_caption = None
    for chapter in content["chapters"]:
        for frame in chapter["frames"]:
            if frame["frame_id"] == victim:
                victim_caption = frame["caption_text"]
    assert victim_caption is not None
    text = (result.group_path / de.MARKDOWN_FILE).read_text(encoding="utf-8")
    assert victim_caption in text
    document = DocxDocument(io.BytesIO((result.group_path / de.DOCX_FILE).read_bytes()))
    captions = "\n".join(
        paragraph.text
        for paragraph in document.paragraphs
        if paragraph.style.name == de.DOCX_IMAGE_CAPTION_STYLE
    )
    assert victim_caption in captions
    # 二次导出早退命中degraded组（E13整组复验通过）
    third = de.export_document_v2(root, TASK)
    assert third.cache_hit is True and third.status == "degraded"


def test_d_image_hash_mismatch_reason(tmp_path: Path) -> None:
    root, content, export = _exported_fresh(tmp_path)
    victim = _referenced(content)[0]
    frames_group = _frames_group(root)
    index = _read_json(frames_group / "frame-set.json")
    for frame in index["frames"]:
        if frame["frame_id"] == victim:
            (frames_group / frame["file"]).write_bytes(b"tampered-png-bytes")
    result = de.export_document_v2(root, TASK)
    assert result.status == "degraded"
    assert result.degraded_reasons == (f"{de.REASON_IMAGE_HASH_MISMATCH}:{victim}",)
    assert de._image_relative_path(victim) not in _read_json(
        result.group_path / de.MANIFEST_FILE
    )["files"]


def test_d_image_unreadable_reason_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frames_group = tmp_path / "frames"
    (frames_group / "frames").mkdir(parents=True)
    path = frames_group / "frames" / "fr000001.png"
    path.write_bytes(b"stub")
    fake_inputs = SimpleNamespace(
        frames_group=frames_group,
        frame_files={
            "fr000001": {
                "file": "frames/fr000001.png",
                "sha256": hashlib.sha256(b"stub").hexdigest(),
            }
        },
    )
    real_read = Path.read_bytes

    def _boom(self: Path) -> bytes:
        if self == path:
            raise PermissionError("simulated device error")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    embedded, reasons = de._collect_frame_bytes(fake_inputs, ["fr000001"])
    assert embedded == {}
    assert reasons == [f"{de.REASON_IMAGE_UNREADABLE}:fr000001"]


def test_d_embed_failed_prune_to_fixpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, content, export = _exported_fresh(tmp_path)
    real_render = de.render_export_docx
    state = {"first": True}

    def _flaky(content_: Any, embedded: Any) -> tuple[bytes, list[str]]:
        if state["first"] and embedded:
            state["first"] = False
            victim = sorted(embedded)[0]
            shrunk = {key: value for key, value in embedded.items() if key != victim}
            payload, _ = real_render(content_, shrunk)
            return payload, [victim]
        return real_render(content_, embedded)

    monkeypatch.setattr(de, "render_export_docx", _flaky)
    result = de.export_document_v2(root, TASK)
    assert state["first"] is False  # 确实触发过一轮失败剪枝
    assert result.status == "degraded"
    assert len(result.degraded_reasons) == 1
    assert result.degraded_reasons[0].startswith(f"{de.REASON_IMAGE_EMBED_FAILED}:")
    victim = result.degraded_reasons[0].split(":", 1)[1]
    present = {
        path.name[: -len(de.IMAGE_SUFFIX)]
        for path in (result.group_path / de.IMAGES_DIRECTORY).iterdir()
    }
    referenced = set(_referenced(content))
    assert referenced == present | {victim}
    manifest = _read_json(result.group_path / de.MANIFEST_FILE)
    assert de._image_relative_path(victim) not in manifest["files"]
    # docx中victim的图片省略、caption保留（E5剪枝＋Q5.4）
    document = DocxDocument(io.BytesIO((result.group_path / de.DOCX_FILE).read_bytes()))
    assert len(document.inline_shapes) == len(present)


def test_d_degraded_reason_parser_matrix() -> None:
    assert de._parse_degraded_reason(
        "image_missing:fr000118", de.CacheCorruptError
    ) == ("image_missing", "fr000118")
    for bad in (
        "no-colon-here",
        "bogus_kind:fr000118",
        "image_missing:fr123",
        "image_missing:frXXXXXX",
        "image_missing:",
        12345,
    ):
        with pytest.raises(de.CacheCorruptError):
            de._parse_degraded_reason(bad, de.CacheCorruptError)


# ---------------------------------------------------------------------------
# I矩阵：输入缺失/损坏/绑定核验，锁前零写入
# ---------------------------------------------------------------------------


def test_i_content_missing_lock_free(tmp_path: Path) -> None:
    root = _chain(tmp_path)  # 链未compose
    with pytest.raises(de.InputContentMissingError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_INPUT_CONTENT_MISSING
    assert excinfo.value.recoverable is True
    assert excinfo.value.next_action == "run_compose_document_v2"
    _assert_zero_export_writes(root)


def test_i_task_json_missing_lock_free(tmp_path: Path) -> None:
    root = _chain(tmp_path)
    (root / TASK / "task.json").unlink()
    with pytest.raises(de.InputTaskMissingError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_INPUT_TASK_MISSING
    assert excinfo.value.next_action == "run_init_task"
    _assert_zero_export_writes(root)


def test_i_frames_current_missing(tmp_path: Path) -> None:
    root, _content, _export = _exported(tmp_path)
    (_m2_root(root) / "frames" / de.CURRENT_FILE).unlink()
    with pytest.raises(de.InputFrameSetMissingError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_INPUT_FRAME_SET_MISSING
    assert excinfo.value.next_action == "run_sample_frames"
    assert not _failures_dir(root).exists()  # 锁前失败零审计


def test_i_content_current_garbage_corrupt(tmp_path: Path) -> None:
    root, _content, _export = _exported(tmp_path)
    (_m2_root(root) / dc.ROOT_DIRECTORY / de.CURRENT_FILE).write_bytes(b"not-json\n")
    with pytest.raises(de.InputContentCorruptError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_INPUT_CONTENT_CORRUPT
    assert not _failures_dir(root).exists()


def test_i_frames_index_missing_corrupt(tmp_path: Path) -> None:
    root, _content, _export = _exported(tmp_path)
    (_frames_group(root) / "frame-set.json").unlink()
    with pytest.raises(de.InputFrameSetCorruptError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_INPUT_FRAME_SET_CORRUPT
    assert not _failures_dir(root).exists()


def test_i_content_file_tampered_corrupt(tmp_path: Path) -> None:
    root, _content, _export = _exported(tmp_path)
    _content_file(root).write_bytes(b'{"tampered": 1}\n')
    with pytest.raises(de.InputContentCorruptError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_INPUT_CONTENT_CORRUPT
    assert not _failures_dir(root).exists()


def test_i_unregistered_frame_id_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, content, _export = _exported(tmp_path)
    victim = _referenced(content)[0]
    real_loader = de._load_frame_inputs

    def _drop(output: Path, task: str):
        index, group, frame_files, manifest_sha = real_loader(output, task)
        frame_files.pop(victim, None)
        return index, group, frame_files, manifest_sha

    monkeypatch.setattr(de, "_load_frame_inputs", _drop)
    with pytest.raises(de.InputFrameSetCorruptError) as excinfo:
        de.export_document_v2(root, TASK)
    assert victim in str(excinfo.value)
    assert excinfo.value.error_code == de.ERROR_INPUT_FRAME_SET_CORRUPT
    assert not _failures_dir(root).exists()


def test_i_frame_binding_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _content, _export = _exported(tmp_path)
    real_loader = de._load_frame_inputs

    def _swapped(output: Path, task: str):
        index, group, frame_files, manifest_sha = real_loader(output, task)
        index = dict(index)
        index["frame_set_id"] = "frame-set-000000000000"
        return index, group, frame_files, manifest_sha

    monkeypatch.setattr(de, "_load_frame_inputs", _swapped)
    with pytest.raises(de.InputIdentityMismatchError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_INPUT_IDENTITY_MISMATCH
    assert not _failures_dir(root).exists()


def test_i_invalid_task_id_rejected_before_any_read(tmp_path: Path) -> None:
    root, _content, _export = _exported(tmp_path)
    snapshot = _tree_sha_pairs(root)
    with pytest.raises(TaskWorkspaceError):
        de.export_document_v2(root, "..\\evil")
    assert _tree_sha_pairs(root) == snapshot


# ---------------------------------------------------------------------------
# G矩阵：导出组整组复验（篡改回退）
# ---------------------------------------------------------------------------


def test_g_export_group_tamper_matrix(tmp_path: Path) -> None:
    root, _content, export = _exported(tmp_path)
    group = export.group_path

    # 形态1：result.json改字节 → manifest钉住哈希 CACHE_CORRUPT，current不切
    original_result_bytes = (group / de.RESULT_FILE).read_bytes()
    (group / de.RESULT_FILE).write_bytes(b'{"tampered": true}\n')
    with pytest.raises(de.CacheCorruptError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_CACHE_CORRUPT
    audit = _only_failure(root)
    assert audit["stage"] == "current_validation"
    assert audit["current_unchanged"] is True
    shutil.rmtree(_failures_dir(root))
    (group / de.RESULT_FILE).write_bytes(original_result_bytes)

    # 形态2：images目录加非法成员名 → 成员名校验 CACHE_CORRUPT
    extra = group / de.IMAGES_DIRECTORY / "extra.png"
    extra.write_bytes(b"x")
    with pytest.raises(de.CacheCorruptError):
        de.export_document_v2(root, TASK)
    extra.unlink()
    shutil.rmtree(_failures_dir(root))

    # 形态3：删一个images文件且不补reason → 引用集不变式 CACHE_CORRUPT
    present = sorted((group / de.IMAGES_DIRECTORY).iterdir())
    assert present
    gone = present[0]
    gone_bytes = gone.read_bytes()
    gone.unlink()
    with pytest.raises(de.CacheCorruptError):
        de.export_document_v2(root, TASK)
    gone.write_bytes(gone_bytes)
    shutil.rmtree(_failures_dir(root))

    # 形态4：docx改字节并同步manifest与current → 重渲染钉住 EXPORT_VALIDATION_FAILED
    tampered = (group / de.DOCX_FILE).read_bytes() + b"\x00"
    (group / de.DOCX_FILE).write_bytes(tampered)
    manifest_path = group / de.MANIFEST_FILE
    manifest = _read_json(manifest_path)
    manifest["files"][de.DOCX_FILE] = {
        "sha256": hashlib.sha256(tampered).hexdigest(),
        "bytes": len(tampered),
    }
    manifest_bytes = de._json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    current_path = _export_current_file(root)
    current = _read_json(current_path)
    current["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    _write_json(current_path, current)
    with pytest.raises(de.ExportValidationFailedError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_EXPORT_VALIDATION_FAILED


def test_g_own_current_corrupt_matrix(tmp_path: Path) -> None:
    root, _content, export = _exported(tmp_path)
    current_path = _export_current_file(root)
    pointer = _read_json(current_path)
    extra = dict(pointer)
    extra["surprise"] = 1
    shapes = {
        "garbage": b"not-json\n",
        "extra-key": extra,
        "group-mismatch": {**pointer, "group": "formal-results/other"},
        "unsafe-id": {**pointer, "result_id": "../evil", "group": "formal-results/../evil"},
        "bad-schema": {**pointer, "artifact_type": "something_else"},
    }
    group_snapshot = _tree_sha_pairs(export.group_path)
    for name, payload in shapes.items():
        if isinstance(payload, bytes):
            current_path.write_bytes(payload)
        else:
            _write_json(current_path, payload)
        tampered_bytes = current_path.read_bytes()
        with pytest.raises(de.CurrentPointerCorruptError) as excinfo:
            de.export_document_v2(root, TASK)
        assert excinfo.value.error_code == de.ERROR_CURRENT_POINTER_CORRUPT, name
        assert current_path.read_bytes() == tampered_bytes, name
        assert _tree_sha_pairs(export.group_path) == group_snapshot, name
        audit = _only_failure(root)
        assert audit["stage"] == "current_validation", name
        assert audit["current_unchanged"] is True, name
        shutil.rmtree(_failures_dir(root))


# ---------------------------------------------------------------------------
# P矩阵：发布注入矩阵、中断、锁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "stage", "cleanup"),
    [
        ("after_inputs", "after_inputs", "not_created"),
        ("before_group_publish", "group_publish", "temp_removed"),
        ("after_group_publish", "group_publish", "group_removed"),
        ("before_current", "current_publish", "group_removed"),
    ],
)
def test_p_injection_matrix(tmp_path: Path, point: str, stage: str, cleanup: str) -> None:
    root, _content, _export = _exported_fresh(tmp_path)
    with pytest.raises(de.InjectedExportFailure) as excinfo:
        de.export_document_v2(root, TASK, failure_injection=point)
    assert excinfo.value.error_code == de.ERROR_PUBLISH_FAILED
    audit = _only_failure(root)
    assert audit["stage"] == stage
    assert audit["temporary_cleanup_status"] == cleanup
    assert audit["error_code"] == de.ERROR_PUBLISH_FAILED
    assert audit["current_unchanged"] is True
    # 本次失败零发布：无组、无current（首导出建过的空目录域允许存在）
    _assert_not_published(root)
    _assert_no_temp_residue(root)
    assert not (_m2_root(root) / ".locks" / de.LOCK_FILE).exists()


def test_p_keyboard_interrupt_discards_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _content, _export = _exported_fresh(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(de, "_replace_current", _boom)
    with pytest.raises(KeyboardInterrupt):
        de.export_document_v2(root, TASK)
    audit = _only_failure(root)
    assert audit["stage"] == "current_publish"
    assert audit["error_code"] == de.ERROR_PUBLISH_FAILED
    assert "KeyboardInterrupt" in audit["safe_message"]
    assert audit["temporary_cleanup_status"] == "group_removed"
    assert audit["current_unchanged"] is True
    _assert_not_published(root)
    _assert_no_temp_residue(root)
    assert not (_m2_root(root) / ".locks" / de.LOCK_FILE).exists()


def test_p_lock_conflict_no_audit_then_success(tmp_path: Path) -> None:
    root, _content, export = _exported(tmp_path)
    shutil.rmtree(export.group_path)
    _export_current_file(root).unlink()
    locks = _m2_root(root) / ".locks"
    assert locks.is_dir()  # 首成功导出已建
    lock = locks / de.LOCK_FILE
    lock.write_text("{}", encoding="utf-8", newline="")
    with pytest.raises(de.ConcurrentRunError) as excinfo:
        de.export_document_v2(root, TASK)
    assert excinfo.value.error_code == de.ERROR_CONCURRENT_RUN
    assert not _failures_dir(root).exists()
    assert lock.exists()  # 冲突方不得删他人锁
    assert not _export_current_file(root).exists()
    lock.unlink()
    result = de.export_document_v2(root, TASK)
    assert result.cache_hit is False
    assert result.status == "success"


def test_p_unknown_injection_point_plain_error(tmp_path: Path) -> None:
    root, _content, _export = _exported(tmp_path)
    snapshot = _tree_sha_pairs(root)
    with pytest.raises(de.DocumentExporterV2Error) as excinfo:
        de.export_document_v2(root, TASK, failure_injection="bogus")
    assert type(excinfo.value) is de.DocumentExporterV2Error
    assert _tree_sha_pairs(root) == snapshot


# ---------------------------------------------------------------------------
# M矩阵：emergency形态消费（无CLI旗、照常导出，裁决Q6.9/Q5.5）
# ---------------------------------------------------------------------------


def test_m_emergency_chain_export(tmp_path: Path) -> None:
    root = _chain_export(tmp_path, emergency=True)
    compose_result = _compose(root, _ContentTransport(_emerg_responses()), tmp_path=tmp_path)
    content = _read_json(compose_result.group_path / dc.CONTENT_FILE)
    assert content["source_plan"]["result_source"] == "emergency"
    export = de.export_document_v2(root, TASK)
    assert export.status == "success"  # emergency不触发导出降级
    assert export.chapter_count == 1
    text = (export.group_path / de.MARKDOWN_FILE).read_text(encoding="utf-8")
    assert de.EMERGENCY_NOTICE in text
    document = DocxDocument(io.BytesIO((export.group_path / de.DOCX_FILE).read_bytes()))
    notices = [
        paragraph.text
        for paragraph in document.paragraphs
        if paragraph.style.name == de.DOCX_NOTICE_STYLE
    ]
    assert notices == [de.EMERGENCY_NOTICE]


# ---------------------------------------------------------------------------
# X矩阵：CLI export-document-v2接线（12键成功摘要、7键失败摘要、退出码）
# ---------------------------------------------------------------------------

_CLI_OK_KEYS = de._SUMMARY_KEYS
_CLI_FAIL_KEYS = {
    "task_id",
    "module",
    "status",
    "error_code",
    "safe_message",
    "recoverable",
    "next_action",
}


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def test_x_cli_success_and_cache_replay(tmp_path: Path) -> None:
    root, _content, export = _exported(tmp_path)
    shutil.rmtree(_doc_root(root))  # 强制首导出走完整发布路径
    code, out, err = _run_cli(["export-document-v2", TASK, "--output-root", str(root)])
    assert code == 0 and err == ""
    ok = json.loads(out)
    assert set(ok) == _CLI_OK_KEYS
    assert ok["module"] == de.EXPORT_MODULE_TOKEN
    assert ok["status"] == "success"
    assert ok["task_id"] == TASK
    assert ok["chapter_count"] == 2
    assert ok["cache_hit"] is False
    assert ok["image_count"] > 0 and ok["image_omitted_count"] == 0
    assert ok["result_id"].startswith(de.RESULT_ID_PREFIX)
    assert ok["group_relative_path"] == f"m2/documents/formal-results/{ok['result_id']}"
    # 第二次CLI：早退缓存，整域零改写
    before = _tree_sha_pairs(_doc_root(root))
    code2, out2, err2 = _run_cli(["export-document-v2", TASK, "--output-root", str(root)])
    assert code2 == 0 and err2 == ""
    ok2 = json.loads(out2)
    assert ok2["cache_hit"] is True
    assert ok2["result_id"] == ok["result_id"]
    assert _tree_sha_pairs(_doc_root(root)) == before


def test_x_cli_degraded_exit_zero(tmp_path: Path) -> None:
    root, content, _export = _exported_fresh(tmp_path)
    victim = _drop_frame_file(root, content)
    code, out, err = _run_cli(["export-document-v2", TASK, "--output-root", str(root)])
    assert code == 0 and err == ""
    ok = json.loads(out)
    assert ok["status"] == "degraded"
    assert ok["degraded_reasons"] == [f"{de.REASON_IMAGE_MISSING}:{victim}"]
    assert ok["image_omitted_count"] == 1


def test_x_cli_failure_stderr_keys(tmp_path: Path) -> None:
    root = _chain(tmp_path)  # 未compose→content缺失
    code, out, err = _run_cli(["export-document-v2", TASK, "--output-root", str(root)])
    assert code == 1 and out == ""
    fail = json.loads(err)
    assert set(fail) == _CLI_FAIL_KEYS
    assert fail["module"] == de.EXPORT_MODULE_TOKEN
    assert fail["error_code"] == de.ERROR_INPUT_CONTENT_MISSING
    assert str(root) not in fail["safe_message"]


def test_x_cli_usage_exit_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["export-document-v2"])
    assert excinfo.value.code == 2


def test_x_cli_keyboard_interrupt_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _chain(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("video_doc.cli.export_document_v2", _boom)
    with pytest.raises(KeyboardInterrupt):
        main(["export-document-v2", TASK, "--output-root", str(root)])


# ---------------------------------------------------------------------------
# DEV01真实沙箱双形态注入e2e＋图像降级＋真实树整树零改写（裁决Q7验收）
# 只读复制task.json＋m2/{timelines,frames,ocr}到tmp沙箱（14-B/14-A先例）；
# 正文三槽位仍为本地脚本注入，零网络、零真实API。
# ---------------------------------------------------------------------------


_DEV01 = "DEV01"


def _sb_m2(outputs: Path) -> Path:
    return outputs / _DEV01 / "m2"


def _sb_doc_root(outputs: Path) -> Path:
    return _sb_m2(outputs) / de.EXPORT_ROOT_DIRECTORY


def _sb_export_current(outputs: Path) -> Path:
    return _sb_doc_root(outputs) / de.CURRENT_FILE


def _sb_content_file(outputs: Path) -> Path:
    pointer = _read_json(_sb_m2(outputs) / dc.ROOT_DIRECTORY / de.CURRENT_FILE)
    return _sb_m2(outputs) / dc.ROOT_DIRECTORY / pointer["group"] / dc.CONTENT_FILE


def _sb_frames_group(outputs: Path) -> Path:
    pointer = _read_json(_sb_m2(outputs) / "frames" / de.CURRENT_FILE)
    return _sb_m2(outputs) / "frames" / pointer["group"]


def _dev01_scripted_responses(outputs: Path) -> list[dict[str, Any]]:
    """由DEV01真实账本动态构造三/两调用脚本应答（期望值不硬编码）。"""

    inputs = dc._load_content_inputs(Path(outputs), _DEV01)

    def _pack(ledger: dc.ChapterLedger) -> list[dict[str, Any]]:
        unit_ids = [str(unit["unit_id"]) for unit in ledger.units]
        size = max(1, -(-len(unit_ids) // dc.PARAGRAPH_RAW_MAX))
        return [
            {
                "source_unit_ids": unit_ids[start : start + size],
                "text": f"沙箱注入正文第{position}段：连续单元覆盖，仅验证导出链。",
            }
            for position, start in enumerate(range(0, len(unit_ids), size), start=1)
        ]

    responses: list[dict[str, Any]] = [{"paragraphs": _pack(inputs.ledgers[0])}]
    if len(inputs.ledgers) > 1:
        responses.append(
            {
                "chapters": [
                    {"chapter_id": ledger.chapter_id, "paragraphs": _pack(ledger)}
                    for ledger in inputs.ledgers[1:]
                ]
            }
        )
    responses.append(
        {
            "title": "DEV01真实时间轴沙箱结构化文档",
            "overview": ["本组正文由本地脚本注入，仅用于导出链离线验收。"],
            "conclusions": ["不评价模型正文语义质量。"],
        }
    )
    return responses


def _dev01_publish(
    tmp_path: Path, *, emergency: bool = False
) -> tuple[Path, dc.DocumentCompositionResult, de.DocumentExportResult, dict, int]:
    """沙箱全链：注入plan→select→compose（脚本应答）→export；返回含真实树快照。"""

    outputs = _dev01_sandbox(tmp_path)
    real_snapshot = _tree_sha_pairs(_DEV01_REAL)
    boundaries = _dev01_injected_plan(outputs, emergency=emergency)
    cf.select_chapter_frames(_DEV01, output_root=outputs)
    content = dc.compose_document_v2(
        outputs,
        _DEV01,
        model_registry=_content_registry(tmp_path),
        transport=ScriptedTransport(_dev01_scripted_responses(outputs)),
        environ=ENV,
    )
    export = de.export_document_v2(outputs, _DEV01)
    return outputs, content, export, real_snapshot, len(boundaries)


@pytest.mark.skipif(not _dev01_ready(), reason=_DEV01_REASON)
def test_dev01_sandbox_primary_compose_export_e2e(tmp_path: Path) -> None:
    outputs, content, export, real_snapshot, boundary_count = _dev01_publish(tmp_path)
    assert boundary_count >= 1
    expected_calls = 2 if boundary_count == 1 else 3
    assert content.cache_hit is False
    assert content.plan_result_source == "primary"
    assert content.chapter_count == boundary_count
    assert content.business_call_count == expected_calls
    assert content.supplier_request_count == expected_calls
    # export成功发布：五件套、真实帧字节逐帧一致、result.json＝正文规范字节
    assert export.status == "success"
    assert export.cache_hit is False and export.pointer_repaired is False
    assert export.degraded_reasons == ()
    assert export.image_omitted_count == 0
    referenced = de._referenced_frame_ids(content.content)
    assert export.image_count == len(referenced) > 0
    group = export.group_path
    assert {entry.name for entry in group.iterdir()} == de._EXPORT_GROUP_FILE_NAMES
    assert (group / de.RESULT_FILE).read_bytes() == _sb_content_file(outputs).read_bytes()
    frames_group = _sb_frames_group(outputs)
    index = _read_json(frames_group / "frame-set.json")
    file_by_id = {str(frame["frame_id"]): str(frame["file"]) for frame in index["frames"]}
    for frame_id in referenced:
        assert (group / de._image_relative_path(frame_id)).read_bytes() == (
            frames_group / file_by_id[frame_id]
        ).read_bytes()
    document = DocxDocument(io.BytesIO((group / de.DOCX_FILE).read_bytes()))
    assert len(document.inline_shapes) == export.image_count
    all_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    for chapter in content.content["chapters"]:
        assert chapter["title"] in all_text
    assert de.EMERGENCY_NOTICE not in all_text
    # export整域缓存复跑零改写
    before = _tree_sha_pairs(_sb_doc_root(outputs))
    second = de.export_document_v2(outputs, _DEV01)
    assert second.cache_hit is True and second.result_id == export.result_id
    assert _tree_sha_pairs(_sb_doc_root(outputs)) == before
    # compose缓存复跑零改写（llm-cache＋组复用，脚本传输未被消费）
    content_before = _tree_sha_pairs(_sb_m2(outputs) / dc.ROOT_DIRECTORY)
    replay = dc.compose_document_v2(
        outputs,
        _DEV01,
        model_registry=_content_registry(tmp_path),
        transport=ScriptedTransport(_dev01_scripted_responses(outputs)),
        environ=ENV,
    )
    assert replay.cache_hit is True and replay.result_id == content.result_id
    assert _tree_sha_pairs(_sb_m2(outputs) / dc.ROOT_DIRECTORY) == content_before
    # 真实outputs/DEV01整树零改写（2026-10-18真实调用验收后新域可合法存在，缺席断言由快照取代）
    assert _tree_sha_pairs(_DEV01_REAL) == real_snapshot


@pytest.mark.skipif(not _dev01_ready(), reason=_DEV01_REASON)
def test_dev01_sandbox_degraded_image_e2e(tmp_path: Path) -> None:
    outputs, content, export, real_snapshot, _ = _dev01_publish(tmp_path)
    assert export.status == "success"
    referenced = de._referenced_frame_ids(content.content)
    victim = referenced[0]
    # 重发布现场：删除已发布组与current（缓存身份不变）
    shutil.rmtree(export.group_path)
    _sb_export_current(outputs).unlink()
    frames_group = _sb_frames_group(outputs)
    index = _read_json(frames_group / "frame-set.json")
    for frame in index["frames"]:
        if frame["frame_id"] == victim:
            (frames_group / str(frame["file"])).unlink()
    result = de.export_document_v2(outputs, _DEV01)
    assert result.status == "degraded"
    assert result.degraded_reasons == (f"{de.REASON_IMAGE_MISSING}:{victim}",)
    assert result.image_count == len(referenced) - 1
    assert result.image_omitted_count == 1
    current = _read_json(_sb_export_current(outputs))
    assert current["result_id"] == result.result_id  # degraded照常发布并切current
    assert (
        de._image_relative_path(victim)
        not in _read_json(result.group_path / de.MANIFEST_FILE)["files"]
    )
    assert not (
        result.group_path / de.IMAGES_DIRECTORY / f"{victim}{de.IMAGE_SUFFIX}"
    ).exists()
    victim_caption = None
    for chapter in content.content["chapters"]:
        for frame in chapter["frames"]:
            if frame["frame_id"] == victim:
                victim_caption = str(frame["caption_text"])
    assert victim_caption
    text = (result.group_path / de.MARKDOWN_FILE).read_text(encoding="utf-8")
    assert victim_caption in text  # 图像降级但说明文本保留（Q5.4）
    document = DocxDocument(io.BytesIO((result.group_path / de.DOCX_FILE).read_bytes()))
    assert len(document.inline_shapes) == result.image_count
    captions = "\n".join(
        paragraph.text
        for paragraph in document.paragraphs
        if paragraph.style.name == de.DOCX_IMAGE_CAPTION_STYLE
    )
    assert victim_caption in captions
    third = de.export_document_v2(outputs, _DEV01)
    assert third.cache_hit is True and third.status == "degraded"
    assert _tree_sha_pairs(_DEV01_REAL) == real_snapshot  # 沙箱删图不触及真实树


@pytest.mark.skipif(not _dev01_ready(), reason=_DEV01_REASON)
def test_dev01_sandbox_emergency_compose_export_e2e(tmp_path: Path) -> None:
    outputs, content, export, real_snapshot, _ = _dev01_publish(tmp_path, emergency=True)
    assert content.plan_result_source == "emergency"
    assert content.chapter_count == 1
    assert content.business_call_count == 2  # 应急两调用（无batch）
    assert content.supplier_request_count == 2
    assert export.status == "success"
    text = (export.group_path / de.MARKDOWN_FILE).read_text(encoding="utf-8")
    assert de.EMERGENCY_NOTICE in text
    document = DocxDocument(io.BytesIO((export.group_path / de.DOCX_FILE).read_bytes()))
    notices = [
        paragraph.text
        for paragraph in document.paragraphs
        if paragraph.style.name == de.DOCX_NOTICE_STYLE
    ]
    assert notices == [de.EMERGENCY_NOTICE]
    assert export.image_count > 0
    # 真实域缺席断言由零改写快照取代（2026-10-18真实调用验收）。
    assert _tree_sha_pairs(_DEV01_REAL) == real_snapshot
