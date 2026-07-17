# -*- coding: utf-8 -*-
"""手动 PDF 深度精读 ingest 回归：标识符抽取 / 分块 / 元数据权威链 / 汇总解析 / bundle 往返。

网络与 LLM 全部 mock（纯离线单测）。真 PDF 端到端见验证步骤，不在单测里跑。
"""
import json
from datetime import date
from pathlib import Path

import pytest

from src.scholar import pdf_ingest as pi
from src.scholar.schema import (
    PaperMetadata, PaperSegment, FilterDecision, CloseReading,
    CloseReadSection, CloseReadSentence,
)


# ---------------- 标识符正则抽取 ----------------

def test_extract_ids_doi_strips_trailing_punct():
    txt = "Published online. https://doi.org/10.1038/s41746-024-01234-5. Received 2024."
    ids = pi._DOI_RE.search(txt)
    assert ids and pi._clean_doi(ids.group(0)).startswith("10.1038/s41746-024-01234-5")


def test_doi_candidates_offers_deglued_alternative():
    # PDF 抽文常把 DOI 与紧邻正文粘连（无空格）；原串必须排第一（真 DOI 后缀也可含字母）
    assert pi.doi_candidates("10.1177/09622802231165001development") == [
        "10.1177/09622802231165001development", "10.1177/09622802231165001",
    ]


def test_doi_candidates_keeps_letter_suffix_dois_intact():
    # 连字符后的字母是 Nature 系列真 DOI 的一部分，不得剥离
    assert pi.doi_candidates("10.1038/s41746-024-01409-w") == ["10.1038/s41746-024-01409-w"]


def test_resolve_metadata_drops_glued_doi_when_crossref_confirms_neither(monkeypatch):
    monkeypatch.setattr(pi, "_meta_from_crossref", lambda hit: None, raising=False)
    monkeypatch.setattr("src.scholar.crossref.crossref_by_doi",
                        lambda doi, email="", **kw: None)
    monkeypatch.setattr("src.scholar.crossref.crossref_lookup",
                        lambda *a, **kw: None)
    meta, source = pi.resolve_metadata(
        {"doi": "10.1177/0962280223116xyz", "arxiv_id": None, "title": "A Title"},
        llm=None, email="")
    assert meta.doi is None and source == "pdf-only"


def test_resolve_metadata_keeps_clean_doi_when_crossref_unreachable(monkeypatch):
    # 无粘连迹象时查不中多半只是网络不通，DOI 应保留
    monkeypatch.setattr("src.scholar.crossref.crossref_by_doi",
                        lambda doi, email="", **kw: None)
    monkeypatch.setattr("src.scholar.crossref.crossref_lookup",
                        lambda *a, **kw: None)
    meta, _ = pi.resolve_metadata(
        {"doi": "10.1016/j.jbi.2025.104877", "arxiv_id": None, "title": "A Title"},
        llm=None, email="")
    assert meta.doi == "10.1016/j.jbi.2025.104877"


def test_extract_ids_arxiv_needs_context(tmp_path, monkeypatch):
    # 无 fitz 依赖：直接喂 first_pages_text
    txt = "arXiv:2401.12345v2 [cs.LG]\nSome table with value 2020.10000 elsewhere."
    monkeypatch.setattr(pi, "fitz", None, raising=False)
    # extract_pdf_ids 会尝试 import fitz；给个不存在的 path，只走文本正则分支
    ids = pi.extract_pdf_ids(Path("/nonexistent.pdf"), first_pages_text=txt)
    assert ids["arxiv_id"] == "2401.12345v2"


def test_extract_ids_arxiv_ignores_bare_number():
    txt = "Our accuracy reached 2401.12345 units (not an arxiv id)."
    ids = pi.extract_pdf_ids(Path("/nonexistent.pdf"), first_pages_text=txt)
    assert ids["arxiv_id"] is None


# ---------------- 分块 ----------------

