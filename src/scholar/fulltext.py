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
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple

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
    """OA 解析结果。pdf_url 为 None 表示未找到合法免费 PDF。

    candidates：额外通道（extra_routes）给出的**备选** PDF 直链列表（含 pdf_url 自己，按命中
    顺序），下载方逐个试到一个通过校验为止。主链路（arXiv/Unpaywall）不填它——保持旧行为。
    """
    oa_status: str = "unknown"          # arxiv / gold / green / hybrid / bronze / closed / unknown
    pdf_url: Optional[str] = None        # 直接可下载的 PDF 链接
    landing_url: Optional[str] = None    # OA 落地页（非 PDF 直链时）
    source: Optional[str] = None         # arxiv / unpaywall / arxiv-title / epmc-render / openalex / s2
    extra: Dict[str, Any] = field(default_factory=dict)
    candidates: List[str] = field(default_factory=list)

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
    *,
    extra_routes: bool = False,
    route_delay: float = 0.0,
) -> OAResult:
    """按 arXiv → Unpaywall（→ 额外四路）顺序解析 OA PDF。任何异常都降级为 closed，绝不抛出中断上层。

    email 是 Unpaywall 的强制礼貌参数（联系句柄，非密钥）；缺失则跳过 Unpaywall。

    extra_routes=False（默认）时行为与历史版本**逐字节一致**。为 True 时，前两路都没给出
    pdf_url 才继续试 `extra_route_candidates` 的四路（arXiv 标题检索 → EPMC 渲染版 PDF →
    OpenAlex → Semantic Scholar），命中则 pdf_url 取第一个、全部候选进 `candidates`，由下载方
    逐个试并做 PDF 三闸校验（反爬页也回 200，见 download_pdf 的 validate）。route_delay 是
    每次额外 API 调用之间的礼貌间隔。设计与边界见 docs/bugs/2026-09-04-fulltext-routes-too-narrow.md。
    """
    # 1) arXiv 直链
    if meta.arxiv_id:
        return OAResult(oa_status="arxiv", pdf_url=arxiv_pdf_url(meta.arxiv_id), source="arxiv")

    own_client = client is None
    c = client if client is not None else (ipv4_client(timeout=timeout)
                                           if (meta.doi and email) or extra_routes else None)
    try:
        primary: Optional[OAResult] = None
        # 2) Unpaywall（需 DOI + email）
        if meta.doi and email:
            try:
                resp = c.get(f"{UNPAYWALL_API}/{meta.doi}", params={"email": email})
                resp.raise_for_status()
                primary = parse_unpaywall(resp.json())
            except Exception as e:
                logger.warning("  ⚠️ Unpaywall 解析失败（{}）: {}".format(meta.doi, e))
                primary = OAResult(oa_status="unknown", source="unpaywall")
            if primary.pdf_url or not extra_routes:
                return primary

        # 3) 额外四路（默认关）
        if extra_routes and c is not None:
            cands = extra_route_candidates(meta, c, email=email, delay=route_delay)
            if cands:
                src, url = cands[0]
                return OAResult(oa_status=(primary.oa_status if primary and primary.oa_status
                                           not in ("closed", "unknown") else "green"),
                                pdf_url=url,
                                landing_url=primary.landing_url if primary else None,
                                source=src,
                                extra={"routes": [s for s, _ in cands]},
                                candidates=[u for _, u in cands])
        if primary is not None:
            return primary
    finally:
        if own_client and c is not None:
            try:
                c.close()
            except Exception:
                pass

    return OAResult(oa_status="closed" if meta.doi else "unknown")


# ---------------- 额外四路（默认关；从 scripts/fetch_missing_pdfs.py 下沉） ----------------
#
# 只走公开 API 与开放副本；**不做任何绕过出版商反爬的改写**（换 UA / 加 Referer / 走机构 IP
# 都实测无效，那是 Cloudflare 认"你不像浏览器"，不是订阅问题——那类篇目应进人工清单）。
# 唯一的宿主改写是 NCBI PMC → Europe PMC：两家镜像同一份 PMC 全文，NCBI 对脚本 403、EPMC 不挡。
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
PDF_ACCEPT_HEADERS = {"Accept": "application/pdf,*/*;q=0.8"}
_ARXIV_TITLE_MIN_OVERLAP = 0.75      # 查询标题实词被命中标题覆盖的比例
_ARXIV_TITLE_MIN_BACK_OVERLAP = 0.6  # 反向：命中标题实词被查询覆盖的比例（防短标题被长综述包含）
_ARXIV_TITLE_MIN_WORDS = 4           # 实词太少的标题不检索：「Missing Data Imputation」对谁都 1.0
_PMC_URL_RE = re.compile(
    r"(?:pmc\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov)/(?:pmc/)?articles/(PMC\d+)")
