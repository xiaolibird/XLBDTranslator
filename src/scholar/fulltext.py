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
    budget: Optional["RouteBudget"] = None,
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
        unpaywall_down = False
        # 2) Unpaywall（需 DOI + email）
        #
        # **必须走 polite_get**：这是主路——库里 2091 篇是 doi: 档身份键，非 arXiv 篇目全靠它开门。
        # 2026-09-05 对抗审计（Lens C, BLOCKER）发现此前它是全 fulltext.py 唯一的裸 c.get：
        # 四路（arXiv/EPMC/OpenAlex/S2）都有退避与熔断，唯独主路没有。后果比四路严重得多——
        # 它挂了既不退避也不跳闸，且不记 routes_suppressed，于是「这次没拿到」与「真没有 OA」同形，
        # 一路沉淀成 no_output 永久跳过；而 counts_toward_streak(no_output, expand=True) 是 False，
        # --expand 批连熔断都不会跳，可以一路把整批烧成「这篇没有 OA 全文」。不是有界损失。
        if meta.doi and email:
            try:
                resp = polite_get(c, "{}/{}".format(UNPAYWALL_API, meta.doi),
                                  params={"email": email})
                if resp.status_code != 200:
                    raise RuntimeError("HTTP {}".format(resp.status_code))
                primary = parse_unpaywall(resp.json())
            except Exception as e:
                logger.warning("  ⚠️ Unpaywall 解析失败（{}）: {}".format(meta.doi, e))
                primary = OAResult(oa_status="unknown", source="unpaywall")
                unpaywall_down = True          # 主路没答上 → 下面按抑制记账，绝不判「真没有」
            if primary.pdf_url or not extra_routes:
                if unpaywall_down and not primary.pdf_url:
                    primary.extra = dict(primary.extra or {}, routes_suppressed=1)
                return primary

        # 3) 额外四路
        if extra_routes and c is not None:
            skipped_before = budget.skipped_routes if budget is not None else 0
            cands = extra_route_candidates(meta, c, email=email, delay=route_delay, budget=budget)
            if cands:
                src, url = cands[0]
                return OAResult(oa_status=(primary.oa_status if primary and primary.oa_status
                                           not in ("closed", "unknown") else "green"),
                                pdf_url=url,
                                landing_url=primary.landing_url if primary else None,
                                source=src,
                                extra={"routes": [s for s, _ in cands]},
                                candidates=[u for _, u in cands])
            # 没命中，但有路被熔断跳过或额度已用完：这次「没全文」可能是**暂时的**。把抑制次数
            # 写进 extra，让 close_read_segment → backfill_deepread 与确定性 no_output 分开记账，
            # 否则暂时性失败会沉淀成永久跳过（第 1 轮审计 R1-2.4，复发上一批 quota-failure 那一类）。
            sup = 0
            if budget is not None:
                sup = (budget.skipped_routes - skipped_before) + (1 if budget.exhausted else 0)
            # 主路 Unpaywall 没答上，本身就算一次抑制——四路健康但确实没有 OA 时，
            # sup 会是 0，若不加这一笔，「Unpaywall 挂了」就被记成确定性的「真没有」。
            sup += 1 if unpaywall_down else 0
            if sup > 0:
                if primary is None:
                    primary = OAResult(oa_status="unknown", source=None)
                primary.extra = dict(primary.extra or {}, routes_suppressed=sup)
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


import dataclasses as _dc


