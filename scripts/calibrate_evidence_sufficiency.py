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
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import qa as Q                                     # noqa: E402
from src.scholar import topics as T                                 # noqa: E402
from src.scholar.coverage import (CalibCase, count_independent,     # noqa: E402
                                  grid_rates, keeper_map, pick_thresholds)
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes-dir", required=True,
                    help="含 embeddings.sqlite3 与 literature_index.json 的目录（只读；建议用快照）")
    ap.add_argument("--topics-config", default="config/topics.yaml")
    ap.add_argument("--qa-dir", default=None, help="已归档问答目录，默认 <notes-dir>/topics/qa（可指向别处的快照）")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

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
