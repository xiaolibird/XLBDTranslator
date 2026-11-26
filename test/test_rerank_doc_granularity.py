# -*- coding: utf-8 -*-
"""Phase 1 粒度感知重排文档构造单元测试。"""
from types import SimpleNamespace

from scripts.notes_search import _rerank_doc


def _sample_inputs():
    abstract_texts = {"k1": "Paper K1 Title\n一句话\nFull English Abstract about MNAR."}
    paper_meta = {
        "k1": {"text": "Paper K1 Title\n一句话", "series": "auto"},
        "b1": {"text": "Textbook B1\n整本统计书", "series": "book"},
    }
    row_k1 = {
        "citekey": "k1",
        "hits": [
            (0.75, "cosine", 0.03, {"text": "句级证据A：逆概率加权在MNAR下有偏。"}),
            (0.70, "cosine", 0.02, {"text": "句级证据B：需配合敏感性分析。"}),
        ],
    }
    row_b1 = {
        "citekey": "b1",
        "hits": [
            (0.72, "cosine", 0.03, {"text": "第15章讨论模式混合模型与选择模型。"}),
        ],
    }
    return abstract_texts, paper_meta, row_k1, row_b1


def test_default_mode_preserves_legacy_abstract_doc():
    ab, pm, r1, _ = _sample_inputs()
    args = SimpleNamespace(level="auto", role=None, book=None)
    assert _rerank_doc(r1, args, ab, pm) == ab["k1"]


def test_evidence_modes_switch_to_hit_sentences():
    ab, pm, r1, r_book = _sample_inputs()
    for args in (
        SimpleNamespace(level="highlight", role=None, book=None),
        SimpleNamespace(level="auto", role="citable", book=None),
        SimpleNamespace(level="auto", role=None, book="b1"),
    ):
        doc = _rerank_doc(r1, args, ab, pm)
        assert "句级证据A" in doc and "句级证据B" in doc
        assert "Full English Abstract" not in doc

    # 无摘要书籍即使在默认 auto 下也用命中句，避免只靠书名打分
    doc_b = _rerank_doc(r_book, SimpleNamespace(level="auto", role=None, book=None), ab, pm)
    assert "第15章讨论模式混合模型" in doc_b
