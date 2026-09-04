# -*- coding: utf-8 -*-
"""取全文的额外四路（默认关）：见 docs/bugs/2026-09-04-fulltext-routes-too-narrow.md。

全部走假 httpx client，不发网络。核心不变式：
  1. extra_routes=False（默认）时 resolve_oa_pdf 的调用形状与结果**逐字节不变**；
  2. 开启后只有 arXiv/Unpaywall 都没给 pdf_url 才试四路，顺序 arXiv 标题 → EPMC render →
     OpenAlex → S2，候选去重、NCBI PMC 换宿主到 EPMC；
  3. 下载侧对候选逐个试、三闸校验，反爬页（200 + HTML）不得落盘。
"""
import io
import json

import pytest

from src.scholar import fulltext as F
from src.scholar import closereading as CR
from src.scholar.schema import PaperMetadata, PaperSegment


class _Resp:
    def __init__(self, status=200, body=None, text="", content=b""):
        self.status_code = status
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")
        self.content = content or self.text.encode("utf-8")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP {}".format(self.status_code))


class _FakeClient:
    """按 URL 子串路由的假 client；记录每次 GET 的 URL，默认 404。"""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp() if callable(resp) else resp
        return _Resp(404)

    def close(self):
        pass


def _meta(**kw):
    base = dict(paper_id="p1", title="Deep Learning for Irregular Clinical Time Series with Missingness",
                doi="10.1000/xyz")
    base.update(kw)
    return PaperMetadata(**base)


UNPAYWALL_CLOSED = _Resp(200, {"is_oa": False, "oa_status": "closed"})
UNPAYWALL_OPEN = _Resp(200, {"is_oa": True, "oa_status": "gold",
                             "best_oa_location": {"url_for_pdf": "https://pub.org/a.pdf",
                                                  "url": "https://pub.org/a"}})
EPMC_HIT = _Resp(200, {"resultList": {"result": [{"pmcid": "PMC8661408", "inEPMC": "Y"}]}})
OPENALEX = _Resp(200, {"best_oa_location": {"pdf_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC8661408/pdf"},
                       "locations": [{"pdf_url": "https://repo.edu/x.pdf"},
                                     {"pdf_url": "https://repo.edu/x.pdf"},   # 重复
                                     {"pdf_url": ""}]})
S2 = _Resp(200, {"openAccessPdf": {"url": "https://s2.org/y.pdf"}})
ARXIV_XML = ("<feed><entry><id>http://arxiv.org/abs/2401.00001v2</id>"
             "<title>Deep Learning for Irregular Clinical Time Series with Missingness</title></entry></feed>")


# ---------------- 默认关：行为逐字节不变 ----------------

def test_default_off_never_touches_extra_apis():
    c = _FakeClient({"unpaywall": UNPAYWALL_CLOSED})
    oa = F.resolve_oa_pdf(_meta(), email="me@x.org", client=c)
    assert oa.pdf_url is None and oa.oa_status == "closed" and oa.source == "unpaywall"
    assert oa.candidates == []
    assert len(c.calls) == 1 and "unpaywall" in c.calls[0]


def test_default_off_without_email_makes_no_call_at_all():
    c = _FakeClient()
    oa = F.resolve_oa_pdf(_meta(), email="", client=c)
    assert oa.oa_status == "closed" and c.calls == []
    oa2 = F.resolve_oa_pdf(_meta(doi=None), email="", client=c)
    assert oa2.oa_status == "unknown" and c.calls == []


def test_settings_default_is_off():
    from src.scholar.settings import ProcessingSettings
    s = ProcessingSettings()
    assert s.fulltext_extra_routes is False
    assert s.fulltext_route_delay == 1.5


# ---------------- 开启后的顺序 / 去重 / 换宿主 ----------------

