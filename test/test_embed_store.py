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


# ---------------- sync_store 增量 diff / 防护（假 client，不连 Ollama） ----------------

from src.scholar.embed_store import sync_store, model_matches  # noqa: E402


class _FakeEmbedClient:
    """确定性假 embedding：文本 sha256 前 4 字节 → L2 归一 4 维向量。"""
    model = "bge-m3"

    def __init__(self):
        self.embed_calls = 0

    def embed(self, texts, batch_size=64):
        self.embed_calls += 1
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for i, t in enumerate(texts):
            h = hashlib.sha256(t.encode("utf-8")).digest()
            v = np.frombuffer(h[:4], dtype=np.uint8).astype(np.float32) + 1.0
            out[i] = v / np.linalg.norm(v)
        return out


def _db_citekeys(db):
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT citekey FROM chunks")}
    finally:
        conn.close()


def test_sync_store_initial_then_noop(tmp_path):
    """首次同步全量嵌入；索引不变的第二次同步 0 嵌入、全部 meta_refreshed。"""
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("a2024A", highlights=[_hl("citable", "s1")]),
                      _paper("b2024B")],
           "generated_at": "2026-01-01T00:00:00"}
    stats = sync_store(db, idx, _FakeEmbedClient())
    assert (stats.total, stats.embedded, stats.deleted) == (3, 3, 0)
    stats2 = sync_store(db, idx, _FakeEmbedClient())
    assert (stats2.embedded, stats2.deleted, stats2.meta_refreshed) == (0, 0, 3)


def test_sync_store_incremental_add_change_delete(tmp_path):
    """增量四路：改文本→重嵌、没动→meta_refresh、消失→删除、新增→嵌入。"""
    db = tmp_path / "e.sqlite3"
    idx1 = {"papers": [_paper("a2024A", highlights=[_hl("citable", "s1")]),
                       _paper("b2024B")]}
    sync_store(db, idx1, _FakeEmbedClient())
    idx2 = {"papers": [_paper("a2024A", one_line="改了", highlights=[_hl("citable", "s1")]),
                       _paper("c2024C")]}
    stats = sync_store(db, idx2, _FakeEmbedClient())
    assert stats.embedded == 2            # a 的 paper 文本变了 + c 新增
    assert stats.deleted == 1             # b 的 paper chunk
    assert stats.meta_refreshed == 1      # a 的 highlight 没动
    assert _db_citekeys(db) == {"a2024A", "c2024C"}


def test_sync_store_dry_run_touches_nothing(tmp_path):
    """dry_run 只算计划：不建库、不调 embed。"""
    db = tmp_path / "e.sqlite3"
    client = _FakeEmbedClient()
    stats = sync_store(db, {"papers": [_paper("a2024A")]}, client, dry_run=True)
    assert stats.embedded == 1 and stats.dry_run
    assert not db.exists()
    assert client.embed_calls == 0


def test_sync_store_empty_index_rejected(tmp_path):
    """空期望集一律拒绝落库（防 --full 把好库清成 0 行）。"""
    with pytest.raises(VectorStoreError, match="拒绝"):
        sync_store(tmp_path / "e.sqlite3", {"papers": []}, _FakeEmbedClient())


def test_sync_store_full_shrink_guard(tmp_path):
    """--full 前索引骤缩（<50%）→ 拒绝替换现有库。"""
    db = tmp_path / "e.sqlite3"
    idx_big = {"papers": [_paper("a2024A", highlights=[_hl("citable", "s1"), _hl("method", "s2")]),
                          _paper("b2024B", highlights=[_hl("citable", "s3")])]}
    sync_store(db, idx_big, _FakeEmbedClient())          # 5 chunks
    idx_small = {"papers": [_paper("c2024C")]}           # 1 chunk < 5*0.5
    with pytest.raises(VectorStoreError, match="不到现有库"):
        sync_store(db, idx_small, _FakeEmbedClient(), full=True)
    assert _db_citekeys(db) == {"a2024A", "b2024B"}      # 原库毫发无损


