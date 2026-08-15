# -*- coding: utf-8 -*-
"""
citekey 与索引条目共享工具模块。

将 notes.py 与 notes_index.py 的共同实现（citekey 回退、消歧后缀序列、
优先级分级、entry_from_segment）收敛到单一模块，打破二者之间的循环导入。
"""
import re
from itertools import product
from typing import Dict, List, Any, Optional
from urllib.parse import urlsplit, parse_qs

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


# 场地原生标识：出版方分配、不可变、全局唯一，但没有 DOI 的会议/评审平台。
# 从 url 里抽，因为这些 id 恰恰就长在 url 上（也长在 PDF 文件名与官方 BibTeX key 上）。
# PMLR：https://proceedings.mlr.press/v287/elsharief25a.html → pmlr:v287/elsharief25a
# OpenReview：https://openreview.net/forum?id=x4UK4GadLd     → openreview:x4UK4GadLd
#
# 用 urlsplit 拆 host/path/query 后再匹配，而不是对整串跑一个正则：正则版要求 slug 紧跟
# `/`、`.html`、`.pdf` 或串尾，且 `?id=` 必须紧跟在 forum/pdf 之后，于是「带 query 无扩展名」
# 「参数换序」「attachment / references 链接」这些真实变体全部抽不出 —— 而抽不出就静默退回
# title: 键，正好回到这一层要解决的问题（标题被解析截断，身份跟着漂）。
# 但放宽的只是「参数位置/顺序」，**不是 path 本身**：OpenReview 仍按白名单只认论文端点
# （_OPENREVIEW_PAPER_PATHS），否则 group/search 页的 `?id=` 会被当成论文身份。
#
# 大小写：PMLR slug 本身全小写，归一化无害；OpenReview 的 forum id 是**区分大小写**的
# base62 串，.lower() 会把两个不同 id 折叠成同一个键 → 并查集判成同一篇 → 落败方被标
# duplicate_of 后被下游一律过滤 —— 即注释里反复强调「代价远高于漏合并」的静默吞篇。
# 故 PMLR 小写归一、OpenReview 原样保留。

# PMLR slug 形态：<姓（可含连字符/数字）> + 两位年 + 消歧后缀（a / b / aa …）。
# 用它在路径段里认 slug，从而绕开镜像路径的分支名/资产目录（main、assets…）与
# 附件后缀（elsharief25a-supp.pdf）。要求至少含一个字母，排除纯数字的分支名（如 2024）。
_PMLR_SLUG_RE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9\-]*?\d{2}[A-Za-z]+$")
_PMLR_VOLUME_RE = re.compile(r"^v\d+$", re.I)
_OPENREVIEW_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# OpenReview 里 `?id=` 承载**论文身份**的 path 只有这几个；group / search / profile /
# venue 之类的 `?id=` 装的是会场名、检索词、用户名，抽出来当身份键会把两篇不相干的论文
# 判成同一篇（`group?id=NeurIPS` → openreview:NeurIPS）→ 落败方标 duplicate_of 被下游
# 一律过滤，即本模块反复强调「代价远高于漏合并」的静默吞篇。
# PMLR 那一支靠 `^v\d+$` + slug 形态天然只认论文本体，OpenReview 这一支只能靠白名单补齐。
_OPENREVIEW_PAPER_PATHS = {"forum", "pdf", "attachment", "references", "revisions"}


def _pmlr_native_id(host: str, parts: List[str]) -> Optional[str]:
    if host == "proceedings.mlr.press":
        segs = parts
    elif host == "raw.githubusercontent.com" and parts[:1] == ["mlresearch"]:
        segs = parts[1:]          # 官方 GitHub 镜像：/mlresearch/v281/main/assets/<slug>/<slug>.pdf
    else:
        return None
    for i, seg in enumerate(segs):
        if not _PMLR_VOLUME_RE.match(seg):
            continue
        for nxt in segs[i + 1:]:
            stem = nxt.rsplit(".", 1)[0] if "." in nxt else nxt
            if _PMLR_SLUG_RE.match(stem):
                return "pmlr:{}/{}".format(seg.lower(), stem.lower())
        return None
    return None


def _openreview_native_id(host: str, parts: List[str], query: str) -> Optional[str]:
    if host != "openreview.net":
        return None
    # path 白名单：`?id=` 只有长在论文端点上才是论文身份（见 _OPENREVIEW_PAPER_PATHS）。
    # 空 path（openreview.net/?id=x）也不认——首页的 query 不承载身份。
    if not parts:
        return None
    if parts[-1].rsplit(".", 1)[0] not in _OPENREVIEW_PAPER_PATHS:
        return None
    for v in parse_qs(query).get("id", []):
        # 只认 forum/pdf 那种 base62 id；profile 链接的 `~John_Doe1` 带 ~，天然被排除
        if _OPENREVIEW_ID_RE.match(v or ""):
            return "openreview:" + v          # 大小写敏感，原样保留
    return None


def venue_native_id(url: Optional[str]) -> Optional[str]:
    """从 url 抽场地原生标识，形如 `pmlr:v287/elsharief25a`；抽不出返回 None。"""
    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u and not u.startswith("//"):
        u = "//" + u              # 无 scheme 时让 urlsplit 认出 netloc
    try:
        sp = urlsplit(u)
    except Exception:
        return None
    host = (sp.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in sp.path.split("/") if p]
    return _pmlr_native_id(host, parts) or _openreview_native_id(host, parts, sp.query)


