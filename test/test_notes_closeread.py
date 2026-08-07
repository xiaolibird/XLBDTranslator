# -*- coding: utf-8 -*-
"""markdown 札记必须渲染全文精读（句级三色标记），而不是只显示摘要翻译。

回归此前缺口：_paper_section 曾只输出「摘要 + AI 归纳」，close_reading 只进 docx、不进 md，
导致 md 里的"AI 总结"与摘要翻译无异、看不到对正文的 track/归纳/标注。
"""
from src.scholar.schema import (
    PaperSegment, PaperMetadata,
    CloseReading, CloseReadSection, CloseReadSentence,
)
from src.scholar.notes import build_digest_note, _paper_section


def _seg_with_closeread():
    cr = CloseReading(
        from_full_text=True, source="arxiv",
        sections=[
            CloseReadSection(heading="方法与数据", sentences=[
                CloseReadSentence(text="用可学习掩码嵌入建模缺失。", tag="方法学创新"),
                CloseReadSentence(text="数据来自 EHRSHOT。", tag=None)]),
            CloseReadSection(heading="关键结论", sentences=[
                CloseReadSentence(text="AUPRC 0.4434 优于基线 0.4173。", tag="重要发现")]),
        ])
    return PaperSegment(
        segment_id=1, paper_id="p1", priority_score=0.9,
        translated_abstract="这是摘要翻译，只讲大意。",
        summary="这是摘要级 AI 归纳。",
        metadata=PaperMetadata(paper_id="p1", title="Full-Text Paper", doi="10.1/p1"),
        close_reading=cr)


def test_markdown_renders_closeread_sections_and_tags():
    seg = _seg_with_closeread()
    md = "\n".join(_paper_section(seg, "x2026", index=1, level=2))
    # 全文精读小节标题 + 来源
    assert "全文精读" in md and "arxiv" in md
    # 分节标题
    assert "【方法与数据】" in md and "【关键结论】" in md
    # 句级三色标记以〔tag〕形式出现
    assert "〔方法学创新〕" in md and "〔重要发现〕" in md
    # 全文精读特有内容（非摘要）出现
    assert "AUPRC 0.4434" in md
    # 有精读时不再退回摘要级 AI 归纳
    assert "AI 归纳" not in md


def test_markdown_falls_back_to_summary_without_closeread():
    seg = _seg_with_closeread()
    seg.close_reading = None
    md = "\n".join(_paper_section(seg, "x2026", index=1, level=2))
    assert "全文精读" not in md
    assert "AI 归纳" in md and "这是摘要级 AI 归纳。" in md


def test_fallback_citekeys_replace_missing(tmp_path):
    """headless 回填：无 Zotero key 时用人读临时键（作者+年+词），不出现 MISSING-KEY，且写入 CSL。"""
    from datetime import date
    from src.scholar.notes import write_notes
    from src.scholar._citekey_utils import _fallback_citekey
    seg = PaperSegment(
        segment_id=1, paper_id="p1", priority_score=0.5,
        metadata=PaperMetadata(paper_id="p1", title="Missing Data Mechanisms in EHR",
                               authors=["Jane Public"], doi="10.9/x",
                               publication_date=date(2025, 3, 1)))
    assert _fallback_citekey(seg.metadata) == "public2025Missing"
    res = write_notes([seg], {"p1": None}, out_dir=tmp_path,
                      filename="m", fallback_citekeys=True)
    md = (tmp_path / "m.md").read_text(encoding="utf-8")
    assert "[@public2025Missing]" in md and "MISSING-KEY" not in md
    assert res["csl_count"] == 1  # 临时键也进 references，pandoc 可渲染


def test_no_fallback_keeps_missing_placeholder(tmp_path):
    """默认（Zotero 权威模式）不启用兜底键：保持占位，不污染 Zotero 依赖流程。"""
    from src.scholar.notes import write_notes
    seg = PaperSegment(segment_id=1, paper_id="p1",
                       metadata=PaperMetadata(paper_id="p1", title="T", authors=["A B"]))
    write_notes([seg], {"p1": None}, out_dir=tmp_path, filename="m")
    md = (tmp_path / "m.md").read_text(encoding="utf-8")
    assert "MISSING-KEY" in md


def test_digest_note_has_tag_legend():
    seg = _seg_with_closeread()
    note = build_digest_note([seg], {"p1": "x2026"}, title="T")
    # 顶部图例说明三色标记
    assert "〔方法学创新〕" in note and "〔重要发现〕" in note and "〔研究背景〕" in note