_POLITE_RETRIES = 3
_POLITE_FIRST_DELAY = 5.0
_POLITE_BACKOFF = 2.5
# PDF 三闸：magic / 体积 / 可解析页数。反爬页与登录页也回 200，不卡死会流到 _pdf_text_with_stats
# 炸成空文本，然后被误诊成「精读质量差」。
# 体积闸原取 20KB（补抓脚本经验值），会误杀 2 页纯文本的合法短文（实测 1.2KB）；反爬/登录页
# 由 magic + 页数两闸兜，这里只挡明显的占位空壳。
PDF_MIN_BYTES = 1_000
PDF_MIN_PAGES = 2
# PDF 规范允许 %PDF- 头出现在文件前 1024 字节内（前面可有 BOM / \r\n / 垃圾字节）
_PDF_MAGIC_WINDOW = 1024


def polite_get(client, url: str, *, retries: int = _POLITE_RETRIES,
               first_delay: float = _POLITE_FIRST_DELAY, backoff: float = _POLITE_BACKOFF,
               sleep=None, **kw):
    """403/429 指数退避重试：公开 API 上这两个码多半是「打太快」不是「没有」，直接判死会把
    本来拿得到的篇目误写进人工清单（补抓脚本实测 24 篇假阴性）。sleep 可注入（测试免等）。"""
    import time as _time
    _sleep = sleep or _time.sleep
    delay = first_delay
    r = None
    n = max(1, retries)
    for i in range(n):
        r = client.get(url, **kw)
        if r.status_code not in (403, 429):
            return r
        if i < n - 1:                 # 最后一轮失败后不再白等（死候选曾多等 31s）
            _sleep(delay)
            delay *= backoff
    return r


def rewrite_pmc_url(url: str) -> str:
    """NCBI PMC 的 articles/PMC… → Europe PMC 渲染版 PDF（同一份文件、不挡脚本的宿主）。其余原样。"""
    m = _PMC_URL_RE.search(url or "")
    if m:
        return "https://europepmc.org/articles/{}?pdf=render".format(m.group(1))
    return url


def _title_words(t: str) -> set:
    return set(re.findall(r"[a-z]{4,}", (t or "").lower()))


def route_arxiv_title(meta: PaperMetadata, client) -> List[Tuple[str, str]]:
    """无 arxiv_id 时按标题在 arXiv 检索副本；只认词面重合 ≥ 0.75 的命中。"""
    title = (meta.title or "").strip()
    if len(title) < 12 or len(_title_words(title)) < _ARXIV_TITLE_MIN_WORDS:
        return []
    try:
        r = client.get("http://export.arxiv.org/api/query",
                       params={"search_query": 'ti:"{}"'.format(title[:180]), "max_results": 1},
                       timeout=30.0)
        if r.status_code != 200:
            return []
        body = r.text
        entry = body[body.find("<entry>"):] if "<entry>" in body else ""
        got = re.search(r"<title>(.*?)</title>", entry, re.S)
        pid = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", entry)
        if not (got and pid):
            return []
        a, b = _title_words(title), _title_words(got.group(1))
        # 双向：|a∩b|/|a| 只防「命中标题缺词」，防不了「命中标题是包含本标题的长综述」（压测 S2）
        if (not a or not b or len(a & b) / len(a) < _ARXIV_TITLE_MIN_OVERLAP
                or len(a & b) / len(b) < _ARXIV_TITLE_MIN_BACK_OVERLAP):
            return []
        return [("arxiv-title", "https://arxiv.org/pdf/{}".format(pid.group(1).strip()))]
    except Exception:                             # noqa: BLE001
        return []


def route_epmc_render(meta: PaperMetadata, client) -> List[Tuple[str, str]]:
    """EPMC 渲染版 PDF——与 JATS 全文接口是两套：XML 404 不代表没 PDF（geva2021 实测）。"""
    pmcid = None
    for _ in range(2):        # EPMC 偶发 502，重试一次再判死
        try:
            pmcid = europepmc_pmcid(doi=meta.doi or None, pmid=getattr(meta, "pmid", None) or None,
                                    client=client)
        except Exception:                         # noqa: BLE001
            pmcid = None
        if pmcid:
            break
    if not pmcid:
        return []
    return [("epmc-render", "https://europepmc.org/articles/{}?pdf=render".format(pmcid))]


