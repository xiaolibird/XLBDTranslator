# -*- coding: utf-8 -*-
"""引文回验回归：归一化口径 / 页码寻址 / 判据严格性。

这一层是书籍链路唯一挡住「引句被改写或杜撰」的机制，且必须是确定性的
（FABLES 实测 LLM 忠实性裁判在书级摘要上不可靠）。放宽任一断言前先读 quote_verify 的模块文档。
"""
import pytest

from src.scholar.quote_verify import (
    PageIndex, QuoteCheck, MIN_QUOTE_CHARS, extract_verbatim, normalize,
    parse_page_anchor, verify_close_reading, verify_quote,
)
from src.scholar.schema import CloseReading, CloseReadSection, CloseReadSentence


# ---------------- 归一化 ----------------

def test_normalize_ligatures_and_quotes():
    assert normalize("the ﬁrst ﬂoor") == "the first floor"
    assert normalize("“smart” ‘quotes’") == '"smart" \'quotes\''
    assert normalize("en–dash em—dash") == "en-dash em-dash"


def test_normalize_joins_linebreak_hyphen():
    """PDF 断行连字符必须合并，否则整句引文永远匹配不上。"""
    assert normalize("impu-\ntation methods") == "imputation methods"
    # 真正的复合词连字符不能被吃掉（没有换行就不合并）
    assert normalize("missing-data mechanism") == "missing-data mechanism"
    # 跨行的复合词：合并后仍是一个词，这是 PDF 抽文的既有形态，接受
    assert normalize("missing-\ndata") == "missingdata"


def test_normalize_collapses_whitespace():
    assert normalize("a   b\n\n c\t d") == "a b c d"


def test_parse_page_anchor():
    assert parse_page_anchor("247") == [247]
    assert parse_page_anchor("241-259") == [241, 259]
    assert parse_page_anchor("8–12") == [8, 12]
    assert parse_page_anchor("247,249") == [247, 249]
    assert parse_page_anchor(None) == []
    assert parse_page_anchor("p. iv") == []


# ---------------- 页码寻址 ----------------

_Q = "Missing data are unobserved values that would be meaningful if observed"


def _index(offset=-12):
    """5 页假书。PDF p.15 = 印刷 p.3（offset=-12，与 Little & Rubin 实测一致）。"""
    pages = [""] * 14 + [
        "page three text " + _Q,                       # pdf 15 → printed 3
        "page four text about monotone patterns here",  # pdf 16 → printed 4
        "page five text with ﬁnite sample properties",  # pdf 17 → printed 5
    ]
    return PageIndex(pages, offset=offset)


def test_printed_addressing_and_range():
    idx = _index()
    assert idx.printed_range == (1 - 12, 17 - 12)       # (-11, 5)：含未寻址的前置页
    assert _Q.lower() in idx.printed(3).lower()
    assert idx.printed(999) == ""                       # 越界返回空串，不抛


def test_verify_quote_exact_hit():
    chk = verify_quote(_Q, "3", _index())
    assert chk.ok and chk.found_page == 3


def test_verify_quote_slack_one_page():
    """版面跨页与偏移差一页是常态 → ±1 放行；差两页不放行。"""
    assert verify_quote(_Q, "4", _index()).ok
    assert verify_quote(_Q, "2", _index()).ok
    bad = verify_quote(_Q, "5", _index())
    assert not bad.ok and "实际在 p.3" in bad.reason


def test_verify_quote_altered_text_is_rejected():
    """改过一个词的引句必须判失败——模糊匹配会放过的正是这类。"""
    tampered = _Q.replace("meaningful", "meaningless")
    chk = verify_quote(tampered, "3", _index())
    assert not chk.ok and "未找到" in chk.reason


def test_verify_quote_fabricated_is_rejected():
    chk = verify_quote("This sentence appears nowhere in the entire book at all", "3", _index())
    assert not chk.ok


def test_verify_quote_normalizes_before_matching():
    """引句写成键盘字符、原文是连字/花引号 → 归一后仍应命中。"""
    assert verify_quote("page five text with finite sample properties", "5", _index()).ok


def test_short_quote_is_rejected_not_silently_passed():
    """太短的引句在任一页都可能偶然命中，验了等于没验，故判失败而非通过。"""
    chk = verify_quote("page three", "3", _index())
    assert not chk.ok and "过短" in chk.reason
    assert len("page three") < MIN_QUOTE_CHARS


def test_missing_anchor_is_flagged_with_actual_page():
    """缺页码锚不算通过，但要给出「其实在第几页」的可操作提示。"""
    chk = verify_quote(_Q, None, _index())
    assert not chk.ok and chk.found_page == 3 and "缺页码锚" in chk.reason


# ---------------- 逐字片段抽取 ----------------

def test_extract_verbatim_only_takes_quoted_spans():
    text = '原文定义："{}"，这说明填补才有意义。'.format(_Q)
    assert extract_verbatim(text) == [_Q]
    # 纯中文归纳句不含逐字引文 → 不进回验分母
    assert extract_verbatim("这一章讨论了缺失机制的分类。") == []


