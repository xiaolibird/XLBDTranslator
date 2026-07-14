# -*- coding: utf-8 -*-
"""
科研札记文献索引：把 output/scholar_notes/ 的月度札记聚合成机器可读的 literature_index.json
（+人读 INDEX.md + 部署 AGENTS.md），供论文项目的 agent 检索可用文献。

数据源两路（build_month_entries）：
  - sidecar `{slug}.index.json`（write_notes 顺手写出，无损：含 arxiv_id/priority_score 等）——优先；
  - 存量札记无 sidecar 时解析 md（行首锚定正则）+ references.json（CSL-JSON）合并。

去重与 scripts/backfill_notes.py 同源同规则（doi: > arxiv: > title: 规范化，最早月优先）；
本模块即权威实现，backfill delegate 到这里。重复条目不删除，标 `duplicate_of` 供消费方过滤。
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1
INDEX_JSON = "literature_index.json"
INDEX_MD = "INDEX.md"
AGENTS_MD = "AGENTS.md"

# 成品札记三件套的 md 命名（天然排除 demo/ideal/validate/digest_* 等杂档）
NOTE_MD_RE = re.compile(r"^科研札记_(\d{4}-\d{2})_全文精读\.md$")

# 每篇论文小节标题行（notes._paper_section 第 92 行的格式契约）：
#   ## 🔴 高 2. Title ... [@citekey]
_SECTION_RE = re.compile(
    r"^## (🔴 高|🟠 中|🟢 低|🔴)\s+(\d+)\.\s+(.*)\s+\[@([^\[\]\s]+)\]\s*$")
_PRIORITY_RE = re.compile(r"^\*\*优先级\*\*: `([\d.]+)`")
_DECISION_RE = re.compile(r"`(INCLUDE|MAYBE|EXCLUDE)`")
_BUCKET_RE = re.compile(r"维度 ([A-G](?:/[A-G])*)")
_ROLE_RE = re.compile(r"角色 (\S+)")
_CONF_RE = re.compile(r"conf ([\d.]+)")
_FLAGS_RE = re.compile(r"⚑ (\S+)")
_DOI_RE = re.compile(r"^\*\*DOI\*\*: \[([^\]]+)\]")
_URL_RE = re.compile(r"^\*\*链接\*\*: (\S+)")
_CLOSEREAD_RE = re.compile(r"^### (全文精读|精读（仅摘要降级）)(?: · 来源 `(.+?)`)?")
_TAG_LINE_RE = re.compile(r"^- 〔(方法学创新|重要发现|研究背景)〕")
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/\d{7})")

_TIER_MAP = {"🔴 高": "high", "🔴": "high", "🟠 中": "mid", "🟢 低": "low"}


# ---------------- 去重键（权威实现，backfill delegate 到此） ----------------

def norm_title(t: Optional[str]) -> str:
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
    t = norm_title(title)
    if t:
        return "title:" + t
    return "id:" + (str(fallback or "").strip() or "unknown")


# ---------------- 从内存对象构造条目（write_notes sidecar 复用，无损） ----------------

def entry_from_segment(seg, citekey: str, rank: int, total: int,
                       citekey_source: str = "fallback") -> Dict[str, Any]:
    """从 PaperSegment 直接构造索引条目（不含 month/note_file 等落盘上下文，索引时补）。"""
    from .notes import _priority_tier  # 延迟导入，避免与 notes.py 的 sidecar 钩子成环
    meta = seg.metadata
    fd = seg.filter_decision
    cr = seg.close_reading

    year = None
    if getattr(meta, "publication_date", None):
        year = meta.publication_date.year
    elif getattr(meta, "email_received_at", None):
        year = meta.email_received_at.year

    tag_counts: Dict[str, int] = {}
    if cr:
        for sec in cr.sections:
            for st in sec.sentences:
                if st.tag:
                    tag_counts[st.tag] = tag_counts.get(st.tag, 0) + 1

    return {
        "citekey": citekey,
        "citekey_source": citekey_source,
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
        "tag_counts": tag_counts,
        "dedup_key": dedup_key_fields(meta.doi, meta.arxiv_id, meta.title,
                                      fallback=meta.paper_id),
    }


# ---------------- 存量札记：md 解析 + CSL 合并 ----------------

def parse_note_md(md_path: Path) -> List[Dict[str, Any]]:
    """逐节解析札记 md（行首锚定），返回条目列表（字段有损：无 priority_score 之外的原始分等）。"""
    entries: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    lines = Path(md_path).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, 1):
        m = _SECTION_RE.match(line)
        if m:
            tier, num, title, citekey = m.groups()
            cur = {
                "citekey": citekey,
                "citekey_source": "unknown",  # md 无法区分 Zotero 键/兜底键
                "doi": None, "arxiv_id": None,
                "title": title.strip(), "title_zh": None,
                "authors": [], "year": None, "journal": None, "url": None,
                "priority_tier": _TIER_MAP.get(tier, "low"),
                "priority_rank": int(num), "priority_score": None,
                "decision": None, "one_line": "", "bucket": [], "role": None,
                "confidence": None, "flags": [],
                "has_full_text_reading": False, "reading_source": None,
                "tag_counts": {},
                "note_heading": line, "note_line": i,
            }
            ym = re.match(r"[a-z]*?(\d{4})", citekey)
            if ym:
                cur["year"] = int(ym.group(1))
            entries.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("# "):          # 参考文献等一级节，论文区结束
            cur = None
            continue
        pm = _PRIORITY_RE.match(line)
        if pm:
            cur["priority_score"] = float(pm.group(1))
            continue
        if line.startswith("**裁决**:"):
            dm = _DECISION_RE.search(line)
            if dm:
                cur["decision"] = dm.group(1)
            bm = _BUCKET_RE.search(line)
            if bm:
                cur["bucket"] = bm.group(1).split("/")
            rm = _ROLE_RE.search(line)
            if rm:
                cur["role"] = rm.group(1)
            cm = _CONF_RE.search(line)
            if cm:
                cur["confidence"] = float(cm.group(1))
            fm = _FLAGS_RE.search(line)
            if fm:
                cur["flags"] = fm.group(1).split("/")
            continue
        if line.startswith("**一句话用处**: "):
            cur["one_line"] = line[len("**一句话用处**: "):].strip()
            continue
        if line.startswith("**作者**: "):
            raw = line[len("**作者**: "):].strip()
            raw = raw[:-len(" et al.")] if raw.endswith(" et al.") else raw
            cur["authors"] = [a.strip() for a in raw.split(",") if a.strip()]
            continue
        if line.startswith("**期刊/来源**: "):
            cur["journal"] = line[len("**期刊/来源**: "):].strip()
            continue
        dm = _DOI_RE.match(line)
        if dm:
            cur["doi"] = dm.group(1).strip()
            continue
        um = _URL_RE.match(line)
        if um:
            cur["url"] = um.group(1).strip()
            am = _ARXIV_URL_RE.search(cur["url"])
            if am:
                cur["arxiv_id"] = am.group(1)
            continue
        crm = _CLOSEREAD_RE.match(line)
        if crm:
            cur["has_full_text_reading"] = crm.group(1) == "全文精读"
            cur["reading_source"] = crm.group(2)
            continue
        tm = _TAG_LINE_RE.match(line)
        if tm:
            tc = cur["tag_counts"]
            tc[tm.group(1)] = tc.get(tm.group(1), 0) + 1
    for e in entries:
        e["dedup_key"] = dedup_key_fields(e["doi"], e["arxiv_id"], e["title"],
                                          fallback=e["citekey"])
    return entries


def load_csl_items(ref_path: Path) -> List[Dict[str, Any]]:
    """references.json（CSL-JSON 数组）→ item 列表。缺文件/坏文件返回空。"""
    try:
        items = json.loads(Path(ref_path).read_text(encoding="utf-8"))
        return [it for it in items if isinstance(it, dict)]
    except Exception:
        return []


def _match_csl(entry: Dict[str, Any], items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """给 md 条目挑对应 CSL item：先按 DOI 精确，citekey 仅在文件内唯一时才用。

    同月多篇论文可能共用同一 citekey（BBT 误配所致）——若盲按 citekey 匹配，
    第二篇会被灌入第一篇的 DOI/作者（曾致索引里两篇不同论文同 DOI 的假重复）。
    """
    doi = (entry.get("doi") or "").strip().lower()
    if doi:
        for it in items:
            if (it.get("DOI") or "").strip().lower() == doi:
                return it
    cand = [it for it in items if it.get("id") == entry.get("citekey")]
    return cand[0] if len(cand) == 1 else None


def _merge_csl(entry: Dict[str, Any], item: Dict[str, Any]) -> None:
    """作者/DOI/年份/期刊以 CSL 为准（结构化、无 et al. 截断），md 值兜底。"""
    if item.get("DOI"):
        entry["doi"] = item["DOI"]
    if item.get("URL") and not entry.get("url"):
        entry["url"] = item["URL"]
    if entry.get("url") and not entry.get("arxiv_id"):
        am = _ARXIV_URL_RE.search(entry["url"])
        if am:
            entry["arxiv_id"] = am.group(1)
    authors = []
    for a in item.get("author", []) or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
        if name:
            authors.append(name)
    if authors:
        entry["authors"] = authors
    try:
        entry["year"] = item["issued"]["date-parts"][0][0]
    except Exception:
        pass
    if item.get("container-title"):
        entry["journal"] = item["container-title"]
    entry["dedup_key"] = dedup_key_fields(entry["doi"], entry["arxiv_id"], entry["title"],
                                          fallback=entry.get("citekey", ""))


def _locate_headings(md_path: Path) -> Dict[str, Any]:
    """citekey -> (行号, 标题行)，供 sidecar 条目补 note_line/note_heading。"""
    out = {}
    try:
        for i, line in enumerate(Path(md_path).read_text(encoding="utf-8").splitlines(), 1):
            m = _SECTION_RE.match(line)
            if m:
                out[m.group(4)] = (i, line)
    except Exception:
        pass
    return out


def build_month_entries(month: str, md_path: Path,
                        ref_path: Optional[Path],
                        sidecar_path: Optional[Path]) -> List[Dict[str, Any]]:
    """单月条目：sidecar 优先（无损）；否则 md 解析 + CSL 合并。补齐落盘上下文字段。"""
    entries: List[Dict[str, Any]] = []
    source = "md-parse"
    if sidecar_path and Path(sidecar_path).exists():
        try:
            data = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
            entries = list(data.get("papers", []))
            source = "sidecar"
        except Exception as e:
            logger.warning("  ⚠️ sidecar 损坏，退回 md 解析（{}）: {}".format(sidecar_path, e))
    if not entries:
        entries = parse_note_md(md_path)
        csl = load_csl_items(ref_path) if ref_path and Path(ref_path).exists() else []
        for e in entries:
            item = _match_csl(e, csl)
            if item:
                _merge_csl(e, item)
        source = "md-parse"
    else:
        locs = _locate_headings(md_path)
        for e in entries:
            loc = locs.get(e.get("citekey"))
            e["note_line"] = loc[0] if loc else None
            e["note_heading"] = loc[1] if loc else None
    has_refs = bool(ref_path and Path(ref_path).exists())
    for e in entries:
        e["month"] = month
        e["note_file"] = Path(md_path).name
        e["references_json"] = Path(ref_path).name if has_refs else None
        e["_source"] = source
        e.setdefault("duplicate_months", [])
        e.setdefault("duplicate_of", None)
    return entries


# ---------------- 索引构建（增量/全量/区间） ----------------

def _note_files(notes_dir: Path) -> Dict[str, Path]:
    """month -> md 路径（只认成品命名）。"""
    out = {}
    for p in sorted(Path(notes_dir).glob("*.md")):
        m = NOTE_MD_RE.match(p.name)
        if m:
            out[m.group(1)] = p
    return out


def _entry_keys(e: Dict[str, Any]) -> List[str]:
    """条目的身份键集合：dedup_key + 规范化标题键（二级）。

    二级标题键捕获「同一论文、不同 dedup_key」的漏网重复——典型场景是
    某月该篇缺 DOI（title 键）、另一月经 Crossref 补出 DOI（doi 键），
    一级键不同但实为同文（如预印本/正刊双收）。
    """
    keys = [e["dedup_key"]]
    t = norm_title(e.get("title"))
    if t:
        tk = "title:" + t
        if tk != e["dedup_key"]:
            keys.append(tk)
    return keys


def _global_pass(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """全局排序 + 跨月去重标记（最早月优先）+ 撞键检测的前置排序。"""
    papers.sort(key=lambda e: (e["month"], e.get("priority_rank") or 9999))
    first: Dict[str, Dict[str, Any]] = {}
    for e in papers:
        e["duplicate_months"] = []
        e["duplicate_of"] = None
    for e in papers:
        keys = _entry_keys(e)
        keeper = next((first[k] for k in keys if k in first), None)
        if keeper is not None and keeper is not e:
            if e["month"] not in keeper["duplicate_months"]:
                keeper["duplicate_months"].append(e["month"])
            e["duplicate_of"] = "{}@{}".format(keeper["dedup_key"], keeper["month"])
        else:
            for k in keys:
                first.setdefault(k, e)
    return papers


def _citekey_collisions(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同 citekey 指向不同论文（不同 dedup_key）→ 撞键告警（合并 bibliography 会吞篇）。"""
    by_key: Dict[str, Set[str]] = {}
    months: Dict[str, List[str]] = {}
    for e in papers:
        if e.get("duplicate_of"):
            continue
        ck = e.get("citekey") or ""
        by_key.setdefault(ck, set()).add(e["dedup_key"])
        months.setdefault(ck, []).append(e["month"])
    return [{"citekey": ck, "months": sorted(set(months[ck]))}
            for ck, keys in sorted(by_key.items()) if len(keys) > 1]


