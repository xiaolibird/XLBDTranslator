# -*- coding: utf-8 -*-
"""
Zotero translation-server 客户端：把标识符（DOI/arXiv/PMID）交给 Zotero 官方翻译器解析为
权威元数据（作者/期刊/卷期/DOI），替代我们自己刮取——Zotero 作为矫正/权威层。

translation-server 是 Zotero 官方 Docker 服务（`zotero/translation-server`，默认端口 1969）：
  POST /search  body=text/plain 的标识符 → 返回 Zotero item JSON 数组（可直接喂连接器 saveItems）。

我们据此：① 用解析结果回填 PaperMetadata（令札记与 references.json 用权威数据）；
         ② 把权威 item 经连接器写入 Zotero；③ BBT 生成 citekey。
标识符由 Crossref（DOI）/ arXiv id / PMID 提供；无任何标识符的论文退回自建 item。
"""
import re
from datetime import date
from typing import List, Dict, Any, Optional

import httpx

from .schema import PaperMetadata
from .fulltext import ipv4_client
from ..utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "http://localhost:1969"


def best_identifier(meta: PaperMetadata) -> Optional[str]:
    """选取送 translation-server 解析的最佳标识符：DOI > arXiv > PMID。"""
    if meta.doi:
        return meta.doi.strip()
    if meta.arxiv_id:
        aid = meta.arxiv_id.strip()
        return aid if aid.lower().startswith("arxiv:") else "arXiv:{}".format(aid)
    if meta.pmid:
        return str(meta.pmid).strip()
    return None


def resolve_identifier(
    identifier: str,
    base_url: str = DEFAULT_BASE_URL,
    client: Optional[httpx.Client] = None,
    timeout: float = 30.0,
) -> Optional[List[Dict[str, Any]]]:
    """POST /search 解析标识符为 Zotero item JSON 列表；无结果/异常返回 None（不抛出）。"""
    ident = (identifier or "").strip()
    if not ident:
        return None
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    try:
        resp = c.post("{}/search".format(base_url.rstrip("/")),
                      content=ident.encode("utf-8"),
                      headers={"Content-Type": "text/plain"})
        if resp.status_code != 200:
            # 300=多义、400/500=解析失败，都视为未命中（上层退回自建 item）
            logger.warning("  ⚠️ translation-server 解析「{}」返回 {}".format(ident[:40], resp.status_code))
            return None
        data = resp.json()
        if isinstance(data, list) and data:
            return data
        return None
    except Exception as e:
        logger.warning("  ⚠️ translation-server 解析「{}」失败: {}".format(ident[:40], e))
        return None
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


def _creators_to_authors(creators: List[Dict[str, Any]]) -> List[str]:
    authors: List[str] = []
    for cr in creators or []:
        if cr.get("creatorType") and cr.get("creatorType") != "author":
            continue
        first = (cr.get("firstName") or "").strip()
        last = (cr.get("lastName") or "").strip()
        name = (cr.get("name") or "").strip()
        if first and last:
            authors.append("{} {}".format(first, last))
        elif last:
            authors.append(last)
        elif name:
            authors.append(name)
    return authors


def _parse_date(date_str: str) -> Optional[date]:
    """从 Zotero 的 date 字段解析日期，容忍多种格式：
    "2026-02-01" / "2026-02" / "2026" / "7/2026"（月/年）/ "Feb 2026" 等。
    策略：取 4 位年份，再从其余数字里挑一个 1-12 当月份；解析不出月/日就用 1。"""
    s = (date_str or "").strip()
    if not s:
        return None
    ym = re.search(r"\d{4}", s)
    if not ym:
        return None
    year = int(ym.group(0))
    month = 1
    for tok in re.findall(r"\d+", s):
        if tok == ym.group(0):
            continue
        v = int(tok)
        if 1 <= v <= 12:
            month = v
            break
    try:
        return date(year, month, 1)
    except Exception:
        return None


def apply_item_to_meta(meta: PaperMetadata, item: Dict[str, Any]) -> bool:
    """用 translation-server 解析出的权威 item 回填 PaperMetadata（作者/期刊/DOI/卷期/日期）。

    仅在 item 有值时覆盖，避免用空值抹掉已有信息。返回是否发生回填。
    """
    if not item:
        return False
    authors = _creators_to_authors(item.get("creators", []))
    if authors:
        meta.authors = authors
    if item.get("publicationTitle"):
        meta.journal = item["publicationTitle"]
    if item.get("DOI") and not meta.doi:
        meta.doi = item["DOI"]
    if item.get("volume"):
        meta.volume = item["volume"]
    if item.get("issue"):
        meta.issue = item["issue"]
    if item.get("pages"):
        meta.pages = item["pages"]
    d = _parse_date(item.get("date", ""))
    if d and not meta.publication_date:
        meta.publication_date = d
    return True


def resolve_and_apply(
    meta: PaperMetadata,
    base_url: str = DEFAULT_BASE_URL,
    client: Optional[httpx.Client] = None,
) -> Optional[Dict[str, Any]]:
    """解析 meta 的标识符并回填。返回权威 item（供 saveItems），未命中返回 None。"""
    ident = best_identifier(meta)
    if not ident:
        return None
    items = resolve_identifier(ident, base_url=base_url, client=client)
    if not items:
        return None
    item = items[0]
    apply_item_to_meta(meta, item)
    return item


def is_available(base_url: str = DEFAULT_BASE_URL, timeout: float = 5.0) -> bool:
    """translation-server 是否在线（探测 /search 端点存在）。"""
    try:
        with ipv4_client(timeout=timeout) as c:
            r = c.post("{}/search".format(base_url.rstrip("/")),
                       content=b"10.0000/probe", headers={"Content-Type": "text/plain"})
            # 端点存在即可（即便这个假 DOI 解析失败返回 400/500）
            return r.status_code in (200, 300, 400, 500, 501)
    except Exception:
        return False
