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
  dense  ：query 整句嵌入一次，与向量库做余弦相似度（原 phase 1 逻辑）。paper 侧
           瘦/厚泳道按原始余弦混排，跨泳道有偏置（见 _paper_side_hits），日常用 hybrid
  sparse ：BM25(k1=1.2, b=0.75) 纯关键词倒排检索，分词走 bm25_tokenize（vault.tokenize
           的英文词+中文2-gram，再补回 EM/MI/IV 这类两字母缩写），不调用 Ollama，
           query 走 --mode dense/hybrid 才需要 embedding
  hybrid ：默认模式。highlight 侧 dense+BM25 两路、paper 侧瘦(p:)/厚(ab:摘要) dense
           +BM25 三路，各取 top-200（TOP_K_PER_LEVEL）后 RRF(k=60) 融合排序；展示用的
           score 优先给 dense 余弦（人更好理解 0~1 的数），只有 dense 没命中、纯靠
           关键词命中的条目才展示 RRF 分并标 [关键词]。最终展示集默认再经
           bge-reranker-v2-m3 交叉编码器重排（--no-rerank 关，语义见 --rerank）。
           注意：--min-score 在 hybrid 下**同样约束 BM25 单路命中**（回查其余弦，见
           _gate_sparse），所以显式传门槛时 [关键词]/score_kind="rrf" 这条展示路径
           基本不会再出现——纯关键词命中的余弦按定义低于门槛。

覆盖面警告：句级证据只覆盖库内约四成精读文献（1095/2743 ≈ 40%，截至 2026-10-13；
实时数以 literature_index.json 为准）。若某篇只有 paper 级命中，
且它在全库中确实没有任何 highlight chunk（不是"这次没搜到"，是真的没有），会在
该条结果下加一行提示——避免被误读为"库里没有这方面内容"。

退出码：0=有命中；1=无命中；2=向量库缺失/损坏/模型与配置不一致；3=Ollama embedding
不可用（与 notes_embed.py 的 3 对齐，wrapper/launchd 可区分"重建库"2 vs "起 Ollama"3）；
4=未预期内部错误（崩溃兜底——Python 未捕获异常默认退出码是 1，会与"无命中"契约撞车，
调用方（如 rag_bench）靠 4 区分"真没搜到"和"程序炸了"）。
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
from src.scholar.settings import load_scholar_settings                    # noqa: E402
from src.scholar.thresholds import NOTES_SEARCH_MIN_SCORE, TOPICS_MIN_SIM  # noqa: E402
from src.scholar.embeddings import (                                      # noqa: E402
    EmbeddingClient, EmbeddingError, resolve_embedding_base_url,
)
from src.scholar.embed_store import (                                      # noqa: E402
    DB_NAME, INDEX_NAME, VectorStore, VectorStoreError, model_matches, read_index_generated_at,
)
from src.scholar.bm25_index import BM25_CACHE_NAME, BM25Index, bm25_tokenize  # noqa: E402
from src.scholar.synthesis_store import SYNTHESIS_DB_NAME, SynthesisStore  # noqa: E402
from src.scholar.vault import tokenize                                    # noqa: E402
from src.scholar import reranker                                          # noqa: E402

ROLE_HINT = {"citable": "可引用证据", "refutable": "可反驳观点", "method": "方法论借鉴"}
TIER_ORDER = {"high": 0, "mid": 1, "low": 2}
TIER_EMOJI = {"high": "🔴", "mid": "🟠", "low": "🟢"}
MONTH_RE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")  # YYYY[-MM[-DD]]，月份/日语义校验
MAX_SHOWN_HITS = 4          # 人读模式每条最多展示几句命中（--json 不截断）
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
TOP_K_PER_LEVEL = 200  # RRF 只融合列表内名次；截断越小越易漏掉"两路都中等但 RRF 真值高"的条目。argpartition O(n) 成本≈0
RERANK_CAP = 100       # 展示集超过它时只重排前 100 条（--limit 0 或大 limit 都会触发，其余保持原序拼在后面）；每对 ~30ms，100 条 ~3s 是上限


