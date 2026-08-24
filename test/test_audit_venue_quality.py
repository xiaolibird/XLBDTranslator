# -*- coding: utf-8 -*-
"""venue 白/黑名单匹配的核心不变量。

这张表会决定文献被不被踢出向量库，判错的代价是把正经文献从检索里抹掉。
2026-08-24 就踩过一次：Crossref 的 ISSN 是**列表**（期刊常有 print + online 两个，
JAMIA 是 ['1067-5027','1527-974X']），代码只存了第一个，白名单里按 online ISSN
写的条目全部漏配——实测 JAMIA 37 篇里 24 篇因此掉进 gray。所以这里重点测的是
「多 ISSN 任一命中」与「三态的优先级」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.audit_venue_quality as A  # noqa: E402

LISTS = {
    "black": {"doi_prefix": {"10.1155": "Hindawi"}, "issn": {"1111-1111": "坏刊"},
              "name_exact": {"Bogus Journal": "假刊"}},
    "white": {"issn": {"1527-974X": "JAMIA", "2398-6352": "npj DM"},
              "name_regex": ["SIGKDD", "neural information processing systems|NeurIPS"]},
}


def test_issn_list_any_match_wins():
    """多 ISSN 里任意一个命中即算命中——这正是 JAMIA 漏配那个 bug 的回归。"""
    cr = {"issns": ["1067-5027", "1527-974X"], "container": "JAMIA"}
    assert A.match_venue({"citekey": "x"}, cr, LISTS)[0] == "white"


def test_single_issn_field_still_works():
    """旧缓存只有 issn 单值字段时不能整批失配（缓存是分批攒的，两种形态会共存）。"""
    cr = {"issn": "2398-6352", "container": "npj Digital Medicine"}
    assert A.match_venue({"citekey": "x"}, cr, LISTS)[0] == "white"


def test_black_beats_white():
    """同时命中黑白名单时黑胜出：宁可错杀标记，不可放过公认掠夺性。"""
    cr = {"issns": ["1527-974X"], "container": "JAMIA"}
    tag, _ = A.match_venue({"doi": "10.1155/2021/8811147"}, cr, LISTS)
    assert tag == "black"


def test_conference_matched_by_name_regex():
    """会议大多没有 ISSN，只能靠刊名正则——顶会掉进 gray 就等于白名单没用。"""
    cr = {"container": "Proceedings of the 28th ACM SIGKDD Conference on KDD"}
    assert A.match_venue({}, cr, LISTS)[0] == "white"
    cr2 = {"container": "Advances in Neural Information Processing Systems 36"}
    assert A.match_venue({}, cr2, LISTS)[0] == "white"


def test_unlisted_is_gray_not_black():
    """**gray 是常态不是问题**：库内 727 个 venue 里 484 个只出现 1 次，白名单不可能
    列全。没列到的必须落 gray，绝不能因为「没在白名单里」就当成坏的。"""
    cr = {"issns": ["9999-9999"], "container": "Some Unlisted Journal"}
    tag, why = A.match_venue({"citekey": "x"}, cr, LISTS)
    assert tag == "gray" and why == ""


def test_grade_whitelist_short_circuits_citation_rule():
    """白名单命中就收工，不让被引判据再踩一脚。

    被引数测的是「这篇论文影响力」而非「venue 是不是野鸡」——实测把 3 篇真
    AAAI/NeurIPS 冷门论文判成了可疑（eiben2021Parameterized 理论 CS，5 年 2 引）。
    """
    cr = {"issns": ["2398-6352"], "container": "npj DM", "cited": 1, "year": 2020}
    g, _ = A.grade({"citekey": "x"}, cr, "journal", LISTS)
    assert g == "whitelisted"          # 而不是 suspect（1 引 / 6 年）


def test_grade_too_new_not_judged():
    """≥2024 的新论文天然没引用，一律不判——库里这批占 56%，误判代价极大。"""
    cr = {"issns": ["9999-9999"], "cited": 0, "year": A.THIS_YEAR - 1}
    g, why = A.grade({"citekey": "x"}, cr, "journal", LISTS)
    assert g == "too-new" and "不适用" in why


def test_preprint_is_its_own_bucket():
    """预印本未经同行评审是**性质**不是**质量**，不该和野鸡刊混为一谈。"""
    g, _ = A.grade({"arxiv_id": "2410.17506"}, {}, "preprint", LISTS)
    assert g == "preprint"
