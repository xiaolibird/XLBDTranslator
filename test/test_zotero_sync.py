# -*- coding: utf-8 -*-
"""Zotero 联动（B1）回归测试：item 映射 / citekey 解析 / OA 解析 / 札记生成 / 编排。

全程 mock，不发真实网络请求、不碰真实 Zotero。
"""
import json
from datetime import date

import httpx
import pytest

from src.scholar.schema import PaperMetadata, PaperSegment, FilterDecision, DigestStatus
from src.scholar import zotero_sync, fulltext, notes


# ---------------- 作者名切分 ----------------

def test_split_creator_name_variants():
    assert zotero_sync.split_creator_name("John A. Smith") == {
        "creatorType": "author", "firstName": "John A.", "lastName": "Smith"}
    assert zotero_sync.split_creator_name("Smith, John") == {
        "creatorType": "author", "firstName": "John", "lastName": "Smith"}
    assert zotero_sync.split_creator_name("Cher") == {"creatorType": "author", "lastName": "Cher"}
    # 空名退回单字段，不猜姓氏
    assert "name" in zotero_sync.split_creator_name("")


# ---------------- item 映射 ----------------

def _meta(**kw):
    base = dict(paper_id="pid1", title="A Study of MNAR in EHR", authors=["Jane Q. Public", "Li Ming"])
    base.update(kw)
    return PaperMetadata(**base)


def test_paper_to_zotero_item_journal():
    m = _meta(doi="10.1/abc", journal="npj Digital Medicine", publication_date=date(2025, 3, 1),
              pmid="12345", source_type="pubmed")
    it = zotero_sync.paper_to_zotero_item(m, abstract="abs text")
    assert it["itemType"] == "journalArticle"
    assert it["DOI"] == "10.1/abc"
    assert it["publicationTitle"] == "npj Digital Medicine"
    assert it["abstractNote"] == "abs text"
    assert it["date"] == "2025-03-01"
    assert "PMID: 12345" in it["extra"]
    assert {"tag": "source:pubmed"} in it["tags"]


def test_paper_to_zotero_item_arxiv_preprint():
    m = _meta(doi=None, arxiv_id="2401.12345v1", journal="arXiv", source_type="arxiv")
    it = zotero_sync.paper_to_zotero_item(m)
    assert it["itemType"] == "preprint"
    assert it["repository"] == "arXiv"
    assert it["archiveID"] == "arXiv:2401.12345v1"
    assert "DOI" not in it


def test_paper_to_zotero_item_oa_attachment():
    m = _meta(doi="10.1/abc")
    oa = fulltext.OAResult(oa_status="gold", pdf_url="https://x/y.pdf", source="unpaywall")
    it = zotero_sync.paper_to_zotero_item(m, oa=oa)
    assert it["attachments"][0]["url"] == "https://x/y.pdf"
    assert it["attachments"][0]["mimeType"] == "application/pdf"


def test_decision_tags_from_filter_decision():
    seg = PaperSegment(
        segment_id=1, paper_id="pid1", metadata=_meta(),
        filter_decision=FilterDecision(
            paper_id="pid1", title="t", verdict="included", decision="INCLUDE",
            stage="llm_judge", bucket=["A", "C"], flags=["THREAT"], role="MUST_ENGAGE"),
    )
    tags = zotero_sync.decision_tags(seg)
    assert "decision:INCLUDE" in tags
    assert "bucket:A" in tags and "bucket:C" in tags
    assert "flag:THREAT" in tags
    assert "role:MUST_ENGAGE" in tags
    # 无裁决时空列表
    assert zotero_sync.decision_tags(PaperSegment(segment_id=2, paper_id="p2", metadata=_meta())) == []


# ---------------- citekey 挑选 ----------------

def test_pick_citekey_prefers_doi():
    results = [
        {"DOI": "10.9/other", "title": "Other", "citation-key": "wrong2020"},
        {"DOI": "10.1/ABC", "title": "A Study of MNAR in EHR", "citation-key": "public2025mnar"},
    ]
    assert zotero_sync.pick_citekey(results, "10.1/abc", "A Study of MNAR in EHR") == "public2025mnar"


