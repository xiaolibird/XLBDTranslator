# -*- coding: utf-8 -*-
"""PubMed/arXiv 检索客户端回归测试。

契约：
1. arXiv Atom XML / PubMed efetch XML → 归一化 dict → PaperSegment 映射正确；
2. 检索失败只跳过、不抛异常（cron 无人值守）；
3. 外部来源与邮件路径跨源去重（DOI/paper_id）。

全部离线（喂样例 XML 或 monkeypatch HTTP），不发真实网络请求。
"""
import json
from pathlib import Path

import pytest

from src.scholar.academic_search import (
    AcademicSearchClient,
    item_to_segment,
    fetch_external_papers,
)

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2601.01234v1</id>
    <title>MNAR structure transportability in EHR</title>
    <summary>We study missing not at random across sites.</summary>
    <published>2026-07-01T00:00:00Z</published>
    <author><name>Jane Doe</name></author>
    <author><name>John Roe</name></author>
    <arxiv:doi>10.1000/arxiv.test</arxiv:doi>
  </entry>
</feed>"""

PUBMED_XML = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle>
  <MedlineCitation>
    <PMID>39999999</PMID>
    <Article>
      <ArticleTitle>Transportability of ICU mortality models</ArticleTitle>
      <Abstract>
        <AbstractText Label="BACKGROUND">Distribution shift matters.</AbstractText>
        <AbstractText>External validation across MIMIC and eICU.</AbstractText>
      </Abstract>
      <AuthorList><Author><LastName>Smith</LastName><ForeName>Ann</ForeName></Author></AuthorList>
      <Journal><Title>npj Digital Medicine</Title>
        <JournalIssue><PubDate><Year>2026</Year><Month>Jun</Month><Day>15</Day></PubDate></JournalIssue>
      </Journal>
      <ELocationID EIdType="doi">10.1038/pm.test</ELocationID>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
    <ArticleId IdType="doi">10.1038/pm.test</ArticleId>
  </ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


def test_parse_arxiv_maps_fields():
    items = AcademicSearchClient.parse_arxiv(ARXIV_XML)
    assert len(items) == 1
    it = items[0]
    assert it["source"] == "arxiv"
    assert it["title"] == "MNAR structure transportability in EHR"
    assert it["arxiv_id"] == "2601.01234v1"
    assert it["doi"] == "10.1000/arxiv.test"
    assert it["authors"] == ["Jane Doe", "John Roe"]
    assert it["published"].year == 2026


def test_parse_pubmed_maps_fields():
    items = AcademicSearchClient.parse_pubmed(PUBMED_XML)
    assert len(items) == 1
    it = items[0]
    assert it["source"] == "pubmed"
    assert it["pmid"] == "39999999"
    assert it["doi"] == "10.1038/pm.test"
    assert it["journal"] == "npj Digital Medicine"
    assert "Distribution shift" in it["abstract"]
    assert it["authors"] == ["Ann Smith"]
    assert it["published"].month == 6


def test_item_to_segment_sets_source_type():
    items = AcademicSearchClient.parse_arxiv(ARXIV_XML)
    seg = item_to_segment(items[0], segment_id=7)
    assert seg.segment_id == 7
    assert seg.metadata.source_type == "arxiv"
    assert seg.metadata.arxiv_id == "2601.01234v1"
    assert seg.original_abstract.startswith("We study")
    # paper_id 稳定（title+authors 的 md5），可用于跨源去重
    assert len(seg.paper_id) == 32


def test_fetch_external_papers_continuous_ids(monkeypatch):
    """arXiv + PubMed 结果的 segment_id 从 start 连续分配"""
    monkeypatch.setattr(AcademicSearchClient, "search_arxiv",
                        lambda self, q, max_results=25, days=None, date_range=None: AcademicSearchClient.parse_arxiv(ARXIV_XML))
    monkeypatch.setattr(AcademicSearchClient, "search_pubmed",
                        lambda self, q, max_results=25, days=None, date_range=None: AcademicSearchClient.parse_pubmed(PUBMED_XML))

    segs = fetch_external_papers(
        arxiv_query="q", pubmed_query="q", max_results=5, days=8, start_segment_id=100,
    )
    assert [s.segment_id for s in segs] == [100, 101]
    assert {s.metadata.source_type for s in segs} == {"arxiv", "pubmed"}


def test_fetch_external_papers_source_failure_is_skipped(monkeypatch):
    """单源抛异常只跳过，另一源仍返回，不中断"""
    def boom(self, q, max_results=25, days=None):
        raise RuntimeError("arXiv down")
    monkeypatch.setattr(AcademicSearchClient, "search_arxiv", boom)
    monkeypatch.setattr(AcademicSearchClient, "search_pubmed",
                        lambda self, q, max_results=25, days=None, date_range=None: AcademicSearchClient.parse_pubmed(PUBMED_XML))

    segs = fetch_external_papers(
        arxiv_query="q", pubmed_query="q", max_results=5, days=None, start_segment_id=0,
    )
    assert len(segs) == 1
    assert segs[0].metadata.source_type == "pubmed"


def test_external_sources_cross_source_dedup(tmp_path, monkeypatch):
    """外部来源与已有 segment 跨源去重（相同 DOI 不重复入库）"""
    from src.scholar.schema import ScholarSettings, PaperMetadata, PaperSegment
    from src.scholar.workflow import ScholarWorkflow

    env = tmp_path / "s.env"
    env.write_text(
        "GMAIL__CREDENTIALS_PATH=fake/c.json\nGMAIL__TOKEN_PATH=fake/t.json\n"
        "LLM__PROVIDER=gemini\nLLM__GEMINI_API_KEY=FAKE\nLLM__MODEL=fake\n",
        encoding="utf-8",
    )
    settings = ScholarSettings.from_env_file(env)
    settings.processing.output_dir = tmp_path / "out"
    settings.processing.external_sources_enabled = True
    settings.processing.blacklist = []
    wf = ScholarWorkflow(settings)

    # 预置一篇已入库、DOI 与 PubMed 结果相同的论文
    existing = PaperSegment(
        segment_id=0, paper_id="existing",
        metadata=PaperMetadata(paper_id="existing", title="dup", doi="10.1038/pm.test"),
    )
    wf.segments = [existing]
    wf._seen_dois.add("10.1038/pm.test")
    wf._seen_paper_ids.add("existing")

    monkeypatch.setattr("src.scholar.academic_search.fetch_external_papers",
                        lambda **kw: AcademicSearchClient_pubmed_segments(kw["start_segment_id"]))
    wf._step_fetch_external_sources()

    # PubMed 那篇 DOI 重复 → 被去重，不新增
    assert len(wf.segments) == 1


def AcademicSearchClient_pubmed_segments(start_id):
    items = AcademicSearchClient.parse_pubmed(PUBMED_XML)
    return [item_to_segment(items[0], start_id)]


# ==================== arXiv 精确补全（enrich_from_arxiv） ====================

def test_fetch_arxiv_by_id_and_enrich(monkeypatch):
    """按 arxiv_id 精确查 → 覆盖脏作者（arxiv_id 精确，无误配风险）。"""
    import httpx
    from src.scholar.academic_search import fetch_arxiv_by_id, enrich_from_arxiv
    from src.scholar.schema import PaperMetadata

    captured = {}

    def handler(request):
        captured["id_list"] = dict(request.url.params).get("id_list")
        return httpx.Response(200, text=ARXIV_XML)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    # 带版本后缀应被剥离
    it = fetch_arxiv_by_id("2601.01234v3", client)
    assert captured["id_list"] == "2601.01234"
    assert it["authors"] == ["Jane Doe", "John Roe"]

    meta = PaperMetadata(paper_id="p", title="x",
                         authors=["structure transportability in EHRJ Doe"], arxiv_id="2601.01234", doi=None)
    ok = enrich_from_arxiv(meta, client)
    assert ok is True
    assert meta.authors == ["Jane Doe", "John Roe"]  # 脏作者被 arXiv 权威版覆盖
    assert meta.doi == "10.1000/arxiv.test"


def test_enrich_from_arxiv_no_id_returns_false():
    import httpx
    from src.scholar.academic_search import enrich_from_arxiv
    from src.scholar.schema import PaperMetadata
    meta = PaperMetadata(paper_id="p", title="x", authors=["A"], arxiv_id=None)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ARXIV_XML)))
    assert enrich_from_arxiv(meta, client) is False
