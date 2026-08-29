# -*- coding: utf-8 -*-
"""scripts/notes_search.py 排序量纲回归（不读真实向量库）。

锁 2026-08-21 双 chunk 改造引入过的跨级排序缺陷：dense 模式下 paper 侧
（_paper_side_hits）一度把 sort_score 换成 RRF 分（单 idx 上限 1/(RRF_K+1)≈0.016），
而 highlight 侧（_level_hits）的 sort_score 仍是余弦（过 min_score 后 ≥0.4），
主循环把两侧 sort_score 直接混排——paper 侧完美命中（余弦 0.95）恒定排在任何
弱 highlight 命中（余弦 0.41）之后。契约：dense 模式两侧 sort_score 必须同为余弦。

导入方式沿用 test_notes_query.py：pytest 时 repo root 在 sys.path，
scripts 作为隐式命名空间包可直接 import。
"""
import json
from pathlib import Path

import numpy as np
import pytest

import scripts.notes_search as ns
from src.scholar.embed_store import VectorStore


def _unit(x, y, z):
    v = np.array([x, y, z], dtype=np.float32)
    return v / np.linalg.norm(v)


QUERY = _unit(1.0, 0.0, 0.0)


def _rec(level, citekey):
    return {"id": "{}:{}".format(level, citekey), "level": level, "citekey": citekey,
            "role": None, "tag": None, "section": None, "month": "2026-08",
            "series": "auto", "tier": "mid", "bucket": [], "year": 2025,
            "has_full_text": True, "note_file": "n.md", "note_line": 1,
            "text": "t\no"}


@pytest.fixture()
def store():
    """4 行：alpha 的瘦(0.95)/厚(0.80) paper 侧 chunk、beta 的弱 highlight(0.41)、
    beta 的低分瘦 chunk(0.10，低于 min_score 0.4 应被滤掉)。"""
    cosines = [0.95, 0.80, 0.41, 0.10]
    records = [_rec("paper", "alpha"), _rec("abstract", "alpha"),
               _rec("highlight", "beta"), _rec("paper", "beta")]
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in cosines])
    return VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)


def _masks(store):
    base = np.ones(len(store.records), dtype=bool)
    level_arr = np.array([r["level"] for r in store.records])
    return base, level_arr


def test_dense_paper_side_sort_score_is_cosine(store):
    """核心回归：dense 下 paper 侧每条 hit 的 sort_score == 展示余弦，不是 RRF 分。"""
    base, level_arr = _masks(store)
    hits = ns._paper_side_hits(store, base, level_arr, "dense", QUERY, [], min_score=0.4)
    assert hits, "paper 侧应有命中"
    for _idx, score, kind, sort_score in hits:
        assert kind == "cosine"
        assert sort_score == pytest.approx(score)
        assert sort_score > 1.0 / (ns.RRF_K + 1) * 2  # 远超 RRF 量纲上限 2/(k+1)
    # 瘦 0.95 排厚 0.80 前；beta 瘦 chunk 0.10 被 min_score 滤掉
    assert [h[0] for h in hits] == [0, 1]


def test_dense_cross_level_ranking_paper_beats_weak_highlight(store):
    """失败场景复现：余弦 0.95 的 paper 命中必须排在余弦 0.41 的弱 highlight 命中之前
    （回归前 paper 侧 sort=RRF≤0.0328 < 0.41 恒败）。"""
    base, level_arr = _masks(store)
    paper_hits = ns._paper_side_hits(store, base, level_arr, "dense", QUERY, [], min_score=0.4)
    hl_hits = ns._level_hits(store, base & (level_arr == "highlight"), "dense",
                             QUERY, [], min_score=0.4)
    assert hl_hits and hl_hits[0][3] == pytest.approx(0.41, abs=1e-6)
    assert max(t[3] for t in paper_hits) > max(t[3] for t in hl_hits)


