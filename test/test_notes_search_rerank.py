# -*- coding: utf-8 -*-
"""notes_search --rerank 重排层回归（reranker 全程打桩，不载真模型）。

契约（docs/decisions/rerank_hyde_experiment_2026-08.md 落地形态）：
1. 默认 auto：hybrid 开、dense/sparse 关——dense 的「按余弦分看排名」是
   scholar-search 判重流程的依赖，默认重排会破坏它。
2. 纯重排：不改集合成员、不动余弦展示分；--min-score 过滤发生在重排之前。
3. 降级铁律：reranker 不可用时按原排序输出、退出码不变，绝不让重排层挡住检索主路径。
4. JSON 契约：reranked 顶层布尔 + 每行 rerank_score（仅重排时出现）；score 字段
   语义不变（判重流程读它，与顺序无关）。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import scripts.notes_search as ns
from src.scholar import reranker
from src.scholar.embed_store import VectorStore


def _unit(x, y, z):
    v = np.array([x, y, z], dtype=np.float32)
    return v / np.linalg.norm(v)


QUERY = _unit(1.0, 0.0, 0.0)


def _rec(level, citekey, text="t\no"):
    return {"id": "{}:{}".format(level, citekey), "level": level, "citekey": citekey,
            "role": None, "tag": None, "section": None, "month": "2026-08",
            "series": "auto", "tier": "mid", "bucket": [], "year": 2025,
            "has_full_text": True, "note_file": "n.md", "note_line": 1,
            "text": text}


def _store():
    """三篇：alpha 余弦 0.9 > beta 0.7 > gamma 0.5；gamma 另有厚(ab:)chunk。"""
    records = [_rec("paper", "alpha", "Alpha title\nalpha 判词"),
               _rec("paper", "beta", "Beta title\nbeta 判词"),
               _rec("paper", "gamma", "Gamma title\ngamma 判词"),
               _rec("abstract", "gamma", "Gamma title\ngamma 判词\ngamma abstract text")]
    cosines = [0.90, 0.70, 0.50, 0.45]
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in cosines])
    return VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)


def _run(monkeypatch, capsys, argv, store=None, entry=None):
    import sys as _sys
    monkeypatch.setattr(ns, "load_scholar_settings", lambda cfg, **kw: SimpleNamespace(
        processing=SimpleNamespace(notes_dir=Path(".")),
        llm=SimpleNamespace(embedding_model="test")))
    monkeypatch.setattr(ns, "_load_store", lambda db, m: (store or _store(), None))
    monkeypatch.setattr(ns, "_freshness_hint", lambda s, d: None)

    class _C:
        model = "test"
        def embed(self, texts): return np.stack([QUERY])
        def close(self): pass
    monkeypatch.setattr(ns, "EmbeddingClient", lambda **kw: _C())
    monkeypatch.setattr(ns, "resolve_embedding_base_url", lambda llm: "http://x")
    monkeypatch.setattr(_sys, "argv", ["notes_search.py"] + argv)
    rc = (entry or ns.main)()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _stub_scores(monkeypatch, mapping):
    """按 doc 文本首词（citekey 派生）给分；记录收到的 (query, docs) 供断言。"""
    calls = []

    def fake(query, docs):
        calls.append((query, list(docs)))
        return [mapping[d.split(" ", 1)[0].lower()] for d in docs]
    monkeypatch.setattr(ns.reranker, "rerank_scores", fake)
    return calls


def _stub_unavailable(monkeypatch):
    def fake(query, docs):
        raise reranker.RerankUnavailable("no model in cache")
    monkeypatch.setattr(ns.reranker, "rerank_scores", fake)


def _stub_never_called(monkeypatch):
    def fake(query, docs):
        raise AssertionError("rerank_scores 不该被调用")
    monkeypatch.setattr(ns.reranker, "rerank_scores", fake)


# ---------------- 默认 auto：hybrid 开、dense/sparse 关 ----------------

def test_hybrid_reranks_by_default(monkeypatch, capsys):
    calls = _stub_scores(monkeypatch, {"alpha": -1.0, "beta": 5.0, "gamma": 2.0})
    rc, out, _err = _run(monkeypatch, capsys, ["判词", "--json", "--min-score", "0.4"])
    data = json.loads(out)
    assert rc == 0 and data["reranked"] is True
    assert [r["citekey"] for r in data["results"]] == ["beta", "gamma", "alpha"]
    assert len(calls) == 1

def test_dense_does_not_rerank_by_default(monkeypatch, capsys):
    _stub_never_called(monkeypatch)
    rc, out, _err = _run(monkeypatch, capsys,
                         ["判词", "--json", "--mode", "dense", "--min-score", "0.4"])
    data = json.loads(out)
    assert rc == 0 and data["reranked"] is False
    assert [r["citekey"] for r in data["results"]] == ["alpha", "beta", "gamma"]

def test_sparse_does_not_rerank_by_default(monkeypatch, capsys):
    _stub_never_called(monkeypatch)
    _rc, out, _err = _run(monkeypatch, capsys,
                          ["判词", "--json", "--mode", "sparse", "--min-score", "0.0"])
    assert json.loads(out)["reranked"] is False

def test_explicit_rerank_overrides_dense_default(monkeypatch, capsys):
    _stub_scores(monkeypatch, {"alpha": 0.0, "beta": 9.0, "gamma": 1.0})
    _rc, out, _err = _run(monkeypatch, capsys,
                          ["判词", "--json", "--mode", "dense", "--rerank",
                           "--min-score", "0.4"])
    data = json.loads(out)
    assert data["reranked"] is True
    assert data["results"][0]["citekey"] == "beta"

def test_no_rerank_flag_disables_hybrid(monkeypatch, capsys):
    _stub_never_called(monkeypatch)
    _rc, out, _err = _run(monkeypatch, capsys,
                          ["判词", "--json", "--no-rerank", "--min-score", "0.4"])
    assert json.loads(out)["reranked"] is False


# ---------------- 纯重排契约：分数/集合/门槛语义不变 ----------------

def test_rerank_preserves_cosine_scores_and_set(monkeypatch, capsys):
    """重排只动顺序：每行 score 仍是原余弦、成员集合不变、rerank_score 单独记账。"""
    _stub_scores(monkeypatch, {"alpha": -1.0, "beta": 5.0, "gamma": 2.0})
    _rc, out, _err = _run(monkeypatch, capsys, ["判词", "--json", "--min-score", "0.4"])
    data = json.loads(out)
    by_ck = {r["citekey"]: r for r in data["results"]}
    assert set(by_ck) == {"alpha", "beta", "gamma"}
    assert by_ck["alpha"]["score"] == pytest.approx(0.90, abs=1e-3)
    assert by_ck["beta"]["score"] == pytest.approx(0.70, abs=1e-3)
    assert all("rerank_score" in r for r in data["results"])
    # rerank 分按序单调（排序键正确接线）
    rs = [r["rerank_score"] for r in data["results"]]
    assert rs == sorted(rs, reverse=True)

def test_min_score_gates_before_rerank(monkeypatch, capsys):
    """门槛在重排前按余弦执行：gamma(0.5) 被 0.6 滤掉后，reranker 根本见不到它。"""
    calls = _stub_scores(monkeypatch, {"alpha": 1.0, "beta": 2.0, "gamma": 99.0})
    _rc, out, _err = _run(monkeypatch, capsys, ["判词", "--json", "--min-score", "0.6"])
    data = json.loads(out)
    assert {r["citekey"] for r in data["results"]} == {"alpha", "beta"}
    assert len(calls[0][1]) == 2

def test_rerank_uses_thick_chunk_text_when_available(monkeypatch, capsys):
    """doc 文本优先 ab: 厚 chunk（gamma 有），无摘要篇（alpha/beta）退瘦文本——与试验口径一致。"""
    calls = _stub_scores(monkeypatch, {"alpha": 3.0, "beta": 2.0, "gamma": 1.0})
    _run(monkeypatch, capsys, ["判词", "--json", "--min-score", "0.4"])
    docs = calls[0][1]
    gamma_doc = next(d for d in docs if d.lower().startswith("gamma"))
    assert "abstract text" in gamma_doc, "gamma 该用厚 chunk 文本"
    alpha_doc = next(d for d in docs if d.lower().startswith("alpha"))
    assert alpha_doc == "Alpha title\nalpha 判词"

def test_single_result_skips_rerank(monkeypatch, capsys):
    _stub_never_called(monkeypatch)
    _rc, out, _err = _run(monkeypatch, capsys, ["判词", "--json", "--min-score", "0.8"])
    data = json.loads(out)
    assert [r["citekey"] for r in data["results"]] == ["alpha"]
    assert data["reranked"] is False


# ---------------- 降级铁律 ----------------

def test_unavailable_falls_back_to_original_order(monkeypatch, capsys):
    _stub_unavailable(monkeypatch)
    rc, out, err = _run(monkeypatch, capsys, ["判词", "--json", "--min-score", "0.4"])
    data = json.loads(out)
    assert rc == 0
    assert data["reranked"] is False
    assert [r["citekey"] for r in data["results"]] == ["alpha", "beta", "gamma"]
    assert all("rerank_score" not in r for r in data["results"])
    assert "reranker 不可用" in err

def test_unavailable_human_mode_no_rerank_marker(monkeypatch, capsys):
    _stub_unavailable(monkeypatch)
    rc, out, _err = _run(monkeypatch, capsys, ["判词", "--min-score", "0.4"])
    assert rc == 0
    assert "已重排" not in out

def test_human_mode_marks_rerank(monkeypatch, capsys):
    _stub_scores(monkeypatch, {"alpha": 1.0, "beta": 2.0, "gamma": 3.0})
    _rc, out, _err = _run(monkeypatch, capsys, ["判词", "--min-score", "0.4"])
    assert "已重排" in out


# ---------------- reranker 模块自身 ----------------

def test_reranker_unavailable_error_is_runtime_error():
    assert issubclass(reranker.RerankUnavailable, RuntimeError)

def test_rerank_cap_constant_sane():
    assert 10 <= ns.RERANK_CAP <= 1000


# ---------------- --cite 路径经过重排 ----------------

def test_cite_output_follows_rerank_order(monkeypatch, capsys):
    """--cite 在重排之后取 shown：引用串顺序 = rerank 序，而非余弦序。"""
    _stub_scores(monkeypatch, {"alpha": -1.0, "beta": 5.0, "gamma": 2.0})
    rc, out, _err = _run(monkeypatch, capsys,
                         ["判词", "--cite", "--min-score", "0.4"])
    assert rc == 0
    assert out.strip() == "[@beta; @gamma; @alpha]"

def test_cite_unavailable_degrades_to_original_order(monkeypatch, capsys):
    """--cite 下 reranker 不可用同样走降级铁律：原序输出、rc=0、stderr 提示。"""
    _stub_unavailable(monkeypatch)
    rc, out, err = _run(monkeypatch, capsys,
                        ["判词", "--cite", "--min-score", "0.4"])
    assert rc == 0
    assert out.strip() == "[@alpha; @beta; @gamma]"
    assert "reranker 不可用" in err


# ---------------- --limit 0 + 超过 RERANK_CAP 的截断拼接 ----------------

def _big_store(n=105):
    """n 篇 paper 级，余弦严格递减：p000 最高。全部高于 0.4 门槛。"""
    records, cosines = [], []
    for i in range(n):
        ck = "p{:03d}".format(i)
        records.append(_rec("paper", ck, "{} title\n判词{}".format(ck.upper(), i)))
        cosines.append(0.94 - i * 0.005)
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in cosines])
    return VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)


def test_limit0_over_cap_reranks_head_and_appends_tail(monkeypatch, capsys):
    """--limit 0 命中 105 条：只有前 RERANK_CAP=100 条送 reranker；重排后的 head
    与保持原序的 tail 拼接；stderr 打截断提示；tail 行无 rerank_score。"""
    calls = []

    def fake(query, docs):
        calls.append((query, list(docs)))
        return [float(i) for i in range(len(docs))]  # 递增分 → 重排后 head 恰好反序
    monkeypatch.setattr(ns.reranker, "rerank_scores", fake)

    rc, out, err = _run(monkeypatch, capsys,
                        ["判词", "--json", "--mode", "dense", "--rerank",
                         "--limit", "0", "--min-score", "0.4"],
                        store=_big_store(105))
    data = json.loads(out)
    assert rc == 0 and data["reranked"] is True and len(data["results"]) == 105
    # 截断：reranker 只见到前 100 条（按原余弦序）
    assert len(calls) == 1 and len(calls[0][1]) == ns.RERANK_CAP
    cks = [r["citekey"] for r in data["results"]]
    orig = ["p{:03d}".format(i) for i in range(105)]
    # head 反序（rerank 分递增 + 降序排 = 反转），tail 保原序拼在后面
    assert cks[:100] == list(reversed(orig[:100]))
    assert cks[100:] == orig[100:]
    # head 有 rerank_score，tail 没有
    assert all("rerank_score" in r for r in data["results"][:100])
    assert all("rerank_score" not in r for r in data["results"][100:])
    assert "仅前 {} 条参与重排".format(ns.RERANK_CAP) in err


def test_limit0_at_cap_no_truncation_notice(monkeypatch, capsys):
    """恰好 100 条：全部参与重排，不打截断提示。"""
    monkeypatch.setattr(ns.reranker, "rerank_scores",
                        lambda q, docs: [float(i) for i in range(len(docs))])
    rc, out, err = _run(monkeypatch, capsys,
                        ["判词", "--json", "--mode", "dense", "--rerank",
                         "--limit", "0", "--min-score", "0.4"],
                        store=_big_store(100))
    data = json.loads(out)
    assert rc == 0 and data["reranked"] is True and len(data["results"]) == 100
    assert "参与重排" not in err


# ---------------- 非 RerankUnavailable 异常 → traceback 可视 + 降级（F1 裁决）----------------
# 初版实现让打分期异常穿透到 cli_entry exit 4，被对抗审计判为铁律 3 违反：检索本身
# 已成功、结果在手，exit 4 把它们全丢，还连坐杀死 rag_bench 整轮（它把 4 当崩溃）。
# 现契约：traceback 打全（可视性）+ 按原排序降级（主路径）+ 退出码不变。

def test_rerank_internal_error_degrades_with_traceback(monkeypatch, capsys):
    def fake(query, docs):
        raise ValueError("scoring exploded mid-batch")
    monkeypatch.setattr(ns.reranker, "rerank_scores", fake)
    rc, out, err = _run(monkeypatch, capsys,
                        ["判词", "--json", "--min-score", "0.4"],
                        entry=ns.cli_entry)
    assert rc == 0
    data = json.loads(out)
    assert data["reranked"] is False
    assert [r["citekey"] for r in data["results"]] == ["alpha", "beta", "gamma"]
    assert all("rerank_score" not in r for r in data["results"])
    assert "scoring exploded mid-batch" in err        # traceback 可视
    assert "打分异常" in err and "按原排序输出" in err


def test_rerank_nan_scores_degrade(monkeypatch, capsys):
    """非有限分数（MPS 数值故障形态）：不排序、不写 rerank_score、警告降级。
    NaN 参与 sort 会静默乱序，json.dumps 会吐非标准 NaN token——必须整批弃用。"""
    monkeypatch.setattr(ns.reranker, "rerank_scores",
                        lambda q, docs: [float("nan")] + [1.0] * (len(docs) - 1))
    rc, out, err = _run(monkeypatch, capsys, ["判词", "--json", "--min-score", "0.4"])
    assert rc == 0
    data = json.loads(out)
    assert data["reranked"] is False
    assert [r["citekey"] for r in data["results"]] == ["alpha", "beta", "gamma"]
    assert all("rerank_score" not in r for r in data["results"])
    assert "非有限分数" in err


# ---------------- JSON 契约：reranked 恒在 / 无重排时无 rerank_score ----------------

@pytest.mark.parametrize("argv", [
    ["判词", "--json", "--mode", "dense", "--min-score", "0.4"],
    ["判词", "--json", "--mode", "sparse", "--min-score", "0.0"],
    ["判词", "--json", "--no-rerank", "--min-score", "0.4"],
])
def test_json_no_rerank_has_flag_false_and_no_rerank_score(monkeypatch, capsys, argv):
    """未重排的每一条路径：reranked 顶层字段恒存在且为 False；
    任何一行都不得出现 rerank_score 字段。"""
    _stub_never_called(monkeypatch)
    _rc, out, _err = _run(monkeypatch, capsys, argv)
    data = json.loads(out)
    assert "reranked" in data and data["reranked"] is False
    assert data["results"], "前置：得有命中才谈得上字段断言"
    assert all("rerank_score" not in r for r in data["results"])

def test_json_reranked_field_present_when_no_hits(monkeypatch, capsys):
    """真无命中（exit 1）也要打印完整 JSON——reranked 字段不能因空集缺席
    （rag_bench 对 exit 1 强校验 stdout 是合法 JSON）。"""
    _stub_never_called(monkeypatch)
    rc, out, _err = _run(monkeypatch, capsys,
                         ["判词", "--json", "--min-score", "0.99"])
    data = json.loads(out)
    assert rc == 1 and data["results"] == []
    assert "reranked" in data and data["reranked"] is False
