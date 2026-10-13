# -*- coding: utf-8 -*-
"""证据充分性 + 覆盖范围说明（WO-4 / WO-4b 标定，WO-5 接线与渲染）。

设计见 docs/decisions/trust_hardening_design_2026-10-13.md 第 6 节。本模块除
`load_certification`（读一个小 JSON）与 `qa_dynamic_verdict`（读向量库矩阵）外都是
**纯函数**（不联网、不调 LLM）。WO-5 在文件末尾追加了「页面接线」一节：判定结果
`Sufficiency`、覆盖说明块、存根页小节、单一来源徽标与 frontmatter 条目——topics.py 与
qa.py 各只经这里取文案，**逻辑不在两边各写一份**。前面的纯函数分两块：

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

from .thresholds import (
    EVIDENCE_DYNAMIC_EXPECTED_CHANCE, EVIDENCE_DYNAMIC_FLOOR, EVIDENCE_SUFFICIENCY_MIN_SIM,
    QA_ANSWER_MIN_SOURCES, QA_MIN_INDEPENDENT_SOURCES, TOPIC_MIN_INDEPENDENT_SOURCES,
)

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


# ===========================================================================
# 页面接线（WO-5）：判定结果 / 覆盖说明块 / 存根页小节 / 徽标 / frontmatter 条目
# ===========================================================================
# 约定：以下文案与统计只在本节生成，topics.py / qa.py 各自只有一个调用点。
# 渲染文案里**不得出现** `[@key]`、`E<数字>` 这类会被页面解析器当成论断/证据编号的片段
# （qa.audit_qa_pages 的残留编号扫描、page_parse 的论断识别都认这两种形状）。

ANSWER_MIN_SOURCES = QA_ANSWER_MIN_SOURCES      # 供 qa 渲染引用；接线断言见 test_thresholds
SINGLE_SOURCE_BADGE = "［单一来源］"
COVERAGE_HEADER = "**本页覆盖范围**"

EVIDENCE_STATUS_OK = "ok"
EVIDENCE_STATUS_INSUFFICIENT = "insufficient"
EVIDENCE_STATUS_FORCED = "forced"


@dataclass(frozen=True)
class Sufficiency:
    """页面级证据充分性判定（固定判据与分布判据统一的承载体，存根页据此印出依据）。"""
    method: str                   # "fixed" | "dynamic"
    refuse: bool
    n_sources: int                # 达标的独立来源数
    min_sources: int              # 要求的下限
    sim_threshold: float          # fixed: 固定下限；dynamic: 实际采用的动态阈值 T
    top1: float                   # 最高来源（fixed: 最高证据）分数，无则 0.0
    detail: Dict[str, Any] = field(default_factory=dict)   # dynamic 专有：floor/z_star/背景统计


def fixed_sufficiency(evidences: Sequence[Any], keeper_of: Mapping[str, Optional[str]], *,
                      min_sim: float = EVIDENCE_SUFFICIENCY_MIN_SIM,
                      min_sources: int = TOPIC_MIN_INDEPENDENT_SOURCES) -> Sufficiency:
    """WO-4 固定判据（概念页）：召回后的证据里 score ≥ min_sim 的独立来源数 < min_sources 即拒答。"""
    n = count_independent(evidences, keeper_of, min_sim)
    top1 = max((float(e.score) for e in evidences), default=0.0)
    return Sufficiency(method="fixed", refuse=would_refuse(n, min_sources), n_sources=n,
                       min_sources=min_sources, sim_threshold=min_sim, top1=top1)


def sufficiency_from_dynamic(v: DynamicVerdict, *, floor: float, expected_chance: float,
                             min_sources: int) -> Sufficiency:
    return Sufficiency(
        method="dynamic", refuse=v.refuse, n_sources=v.n_sources, min_sources=min_sources,
        sim_threshold=v.threshold, top1=v.top1,
        detail={"floor": floor, "expected_chance": expected_chance, "z_star": v.z_star,
                "floor_binding": v.floor_binding, "bg_n": v.bg.n, "bg_mean": v.bg.mean,
                "bg_std": v.bg.std})


def retrieval_mask(spec: Any, store: Any) -> Any:
    """与 `topics.retrieve_evidence` 同口径的 chunk 掩码：句级 highlight、roles 过滤、
    按前缀排除主观批注分节。（那边的循环是内联的，这里镜像一份供全库扫描用；
    等价性由 test_trust_coverage 对着 retrieve_evidence 的召回结果断言。）"""
    import numpy as np
    rf = set(spec.roles) if spec.roles else None
    ex = tuple(spec.exclude_sections) if spec.exclude_sections else ()
    mask = np.zeros(len(store.records), dtype=bool)
    for i, r in enumerate(store.records):
        if r.get("level") != "highlight":
            continue
        if rf and r.get("role") not in rf:
            continue
        if ex and str(r.get("section") or "").startswith(ex):
            continue
        mask[i] = True
    return mask


def qa_dynamic_verdict(spec: Any, store: Any, embed_client: Any,
                       keeper_of: Mapping[str, Optional[str]], *,
                       floor: float = EVIDENCE_DYNAMIC_FLOOR,
                       expected_chance: float = EVIDENCE_DYNAMIC_EXPECTED_CHANCE,
                       min_sources: int = QA_MIN_INDEPENDENT_SOURCES) -> Sufficiency:
    """WO-4b 分布判据（问答页）：对问题的全部 query 向量扫全库（同召回的 mask），
    按该问题自身的背景分布定动态阈值。基于**全库扫描**，不经召回截断
    （top-80 / relative_alpha / per_paper_cap），接口说明见 WO-4b 档案第 1 节。
    mask 为空（过滤条件没匹配到任何句级证据）按 0 个来源处理。"""
    import numpy as np
    mask = retrieval_mask(spec, store)
    queries = list(spec.queries)
    if not mask.any() or not queries:
        v = verdict_from_sources([], BackgroundStats(0, 0.0, 0.0), floor=floor,
                                 expected_chance=expected_chance, min_sources=min_sources)
        return sufficiency_from_dynamic(v, floor=floor, expected_chance=expected_chance,
                                        min_sources=min_sources)
    vecs = np.asarray(embed_client.embed(queries), dtype=np.float32)
    sc = query_scores(store.mat, vecs, mask)
    cks = [store.records[i]["citekey"] for i in np.where(mask)[0]]
    v = dynamic_sufficiency(sc, cks, keeper_of, floor=floor,
                            expected_chance=expected_chance, min_sources=min_sources)
    return sufficiency_from_dynamic(v, floor=floor, expected_chance=expected_chance,
                                    min_sources=min_sources)


# ---------------------------------------------------------------------------
# 库统计
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageStats:
    """库的口径统计，全部现算自索引（不写死任何数字）。keeper = 非重复条目；撤稿不计。"""
    library_keepers: int
    library_include: int
    library_maybe: int
    evidence_pool_papers: int      # 有句级 highlight 的 keeper 数（概念页/问答的取证范围）
    title_only_papers: int         # 其余 keeper：只有题录或摘要，永远不进证据表
    no_fulltext_papers: int        # has_full_text_reading 为假的 keeper（付费墙/抓取失败/仅题录，索引未区分）
    month_range: Tuple[str, str]
    series_counts: Dict[str, int]
    filter_fingerprint: Optional[str]
    filter_certified: Optional[bool]        # None = 无认证记录可查
    filter_certified_at: Optional[str]
    index_generated_at: str


def _n_highlights(p: Mapping[str, Any]) -> int:
    h = p.get("highlights")
    if isinstance(h, list):
        return len(h)
    return int(h) if isinstance(h, (int, float)) and not isinstance(h, bool) else 0


def _is_keeper(p: Any) -> bool:
    return isinstance(p, dict) and bool(p.get("citekey")) and not p.get("duplicate_of")


def _is_retracted(p: Mapping[str, Any]) -> bool:
    return _RETRACTED_FLAG in (p.get("flags") or [])


def load_certification(path: Any, current_fingerprint: Optional[str]
                       ) -> Tuple[Optional[bool], Optional[str]]:
    """读筛选召回认证记录（WO-8 的 certification.json；此前它不存在）。
    -> (是否有当前指纹的通过认证, 认证日期)。文件缺失/不可读/结构不对/指纹未知 -> (None, None)，
    绝不抛异常——覆盖说明显示「尚未经召回认证」，不是报错。"""
    if path is None or not current_fingerprint:
        return None, None
    try:
        import json
        from pathlib import Path
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        certs = data.get("certifications")
        if not isinstance(certs, list):
            return None, None
        for c in certs:
            if (isinstance(c, dict) and c.get("passed") is True
                    and c.get("prompt_fingerprint") == current_fingerprint):
                return True, str(c.get("certified_at") or "") or None
        return False, None
    except Exception:       # noqa: BLE001 — 文件缺失/损坏一律降级为「无记录」
        return None, None


def coverage_stats(index: Mapping[str, Any], *, certification_path: Any = None,
                   current_filter_fingerprint: Optional[str] = None) -> CoverageStats:
    keepers = [p for p in (index.get("papers") or []) if _is_keeper(p) and not _is_retracted(p)]
    pool = [p for p in keepers if _n_highlights(p) > 0]
    months = sorted(str(p.get("month"))[:7] for p in keepers if p.get("month"))
    series: Dict[str, int] = {}
    for p in keepers:
        k = str(p.get("series") or "auto")
        series[k] = series.get(k, 0) + 1
    cert, cert_at = load_certification(certification_path, current_filter_fingerprint)
    return CoverageStats(
        library_keepers=len(keepers),
        library_include=sum(1 for p in keepers if p.get("decision") == "INCLUDE"),
        library_maybe=sum(1 for p in keepers if p.get("decision") == "MAYBE"),
        evidence_pool_papers=len(pool),
        title_only_papers=len(keepers) - len(pool),
        no_fulltext_papers=sum(1 for p in keepers if not p.get("has_full_text_reading")),
        month_range=(months[0], months[-1]) if months else ("", ""),
        series_counts=series,
        filter_fingerprint=current_filter_fingerprint,
        filter_certified=cert, filter_certified_at=cert_at,
        index_generated_at=str(index.get("generated_at") or ""))


@dataclass
class CoverageContext:
    """一次生成所需的覆盖说明上下文（由调用方构造一次，传给渲染函数）。"""
    stats: CoverageStats
    keeper_of: Mapping[str, Optional[str]]
    abstract_only: frozenset                       # 没读到全文的 keeper citekey（证据来自摘要级精读）
    status: str = EVIDENCE_STATUS_OK               # ok / insufficient / forced
    sufficiency: Optional[Sufficiency] = None
    synth: Dict[str, Any] = field(default_factory=dict)    # synth_provider / synth_model / synth_model_verified
    audit_model: Optional[str] = None


def build_context(index: Mapping[str, Any], *, certification_path: Any = None,
                  current_filter_fingerprint: Optional[str] = None) -> CoverageContext:
    km = keeper_map(index)
    abstract_only = frozenset(
        p["citekey"] for p in (index.get("papers") or [])
        if _is_keeper(p) and not p.get("has_full_text_reading"))
    return CoverageContext(
        stats=coverage_stats(index, certification_path=certification_path,
                             current_filter_fingerprint=current_filter_fingerprint),
        keeper_of=km, abstract_only=abstract_only)


@dataclass(frozen=True)
class PageEvidenceProfile:
    n_evidence: int
    n_independent: int            # 证据表里的独立来源数（keeper 去重、撤稿不计，不设分数下限）
    abstract_level_papers: int    # 其中精读只基于摘要的篇数
    single_source_claims: int


def page_evidence_profile(evidences: Sequence[Any], ctx: CoverageContext,
                          claim_refs: Iterable[Sequence[str]] = ()) -> PageEvidenceProfile:
    """`claim_refs`：每条论断引用的证据编号列表（topics: 各小节论断；qa: points+caveats）。"""
    ev_map = {e.ref: e for e in evidences}
    srcs = {s for s in (_source_of(e.citekey, ctx.keeper_of) for e in evidences) if s is not None}
    n_single = sum(1 for refs in claim_refs
                   if claim_independent_sources(refs, ev_map, ctx.keeper_of) == 1)
    return PageEvidenceProfile(
        n_evidence=len(evidences), n_independent=len(srcs),
        abstract_level_papers=sum(1 for s in srcs if s in ctx.abstract_only),
        single_source_claims=n_single)


def single_source_badge(refs: Sequence[str], ev_map: Mapping[str, Any],
                        keeper_of: Mapping[str, Optional[str]]) -> str:
    """论断只挂一个独立来源时返回 ` ［单一来源］`（含前导空格），否则空串。ref 全找不到时不加。"""
    return " " + SINGLE_SOURCE_BADGE if claim_independent_sources(refs, ev_map, keeper_of) == 1 else ""


def answer_support_notice(point_refs: Iterable[Sequence[str]], ev_map: Mapping[str, Any],
                          keeper_of: Mapping[str, Optional[str]], *,
                          min_sources: int = QA_ANSWER_MIN_SOURCES) -> Optional[str]:
    """qa 的 answer 级提示：points 引用的独立来源合计 < min_sources 时返回提示行，否则 None。"""
    refs = [r for group in point_refs for r in group]
    n = claim_independent_sources(refs, ev_map, keeper_of)
    if n >= min_sources:
        return None
    if n == 0:
        return "> ⚠️ 库内证据不足：下面的回答没有带出处的依据论断。"
    return "> ⚠️ 库内证据不足：下面的回答只依据 {} 篇文献。".format(n)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_gap_disclaimer(kind: str, n_evidence: int) -> str:
    """证据缺口一节的免责（topics「证据缺口」与 qa「本次召回没覆盖到的」共用这一处）。
    与「库里没有 ≠ 文献里没有」（覆盖说明块）是两个层级：这里说的是**本次召回切片**没覆盖到，
    不等于**库里**没有；那条说的是**库里**没有，不等于**文献里**没有。"""
    if kind == "qa":
        return ("这是**本次这 {} 条证据**没能回答的部分，**不等于库里没有**——"
                "召回是按单个问题从全库切出来的窄切片，召回不到 ≠ 不存在。"
                "决定要不要去补文献之前，先查 `topics/INDEX.md` 与 `notes_search.py`。"
                .format(n_evidence))
    return ("这是本页这 {} 条证据没覆盖到的部分，**不等于库里没有**——"
            "召回是按概念从全库切出来的切片，召回不到 ≠ 不存在。"
            "决定要不要去补文献之前，先查 `topics/INDEX.md` 与 `notes_search.py`。"
            .format(n_evidence))


def render_coverage_block(ctx: CoverageContext, profile: PageEvidenceProfile, *,
                          kind: str) -> List[str]:
    """页面顶部的「本页覆盖范围」块（引用块形式，末尾带一个空行）。topic 与 qa 逐字相同，
    `kind` 只影响强制生成那一句里的页面称呼。"""
    st = ctx.stats
    sc = st.series_counts
    n_auto, n_manual, n_book = sc.get("auto", 0), sc.get("manual", 0), sc.get("book", 0)
    lo, hi = st.month_range
    L = ["> {}（截至 {}）".format(COVERAGE_HEADER, st.index_generated_at or "未知"),
         "> - 本库是按研究画像筛选的方法学札记库（{} 篇：INCLUDE {} / MAYBE {}；"
         "来源：Google Scholar 提醒与 arXiv/PubMed 抓取 {} 篇、手动精读 {} 篇、书籍 {} 本{}）。"
         "画像之外的主题**系统性缺席**。".format(
             st.library_keepers, st.library_include, st.library_maybe, n_auto, n_manual, n_book,
             "；月份 {} ~ {}".format(lo, hi) if lo else ""),
         "> - 只能从有句级精读的 {} 篇里取证；另有 {} 篇库内文献只有题录或摘要，不会出现在证据表里。"
         "库内共 {} 篇没有读到全文（付费墙、抓取失败或仅题录；索引里没有区分付费墙的字段，故不单列）。".format(
             st.evidence_pool_papers, st.title_only_papers, st.no_fulltext_papers),
         "> - 本页证据表来自 {} 篇独立来源（同一论文的预印本与正式版按一篇计），其中 {} 篇的精读只基于摘要。".format(
             profile.n_independent, profile.abstract_level_papers),
         "> - **库里没有 ≠ 文献里没有**。要确认「领域里有没有人做过」，请用 `scholar-search` 外部检索；"
         "追引用链须 Semantic Scholar 与 OpenAlex 双路（OpenAlex 单路实测会漏约三分之一）。"]
    if st.filter_certified is True:
        L.append("> - 筛选配置 {}：已通过召回认证{}。".format(
            st.filter_fingerprint or "", "（{}）".format(st.filter_certified_at)
            if st.filter_certified_at else ""))
    else:
        L.append("> - 筛选配置{}：尚未经召回认证，被筛掉的论文是否漏判未经人工金标准核对。".format(
            " " + st.filter_fingerprint if st.filter_fingerprint else ""))
    if ctx.status == EVIDENCE_STATUS_FORCED:
        L.append("> - 本页在证据不足时被强制生成（`--force-synthesize`）："
                 "结论所依据的独立来源少于闸门要求，请当作线索而非定论。")
    L.append("")
    return L


def render_insufficient_section(suff: Sufficiency, *, kind: str, force_command: str) -> List[str]:
    """存根页的「库内证据不足」+「下一步」两小节。数字写全，方便判断是不是误拒。"""
    page = "问答页" if kind == "qa" else "概念页"
    L = ["## 库内证据不足", "",
         "库内证据不足以综合出一页{}，本页**没有调用 LLM**，只列出召回到的证据原句。".format(page), ""]
    if suff.method == "dynamic":
        d = suff.detail
        L += ["- 判据：分布判据（按该问题自身的全库相似度分布定阈值，不经召回截断）",
              "- 独立来源：{} 篇达标，要求至少 {} 篇".format(suff.n_sources, suff.min_sources),
              "- 动态阈值 {:.3f} = max(下限 {:.2f}, 背景均值 {:.3f} + z* {:.2f} × 标准差 {:.3f})，"
              "此次起作用的是{}".format(
                  suff.sim_threshold, d.get("floor", 0.0), d.get("bg_mean", 0.0),
                  d.get("z_star", 0.0) if math.isfinite(d.get("z_star", 0.0)) else 0.0,
                  d.get("bg_std", 0.0), "绝对下限" if d.get("floor_binding") else "动态部分"),
              "- 背景：{} 个候选句级片段，期望靠偶然越界 {:g} 个；最高来源分数 {:.3f}".format(
                  d.get("bg_n", 0), d.get("expected_chance", 0.0), suff.top1)]
    else:
        L += ["- 判据：固定判据（召回后证据中相似度不低于 {:.2f} 的独立来源数）".format(suff.sim_threshold),
              "- 独立来源：{} 篇达标，要求至少 {} 篇".format(suff.n_sources, suff.min_sources),
              "- 召回到的最高证据分数 {:.3f}".format(suff.top1)]
    L += ["- 阈值是在有限样本上标定的（见 `src/scholar/thresholds.py` 注释），"
          "「相邻但画像外」的问题与真正冷门的问题在相似度上分不开：认为这是**误拒**时，请用下面的强制参数。",
          "", "## 下一步", "",
          "- 外部检索：`scholar-search`（不入库），确认领域里有没有人做过。",
          "- 追引用链：Semantic Scholar 与 OpenAlex 双路。",
          "- 查库内近邻：`python scripts/notes_search.py \"<关键词>\" --mode dense --min-score 0.62`。",
          "- 认为库里其实够用：`{}`（页面会标注“证据不足时被强制生成”）。".format(force_command),
          ""]
    return L


def frontmatter_items(ctx: CoverageContext, profile: PageEvidenceProfile, *,
                      include_audit_model: bool = False) -> Dict[str, Any]:
    """新增的受管 frontmatter 键（topic 与 qa 共用）。`n_independent_sources` 是**闸门口径**的数
    （有判定时取判定的达标来源数，否则取证据表来源数），倒退保护据此比较。"""
    n_gate = ctx.sufficiency.n_sources if ctx.sufficiency is not None else profile.n_independent
    items: Dict[str, Any] = {
        "coverage_pool_papers": ctx.stats.evidence_pool_papers,
        "coverage_title_only": ctx.stats.title_only_papers,
        "n_independent_sources": n_gate,
        "abstract_level_papers": profile.abstract_level_papers,
        "single_source_claims": profile.single_source_claims,
        "evidence_status": ctx.status,
        "filter_certified": ctx.stats.filter_certified,
        "synth_provider": ctx.synth.get("synth_provider"),
        "synth_model": ctx.synth.get("synth_model"),
        "synth_model_verified": bool(ctx.synth.get("synth_model_verified", False)),
    }
    if include_audit_model:
        items["audit_model"] = ctx.audit_model
    return items


def synth_fields(meta: Optional[Mapping[str, Any]], requested_model: Optional[str]) -> Dict[str, Any]:
    """把 `meta_provenance` 的结果换成 frontmatter 字段。取到 served_model 记为已验证；
    取不到（假 llm、信封无 modelUsage 等）记请求值并标未验证——不把请求值冒充应答者。"""
    served = (meta or {}).get("served_model")
    return {"synth_provider": (meta or {}).get("provider"),
            "synth_model": served or requested_model,
            "synth_model_verified": bool(served)}


def call_llm_with_meta(llm: Any, prompt: str, **kwargs) -> Tuple[str, Dict[str, Any]]:
    """调 llm，返回 (文本, {provider, served_model, fell_back})。拿不到 meta 时三项均 None。
    判据与 closereading.call_llm_with_meta 一致（类上有 call_with_meta 才用，MagicMock 自动
    属性不算）；延迟 import，避免概念页/问答模块在 import 期拖进 PDF 抽取依赖。"""
    from .closereading import call_llm_with_meta as _call, meta_provenance
    text, meta = _call(llm, prompt, **kwargs)
    return text, meta_provenance(meta)


# 页面上不该喂回合成 prompt 的覆盖说明（read_existing 的上一版正文用）


def strip_coverage_block(prev: str) -> str:
    """去掉上一版生成块开头的覆盖说明引用块，避免把它当“上一版答案”喂回 LLM。"""
    if not prev:
        return prev
    lines = prev.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("> " + COVERAGE_HEADER):
            i += 1
            while i < len(lines) and lines[i].startswith(">") and lines[i].strip() != "":
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)