def _split_paper_text(text: str):
    """paper 级 (p:) chunk 文本恒为两段：title\\none_line（one_line 空则仅 title）。
    回填摘要在独立的 ab: chunk 里（embed_store.chunks_from_index 一篇双向量方案），
    只参与检索，不经过本函数。这里还原展示用的 (title, one_line)。"""
    if not text:
        return "", ""
    parts = text.split("\n")
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _build_mask(store: VectorStore, args) -> np.ndarray:
    """元数据布尔掩码：role/tier/month(前缀)/series/full-text-only/book。"""
    n = len(store.records)
    book = getattr(args, "book", None)
    if not (args.role or args.tier or args.month or args.series or args.full_text_only or book):
        return np.ones(n, dtype=bool)
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
        if book and book not in (r.get("book_key"), r.get("citekey")):
            mask[i] = False
            continue
    return mask


_BM25_CACHE: Dict[int, Optional[BM25Index]] = {}


def _get_bm25_index(store: VectorStore, db_path: Optional[Path] = None) -> Optional[BM25Index]:
    key = id(store)
    if key in _BM25_CACHE:
        return _BM25_CACHE[key]
    cv = (store.meta.get("content_version") or "").strip() if hasattr(store, "meta") else ""
    if not cv:
        _BM25_CACHE[key] = None
        return None
    cache_path = (db_path.parent / BM25_CACHE_NAME) if db_path else None
    n = len(store.records)
    idx = BM25Index.try_load(cache_path, cv, n) if cache_path else None
    if idx is None:
        idx = BM25Index.build([r.get("text") or "" for r in store.records], content_version=cv)
        if cache_path is not None:
            idx.save(cache_path)
    _BM25_CACHE[key] = idx
    return idx


def _bm25_search_slow(store: VectorStore, mask: np.ndarray, query_tokens: List[str],
                      top_k: int = TOP_K_PER_LEVEL, k1: float = BM25_K1, b: float = BM25_B
                      ) -> List[Tuple[int, float]]:
    """纯 Python BM25（无缓存或测试打桩时的兜底慢路径）。"""
    if not mask.any() or not query_tokens:
        return []
    idxs = np.nonzero(mask)[0]
    doc_tokens = [bm25_tokenize(store.records[int(i)]["text"]) for i in idxs]
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


def _bm25_search(store: VectorStore, mask: np.ndarray, query_tokens: List[str],
                 top_k: int = TOP_K_PER_LEVEL, k1: float = BM25_K1, b: float = BM25_B,
                 bm25_idx: Optional[BM25Index] = None) -> List[Tuple[int, float]]:
    if bm25_idx is not None:
        return bm25_idx.search(mask, query_tokens, top_k=top_k, k1=k1, b=b)
    return _bm25_search_slow(store, mask, query_tokens, top_k=top_k, k1=k1, b=b)


def _gate_sparse(hits: List[Tuple[int, float]], all_cos: Optional[np.ndarray],
                 min_score: float) -> List[Tuple[int, float]]:
    """BM25 单路命中同受 --min-score 约束（hybrid 专用）。

    --min-score 的语义是"最低余弦相似度"，与哪条泳道找到它无关。不加这道门槛时，
    抬高门槛只会缩小 dense 泳道、把 --limit 名额让给无门槛的 BM25 命中——**门槛越高
    结果越脏，反向单调**（实测 0.62 下 top-5 的 cosine 席位从 100% 掉到 54%；
    离题探针 `--min-score 0.95` 在修复前仍能返回 108 篇，而 thresholds.py 的标定档案
    白纸黑字写着硬离题探针 max sim 0.575；`--cite --min-score 0.7` 还会把全库没有一条
    ≥0.7 的 query 吐成可粘贴的引用串，假引用直接进稿子）。

    all_cos 为 None（--mode sparse 不算 query 向量）时原样放行：纯关键词模式语义不变。
    """
    if all_cos is None:
        return hits
    return [(idx, s) for idx, s in hits if float(all_cos[idx]) >= min_score]


def _level_hits(store: VectorStore, mask: np.ndarray, mode: str,
                 query_vec: Optional[np.ndarray], query_tokens: List[str],
                 min_score: float, top_k: int = TOP_K_PER_LEVEL, rrf_k: int = RRF_K,
                 all_cos: Optional[np.ndarray] = None,
                 bm25_idx: Optional[BM25Index] = None
                 ) -> List[Tuple[int, float, str, float]]:
    """按 mode 检索一个 level（paper 或 highlight），返回 (idx, score, kind, sort_score)。

    - score/kind：展示值。dense=cosine，sparse=bm25，hybrid 下优先展示 dense 余弦
      （kind=cosine），dense 没命中、纯靠关键词命中的条目展示 RRF 分（kind=rrf）
    - sort_score：组内/组间排序用的分。dense/sparse 模式下等于 score；hybrid 模式
      下统一用 RRF 分排序（两路量纲不可比，融合排序不能拿余弦和 bm25 直接比大小，
      但展示给人看时余弦更好懂，所以两者分离）
    - min_score 过滤全部候选的余弦（dense 直接过滤；hybrid 下 BM25 单路命中也用
      all_cos 回查余弦后过滤，见 _gate_sparse）；--mode sparse 不算 query 向量，不生效
    """
    if mode == "dense":
        if not mask.any():
            return []
        hits = store.search(query_vec, mask=mask, top_k=top_k)
        return [(idx, s, "cosine", s) for idx, s in hits if s >= min_score]

    if mode == "sparse":
        hits = _bm25_search(store, mask, query_tokens, top_k=top_k, bm25_idx=bm25_idx)
        return [(idx, s, "bm25", s) for idx, s in hits]

    # hybrid
    dense_hits = []
    if mask.any():
        dense_hits = [(idx, s) for idx, s in store.search(query_vec, mask=mask, top_k=top_k)
                      if s >= min_score]
    sparse_hits = _gate_sparse(_bm25_search(store, mask, query_tokens, top_k=top_k, bm25_idx=bm25_idx),
                               all_cos, min_score)
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


def _paper_side_hits(store: VectorStore, base_mask: np.ndarray, level_arr: np.ndarray,
                     mode: str, query_vec, query_tokens: List[str], min_score: float,
                     top_k: int = TOP_K_PER_LEVEL, rrf_k: int = RRF_K,
                     lanes: str = "both",
                     all_cos: Optional[np.ndarray] = None,
                     bm25_idx: Optional[BM25Index] = None
                     ) -> List[Tuple[int, float, str, float]]:
    """paper 侧检索：瘦 chunk（p:）与厚 chunk（ab:）dense 各一路 + BM25（合并掩码）一路。
    hybrid 下三路按 rank RRF 融合。返回形状与 _level_hits 一致：(idx, score, kind, sort_score)。

    为什么不能把两级 dense 塞同一个掩码里跑一次 search：瘦 chunk 是短中文判词文本、
    厚 chunk 是长中英混合文本，同一 query 下两者的余弦分布整体错位，同掩码 top_k 会让
    某一路系统性淹没另一路（见调用处注释的实测）——所以两路各自独立截断。注意独立截断
    只在 hybrid 的按名次 RRF 融合下真正消除淹没；dense 模式最终仍按原始余弦混排，跨泳道
    偏置依旧（见 dense 分支注释）。但 dense 模式的 sort_score 仍必须是余弦，不能换成 RRF：
    主循环把 paper/highlight 两侧的 sort_score 直接混排，highlight 侧 dense 用的是余弦
    （_level_hits），量纲必须一致。
    min_score 只过滤 dense 候选（与 _level_hits 口径一致）。
    """
    thin_mask = base_mask & (level_arr == "paper")
    # lanes="thin"：禁用厚路（ab: chunk）。p: chunk 文本自摘要喂厚前从未变，
    # 该模式就是喂厚前检索行为的精确复现——rag_bench A/B 对照用，日常检索别用。
    thick_mask = (base_mask & (level_arr == "abstract")) if lanes == "both" \
        else np.zeros(len(base_mask), dtype=bool)
    merged_mask = thin_mask | thick_mask

    if mode == "sparse":
        hits = _bm25_search(store, merged_mask, query_tokens, top_k=top_k, bm25_idx=bm25_idx)
        return [(idx, s, "bm25", s) for idx, s in hits]

    dense_lists = []
    for m in (thin_mask, thick_mask):
        if m.any():
            dense_lists.append([(idx, s) for idx, s in store.search(query_vec, mask=m, top_k=top_k)
                                if s >= min_score])
        else:
            dense_lists.append([])

    if mode == "dense":
        # sort_score 必须用余弦而非 RRF：highlight 侧（_level_hits）dense 的 sort_score
        # 是余弦，主循环把两侧混排比大小，paper 侧若给 RRF 分（上限 1/(rrf_k+1)≈0.016）
        # 会被任何过了 min_score 的 highlight 命中（≥0.4）恒定碾压。
        # 诚实声明：两泳道余弦分布错位在 dense 下并未解决——各泳道独立 top_k(200) 截断
        # 只保证两路都进候选池，下面仍按原始余弦混排；limit（默认 10）远小于 top_k，
        # 分布整体偏高的一路照样淹没另一路的头部。跨泳道偏置是 dense=纯余弦语义的
        # 固有代价；要泳道公平请用 hybrid（三路按名次 RRF 融合）。
        out = [(idx, s, "cosine", s) for lst in dense_lists for idx, s in lst]
        out.sort(key=lambda t: -t[3])
        return out

    # hybrid：瘦 dense + 厚 dense + BM25 三路 RRF
    sparse_hits = _gate_sparse(_bm25_search(store, merged_mask, query_tokens, top_k=top_k, bm25_idx=bm25_idx),
                               all_cos, min_score)
    lists = dense_lists + [sparse_hits]
    rank_maps = [{idx: r for r, (idx, _s) in enumerate(lst, start=1)} for lst in lists]
    dense_score = {}
    for lst in dense_lists:
        dense_score.update(dict(lst))
    out = []
    for idx in set().union(*[set(m) for m in rank_maps]):
        rrf = sum(1.0 / (rrf_k + m[idx]) for m in rank_maps if idx in m)
        if idx in dense_score:
            out.append((idx, dense_score[idx], "cosine", rrf))
        else:
            out.append((idx, rrf, "rrf", rrf))
    out.sort(key=lambda t: -t[3])
    return out


