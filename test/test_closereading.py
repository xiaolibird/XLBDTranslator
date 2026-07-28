# -*- coding: utf-8 -*-
"""全文精读回归测试：句级三色解析 / PDF 下载守卫 / close_read 编排。全程 mock，不发网络。"""
import httpx

from src.scholar.schema import PaperSegment, PaperMetadata
from src.scholar import closereading as cr


def _seg(**kw):
    m = dict(paper_id="p1", title="MNAR in EHR")
    m.update(kw)
    return PaperSegment(segment_id=1, paper_id=m["paper_id"],
                        metadata=PaperMetadata(**m), original_abstract="abs")


# ---------------- 句级三色解析 ----------------

def test_parse_closeread_tags_and_fences():
    resp = '''```json
{"sections":[
  {"heading":"方法与数据","sentences":[
     {"text":"用可学习掩码嵌入。","tag":"方法论借鉴"},
     {"text":"数据来自 MIMIC-IV。","tag":null},
     {"text":"结论稳健。","tag":"可引用证据"}]}]}
```'''
    out = cr.parse_closeread(resp)
    tags = [s.tag for s in out.sections[0].sentences]
    assert tags == ["方法论借鉴", None, "可引用证据"]


def test_parse_closeread_drops_invalid_tag():
    out = cr.parse_closeread('{"sections":[{"heading":"x","sentences":[{"text":"a","tag":"乱标签"}]}]}')
    assert out.sections[0].sentences[0].tag is None  # 非法 tag 归 None


def test_parse_closeread_garbage_returns_none():
    assert cr.parse_closeread("not json") is None
    assert cr.parse_closeread('{"sections":[]}') is None  # 无有效句子


# ---------------- PDF 下载守卫 ----------------

def test_download_pdf_rejects_non_pdf(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"<html>not a pdf</html>")))
    assert cr.download_pdf("http://x/y", tmp_path / "a.pdf", client=client) is None


def test_download_pdf_accepts_pdf(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"%PDF-1.7\n...body...")))
    p = cr.download_pdf("http://x/y.pdf", tmp_path / "a.pdf", client=client)
    assert p is not None and p.exists() and p.read_bytes().startswith(b"%PDF")


def test_download_pdf_non_200_returns_none(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert cr.download_pdf("http://x/y.pdf", tmp_path / "a.pdf", client=client) is None


# ---------------- close_read 编排（fake LLM） ----------------

class _FakeLLM:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def call(self, prompt, model=None, max_tokens=None, temperature=None, json_mode=False):
        self.calls.append({"model": model, "json_mode": json_mode, "prompt": prompt})
        return self.resp


def test_close_read_builds_closereading():
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"发现X。","tag":"可引用证据"}]}]}')
    out = cr.close_read(_seg(), "全文正文……", "我的研究主线 MNAR/MA-GCT", llm,
                        model="strong-model", from_full_text=True, source="arxiv")
    assert out is not None
    assert out.from_full_text is True and out.model == "strong-model" and out.source == "arxiv"
    assert out.sections[0].sentences[0].tag == "可引用证据"
    assert llm.calls[0]["json_mode"] is True                 # 请求了结构化 JSON
    assert "我的研究主线 MNAR/MA-GCT" in llm.calls[0]["prompt"]  # 注入了研究主线


def test_close_read_empty_body_returns_none():
    assert cr.close_read(_seg(), "", "ri", _FakeLLM("{}")) is None


# ---------------- 精读择优：优先全文可得 ----------------

class _OA:
    def __init__(self, url, source="unpaywall"):
        self.pdf_url = url
        self.source = source


def test_close_read_segments_prefers_full_text_available(monkeypatch):
    """top-N 应优先挑「能拿到 OA 全文」的高优先级论文，避免恰好选中付费墙论文导致全文 0 命中。"""
    # 8 篇：优先级降序 p0..p7；只有 p3/p5/p6 有 OA 全文（模拟高优先级多为付费墙）
    segs = []
    for i in range(8):
        s = _seg(paper_id="p{}".format(i))
        s.priority_score = 1.0 - i * 0.1
        segs.append(s)
    oa_ids = {"p3", "p5", "p6"}

    def fake_resolve(meta, email=""):
        return _OA("http://x/{}.pdf".format(meta.paper_id)) if meta.paper_id in oa_ids else None

    read_order = []

    def fake_close_read_segment(seg, ri, llm, email="", model=None, scratch_dir=None, oa=None):
        read_order.append(seg.paper_id)
        from src.scholar.schema import CloseReading, CloseReadSection, CloseReadSentence
        return CloseReading(from_full_text=bool(oa and oa.pdf_url), source="arxiv",
                            sections=[CloseReadSection(heading="x",
                                                       sentences=[CloseReadSentence(text="a", tag=None)])])

    monkeypatch.setattr(cr, "resolve_oa_pdf", fake_resolve)
    monkeypatch.setattr(cr, "close_read_segment", fake_close_read_segment)

    done = cr.close_read_segments(segs, "ri", _FakeLLM("{}"), top_n=3, prefer_full_text=True)
    assert done == 3
    # 选中的应是有全文的 p3/p5/p6（而非纯 top-3 的 p0/p1/p2）
    assert set(read_order) == {"p3", "p5", "p6"}
    # 全部命中全文
    assert all(s.close_reading.from_full_text for s in segs if s.paper_id in oa_ids)