def dedup_key_fields(doi: Optional[str], arxiv_id: Optional[str], title: Optional[str],
                     fallback: str = "", url: Optional[str] = None) -> str:
    """全局去重键：DOI > arXiv id > 场地原生 id > 规范标题 > fallback。

    场地原生 id（PMLR slug / OpenReview id）排在标题之前的理由：它由**出版方分配**、
    不可变、全局唯一，而标题键是派生量——标题被解析截断（本库实测出现过
    "Healthcare Analytics"、"ICU ADMISSION PREDICTION" 这种残条）键就跟着变，
    同一篇论文从不同来源进来会拿到两个键。

    2026-08-15 实测：本库 44 篇无 DOI 的 PMLR 论文里，OpenAlex 只能认出 33 篇，
    且全部经由 arXiv/PubMed（0 篇有 PMLR 落点）；剩下 8 篇在外部库中完全不存在，
    PMLR slug 是它们**唯一**的正经标识符。故这一层不是可选优化，是那批论文的兜底底线。

    排在 arXiv id 之后而非之前：同一篇的预印本记录没有 PMLR url，正刊记录没有 arXiv id，
    两者本就拿不到同一个键——那属于人工裁决层（dedup_overrides.json）的职责，
    不是这里能解决的。

    标题也为空时退回 fallback（paper_id/citekey），避免多篇「三无」论文
    共享空键 "title:" 而被误判为同一篇（丢篇/吞篇）。
    """
    if doi:
        return "doi:" + doi.strip().lower().replace("https://doi.org/", "")
    if arxiv_id:
        return "arxiv:" + arxiv_id.strip().lower()
    vid = venue_native_id(url)
    if vid:
        return vid
    t = _norm_title(title)
    if t:
        return "title:" + t
    return "id:" + (str(fallback or "").strip() or "unknown")


def recompute_entry_key(entry: Dict[str, Any]) -> str:
    """按**当前**键梯重算一条索引/sidecar 记录的 dedup_key（落盘的冻结值只当兜底）。

    唯一的重算入口：notes_index.build_month_entries（索引侧）与 ingest._existing_note_dedup_keys
    （整篇覆盖守卫侧）共用。两处曾各写各的——索引侧按新规则重算、守卫侧直读 sidecar 里
    落盘时冻下来的旧键，于是键梯一升级（2026-08-15 加 pmlr:/openreview: 层），同一篇论文
    经两条路拿到两个键：守卫把「本批已覆盖」的论文算成「本批未覆盖」，同 label 重跑被误判
    成「会丢数据」而拒写，提示换 --label —— 而换 label 会真的产出重复札记。

    例外：重算落到 "id:"（doi/arxiv/场地 id/标题全空）且原本存有键时保留原值——sidecar 的
    id: 用 paper_id 兜底，比这里能拿到的 citekey 更稳，重算反而更差。
    """
    rekey = dedup_key_fields(entry.get("doi"), entry.get("arxiv_id"), entry.get("title"),
                             fallback=entry.get("citekey") or "", url=entry.get("url"))
    if rekey.startswith("id:") and entry.get("dedup_key"):
        return entry["dedup_key"]
    return rekey


# ---------------- 出版日期精度（CSL issued / Zotero date 的产出层截断） ----------------
#
# 术语消歧：CSL 的 `issued` = **出版日期**，`issue` = **期号**，两者无关，别读混。
#
# 背景：`PaperMetadata.publication_date` 是 date 类型，必须凑齐年月日，于是各来源在只知道
# 年（或年月）时一律补 1。参考文献因此渲染出论文并不存在的月份（"2026 (January)"）。
# 修法是另存 `date_precision` 记录真相，**只在产出层按精度截断**，绝不动 date 本身
# ——publication_date 一旦可空，`_fallback_citekey` 取年与索引 year 字段会跟着塌。

def infer_date_precision(d) -> str:
    """存量条目（date_precision 为 None）的精度启发式。

    只能从"月日长什么样"倒推，必然有误伤：真实 1 月 1 日出版的会被判成 year、
    真实某月 1 日出版的会被判成 month（实测全库分别约 2-3 条与 29 条）。
    代价只是渲染少一级月/日，**年份永不丢**，检索与身份键完全不受影响。
    """
    if d is None:
        return "year"
    if (d.month, d.day) == (1, 1):
        return "year"
    if d.day == 1:
        return "month"
    return "day"


def date_parts(d, precision=None):
    """按精度产出 CSL issued 的 date-parts：day→[[y,m,d]]、month→[[y,m]]、year→[[y]]。"""
    if d is None:
        return None
    p = precision or infer_date_precision(d)
    if p == "year":
        return [[d.year]]
    if p == "month":
        return [[d.year, d.month]]
    return [[d.year, d.month, d.day]]


def date_string(d, precision=None) -> str:
    """按精度产出 ISO 串（Zotero 的 date 字段接受部分日期）：2026 / 2026-05 / 2026-05-05。"""
    if d is None:
        return ""
    p = precision or infer_date_precision(d)
    if p == "year":
        return "{:04d}".format(d.year)
    if p == "month":
        return "{:04d}-{:02d}".format(d.year, d.month)
    return d.isoformat()


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
                                      fallback=meta.paper_id,
                                      url=getattr(meta, "url", None)),
    }
