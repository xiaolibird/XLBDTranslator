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


# ---------------- final bundle 保护（数据安全核心）----------------

def _stub_ingest_env(monkeypatch, tmp_path, title="Manual Paper", authors=("Jane Doe",)):
    """把 ingest_pdf 的外部依赖全部打桩，只留「读磁盘上的 bundle → 决定写不写」这条主线。"""
    _txt = "full text " * 500
    monkeypatch.setattr(pi, "extract_pdf_text", lambda p, **k: _txt)
    # ingest_pdf 走带统计的签名（丢弃 raw_chars 曾让 726 页的书静默丢 43%）
    monkeypatch.setattr(pi, "extract_pdf_text_with_stats", lambda p, **k: (_txt, len(_txt)))
    monkeypatch.setattr(pi, "pdf_page_count", lambda p: 31)
    monkeypatch.setattr(pi, "extract_pdf_ids",
                        lambda p, t="": {"doi": None, "arxiv_id": None, "title": title})
    meta = PaperMetadata(paper_id="pm", title=title, authors=list(authors), doi="10.5/m")
    monkeypatch.setattr(pi, "resolve_metadata",
                        lambda ids, **k: (meta, "crossref-doi"))
    monkeypatch.setattr(pi, "extract_abstract", lambda t, llm: ("en", "zh"))
    monkeypatch.setattr(pi, "deep_read_chunks",
                        lambda *a, **k: [{"_chunk": 1, "claims": ["c"]}])
    monkeypatch.setattr(
        pi, "synthesize_deep_read",
        lambda *a, **k: (CloseReading(from_full_text=True, source="manual-pdf", sections=[
            CloseReadSection(heading="研究问题", sentences=[
                CloseReadSentence(text="脚本草稿。", tag=None)])]), "一句话", False))


def test_ingest_refuses_to_clobber_final_bundle(tmp_path, monkeypatch):
    """回归（数据安全）：同一个 PDF 重跑 ingest，绝不能把 agent 写好的终稿冲成 draft。

    bundle 路径 = paper_id（标题+作者哈希）→ 重跑必然落回同一文件；旧实现无条件 write_bundle，
    close_reading_final 与 cross_check_report 直接归 None，几小时的亲读核验静默蒸发。
    """
    _stub_ingest_env(monkeypatch, tmp_path)
    seg = _make_segment()
    bf = pi.bundle_path(tmp_path, "2026-07", "pm")
    final_cr = seg.close_reading.model_dump(mode="json")
    pi.write_bundle(bf, status="final", month="2026-07", pdf_path="/a.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=final_cr,
                    cross_check_report={"verified_count": 42})
    before = bf.read_bytes()

    r = pi.ingest_pdf(Path("/x.pdf"), tmp_path, "2026-07", llm=None)

    assert r["skipped"] == "final"
    assert bf.read_bytes() == before                     # 一个字节都没动
    data = pi.load_bundle(bf)
    assert data["status"] == "final"
    assert data["close_reading_final"] == final_cr
    assert data["cross_check_report"]["verified_count"] == 42


