# -*- coding: utf-8 -*-
"""translation-server 客户端回归测试：标识符选取 / 解析 / 回填 meta。全程 mock，不需真容器。"""
from datetime import date

import httpx

from src.scholar.schema import PaperMetadata
from src.scholar import translation_server as ts


def _meta(**kw):
    base = dict(paper_id="p", title="A Study", authors=["dirtyTitleA Reddy"])
    base.update(kw)
    return PaperMetadata(**base)


def test_best_identifier_priority():
    assert ts.best_identifier(_meta(doi="10.1/abc")) == "10.1/abc"
    assert ts.best_identifier(_meta(doi=None, arxiv_id="2401.123")) == "arXiv:2401.123"
    assert ts.best_identifier(_meta(doi=None, arxiv_id="arXiv:2401.123")) == "arXiv:2401.123"
    assert ts.best_identifier(_meta(doi=None, pmid="12345")) == "12345"
    assert ts.best_identifier(_meta(doi=None)) is None


_ITEM = {
    "itemType": "journalArticle",
    "title": "Deception in clinical large language models",
    "creators": [
        {"firstName": "Aakash", "lastName": "Reddy", "creatorType": "author"},
        {"firstName": "David T", "lastName": "Zhu", "creatorType": "author"},
        {"lastName": "Editor X", "creatorType": "editor"},
    ],
    "publicationTitle": "The Lancet Digital Health",
    "DOI": "10.1016/j.landig.2026.101043",
    "date": "2026-02",
    "volume": "8", "issue": "2", "pages": "e101043",
}


def test_resolve_identifier_via_mock():
    def handler(request):
        assert request.url.path == "/search"
        assert request.content == b"10.1016/j.landig.2026.101043"
        return httpx.Response(200, json=[_ITEM])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = ts.resolve_identifier("10.1016/j.landig.2026.101043", client=client)
    assert items and items[0]["DOI"] == "10.1016/j.landig.2026.101043"


def test_resolve_identifier_non200_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(300, text="multiple")))
    assert ts.resolve_identifier("10.1/x", client=client) is None


def test_apply_item_to_meta_overrides_dirty():
    m = _meta(doi=None, authors=["safety riskA Reddy"], journal=None)
    ok = ts.apply_item_to_meta(m, _ITEM)
    assert ok is True
    # editor 被过滤，只留 author
    assert m.authors == ["Aakash Reddy", "David T Zhu"]
    assert m.journal == "The Lancet Digital Health"
    assert m.doi == "10.1016/j.landig.2026.101043"
    assert m.volume == "8" and m.issue == "2" and m.pages == "e101043"
    assert m.publication_date == date(2026, 2, 1)


def test_resolve_and_apply_end_to_end():
    def handler(request):
        return httpx.Response(200, json=[_ITEM])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    m = _meta(doi="10.1016/j.landig.2026.101043", authors=["riskA Reddy"])
    item = ts.resolve_and_apply(m, client=client)
    assert item is not None
    assert m.authors == ["Aakash Reddy", "David T Zhu"]   # 权威回填


def test_resolve_and_apply_no_identifier():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[_ITEM])))
    m = _meta(doi=None)  # 无任何标识符
    assert ts.resolve_and_apply(m, client=client) is None


def test_parse_date_variants():
    assert ts._parse_date("2026-02-01") == date(2026, 2, 1)
    assert ts._parse_date("2026-02") == date(2026, 2, 1)
    assert ts._parse_date("2026") == date(2026, 1, 1)
    assert ts._parse_date("7/2026") == date(2026, 7, 1)   # TS 常见 月/年
    assert ts._parse_date("") is None
    assert ts._parse_date("no year here") is None