def test_chunk_text_overlap_and_coverage():
    text = "".join(chr(65 + (i % 26)) for i in range(30000))
    chunks = pi.chunk_text(text, size=12000, overlap=600)
    assert len(chunks) >= 3
    # 覆盖全文：拼接去重叠后应还原
    assert chunks[0][-600:] == chunks[1][:600]      # 重叠区一致
    assert chunks[-1].endswith(text[-10:])


def test_chunk_text_small_single():
    assert pi.chunk_text("short text") == ["short text"]
    assert pi.chunk_text("   ") == []


# ---------------- 元数据权威链 ----------------

class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, prompt, **kw):
        self.calls.append(prompt)
        return self.responses.pop(0)


def test_resolve_metadata_doi_first(monkeypatch):
    hit = {"title": "Real Title", "authors": ["Jane Doe"], "journal": "NEJM AI",
           "doi": "10.1/x", "publication_date": date(2024, 5, 1),
           "volume": None, "issue": None, "pages": None, "url": None}
    monkeypatch.setattr(pi, "crossref_by_doi", lambda *a, **k: hit, raising=False)
    # 走 lazy import：patch 到 crossref 模块
    import src.scholar.crossref as cr
    monkeypatch.setattr(cr, "crossref_by_doi", lambda *a, **k: hit)
    meta, src = pi.resolve_metadata({"doi": "10.1/x", "arxiv_id": None, "title": "x"})
    assert src == "crossref-doi" and meta.title == "Real Title" and meta.doi == "10.1/x"


def test_resolve_metadata_title_fallback(monkeypatch):
    import src.scholar.crossref as cr
    monkeypatch.setattr(cr, "crossref_by_doi", lambda *a, **k: None)
    hit = {"title": "By Title", "authors": ["A B"], "journal": None, "doi": "10.2/y",
           "publication_date": None, "volume": None, "issue": None, "pages": None, "url": None}
    monkeypatch.setattr(cr, "crossref_lookup", lambda *a, **k: hit)
    meta, src = pi.resolve_metadata({"doi": None, "arxiv_id": None, "title": "By Title"})
    assert src == "crossref-title" and meta.doi == "10.2/y"


