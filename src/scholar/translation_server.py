# -*- coding: utf-8 -*-
"""
Zotero translation-server 客户端：把标识符（DOI/arXiv/PMID）交给 Zotero 官方翻译器解析为
权威元数据（作者/期刊/卷期/DOI），替代我们自己刮取——Zotero 作为矫正/权威层。

translation-server 是 Zotero 官方 Docker 服务（`zotero/translation-server`，默认端口 1969）：
  POST /search  body=text/plain 的标识符 → 返回 Zotero item JSON 数组（可直接喂连接器 saveItems）。

我们据此：① 用解析结果回填 PaperMetadata（令札记与 references.json 用权威数据）；
         ② 把权威 item 经连接器写入 Zotero；③ BBT 生成 citekey。
标识符由 Crossref（DOI）/ arXiv id / PMID 提供；无任何标识符的论文退回自建 item。

批量入口是 `resolve_batch`：探活 → 并行解析 → 结果级告警，ingest（周/月入库）与
zotero_sync（digest --zotero）共用，停机/失效只在这一处判、只弹一次系统通知。
"""
import concurrent.futures
import re
import threading
from datetime import date
from typing import List, Dict, Any, Optional, Set, Tuple

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
    raw_date = item.get("date", "")
    d = _parse_date(raw_date)
    if d and not meta.publication_date:
        meta.publication_date = d
        # _parse_date 的日恒为 1、月解析不出时也退到 1，所以真实精度只能从原串倒推：
        # 原串里除年之外还挑得出 1-12 的数 → 月精度，否则只有年。签名不动（有测试钉死）。
        import re as _re
        toks = [t for t in _re.findall(r"\d+", raw_date or "") if t != str(d.year)]
        meta.date_precision = "month" if any(1 <= int(t) <= 12 for t in toks) else "year"
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
    """translation-server 是否在线——只回答「端口上有没有进程在收请求」。

    用**空 body** 探活：server 在路由层直接回 400 "POST data not provided"，0.04s、
    零出网。此前用假 DOI "10.0000/probe" 探活会让 server 真去 doi.org 解析一次
    （实测 0.63s、依赖外网），上游慢或限流时探活会假阴性。

    只有**连接层**失败（拒绝 / 连接超时）才算离线。连上了但读超时、协议异常等
    一律按在线处理：容器在收请求只是慢，让逐篇解析自己的 30s 超时兜底——否则
    容器忙时（如 realign 批量重对齐撞上周一 ingest）整批被判离线、跳过权威解析还误报。
    """
    try:
        with ipv4_client(timeout=timeout) as c:
            r = c.post("{}/search".format(base_url.rstrip("/")),
                       content=b"", headers={"Content-Type": "text/plain"})
            # 空 body 正常回 400；200/500 兼容不同版本 server。404/415/502 = 端口上跑的不是它
            ok = r.status_code in (200, 400, 500)
            if not ok:
                logger.debug("translation-server 探活 {} 回 {}（非预期状态，按离线）", base_url, r.status_code)
            return ok
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # localhost 会先试 ::1，ipv4_client 绑了 0.0.0.0 会先报 Errno 47 再回落 127.0.0.1；
        # 容器真离线时最终浮出的也是这条而不是 refused——排障看到 Errno 47 别被带偏。
        logger.debug("translation-server 探活失败（连接层）{}: {}", base_url, e)
        return False
    except Exception as e:
        logger.debug("translation-server 探活异常（非连接层，按在线）{}: {}", base_url, e)
        return True


# ---------------- 批量入口：探活 → 并行解析 → 结果级告警 ----------------

ALERT_TITLE = "Scholar 元数据对齐"
# 进程内每个 (ts_url, 事由) 只弹一次系统通知：backfill 多月循环（默认 41 个月）
# 若每月弹一条，告警面就被刷成噪音（notify 文档里 2026-08-24 的教训）；日志每批照记。
_ALERTED: Set[Tuple[str, str]] = set()
_ALERT_LOCK = threading.Lock()


def _alert_once(base_url: str, kind: str, text: str) -> bool:
    with _ALERT_LOCK:
        key = (base_url, kind)
        if key in _ALERTED:
            return False
        _ALERTED.add(key)
    try:
        from ..utils.notify import notify
        notify(ALERT_TITLE, text)
    except Exception as e:                       # 告警面挂了不能把入库弄挂
        logger.debug("notify 失败: {}", e)
    return True


def resolve_batch(
    metas: Dict[Any, PaperMetadata],
    base_url: str,
    workers: int = 4,
) -> Dict[Any, Optional[Dict[str, Any]]]:
    """对一批 PaperMetadata 做权威解析并就地回填。返回 {key: 权威 item | None}。

    三段：
    1. 探活（`is_available`）。不在线 → warning + 系统通知（进程内一次）→ 整批 None。
       此前逐篇解析各自吞异常只落 stderr WARNING，launchd 日志无人看：2026-08-25→09-01
       容器停机整周，周一入库照常产出、卷期页全空，直到人翻 references.json 才发现。
       不抛、不改退出码：权威解析是矫正层不是必需层，札记照常产出，事后用
       scripts/realign_metadata_ts.py 补对齐。
    2. 分片并行解析（每 worker 一个连接，复用 TLS）。
    3. 结果级告警：探活过了但「有标识符的论文 0 命中」= 出网断 / 上游限流 / 翻译器损坏，
       探活看不出（探活零出网），只能从结果判——同样 warning + 通知一次。
    """
    out: Dict[Any, Optional[Dict[str, Any]]] = {k: None for k in metas}
    if not metas or not base_url:
        return out
    n_ident = sum(1 for m in metas.values() if best_identifier(m))
    if not is_available(base_url):
        logger.warning("  ⚠️ translation-server 不在线（{}）：本批 {} 篇跳过权威解析，元数据走自建口径；"
                       "`docker start zotero-translation-server` 后可用 scripts/realign_metadata_ts.py 补对齐"
                       .format(base_url, len(metas)))
        _alert_once(base_url, "offline",
                    "translation-server 不在线，{} 篇未走权威解析（{}）".format(len(metas), base_url))
        return out

    keys = list(metas)
    workers = max(1, min(workers, len(keys)))
    chunks = [keys[i::workers] for i in range(workers)]

    def _run(batch):
        res = {}
        with ipv4_client(timeout=30) as c:
            for k in batch:
                try:
                    res[k] = resolve_and_apply(metas[k], base_url=base_url, client=c)
                except Exception as e:
                    logger.debug("translation-server 解析失败 [{}]: {}", k, e)
                    res[k] = None
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_run, chunks):
            out.update(r)
    hits = sum(1 for v in out.values() if v)
    logger.info("  translation-server 权威解析 {}/{} 篇（{} 篇有标识符）".format(hits, len(metas), n_ident))
    if hits == 0 and n_ident:
        # 「探活通过」而非「在线」：URL 拼错（协议/主机名错）时非连接层异常也按在线放行，
        # 这里是它唯一的告警出口，文案不能把锅甩给上游
        logger.warning("  ⚠️ translation-server 探活通过但 {} 篇有标识符的论文 0 命中——出网断/上游限流/"
                       "翻译器异常/URL 配错，元数据走自建口径；事后用 scripts/realign_metadata_ts.py 补对齐"
                       .format(n_ident))
        _alert_once(base_url, "zero-hit",
                    "translation-server 探活通过但 0/{} 命中，元数据未对齐（{}）".format(n_ident, base_url))
    return out
