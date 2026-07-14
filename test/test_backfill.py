# -*- coding: utf-8 -*-
"""按月回填回归测试：显式日期区间(PubMed mindate/maxdate、arXiv submittedDate) + 月份解析。"""
import argparse
from datetime import date

import httpx

from src.scholar.academic_search import AcademicSearchClient


def _client_with(handler):
    c = AcademicSearchClient(email="x@y.com")
    c._client = httpx.Client(transport=httpx.MockTransport(handler), headers=c._client.headers)
    return c


_ARXIV_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry><id>http://arxiv.org/abs/2203.00001v1</id><title>MNAR paper</title>
    <summary>x</summary><published>2022-03-15T00:00:00Z</published>
    <author><name>A B</name></author></entry>
</feed>"""


def test_pubmed_date_range_uses_mindate_maxdate():
    captured = {}

    def handler(request):
        if "esearch" in request.url.path:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, text="<PubmedArticleSet></PubmedArticleSet>")

    c = _client_with(handler)
    c.search_pubmed("MNAR", max_results=10, date_range=(date(2022, 3, 1), date(2022, 3, 31)))
    assert captured.get("mindate") == "2022/03/01"
    assert captured.get("maxdate") == "2022/03/31"
    assert captured.get("datetype") == "pdat"
    assert "reldate" not in captured  # 区间模式不用相对天数
    c.close()


def test_arxiv_date_range_injects_submitteddate():
    captured = {}

    def handler(request):
        captured["search_query"] = dict(request.url.params).get("search_query", "")
        return httpx.Response(200, text=_ARXIV_XML)

    c = _client_with(handler)
    items = c.search_arxiv("all:MNAR", max_results=10,
                           date_range=(date(2022, 3, 1), date(2022, 3, 31)))
    assert "submittedDate:[20220301" in captured["search_query"]
    assert "20220331" in captured["search_query"]
    assert len(items) == 1 and items[0]["title"] == "MNAR paper"
    c.close()


def test_month_range_parsing():
    from scholar_main import _parse_month_range
    assert _parse_month_range(argparse.Namespace(month="2022-03", since=None, until=None)) == \
        (date(2022, 3, 1), date(2022, 3, 31))
    # 12 月跨年边界
    assert _parse_month_range(argparse.Namespace(month="2022-12", since=None, until=None)) == \
        (date(2022, 12, 1), date(2022, 12, 31))
    # since/until
    assert _parse_month_range(argparse.Namespace(month=None, since="2026-05-01", until="2026-06-15")) == \
        (date(2026, 5, 1), date(2026, 6, 15))
    # 无参数
    assert _parse_month_range(argparse.Namespace(month=None, since=None, until=None)) is None
