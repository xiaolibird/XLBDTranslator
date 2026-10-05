# -*- coding: utf-8 -*-
"""札记页码全部超出 PDF 总页数时，偏移 0（PDF 页序）不可能成立（2026-10-05）。

实测 IEEE 扫描件 Chow 1970（6 页，印 41–46）：页眉只在 3 页上印了页码，每页页脚的下载水印
又凑出偏移 0 的 3 票，平票按 |offset| 小取胜 → 落到 PDF 页序，41–46 全部截不到。
"""
import fitz

from src.scholar import page_shots as PS


def _doc(tmp_path):
    doc = fitz.open()
    for i in range(6):
        pg = doc.new_page()
        pg.insert_text((60, 300), "body text of the paper")
        if i in (0, 3, 5):                                   # 页眉只印在第 1、4、6 页
            pg.insert_text((500, 30), str(41 + i), fontsize=9)
        if i in (0, 3, 4):                                   # 水印数字恰好等于 PDF 页序
            pg.insert_text((60, 830), "stamp {}".format(i + 1), fontsize=7)
    p = tmp_path / "x.pdf"
    doc.save(str(p))
    return p


def test_out_of_range_note_pages_pick_fitting_offset(tmp_path):
    with fitz.open(str(_doc(tmp_path))) as d:
        pm = PS.build_page_map(d, wanted=[41, 42, 43, 46])
    assert (pm.how, pm.offset) == ("header", 40)
    assert pm.to_pdf(41) == 1 and pm.to_pdf(46) == 6


def test_in_range_note_pages_keep_old_tie_break(tmp_path):
    # 札记页码在 PDF 页序内时两种口径都说得通，不动原有判据（平票取 |offset| 小者）
    with fitz.open(str(_doc(tmp_path))) as d:
        pm = PS.build_page_map(d, wanted=[1, 2, 3])
    assert pm.offset == 0


def test_out_of_range_needs_two_votes(tmp_path):
    doc = fitz.open()
    for i in range(6):
        pg = doc.new_page()
        pg.insert_text((60, 300), "body")
    pg = doc[0]
    pg.insert_text((500, 30), "41", fontsize=9)               # 只有一页印了页码：证据不足
    p = tmp_path / "y.pdf"
    doc.save(str(p))
    with fitz.open(str(p)) as d:
        pm = PS.build_page_map(d, wanted=[41, 45])
    assert pm.how == "pdf-order"                               # 单票时不强行采信（走原有分支）


# ---------------- 页码口径进缓存键 ----------------

def test_passage_cache_invalidated_when_page_map_changes(tmp_path):
    import json
    from test_page_shots import _three_paragraph_pdf, _fake_embed
    pdf = _three_paragraph_pdf(tmp_path)
    calls = []

    def counting(texts):
        calls.append(len(texts))
        return _fake_embed(texts)
    s = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量"}]
    out = tmp_path / "shots"
    PS.passages_for_paper(pdf, "k", s, out, counting)
    n = len(calls)
    PS.passages_for_paper(pdf, "k", s, out, counting)
    assert len(calls) == n                                   # 口径没变：照旧走缓存
    with fitz.open(str(pdf)) as d:
        other = PS.PageMap(d.page_count, 40, "header")
    PS.passages_for_paper(pdf, "k", s, out, counting, page_map=other)
    assert len(calls) > n                                    # 口径变了：旧命中的页码标签作废，重算


def test_legacy_cache_without_pmap_is_kept(tmp_path):
    import json
    from test_page_shots import _three_paragraph_pdf, _fake_embed
    pdf = _three_paragraph_pdf(tmp_path)
    calls = []

    def counting(texts):
        calls.append(len(texts))
        return _fake_embed(texts)
    s = [{"text": "男性更不愿回答性功能问题。", "section": "结果与效应量"}]
    out = tmp_path / "shots"
    PS.passages_for_paper(pdf, "k", s, out, counting)
    n = len(calls)
    for f in (out / "k").glob("passages.*.json"):            # 模拟升级前写下的旧缓存
        d = json.loads(f.read_text("utf-8")); d.pop("pmap", None); f.write_text(json.dumps(d), "utf-8")
    PS.passages_for_paper(pdf, "k", s, out, counting)
    assert len(calls) == n                                   # 旧缓存不因缺字段被整批重算