def test_sync_store_full_rebuild(tmp_path):
    """--full 全量重建：tmp+replace 后库内容与索引一致。"""
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("a2024A", highlights=[_hl("citable", "s1")]), _paper("b2024B")]}
    sync_store(db, idx, _FakeEmbedClient())
    stats = sync_store(db, idx, _FakeEmbedClient(), full=True)
    assert stats.embedded == 3 and stats.full
    assert _db_citekeys(db) == {"a2024A", "b2024B"}
    assert not list(tmp_path.glob("*.tmp-*"))            # 无临时库残留


def test_empty_paper_text_skipped():
    """title 与 one_line 都空 → 整条跳过（不嵌空串占检索名额）。"""
    idx = {"papers": [_paper("g2025Empty", title="", one_line="",
                             highlights=[_hl("citable", "有句子也不进")])]}
    assert chunks_from_index(idx) == []


def test_model_matches_base_name():
    """bge-m3:latest 与 bge-m3 视为同一模型；不同基础名不同。"""
    assert model_matches("bge-m3:latest", "bge-m3")
    assert model_matches("bge-m3", "bge-m3:latest")
    assert not model_matches("bge-m3", "bge-large")
    assert not model_matches(None, "bge-m3")


# ---------------- VectorStore.search mask 语义 ----------------

def test_search_mask_excludes_rows():
    """mask=False 的行即使余弦最高也不得出现；窄过滤不空手。"""
    meta = {"model": "bge-m3", "dim": "2"}
    records = [{"level": "paper"}, {"level": "highlight"}, {"level": "paper"}]
    mat = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
    store = VectorStore(meta, records, mat)
    q = np.array([1, 0], dtype=np.float32)
    hits = store.search(q, mask=np.array([False, True, True]), top_k=3)
    idxs = [i for i, _ in hits]
    assert 0 not in idxs
    assert idxs[0] == 1                                  # 掩码内最相似者居首


# ---------------- workflow._library_neighbors 降级分支 ----------------

def test_library_neighbors_degrades_silently(tmp_path):
    """向量库缺失 → 返回空近邻并置 unavailable，第二次调用短路不再尝试。"""
    from types import SimpleNamespace
    from src.scholar.workflow import ScholarWorkflow

    wf = ScholarWorkflow.__new__(ScholarWorkflow)        # 绕开 __init__（Gmail/输出目录副作用）
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(notes_dir=tmp_path),  # 绝对路径 repo_path 原样返回；库不存在
        llm=SimpleNamespace(embedding_model="bge-m3", embedding_base_url=None,
                            ollama_base_url=None))
    wf._library_neighbors_cache = {}
    wf._vector_store = None
    wf._paper_mask = None
    wf._embedding_client = None
    wf._library_neighbors_unavailable = False

    seg = SimpleNamespace(segment_id=1, metadata=SimpleNamespace(title="t"), original_abstract="a")
    assert wf._library_neighbors([seg]) == {1: []}
    assert wf._library_neighbors_unavailable is True
    seg2 = SimpleNamespace(segment_id=2, metadata=SimpleNamespace(title="t2"), original_abstract="")
    assert wf._library_neighbors([seg2]) == {2: []}      # 短路分支

    wf.close_rag_resources()                             # 幂等收尾不炸
    wf.close_rag_resources()


# ---------------- 第二轮审查回归：并发/骤缩/归一化契约 ----------------

class _RaceEmbedClient(_FakeEmbedClient):
    """embed 期间对库做一次并发写，模拟另一进程的增量同步。"""

    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path

    def embed(self, texts, batch_size=64):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("UPDATE chunks SET text_hash='RACED' WHERE rowid=1")
        conn.commit()
        conn.close()
        return super().embed(texts, batch_size=batch_size)


def test_sync_store_snapshot_recheck_catches_concurrent_write(tmp_path):
    """embed 阶段（无锁窗口）被并发修改 → BEGIN IMMEDIATE 后快照复核必须拒绝本次同步。"""
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("a2024A"), _paper("b2024B")]}
    sync_store(db, idx, _FakeEmbedClient())
    idx2 = {"papers": [_paper("a2024A"), _paper("b2024B"), _paper("c2024C")]}
    with pytest.raises(VectorStoreError, match="并发修改"):
        sync_store(db, idx2, _RaceEmbedClient(db))


def test_sync_store_incremental_shrink_guard(tmp_path):
    """增量同步遇到骤缩索引（<50%）→ 拒绝执行，不做无上限批删。"""
    db = tmp_path / "e.sqlite3"
    idx_big = {"papers": [_paper("a2024A", highlights=[_hl("citable", "s1"), _hl("method", "s2")]),
                          _paper("b2024B", highlights=[_hl("citable", "s3")])]}
    sync_store(db, idx_big, _FakeEmbedClient())          # 5 chunks
    idx_small = {"papers": [_paper("c2024C")]}           # 1 chunk
    with pytest.raises(VectorStoreError, match="拒绝增量"):
        sync_store(db, idx_small, _FakeEmbedClient())
    assert _db_citekeys(db) == {"a2024A", "b2024B"}


def test_embedding_client_l2_normalizes(monkeypatch):
    """EmbeddingClient.embed 契约：输出行 L2 归一；全零向量不除零、原样保留。
    检索余弦正确性完全依赖这一前提（库内与 query 侧共用此入口）。"""
    from src.scholar.embeddings import EmbeddingClient
    client = EmbeddingClient()
    raw = np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32)   # 未归一 + 全零
    monkeypatch.setattr(client, "_embed_batch", lambda batch: raw[:len(batch)])
    out = client.embed(["a", "b"])
    assert out.shape == (2, 2)
    assert abs(np.linalg.norm(out[0]) - 1.0) < 1e-6              # 归一化
    assert np.linalg.norm(out[1]) == 0.0                          # 零向量原样保留不 NaN
    assert not np.isnan(out).any()


def test_multiline_title_keeps_one_line_alignment():
    """title 含内嵌换行时，paper 文本仍只有一个分隔换行——split('\\n',1) 还原不错位。"""
    idx = {"papers": [_paper("h2025NL", title="Line1\nLine2", one_line="一句话")]}
    cks = [c for c in chunks_from_index(idx) if c.level == "paper"]
    assert len(cks) == 1
    parts = cks[0].text.split("\n", 1)
    assert parts[0] == "Line1 Line2"
    assert parts[1] == "一句话"


# ---------------- workflow._library_neighbors happy path ----------------

def test_library_neighbors_happy_path():
    """min_sim 过滤 + one_line 还原 + 缓存写入，全链路（假 store/假 client，不连 Ollama）。"""
    from types import SimpleNamespace
    from src.scholar.workflow import ScholarWorkflow

    # 三条 paper 级记录：与 query 余弦分别为 1.0 / 0.7 / 0.3（最后者应被 min_sim=0.65 滤掉）
    meta = {"model": "bge-m3", "dim": "2"}
    records = [
        {"level": "paper", "citekey": "hit2025A", "year": 2025, "text": "T甲\n一句话甲"},
        {"level": "paper", "citekey": "hit2024B", "year": 2024, "text": "T乙"},   # 无 one_line
        {"level": "paper", "citekey": "far2023C", "year": 2023, "text": "T丙\n一句话丙"},
    ]
    v = np.array([[1, 0], [0.7, np.sqrt(1 - 0.49)], [0.3, np.sqrt(1 - 0.09)]], dtype=np.float32)
    store = VectorStore(meta, records, v)

    class _Client:
        model = "bge-m3"
        def probe(self):
            return {"model": "bge-m3", "dim": 2}
        def embed(self, texts, batch_size=64):
            return np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (len(texts), 1))

    wf = ScholarWorkflow.__new__(ScholarWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(notes_dir=Path(".")),
        llm=SimpleNamespace(embedding_model="bge-m3", embedding_base_url=None, ollama_base_url=None))
    wf._library_neighbors_cache = {}
    wf._vector_store = store
    wf._paper_mask = np.array([True, True, True])
    wf._embedding_client = _Client()
    wf._library_neighbors_unavailable = False

    seg = SimpleNamespace(segment_id=7, metadata=SimpleNamespace(title="q"), original_abstract="a")
    out = wf._library_neighbors([seg], k=3, min_sim=0.65)
    hits = out[7]
    assert [h["citekey"] for h in hits] == ["hit2025A", "hit2024B"]   # 0.3 被滤掉
    assert hits[0]["one_line"] == "一句话甲"
    assert hits[1]["one_line"] == ""                                   # 无 one_line 不错位
    assert wf._library_neighbors_cache[7] == hits                      # 已入缓存