def test_resolve_metadata_llm_fallback(monkeypatch):
    import src.scholar.crossref as cr
    monkeypatch.setattr(cr, "crossref_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(cr, "crossref_lookup", lambda *a, **k: None)
    llm = _FakeLLM([json.dumps({"title": "LLM Title", "authors": ["X Y"],
                                "journal": "J", "year": 2023, "abstract": "abs"})])
    meta, src = pi.resolve_metadata({"doi": None, "arxiv_id": None, "title": ""},
                                    llm=llm, first_pages_text="some front page text")
    assert src == "pdf-llm" and meta.title == "LLM Title" and meta.publication_date.year == 2023


def test_resolve_metadata_pdf_only_last_resort(monkeypatch):
    import src.scholar.crossref as cr
    monkeypatch.setattr(cr, "crossref_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(cr, "crossref_lookup", lambda *a, **k: None)
    meta, src = pi.resolve_metadata({"doi": "10.3/z", "arxiv_id": None, "title": "Only PDF"},
                                    llm=None, first_pages_text="")
    assert src == "pdf-only" and meta.title == "Only PDF" and meta.doi == "10.3/z"


# ---------------- 汇总精读解析 ----------------

def test_synthesize_deep_read_parses_sections():
    payload = {"one_line": "对缺失机制建模有借鉴",
               "sections": [
                   {"heading": "方法与数据", "sentences": [
                       {"text": "提出可学习掩码。", "tag": "方法论借鉴"},
                       {"text": "数据来自 MIMIC。", "tag": None}]},
                   {"heading": "结果与效应量", "sentences": [
                       {"text": "AUC 0.87。", "tag": "可引用证据"}]}]}
    llm = _FakeLLM([json.dumps(payload)])
    cr, one_line, api_err = pi.synthesize_deep_read([{"claims": ["c"]}], llm, "m", "研究主线")
    assert one_line == "对缺失机制建模有借鉴" and api_err is False
    assert cr is not None and cr.from_full_text and cr.source == "manual-pdf"
    assert cr.sections[0].sentences[0].tag == "方法论借鉴"


def test_synthesize_deep_read_all_error_returns_none():
    cr, one_line, api_err = pi.synthesize_deep_read([{"_error": True}], _FakeLLM([]), "m", "x")
    assert cr is None and one_line == "" and api_err is False


class _RaisingLLM:
    def __init__(self, exc):
        self.exc = exc

    def call(self, *a, **k):
        raise self.exc


def test_is_credit_error_classification():
    assert pi.is_credit_error(Exception("Client error '402 Payment Required'"))
    assert pi.is_credit_error(Exception("401 Unauthorized"))
    assert pi.is_credit_error(Exception("insufficient balance"))
    assert not pi.is_credit_error(Exception("500 Internal Server Error"))
    assert not pi.is_credit_error(Exception("Unterminated string in JSON"))


def test_synthesize_flags_api_error():
    llm = _RaisingLLM(Exception("Client error '402 Payment Required'"))
    cr, one_line, api_err = pi.synthesize_deep_read([{"claims": ["c"]}], llm, "m", "x")
    assert cr is None and api_err is True


def test_deep_read_chunks_flags_api_error():
    notes = pi.deep_read_chunks(["c1"], _RaisingLLM(Exception("402 Payment Required")), "m", "x")
    assert notes[0]["_error"] is True and notes[0]["_api_error"] is True


def test_deep_read_chunks_survives_bad_json():
    llm = _FakeLLM(["not json at all !!!", json.dumps({"claims": ["ok"]})])
    notes = pi.deep_read_chunks(["c1", "c2"], llm, "m", "interests")
    assert len(notes) == 2
    assert notes[0]["_error"] is True and notes[1]["claims"] == ["ok"]


def test_loads_lenient_salvages_truncated_json():
    # 完整
    assert pi._loads_lenient('{"a": [1, 2, 3]}') == {"a": [1, 2, 3]}
    # 截断在字符串中途 → 回退到上一个完整元素
    assert pi._loads_lenient('{"m": ["one", "two", "thr') == {"m": ["one", "two"]}
    # 嵌套截断
    got = pi._loads_lenient('{"sections":[{"heading":"h","sentences":[{"text":"a","tag":null},{"text":"tr')
    assert got == {"sections": [{"heading": "h", "sentences": [{"text": "a", "tag": None}]}]}
    # 字符串里含 ] 和 } 不误判
    assert pi._loads_lenient('{"s":"has ] and } inside","x":[1,2') == {"s": "has ] and } inside", "x": [1]}
    # 完全非 JSON
    assert pi._loads_lenient("not json") is None


# ---------------- bundle 往返 + finalize 重建 ----------------

def _make_segment():
    fd = FilterDecision(paper_id="pm", title="Manual Paper", verdict="included",
                        decision="INCLUDE", stage="llm_judge", one_line="手动深读文献")
    cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        CloseReadSection(heading="研究问题", sentences=[
            CloseReadSentence(text="研究缺失机制。", tag=None)])])
    return PaperSegment(
        segment_id=1, paper_id="pm", priority_score=1.0,
        metadata=PaperMetadata(paper_id="pm", title="Manual Paper",
                               authors=["Jane Doe"], doi="10.5/m",
                               publication_date=date(2026, 7, 1)),
        filter_decision=fd, close_reading=cr)


