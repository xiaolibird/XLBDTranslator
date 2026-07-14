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
