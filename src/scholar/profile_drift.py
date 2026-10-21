# -*- coding: utf-8 -*-
"""月度画像漂移检测（可信度加固 WO-8c，设计 §12.3）：只在「分歧成簇」时提醒，**不改画像**。

数据源：入库确认弹窗产生的 stream_labels.jsonl（WO-8a）。两类分歧：

  补勾  筛选器 EXCLUDE、用户 keep —— 研究兴趣可能长出了画像还没覆盖的新方向，或某条排除维度过严。
        聚类两种口径：① 按 exclude_reason（X1..X7）；② 按语义（标题+摘要向量，单链接 ≥ DRIFT_CLUSTER_MIN_SIM）。
  取消  筛选器 INCLUDE/MAYBE、用户 drop —— 某个纳入维度可能过宽，或兴趣已经移走。按 bucket（A..I）聚类。

「成簇」= 同一原因/簇 ≥ DRIFT_MIN_CLUSTER 篇，且占该类分歧（全部补勾 / 全部取消）≥ DRIFT_MIN_SHARE。
零散的分歧是噪声（标注者的随手一勾、锚定偏倚），不提醒；成簇才值得改画像。

诚实边界：
  * 补勾簇是更强的信号（EXCLUDE 默认不勾，勾上是主动行为）；取消簇受「默认勾选 → 懒得取消」
    锚定偏倚压低，数量只是下界，且「取消」也可能只是这周没兴趣，不等于兴趣漂移。
  * 阈值是产品口径，初值见 thresholds.DRIFT_*，跑几个月后按误报/漏报复核。
  * 语义聚类需要本地 embedding 服务；不可用时只做原因/维度聚类，并在报告里写明。

纯函数为主，不调 LLM、不联网（embed_fn 由调用方注入；默认实现走本地 Ollama，仅脚本入口使用）。
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import thresholds as TH

EmbedFn = Callable[[Sequence[str]], Any]       # texts -> (n, d) 数组


@dataclass
class Cluster:
    kind: str                  # reason | semantic | bucket
    side: str                  # added（补勾）| removed（取消）
    key: str                   # X1 / A / s1(语义簇序号)
    n: int
    share: float
    rows: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DriftAnalysis:
    window_start: datetime
    window_end: datetime
    n_labels: int
    n_added: int               # 补勾总数（分母）
    n_removed: int             # 取消总数（分母）
    clusters: List[Cluster]
    semantic_status: str       # ok | skipped_few | unavailable


def _text(r: Dict[str, Any]) -> str:
    return "{}\n{}".format(r.get("title") or "", (r.get("abstract") or "")[:800])


def _components(vecs, thr: float) -> List[List[int]]:
    """单链接连通分量（余弦 ≥ thr 连边）。n 很小（月补勾数十篇），O(n²) 足够。"""
    import numpy as np
    v = np.asarray(vecs, dtype="float64")
    norm = np.linalg.norm(v, axis=1, keepdims=True)
    v = v / np.where(norm == 0, 1.0, norm)
    sim = v @ v.T
    n = len(v)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= thr:
                parent[find(i)] = find(j)
    comp: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        comp[find(i)].append(i)
    return sorted(comp.values(), key=lambda c: (-len(c), c[0]))


def analyze(labels: Sequence[Dict[str, Any]], *, now: datetime,
            lookback_weeks: int = TH.DRIFT_LOOKBACK_WEEKS, min_cluster: int = TH.DRIFT_MIN_CLUSTER,
            min_share: float = TH.DRIFT_MIN_SHARE, min_sim: float = TH.DRIFT_CLUSTER_MIN_SIM,
            embed_fn: Optional[EmbedFn] = None, include_default_accepted: bool = False) -> DriftAnalysis:
    # default_accepted（汇总核对里没动、照单全收默认）不是用户的主动判断，默认不计入漂移
    if not include_default_accepted:
        labels = [r for r in labels if r.get("user_decision_source") != "default_accepted"]
    lo = now - timedelta(weeks=lookback_weeks)
    win = []
    for r in labels:
        try:
            t = datetime.fromisoformat(r["confirmed_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if lo < t <= now:
            win.append(r)
    added = [r for r in win if r.get("filter_decision") == "EXCLUDE" and r.get("user_decision") == "keep"]
    removed = [r for r in win if r.get("filter_decision") in ("INCLUDE", "MAYBE") and r.get("user_decision") == "drop"]
    clusters: List[Cluster] = []

    def ok(n, total):
        return total > 0 and n >= min_cluster and n / total >= min_share

    by_reason: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in added:
        by_reason[r.get("exclude_reason") or "?"].append(r)
    for k, rows in sorted(by_reason.items()):
        if ok(len(rows), len(added)):
            clusters.append(Cluster("reason", "added", k, len(rows), len(rows) / len(added), rows))

    by_bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in removed:
        for b in (r.get("filter_bucket") or ["?"]):
            by_bucket[b].append(r)
    for k, rows in sorted(by_bucket.items()):
        if ok(len(rows), len(removed)):
            clusters.append(Cluster("bucket", "removed", k, len(rows), len(rows) / len(removed), rows))

    sem = "skipped_few"
    if len(added) >= min_cluster:
        if embed_fn is None:
            sem = "unavailable"
        else:
            try:
                vecs = embed_fn([_text(r) for r in added])
                sem = "ok"
                for i, comp in enumerate(_components(vecs, min_sim), 1):
                    if ok(len(comp), len(added)):
                        clusters.append(Cluster("semantic", "added", "s{}".format(i), len(comp),
                                                len(comp) / len(added), [added[j] for j in comp]))
            except Exception:                      # 向量服务挂了不能拖垮提醒本身
                sem = "unavailable"
    return DriftAnalysis(lo, now, len(win), len(added), len(removed), clusters, sem)


def dim_line(code: str, profile: Optional[Dict[str, Any]] = None) -> str:
    """从画像里取某个维度（A..I / X1..X7）的首行说明；取不到返回空串。"""
    try:
        if profile is None:
            from .research_profile import get_profile
            profile = get_profile()
    except Exception:
        return ""
    for field_name in ("inclusion_dims", "exclusion_dims"):
        for ln in str(profile.get(field_name, "")).splitlines():
            t = ln.strip()
            if t.startswith(code) and len(t) > len(code) and t[len(code)] in ".．: 、":
                return t
    return ""


def _suggest(c: Cluster, profile) -> str:
    line = dim_line(c.key, profile) if c.kind in ("reason", "bucket") else ""
    ref = "（现行：{}）".format(line) if line else ""
    pct = "{:.0%}".format(c.share)
    if c.kind == "reason":
        return ("排除维度 {k} 可能过严{ref}：近 {w} 周你补勾了 {n} 篇（占全部补勾的 {p}）。"
                "建议复核 research_profile.yaml 的 exclusion_dims 中 {k} 的表述——是否该收窄、加例外条款，"
                "或把这类论文的主题写进 inclusion_dims / interests。").format(
                    k=c.key, ref=ref, w=TH.DRIFT_LOOKBACK_WEEKS, n=c.n, p=pct)
    if c.kind == "semantic":
        return ("这 {n} 篇补勾论文语义相近（占全部补勾的 {p}），可能是画像尚未覆盖的新方向。"
                "建议读完下面的标题，把共同主题补进 inclusion_dims 的某个维度或 interests；"
                "若其实是一次性好奇，忽略即可。").format(n=c.n, p=pct)
    return ("纳入维度 {k} 可能过宽{ref}：近 {w} 周筛选器想收、你却取消了 {n} 篇（占全部取消的 {p}；"
            "默认勾选有锚定偏倚，真实数量更多）。建议复核 inclusion_dims 中 {k} 的表述是否该收紧，"
            "或确认是不是你的兴趣已转移。").format(k=c.key, ref=ref, w=TH.DRIFT_LOOKBACK_WEEKS, n=c.n, p=pct)


_KIND_TITLE = {
    ("reason", "added"): "补勾簇 · 按排除原因",
    ("semantic", "added"): "补勾簇 · 按语义",
    ("bucket", "removed"): "取消簇 · 按纳入维度",
}


def render_report(a: DriftAnalysis, *, profile: Optional[Dict[str, Any]] = None) -> str:
    L = ["# 研究画像漂移提醒（{}）".format(a.window_end.strftime("%Y-%m")), "",
         "窗口：{} → {}（近 {} 周）；确认标签 {} 条，其中补勾（筛选器 EXCLUDE → 你保留）{} 条，"
         "取消（筛选器收 → 你丢弃）{} 条。".format(
             a.window_start.strftime("%Y-%m-%d"), a.window_end.strftime("%Y-%m-%d"),
             TH.DRIFT_LOOKBACK_WEEKS, a.n_labels, a.n_added, a.n_removed),
         "成簇判据：同一原因/簇 ≥ {} 篇且占该类 ≥ {:.0%}（thresholds.DRIFT_*，初值）。".format(
             TH.DRIFT_MIN_CLUSTER, TH.DRIFT_MIN_SHARE),
         "**本报告只提醒，不会自动改画像**；是否改、怎么改由你决定。", ""]
    if a.semantic_status == "unavailable":
        L += ["> 语义聚类未做：本地 embedding 服务不可用，本月只有按原因/维度的簇。", ""]
    for kind, side in (("reason", "added"), ("semantic", "added"), ("bucket", "removed")):
        cs = [c for c in a.clusters if (c.kind, c.side) == (kind, side)]
        if not cs:
            continue
        L += ["## " + _KIND_TITLE[(kind, side)], ""]
        for c in cs:
            L.append("### {}{}：{} 篇（{:.0%}）".format("语义簇 " if kind == "semantic" else "", c.key, c.n, c.share))
            L.append("")
            L.append("建议：" + _suggest(c, profile))
            L.append("")
            for r in sorted(c.rows, key=lambda x: x.get("confirmed_at", "")):
                conf = r.get("filter_confidence")
                L.append("- {} {}｜{}{}".format(
                    (r.get("title") or "").strip(),
                    "（置信 {:.2f}）".format(conf) if conf is not None else "",
                    r.get("one_line") or "—",
                    "｜排除原因 " + str(r["exclude_reason"]) if r.get("exclude_reason") and c.side == "added" else ""))
            L.append("")
    return "\n".join(L).rstrip() + "\n"


def summary_text(a: DriftAnalysis) -> str:
    parts = []
    for c in a.clusters:
        what = {"reason": "补勾·原因 " + c.key, "semantic": "补勾·语义簇", "bucket": "取消·维度 " + c.key}[c.kind]
        parts.append("{} {} 篇".format(what, c.n))
    return "；".join(parts)