# ---------------- 章级汇总 ----------------

def _cr(*sentences):
    return CloseReading(from_full_text=True, sections=[
        CloseReadSection(heading="要点", sentences=list(sentences))])


def test_verify_close_reading_counts_only_quoted_sentences():
    cr = _cr(
        CloseReadSentence(text='原文："{}"'.format(_Q), tag="可引用证据", page="3"),
        CloseReadSentence(text="这是一句中文归纳，无逐字引文。", tag="方法论借鉴", page="3"),
        CloseReadSentence(text='原文："This is a fabricated sentence not in the book"',
                          tag="可引用证据", page="3"))
    rep = verify_close_reading(cr, _index(), chapter=1)
    assert rep.total == 2                    # 中文归纳句不进分母
    assert rep.passed == 1
    assert rep.pass_rate == 0.5
    assert len(rep.flagged) == 1
    d = rep.to_dict()
    assert d["chapter"] == 1 and d["pass_rate"] == 0.5
    assert d["flagged"][0]["tag"] == "可引用证据"


def test_empty_close_reading_passes_vacuously():
    """无引句 = 无可证伪断言，pass_rate 定义为 1.0（不是 0，那会把空章判死）。"""
    rep = verify_close_reading(_cr(), _index())
    assert rep.total == 0 and rep.pass_rate == 1.0


# ---------------- 连字符抽文缺陷（实测于 Little & Rubin p.101） ----------------

def test_hyphen_dropped_by_extractor_still_matches():
    """PyMuPDF 会**整个丢掉**某些连字符（原文无换行，断行合并救不了）。

    实测：Little & Rubin p.101 的 "repeated-sampling operating characteristics"
    被抽成 "repeatedsampling operating characteristics"。不补这一层，一整类排版
    造成的假阴性会把真引句判成杜撰。
    """
    pages = [""] * 14 + ["through comparisons of their repeatedsampling operating "
                         "characteristics in realistic settings, not their theoretical etiologies"]
    idx = PageIndex(pages, offset=-12)
    q = ("through comparisons of their repeated-sampling operating characteristics "
         "in realistic settings")
    assert verify_quote(q, "3", idx).ok


def test_dehyphenation_does_not_rescue_altered_text():
    """去连字符只放过连字符差异，不放过改词——严格性不能被这层削掉。"""
    pages = [""] * 14 + ["their repeatedsampling operating characteristics in realistic settings"]
    idx = PageIndex(pages, offset=-12)
    bad = "their repeated-sampling operating characteristics in unrealistic settings"
    assert not verify_quote(bad, "3", idx).ok


# ---------------- 真实排版缺陷（实测于 JAMA Users' Guides） ----------------

def test_soft_hyphen_is_stripped():
    """PyMuPDF 原样抽出排版用的软连字符 U+00AD——人眼与渲染图上都看不见。
    实测 JAMA 正文里到处是 `\\xadliterature`，不删则逐字比对必然失败。"""
    assert normalize("the ­literature to guide") == "the literature to guide"
    assert normalize("a​b") == "ab"                      # 零宽空格
    assert normalize("of Interest") == "of Interest"     # 不间断空格


def test_cross_page_quote_matches_with_running_head_removed():
    """引句跨页是常态，而页眉会插进句子中间——两件事必须同时处理。

    实测 JAMA：p.305 末句 "…often have" 与 p.306 首句 "limited quality…" 之间
    隔着页眉 "306 Harm (Observational Studies)"。
    """
    pages = [
        "305 Harm (Observational Studies)\nLarge administrative databases, although "
        "providing a sample size that may allow ascertainment of rare events, often have",
        "306 Harm (Observational Studies)\nlimited quality of data concerning relevant "
        "patient characteristics, health care encounters, or diagnoses.",
    ]
    idx = PageIndex(pages, offset=304)      # pdf 1 → printed 305
    q = ("although providing a sample size that may allow ascertainment of rare events, "
         "often have limited quality of data concerning relevant patient characteristics")
    assert verify_quote(q, "305-306", idx).ok


def test_linebreak_hyphen_join_survives_line_structure():
    """保留行结构（供剔页眉）不能牺牲断行连字符合并——两者的处理顺序不可颠倒。"""
    idx = PageIndex(["301 Header\nthe investigators docu-\nment the characteristics "
                     "of the exposed and nonexposed participants"], offset=300)
    assert verify_quote("the investigators document the characteristics of the exposed",
                        "301", idx).ok


def test_running_head_removal_does_not_eat_body_lines():
    """页眉判据是「短行 + 含该页页码」；含数字的正文长句不得被误删。"""
    body = ("In one study, 24.1% of patients who were given a then-new NSAID, ketoprofen, "
            "had received peptic ulcer therapy during the previous 2 years compared with "
            "15.7% of the control population, which is a long body line mentioning 305.")
    idx = PageIndex(["305 Harm\n" + body], offset=304)
    assert "24.1%" in idx._body(305)
    assert "305 Harm" not in idx._body(305)
