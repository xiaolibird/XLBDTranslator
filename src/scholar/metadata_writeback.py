# -*- coding: utf-8 -*-
"""札记元数据的「三处同步 + bundle」写回层（md 作者/期刊行、{stem}.index.json sidecar、
{stem}.references.json CSL、manual bundle）。

原在 scripts/realign_metadata_ts.py；2026-09-16 抽出，供 scripts/backfill_scholar_metadata.py
共用——两个脚本各写一份，改一边忘一边就会出现「md 改了、CSL 没改，下次重建被顶回」。
变更 dict `ch` 的形状：{"authors": [...], "journal": str, "volume"/"issue"/"pages": str,
"issued": {"date": date, "precision": "year"|"month"|"day", "force": bool?}}。
"""
import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._citekey_utils import csl_names, date_parts
from ..utils.logger import get_logger

logger = get_logger(__name__)

HEADING_RE_TMPL = r"^## .*\[@{}\]\s*$"
AUTHORS_LINE = "**作者**: "
JOURNAL_LINE = "**期刊/来源**: "


def _norm(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKC", (s or "")).strip().lower()
    return re.sub(r"\s+", " ", s)


def _norm_list(xs) -> List[str]:
    return [_norm(x) for x in (xs or [])]


def _families(authors: List[str]) -> set:
    return {_norm(n.get("family")) for n in csl_names(authors) if n.get("family")}


def _authors_md(authors: List[str]) -> str:
    # 与 notes.py 渲染口径一致：前 5 位 + " et al."
    return AUTHORS_LINE + ", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")


def _json_style(path: Path) -> Tuple[Optional[int], bool]:
    """沿用文件原有缩进与尾换行，别让一次改写把整个文件 diff 掉。"""
    try:
        raw = path.read_bytes()
    except Exception:
        return 2, False
    second = raw.split(b"\n", 2)[1] if b"\n" in raw else b""
    m = re.match(rb"^( +)", second)
    return (len(m.group(1)) if m else None), raw.endswith(b"\n")


def _write_json(path: Path, data, style: Tuple[Optional[int], bool]) -> None:
    indent, trailing = style
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent) + ("\n" if trailing else ""),
                   encoding="utf-8")
    tmp.replace(path)


class Backup:
    def __init__(self, root: Path, notes_dir: Path):
        self.root, self.notes_dir, self.done = root, notes_dir, set()

    def save(self, path: Path) -> None:
        if path in self.done or not path.exists():
            return
        rel = path.relative_to(self.notes_dir)
        dst = self.root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        self.done.add(path)


# ---------------- 定位 ----------------

def _find_heading(lines: List[str], citekey: str, hint_line: Optional[int]) -> Optional[int]:
    """返回该 citekey 的 `## ... [@ck]` 行下标（0-based）；同键多次出现取离索引 note_line 最近的。"""
    pat = re.compile(HEADING_RE_TMPL.format(re.escape(citekey)))
    hits = [i for i, ln in enumerate(lines) if pat.match(ln)]
    if not hits:
        return None
    if hint_line is None or len(hits) == 1:
        return hits[0]
    return min(hits, key=lambda i: abs((i + 1) - hint_line))


def _section_end(lines: List[str], start: int) -> int:
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") or lines[j].startswith("# "):
            return j
    return len(lines)


def _find_line(lines: List[str], lo: int, hi: int, prefix: str) -> Optional[int]:
    for j in range(lo, hi):
        if lines[j].startswith(prefix):
            return j
    return None


