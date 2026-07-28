# -*- coding: utf-8 -*-
"""
开放获取（OA）全文 PDF 解析。

只走合法免费来源，按序尝试：
  1. arXiv：由 arxiv_id 直接构造 PDF URL（预印本天然 OA）。
  2. Unpaywall：任意 DOI → 最佳合法 OA 位置（覆盖 gold/green/hybrid/bronze、PMC、机构库）。
无命中则标记 closed，交由上层退化为 abstract-only（不碰付费墙，不引入非法源）。

另有一条**不依赖 PDF** 的全文来路 `europepmc_fulltext()`：Elsevier/Cell、MDPI、OUP 等
出版商对机器人下载 PDF 一律回 403（2026-07-27 实测：Patterns/iScience/Lancet Digit Health/
J Clin Med/Eur Heart J 全挂），但同一批论文的 OA 全文在 Europe PMC 有干净的 JATS XML，
既不用解析 PDF 版面也不被反爬拦。因此 OA PDF 取不到时应回退到这条链，而非直接降级成摘要。

只返回 URL 与 OA 状态，实际下载交给 Zotero（连接器 attachments 让 Zotero 自己抓），
或上层显式下载。HTTP 与解析分离，便于 mock 测试。
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

import httpx

from .schema import PaperMetadata
from ..utils.logger import get_logger

logger = get_logger(__name__)

UNPAYWALL_API = "https://api.unpaywall.org/v2"
EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def ipv4_client(timeout: float = 15.0, headers: Optional[Dict[str, str]] = None) -> httpx.Client:
    """构造强制 IPv4 的 httpx.Client。

    本机若有代理/fake-ip（Clash/Surge 等把域名解析到 198.18.x.x）叠加 IPv6 黑洞，
    默认连接会对 crossref/arxiv/unpaywall 超时（而 `curl -4` 正常）。绑定 IPv4 源地址规避。
    """
    return httpx.Client(
        timeout=timeout,
        headers=headers or {"User-Agent": "xlbd-scholar-digest"},
        transport=httpx.HTTPTransport(local_address="0.0.0.0"),
        follow_redirects=True,  # arXiv/NCBI 等 http→https 会 301，默认不跟随会拿到空响应
    )


@dataclass
class OAResult:
    """OA 解析结果。pdf_url 为 None 表示未找到合法免费 PDF。"""
    oa_status: str = "unknown"          # arxiv / gold / green / hybrid / bronze / closed / unknown
    pdf_url: Optional[str] = None        # 直接可下载的 PDF 链接
    landing_url: Optional[str] = None    # OA 落地页（非 PDF 直链时）
    source: Optional[str] = None         # arxiv / unpaywall
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_oa(self) -> bool:
        return bool(self.pdf_url)


def arxiv_pdf_url(arxiv_id: str) -> str:
    """由 arxiv_id 构造 PDF 直链（保留版本号，arXiv 接受带版本的 pdf 路径）。"""
    aid = (arxiv_id or "").strip()
    # 容错：有时带 abs/ 前缀或完整 URL
    if "/" in aid:
        aid = aid.rsplit("/", 1)[-1]
    return f"https://arxiv.org/pdf/{aid}.pdf"


def parse_unpaywall(data: Dict[str, Any]) -> OAResult:
    """解析 Unpaywall 响应为 OAResult（纯函数）。"""
    if not data or not data.get("is_oa"):
        return OAResult(oa_status=(data or {}).get("oa_status") or "closed", source="unpaywall")
    best = data.get("best_oa_location") or {}
    pdf = best.get("url_for_pdf")
    landing = best.get("url") or best.get("url_for_landing_page")
    return OAResult(
        oa_status=data.get("oa_status") or ("gold" if pdf else "unknown"),
        pdf_url=pdf,
        landing_url=landing,
        source="unpaywall",
        extra={"host_type": best.get("host_type"), "version": best.get("version")},
    )


def resolve_oa_pdf(
    meta: PaperMetadata,
    email: str = "",
    client: Optional[httpx.Client] = None,
    timeout: float = 15.0,
) -> OAResult:
    """按 arXiv → Unpaywall 顺序解析 OA PDF。任何异常都降级为 closed，绝不抛出中断上层。

    email 是 Unpaywall 的强制礼貌参数（联系句柄，非密钥）；缺失则跳过 Unpaywall。
    """
    # 1) arXiv 直链
    if meta.arxiv_id:
        return OAResult(oa_status="arxiv", pdf_url=arxiv_pdf_url(meta.arxiv_id), source="arxiv")

    # 2) Unpaywall（需 DOI + email）
    if meta.doi and email:
        own_client = client is None
        c = client or ipv4_client(timeout=timeout)
        try:
            resp = c.get(f"{UNPAYWALL_API}/{meta.doi}", params={"email": email})
            resp.raise_for_status()
            return parse_unpaywall(resp.json())
        except Exception as e:
            logger.warning("  ⚠️ Unpaywall 解析失败（{}）: {}".format(meta.doi, e))
            return OAResult(oa_status="unknown", source="unpaywall")
        finally:
            if own_client:
                try:
                    c.close()
                except Exception:
                    pass

    return OAResult(oa_status="closed" if meta.doi else "unknown")


# ---------------- Europe PMC 全文（不经 PDF） ----------------

# 正文以外的成分：参考文献表、图表版面、公式、补充材料清单进了精读正文只会稀释 token；
# xref 是正文里的引文角标（"…如前人所示 12,13 …"），留着会把数字塞进句子中间。
_JATS_SKIP = {"xref", "table-wrap", "fig", "graphic", "inline-graphic",
              "disp-formula", "inline-formula", "ref-list", "back",
              "supplementary-material", "funding-group", "contrib-group",
              "aff", "author-notes", "journal-meta", "history", "permissions"}
# 这些标签结束后补换行，否则整篇会连成一坨没有段落边界的长串
_JATS_BLOCK = {"p", "title", "sec", "abstract", "caption", "list-item", "article-title"}


def _jats_walk(el, parts, budget):
    tag = el.tag if isinstance(el.tag, str) else ""
    tag = tag.rsplit("}", 1)[-1]          # 去命名空间（EuropePMC 通常无 ns，防御性处理）
    if tag not in _JATS_SKIP:
        if el.text:
            parts.append(el.text)
        for child in el:
            if sum(len(p) for p in parts) > budget:
                break
            _jats_walk(child, parts, budget)
        if tag in _JATS_BLOCK:
            parts.append("\n")
    if el.tail:                            # tail 属于父级文本流，跳过的标签也要保留
        parts.append(el.tail)


def jats_to_text(xml_text: str, max_chars: int = 40000) -> str:
    """JATS 全文 XML → 纯文本（标题 + 摘要 + 正文）。解析失败时退化为剥标签。"""
    if not xml_text or not xml_text.strip():
        return ""
    import re as _re
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        # DTD 外部实体未定义等情况：粗暴剥标签也比丢掉整篇全文强
        stripped = _re.sub(r"<[^>]+>", " ", xml_text)
        return _re.sub(r"\s+", " ", stripped).strip()[:max_chars]

    parts: list = []
    for path in ("./front/article-meta/title-group/article-title",
                 "./front/article-meta/abstract",
                 "./body"):
        for node in root.findall(path):
            _jats_walk(node, parts, max_chars * 2)
            parts.append("\n")
    text = "".join(parts)
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


def europepmc_pmcid(doi: Optional[str] = None, pmid: Optional[str] = None,
                    client: Optional[httpx.Client] = None,
                    timeout: float = 20.0) -> Optional[str]:
    """DOI/PMID → Europe PMC 的 PMCID（仅当该文在 EPMC 有全文时才返回）。异常一律 None。"""
    if not doi and not pmid:
        return None
    query = 'DOI:"{}"'.format(doi) if doi else "EXT_ID:{}".format(pmid)
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    try:
        resp = c.get("{}/search".format(EUROPEPMC_API),
                     params={"query": query, "format": "json", "resultType": "core",
                             "pageSize": 1})
        resp.raise_for_status()
        hits = ((resp.json() or {}).get("resultList") or {}).get("result") or []
        if not hits:
            return None
        hit = hits[0]
        # inEPMC=Y 才代表全文在 EPMC 站内可取；只有 pmcid 而 inEPMC=N 的取回来是 404
        if (hit.get("inEPMC") or "").upper() != "Y":
            return None
        return hit.get("pmcid") or None
    except Exception as e:
        logger.warning("  ⚠️ Europe PMC 检索失败（{}）: {}".format(doi or pmid, e))
        return None
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


def europepmc_fulltext(doi: Optional[str] = None, pmid: Optional[str] = None,
                       pmcid: Optional[str] = None,
                       client: Optional[httpx.Client] = None,
                       timeout: float = 40.0, max_chars: int = 40000) -> Optional[str]:
    """取 Europe PMC 的 OA 全文纯文本。取不到（非 OA/无收录/网络异常）返回 None。"""
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    try:
        pid = pmcid or europepmc_pmcid(doi=doi, pmid=pmid, client=c, timeout=timeout)
        if not pid:
            return None
        resp = c.get("{}/{}/fullTextXML".format(EUROPEPMC_API, pid))
        if resp.status_code != 200:
            logger.warning("  ⚠️ Europe PMC 全文返回 {}（{}）".format(resp.status_code, pid))
            return None
        text = jats_to_text(resp.text, max_chars=max_chars)
        return text or None
    except Exception as e:
        logger.warning("  ⚠️ Europe PMC 全文获取失败（{}）: {}".format(pmcid or doi or pmid, e))
        return None
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass
