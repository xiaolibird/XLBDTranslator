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
           关键词命中的条目才展示 RRF 分并标 [关键词]。
           注意：--min-score 在 hybrid 下**同样约束 BM25 单路命中**（回查其余弦，见
           _gate_sparse），所以显式传门槛时 [关键词]/score_kind="rrf" 这条展示路径
           基本不会再出现——纯关键词命中的余弦按定义低于门槛。

覆盖面警告：句级证据只覆盖库内约三成精读文献（668/2254 ≈ 30%，截至 2026-08-21；
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
from src.scholar.thresholds import NOTES_SEARCH_MIN_SCORE                 # noqa: E402
from src.scholar.embeddings import (                                      # noqa: E402
    EmbeddingClient, EmbeddingError, resolve_embedding_base_url,
)
from src.scholar.embed_store import DB_NAME, INDEX_NAME, VectorStore, VectorStoreError, model_matches  # noqa: E402
from src.scholar.vault import tokenize                                    # noqa: E402

ROLE_HINT = {"citable": "可引用证据", "refutable": "可反驳观点", "method": "方法论借鉴"}
TIER_ORDER = {"high": 0, "mid": 1, "low": 2}
TIER_EMOJI = {"high": "🔴", "mid": "🟠", "low": "🟢"}
MONTH_RE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")  # YYYY[-MM[-DD]]，月份/日语义校验
MAX_SHOWN_HITS = 4          # 人读模式每条最多展示几句命中（--json 不截断）
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
TOP_K_PER_LEVEL = 200  # RRF 只融合列表内名次；截断越小越易漏掉"两路都中等但 RRF 真值高"的条目。argpartition O(n) 成本≈0


_TWO_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z]{2})(?![A-Za-z])")


def bm25_tokenize(text: str) -> List[str]:
    """BM25 专用分词 = vault.tokenize + 补回所有独立的两字母词（小写归一）。

    vault.tokenize 的英文正则是 `[a-z]{3,}`，恰好两字母的 EM/MI/IV/RR 被整个丢掉，
    而它们正是本库里 IDF 最高、BM25 本该最出力的术语（真库：含独立 EM/MI/IV 的
    chunk 各 47/113/538 条）。丢掉的后果不是少一个词——`EM algorithm` 退化成只查
    `algorithm`，BM25 top10 全成了算法选择类文献，与 dense 侧 top5 零交集，再经
    RRF 把这批泛词命中送进 hybrid 默认结果（实测 2026-08-23，见
    docs/decisions/oss_alignment_audit_2026-08.md ⚠️-2）。

    **不论大小写全收，让 IDF 去决定权重**——这条是 R1 复审改过来的，初版只补大写
    (`[A-Z]{2}`)，两个方向都被实测打脸（同日，见该报告 R1-C/R1-D）：
      - 小写查询整条失效：`em algorithm` 的 hybrid top5 原封不动还是修复前的病态
        结果（Algorithm Selection 那批泛词命中），修复只对按了 shift 的用户生效；
      - 更隐蔽的是 df 畸变。`of` 只在全大写标题里才被收，df 被压到 12 → IDF 7.50
        比 `em` 的 6.16 还高，摇身变成假阳性源：`MODELS OF CARE` 比 `models of
        care` 凭空多召回一篇靠 `of` 命中的无关文献。
    全收后 IDF 自动归位：`of` 7.50→2.02、`on` 9.62→2.77 被压平，而真缩写几乎不动
    （`em` 6.16→6.12、`iv` 3.74→3.73），doc_len 仅 +3.3%（60.7→62.7）。硬删虚词
    反而要维护一张黑名单，而真库里 OR/US/AS/AT/IS/IF/AN 恰恰都是真缩写
    （odds ratio、adversarial training、homomorphic encryption…），删了就是丢信号。

    仍只补**恰好两个**字母：单字母噪音太大；三字母及以上（EHR/MNAR）本就被
    `[a-z]{3,}` 收着，再补一份等于给它们双倍词频。小写后不与任何既有 token 撞车
    ——`[a-z]{3,}` 产不出两字母词，中文 2-gram 只产汉字对，_STOP 里也没有两字母词。

    刻意不改 vault.tokenize 本体：那里还供着近邻图与 qa 词面查重，动它等于同时改掉
    两处已标定的相似度分布。文档侧与查询侧必须共用本函数，否则 query 有 `em` 而
    文档没有，等于白补。
    """
    return tokenize(text) + [m.lower() for m in _TWO_LETTER.findall(text)]


def _split_paper_text(text: str):
    """paper 级 (p:) chunk 文本恒为两段：title\\none_line（one_line 空则仅 title）。
    回填摘要在独立的 ab: chunk 里（embed_store.chunks_from_index 一篇双向量方案），
    只参与检索，不经过本函数。这里还原展示用的 (title, one_line)。"""
    if not text:
        return "", ""
    parts = text.split("\n")
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _build_mask(store: VectorStore, args) -> np.ndarray:
    """元数据布尔掩码：role/tier/month(前缀)/series/full-text-only/book。
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
        if getattr(args, "book", None):
            # 专著：citekey 本身即书键；编著：各章的 book_key 指向所属书。两者都收，
            # 否则「--book guyatt2015users」查不到那本书的任何一章（章的 citekey 是章键）。
            if args.book not in (r.get("book_key"), r.get("citekey")):
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
                 all_cos: Optional[np.ndarray] = None
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
        hits = _bm25_search(store, mask, query_tokens, top_k=top_k)
        return [(idx, s, "bm25", s) for idx, s in hits]

    # hybrid
    dense_hits = []
    if mask.any():
        dense_hits = [(idx, s) for idx, s in store.search(query_vec, mask=mask, top_k=top_k)
                      if s >= min_score]
    sparse_hits = _gate_sparse(_bm25_search(store, mask, query_tokens, top_k=top_k),
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
                     all_cos: Optional[np.ndarray] = None
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
        hits = _bm25_search(store, merged_mask, query_tokens, top_k=top_k)
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
    sparse_hits = _gate_sparse(_bm25_search(store, merged_mask, query_tokens, top_k=top_k),
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
                         "hybrid 仍按 RRF 名次排序，要按分数看排名请用 --mode dense"
                         .format(NOTES_SEARCH_MIN_SCORE))
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

    query = " ".join(q.strip() for q in args.query if q and q.strip())
    if not query:
        ap.error("查询词为空")

    settings = load_scholar_settings(args.config, patch_gemini=False)  # 本脚本不碰 LLM provider
    notes_dir = settings.processing.notes_dir  # loader 已锚定仓库根
    db_path = notes_dir / DB_NAME

    store, err = _load_store(db_path, settings.llm.embedding_model)
    if store is None:
        print(err, file=sys.stderr)
        return 2

    hint = _freshness_hint(store, notes_dir)
    if hint:
        print(hint, file=sys.stderr)

    query_tokens = bm25_tokenize(query) if args.mode in ("sparse", "hybrid") else []
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
    # hybrid 下 BM25 单路命中要能回查自己的余弦才能受 --min-score 约束（见 _gate_sparse）。
    # 只在 hybrid 算：dense 分支本来就拿得到余弦，sparse 分支压根没有 query 向量。
    # 成本可忽略——store.search 内部无视 mask 做的就是同一个整矩阵乘法，--level auto 下
    # 已经做了 3 次，这里是第 4 次（真库 22592×1024 实测 ~1ms，同 query 的 BM25 是 40-185ms）。
    all_cos = (store.mat @ query_vec.astype(np.float32)) if (
        args.mode == "hybrid" and query_vec is not None) else None
    level_arr = np.array([r["level"] for r in store.records])
    search_paper = args.level in ("auto", "paper")
    search_highlight = args.level in ("auto", "highlight")

    paper_hits: List[Tuple[int, float, str, float]] = []
    if search_paper:
        # paper 侧 = 瘦 chunk（p:，title+判词）与厚 chunk（ab:，title+判词+摘要）两路。
        # 两路余弦分布不可同台比较（短中文文本 vs 长混合文本；2026-08-21 实测中文 query
        # 下瘦 chunk 系统性占前排、把厚 chunk 的真命中挤出 top-5），所以各自独立排名后
        # 按 rank 做 RRF 融合，BM25 在合并掩码上单独一路。展示层仍按 citekey 取
        # paper 级的 title/one_line，摘要不刷屏。
        paper_hits = _paper_side_hits(store, base_mask, level_arr, args.mode,
                                       query_vec, query_tokens, args.min_score,
                                       lanes=args.paper_lanes, all_cos=all_cos)
    highlight_hits: List[Tuple[int, float, str, float]] = []
    if search_highlight:
        highlight_mask = base_mask & (level_arr == "highlight")
        highlight_hits = _level_hits(store, highlight_mask, args.mode, query_vec, query_tokens,
                                      args.min_score, all_cos=all_cos)

    # 全库真相：这个 citekey 是否本就有句级证据（与本次查询命中与否无关）
    highlight_citekeys = {r["citekey"] for r in store.records if r["level"] == "highlight"}
    paper_meta = {r["citekey"]: r for r in store.records if r["level"] == "paper"}

    # 每条 hit 是 (idx, score, kind, sort_score)：score/kind 是展示值（dense 命中给
    # 余弦、纯关键词命中给 RRF/BM25 分并标 kind!=cosine）；sort_score 是排序值（hybrid
    # 下统一用 RRF 分，跟展示值分离——RRF 才是两路真正可比的量纲）
    # paper     ：排序代表（sort_score 最大者，hybrid 下即 RRF）
    # paper_cos ：展示代表（paper 侧全部 chunk 的最高余弦 + 它来自哪条泳道）
    # 两者必须分开存：hybrid 下 RRF 最高的那条未必余弦最高，只留一条会把更高的兄弟
    # chunk 当场丢掉，下游 cosine_cands 的 max 再也捞不回来——实测 1898/28076 个
    # (query,citekey) 对被低报、最大低报 0.080，其中 72 例展示 <0.62 而真实 ≥0.62，
    # 正好落在 skill 判重线两侧；并列时 paper_src 还会由 set 迭代序而非分数决定
    # （45 个实例分布在 33/75 条 bench query 上）。
    groups = defaultdict(lambda: {"paper": None, "paper_src": None,
                                  "paper_cos": None, "hits": []})
    for idx, score, kind, sort_score in paper_hits:
        r = store.records[idx]
        g = groups[r["citekey"]]
        if g["paper"] is None or sort_score > g["paper"][2]:
            g["paper"] = (score, kind, sort_score)
            g["paper_src"] = r["level"]          # "paper"（标题+判词）或 "abstract"（回填摘要）
        if kind == "cosine" and (g["paper_cos"] is None or score > g["paper_cos"][0]):
            g["paper_cos"] = (score, r["level"])
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
        # 展示候选 = paper 侧**最高余弦**（paper_cos，而非排序代表 g["paper"]）+ 句级余弦命中。
        # 同时记账它来自哪一侧：score_from 不能靠"与 paper_cos 比大小"反推——纯关键词命中的
        # paper 行 paper_cos 恒为 None，反推会一律落到 "highlight"，给一条 hits 为空的行打上
        # 「句」标记，与同一条目下紧跟着的"该篇无精读句级证据"直接打架。
        cosine_cands = ([(g["paper_cos"][0], "paper")] if g["paper_cos"] else []) + \
                       [(s, "highlight") for s, k, _ss, _r in g["hits"] if k == "cosine"]
        if cosine_cands:
            best_score, score_from = max(cosine_cands, key=lambda t: t[0])
            best_kind = "cosine"
        else:
            _b = max(candidates, key=lambda t: t[2])
            best_score, best_kind = _b[0], _b[1]
            score_from = "paper" if (g["paper"] and _b is g["paper"]) else "highlight"
        # 这个展示分**未必是篇级分**：--level auto 下候选里混着句级命中，某篇标题/摘要
        # 根本没过 min_score、只有一条精读句命中 0.71 时，展示分就是那 0.71。而
        # scholar-search skill 教的是"≥0.62 判库内疑似同篇"——那条判据只对篇级分成立，
        # 不标出来就会被当成篇级分读，把"库里有一句相关证据"误报成"库里已有这篇"。
        title, one_line = _split_paper_text(meta["text"])
        match_level = "highlight" if g["hits"] else "paper"
        # paper_src 跟**展示分实际取自哪条 chunk**，不再跟 RRF 排序代表（并列时由 set
        # 迭代序决定，等于掷硬币）。没有余弦命中时退回排序代表的泳道。
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

    if args.as_json:
        print(json.dumps({
            "total": len(rows), "shown": len(shown), "truncated": truncated,
            "query": query, "mode": args.mode,
            "results": [{
                "citekey": row["citekey"], "score": round(row["score"], 4),
                "score_kind": row["score_kind"],
                # 展示分取自篇级还是句级。skill 的「≥0.62 判库内疑似同篇」判据**只对
                # score_from=="paper" 成立**；match_source 回答的是另一个问题（这篇有没有
                # 句级命中），两者不可互相替代：paper 0.71+句级 0.55 与 paper 0.55+句级 0.71
                # 的 score 都是 0.71、match_source 都是 "highlight"，JSON 消费方无从区分。
                # 人读模式有「句」标记兜着，而 skill 推荐的正是 --json。
                "score_from": row["score_from"],
                "match_level": row["match_level"],
                "match_source": ("highlight" if row["hits"]
                                 else ("abstract" if row["paper_src"] == "abstract" else "title")),
                "no_sentence_evidence": row["no_sentence_evidence"],
                "year": row["year"],
                "title": row["title"], "one_line": row["one_line"],
                "hits": [dict({"text": r["text"], "role": r["role"], "section": r["section"],
                               "score": round(s, 4), "score_kind": k},
                              # 书籍链路：章节定位与原书页码。文章命中不带这两个键，
                              # 消费方（scholar-write）据 pages 产出 [@key, p. N] 定位符。
                              **{k2: r[k2] for k2 in ("chapter", "pages", "book_key")
                                 if r.get(k2)})
                         for s, k, _ss, r in row["hits"]],
                "note_file": row["note_file"], "note_line": row["note_line"],
            } for row in shown],
        }, ensure_ascii=False, indent=2))
        return 0 if shown else 1

    if args.cite:
        if not shown:
            print("无命中", file=sys.stderr)
            return 1
        # 书籍命中带页码定位符：[@little2020rubin, p. 247]。pandoc 的 citeproc 认这个
        # 语法，而一本 460 页的书不带定位符的引用等于没标出处。
        def _cite_one(row):
            pages = next((r.get("pages") for _s, _k, _ss, r in row["hits"] if r.get("pages")),
                         None)
            return "@{}{}".format(row["citekey"], ", p. {}".format(pages) if pages else "")

        print("[" + "; ".join(_cite_one(row) for row in shown) + "]")
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
        # 篇级分数必须打出来：句级命中行一直带分，篇级此前只有标题——判"库里是否已有
        # 类似文献"恰恰看的是篇级分（0.62 是 paper 级强近邻的标定线，见 thresholds.py），
        # 不显示就只能靠 --json 反查，实测让调用方把弱相关误报成"库内已有"。
        # 分数来自句级时打 `句` 标记：0.62 那条判据只对篇级分成立，不标出来会把
        # "库里有一句相关证据"读成"库里已有这篇"（--level auto 下才会出现）。
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
            # 书籍命中优先显示「章 · 页」而不是精读分节名：分节名（方法与数据…）
            # 对书没有定位价值，读者要的是「第几章第几页」。
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
