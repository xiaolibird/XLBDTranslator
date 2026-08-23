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


def _capture_warnings():
    """loguru 不走标准 logging，caplog 抓不到——挂一个临时 sink 收字符串。"""
    from loguru import logger as _lg
    buf = []
    sink_id = _lg.add(lambda m: buf.append(str(m)), level="WARNING")
    return buf, (lambda: _lg.remove(sink_id))


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
    """首次同步全量嵌入；索引不变的第二次同步 0 嵌入、全部 meta_refreshed，
    且 meta_updated == 0——零变化同步不许发任何 UPDATE（churn 验收判据）。"""
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("a2024A", highlights=[_hl("citable", "s1")]),
                      _paper("b2024B")],
           "generated_at": "2026-01-01T00:00:00"}
    stats = sync_store(db, idx, _FakeEmbedClient())
    assert (stats.total, stats.embedded, stats.deleted) == (3, 3, 0)
    stats2 = sync_store(db, idx, _FakeEmbedClient())
    assert (stats2.embedded, stats2.deleted, stats2.meta_refreshed) == (0, 0, 3)
    assert stats2.meta_updated == 0


def test_sync_store_meta_change_updates_only_changed_rows(tmp_path):
    """文本没动、元数据变了（如 tier 调级）→ 不重嵌但 UPDATE，且只 UPDATE 变了的行。"""
    import sqlite3 as _sq
    db = tmp_path / "e.sqlite3"
    idx1 = {"papers": [_paper("a2024A", highlights=[_hl("citable", "s1")]),
                       _paper("b2024B")]}
    sync_store(db, idx1, _FakeEmbedClient())
    idx2 = {"papers": [_paper("a2024A", priority_tier="low", highlights=[_hl("citable", "s1")]),
                       _paper("b2024B")]}
    stats = sync_store(db, idx2, _FakeEmbedClient())
    assert stats.embedded == 0
    assert stats.meta_refreshed == 3          # 三条 hash 都没变
    assert stats.meta_updated == 2            # 只有 a 的 paper+highlight 元数据变了
    conn = _sq.connect(str(db))
    try:
        tiers = dict(conn.execute("SELECT citekey, tier FROM chunks WHERE level='paper'"))
    finally:
        conn.close()
    assert tiers == {"a2024A": "low", "b2024B": "high"}   # 变更真的落了库


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
    """title 与 one_line 都空 → 只跳过 paper 级（不嵌空串占检索名额），
    highlight 照常入库——md 解析残条但精读句仍在时，句级证据不得连带静默退出检索。"""
    idx = {"papers": [_paper("g2025Empty", title="", one_line="",
                             highlights=[_hl("citable", "句子照常入库")])]}
    chunks = chunks_from_index(idx)
    assert [c.level for c in chunks] == ["highlight"]
    assert chunks[0].text == "句子照常入库"


def test_chunk_columns_match_create_table_and_dataclass():
    """③ 列名单点：CREATE TABLE 的列、_CHUNK_COLUMNS、Chunk 字段三方必须一致——
    SELECT/UPDATE/INSERT 语句都由 _CHUNK_COLUMNS/_META_COLUMNS 生成，
    加列时改漏任何一方这条测试就红。"""
    import re as _re
    from dataclasses import fields
    from src.scholar.embed_store import _CREATE_CHUNKS_SQL, _CHUNK_COLUMNS, _META_COLUMNS, Chunk
    body = _CREATE_CHUNKS_SQL.split("(", 1)[1].rsplit(")", 1)[0]
    create_cols = [ln.strip().split()[0] for ln in body.replace("\n", ",").split(",")
                   if ln.strip()]
    assert tuple(create_cols) == _CHUNK_COLUMNS
    # Chunk 字段（顺序无关）必须覆盖除 text_hash/vec 外的全部列，且与 _META_COLUMNS 同名
    chunk_fields = {f.name for f in fields(Chunk)}
    assert set(_META_COLUMNS) <= chunk_fields
    assert set(_CHUNK_COLUMNS) - {"text_hash", "vec"} == chunk_fields


def test_load_index_file_single_point(tmp_path):
    """② 读索引单点：缺失/损坏/缺 papers 三态给可操作 err，正常返回 (data, None)。"""
    from src.scholar.notes_index import load_index_file
    import json as _json
    p = tmp_path / "literature_index.json"
    data, err = load_index_file(p)
    assert data is None and "找不到索引" in err
    p.write_text("{broken", encoding="utf-8")
    data, err = load_index_file(p)
    assert data is None and "解析失败" in err
    p.write_text('{"papers": "not-a-list"}', encoding="utf-8")
    data, err = load_index_file(p)
    assert data is None and "结构异常" in err
    p.write_text(_json.dumps({"papers": [], "generated_at": "x"}), encoding="utf-8")
    data, err = load_index_file(p)
    assert err is None and data["papers"] == []


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


