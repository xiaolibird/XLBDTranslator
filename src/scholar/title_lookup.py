# -*- coding: utf-8 -*-
"""按标题检索权威元数据的共用判据（OpenAlex）——盘点脚本与入库补全同源。

为什么抽出来：`scripts/inventory_missing_ids.py` 的阈值是在真实库上标定过的
（SIM_FILLABLE 0.92 / 年份差 ≤1 / 首作者），入库补全要按标题补作者与年份时
必须用**同一套**判据；各写一份，两边迟早漂移（同 notes_index 键梯漂移的教训）。

⚠️ OpenAlex 自 2026-09 起按日额度计费（免费约 100 次/日，UTC 零点重置），额度用完一律
429 `Insufficient budget`——这是「没答上」，不是「源里没有」，调用方必须分开处理。
"""
import re
import threading
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .crossref import _norm_title, title_similarity
from ..utils.logger import get_logger

logger = get_logger(__name__)

OPENALEX_API = "https://api.openalex.org/works"
SIM_FILLABLE = 0.92          # 标题相似度：三项齐才算可信命中
SIM_STRONG = 0.97            # 相似度极高时允许首作者缺失/对不上（元数据首作者本就常错）
SIM_CANDIDATE = 0.80         # 低于这个连 ambiguous 都不算，视为没命中
YEAR_TOLERANCE = 1           # 预印本 vs 正刊差一年是常态
MIN_TITLE_WORDS = 4          # 实词太少的标题对谁都像，不查（同 fulltext._ARXIV_TITLE_MIN_WORDS）

_ELLIPSIS_RE = re.compile(r"\s*(?:…|\.{3})\s*$")


# ---------------- 纯函数：可单测 ----------------

def title_words(t: str) -> List[str]:
    return re.findall(r"[a-z]{4,}", (t or "").lower())


def family_name(author: str) -> str:
    """'Xiaoli Bird' / 'Bird, Xiaoli' / 'X. Bird' → 'bird'。"""
    a = (author or "").strip()
    if not a:
        return ""
    if "," in a:
        return _norm_title(a.split(",")[0])
    return _norm_title(a.split()[-1])


def first_author_matches(entry_authors: List[str], hit_authors: List[str]) -> Optional[bool]:
    """None = 任一侧没有作者信息（不算对上也不算对不上）。"""
    if not entry_authors or not hit_authors:
        return None
    return family_name(entry_authors[0]) == family_name(hit_authors[0]) and bool(family_name(entry_authors[0]))


def year_ok(entry_year, hit_year) -> Optional[bool]:
    try:
        a, b = int(entry_year), int(hit_year)
    except (TypeError, ValueError):
        return None
    return abs(a - b) <= YEAR_TOLERANCE


def truncation_aware_similarity(entry_title: str, hit_title: str) -> float:
    """Scholar 推送会把长标题截成 "… COVID-19 screening …"。截断标题与完整标题的 Jaccard
    会被尾部缺词拉低，于是拿完整标题的**同长前缀**来比；未截断时就是普通 title_similarity。"""
    if not _ELLIPSIS_RE.search(entry_title or ""):
        return title_similarity(entry_title, hit_title)
    head = _ELLIPSIS_RE.sub("", entry_title)
    n = len(_norm_title(head).split())
    prefix = " ".join(_norm_title(hit_title).split()[:n])
    return max(title_similarity(head, prefix), title_similarity(entry_title, hit_title))


def confident_title_hit(entry_title: str, entry_authors: List[str], entry_year,
                        hit: Optional[Dict[str, Any]]) -> bool:
    """按标题检索回来的命中能不能直接拿来覆盖作者/年份。

    判据与盘点脚本 classify 的 fillable 档一致：相似度 ≥ SIM_FILLABLE、年份不冲突、
    首作者对上（或缺作者时相似度 ≥ SIM_STRONG，或近乎逐字 ≥0.99）。
    """
    if not hit:
        return False
    if len(title_words(entry_title)) < MIN_TITLE_WORDS:
        return False
    sim = truncation_aware_similarity(entry_title, hit.get("title") or "")
    if sim < SIM_FILLABLE or year_ok(entry_year, hit.get("year")) is False:
        return False
    fa = first_author_matches(entry_authors or [], hit.get("authors") or [])
    return fa is True or (fa is None and sim >= SIM_STRONG) or sim >= 0.99


# ---------------- 联网 ----------------

def query_openalex(title: str, email: str, client) -> Tuple[Optional[Dict[str, Any]], bool]:
    """返回 (命中|None, 源是否答上)。

    2026-09-05 实测：OpenAlex 已改成**按日额度计费**（每请求 $0.001，免费额度约 $0.1/日、
    UTC 零点重置），额度用完一律 429 `Insufficient budget`。这不是限流、放慢没用，
    因此必须记成「没答上」而不是「源里没有」，否则一整批尾巴会被永久判成 no_hit。
    """
    from .fulltext import polite_get
    # 记忆里踩过：无 DOI 的 PMLR 论文必须用 filter=title.search，别用 search=（那会全走 arXiv 侧）
    q = re.sub(r"[^\w\s\-]", " ", _ELLIPSIS_RE.sub("", title or ""))[:200]
    params = {"filter": "title.search:{}".format(q), "per-page": 3}
    if email:
        params["mailto"] = email
    try:
        r = polite_get(client, OPENALEX_API, params=params, timeout=30.0)
        if r.status_code != 200:
            return None, False
        results = (r.json() or {}).get("results") or []
    except Exception as e:                                        # noqa: BLE001
        logger.warning("  ⚠️ OpenAlex 检索失败（{}）: {}".format((title or "")[:40], e))
        return None, False
    best = None
    for w in results:
        t = w.get("title") or w.get("display_name") or ""
        sim = title_similarity(title, t)
        if best is None or sim > best["sim"]:
            doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
            authors = [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])]
            best = {"source": "openalex", "title": t, "doi": doi, "year": w.get("publication_year"),
                    "authors": [a for a in authors if a], "sim": sim}
    return best, True


def enrich_from_openalex_title(meta, email: str, client,
                               exhausted: Optional[threading.Event] = None) -> Optional[bool]:
    """Crossref 未命中、又没有 DOI/arXiv 的条目，按标题去 OpenAlex 补作者/年份/DOI。

    返回 True=命中并已覆盖；False=问过了没有可信命中；None=没问成（额度耗尽/网络）。
    `exhausted` 是整批共享的熔断标志：第一次没答上就置位，其余 worker 不再白烧请求。
    只在原字段缺失时填 DOI 与年份；作者一律以权威源为准（Scholar 作者本就是截断缩写）。
    """
    if meta.doi or getattr(meta, "arxiv_id", None):
        return False
    if len(title_words(meta.title or "")) < MIN_TITLE_WORDS:
        return False
    if exhausted is not None and exhausted.is_set():
        return None
    hit, answered = query_openalex(meta.title or "", email, client)
    if not answered:
        if exhausted is not None:
            exhausted.set()
        return None
    year = meta.publication_date.year if getattr(meta, "publication_date", None) else None
    if not confident_title_hit(meta.title or "", list(meta.authors or []), year, hit):
        return False
    if hit.get("authors"):
        meta.authors = list(hit["authors"])
    if hit.get("doi") and not meta.doi:
        meta.doi = hit["doi"]
    if hit.get("year") and not meta.publication_date:
        meta.publication_date = date(int(hit["year"]), 1, 1)
        meta.date_precision = "year"
    return True
