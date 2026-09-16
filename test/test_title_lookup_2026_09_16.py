# -*- coding: utf-8 -*-
"""title_lookup：入库补全按标题查 OpenAlex 的判据与降级（2026-09-16）。

背景：Scholar 推送里的无 DOI/arXiv 条目（学位论文、会议稿）Crossref 常不命中，
translation-server 又没有标识可解，作者/年份只剩 Scholar 截断的脏数据。
"""
import threading
from datetime import date

import pytest

from src.scholar import title_lookup as TL
from src.scholar.schema import PaperMetadata


def _meta(**kw):
    base = dict(paper_id="p1", title="Generalizable Risk Predictive Deep Learning Models",
                authors=["F Amrollahi"])
    base.update(kw)
    return PaperMetadata(**base)


HIT = {"source": "openalex", "title": "Generalizable Risk Predictive Deep Learning Models",
       "doi": None, "year": 2023, "authors": ["Fatemeh Amrollahi"], "sim": 1.0}


def test_truncated_title_similarity_uses_prefix():
    full = ("A scalable federated learning solution for secondary care using low-cost microcomputing: "
            "privacy-preserving development and evaluation of a COVID-19 screening test in UK hospitals")
    truncated = ("A scalable federated learning solution for secondary care using low-cost microcomputing: "
                 "privacy-preserving development and evaluation of a COVID-19 screening …")
    assert TL.title_similarity(truncated, full) < TL.SIM_FILLABLE          # 普通 Jaccard 会被尾部拉低
    assert TL.truncation_aware_similarity(truncated, full) >= TL.SIM_FILLABLE


@pytest.mark.parametrize("authors, year, hit_patch, ok", [
    (["F Amrollahi"], 2023, {}, True),
    (["F Amrollahi"], None, {}, True),                                    # 原本没年份
    (["F Amrollahi"], 2019, {}, False),                                   # 年份冲突
    (["J Smith"], 2023, {}, False),                                       # 首作者对不上且相似度非逐字
    ([], 2023, {}, True),                                                 # 无作者 + 相似度 1.0
    (["F Amrollahi"], 2023, {"title": "Generalizable Risk Models"}, False),   # 相似度不够
])
def test_confident_title_hit(authors, year, hit_patch, ok):
    hit = dict(HIT, **hit_patch)
    if authors == ["J Smith"]:
        hit["title"] = "Generalizable Risk Predictive Deep Learning Model"   # 0.83 < 0.99，不能靠逐字放行
    assert TL.confident_title_hit(HIT["title"], authors, year, hit) is ok


def test_confident_title_hit_rejects_short_titles():
    assert not TL.confident_title_hit("Deep Learning", [], None, dict(HIT, title="Deep Learning"))


def test_enrich_fills_authors_and_missing_year(monkeypatch):
    monkeypatch.setattr(TL, "query_openalex", lambda *a, **k: (dict(HIT), True))
    m = _meta()
    assert TL.enrich_from_openalex_title(m, "", None) is True
    assert m.authors == ["Fatemeh Amrollahi"]
    assert m.publication_date == date(2023, 1, 1) and m.date_precision == "year"


def test_enrich_keeps_existing_year_and_doi(monkeypatch):
    monkeypatch.setattr(TL, "query_openalex", lambda *a, **k: (dict(HIT, doi="10.1/x", year=2024), True))
    m = _meta(publication_date=date(2023, 1, 1))
    assert TL.enrich_from_openalex_title(m, "", None) is True
    assert m.publication_date == date(2023, 1, 1)
    assert m.doi == "10.1/x"                                              # 原本为空才填


def test_enrich_skips_identified_entries(monkeypatch):
    called = []
    monkeypatch.setattr(TL, "query_openalex", lambda *a, **k: called.append(1) or (dict(HIT), True))
    assert TL.enrich_from_openalex_title(_meta(doi="10.1/y"), "", None) is False
    assert TL.enrich_from_openalex_title(_meta(arxiv_id="2401.00001"), "", None) is False
    assert not called


def test_quota_exhausted_trips_breaker(monkeypatch):
    calls = []
    monkeypatch.setattr(TL, "query_openalex", lambda *a, **k: calls.append(1) or (None, False))
    ev = threading.Event()
    m1, m2 = _meta(), _meta(paper_id="p2")
    assert TL.enrich_from_openalex_title(m1, "", None, exhausted=ev) is None
    assert ev.is_set()
    assert TL.enrich_from_openalex_title(m2, "", None, exhausted=ev) is None
    assert len(calls) == 1                                                # 熔断后不再发请求
    assert m1.authors == ["F Amrollahi"]                                  # 没问成不改原值


def test_no_confident_hit_leaves_meta(monkeypatch):
    monkeypatch.setattr(TL, "query_openalex", lambda *a, **k: (dict(HIT, year=2010), True))
    m = _meta(publication_date=date(2023, 1, 1))                          # 年份冲突 → 不可信
    assert TL.enrich_from_openalex_title(m, "", None) is False
    assert m.authors == ["F Amrollahi"]


def test_enrich_segments_openalex_flag(monkeypatch):
    """enrich_segments：开关关时不调用；开时 Crossref 未命中才调用；额度耗尽不抛错。"""
    from src.scholar import ingest, crossref, translation_server
    from src.scholar.schema import PaperSegment
    monkeypatch.setattr(crossref, "enrich_metadata", lambda *a, **k: False)
    calls = []

    def fake(meta, email, client, exhausted=None):
        calls.append(meta.paper_id)
        exhausted.set()
        return None
    monkeypatch.setattr(TL, "enrich_from_openalex_title", fake)
    segs = [PaperSegment(segment_id=i, paper_id="p%d" % i, original_abstract="x" * 40,
                         metadata=_meta(paper_id="p%d" % i)) for i in range(3)]
    ingest.enrich_segments(segs, "", "", max_workers=1, openalex_title=False)
    assert calls == []
    ingest.enrich_segments(segs, "", "", max_workers=1, openalex_title=True)
    assert calls                                                          # 调用了且没有抛错
