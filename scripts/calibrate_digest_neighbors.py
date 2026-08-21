# -*- coding: utf-8 -*-
"""digest 近邻相似度分布监控（跨论文正例法——原主标定方案，已判失败降级）。

背景（2026-08-21 批 P1b）：此前 0.65 的依据是"精读 highlights 拼 800 字回找自身"，
但摘要喂厚后库侧 paper 文本含摘要、查询侧也是 title+abstract[:800]——自回找变成
近乎同文互查，分数系统性虚高，标出的阈值偏高会漏真近邻。审核裁决：自回找只当
**上界参考**，改跑跨论文正例：

- 正例（宽主题相关）：topics 页证据表里共现的论文对（同一概念页的证据来自语义相关的
  论文集合；取每页贡献证据最多的前若干篇两两组对）。query 侧用其中一篇的
  title+abstract[:800]（与线上 digest 查询形状一致），在 paper 级掩码上查另一篇的分数。
- 负例（离题对）：不同 topics 页（无共享 citekey）的论文随机组对，同样互查。
- 自回找分数另算一列，仅作上界参考。

实跑结论（见 docs/decisions/digest_neighbors_calibration_2026-08.md 方法一）：
正负不可分（正例中位 0.547 vs 负例中位 0.522，共现只保证宽主题相关），
「正例丢弃 ≤5% 的最高阈值」推荐规则给不出可用值。线上 min_sim=0.62 的定案依据
是该文档方法二（90 对逐对人工判定 + 离题探针），与本脚本无关、本脚本复现不了。
本脚本保留仅作分布监控：重嵌向量库/换 embedding 模型后复跑，对比正/负/自回找
分布有无漂移。

复跑：python scripts/calibrate_digest_neighbors.py [--json]
前置：abstracts.json 已回填且向量库已重嵌（喂厚后分布才是线上分布）。
"""
import argparse
import itertools
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from src.scholar.paths import repo_path                                    # noqa: E402
from src.scholar.settings import ScholarSettings                           # noqa: E402
from src.scholar.embeddings import EmbeddingClient, resolve_embedding_base_url  # noqa: E402
from src.scholar.embed_store import (                                      # noqa: E402
    DB_NAME, VectorStore, VectorStoreError, load_abstracts,
)

THRESHOLDS = [round(0.50 + 0.01 * i, 2) for i in range(21)]   # 0.50..0.70
PER_TOPIC_TOP = 6      # 每个概念页取贡献证据最多的前 N 篇组正例对
MAX_PAIRS = 120        # 正/负例对上限（控制 embedding 调用量）


