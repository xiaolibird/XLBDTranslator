# -*- coding: utf-8 -*-
"""书籍结构脊柱回归：目录切章 / 印刷目录解析 / 页偏移 / manifest 前缀。

每个用例对应旧手工 digest 的一类真实翻车（见 book_ingest 模块文档），
挂了就说明那类翻车会复发，不是「测试太严」。
"""
import json

import pytest

from src.scholar.book_ingest import (
    BookManifest, Chapter, build_chapters, chapter_label, chapter_text,
    detect_page_offset, is_matter, parse_printed_toc,
)


def _toc(*triples):
    return [{"level": lv, "title": t, "key": k} for lv, t, k in triples]


# ---------------- 切章 ----------------

def test_split_level_is_exact_not_at_most():
    """JAMA 形态：L1=Part、L2=章。按「<=2」收会把 Part 混成伪章节。"""
    toc = _toc((1, "The Foundations", 29), (2, "1 How to Use", 31), (2, "2 What Is EBM", 35),
               (1, "Therapy", 85), (2, "7 Therapy", 87))
    chs = build_chapters(toc, n_pages=200, split_level=2)
    assert [c.title for c in chs] == ["1 How to Use", "2 What Is EBM", "7 Therapy"]
    # 上级 Part 给前一章封口：ch2 止于 Part「Therapy」起页前一页
    assert (chs[1].pdf_start, chs[1].pdf_end) == (36, 85)


def test_chapter_end_comes_from_next_sibling_or_parent():
    toc = _toc((2, "1 A", 10), (3, "1.1 a", 12), (2, "2 B", 20))
    chs = build_chapters(toc, n_pages=40, split_level=2)
    assert (chs[0].pdf_start, chs[0].pdf_end) == (11, 20)
    assert (chs[1].pdf_start, chs[1].pdf_end) == (21, 40)   # 末章到全书末页
    assert chs[0].subsections == ["1.1 a"]                  # 深层条目归为子节
    assert chs[0].n_pages == 10


def test_front_and_back_matter_excluded():
    """JAMA 的 Glossary/Index 下挂着 46 个索引字母，与真章同级——不滤会变成 46 个伪章。"""
    toc = _toc((1, "Preface", 5), (2, "1 Real Chapter", 30),
               (1, "Index", 700), (2, "A", 701), (2, "B", 705), (2, "Mc", 706))
    chs = build_chapters(toc, n_pages=726, split_level=2)
    assert [c.title for c in chs] == ["1 Real Chapter"]
    assert is_matter("Glossary") and is_matter("A") and is_matter("参考文献")
    assert not is_matter("14 Harm (Observational Studies)")


def test_chapter_label_extraction():
    assert chapter_label("14 Harm (Observational Studies)") == "14"
    assert chapter_label("Chapter 7 · Therapy") == "7"
    assert chapter_label("第14章 危害") == "14"
    assert chapter_label("Introduction") == ""


def test_build_chapters_empty_toc_returns_empty_not_page_windows():
    """没有目录时必须返回空，让上层报错——退回页窗盲切正是本流水线要消灭的做法。"""
    assert build_chapters([], n_pages=400, split_level=2) == []


# ---------------- 印刷目录 ----------------

_RUBIN_TOC_PAGE = """
      Contents

        Preface to the Third Edition  xi

         Part I  Overview and Basic Approaches  1

1       Introduction  3
1.1    The Problem of Missing Data  3
1.2     Missingness Patterns and Mechanisms  8

2       Missing Data in Experiments  29

3      Complete-Case and Available-Case Analysis, Including
       Weighting Methods  47

           References  405
           Subject Index  437
"""


def _rubin_pages():
    """把上面这页目录放在 PDF 第 5 页；正文 462 页，offset=-12。"""
    pages = [""] * 4 + [_RUBIN_TOC_PAGE] + [""] * 457
    return pages


def test_printed_toc_joins_wrapped_titles():
    """折行条目（页码在第二行）必须合并——只认单行会静默漏掉 Rubin 的 7 个章。"""
    items = parse_printed_toc(_rubin_pages(), [5], page_offset=-12)
    titles = [it["title"] for it in items]
    assert "3 Complete-Case and Available-Case Analysis, Including Weighting Methods" in titles


def test_printed_toc_levels_match_native_convention():
    """印刷目录里 Part→L1、章→L2、小节→L3，与 JAMA 原生书签口径一致（两书同用 split_level=2）。"""
    items = parse_printed_toc(_rubin_pages(), [5], page_offset=-12)
    by_title = {it["title"]: it for it in items}
    assert by_title["Part I Overview and Basic Approaches"]["level"] == 1
    assert by_title["1 Introduction"]["level"] == 2
    assert by_title["1.1 The Problem of Missing Data"]["level"] == 3