def _rerank_doc(row: dict, args, abstract_texts: Dict[str, str],
                paper_meta: Dict[str, dict]) -> str:
    """粒度感知重排文档构造：
    - 默认（--level auto 且无 --role/--book）的论文级/混合检索保持喂 ab:/p:，
      严格保护 87-case 存量标定；
    - 显式 --level highlight / --role / --book，或无摘要的书籍命中时，改喂
      title + top-2 命中句，让交叉编码器直接对句级/书章证据打分。
    """
    ck = row["citekey"]
    meta_rec = paper_meta[ck]
    evidence_mode = bool(
        getattr(args, "level", "auto") == "highlight"
        or getattr(args, "role", None)
        or getattr(args, "book", None)
    )
    if (evidence_mode or (ck not in abstract_texts and meta_rec.get("series") == "book")) and row["hits"]:
        title, _ = _split_paper_text(meta_rec["text"])
        sents = [r["text"] for _s, _k, _ss, r in row["hits"][:2] if r.get("text")]
        if sents:
            return (title + "\n" + "\n".join(sents)) if title else "\n".join(sents)
    return abstract_texts.get(ck) or meta_rec["text"]


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
    """best-effort：仅读索引文件头 8KB，避免整份解析 27MB JSON。"""
    try:
        index_path = notes_dir / INDEX_NAME
        if not index_path.exists():
            return None
        return store.freshness_warning(read_index_generated_at(index_path))
    except Exception:
        return None


