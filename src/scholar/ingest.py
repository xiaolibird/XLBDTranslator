# -*- coding: utf-8 -*-
"""按需/按周入库：把已判过的论文接上「精读 → 札记」这最后一段。

背景：周报（digest）判完就把结果丢在 `output/scholar_digest/*.json`，从不写札记——
因为 `write_notes()` 埋在 `workflow._step_sync_zotero()` 里，被 `zotero_enabled`
一起关掉了（无人值守跑不了 Zotero 桌面端）。本模块把那最后一段接回来，并且
**直接复用 digest JSON 里已有的 filter-v3 裁决，不重跑 LLM 筛选**。

三条入口共用同一条管线（收集 segments → 跨库去重 → 元数据增强 → citekey →
top-N 全文精读 → write_notes）：
  1. digest 复用：`load_digest_segments()`  ← 周一自动跑，零额外 LLM 筛选成本
  2. 任意标识符：`segments_from_identifiers()`（DOI / arXiv id / 标题）
  3. 本地 PDF：走 `scripts/read_pdf.py`（三段式 agent 交叉核验，另一条链路）

产物是**周札记** `科研札记_YYYY-MM-DD_全文精读.md`（周一日期）。命名规范已被
`notes_index.NOTE_MD_RE` 接受（手动精读系列在用同款日期后缀），vault 的
`month_key()` 会把同月的周文件折进一个 `2026-07` 月度页，两边都不用改。
"""
import concurrent.futures
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import json
import re

from .schema import (DigestOutput, DigestStatus, FilterDecision, PaperField,
                     PaperMetadata, PaperSegment, ScholarSettings)
from ..utils.logger import get_logger

logger = get_logger("ingest")

DIGEST_RE = re.compile(r"^digest_(\d{8})_(\d{6})\.json$")
ARXIV_RE = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$", re.I)
DOI_RE = re.compile(r"(10\.\d{4,9}/\S+)", re.I)


# ---------------------------------------------------------------- 周历

def week_monday(d: Optional[date] = None) -> date:
    """所属自然周的周一。周札记用它当 label，同一周内多次跑落在同一个文件上。"""
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def week_label(d: Optional[date] = None) -> str:
    return week_monday(d).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 元数据增强（原在 backfill_notes.py）

def dedup_key(meta: PaperMetadata) -> str:
    """全局去重键（权威实现在 notes_index，与索引同源同规则）。"""
    from ._citekey_utils import dedup_key_fields
    return dedup_key_fields(meta.doi, meta.arxiv_id, meta.title, fallback=meta.paper_id,
                            url=getattr(meta, "url", None))