def route_openalex(meta: PaperMetadata, client, email: str = "") -> List[Tuple[str, str]]:
    """OpenAlex best_oa_location + locations 的 pdf_url（最多 3 条），NCBI PMC 链接换成 EPMC。"""
    doi = (meta.doi or "").strip()
    if not doi:
        return []
    try:
        r = polite_get(client, "https://api.openalex.org/works/doi:{}".format(doi),
                       params={"mailto": email} if email else None,
                       headers={"User-Agent": "{} mailto:{}".format(_BROWSER_UA, email)} if email else None,
                       timeout=30.0)
        if r.status_code != 200:
            return []
        d = r.json() or {}
    except Exception:                             # noqa: BLE001
        return []
    out, seen = [], set()
    for loc in [d.get("best_oa_location")] + list(d.get("locations") or []):
        u = rewrite_pmc_url(((loc or {}).get("pdf_url") or "").strip())
        if u and u not in seen:
            seen.add(u)
            out.append(("openalex", u))
    return out[:3]


def route_s2(meta: PaperMetadata, client) -> List[Tuple[str, str]]:
    """Semantic Scholar openAccessPdf；把 doi.org 本身填进来的等于没给，过滤掉。"""
    doi = (meta.doi or "").strip()
    if not doi:
        return []
    try:
        r = polite_get(client, "https://api.semanticscholar.org/graph/v1/paper/DOI:{}".format(doi),
                       params={"fields": "openAccessPdf"}, timeout=30.0)
        if r.status_code != 200:
            return []
        u = ((r.json() or {}).get("openAccessPdf") or {}).get("url") or ""
    except Exception:                             # noqa: BLE001
        return []
    u = u.strip()
    if not u or "doi.org/" in u:
        return []
    return [("s2", rewrite_pmc_url(u))]


EXTRA_ROUTES = (route_arxiv_title, route_epmc_render, route_openalex, route_s2)


def extra_route_candidates(meta: PaperMetadata, client, *, email: str = "",
                           delay: float = 0.0, sleep=None) -> List[Tuple[str, str]]:
    """按实测命中率顺序跑四路，返回去重后的 (source, url) 列表；任何一路异常都跳过。
    delay 是路与路之间的礼貌间隔（首路之前不等）。"""
    import time as _time
    _sleep = sleep or _time.sleep
    out: List[Tuple[str, str]] = []
    seen = set()
    for i, route in enumerate(EXTRA_ROUTES):
        if i and delay > 0:
            _sleep(delay)
        try:
            hits = route(meta, client, email=email) if route is route_openalex else route(meta, client)
        except Exception:                         # noqa: BLE001
            hits = []
        for src, url in hits:
            if url and url not in seen:
                seen.add(url)
                out.append((src, url))
    if out:
        logger.info("  额外通道命中 {} 条候选（{}）".format(
            len(out), " / ".join(sorted({s for s, _ in out}))))
    return out


def validate_pdf_bytes(blob: bytes, *, min_bytes: int = PDF_MIN_BYTES,
                       min_pages: int = PDF_MIN_PAGES) -> Tuple[bool, str]:
    """三道闸：%PDF magic、体积、可解析页数。返回 (是否通过, 人读原因)。
    pypdf 缺失时跳过页数闸（前两闸仍卡）。"""
    if b"%PDF-" not in blob[:_PDF_MAGIC_WINDOW]:
        return False, "不是 PDF（多半是反爬页/登录页）"
    if len(blob) < min_bytes:
        return False, "体积仅 {} 字节，疑似占位页".format(len(blob))
    try:
        import io as _io
        from pypdf import PdfReader
    except Exception:                             # noqa: BLE001
        return True, "{:.1f} KB（未装 pypdf，跳过页数闸）".format(len(blob) / 1024)
    try:
        n = len(PdfReader(_io.BytesIO(blob)).pages)
    except Exception as e:                        # noqa: BLE001
        return False, "PDF 解析失败：{}".format(e)
    if n < min_pages:
        return False, "只有 {} 页".format(n)
    return True, "{} 页 / {:.1f} KB".format(n, len(blob) / 1024)


# ---------------- Europe PMC 全文（不经 PDF） ----------------