class HostBreaker:
    """按宿主熔断（2026-09-05，四通道默认打开时的配套闸）。

    同一 API 宿主连续 `failures` 次**终局**失败（403/429 退避用尽、5xx 服务端故障、或传输异常）后跳闸：此后对该宿主
    的 polite_get 直接返回合成 429、不再发网络，直到冷却期满**半开放行一次**（成功即复位、再失败
    立刻重新跳闸），或显式 reset()。
    为什么按宿主不按通道：OpenAlex/EPMC/S2 各是一个宿主，一路被限流时另几路照常；而同一宿主
    被两个入口（择优阶段的候选预解析 + 精读阶段的下载）共用，按通道计会漏掉一半。
    成功一次即清零连败计数——限流是阵发的，别把一次偶发当成永久。
    """

    def __init__(self, failures: int = 3, cooldown_sec: float = 1800.0, _now=None):
        self.failures = max(1, int(failures))
        # 冷却：跳闸 cooldown_sec 后半开——放一次请求进去，成功即复位、再失败立刻重新跳闸。
        # 没有冷却的话，十来个小时的 expand 批只要 OpenAlex 抖一次，后面几百篇全部与它无缘
        #（第 1 轮审计 R1-2.4）。
        self.cooldown_sec = float(cooldown_sec)
        self._now = _now or __import__("time").monotonic
        self._fails: Dict[str, int] = {}
        self.tripped: set = set()
        self._tripped_at: Dict[str, float] = {}
        self.n_trips = 0

    @staticmethod
    def host_of(url: str) -> str:
        try:
            from urllib.parse import urlsplit
            return (urlsplit(url).hostname or "").lower()
        except Exception:                         # noqa: BLE001
            return ""

    def allow(self, url: str) -> bool:
        h = self.host_of(url)
        if h not in self.tripped:
            return True
        if self.cooldown_sec > 0 and (self._now() - self._tripped_at.get(h, 0.0)) >= self.cooldown_sec:
            # 半开：放行这一次；连败计数留在阈值-1，下一次失败立刻重新跳闸
            self.tripped.discard(h)
            self._fails[h] = self.failures - 1
            logger.info("  熔断冷却到期，半开放行一次：{}".format(h))
            return True
        return False

    def record(self, url: str, ok: bool) -> None:
        h = self.host_of(url)
        if not h:
            return
        if ok:
            self._fails[h] = 0
            return
        self._fails[h] = self._fails.get(h, 0) + 1
        if self._fails[h] >= self.failures and h not in self.tripped:
            self.tripped.add(h)
            self._tripped_at[h] = self._now()
            self.n_trips += 1
            logger.warning("  ⛔ {} 连续 {} 次限流/失败，本进程内暂停请求该宿主（{:.0f}s 后半开放行一次）"
                           .format(h, self._fails[h], self.cooldown_sec))

    def reset(self) -> None:
        self._fails.clear()
        self.tripped.clear()
        self._tripped_at.clear()


# 进程级熔断器：额外四路（择优预解析、候选下载、四路检索）共享。
HOST_BREAKER = HostBreaker()

# 主链路 EPMC 全文回退**专用**熔断器，与四路的 HOST_BREAKER 隔离。四路是锦上添花——OpenAlex
# 抖一次、四路被限流跳闸，绝不该把主链路的 JATS 全文回退一并切断（那才是雪中送炭：PDF 被反爬
# 挡死的 Elsevier/MDPI/OUP 全靠它）。europepmc_pmcid / europepmc_fulltext 缺省走它，
# route_epmc_render 那条四路显式传 HOST_BREAKER（第 2 轮审计 R2-1.1）。
EPMC_BREAKER = HostBreaker()

# europepmc_pmcid 结果缓存：同一批里 route_epmc_render（四路）、close_read_segments 的第二遍、
# europepmc_fulltext 的精读回退会重复问同一 DOI/PMID。只缓存**命中**（非 None）——成功查到 pmcid
# 是进程生命周期内的稳定事实，重复问只会再打一次 ebi.ac.uk（第 2 轮审计 R2-1.3：实测同 query
# 重复 3~4 次，range(2) 已撤、这里再收掉命中重复）。None 不缓存：那可能是未收录（稳定）也可能
# 是一次 429 抖动（暂时），缓存会把一次抖动变成本批永久缺失。
_PMCID_CACHE: Dict[Tuple[str, str], Optional[str]] = {}


def reset_pmcid_cache() -> None:
    """清空 europepmc_pmcid 结果缓存（进程级状态，测试用）。"""
    _PMCID_CACHE.clear()