def test_embed_request_disables_truncation_and_lifts_num_batch():
    """入库/查询的请求体契约（对标审计 ⚠️-1）：必须显式关掉 truncate 并抬 num_batch。

    llama.cpp 对 embedding 的 num_batch 默认 2048，与模型 8192 的上下文无关；
    不传时 2048 token 以上的文本被**静默截断**，产出与全文语义不符的向量且没有
    任何信号——正是本模块拒绝的坏索引。实测真库最长 chunk 1414 token，余量只有
    1.45 倍而非曾经以为的 4 倍，且 highlight 没有长度上限。
    """
    from src.scholar.embeddings import EmbeddingClient, _NUM_BATCH

    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [[1.0, 0.0]]}

    class _Client:
        def post(self, url, json=None):
            seen.update(json or {})
            return _Resp()

    client = EmbeddingClient()
    client._client = _Client()
    client.embed(["hello"])

    assert seen.get("truncate") is False, "truncate 必须显式 False，默认 true 会静默截断"
    assert seen.get("options", {}).get("num_batch") == _NUM_BATCH
    assert _NUM_BATCH >= 8192, "低于模型上下文就白抬了"


def test_oversized_input_error_names_the_longest_text():
    """truncate=False 后超长会拿 400，而一批 64 条只要一条超长就整批失败——
    错误信息必须点名最长的那条，否则等于让人从 64 条里瞎猜是谁。"""
    import httpx
    from src.scholar.embeddings import _status_error_message

    class _Resp:
        status_code = 400
        text = '{"error":"the input length exceeds the context length"}'

    exc = httpx.HTTPStatusError("400", request=None, response=_Resp())
    msg = _status_error_message(exc, ["短的", "这条特别长" * 40])
    assert "这条特别长" in msg and "truncate" in msg
    assert str(len("这条特别长" * 40)) in msg


def test_status_error_message_falls_back_for_other_errors():
    """非超长类错误（如 500）不许被伪装成"文本太长"，否则会把人引去砍文本。"""
    import httpx
    from src.scholar.embeddings import _status_error_message

    class _Resp:
        status_code = 500
        text = "internal server error"

    msg = _status_error_message(httpx.HTTPStatusError("500", request=None, response=_Resp()),
                                ["abc"])
    assert "超出" not in msg and "internal server error" in msg


def test_multiline_title_keeps_one_line_alignment():
    """title 含内嵌换行时压成一段——按段 split('\\n') 还原不错位。"""
    idx = {"papers": [_paper("h2025NL", title="Line1\nLine2", one_line="一句话")]}
    cks = [c for c in chunks_from_index(idx) if c.level == "paper"]
    assert len(cks) == 1
    parts = cks[0].text.split("\n")
    assert parts == ["Line1 Line2", "一句话"]


# ---------------- abstracts 喂厚：独立 abstract chunk + sidecar 单点加载 ----------------

from src.scholar.embed_store import load_abstracts, ABSTRACTS_NAME, ABSTRACT_CLIP  # noqa: E402


def test_abstract_becomes_separate_chunk():
    """有摘要 → 独立 ab:<citekey> 厚召回 chunk（title+one_line+摘要三段同文），
    paper 文本保持两段式纹丝不动——摘要拼进 paper 会稀释中文判词信号
    （实测 zh_oneline @1 87%→73%），纯英文摘要 chunk 又接不住中文换述查询。"""
    idx = {"papers": [_paper("i2025Abs", one_line="判词", dedup_key="doi:10.1/x")]}
    abstracts = {"doi:10.1/x": "First line.\nSecond line. " + "长" * 900}
    cks = chunks_from_index(idx, abstracts=abstracts)
    by_level = {c.level: c for c in cks}
    assert by_level["paper"].text == "T\n判词"                 # 瘦向量不变
    ab = by_level["abstract"]
    assert ab.id == "ab:i2025Abs" and ab.citekey == "i2025Abs"
    parts = ab.text.split("\n")
    assert parts[0] == "T" and parts[1] == "判词"              # 判词随摘要同文（跨语桥）
    assert parts[2].startswith("First line. Second line.")     # 摘要换行压平
    assert len(parts[2]) == ABSTRACT_CLIP                      # 摘要段截断
    assert ab.role is None and ab.month == by_level["paper"].month   # 元数据同源


