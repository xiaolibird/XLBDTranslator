# -*- coding: utf-8 -*-
"""
citekey 与索引条目共享工具模块。

将 notes.py 与 notes_index.py 的共同实现（citekey 回退、消歧后缀序列、
优先级分级、entry_from_segment）收敛到单一模块，打破二者之间的循环导入。
"""
from itertools import product
from typing import Dict, List, Any, Optional

_STOP_WORDS = {"the", "a", "an", "of", "for", "and", "with", "using", "based",
               "from", "into", "via", "toward", "towards", "study", "novel"}

_TIER_MAP = {"🔴 高": "high", "🔴": "high", "🟠 中": "mid", "🟢 低": "low"}


# ---------------- citekey 生成与消歧 ----------------

def _suffix_seq(max_len: int = 3):
    """消歧后缀序列：b…z、bb…zz、bbb…（仿 BBT）。

    恒为纯小写字母——曾用 chr(ord('b')+n) 递增，'z' 之后落到 '{' '|'，
    而 pandoc 会在这些字符处截断 citekey，把引用**静默指到基键那篇论文**。
    """
    letters = [chr(c) for c in range(ord("b"), ord("z") + 1)]
    for n in range(1, max_len + 1):
        for combo in product(letters, repeat=n):
            yield "".join(combo)


def _fallback_citekey(meta) -> str:
    """headless 回填无 Zotero key 时，生成人读的临时引用键（作者姓+年+标题实词，仿 BBT 动态键）。"""
    author = ""
    if meta.authors:
        first = (meta.authors[0] or "").strip()
        surname = first.split(",")[0].split()[-1] if first else ""
        author = "".join(c for c in surname if c.isalpha()).lower()
    year = ""
    if getattr(meta, "publication_date", None):
        year = str(meta.publication_date.year)
    elif getattr(meta, "email_received_at", None):
        year = str(meta.email_received_at.year)
    word = ""
    for w in (meta.title or "").split():
        cw = "".join(c for c in w if c.isalnum())
        if len(cw) >= 4 and cw.lower() not in _STOP_WORDS:
            word = cw[:1].upper() + cw[1:].lower()
            break
    key = "{}{}{}".format(author or "anon", year, word)
    return key or "ref{}".format((meta.doi or meta.paper_id or "x")[:8])


def _priority_tier(rank: int, total: int) -> str:
    """按排名分三级着色标记（Word 里 emoji 可见，跨格式稳定）。"""
    if total <= 1:
        return "🔴"
    if rank < max(1, total // 3):
        return "🔴 高"
    if rank < max(2, 2 * total // 3):
        return "🟠 中"
    return "🟢 低"


# ---------------- 去重键（权威实现，backfill delegate 到此） ----------------

def _norm_title(t: Optional[str]) -> str:
    return "".join(ch.lower() for ch in (t or "") if ch.isalnum())


def dedup_key_fields(doi: Optional[str], arxiv_id: Optional[str], title: Optional[str],
                     fallback: str = "") -> str:
    """全局去重键：优先 DOI，其次 arXiv id，最后规范标题。

    标题也为空时退回 fallback（paper_id/citekey），避免多篇「三无」论文
    共享空键 "title:" 而被误判为同一篇（丢篇/吞篇）。
    """
    if doi:
        return "doi:" + doi.strip().lower().replace("https://doi.org/", "")
    if arxiv_id:
        return "arxiv:" + arxiv_id.strip().lower()
    t = _norm_title(title)
    if t:
        return "title:" + t
    return "id:" + (str(fallback or "").strip() or "unknown")


# ---------------- 句级角色 → highlights（工作流可调取的核心结构） ----------------

def _collect_highlights(triples):
    """把 (section_heading, tag, text) 三元组流聚合成 highlights[] + tag_counts。

    - tag 经 schema.TAG_TO_ROLE 归一到英文 role slug（citable/refutable/method）；
      映射为 None 的（如旧「研究背景」）丢弃，天然去噪。
    - highlights 项：{role, tag(原始中文), section, text}，供工作流按 role 跨库 jq 检索。
    - tag_counts 键为 role slug，口径与 highlights 一致（历史/新数据可比）。
    """
    from .schema import TAG_TO_ROLE
    highlights: List[Dict[str, Any]] = []
    tag_counts: Dict[str, int] = {}
    for heading, tag, text in triples:
        if not tag:
            continue
        role = TAG_TO_ROLE.get(tag)
        if role is None:
            continue
        highlights.append({"role": role, "tag": tag,
                           "section": heading or "", "text": (text or "").strip()})
        tag_counts[role] = tag_counts.get(role, 0) + 1
    return highlights, tag_counts


# ---------------- 阅读深度 ----------------

def _reading_depth(cr, series: str) -> Optional[str]:
    """条目的阅读深度。manual 链路必须兜住：pdf_ingest.synthesize_deep_read 不写 reading_depth，
    而 manual 正是全库读得最深的一批（逐块深读）；不兜的话量尺上最深的条目反倒落成 null。
    """
    if cr is None:
        return None
    if series == "manual" or cr.source == "manual-pdf":
        return getattr(cr, "reading_depth", None) or "chunked"
    return getattr(cr, "reading_depth", None)


# ---------------- 从内存对象构造条目（write_notes sidecar 复用，无损） ----------------

def entry_from_segment(seg, citekey: str, rank: int, total: int,
                       citekey_source: str = "fallback",
                       series: str = "auto") -> Dict[str, Any]:
    """从 PaperSegment 直接构造索引条目（不含 month/note_file 等落盘上下文，索引时补）。"""
    meta = seg.metadata
    fd = seg.filter_decision
    cr = seg.close_reading

    year = None
    if getattr(meta, "publication_date", None):
        year = meta.publication_date.year
    elif getattr(meta, "email_received_at", None):
        year = meta.email_received_at.year

    highlights, tag_counts = _collect_highlights(
        (sec.heading, st.tag, st.text)
        for sec in (cr.sections if cr else [])
        for st in sec.sentences)

    return {
        "citekey": citekey,
        "citekey_source": citekey_source,
        "series": series,
        "doi": meta.doi or None,
        "arxiv_id": meta.arxiv_id or None,
        "title": meta.title or "",
        "title_zh": seg.translated_title or None,
        "authors": list(meta.authors or []),
        "year": year,
        "journal": meta.journal or None,
        "url": meta.url or None,
        "priority_tier": _TIER_MAP.get(_priority_tier(rank, total), "low"),
        "priority_rank": rank + 1,
        "priority_score": round(float(seg.priority_score or 0.0), 4),
        "decision": fd.decision if fd else None,
        "one_line": (fd.one_line or "") if fd else "",
        "bucket": list(fd.bucket) if fd and fd.bucket else [],
        "role": (fd.role if fd and fd.role and fd.role != "NONE" else None),
        "confidence": (fd.confidence if fd else None),
        "flags": list(fd.flags) if fd and fd.flags else [],
        "has_full_text_reading": bool(cr and cr.from_full_text),
        "reading_source": (cr.source if cr else None),
        "fulltext_chars": (cr.body_chars if cr else None),
        "fulltext_chars_raw": (cr.body_chars_raw if cr else None),
        "fulltext_truncated": (cr.truncated if cr else None),
        "reading_depth": _reading_depth(cr, series),
        "tag_counts": tag_counts,
        "highlights": highlights,
        "dedup_key": dedup_key_fields(meta.doi, meta.arxiv_id, meta.title,
                                      fallback=meta.paper_id),
    }
