# -*- coding: utf-8 -*-
"""偏移分段的书：按 PDF 自带页标签换算印刷页（2026-10-05）。

实测 Springer《Metalearning》2e：每个 Part 与每章开头有不编号的扉页，印刷页码在章间跳号，
偏移从第 1 章的 −12 漂到第 16 章的 −6。manifest 只有单一 page_offset=−7 时，各章页码锚
系统性错 2–5 页，而回验的 ±1 页容差照样放行——错误的引用定位符会安静地进库。
另：PyMuPDF 把 UTF-16 页标签的 BOM 原样返回成字面量 `<FEFF>`，不剥掉就 isdigit() 恒假。
"""
import fitz
import pytest

from src.scholar.book_ingest import (
    BookManifest, Chapter, _clean_label, pdf_page_labels,
)
from src.scholar.book_notes import book_pdf_specs
from src.scholar.quote_verify import PageIndex, verify_quote


class _Page:
    def __init__(self, label):
        self._l = label

    def get_label(self):
        return self._l


class _Doc:
    def __init__(self, labels):
        self._p = [_Page(x) for x in labels]
        self.page_count = len(self._p)

    def __getitem__(self, i):
        return self._p[i]


def test_clean_label_strips_literal_and_real_bom():
    assert _clean_label("<FEFF>10") == "10"
    assert _clean_label("﻿10") == "10"
    assert _clean_label("<FEFF>XII") == "XII"
    assert _clean_label("") == ""


def test_pdf_page_labels_keeps_arabic_only_and_first_occurrence():
    doc = _Doc(["<FEFF>I", "<FEFF>II", "<FEFF>1", "<FEFF>3", "<FEFF>4", "", "<FEFF>4"])
    assert pdf_page_labels(doc) == {1: 3, 3: 4, 4: 5}


def test_pdf_page_labels_on_real_pdf_with_skipped_numbers(tmp_path):
    doc = fitz.open()
    for _ in range(6):
        doc.new_page()
    # PDF 第 1–2 页罗马数字，第 3 页起阿拉伯 1 起——再在第 5 页处跳到 10
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                         {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1},
                         {"startpage": 4, "prefix": "", "style": "D", "firstpagenum": 10}])
    p = tmp_path / "x.pdf"
    doc.save(str(p))
    with fitz.open(str(p)) as d:
        assert pdf_page_labels(d) == {1: 3, 2: 4, 10: 5, 11: 6}


def _seg_manifest(**kw):
    # 第 1 章 PDF 14–28 印 3–17（−11）；第 2 章 PDF 29–47 印 19–37（−10）；PDF 13 是无号扉页
    labels = {str(i - 11): i for i in range(14, 29)}
    labels.update({str(i - 10): i for i in range(29, 48)})
    d = dict(slug="Meta", pdf_path="/tmp/x.pdf", entry_type="book", title="Metalearning",
             n_pages=60, page_offset=-7, page_labels=labels,
             chapters=[{"number": 1, "title": "1 Introduction", "level": 1,
                        "pdf_start": 13, "pdf_end": 28},
                       {"number": 2, "title": "2 Rankings", "level": 1,
                        "pdf_start": 29, "pdf_end": 47}])
    d.update(kw)
    return BookManifest(**d)


def test_chapter_printed_range_follows_labels_not_offset():
    m = _seg_manifest()
    c1, c2 = m.chapter_objs()
    # 章首 PDF 13 无标签 → 取章内第一个有标签的页
    assert m.chapter_printed_range(c1) == (3, 17)
    assert m.chapter_printed_range(c2) == (19, 37)
    assert c2.printed_range(-7) == (22, 40)          # 旧算法错 3 页，正是要修的
    assert m.printed_of(29) == 19 and m.printed_of(13) is None


def test_manifest_without_labels_unchanged():
    m = _seg_manifest(page_labels={})
    c1 = m.chapter_objs()[0]
    assert m.chapter_printed_range(c1) == c1.printed_range(-7) == (6, 21)
    assert m.printed_of(20) == 13
    assert "PDF 页序 -7" in m.prompt_prefix()


def test_prompt_prefix_says_labels_and_uses_label_ranges():
    pre = _seg_manifest().prompt_prefix()
    assert "页标签" in pre
    assert "原书 pp.19-37" in pre


def test_manifest_round_trips_page_labels(tmp_path):
    m = _seg_manifest()
    m.save(tmp_path)
    back = BookManifest.load(tmp_path)
    assert back.label_map() == m.label_map()
    assert back.label_map()[19] == 29


def _pages(n=60):
    pages = ["filler page {} ".format(i + 1) * 3 for i in range(n)]
    pages[28] = ("Average ranking aggregates performance across all datasets in the "
                 "metadatabase equally.")               # PDF 29 = 印刷 19
    return pages


def test_page_index_with_labels_addresses_printed_pages():
    m = _seg_manifest()
    idx = PageIndex(_pages(), offset=m.page_offset, labels=m.label_map())
    assert "Average ranking" in idx.printed(19)
    assert idx.printed(22) != idx.printed(19)        # 旧口径 pdf−7 的 22 不再指向这一页
    assert idx.printed_range == (3, 37)
    q = "Average ranking aggregates performance across all datasets"
    assert verify_quote(q, "19", idx).ok
    bad = verify_quote(q, "22", idx)                 # 旧锚：差 3 页，超出 ±1 容差
    assert not bad.ok and bad.found_page == 19


def test_page_index_find_anywhere_skips_unlabeled_pages():
    m = _seg_manifest()
    pages = _pages()
    pages[12] = "Part I Basic Concepts and Architecture of metalearning systems overview"
    idx = PageIndex(pages, offset=m.page_offset, labels=m.label_map())
    assert idx.find_anywhere("part i basic concepts and architecture of metalearning") is None


def test_page_index_without_labels_keeps_offset_behaviour():
    idx = PageIndex(_pages(), offset=-7)
    assert "Average ranking" in idx.printed(22)
    assert idx.printed_range == (-6, 53)


def test_book_pdf_specs_passes_labels_only_when_present():
    m = _seg_manifest()
    data = {"chapter": {"number": 2, "pdf_start": 29, "pdf_end": 47}}
    spec = book_pdf_specs(m, [(data, None)])["book:Meta"]
    assert spec["labels"][19] == 29 and spec["ranges"] == [(29, 47)]
    plain = book_pdf_specs(_seg_manifest(page_labels={}), [(data, None)])["book:Meta"]
    assert "labels" not in plain and plain["offset"] == -7