def test_abstract_chunk_survives_empty_paper_text():
    """title/one_line 全空的残条：paper 级照旧跳过，abstract chunk 仍入库
    （消费方按 citekey 找不到 paper 元数据时自行跳过展示）。"""
    idx = {"papers": [_paper("j2025NoOL", title="", one_line="", dedup_key="doi:10.1/y")]}
    cks = chunks_from_index(idx, abstracts={"doi:10.1/y": "Some abstract."})
    assert [c.level for c in cks] == ["abstract"]


def test_no_abstract_keeps_legacy_two_segment_text():
    """abstracts 缺失/缺键时产出与历史逐字节一致（text_hash 不变 → 不触发重嵌），
    且不产生 abstract chunk。"""
    idx = {"papers": [_paper("k2025Old", one_line="判词", dedup_key="doi:10.1/z")]}
    legacy = chunks_from_index(idx)
    assert [c.level for c in legacy] == ["paper"]
    assert legacy[0].text == "T\n判词"
    assert chunks_from_index(idx, abstracts=None)[0].text == legacy[0].text
    assert chunks_from_index(idx, abstracts={})[0].text == legacy[0].text
    assert [c.level for c in chunks_from_index(idx, abstracts={"doi:10.1/other": "x"})] == ["paper"]


def test_load_abstracts_defensive(tmp_path):
    """文件缺失 → {}（合法降级）；**存在但损坏/结构不对 → raise**（按无摘要继续
    会让增量 diff 删光全部 ab: 向量，0.5 骤缩闸拦不住）；正常 → 只收非空 abstract、
    单条脏 entry 只跳过不连坐。"""
    assert load_abstracts(tmp_path) == {}
    p = tmp_path / ABSTRACTS_NAME
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(VectorStoreError, match="读不出"):
        load_abstracts(tmp_path)
    p.write_text('{"no_abstracts_section": 1}', encoding="utf-8")
    with pytest.raises(VectorStoreError, match="缺少 abstracts 段"):
        load_abstracts(tmp_path)
    p.write_text(
        '{"abstracts": {"doi:10.1/a": {"abstract": "text A", "source": "openalex"},'
        ' "doi:10.1/b": {"abstract": "  "}, "doi:10.1/c": "malformed"}}',
        encoding="utf-8")
    assert load_abstracts(tmp_path) == {"doi:10.1/a": "text A"}


def test_sync_refuses_when_abstracts_sidecar_vanishes(tmp_path):
    """abstracts.json 被误删（文件不存在 = load_abstracts 合法降级路径）后跑增量：
    期望集零 ab: 而库内有 → 拒绝同步且库内厚向量原样保留。0.5 骤缩闸对此无感
    （实测比值 20780/22584≈0.92），没有这道专项闸就是静默删光 + digest 阈值失配。"""
    import json as _json
    import sqlite3 as _sq
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("n2025Gone", one_line="判词", dedup_key="doi:10.1/n")]}
    sidecar = tmp_path / ABSTRACTS_NAME
    sidecar.write_text(_json.dumps(
        {"abstracts": {"doi:10.1/n": {"abstract": "An abstract."}}}), encoding="utf-8")
    sync_store(db, idx, _FakeEmbedClient())                       # 厚库首嵌（p: + ab:）
    sidecar.unlink()                                              # 模拟误删
    with pytest.raises(VectorStoreError, match="abstract 级"):
        sync_store(db, idx, _FakeEmbedClient())
    conn = _sq.connect(str(db))
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    finally:
        conn.close()
    assert ids == {"p:n2025Gone", "ab:n2025Gone"}                 # 厚向量未被删


def test_sync_refuses_when_abstracts_sidecar_halved(tmp_path):
    """abstracts.json 被截半/删档重抓的中途产物（2026-08-21 第2轮：闸只拦全灭不拦半灭）：
    backfill 从头重抓时首次 flush 只落零头键，watcher 随即增量同步——期望集 ab: 仅剩
    1/3，比例闸必须拒绝且厚向量原样保留，否则其余 ab: 向量被当场删掉再分批重嵌（震荡）。"""
    import json as _json
    import sqlite3 as _sq
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("h2025A", one_line="判词", dedup_key="doi:10.1/ha"),
                      _paper("h2025B", one_line="判词", dedup_key="doi:10.1/hb"),
                      _paper("h2025C", one_line="判词", dedup_key="doi:10.1/hc")]}
    sidecar = tmp_path / ABSTRACTS_NAME
    sidecar.write_text(_json.dumps({"abstracts": {
        "doi:10.1/ha": {"abstract": "Abs A."},
        "doi:10.1/hb": {"abstract": "Abs B."},
        "doi:10.1/hc": {"abstract": "Abs C."}}}), encoding="utf-8")
    sync_store(db, idx, _FakeEmbedClient())                       # 3 ab: 首嵌
    sidecar.write_text(_json.dumps({"abstracts": {
        "doi:10.1/ha": {"abstract": "Abs A."}}}), encoding="utf-8")   # 截到 1/3
    with pytest.raises(VectorStoreError, match="abstract 级"):
        sync_store(db, idx, _FakeEmbedClient())
    conn = _sq.connect(str(db))
    try:
        ab_ids = {r[0] for r in conn.execute(
            "SELECT id FROM chunks WHERE level='abstract'")}
    finally:
        conn.close()
    assert ab_ids == {"ab:h2025A", "ab:h2025B", "ab:h2025C"}      # 厚向量未被删