def test_pick_citekey_title_fallback():
    results = [{"title": "A Study of MNAR in EHR", "citation-key": "public2025mnar"}]
    assert zotero_sync.pick_citekey(results, "", "A Study of MNAR in EHR") == "public2025mnar"


def test_pick_citekey_unique_fallback_and_none():
    assert zotero_sync.pick_citekey([{"citation-key": "solo2024"}], "", None) == "solo2024"
    assert zotero_sync.pick_citekey([], "10.1/x", "t") is None
    # 多结果无匹配 → None（不乱猜）
    two = [{"DOI": "10.a/1", "citation-key": "a"}, {"DOI": "10.b/2", "citation-key": "b"}]
    assert zotero_sync.pick_citekey(two, "10.c/3", "no-match") is None


# ---------------- OA 解析 ----------------

def test_resolve_oa_arxiv_no_network():
    m = _meta(doi=None, arxiv_id="2401.12345v1")
    oa = fulltext.resolve_oa_pdf(m, email="x@y.com")  # arXiv 不走网络
    assert oa.oa_status == "arxiv"
    assert oa.pdf_url == "https://arxiv.org/pdf/2401.12345v1.pdf"


def test_parse_unpaywall_oa_and_closed():
    oa = fulltext.parse_unpaywall({
        "is_oa": True, "oa_status": "green",
        "best_oa_location": {"url_for_pdf": "https://repo/x.pdf", "url": "https://repo/x", "host_type": "repository"},
    })
    assert oa.is_oa and oa.pdf_url == "https://repo/x.pdf" and oa.oa_status == "green"
    closed = fulltext.parse_unpaywall({"is_oa": False, "oa_status": "closed"})
    assert not closed.is_oa and closed.oa_status == "closed"


def test_resolve_oa_unpaywall_via_mock_client():
    def handler(request):
        assert "api.unpaywall.org" in str(request.url)
        assert "email=" in str(request.url)
        return httpx.Response(200, json={
            "is_oa": True, "oa_status": "hybrid",
            "best_oa_location": {"url_for_pdf": "https://p/f.pdf", "url": "https://p"},
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    m = _meta(doi="10.1/abc", arxiv_id=None)
    oa = fulltext.resolve_oa_pdf(m, email="x@y.com", client=client)
    assert oa.pdf_url == "https://p/f.pdf"


def test_resolve_oa_no_doi_no_arxiv_is_unknown():
    m = _meta(doi=None, arxiv_id=None)
    assert fulltext.resolve_oa_pdf(m, email="x@y.com").pdf_url is None


# ---------------- 连接器客户端（MockTransport） ----------------

def _connector_client(handler):
    c = zotero_sync.ZoteroConnectorClient()
    c._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=c._client.headers,
    )
    return c


def test_connector_ping_and_save_and_resolve():
    state = {"saved": None}

    def handler(request):
        path = request.url.path
        if path == "/connector/ping":
            return httpx.Response(200, json={"prefs": {}})
        if path == "/connector/saveItems":
            state["saved"] = json.loads(request.content)
            return httpx.Response(201)
        if path == "/better-bibtex/json-rpc":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": [
                {"DOI": "10.1/abc", "title": "A Study of MNAR in EHR", "citation-key": "public2025mnar"}]})
        return httpx.Response(404)

    c = _connector_client(handler)
    assert c.ping() is True
    assert c.save_items([{"itemType": "journalArticle", "title": "t"}]) is True
    assert state["saved"]["items"][0]["title"] == "t"
    ck = c.resolve_citekey(doi="10.1/abc", title="A Study of MNAR in EHR", retries=1, delay=0)
    assert ck == "public2025mnar"
    c.close()


