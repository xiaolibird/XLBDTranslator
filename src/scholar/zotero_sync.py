# -*- coding: utf-8 -*-
"""
Zotero 本地写库 + Better BibTeX citekey 解析（B1 全自动写库）。

写通道：Zotero 连接器服务 `/connector/saveItems`（本地 /api/ 只读，无法写；连接器可写入
当前选中的库/分类，与浏览器插件同一机制）。写入后由 Better BibTeX 自动分配 citekey，
经其 json-rpc `item.search` 回查，实现「digest → 入库 → 自动拿 key」闭环。

依赖前提（运行时，非本模块职责）：
  - Zotero 桌面端开着，且「允许本机其他应用通信」已启用（连接器服务在 23119）。
  - 安装 Better BibTeX 插件（生成 citekey + 自动导出 references.json）。
无人值守 cron 下 Zotero 可能没开，故本步默认关闭、建议交互式运行（见 CLI 的 zotero 子命令）。

item 映射（paper_to_zotero_item）是纯函数，可脱离网络单测。
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

import httpx

from .schema import PaperMetadata, PaperSegment
from .fulltext import OAResult, resolve_oa_pdf, ipv4_client
from .crossref import enrich_metadata
from ..utils.logger import get_logger

logger = get_logger(__name__)


# ==================== 纯函数：作者名切分 + item 映射 ====================

def split_creator_name(name: str) -> Dict[str, str]:
    """把「Firstname M. Lastname」或「Lastname, Firstname」切成 Zotero creator。

    切不干净时退回单字段 name（Zotero 接受 {creatorType, name}），避免猜错姓氏。
    """
    n = (name or "").strip()
    if not n:
        return {"creatorType": "author", "name": ""}
    if "," in n:
        last, _, first = n.partition(",")
        return {"creatorType": "author", "firstName": first.strip(), "lastName": last.strip()}
    parts = n.split()
    if len(parts) == 1:
        return {"creatorType": "author", "lastName": parts[0]}
    return {"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}


def _normalize_doi(doi: Optional[str]) -> str:
    if not doi:
        return ""
    return doi.strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "").replace("doi:", "")


def paper_to_zotero_item(
    meta: PaperMetadata,
    abstract: str = "",
    oa: Optional[OAResult] = None,
    extra_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """把 PaperMetadata 映射为 Zotero 连接器 item（translator schema）。

    arXiv → preprint，其余 → journalArticle。OA PDF 作为 attachment 交给 Zotero 自抓。
    """
    is_preprint = bool(meta.arxiv_id) and not meta.doi
    item: Dict[str, Any] = {
        "itemType": "preprint" if is_preprint else "journalArticle",
        "title": meta.title or "",
        "creators": [split_creator_name(a) for a in (meta.authors or []) if a] or
                    [{"creatorType": "author", "name": "Unknown"}],
        "abstractNote": abstract or "",
        "url": meta.url or (f"https://doi.org/{meta.doi}" if meta.doi else ""),
    }

    if meta.publication_date:
        # Zotero 的 date 是自由文本，接受 "2026" / "2026-05" 这类部分日期——
        # 按真实精度写，别把占位的 1/1 当成确切出版日灌进库里（再经 BBT 回流会固化）。
        from ._citekey_utils import date_string
        item["date"] = date_string(meta.publication_date,
                                   getattr(meta, "date_precision", None))

    if is_preprint:
        item["repository"] = "arXiv"
        if meta.arxiv_id:
            item["archiveID"] = f"arXiv:{meta.arxiv_id}"
    else:
        if meta.journal:
            item["publicationTitle"] = meta.journal
        if meta.volume:
            item["volume"] = meta.volume
        if meta.issue:
            item["issue"] = meta.issue
        if meta.pages:
            item["pages"] = meta.pages
    if meta.doi:
        item["DOI"] = meta.doi

    # PMID / arXiv id 塞进 extra（Zotero 无独立字段，但 BBT/citeproc 会保留）
    extra_lines = []
    if meta.pmid:
        extra_lines.append(f"PMID: {meta.pmid}")
    if meta.arxiv_id and not is_preprint:
        extra_lines.append(f"arXiv: {meta.arxiv_id}")
    if extra_lines:
        item["extra"] = "\n".join(extra_lines)

    # 标签：来源 + 三态裁决 / 维度 / 危险信号（便于在 Zotero 里筛选）
    tags = list(extra_tags or [])
    if meta.source_type and meta.source_type != "unknown":
        tags.append(f"source:{meta.source_type}")
    item["tags"] = [{"tag": t} for t in tags if t]

    # OA PDF 作为附件：让 Zotero 自己下载（连接器 supportsAttachmentUpload）
    if oa and oa.pdf_url:
        item["attachments"] = [{
            "title": "OA Full Text PDF",
            "url": oa.pdf_url,
            "mimeType": "application/pdf",
            "snapshot": False,
        }]

    return item


def _augment_translator_item(ts_item: Dict[str, Any], seg: PaperSegment,
                             oa: Optional[OAResult] = None) -> Dict[str, Any]:
    """在 translation-server 解析出的权威 item 上补：我们的标签、OA 附件、缺失的摘要。"""
    item = dict(ts_item)
    # 标签：保留 TS 自带 + 追加 decision/来源
    tags = list(item.get("tags") or [])
    for t in decision_tags(seg):
        tags.append({"tag": t})
    if seg.metadata.source_type and seg.metadata.source_type != "unknown":
        tags.append({"tag": "source:{}".format(seg.metadata.source_type)})
    item["tags"] = tags
    # 摘要：TS 常无 abstractNote，用我们的补上
    if not item.get("abstractNote") and seg.original_abstract:
        item["abstractNote"] = seg.original_abstract
    # OA 附件让 Zotero 自抓（TS item 自身也可能带 attachments，追加不覆盖）
    if oa and oa.pdf_url:
        atts = list(item.get("attachments") or [])
        atts.append({"title": "OA Full Text PDF", "url": oa.pdf_url,
                     "mimeType": "application/pdf", "snapshot": False})
        item["attachments"] = atts
    return item


def decision_tags(seg: PaperSegment) -> List[str]:
    """从 filter_decision 生成 Zotero 标签（decision/维度/危险信号/角色）。"""
    fd = seg.filter_decision
    if not fd:
        return []
    tags: List[str] = []
    if fd.decision:
        tags.append(f"decision:{fd.decision}")
    for b in (fd.bucket or []):
        tags.append(f"bucket:{b}")
    for fl in (fd.flags or []):
        tags.append(f"flag:{fl}")
    if fd.role and fd.role != "NONE":
        tags.append(f"role:{fd.role}")
    return tags


# ==================== 连接器客户端 ====================

class ZoteroConnectorClient:
    """Zotero 连接器 + Better BibTeX json-rpc 的最小客户端。"""

    def __init__(self, base_url: str = "http://localhost:23119", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "XLBDTranslator/1.0",
                "X-Zotero-Connector-API-Version": "3",
                "Zotero-Allowed-Request": "true",
            },
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

    def ping(self) -> bool:
        """连接器是否在线（Zotero 已开 + 允许本机通信）。"""
        try:
            r = self._client.post(f"{self.base_url}/connector/ping", json={})
            return r.status_code == 200
        except Exception:
            return False

    def get_selected_collection(self) -> Optional[Dict[str, Any]]:
        """当前选中的库/分类（写入目标）。失败返回 None。"""
        try:
            r = self._client.post(f"{self.base_url}/connector/getSelectedCollection", json={})
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.warning("  ⚠️ 获取选中分类失败: {}".format(e))
        return None

    def save_items(self, items: List[Dict[str, Any]], uri: str = "") -> bool:
        """通过连接器写入 items 到当前选中分类。201 视为成功。"""
        if not items:
            return True
        payload = {
            "sessionID": uuid.uuid4().hex,
            "uri": uri or "https://github.com/xiaolibird/XLBDTranslator",
            "items": items,
        }
        try:
            r = self._client.post(f"{self.base_url}/connector/saveItems", json=payload)
            if r.status_code in (200, 201):
                return True
            logger.warning("  ⚠️ saveItems 返回 {}: {}".format(r.status_code, r.text[:200]))
            return False
        except Exception as e:
            logger.warning("  ⚠️ saveItems 失败: {}".format(e))
            return False

    def resolve_citekey(
        self,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        retries: int = 6,
        delay: float = 0.8,
    ) -> Optional[str]:
        """写入后回查 citekey：BBT item.search 按 DOI（优先）或标题匹配。

        BBT 异步生成 citekey，故带重试。匹配失败返回 None（上层退化为无引用键）。
        """
        norm_doi = _normalize_doi(doi)
        terms: List[str] = []
        # DOI/全标题在 BBT item.search 里常 0 命中（DOI 不入自由文本索引；长标题短语匹配易失败），
        # 但仍先试——命中则按 DOI 精确挑；随后补短前缀片段提升召回，再在结果里按 DOI/标题精确匹配。
        for t in (doi, title):
            if t and t not in terms:
                terms.append(t)
        if title:
            words = title.split()
            for k in (6, 4, 3):
                if len(words) > k:
                    frag = " ".join(words[:k])
                    if frag not in terms:
                        terms.append(frag)
        if not terms:
            return None
        for attempt in range(retries):
            for term in terms:
                key = self._bbt_search_pick(term, norm_doi, title)
                if key:
                    return key
            time.sleep(delay)
        return None

    def _bbt_search_pick(self, term: str, norm_doi: str, title: Optional[str]) -> Optional[str]:
        """单次 BBT 搜索并从结果里挑最匹配项的 citation-key。"""
        try:
            r = self._client.post(
                f"{self.base_url}/better-bibtex/json-rpc",
                json={"jsonrpc": "2.0", "method": "item.search", "params": [term], "id": 1},
            )
            if r.status_code != 200:
                return None
            results = r.json().get("result", []) or []
        except Exception:
            return None
        return pick_citekey(results, norm_doi, title)


def _norm_alnum(t: str) -> str:
    return "".join(ch.lower() for ch in (t or "") if ch.isalnum())


def pick_citekey(results: List[Dict[str, Any]], norm_doi: str, title: Optional[str]) -> Optional[str]:
    """从 BBT 搜索结果里挑与 DOI/标题最匹配项的 citation-key（纯函数，便于单测）。"""
    if not results:
        return None
    title_norm = (title or "").strip().lower()
    # 1) DOI 精确匹配
    if norm_doi:
        for it in results:
            if _normalize_doi(it.get("DOI")) == norm_doi:
                ck = it.get("citation-key") or it.get("citationKey")
                if ck:
                    return ck
    # 2) 标题匹配（先严格，再忽略大小写/标点/空白的规范化比对）
    if title_norm:
        tqn = _norm_alnum(title_norm)
        for it in results:
            rt = (it.get("title") or "").strip().lower()
            if rt == title_norm or (tqn and _norm_alnum(rt) == tqn):
                ck = it.get("citation-key") or it.get("citationKey")
                if ck:
                    return ck
    # 3) 唯一结果兜底——必须标题近似（一方规范化包含另一方）才采纳。
    #    片段检索（标题前 6/4/3 词）常唯一命中库里*另一篇*同前缀文献，
    #    盲取会把别人的 citekey 安给新论文（曾致两篇不同论文共用 dayan…2021 键）。
    if len(results) == 1:
        it = results[0]
        tqn = _norm_alnum(title or "")
        rtn = _norm_alnum(it.get("title") or "")
        if tqn and rtn and (tqn in rtn or rtn in tqn):
            return it.get("citation-key") or it.get("citationKey")
        return None
    return None


# ==================== 编排：入库 → citekey ====================

@dataclass
class SyncResult:
    """单篇论文的同步结果。"""
    paper_id: str
    saved: bool = False
    citekey: Optional[str] = None
    oa_status: str = "unknown"
    pdf_url: Optional[str] = None
    enriched: bool = False


def sync_segments_to_zotero(
    segments: List[PaperSegment],
    base_url: str = "http://localhost:23119",
    email: str = "",
    attach_pdf: bool = True,
    enrich_crossref: bool = True,
    require_collection: str = "",
    write_to_zotero: bool = True,
    translation_server_url: str = "",
    timeout: float = 30.0,
    client: Optional[ZoteroConnectorClient] = None,
) -> Dict[str, SyncResult]:
    """把入选论文写入 Zotero 并回查 citekey。

    流程：（可选）Crossref 元数据增强 → 逐篇解析 OA → 批量 saveItems 一次写入 → 逐篇回查 citekey。
    返回 paper_id -> SyncResult。Zotero 不可用（ping 失败）时返回空结果并 warning，不抛出。

    require_collection 非空时做防呆：当前选中分类必须与之同名，否则拒绝写入（避免落到库根/错分类）。
    """
    results: Dict[str, SyncResult] = {seg.paper_id: SyncResult(paper_id=seg.paper_id)
                                      for seg in segments}
    if not segments:
        return results

    own_client = client is None
    c = client or ZoteroConnectorClient(base_url=base_url, timeout=timeout)
    try:
        if not c.ping():
            logger.warning("⚠️ Zotero 连接器不可用（Zotero 未开或未允许本机通信），跳过入库")
            return results

        if write_to_zotero:
            coll = c.get_selected_collection()
            sel_name = coll.get("name") if coll else None
            is_root = bool(coll) and coll.get("name") == coll.get("libraryName")
            if coll:
                logger.info("  写入目标分类: {} / {}".format(
                    coll.get("libraryName", "?"), "(库根/Unfiled)" if is_root else sel_name))
            # 防呆：要求指定分类时，选中项必须匹配，否则拒绝写（不污染错误位置）
            if require_collection:
                if not coll or sel_name != require_collection:
                    logger.error("⛔ 当前选中「{}」，与要求的分类「{}」不符——请先在 Zotero 左栏选中「{}」再运行。未写入任何条目。".format(
                        sel_name or "(无)", require_collection, require_collection))
                    return results
            elif is_root:
                logger.warning("⚠️ 当前写入目标是库根（未选中具体分类），条目将落到 Unfiled Items。"
                               "如需写入某分类，请先在 Zotero 选中它，或用 --collection 指定。")
        else:
            logger.info("  notes-only 模式：跳过写库，仅（增强 +）解析 citekey 后生成札记")

        # 0) 元数据增强：Crossref 标题检索（规范作者+DOI，门槛防误配）；
        #    未命中的预印本用 arXiv id 精确补作者（arxiv_id 是精确标识，无误配风险）。
        if enrich_crossref:
            from .academic_search import enrich_from_arxiv
            cr_hit = ax_hit = 0
            with ipv4_client(timeout=timeout) as xc:
                for seg in segments:
                    hit = False
                    try:
                        hit = enrich_metadata(seg.metadata, email=email, client=xc)
                        if hit:
                            cr_hit += 1
                    except Exception as e:
                        logger.warning("  ⚠️ Crossref 增强失败（{}）: {}".format(seg.paper_id[:8], e))
                    if not hit and seg.metadata.arxiv_id:
                        try:
                            if enrich_from_arxiv(seg.metadata, client=xc):
                                ax_hit += 1
                                hit = True
                        except Exception as e:
                            logger.warning("  ⚠️ arXiv 增强失败（{}）: {}".format(seg.paper_id[:8], e))
                    if hit:
                        results[seg.paper_id].enriched = True
            logger.info("  元数据增强命中 {}/{} 篇（Crossref {} + arXiv {}）".format(
                cr_hit + ax_hit, len(segments), cr_hit, ax_hit))

        # 0.5) translation-server 权威解析：把标识符交给 Zotero 翻译器，回填 meta（令札记用权威数据）
        ts_items: Dict[str, Dict[str, Any]] = {}
        if translation_server_url:
            # 与 ingest.enrich_segments 同一入口：探活、并行、停机/0 命中告警都在 resolve_batch，
            # 别在这里再写一遍逐篇循环——那样 digest --zotero 这条路停机时又是静默的。
            from .translation_server import resolve_batch
            # 按位置作键（同 ingest.enrich_segments）：同批重复 paper_id 按 id 作键会折叠掉一篇
            resolved = resolve_batch({i: seg.metadata for i, seg in enumerate(segments)},
                                     translation_server_url)
            ts_items = {segments[i].paper_id: item for i, item in resolved.items() if item}

        # 1) + 2) 解析 OA + 构造 item + 一次性写入（notes-only 时跳过）
        if write_to_zotero:
            items: List[Dict[str, Any]] = []
            # 与上面 Crossref 增强阶段同一写法：整批共享连接，别对 Unpaywall 逐篇重建 TLS
            with ipv4_client(timeout=timeout) as xc:
                for seg in segments:
                    oa = None
                    if attach_pdf:
                        oa = resolve_oa_pdf(seg.metadata, email=email, client=xc)
                        results[seg.paper_id].oa_status = oa.oa_status
                        results[seg.paper_id].pdf_url = oa.pdf_url
                    ts_item = ts_items.get(seg.paper_id)
                    if ts_item:
                        # 权威 item：加我们的标签 + OA 附件 + 摘要
                        item = _augment_translator_item(ts_item, seg, oa)
                    else:
                        item = paper_to_zotero_item(
                            seg.metadata,
                            abstract=seg.original_abstract,
                            oa=oa,
                            extra_tags=decision_tags(seg),
                        )
                    items.append(item)

            saved = c.save_items(items)
            for seg in segments:
                results[seg.paper_id].saved = saved
            if not saved:
                logger.warning("⚠️ saveItems 未成功，citekey 解析可能落空")

        # 3) 逐篇回查 citekey（BBT 异步，首篇多等一会）
        resolved = 0
        for seg in segments:
            ck = c.resolve_citekey(doi=seg.metadata.doi, title=seg.metadata.title)
            results[seg.paper_id].citekey = ck
            if ck:
                resolved += 1
        logger.info("  citekey 解析成功 {}/{} 篇".format(resolved, len(segments)))
        return results
    finally:
        if own_client:
            c.close()