def update_index(notes_dir: Path, *, full: bool = False,
                 since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """构建/更新索引 dict。

    增量（默认）：只重解析 mtime/size 变化或未入索引的月份；
    since/until：**强制重扫**区间内月份（不看 mtime），区间外沿用旧条目；
    full：全量重建。已删除的月份 md 会连同其索引条目一起消失（months 以磁盘为准）。
    """
    notes_dir = Path(notes_dir)
    index_path = notes_dir / INDEX_JSON
    old: Dict[str, Any] = {}
    if not full and index_path.exists():
        try:
            old = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old_months = old.get("months", {})
    old_papers = old.get("papers", [])

    files = _note_files(notes_dir)
    months_meta: Dict[str, Any] = {}
    papers: List[Dict[str, Any]] = []
    reparsed = kept = 0
    range_mode = since is not None or until is not None
    for month, md_path in sorted(files.items()):
        in_range = (since is None or month >= since) and (until is None or month <= until)
        st = md_path.stat()
        prev = old_months.get(month)
        unchanged = (prev and prev.get("md_mtime") == st.st_mtime
                     and prev.get("md_size") == st.st_size)
        force = full or (range_mode and in_range)   # 区间模式：区间内强制重扫
        if (not in_range and prev) or (unchanged and not force):
            # 沿用旧条目（区间外月份即使变化也不动，除非它根本不在旧索引里）
            entries = [e for e in old_papers if e.get("month") == month]
            months_meta[month] = prev
            kept += 1
        else:
            stem = md_path.name[:-3]
            entries = build_month_entries(
                month, md_path,
                ref_path=notes_dir / "{}.references.json".format(stem),
                sidecar_path=notes_dir / "{}.index.json".format(stem))
            months_meta[month] = {"md_mtime": st.st_mtime, "md_size": st.st_size,
                                  "papers": len(entries),
                                  "source": entries[0]["_source"] if entries else "empty"}
            reparsed += 1
        papers.extend(entries)

    for e in papers:
        e.pop("_source", None)
    papers = _global_pass(papers)
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "months": months_meta,
        "citekey_collisions": _citekey_collisions(papers),
        "papers": papers,
    }
    logger.info("  索引：{} 个月（重解析 {}，沿用 {}），共 {} 篇，撞键 {} 组".format(
        len(months_meta), reparsed, kept, len(papers), len(index["citekey_collisions"])))
    return index


