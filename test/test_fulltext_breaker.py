# -*- coding: utf-8 -*-
"""四通道默认打开（2026-09-05）的三道配套闸：按宿主熔断、每批额度、arXiv 路也走礼貌退避。

对应台账：docs/bugs/2026-09-04-fulltext-routes-too-narrow.md「打开」节。
全部打桩、不触网；每条用例前后把进程级 HOST_BREAKER 复位，避免顺序依赖。
"""
import pytest

from src.scholar import fulltext as FT
from src.scholar.schema import PaperMetadata


@pytest.fixture(autouse=True)
def _reset_breaker():
    FT.HOST_BREAKER.reset()
    FT.HOST_BREAKER.failures = 3
    yield
    FT.HOST_BREAKER.reset()
    FT.HOST_BREAKER.failures = 3


class _Resp:
    def __init__(self, code, text="", payload=None):
        self.status_code = code
        self.text = text
        self._payload = payload or {}
        self.content = b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Client:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        code = self.codes.pop(0) if self.codes else 200
        return _Resp(code)


# ---------------- HostBreaker ----------------

def test_host_breaker_trips_after_consecutive_failures_and_resets_on_success():
    b = FT.HostBreaker(failures=2)
    u = "https://api.openalex.org/works/doi:10.1/x"
    b.record(u, False)
    assert b.allow(u)
    b.record(u, True)                      # 成功清零连败
    b.record(u, False)
    assert b.allow(u)
    b.record(u, False)
    assert not b.allow(u) and "api.openalex.org" in b.tripped
    assert b.allow("https://www.ebi.ac.uk/x"), "按宿主熔断，别的宿主不受影响"
    b.reset()
    assert b.allow(u)


def test_polite_get_records_terminal_failures_and_short_circuits_once_tripped():
    b = FT.HostBreaker(failures=2)
    c = _Client([429, 429, 429,   # 第 1 次：退避用尽仍 429 → 终局失败 1
                 429, 429, 429])  # 第 2 次：终局失败 2 → 跳闸
    url = "https://api.semanticscholar.org/graph/v1/paper/x"
    r1 = FT.polite_get(c, url, sleep=lambda s: None, breaker=b)
    r2 = FT.polite_get(c, url, sleep=lambda s: None, breaker=b)
    assert r1.status_code == 429 and r2.status_code == 429 and len(c.calls) == 6
    assert not b.allow(url)
    r3 = FT.polite_get(c, url, sleep=lambda s: None, breaker=b)
    assert r3.status_code == 429 and "tripped" in r3.text and len(c.calls) == 6, "跳闸后不再触网"
    assert r3.json() == {}
    with pytest.raises(RuntimeError):
        r3.raise_for_status()


def test_polite_get_success_after_backoff_does_not_count_as_failure():
    b = FT.HostBreaker(failures=1)
    c = _Client([429, 200])
    r = FT.polite_get(c, "https://www.ebi.ac.uk/europepmc/x", sleep=lambda s: None, breaker=b)
    assert r.status_code == 200 and b.allow("https://www.ebi.ac.uk/") and len(c.calls) == 2


def test_polite_get_transport_exception_counts_as_failure_and_reraises():
    class _Boom:
        def get(self, url, **kw):
            raise ConnectionError("boom")
    b = FT.HostBreaker(failures=1)
    with pytest.raises(ConnectionError):
        FT.polite_get(_Boom(), "https://export.arxiv.org/api/query", breaker=b)
    assert not b.allow("https://export.arxiv.org/x")


# ---------------- RouteBudget ----------------

def test_route_budget_caps_calls_and_warns_once():
    from loguru import logger as _lg
    lines = []
    hid = _lg.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        b = FT.RouteBudget(max_calls=2, breaker_failures=5)
        assert FT.HOST_BREAKER.failures == 5, "阈值写进进程级熔断器"
        assert b.take() and b.take() and not b.take() and not b.take()
        assert b.calls == 2 and b._warned
    finally:
        _lg.remove(hid)
    # R2-2.13：额度耗尽的警告只打一次——第 3、4 次 take() 都返回 False，日志不得刷屏
    assert "\n".join(lines).count("本批额度") == 1