def test_paper_lanes_thin_reproduces_old_dense_behavior(store):
    """--paper-lanes thin 的'喂厚前行为精确复现'声明：dense 下须与旧实现
    （_level_hits 查 paper 掩码，sort=cosine）逐条一致。"""
    base, level_arr = _masks(store)
    thin = ns._paper_side_hits(store, base, level_arr, "dense", QUERY, [],
                               min_score=0.4, lanes="thin")
    legacy = ns._level_hits(store, base & (level_arr == "paper"), "dense",
                            QUERY, [], min_score=0.4)
    assert thin == legacy


def test_hybrid_paper_side_still_rrf(store):
    """hybrid 契约不受修复影响：sort_score 仍是 RRF 量纲（≤ 泳道数/(k+1)）。"""
    base, level_arr = _masks(store)
    hits = ns._paper_side_hits(store, base, level_arr, "hybrid", QUERY,
                               ["missing"], min_score=0.4)
    assert hits
    for _idx, _score, _kind, sort_score in hits:
        assert sort_score <= 3.0 / (ns.RRF_K + 1) + 1e-9


# ---------------------------------------------------------------------------
# 文档口径回归：--level 帮助与 scholar-search skill 不许退回瘦库描述
# （2026-08-21 第2轮审计：paper 侧默认双路含摘要后，帮助仍写"只查标题+一句话"、
#  SKILL.md 判重段落不提 match_source=abstract）
# ---------------------------------------------------------------------------

def test_level_help_describes_dual_lane_paper_search():
    """--level paper 默认（--paper-lanes both）连摘要厚路一起查，帮助不许再说
    "只查标题+一句话"。"""
    # parser 在 main() 内部构造，无法取实例——对源码断言（帮助文本就在源文件里）
    from pathlib import Path
    src = Path(ns.__file__).read_text(encoding="utf-8")
    assert "只查标题+一句话" not in src
    # 帮助必须同时提到摘要与 --paper-lanes，读者能追到双路语义
    lvl = src[src.index('"--level"'):src.index('"--min-score"')]
    assert "摘要" in lvl and "--paper-lanes" in lvl


def test_dense_branch_comment_honest_about_lane_bias():
    """dense 分支注释不许再称跨泳道分布错位「由 top_k 截断兜住」（2026-08-21 第3轮
    审计）：top_k(200) 远大于 limit(默认10)，独立截断只圈候选池，最终仍按原始余弦
    混排，偏置原样存在。注释与 --mode 帮助必须如实写偏置并把用户指向 hybrid。"""
    from pathlib import Path
    src = Path(ns.__file__).read_text(encoding="utf-8")
    # 虚假安全感措辞绝迹
    assert "截断兜住" not in src and "兜住" not in src
    # dense 分支（取最后一处 if mode=="dense"，即 _paper_side_hits 内）到 hybrid
    # 分支之间的注释必须写明偏置且指向 hybrid
    dense_block = src[src.rindex('if mode == "dense":'):src.index("# hybrid：")]
    assert "偏置" in dense_block and "hybrid" in dense_block
    # --mode 帮助同口径：dense 的偏置要写出来
    mode_help = src[src.index('"--mode"'):src.index('"--level"')]
    assert "偏置" in mode_help