# ---------------- R3：陈旧向量库的告警与降级 ----------------

def _stale_store(src="2026-08-14T20:23:56"):
    from src.scholar.embed_store import VectorStore
    return VectorStore({"model": "bge-m3", "dim": "2", "source_generated_at": src},
                       [{"level": "paper", "citekey": "fani2025Coefficient", "year": 2025,
                         "text": "T\n一句话"}],
                       np.array([[1.0, 0.0]], dtype=np.float32))


def test_freshness_warning_says_citekeys_may_be_dead_not_just_incomplete():
    """R3-5：措辞必须点明「检索到的 citekey 可能已注销」，不能只说「可能漏掉最新内容」。

    只说"漏内容"会把危害说轻——读者的合理反应是"那我先用着，反正只是不全"，
    于是照单全收地把 [@已注销键] 粘进稿子，直到 pandoc 报 not found 才发现。
    """
    warn = _stale_store().freshness_warning("2026-08-15T14:47:18")
    assert warn is not None
    assert "citekey" in warn
    assert "改名" in warn or "删除" in warn          # 说清是"键没了"，不是"内容少了"
    assert "书目" in warn or "pandoc" in warn        # 说清后果落在哪
    assert "notes_embed.py" in warn                  # 且给得出可执行的补救


def test_freshness_warning_silent_when_store_is_current():
    store = _stale_store(src="2026-08-15T14:47:18")
    assert store.freshness_warning("2026-08-15T14:47:18") is None
    assert store.freshness_lag_seconds("2026-08-15T14:47:18") is None


def test_freshness_lag_seconds_measures_gap_and_tolerates_junk():
    """幅度用于「提醒还是降级」的分档；时间戳解析不了要返回 None 交给调用方从严处理。"""
    store = _stale_store(src="2026-08-14T20:00:00")
    assert store.freshness_lag_seconds("2026-08-15T20:00:00") == pytest.approx(86400.0)
    assert store.freshness_lag_seconds("不是时间戳") is None
    assert store.freshness_lag_seconds(None) is None


def test_read_index_generated_at_head_and_fallback(tmp_path):
    """8MB 索引不值得为一个时间戳整份解析：头部正则命中即可；字段挪到尾部也要能兜住。"""
    from src.scholar.embed_store import read_index_generated_at
    import json as _json
    p = tmp_path / "literature_index.json"
    p.write_text('{\n "schema_version": 4,\n "generated_at": "2026-08-15T14:47:18",\n'
                 ' "papers": []\n}', encoding="utf-8")
    assert read_index_generated_at(p) == "2026-08-15T14:47:18"
    # 字段被 9KB 的填充挤出头部窗口 → 退回整份解析
    p.write_text(_json.dumps({"pad": "x" * 9000, "generated_at": "2026-01-01T00:00:00"}),
                 encoding="utf-8")
    assert read_index_generated_at(p) == "2026-01-01T00:00:00"
    assert read_index_generated_at(tmp_path / "nope.json") is None
    (tmp_path / "broken.json").write_text("{半个 JSON", encoding="utf-8")
    assert read_index_generated_at(tmp_path / "broken.json") is None


def _wf_with_db(tmp_path, index_generated_at, store_src):
    """造一个「库已建好但快照时间可控」的 workflow，走 _vector_store is None 那条真实分支。"""
    import sqlite3
    from types import SimpleNamespace
    from src.scholar.embed_store import DB_NAME, sync_store
    from src.scholar.workflow import ScholarWorkflow

    db = tmp_path / DB_NAME
    sync_store(db, {"papers": [_paper("fani2025Coefficient")]}, _FakeEmbedClient())
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE meta SET value=? WHERE key='source_generated_at'", (store_src,))
    conn.commit()
    conn.close()
    (tmp_path / "literature_index.json").write_text(
        '{"schema_version": 4, "generated_at": "%s", "papers": []}' % index_generated_at,
        encoding="utf-8")

    # query 侧直接回放库里那条向量：余弦恒为 1，测的是"降不降级"而非相似度算法
    stored = VectorStore.load(db).mat[0].copy()

    class _Client:
        model = "bge-m3"
        def probe(self):
            return {"model": "bge-m3", "dim": int(stored.shape[0])}
        def embed(self, texts, batch_size=64):
            return np.tile(stored, (len(texts), 1))

    wf = ScholarWorkflow.__new__(ScholarWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(notes_dir=tmp_path),
        llm=SimpleNamespace(embedding_model="bge-m3", embedding_base_url=None,
                            ollama_base_url=None))
    wf._library_neighbors_cache = {}
    wf._vector_store = None
    wf._paper_mask = None
    wf._embedding_client = _Client()
    wf._library_neighbors_unavailable = False
    return wf


def test_library_neighbors_degrades_when_vector_store_is_badly_stale(tmp_path):
    """R3-2：周一 09:00 无人值守的 digest 近邻注入直接吃向量库的 citekey/year。

    库一旧，注入给 LLM 的就是已注销的旧键与旧年份（"你的札记库里已有 fani2025Coefficient
    (2025)"，而磁盘上这篇现在叫 fani2026Coefficient/2026）。近邻注入的用途正是判"是否与
    已入库文献重复"，喂错年份直接干扰该判断，LLM 还可能把旧键回写进 reason 固化下去。
    这条路径此前**根本不调 freshness_warning**——有人盯着的 CLI 会告警，无人值守的定时
    任务反而静默，恰好反了。落后过阈值时宁可本轮没有近邻。
    """
    from loguru import logger as _lg
    from types import SimpleNamespace
    wf = _wf_with_db(tmp_path, "2026-08-15T14:47:18", "2026-08-01T00:00:00")   # 落后 14 天
    seg = SimpleNamespace(segment_id=1, metadata=SimpleNamespace(title="q"), original_abstract="a")

    lines = []
    sink = _lg.add(lines.append, level="WARNING")
    try:
        out = wf._library_neighbors([seg])
    finally:
        _lg.remove(sink)
    blob = "\n".join(lines)

    assert out == {1: []}                              # 走既有的降级分支，不注入陈旧身份
    assert wf._library_neighbors_unavailable is True
    assert "落后" in blob and "notes_embed.py" in blob   # 且告警说得出原因与补救


def test_library_neighbors_warns_but_still_serves_when_only_slightly_stale(tmp_path):
    """轻微落后（远小于阈值）只提醒不降级——刚入库那几分钟的正常抖动不该废掉整轮 RAG。"""
    from loguru import logger as _lg
    from types import SimpleNamespace
    wf = _wf_with_db(tmp_path, "2026-08-15T14:47:18", "2026-08-15T14:00:00")   # 落后 47 分钟
    seg = SimpleNamespace(segment_id=1, metadata=SimpleNamespace(title="q"), original_abstract="a")

    lines = []
    sink = _lg.add(lines.append, level="WARNING")
    try:
        out = wf._library_neighbors([seg], k=3, min_sim=0.5)
    finally:
        _lg.remove(sink)

    assert wf._library_neighbors_unavailable is False
    assert [h["citekey"] for h in out[1]] == ["fani2025Coefficient"]   # 仍然出结果
    assert any("落后" in s for s in lines)                              # 但提醒照打
