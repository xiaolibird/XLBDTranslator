# -*- coding: utf-8 -*-
"""书籍精读的落盘层：章 bundle → 札记四件套 → 索引 → 向量库 → 概念页。

与 read_pdf._rebuild_month（手动论文链路）**平行**而不是复用：那一支焊死在
`manual/<month>/` 路径与 `科研札记_*_手动精读` 文件名上。门禁语义逐条对齐，
两边改动必须同步（各自留了交叉引用注释）。

三道 finalize 门（前两道抄自论文链路，第三道是书籍链路新增）：
  1. 有 cross_check_report          —— status=final 是 agent 自报的，无报告即未经核验
  2. 未显式自报 verified_count=0    —— 「一项都没核验」不许搭车入库
  3. 引文回验通过率 ≥ QUOTE_PASS_MIN —— 书籍精读的价值全在可引用的逐字引句上，
                                      回验不过的引句进了库就是带页码的假引用

专著 vs 编著的落盘差异（引用粒度不同，见 book_ingest.BookManifest 文档）：
  book    N 个章 bundle 合并成**一条**索引条目，章成为精读分节
  chapter 每章各成一条索引条目（各有 citekey / CSL type=chapter）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .book_ingest import BookManifest, Chapter, MANIFEST_NAME
from .schema import (CloseReading, CloseReadSection, CloseReadSentence,
                     DigestStatus, FilterDecision, PaperMetadata, PaperSegment)

logger = get_logger(__name__)

BOOKS_DIRNAME = "books"
CHAPTER_BUNDLE_SUFFIX = ".chapter.json"
QUOTECHECK_SUFFIX = ".quotecheck.json"
BUNDLE_SCHEMA_VERSION = 1

# 引文回验通过率门槛。不是 1.0：PDF 抽文本身会因分栏/公式/表格产生少量无法回验的
# 片段，卡死会让整章因排版问题进不了库。0.8 之下则说明引句大面积对不上原文——
# 那是内容问题，不是排版问题。
QUOTE_PASS_MIN = 0.8


def book_dir(notes_dir: Path, slug: str) -> Path:
    return Path(notes_dir) / BOOKS_DIRNAME / slug


def chapter_bundle_path(notes_dir: Path, slug: str, number: int) -> Path:
    return book_dir(notes_dir, slug) / "ch{:02d}{}".format(int(number), CHAPTER_BUNDLE_SUFFIX)


def quotecheck_path(notes_dir: Path, slug: str, number: int) -> Path:
    return book_dir(notes_dir, slug) / "ch{:02d}{}".format(int(number), QUOTECHECK_SUFFIX)


def note_label(manifest: BookManifest, date_prefix: str) -> str:
    """札记标签 YYYY-MM-DD-<BookSlug>（validate_note_label 已支持这个形状）。"""
    return "{}-{}".format(date_prefix, manifest.slug)


def note_stem(label: str) -> str:
    return "科研札记_{}_书籍精读".format(label)


# ---------------- 章 bundle ----------------

def write_chapter_bundle(path: Path, *, manifest: BookManifest, chapter: Chapter,
                         status: str = "draft",
                         close_reading_script: Optional[CloseReading] = None,
                         close_reading_final: Optional[dict] = None,
                         cross_check_report: Optional[dict] = None,
                         quote_verify: Optional[dict] = None,
                         chapter_meta: Optional[dict] = None,
                         draft_status: str = "ok", draft_note: str = "") -> Path:
    """落盘/更新一章的 bundle（形状与 pdf_ingest.write_bundle 同构，多带书籍字段）。"""
    from .notes_index import _atomic_write
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": status,
        "draft_status": draft_status,
        "draft_note": draft_note,
        "book_slug": manifest.slug,
        "pdf_path": manifest.pdf_path,
        "chapter": {"number": chapter.number, "title": chapter.title,
                    "label": chapter.label,
                    "pdf_start": chapter.pdf_start, "pdf_end": chapter.pdf_end,
                    "printed_start": chapter.printed_range(manifest.page_offset)[0],
                    "printed_end": chapter.printed_range(manifest.page_offset)[1],
                    "subsections": list(chapter.subsections)},
        "chapter_meta": chapter_meta or {},      # 编著文集：本章作者/标题（可引单元）
        "close_reading_script": (close_reading_script.model_dump(mode="json")
                                 if close_reading_script else None),
        "close_reading_final": close_reading_final,
        "cross_check_report": cross_check_report,
        "quote_verify": quote_verify,
    }
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    return path


def load_chapter_bundle(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _close_reading_from(data: Dict[str, Any]) -> Optional[CloseReading]:
    cr = data.get("close_reading_final")
    return CloseReading(**cr) if cr else None


# ---------------- 门禁 ----------------

@dataclass
class GateResult:
    ok: bool
    reason: str = ""


def check_gates(data: Dict[str, Any], *, quote_pass_min: float = QUOTE_PASS_MIN) -> GateResult:
    """三道 finalize 门（见模块文档）。返回不通过的原因。"""
    if data.get("status") != "final" or not data.get("close_reading_final"):
        return GateResult(False, "仍是草稿（status != final 或无 close_reading_final）")
    report = data.get("cross_check_report")
    if not report:
        return GateResult(False, "无 cross_check_report（status=final 系 agent 自报，未经亲读核验）")
    vc = report.get("verified_count") if isinstance(report, dict) else None
    if isinstance(vc, int) and vc < 1:
        return GateResult(False, "cross_check_report 自报 verified_count=0（一项都没核验）")
    qv = data.get("quote_verify")
    if not isinstance(qv, dict):
        return GateResult(False, "缺 quote_verify 报告（先跑 book_digest verify）")
    total = int(qv.get("total") or 0)
    rate = float(qv.get("pass_rate") or 0.0)
    if total and rate < quote_pass_min:
        return GateResult(False, "引文回验通过率 {:.0%} < {:.0%}（{} 条引句中 {} 条对不上原文）"
                          .format(rate, quote_pass_min, total, len(qv.get("flagged") or [])))
    return GateResult(True)


# ---------------- 章 bundle → PaperSegment ----------------

def _sections_from(cr: CloseReading, chapter_heading: str) -> List[CloseReadSection]:
    """给专著用：把一章的精读分节加上「【Ch.N · pp.a-b · 标题】」前缀。

    专著全书只有一条索引条目，若不加前缀，15 章的「方法与数据」会挤成 15 个同名分节，
    highlights 的 section 字段随之失去溯源能力（scholar-write 按 section 找不回是哪章）。
    """
    out = []
    for sec in cr.sections:
        out.append(CloseReadSection(
            heading="{} · {}".format(chapter_heading, sec.heading),
            sentences=list(sec.sentences)))
    return out


def _year_of(manifest: BookManifest):
    from datetime import date
    return date(manifest.year, 1, 1) if manifest.year else None


def segments_from_book(notes_dir: Path, manifest: BookManifest,
                       quote_pass_min: float = QUOTE_PASS_MIN
                       ) -> Tuple[List[PaperSegment], List[str], List[str]]:
    """全书 final 章 bundle → PaperSegment 列表。返回 (segments, skipped, broken)。"""
    bdir = book_dir(notes_dir, manifest.slug)
    files = sorted(bdir.glob("*{}".format(CHAPTER_BUNDLE_SUFFIX))) if bdir.exists() else []
    skipped: List[str] = []
    broken: List[str] = []
    accepted: List[Tuple[Dict[str, Any], CloseReading]] = []

    for bf in files:
        try:
            data = load_chapter_bundle(bf)
        except Exception as e:                       # noqa: BLE001
            # 读不出 = 这一章的亲读核验成果进不了库。必须留痕：只 continue 会让它
            # 既不在 skipped 也不在 papers 里，回执照打 ✅（同 read_pdf 的 broken_bundles）。
            logger.warning("  ⚠️ 读 bundle 失败 {}: {}".format(bf.name, e))
            broken.append(bf.name)
            continue
        gate = check_gates(data, quote_pass_min=quote_pass_min)
        if not gate.ok:
            logger.warning("  ⛔ {}：{}".format(bf.name, gate.reason))
            skipped.append(bf.name)
            continue
        try:
            cr = _close_reading_from(data)
        except Exception as e:                       # noqa: BLE001
            logger.error("  ⛔ bundle 结构非法，跳过 {}（{}: {}）"
                         .format(bf.name, type(e).__name__, str(e)[:300]))
            broken.append(bf.name)
            continue
        accepted.append((data, cr))

    if not accepted:
        return [], skipped, broken

    accepted.sort(key=lambda t: int((t[0].get("chapter") or {}).get("number") or 0))

    if manifest.entry_type == "book":
        return ([_monograph_segment(manifest, accepted)], skipped, broken)
    return ([_chapter_segment(manifest, d, cr) for d, cr in accepted], skipped, broken)


def _monograph_segment(manifest: BookManifest,
                       accepted: Sequence[Tuple[Dict[str, Any], CloseReading]]) -> PaperSegment:
    """专著：N 章合并成一条条目，章成为精读分节。"""
    sections: List[CloseReadSection] = []
    sources: List[str] = []
    for data, cr in accepted:
        ch = data.get("chapter") or {}
        heading = "Ch.{} · pp.{}-{} · {}".format(
            ch.get("number"), ch.get("printed_start"), ch.get("printed_end"),
            (ch.get("title") or "").strip())
        sections.extend(_sections_from(cr, heading))
        sources.append(str(ch.get("number")))

    meta = PaperMetadata(
        paper_id="book:{}".format(manifest.slug),
        title=manifest.title or manifest.slug,
        authors=list(manifest.authors),
        entry_type="book",
        journal=None,
        publication_date=_year_of(manifest),
        date_precision="year" if manifest.year else None,
        source_type="manual-book",
        **_common_meta_kwargs(manifest))
    # reading_depth 用既有的 'chunked'，不新造 'chapterwise'：那是与 AGENTS.md
    # 逐字绑定的四态量尺（schema.py 明写「不得有第二套定义」），而按章多次调用
    # 本就落在 chunked 的语义里。章节结构靠 series=book 与分节标题体现，不靠这个字段。
    cr_all = CloseReading(from_full_text=True, source="manual-book",
                          sections=sections, reading_depth="chunked")
    return _wrap_segment(meta, cr_all,
                         one_line="全书 {} 章精读（已读 {}）".format(
                             len(manifest.chapters), "/".join(sources)))


def _chapter_segment(manifest: BookManifest, data: Dict[str, Any],
                     cr: CloseReading) -> PaperSegment:
    """编著文集：一章一条条目（章作者、章标题、章页码范围）。"""
    ch = data.get("chapter") or {}
    cm = data.get("chapter_meta") or {}
    number = int(ch.get("number") or 0)
    title = (cm.get("title") or ch.get("title") or "").strip()
    meta = PaperMetadata(
        paper_id="book:{}:ch{:02d}".format(manifest.slug, number),
        title=title,
        authors=list(cm.get("authors") or []),
        entry_type="chapter",
        container_title=manifest.title or manifest.slug,
        chapter_number=number,
        page_range="{}-{}".format(ch.get("printed_start"), ch.get("printed_end")),
        book_key=manifest.citekey or None,
        publication_date=_year_of(manifest),
        date_precision="year" if manifest.year else None,
        source_type="manual-book",
        **_common_meta_kwargs(manifest))
    cr2 = CloseReading(from_full_text=True, source="manual-book",
                       sections=list(cr.sections), reading_depth="chunked")
    return _wrap_segment(meta, cr2, one_line=(cm.get("one_line") or "")[:300])


def _common_meta_kwargs(manifest: BookManifest) -> Dict[str, Any]:
    return {"isbn": manifest.isbn or None,
            "publisher": manifest.publisher or None,
            "edition": manifest.edition or None,
            "editors": list(manifest.editors)}


def _wrap_segment(meta: PaperMetadata, cr: CloseReading, one_line: str) -> PaperSegment:
    # stage 是 Literal 四态，没有 manual 档；论文链路（pdf_ingest.build_segment）同样
    # 拿 llm_judge 兜手动选入，这里对齐它而不是给 schema 加第五态。
    fd = FilterDecision(paper_id=meta.paper_id, title=meta.title, verdict="included",
                        stage="llm_judge", reason="手动选入书籍精读",
                        decision="INCLUDE", one_line=one_line,
                        bucket=[], role="CITE_SUPPORT", confidence=1.0, flags=[])
    return PaperSegment(segment_id=1, paper_id=meta.paper_id, metadata=meta,
                        priority_score=1.0, status=DigestStatus.COMPLETED,
                        filter_decision=fd, close_reading=cr)


# ---------------- 重建札记四件套 ----------------

def _archived_keys(notes_dir: Path, label: str):
    """上一轮 sidecar 已归档条目的 dedup_key 集合；不存在/读不出返回 None。

    None = 「不知道上一轮有什么」→ 调用方不做止损判断（同 read_pdf._archived_keys：
    拿空集当「上一轮什么都没有」会让止损闸恒不触发）。
    """
    from ._citekey_utils import recompute_entry_key
    sidecar = Path(notes_dir) / "{}.index.json".format(note_stem(label))
    if not sidecar.exists():
        return None
    try:
        rows = (json.loads(sidecar.read_text(encoding="utf-8")) or {}).get("papers") or []
    except Exception:                                 # noqa: BLE001
        return None
    return {recompute_entry_key(r) for r in rows if isinstance(r, dict) and r.get("citekey")}


def rebuild_book(notes_dir: Path, manifest: BookManifest, label: str,
                 settings=None, allow_removals: bool = False,
                 quote_pass_min: float = QUOTE_PASS_MIN) -> Dict[str, Any]:
    """从全书 final 章 bundle 重建书籍精读四件套 + 刷索引/向量库/概念页。

    净删除止损闸与 read_pdf._rebuild_month 同语义：有 bundle 被拒收时，先比对这一轮
    会不会净删掉上一轮已归档的条目；会就一字不动，让人先修那份 JSON。
    """
    from .notes import write_notes
    from .notes_index import update_index, write_outputs, existing_citekeys

    notes_dir = Path(notes_dir)
    segments, skipped, broken = segments_from_book(notes_dir, manifest,
                                                   quote_pass_min=quote_pass_min)
    stem = note_stem(label)
    own_note_file = stem + ".md"
    existing_ckeys = existing_citekeys(notes_dir / "literature_index.json",
                                       exclude_note_files={own_note_file})

    if not segments:
        logger.info("  {} 无通过门禁的章（草稿/拒收 {} 份）".format(manifest.slug, len(skipped)))
        idx = update_index(notes_dir)
        write_outputs(idx, notes_dir)
        return {"slug": manifest.slug, "papers": 0, "skipped": skipped,
                "broken": broken, "index": idx}

    if (broken or skipped) and not allow_removals:
        prev = _archived_keys(notes_dir, label)
        if prev:
            from ._citekey_utils import dedup_key_fields
            now = {dedup_key_fields(s.metadata.doi, s.metadata.arxiv_id, s.metadata.title,
                                    fallback=s.metadata.paper_id,
                                    url=s.metadata.url,
                                    isbn=s.metadata.isbn,
                                    chapter_number=s.metadata.chapter_number)
                   for s in segments}
            missing = prev - now
            if missing:
                logger.error(
                    "  ⛔ 本轮重建会从 {} 的札记里**净删除 {} 条已归档条目**"
                    "（有 {} 份 bundle 被拒收）。整本一字未动；修好后重跑，"
                    "确要删除加 --allow-removals。"
                    .format(manifest.slug, len(missing), len(broken) + len(skipped)))
                idx = update_index(notes_dir)
                return {"slug": manifest.slug, "papers": len(segments), "refused": True,
                        "removed_keys": sorted(missing), "skipped": skipped,
                        "broken": broken, "index": idx}

    citekeys = _plan_citekeys(notes_dir, manifest, segments, label)
    proc = getattr(settings, "processing", None)
    res = write_notes(
        segments, citekeys, out_dir=notes_dir,
        instruction=getattr(proc, "notes_instruction", "") if proc else "",
        digest_title="科研札记 · {}（书籍精读）".format(manifest.title or manifest.slug),
        filename=stem,
        emit_docx=bool(getattr(proc, "notes_emit_docx", False)) if proc else False,
        cjk_font=getattr(proc, "notes_docx_cjk_font", "") if proc else "",
        fallback_citekeys=True, index_series="book",
        existing_citekeys=existing_ckeys,
        explicit_citekey_source="fallback")
    idx = update_index(notes_dir)
    write_outputs(idx, notes_dir)
    return {"slug": manifest.slug, "papers": len(segments), "skipped": skipped,
            "broken": broken, "md": res["note_path"], "docx": res.get("docx_path"),
            "index": idx}


def _plan_citekeys(notes_dir: Path, manifest: BookManifest,
                   segments: Sequence[PaperSegment], label: str) -> Dict[str, Optional[str]]:
    """决定每个 segment 的 citekey。

    专著用 manifest.citekey（人给的稳定键，如 little2020rubin）；编著各章走 write_notes
    的兜底生成（章作者姓+年+标题实词 → guyatt2015Harm）。沿用上一轮键的逻辑由
    write_notes + existing_citekeys 负责，这里只钉专著那一个。
    """
    keys: Dict[str, Optional[str]] = {}
    for seg in segments:
        if manifest.entry_type == "book" and manifest.citekey:
            keys[seg.paper_id] = manifest.citekey
        else:
            keys[seg.paper_id] = None
    return keys