def test_coverage_warning_not_stale():
    """覆盖面警告的句级覆盖数不许再度悄悄过期（2026-08-21 审计：写着 480 篇/23%，
    实测 668/2254≈30%）。三处口径一致性：
    1) 旧数字（480 篇/23%/508 篇）在三份文档里必须绝迹；
    2) docstring 必须带"实时数以 literature_index.json 为准"的免责口径；
    3) 若本机存在真实索引，按 embed_store.chunks_from_index 同口径重算，
       docstring 里的 N/M 比例与实测偏差超过 10 个百分点即判过期。"""
    import re
    from pathlib import Path

    docs = {
        "scripts/notes_search.py": Path(ns.__file__).read_text(encoding="utf-8"),
        "docs/scholar_notes_AGENTS.md":
            Path("docs/scholar_notes_AGENTS.md").read_text(encoding="utf-8"),
        "docs/skills/scholar-notes/SKILL.md":
            Path("docs/skills/scholar-notes/SKILL.md").read_text(encoding="utf-8"),
        "docs/skills/scholar-write/SKILL.md":
            Path("docs/skills/scholar-write/SKILL.md").read_text(encoding="utf-8"),
    }
    for name, text in docs.items():
        for stale in ("480 篇", "480篇", "508 篇", "占 keeper 的 24%"):
            assert stale not in text, "{} 仍含过期覆盖数：{}".format(name, stale)
    assert "literature_index.json 为准" in docs["scripts/notes_search.py"]

    m = re.search(r"（(\d+)/(\d+)\s*≈\s*(\d+)%", docs["scripts/notes_search.py"])
    assert m, "docstring 覆盖面警告须写成 N/M ≈ P% 格式，便于本测试比对"
    doc_n, doc_m = int(m.group(1)), int(m.group(2))

    from src.scholar.paths import repo_path
    from src.scholar.notes_index import load_index_file
    from src.scholar.embed_store import is_retracted
    data, err = load_index_file(repo_path("output/scholar_notes/literature_index.json"))
    if err:
        pytest.skip("本机无真实 literature_index.json，跳过实测比对")
    keepers = with_h = 0
    for e in data.get("papers") or []:
        if not isinstance(e, dict):
            continue
        ck = e.get("citekey")
        if e.get("duplicate_of") or not ck:
            continue
        if ck.startswith("MISSING-KEY-") or e.get("citekey_source") == "missing":
            continue
        if is_retracted(e):
            continue
        keepers += 1
        hl = e.get("highlights")
        if hl and any((h.get("text") or "").strip() for h in hl if isinstance(h, dict)):
            with_h += 1
    assert keepers > 0
    drift = abs(with_h / keepers - doc_n / doc_m)
    assert drift < 0.10, (
        "覆盖面警告已过期：docstring 写 {}/{}（{:.0%}），实测 {}/{}（{:.0%}）——"
        "请更新 scripts/notes_search.py 及两份文档".format(
            doc_n, doc_m, doc_n / doc_m, with_h, keepers, with_h / keepers))


def test_scholar_search_skill_doc_mentions_abstract_match_source():
    """SKILL.md 判重段落必须交代 match_source=abstract：agent 判"库里是否已有
    类似文献"时，摘要命中与标题命中的置信度不同，瘦库口径会误导裁决。"""
    from pathlib import Path
    text = Path("docs/skills/scholar-search/SKILL.md").read_text(encoding="utf-8")
    assert "match_source" in text
    assert "abstract" in text
    # 与 --level paper 的建议命令出现在同一份文档，且明确双路（摘要也参与）
    assert "--level paper" in text and "摘要" in text


# ---------------- R1：--min-score 在 hybrid 下同等约束 sparse 泳道 ----------------

def _gate_store():
    """两行：alpha 余弦 0.90（强命中）、noise 余弦 0.20（离题，但 BM25 词面命中）。"""
    records = [_rec("paper", "alpha"), _rec("paper", "noise")]
    records[0]["text"] = "missing data mechanism\n关于缺失机制"
    records[1]["text"] = "missing missing missing\n词面撞车但语义离题"
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in (0.90, 0.20)])
    return VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)


def test_hybrid_min_score_gates_sparse_lane():
    """BM25 单路命中此前无任何分数门槛却照样占 --limit 名额。真库实测：离题探针
    `--min-score 0.95` 修复前仍返回 108 篇，而标定档案写着硬离题探针 max sim 0.575。"""
    store = _gate_store()
    base, level_arr = _masks(store)
    all_cos = store.mat @ QUERY
    hits = ns._paper_side_hits(store, base, level_arr, "hybrid", QUERY,
                               ["missing"], 0.62, all_cos=all_cos)
    assert [store.records[i]["citekey"] for i, *_ in hits] == ["alpha"]


