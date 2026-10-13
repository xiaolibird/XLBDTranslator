# -*- coding: utf-8 -*-
"""证据充分性标定（WO-4）：在 (sim 下限 × 来源阈值) 网格上量化「误拒」与「漏拒」。

设计：docs/decisions/trust_hardening_design_2026-10-13.md 第 6.6 节。不调 LLM，只用
本地 Ollama 的 embedding（读已有向量库 + 嵌入问题/检索词）与现有生产召回函数：
  正例（应当不拒答）：topics.yaml 全部 topic（retrieve_evidence 生产默认参数）；
                      topics/qa/ 已归档问题（recall_qa_evidence，参数取页面 frontmatter 的 ask_*）；
                      「冷门但库里确实有」的补充问题（NICHE_POSITIVES，需用户确认）。
  反例（应当拒答）：库里绝对没有的话题改写成的问句 + 画像外但医学相关的问句。
口径：每个问题**只召回一次**（生产默认下限 0.55），再对结果按 sim 下限 s 过滤后数独立来源
（keeper 去重、撤稿不计）——与 WO-5 的运行时判据同形（「计数只算 score ≥ 下限的证据」）。

只读：向量库与索引按参数指向的目录读取，**建议先拷一份快照再指过去**（VectorStore.load
虽是读事务，但 WAL 库连接时会碰 -shm 文件）；脚本本身不写 output/，只打印 / 写 --json-out。

复跑：
  python scripts/calibrate_evidence_sufficiency.py --notes-dir <含 embeddings.sqlite3 与
      literature_index.json 的目录> [--topics-config config/topics.yaml] [--json-out X.json]
Ollama 需在 localhost:11434（或 --ollama-url）提供 bge-m3。

WO-4b 新增 `--mode distribution`：不按固定 sim 下限，而是按每个问题自身的全库相似度分布
动态定阈值（coverage.dynamic_sufficiency），并与固定阈值 / 曲线形状 / 离题零分布两个候选
并列比较。口径与结论见 docs/decisions/evidence_sufficiency_distribution_2026-10-13.md。
  python scripts/calibrate_evidence_sufficiency.py --mode distribution --notes-dir <快照> --json-out X.json
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import qa as Q                                     # noqa: E402
from src.scholar import topics as T                                 # noqa: E402
from src.scholar.coverage import (CalibCase, DistCase, count_independent,  # noqa: E402
                                  background_stats, dynamic_grid_rates,
                                  grid_rates, keeper_map, pick_dynamic,
                                  pick_thresholds, query_scores,
                                  source_max_scores, verdict_from_sources)
from src.scholar.embed_store import DB_NAME, VectorStore            # noqa: E402
from src.scholar.embeddings import EmbeddingClient                  # noqa: E402

SIMS = (0.55, 0.58, 0.60, 0.62, 0.65)
KS = (2, 3, 4, 5)

# 冷门但库里确实有的问题。设计要求「由用户补 5 个」，用户尚未给出，这是实现者代拟的
# 占位（取自设计示例与本库已有的窄主题），档案里会标明待用户替换/确认。
NICHE_POSITIVES = [
    "影子变量（shadow variable）在 MNAR 识别中起什么作用？",
    "collider 偏倚在缺失数据分析里是怎么出现的？",
    "Rubin 1976 的可忽略性条件（ignorability）具体要求什么？",
    "自遮蔽缺失（self-masking）为什么会破坏可识别性？",
    "把缺失指示符当作特征加入预测模型，什么情况下有帮助？",
]

# 反例 A：「库里绝对没有」。前 5 条是 thresholds.py QA gap 标定档案里点了名的；
# 其余 3 条该档案没写明，取自 digest 近邻标定的离题探针（量子纠错 / 小麦施肥 / 弱引力透镜）。
NEG_GAP = [
    "AlphaFold 的预测置信度（pLDDT）校准得怎么样？",
    "量子退火相对经典算法的加速比有多大？",
    "中世纪欧洲农业史上三圃制是怎么演变的？",
    "NICU 早产儿喂养耐受性量表有哪些？",
    "机器人抓取任务的样本效率如何提升？",
    "量子纠错码的阈值定理对物理比特错误率有什么要求？",
    "小麦氮肥施用量与产量的响应关系是什么？",
    "弱引力透镜测量宇宙学参数的系统误差来源有哪些？",
]

# 反例 B：画像外但医学相关（有 EHR/医学语境，却不在缺失机制/方法学画像里）
NEG_OFFPROFILE = [
    "医学影像分割任务里常用哪些数据增强策略？",
    "糖尿病视网膜病变眼底照片的深度学习筛查准确率是多少？",
    "新冠疫苗接种后的抗体滴度衰减规律是什么？",
    "CRISPR 基因编辑的脱靶效应如何检测？",
    "阿尔茨海默病的 amyloid PET 生物标志物阈值是多少？",
]


# WO-4b：离题探针，用来给「离题零分布」候选建经验分布，**与上面 13 条评估反例互不重叠**
# （否则零分布就是在拿评估集训练）。注意：探针不保证库里真没有相关内容——「脓毒症预警评分」
# 「eGFR 分期」等在 EHR 方法学库里有真实近邻，它们被判「不拒答」未必是判据错，所以探针只作
# 旁证，不进选择规则。
PROBES_OFFTOPIC = [
    "如何用强化学习训练围棋 AI？", "钙钛矿太阳能电池的稳定性问题如何解决？",
    "大语言模型的上下文长度扩展有哪些方法？", "古罗马水道的工程技术特点是什么？",
    "深海热液喷口的微生物群落有什么特征？", "高温超导体的配对机制目前有哪些理论？",
    "城市交通拥堵的排队论建模方法有哪些？", "自动驾驶的激光雷达点云目标检测怎么做？",
    "区块链共识算法的拜占庭容错性能比较", "咖啡豆烘焙程度对风味物质的影响",
    "国际象棋引擎的剪枝搜索算法", "地震预警系统的 P 波检测延迟是多少？",
]
PROBES_MEDICAL = [
    "肺癌低剂量 CT 筛查的假阳性率是多少？", "抗生素耐药性在 ICU 中的传播机制",
    "乳腺钼靶影像的 AI 辅助诊断准确率", "慢性肾病的 eGFR 分期标准",
    "脓毒症早期预警评分的敏感性", "帕金森病的深部脑刺激疗效",
    "基因组关联分析 GWAS 的样本量要求", "心电图房颤检测的深度学习方法",
    "病理切片全视野图像的多实例学习", "新生儿黄疸的光疗阈值",
    "流感疫苗的有效性逐年变化", "COVID-19 长期症状的流行病学特征",
]

DIST_FLOORS = (0.55, 0.57, 0.58, 0.59, 0.60)
DIST_ES = (0.5, 1.0, 2.0, 4.0, 8.0)
DIST_MS = (2, 3, 4, 5)
TRIVIAL_TOP1 = 0.55   # top1 低于生产召回 min_sim：本来就召回 0 条、qa 不落盘的平凡反例


class _CachedEmbed:
    """问题 / 检索词的向量缓存（同一批文本不重复请求）。"""

    def __init__(self, client):
        self._c, self._m = client, {}

    def embed(self, texts, *a, **kw):
        key = tuple(texts)
        if key not in self._m:
            self._m[key] = self._c.embed(list(texts), *a, **kw)
        return self._m[key]


def _counts(evidences, keeper_of) -> Dict[float, int]:
    return {s: count_independent(evidences, keeper_of, s) for s in SIMS}


def _case(name, group, positive, evidences, keeper_of) -> Tuple[CalibCase, Dict[str, Any]]:
    c = CalibCase(name=name, group=group, positive=positive, counts=_counts(evidences, keeper_of))
    meta = {"name": name, "group": group, "positive": positive,
            "n_evidence": len(evidences),
            "top1": round(max((e.score for e in evidences), default=0.0), 3),
            "counts": {str(s): n for s, n in c.counts.items()}}
    return c, meta


def _load_archived_qa(qa_dir: Path) -> List[Tuple[str, Q.ArchivedAskParams]]:
    out = []
    for p in sorted(qa_dir.glob("qa-*.md")):
        _txt, fm, _prev = T.read_existing(p)
        if not fm or not fm.get("question"):
            continue
        out.append((str(fm["question"]), Q.read_archived_ask_params(fm)))
    return out


def _retrieval_mask(spec, store):
    """与 topics.retrieve_evidence 同口径的 chunk 掩码：句级 highlight、roles 过滤、排除主观批注分节。"""
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


def _auc(pos, neg):
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def _loo_dynamic(cases):
    """留一：每次拿掉一个样本，用其余样本按选择规则选参数，再看被拿掉的样本。
    正例被拒 = 留一误拒；反例未被拒 = 留一漏拒。"""
    fr, ms = [], []
    for i, c in enumerate(cases):
        rest = [x for j, x in enumerate(cases) if j != i]
        pick = pick_dynamic(dynamic_grid_rates(rest, DIST_FLOORS, DIST_ES, DIST_MS))
        if pick is None:
            (fr if c.positive else ms).append(c.name)   # 选不出参数：按最坏计
            continue
        v = verdict_from_sources(c.source_scores, c.bg, floor=pick["floor"],
                                 expected_chance=pick["e"], min_sources=pick["m"])
        if c.positive and v.refuse:
            fr.append(c.name)
        if not c.positive and not v.refuse:
            ms.append(c.name)
    return fr, ms


def _loo_fixed(fcases):
    """固定阈值（sim×k）的留一，与 `_loo_dynamic` 同口径，供同口径对照。"""
    fr, ms = [], []
    for i, c in enumerate(fcases):
        rest = [x for j, x in enumerate(fcases) if j != i]
        pick = pick_thresholds(grid_rates(rest, SIMS, KS))
        if pick is None:
            (fr if c.positive else ms).append(c.name)
            continue
        ref = c.counts[pick["sim"]] < pick["k"]
        if c.positive and ref:
            fr.append(c.name)
        if not c.positive and not ref:
            ms.append(c.name)
    return fr, ms


def run_distribution(args) -> int:
    import numpy as np
    notes = Path(args.notes_dir)
    index = json.loads((notes / "literature_index.json").read_text(encoding="utf-8"))
    keeper_of = keeper_map(index)
    t0 = time.time()
    store = VectorStore.load(notes / DB_NAME)
    t_load = time.time() - t0
    client = EmbeddingClient(base_url=args.ollama_url, model=args.embed_model)
    specs = T.load_topic_specs(args.topics_config)
    qa_dir = Path(args.qa_dir) if args.qa_dir else notes / "topics" / "qa"

    items = []   # (name, group, positive, spec)
    for sp in specs:
        items.append(("topic:" + sp.slug, "topic", True, sp))
    for q, prm in _load_archived_qa(qa_dir):
        sp = Q.build_qa_spec(q, extra_queries=prm.extra_queries if prm.has_params else (),
                             roles=prm.roles if prm.has_params else ())
        items.append(("qa:" + q[:30], "qa_archived", True, sp))
    for group, positive, qs in (("qa_niche", True, NICHE_POSITIVES), ("neg_gap", False, NEG_GAP),
                                ("neg_offprofile", False, NEG_OFFPROFILE),
                                ("probe_off", False, PROBES_OFFTOPIC), ("probe_med", False, PROBES_MEDICAL)):
        for q in qs:
            items.append(("{}:{}".format(group, q[:30]), group, positive, Q.build_qa_spec(q)))

    cases, feats = [], []
    t_scan, t_mask = [], []
    try:
        for name, group, positive, sp in items:
            vecs = np.asarray(client.embed(list(sp.queries)), dtype=np.float32)
            t1 = time.time()
            mask = _retrieval_mask(sp, store)
            t_mask.append(time.time() - t1)
            t1 = time.time()
            sc = query_scores(store.mat, vecs, mask)
            t_scan.append(time.time() - t1)
            cks = [store.records[i]["citekey"] for i in np.where(mask)[0]]
            ss = source_max_scores(sc, cks, keeper_of)
            bg = background_stats(sc)
            cases.append(DistCase(name, group, positive, ss, bg))
            feats.append({"name": name, "group": group, "positive": positive, "n_chunks": bg.n,
                          "mean": bg.mean, "std": bg.std, "top": [round(v, 4) for v in ss[:8]]})
    finally:
        client.close()

    def role(c):
        if c.group in ("topic", "qa_archived"):
            return "pos"
        if c.group == "qa_niche":
            return "niche"
        if c.group.startswith("probe"):
            return "probe"
        return "triv" if (c.source_scores[0] if c.source_scores else 0.0) < TRIVIAL_TOP1 else "neg"
    for c, f in zip(cases, feats):
        f["role"] = role(c)
    by = lambda *rs: [c for c, f in zip(cases, feats) if f["role"] in rs]
    core = by("pos", "niche", "neg", "triv")      # 进选择规则 / 留一
    pos_all, niche = by("pos", "niche"), by("niche")

    print("# 分布判据标定（WO-4b）")
    print("向量库 {} 条（加载 {:.2f}s）；单问题：构 mask {:.0f}ms + 全库相似度扫描 {:.0f}ms（中位；矩阵乘 + 取 max，不含 embedding 请求）".format(
        len(store.records), t_load, 1000 * float(np.median(t_mask)), 1000 * float(np.median(t_scan))))
    print("\n## 每样本特征（top = 前 5 个独立来源最高分；z_k = (s_k - mean)/std；Tdyn = mean + z*·std，z* 取 e=1）")
    print("| 角色 | 问题 | N | mean | std | top1 | top3 | top5 | z1 | z3 | z5 | z*(e=1) | Tdyn | T=max(.58,Tdyn) | n_src(≥T) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    from src.scholar.coverage import chance_exceedance_z
    for c, f in zip(cases, feats):
        ss = c.source_scores
        zk = lambda k: (ss[k - 1] - c.bg.mean) / c.bg.std if len(ss) >= k and c.bg.std else float("nan")
        v = verdict_from_sources(ss, c.bg, floor=0.58, expected_chance=1.0, min_sources=3)
        zs = chance_exceedance_z(c.bg.n, 1.0)
        sg = lambda k: ss[k - 1] if len(ss) >= k else float("nan")
        print("| {} | {} | {} | {:.3f} | {:.3f} | {:.3f} | {:.3f} | {:.3f} | {:.1f} | {:.1f} | {:.1f} | {:.2f} | {:.3f} | {:.3f} | {} |".format(
            f["role"], c.name.split(":", 1)[1], c.bg.n, c.bg.mean, c.bg.std, sg(1), sg(3), sg(5),
            zk(1), zk(3), zk(5), zs, c.bg.mean + zs * c.bg.std, v.threshold, v.n_sources))
        f["verdict_floor058_e1"] = {"threshold": v.threshold, "n_sources": v.n_sources}

    # ---- 候选 B：动态阈值网格 ----
    rows = dynamic_grid_rates(core, DIST_FLOORS, DIST_ES, DIST_MS)
    print("\n## 候选 B：动态阈值网格（正例=pos+niche 共 {}，反例 {}（含平凡 {}））".format(
        len(pos_all), len([c for c in core if not c.positive]), len(by("triv"))))
    print("| floor | e | m | 误拒 | 漏拒(13) | 其中平凡 | 余量 | 漏掉的反例 |")
    print("|---|---|---|---|---|---|---|---|")
    triv_names = {c.name for c in by("triv")}
    for r in rows:
        if r["false_refusals"] == 0 or (r["floor"] == 0.58 and r["e"] in (1.0, 2.0)):
            print("| {} | {} | {} | {}/{} | {}/{} | {} | {} | {} |".format(
                r["floor"], r["e"], r["m"], r["false_refusals"], r["n_pos"], r["missed_refusals"], r["n_neg"],
                sum(1 for n in r["missed_names"] if n in triv_names), r["margin"],
                "; ".join(n.split(":", 1)[1][:10] for n in r["missed_names"])))
    pick = pick_dynamic(rows)
    print("\n选择规则（误拒=0 → 漏拒最少 → 余量大 → floor 小 → e 大 → m 小）:", json.dumps(pick, ensure_ascii=False))
    fr, ms = _loo_dynamic(core)
    print("留一（{} 个样本）：误拒 {} {}；漏拒 {} {}".format(len(core), len(fr), fr, len(ms), ms))
    pr = by("probe")
    if pick:
        pm = [c.name for c in pr if not verdict_from_sources(c.source_scores, c.bg, floor=pick["floor"],
              expected_chance=pick["e"], min_sources=pick["m"]).refuse]
        print("旁证：{} 条离题探针里 {} 条未被拒 {}".format(len(pr), len(pm), [n.split(":", 1)[1][:10] for n in pm]))

    # ---- 候选 A：固定阈值（全库口径复算，用于同口径对照） ----
    print("\n## 候选 A：固定 sim 下限（全库口径：来源最高分 ≥ s 的 keeper 数）")
    fcases = [CalibCase(c.name, c.group, c.positive, {s: sum(1 for v in c.source_scores if v >= s) for s in SIMS})
              for c in core]
    frows = grid_rates(fcases, SIMS, KS)
    fpick = pick_thresholds(frows)
    print("选择规则结果:", json.dumps(fpick, ensure_ascii=False))
    ffr, fms = _loo_fixed(fcases)
    print("留一（同口径）：误拒 {} {}；漏拒 {} {}".format(len(ffr), ffr, len(fms), fms))

    # ---- 候选 C/3：单特征可分性（AUC） + 离题零分布 ----
    print("\n## 候选 3 / 单特征可分性：AUC（正例 pos+niche vs 非平凡反例 neg；括号=对全部 13 条反例）")
    def feat(c, kind, k):
        ss = c.source_scores
        if len(ss) < k:
            return -1.0
        return {"raw": ss[k - 1], "z": (ss[k - 1] - c.bg.mean) / c.bg.std if c.bg.std else 0.0,
                "gap": ss[0] - ss[k - 1]}[kind]
    neg_nt, neg_all = by("neg"), by("neg", "triv")
    print("| 特征 | AUC(neg) | AUC(neg+triv) | AUC(probe 24) |")
    print("|---|---|---|---|")
    auc_rows = {}
    for kind in ("raw", "z", "gap"):
        for k in (1, 3, 5, 8):
            if kind == "gap" and k == 1:
                continue
            a1 = _auc([feat(c, kind, k) for c in pos_all], [feat(c, kind, k) for c in neg_nt])
            a2 = _auc([feat(c, kind, k) for c in pos_all], [feat(c, kind, k) for c in neg_all])
            a3 = _auc([feat(c, kind, k) for c in pos_all], [feat(c, kind, k) for c in pr])
            auc_rows["{}{}".format(kind, k)] = [a1, a2, a3]
            print("| {}{} | {:.3f} | {:.3f} | {:.3f} |".format(kind, k, a1, a2, a3))
    print("\n## 候选 2：离题零分布（24 条探针的 s_k 做经验分布，p = (1+#{探针 ≥ 该问题})/(1+24)；p>α 则拒答）")
    print("| s_k | α | 误拒 | 漏拒(13) |")
    print("|---|---|---|---|")
    null_rows = []
    for k in (1, 3, 5):
        null = np.array([feat(c, "raw", k) for c in pr])
        for a in (0.05, 0.1, 0.2, 0.3):
            pv = lambda c: (1 + float((null >= feat(c, "raw", k)).sum())) / (1 + len(null))
            f_ = sum(1 for c in pos_all if pv(c) > a)
            m_ = sum(1 for c in neg_all if pv(c) <= a)
            null_rows.append({"k": k, "alpha": a, "false_refusals": f_, "missed": m_})
            print("| s_{} | {} | {}/{} | {}/{} |".format(k, a, f_, len(pos_all), m_, len(neg_all)))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"features": feats, "dynamic_rows": rows, "dynamic_pick": pick, "loo": {"false_refusals": fr, "missed": ms},
             "fixed_pick": fpick, "auc": auc_rows, "null_rows": null_rows,
             "fixed_loo": {"false_refusals": ffr, "missed": fms},
             "timing": {"load_s": t_load, "mask_ms_median": 1000 * float(np.median(t_mask)),
                        "scan_ms_median": 1000 * float(np.median(t_scan))}},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes-dir", required=True,
                    help="含 embeddings.sqlite3 与 literature_index.json 的目录（只读；建议用快照）")
    ap.add_argument("--topics-config", default="config/topics.yaml")
    ap.add_argument("--qa-dir", default=None, help="已归档问答目录，默认 <notes-dir>/topics/qa（可指向别处的快照）")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--mode", choices=("fixed", "distribution"), default="fixed",
                    help="fixed=WO-4 固定阈值网格（默认）；distribution=WO-4b 分布判据分析")
    args = ap.parse_args()
    if args.mode == "distribution":
        return run_distribution(args)

    notes = Path(args.notes_dir)
    index = json.loads((notes / "literature_index.json").read_text(encoding="utf-8"))
    keeper_of = keeper_map(index)
    store = VectorStore.load(notes / DB_NAME)
    client = EmbeddingClient(base_url=args.ollama_url, model=args.embed_model)
    emb = _CachedEmbed(client)
    specs = T.load_topic_specs(args.topics_config)
    qa_dir = Path(args.qa_dir) if args.qa_dir else notes / "topics" / "qa"

    cases: List[CalibCase] = []
    metas: List[Dict[str, Any]] = []

    def add(c_m):
        cases.append(c_m[0])
        metas.append(c_m[1])

    try:
        # 概念页：生产默认参数（retrieve_evidence 的默认 min_sim / alpha / per_paper_cap）
        for spec in specs:
            ev = T.retrieve_evidence(spec, store, emb)
            add(_case("topic:" + spec.slug, "topic", True, ev, keeper_of))
        # 已归档问答：参数取页面 frontmatter 的 ask_*（老页无参数则用生产默认）
        for q, prm in _load_archived_qa(qa_dir):
            spec = Q.build_qa_spec(q, extra_queries=prm.extra_queries if prm.has_params else (),
                                   roles=prm.roles if prm.has_params else (),
                                   max_evidence=(prm.max_evidence if prm.has_params and prm.max_evidence
                                                 else Q.DEFAULT_QA_MAX_EVIDENCE))
            kw = {}
            if prm.has_params and prm.min_sim is not None:
                kw["min_sim"] = prm.min_sim
            if prm.has_params and prm.per_paper_cap:
                kw["per_paper_cap"] = prm.per_paper_cap
            rec = Q.recall_qa_evidence(spec, store, emb, **kw)
            add(_case("qa:" + q[:36], "qa_archived", True, rec.evidences, keeper_of))
        # 默认参数的问答召回（与 ask_notes 默认一致）
        groups = [("qa_niche", True, NICHE_POSITIVES), ("neg_gap", False, NEG_GAP),
                  ("neg_offprofile", False, NEG_OFFPROFILE)]
        for group, positive, qs in groups:
            for q in qs:
                spec = Q.build_qa_spec(q)
                rec = Q.recall_qa_evidence(spec, store, emb)
                add(_case("{}:{}".format(group, q[:36]), group, positive, rec.evidences, keeper_of))
    finally:
        client.close()

    qa_cases = [c for c in cases if c.group != "topic"]
    topic_cases = [c for c in cases if c.group == "topic"]

    print("## 逐题独立来源数（行=问题，列=sim 下限）")
    print("| 组 | 问题 | 证据条数 | top1 | " + " | ".join("≥{:.2f}".format(s) for s in SIMS) + " |")
    print("|---|---|---|---|" + "---|" * len(SIMS))
    for m in metas:
        print("| {} | {} | {} | {} | {} |".format(
            m["group"], m["name"].split(":", 1)[1], m["n_evidence"], m["top1"],
            " | ".join(str(m["counts"][str(s)]) for s in SIMS)))

    qa_rows = grid_rates(qa_cases, SIMS, KS)
    print("\n## qa 网格（正例=归档+冷门，反例=gap+画像外）：误拒 / 漏拒")
    print("| sim | k | 误拒 | 漏拒 | 正例余量 |")
    print("|---|---|---|---|---|")
    for r in qa_rows:
        print("| {:.2f} | {} | {}/{} | {}/{} | {} |".format(
            r["sim"], r["k"], r["false_refusals"], r["n_pos"],
            r["missed_refusals"], r["n_neg"], r["margin"]))
    pick = pick_thresholds(qa_rows)
    print("\n## 选择规则结果（误拒=0 前提下漏拒最少）")
    print(json.dumps(pick, ensure_ascii=False) if pick else "无误拒=0 的组合：停下交用户决定")

    topic_rows = grid_rates(topic_cases, SIMS, KS)
    print("\n## topic 网格（仅正例）：误拒")
    print("| sim | k | 误拒 | 余量 |")
    print("|---|---|---|---|")
    for r in topic_rows:
        print("| {:.2f} | {} | {}/{} | {} |".format(
            r["sim"], r["k"], r["false_refusals"], r["n_pos"], r["margin"]))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"cases": metas, "qa_rows": qa_rows, "topic_rows": topic_rows, "pick": pick},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