# ---------------- 输出（幂等：内容未变不落盘） ----------------

def _stable(index: Dict[str, Any]) -> str:
    d = dict(index)
    d.pop("generated_at", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
    except Exception:
        pass
    path.write_text(content, encoding="utf-8")
    return True


def build_index_md(index: Dict[str, Any]) -> str:
    """人读 INDEX.md：顶部统计 + 撞键告警 + 按月倒序表。"""
    papers = [e for e in index["papers"] if not e.get("duplicate_of")]
    n_inc = sum(1 for e in papers if e.get("decision") == "INCLUDE")
    n_ft = sum(1 for e in papers if e.get("has_full_text_reading"))
    lines = ["# 科研札记文献索引", "",
             "机器可读版：`literature_index.json`（查询配方见 `AGENTS.md`）。", "",
             "- 覆盖月份：**{}**（{} → {}）".format(
                 len(index["months"]),
                 min(index["months"]) if index["months"] else "-",
                 max(index["months"]) if index["months"] else "-"),
             "- 论文：**{}** 篇（INCLUDE {} · 全文精读 {}）".format(len(papers), n_inc, n_ft)]
    if index["citekey_collisions"]:
        lines.append("- ⚠️ **citekey 撞键 {} 组**（不同论文同键，合并 bibliography 前必须处理）：{}".format(
            len(index["citekey_collisions"]),
            "; ".join("`{}` ({})".format(c["citekey"], ",".join(c["months"]))
                      for c in index["citekey_collisions"])))
    lines.append("")
    esc = lambda s: (s or "").replace("|", "/")
    tier_emoji = {"high": "🔴", "mid": "🟠", "low": "🟢"}
    for month in sorted(index["months"], reverse=True):
        rows = [e for e in papers if e["month"] == month]
        if not rows:
            continue
        lines.extend(["## {}".format(month), "",
                      "| # | 优先级 | 裁决 | citekey | 标题 | 一句话用处 | DOI |",
                      "|:-:|:-:|:-:|---|---|---|---|"])
        for e in sorted(rows, key=lambda x: x.get("priority_rank") or 9999):
            lines.append("| {} | {} | {} | `{}` | {} | {} | {} |".format(
                e.get("priority_rank") or "", tier_emoji.get(e.get("priority_tier"), ""),
                e.get("decision") or "", e.get("citekey") or "",
                esc(e.get("title")), esc(e.get("one_line")), e.get("doi") or ""))
        lines.append("")
    lines.append("_generated_at: {}_".format(index.get("generated_at", "")))
    lines.append("")
    return "\n".join(lines)


def _agents_source() -> Optional[Path]:
    p = Path(__file__).resolve().parents[2] / "docs" / "scholar_notes_AGENTS.md"
    return p if p.exists() else None


def write_outputs(index: Dict[str, Any], notes_dir: Path) -> Dict[str, bool]:
    """写 literature_index.json + INDEX.md + 部署 AGENTS.md。内容未变不落盘（mtime 不抖）。"""
    notes_dir = Path(notes_dir)
    wrote = {"index_json": False, "index_md": False, "agents_md": False}

    index_path = notes_dir / INDEX_JSON
    if index_path.exists():
        try:
            if _stable(json.loads(index_path.read_text(encoding="utf-8"))) == _stable(index):
                logger.info("  索引内容未变，跳过写盘")
            else:
                wrote["index_json"] = True
        except Exception:
            wrote["index_json"] = True
    else:
        wrote["index_json"] = True
    if wrote["index_json"]:
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    md = build_index_md(index)
    old_md = (notes_dir / INDEX_MD)
    if wrote["index_json"] or not old_md.exists():
        wrote["index_md"] = _write_if_changed(old_md, md)

    src = _agents_source()
    if src:
        wrote["agents_md"] = _write_if_changed(notes_dir / AGENTS_MD,
                                               src.read_text(encoding="utf-8"))
    return wrote


# ---------------- citekey 撞键修复 ----------------

def _rename_citekey_in_note(notes_dir: Path, entry: Dict[str, Any],
                            old: str, new: str) -> bool:
    """把 entry 所在札记里的 [@old] 改为 [@new]，并同步 references.json 的 id。

    优先按 entry.note_line 定点替换（防同名键误伤），找不到再全文首个命中。
    """
    md = Path(notes_dir) / entry["note_file"]
    try:
        lines = md.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        logger.warning("  ⚠️ 读札记失败，跳过改键 {}: {}".format(md, e))
        return False
    tag_old, tag_new = "[@{}]".format(old), "[@{}]".format(new)
    ln = entry.get("note_line")
    hit_line = None
    if ln and 1 <= ln <= len(lines) and tag_old in lines[ln - 1]:
        hit_line = ln - 1
    else:
        hit_line = next((i for i, l in enumerate(lines) if tag_old in l), None)
    if hit_line is None:
        logger.warning("  ⚠️ 未在 {} 找到 {}，跳过".format(md.name, tag_old))
        return False
    lines[hit_line] = lines[hit_line].replace(tag_old, tag_new)
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ref_name = entry.get("references_json")
    if ref_name:
        rp = Path(notes_dir) / ref_name
        try:
            items = json.loads(rp.read_text(encoding="utf-8"))
            cand = [it for it in items if isinstance(it, dict) and it.get("id") == old]
            # 按 DOI 精确挑（同文件同 id 极罕见，防御一下）
            doi = (entry.get("doi") or "").lower()
            tgt = next((it for it in cand if doi and (it.get("DOI") or "").lower() == doi),
                       cand[0] if cand else None)
            if tgt is not None:
                tgt["id"] = new
                rp.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except Exception as e:
            logger.warning("  ⚠️ 同步 references.json 失败（{}）: {}".format(ref_name, e))
    return True


def fix_citekey_collisions(notes_dir: Path) -> int:
    """自动修复撞键：同 citekey 指向不同论文时，保最早月不动，
    后出现者加 b/c… 后缀（仿 BBT 消歧），就地改 md + references.json。

    返回重命名条数；调用方随后应重建索引（md 已变更）。docx 为人读版不回写。
    """
    notes_dir = Path(notes_dir)
    index = update_index(notes_dir)
    live = [e for e in index["papers"] if not e.get("duplicate_of")]
    all_keys = {e.get("citekey") for e in index["papers"]}
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for e in live:
        by_key.setdefault(e.get("citekey") or "", []).append(e)
    renamed = 0
    for key, group in sorted(by_key.items()):
        if not key or len(group) <= 1 or len({e["dedup_key"] for e in group}) <= 1:
            continue
        group.sort(key=lambda e: (e["month"], e.get("priority_rank") or 9999))
        for e in group[1:]:                      # 最早月保留原键
            suf = ord("b")
            new = "{}{}".format(key, chr(suf))
            while new in all_keys:
                suf += 1
                new = "{}{}".format(key, chr(suf))
            if _rename_citekey_in_note(notes_dir, e, key, new):
                all_keys.add(new)
                renamed += 1
                logger.info("  🔧 改键 {} → {}（{}）".format(key, new, e["month"]))
    return renamed


# ---------------- backfill 去重集 ----------------

def load_seen_keys(index_path: Path,
                   exclude_months: Optional[Set[str]] = None) -> Set[str]:
    """从索引恢复全局去重键集合（供 backfill 跨运行去重）。文件不存在返回空集。

    exclude_months：--force 重跑月份时剔除该月涉及的键，避免自 dedup 成空札记。
    按**键**整体剔除而非按条目 month 过滤——同一 dedup_key 可能同时以 keeper 身份
    落在重跑月、又以 duplicate_of 身份落在别的月；只剔本月条目会让该键残留在
    seen 里，重跑时把这篇论文 dedup 掉（丢篇）。
    """
    p = Path(index_path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("  ⚠️ 读索引失败，去重集置空: {}".format(e))
        return set()
    papers = [e for e in data.get("papers", []) if e.get("dedup_key")]
    excl_months = exclude_months or set()
    excluded_keys = {e["dedup_key"] for e in papers if e.get("month") in excl_months}
    return {e["dedup_key"] for e in papers if e["dedup_key"] not in excluded_keys}