# ---------------- Europe PMC 全文回退（PDF 被出版商 403 挡住时） ----------------

_JATS = """<article>
  <front><article-meta>
    <title-group><article-title>Tiered early warning</article-title></title-group>
    <abstract><p>We built a two-stage framework.</p></abstract>
    <contrib-group><contrib>Someone</contrib></contrib-group>
  </article-meta></front>
  <body>
    <sec><title>Methods</title>
      <p>AUROC was 0.91 <xref ref-type="bibr" rid="b1">12</xref> across sites.</p>
    </sec>
    <sec><title>Results</title><p>False alarms dropped.</p>
      <table-wrap><caption><p>Table 1</p></caption></table-wrap>
    </sec>
  </body>
  <back><ref-list><ref><p>Reference one</p></ref></ref-list></back>
</article>"""


def test_jats_to_text_keeps_body_drops_refs_and_xref():
    from src.scholar.fulltext import jats_to_text
    txt = jats_to_text(_JATS)
    assert "Tiered early warning" in txt          # 标题
    assert "two-stage framework" in txt           # 摘要
    assert "AUROC was 0.91" in txt                # 正文
    assert "Methods" in txt and "Results" in txt  # 小节标题
    assert "12" not in txt                        # 引文角标不该混进句子
    assert "Reference one" not in txt             # 参考文献表不进精读正文
    assert "Table 1" not in txt                   # 表格版面不进精读正文


def test_jats_to_text_falls_back_when_unparseable():
    """DTD 外部实体等导致 XML 解析失败时，剥标签也好过丢掉整篇全文。"""
    from src.scholar.fulltext import jats_to_text
    txt = jats_to_text("<article><body><p>half broken &undefinedEntity; text</p>")
    assert "half broken" in txt and "text" in txt


def test_europepmc_pmcid_requires_in_epmc():
    """只有 pmcid 而 inEPMC=N 的条目取全文会 404，不能当成可得。"""
    from src.scholar.fulltext import europepmc_pmcid

    def handler(req):
        q = req.url.params.get("query", "")
        in_epmc = "Y" if "10.1/yes" in q else "N"
        return httpx.Response(200, json={"resultList": {"result": [
            {"pmcid": "PMC1", "inEPMC": in_epmc}]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert europepmc_pmcid(doi="10.1/yes", client=client) == "PMC1"
    assert europepmc_pmcid(doi="10.1/no", client=client) is None
    assert europepmc_pmcid(client=client) is None          # 无 DOI/PMID 直接放弃，不发请求


def test_close_read_segment_falls_back_to_europepmc_when_pdf_blocked(monkeypatch):
    """回归：Elsevier/MDPI/OUP 对机器人下 PDF 回 403，此时必须走 EPMC 全文而非降级成摘要。"""
    seg = _seg(doi="10.1016/j.patter.2023.100795")
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": _OA("http://blocked/x.pdf"))
    monkeypatch.setattr(cr, "download_pdf", lambda url, dest, **kw: None)   # 403 → None
    monkeypatch.setattr(cr, "europepmc_fulltext",
                        lambda doi=None, pmid=None: "全文正文，含 AUROC 0.91。")
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)
    assert out.from_full_text is True and out.source == "europepmc"
    assert "全文正文" in llm.calls[0]["prompt"] and "全文：" in llm.calls[0]["prompt"]


def test_close_read_segment_degrades_to_abstract_when_no_source(monkeypatch):
    seg = _seg(doi="10.1/closed")
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)
    monkeypatch.setattr(cr, "europepmc_fulltext", lambda doi=None, pmid=None: None)
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)
    assert out.from_full_text is False and out.source == "abstract"
