# -*- coding: utf-8 -*-
"""回归 journal_screen.py:296 — run_filter 两条空结果早退分支必须带 journal/
total_pubmed 等降量字段，否则 export_candidates 落盘文件名/降量表全部退化。

两条早退：
  1. Stage 1 kept 为空（PubMed 检索/黑名单预筛后什么也没剩）；
  2. 关键词预筛把 kept 砍空（白名单一个词都不命中）。

此前两条分支的返回 dict 只有 candidates/total_input/keyword_prefilter_skipped，
缺 journal 字段——export_candidates 用 result.get("journal", "journal") 兜底成
"journal"，落盘成 journal_candidates.json 而非 {真实期刊}_candidates.json；
_write_candidates_md 用 result.get(..., 0) 兜底，降量表打出全 0，真实原因
（PubMed 捞到多少篇、被谁筛没）完全看不到。
"""
from pathlib import Path

import pytest

from src.scholar.journal_screen import JournalScreener
from src.scholar.schema import ScholarSettings

MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _settings(tmp_path: Path) -> ScholarSettings:
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    s = ScholarSettings.from_env_file(env_file)
    s.processing.output_dir = tmp_path / "out"
    return s


def _make_screener(tmp_path):
    return JournalScreener(tmp_path / "screen_out", _settings(tmp_path))


def _search_results(kept, total_pubmed=37, blacklist_excluded=12, journal="npj Digit Med"):
    return {
        "kept": kept,
        "journal": journal,
        "date_range": ("2020-01-01", "2026-01-01"),
        "total_pubmed": total_pubmed,
        "blacklist_excluded": blacklist_excluded,
    }


def _item(title):
    return {"title": title, "abstract": "abs", "authors": ["A B"], "doi": "10.1/x",
           "journal": "npj Digit Med", "published": None, "source": "pubmed"}


# ---------------- 分支 1：Stage 1 kept 为空 ----------------

def test_empty_kept_result_carries_journal_and_pubmed_totals(tmp_path):
    screener = _make_screener(tmp_path)
    result = screener.run_filter(_search_results([], total_pubmed=37, blacklist_excluded=37))

    assert result["candidates"] == []
    assert result["journal"] == "npj Digit Med"
    assert result["date_range"] == ("2020-01-01", "2026-01-01")
    assert result["total_pubmed"] == 37
    assert result["blacklist_excluded"] == 37
    assert result["after_blacklist"] == 0
    assert result["candidates_count"] == 0
    assert result["filter_stats"] == {}


def test_empty_kept_result_exports_to_journal_named_files(tmp_path):
    screener = _make_screener(tmp_path)
    result = screener.run_filter(_search_results([], total_pubmed=37, blacklist_excluded=37))
    json_path, md_path = screener.export_candidates(result)

    assert json_path.name == "npj_digit_med_candidates.json"
    assert md_path.name == "npj_digit_med_candidates.md"
    md_text = md_path.read_text(encoding="utf-8")
    assert "# npj Digit Med 文献筛候选列表" in md_text
    assert "PubMed 总数 | 37" in md_text
    assert "标题黑名单排除 | -37 → 0" in md_text


# ---------------- 分支 2：关键词预筛砍空 ----------------

def test_keyword_prefilter_empties_kept_still_carries_journal(tmp_path):
    screener = _make_screener(tmp_path)
    items = [_item("Blockchain for supply chain traceability")]
    result = screener.run_filter(
        _search_results(items, total_pubmed=40, blacklist_excluded=3))

    assert result["candidates"] == []
    assert result["journal"] == "npj Digit Med"
    assert result["total_pubmed"] == 40
    assert result["blacklist_excluded"] == 3
    assert result["after_blacklist"] == 37
    assert result["keyword_prefilter_skipped"] == 1
    assert result["candidates_count"] == 0


def test_keyword_prefilter_empties_kept_exports_real_journal_slug(tmp_path):
    """实测里踩的坑：白名单预筛砍光后落盘文件名不能是 journal_candidates.json。"""
    screener = _make_screener(tmp_path)
    items = [_item("Blockchain for supply chain traceability")]
    result = screener.run_filter(
        _search_results(items, total_pubmed=40, blacklist_excluded=3))
    json_path, md_path = screener.export_candidates(result)

    assert json_path.name == "npj_digit_med_candidates.json"
    assert json_path.name != "journal_candidates.json"
    md_text = md_path.read_text(encoding="utf-8")
    assert "# npj Digit Med 文献筛候选列表" in md_text
    assert "关键词预筛排除 | -1 → 36" in md_text