def enrich_segments(segs: Sequence[PaperSegment], email: str,
                    ts_url: str, max_workers: int = 4) -> Tuple[int, int, int]:
    """Crossref 标题检索补 DOI/作者 + arXiv id 补作者 + PubMed 补摘要 + translation-server 权威解析。

    4-worker 分片并行（同 resolve_citekeys 的模板）：每篇 1-2 次 Crossref 往返全串行时，
    周批 30-50 篇要纯等 1-3 分钟、按月回填/作者语料批（99 篇）5-10 分钟级。每个 worker
    持独立 client、只动自己分片里的 seg（各 seg 互不依赖），计数按分片返回后求和。
    """
    from .crossref import enrich_metadata
    from .academic_search import enrich_from_arxiv, enrich_abstract_from_pubmed
    from .fulltext import ipv4_client
    from .translation_server import resolve_and_apply

    seg_list = list(segs)
    if not seg_list:
        return 0, 0, 0

    def _enrich_batch(batch) -> Tuple[int, int, int]:
        cr = ax = pm = 0
        with ipv4_client(timeout=30) as xc:
            for seg in batch:
                hit = False
                try:
                    hit = enrich_metadata(seg.metadata, email=email, client=xc)
                    if hit:
                        cr += 1
                except Exception as e:
                    logger.debug("Crossref enrich 失败 [{}]: {}", seg.paper_id, e)
                if not hit and seg.metadata.arxiv_id:
                    try:
                        if enrich_from_arxiv(seg.metadata, client=xc):
                            ax += 1
                    except Exception as e:
                        logger.debug("arXiv enrich 失败 [{}]: {}", seg.paper_id, e)
                # 无摘要的论文若拿不到 OA 全文，精读会因空正文整篇落空——先去 PubMed 补一份
                if not (seg.original_abstract or "").strip():
                    try:
                        abs_ = enrich_abstract_from_pubmed(
                            seg.metadata, seg.original_abstract, client=xc, email=email)
                        if abs_:
                            seg.original_abstract = abs_
                            pm += 1
                    except Exception as e:
                        logger.debug("PubMed 补摘要失败 [{}]: {}", seg.paper_id, e)
        return cr, ax, pm

    workers = min(max_workers, len(seg_list))
    chunk_size = max(1, len(seg_list) // workers)
    chunks = [seg_list[i:i + chunk_size] for i in range(0, len(seg_list), chunk_size)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_enrich_batch, chunks))
    cr = sum(r[0] for r in results)
    ax = sum(r[1] for r in results)
    pm = sum(r[2] for r in results)
    if pm:
        logger.info("  PubMed 补摘要：{} 篇（原本无摘要，避免精读空转）".format(pm))

    ts = 0
    if ts_url:
        def _ts_batch(batch) -> int:
            n = 0
            for seg in batch:
                try:
                    if resolve_and_apply(seg.metadata, base_url=ts_url):
                        n += 1
                except Exception as e:
                    logger.debug("translation-server 解析失败 [{}]: {}", seg.paper_id, e)
            return n
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            ts = sum(executor.map(_ts_batch, chunks))
    return cr, ax, ts