# 正文以外的成分：参考文献表、图片版面、公式进了精读正文只会稀释 token；
# xref 是正文里的引文角标（"…如前人所示 12,13 …"），留着会把数字塞进句子中间。
# table-wrap / fig / supplementary-material 曾在此集合里，现已移出：精读要引用的效应量、
# 样本量、置信区间基本只出现在 Results 的表格与图注里，整块丢掉等于把最硬的数字丢掉。
# 真正的版面噪声是 graphic/inline-graphic（图片文件名），它们仍被跳过。
_JATS_SKIP = {"xref", "graphic", "inline-graphic",
              "disp-formula", "inline-formula", "ref-list", "back",
              "funding-group", "contrib-group",
              "aff", "author-notes", "journal-meta", "history", "permissions"}
# 这些标签结束后补换行，否则整篇会连成一坨没有段落边界的长串。
# table-wrap/tr/td/th 是随表格解禁一并加入的：单元格之间没有任何分隔符，
# 不补换行会把一行数字连成 "0.910.880.94" 这种无法归属的长串，并粘住前后正文。
_JATS_BLOCK = {"p", "title", "sec", "abstract", "caption", "list-item", "article-title",
               "table-wrap", "tr", "td", "th"}


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


# 只为「量出真长度」而设的 walk 预算：与 max_chars 解耦。
# 原先传的是 max_chars*2，于是截断前的文本本身就被封顶在 ~2×max_chars，EPMC 路线的
# 「原始长度」和 PDF 路线的真实长度不是同一量纲，截断比例统计会被系统性压低。
# 取 2,000,000 只是为了给病态输入留个天花板；正文超过它时 raw 退化为下界。
_JATS_RAW_BUDGET = 2_000_000


def _jats_to_text_with_stats(xml_text: str, max_chars: int = 40000):
    """JATS 全文 XML → (纯文本, 原始长度)。原始长度是 max_chars 截断**之前**的长度。

    改用 _JATS_RAW_BUDGET 后 text 的返回值逐字节不变：原预算下被跳过的下钻只发生在
    累计长度已 > 2×max_chars 之后，其产生的差异全部落在 > max_chars 的偏移上，
    被末尾 [:max_chars] 切掉。
    """
    if not xml_text or not xml_text.strip():
        return "", 0
    import re as _re
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        # DTD 外部实体未定义等情况：粗暴剥标签也比丢掉整篇全文强
        stripped = _re.sub(r"<[^>]+>", " ", xml_text)
        text = _re.sub(r"\s+", " ", stripped).strip()
        return text[:max_chars], len(text)

    parts: list = []
    for path in ("./front/article-meta/title-group/article-title",
                 "./front/article-meta/abstract",
                 "./body"):
        for node in root.findall(path):
            _jats_walk(node, parts, _JATS_RAW_BUDGET)
            parts.append("\n")
    text = "".join(parts)
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text[:max_chars], len(text)


def jats_to_text(xml_text: str, max_chars: int = 40000) -> str:
    """JATS 全文 XML → 纯文本（标题 + 摘要 + 正文）。解析失败时退化为剥标签。"""
    return _jats_to_text_with_stats(xml_text, max_chars=max_chars)[0]


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
                       timeout: float = 40.0, max_chars: int = 40000,
                       return_stats: bool = False):
    """取 Europe PMC 的 OA 全文纯文本。取不到（非 OA/无收录/网络异常）返回 None。

    return_stats=False（默认）→ 返回 Optional[str]，与旧签名逐字节一致，调用方零改动。
    return_stats=True → **四条出口一律返回 2-元组**，取不到统一为 (None, 0)；
      绝不允许 True 分支漏出裸 None：调用方对每篇拿不到 PDF 的论文都会解包，
      而绝大多数论文非 OA、走的正是失败出口，漏一处就是每篇 TypeError → 精读全挂 →
      done 归零 → 整批被判成 LLM 故障批而不写终稿。
    """
    own = client is None
    c = client or ipv4_client(timeout=timeout)

    def _fail():
        return (None, 0) if return_stats else None

    try:
        pid = pmcid or europepmc_pmcid(doi=doi, pmid=pmid, client=c, timeout=timeout)
        if not pid:
            return _fail()
        resp = c.get("{}/{}/fullTextXML".format(EUROPEPMC_API, pid))
        if resp.status_code != 200:
            logger.warning("  ⚠️ Europe PMC 全文返回 {}（{}）".format(resp.status_code, pid))
            return _fail()
        text, raw_chars = _jats_to_text_with_stats(resp.text, max_chars=max_chars)
        if not text:
            return _fail()
        return (text, raw_chars) if return_stats else text
    except Exception as e:
        logger.warning("  ⚠️ Europe PMC 全文获取失败（{}）: {}".format(pmcid or doi or pmid, e))
        return _fail()
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass
