# -*- coding: utf-8 -*-
"""journal_screen in_library 阈值（IN_LIBRARY_SIM_THRESHOLD）回归测试。

背景（2026-08-21 第 3 轮审计）：摘要喂厚为双 chunk 后，近邻 sim 多来自 ab: 厚向量，
瘦库口径标定的 0.85（"同篇自身 >0.9"）已失效——1804 篇全量自回找实测尾部有 6 篇
<0.85，重标定为 0.82（定案依据 docs/decisions/digest_neighbors_calibration_2026-08.md
方法三）。本测试钉住阈值数值与 run_filter 的边界判定语义（>= 含边界、任一近邻达标
即标记），防止后续改动无标定档案背书就漂移。

复用 test_journal_screen_undecided 的 mock 手法：patch src.scholar.ingest.
classify_segments，不触发真实 LLM/向量库/网络。
"""
from pathlib import Path

from src.scholar.journal_screen import IN_LIBRARY_SIM_THRESHOLD, JournalScreener
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


def _decision(paper_id, title, neighbors):
    return FilterDecision(
        paper_id=paper_id, title=title, verdict="included",
        decision="INCLUDE", stage="llm_judge", library_neighbors=neighbors)


def _neighbor(sim):
    return {"citekey": "someone2024Paper", "year": 2024,
            "one_line": "判词", "sim": sim}


def _run(tmp_path, monkeypatch, decisions_by_title, items):
    import src.scholar.ingest as ing

    def fake_classify(segments, settings, force_include=False):
        for seg in segments:
            seg.filter_decision = decisions_by_title[seg.metadata.title]

    monkeypatch.setattr(ing, "classify_segments", fake_classify)
    screener = JournalScreener(tmp_path / "screen_out", _settings(tmp_path))
    return screener.run_filter(
        {"kept": items, "journal": "NPJ Digit Med",
         "date_range": ("2020-01-01", "2026-01-01"),
         "total_pubmed": len(items), "blacklist_excluded": 0},
        prefilter=False, llm_filter=True)


def test_threshold_value_pinned_to_calibration():
    """阈值数值钉在 0.82：改这个数必须同步更新标定档案（方法三）与本测试。"""
    assert IN_LIBRARY_SIM_THRESHOLD == 0.82


def test_in_library_boundary(tmp_path, monkeypatch):
    """>= 0.82 标记疑似在库（含边界），< 0.82 不标；判定取任一近邻的 sim。"""
    items = [_item("At threshold", "10.1/a"),
             _item("Below threshold", "10.1/b"),
             _item("Mixed neighbors", "10.1/c")]
    decisions = {
        # 恰好踩线：厚库实测同篇自身尾部就落在这一带，边界必须含等号
        "At threshold": _decision("p1", "At threshold",
                                  [_neighbor(IN_LIBRARY_SIM_THRESHOLD)]),
        # 线下 0.001：真近邻缓冲区（0.74-0.82），不得误标
        "Below threshold": _decision("p2", "Below threshold",
                                     [_neighbor(IN_LIBRARY_SIM_THRESHOLD - 0.001)]),
        # 多近邻：只要有一个达标即标记（workflow 按 citekey 取最高分后可有多条）
        "Mixed neighbors": _decision("p3", "Mixed neighbors",
                                     [_neighbor(0.63), _neighbor(0.95)]),
    }
    result = _run(tmp_path, monkeypatch, decisions, items)

    by_title = {c["title"]: c for c in result["candidates"]}
    assert by_title["At threshold"]["in_library"] is True
    assert by_title["Below threshold"]["in_library"] is False
    assert by_title["Mixed neighbors"]["in_library"] is True


def test_no_neighbors_not_in_library(tmp_path, monkeypatch):
    """无近邻（向量库降级返回空）时 in_library 必须为 False，不得异常。"""
    items = [_item("No neighbors", "10.1/d")]
    decisions = {"No neighbors": _decision("p4", "No neighbors", [])}
    result = _run(tmp_path, monkeypatch, decisions, items)
    assert result["candidates"][0]["in_library"] is False
