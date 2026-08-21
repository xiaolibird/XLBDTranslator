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
