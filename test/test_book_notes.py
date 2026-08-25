# -*- coding: utf-8 -*-
"""书籍落盘层回归：三道 finalize 门 / 专著与编著的引用粒度 / 净删除止损闸。

门禁放宽任一条之前先读 book_notes 的模块文档：每一道都对应一类「未经核验的内容
静默进库」的失效，且书籍链路的产物是要被论文直接引用的。
"""
import json

import pytest

from src.scholar import notes_index as ni
from src.scholar.book_ingest import BookManifest, Chapter
from src.scholar.book_notes import (
    QUOTE_PASS_MIN, book_dir, chapter_bundle_path, check_gates, note_label, note_stem,
    rebuild_book, segments_from_book, write_chapter_bundle,
)


def _manifest(entry_type="book", **kw):
    d = dict(
        slug="TestBook", pdf_path="/tmp/x.pdf", entry_type=entry_type,
        title="Statistical Analysis with Missing Data",
        authors=["Roderick J. A. Little", "Donald B. Rubin"],
        publisher="Wiley", edition="3rd", year=2019, isbn="9781119482260",
        citekey="little2020rubin", n_pages=462, page_offset=-12,
        chapters=[
            {"number": 1, "title": "1 Introduction", "level": 2,
             "pdf_start": 15, "pdf_end": 40, "label": "1", "subsections": []},
            {"number": 2, "title": "2 Missing Data in Experiments", "level": 2,
             "pdf_start": 41, "pdf_end": 58, "label": "2", "subsections": []},
        ])
    d.update(kw)
    return BookManifest(**d)


def _cr(text="缺失机制的分类。", tag="可引用证据", page="4"):
    return {"from_full_text": True, "source": "manual-book",
            "sections": [{"heading": "研究问题",
                          "sentences": [{"text": text, "tag": tag, "page": page}]}]}


def _write_ch(notes_dir, man, number=1, *, status="final", cr=True,
              report={"verified_count": 3}, qv={"total": 5, "passed": 5, "pass_rate": 1.0},
              chapter_meta=None):
    ch = man.chapter(number)
    return write_chapter_bundle(
        chapter_bundle_path(notes_dir, man.slug, number),
        manifest=man, chapter=ch, status=status,
        close_reading_final=_cr() if cr else None,
        cross_check_report=report, quote_verify=qv, chapter_meta=chapter_meta)


# ---------------- 三道门 ----------------

def test_gate_rejects_draft():
    man = _manifest()
    data = {"status": "draft", "close_reading_final": _cr()}
    assert not check_gates(data).ok


def test_gate_rejects_missing_cross_check():
    """status=final 是 agent 自报的；无核验报告即未经亲读核验。"""
    g = check_gates({"status": "final", "close_reading_final": _cr(),
                     "quote_verify": {"total": 1, "pass_rate": 1.0}})
    assert not g.ok and "cross_check_report" in g.reason


def test_gate_rejects_self_reported_zero_verified():
    g = check_gates({"status": "final", "close_reading_final": _cr(),
                     "cross_check_report": {"verified_count": 0},
                     "quote_verify": {"total": 1, "pass_rate": 1.0}})
    assert not g.ok and "verified_count=0" in g.reason


def test_gate_tolerates_legacy_heterogeneous_report():
    """存量报告 schema 异构（verified_count 为 None / 用别的键）——只拒显式的 0。"""
    g = check_gates({"status": "final", "close_reading_final": _cr(),
                     "cross_check_report": {"verified": ["a"]},
                     "quote_verify": {"total": 1, "passed": 1, "pass_rate": 1.0}})
    assert g.ok


def test_gate_rejects_low_quote_pass_rate():
    """书籍链路专属的第三道门：引句对不上原文的章不许进库。"""
    g = check_gates({"status": "final", "close_reading_final": _cr(),
                     "cross_check_report": {"verified_count": 2},
                     "quote_verify": {"total": 10, "passed": 3, "pass_rate": 0.3,
                                      "flagged": [{}] * 7}})
    assert not g.ok and "引文回验通过率" in g.reason