def test_printed_toc_converts_printed_to_pdf_pages():
    """印刷页 3 + offset(-12) → PDF 页 15（key 是 0-based，故为 14）。"""
    items = parse_printed_toc(_rubin_pages(), [5], page_offset=-12)
    intro = next(it for it in items if it["title"] == "1 Introduction")
    assert intro["key"] == 14
    chs = build_chapters(items, n_pages=462, split_level=2)
    assert chs[0].pdf_start == 15


def test_printed_toc_backmatter_caps_last_chapter():
    """References 是 L1 边界：不认它，末章会一路吞到 PDF 末页（Rubin 会多算 46 页）。"""
    items = parse_printed_toc(_rubin_pages(), [5], page_offset=-12)
    assert any(it["title"] == "References" and it["level"] == 1 for it in items)
    chs = build_chapters(items, n_pages=462, split_level=2)
    assert chs[-1].pdf_end == 416          # 印刷 405（References）− offset − 1


def test_printed_toc_skips_running_head_and_roman_pages():
    """目录页眉（"vi Contents"）与罗马数字页码条目不得混成章节。"""
    items = parse_printed_toc(_rubin_pages(), [5], page_offset=-12)
    assert not any("Contents" == it["title"] for it in items)
    assert not any("Preface" in it["title"] for it in items)   # 页码是 xi，非阿拉伯数字


# ---------------- 页偏移探测 ----------------

def test_detect_page_offset_majority_vote():
    """页脚孤立数字与页序恒差同一个数 → 众数即 offset。"""
    pages = ["body text\n{}".format(i - 12) for i in range(1, 41)]
    assert detect_page_offset(pages, probe_start=1) == -12


def test_detect_page_offset_returns_none_when_unreliable():
    """证据不足时返回 None 而非猜 0——错的 offset 会把全书页码锚系统性偏掉。"""
    assert detect_page_offset(["no numbers here"] * 30, probe_start=1) is None
    assert detect_page_offset(["body\n{}".format(i) for i in range(1, 4)], probe_start=1) is None


# ---------------- manifest ----------------

def _manifest():
    return BookManifest(
        slug="LittleRubin2020", pdf_path="/tmp/x.pdf", entry_type="book",
        title="Statistical Analysis with Missing Data",
        authors=["Roderick J. A. Little", "Donald B. Rubin"],
        publisher="Wiley", edition="3rd", year=2019, isbn="9781119482260",
        citekey="little2020rubin", n_pages=462, page_offset=-12, toc_source="printed",
        chapters=[{"number": 1, "title": "1 Introduction", "level": 2,
                   "pdf_start": 15, "pdf_end": 40, "label": "1", "subsections": []}])


def test_manifest_prompt_prefix_carries_bibliography():
    """每次 LLM 调用的强制前缀。缺它 = 旧 digest 那种「一本书被猜出 7 个标题」。"""
    pre = _manifest().prompt_prefix()
    assert "Statistical Analysis with Missing Data" in pre
    assert "9781119482260" in pre and "3rd" in pre
    assert "Little" in pre
    assert "-12" in pre                       # 页码换算规则也必须在前缀里
    assert "原书 pp.3-28" in pre              # 目录用印刷页码给出


def test_manifest_round_trips_on_disk(tmp_path):
    m = _manifest()
    m.save(tmp_path)
    back = BookManifest.load(tmp_path)
    assert back.to_dict() == m.to_dict()
    assert back.chapter(1).title == "1 Introduction"
    assert back.chapter(99) is None


def test_manifest_load_tolerates_unknown_fields(tmp_path):
    """未来版本多写的字段不应让旧代码崩——manifest 是长期落盘物。"""
    d = _manifest().to_dict()
    d["some_future_field"] = 1
    (tmp_path / "book.manifest.json").write_text(json.dumps(d), encoding="utf-8")
    assert BookManifest.load(tmp_path).slug == "LittleRubin2020"


def test_chapter_printed_range():
    ch = Chapter(number=1, title="1 Introduction", level=2, pdf_start=15, pdf_end=40)
    assert ch.printed_range(-12) == (3, 28)
    assert ch.n_pages == 26


def test_chapter_text_carries_page_markers():
    """页码标记不是装饰：LLM 与 agent 都靠它把引句对回具体页。"""
    pages = ["p1 body", "p2 body", "p3 body"]
    ch = Chapter(number=1, title="t", level=2, pdf_start=1, pdf_end=2)
    txt = chapter_text(pages, ch)
    assert "[[PDF p.1]]" in txt and "[[PDF p.2]]" in txt
    assert "p3 body" not in txt               # 不越界取下一章的页


# ---------------- 分行形态目录（列感知抽取的产物） ----------------

_RUBIN_TOC_BLOCKWISE = """v
Contents

Preface to the Third Edition
xi

Part I
Overview and Basic Approaches
1

1
Introduction
3
1.1
The Problem of Missing Data
3

3
Complete-Case and Available-Case Analysis, Including
Weighting Methods
47

References
405
"""


def test_printed_toc_parses_blockwise_layout():
    """列感知抽取把目录的三列拆成独立行（编号/标题/页码各一行）。

    这不是退化而是更规整，但一行式正则认不出——实测切到块级抽取后 Rubin 的
    印刷目录从 15 章掉到 0 章。两种形态必须都支持。
    """
    pages = [""] * 4 + [_RUBIN_TOC_BLOCKWISE] + [""] * 457
    items = parse_printed_toc(pages, [5], page_offset=-12)
    by_title = {it["title"]: it for it in items}
    assert by_title["1 Introduction"]["level"] == 2
    assert by_title["1 Introduction"]["key"] == 14           # 印刷 3 → PDF 15
    assert by_title["1.1 The Problem of Missing Data"]["level"] == 3
    assert by_title["Part I Overview and Basic Approaches"]["level"] == 1
    # 折行标题在分行形态下靠状态机累加
    assert ("3 Complete-Case and Available-Case Analysis, Including Weighting Methods"
            in by_title)
    assert by_title["References"]["level"] == 1              # 后置材料仍是边界


def test_blockwise_toc_ignores_roman_paged_frontmatter():
    """罗马数字页码的前言不该混成章节（页码不是阿拉伯数字，状态机收不到收尾信号）。"""
    pages = [""] * 4 + [_RUBIN_TOC_BLOCKWISE] + [""] * 457
    items = parse_printed_toc(pages, [5], page_offset=-12)
    assert not any("Preface" in it["title"] for it in items)


def test_page_text_columnwise_reads_by_column_not_by_row():
    """双栏页必须按栏读：按行读会把左右两栏逐行交错，句子被另一栏从中间劈开。"""
    from src.scholar.book_ingest import _page_text_columnwise

    class _Rect:
        x0, width = 0.0, 100.0

    class _Page:
        rect = _Rect()

        def get_text(self, kind):
            assert kind == "blocks"
            #  (x0, y0, x1, y1, text, bno, btype)
            return [
                (5, 10, 45, 20, "left line one", 0, 0),
                (55, 10, 95, 20, "right line one", 1, 0),
                (5, 30, 45, 40, "left line two", 2, 0),
                (55, 30, 95, 40, "right line two", 3, 0),
            ]

    out = _page_text_columnwise(_Page())
    assert out.split("\n") == ["left line one", "left line two",
                               "right line one", "right line two"]


# ---------------- 分诊探针的覆盖率（长章盲区） ----------------

def test_probe_samples_middle_of_long_chapters():
    """首尾探针对长章是结构性盲区：实测 Rubin 第 15 章（54 页）在 shadow-variable
    轴被判 0 分，而 proxy pattern-mixture / SSIL / tipping point 全在该章中段。"""
    from src.scholar.book_triage import chapter_probe
    pages = ["page {} body text".format(i) for i in range(1, 61)]
    ch = Chapter(number=15, title="15 MNAR", level=2, pdf_start=1, pdf_end=54)
    import re
    seen = [int(m) for m in re.findall(r"\[\[PDF p\.(\d+)\]\]", chapter_probe(pages, ch))]
    assert seen[:3] == [1, 2, 3] and seen[-2:] == [53, 54]      # 首尾仍在
    mid = [p for p in seen if 3 < p < 53]
    assert len(mid) >= 5, "长章必须抽到中段页"
    assert max(mid) > 40, "中段抽样要铺到接近章尾，不能只在前半"


def test_probe_on_short_chapter_stays_contiguous():
    """短章本就被首尾覆盖完，中段抽样不该重复或越界。"""
    from src.scholar.book_triage import chapter_probe
    pages = ["p{}".format(i) for i in range(1, 21)]
    ch = Chapter(number=1, title="short", level=2, pdf_start=5, pdf_end=10)
    import re
    seen = [int(m) for m in re.findall(r"\[\[PDF p\.(\d+)\]\]", chapter_probe(pages, ch))]
    assert seen == sorted(set(seen))                 # 无重复
    assert min(seen) >= 5 and max(seen) <= 10        # 不越界到邻章