def test_extra_routes_run_in_order_and_dedupe(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = _FakeClient({"unpaywall": UNPAYWALL_CLOSED, "export.arxiv.org": _Resp(200, text=ARXIV_XML),
                     "europepmc/webservices": EPMC_HIT, "api.openalex.org": OPENALEX,
                     "api.semanticscholar.org": S2})
    oa = F.resolve_oa_pdf(_meta(), email="me@x.org", client=c, extra_routes=True)
    assert oa.pdf_url == "https://arxiv.org/pdf/2401.00001v2"
    assert oa.source == "arxiv-title"
    assert oa.candidates == [
        "https://arxiv.org/pdf/2401.00001v2",
        "https://europepmc.org/articles/PMC8661408?pdf=render",
        # OpenAlex 的 NCBI 链接换宿主后与 EPMC 那条**重复**，去重掉；repo.edu 只出一次
        "https://repo.edu/x.pdf",
        "https://s2.org/y.pdf",
    ]
    assert oa.extra["routes"] == ["arxiv-title", "epmc-render", "openalex", "s2"]
    # 调用顺序：Unpaywall → arXiv → EPMC → OpenAlex → S2
    hosts = [u.split("/")[2] for u in c.calls]
    assert hosts == ["api.unpaywall.org", "export.arxiv.org", "www.ebi.ac.uk",
                     "api.openalex.org", "api.semanticscholar.org"]


def test_extra_routes_skipped_when_unpaywall_has_pdf():
    c = _FakeClient({"unpaywall": UNPAYWALL_OPEN, "api.openalex.org": OPENALEX})
    oa = F.resolve_oa_pdf(_meta(), email="me@x.org", client=c, extra_routes=True)
    assert oa.pdf_url == "https://pub.org/a.pdf" and oa.source == "unpaywall"
    assert oa.candidates == [] and len(c.calls) == 1


def test_extra_routes_skipped_when_arxiv_id_present():
    c = _FakeClient()
    oa = F.resolve_oa_pdf(_meta(arxiv_id="2401.00001"), email="me@x.org", client=c, extra_routes=True)
    assert oa.source == "arxiv" and c.calls == []


def test_extra_routes_all_miss_returns_primary_result(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = _FakeClient({"unpaywall": UNPAYWALL_CLOSED})
    oa = F.resolve_oa_pdf(_meta(), email="me@x.org", client=c, extra_routes=True)
    assert oa.pdf_url is None and oa.oa_status == "closed" and oa.candidates == []


def test_extra_routes_without_email_still_try_doi_routes(monkeypatch):
    """没 email 走不了 Unpaywall，但 EPMC/S2/OpenAlex 只要 DOI 就能问。"""
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = _FakeClient({"api.semanticscholar.org": S2})
    oa = F.resolve_oa_pdf(_meta(), email="", client=c, extra_routes=True)
    assert oa.pdf_url == "https://s2.org/y.pdf" and oa.source == "s2"
    assert not any("unpaywall" in u for u in c.calls)


def test_extra_routes_no_doi_only_arxiv_title(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = _FakeClient({"export.arxiv.org": _Resp(200, text=ARXIV_XML)})
    oa = F.resolve_oa_pdf(_meta(doi=None), email="me@x.org", client=c, extra_routes=True)
    assert oa.source == "arxiv-title"
    assert all("arxiv" in u for u in c.calls)


def test_route_delay_sleeps_between_routes_not_before_first(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    c = _FakeClient({"unpaywall": UNPAYWALL_CLOSED})
    F.resolve_oa_pdf(_meta(), email="me@x.org", client=c, extra_routes=True, route_delay=1.5)
    assert slept == [1.5, 1.5, 1.5]          # 四路之间三次间隔


def test_route_exception_is_swallowed(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    class _Boom(_FakeClient):
        def get(self, url, **kw):
            self.calls.append(url)
            if "openalex" in url:
                raise ConnectionError("down")
            return super().get(url, **kw)
    c = _Boom({"unpaywall": UNPAYWALL_CLOSED, "api.semanticscholar.org": S2})
    oa = F.resolve_oa_pdf(_meta(), email="me@x.org", client=c, extra_routes=True)
    assert oa.source == "s2"


# ---------------- 单路细节 ----------------

def test_arxiv_title_rejects_low_overlap():
    xml = ("<feed><entry><id>http://arxiv.org/abs/2401.99999</id>"
           "<title>Completely Unrelated Paper About Galaxies</title></entry></feed>")
    c = _FakeClient({"export.arxiv.org": _Resp(200, text=xml)})
    assert F.route_arxiv_title(_meta(), c) == []
    assert F.route_arxiv_title(_meta(title="short"), c) == []


def test_arxiv_title_rejects_short_titles_and_one_sided_containment():
    """压测 S2：|a∩b|/|a| 单向重合对短标题恒为 1.0——「Missing Data Imputation」会命中一篇长综述，
    然后把别篇 PDF 当全文精读。实词 <4 不检索；命中标题要反向覆盖 ≥0.6。"""
    survey = ("<feed><entry><id>http://arxiv.org/abs/2401.00001v1</id>"
              "<title>A Survey of Missing Data Imputation Methods for Multivariate Time Series "
              "with Transformers</title></entry></feed>")
    c = _FakeClient({"export.arxiv.org": _Resp(200, text=survey)})
    assert F.route_arxiv_title(_meta(title="Missing Data Imputation"), c) == []
    assert c.calls == []                                             # 实词太少，连请求都不发
    assert F.route_arxiv_title(_meta(title="Missing Data Imputation Methods Study"), c) == []   # 单向包含
    c2 = _FakeClient({"export.arxiv.org": _Resp(200, text=ARXIV_XML)})
    assert F.route_arxiv_title(_meta(), c2) == [("arxiv-title", "https://arxiv.org/pdf/2401.00001v2")]


def test_s2_strips_whitespace_in_url():
    c = _FakeClient({"api.semanticscholar.org": _Resp(200, {"openAccessPdf": {"url": "  https://s2.org/y.pdf \n"}})})
    assert F.route_s2(_meta(), c) == [("s2", "https://s2.org/y.pdf")]


def test_validate_accepts_pdf_header_within_first_kilobyte_and_small_text_pdf():
    """规范允许 %PDF- 头前有 BOM/垃圾字节；2 页纯文本短文 ~1.2KB 是合法 PDF，20KB 体积闸误杀（压测 S9）。"""
    data = _two_page_pdf()
    ok, _ = F.validate_pdf_bytes(b"\r\n\xef\xbb\xbf" + data)
    assert ok
    # 去掉测试填充、只留约 1.2KB（审计实测的 2 页纯文本短文体量）：必须通过体积闸
    raw = data[: data.rfind(b"\n%")] if b"\n%" in data else data
    small = raw + b"\n%" + b"y" * max(0, 1200 - len(raw))
    ok, why = F.validate_pdf_bytes(small)
    assert ok, why
    ok, why = F.validate_pdf_bytes(raw[:400])
    assert not ok                                                    # 残片仍被拒


def test_download_pdf_validate_rejects_truncated_tiny_pdf(tmp_path):
    """变异 M26b：validate 分支要真的卡住「有 %PDF magic 但根本解析不了」的残片。"""
    c = _FakeClient({"x": _Resp(200, content=b"%PDF-1.4 tiny")})
    assert CR.download_pdf("https://x/a.pdf", tmp_path / "t.pdf", client=c, validate=True) is None
    assert not (tmp_path / "t.pdf").exists()


def test_epmc_render_requires_in_epmc_flag():
    c = _FakeClient({"europepmc/webservices":
                     _Resp(200, {"resultList": {"result": [{"pmcid": "PMC1", "inEPMC": "N"}]}})})
    assert F.route_epmc_render(_meta(), c) == []
    c2 = _FakeClient({"europepmc/webservices": EPMC_HIT})
    assert F.route_epmc_render(_meta(), c2) == [
        ("epmc-render", "https://europepmc.org/articles/PMC8661408?pdf=render")]


def test_s2_ignores_doi_org_self_reference():
    c = _FakeClient({"api.semanticscholar.org": _Resp(200, {"openAccessPdf": {"url": "https://doi.org/10.1/x"}})})
    assert F.route_s2(_meta(), c) == []
    c2 = _FakeClient()
    assert F.route_s2(_meta(doi=None), c2) == [] and c2.calls == []


@pytest.mark.parametrize("url,expect", [
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC8661408/pdf/x.pdf",
     "https://europepmc.org/articles/PMC8661408?pdf=render"),
    ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/", "https://europepmc.org/articles/PMC123?pdf=render"),
    ("https://repo.edu/x.pdf", "https://repo.edu/x.pdf"),
    ("", ""),
])
def test_rewrite_pmc_url(url, expect):
    assert F.rewrite_pmc_url(url) == expect


def test_polite_get_backs_off_on_429_then_returns_200():
    seq = iter([_Resp(429), _Resp(403), _Resp(200, text="ok")])
    c = _FakeClient({"x": lambda: next(seq)})
    slept = []
    r = F.polite_get(c, "https://x/y", sleep=slept.append)
    assert r.status_code == 200
    assert slept == [5.0, 12.5]


def test_polite_get_gives_up_after_retries():
    c = _FakeClient({"x": _Resp(403)})
    slept = []
    r = F.polite_get(c, "https://x/y", sleep=slept.append)
    # 最后一轮失败后不再 sleep：死候选此前要白等 31s（第 1 轮审计 A9）
    assert r.status_code == 403 and len(c.calls) == 3 and slept == [5.0, 12.5]


# ---------------- 三闸校验 + 下载侧逐候选试 ----------------

def _two_page_pdf() -> bytes:
    pypdf = pytest.importorskip("pypdf")
    w = pypdf.PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    data = buf.getvalue()
    # 凑到体积闸之上（空白页 PDF 很小）：往尾部塞注释字节不影响解析
    return data + b"\n%" + b"x" * F.PDF_MIN_BYTES


def test_validate_pdf_bytes_three_gates():
    ok, why = F.validate_pdf_bytes(b"<html>403 Forbidden</html>")
    assert not ok and "不是 PDF" in why
    ok, why = F.validate_pdf_bytes(b"%PDF-1.4 tiny")
    assert not ok and "体积" in why
    ok, why = F.validate_pdf_bytes(_two_page_pdf())
    assert ok, why
    ok, why = F.validate_pdf_bytes(b"%PDF-1.4" + b"x" * F.PDF_MIN_BYTES)
    assert not ok and ("解析失败" in why or "页" in why)


def test_download_pdf_validate_rejects_anti_bot_page(tmp_path):
    c = _FakeClient({"x": _Resp(200, text="<html>Just a moment...</html>")})
    assert CR.download_pdf("https://x/a.pdf", tmp_path / "a.pdf", client=c, validate=True) is None
    assert not (tmp_path / "a.pdf").exists()
    # 默认 validate=False：只看 %PDF magic（历史行为）
    c2 = _FakeClient({"x": _Resp(200, content=b"%PDF-1.4 tiny")})
    assert CR.download_pdf("https://x/a.pdf", tmp_path / "b.pdf", client=c2) == tmp_path / "b.pdf"
    c3 = _FakeClient({"x": _Resp(200, content=_two_page_pdf())})
    assert CR.download_pdf("https://x/a.pdf", tmp_path / "c.pdf", client=c3, validate=True) == tmp_path / "c.pdf"


def test_close_read_segment_tries_candidates_in_order_with_validation(monkeypatch, tmp_path):
    seen = []

    def _fake_download(url, dest, client=None, timeout=60.0, max_bytes=0, *, validate=False):
        seen.append((url, validate))
        if url.endswith("bad.pdf"):
            return None
        dest = tmp_path / "ok.pdf"
        dest.write_bytes(b"%PDF-1.4")
        return dest
    monkeypatch.setattr(CR, "download_pdf", _fake_download)
    monkeypatch.setattr(CR, "_pdf_text_with_stats", lambda p, **k: ("body " * 400, 4000))
    monkeypatch.setattr(CR, "close_read",
                        lambda seg, body, ri, llm, **k: CR.CloseReading(
                            from_full_text=k.get("from_full_text", True), source=k.get("source"),
                            sections=[CR.CloseReadSection(heading="h", sentences=[
                                CR.CloseReadSentence(text="s", tag=None)])]))
    oa = F.OAResult(oa_status="green", pdf_url="https://a/bad.pdf", source="epmc-render",
                    candidates=["https://a/bad.pdf", "https://b/good.pdf"])
    seg = PaperSegment(segment_id=1, paper_id="p1", metadata=_meta(), original_abstract="abs")
    cr = CR.close_read_segment(seg, "ri", llm=None, oa=oa, scratch_dir=tmp_path)
    assert cr is not None and cr.from_full_text and cr.source == "epmc-render"
    assert seen == [("https://a/bad.pdf", True), ("https://b/good.pdf", True)]


def test_close_read_segment_without_candidates_keeps_legacy_download_shape(monkeypatch, tmp_path):
    """主链路（无 candidates）下载调用不带 validate——既有桩 `lambda url, dest` 必须继续能用。"""
    calls = []

    def _legacy_download(url, dest):
        calls.append(url)
        return None
    monkeypatch.setattr(CR, "download_pdf", _legacy_download)
    monkeypatch.setattr(CR, "europepmc_fulltext",
                        lambda doi=None, pmid=None, max_chars=40000, return_stats=False, **kw: (None, 0))
    monkeypatch.setattr(CR, "close_read",
                        lambda seg, body, ri, llm, **k: None)
    oa = F.OAResult(oa_status="gold", pdf_url="https://pub/a.pdf", source="unpaywall")
    seg = PaperSegment(segment_id=1, paper_id="p1", metadata=_meta(), original_abstract="abs")
    CR.close_read_segment(seg, "ri", llm=None, oa=oa, scratch_dir=tmp_path)
    assert calls == ["https://pub/a.pdf"]


def test_close_read_segments_passes_extra_routes_only_when_on(monkeypatch):
    """开关关着时 resolve_oa_pdf 的调用形状与历史一致（旧式桩 `lambda meta, email="", client=None`）。"""
    shapes = []

    def _legacy_resolve(meta, email="", client=None):
        shapes.append("legacy")
        return None
    monkeypatch.setattr(CR, "resolve_oa_pdf", _legacy_resolve)
    monkeypatch.setattr(CR, "europepmc_pmcid", lambda **k: None)
    monkeypatch.setattr(CR, "close_read_segment", lambda *a, **k: None)
    segs = [PaperSegment(segment_id=i, paper_id="p{}".format(i), metadata=_meta(paper_id="p{}".format(i)),
                         priority_score=1.0 - i / 10) for i in range(3)]
    CR.close_read_segments(segs, "ri", llm=None, top_n=1)
    assert shapes == ["legacy"] * 3

    got = []
    monkeypatch.setattr(CR, "resolve_oa_pdf",
                        lambda meta, email="", client=None, **kw: got.append(kw) or None)
    CR.close_read_segments(segs, "ri", llm=None, top_n=1, extra_routes=True, route_delay=0.2)
    assert got and all(k == {"extra_routes": True, "route_delay": 0.2} for k in got)


def test_download_pdf_validate_accepts_magic_beyond_first_five_bytes(tmp_path):
    """变异 R23：`download_pdf` 的 validate 分支把 magic 窗口改回前 5 字节 → 合法但前面有
    BOM/`\\r\\n` 的 PDF（规范允许 %PDF- 出现在前 1024 字节内）被当成反爬页丢掉。
    既有那条 validate 测试用的载荷 magic 在第 0 字节，盯不住这个。"""
    body = b"\r\n\xef\xbb\xbf" + _two_page_pdf()
    c = _FakeClient({"x": _Resp(200, content=body)})
    dest = tmp_path / "ok.pdf"
    assert CR.download_pdf("https://x/a.pdf", dest, client=c, validate=True) == dest
    assert dest.read_bytes() == body
    # 默认（不校验）分支仍是严格前 5 字节：历史行为逐字节不变
    c2 = _FakeClient({"x": _Resp(200, content=body)})
    assert CR.download_pdf("https://x/b.pdf", tmp_path / "b.pdf", client=c2) is None
