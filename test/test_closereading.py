# -*- coding: utf-8 -*-
"""全文精读回归测试：句级三色解析 / PDF 下载守卫 / close_read 编排。全程 mock，不发网络。"""
import httpx
import pytest

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


# ---------------- pdf_to_text 单页上限 ----------------

def _three_page_pdf(tmp_path):
    """三页夹具：正常页 / 图表矢量文字层病态页（40000+ 字符）/ 只含唯一标记的第三页。

    第 2 页模拟真实语料里的病态页——基线实现读到它就会用光 40000 预算，第 3 页永远读不到。
    """
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_textbox(fitz.Rect(20, 20, 575, 822), ("Alpha beta gamma delta. " * 60)[:1000], fontsize=8)
    p2 = doc.new_page(width=2000, height=4000)
    p2.insert_textbox(fitz.Rect(5, 5, 1995, 3995), ("X9 " * 20000)[:45000], fontsize=3)
    p3 = doc.new_page()
    p3.insert_text((72, 72), "MARKER_PAGE3")
    out = tmp_path / "three_page.pdf"
    doc.save(str(out))
    doc.close()
    return out


def _page_texts(path):
    import fitz
    with fitz.open(str(path)) as doc:
        return [p.get_text("text", sort=True) for p in doc]


def _legacy_pdf_to_text(path, max_chars):
    """基线实现（页循环内 total >= max_chars 即 break），只用于对照断言。"""
    import fitz
    parts, total = [], 0
    with fitz.open(str(path)) as doc:
        for page in doc:
            t = page.get_text("text", sort=True)
            if not t:
                continue
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
    return "".join(parts)[:max_chars]


def test_pdf_to_text_page_cap_bypasses_pathological_page(tmp_path):
    fx = _three_page_pdf(tmp_path)
    pages = _page_texts(fx)
    assert len(pages[1]) >= 39000  # 夹具前提：第 2 页确实是病态页

    text = cr.pdf_to_text(fx, max_chars=40000, page_max_chars=20000)
    # 病态页只贡献 20000 字符，省下的预算让第 3 页进得来
    assert pages[1][:20000] in text
    assert len(pages[1][:20000]) == 20000
    assert "MARKER_PAGE3" in text
    # 基线实现读完第 2 页就用光预算，取不到第 3 页
    assert "MARKER_PAGE3" not in _legacy_pdf_to_text(fx, 40000)


def test_pdf_to_text_default_matches_legacy_for_large_max_chars(tmp_path):
    """page_max_chars 默认 None 时逐字节等价于基线——manual 链路的分块边界不许漂移。"""
    fx = _three_page_pdf(tmp_path)
    assert cr.pdf_to_text(fx, max_chars=1_000_000) == _legacy_pdf_to_text(fx, 1_000_000)


# ---------------- close_read 编排（fake LLM） ----------------

class _FakeLLM:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def call(self, prompt, model=None, max_tokens=None, temperature=None, json_mode=False):
        self.calls.append({"model": model, "json_mode": json_mode, "prompt": prompt})
        return self.resp


def test_verify_citable_numbers_demotes_unsupported():
    """数值幻觉防线：「可引用证据」句的数字必须能在喂入正文中逐字找到（去千分位逗号），
    找不到的句子降级 tag=None——不再进 citable highlight/取证链，句子本身保留。"""
    out = cr.parse_closeread(
        '{"sections":[{"heading":"关键结论","sentences":['
        '{"text":"AUPRC 0.44，样本 1,234 例。","tag":"可引用证据"},'
        '{"text":"错误率下降 99.9%。","tag":"可引用证据"},'
        '{"text":"背景提到 777。","tag":null}]}]}')
    body = "实验结果 AUPRC 0.44，队列共 1,234 例患者。"
    demoted = cr.verify_citable_numbers(out, body)
    s = out.sections[0].sentences
    assert demoted == 1
    assert s[0].tag == "可引用证据"    # 0.44 与 1234（正文写作 1,234）都可溯源
    assert s[1].tag is None            # 99.9 在正文无处溯源 → 降级
    assert s[2].tag is None            # 非可引用句不参与回查（原本就是 None）


