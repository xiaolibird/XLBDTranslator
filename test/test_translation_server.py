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


# ---- 探活：空 body、零出网；只有连接层失败才算离线 ----

def _mock_ipv4(monkeypatch, handler):
    monkeypatch.setattr(ts, "ipv4_client",
                        lambda timeout=5.0: httpx.Client(transport=httpx.MockTransport(handler)))


def test_is_available_probes_with_empty_body_and_accepts_400(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.content, request.headers.get("content-type")))
        assert request.url.path == "/search"
        return httpx.Response(400, text="POST data not provided")
    _mock_ipv4(monkeypatch, handler)
    assert ts.is_available("http://ts.local:1969") is True
    # 探活不得带真标识符出网——空 body 让 server 在路由层就回 400，不碰 doi.org；
    # 缺 Content-Type 真 server 回 415 会被判离线，所以头也要钉住
    assert seen == [(b"", "text/plain")]


def test_is_available_false_on_connect_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    _mock_ipv4(monkeypatch, handler)
    assert ts.is_available("http://ts.local:1969") is False


def test_is_available_false_on_connect_timeout(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)
    _mock_ipv4(monkeypatch, handler)
    assert ts.is_available("http://ts.local:1969") is False


def test_is_available_true_on_read_timeout(monkeypatch):
    # 连上了但读超时 = 容器在收请求只是忙（如 realign 批量重对齐撞上周一 ingest）——
    # 不能判离线，否则整批跳过权威解析还误报
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    _mock_ipv4(monkeypatch, handler)
    assert ts.is_available("http://ts.local:1969") is True


def test_is_available_false_on_unexpected_status(monkeypatch):
    # 404/415/502 之类 = 端口上跑的不是 translation-server（反代 / 别的服务占了端口）
    for code in (404, 415, 502):
        _mock_ipv4(monkeypatch, lambda r, c=code: httpx.Response(c, text="nope"))
        assert ts.is_available("http://ts.local:1969") is False


# ---- resolve_batch：与 zotero_sync 共用的入口，键任意、就地回填 ----

def test_resolve_batch_returns_items_keyed_and_applies(monkeypatch):
    monkeypatch.setattr(ts, "_ALERTED", set())
    monkeypatch.setattr(ts, "is_available", lambda url, timeout=5.0: True)
    _mock_ipv4(monkeypatch, lambda r: httpx.Response(200, json=[_ITEM]))
    m1 = _meta(doi="10.1016/j.landig.2026.101043", authors=["riskA Reddy"])
    m2 = _meta(doi=None)                                  # 无标识符
    out = ts.resolve_batch({"a": m1, 7: m2}, "http://ts.local:1969", workers=4)
    assert set(out) == {"a", 7}
    assert out["a"]["DOI"] == "10.1016/j.landig.2026.101043" and out[7] is None
    assert m1.authors == ["Aakash Reddy", "David T Zhu"]   # 就地回填


def test_resolve_batch_empty_url_or_batch_is_noop(monkeypatch):
    monkeypatch.setattr(ts, "is_available", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该探活")))
    assert ts.resolve_batch({}, "http://ts.local:1969") == {}
    assert ts.resolve_batch({"a": _meta(doi="10.1/x")}, "") == {"a": None}