def resolve_citekeys(segs: Sequence[PaperSegment],
                     base_url: str,
                     max_workers: int = 4) -> Dict[str, Optional[str]]:
    """尽力回查 BBT citekey；Zotero 未开/拿不到则返回 None（札记用兜底键，自包含可渲染）。"""
    citekeys: Dict[str, Optional[str]] = {seg.paper_id: None for seg in segs}
    if not segs:
        return citekeys
    try:
        from .zotero_sync import ZoteroConnectorClient
        # 先探活一次，避免每个 worker 都去 ping 失败
        cli = ZoteroConnectorClient(base_url=base_url)
        if not cli.ping():
            logger.info("  Zotero 未开，citekey 全用兜底键")
            cli.close()
            return citekeys
        cli.close()

        def _resolve_batch(batch):
            """单 worker 内串行解析一批 segment（每个 worker 持有独立 client）。"""
            cli2 = ZoteroConnectorClient(base_url=base_url)
            try:
                for seg in batch:
                    m = seg.metadata
                    try:
                        citekeys[seg.paper_id] = cli2.resolve_citekey(
                            doi=m.doi, title=m.title, retries=2, delay=0.5)
                    except Exception as e:
                        logger.debug("citekey 回查失败 [{}]: {}", seg.paper_id, e)
            finally:
                cli2.close()

        workers = min(max_workers, len(segs))
        seg_list = list(segs)
        chunk_size = max(1, len(seg_list) // workers)
        chunks = [seg_list[i:i + chunk_size] for i in range(0, len(seg_list), chunk_size)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(_resolve_batch, chunks))
    except Exception as e:
        logger.warning("  citekey 回查异常（全用兜底）: {}".format(e))
    return citekeys


# ---------------------------------------------------------------- 入口 1：复用 digest 裁决

def digest_runs(digest_dir: Path) -> List[Tuple[datetime, Path]]:
    """列出 digest 产物（(运行时刻, 路径)，按时刻升序）。排除 _excluded/_stats 旁挂文件。"""
    out: List[Tuple[datetime, Path]] = []
    for p in sorted(Path(digest_dir).glob("digest_*.json")):
        m = DIGEST_RE.match(p.name)
        if not m:
            continue                      # digest_xxx_excluded.json / _stats.json
        try:
            out.append((datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"), p))
        except ValueError:
            continue
    out.sort()
    return out


def load_digest_segments(digest_dir: Path,
                         since: Optional[date] = None,
                         until: Optional[date] = None) -> List[PaperSegment]:
    """取 [since, until] 内 digest run 的入选论文，跨 run 按 dedup_key 去重。

    同一周常有多次 run（手工重跑），且相邻周的 8 天窗口有 1 天重叠，必然产生重复。
    保留**最早**出现的那条：它的 filter_decision 时间戳最贴近首次判定，且后续 run
    的裁决内容一致（同一 prompt 版本、同一篇论文）。
    """
    picked: Dict[str, PaperSegment] = {}
    undecided = 0
    for ts, path in digest_runs(digest_dir):
        d = ts.date()
        if since and d < since:
            continue
        if until and d > until:
            continue
        try:
            out = DigestOutput.load_from_file(path)
        except Exception as e:
            logger.warning("  ⚠️ digest 读取失败，跳过 {}: {}".format(path.name, e))
            continue
        for seg in out.segments:
            fd = seg.filter_decision
            # 必须带裁决才收：目录里躺着 4700+ 条早期无裁决产物（当时还没记录 filter_decision），
            # 那些是**未经筛选**的原始抽取结果，放进来等于把 Scholar 告警原封不动倒进札记库。
            if fd is None:
                undecided += 1
                continue
            if fd.verdict != "included":
                continue
            picked.setdefault(dedup_key(seg.metadata), seg)
    if undecided:
        logger.info("  跳过 {} 条无 filter 裁决的早期 digest 产物（未经筛选，不入库）".format(undecided))
    segs = list(picked.values())
    segs.sort(key=lambda s: -(s.priority_score or 0))
    return segs


# ---------------------------------------------------------------- 入口 2：任意标识符列表

def parse_identifiers(text: str) -> List[str]:
    """一行一个标识符；`#` 起头与空行忽略。行内 `#` 不当注释（DOI 里合法）。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _seg_from_norm(item: Dict[str, Any], sid: int, source: str,
                   abstract: str = "") -> PaperSegment:
    from .academic_search import _generate_paper_id
    authors = item.get("authors") or []
    meta = PaperMetadata(
        paper_id=_generate_paper_id(item.get("title", ""), authors),
        title=item.get("title", ""),
        authors=authors,
        publication_date=item.get("publication_date") or item.get("published"),
        date_precision=item.get("date_precision"),
        journal=item.get("journal"),
        doi=item.get("doi"),
        arxiv_id=item.get("arxiv_id"),
        pmid=item.get("pmid"),
        url=item.get("url"),
        field=PaperField.OTHER,
        source_type=source,
    )
    return PaperSegment(segment_id=sid, paper_id=meta.paper_id,
                        original_abstract=abstract or item.get("abstract", "") or "",
                        metadata=meta, status=DigestStatus.PENDING)


def segments_from_identifiers(ids: Sequence[str], email: str = "") -> List[PaperSegment]:
    """DOI / arXiv id / 裸标题 → PaperSegment。解析不出的条目跳过并告警，不中断整批。

    Crossref 基本不给摘要，所以这条路进来的论文摘要多为空——筛选凭标题判，
    真正的内容在后面的全文精读里补。
    """
    from .crossref import crossref_by_doi, crossref_lookup
    from .academic_search import fetch_arxiv_by_id
    from .fulltext import ipv4_client

    segs: List[PaperSegment] = []
    with ipv4_client(timeout=30) as xc:
        for i, raw in enumerate(ids, 1):
            s = raw.strip()
            am = ARXIV_RE.match(s) or ARXIV_RE.match(
                s.rsplit("/", 1)[-1] if "arxiv.org" in s.lower() else "")
            try:
                if am:
                    it = fetch_arxiv_by_id(am.group(1), xc)
                    if it:
                        seg = _seg_from_norm(it, i, "arxiv", it.get("abstract", ""))
                        seg.metadata.arxiv_id = am.group(1)
                        segs.append(seg)
                        continue
                dm = DOI_RE.search(s)
                if dm:
                    it = crossref_by_doi(dm.group(1), email=email, client=xc)
                    if it:
                        segs.append(_seg_from_norm(it, i, "crossref"))
                        continue
                if not am and not dm:                       # 当成标题模糊检索
                    it = crossref_lookup(s, email=email, client=xc)
                    if it:
                        segs.append(_seg_from_norm(it, i, "crossref"))
                        continue
            except Exception as e:
                logger.warning("  ⚠️ 解析失败 [{}]: {}".format(s, e))
                continue
            logger.warning("  ⚠️ 查不到元数据，跳过：{}".format(s))
    return segs


def classify_segments(segs: Sequence[PaperSegment], settings: ScholarSettings,
                      force_include: bool = True) -> None:
    """就地给 segments 打 filter-v3 裁决（bucket/role/flags/one_line）。

    force_include：手选的论文即便被判 EXCLUDE 也强行入库——用户已经用行动表态了。
    但**保留** LLM 给的 bucket/role/flags，否则这些论文在 vault 里全落进「未分维度」。
    """
    from .workflow import ScholarWorkflow
    wf = ScholarWorkflow(settings)
    wf.segments = list(segs)
    try:
        wf._step_filter_papers()          # 裁决就地写进每个 seg.filter_decision（含被排除的）
    finally:
        wf.close_rag_resources()
    if not force_include:
        return
    for seg in segs:
        fd = seg.filter_decision
        if fd is None:
            seg.filter_decision = FilterDecision(
                paper_id=seg.paper_id, title=seg.metadata.title, verdict="included",
                decision="INCLUDE", stage="manual_pick", reason="手动指定入库",
                one_line="手动指定入库")
        elif fd.verdict != "included":
            logger.info("  手选覆盖裁决：{} 原判 {} → INCLUDE".format(
                (seg.metadata.title or "")[:48], fd.decision))
            fd.verdict, fd.decision = "included", "INCLUDE"
            fd.stage = "manual_pick"
            fd.exclude_reason = None


# ---------------------------------------------------------------- 共用管线

def _existing_note_dedup_keys(out_dir: Path, filename: str) -> Optional[Set[str]]:
    """读同名周札记的 sidecar 索引，取其已收录论文的 dedup_key 全集（按**当前**键梯重算）。

    重算而非直读 papers[].dedup_key：sidecar 是 write_notes 当时写下的快照，键也被冻在
    那一刻。键梯一升级（2026-08-15 加 pmlr:/openreview: 层），调用方那侧 `dedup_key(seg.metadata)`
    走新规则、这侧却还是旧键，同一篇论文两边拿到两个键 → existing - new 恒非空 →
    同 label 重跑被误报「含 N 篇本批未覆盖的论文，拒绝写入」，提示换 --label，而换 label
    会真的产出重复札记。实测该状态在库内已存在 43 条。重算逻辑与 notes_index.build_month_entries
    共用 _citekey_utils.recompute_entry_key，杜绝两处再次漂移。

    write_notes() 是 `open(path, "w")` 整篇重写：同一 label 第二次跑若 segs 里
    不含上一批论文（跨库去重会把它们过滤掉——它们已经"在库里"），整篇覆盖会
    把上一批已入库论文从 md/references/sidecar 里连根抹掉。这里在落盘前探一
    次既有内容，供调用方判断本批是否会造成数据丢失。

    Returns:
        None：同名 md 不存在，可放心写。
        非空 set：同名 md 存在，返回其已收录论文的 dedup_key 集合（理论上不会是
                空集——run_ingest 对 0 篇 segs 提前 return，从不会写出零条目的
                札记）。

    Raises:
        Exception：sidecar 缺失/损坏，无法确认既有内容。故意不吞掉——调用方须
                   把"读不出既有内容"当成"可能会覆盖丢弃"同等保守处理，不能
                   静默当作"没有既有内容"直接放行覆盖。
    """
    from .notes import _slug
    from ._citekey_utils import recompute_entry_key
    slug = _slug(filename, 80) or "scholar_digest"
    note_path = out_dir / "{}.md".format(slug)
    if not note_path.exists():
        return None
    sidecar_path = out_dir / "{}.index.json".format(slug)
    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    # 过滤条件与重算前一致（只认本来就落了键的条目），改的只是「拿哪一版键」
    return {recompute_entry_key(e) for e in data.get("papers", [])
            if isinstance(e, dict) and e.get("dedup_key")}


def _rehydrate_close_readings(out_dir: Path, filename: str,
                              segs: Sequence[PaperSegment]) -> int:
    """同 label 重跑时，把既有周札记 md 里的精读节回读进本批 segs（就地补 close_reading）。

    为什么必须回读：segs 来自 Step 4 固化的 digest JSON（无人值守周流程
    zotero_enabled=false，精读被关在 workflow 里），close_reading 恒为 None；
    合并批通过整篇覆盖守卫后，close_read_segments 只按优先级重挑 top-N，
    上一批已精读的论文一旦被新论文挤出，就带着 None 走到 write_notes 的
    `open(path, "w")` 整篇重写——精读节/highlights/tag_counts 从 md/sidecar
    静默消失、还打 ✅ 报成功，随后向量库增量同步把它的 chunk 也删掉。
    守卫报错文案推荐的补救路径（--auto 全选合并重跑）恰恰就是这条触发链。

    解析复用 notes_index 的同一组行首正则（md 渲染 notes._paper_section 与
    parse_note_md 之间的既有格式契约），勿在这里另写第二套。heading/句子/
    句级 tag 在 md 里无损往返；model/read_at/body_chars 等量尺字段 md 没存、
    回读后为默认值——比起整节丢失这是可接受的损耗。

    只补 close_reading 为 None 的 seg：本轮真跑出的新精读优先于旧文。
    Returns: 回读成功的篇数（同名 md 不存在时为 0）。
    """
    from .notes import _slug
    from ._citekey_utils import recompute_entry_key
    from .notes_index import (_CLOSEREAD_RE, _CR_SECTION_RE, _SECTION_RE,
                              _TAG_LINE_RE)
    from .schema import CloseReading, CloseReadSection, CloseReadSentence
    slug = _slug(filename, 80) or "scholar_digest"
    note_path = out_dir / "{}.md".format(slug)
    if not note_path.exists():
        return 0
    # citekey → 当前键：与覆盖守卫共用 recompute_entry_key，防键梯升级后两侧漂移
    sidecar = json.loads((out_dir / "{}.index.json".format(slug))
                         .read_text(encoding="utf-8"))
    ck2dk = {e["citekey"]: recompute_entry_key(e)
             for e in sidecar.get("papers", [])
             if isinstance(e, dict) and e.get("citekey")}
    # md → 按 citekey 收集精读结构（行首锚定，与 parse_note_md 同一状态机走法）
    readings: Dict[str, CloseReading] = {}
    cur_ck: Optional[str] = None
    cur_cr: Optional[CloseReading] = None
    cur_sec: Optional[CloseReadSection] = None
    for line in note_path.read_text(encoding="utf-8").splitlines():
        m = _SECTION_RE.match(line)
        if m:
            cur_ck, cur_cr, cur_sec = m.group(4), None, None
            continue
        if line.startswith("# "):          # 参考文献等一级节，论文区结束
            cur_ck, cur_cr, cur_sec = None, None, None
            continue
        if cur_ck is None:
            continue
        crm = _CLOSEREAD_RE.match(line)
        if crm:
            cur_cr = CloseReading(from_full_text=(crm.group(1) == "全文精读"),
                                  source=crm.group(2), sections=[])
            readings[cur_ck] = cur_cr
            cur_sec = None
            continue
        if line.startswith("### "):        # 精读之后的其他三级小节，精读区结束
            cur_cr, cur_sec = None, None
            continue
        if cur_cr is None:
            continue
        sm = _CR_SECTION_RE.match(line)
        if sm:
            cur_sec = CloseReadSection(heading=sm.group(1).strip(), sentences=[])
            cur_cr.sections.append(cur_sec)
            continue
        if cur_sec is not None and line.startswith("- "):
            tm = _TAG_LINE_RE.match(line)
            if tm:
                cur_sec.sentences.append(
                    CloseReadSentence(text=tm.group(2).strip(), tag=tm.group(1)))
            else:
                cur_sec.sentences.append(CloseReadSentence(text=line[2:].strip()))
    dk2cr = {ck2dk[ck]: cr for ck, cr in readings.items()
             if ck in ck2dk and cr.sections}
    restored = 0
    for seg in segs:
        if seg.close_reading is None:
            cr = dk2cr.get(dedup_key(seg.metadata))
            if cr is not None:
                seg.close_reading = cr
                restored += 1
    return restored


def run_ingest(segs: Sequence[PaperSegment], settings: ScholarSettings, label: str,
               top_n: int = 5, close_read: bool = True,
               seen: Optional[Set[str]] = None,
               title_suffix: str = "（全文精读）") -> Dict[str, Any]:
    """增强 → citekey → top-N 全文精读 → 写周札记。返回落盘摘要。"""
    proc = settings.processing
    pre_keys: Set[str] = set()
    if seen is not None:
        fresh, dropped = [], 0
        for seg in segs:
            k = dedup_key(seg.metadata)
            if k in seen:
                dropped += 1
                continue
            seen.add(k)
            pre_keys.add(k)     # 本批自己刚加进去的，增强后那一轮不能再拿它当"库里已有"
            fresh.append(seg)
        if dropped:
            logger.info("  跨库去重：{} 篇已在札记库中，跳过".format(dropped))
        segs = fresh
    if not segs:
        return {"label": label, "status": "empty", "count": 0}

    email = proc.zotero_email or proc.external_email or ""
    cr, ax, ts = enrich_segments(segs, email, proc.zotero_translation_server_url)

    # 增强后再去一次重：digest JSON 是 Step 4 固化的，此时多数条目还没有 DOI，
    # dedup_key 只能退到标题；Crossref 补上 DOI 后键会变，可能撞上库里已有的 DOI 键。
    if seen is not None:
        kept, late = [], 0
        for seg in segs:
            k = dedup_key(seg.metadata)
            if k in seen and k not in pre_keys:
                late += 1
                continue
            seen.add(k)
            kept.append(seg)
        if late:
            logger.info("  增强后补去重：{} 篇补到 DOI 后发现库里已有".format(late))
        segs = kept
        if not segs:
            return {"label": label, "status": "empty", "count": 0}

    citekeys = resolve_citekeys(segs, proc.zotero_base_url)

    from .notes import write_notes
    from .notes_index import existing_citekeys
    notes_out_dir = Path(proc.notes_dir)
    note_filename = "科研札记_{}_全文精读".format(label)
    # 排除本次要重写的周札记自己的旧条目——否则本批重算出同样的兜底键会被判「库内
    # 已占用」而加消歧后缀，下一轮又因为后缀键才是「已占用」而改回原键，来回改名
    # （citekey 抖动，同 backfill_notes.run_month / read_pdf._rebuild_month 同源坑）。
    # 正常路径已被 _existing_note_dedup_keys 的整篇覆盖检查挡住，但 --no-dedup 或
    # 本批覆盖既有全集时仍会走到这里，防御一下不额外增加成本。
    idx_path = Path(proc.notes_dir) / "literature_index.json"
    existing_ckeys = existing_citekeys(
        idx_path, exclude_note_files={"{}.md".format(note_filename)})
    try:
        existing_dk = _existing_note_dedup_keys(notes_out_dir, note_filename)
    except Exception as e:
        raise RuntimeError(
            "周札记 {} 已存在但索引 sidecar 无法读取（{}），无法确认既有内容是否会被"
            "整篇覆盖丢弃，拒绝写入。请改用不同的 --label 重跑，或先修复/补齐 sidecar 后再试。"
            .format(note_filename, e))
    if existing_dk is not None:
        new_dk = {dedup_key(seg.metadata) for seg in segs}
        missing_dk = existing_dk - new_dk
        if missing_dk:
            # write_notes 会用本批 segs 整篇覆盖同名 md/references/sidecar；本批不
            # 含既有文件里的全部论文，直接写会把 missing_dk 那些论文从库里抹掉
            # （连带它们的 citekey/向量/vault 笔记）。宁可拒绝，让调用方换个
            # --label（或先把上一批一起传进来）。
            raise RuntimeError(
                "周札记 {} 已存在且含 {} 篇本批未覆盖的论文（[@key] 与索引会被整篇覆盖丢弃）："
                "拒绝写入以避免数据丢失。请把上一批论文与本批一起传入（ingest_notes 已把"
                "本周文件的键剔出去重集，它们就在候选列表里，--auto 或 --pick 全选上即可）；"
                "改用不同的 --label 会另起一篇新札记、造成同周重复，仅在确要分篇时使用。"
                .format(note_filename, len(missing_dk)))
        # 合并批通过守卫≠安全：write_notes 仍是整篇覆盖，先把既有精读回读进内存，
        # 否则上一批被 top-N 重挑挤出的论文精读会被静默抹掉（见函数 docstring）。
        restored = _rehydrate_close_readings(notes_out_dir, note_filename, segs)
        if restored:
            logger.info("  精读回读：{} 篇沿用既有周札记的精读节".format(restored))

    full_text = 0
    if close_read:
        from .closereading import close_read_segments
        from .llm_client import LLMClient
        # 只对尚无精读的论文重挑 top-N：已回读的旧精读不重跑（省 LLM 成本），
        # 更关键的是不让上一批高分论文再次占满名额——合并重跑的诉求本来就是给新论文补精读
        pending = [s for s in segs if s.close_reading is None]
        llm_client = LLMClient(settings.llm)
        try:
            done = close_read_segments(
                pending, proc.research_interests, llm_client,
                top_n=top_n, email=email,
                model=(settings.llm.closeread_model or settings.llm.model),
                scratch_dir=Path("output/scholar_pdfs"),
                deep=proc.closeread_deep, max_chars=proc.closeread_max_chars,
                max_chunks=proc.closeread_max_chunks)
        finally:
            llm_client.close()
        if top_n > 0 and pending and done == 0:
            # 0/N 成功几乎只出现在 claude-agent 限流/欠费这类通路级故障；照常写盘会让
            # 降级札记被索引 seen 去重永久固化（周度没有 --force 入口）。宁可本批不写：
            # digest JSON 还在，进程非零退出后重跑即恢复。done≥1 的部分成功照常写盘。
            # 门控按 pending 计：全批都靠回读补齐（pending 为空）时 done=0 是正常态。
            raise RuntimeError("全文精读 0/{}，视为 LLM 故障批，不写终稿".format(
                min(top_n, len(pending))))
        full_text = sum(1 for s in segs if s.close_reading and s.close_reading.from_full_text)

    res = write_notes(
        list(segs), citekeys, out_dir=notes_out_dir,
        instruction=proc.notes_instruction,
        digest_title="科研札记 · {}{}".format(label, title_suffix),
        filename=note_filename,
        emit_docx=proc.notes_emit_docx, cjk_font=proc.notes_docx_cjk_font,
        fallback_citekeys=True, existing_citekeys=existing_ckeys)

    hit_ck = sum(1 for v in citekeys.values() if v)
    logger.info("  ✅ {} → {} 篇 | citekey {}/{} | 全文精读 {} | 增强 CR{}/AX{}/TS{}".format(
        label, len(segs), hit_ck, len(segs), full_text, cr, ax, ts))
    return {"label": label, "status": "ok", "count": len(segs), "citekey": hit_ck,
            "full_text": full_text, "md": res["note_path"], "docx": res.get("docx_path")}


def describe(segs: Sequence[PaperSegment]) -> List[str]:
    """`--list` 的人读表格：序号与 `--pick` 一一对应。"""
    lines = []
    for i, seg in enumerate(segs, 1):
        fd = seg.filter_decision
        bucket = "/".join((fd.bucket or []) if fd else []) or "-"
        flags = ",".join((fd.flags or []) if fd else [])
        tag = "{}{}".format(bucket, "!" + flags if flags else "")
        lines.append("  {:>3}. [{:<10}] {:<62} {}".format(
            i, tag, (seg.metadata.title or "?")[:62],
            (fd.one_line if fd else "") or ""))
    return lines