def test_close_read_page_rule_follows_anchor_presence():
    """页码锚防线：喂入文本没有 [p.N] 锚时，prompt 必须禁止标页码（模型看不见页边界，
    标出的页码必然是编造）；带锚时才允许并要求按锚标注。"""
    resp = '{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}'
    llm = _FakeLLM(resp)
    cr.close_read(_seg(), "无页锚的全文正文……", "ri", llm)
    assert "禁止标注任何页码" in llm.calls[0]["prompt"]
    llm2 = _FakeLLM(resp)
    cr.close_read(_seg(), "[p.3] 带页锚的正文……", "ri", llm2)
    assert "[p.N] 页锚标注页码" in llm2.calls[0]["prompt"]
    assert "禁止标注任何页码" not in llm2.calls[0]["prompt"]


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

    def fake_resolve(meta, email="", client=None):
        return _OA("http://x/{}.pdf".format(meta.paper_id)) if meta.paper_id in oa_ids else None

    read_order = []

    # **kw 吸收 close_read_segments 无条件透传的 deep/max_chars/max_chunks：
    # 少了它替身会在调用处 TypeError，被 close_read_segments 的 except 吞成 cr=None
    def fake_close_read_segment(seg, ri, llm, email="", model=None, scratch_dir=None,
                                oa=None, **kw):
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
      <table-wrap><caption><p>Table 1</p></caption>
        <table><tbody><tr><td>AUROC</td><td>0.913</td></tr></tbody></table>
      </table-wrap>
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
    # 表格是 Results 里效应量/样本量的唯一载体，caption 与单元格数值都必须进正文
    assert "Table 1" in txt
    assert "0.913" in txt
    # 且要带段落边界：单元格既不能互相粘连，也不能粘到前后正文上
    lines = [ln.strip() for ln in txt.splitlines()]
    assert "0.913" in lines
    assert "AUROC0.913" not in txt and "dropped.Table" not in txt


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
    _TXT = "全文正文，含 AUROC 0.91。"
    monkeypatch.setattr(
        cr, "europepmc_fulltext",
        lambda doi=None, pmid=None, max_chars=40000, return_stats=False, **kw:
            ((_TXT, len(_TXT)) if return_stats else _TXT))
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)
    assert out.from_full_text is True and out.source == "europepmc"
    assert "全文正文" in llm.calls[0]["prompt"] and "全文：" in llm.calls[0]["prompt"]


def test_close_read_segment_degrades_to_abstract_when_no_source(monkeypatch):
    seg = _seg(doi="10.1/closed")
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)
    monkeypatch.setattr(
        cr, "europepmc_fulltext",
        lambda doi=None, pmid=None, max_chars=40000, return_stats=False, **kw:
            ((None, 0) if return_stats else None))
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)
    assert out.from_full_text is False and out.source == "abstract"


def _degrade_to_abstract(monkeypatch):
    """把 PDF 与 EPMC 两条全文来源都断掉，逼 close_read_segment 走摘要降级。"""
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)
    monkeypatch.setattr(
        cr, "europepmc_fulltext",
        lambda doi=None, pmid=None, max_chars=40000, return_stats=False, **kw:
            ((None, 0) if return_stats else None))


def test_verify_on_abstract_route_checks_original_not_translation(monkeypatch):
    """回归：摘要降级篇的数字回查必须对照英文原摘要，不能拿 digest 译文自证闭环。

    translated_abstract 是批量翻译 LLM 的输出、无数字保真校验：原文 AUC 0.87 被译丢成
    0.78 时，旧逻辑在译文里回查命中 0.78 → 错数顶着「可引用证据」进取证链。数字是语言
    不变量，对照原摘要后 0.78 必须降级，译对的 0.91 仍应通过。"""
    seg = _seg(doi="10.1/closed")
    seg.original_abstract = "The model achieved AUC 0.87 and sensitivity 0.91."
    seg.translated_abstract = "该模型 AUC 达 0.78，敏感度 0.91。"   # 译文把 0.87 抄错成 0.78
    _degrade_to_abstract(monkeypatch)
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":['
                   '{"text":"AUC 0.78。","tag":"可引用证据"},'
                   '{"text":"敏感度 0.91。","tag":"可引用证据"}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)
    s = out.sections[0].sentences
    assert s[0].tag is None            # 0.78 只存在于译文，原摘要无处溯源 → 降级
    assert s[1].tag == "可引用证据"    # 0.91 语言不变，原摘要可溯源 → 保留
    # 喂读正文仍是中文译文（精读体验不变），换的只是回查对照源
    assert "该模型 AUC 达 0.78" in llm.calls[0]["prompt"]