def test_resolve_citekey_recovers_via_title_fragment():
    """BBT item.search 对 DOI 和长标题常 0 命中，只有短前缀片段命中；
    resolver 必须补发短片段并在结果里按 DOI 精确挑（回归 2026-06 的 5/12 漏解析）。"""
    full = "Early Sepsis Detection Using Heterogeneous Sensor Data: A ML Approach"
    seen = []

    def handler(request):
        if request.url.path != "/better-bibtex/json-rpc":
            return httpx.Response(404)
        term = json.loads(request.content)["params"][0]
        seen.append(term)
        # 只有短片段（<=4 词）才返回命中，模拟 BBT 长短语/DOI 0 命中
        if len(term.split()) <= 4 and term.lower() in full.lower():
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": [
                {"DOI": "10.3390/s26123648", "title": full, "citation-key": "tap2026Early"}]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": []})

    c = _connector_client(handler)
    ck = c.resolve_citekey(doi="10.3390/s26123648", title=full, retries=1, delay=0)
    assert ck == "tap2026Early"
    # 确认确实先试了 DOI/全标题（0 命中），再补了短片段
    assert "10.3390/s26123648" in seen and full in seen
    assert any(len(t.split()) <= 4 for t in seen)
    c.close()


def test_connector_ping_false_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")
    c = _connector_client(handler)
    assert c.ping() is False
    c.close()


# ---------------- 编排：sync_segments_to_zotero（注入 fake client） ----------------

class _FakeClient:
    def __init__(self, up=True, citekey="public2025mnar", coll_name="兴趣"):
        self.up = up
        self.citekey = citekey
        self.coll_name = coll_name
        self.saved_items = None

    def ping(self):
        return self.up

    def get_selected_collection(self):
        return {"libraryName": "我的文库", "name": self.coll_name}

    def save_items(self, items, uri=""):
        self.saved_items = items
        return True

    def resolve_citekey(self, doi=None, title=None, retries=6, delay=0.8):
        return self.citekey

    def close(self):
        pass


def test_sync_segments_orchestration(monkeypatch):
    # OA 解析不走网络：arXiv 直链即可
    seg = PaperSegment(
        segment_id=1, paper_id="pidZ",
        metadata=_meta(doi=None, arxiv_id="2401.99999", journal="arXiv", source_type="arxiv"),
        original_abstract="abs", status=DigestStatus.PENDING,
    )
    fake = _FakeClient(up=True, citekey="ming2024arxiv")
    results = zotero_sync.sync_segments_to_zotero([seg], client=fake, email="x@y.com",
                                                  enrich_crossref=False)
    assert results["pidZ"].saved is True
    assert results["pidZ"].citekey == "ming2024arxiv"
    assert results["pidZ"].oa_status == "arxiv"
    # 传给 Zotero 的 item 带 OA 附件
    assert fake.saved_items[0]["attachments"][0]["url"].endswith("2401.99999.pdf")


def test_sync_require_collection_match_writes(monkeypatch):
    seg = PaperSegment(segment_id=1, paper_id="pidZ",
                       metadata=_meta(doi=None, arxiv_id="2401.99999", source_type="arxiv"),
                       status=DigestStatus.PENDING)
    fake = _FakeClient(coll_name="P4")
    results = zotero_sync.sync_segments_to_zotero([seg], client=fake, email="x@y.com",
                                                  enrich_crossref=False, require_collection="P4")
    assert fake.saved_items is not None  # 分类匹配 → 写入
    assert results["pidZ"].saved is True


def test_sync_require_collection_mismatch_aborts(monkeypatch):
    seg = PaperSegment(segment_id=1, paper_id="pidZ",
                       metadata=_meta(doi=None, arxiv_id="2401.99999", source_type="arxiv"),
                       status=DigestStatus.PENDING)
    fake = _FakeClient(coll_name="我的文库")  # 选中库根，与要求的 P4 不符
    results = zotero_sync.sync_segments_to_zotero([seg], client=fake, email="x@y.com",
                                                  enrich_crossref=False, require_collection="P4")
    assert fake.saved_items is None       # 拒绝写入
    assert results["pidZ"].saved is False


def test_sync_segments_zotero_down_returns_empty(monkeypatch):
    seg = PaperSegment(segment_id=1, paper_id="pidZ", metadata=_meta(), status=DigestStatus.PENDING)
    fake = _FakeClient(up=False)
    results = zotero_sync.sync_segments_to_zotero([seg], client=fake)
    assert results["pidZ"].saved is False
    assert results["pidZ"].citekey is None


# ---------------- 札记生成 ----------------