@_dc.dataclass
class RouteBudget:
    """一次精读批里额外四路的总额度 + 熔断阈值（由 close_read_segments 创建、整批共享）。

    max_calls：四路各算一次调用，超出后 extra_route_candidates 直接返回 []（只走主链路）；
    breaker_failures：写进进程级 HOST_BREAKER 的阈值。exhausted 只打一次日志。
    """
    max_calls: int = 80          # = 候选篇数(20) × 4 路；2026-09-05 真网络实测 24 只够 6 篇
    breaker_failures: int = 3
    calls: int = 0
    skipped_routes: int = 0        # 因宿主熔断被跳过的路数（整批累计）
    exhausted: bool = False        # 额度是否已用完（之后的候选只走主链路）
    _warned: bool = False

    def __post_init__(self):
        HOST_BREAKER.failures = max(1, int(self.breaker_failures))
        EPMC_BREAKER.failures = max(1, int(self.breaker_failures))

    def take(self) -> bool:
        if self.calls >= self.max_calls:
            self.exhausted = True
            if not self._warned:
                logger.warning("  ⚠️ 额外通道本批额度 {} 次已用完，其余候选只走主链路".format(self.max_calls))
                self._warned = True
            return False
        self.calls += 1
        return True


class _SyntheticResponse:
    """熔断时 polite_get 返回的替身：status_code=429、text 说明原因，不触网。"""
    status_code = 429

    def __init__(self, host: str):
        self.text = "host {} tripped by HostBreaker".format(host)
        self.content = b""
        self.headers = {}

    def json(self):
        return {}

    def raise_for_status(self):
        raise RuntimeError(self.text)


