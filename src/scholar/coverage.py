# -*- coding: utf-8 -*-
"""证据充分性：独立来源计数 + 拒答阈值网格（WO-4；WO-5 在此继续补覆盖说明与渲染）。

设计见 docs/decisions/trust_hardening_design_2026-10-13.md 第 6 节。本模块目前只含
**纯函数**（不读文件、不联网、不调 LLM），分两块：

1. 独立来源计数（6.3）：一条证据的来源 = 其 citekey 在索引里解析到的 keeper；
   独立来源数 = 去重后的 keeper 数；撤稿条目不计；不按作者组合并（Q6，本批不做）。
2. 拒答阈值网格（6.6）：给定一批「应当不拒答」的正例与「应当拒答」的反例的召回结果，
   在 (sim 下限 × 来源阈值) 网格上算误拒率与漏拒率，并按设计的选择规则选组合。
   标定脚本 scripts/calibrate_evidence_sufficiency.py 只负责取数，口径全在这里。

证据对象按鸭子类型处理，只要求有 `.citekey` 与 `.score`（topics.Evidence 满足）。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# 撤稿标记，与 notes_index.RETRACTED_FLAG 同值；这里不 import 它，避免纯函数模块
# 拖进索引构建的整条依赖链（接线由测试断言保证不漂移）。
_RETRACTED_FLAG = "RETRACTED"


# ---------------------------------------------------------------------------
# 独立来源计数
# ---------------------------------------------------------------------------

def keeper_map(index: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """索引 -> `{citekey: keeper 的 citekey}`；撤稿的 keeper（及指向它的重复条目）映射为 None。

    - keeper 条目（`duplicate_of` 为空）映射到自己。
    - 重复条目的 `duplicate_of` 形如 `"<dedup_key>@<month>"`，指向 keeper 的
      `dedup_key@month`；解析到 keeper 后映射到 keeper 的 citekey。
      实测 2026-10-13 索引里 144 条重复条目的 citekey 与 keeper **相同**（陈旧键守卫的结果），
      所以同 citekey 既有 keeper 又有重复条目是常态——**keeper 的映射永远优先**，
      不会被同名的重复条目覆盖。
    - `duplicate_of` 指向不存在的 keeper（索引损坏）时，该条目按自身为独立来源处理
      （宁可多计一篇，也不因脏数据把证据静默吞掉；少计会触发误拒，是更坏的方向）。
    - 撤稿判定与 `notes_index.is_retracted` 同口径（flags 含 RETRACTED）；
      以 keeper 的撤稿状态为准，重复条目随 keeper。
    """
    papers = [p for p in (index.get("papers") or []) if isinstance(p, dict) and p.get("citekey")]
    by_slot: Dict[str, dict] = {}
    for p in papers:
        if not p.get("duplicate_of") and p.get("dedup_key"):
            by_slot["{}@{}".format(p["dedup_key"], p.get("month"))] = p

    def _retracted(p: dict) -> bool:
        return _RETRACTED_FLAG in (p.get("flags") or [])

    out: Dict[str, Optional[str]] = {}
    # 先放重复条目，再放 keeper：同 citekey 时 keeper 覆盖
    for p in papers:
        dup = p.get("duplicate_of")
        if not dup:
            continue
        k = by_slot.get(dup)
        if k is None:
            out[p["citekey"]] = None if _retracted(p) else p["citekey"]
        else:
            out[p["citekey"]] = None if _retracted(k) else k["citekey"]
    for p in papers:
        if not p.get("duplicate_of"):
            out[p["citekey"]] = None if _retracted(p) else p["citekey"]
    return out


def _source_of(citekey: str, keeper_of: Mapping[str, Optional[str]]) -> Optional[str]:
    """citekey -> 独立来源标识；撤稿返回 None（不计）。不在索引里的 citekey 按自身计。"""
    if citekey in keeper_of:
        return keeper_of[citekey]
    return citekey


def count_independent(evidences: Iterable[Any], keeper_of: Mapping[str, Optional[str]],
                      min_sim: float = 0.0) -> int:
    """证据列表的独立来源数：只算 `score >= min_sim` 的证据，keeper 去重，撤稿不计。"""
    seen = set()
    for e in evidences:
        if float(e.score) < min_sim:
            continue
        src = _source_of(e.citekey, keeper_of)
        if src is not None:
            seen.add(src)
    return len(seen)


def claim_independent_sources(refs: Iterable[str], ev_map: Mapping[str, Any],
                              keeper_of: Mapping[str, Optional[str]]) -> int:
    """一条论断引用的独立来源数。`refs` 是 "E1" 这类编号；ev_map 里找不到的编号忽略
    （它是别的校验该报的事，不在计数里放大）。"""
    seen = set()
    for r in refs:
        e = ev_map.get(r)
        if e is None:
            continue
        src = _source_of(e.citekey, keeper_of)
        if src is not None:
            seen.add(src)
    return len(seen)


# ---------------------------------------------------------------------------
# 拒答阈值网格（6.6）
# ---------------------------------------------------------------------------

@dataclass
class CalibCase:
    """一个标定问题：`positive=True` 表示应当**不**拒答；`counts[sim]` = 该 sim 下限的独立来源数。"""
    name: str
    group: str                      # topic / qa_archived / qa_niche / neg_gap / neg_offprofile ...
    positive: bool
    counts: Dict[float, int] = field(default_factory=dict)


def would_refuse(n_independent: int, min_sources: int) -> bool:
    """页面级拒答判据：独立来源数 < 阈值。"""
    return n_independent < min_sources


def grid_rates(cases: Sequence[CalibCase], sims: Sequence[float],
               ks: Sequence[int]) -> List[Dict[str, Any]]:
    """网格每格返回 `{sim, k, false_refusals, n_pos, missed_refusals, n_neg, margin}`。

    - 误拒 = 正例被拒（过度回避）；漏拒 = 反例没被拒（回避不足）。
    - `margin` = 正例里 (n_independent - k) 的最小值，越大离误拒越远（选择规则的次级键）；
      没有正例时为 None。
    - 某个 case 缺少某个 sim 的计数时抛 KeyError，不静默当 0：缺数据当 0 会把误拒
      凭空放大，让标定结论建立在漏算上。
    """
    pos = [c for c in cases if c.positive]
    neg = [c for c in cases if not c.positive]
    rows: List[Dict[str, Any]] = []
    for sim in sims:
        for k in ks:
            fr = [c.name for c in pos if would_refuse(c.counts[sim], k)]
            miss = [c.name for c in neg if not would_refuse(c.counts[sim], k)]
            margin = min((c.counts[sim] - k for c in pos), default=None)
            rows.append({"sim": sim, "k": k,
                         "false_refusals": len(fr), "n_pos": len(pos),
                         "false_refusal_names": fr,
                         "missed_refusals": len(miss), "n_neg": len(neg),
                         "missed_names": miss, "margin": margin})
    return rows


def pick_thresholds(rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """设计 6.6 的选择规则：先要求误拒 = 0，再取漏拒最少；不存在误拒 = 0 的格返回 None
    （调用方必须停下交用户决定，不得自行放宽）。

    同漏拒的并列项按顺序打破：正例余量 margin 大者优先（离误拒更远）→ sim 小者 →
    k 小者。偏向「更不容易拒答」的一侧，与 Q8「宁可回避不足也不误拒」同向。
    """
    ok = [r for r in rows if r["false_refusals"] == 0]
    if not ok:
        return None
    return min(ok, key=lambda r: (r["missed_refusals"],
                                  -(r["margin"] if r["margin"] is not None else 0),
                                  r["sim"], r["k"]))
