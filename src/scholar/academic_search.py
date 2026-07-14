# -*- coding: utf-8 -*-
"""
学术检索客户端：arXiv Atom API + PubMed E-utilities。

作为 Gmail Scholar Alert 之外的论文来源，把检索式抓取的结果统一映射为 PaperSegment，
注入同一条 筛选→翻译→摘要 流水线。全部使用公开 HTTP 接口，无需密钥；
不依赖 MCP 连接器（launchd headless 下 MCP 可能不可用）。

HTTP 抓取与 XML 解析分离，便于单元测试（解析函数是纯函数，可直接喂样例 XML）。
"""
import re
import time
import hashlib
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
from xml.etree import ElementTree as ET

import httpx

from .schema import PaperMetadata, PaperSegment, PaperField, DigestStatus
from ..utils.logger import get_logger

logger = get_logger(__name__)

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def _generate_paper_id(title: str, authors: Optional[List[str]] = None) -> str:
    """与 ScholarEmailParser.generate_paper_id 一致的 ID 生成（title + 前3作者的 md5）。"""
    content = (title or "").lower().strip()
    if authors:
        content += '|' + '|'.join(a.lower().strip() for a in authors[:3])
    return hashlib.md5(content.encode('utf-8')).hexdigest()


class AcademicSearchClient:
    """arXiv + PubMed 检索客户端。检索式由调用方（配置）给出。"""

    ARXIV_API = "https://export.arxiv.org/api/query"
    EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, timeout: float = 20.0, email: str = "", tool: str = "xlbd-scholar-digest"):
        self.email = email
        self.tool = tool
        # 强制 IPv4：本机代理/fake-ip（198.18.x.x）叠加 IPv6 黑洞会让 arXiv/PubMed 静默超时。
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": f"{tool} (https://github.com/xiaolibird/XLBDTranslator)"},
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
            follow_redirects=True,  # arXiv/NCBI http→https 301 需跟随，否则拿到空响应
        )

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------- arXiv ----------------

    def search_arxiv(self, query: str, max_results: int = 25, days: Optional[int] = None,
                     date_range: Optional[Tuple[date, date]] = None) -> List[Dict[str, Any]]:
        """按检索式抓 arXiv，返回归一化 dict 列表。

        date_range 指定 [start,end] 时把 submittedDate 过滤进检索式并翻页（历史月份在结果深处，
        不能只取首页）；否则沿用 days 客户端裁剪。异常向上抛，由调用方决定容错。
        """
        q = query
        if date_range:
            s, e = date_range
            q = "({}) AND submittedDate:[{}0000 TO {}2359]".format(
                query, s.strftime("%Y%m%d"), e.strftime("%Y%m%d"))
        items: List[Dict[str, Any]] = []
        page = min(100, max_results)
        start = 0
        while len(items) < max_results:
            params = {
                "search_query": q, "start": start, "max_results": page,
                "sortBy": "submittedDate", "sortOrder": "descending",
            }
            resp = self._client.get(self.ARXIV_API, params=params)
            resp.raise_for_status()
            batch = self.parse_arxiv(resp.text)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < page:
                break
            start += page
            time.sleep(0.5)  # arXiv 礼貌限速
        items = items[:max_results]
        if days is not None and not date_range:
            cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
            items = [it for it in items if not it.get("published") or it["published"] >= cutoff]
        return items

    @staticmethod
    def parse_arxiv(xml_text: str) -> List[Dict[str, Any]]:
        """解析 arXiv Atom XML 为归一化 dict 列表（纯函数）。"""
        root = ET.fromstring(xml_text)
        results: List[Dict[str, Any]] = []
        for entry in root.findall(f"{_ATOM_NS}entry"):
            title = (entry.findtext(f"{_ATOM_NS}title") or "").strip().replace("\n", " ")
            summary = (entry.findtext(f"{_ATOM_NS}summary") or "").strip().replace("\n", " ")
            authors = [
                (a.findtext(f"{_ATOM_NS}name") or "").strip()
                for a in entry.findall(f"{_ATOM_NS}author")
            ]
            authors = [a for a in authors if a]
            raw_id = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
            # raw_id 形如 http://arxiv.org/abs/2401.12345v1
            arxiv_id = raw_id.rsplit("/", 1)[-1] if raw_id else None
            doi = entry.findtext(f"{_ARXIV_NS}doi")
            published_raw = entry.findtext(f"{_ATOM_NS}published")
            published = None
            if published_raw:
                try:
                    published = datetime.strptime(published_raw[:10], "%Y-%m-%d").date()
                except ValueError:
                    published = None
            if not title:
                continue
            results.append({
                "source": "arxiv",
                "title": title,
                "abstract": summary,
                "authors": authors,
                "journal": "arXiv",
                "doi": doi.strip() if doi else None,
                "arxiv_id": arxiv_id,
                "pmid": None,
                "url": raw_id or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None),
                "published": published,
            })
        return results

    # ---------------- PubMed ----------------

    def search_pubmed(self, query: str, max_results: int = 25, days: Optional[int] = None,
                      date_range: Optional[Tuple[date, date]] = None) -> List[Dict[str, Any]]:
        """esearch 取 PMID → efetch 取详情，返回归一化 dict 列表。

        date_range 指定 [start,end] 时用 mindate/maxdate+datetype=pdat（历史区间）；否则沿用 reldate。
        """
        esearch_params = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "sort": "date",
            "tool": self.tool,
        }
        if self.email:
            esearch_params["email"] = self.email
        if date_range:
            s, e = date_range
            esearch_params["mindate"] = s.strftime("%Y/%m/%d")
            esearch_params["maxdate"] = e.strftime("%Y/%m/%d")
            esearch_params["datetype"] = "pdat"
        elif days is not None:
            esearch_params["reldate"] = days
            esearch_params["datetype"] = "pdat"

        resp = self._client.get(f"{self.EUTILS}/esearch.fcgi", params=esearch_params)
        resp.raise_for_status()
        try:
            ids = resp.json().get("esearchresult", {}).get("idlist", [])
        except Exception:
            ids = []
        if not ids:
            return []

        time.sleep(0.34)  # NCBI 礼貌限速（<3 req/s）
        efetch_params = {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "xml",
            "tool": self.tool,
        }
        if self.email:
            efetch_params["email"] = self.email
        resp2 = self._client.get(f"{self.EUTILS}/efetch.fcgi", params=efetch_params)
        resp2.raise_for_status()
        return self.parse_pubmed(resp2.text)

    @staticmethod
    def parse_pubmed(xml_text: str) -> List[Dict[str, Any]]:
        """解析 PubMed efetch XML（PubmedArticleSet）为归一化 dict 列表（纯函数）。"""
        root = ET.fromstring(xml_text)
        results: List[Dict[str, Any]] = []
        for art in root.findall(".//PubmedArticle"):
            medline = art.find("MedlineCitation")
            if medline is None:
                continue
            pmid = medline.findtext("PMID")
            article = medline.find("Article")
            if article is None:
                continue
            title = (article.findtext("ArticleTitle") or "").strip()
            # 摘要可能有多段（结构化）
            abstract_parts = []
            for ab in article.findall("./Abstract/AbstractText"):
                label = ab.get("Label")
                text = "".join(ab.itertext()).strip()
                if text:
                    abstract_parts.append(f"{label}: {text}" if label else text)
            abstract = " ".join(abstract_parts)

            authors = []
            for a in article.findall("./AuthorList/Author"):
                last = a.findtext("LastName")
                fore = a.findtext("ForeName")
                if last:
                    authors.append(f"{fore} {last}".strip() if fore else last)

            journal = article.findtext("./Journal/Title")

            # DOI：ELocationID[@EIdType=doi] 或 ArticleIdList
            doi = None
            for eloc in article.findall("./ELocationID"):
                if eloc.get("EIdType") == "doi":
                    doi = (eloc.text or "").strip()
                    break
            if not doi:
                for aid in art.findall("./PubmedData/ArticleIdList/ArticleId"):
                    if aid.get("IdType") == "doi":
                        doi = (aid.text or "").strip()
                        break

            # 发表日期
            published = None
            pubdate = article.find("./Journal/JournalIssue/PubDate")
            if pubdate is not None:
                y = pubdate.findtext("Year")
                m = pubdate.findtext("Month") or "1"
                d = pubdate.findtext("Day") or "1"
                if y:
                    published = _safe_date(y, m, d)

            if not title:
                continue
            results.append({
                "source": "pubmed",
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "journal": journal,
                "doi": doi,
                "arxiv_id": None,
                "pmid": pmid,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
                "published": published,
            })
        return results


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _safe_date(y: str, m: str, d: str) -> Optional[date]:
    try:
        month = int(m) if m.isdigit() else _MONTHS.get(m[:3].lower(), 1)
        return date(int(y), month, int(d) if d.isdigit() else 1)
    except Exception:
        return None


