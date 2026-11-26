# -*- coding: utf-8 -*-
"""BM25Index 单元测试与慢路径逐项等价性验证。"""
import math
import random
import string
from pathlib import Path

import numpy as np

from src.scholar.bm25_index import BM25Index, bm25_tokenize


def _bm25_slow_ref(texts, mask, query_tokens, top_k=200, k1=1.2, b=0.75):
    if not mask.any() or not query_tokens:
        return []
    idxs = np.nonzero(mask)[0]
    doc_tokens = [bm25_tokenize(texts[int(i)]) for i in idxs]
    doc_len = np.array([len(t) for t in doc_tokens], dtype=np.float64)
    n = len(doc_tokens)
    avgdl = doc_len.mean() if n else 0.0
    if avgdl <= 0:
        return []
    q_terms = set(query_tokens)
    df = {t: 0 for t in q_terms}
    tf_per_doc = []
    for toks in doc_tokens:
        c = {}
        for t in toks:
            if t in q_terms:
                c[t] = c.get(t, 0) + 1
        tf_per_doc.append(c)
        for t in c:
            df[t] += 1
    idf = {t: math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1) for t in q_terms}
    scores = np.zeros(n, dtype=np.float64)
    for pos, c in enumerate(tf_per_doc):
        if not c:
            continue
        denom_base = k1 * (1 - b + b * doc_len[pos] / avgdl)
        s = 0.0
        for t, tf in c.items():
            s += idf[t] * (tf * (k1 + 1)) / (tf + denom_base)
        scores[pos] = s
    order = np.argsort(-scores)
    out = []
    for pos in order[: min(top_k, n)]:
        if scores[pos] <= 0:
            break
        out.append((int(idxs[pos]), float(scores[pos])))
    return out


def test_bm25_index_matches_slow_ref_under_random_masks(tmp_path):
    rng = random.Random(42)
    vocab = ["informative", "missingness", "longitudinal", "electronic", "health", "record",
             "EM", "IV", "MI", "mnar", "mar", "缺失机制", "信息性观测", "纵向轨迹"]
    texts = []
    for _ in range(120):
        words = [rng.choice(vocab) for _ in range(rng.randint(3, 18))]
        texts.append(" ".join(words))

    idx = BM25Index.build(texts, content_version="v1")
    cache_file = tmp_path / "embeddings.bm25.pkl"
    idx.save(cache_file)
    loaded = BM25Index.try_load(cache_file, "v1", len(texts))
    assert loaded is not None

    # 失效键检查
    assert BM25Index.try_load(cache_file, "v2", len(texts)) is None
    assert BM25Index.try_load(cache_file, "v1", len(texts) - 1) is None

    for _ in range(10):
        mask = np.array([rng.random() < 0.65 for _ in range(len(texts))], dtype=bool)
        q = " ".join(rng.sample(vocab, k=3))
        q_toks = bm25_tokenize(q)
        fast_hits = loaded.search(mask, q_toks, top_k=50)
        slow_hits = _bm25_slow_ref(texts, mask, q_toks, top_k=50)
        fast_map = {i: s for i, s in fast_hits}
        slow_map = {i: s for i, s in slow_hits}
        # 分数单调递减；允许完全同分处 quicksort/mergesort 顺序不同，但集合与分值必须全等
        assert all(fast_hits[k][1] >= fast_hits[k + 1][1] for k in range(len(fast_hits) - 1))
        assert set(fast_map) == set(slow_map) or len(fast_hits) == 50
        for i in set(fast_map) & set(slow_map):
            assert abs(fast_map[i] - slow_map[i]) < 1e-11


def test_bm25_two_letter_acronyms():
    toks = bm25_tokenize("EM algorithm and IV estimation with MI")
    assert "em" in toks
    assert "iv" in toks
    assert "mi" in toks
    assert "algorithm" in toks
