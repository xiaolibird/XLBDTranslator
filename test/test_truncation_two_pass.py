# -*- coding: utf-8 -*-
"""截断双修（2026-09-05）：两轮读 + 成因拆字段 + 叠式分数修复 + 参考文献区剔除。

对应台账：docs/bugs/2026-09-04-closeread-still-truncates.md、
2026-09-04-truncated-flag-conflates-two-causes.md、2026-09-03-ingest-draft-systematic-errors.md 第 1 类。
全部用 fitz 现场造 PDF，不依赖桌面文件；真 PDF 夹具（zhao2024 第 8 页的 β3 = 8/3）存在时多跑一条。
"""
import json
import math
from pathlib import Path

import fitz
import pytest

from src.scholar import closereading as cr
from src.scholar.schema import (CloseReading, CloseReadSection, CloseReadSentence,
                                PaperMetadata, PaperSegment)

REAL_FRACTION_PDF = Path("/Users/xiaolibird/Desktop/Lab/Reading/2026-09-03-景观地基/zhao2024_eprnet_landscape.pdf")


# ---------------- 夹具：现场造 PDF ----------------

def _pdf_with_pages(path: Path, texts):
    doc = fitz.open()
    for t in texts:
        page = doc.new_page(width=595, height=842)
        y = 60
        for line in t.split("\n"):
            page.insert_text((40, y), line, fontsize=9)
            y += 12
            if y > 800:
                page = doc.new_page(width=595, height=842)
                y = 60
    doc.save(str(path))
    doc.close()