def test_gate_requires_quote_verify_report():
    g = check_gates({"status": "final", "close_reading_final": _cr(),
                     "cross_check_report": {"verified_count": 2}})
    assert not g.ok and "quote_verify" in g.reason


def test_gate_passes_chapter_with_no_verbatim_quotes():
    """全中文归纳的章没有可回验引句（total=0）→ 不因此被拒。"""
    g = check_gates({"status": "final", "close_reading_final": _cr(),
                     "cross_check_report": {"verified_count": 2},
                     "quote_verify": {"total": 0, "passed": 0, "pass_rate": 1.0}})
    assert g.ok


# ---------------- 引用粒度 ----------------

def test_monograph_merges_chapters_into_one_entry(tmp_path):
    """专著：N 章 → 一条条目，章成为精读分节（分节标题带 Ch.N · pp.a-b 溯源）。"""
    man = _manifest("book")
    _write_ch(tmp_path, man, 1)
    _write_ch(tmp_path, man, 2)
    segs, skipped, broken = segments_from_book(tmp_path, man)
    assert len(segs) == 1 and not skipped and not broken
    seg = segs[0]
    assert seg.metadata.entry_type == "book"
    assert seg.metadata.isbn == "9781119482260"
    heads = [s.heading for s in seg.close_reading.sections]
    assert heads[0].startswith("Ch.1 · pp.3-28 ·")
    assert heads[1].startswith("Ch.2 · pp.29-46 ·")


def test_edited_volume_makes_one_entry_per_chapter(tmp_path):
    """编著文集：各章作者不同 → 章才是可引单元，各成一条条目。"""
    man = _manifest("chapter", title="Users' Guides", citekey="guyatt2015users")
    _write_ch(tmp_path, man, 1, chapter_meta={"title": "Harm", "authors": ["Gordon Guyatt"]})
    _write_ch(tmp_path, man, 2, chapter_meta={"title": "Prognosis", "authors": ["Ian Stiell"]})
    segs, _, _ = segments_from_book(tmp_path, man)
    assert len(segs) == 2
    assert [s.metadata.title for s in segs] == ["Harm", "Prognosis"]
    assert segs[0].metadata.entry_type == "chapter"
    assert segs[0].metadata.container_title == "Users' Guides"
    assert segs[0].metadata.book_key == "guyatt2015users"
    assert segs[0].metadata.chapter_number == 1
    assert segs[0].metadata.page_range == "3-28"
    assert segs[0].metadata.authors == ["Gordon Guyatt"]


def test_rejected_chapters_are_reported_not_silently_dropped(tmp_path):
    man = _manifest("book")
    _write_ch(tmp_path, man, 1)
    _write_ch(tmp_path, man, 2, report=None)          # 无核验报告 → 拒收
    segs, skipped, broken = segments_from_book(tmp_path, man)
    assert len(segs) == 1
    assert skipped and "ch02" in skipped[0]


def test_unreadable_bundle_goes_to_broken(tmp_path):
    """读不出的 bundle 必须留痕：只 continue 会让回执照打 ✅ 而内容永久蒸发。"""
    man = _manifest("book")
    _write_ch(tmp_path, man, 1)
    bad = chapter_bundle_path(tmp_path, man.slug, 2)
    bad.write_text("{ not json", encoding="utf-8")
    segs, skipped, broken = segments_from_book(tmp_path, man)
    assert len(segs) == 1 and broken and "ch02" in broken[0]


# ---------------- 落库 ----------------