def _meta(**kw):
    base = dict(paper_id="p1", title="A fairly long title about missing data in EHR cohorts",
                doi="10.1/abc")
    base.update(kw)
    return PaperMetadata(**base)


def test_extra_route_candidates_respects_budget_and_skips_tripped_hosts(monkeypatch):
    ran = []

    def route_arxiv_title(meta, client):
        ran.append("arxiv"); return [("arxiv", "http://a/1.pdf")]

    def route_epmc_render(meta, client):
        ran.append("epmc"); return [("epmc", "http://e/1.pdf")]

    def route_openalex(meta, client, email=""):
        ran.append("openalex"); return [("openalex", "http://o/1.pdf")]

    def route_s2(meta, client):
        ran.append("s2"); return [("s2", "http://s/1.pdf")]
    monkeypatch.setattr(FT, "EXTRA_ROUTES", (route_arxiv_title, route_epmc_render, route_openalex, route_s2))
    monkeypatch.setattr(FT, "route_openalex", route_openalex)

    # 额度 2：只跑前两路
    b = FT.RouteBudget(max_calls=2)
    out = FT.extra_route_candidates(_meta(), client=None, budget=b, sleep=lambda s: None)
    assert ran == ["arxiv", "epmc"] and [s for s, _ in out] == ["arxiv", "epmc"]

    # openalex 宿主已跳闸：跳过它、不耗额度、记 skipped_routes
    ran.clear()
    FT.HOST_BREAKER.tripped.add("api.openalex.org")
    b2 = FT.RouteBudget(max_calls=10)
    out2 = FT.extra_route_candidates(_meta(), client=None, budget=b2, sleep=lambda s: None)
    assert ran == ["arxiv", "epmc", "s2"] and b2.skipped_routes == 1 and b2.calls == 3
    assert [s for s, _ in out2] == ["arxiv", "epmc", "s2"]

    # 没给 budget（旧调用形状）：四路照跑、只跳过被熔断的宿主
    ran.clear()
    FT.extra_route_candidates(_meta(), client=None, sleep=lambda s: None)
    assert ran == ["arxiv", "epmc", "s2"]


def test_resolve_oa_pdf_forwards_the_budget(monkeypatch):
    got = {}

    def fake_cands(meta, client, *, email="", delay=0.0, sleep=None, budget=None):
        got["budget"] = budget
        return [("epmc", "http://e/1.pdf")]
    monkeypatch.setattr(FT, "extra_route_candidates", fake_cands)

    class _C:
        def get(self, *a, **k):
            return _Resp(404)

        def close(self):
            pass
    b = FT.RouteBudget(max_calls=3)
    r = FT.resolve_oa_pdf(_meta(doi=None), email="", client=_C(), extra_routes=True, budget=b)
    assert got["budget"] is b and r.pdf_url == "http://e/1.pdf" and r.candidates == ["http://e/1.pdf"]


def test_arxiv_title_route_goes_through_polite_get(monkeypatch):
    seen = {}

    def fake_polite(client, url, **kw):
        seen["url"] = url
        return _Resp(500)
    monkeypatch.setattr(FT, "polite_get", fake_polite)
    assert FT.route_arxiv_title(_meta(arxiv_id=None), client=None) == []
    assert "export.arxiv.org" in seen["url"]


# ---------------- 第 1 轮审计补的守卫 ----------------

def test_breaker_half_opens_after_cooldown():
    """R1-2.4：跳闸不是进程终身——冷却到期放一次；成功复位、再失败立刻重新跳闸。"""
    t = [1000.0]
    b = FT.HostBreaker(failures=2, cooldown_sec=60.0, _now=lambda: t[0])
    u = "https://api.openalex.org/works/x"
    b.record(u, False); b.record(u, False)
    assert not b.allow(u)
    t[0] += 30
    assert not b.allow(u), "冷却未到，仍跳闸"
    t[0] += 31
    assert b.allow(u), "冷却到期，半开放行一次"
    b.record(u, False)
    assert not b.allow(u) and b.n_trips == 2, "半开后一失败立刻重新跳闸"
    t[0] += 61
    assert b.allow(u)
    b.record(u, True)
    assert b.allow(u) and b._fails["api.openalex.org"] == 0