def test_sync_allows_abstract_shrink_at_half_boundary(tmp_path):
    """边界口径与 0.5 总量闸一致（严格小于才拦）：2 条 ab: 缩到 1 条（恰为一半）放行，
    合法的少量摘要删减不被误伤。"""
    import json as _json
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("g2025A", one_line="判词", dedup_key="doi:10.1/ga"),
                      _paper("g2025B", one_line="判词", dedup_key="doi:10.1/gb")]}
    sidecar = tmp_path / ABSTRACTS_NAME
    sidecar.write_text(_json.dumps({"abstracts": {
        "doi:10.1/ga": {"abstract": "Abs A."},
        "doi:10.1/gb": {"abstract": "Abs B."}}}), encoding="utf-8")
    sync_store(db, idx, _FakeEmbedClient())
    sidecar.write_text(_json.dumps({"abstracts": {
        "doi:10.1/ga": {"abstract": "Abs A."}}}), encoding="utf-8")
    stats = sync_store(db, idx, _FakeEmbedClient())
    assert stats.deleted_abstract == 1                            # 恰半放行，正常删 1 条
    assert stats.deleted == 1


def _abs_idx_and_sidecar(tmp_path, n_total, n_keep):
    """n_total 篇带摘要的索引 + 只保留前 n_keep 键的 sidecar（模拟 backfill 中途产物）。"""
    import json as _json
    idx = {"papers": [_paper("w2025K{}".format(i), one_line="判词",
                             dedup_key="doi:10.1/k{}".format(i))
                      for i in range(n_total)]}
    full = {"doi:10.1/k{}".format(i): {"abstract": "Abs {}.".format(i)}
            for i in range(n_total)}
    sidecar = tmp_path / ABSTRACTS_NAME
    sidecar.write_text(_json.dumps({"abstracts": full}), encoding="utf-8")
    partial = {"doi:10.1/k{}".format(i): full["doi:10.1/k{}".format(i)]
               for i in range(n_keep)}
    return idx, sidecar, _json.dumps({"abstracts": partial})


def test_sync_refuses_mass_abstract_delete_past_half(tmp_path):
    """2026-08-21 第3轮：0.5 比例闸只挡 <50% 阶段——backfill 从头重抓每 ~100 键 flush
    一次，watcher 采样一旦落在 50%-99% 窗口（new_ab ≥ 半数）比例闸放行，未抓完的
    ab: 厚向量仍被整批删掉再震荡重嵌。删除量独立闸（> max(20, 5%)）必须拒绝且厚向量
    原样保留。42 条抓回 21 条：new_ab=21 恰为半数（比例闸放行），ab_delete=21 > 20。"""
    import sqlite3 as _sq
    db = tmp_path / "e.sqlite3"
    idx, sidecar, partial_json = _abs_idx_and_sidecar(tmp_path, 42, 21)
    sync_store(db, idx, _FakeEmbedClient())                       # 42 ab: 首嵌
    sidecar.write_text(partial_json, encoding="utf-8")            # 中途产物：21/42
    with pytest.raises(VectorStoreError, match="abstract 级"):
        sync_store(db, idx, _FakeEmbedClient())
    conn = _sq.connect(str(db))
    try:
        n_ab = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE level='abstract'").fetchone()[0]
    finally:
        conn.close()
    assert n_ab == 42                                             # 厚向量未被删


def test_sync_allows_abstract_delete_at_floor_boundary(tmp_path):
    """删除量闸边界口径（严格大于才拦）：恰删 _AB_DELETE_FLOOR 条放行——
    零星合法删减（撤稿踢库/摘要修订）不被误伤。41 条保留 21 条：ab_delete=20 = 闸值。"""
    db = tmp_path / "e.sqlite3"
    idx, sidecar, partial_json = _abs_idx_and_sidecar(tmp_path, 41, 21)
    sync_store(db, idx, _FakeEmbedClient())
    sidecar.write_text(partial_json, encoding="utf-8")
    stats = sync_store(db, idx, _FakeEmbedClient())
    assert stats.deleted_abstract == 20                           # 恰在上限，放行
    assert stats.deleted == 20


