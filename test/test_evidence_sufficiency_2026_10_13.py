# -*- coding: utf-8 -*-
"""WO-4：独立来源计数与拒答阈值网格（纯函数，小夹具，不碰网络与真实库）。"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar import coverage as C          # noqa: E402
from src.scholar import notes_index as NI      # noqa: E402


@dataclass
class Ev:
    citekey: str
    score: float


def _index():
    return {"papers": [
        {"citekey": "a2020", "dedup_key": "doi:1", "month": "2020-01", "duplicate_of": None},
        # 重复条目：citekey 与 keeper 相同（真实索引的常态）
        {"citekey": "a2020", "dedup_key": "title:a", "month": "2020-03", "duplicate_of": "doi:1@2020-01"},
        # 重复条目：citekey 不同（预印本 vs 正式版）
        {"citekey": "a2020Preprint", "dedup_key": "arxiv:9", "month": "2020-02", "duplicate_of": "doi:1@2020-01"},
        {"citekey": "b2021", "dedup_key": "doi:2", "month": "2021-01", "duplicate_of": None},
        {"citekey": "c2022", "dedup_key": "doi:3", "month": "2022-01", "duplicate_of": None,
         "flags": ["RETRACTED"]},
        {"citekey": "c2022Dup", "dedup_key": "doi:3b", "month": "2022-02", "duplicate_of": "doi:3@2022-01"},
        # 指向不存在 keeper 的损坏条目
        {"citekey": "d2023", "dedup_key": "doi:4", "month": "2023-01", "duplicate_of": "doi:zzz@2023-01"},
    ]}


def test_retracted_flag_in_sync_with_notes_index():
    assert C._RETRACTED_FLAG == NI.RETRACTED_FLAG


def test_keeper_map_merges_dups_and_retracted():
    m = C.keeper_map(_index())
    assert m["a2020"] == "a2020"
    assert m["a2020Preprint"] == "a2020"      # 预印本并入 keeper
    assert m["b2021"] == "b2021"
    assert m["c2022"] is None                 # 撤稿 keeper
    assert m["c2022Dup"] is None              # 撤稿 keeper 的重复条目随之
    assert m["d2023"] == "d2023"              # keeper 缺失：按自身计，不吞证据


def test_keeper_priority_over_same_citekey_dup():
    # 同 citekey 的重复条目排在 keeper 之后也不能覆盖 keeper 的映射
    idx = {"papers": [
        {"citekey": "x", "dedup_key": "doi:9", "month": "2020-01", "duplicate_of": None},
        {"citekey": "x", "dedup_key": "t:x", "month": "2020-02", "duplicate_of": "doi:9@2020-01"},
    ]}
    assert C.keeper_map(idx) == {"x": "x"}


def test_count_independent_dedup_retract_and_sim_floor():
    km = C.keeper_map(_index())
    evs = [Ev("a2020", 0.70), Ev("a2020Preprint", 0.66),   # 两个 citekey 同一 keeper -> 1
           Ev("a2020", 0.58),                              # 同篇多句 -> 仍 1
           Ev("b2021", 0.56),
           Ev("c2022", 0.80),                              # 撤稿不计
           Ev("unknown9", 0.60)]                           # 不在索引：按自身计
    assert C.count_independent(evs, km, 0.55) == 3
    assert C.count_independent(evs, km, 0.60) == 2         # b2021 被下限挡掉
    assert C.count_independent(evs, km, 0.99) == 0
    assert C.count_independent([], km) == 0


def test_claim_independent_sources():
    km = C.keeper_map(_index())
    ev_map = {"E1": Ev("a2020", .7), "E2": Ev("a2020Preprint", .7),
              "E3": Ev("b2021", .7), "E4": Ev("c2022", .7)}
    assert C.claim_independent_sources(["E1", "E2"], ev_map, km) == 1   # 预印本+正式版算一篇
    assert C.claim_independent_sources(["E1", "E3", "E4"], ev_map, km) == 2  # 撤稿不计
    assert C.claim_independent_sources(["E9"], ev_map, km) == 0         # 悬空编号忽略
    assert C.claim_independent_sources([], ev_map, km) == 0


def test_would_refuse_boundary():
    assert C.would_refuse(2, 3) and not C.would_refuse(3, 3)


def _cases():
    return [
        C.CalibCase("pos1", "qa", True, {0.55: 10, 0.60: 3}),
        C.CalibCase("pos2", "qa", True, {0.55: 4, 0.60: 1}),
        C.CalibCase("neg1", "neg", False, {0.55: 6, 0.60: 0}),
        C.CalibCase("neg2", "neg", False, {0.55: 2, 0.60: 0}),
    ]


def test_grid_rates_counts():
    rows = {(r["sim"], r["k"]): r for r in C.grid_rates(_cases(), (0.55, 0.60), (2, 3))}
    r = rows[(0.55, 3)]
    assert (r["false_refusals"], r["n_pos"], r["missed_refusals"], r["n_neg"]) == (0, 2, 1, 2)
    assert r["missed_names"] == ["neg1"] and r["margin"] == 1
    r = rows[(0.60, 2)]       # pos2 只有 1 篇 -> 误拒；两个反例都被拒
    assert r["false_refusal_names"] == ["pos2"] and r["missed_refusals"] == 0
    assert r["margin"] == -1


def test_grid_missing_count_raises():
    cs = [C.CalibCase("p", "qa", True, {0.55: 3})]
    with pytest.raises(KeyError):
        C.grid_rates(cs, (0.55, 0.60), (3,))


def test_pick_requires_zero_false_refusal_first():
    rows = C.grid_rates(_cases(), (0.55, 0.60), (2, 3, 4))
    best = C.pick_thresholds(rows)
    # 零误拒的格只有 sim=0.55 的 k=2/3/4（pos2 在 0.55 有 4 篇）；k=3、k=4 漏拒同为 1
    # （neg1=6 仍放过），并列时取正例余量大者 -> k=3（余量 1，k=4 余量 0）
    assert best["false_refusals"] == 0
    assert (best["sim"], best["k"]) == (0.55, 3)
    # 0.60 下漏拒为 0 但误拒 >0，不得被选中
    assert not any(r["false_refusals"] == 0 and r["sim"] == 0.60 for r in rows)


def test_pick_returns_none_when_no_zero_false_refusal():
    cs = [C.CalibCase("p", "qa", True, {0.55: 1}), C.CalibCase("n", "neg", False, {0.55: 0})]
    assert C.pick_thresholds(C.grid_rates(cs, (0.55,), (2, 3))) is None


def test_pick_tie_breaks_toward_larger_margin_then_smaller_sim_k():
    cs = [C.CalibCase("p", "qa", True, {0.55: 9, 0.60: 9}),
          C.CalibCase("n", "neg", False, {0.55: 0, 0.60: 0})]
    best = C.pick_thresholds(C.grid_rates(cs, (0.55, 0.60), (2, 3)))
    # 漏拒与误拒都相同，余量也相同（9-k 取决于 k）：k=2 余量最大，sim 取小
    assert (best["sim"], best["k"]) == (0.55, 2)