def test_ingest_final_guard_fires_before_expensive_llm_steps(tmp_path, monkeypatch):
    """拦截点必须在摘要抽取与分块通读之前——21 篇重跑不该白烧一遍 LLM 额度。"""
    _stub_ingest_env(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(pi, "extract_abstract",
                        lambda *a, **k: called.append("abstract") or ("", ""))
    monkeypatch.setattr(pi, "deep_read_chunks",
                        lambda *a, **k: called.append("chunks") or [])
    seg = _make_segment()
    pi.write_bundle(pi.bundle_path(tmp_path, "2026-07", "pm"), status="final",
                    month="2026-07", pdf_path="/a.pdf", metadata_source="crossref-doi",
                    segment=seg, close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"))
    pi.ingest_pdf(Path("/x.pdf"), tmp_path, "2026-07", llm=None)
    assert called == []


def test_ingest_guard_survives_paper_id_drift(tmp_path, monkeypatch):
    """回归：paper_id 变了也不能重读。

    paper_id = md5(标题 + 前三作者)，而元数据每次 ingest 都重新解析——Crossref 超时、
    DOI 抽取粘连、LLM 换个措辞，同一个 PDF 就算出另一个 paper_id、落到另一个文件名。
    只认 paper_id 的话保护形同虚设：不是覆盖旧终稿，而是又白读一遍并留下一个孤儿 draft。
    兜底判据是 PDF 路径。
    """
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    _stub_ingest_env(monkeypatch, tmp_path)
    seg = _make_segment()
    old_bf = pi.bundle_path(tmp_path, "2026-07", "OLD_ID_FROM_LAST_RUN")
    pi.write_bundle(old_bf, status="final", month="2026-07", pdf_path=str(pdf),
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"))

    # 本次解析出的 paper_id 与上次不同（标题措辞变了）
    r = pi.ingest_pdf(pdf, tmp_path, "2026-07", llm=None)

    assert r["skipped"] == "final"
    assert Path(r["bundle"]) == old_bf
    assert not pi.bundle_path(tmp_path, "2026-07", "pm").exists()   # 没留下孤儿 draft


def test_find_final_bundle_ignores_other_pdfs_and_drafts(tmp_path):
    seg = _make_segment()
    other = tmp_path / "other.pdf"
    mine = tmp_path / "mine.pdf"
    for f in (other, mine):
        f.write_bytes(b"%PDF")
    pi.write_bundle(pi.bundle_path(tmp_path, "2026-07", "aaa"), status="final",
                    month="2026-07", pdf_path=str(other), metadata_source="x",
                    segment=seg, close_reading_script=None,
                    close_reading_final={"sections": []})
    pi.write_bundle(pi.bundle_path(tmp_path, "2026-07", "bbb"), status="draft",
                    month="2026-07", pdf_path=str(mine), metadata_source="x",
                    segment=seg, close_reading_script=None)
    assert pi.find_final_bundle(tmp_path, "2026-07", mine, "ccc") is None


def test_ingest_force_overwrites_final(tmp_path, monkeypatch):
    _stub_ingest_env(monkeypatch, tmp_path)
    seg = _make_segment()
    bf = pi.bundle_path(tmp_path, "2026-07", "pm")
    pi.write_bundle(bf, status="final", month="2026-07", pdf_path="/a.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"))
    r = pi.ingest_pdf(Path("/x.pdf"), tmp_path, "2026-07", llm=None, force=True)
    assert r["skipped"] is None
    assert pi.load_bundle(bf)["status"] == "draft"


def test_ingest_overwrites_draft_without_force(tmp_path, monkeypatch):
    """draft 没有需要保护的人工成果，重跑照旧覆盖（否则修草稿要先删文件）。"""
    _stub_ingest_env(monkeypatch, tmp_path)
    seg = _make_segment()
    bf = pi.bundle_path(tmp_path, "2026-07", "pm")
    pi.write_bundle(bf, status="draft", month="2026-07", pdf_path="/a.pdf",
                    metadata_source="pdf-only", segment=seg, close_reading_script=None)
    r = pi.ingest_pdf(Path("/x.pdf"), tmp_path, "2026-07", llm=None)
    assert r["skipped"] is None
    assert pi.load_bundle(bf)["metadata_source"] == "crossref-doi"


# ---------------- 页数 / 亲读窗口 ----------------

def test_read_windows_covers_last_page():
    """回归：末窗必须盖到最后一页——差一页就可能把附录整段判成「脚本编造」。"""
    assert pi.read_windows(31) == [(1, 20), (21, 31)]
    assert pi.read_windows(20) == [(1, 20)]
    assert pi.read_windows(21) == [(1, 20), (21, 21)]
    assert pi.read_windows(1) == [(1, 1)]
    assert pi.read_windows(46) == [(1, 20), (21, 40), (41, 46)]
    assert pi.read_windows(46)[-1][1] == 46


def test_read_windows_unknown_pages_is_empty_not_guessed():
    """页数读不出时返回空——CLI 据此提示「务必自行确认」，而不是编一个 1-20 让人以为读完了。"""
    assert pi.read_windows(None) == [] and pi.read_windows(0) == []


def test_n_pages_written_into_bundle(tmp_path, monkeypatch):
    _stub_ingest_env(monkeypatch, tmp_path)
    r = pi.ingest_pdf(Path("/x.pdf"), tmp_path, "2026-07", llm=None)
    assert r["n_pages"] == 31
    assert pi.load_bundle(Path(r["bundle"]))["n_pages"] == 31


# ---------------- 查重不再静默 ----------------

def test_find_duplicate_hits_by_dedup_key(tmp_path):
    from src.scholar.notes_index import dedup_key_fields
    meta = PaperMetadata(paper_id="pm", title="T", authors=[], doi="10.5/m")
    key = dedup_key_fields("10.5/m", None, "T", fallback="pm")
    ip = tmp_path / "literature_index.json"
    ip.write_text(json.dumps({"papers": [
        {"dedup_key": key, "month": "2026-06", "note_file": "科研札记_2026-06_手动精读.md",
         "citekey": "old2026Key"}]}), encoding="utf-8")
    assert pi.find_duplicate(ip, meta)["citekey"] == "old2026Key"


def test_find_duplicate_ignores_entries_already_marked_duplicate(tmp_path):
    from src.scholar.notes_index import dedup_key_fields
    meta = PaperMetadata(paper_id="pm", title="T", authors=[], doi="10.5/m")
    key = dedup_key_fields("10.5/m", None, "T", fallback="pm")
    ip = tmp_path / "literature_index.json"
    ip.write_text(json.dumps({"papers": [
        {"dedup_key": key, "duplicate_of": "someone", "citekey": "dup"}]}), encoding="utf-8")
    assert pi.find_duplicate(ip, meta) is None


def test_find_duplicate_warns_on_broken_index(tmp_path):
    """回归：索引读坏时必须出声——静默 None 与「确实没重复」不可区分，等于白读一篇。

    日志走 loguru（caplog/capfd 都拿不到它绑定的 sink），所以临时挂一个自己的 sink 来收。
    """
    from loguru import logger as _lg
    lines = []
    sink = _lg.add(lines.append, level="WARNING")
    try:
        ip = tmp_path / "literature_index.json"
        ip.write_text("{not json", encoding="utf-8")
        meta = PaperMetadata(paper_id="pm", title="T", authors=[], doi="10.5/m")
        assert pi.find_duplicate(ip, meta) is None
    finally:
        _lg.remove(sink)
    assert any("查重失败" in s for s in lines)


# ---------------- LLM 标题回查 Crossref（少产 anon* 键）----------------

def test_llm_title_is_retried_against_crossref(monkeypatch):
    """首行启发式标题查不中，但 LLM 抽出的干净标题能中——此时应升级为 crossref-title，
    拿回作者/年份/卷期页，而不是留个空作者的 pdf-llm 条目落成 anon* 键。"""
    import src.scholar.crossref as cr
    monkeypatch.setattr(cr, "crossref_by_doi", lambda *a, **k: None)
    hit = {"title": "Real Clean Title", "authors": ["Jane Doe", "John Roe"],
           "journal": "JAMIA", "doi": "10.9/z", "publication_date": date(2025, 3, 1),
           "volume": "32", "issue": "3", "pages": "e100", "url": None}
    seen = []

    def _lookup(t, **k):
        seen.append(t)
        return hit if t == "Real Clean Title" else None

    monkeypatch.setattr(cr, "crossref_lookup", _lookup)
    llm = _FakeLLM([json.dumps({"title": "Real Clean Title", "authors": ["X Y"],
                                "journal": "J", "year": 2023})])
    meta, source = pi.resolve_metadata(
        {"doi": None, "arxiv_id": None, "title": "PROCEEDINGS OF THE 41ST CONFERENCE"},
        llm=llm, first_pages_text="front page")
    assert source == "crossref-title"
    assert meta.authors == ["Jane Doe", "John Roe"] and meta.pages == "e100"
    assert seen == ["PROCEEDINGS OF THE 41ST CONFERENCE", "Real Clean Title"]


def test_llm_title_retry_does_not_duplicate_query_when_title_unchanged(monkeypatch):
    import src.scholar.crossref as cr
    monkeypatch.setattr(cr, "crossref_by_doi", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(cr, "crossref_lookup",
                        lambda t, **k: seen.append(t) or None)
    llm = _FakeLLM([json.dumps({"title": "Same Title", "authors": ["X Y"], "year": 2023})])
    meta, source = pi.resolve_metadata(
        {"doi": None, "arxiv_id": None, "title": "Same Title"},
        llm=llm, first_pages_text="front page")
    assert source == "pdf-llm" and seen == ["Same Title"]


def test_llm_title_retry_survives_crossref_exception(monkeypatch):
    """回查只是锦上添花——它抛异常不能把整篇 ingest 拖垮。"""
    import src.scholar.crossref as cr
    monkeypatch.setattr(cr, "crossref_by_doi", lambda *a, **k: None)

    def _boom(t, **k):
        if t == "LLM Title":
            raise RuntimeError("network down")
        return None

    monkeypatch.setattr(cr, "crossref_lookup", _boom)
    llm = _FakeLLM([json.dumps({"title": "LLM Title", "authors": ["X Y"], "year": 2023})])
    meta, source = pi.resolve_metadata({"doi": None, "arxiv_id": None, "title": "heuristic"},
                                       llm=llm, first_pages_text="front page")
    assert source == "pdf-llm" and meta.title == "LLM Title"


def test_repo_dedup_overrides_are_isolated_from_this_file():
    """R2-3：本文件的 update_index 调用同样不得读仓库那份真 dedup_overrides.json。

    隔离夹具在 test/conftest.py（对整个 test/ 目录生效）；原先只挂在 test_notes_index.py
    上时，这里与 test_vault.py 会随仓库裁决文件的内容而漂。
    """
    from src.scholar import notes_index as ni
    assert ni._read_override_files(None) == []
    assert not Path(ni.REPO_OVERRIDES_PATH).exists()


# ---------------- R1：跨月守卫 / 403 / 汇总预算 / 原子写 / 摘要 ----------------

def test_final_guard_holds_across_month_buckets(tmp_path):
    """--month 缺省即当月：同一批 PDF 在月边界后重跑会落到另一个桶，同月扫描完全失效。
    磁盘上已因此留下 3 组同 paper_id 跨桶双 final（2 组还把 citekey 分裂成两个键）。"""
    seg = _make_segment()                      # segment.paper_id == "pm"
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    pi.write_bundle(pi.bundle_path(tmp_path, "2026-08", "pm"), status="final",
                    month="2026-08", pdf_path=str(pdf), metadata_source="x",
                    segment=seg, close_reading_script=None,
                    close_reading_final={"sections": []})
    assert pi.find_final_bundle(tmp_path, "2026-08", pdf, "pm") is not None
    assert pi.find_final_bundle(tmp_path, "2026-09", pdf, "pm") is not None
    # paper_id 漂移（元数据重解析出不同哈希）+ 跨月：靠 pdf_path 判据兜住
    assert pi.find_final_bundle(tmp_path, "2026-09", pdf, "pid_drifted") is not None
    # 反过来：PDF 换了文件（重新下载 / 读完后移出待读目录）而 paper_id 相同——
    # 磁盘上那 3 组跨桶双 final 事故的 pdf_path 全不相同，只有 paper_id 判据拦得住。
    moved = tmp_path / "moved" / "paper.pdf"
    moved.parent.mkdir(parents=True, exist_ok=True)
    moved.write_bytes(b"%PDF")
    assert pi.find_final_bundle(tmp_path, "2026-09", moved, "pm") is not None
    # 别的 PDF 不该被误保护
    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF")
    assert pi.find_final_bundle(tmp_path, "2026-09", other, "pid_other") is None


def test_final_guard_covers_bundle_that_forgot_status_flip(tmp_path):
    """写了 close_reading_final 却忘翻 status 的 bundle 也要被保护——否则重跑 ingest
    会把已完成的核验成果当可覆盖的 draft 静默抹掉。"""
    seg = _make_segment()
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF")
    pi.write_bundle(pi.bundle_path(tmp_path, "2026-08", "pidx"), status="draft",
                    month="2026-08", pdf_path=str(pdf), metadata_source="x",
                    segment=seg, close_reading_script=None,
                    close_reading_final={"sections": []})
    assert pi.find_final_bundle(tmp_path, "2026-08", pdf, "pidx") is not None


def test_is_credit_error_covers_403():
    """403 = key 被吊销/组织禁用/区域封锁，与 401/402 同为「重试与换棒都无用」的终局。
    漏认会让 draft_status 落成 empty 而非 api_error，agent 被指去核验一份空草稿。"""
    assert pi.is_credit_error(Exception("API Error: 403 Forbidden"))
    assert pi.is_credit_error(Exception("Client error '403 Forbidden' for url"))
    assert pi.is_credit_error(Exception("permission denied for this organization"))
    assert not pi.is_credit_error(Exception("我读了 4031 页"))     # 整词匹配不回退
    assert not pi.is_credit_error(Exception("500 Internal Server Error"))


def test_pack_chunk_notes_keeps_every_chunk_represented(caplog):
    """裸 [:60000] 会从 JSON 串中间切开，且尾部整块静默消失——而论文尾部正是
    结果/局限/附录，恰是 _SYNTH_PROMPT 里价值最高的三节。"""
    # 条目长度贴近真实块笔记（~60 字符），而不是 200+ ——过长的条目会让 pop 大幅过冲、
    # 留下几千字符头寸，恰好把"预算没扣 JSON 开销就去丢块"这个缺陷遮住。
    notes = [{"method_details": ["m{}-{}".format(i, "x" * 55) for _ in range(12)],
              "key_numbers": ["k{}={}".format(i, "9" * 50) for _ in range(12)],
              "claims": ["c{}-{}".format(i, "y" * 50) for _ in range(12)],
              "limitations": ["l{}-{}".format(i, "z" * 50) for _ in range(12)]}
             for i in range(40)]
    raw = json.dumps(notes, ensure_ascii=False)
    assert len(raw) > pi._SYNTH_NOTES_BUDGET, "构造前提：必须超预算"
    info = {}
    packed = pi._pack_chunk_notes(notes, info=info)
    assert len(packed) <= pi._SYNTH_NOTES_BUDGET
    data = json.loads(packed)              # 必须仍是合法 JSON（裸切片做不到）
    assert len(data) == 40, "每一块都要有代表，不能丢尾块"
    assert info.get("note") and "超汇总预算" in info["note"]
    assert any(n.get("_truncated") for n in data)
    assert info.get("n_dropped") == 0, "均摊裁剪够用时不许丢整块（预算必须先扣 JSON 开销）"


def test_pack_chunk_notes_passthrough_when_within_budget():
    notes = [{"claims": ["a"]}, {"claims": ["b"]}]
    assert pi._pack_chunk_notes(notes) == json.dumps(notes, ensure_ascii=False)


def test_write_bundle_is_atomic(tmp_path, monkeypatch):
    """bundle 是 agent 亲读核验成果的唯一载体，必须与 notes.py 三件套同一套原子写语义。"""
    from src.scholar import notes_index
    calls = []
    real = notes_index._atomic_write
    monkeypatch.setattr(notes_index, "_atomic_write",
                        lambda p, s: calls.append(Path(p).name) or real(p, s))
    seg = _make_segment()
    bf = pi.bundle_path(tmp_path, "2026-08", "pat")
    pi.write_bundle(bf, status="draft", month="2026-08", pdf_path="x.pdf",
                    metadata_source="x", segment=seg, close_reading_script=None)
    assert calls == [bf.name]
    assert json.loads(bf.read_text(encoding="utf-8"))["paper_id"] == seg.paper_id


def test_abstract_keeps_english_when_json_clean_but_no_translation():
    """JSON 完整、模型单纯没给译文时丢掉整条 = 拿完好英文摘要换来「*摘要暂无*」，
    严格少信息（存量实测手动精读 md 里 11 处「摘要暂无」）。"""
    llm = _FakeLLM(['{"abstract_en": "We study missing data.", "abstract_zh": ""}'])
    en, zh = pi.extract_abstract("body text", llm)
    assert en == "We study missing data." and zh == ""


def test_abstract_drops_pair_when_salvaged_and_zh_missing():
    """走过抢救分支时中文为空说明是被削掉的半截，此时整条判失败仍是对的。"""
    llm = _FakeLLM(['{"abstract_en": "We study missing data.", "abstract_zh": "研究'])
    en, zh = pi.extract_abstract("body text", llm)
    assert (en, zh) == ("", "")


def test_chunk_text_never_loops_when_overlap_exceeds_size():
    """overlap >= size 会让 `start = end - overlap` 不前进、while 死循环。

    ⚠️ 不直接调 chunk_text 跑穿：去掉钳位后它是**挂死**而非报错，会把整个套件吊死在 CI
    上（本仓无 pytest-timeout）。改为断言钳位这件事本身：块数必须等于按钳位后步长
    (size-overlap 被钳成 ≥1) 算出来的有限值。"""
    import inspect
    src = inspect.getsource(pi.chunk_text)
    assert "min(overlap, size - 1)" in src, "overlap 钳位不见了，chunk_text 可能死循环"
    # overlap 被钳到 size-1=99 → 步长 1 → 5000-100+1 = 4901 块。数字大但**有限**，
    # 这正是钳位要保证的（不钳位则 start 不前进、永不退出）。
    out = pi.chunk_text("x" * 5000, size=100, overlap=500)
    assert len(out) == 4901 and all(len(c) <= 100 for c in out)


def test_synthesize_writes_truncation_onto_closereading():
    """R3 变异发现:裁剪写进 CloseReading 这件事只有 auto 侧被咬住,manual 侧零保护——
    而 R1/R2 举证的活体样本(229 页/49 块)正是 manual 链路。"""
    payload = {"one_line": "x", "sections": [
        {"heading": "研究问题", "sentences": [{"text": "s", "tag": None}]}]}
    llm = _FakeLLM([json.dumps(payload)])
    notes = [{"claims": ["c" * 400 for _ in range(12)]} for _ in range(40)]
    info = {}
    cr, _one, _err = pi.synthesize_deep_read(notes, llm, "m", "ri", budget_info=info)
    assert info.get("note"), "构造前提：必须触发裁剪"
    assert cr is not None
    assert cr.synth_truncated is True
    assert cr.synth_dropped_chunks == (info.get("n_dropped") or 0)


def test_synthesize_leaves_truncation_unset_when_within_budget():
    """缺失 = 未知，不是 false。没裁过就不许写出 True（同 fulltext_chars 那组口径）。"""
    payload = {"one_line": "x", "sections": [
        {"heading": "研究问题", "sentences": [{"text": "s", "tag": None}]}]}
    cr, _one, _err = pi.synthesize_deep_read(
        [{"claims": ["c"]}], _FakeLLM([json.dumps(payload)]), "m", "ri", budget_info={})
    assert cr.synth_truncated is None and cr.synth_dropped_chunks is None


def test_pack_chunk_notes_drops_from_middle_not_tail():
    """丢块必须从中段起:头部是背景/方法、尾部是结果/局限,两端都比中段贵。"""
    # 不可压缩底座：单个超长**标量**字段，_shrink_note 只能丢列表条目、压不动它，
    # 于是均摊裁剪不够用，必然退到丢块——这才是能测出丢块方位的构造。
    notes = [{"tag": "chunk{}".format(i), "note": "n" * 400, "claims": ["c" * 40]}
             for i in range(30)]
    info = {}
    out = pi._pack_chunk_notes(notes, budget=3000, info=info)
    data = json.loads(out)
    assert info.get("n_dropped", 0) > 0, "构造前提：均摊裁剪不够用，必须丢块"
    tags = [d.get("tag") for d in data]
    assert "chunk0" in tags and "chunk29" in tags, "首尾块必须保住"


def test_shrink_note_drops_round_robin_across_fields():
    """轮转丢弃:四类要点等比例受损,不能先削光某一类让它整类消失。"""
    note = {"method_details": ["m" * 40] * 10, "key_numbers": ["k" * 40] * 10,
            "claims": ["c" * 40] * 10, "limitations": ["l" * 40] * 10}
    out, did = pi._shrink_note(note, 700)
    assert did
    for k in ("method_details", "key_numbers", "claims", "limitations"):
        assert len(out[k]) >= 1, "{} 整类消失了".format(k)
