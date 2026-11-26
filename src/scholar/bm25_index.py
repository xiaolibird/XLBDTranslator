# -*- coding: utf-8 -*-
"""持久化 BM25 倒排索引（notes_search.py 稀疏/混合检索的零损加速层）。

设计原则：
- 与 `embeddings.sqlite3` 同目录旁挂 `embeddings.bm25.pkl`，以 `meta.content_version`
  + 文档数 `n` 作为唯一失效键；
- 分词器 `bm25_tokenize` 与 `vault.tokenize` 保持严格超集契约（补回独立两字母缩写）；
- 在任意布尔 `mask` 上精确按掩码子集计算 `n`、`avgdl` 与 `df/idf`，保证与
  `_bm25_search_slow` 在数学定义上逐项等价（仅可能存在 float64 求和顺序带来的
  < 1e-12 浮点舍入差），并列按 doc 下标稳定排序。
"""
from __future__ import annotations

import math
import os
import pickle
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..utils.logger import get_logger
from .vault import tokenize

logger = get_logger(__name__)

BM25_CACHE_NAME = "embeddings.bm25.pkl"
BM25_CACHE_VERSION = 1

_TWO_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z]{2})(?![A-Za-z])")
_EMPTY_POSTING = (np.zeros(0, dtype=np.int32), np.zeros(0, dtype=np.int32))


def bm25_tokenize(text: str) -> List[str]:
    """BM25 专用分词 = vault.tokenize + 补回所有独立的两字母词（小写归一）。"""
    return tokenize(text) + [m.lower() for m in _TWO_LETTER.findall(text)]


class BM25Index:
    """内存紧凑倒排表：doc_len (int32[n]) + term -> (doc_idx int32[], tf int32[])。"""

    def __init__(
        self,
        doc_len: np.ndarray,
        postings: Dict[str, Tuple[np.ndarray, np.ndarray]],
        content_version: str = "",
    ):
        self.doc_len = doc_len
        self.postings = postings
        self.content_version = content_version

    def __len__(self) -> int:
        return int(self.doc_len.shape[0])

    @classmethod
    def build(cls, texts: List[str], content_version: str = "") -> "BM25Index":
        n = len(texts)
        doc_len = np.zeros(n, dtype=np.int32)
        term_docs: Dict[str, List[int]] = {}
        term_tfs: Dict[str, List[int]] = {}

        for doc_id, text in enumerate(texts):
            toks = bm25_tokenize(text or "")
            doc_len[doc_id] = len(toks)
            if not toks:
                continue
            tf_map: Dict[str, int] = {}
            for t in toks:
                tf_map[t] = tf_map.get(t, 0) + 1
            for t, tf in tf_map.items():
                dl = term_docs.get(t)
                if dl is None:
                    term_docs[t] = [doc_id]
                    term_tfs[t] = [tf]
                else:
                    dl.append(doc_id)
                    term_tfs[t].append(tf)

        postings: Dict[str, Tuple[np.ndarray, np.ndarray]] = {
            t: (
                np.asarray(term_docs[t], dtype=np.int32),
                np.asarray(term_tfs[t], dtype=np.int32),
            )
            for t in term_docs
        }
        return cls(doc_len=doc_len, postings=postings, content_version=content_version)

    def save(self, cache_path: Path) -> None:
        cache_path = Path(cache_path)
        tmp = cache_path.with_name(cache_path.name + ".tmp-{}".format(os.getpid()))
        payload = {
            "version": BM25_CACHE_VERSION,
            "content_version": self.content_version,
            "n": len(self),
            "doc_len": self.doc_len,
            "postings": self.postings,
        }
        try:
            with open(str(tmp), "wb") as fh:
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(str(tmp), str(cache_path))
        except Exception as e:  # noqa: BLE001 —— 缓存写入失败绝不阻塞主路径
            logger.debug("写入 BM25 缓存失败（忽略）：{}", e)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @classmethod
    def try_load(
        cls, cache_path: Path, expected_content_version: str, expected_n: int
    ) -> Optional["BM25Index"]:
        if not expected_content_version:
            return None
        cache_path = Path(cache_path)
        if not cache_path.exists():
            return None
        try:
            with open(str(cache_path), "rb") as fh:
                payload = pickle.load(fh)
            if not isinstance(payload, dict):
                return None
            if payload.get("version") != BM25_CACHE_VERSION:
                return None
            if payload.get("content_version") != expected_content_version:
                return None
            if payload.get("n") != expected_n:
                return None
            doc_len = payload.get("doc_len")
            postings = payload.get("postings")
            if not isinstance(doc_len, np.ndarray) or len(doc_len) != expected_n:
                return None
            if not isinstance(postings, dict):
                return None
            return cls(
                doc_len=doc_len,
                postings=postings,
                content_version=expected_content_version,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("读取 BM25 缓存失败，退回慢路径：{}", e)
            return None

    def search(
        self,
        mask: np.ndarray,
        query_tokens: List[str],
        top_k: int = 200,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> List[Tuple[int, float]]:
        """在布尔 mask 子集上精确计算 BM25 分数，返回 [(doc_idx, score), ...] 降序。"""
        if len(self) == 0 or not query_tokens or not mask.any():
            return []
        n_sub = int(mask.sum())
        avgdl = float(self.doc_len[mask].mean()) if n_sub else 0.0
        if avgdl <= 0.0:
            return []

        q_terms = set(query_tokens)
        scores = np.zeros(len(self), dtype=np.float64)
        hit_any = False

        for t in q_terms:
            d_idx, tfs = self.postings.get(t, _EMPTY_POSTING)
            if d_idx.size == 0:
                continue
            keep = mask[d_idx]
            if not keep.any():
                continue
            sub_idx = d_idx[keep]
            sub_tf = tfs[keep].astype(np.float64)
            df = int(sub_idx.size)
            idf = math.log((n_sub - df + 0.5) / (df + 0.5) + 1.0)
            denom = sub_tf + k1 * (1.0 - b + b * self.doc_len[sub_idx].astype(np.float64) / avgdl)
            scores[sub_idx] += idf * (sub_tf * (k1 + 1.0)) / denom
            hit_any = True

        if not hit_any:
            return []

        pos_idx = np.nonzero(scores > 0.0)[0]
        if pos_idx.size == 0:
            return []
        pos_scores = scores[pos_idx]
        # 稳定排序：分数降序，同分按 doc_idx 升序（与原 np.argsort(-scores) 在顺序 idxs 上一致）
        order = np.argsort(-pos_scores, kind="mergesort")
        k = min(top_k, int(pos_idx.size)) if top_k > 0 else int(pos_idx.size)
        top_order = order[:k]
        return [(int(pos_idx[i]), float(pos_scores[i])) for i in top_order]
