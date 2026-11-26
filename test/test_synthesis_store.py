# -*- coding: utf-8 -*-
"""Phase 2 二阶合成小向量库单元测试（隔离性、存根过滤、缺失降级）。"""
import numpy as np

from src.scholar.synthesis_store import (
    SYNTHESIS_DB_NAME,
    SynthesisStore,
    collect_synthesis_pages,
    sync_synthesis_store,
)


class _DummyEmbed:
    model = "dummy-embed"

    def embed(self, texts, batch_size=32):
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for i, _t in enumerate(texts):
            out[i] = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        return out


def test_collect_excludes_stub_index_and_lint(tmp_path):
    topics_dir = tmp_path / "topics"
    qa_dir = topics_dir / "qa"
    qa_dir.mkdir(parents=True)

    (topics_dir / "INDEX.md").write_text("---\ntopic: idx\n---\nindex", encoding="utf-8")
    (topics_dir / "_lint.md").write_text("---\ntopic: lint\n---\nlint", encoding="utf-8")
    (topics_dir / "mnar-diagnosis.md").write_text(
        "---\ntopic: mnar-diagnosis\ntitle: MNAR 诊断\nevidence_status: ok\n---\n\n"
        "<!-- BEGIN GENERATED v2 h=abc · 由 x 生成 · y -->\n# MNAR\n核心结论\n## 本页证据\n表\n<!-- END GENERATED -->\n",
        encoding="utf-8",
    )
    (topics_dir / "stub-topic.md").write_text(
        "---\ntopic: stub-topic\ntitle: 存根\nevidence_status: insufficient\n---\n存根",
        encoding="utf-8",
    )
    (qa_dir / "qa-test.md").write_text(
        "---\nqa: qa-test\ntitle: 测试问答\nevidence_status: ok\n---\n\n"
        "<!-- BEGIN GENERATED v4 h=abc · 由 x 生成 · y -->\n# 问题\n问答依据\n<!-- END GENERATED -->\n",
        encoding="utf-8",
    )

    pages = collect_synthesis_pages(tmp_path, model="dummy-embed")
    slugs = {p.slug for p in pages}
    assert slugs == {"mnar-diagnosis", "qa-test"}

    res = sync_synthesis_store(tmp_path, _DummyEmbed())
    assert res == {"total": 2, "embedded": 2, "deleted": 0}
    # 增量零变化
    res2 = sync_synthesis_store(tmp_path, _DummyEmbed())
    assert res2 == {"total": 2, "embedded": 0, "deleted": 0}

    store = SynthesisStore.load(tmp_path / SYNTHESIS_DB_NAME)
    assert store is not None
    hits = store.search(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), min_sim=0.55, top_k=2)
    assert len(hits) == 2
    assert all(h["score"] >= 0.99 for h in hits)


def test_missing_synthesis_db_returns_none(tmp_path):
    assert SynthesisStore.load(tmp_path / "nonexistent.sqlite3") is None