def test_hybrid_min_score_is_monotone():
    """反向单调是这条缺陷的根因：门槛越高，dense 泳道越小、无门槛的 BM25 命中占的
    名额越多，结果反而更脏。修复后结果集必须随门槛单调收缩。"""
    store = _gate_store()
    base, level_arr = _masks(store)
    all_cos = store.mat @ QUERY
    def _keys(ms):
        return {store.records[i]["citekey"] for i, *_ in ns._paper_side_hits(
            store, base, level_arr, "hybrid", QUERY, ["missing"], ms, all_cos=all_cos)}
    lo, hi = _keys(0.1), _keys(0.7)
    assert hi <= lo and lo - hi == {"noise"}


def test_sparse_mode_unaffected_by_min_score():
    """--mode sparse 在 _paper_side_hits 里**直接早返回**，_gate_sparse 一次都不会被调用。
    这条钉的是「早返回路径不受门槛影响」，不是 all_cos=None 放行（那条见
    test_gate_sparse_passthrough_without_cosines 与 test_level_hits_sparse_lane_gated）。"""
    store = _gate_store()
    base, level_arr = _masks(store)
    hits = ns._paper_side_hits(store, base, level_arr, "sparse", None,
                               ["missing"], 0.95, all_cos=None)
    assert {store.records[i]["citekey"] for i, *_ in hits} == {"alpha", "noise"}


def test_gate_sparse_passthrough_without_cosines():
    assert ns._gate_sparse([(0, 3.2), (1, 1.1)], None, 0.9) == [(0, 3.2), (1, 1.1)]


def test_level_hits_sparse_lane_gated():
    """R3 变异发现:paper 侧的 gate 有 3 条测试,**highlight 侧一条都没有**——
    而 highlight 泳道正是 --cite 取句级证据那条。"""
    records = [_rec("highlight", "alpha"), _rec("highlight", "noise")]
    records[0]["text"] = "missing data mechanism 缺失机制"
    records[1]["text"] = "missing missing missing 词面撞车但语义离题"
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in (0.90, 0.20)])
    store = VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)
    mask = np.ones(2, dtype=bool)
    all_cos = store.mat @ QUERY
    hits = ns._level_hits(store, mask, "hybrid", QUERY, ["missing"], 0.62, all_cos=all_cos)
    assert [store.records[i]["citekey"] for i, *_ in hits] == ["alpha"]


# ---------------- R1：展示分取篇级最高余弦 / score_from 按来源记账 / JSON 导出 ----------------

def _run_main(monkeypatch, capsys, store, argv, min_score="0.4"):
    """驱动 main()：桩掉配置加载、库加载与 query 嵌入，只测聚组/展示/输出这一段。

    恒传 --no-rerank：重排默认 auto（hybrid 开），不关掉的话本组测试的结果顺序会
    取决于跑测试的机器上有没有 bge-reranker 模型缓存——测试结果依赖机器状态是
    不可接受的。重排行为有自己的专属测试组（下方「reranker 重排」节，全部打桩）。
    """
    import sys as _sys
    from types import SimpleNamespace
    monkeypatch.setattr(ns, "load_scholar_settings", lambda cfg, **kw: SimpleNamespace(
        processing=SimpleNamespace(notes_dir=Path(".")),
        llm=SimpleNamespace(embedding_model="test")))
    monkeypatch.setattr(ns, "_load_store", lambda db, m: (store, None))
    monkeypatch.setattr(ns, "_freshness_hint", lambda s, d: None)
    class _C:
        model = "test"
        def embed(self, texts): return np.stack([QUERY])
        def close(self): pass
    monkeypatch.setattr(ns, "EmbeddingClient", lambda **kw: _C())
    monkeypatch.setattr(ns, "resolve_embedding_base_url", lambda llm: "http://x")
    monkeypatch.setattr(_sys, "argv", ["notes_search.py"] + argv
                        + ["--min-score", min_score, "--no-rerank"])
    rc = ns.main()
    return rc, capsys.readouterr().out


