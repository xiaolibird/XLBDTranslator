# -*- coding: utf-8 -*-
"""src/scholar/embed_store.py 关键纯函数回归（不读真实索引、不连 Ollama）。

锁 RAG 向量库最容易悄悄退化的四件事：
1. **chunks_from_index 入选口径**：与 notes_query 平行的同规则再实现，必须只收
   duplicate_of 为空且有真实 citekey 的 keeper——尤其 MISSING-KEY 占位键不能进（
   否则 notes_search --cite 输出死引用）；
2. **text_hash 织入模型名**：换 embedding 模型必须让全部旧 hash 失效（否则增量
   同步静默产出混合模型坏索引）；
3. **highlight chunk id 掺 role+序号**：同篇同文本不同 role 的证据不能碰撞覆盖丢数据；
4. **VectorStore BLOB 维度校验**：维度与 meta.dim 不符必须抛可操作错误而非裸崩溃。
"""
import sys
import hashlib
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar.embed_store import chunks_from_index, _text_hash, VectorStore, VectorStoreError  # noqa: E402


def _paper(citekey, *, title="T", one_line="x", highlights=(), **kw):
    e = {"citekey": citekey, "title": title, "one_line": one_line,
         "highlights": list(highlights), "duplicate_of": None,
         "year": 2025, "month": "2026-07", "series": "manual",
         "bucket": ["A"], "priority_tier": "high"}
    e.update(kw)
    return e


def _hl(role, text, section="方法与数据"):
    return {"role": role, "tag": "x", "section": section, "text": text}


# ---------------- chunks_from_index 入选口径 ----------------

def test_duplicate_of_filtered():
    """duplicate_of 非空的条目（重复）不入向量库。"""
    idx = {"papers": [
        _paper("a2024Keep", duplicate_of=None),
        _paper("a2024Dup", duplicate_of="a2024Keep"),
    ]}
    cks = chunks_from_index(idx)
    keys = [c.citekey for c in cks]
    assert "a2024Keep" in keys
    assert "a2024Dup" not in keys


def test_missing_citekey_filtered():
    """MISSING-KEY 占位键不对应真实文献，必须拦截（对齐 notes_index.is_missing_citekey 契约）。"""
    idx = {"papers": [
        _paper("MISSING-KEY-abc123"),
        _paper("b2024Real"),
        _paper("c2024SrcMissing", citekey_source="missing"),
    ]}
    cks = chunks_from_index(idx)
    keys = [c.citekey for c in cks]
    assert "b2024Real" in keys
    assert "MISSING-KEY-abc123" not in keys
    assert "c2024SrcMissing" not in keys


def test_missing_citekey_also_filters_highlights():
    """占位键的句级证据同样不能进——否则 --cite 仍输出死引用。"""
    idx = {"papers": [_paper("MISSING-KEY-x", highlights=[_hl("citable", "某句")])]}
    assert chunks_from_index(idx) == []


def test_paper_and_highlight_levels():
    """keeper 同时产出 paper 级与 highlight 级 chunk，id 前缀正确。"""
    idx = {"papers": [_paper("a2024X", highlights=[_hl("citable", "句1"), _hl("method", "句2")])]}
    cks = chunks_from_index(idx)
    levels = {c.id.split(":")[0] for c in cks}
    assert levels == {"p", "h"}


# ---------------- text_hash 织入模型名 ----------------

def test_text_hash_changes_with_model():
    """同文本不同模型 → 不同 hash（换模型全体过期）。"""
    t = "informative missingness is a special case of MNAR"
    assert _text_hash("bge-m3", t) != _text_hash("bge-large", t)


def test_text_hash_deterministic():
    """同文本同模型多次计算 hash 稳定（增量 diff 依赖）。"""
    t = "缺失本身携带信息"
    assert _text_hash("bge-m3", t) == _text_hash("bge-m3", t)


# ---------------- highlight id 掺 role+序号 ----------------

def test_same_text_diff_role_keeps_both():
    """同篇同文本不同 role（一句同时标 citable/refutable）必须各自独立成 chunk。"""
    sent = "附录 B 与 Table 2 的总数对不上"
    idx = {"papers": [_paper("e2025Tab", highlights=[
        _hl("citable", sent), _hl("refutable", sent)])]}
    cks = chunks_from_index(idx)
    hids = {c.id for c in cks if c.level == "highlight"}
    assert len(hids) == 2, "同文不同 role 的 highlight 碰撞覆盖了！"
    roles = {c.role for c in cks if c.level == "highlight"}
    assert roles == {"citable", "refutable"}


def test_same_text_same_role_duplicate_keeps_both():
    """同篇同文本同 role 重复出现 → seq 序号独立成 chunk，不 last-wins 丢数据。"""
    sent = "重复的方法论句子"
    idx = {"papers": [_paper("f2025Dup", highlights=[
        _hl("method", sent), _hl("method", sent)])]}
    cks = [c for c in chunks_from_index(idx) if c.level == "highlight"]
    assert len(cks) == 2


# ---------------- VectorStore BLOB 维度校验 ----------------

def _fake_store(db_path, dim, blob_len):
    """建一个 meta.dim=dim、chunk vec 为 blob_len 字节的假库。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE chunks(id TEXT PRIMARY KEY, level TEXT, citekey TEXT, "
                 "role TEXT, tag TEXT, section TEXT, month TEXT, series TEXT, tier TEXT, "
                 "bucket TEXT, year INT, has_full_text INT, note_file TEXT, note_line INT, "
                 "text TEXT, text_hash TEXT, vec BLOB)")
    conn.execute("INSERT INTO meta VALUES('schema_version','2'),('model','bge-m3'),('dim',?)",
                 (str(dim),))
    conn.execute("INSERT INTO chunks VALUES('p:a','paper','a',NULL,NULL,NULL,'2026-08','auto',"
                 "'high','A',2026,0,NULL,NULL,'t','h',?)", (b"\x00" * blob_len,))
    conn.commit()
    conn.close()


def test_blob_dim_mismatch_raises(tmp_path):
    """meta.dim=1024 但 vec blob 只有 100 字节 → 抛可操作错误而非裸 ValueError。"""
    db = tmp_path / "e.sqlite3"
    _fake_store(db, 1024, 100)
    with pytest.raises(VectorStoreError, match="维度"):
        VectorStore.load(db)


def test_blob_dim_match_loads(tmp_path):
    """meta.dim=4 且 vec blob 恰为 16 字节（4×float32）→ 正常加载。"""
    db = tmp_path / "e.sqlite3"
    _fake_store(db, 4, 16)
    store = VectorStore.load(db)
    assert len(store) == 1
    assert store.mat.shape == (1, 4)
