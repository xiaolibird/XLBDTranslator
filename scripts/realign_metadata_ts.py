#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Zotero translation-server 对**存量**札记做元数据重对齐（作者 / 期刊 / 卷 / 期 / 页 / 出版日期）。

## 背景
- auto 周札记的 ingest 链路会把标识符交给 translation-server 做权威解析（enrich_segments），
  但 docker 容器 2026-08-25 06:09 停机（宿主重启、OrbStack 不随登录启动）、09-01 08:41 才起，
  期间入库的月份全走了自建元数据。
- manual 手动精读（read_pdf.py / pdf_ingest.py）**从未**接 translation-server，
  元数据来自 Crossref-doi / arXiv / pdf-llm 三路各自的口径。
本脚本对指定月份前缀下的 auto+manual 条目补这一步；book 系列（ISBN 章条目）不在范围。

## 只改什么
authors / journal / volume / issue / pages 以 translation-server 返回值为准（有值才覆盖）；
出版日期：DOI 条目仅在 CSL 缺 issued 时用 TS 的 date 补（保留日精度）；
arXiv 条目**不用 TS 的 date**（那是被请求版本的日期，v4 会把 2025-10 首发写成 2026-08）——
arXiv 号本身编码了首发年月（YYMM.NNNNN），缺 issued 或年份与号段不符时按号段写月精度。
**不碰 title / DOI / arxiv_id / citekey**：DOI 是 dedup_key 的第一档身份键，
TS 对 arXiv 预印本会回 10.48550/arXiv.* 的 DOI，一写就把身份键从 arxiv: 换成 doi:，
派生物（向量库 / 书目 / vault / 已发出的 [@key]）全要跟着扫——不是本脚本的事。
arXiv 号被 TS 解析成**正刊**（回了非 10.48550 的 DOI）时，也不写刊名卷期页——否则 CSL
成了「预印本类型 + 期刊卷期页 + 无 DOI」的混合体；该 DOI 记进报告 `doi_candidate`，
身份键要不要整体升级由人决定。

## 守卫
- itemType 不在 {journalArticle, preprint, conferencePaper, bookSection, report, thesis, book}
  的（如 Zenodo 代码发布 computerProgram）整条跳过——它的 creators 是代码仓署名不是论文作者。
- 作者表比现有更**短**、或新旧姓氏集合**无交集**的 TS 结果默认拒绝（--allow-author-shrink 只放开前者），
  防止把完整作者表换成残条 / 换成另一篇的作者。

## 三处同步 + bundle
札记的元数据有三份快照必须一致（踩过的坑）：md 的 `**作者**:`/`**期刊/来源**:` 行、
`{stem}.index.json` sidecar（索引优先读它；issued 变更要同步 sidecar.year）、
`{stem}.references.json`（pandoc 书目）。manual 系列还要把同样的字段写回
`manual/<month>/*.paper.json` 的 segment.metadata——finalize/regen 是从 bundle **整月重建**，
不回写就会在下一次 regen 被旧值顶回。

## 完成判据
「重跑 dry-run 0 变更」**不是**完成判据：TS 命中集合随时间漂移（上游限流 501、arXiv 新号
export API 滞后几天才可查），首轮没命中的下一轮会命中。报告里 `unresolved[]` 列出本轮
没解析到的标识符；完成 = 变更 0 **且** unresolved 为空（或剩下的都是已知坏标识符）。

## 用法
    PYTHONPATH=. python3 scripts/realign_metadata_ts.py --month-prefix 2026-08            # dry-run
    PYTHONPATH=. python3 scripts/realign_metadata_ts.py --month-prefix 2026-08 --apply    # 写盘（先备份）
--apply 收尾会**自己**按 --since/--until 强制重扫该前缀的索引月份（notes_index 的增量模式只认
md 的 mtime，只改了 sidecar/CSL 的文件不会被重扫）并重写 all_references.json；
vault 由 launchd 的 scholar-vault job 跟进，也可手跑 `scripts/sync_vault.py`。

