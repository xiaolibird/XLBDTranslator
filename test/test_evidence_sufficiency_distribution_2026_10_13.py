# -*- coding: utf-8 -*-
"""WO-4b 分布判据纯函数测试：小夹具，不读真实数据、不联网。"""
import math

import numpy as np
import pytest

from src.scholar import coverage as C


def test_chance_exceedance_z_known_values():
    # N=1e6、期望 1 个越界 -> 单侧 1e-6 -> z≈4.753
    assert C.chance_exceedance_z(10 ** 6, 1.0) == pytest.approx(4.7534, abs=1e-3)
    # 期望越界数越大，z* 越低；N 越大，z* 越高
    assert C.chance_exceedance_z(10 ** 4, 4.0) < C.chance_exceedance_z(10 ** 4, 1.0)
    assert C.chance_exceedance_z(10 ** 3, 1.0) < C.chance_exceedance_z(10 ** 5, 1.0)


def test_chance_exceedance_z_degenerate_is_inf():
    assert math.isinf(C.chance_exceedance_z(0, 1.0))
    assert math.isinf(C.chance_exceedance_z(1, 1.0))     # 只有一个背景点：不可能「显著」
    assert math.isinf(C.chance_exceedance_z(100, 0.0))


def test_background_stats_and_empty():
    bg = C.background_stats(np.array([0.0, 1.0, 2.0, 3.0]))
    assert bg.n == 4 and bg.mean == pytest.approx(1.5) and bg.std == pytest.approx(math.sqrt(1.25))
    e = C.background_stats(np.array([]))
    assert e.n == 0 and e.mean == 0.0 and e.std == 0.0


def test_dynamic_threshold_floor_vs_dynamic():
    bg = C.BackgroundStats(n=10000, mean=0.40, std=0.05)
    z = C.chance_exceedance_z(10000, 1.0)
    thr, zz, binding = C.dynamic_threshold(bg, floor=0.55)
    assert zz == pytest.approx(z) and thr == pytest.approx(0.40 + z * 0.05) and not binding
    # 背景很低 -> 动态部分低于 floor -> floor 起作用
    low = C.BackgroundStats(n=10000, mean=0.20, std=0.03)
    thr2, _, binding2 = C.dynamic_threshold(low, floor=0.55)
    assert thr2 == 0.55 and binding2
    # 空背景退回 floor
    assert C.dynamic_threshold(C.BackgroundStats(0, 0.0, 0.0), floor=0.5) == (0.5, math.inf, True)


def test_generic_question_gets_higher_bar_than_niche():
    # 同样的 top 分数，背景高的（泛化问题）阈值更高 —— 动态判据存在的理由
    hi = C.BackgroundStats(10000, 0.45, 0.05)
    lo = C.BackgroundStats(10000, 0.35, 0.05)
    assert C.dynamic_threshold(hi, floor=0.5)[0] > C.dynamic_threshold(lo, floor=0.5)[0]


def test_source_max_scores_dedup_retracted_unknown():
    keeper_of = {"a2": "a1", "a1": "a1", "r": None}
    scores = [0.9, 0.7, 0.8, 0.95, 0.6]
    cks = ["a1", "a2", "b", "r", "unknown"]
    out = C.source_max_scores(scores, cks, keeper_of)
    # a1/a2 合并取 0.9；撤稿 r 不计；b 与 unknown 各自一篇
    assert out == [0.9, 0.8, 0.6]


def test_query_scores_max_over_queries_and_mask():
    mat = np.array([[1, 0], [0, 1], [0.6, 0.8]], dtype=np.float32)
    q = np.array([[1, 0], [0, 1]], dtype=np.float32)
    sc = C.query_scores(mat, q)
    assert np.allclose(sc, [1.0, 1.0, 0.8])
    sc_m = C.query_scores(mat, q, mask=np.array([False, True, True]))
    assert np.allclose(sc_m, [1.0, 0.8])
    # 单个 1 维 query 也接受
    assert np.allclose(C.query_scores(mat, np.array([1.0, 0.0])), [1.0, 0.0, 0.6])


def test_dynamic_sufficiency_refuse_and_pass():
    rng = np.random.default_rng(0)
    bulk = rng.normal(0.30, 0.04, 5000)
    # 无离群点：任何来源都过不了动态阈值 -> 拒答
    cks = ["p{}".format(i) for i in range(5000)]
    v = C.dynamic_sufficiency(bulk, cks, {}, floor=0.50, expected_chance=1.0, min_sources=3)
    assert v.refuse and v.n_sources < 3 and v.bg.n == 5000
    # 注入 4 个不同来源的显著离群点 -> 不拒答
    s2 = bulk.copy()
    s2[:4] = 0.80
    v2 = C.dynamic_sufficiency(s2, cks, {}, floor=0.50, expected_chance=1.0, min_sources=3)
    assert not v2.refuse and v2.n_sources >= 4 and v2.top1 == pytest.approx(0.80)
    # 同一来源的 4 个离群 chunk 只算 1 篇
    s3 = bulk.copy(); s3[:4] = 0.80
    cks3 = ["same"] * 4 + cks[4:]
    v3 = C.dynamic_sufficiency(s3, cks3, {}, floor=0.50, expected_chance=1.0, min_sources=3)
    assert v3.refuse and v3.n_sources == 1


def test_floor_blocks_far_query_tail():
    # 背景均值极低时动态阈值也很低；floor 保证低于 floor 的分数不算证据
    bg = C.BackgroundStats(10000, 0.20, 0.03)
    v = C.verdict_from_sources([0.45, 0.44, 0.43, 0.42], bg, floor=0.58, min_sources=3)
    assert v.refuse and v.floor_binding and v.n_sources == 0 and v.threshold == 0.58


def test_dynamic_grid_and_pick_prefer_zero_false_refusal():
    bg = C.BackgroundStats(10000, 0.40, 0.05)   # z*≈3.72 -> Tdyn≈0.586
    cases = [
        C.DistCase("pos1", "g", True, [0.70, 0.68, 0.66, 0.30], bg),
        C.DistCase("pos2", "g", True, [0.65, 0.64, 0.30], bg),
        C.DistCase("neg1", "g", False, [0.62, 0.61, 0.30], bg),
        C.DistCase("neg2", "g", False, [0.59, 0.58, 0.57], bg),
    ]
    rows = C.dynamic_grid_rates(cases, floors=(0.55, 0.60), es=(1.0,), ms=(2, 3))
    assert len(rows) == 4
    r = {(x["floor"], x["m"]): x for x in rows}
    assert r[(0.55, 2)]["false_refusals"] == 0 and r[(0.55, 2)]["missed_names"] == ["neg1"]
    assert r[(0.55, 3)]["false_refusals"] == 1 and r[(0.55, 3)]["false_refusal_names"] == ["pos2"]
    pick = C.pick_dynamic(rows)
    assert pick["false_refusals"] == 0
    # 余量：pos 的 n_sources - m 的最小值
    assert pick["margin"] is not None and pick["margin"] >= 0


def test_pick_dynamic_none_when_all_refuse_a_positive():
    bg = C.BackgroundStats(10000, 0.40, 0.05)
    cases = [C.DistCase("pos", "g", True, [0.3], bg), C.DistCase("neg", "g", False, [0.3], bg)]
    rows = C.dynamic_grid_rates(cases, (0.55,), (1.0,), (1,))
    assert C.pick_dynamic(rows) is None