def _tie_store():
    """alpha 的瘦 chunk 余弦 0.66、厚 chunk 0.61，但**厚泳道赢 BM25**（词面重复更多）
    → 厚的 RRF 更高、成为排序代表。此时按 RRF 选展示代表就会展示 0.61 而非 0.66，
    正是真库里 1898 次低报的形态。"""
    records = [_rec("paper", "alpha"), _rec("abstract", "alpha")]
    records[0]["text"] = "Thin title\n瘦判词 filler filler filler filler"
    records[1]["text"] = "Thin title thin title thin title\n判词\nthin title abstract"
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in (0.66, 0.61)])
    return VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)


def test_paper_display_score_is_max_cosine_across_lanes(monkeypatch, capsys):
    """真库实测 1898/28076 个 (query,citekey) 对被低报、最大低报 0.080，其中 72 例
    展示 <0.62 而真实 ≥0.62——正好落在 skill 判重线两侧。"""
    from pathlib import Path as _P
    globals().setdefault("Path", _P)
    rc, out = _run_main(monkeypatch, capsys, _tie_store(), ["thin", "title", "--json"])
    data = json.loads(out)
    assert rc == 0
    row = data["results"][0]
    assert row["score"] == pytest.approx(0.66, abs=1e-3), "必须是篇级最高余弦，不是 RRF 代表那条"
    assert row["match_source"] == "title"      # 展示分来自瘦泳道
    assert row["score_from"] == "paper"


def test_json_exposes_score_from(monkeypatch, capsys):
    """skill 推荐的正是 --json，而 ≥0.62 判据只对 score_from=="paper" 成立。"""
    from pathlib import Path as _P
    globals().setdefault("Path", _P)
    _rc, out = _run_main(monkeypatch, capsys, _tie_store(), ["thin", "title", "--json"])
    row = json.loads(out)["results"][0]
    assert "score_from" in row and row["score_from"] in ("paper", "highlight")


def test_score_from_consistent_with_no_sentence_evidence(monkeypatch, capsys):
    """永久不变量：一条根本没有句级命中的行，score_from 不许是 "highlight"
    （此前纯关键词命中的 paper 行 paper_cos 恒为 None，反推一律落到 highlight，
    于是人读输出给它打「句」标记，与紧邻的"该篇无精读句级证据"直接打架）。"""
    from pathlib import Path as _P
    globals().setdefault("Path", _P)
    store = _gate_store()
    _rc, out = _run_main(monkeypatch, capsys, store,
                         ["missing", "--json", "--mode", "sparse"], min_score="0.0")
    for row in json.loads(out)["results"]:
        if row["no_sentence_evidence"]:
            assert row["score_from"] != "highlight", row


# ---------------- 两字母缩写（对标审计 ⚠️-2）----------------

def test_bm25_tokenize_keeps_two_letter_acronyms():
    """vault.tokenize 的 `[a-z]{3,}` 把 EM/MI/IV 整个丢掉，BM25 泳道对缩写失明：
    `EM algorithm` 退化成只查 `algorithm`（真库实测 top10 全是算法选择类文献，
    与 dense top5 零交集）。三字母及以上本就被收着，不该重复补。"""
    assert "em" in ns.bm25_tokenize("EM algorithm")
    assert "mi" in ns.bm25_tokenize("MI vs RI imputation")
    assert "iv" in ns.bm25_tokenize("MIMIC-IV benchmark")
    # 三字母以上不重复补：EHR 只应出现一次（来自 [a-z]{3,}），不该被缩写分支再加一份
    assert ns.bm25_tokenize("EHR data").count("ehr") == 1


def test_bm25_tokenize_ignores_single_letters():
    """恰好两字母才收：单字母（公式里的 a/b/c/n/k）噪音太大，收进来等于给 BM25 灌词。"""
    toks = ns.bm25_tokenize("a b c EM x")
    assert "em" in toks
    assert not any(len(t) == 1 for t in toks)


def test_bm25_tokenize_is_case_insensitive():
    """R1-C 锚：大小写必须产出同一套 token。

    初版只补 `[A-Z]{2}` 时，`of` 仅在全大写标题里才被收，df 被压到 12 → IDF 7.50
    比真缩写 `em` 的 6.16 还高，于是 `MODELS OF CARE` 比 `models of care` 凭空多召
    一篇靠 `of` 命中的无关文献。改回只认大写就会重现这条假阳性。
    """
    assert ns.bm25_tokenize("MODELS OF CARE") == ns.bm25_tokenize("models of care")
    assert ns.bm25_tokenize("EM algorithm") == ns.bm25_tokenize("em algorithm")


