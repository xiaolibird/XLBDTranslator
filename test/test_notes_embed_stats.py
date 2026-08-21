# -*- coding: utf-8 -*-
"""notes_embed --stats（_print_stats）回归。

锁一件事（2026-08-21 缺陷修复）：--stats 必须把 abstract 级 chunk 单列并计入
合计——此前只 COUNT paper/highlight 两级，回填摘要后 --stats 总数与 sync 输出
差整整一个 abstract 量级，观测器自身失真；同时锁 abstracts.json 的三态观测行
（不存在 / 正常条数 / 存在但损坏，损坏只提示不改退出码——sync 才硬拒）。
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.scholar.embed_store import ABSTRACTS_NAME, INDEX_NAME, sync_store  # noqa: E402


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "notes_embed", REPO / "scripts" / "notes_embed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_script()


class _FakeEmbedClient:
    model = "bge-m3"

    def embed(self, texts, batch_size=64):
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for i, t in enumerate(texts):
            h = hashlib.sha256(t.encode("utf-8")).digest()
            v = np.frombuffer(h[:4], dtype=np.uint8).astype(np.float32) + 1.0
            out[i] = v / np.linalg.norm(v)
        return out


def _paper(citekey, dedup_key, highlights=()):
    return {"citekey": citekey, "title": "T", "one_line": "判词",
            "highlights": list(highlights), "duplicate_of": None,
            "dedup_key": dedup_key, "year": 2025, "month": "2026-07",
            "series": "manual", "bucket": ["A"], "priority_tier": "high"}


def _build(tmp_path, with_abstract=True):
    """建厚库：1 paper → p: + ab: + h: 各一条；返回 (db_path, index_path)。"""
    idx = {"papers": [_paper("a2025X", "doi:10.1/a", highlights=[
        {"role": "citable", "tag": "x", "section": "方法与数据", "text": "s1"}])],
        "generated_at": "2026-01-01T00:00:00"}
    if with_abstract:
        (tmp_path / ABSTRACTS_NAME).write_text(json.dumps(
            {"abstracts": {"doi:10.1/a": {"abstract": "An abstract."}}}),
            encoding="utf-8")
    db = tmp_path / "e.sqlite3"
    sync_store(db, idx, _FakeEmbedClient())
    index_path = tmp_path / INDEX_NAME
    index_path.write_text(json.dumps(idx), encoding="utf-8")
    return db, index_path


def test_stats_counts_abstract_chunks_in_total(tmp_path, capsys):
    """abstract 级单列且计入合计（1 paper + 1 abstract + 1 highlight = 3）。"""
    db, index_path = _build(tmp_path)
    rc = MOD._print_stats(db, index_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "paper chunks = 1" in out
    assert "abstract chunks = 1" in out
    assert "highlight chunks = 1" in out
    assert "合计 = 3" in out
    assert "{} = 1 条摘要".format(ABSTRACTS_NAME) in out


def test_stats_reports_missing_sidecar_as_thin_mode(tmp_path, capsys):
    """无 sidecar：abstract chunks = 0，观测行明示瘦库模式。"""
    db, index_path = _build(tmp_path, with_abstract=False)
    rc = MOD._print_stats(db, index_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "abstract chunks = 0" in out
    assert "合计 = 2" in out
    assert "不存在" in out


def test_stats_warns_on_corrupt_sidecar_without_failing(tmp_path, capsys):
    """sidecar 写坏：--stats 仍出完整报告（退出码 0），但明示下次同步会被拒。"""
    db, index_path = _build(tmp_path)
    (tmp_path / ABSTRACTS_NAME).write_text("{truncated", encoding="utf-8")
    rc = MOD._print_stats(db, index_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "合计 = 3" in out
    assert "读不出" in out and "拒绝" in out