def test_bundle_write_load_roundtrip(tmp_path):
    seg = _make_segment()
    bf = pi.bundle_path(tmp_path, "2026-07", seg.paper_id)
    pi.write_bundle(bf, status="draft", month="2026-07", pdf_path="/x.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading)
    assert bf.exists()
    data = pi.load_bundle(bf)
    assert data["status"] == "draft" and data["close_reading_final"] is None
    seg2 = pi.segment_from_bundle(data)
    assert seg2.paper_id == "pm" and seg2.close_reading.sections[0].heading == "研究问题"


def test_segment_from_bundle_applies_one_line_override():
    seg = _make_segment()
    data = {"segment": seg.model_dump(mode="json"),
            "close_reading_script": seg.close_reading.model_dump(mode="json"),
            "close_reading_final": seg.close_reading.model_dump(mode="json"),
            "one_line": "agent 亲读后的一句话用处"}
    seg2 = pi.segment_from_bundle(data)
    assert seg2.filter_decision.one_line == "agent 亲读后的一句话用处"


def test_segment_from_bundle_prefers_final():
    seg = _make_segment()
    final = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        CloseReadSection(heading="终稿节", sentences=[
            CloseReadSentence(text="agent 核验后的句子。", tag="重要发现")])]).model_dump(mode="json")
    data = {"segment": seg.model_dump(mode="json"),
            "close_reading_script": seg.close_reading.model_dump(mode="json"),
            "close_reading_final": final}
    seg2 = pi.segment_from_bundle(data)
    assert seg2.close_reading.sections[0].heading == "终稿节"


def test_finalize_rebuild_month(tmp_path, monkeypatch):
    """draft 不进札记；final 进；同月第二篇追加后 md 含两篇且幂等。"""
    from src.scholar import notes_index as ni
    from src.scholar.notes import write_notes

    notes_dir = tmp_path
    seg = _make_segment()

    # 造一个 fake settings（只用到 processing 的几个字段）
    class _Proc:
        notes_instruction = ""
        notes_emit_docx = False
        notes_docx_cjk_font = ""

    class _Settings:
        processing = _Proc()

    # 手工模拟 read_pdf._rebuild_month 的核心逻辑（避免依赖 scholar.env 配置）
    def rebuild(month):
        bundles = sorted((notes_dir / "manual" / month).glob("*" + pi.BUNDLE_SUFFIX))
        segs = []
        for bf in bundles:
            d = pi.load_bundle(bf)
            if d.get("status") == "final" and d.get("close_reading_final"):
                segs.append(pi.segment_from_bundle(d))
        if not segs:
            return 0
        write_notes(segs, {s.paper_id: None for s in segs}, out_dir=notes_dir,
                    filename="科研札记_{}_手动精读".format(month),
                    digest_title="科研札记 · {}（手动深度精读）".format(month),
                    fallback_citekeys=True, index_series="manual")
        return len(segs)

    # 第一篇：draft → 不进
    bf1 = pi.bundle_path(notes_dir, "2026-07", "pm")
    pi.write_bundle(bf1, status="draft", month="2026-07", pdf_path="/a.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading)
    assert rebuild("2026-07") == 0
    assert not (notes_dir / "科研札记_2026-07_手动精读.md").exists()

    # 标 final → 进
    pi.write_bundle(bf1, status="final", month="2026-07", pdf_path="/a.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"))
    assert rebuild("2026-07") == 1
    md = notes_dir / "科研札记_2026-07_手动精读.md"
    assert md.exists() and "Manual Paper" in md.read_text(encoding="utf-8")

    # 追加第二篇 final → md 含两篇
    seg2 = _make_segment()
    seg2.paper_id = "pn"
    seg2.metadata.paper_id = "pn"
    seg2.metadata.title = "Second Manual Paper"
    seg2.metadata.doi = "10.5/n"
    bf2 = pi.bundle_path(notes_dir, "2026-07", "pn")
    pi.write_bundle(bf2, status="final", month="2026-07", pdf_path="/b.pdf",
                    metadata_source="crossref-doi", segment=seg2,
                    close_reading_script=seg2.close_reading,
                    close_reading_final=seg2.close_reading.model_dump(mode="json"))
    assert rebuild("2026-07") == 2
    text = md.read_text(encoding="utf-8")
    assert "Manual Paper" in text and "Second Manual Paper" in text

    # 索引：两篇都是 manual series
    idx = ni.update_index(notes_dir, full=True)
    manual = [e for e in idx["papers"] if e.get("series") == "manual"]
    assert len(manual) == 2
