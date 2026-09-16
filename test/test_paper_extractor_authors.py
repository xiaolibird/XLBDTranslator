# -*- coding: utf-8 -*-
"""paper_extractor 作者解析回归测试：剥除粘连的标题尾巴 + 过滤邮件样板噪声。

样例取自真实 digest 里被污染的作者串（标题末段被 get_text 粘进作者行）。
纯函数 _clean_author_list / _strip_title_bleed，离线可测。
"""
from src.scholar.paper_extractor import ScholarEmailParser as P


def test_strip_title_bleed_with_punctuation():
    title = "MAS-PromptBench: When Does Prompt Optimization Improve Multi-Agent LLM Systems?"
    raw = "Agent LLM Systems?J Bai, L Shi - arXiv preprint arXiv:2606.23664, 2026"
    assert P._clean_author_list(raw, title) == ["J Bai", "L Shi"]


def test_strip_title_bleed_no_delimiter():
    """标题与首作者无分隔（"RecordsD Pant"）也能剥离。"""
    title = ("Mining and Mapping 25 Years of Medication Use in Child and Adolescent "
             "Mental Health Services: Contact-Level Descriptive Analysis of Electronic Health Records")
    raw = "Level Descriptive Analysis of Electronic Health RecordsD Pant, C Clausen, BL Leventhal"
    assert P._clean_author_list(raw, title) == ["D Pant", "C Clausen", "BL Leventhal"]


def test_strip_title_bleed_long_prefix():
    title = ("GlaKG: A Biomarker-Centric Fundus Knowledge Graph for Explainable "
             "Glaucoma Diagnosis and Risk Assessment")
    raw = "Centric Fundus Knowledge Graph for Explainable Glaucoma Diagnosis and Risk AssessmentC Huang, J Zhang"
    assert P._clean_author_list(raw, title) == ["C Huang", "J Zhang"]


def test_clean_drops_venue_and_year():
    raw = "Aakash Reddy, David T Zhu, Kinza Khan - The Lancet Digital Health, 2026"
    assert P._clean_author_list(raw, "Deception in clinical large language models") == [
        "Aakash Reddy", "David T Zhu", "Kinza Khan"]


def test_clean_filters_email_boilerplate():
    """邮件样板文字（Google Scholar / following…）应被丢弃。"""
    raw = ("Google Scholar because you're following new articles related to research by Fei Wang, "
           "Aakash Reddy")
    got = P._clean_author_list(raw, "Some Title")
    assert "Aakash Reddy" in got
    assert all("google scholar" not in a.lower() and "following" not in a.lower() for a in got)


def test_clean_no_title_still_works():
    raw = "J Bai, L Shi - arXiv preprint arXiv:2606.23664, 2026"
    assert P._clean_author_list(raw, "") == ["J Bai", "L Shi"]


def test_strip_title_bleed_no_false_positive():
    """作者串未粘标题时不应被误改。"""
    title = "A Completely Different Title About Graphs"
    raw = "Jie Huang, Pengfei Yin"
    assert P._clean_author_list(raw, title) == ["Jie Huang", "Pengfei Yin"]


# ---------------------------------------------------------------------------
# 元数据行解析（2026-09-16）：anon2024Generalizable 追查出的解析缺陷回归
#
# fixture 为真实推送（2024-01-28 与 2026-09-16 两封）里原样截取的 5 个论文块，只替换了链接。
# 真实 HTML 没有换行：旧解析器 block.get_text() 无分隔符 → "Models" 与作者粘连、
# "2023" 与摘要 "The" 粘连（\b 失效）→ 空作者 / 丢首作者 / 无年份。
# ---------------------------------------------------------------------------
from datetime import date, datetime
from pathlib import Path

import pytest

from src.scholar.schema import EmailMetadata

FIXTURE = Path(__file__).parent / "data" / "scholar_alert_2024_2026_samples.html"


@pytest.mark.parametrize("line, authors, venue, year", [
    ("F Amrollahi - 2023", ["F Amrollahi"], None, 2023),
    ("AAS Soltan, A Thakur, J Yang, A Chauhan, LG D'Cruz… - The Lancet Digital Health, 2024",
     ["AAS Soltan", "A Thakur", "J Yang", "A Chauhan", "LG D'Cruz"], "The Lancet Digital Health", 2024),
    ("S Tang, FP Huang, C Idegawa - arXiv preprint arXiv:2609.11747, 2026",
     ["S Tang", "FP Huang", "C Idegawa"], "arXiv preprint arXiv:2609.11747", 2026),
    # Scholar 截断的刊名不写进书目（残缺刊名比留空更糟）
    ("SJ Conklin, F Albrecht - Journal of Neurologic Physical …", ["SJ Conklin", "F Albrecht"], None, None),
    ("YT Tseng - … Epidemiology, Biomarkers & …, 2026", ["YT Tseng"], None, 2026),
    ("J Doe - example.com", ["J Doe"], None, None),              # 出版方域名不是期刊
    ("付茗睿， 冯钰茹， 薛晓君 - 河南工业大学学报自然科学版, 2026",
     ["付茗睿", "冯钰茹", "薛晓君"], "河南工业大学学报自然科学版", 2026),
])
def test_parse_meta_line_known_shapes(line, authors, venue, year):
    assert P._parse_meta_line(line) == (authors, venue, year)


def test_parse_meta_line_rejects_non_meta():
    assert P._parse_meta_line("Background Multicentre training could reduce biases") == ([], None, None)
    # 无着色标记时的按行判定：摘要句子不能被当成元数据行
    assert not P._looks_like_meta_line("The broad adoption - of EHR, 2020")
    assert P._looks_like_meta_line("F Amrollahi - 2023")


def test_authors_only_line_requires_explicit_trust():
    line = "H Xie, Y Yang, H Cui, C Yang"
    assert P._parse_meta_line(line) == ([], None, None)
    assert P._parse_meta_line(line, authors_only_ok=True) == (["H Xie", "Y Yang", "H Cui", "C Yang"], None, None)


def test_clean_author_list_strips_ellipsis():
    assert P._clean_author_list("K Liu…, …") == ["K Liu"]


@pytest.fixture(scope="module")
def parsed():
    em = EmailMetadata(email_id="f" * 16, subject="s", sender="scholaralerts-noreply@google.com",
                       received_at=datetime(2024, 1, 28))
    segs = P().parse_email(FIXTURE.read_text(encoding="utf-8"), em)
    return {s.metadata.title[:20]: s for s in segs}


def test_fixture_block_count(parsed):
    assert len(parsed) == 5


def test_single_author_thesis_not_anon(parsed):
    m = parsed["Generalizable Risk P"].metadata
    assert m.authors == ["F Amrollahi"]
    # 年份来自元数据行（2023），不是邮件年份（2024），也不是摘要里的数字
    assert m.publication_date == date(2023, 1, 1)
    assert m.date_precision == "year"


def test_first_author_kept_and_ellipsis_dropped(parsed):
    m = parsed["A scalable federated"].metadata
    assert m.authors[0] == "AAS Soltan"
    assert not any("…" in a for a in m.authors)
    assert m.journal == "The Lancet Digital Health"
    assert m.publication_date == date(2024, 1, 1)


def test_arxiv_venue_yields_arxiv_id(parsed):
    m = parsed["Precise bubble wall "].metadata
    assert m.authors[0] == "S Tang"
    assert m.arxiv_id == "2609.11747"


def test_cjk_authors(parsed):
    m = parsed["抗性糊精制备工艺研究现状及在食品中的应用"[:20]].metadata
    assert m.authors[:2] == ["付茗睿", "冯钰茹"]
    assert m.publication_date == date(2026, 1, 1)


def test_authors_only_colored_line(parsed):
    seg = parsed["Federated Learning f"]
    assert seg.metadata.authors == ["H Xie", "Y Yang", "H Cui", "C Yang"]
    assert seg.metadata.publication_date is None


def test_abstract_excludes_meta_line(parsed):
    abstract = parsed["A scalable federated"].original_abstract
    assert abstract.startswith("Background Multicentre training")
    assert "Soltan" not in abstract