def test_verify_on_abstract_route_falls_back_to_translation_when_no_original(monkeypatch):
    """原摘要缺失时对照源退回译文：此时喂读与对照本就同源，不能因缺原文把数字全数误杀。"""
    seg = _seg(doi="10.1/closed")
    seg.original_abstract = ""
    seg.translated_abstract = "该模型 AUC 达 0.78。"
    _degrade_to_abstract(monkeypatch)
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":['
                   '{"text":"AUC 0.78。","tag":"可引用证据"}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)
    assert out.sections[0].sentences[0].tag == "可引用证据"


# ---------------- 阅读深度量尺（新字段） ----------------

def test_pdf_text_with_stats_raw_ignores_page_cap(tmp_path):
    """raw 必须是未加工的真长度：若也按单页上限收，truncated 会自证为假。"""
    fx = _three_page_pdf(tmp_path)
    pages = _page_texts(fx)
    expect_raw = sum(len(t) for t in pages)

    text, raw = cr._pdf_text_with_stats(fx, max_chars=40000, page_max_chars=20000)
    assert raw == expect_raw                     # 不受 page_max_chars=20000 影响
    assert raw > len(text)                       # 夹具确实超预算
    assert text == cr.pdf_to_text(fx, max_chars=40000, page_max_chars=20000)


def test_close_read_segment_pdf_route_records_depth_fields(tmp_path, monkeypatch):
    fx = _three_page_pdf(tmp_path)
    expect_raw = sum(len(t) for t in _page_texts(fx))
    seg = _seg(doi="10.1/oa")
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": _OA("http://x/y.pdf"))
    monkeypatch.setattr(cr, "download_pdf", lambda url, dest, **kw: fx)
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
    out = cr.close_read_segment(seg, "ri", llm, scratch_dir=tmp_path)

    body = cr.pdf_to_text(fx, max_chars=cr.AUTO_MAX_CHARS,
                          page_max_chars=cr.AUTO_PAGE_MAX_CHARS)
    assert out.body_chars == len(body)
    assert out.body_chars_raw == expect_raw
    assert out.truncated is (expect_raw > out.body_chars) is True
    assert out.reading_depth == "single-call" and out.n_chunks == 1


def _fake_jats(body_chars, n_paras=200):
    """造一篇总正文 body_chars 字、切成 n_paras 个 <sec>/<p> 的假 JATS。

    必须切成多段：_jats_walk 的预算检查只写在 `for child in el:` 的下钻处，
    单个 text 节点无论多大都不会被预算截断。若把正文塞进一个 <p>，新旧 walk 预算
    量出的 raw 完全相同，「EPMC 的 raw 与 PDF 同量纲、不受 max_chars*2 封顶」
    这条不变式就没有任何回归护栏（回退 _JATS_RAW_BUDGET 也照样全绿）。
    """
    per = max(1, body_chars // n_paras)
    secs = "".join("<sec><p>{}</p></sec>".format("A" * per) for _ in range(n_paras))
    return "<article><body>{}</body></article>".format(secs)


def test_close_read_segment_epmc_route_records_raw_before_truncation(monkeypatch):
    """EPMC 路线的 raw 必须是截断前真长度，且与 PDF 路线同量纲。"""
    seg = _seg(doi="10.1/epmc")
    xml = _fake_jats(150000)
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)

    from src.scholar import fulltext as ft
    monkeypatch.setattr(ft, "europepmc_pmcid", lambda **kw: "PMC9")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text=xml)))
    monkeypatch.setattr(
        cr, "europepmc_fulltext",
        lambda doi=None, pmid=None, max_chars=40000, return_stats=False, **kw:
            ft.europepmc_fulltext(doi=doi, pmid=pmid, client=client,
                                  max_chars=max_chars, return_stats=return_stats))
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)

    assert out.source == "europepmc" and out.truncated is True
    assert out.body_chars == 40000
    # 旧的 max_chars*2 walk 预算会把 raw 削平到 ~80000；真长度必须 >= 正文本身
    assert out.body_chars_raw >= 150000


