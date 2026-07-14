# -*- coding: utf-8 -*-
"""
Crossref 标题检索元数据增强。

Scholar 邮件解析出的作者常把标题片段混入作者列表，且普遍缺 DOI。写入 Zotero 前用
标题查 Crossref（`query.bibliographic`），拿回规范作者 + DOI + 期刊卷期，覆盖脏字段。

带标题相似度门槛（token Jaccard），避免匹配到无关论文污染文献库；不达标则保留原始元数据。
公开接口、无需密钥；mailto 礼貌参数走 polite pool。HTTP 与解析分离，便于 mock 测试。
"""
import re
from datetime import date
from typing import List, Dict, Any, Optional

import httpx

from .schema import PaperMetadata
from .fulltext import ipv4_client
from ..utils.logger import get_logger

logger = get_logger(__name__)

CROSSREF_API = "https://api.crossref.org/works"
# 门槛取高：正确匹配的标题相似度≈1.0，错配普遍≤0.3，鸿沟极大。
# 宁可漏增强（保留原始元数据）也绝不用错误论文覆盖（污染文献库比脏作者更糟）。
_TITLE_MATCH_THRESHOLD = 0.85


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def title_similarity(a: str, b: str) -> float:
    """两标题的 token Jaccard 相似度（0-1）。"""
    ta, tb = set(_norm_title(a).split()), set(_norm_title(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def parse_crossref_work(item: Dict[str, Any]) -> Dict[str, Any]:
    """把单条 Crossref work 映射为归一化 dict（纯函数）。"""
    title_list = item.get("title") or []
    title = title_list[0] if title_list else ""

    authors: List[str] = []
    for a in item.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        if family and given:
            authors.append(f"{given} {family}")
        elif family:
            authors.append(family)
        elif a.get("name"):
            authors.append(a["name"].strip())

    container = item.get("container-title") or []
    journal = container[0] if container else None

    # 日期：issued > published-print > published-online
    pub_date = None
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            dp = parts[0]
            y = dp[0] if len(dp) >= 1 else None
            m = dp[1] if len(dp) >= 2 else 1
            d = dp[2] if len(dp) >= 3 else 1
            if y:
                try:
                    pub_date = date(int(y), int(m or 1), int(d or 1))
                except Exception:
                    pub_date = None
            break

    doi = (item.get("DOI") or "").strip() or None
    return {
        "title": title,
        "authors": authors,
        "journal": journal,
        "doi": doi,
        "volume": (item.get("volume") or "").strip() or None,
        "issue": (item.get("issue") or "").strip() or None,
        "pages": (item.get("page") or "").strip() or None,
        "publication_date": pub_date,
        "url": item.get("URL") or (f"https://doi.org/{doi}" if doi else None),
    }


def best_match(items: List[Dict[str, Any]], query_title: str,
               threshold: float = _TITLE_MATCH_THRESHOLD) -> Optional[Dict[str, Any]]:
    """从 Crossref 候选里挑标题相似度达标的最佳项（纯函数）。不达标返回 None。"""
    best = None
    best_sim = 0.0
    for it in items:
        parsed = parse_crossref_work(it)
        sim = title_similarity(query_title, parsed["title"])
        if sim > best_sim:
            best_sim, best = sim, parsed
    if best and best_sim >= threshold:
        return best
    return None


def crossref_lookup(
    title: str,
    email: str = "",
    client: Optional[httpx.Client] = None,
    timeout: float = 15.0,
    rows: int = 3,
) -> Optional[Dict[str, Any]]:
    """按标题查 Crossref，返回达标的规范元数据 dict；无匹配/异常返回 None（不抛出）。"""
    if not title or not title.strip():
        return None
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    try:
        params = {"query.bibliographic": title, "rows": rows}
        if email:
            params["mailto"] = email
        resp = c.get(CROSSREF_API, params=params)
        resp.raise_for_status()
        items = (resp.json().get("message") or {}).get("items") or []
        return best_match(items, title)
    except Exception as e:
        logger.warning("  ⚠️ Crossref 检索失败（{}）: {}".format((title or "")[:40], e))
        return None
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


def enrich_metadata(
    meta: PaperMetadata,
    email: str = "",
    client: Optional[httpx.Client] = None,
) -> bool:
    """用 Crossref 规范元数据就地覆盖 meta 的脏/缺字段。返回是否命中增强。

    覆盖：authors（规范作者）、doi、journal、volume、issue、pages、publication_date。
    仅在 Crossref 有值时覆盖，避免用空值抹掉已有信息。
    """
    hit = crossref_lookup(meta.title, email=email, client=client)
    if not hit:
        return False
    if hit.get("authors"):
        meta.authors = hit["authors"]
    if hit.get("doi") and not meta.doi:
        meta.doi = hit["doi"]
    if hit.get("journal"):
        meta.journal = hit["journal"]
    if hit.get("volume"):
        meta.volume = hit["volume"]
    if hit.get("issue"):
        meta.issue = hit["issue"]
    if hit.get("pages"):
        meta.pages = hit["pages"]
    if hit.get("publication_date") and not meta.publication_date:
        meta.publication_date = hit["publication_date"]
    return True
