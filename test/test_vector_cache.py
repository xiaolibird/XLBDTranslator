# -*- coding: utf-8 -*-
"""VectorStore sidecar 缓存（float32 mmap + records pickle）与失效契约测试。"""
import sqlite3
import numpy as np

from src.scholar.embed_store import (
    VectorStore,
    _cache_paths,
    sync_store,
)


class _DummyEmbedClient:
    model = "dummy-embed"

    def __init__(self, dim=8):
        self.dim = dim

    def embed(self, texts, batch_size=64):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2**31))
            v = rng.normal(size=self.dim).astype(np.float32)
            v /= np.linalg.norm(v)
            out[i] = v
        return out


def _sample_index(note_line=10):
    return {
        "generated_at": "2026-11-25T20:00:00",
        "papers": [
            {
                "citekey": "alpha2025Test",
                "dedup_key": "doi:10.1000/a",
                "title": "Informative Missingness in EHR",
                "one_line": "双观测过程示例",
                "priority_tier": "high",
                "month": "2026-11",
                "series": "manual",
                "year": 2025,
                "has_full_text": True,
                "note_file": "n.md",
                "note_line": note_line,
                "bucket": ["missingness"],
                "highlights": [
                    {"text": "区分就诊强度与条件测量。", "tag": "可引用证据", "section": "结果"}
                ],
            }
        ],
    }


def test_cache_roundtrip_corruption_and_meta_update_invalidation(tmp_path):
    db_path = tmp_path / "embeddings.sqlite3"
    client = _DummyEmbedClient()
    sync_store(db_path, _sample_index(note_line=10), client)

    vec_path, rec_path = _cache_paths(db_path)
    assert vec_path.exists() and rec_path.exists()

    # 1. 快路径加载：mmap 数组必须只读，且内容与 SQLite 一致
    fast_store = VectorStore.load(db_path)
    assert fast_store.mat.flags.writeable is False
    assert fast_store.meta.get("content_version")
    sqlite_store = VectorStore._load_from_sqlite(db_path)
    np.testing.assert_allclose(fast_store.mat, sqlite_store.mat, atol=0.0, rtol=0.0)
    assert fast_store.records == sqlite_store.records

    # 2. 破坏 vec.npy（截断）→ 自动退回 SQLite 并自愈重建缓存
    vec_path.write_bytes(b"corrupt")
    healed = VectorStore.load(db_path)
    np.testing.assert_allclose(healed.mat, sqlite_store.mat, atol=0.0, rtol=0.0)
    assert vec_path.stat().st_size > 16

    # 3. 仅变更元数据（note_line 10 -> 42，行数不变）→ content_version 改变，缓存必须刷新
    old_cv = healed.meta["content_version"]
    stats = sync_store(db_path, _sample_index(note_line=42), client)
    assert stats.embedded == 0
    assert stats.meta_updated > 0
    updated = VectorStore.load(db_path)
    assert updated.meta["content_version"] != old_cv
    assert all(r["note_line"] == 42 for r in updated.records)


def test_legacy_db_without_content_version_falls_back_to_sqlite(tmp_path):
    db_path = tmp_path / "embeddings.sqlite3"
    client = _DummyEmbedClient()
    sync_store(db_path, _sample_index(), client)
    vec_path, rec_path = _cache_paths(db_path)
    # 模拟老库：删掉 meta.content_version 并删缓存
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM meta WHERE key='content_version'")
    conn.commit()
    conn.close()
    vec_path.unlink()
    rec_path.unlink()

    store = VectorStore.load(db_path)
    assert len(store) == 2
    assert not vec_path.exists()