def test_jats_to_text_with_stats_raw_not_capped_by_double_max_chars():
    """正文远超 2×max_chars 时 raw 仍是真长度（旧的 max_chars*2 预算会削到 ~80000）。"""
    from src.scholar.fulltext import _jats_to_text_with_stats
    text, raw = _jats_to_text_with_stats(_fake_jats(400000), max_chars=40000)
    assert len(text) == 40000
    # 判别点：旧预算下 walk 在累计 ~2×max_chars 处 break，raw 只有 8 万出头；
    # 解耦后必须量到全篇。阈值取真长度而非 2*max_chars，避免 break 恰好越界几百字时误绿。
    assert raw >= 400000


def test_close_read_segment_abstract_route_not_truncated(monkeypatch):
    seg = _seg(doi="10.1/closed")
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)
    monkeypatch.setattr(
        cr, "europepmc_fulltext",
        lambda doi=None, pmid=None, max_chars=40000, return_stats=False, **kw:
            ((None, 0) if return_stats else None))
    llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
    out = cr.close_read_segment(seg, "ri", llm)
    assert out.source == "abstract"
    assert out.truncated is False
    assert out.body_chars_raw == out.body_chars == len(seg.original_abstract)


# ---------------- europepmc_fulltext 的 return_stats 契约（四条失败出口） ----------------

def _epmc_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _epmc_failure_cases(monkeypatch):
    """四条失败出口：无 pmcid / 非 200 / text 为空 / 抛异常。返回 (名字, kwargs) 列表。"""
    from src.scholar import fulltext as ft

    cases = []
    # 1) 无 pmcid（检索结果 inEPMC=N）
    cases.append(("no_pmcid", dict(client=_epmc_client(
        lambda req: httpx.Response(200, json={"resultList": {"result": []}})))))
    # 2) fullTextXML 非 200
    # 3) fullTextXML 200 但正文为空 → jats_to_text 返回 ""
    # 4) 请求过程抛异常
    def make(status_or_exc, body=""):
        def handler(req):
            if "/search" in str(req.url):
                return httpx.Response(200, json={"resultList": {"result": [
                    {"pmcid": "PMC1", "inEPMC": "Y"}]}})
            if isinstance(status_or_exc, Exception):
                raise status_or_exc
            return httpx.Response(status_or_exc, text=body)
        return _epmc_client(handler)

    cases.append(("non_200", dict(client=make(404))))
    cases.append(("empty_text", dict(client=make(200, "<article><body></body></article>"))))
    cases.append(("raises", dict(client=make(httpx.ConnectError("boom")))))
    return ft, cases


def test_europepmc_fulltext_return_stats_all_failure_exits_return_tuple(monkeypatch):
    """强制门禁：True 分支任何一条出口漏出裸 None，生产上就是整批不写终稿。"""
    ft, cases = _epmc_failure_cases(monkeypatch)
    for name, kw in cases:
        got = ft.europepmc_fulltext(doi="10.1/x", return_stats=True, **kw)
        assert got == (None, 0), name
        # 不传 return_stats 时仍是 Optional[str]（现有生产调用方零改动）
        assert ft.europepmc_fulltext(doi="10.1/x", **kw) is None, name


def test_close_read_segment_degrades_on_every_epmc_failure_exit(monkeypatch):
    """四条失败出口下都必须正常降级到 abstract，且不抛异常。"""
    ft, cases = _epmc_failure_cases(monkeypatch)
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)
    for name, kw in cases:
        client = kw["client"]
        monkeypatch.setattr(
            cr, "europepmc_fulltext",
            lambda doi=None, pmid=None, max_chars=40000, return_stats=False,
            _c=client, **rest:
                ft.europepmc_fulltext(doi=doi, pmid=pmid, client=_c,
                                      max_chars=max_chars, return_stats=return_stats))
        llm = _FakeLLM('{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}')
        out = cr.close_read_segment(_seg(doi="10.1/x"), "ri", llm)
        assert out is not None and out.source == "abstract", name
        assert out.from_full_text is False, name