def test_bm25_tokenize_lowercase_acronym_query_works(monkeypatch, capsys):
    """R1-D 锚：小写打字的用户也得吃到缩写召回。

    初版只补大写，`em algorithm` 的 hybrid top5 原封不动还是修复前的病态结果
    （全是 Algorithm Selection 泛词命中）——修复只对按了 shift 的人生效。
    """
    from pathlib import Path as _P
    globals().setdefault("Path", _P)
    records = [_rec("paper", "emPaper"), _rec("paper", "genericPaper")]
    records[0]["text"] = "Causal discovery\nEM algorithm 联合插补与结构学习"
    records[1]["text"] = "Algorithm selection survey\nalgorithm algorithm algorithm 算法选择"
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in (0.50, 0.50)])
    store = VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)
    _rc, out = _run_main(monkeypatch, capsys, store,
                         ["em", "algorithm", "--json", "--mode", "sparse"], min_score="0.0")
    assert json.loads(out)["results"][0]["citekey"] == "emPaper"


def test_bm25_tokenize_boundaries():
    """两字母只在**独立成词**时才收：粘在词里的不算，标点/连字符/中文都算边界。

    `AImodel` 切出 `ai` 会把 AI 灌进一切含该字母对的词；反过来 `EM-algorithm`、
    `(EM)`、`EM's`、中文夹用的 `用EM法` 都是真·独立缩写，必须收得到。
    """
    assert "ai" not in ns.bm25_tokenize("AImodel")          # 粘在词里，不切
    assert "em" in ns.bm25_tokenize("EM-algorithm")         # 连字符是边界
    assert "em" in ns.bm25_tokenize("(EM)")                 # 括号是边界
    assert "em" in ns.bm25_tokenize("EM's")                 # 撇号是边界
    assert "em" in ns.bm25_tokenize("用EM法做插补")           # 中文是边界
    for junk in ("", " ", "a", "A B", "123", "R²", "🙂", "\n\t"):
        assert ns.bm25_tokenize(junk) == [] or all(len(t) > 1 for t in ns.bm25_tokenize(junk))


def test_bm25_tokenize_superset_of_vault_tokenize():
    """契约：只做加法。vault.tokenize 还供着近邻图与 qa 词面查重，
    BM25 侧的增强绝不能反过来改掉那两处已标定的相似度分布。"""
    from src.scholar.vault import tokenize as vault_tokenize
    text = "EM algorithm 缺失机制 with EHR data"
    assert vault_tokenize(text) == ns.bm25_tokenize(text)[:len(vault_tokenize(text))]


def test_bm25_acronym_query_beats_generic_word(monkeypatch, capsys):
    """端到端：查 "EM algorithm" 时，真讲 EM 的那篇必须压过只含 algorithm 的泛词篇。
    修复前 query 只剩 `algorithm`，两篇 BM25 分数由词频决定，泛词篇反而赢。"""
    from pathlib import Path as _P
    globals().setdefault("Path", _P)
    records = [_rec("paper", "emPaper"), _rec("paper", "genericPaper")]
    records[0]["text"] = "Causal discovery\nEM algorithm 联合插补与结构学习"
    records[1]["text"] = "Algorithm selection survey\nalgorithm algorithm algorithm 算法选择"
    mat = np.stack([_unit(c, np.sqrt(1 - c * c), 0.0) for c in (0.50, 0.50)])
    store = VectorStore(meta={"model": "test", "dim": "3"}, records=records, mat=mat)
    _rc, out = _run_main(monkeypatch, capsys, store,
                         ["EM", "algorithm", "--json", "--mode", "sparse"], min_score="0.0")
    assert json.loads(out)["results"][0]["citekey"] == "emPaper"
