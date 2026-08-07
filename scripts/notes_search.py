# -*- coding: utf-8 -*-
"""札记库语义检索 CLI（hybrid 版，phase 2；核心逻辑在 src/scholar/embed_store.py）。

与 `scripts/notes_query.py` 的分工：notes_query 是精确子串 AND 匹配（确切术语/
citekey/role 硬门槛场景用它）；本工具是语义检索，专治"中文找英文表述"（如
"缺失机制不可忽略" 查不到 "informative missingness"）与换述同义词。两者互补，
不是替代——notes_query 一行不改。

用法（仓库根运行）：
    python scripts/notes_search.py 缺失机制不可忽略
    python scripts/notes_search.py informative missingness --role citable --cite
    python scripts/notes_search.py 图神经网络 插补 --level paper --json
    python scripts/notes_search.py informative missingness --mode sparse   # 纯关键词，不需要 Ollama

三种 --mode：
  dense  ：query 整句嵌入一次，与向量库做余弦相似度（原 phase 1 逻辑）
  sparse ：BM25(k1=1.2, b=0.75) 纯关键词倒排检索，分词复用 vault.tokenize（英文词+
           中文2-gram），不调用 Ollama，query 走 --mode dense/hybrid 才需要 embedding
  hybrid ：默认模式。dense 与 sparse 各自在 paper 级、highlight 级分别取
           top-200（TOP_K_PER_LEVEL），RRF(k=60) 融合排序；展示用的 score 优先给 dense 余弦（人更好理解 0~1 的
           数），只有 dense 没命中、纯靠关键词命中的条目才展示 RRF 分并标 [关键词]

覆盖面警告：句级证据只覆盖库内 480 篇精读文献（23%）。若某篇只有 paper 级命中，
且它在全库中确实没有任何 highlight chunk（不是"这次没搜到"，是真的没有），会在
该条结果下加一行提示——避免被误读为"库里没有这方面内容"。

退出码：0=有命中；1=无命中；2=向量库缺失/损坏/模型与配置不一致；3=Ollama embedding
不可用（与 notes_embed.py 的 3 对齐，wrapper/launchd 可区分"重建库"2 vs "起 Ollama"3）。
--mode sparse 不需要 Ollama，纯向量库缺失/损坏才会走到 2。
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from src.scholar.paths import repo_path                                   # noqa: E402
from src.scholar.schema import ScholarSettings                            # noqa: E402
from src.scholar.embeddings import (                                      # noqa: E402
    EmbeddingClient, EmbeddingError, resolve_embedding_base_url,
)
from src.scholar.embed_store import DB_NAME, VectorStore, VectorStoreError, model_matches  # noqa: E402
from src.scholar.vault import tokenize                                    # noqa: E402

INDEX_NAME = "literature_index.json"

ROLE_HINT = {"citable": "可引用证据", "refutable": "可反驳观点", "method": "方法论借鉴"}
TIER_ORDER = {"high": 0, "mid": 1, "low": 2}
TIER_EMOJI = {"high": "🔴", "mid": "🟠", "low": "🟢"}
MONTH_RE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")  # YYYY[-MM[-DD]]，月份/日语义校验
MAX_SHOWN_HITS = 4          # 人读模式每条最多展示几句命中（--json 不截断）
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
TOP_K_PER_LEVEL = 200  # RRF 只融合列表内名次；截断越小越易漏掉"两路都中等但 RRF 真值高"的条目。argpartition O(n) 成本≈0


def _split_paper_text(text: str):
    """paper 级 chunk 文本是 chunks_from_index 拼的 title[\\ntitle_zh 之后是 one_line]。
    还原展示用的 (title, one_line)。"""
    if not text:
        return "", ""
    parts = text.split("\n", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _build_mask(store: VectorStore, args) -> np.ndarray:
    """元数据布尔掩码：role/tier/month(前缀)/series/full-text-only。
    role 只对 highlight 级 chunk 有意义——paper 级 chunk 的 role 恒为 None，
    传了 --role 时它们自然被滤掉，不需要单独分支。"""
    n = len(store.records)
    mask = np.ones(n, dtype=bool)
    for i, r in enumerate(store.records):
        if args.role and r.get("role") != args.role:
            mask[i] = False
            continue
        if args.tier and r.get("tier") != args.tier:
            mask[i] = False
            continue
        if args.month and not (r.get("month") or "").startswith(args.month):
            mask[i] = False
            continue
        if args.series and r.get("series") != args.series:
            mask[i] = False
            continue
        if args.full_text_only and not r.get("has_full_text"):
            mask[i] = False
            continue
    return mask


def _bm25_search(store: VectorStore, mask: np.ndarray, query_tokens: List[str],
                  top_k: int = TOP_K_PER_LEVEL, k1: float = BM25_K1, b: float = BM25_B
                  ) -> List[Tuple[int, float]]:
    """纯 Python BM25，仅在掩码后的候选集上建倒排（10k 文档量级，无需缓存）。

    只统计 query 词在候选文档里的词频（tf_per_doc 只存 query 词），不是全量倒排——
    候选集与 query 词表都小，没必要为一次性检索建完整倒排索引。
    """
    if not mask.any() or not query_tokens:
        return []
    idxs = np.nonzero(mask)[0]
    doc_tokens = [tokenize(store.records[int(i)]["text"]) for i in idxs]
    doc_len = np.array([len(t) for t in doc_tokens], dtype=np.float64)
    n = len(doc_tokens)
    avgdl = doc_len.mean() if n else 0.0
    if avgdl <= 0:
        return []

    q_terms = set(query_tokens)
    df: Dict[str, int] = {t: 0 for t in q_terms}
    tf_per_doc: List[Dict[str, int]] = []
    for toks in doc_tokens:
        c: Dict[str, int] = {}
        for t in toks:
            if t in q_terms:
                c[t] = c.get(t, 0) + 1
        tf_per_doc.append(c)
        for t in c:
            df[t] += 1
    idf = {t: math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1) for t in q_terms}

    scores = np.zeros(n, dtype=np.float64)
    for pos, c in enumerate(tf_per_doc):
        if not c:
            continue
        denom_base = k1 * (1 - b + b * doc_len[pos] / avgdl)
        s = 0.0
        for t, tf in c.items():
            s += idf[t] * (tf * (k1 + 1)) / (tf + denom_base)
        scores[pos] = s

    order = np.argsort(-scores)
    result: List[Tuple[int, float]] = []
    for pos in order[: min(top_k, n)]:
        if scores[pos] <= 0:
            break
        result.append((int(idxs[pos]), float(scores[pos])))
    return result


def _level_hits(store: VectorStore, mask: np.ndarray, mode: str,
                 query_vec: Optional[np.ndarray], query_tokens: List[str],
                 min_score: float, top_k: int = TOP_K_PER_LEVEL, rrf_k: int = RRF_K
                 ) -> List[Tuple[int, float, str, float]]:
    """按 mode 检索一个 level（paper 或 highlight），返回 (idx, score, kind, sort_score)。

    - score/kind：展示值。dense=cosine，sparse=bm25，hybrid 下优先展示 dense 余弦
      （kind=cosine），dense 没命中、纯靠关键词命中的条目展示 RRF 分（kind=rrf）
    - sort_score：组内/组间排序用的分。dense/sparse 模式下等于 score；hybrid 模式
      下统一用 RRF 分排序（两路量纲不可比，融合排序不能拿余弦和 bm25 直接比大小，
      但展示给人看时余弦更好懂，所以两者分离）
    - min_score 只过滤 dense 候选（cosine 分数），语义见模块 docstring
    """
    if mode == "dense":
        if not mask.any():
            return []
        hits = store.search(query_vec, mask=mask, top_k=top_k)
        return [(idx, s, "cosine", s) for idx, s in hits if s >= min_score]

    if mode == "sparse":
        hits = _bm25_search(store, mask, query_tokens, top_k=top_k)
        return [(idx, s, "bm25", s) for idx, s in hits]

    # hybrid
    dense_hits = []
    if mask.any():
        dense_hits = [(idx, s) for idx, s in store.search(query_vec, mask=mask, top_k=top_k)
                      if s >= min_score]
    sparse_hits = _bm25_search(store, mask, query_tokens, top_k=top_k)
    dense_rank = {idx: r for r, (idx, _s) in enumerate(dense_hits, start=1)}
    sparse_rank = {idx: r for r, (idx, _s) in enumerate(sparse_hits, start=1)}
    dense_score = dict(dense_hits)

    out: List[Tuple[int, float, str, float]] = []
    for idx in set(dense_rank) | set(sparse_rank):
        rrf = 0.0
        if idx in dense_rank:
            rrf += 1.0 / (rrf_k + dense_rank[idx])
        if idx in sparse_rank:
            rrf += 1.0 / (rrf_k + sparse_rank[idx])
        if idx in dense_score:
            out.append((idx, dense_score[idx], "cosine", rrf))
        else:
            out.append((idx, rrf, "rrf", rrf))
    out.sort(key=lambda t: -t[3])
    return out


def _load_store(db_path: Path, expected_model: str):
    """load 校验：库缺失/损坏 -> (None, 提示)；model 与配置不一致 -> (None, 提示)。"""
    try:
        store = VectorStore.load(db_path)
    except VectorStoreError as e:
        return None, str(e)
    if expected_model and store.model and not model_matches(store.model, expected_model):
        return None, (
            "向量库使用的 embedding 模型是 '{}'，配置当前模型是 '{}'（不一致会导致向量空间"
            "不可比，检索结果无意义）\n请跑：PYTHONPATH=. python scripts/notes_embed.py --full".format(
                store.model, expected_model))
    return store, None


def _freshness_hint(store: VectorStore, notes_dir: Path):
    """best-effort：新鲜度告警不该因索引读取失败而炸检索本身。"""
    try:
        index_path = notes_dir / INDEX_NAME
        if not index_path.exists():
            return None
        data = json.loads(index_path.read_text(encoding="utf-8"))
        return store.freshness_warning(data.get("generated_at"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="札记库语义检索（hybrid，phase 2）")
    ap.add_argument("query", nargs="+", help="查询词/短语（拼成一个语义 query，非逐词 AND）")
    ap.add_argument("--role", choices=["citable", "refutable", "method"],
                    help="只看该角色的句子：citable=可引证据 refutable=可反驳 method=方法借鉴")
    ap.add_argument("--tier", choices=["high", "mid", "low"], help="优先级层过滤")
    ap.add_argument("--month", help="限定月份，YYYY / YYYY-MM / YYYY-MM-DD（前缀匹配）")
    ap.add_argument("--series", choices=["auto", "manual"], help="auto=流水线精读 manual=手动深读")
    ap.add_argument("--full-text-only", action="store_true", help="只要有全文精读的条目")
    ap.add_argument("--mode", choices=["dense", "hybrid", "sparse"], default="hybrid",
                    help="检索模式（默认 hybrid）：dense=纯向量余弦 "
                         "sparse=纯关键词BM25(不需要Ollama) hybrid=两者RRF融合")
    ap.add_argument("--level", choices=["auto", "paper", "highlight"], default="auto",
                    help="auto=两级都查（默认） paper=只查标题+一句话 highlight=只查精读句")
    ap.add_argument("--min-score", type=float, default=0.4,
                    help="最低余弦相似度（默认 0.4）。只过滤 dense 侧候选；--mode sparse "
                         "下不生效，--mode hybrid 下只过滤参与 RRF 融合的 dense 候选")
    ap.add_argument("--limit", type=int, default=10, help="最多显示条数（默认 10，0=不限）")
    ap.add_argument("--cite", action="store_true", help="只输出可直接粘贴的 [@a; @b] 引用串")
    ap.add_argument("--json", action="store_true", dest="as_json", help="结构化输出（含 total）")
    ap.add_argument("--config", default="config/scholar.env")
    args = ap.parse_args()

    if args.limit < 0:
        ap.error("--limit 不能为负（0=不限量）")
    if args.month and not MONTH_RE.match(args.month):
        ap.error("--month 格式应为 YYYY / YYYY-MM / YYYY-MM-DD，收到：{}".format(args.month))
    if args.level == "paper" and args.role:
        ap.error("--level paper 与 --role 互斥：paper 级 chunk 无 role，该组合必然 0 命中"
                 "（--role 请配 --level auto 或 highlight）")

    query = " ".join(q.strip() for q in args.query if q and q.strip())
    if not query:
        ap.error("查询词为空")

    cfg = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
    notes_dir = repo_path(settings.processing.notes_dir)
    db_path = notes_dir / DB_NAME

    store, err = _load_store(db_path, settings.llm.embedding_model)
    if store is None:
        print(err, file=sys.stderr)
        return 2

    hint = _freshness_hint(store, notes_dir)
    if hint:
        print(hint, file=sys.stderr)

    query_tokens = tokenize(query) if args.mode in ("sparse", "hybrid") else []
    # 查询无法分词（单字中文/纯数字/纯符号/全停用词）时，sparse 必然 0 命中——给针对性提示
    no_query_tokens = args.mode in ("sparse", "hybrid") and not query_tokens

    query_vec = None
    if args.mode in ("dense", "hybrid"):
        client = EmbeddingClient(
            base_url=resolve_embedding_base_url(settings.llm),
            model=settings.llm.embedding_model,
        )
        try:
            query_vec = client.embed([query])[0]
        except EmbeddingError as e:
            print("❌ query 嵌入失败：{}".format(e), file=sys.stderr)
            return 3
        finally:
            client.close()

    base_mask = _build_mask(store, args)
    level_arr = np.array([r["level"] for r in store.records])
    search_paper = args.level in ("auto", "paper")
    search_highlight = args.level in ("auto", "highlight")

    paper_hits: List[Tuple[int, float, str, float]] = []
    if search_paper:
        paper_mask = base_mask & (level_arr == "paper")
        paper_hits = _level_hits(store, paper_mask, args.mode, query_vec, query_tokens,
                                  args.min_score)
    highlight_hits: List[Tuple[int, float, str, float]] = []
    if search_highlight:
        highlight_mask = base_mask & (level_arr == "highlight")
        highlight_hits = _level_hits(store, highlight_mask, args.mode, query_vec, query_tokens,
                                      args.min_score)

    # 全库真相：这个 citekey 是否本就有句级证据（与本次查询命中与否无关）
    highlight_citekeys = {r["citekey"] for r in store.records if r["level"] == "highlight"}
    paper_meta = {r["citekey"]: r for r in store.records if r["level"] == "paper"}

    # 每条 hit 是 (idx, score, kind, sort_score)：score/kind 是展示值（dense 命中给
    # 余弦、纯关键词命中给 RRF/BM25 分并标 kind!=cosine）；sort_score 是排序值（hybrid
    # 下统一用 RRF 分，跟展示值分离——RRF 才是两路真正可比的量纲）
    groups = defaultdict(lambda: {"paper": None, "hits": []})
    for idx, score, kind, sort_score in paper_hits:
        r = store.records[idx]
        g = groups[r["citekey"]]
        if g["paper"] is None or sort_score > g["paper"][2]:
            g["paper"] = (score, kind, sort_score)
    for idx, score, kind, sort_score in highlight_hits:
        r = store.records[idx]
        groups[r["citekey"]]["hits"].append((score, kind, sort_score, r))

    rows = []
    for ck, g in groups.items():
        meta = paper_meta.get(ck)
        if meta is None:
            continue  # 元数据过滤把它的 paper 级记录也滤掉了（理论上不该发生，容错跳过）
        candidates = ([g["paper"]] if g["paper"] else []) + [
            (s, k, ss) for s, k, ss, _r in g["hits"]]
        # 展示分优先 dense 余弦命中（有则给真实余弦分），纯关键词命中才给 RRF 分并标 [关键词]。
        # 跨篇排序与展示解耦：_sort 恒取全篇最优 sort_score（RRF），保证位次可比。
        best_sort = max(t[2] for t in candidates)
        cosine_cands = [c for c in candidates if c[1] == "cosine"]
        if cosine_cands:
            best_score, best_kind = max(cosine_cands, key=lambda t: t[2])[:2]
        else:
            best_score, best_kind = max(candidates, key=lambda t: t[2])[:2]
        title, one_line = _split_paper_text(meta["text"])
        match_level = "highlight" if g["hits"] else "paper"
        no_evidence = not g["hits"] and ck not in highlight_citekeys
        g["hits"].sort(key=lambda t: -t[2])
        rows.append({
            "citekey": ck, "score": best_score, "score_kind": best_kind, "_sort": best_sort,
            "match_level": match_level,
            "year": meta.get("year"), "title": title, "one_line": one_line,
            "tier": meta.get("tier"), "note_file": meta.get("note_file"),
            "note_line": meta.get("note_line"), "hits": g["hits"],
            "no_sentence_evidence": no_evidence,
        })

    rows.sort(key=lambda row: (-row["_sort"], TIER_ORDER.get(row["tier"], 3), row["citekey"]))
    shown = rows if args.limit == 0 else rows[:args.limit]
    truncated = len(rows) - len(shown)

    if args.as_json:
        print(json.dumps({
            "total": len(rows), "shown": len(shown), "truncated": truncated,
            "query": query, "mode": args.mode,
            "results": [{
                "citekey": row["citekey"], "score": round(row["score"], 4),
                "score_kind": row["score_kind"],
                "match_level": row["match_level"], "match_source": "highlight" if row["hits"] else "title",
                "no_sentence_evidence": row["no_sentence_evidence"],
                "year": row["year"],
                "title": row["title"], "one_line": row["one_line"],
                "hits": [{"text": r["text"], "role": r["role"], "section": r["section"],
                          "score": round(s, 4), "score_kind": k}
                         for s, k, _ss, r in row["hits"]],
                "note_file": row["note_file"], "note_line": row["note_line"],
            } for row in shown],
        }, ensure_ascii=False, indent=2))
        return 0 if shown else 1

    if args.cite:
        if not shown:
            print("无命中", file=sys.stderr)
            return 1
        print("[" + "; ".join("@{}".format(row["citekey"]) for row in shown) + "]")
        if truncated:
            print("⚠️ 共 {} 条命中，仅输出前 {} 条（--limit 调整，0=不限）".format(
                len(rows), len(shown)), file=sys.stderr)
        return 0

    if not shown:
        if no_query_tokens:
            if args.mode == "sparse":
                print("查询无法分词（单字中文/纯数字/纯符号），关键词检索必然 0 命中。"
                      "建议用 --mode dense/hybrid 或多字短语。")
            else:
                print("无命中。注：查询无法分词（单字中文/纯数字/纯符号），本次只有语义"
                      "检索实际生效。试试换个说法或降低 --min-score。")
        else:
            print("无命中（试试换个说法、降低 --min-score、去掉 --role/--tier 过滤）")
        return 1
    print("语义命中 {} 篇（显示 {}）{}\n".format(
        len(rows), len(shown),
        "· role={}({})".format(args.role, ROLE_HINT[args.role]) if args.role else ""))
    for row in shown:
        row_kw = "[关键词] " if row["score_kind"] != "cosine" else ""
        print("[@{}] ({}) {} {}{}".format(
            row["citekey"], row["year"] or "?", TIER_EMOJI.get(row["tier"], ""),
            row_kw, (row["title"] or "")[:90]))
        if row["one_line"]:
            print("    ⭐ {}".format(row["one_line"]))
        if row["no_sentence_evidence"]:
            print("    （语义命中标题/一句话用处，该篇无精读句级证据）")
        for s, k, _ss, r in row["hits"][:MAX_SHOWN_HITS]:
            hit_kw = "[关键词] " if k != "cosine" else ""
            print("    [{}·{}] {}{:.2f} {}".format(r["role"], r["section"], hit_kw, s, r["text"]))
        if len(row["hits"]) > MAX_SHOWN_HITS:
            print("    …还有 {} 条命中句（--json 看全部）".format(len(row["hits"]) - MAX_SHOWN_HITS))
        print("    ↳ {}:{}".format(row["note_file"], row["note_line"]))
        print()
    if truncated:
        print("（另有 {} 条未显示，--limit 调整，0=不限）".format(truncated))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