# ---------------- 分块深读（CLOSEREAD_DEEP，默认关） ----------------

_SYNTH_RESP = ('{"one_line":"有用","sections":['
               '{"heading":"研究问题","sentences":[{"text":"q","tag":null}]},'
               '{"heading":"结果与效应量","sentences":[{"text":"AUC=0.91。","tag":"可引用证据"}]},'
               '{"heading":"图表与补充材料要点","sentences":[{"text":"图2","tag":null}]},'
               '{"heading":"逐节通读要点","sentences":[{"text":"节要","tag":null}]}]}')
_SINGLE_RESP = '{"sections":[{"heading":"关键结论","sentences":[{"text":"x","tag":null}]}]}'


class _RoutingLLM:
    """按 prompt 特征分发响应：块通读 / 汇总 / 单跳精读三类各回各的 JSON。"""

    def __init__(self):
        self.calls = []

    def call(self, prompt, model=None, max_tokens=None, temperature=None, json_mode=False):
        self.calls.append(prompt)
        # 顺序要紧：汇总 prompt 里嵌了块笔记 JSON，同样含 "method_details"，
        # 先判块会把汇总也当成块（判错的后果是静默回落单跳，断言变成假绿）
        if prompt.startswith("你在逐块通读"):
            return '{"method_details":["m"],"key_numbers":["AUC=0.91"],"claims":["c"],"limitations":["l"]}'
        if prompt.startswith("你在把一篇论文"):
            return _SYNTH_RESP
        return _SINGLE_RESP

    @property
    def n_chunk_calls(self):
        return sum(1 for p in self.calls if p.startswith("你在逐块通读"))


def _pdf_route(monkeypatch, tmp_path, body):
    """把 close_read_segment 的 PDF 分支接到给定正文上，返回记录 max_chars 的字典。"""
    seen = {}
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": _OA("http://x/y.pdf"))
    monkeypatch.setattr(cr, "download_pdf", lambda url, dest, **kw: tmp_path / "y.pdf")

    def fake_stats(path, max_chars=40000, page_max_chars=None):
        seen["max_chars"] = max_chars
        seen["page_max_chars"] = page_max_chars
        return body, len(body)

    monkeypatch.setattr(cr, "_pdf_text_with_stats", fake_stats)
    return seen


def test_deep_close_read_chunks_and_keeps_auto_source(monkeypatch, tmp_path):
    """deep=True：调用数 == 块数+1，source 不能被 synthesize 的 'manual-pdf' 污染。"""
    body = "正文段落。" * 6000                      # 30000 字符 → chunk_text 切 3 块
    seen = _pdf_route(monkeypatch, tmp_path, body)
    llm = _RoutingLLM()
    out = cr.close_read_segment(_seg(doi="10.1/oa"), "ri", llm, scratch_dir=tmp_path,
                                deep=True, max_chars=120000, max_chunks=12)

    from src.scholar.pdf_ingest import chunk_text
    n = len(chunk_text(body))
    assert n > 1                                    # 夹具前提：确实切成多块
    assert len(llm.calls) == n + 1 and llm.n_chunk_calls == n
    assert out.source == "unpaywall"                # 不是 manual-pdf
    assert out.from_full_text is True
    assert out.reading_depth == "chunked" and out.n_chunks == n
    assert "结果与效应量" in [s.heading for s in out.sections]
    assert out.body_chars == len(body) and out.truncated is False
    # 预算随 deep 放宽，单页 cap 不变
    assert seen["max_chars"] == 120000 and seen["page_max_chars"] == cr.AUTO_PAGE_MAX_CHARS


def test_deep_off_keeps_single_call_and_auto_budget(monkeypatch, tmp_path):
    """deep=False（默认）：仍是一次调用、single-call，预算仍是 AUTO_MAX_CHARS。"""
    body = "正文段落。" * 6000
    seen = _pdf_route(monkeypatch, tmp_path, body)
    llm = _RoutingLLM()
    out = cr.close_read_segment(_seg(doi="10.1/oa"), "ri", llm, scratch_dir=tmp_path,
                                max_chars=120000)   # deep 未开 → max_chars 不生效
    assert len(llm.calls) == 1 and llm.n_chunk_calls == 0
    assert out.reading_depth == "single-call" and out.n_chunks == 1
    assert out.source == "unpaywall" and out.from_full_text is True
    assert seen["max_chars"] == cr.AUTO_MAX_CHARS