def _topic_paper_sets(topics_dir: Path):
    """每个概念页 -> 证据表里出现的 citekey 计数（从生成区的 [@citekey] 提取）。"""
    out = {}
    for md in sorted(topics_dir.glob("*.md")):
        if md.name.startswith("_"):
            continue
        counts = defaultdict(int)
        for m in re.finditer(r"@([A-Za-z][\w\-]+\d{4}[A-Za-z][\w]*)", md.read_text(encoding="utf-8")):
            counts[m.group(1)] += 1
        if counts:
            out[md.stem] = counts
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    cfg = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
    notes_dir = repo_path(settings.processing.notes_dir)
    store = VectorStore.load(notes_dir / DB_NAME)
    try:
        abstracts = load_abstracts(notes_dir)
    except VectorStoreError as e:
        # 「存在但坏」新契约（load_abstracts 对损坏 sidecar raise 而非返回空）：
        # 标定脚本只读不同步，不会删向量，但坏 sidecar 说明库的厚薄状态不可信，
        # 标出的分布没意义——给可操作提示退出，别让 traceback 收场。
        print("abstracts.json 存在但读不出，先修复再标定：{}".format(e), file=sys.stderr)
        return 2
    if not abstracts:
        print("abstracts.json 缺失/为空——喂厚前标定无意义，先跑 backfill_abstracts.py", file=sys.stderr)
        return 2

    paper_rec = {r["citekey"]: (i, r) for i, r in enumerate(store.records)
                 if r["level"] == "paper"}
    ab_rec = {r["citekey"]: r for r in store.records if r["level"] == "abstract"}
    # 与 workflow._library_neighbors 一致：paper+abstract 双 chunk 掩码
    mask = np.array([r["level"] in ("paper", "abstract") for r in store.records])

    # 论文 -> 线上形状的查询文本（title + abstract[:800]）。摘要在 ab: chunk 的
    # 第 3 段（title\none_line\nabstract 厚召回文本，embed_store 双 chunk 契约）。
    def query_text(ck):
        if ck not in paper_rec or ck not in ab_rec:
            return None
        title = (paper_rec[ck][1]["text"] or "").split("\n")[0]
        parts = (ab_rec[ck]["text"] or "").split("\n")
        abstract = parts[2] if len(parts) > 2 else ""
        if not abstract:
            return None      # 无摘要的论文当不了 digest 形状的 query
        return "{}\n{}".format(title, abstract[:800])

    topic_sets = _topic_paper_sets(notes_dir / "topics")
    if len(topic_sets) < 2:
        print("topics/ 页不足两页，构造不了负例", file=sys.stderr)
        return 2

    pos_pairs, neg_pairs = [], []
    top_by_topic = {}
    for slug, counts in topic_sets.items():
        top = [ck for ck, _ in sorted(counts.items(), key=lambda kv: -kv[1])
               if ck in paper_rec and query_text(ck)][:PER_TOPIC_TOP]
        top_by_topic[slug] = top
        pos_pairs += list(itertools.combinations(top, 2))
    slugs = sorted(top_by_topic)
    for a, b in itertools.combinations(slugs, 2):
        shared = set(topic_sets[a]) & set(topic_sets[b])
        for ck_a in top_by_topic[a][:3]:
            for ck_b in top_by_topic[b][:3]:
                if ck_a not in shared and ck_b not in shared and ck_a != ck_b:
                    neg_pairs.append((ck_a, ck_b))
    rng = np.random.RandomState(20260821)
    if len(pos_pairs) > MAX_PAIRS:
        pos_pairs = [pos_pairs[i] for i in rng.choice(len(pos_pairs), MAX_PAIRS, replace=False)]
    if len(neg_pairs) > MAX_PAIRS:
        neg_pairs = [neg_pairs[i] for i in rng.choice(len(neg_pairs), MAX_PAIRS, replace=False)]

    client = EmbeddingClient(base_url=resolve_embedding_base_url(settings.llm),
                             model=settings.llm.embedding_model)
    vec_cache = {}

    def sim(ck_query, ck_target):
        if ck_query not in vec_cache:
            vec_cache[ck_query] = client.embed([query_text(ck_query)])[0]
        v = vec_cache[ck_query]
        hits = store.search(v, mask=mask, top_k=len(store.records))
        for idx, s in hits:
            if store.records[idx]["citekey"] == ck_target:
                return float(s)
        return 0.0

    try:
        pos_scores = [sim(a, b) for a, b in pos_pairs]
        neg_scores = [sim(a, b) for a, b in neg_pairs]
        # 自回找上界参考（n=30）
        self_cks = [ck for tops in top_by_topic.values() for ck in tops][:30]
        self_scores = [sim(ck, ck) for ck in self_cks]
    finally:
        client.close()

    pos = np.array(pos_scores)
    neg = np.array(neg_scores)
    table = []
    for t in THRESHOLDS:
        table.append({"threshold": t,
                      "pos_drop": round(float((pos < t).mean()), 3),
                      "neg_pass": round(float((neg >= t).mean()), 3)})
    result = {
        "n_pos": len(pos_scores), "n_neg": len(neg_scores),
        "pos_p5": round(float(np.percentile(pos, 5)), 3),
        "pos_median": round(float(np.median(pos)), 3),
        "neg_p95": round(float(np.percentile(neg, 95)), 3),
        "neg_median": round(float(np.median(neg)), 3),
        "self_median": round(float(np.median(self_scores)), 3) if self_scores else None,
        "self_min": round(float(np.min(self_scores)), 3) if self_scores else None,
        "table": table,
        "recommend": max((row["threshold"] for row in table if row["pos_drop"] <= 0.05),
                         default=None),
    }
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    print("正例 {} 对（p5={} 中位={}）  负例 {} 对（p95={} 中位={}）  自回找中位={} 最低={}".format(
        result["n_pos"], result["pos_p5"], result["pos_median"],
        result["n_neg"], result["neg_p95"], result["neg_median"],
        result["self_median"], result["self_min"]))
    print("| 阈值 | 正例丢弃率 | 负例误放率 |")
    print("|---|---|---|")
    for row in table:
        print("| {:.2f} | {:.1%} | {:.1%} |".format(row["threshold"], row["pos_drop"], row["neg_pass"]))
    print("正例丢弃 ≤5% 的最高阈值（仅分布监控参考，非线上 min_sim 依据）：{}".format(
        result["recommend"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
