# -*- coding: utf-8 -*-
"""Phase 2 notes_search --batch-queries 与单查询等价性测试（打桩 Embedding/Store）。"""
import json
from types import SimpleNamespace
import numpy as np

from scripts import notes_search
from src.scholar.embed_store import VectorStore


def _make_fake_store():
    records = [
        {
            "id": "p:k1", "level": "paper", "citekey": "k1", "role": None, "tag": None,
            "section": None, "month": "2026-11", "series": "manual", "tier": "high",
            "bucket": [], "year": 2026, "has_full_text": True, "note_file": "n.md",
            "note_line": 1, "chapter": None, "pages": None, "book_key": None,
            "text": "Recommended Visit Intervals\n用推荐复诊间隔做ANAR敏感性分析",
        },
        {
            "id": "h:k1:method:abc:0", "level": "highlight", "citekey": "k1", "role": "method",
            "tag": "方法论借鉴", "section": "方法", "month": "2026-11", "series": "manual",
            "tier": "high", "bucket": [], "year": 2026, "has_full_text": True,
            "note_file": "n.md", "note_line": 1, "chapter": None, "pages": "7", "book_key": None,
            "text": "推荐复诊间隔解释了78%的评估时间变异。",
        },
    ]
    mat = np.array([[1.0, 0.0, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0]], dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    return VectorStore(
        meta={"schema_version": "3", "model": "bge-m3", "dim": "4", "content_version": "cv1"},
        records=records,
        mat=mat,
    )


def test_batch_queries_matches_single_json(monkeypatch, tmp_path, capsys):
    store = _make_fake_store()
    qfile = tmp_path / "queries.txt"
    qfile.write_text("recommended visit intervals\n不存在的离题词\n", encoding="utf-8")

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def embed(self, texts, batch_size=64):
            out = []
            for t in texts:
                if "recommended" in t:
                    out.append(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
                else:
                    out.append(np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32))
            return np.stack(out)

        def close(self):
            pass

    fake_settings = SimpleNamespace(
        processing=SimpleNamespace(notes_dir=tmp_path),
        llm=SimpleNamespace(embedding_model="bge-m3", ollama_base_url="http://localhost:11434/v1"),
    )
    monkeypatch.setattr(notes_search, "load_scholar_settings", lambda *a, **kw: fake_settings)
    monkeypatch.setattr(notes_search, "_load_store", lambda *a, **kw: (store, None))
    monkeypatch.setattr(notes_search, "EmbeddingClient", _FakeClient)
    monkeypatch.setattr(notes_search, "_get_bm25_index", lambda *a, **kw: None)

    # 1. 批量模式
    monkeypatch.setattr("sys.argv", ["notes_search.py", "--batch-queries", str(qfile), "--mode", "dense"])
    rc_batch = notes_search.main()
    assert rc_batch == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["query"] == "recommended visit intervals"
    assert lines[0]["results"][0]["citekey"] == "k1"
    assert lines[1]["results"] == []

    # 2. 单查询对照
    monkeypatch.setattr("sys.argv", ["notes_search.py", "recommended visit intervals", "--json", "--mode", "dense"])
    rc_single = notes_search.main()
    assert rc_single == 0
    single_obj = json.loads(capsys.readouterr().out)
    assert single_obj == lines[0]