def test_rebuild_book_writes_note_and_index(tmp_path):
    man = _manifest("book")
    _write_ch(tmp_path, man, 1)
    label = note_label(man, "2026-08-25")
    res = rebuild_book(tmp_path, man, label)
    assert res["papers"] == 1
    md = tmp_path / (note_stem(label) + ".md")
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "[@little2020rubin]" in text          # 专著用 manifest 钉的稳定键
    assert "**所属书籍**" in text and "9781119482260" in text
    assert "〔p.4〕" in text                      # 页码锚渲染进 md
    entry = next(e for e in res["index"]["papers"] if e["series"] == "book")
    assert entry["entry_type"] == "book"
    assert entry["dedup_key"] == "isbn:9781119482260"
    assert entry["highlights"][0]["pages"] == "4"


def test_rebuild_book_chapter_entries_get_chapter_dedup_keys(tmp_path):
    man = _manifest("chapter", citekey="guyatt2015users")
    _write_ch(tmp_path, man, 1, chapter_meta={"title": "Harm", "authors": ["Gordon Guyatt"]})
    _write_ch(tmp_path, man, 2, chapter_meta={"title": "Prognosis", "authors": ["Ian Stiell"]})
    res = rebuild_book(tmp_path, man, note_label(man, "2026-08-25"))
    keys = sorted(e["dedup_key"] for e in res["index"]["papers"] if e["series"] == "book")
    assert keys == ["isbn:9781119482260:ch01", "isbn:9781119482260:ch02"]


def test_net_removal_brake_refuses_to_delete_archived_entries(tmp_path):
    """有 bundle 被拒收时，若本轮会净删掉上一轮已归档条目 → 整本一字不动。"""
    man = _manifest("chapter", citekey="guyatt2015users")
    _write_ch(tmp_path, man, 1, chapter_meta={"title": "Harm", "authors": ["G Guyatt"]})
    _write_ch(tmp_path, man, 2, chapter_meta={"title": "Prognosis", "authors": ["I Stiell"]})
    label = note_label(man, "2026-08-25")
    first = rebuild_book(tmp_path, man, label)
    assert first["papers"] == 2

    # 第二章的报告被人误删 → 会被拒收 → 本轮只剩 1 条，等于净删 1 条
    _write_ch(tmp_path, man, 2, chapter_meta={"title": "Prognosis", "authors": ["I Stiell"]},
              report=None)
    second = rebuild_book(tmp_path, man, label)
    assert second.get("refused") is True
    assert second["removed_keys"] == ["isbn:9781119482260:ch02"]
    # md 未被改写：上一轮的两条仍在
    md = (tmp_path / (note_stem(label) + ".md")).read_text(encoding="utf-8")
    assert "Prognosis" in md


def test_allow_removals_overrides_the_brake(tmp_path):
    man = _manifest("chapter", citekey="guyatt2015users")
    _write_ch(tmp_path, man, 1, chapter_meta={"title": "Harm", "authors": ["G Guyatt"]})
    _write_ch(tmp_path, man, 2, chapter_meta={"title": "Prognosis", "authors": ["I Stiell"]})
    label = note_label(man, "2026-08-25")
    rebuild_book(tmp_path, man, label)
    _write_ch(tmp_path, man, 2, chapter_meta={"title": "Prognosis", "authors": ["I Stiell"]},
              report=None)
    res = rebuild_book(tmp_path, man, label, allow_removals=True)
    assert not res.get("refused") and res["papers"] == 1


def test_no_final_chapters_does_not_write_note(tmp_path):
    man = _manifest("book")
    _write_ch(tmp_path, man, 1, status="draft")
    res = rebuild_book(tmp_path, man, note_label(man, "2026-08-25"))
    assert res["papers"] == 0
    assert not (tmp_path / (note_stem(note_label(man, "2026-08-25")) + ".md")).exists()


def test_book_note_label_is_index_visible():
    """标签必须能被 NOTE_MD_RE 认出，否则整本书对索引/向量库不可见。"""
    label = note_label(_manifest(), "2026-08-25")
    assert ni.validate_note_label(label) == label
    m = ni.NOTE_MD_RE.match(note_stem(label) + ".md")
    assert m and ni._SERIES_MAP[m.group(2)] == "book"