def polite_get(client, url: str, *, retries: int = _POLITE_RETRIES,
               first_delay: float = _POLITE_FIRST_DELAY, backoff: float = _POLITE_BACKOFF,
               sleep=None, breaker: Optional[HostBreaker] = None, **kw):
    """403/429 指数退避重试：公开 API 上这两个码多半是「打太快」不是「没有」，直接判死会把
    本来拿得到的篇目误写进人工清单（补抓脚本实测 24 篇假阴性）。sleep 可注入（测试免等）。

    breaker（缺省 HOST_BREAKER）：宿主已跳闸则不发网络、直接返回合成 429；退避用尽仍 403/429
    或传输异常 → 记一次终局失败；拿到任何别的状态码 → 清零该宿主连败。
    """
    import time as _time
    _sleep = sleep or _time.sleep
    br = breaker if breaker is not None else HOST_BREAKER
    if not br.allow(url):
        return _SyntheticResponse(br.host_of(url))
    delay = first_delay
    r = None
    n = max(1, retries)
    try:
        for i in range(n):
            r = client.get(url, **kw)
            if r.status_code not in (403, 429):
                # 非限流码：<500 视为成功、清零连败；5xx 服务端故障也返回原响应（路由侧 !=200 自会
                # 返回 []），但按**终局失败**计——否则 5xx 潮（如 export.arxiv.org 的 503）永不跳闸，
                # 一整批会被记成 no_output 永久跳过（第 3 轮审计 R3-A1）。
                br.record(url, r.status_code < 500)
                return r
            if i < n - 1:                 # 最后一轮失败后不再白等（死候选曾多等 31s）
                _sleep(delay)
                delay *= backoff
    except Exception:
        br.record(url, False)
        raise
    br.record(url, False)                 # 退避用尽仍是 403/429 = 终局失败
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
        # 走 polite_get：export.arxiv.org 对连发请求回 429/503，且要过 HOST_BREAKER 那道闸
        r = polite_get(client, "http://export.arxiv.org/api/query",
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
    try:
        # 这条是四路之一，显式走 HOST_BREAKER（与主链路 EPMC_BREAKER 隔离，R2-1.1）；
        # 不重试——polite_get 内部已对 403/429 退避（R2-1.3）。
        pmcid = europepmc_pmcid(doi=meta.doi or None, pmid=getattr(meta, "pmid", None) or None,
                                client=client, breaker=HOST_BREAKER)
    except Exception:                         # noqa: BLE001
        pmcid = None
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


_ROUTE_HOSTS = {
    "route_arxiv_title": "export.arxiv.org",
    "route_epmc_render": "www.ebi.ac.uk",
    "route_openalex": "api.openalex.org",
    "route_s2": "api.semanticscholar.org",
}


def _route_applicable(route, meta: PaperMetadata) -> bool:
    """这条路对本篇**本来就会发请求**吗？被熔断跳过时，只有「适用」的路才计 skipped_routes。

    无 DOI 篇目 openalex/s2/epmc 开头就 return []（拿不到标识没法查），宿主跳不跳闸都跑不到；
    把它计进 skipped_routes 会把「真没有全文」误标成「被抑制」（第 2 轮审计 R2-1.2）。
    """
    name = route.__name__
    if name == "route_arxiv_title":
        t = (meta.title or "").strip()
        return len(t) >= 12 and len(_title_words(t)) >= _ARXIV_TITLE_MIN_WORDS
    if name == "route_epmc_render":
        return bool((meta.doi or "").strip()) or bool(getattr(meta, "pmid", None))
    if name in ("route_openalex", "route_s2"):
        return bool((meta.doi or "").strip())
    return True


def extra_route_candidates(meta: PaperMetadata, client, *, email: str = "",
                           delay: float = 0.0, sleep=None,
                           budget: Optional[RouteBudget] = None) -> List[Tuple[str, str]]:
    """按实测命中率顺序跑四路，返回去重后的 (source, url) 列表；任何一路异常都跳过。
    delay 是路与路之间的礼貌间隔（首路之前不等）。

    budget 非空时：每跑一路记一次调用，额度用完不再跑剩余各路（本篇返回已跑到的候选、可能非空，
    之后的篇目直接只走主链路）；宿主已被 HOST_BREAKER 跳闸的路不再调（省下 delay 与一次注定的
    合成 429），且只有对本篇**适用**的路才把这次跳过计入 budget.skipped_routes（R2-1.2）。
    """
    import time as _time
    _sleep = sleep or _time.sleep
    out: List[Tuple[str, str]] = []
    seen = set()
    ran = 0
    for route in EXTRA_ROUTES:
        host = _ROUTE_HOSTS.get(route.__name__, "")
        if host and host in HOST_BREAKER.tripped:
            if budget is not None and _route_applicable(route, meta):
                budget.skipped_routes += 1
            continue
        if budget is not None and not budget.take():
            break
        if ran and delay > 0:
            _sleep(delay)
        ran += 1
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


def _jats_to_text_ex(xml_text: str, max_chars: int = 40000):
    """JATS 全文 XML → (纯文本, 原始长度, 总上限之外的余量)。原始长度是 max_chars 截断**之前**的长度。

    余量（tail）供两轮读的第二轮：EPMC 正文没有「页」，但总上限之外的部分同样能另起一轮读
    （2026-09-05，第 1 轮审计 R1-1.1）。
    改用 _JATS_RAW_BUDGET 后 text 的返回值逐字节不变：原预算下被跳过的下钻只发生在
    累计长度已 > 2×max_chars 之后，其产生的差异全部落在 > max_chars 的偏移上，
    被末尾 [:max_chars] 切掉。
    """
    if not xml_text or not xml_text.strip():
        return "", 0, ""
    import re as _re
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        # DTD 外部实体未定义等情况：粗暴剥标签也比丢掉整篇全文强
        stripped = _re.sub(r"<[^>]+>", " ", xml_text)
        text = _re.sub(r"\s+", " ", stripped).strip()
        return text[:max_chars], len(text), text[max_chars:]

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
    return text[:max_chars], len(text), text[max_chars:]


def _jats_to_text_with_stats(xml_text: str, max_chars: int = 40000):
    """JATS 全文 XML → (纯文本, 原始长度)。老签名，薄包装 `_jats_to_text_ex`。"""
    t, raw, _tail = _jats_to_text_ex(xml_text, max_chars=max_chars)
    return t, raw


def jats_to_text(xml_text: str, max_chars: int = 40000) -> str:
    """JATS 全文 XML → 纯文本（标题 + 摘要 + 正文）。解析失败时退化为剥标签。"""
    return _jats_to_text_with_stats(xml_text, max_chars=max_chars)[0]


def europepmc_pmcid(doi: Optional[str] = None, pmid: Optional[str] = None,
                    client: Optional[httpx.Client] = None,
                    timeout: float = 20.0, *, breaker: Optional[HostBreaker] = None) -> Optional[str]:
    """DOI/PMID → Europe PMC 的 PMCID（仅当该文在 EPMC 有全文时才返回）。异常一律 None。

    breaker 缺省 EPMC_BREAKER（主链路专用）——四路的 HOST_BREAKER 被限流跳闸**不得**切断主链路
    的回退；route_epmc_render 这条四路显式传 breaker=HOST_BREAKER（第 2 轮审计 R2-1.1）。
    """
    if not doi and not pmid:
        return None
    key = (doi or "", pmid or "")
    if key in _PMCID_CACHE:
        return _PMCID_CACHE[key]
    query = 'DOI:"{}"'.format(doi) if doi else "EXT_ID:{}".format(pmid)
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    br = breaker if breaker is not None else EPMC_BREAKER
    try:
        # 走 polite_get：ebi.ac.uk 也要过 403/429 退避与熔断（R1-2.3）
        resp = polite_get(c, "{}/search".format(EUROPEPMC_API),
                          params={"query": query, "format": "json", "resultType": "core",
                                  "pageSize": 1}, breaker=br)
        resp.raise_for_status()
        hits = ((resp.json() or {}).get("resultList") or {}).get("result") or []
        if not hits:
            return None                               # 未收录：None 不缓存（见 _PMCID_CACHE 注释）
        hit = hits[0]
        # inEPMC=Y 才代表全文在 EPMC 站内可取；只有 pmcid 而 inEPMC=N 的取回来是 404
        if (hit.get("inEPMC") or "").upper() != "Y":
            return None
        result = hit.get("pmcid") or None
        if result:
            _PMCID_CACHE[key] = result
        return result
    except Exception as e:
        logger.warning("  ⚠️ Europe PMC 检索失败（{}）: {}".format(doi or pmid, e))
        return None                                  # 异常不缓存：可能是暂时的
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
                       return_stats: bool = False, return_tail: bool = False,
                       *, breaker: Optional[HostBreaker] = None):
    """取 Europe PMC 的 OA 全文纯文本。取不到（非 OA/无收录/网络异常）返回 None。

    return_stats=False（默认）→ 返回 Optional[str]，与旧签名逐字节一致，调用方零改动。
    return_stats=True → **四条出口一律返回 2-元组**，取不到统一为 (None, 0)；
      绝不允许 True 分支漏出裸 None：调用方对每篇拿不到 PDF 的论文都会解包，
      而绝大多数论文非 OA、走的正是失败出口，漏一处就是每篇 TypeError → 精读全挂 →
      done 归零 → 整批被判成 LLM 故障批而不写终稿。
    return_tail=True（须与 return_stats 同用）→ 3-元组 (text, raw, tail)，tail 是总上限之外的余量，
      供两轮读第二轮；取不到统一为 (None, 0, "")。

    2026-09-05：两次请求都走 polite_get——www.ebi.ac.uk 此前完全不经退避与熔断（第 1 轮审计
    R1-2.3）。breaker 缺省 EPMC_BREAKER（主链路专用，与四路的 HOST_BREAKER 隔离——R2-1.1）。
    """
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    br = breaker if breaker is not None else EPMC_BREAKER

    def _fail():
        if return_stats and return_tail:
            return (None, 0, "")
        return (None, 0) if return_stats else None

    try:
        pid = pmcid or europepmc_pmcid(doi=doi, pmid=pmid, client=c, timeout=timeout, breaker=br)
        if not pid:
            return _fail()
        resp = polite_get(c, "{}/{}/fullTextXML".format(EUROPEPMC_API, pid), breaker=br)
        if resp.status_code != 200:
            logger.warning("  ⚠️ Europe PMC 全文返回 {}（{}）".format(resp.status_code, pid))
            return _fail()
        text, raw_chars, tail = _jats_to_text_ex(resp.text, max_chars=max_chars)
        if not text:
            return _fail()
        if return_stats and return_tail:
            return text, raw_chars, tail
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