def test_sync_refuses_when_abstracts_sidecar_corrupt(tmp_path):
    """abstracts.json 存在但写坏：sync_store 在单点加载处即拒绝（load_abstracts raise），
    不产生任何删除计划。"""
    import json as _json
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("o2025Bad", one_line="判词", dedup_key="doi:10.1/o")]}
    sidecar = tmp_path / ABSTRACTS_NAME
    sidecar.write_text(_json.dumps(
        {"abstracts": {"doi:10.1/o": {"abstract": "An abstract."}}}), encoding="utf-8")
    sync_store(db, idx, _FakeEmbedClient())
    sidecar.write_text("{truncated", encoding="utf-8")            # 模拟写坏
    with pytest.raises(VectorStoreError, match="读不出"):
        sync_store(db, idx, _FakeEmbedClient())


def test_sync_store_discovers_abstracts_sidecar_no_churn(tmp_path):
    """sync_store 按 db 同目录自动发现 abstracts.json（单点加载，入口零改动）；
    摘要落地 = 只新增 ab: chunk（paper 向量纹丝不动），第二次增量 0 嵌入——
    厚/瘦震荡是本批审核确认的 high 风险。"""
    import json as _json
    import sqlite3 as _sq
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("m2025Fat", one_line="判词", dedup_key="doi:10.1/m")]}
    sync_store(db, idx, _FakeEmbedClient())                       # 无摘要首嵌
    (tmp_path / ABSTRACTS_NAME).write_text(_json.dumps(
        {"abstracts": {"doi:10.1/m": {"abstract": "An abstract."}}}), encoding="utf-8")
    stats = sync_store(db, idx, _FakeEmbedClient())
    assert stats.embedded == 1                                    # 只新增 ab: chunk
    assert stats.embedded_paper == 0                              # paper 级零扰动
    stats2 = sync_store(db, idx, _FakeEmbedClient())
    assert (stats2.embedded, stats2.deleted) == (0, 0)            # 稳定，无震荡
    conn = _sq.connect(str(db))
    try:
        rows = dict(conn.execute("SELECT id, text FROM chunks"))
    finally:
        conn.close()
    assert rows["p:m2025Fat"] == "T\n判词"
    assert rows["ab:m2025Fat"] == "T\n判词\nAn abstract."


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


def test_library_neighbors_abstract_hit_maps_back_to_paper_one_line():
    """abstract chunk（ab:）命中：近邻注入必须反查该篇 paper 记录取判词——
    800 字摘要文本冒充判词进裁决 prompt 是本批审核确认的缺陷模式；
    同篇 paper+abstract 双命中只保留一条（按 citekey 去重取高分）。"""
    from types import SimpleNamespace
    from src.scholar.workflow import ScholarWorkflow

    meta = {"model": "bge-m3", "dim": "2"}
    records = [
        # 同一篇论文的两个 chunk：abstract 与 query 更相似（0.99），paper 稍低（0.9）
        {"level": "abstract", "citekey": "fat2026A", "year": 2026,
         "text": "A very long backfilled abstract that must stay out."},
        {"level": "paper", "citekey": "fat2026A", "year": 2026, "text": "Some Title\n人工判词"},
        # 只有 abstract 在库、paper 级残缺的条目：没法展示，必须被跳过
        {"level": "abstract", "citekey": "orphan2026B", "year": 2026,
         "text": "Orphan abstract without paper chunk."},
    ]
    v = np.array([[0.99, np.sqrt(1 - 0.9801)],
                  [0.90, np.sqrt(1 - 0.81)],
                  [0.80, np.sqrt(1 - 0.64)]], dtype=np.float32)
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

    seg = SimpleNamespace(segment_id=1, metadata=SimpleNamespace(title="q"), original_abstract="a")
    hits = wf._library_neighbors([seg], k=3, min_sim=0.65)[1]
    assert [h["citekey"] for h in hits] == ["fat2026A"]      # 双命中去重 + 孤儿摘要被跳过
    assert hits[0]["one_line"] == "人工判词"                  # 反查 paper 记录的判词
    assert hits[0]["sim"] == pytest.approx(0.99, abs=1e-3)   # 保留的是更高的 abstract 分
    assert "abstract" not in hits[0]["one_line"].lower()


def test_library_neighbors_self_hit_strips_one_line():
    """自命中：保留近邻本体（sim/citekey/year），只把 one_line 换成标记。

    Scholar 邮件臂每周重复推送已入库论文，而 seen 去重在 filter 之后，所以裁决时必然
    检索到论文自己。这条近邻**不能剔除**——prompt 靠它判「与已收文献重复 → MAYBE」，
    journal_screen 的 in_library 列也只读 sim；但 one_line 是当初人工写的裁决判词，
    喂回给裁决者就是自我背书，必须掐掉。
    """
    from types import SimpleNamespace
    from src.scholar.workflow import ScholarWorkflow

    meta = {"model": "bge-m3", "dim": "2"}
    records = [
        # 与被裁决论文同题（大小写/标点不同，_norm_title 后相等）→ 自命中
        {"level": "paper", "citekey": "self2025X", "year": 2025,
         "text": "Deep Learning for EHR!\n人工写的判词，不该被当独立佐证"},
        # 真·他篇，one_line 必须原样保留
        {"level": "paper", "citekey": "other2024Y", "year": 2024, "text": "别的题目\n他篇一句话"},
    ]
    v = np.array([[1, 0], [0.9, np.sqrt(1 - 0.81)]], dtype=np.float32)
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
    wf._paper_mask = np.array([True, True])
    wf._embedding_client = _Client()
    wf._library_neighbors_unavailable = False

    seg = SimpleNamespace(segment_id=3,
                          metadata=SimpleNamespace(title="deep learning for ehr"),
                          original_abstract="a")
    hits = wf._library_neighbors([seg], k=3, min_sim=0.65)[3]

    assert [h["citekey"] for h in hits] == ["self2025X", "other2024Y"]  # 自命中未被剔除
    assert hits[0]["one_line"].startswith("（本篇自身")                  # 判词被掐掉
    assert "人工写的判词" not in hits[0]["one_line"]
    assert hits[0]["sim"] == 1.0 and hits[0]["year"] == 2025            # sim/year 原样
    assert hits[1]["one_line"] == "他篇一句话"                           # 他篇不受影响


def test_library_neighbors_self_hit_keeps_in_library_signal():
    """journal_screen 的 in_library 只读 sim，掐 one_line 不得影响它。"""
    from src.scholar.journal_screen import IN_LIBRARY_SIM_THRESHOLD

    neighbors = [{"citekey": "self2025X", "year": 2025,
                  "one_line": "（本篇自身的在库记录，仅可用于判定重复，不是独立佐证）",
                  "sim": 0.93}]
    in_library = bool(neighbors and any(n.get("sim", 0) >= IN_LIBRARY_SIM_THRESHOLD
                                        for n in neighbors))
    assert in_library is True


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


# ---------------- 第2轮审查回归：abstract 掩码构造必须走真实加载路径 ----------------

def test_library_neighbors_real_load_path_mask_admits_abstract_chunks(tmp_path):
    """封堵假绿盲区：workflow 里唯一构造 _paper_mask 的分支（`_vector_store is None`
    时的 `level in ("paper", "abstract")`，workflow.py:760-762）此前零覆盖——既有的
    ab: 命中/自命中测试全部手工注入 `_paper_mask = np.array([True, ...])`，把表达式
    回退成 `== "paper"` 全量测试依旧全绿，而 digest 近邻的摘要向量召回（本批 P0 的
    核心收益）在生产中静默消失。本测试用 sync_store 建真库（p:/ab:/h: 三级 chunk
    齐备）后让 workflow 自己加载并构造掩码：
    - query 精确回放 ab: 向量 → 必须以 sim≈1.0 命中并反查回 paper 判词；
    - query 精确回放 highlight 向量 → 必须空手（掩码同时要挡住 highlight 级）。"""
    import json as _json
    from types import SimpleNamespace
    from src.scholar.embed_store import DB_NAME
    from src.scholar.workflow import ScholarWorkflow

    gen = "2026-08-21T09:00:00"
    db = tmp_path / DB_NAME
    (tmp_path / ABSTRACTS_NAME).write_text(_json.dumps(
        {"abstracts": {"doi:10.1/mask": {"abstract": "A thick recall abstract."}}}),
        encoding="utf-8")
    idx = {"generated_at": gen,
           "papers": [_paper("mask2026Fat", one_line="人工判词", dedup_key="doi:10.1/mask",
                             highlights=[_hl("citable", "a highlight sentence")])]}
    sync_store(db, idx, _FakeEmbedClient())              # source_generated_at=gen，新鲜度对齐
    (tmp_path / "literature_index.json").write_text(
        '{"schema_version": 4, "generated_at": "%s", "papers": []}' % gen, encoding="utf-8")

    loaded = VectorStore.load(db)
    vec_by_id = {rec["id"]: loaded.mat[i].copy() for i, rec in enumerate(loaded.records)}
    ab_vec = vec_by_id["ab:mask2026Fat"]
    hl_vec = next(v for cid, v in vec_by_id.items() if cid.startswith("h:"))

    class _ReplayClient:
        model = "bge-m3"
        def __init__(self, vecs):
            self._vecs = vecs
        def probe(self):
            return {"model": "bge-m3", "dim": int(ab_vec.shape[0])}
        def embed(self, texts, batch_size=64):
            assert len(texts) == len(self._vecs)
            return np.stack(self._vecs)

    wf = ScholarWorkflow.__new__(ScholarWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(notes_dir=tmp_path),
        llm=SimpleNamespace(embedding_model="bge-m3", embedding_base_url=None,
                            ollama_base_url=None))
    wf._library_neighbors_cache = {}
    wf._vector_store = None                              # 关键：不注入，逼生产分支自建掩码
    wf._paper_mask = None
    wf._embedding_client = _ReplayClient([ab_vec, hl_vec])
    wf._library_neighbors_unavailable = False

    segs = [SimpleNamespace(segment_id=1, metadata=SimpleNamespace(title="q1"),
                            original_abstract="a"),
            SimpleNamespace(segment_id=2, metadata=SimpleNamespace(title="q2"),
                            original_abstract="b")]
    out = wf._library_neighbors(segs, k=3, min_sim=0.999)   # 只有精确回放才够到阈值

    assert wf._library_neighbors_unavailable is False        # 全程没走静默降级
    # 掩码表达式本体：paper/abstract 放行、highlight 拦下，且三种 level 都真实在库
    levels = [r.get("level") for r in wf._vector_store.records]
    assert {"paper", "abstract", "highlight"} <= set(levels)
    assert [bool(m) for m in wf._paper_mask] == [lvl in ("paper", "abstract") for lvl in levels]
    # ab: 命中走通生产掩码，并反查 paper 记录取判词（摘要文本不外泄）
    assert [h["citekey"] for h in out[1]] == ["mask2026Fat"]
    assert out[1][0]["sim"] == pytest.approx(1.0, abs=1e-5)
    assert out[1][0]["one_line"] == "人工判词"
    # highlight 向量即使与 query 余弦为 1 也不得进近邻
    assert out[2] == []


# ---------------- sync_store_best_effort（F2 三处复制收敛） ----------------
# 契约：成功返回 SyncStats；任何异常绝不外抛——warning + notify 恰好一次后返回 None
# （向量库是索引的纯派生物，同步失败不允许影响调用方退出码）。

def _bes_settings():
    import types
    return types.SimpleNamespace(llm=types.SimpleNamespace(embedding_model="bge-m3"))


def _patch_embedding_client(monkeypatch):
    import types
    from src.scholar import embeddings as E

    closed = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(E, "EmbeddingClient", _Client)
    monkeypatch.setattr(E, "resolve_embedding_base_url", lambda llm: "http://localhost:11434")
    return closed


def test_sync_best_effort_success_returns_stats(monkeypatch, tmp_path):
    import types
    from src.scholar import embed_store as S
    from src.utils import notify as N
    closed = _patch_embedding_client(monkeypatch)
    stats = types.SimpleNamespace(embedded=3, deleted=1, meta_refreshed=2)
    monkeypatch.setattr(S, "sync_store", lambda *a, **kw: stats)
    calls = []
    monkeypatch.setattr(N, "notify", lambda *a: calls.append(a))
    out = S.sync_store_best_effort(tmp_path, {"papers": []}, _bes_settings(),
                                   notify_title="Scholar 周入库", context="入库")
    assert out is stats
    assert calls == []          # 成功不打扰人
    assert closed == [True]     # 连接必须关闭（此前删掉 finally: close() 测试照样绿）


def test_sync_best_effort_failure_notifies_once_and_swallows(monkeypatch, tmp_path):
    from src.scholar import embed_store as S
    from src.utils import notify as N
    closed = _patch_embedding_client(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("Ollama 不可达")

    monkeypatch.setattr(S, "sync_store", _boom)
    calls = []
    monkeypatch.setattr(N, "notify", lambda title, text: calls.append((title, text)))
    out = S.sync_store_best_effort(tmp_path, {"papers": []}, _bes_settings(),
                                   notify_title="Scholar 手动精读", context="手动精读归档")
    assert out is None
    assert len(calls) == 1
    title, text = calls[0]
    assert title == "Scholar 手动精读"
    assert "手动精读归档" in text and "Ollama 不可达" in text
    assert closed == [True]     # sync_store 抛异常也必须走到 finally 关连接


# ---------------- R1：撞键 / schema 版本闸 / 漂移告警 ----------------

def test_citekey_collision_warns_and_keeps_first(tmp_path):
    """两条 keeper 共用一个 citekey（fix_citekey_collisions 存在的理由）时，
    字典推导式是 last-wins：甲的 p:/ab: 被乙静默覆盖、total 少算，而甲的 highlight
    仍带该 citekey，被 workflow 的 paper_rec_by_ck 反查到乙的身份。"""
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [
        _paper("dup2025A", title="论文甲", one_line="甲的判词",
               highlights=[_hl("citable", "甲的证据句")]),
        _paper("dup2025A", title="论文乙", one_line="乙的判词",
               highlights=[_hl("citable", "乙的证据句")]),
    ]}
    buf, stop = _capture_warnings()
    try:
        stats = sync_store(db, idx, _FakeEmbedClient())
    finally:
        stop()
    log = "".join(buf)
    assert stats.total == 3, "撞键的第二份 p: 被丢弃，total 必须如实反映"
    assert "撞键" in log and "fix-collisions" in log
    import sqlite3 as _sq
    conn = _sq.connect(str(db))
    try:
        texts = dict(conn.execute("SELECT id, text FROM chunks WHERE level='paper'"))
    finally:
        conn.close()
    assert texts["p:dup2025A"].startswith("论文甲"), "保留先出现者，可复现"


def test_incremental_refuses_stale_schema_version(tmp_path):
    """_ensure_schema 只 CREATE IF NOT EXISTS、从不看版本，而收尾 _write_meta 无条件写
    当前版本——一次增量就把旧库静默盖章成新版，永久解除读侧 VectorStore.load 那道闸。"""
    import sqlite3 as _sq
    from src.scholar.embed_store import sync_store as _sync
    db = tmp_path / "e.sqlite3"
    idx = {"papers": [_paper("a2024A")]}
    _sync(db, idx, _FakeEmbedClient())
    conn = _sq.connect(str(db))
    try:
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(VectorStoreError, match="schema 版本"):
        _sync(db, idx, _FakeEmbedClient())
    conn = _sq.connect(str(db))
    try:
        ver = dict(conn.execute("SELECT key, value FROM meta"))["schema_version"]
    finally:
        conn.close()
    assert ver == "1", "拒绝之后不许把版本号盖成新的"


def test_orphan_abstract_delete_warns_and_names_backfill(tmp_path):
    """dedup_key 升级（backfill_pmlr_metadata 补 url）让 abstracts.json 的键成孤儿：
    该篇仍在索引里，厚向量却被当作"该删的"删掉，且没有任何自动入口补回。
    删除量小于 ab: 闸的放行窗口时它完全静默，日志只写"-1 删除"，读起来像正常增量。"""
    import json as _json
    db = tmp_path / "e.sqlite3"
    keys = ["title:k{}".format(i) for i in range(30)]
    (tmp_path / "abstracts.json").write_text(
        _json.dumps({"abstracts": {k: {"abstract": "abstract text {}".format(k)}
                                   for k in keys}}), encoding="utf-8")
    papers = [_paper("p2024K{}".format(i), dedup_key=keys[i]) for i in range(30)]
    s1 = sync_store(db, {"papers": papers}, _FakeEmbedClient())
    assert s1.embedded_abstract == 30
    # 一篇的键升级：条目还在索引里，只是 dedup_key 变了 → abstracts.get 查空
    papers[3] = _paper("p2024K3", dedup_key="pmlr:v297/new")
    buf, stop = _capture_warnings()
    try:
        s2 = sync_store(db, {"papers": papers}, _FakeEmbedClient())
    finally:
        stop()
    log = "".join(buf)
    assert s2.deleted_abstract == 1, "放行窗口内，闸不会拦——正是它静默的原因"
    assert "backfill_abstracts" in log and "p2024K3" in log


def test_orphan_warning_silent_when_paper_left_index(tmp_path):
    """条目整个离开索引（撤稿踢库/duplicate_of 归并）时删厚向量是正确的，不该报警。"""
    import json as _json
    db = tmp_path / "e.sqlite3"
    keys = ["title:k{}".format(i) for i in range(30)]
    (tmp_path / "abstracts.json").write_text(
        _json.dumps({"abstracts": {k: {"abstract": "abstract text {}".format(k)}
                                   for k in keys}}), encoding="utf-8")
    papers = [_paper("p2024K{}".format(i), dedup_key=keys[i]) for i in range(30)]
    sync_store(db, {"papers": papers}, _FakeEmbedClient())
    buf, stop = _capture_warnings()
    try:
        sync_store(db, {"papers": papers[:-1]}, _FakeEmbedClient())   # 最后一篇整个走了
    finally:
        stop()
    assert "backfill_abstracts" not in "".join(buf)