def _seg_with_decision():
    return PaperSegment(
        segment_id=1, paper_id="pid1",
        metadata=_meta(doi="10.1/abc", journal="npj Digital Medicine"),
        original_abstract="English abstract", translated_abstract="中文摘要",
        summary="AI 总结要点", filter_decision=FilterDecision(
            paper_id="pid1", title="t", verdict="included", decision="INCLUDE",
            stage="llm_judge", bucket=["A"], flags=["THREAT"], role="MUST_ENGAGE",
            one_line="缺失机制对照证据", confidence=0.82),
    )


def test_build_note_has_pandoc_citation():
    note = notes.build_note(_seg_with_decision(), "public2025mnar", instruction="按方法学归纳")
    assert "[@public2025mnar]" in note
    assert "citekey: \"public2025mnar\"" in note
    assert "INCLUDE" in note and "THREAT" in note
    assert "中文摘要" in note  # 优先译文
    assert "AI 总结要点" in note
    assert "按方法学归纳" in note


def test_build_note_missing_citekey_uses_placeholder():
    note = notes.build_note(_seg_with_decision(), None)
    assert "MISSING-KEY-" in note  # 占位键，导入后可重跑 resolve


def test_build_csl_item():
    csl = notes.build_csl_item(_meta(doi="10.1/abc", journal="npj Digital Medicine",
                                     publication_date=date(2025, 3, 1)), "public2025mnar")
    assert csl["id"] == "public2025mnar"
    assert csl["DOI"] == "10.1/abc"
    assert csl["issued"]["date-parts"] == [[2025, 3, 1]]
    assert csl["author"][0]["family"] == "Public"


def _seg_with_decision_2():
    return PaperSegment(
        segment_id=2, paper_id="pid2",
        metadata=_meta(paper_id="pid2", title="Second Paper on Missingness",
                       doi="10.2/xyz", authors=["Li Ming"]),
        original_abstract="second abstract", summary="第二篇总结",
        filter_decision=FilterDecision(paper_id="pid2", title="t2", verdict="included",
                                       decision="MAYBE", stage="llm_judge", bucket=["B"]),
    )


def test_write_notes_aggregates_into_single_file(tmp_path):
    """一个时间窗的多篇论文聚合进【一个】md 文件（不是一篇一个文件）。"""
    segs = [_seg_with_decision(), _seg_with_decision_2()]
    citekeys = {"pid1": "public2025mnar", "pid2": "ming2025missing"}
    summary = notes.write_notes(segs, citekeys, out_dir=tmp_path,
                                digest_title="周报 2026-07", filename="digest_test")
    # 只有一个聚合 md
    mds = list(tmp_path.glob("*.md"))
    assert len(mds) == 1
    assert mds[0].name == "digest_test.md"
    text = mds[0].read_text(encoding="utf-8")
    # 两篇都在同一文档，各带引用
    assert "[@public2025mnar]" in text and "[@ming2025missing]" in text
    assert "1. " in text and "2. " in text        # 分节序号（带优先级着色前缀）
    assert "优先级速览" in text                     # 顶部速览表
    assert "🔴" in text or "🟠" in text or "🟢" in text  # 分级着色标记
    assert "周报 2026-07" in text
    assert "::: {#refs}" in text          # 末尾统一参考文献占位
    assert summary["csl_count"] == 2
    assert summary["note_path"].endswith("digest_test.md")


def test_write_notes_missing_citekey_placeholder(tmp_path):
    """未入库（Zotero 无 citekey）→ 正文用占位键、不进 references.json（以 Zotero 为权威源）。"""
    segs = [_seg_with_decision()]
    summary = notes.write_notes(segs, {"pid1": None}, out_dir=tmp_path)
    assert summary["missing_citekey"] == 1
    assert summary["csl_count"] == 0
    text = list(tmp_path.glob("*.md"))[0].read_text(encoding="utf-8")
    assert "MISSING-KEY-" in text


def test_write_notes_counts_missing_citekey(tmp_path):
    segs = [_seg_with_decision()]
    summary = notes.write_notes(segs, {"pid1": None}, out_dir=tmp_path)
    assert summary["missing_citekey"] == 1
    assert summary["csl_count"] == 0  # 无 key 不进 CSL