def item_to_segment(item: Dict[str, Any], segment_id: int) -> PaperSegment:
    """把归一化 dict 映射为 PaperSegment（source_type 标注来源，便于优先级识别预印本）。"""
    authors = item.get("authors") or []
    metadata = PaperMetadata(
        paper_id=_generate_paper_id(item.get("title", ""), authors),
        title=item.get("title", ""),
        authors=authors,
        publication_date=item.get("published"),
        journal=item.get("journal"),
        doi=item.get("doi"),
        arxiv_id=item.get("arxiv_id"),
        pmid=item.get("pmid"),
        url=item.get("url"),
        field=PaperField.OTHER,
        source_type=item.get("source", "unknown"),
    )
    return PaperSegment(
        segment_id=segment_id,
        paper_id=metadata.paper_id,
        original_abstract=item.get("abstract", ""),
        metadata=metadata,
        status=DigestStatus.PENDING,
    )


def fetch_arxiv_by_id(arxiv_id: str, client: httpx.Client, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    """按 arxiv_id 精确查 arXiv（id_list），返回归一化 dict；无结果/异常返回 None。

    arxiv_id 是精确标识，无需模糊匹配，作者即权威版。传入的 client 应已强制 IPv4。
    """
    aid = (arxiv_id or "").strip()
    if not aid:
        return None
    aid = re.sub(r"v\d+$", "", aid)  # 去掉版本后缀取最新
    resp = client.get(AcademicSearchClient.ARXIV_API, params={"id_list": aid})
    resp.raise_for_status()
    items = AcademicSearchClient.parse_arxiv(resp.text)
    return items[0] if items else None


def enrich_from_arxiv(meta, client: httpx.Client) -> bool:
    """用 arXiv 权威元数据就地覆盖 meta 的脏作者（Crossref 未命中的预印本兜底）。返回是否命中。"""
    if not getattr(meta, "arxiv_id", None):
        return False
    try:
        it = fetch_arxiv_by_id(meta.arxiv_id, client)
    except Exception as e:
        logger.warning("  ⚠️ arXiv 补全失败（{}）: {}".format(meta.arxiv_id, e))
        return False
    if not it or not it.get("authors"):
        return False
    meta.authors = it["authors"]
    if it.get("doi") and not meta.doi:
        meta.doi = it["doi"]
    return True


def fetch_external_papers(
    arxiv_query: str,
    pubmed_query: str,
    max_results: int,
    days: Optional[int],
    start_segment_id: int,
    email: str = "",
    timeout: float = 20.0,
    date_range: Optional[Tuple[date, date]] = None,
) -> List[PaperSegment]:
    """抓取 arXiv + PubMed，返回 PaperSegment 列表（segment_id 从 start_segment_id 连续分配）。

    date_range 指定 [start,end] 时按历史区间检索（回填用）；否则用相对 days。
    单源失败只 warning 跳过，不影响另一源与整体流程（cron 无人值守）。
    """
    segments: List[PaperSegment] = []
    next_id = start_segment_id

    with AcademicSearchClient(timeout=timeout, email=email) as client:
        if arxiv_query:
            try:
                items = client.search_arxiv(arxiv_query, max_results=max_results, days=days,
                                            date_range=date_range)
                logger.info("  arXiv 检索命中 {} 篇".format(len(items)))
                for it in items:
                    segments.append(item_to_segment(it, next_id))
                    next_id += 1
            except Exception as e:
                logger.warning("  ⚠️ arXiv 检索失败（跳过）: {}".format(e))

        if pubmed_query:
            try:
                items = client.search_pubmed(pubmed_query, max_results=max_results, days=days,
                                             date_range=date_range)
                logger.info("  PubMed 检索命中 {} 篇".format(len(items)))
                for it in items:
                    segments.append(item_to_segment(it, next_id))
                    next_id += 1
            except Exception as e:
                logger.warning("  ⚠️ PubMed 检索失败（跳过）: {}".format(e))

    return segments