def test_deep_falls_back_to_single_call_when_synthesis_fails(monkeypatch, tmp_path):
    """汇总失败必须回落单跳——最差不比现状差，done 计数照旧。"""
    body = "正文段落。" * 6000
    _pdf_route(monkeypatch, tmp_path, body)
    from src.scholar import pdf_ingest as pi
    monkeypatch.setattr(pi, "synthesize_deep_read",
                        lambda notes, llm, model, ri: (None, "", False))
    llm = _RoutingLLM()
    seg = _seg(doi="10.1/oa")
    seg.priority_score = 1.0
    done = cr.close_read_segments([seg], "ri", llm, top_n=1, prefer_full_text=False,
                                  scratch_dir=tmp_path, deep=True, max_chars=120000)
    assert done == 1
    assert seg.close_reading.reading_depth == "single-call"
    assert seg.close_reading.source == "unpaywall"
    assert llm.calls[-1].startswith("你是一名方法学审稿助手")   # 末次确实是单跳精读


def test_deep_never_pollutes_from_full_text_on_abstract_route(monkeypatch):
    """无 OA 无 EPMC 时即使 deep=True 也走单跳：摘要篇不得被记成全文精读。"""
    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)
    monkeypatch.setattr(
        cr, "europepmc_fulltext",
        lambda doi=None, pmid=None, max_chars=40000, return_stats=False, **kw:
            ((None, 0) if return_stats else None))
    llm = _RoutingLLM()
    out = cr.close_read_segment(_seg(doi="10.1/closed"), "ri", llm,
                                deep=True, max_chars=120000)
    assert out.from_full_text is False and out.source == "abstract"
    assert len(llm.calls) == 1 and out.reading_depth == "single-call"


def test_deep_budget_reaches_europepmc_branch(monkeypatch):
    """EPMC 也必须吃到放宽后的预算，否则 deep 模式下 EPMC 正文仍被钉死在 40k。"""
    seen = {}

    def fake_epmc(doi=None, pmid=None, max_chars=40000, return_stats=False, **kw):
        seen["max_chars"] = max_chars
        txt = "全文。" * 6000
        return (txt, len(txt)) if return_stats else txt

    monkeypatch.setattr(cr, "resolve_oa_pdf", lambda meta, email="": None)
    monkeypatch.setattr(cr, "europepmc_fulltext", fake_epmc)
    out = cr.close_read_segment(_seg(doi="10.1/epmc"), "ri", _RoutingLLM(),
                                deep=True, max_chars=120000)
    assert seen["max_chars"] == 120000
    assert out.source == "europepmc" and out.reading_depth == "chunked"

    # 反向对照：deep 关掉时预算必须落回 40k，否则表格解禁后的正文会无声地撑爆单跳预算
    seen.clear()
    out = cr.close_read_segment(_seg(doi="10.1/epmc"), "ri", _RoutingLLM(),
                                deep=False)
    assert seen["max_chars"] == 40000
    assert out.source == "europepmc" and out.reading_depth == "single-call"


def test_prompts_require_experiment_method_section():
    """两条精读 prompt 都必须要「实验方法」节，且明确「原文未报告」的写法。

    加这一节是为了让札记直接给出可复现的实验细节（数据集/划分/超参/评估协议/代码），
    此前这些散落在「方法与数据」里、深浅不一。空值处理沿用全库口径：
    原文没写就显式写出来，不许省略、更不许推测填补。
    """
    from src.scholar.closereading import _CLOSEREAD_PROMPT
    from src.scholar.pdf_ingest import _SYNTH_PROMPT
    for name, p in (("单跳版", _CLOSEREAD_PROMPT), ("深读汇总版", _SYNTH_PROMPT)):
        assert "实验方法" in p, name
        assert "原文未报告" in p, name
    # 单跳版会被「只有摘要」的降级篇用到，硬要页码会诱导编造 —— 必须是软化措辞
    assert "仅在可得文本确有报告时" in _CLOSEREAD_PROMPT