def test_budget_exhaustion_and_tripped_hosts_are_reported_on_the_oa_result(monkeypatch):
    """R1-2.4：额度用完 / 宿主被熔断导致没命中时，resolve_oa_pdf 必须在 extra 里说出来，
    否则「暂时没拿到」与「真没有」同形，下游会把它沉淀成永久跳过。"""
    def route_arxiv_title(meta, client):
        return []

    def route_epmc_render(meta, client):
        return []

    def route_openalex(meta, client, email=""):
        return []

    def route_s2(meta, client):
        return []
    monkeypatch.setattr(FT, "EXTRA_ROUTES", (route_arxiv_title, route_epmc_render, route_openalex, route_s2))
    monkeypatch.setattr(FT, "route_openalex", route_openalex)

    class _C:
        def get(self, *a, **k):
            return _Resp(404)

        def close(self):
            pass
    # ① 宿主熔断：openalex 被跳过（本篇有 DOI、openalex 适用）→ routes_suppressed=1
    FT.HOST_BREAKER.tripped.add("api.openalex.org")
    b = FT.RouteBudget(max_calls=10)
    r = FT.resolve_oa_pdf(_meta(), client=_C(), extra_routes=True, budget=b, route_delay=0)
    assert r is not None and r.pdf_url is None and r.extra.get("routes_suppressed") == 1
    # ①b 无 DOI 篇目：openalex 跳不跳闸都跑不到（无标识没法查），不算被抑制 → 判 no_output（R2-1.2）
    b_nodoi = FT.RouteBudget(max_calls=10)
    r_nodoi = FT.resolve_oa_pdf(_meta(doi=None), client=_C(), extra_routes=True, budget=b_nodoi, route_delay=0)
    assert r_nodoi is not None and r_nodoi.pdf_url is None
    assert r_nodoi.extra is None or "routes_suppressed" not in (r_nodoi.extra or {})
    FT.HOST_BREAKER.reset()
    # ② 额度耗尽：一路都没跑 → exhausted → routes_suppressed ≥ 1
    b2 = FT.RouteBudget(max_calls=0)
    r2 = FT.resolve_oa_pdf(_meta(doi=None), client=_C(), extra_routes=True, budget=b2, route_delay=0)
    assert b2.exhausted and r2.extra.get("routes_suppressed") == 1
    # ③ 正常跑完四路、无抑制 → 不带该键
    b3 = FT.RouteBudget(max_calls=10)
    r3 = FT.resolve_oa_pdf(_meta(doi=None), client=_C(), extra_routes=True, budget=b3, route_delay=0)
    assert r3 is None or "routes_suppressed" not in (r3.extra or {})


def test_epmc_requests_go_through_polite_get_and_trip_the_breaker():
    """R1-2.3：www.ebi.ac.uk 也要过退避与熔断——此前四路里唯独它裸 c.get。
    R2-1.1：主链路走 EPMC_BREAKER，四路的 HOST_BREAKER 跳闸不切断它。"""
    FT.EPMC_BREAKER.failures = 2
    c = _Client([429] * 12)
    for _ in range(2):
        assert FT.europepmc_pmcid(doi="10.1/x", client=c) is None
    assert "www.ebi.ac.uk" in FT.EPMC_BREAKER.tripped
    assert "www.ebi.ac.uk" not in FT.HOST_BREAKER.tripped, "主链路不污染四路的 HOST_BREAKER"
    n = len(c.calls)
    assert FT.europepmc_fulltext(doi="10.1/x", client=c, return_stats=True, return_tail=True) == (None, 0, "")
    assert len(c.calls) == n, "跳闸后不再触网"


def test_extra_route_breaker_does_not_cut_primary_epmc():
    """R2-1.1：四路的 HOST_BREAKER 把 www.ebi.ac.uk 跳闸后，主链路 europepmc_fulltext 仍照常触网。"""
    # 模拟四路把 ebi.ac.uk 打跳闸
    FT.HOST_BREAKER.tripped.add("www.ebi.ac.uk")
    FT.HOST_BREAKER._fails["www.ebi.ac.uk"] = 3
    c = _Client([200])          # 主链路第一问就 200
    # europepmc_fulltext 内部先问 pmcid（经 EPMC_BREAKER，未跳闸 → 真发请求），
    # 桩 client 返回 200，pmcid 解析需一个合法 payload；这里只断言「确实触了网」（calls 非空）。
    FT.europepmc_fulltext(doi="10.1/x", client=c, return_stats=True, return_tail=True)
    assert len(c.calls) >= 1, "主链路 EPMC 未被四路熔断切断"