def _match_sidecar(entry: Dict[str, Any], rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """同 _match_csl 的谨慎度：DOI 精确优先，citekey 仅在唯一时用。"""
    doi = (entry.get("doi") or "").strip().lower()
    if doi:
        for r in rows:
            if (r.get("doi") or "").strip().lower() == doi:
                return r
    cand = [r for r in rows if r.get("citekey") == entry.get("citekey")]
    return cand[0] if len(cand) == 1 else None


# _match_bundle 的目录级缓存：{目录: [(路径, doi小写, 归一化标题)]}。
# realign 对同一 --month-prefix 范围逐条调用本函数，同月 manual 条目大概率落在同一目录
# （实测单目录 126 份 bundle：无缓存时 126 条 × 126 份 = 1.6 万次 读文件+JSON 解析，
# 有缓存只需 126 次）。缓存生命周期 = 进程（realign 是一次性 CLI）；同进程内若有写
# bundle 的路径（_apply_bundle 只改既有文件的字段，不增删文件、不动 doi/title），
# 键集合不变，缓存仍然有效。
_BUNDLE_DIR_CACHE: Dict[str, List[Tuple[Path, str, str]]] = {}


def _bundle_dir_rows(mdir: Path) -> List[Tuple[Path, str, str]]:
    key = str(mdir)
    rows = _BUNDLE_DIR_CACHE.get(key)
    if rows is None:
        rows = []
        for bf in sorted(mdir.glob("*.paper.json")):
            try:
                meta = ((json.loads(bf.read_text(encoding="utf-8")).get("segment") or {})
                        .get("metadata")) or {}
            except Exception as e:
                # 出声（缓存进程内永久生效，坏 bundle 会被静默剔除一整轮）：否则该篇
                # 只显示「未匹配」，看不出根因是这份 bundle 本身坏了——与本批修过的
                # _bbt_search_pick/_rename_pages 同型。
                logger.debug("bundle 解析失败，跳过（{}）：{}: {}", bf.name, type(e).__name__, e)
                continue
            rows.append((bf, (meta.get("doi") or "").strip().lower(), _norm(meta.get("title"))))
        _BUNDLE_DIR_CACHE[key] = rows
    return rows


def _match_bundle(entry: Dict[str, Any], notes_dir: Path) -> Optional[Path]:
    mdir = notes_dir / "manual" / (entry.get("month") or "")
    if not mdir.is_dir():
        return None
    doi = (entry.get("doi") or "").strip().lower()
    title = _norm(entry.get("title"))
    for bf, b_doi, b_title in _bundle_dir_rows(mdir):
        if doi and b_doi == doi:
            return bf
        if not doi and title and b_title == title:
            return bf
    return None


# ---------------- 写盘 ----------------

def _apply_md(lines: List[str], entry: Dict[str, Any], ch: Dict[str, Any]) -> Optional[bool]:
    """None=段落没定位到；False=定位到但行内容无需改（如只改了第 6 位以后的作者，
    md 只渲染前 5 位 + et al.）；True=改了。三态必须分开，否则无需改会被误报成没找到。"""
    h = _find_heading(lines, entry["citekey"], entry.get("note_line"))
    if h is None:
        return None
    end = _section_end(lines, h)
    changed = False
    if "authors" in ch:
        j = _find_line(lines, h + 1, end, AUTHORS_LINE)
        new = _authors_md(ch["authors"])
        if j is not None:
            if lines[j] != new:
                lines[j] = new
                changed = True
        else:
            k = _find_line(lines, h + 1, end, JOURNAL_LINE) or _find_line(lines, h + 1, end, "**DOI**") \
                or _find_line(lines, h + 1, end, "**链接**")
            if k is None:                       # 元信息块之后第一个空行之前
                k = next((x for x in range(h + 2, end) if lines[x].strip() == ""), end)
            lines.insert(k, new)
            end += 1
            changed = True
    if "journal" in ch:
        j = _find_line(lines, h + 1, end, JOURNAL_LINE)
        new = JOURNAL_LINE + ch["journal"]
        if j is not None:
            if lines[j] != new:
                lines[j] = new
                changed = True
        else:
            k = _find_line(lines, h + 1, end, AUTHORS_LINE)
            k = (k + 1) if k is not None else next(
                (x for x in range(h + 2, end) if lines[x].strip() == ""), end)
            lines.insert(k, new)
            changed = True
    return changed


def _apply_csl(csl: Dict[str, Any], ch: Dict[str, Any]) -> None:
    if "authors" in ch:
        csl["author"] = csl_names(ch["authors"])
    if "journal" in ch:
        csl["container-title"] = ch["journal"]
    for src, dst in (("volume", "volume"), ("issue", "issue"), ("pages", "page")):
        if src in ch:
            csl[dst] = ch[src]
    if "issued" in ch:
        csl["issued"] = {"date-parts": date_parts(ch["issued"]["date"], ch["issued"]["precision"])}


def _apply_sidecar(row: Dict[str, Any], ch: Dict[str, Any]) -> bool:
    """只有真改了字段才返回 True——sidecar 不存卷期页，只改那些时不该被重写。"""
    changed = False
    if "authors" in ch and list(row.get("authors") or []) != list(ch["authors"]):
        row["authors"] = list(ch["authors"])
        changed = True
    if "journal" in ch and row.get("journal") != ch["journal"]:
        row["journal"] = ch["journal"]
        changed = True
    if "issued" in ch and row.get("year") != ch["issued"]["date"].year:
        row["year"] = ch["issued"]["date"].year
        changed = True
    return changed


def _apply_bundle(data: Dict[str, Any], ch: Dict[str, Any]) -> None:
    meta = data["segment"]["metadata"]
    for k in ("authors", "journal", "volume", "issue", "pages"):
        if k in ch:
            meta[k] = ch[k]
    if "issued" in ch and (ch["issued"].get("force") or not meta.get("publication_date")):
        meta["publication_date"] = ch["issued"]["date"].isoformat()
        meta["date_precision"] = ch["issued"]["precision"]