def _pdf_with_fraction(path: Path, num="8", den="3", bar_w=8.0, extra_bar=False, decoy=None):
    """一页：一句话里嵌一个叠式分数（分子在线上、分母在线下、中间一条短横线）。

    decoy：在页面下方（40,400）另插一行普通文本（如「8 3」），用来测「不相邻的分数
    不得改写别处的诱饵数字」。
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((40, 200), "where beta1 = 10, beta2 = 28 and beta3 =", fontsize=9)
    x0 = 232.0
    y_bar = 197.0
    page.insert_text((x0 + 1.5, y_bar - 2.5), num, fontsize=8)      # 分子（基线在线上方）
    page.insert_text((x0 + 1.5, y_bar + 8.5), den, fontsize=8)      # 分母（基线在线下方）
    sh = page.new_shape()
    sh.draw_line((x0, y_bar), (x0 + bar_w, y_bar))
    if extra_bar:                                                   # 同一高度另一条线 = 表格行线
        sh.draw_line((x0 + 60, y_bar), (x0 + 68, y_bar))
    sh.finish(width=0.5)
    sh.commit()
    page.insert_text((x0 + 14, 200), ". We add noise.", fontsize=9)
    if decoy:
        page.insert_text((40, 400), decoy, fontsize=9)
    doc.save(str(path))
    doc.close()


# ---------------- ① 抽文量尺：两种成因分开记 ----------------

def test_pdf_text_ex_separates_page_cap_from_total_cap(tmp_path):
    p = tmp_path / "t.pdf"
    _pdf_with_pages(p, ["A" * 50 + "\n" + "B" * 50, ("C" * 60 + "\n") * 12, "D" * 80])
    doc = fitz.open(str(p))
    raw_pages = [pg.get_text("text", sort=True) for pg in doc]
    raw_total = sum(len(t) for t in raw_pages)
    page_cap = 300
    total_cap = 260
    ex = cr._pdf_text_ex(p, max_chars=total_cap, page_max_chars=page_cap, fix_fractions=False)
    capped = "".join(t[:page_cap] for t in raw_pages)
    assert ex.raw_chars == raw_total, "raw 不受任何上限影响"
    assert ex.page_dropped == sum(max(0, len(t) - page_cap) for t in raw_pages)
    assert ex.page_dropped > 0 and ex.truncated_by_page_cap
    assert ex.text == capped[:total_cap] and ex.tail == capped[total_cap:]
    assert ex.truncated_by_total_cap and len(ex.text) == total_cap
    assert ex.n_pages == 3
    # 老签名照旧：(text, raw)
    t, r = cr._pdf_text_with_stats(p, max_chars=total_cap, page_max_chars=page_cap)
    assert (t, r) == (ex.text, ex.raw_chars)


def test_pdf_text_ex_no_truncation_when_budget_is_enough(tmp_path):
    p = tmp_path / "s.pdf"
    _pdf_with_pages(p, ["short text"])
    ex = cr._pdf_text_ex(p, max_chars=10_000, page_max_chars=10_000, fix_fractions=False)
    assert ex.tail == "" and ex.page_dropped == 0
    assert not ex.truncated_by_total_cap and not ex.truncated_by_page_cap


def test_pdf_text_ex_honours_a_legacy_monkeypatched_stub(monkeypatch, tmp_path):
    """既有测试 monkeypatch 的是 `_pdf_text_with_stats`；新入口必须尊重那个替身，
    否则几十条老测试会去真解析一个不存在的 PDF、静默退到摘要路径。"""
    monkeypatch.setattr(cr, "_pdf_text_with_stats",
                        lambda path, max_chars=40000, page_max_chars=None: ("BODY", 99))
    ex = cr._pdf_text_ex(tmp_path / "nope.pdf", max_chars=10)
    assert (ex.text, ex.raw_chars, ex.tail, ex.page_dropped) == ("BODY", 99, "", 0)


def test_set_truncation_causes_from_stats_and_from_epmc():
    c = CloseReading(sections=[CloseReadSection(heading="h", sentences=[CloseReadSentence(text="s")])])
    ex = cr.PdfText(text="x" * 10, raw_chars=100, page_dropped=5, tail="y" * 20)
    cr._set_truncation_causes(c, ex)
    assert c.truncated_by_total_cap is True and c.truncated_by_page_cap is True
    c2 = CloseReading(sections=c.sections)
    c2.truncated = True
    cr._set_truncation_causes(c2, None)                 # EPMC 路径：没有页，只能判总上限
    assert c2.truncated_by_total_cap is True and c2.truncated_by_page_cap is False


# ---------------- ② 叠式分数 ----------------

def test_stacked_fraction_detected_on_a_synthetic_page(tmp_path):
    """几何判据：短横线 + 上下各一个短词块 → 认出 (8, 3)，且同一条线只报一次。

    文本层的改写在这里**不**断言：fitz 现场造的页里分子/分母各成一个 span，`sort=True` 后
    落在行块两端、并不相邻，与真实排版（分子分母紧挨着拼成 `83`）不同——那一半由
    test_fix_stacked_fractions_only_rewrites_a_unique_adjacent_pair（字符串层）与
    test_real_pdf_beta3_is_read_as_eight_thirds（真 PDF）各守一半。"""
    p = tmp_path / "f.pdf"
    _pdf_with_fraction(p)
    page = fitz.open(str(p))[0]
    fr = cr.stacked_fractions(page)
    # 三元组：(分子, 分母, 是否在阅读顺序里相邻)；相邻与否取决于 fitz 的排版，改写只在相邻时发生
    assert [f[:2] for f in fr] == [("8", "3")] and isinstance(fr[0][2], bool)
    ex = cr._pdf_text_ex(p, max_chars=10_000, fix_fractions=True)
    assert ex.n_pages == 1 and ex.fractions_fixed in (0, 1)
    ex_off = cr._pdf_text_ex(p, max_chars=10_000, fix_fractions=False)
    assert ex_off.fractions_fixed == 0 and "8/3" not in ex_off.text


def test_table_rule_is_not_a_fraction_bar(tmp_path):
    """同一高度上还有别的线段 = 表格行线被切成一格一格，不是分数线（30 份真 PDF 扫描的主要假阳性）。"""
    p = tmp_path / "t.pdf"
    _pdf_with_fraction(p, extra_bar=True)
    page = fitz.open(str(p))[0]
    assert cr.stacked_fractions(page) == []


@pytest.mark.parametrize("num,den,ok", [
    ("8", "3", True), ("1", "n", True), ("dt", "2", False), ("r", "2", True),
    ("Embed", "Angle", False), ("from", "2", False), ("[13],", "in", False),
    ("94.2", "-0.242", False), ("0.97", "NA", False), ("2000", "-0.082", False),
    ("do(X", ")", False), ("1", "2π", True), ("σ2", "2", True),
    # R2-3.1：两个纯数字里任一个带 ≥2 位小数就放行不得改写（0.97/0.93 是概率值不是 97/93）；
    # 带一位小数的分数（1.5/2）仍算分数。
    ("0.97", "0.93", False), ("12.34", "5", False), ("1.5", "2", True),
])
def test_fraction_like_gates(num, den, ok):
    assert cr._fraction_like(num, den) is ok


def test_fix_stacked_fractions_only_rewrites_a_unique_adjacent_pair():
    text = "where beta3 = 8\n3 . We add noise 8 and 3 elsewhere: 1 N and again 1 N"
    out, n = cr.fix_stacked_fractions(text, [("8", "3"), ("1", "N")])
    assert n == 1 and "beta3 = 8/3 ." in out
    assert "1/N" not in out, "1 N 出现两次 → 有歧义 → 一律不改"


def test_fraction_fix_switch_comes_from_env(monkeypatch):
    monkeypatch.setenv("XLBD_PDF_FRACTION_FIX", "0")
    assert cr._fraction_fix_enabled() is False
    monkeypatch.setenv("XLBD_PDF_FRACTION_FIX", "1")
    assert cr._fraction_fix_enabled() is True
    monkeypatch.delenv("XLBD_PDF_FRACTION_FIX", raising=False)
    assert cr._fraction_fix_enabled() is True


def test_settings_bridge_writes_the_env_switch(tmp_path, monkeypatch):
    from src.scholar.settings import load_scholar_settings
    env = tmp_path / "s.env"
    env.write_text("GMAIL__CREDENTIALS_PATH=x\nGMAIL__TOKEN_PATH=y\nLLM__PROVIDER=deepseek\n"
                   "LLM__MODEL=m\nPROCESSING__PDF_FRACTION_FIX=false\n", encoding="utf-8")
    monkeypatch.delenv("XLBD_PDF_FRACTION_FIX", raising=False)
    load_scholar_settings(str(env))
    import os
    assert os.environ.get("XLBD_PDF_FRACTION_FIX") == "0"


@pytest.mark.skipif(not REAL_FRACTION_PDF.exists(), reason="真 PDF 夹具不在本机")
def test_real_pdf_beta3_is_read_as_eight_thirds():
    ex = cr._pdf_text_ex(REAL_FRACTION_PDF, max_chars=10_000_000, fix_fractions=True)
    assert "β3 = 8/3" in ex.text
    assert "β3 = 83" not in ex.text


# ---------------- ③ 参考文献区 ----------------

def _refs_block(n=60):
    return "\n".join(
        "[{}] Author{}, A., Other, B. (20{:02d}). A title about things {}. J. Things, vol. {}, pp. {}-{}. "
        "doi:10.1000/x{}".format(i, i, i % 25, i, i % 9 + 1, i, i + 8, i) for i in range(1, n + 1))


def test_strip_reference_block_keeps_body_and_appendix_in_order():
    body = ("This is prose about methods and results. " * 150)
    refs = _refs_block(80)
    appendix = ("Appendix A. Extra derivations and tables. " * 80)
    text = body + "\nReferences\n" + refs + "\n" + appendix
    rest, cut = cr.strip_reference_block(text)
    assert cut >= 4000
    assert "Author1, A." not in rest or rest.count("doi:10.1000") < 5
    assert rest.startswith(body[:200]) and "Appendix A. Extra derivations" in rest
    assert rest.index("prose about methods") < rest.index("Appendix A.")


def test_strip_reference_block_is_conservative_on_short_or_plain_text():
    plain = "no citations here, just prose. " * 300
    assert cr.strip_reference_block(plain) == (plain, 0)
    assert cr.strip_reference_block("tiny") == ("tiny", 0)


# ---------------- ④ 两轮读第二轮 ----------------

def _seg():
    return PaperSegment(segment_id=1, paper_id="p1", priority_score=1.0,
                        metadata=PaperMetadata(paper_id="p1", title="T"))


def _cr(headings, n_chunks=3):
    c = CloseReading(from_full_text=True, source="unpaywall", sections=[
        CloseReadSection(heading=h, sentences=[CloseReadSentence(text="s{}".format(i))])
        for i, h in enumerate(headings)])
    c.n_chunks = n_chunks
    c.reading_depth = "chunked"
    return c


def test_read_tail_appendix_merges_prefixed_sections(monkeypatch):
    seen = {}

    def fake_deep(seg, body, ri, llm, model=None, source=None, from_full_text=True, max_chunks=12, diag=None):
        seen["body"] = body
        seen["max_chunks"] = max_chunks
        return _cr(["结果与效应量", "图表与补充材料要点"], n_chunks=2)
    monkeypatch.setattr(cr, "deep_close_read", fake_deep)
    main = _cr(["研究问题"], n_chunks=3)
    tail = ("Appendix B. More results with numbers 0.87 and 0.91. " * 120)
    fed = cr.read_tail_appendix(main, _seg(), tail, "ri", llm=None, model="m",
                                max_chars=3000, max_chunks=4, source="unpaywall")
    # 返回的是**真正喂进模型的尾段**（R1-2.6：数字回查的对照源只能是模型读过的文本）
    assert fed == seen["body"] and len(fed) == 3000 and seen["max_chunks"] == 4
    assert [s.heading for s in main.sections] == ["研究问题", "附录·结果与效应量", "附录·图表与补充材料要点"]
    assert main.n_chunks == 5


def test_read_tail_appendix_skips_when_tail_is_only_references(monkeypatch):
    called = []
    monkeypatch.setattr(cr, "deep_close_read", lambda *a, **k: called.append(1) or _cr(["x"]))
    diag = {}
    fed = cr.read_tail_appendix(_cr(["研究问题"]), _seg(), _refs_block(90), "ri", llm=None, model="m",
                                max_chars=40000, max_chunks=4, source="unpaywall", diag=diag)
    assert fed == "" and not called and "tail_skipped" in diag and diag["tail_refs_cut"] > 0


def test_read_tail_appendix_off_when_budget_is_zero(monkeypatch):
    monkeypatch.setattr(cr, "deep_close_read", lambda *a, **k: pytest.fail("不该调"))
    assert cr.read_tail_appendix(_cr(["a"]), _seg(), "x" * 9000, "ri", None, "m",
                                 max_chars=0, max_chunks=4, source="s") == ""


def test_close_read_segment_runs_second_pass_and_records_causes(monkeypatch, tmp_path):
    """端到端（打桩网络与模型）：尾段存在且开关开 → 第二轮跑、成因两项都记、tail_chars 落下。"""
    class _OA:
        pdf_url = "http://x/y.pdf"
        source = "unpaywall"
        candidates = []
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="", client=None, **kw: _OA())
    monkeypatch.setattr(cr, "download_pdf", lambda url, dest, **kw: tmp_path / "y.pdf")
    body = "Main body prose. " * 500
    tail = ("Appendix with content and numbers 12 and 34. " * 200)
    monkeypatch.setattr(cr, "_pdf_text_ex",
                        lambda path, max_chars=40000, page_max_chars=None, fix_fractions=None:
                        cr.PdfText(text=body, raw_chars=len(body) + len(tail) + 500,
                                   page_dropped=500, tail=tail, n_pages=9))
    calls = []

    def fake_deep(seg, text, ri, llm, model=None, source=None, from_full_text=True, max_chunks=12, diag=None):
        calls.append(len(text))
        return _cr(["研究问题"] if len(calls) == 1 else ["结果与效应量"], n_chunks=1)
    monkeypatch.setattr(cr, "deep_close_read", fake_deep)

    seg = _seg()
    out = cr.close_read_segment(seg, "ri", llm=None, deep=True, max_chars=137400,
                                tail_max_chars=5000, tail_max_chunks=2)
    assert out is not None and len(calls) == 2 and calls[1] <= 5000
    assert out.truncated is True and out.truncated_by_total_cap is True and out.truncated_by_page_cap is True
    assert out.tail_chars == calls[1] and out.body_chars == len(body)
    assert [s.heading for s in out.sections] == ["研究问题", "附录·结果与效应量"]

    calls.clear()
    out2 = cr.close_read_segment(_seg(), "ri", llm=None, deep=True, max_chars=137400, tail_max_chars=0)
    assert len(calls) == 1 and out2.tail_chars is None and out2.truncated_by_total_cap is True


# ---------------- ⑤ 量尺透传：sidecar → 回读 ----------------

def test_rehydrate_reads_the_cause_fields_from_sidecar(tmp_path):
    from src.scholar import ingest as ING
    from src.scholar.schema import PaperMetadata as PM
    nd = tmp_path
    stem = "科研札记_2026-09_全文精读"
    (nd / (stem + ".md")).write_text(
        "# 科研札记\n\n## 🔴 高 1. Title [@wang2026Title]\n\n### 摘要\nabs\n\n"
        "### 全文精读 · 来源 `unpaywall`\n\n**【研究问题】**\n- 句子。\n\n---\n", encoding="utf-8")
    meta = PM(paper_id="p1", title="Title", doi="10.1/abc")
    from src.scholar.ingest import dedup_key
    (nd / (stem + ".index.json")).write_text(json.dumps({"papers": [{
        "citekey": "wang2026Title", "title": "Title", "doi": "10.1/abc", "dedup_key": dedup_key(meta),
        "fulltext_chars": 137400, "fulltext_chars_raw": 260000, "fulltext_truncated": True,
        "fulltext_truncated_by_total_cap": True, "fulltext_truncated_by_page_cap": False,
        "fulltext_tail_chars": 46200}]}), encoding="utf-8")
    seg = PaperSegment(segment_id=1, paper_id="p1", metadata=meta, priority_score=1.0)
    n = ING._rehydrate_close_readings(nd, stem, [seg])
    assert n == 1
    c = seg.close_reading
    assert (c.body_chars, c.body_chars_raw, c.truncated) == (137400, 260000, True)
    assert (c.truncated_by_total_cap, c.truncated_by_page_cap, c.tail_chars) == (True, False, 46200)


def test_entry_builder_and_sidecar_writer_carry_the_cause_keys():
    """接线守卫：索引条目构造与补深度批的 sidecar 写回都要带这三个键（消费方按键名读）。"""
    import inspect
    from src.scholar import _citekey_utils as CU
    import scripts.backfill_deepread as bd
    src = inspect.getsource(CU)
    for k in ("fulltext_truncated_by_total_cap", "fulltext_truncated_by_page_cap", "fulltext_tail_chars"):
        assert '"{}"'.format(k) in src, k
        assert '"{}"'.format(k) in inspect.getsource(bd), k


# ---------------- ⑥ 第二轮预算与块顶的耦合 ----------------

def test_tail_budget_fits_its_chunk_cap():
    from src.scholar import pdf_ingest
    from src.scholar.settings import ProcessingSettings
    import inspect
    sig = inspect.signature(pdf_ingest.chunk_text)
    size, overlap = sig.parameters["size"].default, sig.parameters["overlap"].default
    s = ProcessingSettings()
    n = math.ceil((s.closeread_tail_max_chars - overlap) / (size - overlap))
    assert n == s.closeread_tail_max_chunks, "第二轮字符上限与块顶脱钩：改一个必须同时改另一个"


# ---------------- ⑦ 第 1 轮审计补的守卫 ----------------

def test_spy_style_monkeypatch_does_not_recurse(monkeypatch, tmp_path):
    """R1-1.2：替身内部调回原函数（spy 写法）不能绕成无限递归。"""
    orig = cr._pdf_text_with_stats
    calls = []

    def spy(path, max_chars=40000, page_max_chars=None):
        calls.append(1)
        return orig(path, max_chars=max_chars, page_max_chars=page_max_chars)
    monkeypatch.setattr(cr, "_pdf_text_with_stats", spy)
    p = tmp_path / "s.pdf"
    _pdf_with_pages(p, ["hello world"])
    ex = cr._pdf_text_ex(p, max_chars=1000)
    assert "hello" in ex.text and calls == [1]


def test_epmc_source_also_gets_a_second_pass(monkeypatch):
    """R1-1.1：EPMC 正文没有页，但总上限之外的余量同样走第二轮——「by_total_cap ⇒ 两轮读能救」
    对两条全文来源都成立。"""
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="", client=None, **kw: None)
    body = "EPMC body. " * 3000
    tail = "Appendix from JATS with numbers 12 and 34. " * 200
    monkeypatch.setattr(cr, "europepmc_fulltext",
                        lambda **kw: (body[:kw["max_chars"]], len(body) + len(tail), tail))
    calls = []

    def fake_deep(seg, text, ri, llm, model=None, source=None, from_full_text=True, max_chunks=12, diag=None):
        calls.append(len(text))
        return _cr(["研究问题"] if len(calls) == 1 else ["结果与效应量"], n_chunks=1)
    monkeypatch.setattr(cr, "deep_close_read", fake_deep)
    diag = {}
    out = cr.close_read_segment(_seg(), "ri", llm=None, deep=True, max_chars=20000,
                                tail_max_chars=5000, tail_max_chunks=2, diag=diag)
    # 来源看 diag（_cr 替身把 source 写死成 unpaywall，真 deep_close_read 才会按参数回填）
    assert diag["source"] == "europepmc" and len(calls) == 2
    assert out.truncated_by_total_cap is True and out.truncated_by_page_cap is False
    assert out.tail_chars == calls[1] and "附录·结果与效应量" in [s.heading for s in out.sections]


def test_strip_reference_block_snaps_to_entry_lines_and_ignores_subscript_brackets():
    """R1-2.2：边界收缩到行——正文最后一段与附录第一段不再被整窗吞掉；`x[k]` 这类下标不算引文标记。"""
    body_tail = "The final paragraph of the discussion ends here with x[k] and w[1] indices. "
    body = ("Prose about methods. " * 120) + body_tail
    refs = "\nReferences\n" + _refs_block(70) + "\n"
    appendix_head = "Appendix A. Proof of Theorem 1 begins here. "
    appendix = appendix_head + ("More derivations. " * 150)
    rest, cut = cr.strip_reference_block(body + refs + appendix)
    assert cut > 0
    assert body_tail.strip() in rest, "参考文献前的最后一段正文必须保住"
    assert appendix_head in rest, "附录第一段必须保住"
    # 下标方括号密集的正文不被当成引文区
    sub = ("we update x[k] from x[k-1] and w[1], w[2] each step; see eq. (3). " * 200)
    assert cr.strip_reference_block(sub) == (sub, 0)


# ---------------- ⑧ 第 2 轮审计补的守卫（R2-3.1~3.6） ----------------

def test_non_adjacent_fraction_is_not_rewritten_despite_decoy(tmp_path):
    """R2-3.2：分子/分母在阅读顺序里不相邻（adjacent=False）时，即便页面下方另有一处
    「8 3」诱饵，改写也不得发生——改写靠「唯一相邻对」这一文本证据，不是见分数就改。"""
    p = tmp_path / "d.pdf"
    _pdf_with_fraction(p, decoy="candidate 8 3 appears again here")
    page = fitz.open(str(p))[0]
    assert cr.stacked_fractions(page) == [("8", "3", False)]
    ex = cr._pdf_text_ex(p, max_chars=10_000, fix_fractions=True)
    assert ex.fractions_fixed == 0 and "8/3" not in ex.text


def test_fix_stacked_fractions_skips_a_non_adjacent_triple():
    """R2-3.3：字符串层同样只改「相邻」的分数——三元组第三位 False 时原样返回（0 处改动）。"""
    s = "beta3 = 8\n3 ."
    assert cr.fix_stacked_fractions(s, [("8", "3", False)]) == (s, 0)


def test_strip_reference_block_keeps_sparse_citation_prose():
    """R2-3.4：正文只有零散引用（每 2000 字符窗口 ~4 个年份标记，低于密度门槛 7），
    不是参考文献区，原样返回。现有保守用例是零标记，这条补「有标记但稀疏」这一档。"""
    chunk = ("We report a longitudinal observational study drawing on a large multi-centre registry "
             "that spans several jurisdictions and years, detailing in turn the covariate set, the "
             "handling of missing values through multiple imputation, the propensity weighting scheme, "
             "the outcome definitions and adjudication procedures, the subgroup analyses, and a battery "
             "of sensitivity analyses presented in 2019. ")
    text = chunk * 12                       # ≈5400 字符，每窗 ~4 标记 < 7
    assert len(text) >= 4000
    assert cr.strip_reference_block(text) == (text, 0)


def test_strip_reference_block_keeps_citation_dense_discussion_prose():
    """R2-3.5：引用密集的讨论段（每窗 `(Smith et al., 2019)` 式引用可达 11~12 个标记）
    没有「像条目的行」——整窗切会把正文当参考文献剔掉，必须原样返回（真 bug，已修）。"""
    unit = ("Prior work (Smith et al., 2019) reported X, later (Jones et al., 2020) found Y, "
            "and (Lee et al., 2021) revisited Z. ")
    text = unit * 40
    assert len(text) >= 4000
    assert cr.strip_reference_block(text) == (text, 0)


def test_close_read_segments_forwards_tail_budget(monkeypatch):
    """R2-3.6：close_read_segments 必须把 tail_max_chars / tail_max_chunks 透传给
    close_read_segment——删掉这一传参，两轮读在批量入口上就静默关掉了。"""
    captured = {}
    monkeypatch.setattr(cr, "close_read_segment",
                        lambda seg, ri, llm, **kw: captured.update(kw) or _cr(["研究问题"], n_chunks=1))
    n = cr.close_read_segments([_seg()], "ri", llm=None, top_n=1, prefer_full_text=False,
                               deep=True, max_chars=137400, tail_max_chars=5000, tail_max_chunks=2)
    assert n == 1
    assert captured["tail_max_chars"] == 5000 and captured["tail_max_chunks"] == 2


def test_routes_suppressed_lands_in_diag(monkeypatch):
    """R1-2.4：额外通道被熔断/额度抑制过，diag 要带 routes_suppressed，让账本与 no_output 分开。"""
    class _OA:
        pdf_url = None
        source = None
        candidates = []
        extra = {"routes_suppressed": 2}
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="", client=None, **kw: _OA())
    monkeypatch.setattr(cr, "europepmc_fulltext", lambda **kw: (None, 0, ""))
    diag = {}
    seg = _seg()
    seg.original_abstract = ""
    assert cr.close_read_segment(seg, "ri", llm=None, deep=False, diag=diag) is None
    assert diag["routes_suppressed"] == 2 and diag["from_full_text"] is False


def test_second_pass_exception_keeps_first_pass_result(monkeypatch, tmp_path):
    """R1-2.7：第二轮是加法——它自己炸了不能把第一轮的产物拖成 None。"""
    class _OA:
        pdf_url = "http://x/y.pdf"
        source = "unpaywall"
        candidates = []
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="", client=None, **kw: _OA())
    monkeypatch.setattr(cr, "download_pdf", lambda url, dest, **kw: tmp_path / "y.pdf")
    monkeypatch.setattr(cr, "_pdf_text_ex",
                        lambda path, max_chars=40000, page_max_chars=None, fix_fractions=None:
                        cr.PdfText(text="body " * 2000, raw_chars=99999, tail="tail " * 3000))
    monkeypatch.setattr(cr, "deep_close_read", lambda *a, **k: _cr(["研究问题"], n_chunks=1))

    def boom(*a, **k):
        raise RuntimeError("synth exploded in pass 2")
    monkeypatch.setattr(cr, "read_tail_appendix", boom)
    diag = {}
    out = cr.close_read_segment(_seg(), "ri", llm=None, deep=True, max_chars=137400,
                                tail_max_chars=5000, diag=diag)
    assert out is not None and [s.heading for s in out.sections] == ["研究问题"]
    assert out.tail_chars is None and "synth exploded" in diag["tail_failed"]
