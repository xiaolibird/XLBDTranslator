# -*- coding: utf-8 -*-
"""topics 相对判据标定：用 config/topics.yaml 的真实 queries 在当前向量库上出分布表。

背景（2026-08-21 批 P1a）：topics.py 此前用全局绝对阈值 DEFAULT_MIN_SIM=0.55，
08-17 实测绝对阈值与查询长度/语言强耦合（强 query 下 0.55~0.60 混入边缘证据，
冷门 query 又召回十来条是诚实信号）。改为 per-query 相对判据：
    eff_min = max(floor, alpha * top1)
本脚本对每个 topic×query 输出 top1 与各候选 (floor, alpha) 下的召回数/门槛值，
标定表落 docs/decisions/（人工选定 alpha 后写回 topics.py 常量与注释）。

复跑：python scripts/calibrate_topics_threshold.py [--json]
改动检索语料（如摘要喂厚，虽不动 highlight 向量）后建议复跑核对分布无漂移。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from src.scholar.paths import repo_path                                    # noqa: E402
from src.scholar.settings import ScholarSettings                           # noqa: E402
from src.scholar.embeddings import EmbeddingClient, resolve_embedding_base_url  # noqa: E402
from src.scholar.embed_store import DB_NAME, VectorStore                   # noqa: E402
from src.scholar.topics import load_topic_specs, DEFAULT_CONFIG, DEFAULT_MIN_SIM  # noqa: E402

ALPHAS = (0.75, 0.80, 0.85, 0.90)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--topics-config", default=DEFAULT_CONFIG)
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    cfg = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
    notes_dir = repo_path(settings.processing.notes_dir)
    store = VectorStore.load(notes_dir / DB_NAME)
    client = EmbeddingClient(base_url=resolve_embedding_base_url(settings.llm),
                             model=settings.llm.embedding_model)
    specs = load_topic_specs(repo_path(args.topics_config))

    rows = []
    try:
        for spec in specs:
            role_filter = set(spec.roles) if spec.roles else None
            exclude_prefixes = tuple(spec.exclude_sections) if spec.exclude_sections else ()
            mask = np.zeros(len(store.records), dtype=bool)
            for i, r in enumerate(store.records):
                if r.get("level") != "highlight":
                    continue
                if role_filter and r.get("role") not in role_filter:
                    continue
                if exclude_prefixes and str(r.get("section") or "").startswith(exclude_prefixes):
                    continue
                mask[i] = True
            for q in spec.queries:
                vec = client.embed([q])[0]
                hits = store.search(vec, mask=mask, top_k=200)
                scores = np.array([s for _, s in hits], dtype=np.float64)
                top1 = float(scores[0]) if scores.size else 0.0
                row = {"topic": spec.slug, "query": q, "top1": round(top1, 3),
                       "n_abs_055": int((scores >= DEFAULT_MIN_SIM).sum())}
                for a in ALPHAS:
                    eff = max(DEFAULT_MIN_SIM, a * top1)
                    row["a{}".format(int(a * 100))] = {
                        "eff_min": round(eff, 3), "n": int((scores >= eff).sum())}
                rows.append(row)
    finally:
        client.close()

    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    print("| topic | query | top1 | n@0.55 |" + "".join(
        " α={} (门槛/召回) |".format(a) for a in ALPHAS))
    print("|---|---|---|---|" + "---|" * len(ALPHAS))
    for r in rows:
        cells = "".join(" {:.3f}/{} |".format(r["a{}".format(int(a * 100))]["eff_min"],
                                              r["a{}".format(int(a * 100))]["n"])
                        for a in ALPHAS)
        print("| {} | {} | {} | {} |{}".format(
            r["topic"], r["query"][:38], r["top1"], r["n_abs_055"], cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