def test_polite_get_success_resets_consecutive_failure_count():
    """R2-3.8：成功（任意非 403/429）必须把该宿主连败清零——否则偶发成功之后，
    之前累计的连败还在账上，下次限流就被误判成「连续失败」而提前跳闸。"""
    b = FT.HostBreaker(failures=3)
    b.record("https://www.ebi.ac.uk/x", False)
    b.record("https://www.ebi.ac.uk/x", False)
    assert b._fails["www.ebi.ac.uk"] == 2
    FT.polite_get(_Client([200]), "https://www.ebi.ac.uk/x", sleep=lambda s: None, breaker=b)
    assert b._fails.get("www.ebi.ac.uk", 0) == 0 and b.allow("https://www.ebi.ac.uk/x")


def test_polite_get_5xx_counts_as_terminal_failure():
    """R3-A1：5xx 服务端故障（如 export.arxiv.org 的 503）必须计终局失败、会跳闸——否则 5xx 潮
    永不触发熔断，整批被记成 no_output 永久跳过（复发上一批 quota-failure 那类）。"""
    b = FT.HostBreaker(failures=2)
    c = _Client([503, 503])
    r1 = FT.polite_get(c, "https://export.arxiv.org/api/query", sleep=lambda s: None, breaker=b)
    r2 = FT.polite_get(c, "https://export.arxiv.org/api/query", sleep=lambda s: None, breaker=b)
    assert r1.status_code == 503 and r2.status_code == 503
    assert "export.arxiv.org" in b.tripped, "连续 5xx 应像 429 一样跳闸"
    # 跳闸后：合成 429、不再触网
    r3 = FT.polite_get(c, "https://export.arxiv.org/api/query", sleep=lambda s: None, breaker=b)
    assert r3.status_code == 429 and len(c.calls) == 2


def test_fulltext_xml_request_respects_a_tripped_epmc_breaker(monkeypatch):
    """R2-3.7：europepmc_fulltext 的 fullTextXML 取回必须过 polite_get——pmcid 那步已跳闸后，
    全文取回这步同样短路、不触网。此前把这一步改回裸 c.get 没有任何测试能拦。"""
    FT.EPMC_BREAKER.failures = 3
    for _ in range(3):
        FT.EPMC_BREAKER.record("https://www.ebi.ac.uk/europepmc/webservices/rest/x/fullTextXML", False)
    assert "www.ebi.ac.uk" in FT.EPMC_BREAKER.tripped
    monkeypatch.setattr(FT, "europepmc_pmcid", lambda **kw: "PMC1")
    c = _Client([200])
    out = FT.europepmc_fulltext(doi="10.1/x", client=c, return_stats=True, return_tail=True)
    assert out == (None, 0, "") and len(c.calls) == 0, "跳闸后 fullTextXML 也不得触网"


def test_europepmc_fulltext_return_tail_shape(monkeypatch):
    """R1-1.1：return_tail=True 三元组，余量供第二轮；四条失败出口统一 (None, 0, "")。"""
    xml = "<article><body><p>" + ("word " * 5000) + "</p></body></article>"

    class _C:
        def get(self, url, **kw):
            return _Resp(200, text=xml)
    monkeypatch.setattr(FT, "europepmc_pmcid", lambda **kw: "PMC1")
    text, raw, tail = FT.europepmc_fulltext(doi="10.1/x", client=_C(), max_chars=1000,
                                            return_stats=True, return_tail=True)
    assert len(text) == 1000 and raw > 1000 and tail and (text + tail).startswith("word word")
    monkeypatch.setattr(FT, "europepmc_pmcid", lambda **kw: None)
    assert FT.europepmc_fulltext(doi="10.1/x", client=_C(), return_stats=True, return_tail=True) == (None, 0, "")
    assert FT.europepmc_fulltext(doi="10.1/x", client=_C(), return_stats=True) == (None, 0)
