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
import math
from dataclasses import dataclass, field
from statistics import NormalDist
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


# ---------------------------------------------------------------------------
# 分布判据（WO-4b）：拒答阈值随「该问题自身的全库相似度分布」动态决定
# ---------------------------------------------------------------------------
# 与上面固定阈值（sim 下限 × 来源数）并存，不替换。口径：
#   1. 对问题的全部 query 向量，算它与**生产召回同一 mask 内全部 chunk** 的余弦，
#      每个 chunk 取各 query 的最大值（与 retrieve_evidence 的「同 chunk 多 query 取最高」一致）；
#   2. 这批分数的 mean/std 即该问题的「背景分布」（泛化问题整体偏高，冷门问题整体偏低）；
#   3. 动态阈值 T = max(floor, mean + z*·std)，z* = Φ⁻¹(1 − e/N)：N 个背景点里
#      期望只有 e 个靠偶然越过 T（e=1 → N≈3.7 万时 z*≈4.04）。floor 是绝对下限，
#      防止「很远的问题背景均值极低、重尾点也被算成显著」；
#   4. 独立来源数 = 去重后 max 分数 ≥ T 的 keeper 数；< min_sources 则拒答。
# 注意这里的计数基于**全库扫描**而不是召回截断后的证据列表（召回每 query 只取 top-80），
# 因此不依赖 relative_alpha / per_paper_cap 等召回参数。

try:  # numpy 只用于相似度扫描；缺失时纯计数函数仍可用
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


@dataclass(frozen=True)
class BackgroundStats:
    n: int            # 背景点数（mask 内 chunk 数）
    mean: float
    std: float


@dataclass(frozen=True)
class DynamicVerdict:
    threshold: float          # 实际采用的阈值 max(floor, mean + z*·std)
    z_star: float             # 机会越界 z*
    floor_binding: bool       # True = 下限在起作用（动态部分低于 floor）
    n_sources: int            # 分数 ≥ threshold 的独立来源数
    top1: float               # 最高来源分数（无来源为 0.0）
    refuse: bool
    bg: BackgroundStats


def query_scores(mat: Any, query_vecs: Any, mask: Any = None) -> Any:
    """矩阵乘一次：返回 mask 内每个 chunk 对所有 query 的**最大**余弦（1 维数组，只含 mask 内行）。
    mat 与 query_vecs 须已 L2 归一（VectorStore 保证）。返回数组与 `np.where(mask)[0]` 对齐。"""
    q = _np.atleast_2d(_np.asarray(query_vecs, dtype=_np.float32))
    m = mat if mask is None else mat[mask]
    return (m @ q.T).max(axis=1)


def background_stats(scores: Any) -> BackgroundStats:
    a = _np.asarray(scores, dtype=_np.float64)
    if a.size == 0:
        return BackgroundStats(0, 0.0, 0.0)
    return BackgroundStats(int(a.size), float(a.mean()), float(a.std()))


def chance_exceedance_z(n: int, expected: float = 1.0) -> float:
    """z*：N 个服从正态背景的点里期望只有 `expected` 个越过。n<=expected 时返回 +inf（不可能显著）。"""
    if n <= 0 or expected <= 0 or n <= expected:
        return math.inf
    return NormalDist().inv_cdf(1.0 - expected / n)


def dynamic_threshold(bg: BackgroundStats, *, floor: float, expected_chance: float = 1.0) -> Tuple[float, float, bool]:
    """-> (threshold, z*, floor_binding)。背景为空（n==0）或 z* 无穷时阈值退回 floor。"""
    z = chance_exceedance_z(bg.n, expected_chance)
    if bg.n <= 0 or math.isinf(z):
        return floor, z, True
    dyn = bg.mean + z * bg.std
    return (dyn, z, False) if dyn > floor else (floor, z, True)


def source_max_scores(scores: Any, citekeys: Sequence[str],
                      keeper_of: Mapping[str, Optional[str]]) -> List[float]:
    """chunk 分数 -> 每个独立来源（keeper）的最高分，降序。撤稿不计，不在索引里的 citekey 按自身计。"""
    best: Dict[str, float] = {}
    for sc, ck in zip(scores, citekeys):
        src = _source_of(ck, keeper_of)
        if src is None:
            continue
        v = float(sc)
        if v > best.get(src, -math.inf):
            best[src] = v
    return sorted(best.values(), reverse=True)


def dynamic_sufficiency(scores: Any, citekeys: Sequence[str],
                        keeper_of: Mapping[str, Optional[str]], *,
                        floor: float, expected_chance: float = 1.0,
                        min_sources: int = 3) -> DynamicVerdict:
    """一次判定：scores/citekeys 是 mask 内全部 chunk 的（max-over-query）分数与 citekey。"""
    bg = background_stats(scores)
    return verdict_from_sources(source_max_scores(scores, citekeys, keeper_of), bg,
                                floor=floor, expected_chance=expected_chance,
                                min_sources=min_sources)


def verdict_from_sources(source_scores: Sequence[float], bg: BackgroundStats, *,
                         floor: float, expected_chance: float = 1.0,
                         min_sources: int = 3) -> DynamicVerdict:
    """标定网格用：已算好的「来源最高分（降序）」+ 背景统计 -> 判定。"""
    thr, z, binding = dynamic_threshold(bg, floor=floor, expected_chance=expected_chance)
    n = sum(1 for v in source_scores if v >= thr)
    top1 = float(source_scores[0]) if len(source_scores) else 0.0
    return DynamicVerdict(threshold=thr, z_star=z, floor_binding=binding, n_sources=n,
                          top1=top1, refuse=would_refuse(n, min_sources), bg=bg)


@dataclass
class DistCase:
    """分布判据的标定问题：positive=True 表示应当不拒答。"""
    name: str
    group: str
    positive: bool
    source_scores: List[float]      # 降序
    bg: BackgroundStats


def dynamic_grid_rates(cases: Sequence[DistCase], floors: Sequence[float],
                       es: Sequence[float], ms: Sequence[int]) -> List[Dict[str, Any]]:
    """(floor × e × m) 网格，字段同 `grid_rates`（误拒/漏拒/余量）。"""
    pos = [c for c in cases if c.positive]
    neg = [c for c in cases if not c.positive]
    rows: List[Dict[str, Any]] = []
    for fl in floors:
        for e in es:
            vp = {c.name: verdict_from_sources(c.source_scores, c.bg, floor=fl, expected_chance=e, min_sources=1)
                  for c in cases}
            for m in ms:
                fr = [c.name for c in pos if vp[c.name].n_sources < m]
                miss = [c.name for c in neg if vp[c.name].n_sources >= m]
                margin = min((vp[c.name].n_sources - m for c in pos), default=None)
                rows.append({"floor": fl, "e": e, "m": m,
                             "false_refusals": len(fr), "n_pos": len(pos), "false_refusal_names": fr,
                             "missed_refusals": len(miss), "n_neg": len(neg), "missed_names": miss,
                             "margin": margin})
    return rows


def pick_dynamic(rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """同 `pick_thresholds`：误拒=0 前提下漏拒最少；并列取余量大者，再取 floor 小、e 大（阈值更低，
    偏向不拒答）、m 小——与 Q8「宁可回避不足也不误拒」同向。无误拒=0 的格返回 None（调用方须停下交用户）。"""
    ok = [r for r in rows if r["false_refusals"] == 0]
    if not ok:
        return None
    return min(ok, key=lambda r: (r["missed_refusals"], -(r["margin"] if r["margin"] is not None else 0),
                                  r["floor"], -r["e"], r["m"]))
