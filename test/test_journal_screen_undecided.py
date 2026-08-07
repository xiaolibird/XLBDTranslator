# -*- coding: utf-8 -*-
"""journal_screen.run_filter 的 UNDECIDED 裁决回归测试。

背景：workflow._fallback_partition（LLM 批次异常/单篇 id 缺失时触发）对未命中
白名单者写 verdict="undecided", decision=None——语义是「没判过」，不是「判了
无关」。run_filter 此前的裁决收集只认 decision 字段：decision is None 落进
else 分支被计入 EXCLUDE、静默从候选列表消失，用户会误以为 LLM 判定无关，
实际根本没跑到那篇论文的裁决。

全部 mock classify_segments（不触发真实 LLM/网络调用），直接控制每篇论文的
filter_decision 来覆盖 INCLUDE/MAYBE/UNDECIDED/EXCLUDE/无裁决五种分支。
"""
from pathlib import Path

import pytest

from src.scholar.journal_screen import JournalScreener
from src.scholar.schema import FilterDecision, ScholarSettings

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


def _item(title, doi):
    return {"title": title, "abstract": "abs", "authors": ["A B"], "doi": doi,
           "journal": "NPJ Digit Med", "published": None, "source": "pubmed"}


def _fake_decision(paper_id, title, **kw):
    base = dict(paper_id=paper_id, title=title, verdict="included",
                decision="INCLUDE", stage="llm_judge")
    base.update(kw)
    return FilterDecision(**base)


def _undecided_decision(paper_id, title):
    """对齐 workflow._fallback_partition 真实构造的未决裁决：verdict=undecided，
    decision 字段不传（默认 None），reason 说明失败原因。"""
    return FilterDecision(
        paper_id=paper_id, title=title, verdict="undecided",
        stage="keyword_fallback",
        reason="LLM 裁决失败且未命中白名单，待人工复核",
    )


def _make_screener(tmp_path):
    return JournalScreener(tmp_path / "screen_out", _settings(tmp_path))


def _search_results(items):
    return {
        "kept": items,
        "journal": "NPJ Digit Med",
        "date_range": ("2020-01-01", "2026-01-01"),
        "total_pubmed": len(items),
        "blacklist_excluded": 0,
    }


def _mock_classify(monkeypatch, decisions_by_title):
    """monkeypatch src.scholar.ingest.classify_segments（run_filter 内是运行时
    局部 import，patch 源头模块属性即生效）：按标题给每个 segment 分配裁决。"""
    import src.scholar.ingest as ing

    def fake_classify(segments, settings, force_include=False):
        for seg in segments:
            seg.filter_decision = decisions_by_title[seg.metadata.title]

    monkeypatch.setattr(ing, "classify_segments", fake_classify)


def test_undecided_is_not_counted_as_exclude(tmp_path, monkeypatch):
    """核心回归：UNDECIDED 论文必须单列统计，不得并入 EXCLUDE。"""
    screener = _make_screener(tmp_path)
    items = [_item("Included paper", "10.1/a"), _item("Undecided paper", "10.1/b")]
    decisions = {
        "Included paper": _fake_decision("p1", "Included paper"),
        "Undecided paper": _undecided_decision("p2", "Undecided paper"),
    }
    _mock_classify(monkeypatch, decisions)

    result = screener.run_filter(_search_results(items), prefilter=False, llm_filter=True)

    assert result["filter_stats"]["UNDECIDED"] == 1
    assert result["filter_stats"]["EXCLUDE"] == 0
    assert result["filter_stats"]["INCLUDE"] == 1


def test_undecided_paper_appears_in_candidates(tmp_path, monkeypatch):
    """UNDECIDED 论文必须进候选列表（标注待人工复核），而不是从列表里消失。"""
    screener = _make_screener(tmp_path)
    items = [_item("Undecided paper", "10.1/b")]
    decisions = {"Undecided paper": _undecided_decision("p2", "Undecided paper")}
    _mock_classify(monkeypatch, decisions)

    result = screener.run_filter(_search_results(items), prefilter=False, llm_filter=True)

    assert result["candidates_count"] == 1
    cand = result["candidates"][0]
    assert cand["decision"] == "UNDECIDED"
    assert "待人工复核" in cand["one_line"] or "复核" in cand["one_line"]


def test_true_exclude_still_excluded(tmp_path, monkeypatch):
    """对照组：真正被 LLM 判定 EXCLUDE 的论文（decision="EXCLUDE"）仍不进候选——
    修复不能把所有排除路径都放宽。"""
    screener = _make_screener(tmp_path)
    items = [_item("Excluded paper", "10.1/c")]
    decisions = {
        "Excluded paper": _fake_decision("p3", "Excluded paper", verdict="excluded",
                                         decision="EXCLUDE"),
    }
    _mock_classify(monkeypatch, decisions)

    result = screener.run_filter(_search_results(items), prefilter=False, llm_filter=True)

    assert result["candidates_count"] == 0
    assert result["filter_stats"]["EXCLUDE"] == 1
    assert result["filter_stats"]["UNDECIDED"] == 0


def test_maybe_still_counted_as_maybe_not_undecided(tmp_path, monkeypatch):
    """对照组：正常 MAYBE 裁决（decision="MAYBE"，非 undecided）不得被误判为 UNDECIDED。"""
    screener = _make_screener(tmp_path)
    items = [_item("Maybe paper", "10.1/d")]
    decisions = {
        "Maybe paper": _fake_decision("p4", "Maybe paper", verdict="included",
                                      decision="MAYBE"),
    }
    _mock_classify(monkeypatch, decisions)

    result = screener.run_filter(_search_results(items), prefilter=False, llm_filter=True)

    assert result["filter_stats"]["MAYBE"] == 1
    assert result["filter_stats"]["UNDECIDED"] == 0
    assert result["candidates"][0]["decision"] == "MAYBE"


def test_missing_filter_decision_still_counted_as_exclude(tmp_path, monkeypatch):
    """对照组：seg.filter_decision 整个是 None（不是 undecided verdict）时维持
    既有行为——计入 EXCLUDE、不进候选（这是真正"没裁决"，与"裁决未决"不同）。"""
    screener = _make_screener(tmp_path)
    items = [_item("No decision paper", "10.1/e")]

    import src.scholar.ingest as ing
    monkeypatch.setattr(ing, "classify_segments", lambda segments, settings, force_include=False: None)

    result = screener.run_filter(_search_results(items), prefilter=False, llm_filter=True)

    assert result["candidates_count"] == 0
    assert result["filter_stats"]["EXCLUDE"] == 1
    assert result["filter_stats"]["UNDECIDED"] == 0


def test_undecided_sorts_after_include_and_maybe(tmp_path, monkeypatch):
    """候选排序：INCLUDE 最前，MAYBE 次之，UNDECIDED 最后（待复核的不该抢占视觉焦点）。"""
    screener = _make_screener(tmp_path)
    items = [_item("Undecided", "10.1/u"), _item("Maybe", "10.1/m"), _item("Include", "10.1/i")]
    decisions = {
        "Undecided": _undecided_decision("pu", "Undecided"),
        "Maybe": _fake_decision("pm", "Maybe", verdict="included", decision="MAYBE"),
        "Include": _fake_decision("pi", "Include", verdict="included", decision="INCLUDE"),
    }
    _mock_classify(monkeypatch, decisions)

    result = screener.run_filter(_search_results(items), prefilter=False, llm_filter=True)

    order = [c["decision"] for c in result["candidates"]]
    assert order == ["INCLUDE", "MAYBE", "UNDECIDED"]