def _load_batch_queries(path: Path) -> List[str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if raw.startswith("["):
        arr = json.loads(raw)
        if not isinstance(arr, list):
            raise ValueError("--batch-queries JSON 文件必须是字符串数组")
        return [str(x).strip() for x in arr if str(x).strip()]
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _execute_single_query(
    query: str,
    store: VectorStore,
    args,
    db_path: Path,
    query_vec: Optional[np.ndarray],
    base_mask: np.ndarray,
    level_arr: np.ndarray,
    bm25_idx: Optional[BM25Index],
    highlight_citekeys: set,
    paper_meta: Dict[str, dict],
    abstract_texts: Dict[str, str],
    syn_store: Optional[SynthesisStore],
):
    query_tokens = bm25_tokenize(query) if args.mode in ("sparse", "hybrid") else []
    no_query_tokens = args.mode in ("sparse", "hybrid") and not query_tokens

    all_cos = (store.mat @ query_vec.astype(np.float32)) if (
        args.mode == "hybrid" and query_vec is not None) else None
    search_paper = args.level in ("auto", "paper")
    search_highlight = args.level in ("auto", "highlight")

    paper_hits: List[Tuple[int, float, str, float]] = []
    if search_paper:
        paper_hits = _paper_side_hits(store, base_mask, level_arr, args.mode,
                                       query_vec, query_tokens, args.min_score,
                                       lanes=args.paper_lanes, all_cos=all_cos,
                                       bm25_idx=bm25_idx)
    highlight_hits: List[Tuple[int, float, str, float]] = []
    if search_highlight:
        highlight_mask = base_mask & (level_arr == "highlight")
        highlight_hits = _level_hits(store, highlight_mask, args.mode, query_vec, query_tokens,
                                      args.min_score, all_cos=all_cos, bm25_idx=bm25_idx)

    groups = defaultdict(lambda: {"paper": None, "paper_src": None,
                                  "paper_cos": None, "hits": []})
    for idx, score, kind, sort_score in paper_hits:
        r = store.records[idx]
        g = groups[r["citekey"]]
        if g["paper"] is None or sort_score > g["paper"][2]:
            g["paper"] = (score, kind, sort_score)
            g["paper_src"] = r["level"]
        if kind == "cosine" and (g["paper_cos"] is None or score > g["paper_cos"][0]):
            g["paper_cos"] = (score, r["level"])
    for idx, score, kind, sort_score in highlight_hits:
        r = store.records[idx]
        groups[r["citekey"]]["hits"].append((score, kind, sort_score, r))

    rows = []
    for ck, g in groups.items():
        meta = paper_meta.get(ck)
        if meta is None:
            continue
        candidates = ([g["paper"]] if g["paper"] else []) + [
            (s, k, ss) for s, k, ss, _r in g["hits"]]
        best_sort = max(t[2] for t in candidates)
        cosine_cands = ([(g["paper_cos"][0], "paper")] if g["paper_cos"] else []) + \
                       [(s, "highlight") for s, k, _ss, _r in g["hits"] if k == "cosine"]
        if cosine_cands:
            best_score, score_from = max(cosine_cands, key=lambda t: t[0])
            best_kind = "cosine"
        else:
            _b = max(candidates, key=lambda t: t[2])
            best_score, best_kind = _b[0], _b[1]
            score_from = "paper" if (g["paper"] and _b is g["paper"]) else "highlight"
        title, one_line = _split_paper_text(meta["text"])
        match_level = "highlight" if g["hits"] else "paper"
        paper_src = (g["paper_cos"][1] if g["paper_cos"] else g.get("paper_src")) or "paper"
        no_evidence = not g["hits"] and ck not in highlight_citekeys
        g["hits"].sort(key=lambda t: -t[2])
        rows.append({
            "citekey": ck, "score": best_score, "score_kind": best_kind, "_sort": best_sort,
            "score_from": score_from,
            "match_level": match_level, "paper_src": paper_src,
            "year": meta.get("year"), "title": title, "one_line": one_line,
            "tier": meta.get("tier"), "note_file": meta.get("note_file"),
            "note_line": meta.get("note_line"), "hits": g["hits"],
            "no_sentence_evidence": no_evidence,
        })

    rows.sort(key=lambda row: (-row["_sort"], TIER_ORDER.get(row["tier"], 3), row["citekey"]))
    shown = rows if args.limit == 0 else rows[:args.limit]
    truncated = len(rows) - len(shown)

    rerank_on = args.rerank if args.rerank is not None else (args.mode == "hybrid")
    reranked = False
    if rerank_on and len(shown) >= 2:
        head = shown[:RERANK_CAP]
        docs = [_rerank_doc(row, args, abstract_texts, paper_meta) for row in head]
        scores = None
        try:
            scores = reranker.rerank_scores(query, docs)
        except reranker.RerankUnavailable as e:
            print("⚠️ reranker 不可用（{}），按原排序输出；--no-rerank 可关掉本提示"
                  .format(e), file=sys.stderr)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print("⚠️ reranker 打分异常（见上方 traceback），按原排序输出", file=sys.stderr)
        if scores is not None and not all(math.isfinite(s) for s in scores):
            print("⚠️ reranker 产出非有限分数（NaN/inf，疑似数值故障），按原排序输出",
                  file=sys.stderr)
            scores = None
        if scores is not None:
            for row, s in zip(head, scores):
                row["rerank_score"] = s
            head.sort(key=lambda row: -row["rerank_score"])
            shown = head + shown[RERANK_CAP:]
            reranked = True
            if len(shown) > RERANK_CAP:
                print("⚠️ 展示超过 {} 条，仅前 {} 条参与重排（其余保持原序拼在后面）"
                      .format(RERANK_CAP, RERANK_CAP), file=sys.stderr)

    related_syntheses = (
        syn_store.search(query_vec, min_sim=TOPICS_MIN_SIM, top_k=2)
        if (syn_store is not None and query_vec is not None) else []
    )

    payload = {
        "total": len(rows), "shown": len(shown), "truncated": truncated,
        "query": query, "mode": args.mode,
        "reranked": reranked,
        "related_syntheses": related_syntheses,
        "results": [{
            "citekey": row["citekey"], "score": round(row["score"], 4),
            "score_kind": row["score_kind"],
            **({"rerank_score": round(row["rerank_score"], 4)}
               if "rerank_score" in row else {}),
            "score_from": row["score_from"],
            "match_level": row["match_level"],
            "match_source": ("highlight" if row["hits"]
                             else ("abstract" if row["paper_src"] == "abstract" else "title")),
            "no_sentence_evidence": row["no_sentence_evidence"],
            "year": row["year"],
            "title": row["title"], "one_line": row["one_line"],
            "hits": [dict({"text": r["text"], "role": r["role"], "section": r["section"],
                           "score": round(s, 4), "score_kind": k},
                          **{k2: r[k2] for k2 in ("chapter", "pages", "book_key")
                             if r.get(k2)})
                     for s, k, _ss, r in row["hits"]],
            "note_file": row["note_file"], "note_line": row["note_line"],
        } for row in shown],
    }
    return payload, shown, rows, truncated, reranked, no_query_tokens, related_syntheses


def main() -> int:
    ap = argparse.ArgumentParser(description="札记库语义检索（hybrid，phase 2）")
    ap.add_argument("query", nargs="*", help="查询词/短语（拼成一个语义 query，非逐词 AND）")
    ap.add_argument("--batch-queries", default=None, metavar="FILE",
                    help="批量查询文件（每行一条或 JSON 字符串数组），一次载入模型输出 JSONL")
    ap.add_argument("--role", choices=["citable", "refutable", "method"],
                    help="只看该角色的句子：citable=可引证据 refutable=可反驳 method=方法借鉴")
    ap.add_argument("--tier", choices=["high", "mid", "low"], help="优先级层过滤")
    ap.add_argument("--month", help="限定月份，YYYY / YYYY-MM / YYYY-MM-DD（前缀匹配）")
    ap.add_argument("--series", choices=["auto", "manual", "book"],
                    help="auto=流水线精读 manual=手动深读 book=书籍精读")
    ap.add_argument("--full-text-only", action="store_true", help="只要有全文精读的条目")
    ap.add_argument("--mode", choices=["dense", "hybrid", "sparse"], default="hybrid",
                    help="检索模式（默认 hybrid）：dense=纯向量余弦(paper 侧瘦/厚泳道按原始"
                         "余弦混排，跨泳道有偏置) sparse=纯关键词BM25(不需要Ollama) "
                         "hybrid=按名次RRF融合(泳道公平，推荐)")
    ap.add_argument("--level", choices=["auto", "paper", "highlight"], default="auto",
                    help="auto=两级都查（默认） paper=只查论文级（默认双路：标题+一句话 与 "
                         "回填摘要，见 --paper-lanes） highlight=只查精读句")
    ap.add_argument("--min-score", type=float, default=NOTES_SEARCH_MIN_SCORE,
                    help="最低余弦相似度（默认 {}，集中在 thresholds.py）。dense 与 hybrid "
                         "下**全部候选**都受它约束（hybrid 的 BM25 单路命中回查余弦后过滤）；"
                         "--mode sparse 不算 query 向量，不生效。注意它只管过滤，不管排序——"
                         "hybrid 按 RRF 名次再经 reranker 重排，要按分数看排名请用 "
                         "--mode dense（dense 默认不重排，见 --rerank）"
                         .format(NOTES_SEARCH_MIN_SCORE))
    ap.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=None,
                    help="对最终展示集用 bge-reranker-v2-m3 交叉编码器重排（试验依据 "
                         "docs/decisions/rerank_hyde_experiment_2026-08.md：hybrid 87 case "
                         "66→75@1）。默认 auto：hybrid 开、dense/sparse 关——dense 的"
                         "「按余弦分看排名」契约是 scholar-search 判重流程的依赖，重排会破坏"
                         "分数单调性。只重排、不改集合成员，--min-score 过滤在重排之前按余弦"
                         "执行；rerank 分与任何 min_sim 门槛**不可比**。模型不可用/打分异常时"
                         "自动降级回原排序（stderr 提示，退出码不变）。注意：--role/--book 等"
                         "过滤组合下的重排未经 bench 标定（doc 文本恒取篇级摘要/瘦文本，"
                         "不是命中的句级证据）")
    ap.add_argument("--limit", type=int, default=10, help="最多显示条数（默认 10，0=不限）")
    ap.add_argument("--cite", action="store_true", help="只输出可直接粘贴的 [@a; @b] 引用串")
    ap.add_argument("--book", default=None, metavar="CITEKEY",
                    help="只看某本书（专著的 citekey，或编著各章所属书的 book_key）")
    ap.add_argument("--json", action="store_true", dest="as_json", help="结构化输出（含 total）")
    ap.add_argument("--paper-lanes", choices=["both", "thin"], default="both",
                    help="paper 侧检索泳道：both=瘦(p:)+厚(ab:摘要)双路 RRF（默认）；"
                         "thin=只查瘦路（=摘要喂厚前的检索行为，rag_bench A/B 对照用）")
    ap.add_argument("--config", default="config/scholar.env")
    args = ap.parse_args()

    if args.limit < 0:
        ap.error("--limit 不能为负（0=不限量）")
    if args.month and not MONTH_RE.match(args.month):
        ap.error("--month 格式应为 YYYY / YYYY-MM / YYYY-MM-DD，收到：{}".format(args.month))
    if args.level == "paper" and args.role:
        ap.error("--level paper 与 --role 互斥：paper 级 chunk 无 role，该组合必然 0 命中"
                 "（--role 请配 --level auto 或 highlight）")

    if args.batch_queries:
        batch_path = Path(args.batch_queries)
        if not batch_path.exists():
            ap.error("--batch-queries 文件不存在：{}".format(batch_path))
        queries = _load_batch_queries(batch_path)
        if not queries:
            ap.error("--batch-queries 文件为空")
    else:
        single_q = " ".join(q.strip() for q in args.query if q and q.strip())
        if not single_q:
            ap.error("查询词为空（请传入 query 或 --batch-queries）")
        queries = [single_q]

    settings = load_scholar_settings(args.config, patch_gemini=False)
    notes_dir = settings.processing.notes_dir
    db_path = notes_dir / DB_NAME

    store, err = _load_store(db_path, settings.llm.embedding_model)
    if store is None:
        print(err, file=sys.stderr)
        return 2

    hint = _freshness_hint(store, notes_dir)
    if hint:
        print(hint, file=sys.stderr)

    query_vecs: List[Optional[np.ndarray]] = [None] * len(queries)
    if args.mode in ("dense", "hybrid"):
        client = EmbeddingClient(
            base_url=resolve_embedding_base_url(settings.llm),
            model=settings.llm.embedding_model,
        )
        try:
            vecs = client.embed(queries)
            query_vecs = [vecs[i] for i in range(len(queries))]
        except EmbeddingError as e:
            print("❌ query 嵌入失败：{}".format(e), file=sys.stderr)
            return 3
        finally:
            client.close()

    base_mask = _build_mask(store, args)
    level_arr = np.array([r["level"] for r in store.records])
    bm25_idx = _get_bm25_index(store, db_path) if args.mode in ("sparse", "hybrid") else None
    highlight_citekeys = {r["citekey"] for r in store.records if r["level"] == "highlight"}
    paper_meta = {r["citekey"]: r for r in store.records if r["level"] == "paper"}
    abstract_texts = {r["citekey"]: r["text"] for r in store.records if r["level"] == "abstract"}
    syn_store = SynthesisStore.load(notes_dir / SYNTHESIS_DB_NAME)

    if args.batch_queries:
        any_hit = False
        for q, qv in zip(queries, query_vecs):
            payload, shown, _r, _t, _rr, _nq, _rs = _execute_single_query(
                q, store, args, db_path, qv, base_mask, level_arr, bm25_idx,
                highlight_citekeys, paper_meta, abstract_texts, syn_store,
            )
            if shown:
                any_hit = True
            print(json.dumps(payload, ensure_ascii=False))
        return 0 if any_hit else 1

    query = queries[0]
    payload, shown, rows, truncated, reranked, no_query_tokens, related_syntheses = _execute_single_query(
        query, store, args, db_path, query_vecs[0], base_mask, level_arr, bm25_idx,
        highlight_citekeys, paper_meta, abstract_texts, syn_store,
    )

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if shown else 1

    if args.cite:
        if not shown:
            print("无命中", file=sys.stderr)
            return 1
        def _cite_one(row):
            pages = next((r.get("pages") for _s, _k, _ss, r in row["hits"] if r.get("pages")),
                         None)
            return "@{}{}".format(row["citekey"], ", p. {}".format(pages) if pages else "")

        print("[" + "; ".join(_cite_one(row) for row in shown) + "]")
        if truncated:
            print("⚠️ 共 {} 条命中，仅输出前 {} 条（--limit 调整，0=不限）".format(
                len(rows), len(shown)), file=sys.stderr)
        return 0

    if related_syntheses:
        syn_str = " · ".join("{} ({:.2f})".format(s["path"], s["score"]) for s in related_syntheses)
        print("📎 库内已有相关概念页/问答：{}\n".format(syn_str))

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
    print("语义命中 {} 篇（显示 {}）{}{}\n".format(
        len(rows), len(shown),
        "· role={}({})".format(args.role, ROLE_HINT[args.role]) if args.role else "",
        " · 已重排(bge-reranker，余弦分不再单调)" if reranked else ""))
    for row in shown:
        row_kw = "[关键词] " if row["score_kind"] != "cosine" else ""
        row_sfx = "句" if row.get("score_from") == "highlight" else ""
        print("[@{}] ({}) {} {}{:.2f}{} {}".format(
            row["citekey"], row["year"] or "?", TIER_EMOJI.get(row["tier"], ""),
            row_kw, row["score"], row_sfx, (row["title"] or "")[:90]))
        if row["one_line"]:
            print("    ⭐ {}".format(row["one_line"]))
        if row["no_sentence_evidence"]:
            print("    （语义命中标题/一句话用处，该篇无精读句级证据）")
        for s, k, _ss, r in row["hits"][:MAX_SHOWN_HITS]:
            hit_kw = "[关键词] " if k != "cosine" else ""
            where = r.get("chapter") or r["section"]
            page_sfx = " ⟨p.{}⟩".format(r["pages"]) if r.get("pages") else ""
            print("    [{}·{}] {}{:.2f} {}{}".format(
                r["role"], where, hit_kw, s, r["text"], page_sfx))
        if len(row["hits"]) > MAX_SHOWN_HITS:
            print("    …还有 {} 条命中句（--json 看全部）".format(len(row["hits"]) - MAX_SHOWN_HITS))
        print("    ↳ {}:{}".format(row["note_file"], row["note_line"]))
        print()
    if truncated:
        print("（另有 {} 条未显示，--limit 调整，0=不限）".format(truncated))
    return 0


def cli_entry() -> int:
    """CLI 兜底层：未预期异常一律转退出码 4，绝不让默认的 1 污染"无命中"契约。"""
    try:
        return main()
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 —— 兜底就是要接住一切未预期异常
        import traceback
        traceback.print_exc()
        print("notes_search 未预期内部错误（exit 4，见上方 traceback）", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(cli_entry())