退出码：0 正常 / 2 参数或 translation-server 不在线。
"""
import argparse
import concurrent.futures
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                   # noqa: E402
from src.scholar.notes_index import (                                      # noqa: E402
    INDEX_JSON, load_csl_items, _match_csl, update_index, write_outputs)
from src.scholar._citekey_utils import csl_names, date_parts               # noqa: E402
from src.scholar.translation_server import (                               # noqa: E402
    resolve_identifier, _creators_to_authors, _parse_date, is_available, DEFAULT_BASE_URL)
from src.scholar.fulltext import ipv4_client                               # noqa: E402

FIELDS = ("authors", "journal", "volume", "issue", "pages", "issued")
HEADING_RE_TMPL = r"^## .*\[@{}\]\s*$"
AUTHORS_LINE = "**作者**: "
JOURNAL_LINE = "**期刊/来源**: "
ALLOWED_TYPES = {"journalArticle", "preprint", "conferencePaper", "bookSection", "report", "thesis", "book"}
ARXIV_NEW_ID = re.compile(r"^(\d{2})(\d{2})\.\d{4,5}$")


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


def _match_bundle(entry: Dict[str, Any], notes_dir: Path) -> Optional[Path]:
    mdir = notes_dir / "manual" / (entry.get("month") or "")
    if not mdir.is_dir():
        return None
    doi = (entry.get("doi") or "").strip().lower()
    title = _norm(entry.get("title"))
    for bf in sorted(mdir.glob("*.paper.json")):
        try:
            meta = ((json.loads(bf.read_text(encoding="utf-8")).get("segment") or {})
                    .get("metadata")) or {}
        except Exception:
            continue
        if doi and (meta.get("doi") or "").strip().lower() == doi:
            return bf
        if not doi and title and _norm(meta.get("title")) == title:
            return bf
    return None


# ---------------- 解析 ----------------

def _arxiv_bare(arxiv_id: Optional[str]) -> Optional[str]:
    """去掉 arXiv: 前缀与版本后缀：TS 按版本查回的是那个版本的日期，我们要的是这篇论文。"""
    a = (arxiv_id or "").strip()
    if not a:
        return None
    a = re.sub(r"^arxiv:", "", a, flags=re.I)
    return re.sub(r"v\d+$", "", a, flags=re.I)


def _arxiv_first_month(arxiv_id: Optional[str]) -> Optional[date]:
    """新式 arXiv 号 YYMM.NNNNN 编码首发年月；旧式（math/0501001）返回 None。"""
    m = ARXIV_NEW_ID.match(_arxiv_bare(arxiv_id) or "")
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None


def _identifier(entry: Dict[str, Any], bundle_meta: Optional[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """返回 (送 TS 的标识符, 类型 doi/arxiv/pmid/none)。"""
    if entry.get("doi"):
        return entry["doi"].strip(), "doi"
    if entry.get("arxiv_id"):
        return "arXiv:{}".format(_arxiv_bare(entry["arxiv_id"])), "arxiv"
    pmid = (bundle_meta or {}).get("pmid")
    return (str(pmid).strip(), "pmid") if pmid else (None, "none")


def _resolve_all(idents: List[str], base_url: str, workers: int) -> Dict[str, Optional[Dict[str, Any]]]:
    idents = sorted(set(idents))
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    if not idents:
        return out
    chunks = [idents[i::workers] for i in range(min(workers, len(idents)))]

    def _run(batch):
        res = {}
        with ipv4_client(timeout=40) as c:
            for ident in batch:
                items = resolve_identifier(ident, base_url=base_url, client=c)
                res[ident] = items[0] if items else None
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chunks)) as ex:
        for r in ex.map(_run, chunks):
            out.update(r)
    # 并发下 doi.org/Crossref 会限流，TS 把上游失败一律包成 501——实测 45 个 501 里
    # DOI 类串行重试全部 200。失败项串行慢速再走一遍。
    failed = [k for k, v in out.items() if v is None]
    if failed:
        with ipv4_client(timeout=40) as c:
            for ident in failed:
                time.sleep(0.6)
                items = resolve_identifier(ident, base_url=base_url, client=c)
                if items:
                    out[ident] = items[0]
        print("重试 {} 个失败标识符，追回 {} 个".format(
            len(failed), sum(1 for k in failed if out.get(k))))
    return out


def _ts_issued(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """TS 的 date → {date, precision}，保留日精度（_parse_date 会把日抹成 1）。"""
    raw = (item.get("date") or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        try:
            return {"date": date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "precision": "day"}
        except ValueError:
            pass
    d = _parse_date(raw)
    if not d:
        return None
    toks = [t for t in re.findall(r"\d+", raw) if t != str(d.year)]
    return {"date": d, "precision": "month" if any(1 <= int(t) <= 12 for t in toks) else "year"}


def _propose(entry: Dict[str, Any], side: Optional[Dict[str, Any]], csl: Optional[Dict[str, Any]],
             item: Dict[str, Any], ident_kind: str, allow_shrink: bool) -> Tuple[Dict[str, Any], List[str]]:
    """算出要改的字段 → {field: new}，并返回被拒绝/存疑的说明。"""
    changes: Dict[str, Any] = {}
    notes: List[str] = []
    csl = csl or {}
    itype = item.get("itemType")
    if itype not in ALLOWED_TYPES:
        return {}, ["itemType={} 非论文条目，整条跳过".format(itype)]

    cur_authors = (side or {}).get("authors") or entry.get("authors") or []
    cur_journal = (side or {}).get("journal") or entry.get("journal") or ""

    new_authors = _creators_to_authors(item.get("creators", []))
    if new_authors and _norm_list(new_authors) != _norm_list(cur_authors):
        if len(new_authors) < len(cur_authors) and not allow_shrink:
            notes.append("authors-shrink {}→{} 拒绝".format(len(cur_authors), len(new_authors)))
        elif cur_authors and not (_families(new_authors) & _families(cur_authors)):
            notes.append("authors-disjoint 新旧姓氏无交集 拒绝: {} → {}".format(cur_authors[:3], new_authors[:3]))
        else:
            changes["authors"] = new_authors

    ts_doi = (item.get("DOI") or "").strip()
    journal_ok = True
    if ident_kind == "arxiv" and ts_doi and not ts_doi.lower().startswith("10.48550/"):
        journal_ok = False
        # arXiv 翻译器解析到的正刊 DOI 可能是**另一篇**（实测 2404.11171 被配到 IJCAI 的
        # Lorello 等人论文）：姓氏无交集就标「疑似误配」，升级身份键前必须人工核对
        overlap = bool(_families(new_authors) & _families(cur_authors)) if (new_authors and cur_authors) else None
        tag = "疑似误配（TS 作者与现有作者姓氏无交集）" if overlap is False else "作者对得上" if overlap else "作者无法比对"
        notes.append("doi_candidate={}（arXiv 号解析到正刊，刊名卷期页不写；{}；身份键升级需人工）".format(ts_doi, tag))
        if overlap is False:
            # 误配的记录连作者也不能信
            changes.pop("authors", None)
    if journal_ok:
        pub = (item.get("publicationTitle") or "").strip()
        if pub and _norm(pub) != _norm(cur_journal):
            changes["journal"] = pub
        for src, dst, cur_key in (("volume", "volume", "volume"), ("issue", "issue", "issue"),
                                  ("pages", "pages", "page")):
            v = (item.get(src) or "").strip()
            if v and _norm(v) != _norm(csl.get(cur_key)):
                changes[dst] = v

    cur_issued = csl.get("issued") or {}
    cur_year = None
    try:
        cur_year = int(cur_issued["date-parts"][0][0])
    except Exception:
        pass
    if ident_kind == "arxiv" and not journal_ok:
        pass                                    # 实质是正刊：预印本号段的月份不是它的出版日期，留给人判
    elif ident_kind == "arxiv":
        d = _arxiv_first_month(entry.get("arxiv_id"))
        if d and (cur_year is None or cur_year != d.year):
            changes["issued"] = {"date": d, "precision": "month", "force": True}
            if cur_year is not None:
                notes.append("issued 年份 {} 与 arXiv 号段 {} 不符，按号段改".format(cur_year, d.year))
    elif cur_year is None:
        iss = _ts_issued(item)
        if iss:
            iss["force"] = False
            changes["issued"] = iss
    return changes, notes


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


# ---------------- 主流程 ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="translation-server 存量元数据重对齐")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--month-prefix", required=True, help="索引 month 前缀，如 2026-08")
    ap.add_argument("--series", default="auto,manual", help="逗号分隔；book 不支持")
    ap.add_argument("--ts-url", default=os.environ.get("PROCESSING__ZOTERO_TRANSLATION_SERVER_URL",
                                                       DEFAULT_BASE_URL))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--allow-author-shrink", action="store_true",
                    help="允许 TS 作者表比现有更短时覆盖（默认拒绝；姓氏无交集仍拒绝）")
    ap.add_argument("--backup-dir", default="", help="默认 <notes-dir>/_archive/realign_ts_<时间戳>/")
    ap.add_argument("--report", default="", help="把逐条变更 + unresolved 写成 JSON 到此路径")
    ap.add_argument("--show", type=int, default=40, help="终端最多列出多少条作者/期刊/日期变更")
    ap.add_argument("--no-index", action="store_true", help="--apply 后不刷索引（调试用）")
    args = ap.parse_args()

    notes_dir = repo_path(args.notes_dir)
    series_ok = {s.strip() for s in args.series.split(",") if s.strip()}
    if "book" in series_ok:
        print("⛔ book 系列（ISBN 章条目）不在本脚本范围", file=sys.stderr)
        return 2
    if not is_available(args.ts_url):
        print("⛔ translation-server 不在线：{}（docker ps 看 zotero-translation-server）".format(args.ts_url),
              file=sys.stderr)
        return 2

    idx = json.loads((notes_dir / INDEX_JSON).read_text(encoding="utf-8"))
    entries = [p for p in idx.get("papers", [])
               if str(p.get("month", "")).startswith(args.month_prefix)
               and p.get("series") in series_ok and p.get("note_file")]
    print("范围：month 前缀 {} · series {} · {} 条（{} 个札记文件）".format(
        args.month_prefix, "/".join(sorted(series_ok)), len(entries),
        len({p["note_file"] for p in entries})))

    # 1) 收集标识符（manual 的 pmid 要看 bundle）
    bundle_path: Dict[int, Optional[Path]] = {}
    bundle_meta: Dict[int, Dict[str, Any]] = {}
    for i, e in enumerate(entries):
        if e.get("series") == "manual":
            bp = _match_bundle(e, notes_dir)
            bundle_path[i] = bp
            if bp:
                bundle_meta[i] = (json.loads(bp.read_text(encoding="utf-8")).get("segment") or {}) \
                    .get("metadata") or {}
    idents: Dict[int, Tuple[Optional[str], str]] = {i: _identifier(e, bundle_meta.get(i))
                                                    for i, e in enumerate(entries)}
    n_ident = sum(1 for v, _ in idents.values() if v)
    print("有 DOI/arXiv/PMID 标识符：{} 条；无标识符（TS 帮不上）：{} 条".format(
        n_ident, len(entries) - n_ident))

    # 2) 解析
    resolved = _resolve_all([v for v, _ in idents.values() if v], args.ts_url, args.workers)
    unresolved = sorted({v for v, _ in idents.values() if v and not resolved.get(v)})
    n_hit = n_ident - len(unresolved)
    print("translation-server 命中：{}/{}；未解析 {} 个（见报告 unresolved，下轮会再试）".format(
        n_hit, n_ident, len(unresolved)))

    # 3) 按文件分组 → 提案
    by_file: Dict[str, List[int]] = {}
    for i, e in enumerate(entries):
        by_file.setdefault(e["note_file"], []).append(i)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    backup = Backup(Path(args.backup_dir) if args.backup_dir
                    else notes_dir / "_archive" / "realign_ts_{}".format(stamp), notes_dir)
    report: List[Dict[str, Any]] = []
    field_tot = {f: 0 for f in FIELDS}
    n_changed_entries = 0
    rejected: List[str] = []
    unmatched: List[str] = []
    printed = 0

    for fname in sorted(by_file):
        md_path = notes_dir / fname
        stem = md_path.name[:-3]
        side_path = notes_dir / "{}.index.json".format(stem)
        ref_path = notes_dir / "{}.references.json".format(stem)
        lines = md_path.read_text(encoding="utf-8").split("\n") if md_path.exists() else None
        side = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else None
        refs = load_csl_items(ref_path) if ref_path.exists() else []
        touched = {"md": False, "side": False, "refs": False}
        bundles_touched: Dict[Path, Dict[str, Any]] = {}
        file_changes = 0

        for i in by_file[fname]:
            e = entries[i]
            ident, kind = idents[i]
            item = resolved.get(ident) if ident else None
            if not item:
                continue
            row = _match_sidecar(e, (side or {}).get("papers", [])) if side else None
            csl = _match_csl(e, refs)
            ch, notes = _propose(e, row, csl, item, kind, args.allow_author_shrink)
            for n in notes:
                rejected.append("{} [{}] {}".format(fname, e["citekey"], n))
            if not ch:
                continue
            file_changes += 1
            n_changed_entries += 1
            for f in ch:
                field_tot[f] += 1
            rec = {"note_file": fname, "citekey": e["citekey"], "series": e.get("series"),
                   "ident": ident, "notes": notes,
                   "before": {"authors": (row or {}).get("authors") or e.get("authors"),
                              "journal": (row or {}).get("journal") or e.get("journal"),
                              "volume": (csl or {}).get("volume"), "issue": (csl or {}).get("issue"),
                              "page": (csl or {}).get("page"), "issued": (csl or {}).get("issued")},
                   "after": {k: (v if k != "issued" else
                                 {"date": v["date"].isoformat(), "precision": v["precision"]})
                             for k, v in ch.items()}}
            report.append(rec)
            if ("authors" in ch or "journal" in ch or "issued" in ch) and printed < args.show:
                printed += 1
                print("  · {} [{}]".format(fname.replace("科研札记_", ""), e["citekey"]))
                if "authors" in ch:
                    print("      作者 {} → {}".format(rec["before"]["authors"], ch["authors"]))
                if "journal" in ch:
                    print("      期刊 「{}」 → 「{}」".format(rec["before"]["journal"], ch["journal"]))
                if "issued" in ch:
                    print("      日期 {} → {}".format(rec["before"]["issued"], rec["after"]["issued"]))

            if not args.apply:
                continue
            if lines is not None:
                md_state = _apply_md(lines, e, ch)
                if md_state:
                    touched["md"] = True
                elif md_state is None and ("authors" in ch or "journal" in ch):
                    unmatched.append("{} [{}] md 段落未定位".format(fname, e["citekey"]))
            if row is not None:
                if _apply_sidecar(row, ch):
                    touched["side"] = True
            elif side is not None and ("authors" in ch or "journal" in ch or "issued" in ch):
                unmatched.append("{} [{}] sidecar 条目未匹配".format(fname, e["citekey"]))
            if csl is not None:
                _apply_csl(csl, ch)
                touched["refs"] = True
            else:
                unmatched.append("{} [{}] CSL 条目未匹配".format(fname, e["citekey"]))
            if e.get("series") == "manual":
                bp = bundle_path.get(i)
                if bp:
                    data = bundles_touched.get(bp) or json.loads(bp.read_text(encoding="utf-8"))
                    _apply_bundle(data, ch)
                    bundles_touched[bp] = data
                else:
                    unmatched.append("{} [{}] bundle 未匹配（下次 regen 会顶回）".format(fname, e["citekey"]))

        if args.apply and file_changes:
            if touched["md"]:
                backup.save(md_path)
                tmp = md_path.with_suffix(".md.tmp")
                tmp.write_text("\n".join(lines), encoding="utf-8")
                tmp.replace(md_path)
            if touched["side"]:
                style = _json_style(side_path)
                backup.save(side_path)
                _write_json(side_path, side, style)
            if touched["refs"]:
                style = _json_style(ref_path)
                backup.save(ref_path)
                _write_json(ref_path, refs, style)
            for bp, data in bundles_touched.items():
                style = _json_style(bp)
                backup.save(bp)
                _write_json(bp, data, style)
        print("{} {}：{} 条有变更".format("✍️" if args.apply else "·", fname, file_changes))

    print("\n==== 汇总 ====")
    print("变更条目 {} / TS 命中 {} / 有标识符 {} / 范围 {}".format(
        n_changed_entries, n_hit, n_ident, len(entries)))
    print("按字段：" + " · ".join("{} {}".format(k, v) for k, v in field_tot.items()))
    if rejected:
        print("拒绝/存疑（{}）：".format(len(rejected)))
        for r in rejected[:30]:
            print("  ⚠️ " + r)
    if unresolved:
        print("未解析（{}）：{}{}".format(len(unresolved), ", ".join(unresolved[:8]),
                                      " …" if len(unresolved) > 8 else ""))
    if unmatched:
        print("写盘未落到位（{}）：".format(len(unmatched)))
        for u in unmatched[:20]:
            print("  ⛔ " + u)
    if args.report:
        Path(args.report).write_text(json.dumps(
            {"changes": report, "rejected": rejected, "unresolved": unresolved, "unmatched": unmatched},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print("逐条报告：{}".format(args.report))
    if args.apply:
        print("备份：{}（{} 个文件）".format(backup.root, len(backup.done)))
        if n_changed_entries and not args.no_index:
            # 增量模式只认 md mtime，只改了 sidecar/CSL 的文件不会被重扫——按前缀强扫
            mp = args.month_prefix[:7]
            idx2 = update_index(notes_dir, since=mp, until=mp)
            wrote = write_outputs(idx2, notes_dir)
            print("索引已按 --since/--until {} 强扫：写盘 {}".format(
                mp, ", ".join(k for k, v in wrote.items() if v) or "无变化"))
        print("vault：launchd 的 scholar-vault 会跟进，或手跑 "
              "PYTHONPATH=. python3 scripts/sync_vault.py --vault-dir ~/Documents/ScholarVault")
    else:
        print("（dry-run，未写盘；加 --apply 执行）")
    print("完成判据：变更 0 且 unresolved 为空（当前 unresolved={}）".format(len(unresolved)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
