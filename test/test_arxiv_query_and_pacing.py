"""arXiv 外部访问的回归（2026-09-21）：

1. 默认检索式不得再叠 AND —— 原式「缺失机制词 AND 临床场景词 AND 方法词」在 arXiv 全部历史里
   只命中 1 篇，周度 digest 的 arXiv 一路从未有过产出；放宽为只用缺失机制词，噪声交给 LLM 裁决。
2. 翻页间隔按 arXiv 条款 ≥3 秒（此前 0.5 秒）。
3. 进程级节流：ipv4_client 发往 *.arxiv.org 的所有请求两两间隔 ≥3 秒——入库补全逐篇连发
   12 次后，同一 IP 被 CDN 罚 406，全文精读的 PDF 下载全灭。
"""
import httpx
import pytest

from src.scholar import academic_search, fulltext
from src.scholar.academic_search import AcademicSearchClient
from src.scholar.settings import ProcessingSettings


@pytest.fixture
def fake_clock(monkeypatch):
    """假时钟：sleep 推进时钟并记录；每个用例从干净的「上次请求时刻」开始。"""
    now = [1000.0]
    slept = []

    def sleep(s):
        slept.append(round(s, 6))
        now[0] += s

    monkeypatch.setattr(fulltext.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(fulltext.time, "sleep", sleep)
    monkeypatch.setattr(fulltext, "_arxiv_last", [0.0])
    return now, slept


def _req(url):
    return httpx.Request("GET", url)


def test_arxiv_host_matching():
    assert fulltext._is_arxiv_host("arxiv.org")
    assert fulltext._is_arxiv_host("export.arxiv.org")
    assert fulltext._is_arxiv_host("ARXIV.ORG")
    assert not fulltext._is_arxiv_host("notarxiv.org")
    assert not fulltext._is_arxiv_host("arxiv.org.evil.example")
    assert not fulltext._is_arxiv_host("api.crossref.org")


def test_throttle_spaces_back_to_back_arxiv_requests(fake_clock):
    now, slept = fake_clock
    fulltext._arxiv_throttle(_req("https://export.arxiv.org/api/query?id_list=1"))
    fulltext._arxiv_throttle(_req("https://arxiv.org/pdf/2609.00001v1"))   # 不同子域共用一个节拍
    assert slept == [fulltext._ARXIV_MIN_INTERVAL]
    now[0] += 10.0                                                          # 间隔已够：不再等
    fulltext._arxiv_throttle(_req("https://export.arxiv.org/api/query?id_list=2"))
    assert slept == [fulltext._ARXIV_MIN_INTERVAL]
    assert fulltext._ARXIV_MIN_INTERVAL >= 3.0


def test_throttle_ignores_other_hosts(fake_clock):
    _, slept = fake_clock
    for u in ("https://api.crossref.org/works/x", "https://eutils.ncbi.nlm.nih.gov/x",
              "https://api.crossref.org/works/y"):
        fulltext._arxiv_throttle(_req(u))
    assert slept == []
    assert fulltext._arxiv_last == [0.0]     # 非 arXiv 请求不占 arXiv 的节拍


def test_ipv4_client_registers_arxiv_throttle():
    with fulltext.ipv4_client(timeout=5) as c:
        assert fulltext._arxiv_throttle in c.event_hooks["request"]


def test_throttle_applies_through_real_client(fake_clock, monkeypatch):
    """钩子必须真的在请求路径上：换成 MockTransport 发两次 arXiv 请求，第二次要等。"""
    _, slept = fake_clock
    real_transport = httpx.HTTPTransport
    monkeypatch.setattr(fulltext.httpx, "HTTPTransport",
                        lambda **kw: httpx.MockTransport(lambda r: httpx.Response(200, text="ok")))
    with fulltext.ipv4_client(timeout=5) as c:
        c.get("https://export.arxiv.org/api/query?id_list=1")
        c.get("https://export.arxiv.org/api/query?id_list=2")
        c.get("https://api.crossref.org/works/z")
    monkeypatch.setattr(fulltext.httpx, "HTTPTransport", real_transport)
    assert slept == [fulltext._ARXIV_MIN_INTERVAL]


def _atom(ids):
    entries = "".join(
        "<entry><id>http://arxiv.org/abs/{0}v1</id><title>MNAR paper {0}</title>"
        "<summary>missing not at random</summary><published>2026-09-01T00:00:00Z</published>"
        "<author><name>A B</name></author></entry>".format(i) for i in ids)
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:arxiv="http://arxiv.org/schemas/atom">{}</feed>'.format(entries))


def test_default_arxiv_query_is_not_and_stacked():
    q = ProcessingSettings().arxiv_query
    assert " AND " not in q, (
        "默认 arXiv 检索式又叠了 AND：2026-09-21 实测三重 AND 全历史仅 1 篇、周度从未产出。"
        "确需收窄请先在 arXiv 上核对命中数（opensearch:totalResults），再同步改这条测试。")
    for term in ("MNAR", "missing not at random", "informative missingness", "missingness mechanism"):
        assert term in q


def test_arxiv_page_delay_meets_terms_of_use(monkeypatch):
    assert academic_search._ARXIV_PAGE_DELAY >= 3.0
    slept = []
    monkeypatch.setattr(academic_search.time, "sleep", lambda s: slept.append(s))
    c = AcademicSearchClient()
    c._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=_atom(["2609.00001", "2609.00002"]))))
    items = c.search_arxiv("all:MNAR", max_results=2)   # 整页返回 → 按翻页路径走一次等待
    c.close()
    assert len(items) == 2
    assert slept and set(slept) == {academic_search._ARXIV_PAGE_DELAY}


def test_arxiv_pagination_sleeps_between_full_pages(monkeypatch):
    """date_range 模式会翻多页（历史月份在结果深处）：每翻一页都要等满间隔。"""
    from datetime import date
    pages = [_atom(["2609.0001{}".format(i) for i in range(100)]), _atom(["2609.02001"])]
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, text=pages[min(len(calls) - 1, 1)])

    slept = []
    monkeypatch.setattr(academic_search.time, "sleep", lambda s: slept.append(s))
    c = AcademicSearchClient()
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    items = c.search_arxiv("all:MNAR", max_results=150,
                           date_range=(date(2026, 8, 16), date(2026, 9, 21)))
    c.close()
    assert len(calls) == 2 and calls[1]["start"] == "100"
    assert slept == [academic_search._ARXIV_PAGE_DELAY]
    assert len(items) == 101
