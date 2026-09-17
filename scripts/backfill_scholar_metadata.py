#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量回填：用札记里原样保存的 Google Scholar 推送片段，修正 Scholar 解析留下的残缺元数据。

## 背景（2026-09-16）
旧的 Scholar 推送解析器（`paper_extractor`，已修）用无分隔符的 `get_text()`：标题与作者行粘连
丢首作者、年份后粘着摘要让 `\\b` 失效、单作者 "F Amrollahi - 2023" 不匹配作者正则。于是库里有
空作者 + `anon*` 兜底键、作者带「…」、首作者缺失（citekey 挂到第二作者名下）、年份退成邮件年份。

auto 札记的「### 摘要」字段原样存着推送片段：`[PDF] <标题> <作者> - <期刊>, <年> <摘要>`，
用修好后的 `ScholarEmailParser._parse_meta_line` 重解析即可，**零网络、零额度**。

## 只改什么（守卫）
- 作者：当前为空 / 首作者是标题残片或邮件样板 / Scholar 缩写式且丢了首作者 → 换成片段作者。
  当前已是全名（Crossref / translation-server 补过）的不动；只是去掉「…」、首作者不变的不动
  （去掉截断标记会让残表看不出是残表）；缩写式要求旧作者姓氏 ⊆ 新作者姓氏、新表不比旧表短。
- 片段形态：「作者 - 期刊, 年」可信；无「 - 」的作者单行只认 ≥2 位作者（单个「缩写 + 一词」
  和摘要首词分不开：实测 "R Login"、"G Fonarow"）；姓落在 SURNAME_STOP（Login/Center…）的整条弃用。
- 年份：仅无 DOI/arXiv 的条目，且当前年份缺失或恰等于入库月份年（邮件年份兜底的特征）时改；差距 >3 年拒绝。
- **不改 title / DOI / arxiv_id**（dedup_key 不变）；**不往 md 插行**（不打乱 topics 的 `文件.md:行号` 锚点）；
  md 里定位不到标题的条目整条跳过（不单写 CSL/sidecar）。只处理 auto 系列。

## 预演
dry-run 与 --apply 都先在临时副本上完整跑一遍写回 + 全量重建索引：改掉残片作者会让「首作者姓不同
就不合并」的守卫失效，标题相似度层会**新合并**若干同文（2026-09-16 R1 对抗审计实测 4 组）。
新合并的条目单独列出、不改键；--apply 时实际合并集合与预演不一致就中止改键。

## citekey
仅当现键本身是兜底键形态（旧元数据能生成，允许 b/c 消歧后缀）才改；新键撞了别的论文加后缀，
撞了标题相近的另一身份（疑似同文未合并）则不改。计划在写回**之前**落盘到 `_rekey/pending_*.json`，
中途失败用 `--resume` 续跑（重跑时写回已无差异，算不出旧键）。改名走 `_rename_citekey_in_note`
（三处原子）+ `announce_rekey_side_effects(old_keys_retired=True)`（向量库、topics/qa 页）；
keeper 被改名后挂着旧键的 duplicate 行内键跟随改；按 citekey 记账的
`backfill_deepread_progress.json` / `backfill_expand_progress.json` / `需下载全文清单.md` 跟随改。
旧键→新键对照表写到 `_rekey/scholar_metadata_<时间戳>.{json,md}`；`~/Desktop/Lab` 里还引用旧键的
稿件、以旧键命名的文件只报告不改。改前备份：三处文件 + literature_index / all_references / INDEX.md /
topics / qa / 账本 → `_archive/scholar_meta_<时间戳>/`。

## 用法
    PYTHONPATH=. python3 scripts/backfill_scholar_metadata.py                  # dry-run（含预演）
    PYTHONPATH=. python3 scripts/backfill_scholar_metadata.py --report r.json  # 逐条报告
    PYTHONPATH=. python3 scripts/backfill_scholar_metadata.py --apply          # 写盘 + 改键
    PYTHONPATH=. python3 scripts/backfill_scholar_metadata.py --resume output/scholar_notes/_rekey/pending_….json

退出码：0 正常 / 1 有改键被拒或半改、或向量库同步失败。
"""
import argparse
import json
import re
import os
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                        # noqa: E402
from src.scholar.paper_extractor import ScholarEmailParser                       # noqa: E402
from src.scholar._citekey_utils import _fallback_citekey, _suffix_seq            # noqa: E402
from src.scholar.crossref import title_similarity                                # noqa: E402
from src.scholar.notes_index import (                                            # noqa: E402
    load_csl_items, _match_csl, update_index, write_outputs, _rename_citekey_in_note,
    RENAME_OK, RENAME_PARTIAL, announce_rekey_side_effects)
from src.scholar.metadata_writeback import (                                     # noqa: E402
    Backup, _apply_csl, _apply_md, _apply_sidecar, _json_style, _match_sidecar, _write_json)

P = ScholarEmailParser
VALID_CITEKEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.#$%&+?<>~/-]*$")    # 同 audit_citekeys_vs_pmlr
TYPE_TAG_RE = re.compile(r"^\s*\[(?:PDF|HTML|BOOK|B|CITATION|C)\]\s*", re.I)
ABBREV_RE = re.compile(r"^(?:[A-Z]{1,4}\s+\S|[一-鿿])")                 # Scholar 缩写式 / 中文名
NAME_START_RE = re.compile(r"^[A-ZÀ-ɏͰ-ӿ一-鿿]")
ONLY_AUTHOR_RE = re.compile(r"^[A-Z]{1,4}\s+[^\W\d_][\w'’.-]*", re.U)
MAX_YEAR_JUMP = 3
# 单行形态里像姓、其实是网页控件/摘要小标题的词（R1 审计实测 "R Login"、"AUC Center"）
SURNAME_STOP = {"login", "center", "background", "abstract", "objective", "objectives", "introduction",
                "methods", "results", "purpose", "aims", "summary", "download", "pdf", "html", "view"}
SUSPECT_DUP_SIM = 0.6        # 新基键被另一身份占着时，标题词重合达到此值即视为疑似同文、不改键


def _norm(s: Optional[str]) -> str:
    # NFC 而非 NFKC：NFKC 会把「…」(U+2026) 拆成 "..."，截断判定就认不出了
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()


def _family(name: str) -> str:
    n = unicodedata.normalize("NFKD", _norm(name).rstrip("…").strip())
    parts = n.split()
    last = parts[-1] if parts else ""
    return "".join(c for c in last if c.isalnum() and not unicodedata.combining(c)).lower()


# ---------------- 纯函数：可单测 ----------------

def meta_from_scholar_snippet(abstract: str, title: str) -> Optional[Dict[str, Any]]:
    """札记摘要字段里的 Scholar 片段 → {"authors", "venue", "year"}；不是 Scholar 片段返回 None。

    判定：去掉 [PDF] 等类型标签后必须以本条目标题开头（防止把 PubMed/arXiv 摘要误当片段）。
    两种形态：
      "<标题> A, B, … - Venue, 2024 <摘要>"   → 截出 "A, B, … - Venue, 2024" 交给 _parse_meta_line
      "<标题> A, B, C <摘要>"                 → 无 " - " 的作者单行；首个作者须是缩写式，
                                                末个 token 会粘上摘要首句，只保留「缩写 + 一个词」，
                                                多作者时干脆丢掉这个被截的末位（宁缺不错）
    """
    text = TYPE_TAG_RE.sub("", _norm(abstract))
    head = _norm(title).rstrip("…").strip()
    if not head or not text.lower().startswith(head.lower()):
        return None
    # 标题不足 3 个词多半是解析残条或干脆是人名（R1 审计实测 anon2024Mohsen 的「标题」是首作者
    # "Mohsen Bakouri"），拿它截出来的作者表首位必错
    if len(head.split()) < 3:
        return None
    rest = text[len(head):].lstrip(" …")
    m = re.match(r"^(.{2,400}?)\s[-–—]\s(.*)$", rest)
    if m and not re.search(r"\s[-–—]\s", m.group(1)):
        tail = m.group(2)
        y = re.match(r"^((?:19|20)\d{2})\b", tail)
        if y:
            line = "{} - {}".format(m.group(1), y.group(1))
        else:
            vy = re.match(r"^(.{1,200}?,\s*(?:19|20)\d{2})\b", tail)
            line = "{} - {}".format(m.group(1), vy.group(1) if vy else "")
        authors, venue, year = P._parse_meta_line(line.rstrip(" -"), authors_only_ok=True)
        if authors and ABBREV_RE.match(authors[0]) and not _glued(authors) \
                and not any(_family(a) in SURNAME_STOP for a in authors):
            truncated = "…" in m.group(1) or "..." in m.group(1)
            return {"authors": _mark_truncated(authors, truncated), "venue": venue, "year": year, "form": "dash"}
        return None
    toks = [t.strip() for t in re.split(r"[,，]", rest)]
    out: List[str] = []
    truncated = False
    for i, tk in enumerate(toks):
        if "…" in tk:
            # "M Zhang… ABSTRACT In National…"：「…」之前是被 Scholar 截断的末位作者，之后是摘要
            clean = tk.split("…", 1)[0].strip()
            if ONLY_AUTHOR_RE.fullmatch(clean) and len(clean.split()) <= 4:
                out.append(clean)
            truncated = True
            break
        mm = ONLY_AUTHOR_RE.match(tk)
        if not mm:
            break
        if mm.group(0) == tk and len(tk.split()) <= 4:
            out.append(tk)
            continue
        truncated = True                  # 末位粘着摘要首句（"J Hayes We consider…"）：无法切准，丢掉并标截断
        break
    # 单行形态只认 ≥2 位作者：单个「缩写 + 一词」无法和摘要首词区分——实测 "R Login Persistent
    # symptoms…"（Login 是网页登录按钮）、"… G Fonarow, AUC Center Background…"（Fonarow 实为末位作者），
    # 两条都会造出错误的 citekey（R1 对抗审计）
    out = [a for a in out if _family(a) not in SURNAME_STOP]
    if len(out) >= 2 and ABBREV_RE.match(out[0]) and not _glued(out):
        return {"authors": _mark_truncated(out, truncated), "venue": None, "year": None, "form": "authors-only"}
    return None


def _mark_truncated(authors: List[str], truncated: bool) -> List[str]:
    """片段作者表被 Scholar 截断（或末位被丢）时，给末位作者保留「…」——与库里既有口径一致
    （"WW Stead…"），否则写回后残表看不出是残表（R2 审计）。"""
    out = [a.rstrip("…").strip() for a in authors]
    if truncated and out:
        out[-1] = out[-1] + "…"
    return out


GLUED_NAME_RE = re.compile(r"[a-z]{4,}[A-Z][a-z]{3,}")


def _glued(authors: List[str]) -> bool:
    """两个人名被 Scholar 截断后粘成一个姓（R1 审计实测 "R PisaniAnthony" = Pisani + Anthony）。
    两侧各 ≥4 个小写字母才算，避开 McDonald / MacKenzie / DeLong 这类真姓。"""
    return any(GLUED_NAME_RE.search(a.split()[-1] if a.split() else "") for a in authors)


def _garbage(authors: List[str]) -> bool:
    """标题残片 / 摘要句子被当成作者（"properties such as portability"）。

    只看**首作者**：全名作者表里夹一个团体署名（"for the Alzheimer's Disease Neuroimaging
    Initiative"）或多缩写长名（"Ana Helena D. P. S. Ulbrich"）很常见，那是 Crossref 的正确数据，
    按「任一 token 可疑」判会把整表换成 Scholar 缩写（dry-run 实测误伤 10 条）。
    """
    first = next((a.rstrip("…").strip() for a in authors if a.rstrip("…").strip()), "")
    if not first:
        return False
    # 单个词（非中文名）也算：邮件样板 "FAMIA, FIAHSI, FACMI"（订阅者头衔）首字母大写能过 NAME_START_RE
    single_word = len(first.split()) == 1 and not re.match(r"^[一-鿿]", first)
    return (not NAME_START_RE.match(first) or any(ch.isdigit() for ch in first)
            or len(first.split()) > 6 or single_word)


def propose(entry: Dict[str, Any], snip: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """返回 (变更 ch, 说明/拒绝理由)。ch 形状同 metadata_writeback（authors / issued）。"""
    ch: Dict[str, Any] = {}
    notes: List[str] = []
    cur = [a for a in (entry.get("authors") or []) if (a or "").strip()]
    new = list(snip.get("authors") or [])
    if new:
        cur_clean = [a.rstrip("…").strip() for a in cur if a.rstrip("…").strip()]
        if not cur:
            ch["authors"] = new
            notes.append("空作者 → 片段作者")
        elif _garbage(cur):
            ch["authors"] = new
            notes.append("作者是标题/摘要残片 → 片段作者")
        elif all(ABBREV_RE.match(a) for a in cur_clean):
            had_ellipsis = any("…" in a for a in cur)
            fam_new = [_family(a) for a in new]
            fam_cur = [_family(a) for a in cur_clean]
            if not had_ellipsis and fam_cur and fam_new and fam_cur[0] == fam_new[0]:
                # 首作者对得上、也没截断：现表可能比片段更全（"M Shoufie Ukhtary" vs "MS Ukhtary"），不降级
                pass
            elif not set(fam_cur) <= set(fam_new) or len(new) < len(cur_clean):
                notes.append("拒绝：缩写作者与片段作者对不上（现 {} / 片段 {}）".format(cur[:3], new[:3]))
            elif fam_cur and fam_new[0] != fam_cur[0]:
                ch["authors"] = new
                notes.append("首作者缺失 → 补回")
            # 只是去掉「…」、首作者不变：不改——截断标记一去掉，残表就看不出是残表了（R1 审计），
            # 对 citekey 也没有影响
        # 其余（全名，来自 Crossref/TS）不动
    year = snip.get("year")
    if year and not entry.get("doi") and not entry.get("arxiv_id"):
        cur_year = entry.get("year")
        try:
            month_year = int(str(entry.get("month") or "")[:4])
        except ValueError:
            month_year = None
        if cur_year == year:
            pass
        elif cur_year is not None and cur_year != month_year:
            notes.append("年份不改：现 {} 不是入库月份年，不像邮件兜底（片段 {}）".format(cur_year, year))
            ch["_year_doubt"] = True
        elif cur_year is not None and abs(cur_year - year) > MAX_YEAR_JUMP:
            notes.append("拒绝：片段年份 {} 与现 {} 相差 >{} 年".format(year, cur_year, MAX_YEAR_JUMP))
            ch["_year_doubt"] = True
        else:
            ch["issued"] = {"date": date(year, 1, 1), "precision": "year", "force": True}
            notes.append("年份 {} → {}（邮件年份兜底 → 推送元数据行）".format(cur_year, year))
    return ch, notes


def _meta_ns(entry: Dict[str, Any], authors: List[str], year: Optional[int]) -> SimpleNamespace:
    return SimpleNamespace(authors=authors, publication_date=date(year, 1, 1) if year else None,
                           email_received_at=None, title=entry.get("title") or "",
                           doi=entry.get("doi"), paper_id=entry.get("dedup_key") or "")


def is_generated_key(entry: Dict[str, Any]) -> bool:
    """现 citekey 是否由旧元数据经 _fallback_citekey 生成（可带 b/c… 消歧后缀）。"""
    ck = entry.get("citekey") or ""
    base = _fallback_citekey(_meta_ns(entry, list(entry.get("authors") or []), entry.get("year")))
    if not base or not ck.startswith(base):
        return False
    return re.fullmatch(r"[b-z]{0,3}", ck[len(base):]) is not None


def _title_key(t: Optional[str]) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower()).rstrip("…")
    return re.sub(r"[^a-z0-9]+", "", t)


def plan_new_key(entry: Dict[str, Any], ch: Dict[str, Any], owners: Dict[str, set],
                 titles: Optional[Dict[str, List[str]]] = None) -> Tuple[Optional[str], str]:
    """按修正后的元数据重算键；撞了**别的论文**（不同 dedup_key 的 live 条目）就加后缀。

    返回 (新键|None, 不改键的原因)。基键被另一个 dedup_key 占着、而对方标题与本条相同时，
    多半是同一篇论文被拆成了两个身份（一边有 DOI、一边只有标题键）——加后缀只会再造一个键，
    应先合并身份，故不改键、交人看。
    """
    authors = ch.get("authors") or list(entry.get("authors") or [])
    year = ch["issued"]["date"].year if "issued" in ch else entry.get("year")
    base = _fallback_citekey(_meta_ns(entry, authors, year))
    if not base or not VALID_CITEKEY_RE.match(base):
        return None, "新键 {} 含非 ASCII 字符或为空（pandoc 口径同 audit_citekeys_vs_pmlr），不改键".format(base)
    dk = entry.get("dedup_key") or ""
    others = owners.get(base, set()) - {dk}
    if not others:
        return base, ""
    mine = _title_key(entry.get("title"))
    for other in sorted(others):
        for ot in (titles or {}).get(other, []):
            otk = _title_key(ot)
            # 前缀（截断标题）或词重合 ≥ SUSPECT_DUP_SIM（Scholar 与出版方标题措辞略有出入，
            # 实测 "…NEXT-GENERATION PHENOTYPE LIBRARIES" vs "…NEXT-GENERATION ELECTRONIC HEALTH
            # RECORD PHENOTYPE LIBRARIES" 为 0.75，同一篇 GigaScience 2021）
            if (mine and otk and (mine.startswith(otk) or otk.startswith(mine))) \
                    or title_similarity(entry.get("title") or "", ot) >= SUSPECT_DUP_SIM:
                return None, "基键 {} 属于标题相近的另一身份 {}（「{}」），疑似同文未合并，先合并再改键".format(
                    base, other, (ot or "")[:60])
    for suf in _suffix_seq():
        if not (owners.get(base + suf, set()) - {dk}):
            return base + suf, ""
    return None, "后缀用尽"


def section_abstract(lines: List[str], heading_idx: int) -> Optional[str]:
    """标题行下标 → 该节「### 摘要」后第一行非空文本。"""
    for j in range(heading_idx + 1, min(heading_idx + 60, len(lines))):
        if lines[j].startswith("## "):
            return None
        if lines[j].strip() == "### 摘要":
            for k in range(j + 1, min(j + 6, len(lines))):
                if lines[k].strip():
                    return lines[k]
            return None
    return None


# ---------------- 主流程 ----------------

def _targets(index: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in index.get("papers", [])
            if e.get("series") == "auto" and not e.get("duplicate_of") and not e.get("retracted")
            and e.get("note_file") and e.get("note_line")]


def collect(notes_dir: Path, index: Dict[str, Any]) -> List[Dict[str, Any]]:
    """逐条目：读片段 → propose。返回有变更或有拒绝理由的记录。"""
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for e in _targets(index):
        by_file.setdefault(e["note_file"], []).append(e)
    out = []
    for fname, entries in sorted(by_file.items()):
        md = notes_dir / fname
        if not md.exists():
            continue
        lines = md.read_text(encoding="utf-8").split("\n")
        for e in entries:
            abstract = section_abstract(lines, int(e["note_line"]) - 1)
            snip = meta_from_scholar_snippet(abstract or "", e.get("title") or "") if abstract else None
            if not snip:
                continue
            ch, notes = propose(e, snip)
            year_doubt = bool(ch.pop("_year_doubt", False))
            if ch or notes:
                out.append({"entry": e, "snippet": snip, "ch": ch, "notes": notes, "year_doubt": year_doubt})
    return out


def apply_changes(notes_dir: Path, recs: List[Dict[str, Any]], backup: Backup) -> List[str]:
    """三处同步写回（md 作者行 / sidecar / CSL）。返回没落到位的清单。

    - **不往 md 插行**：原本没有 `**作者**:` 行的条目只写 CSL/sidecar（索引与书目读它们）。
      插行会让同文件后续各节整体下移，topics/qa 页里 `文件.md:行号` 锚点随之错位（R1 审计实测 93 处）。
    - md 里连标题都定位不到的条目**整条跳过**，不单写 CSL/sidecar——否则三处不一致，
      而索引优先读 sidecar，这种不一致看不出来（R1 审计）。
    """
    from src.scholar.metadata_writeback import _find_heading, _find_line, _section_end, AUTHORS_LINE
    unmatched: List[str] = []
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for r in recs:
        if r["ch"]:
            by_file.setdefault(r["entry"]["note_file"], []).append(r)
    for fname, rs in sorted(by_file.items()):
        md_path = notes_dir / fname
        stem = md_path.name[:-3]
        side_path = notes_dir / "{}.index.json".format(stem)
        ref_path = notes_dir / "{}.references.json".format(stem)
        lines = md_path.read_text(encoding="utf-8").split("\n")
        side = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else None
        refs = load_csl_items(ref_path) if ref_path.exists() else []
        touched = {"md": False, "side": False, "refs": False}
        for r in rs:
            e, ch = r["entry"], r["ch"]
            h = _find_heading(lines, e["citekey"], e.get("note_line"))
            if h is None:
                unmatched.append("{} [{}] md 段落未定位，整条跳过".format(fname, e["citekey"]))
                r["skipped"] = True
                continue
            if "authors" in ch and _find_line(lines, h + 1, _section_end(lines, h), AUTHORS_LINE) is not None:
                if _apply_md(lines, e, {"authors": ch["authors"]}):
                    touched["md"] = True
            if side is not None:
                row = _match_sidecar(e, side.get("papers", []))
                if row is not None:
                    touched["side"] |= _apply_sidecar(row, ch)
                else:
                    unmatched.append("{} [{}] sidecar 条目未匹配".format(fname, e["citekey"]))
            csl = _match_csl(e, refs)
            if csl is not None:
                _apply_csl(csl, ch)
                touched["refs"] = True
            elif ref_path.exists():
                unmatched.append("{} [{}] CSL 条目未匹配".format(fname, e["citekey"]))
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
    return unmatched


def _entry_pos(e: Dict[str, Any]) -> Tuple[str, int]:
    """条目的稳定位置键：本脚本不插行，写回前后 (note_file, note_line) 不变。"""
    return (e.get("note_file") or "", int(e.get("note_line") or 0))


def simulate(notes_dir: Path, recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """在临时副本上完整跑一遍写回 + 全量重建索引，返回写回后的索引。

    必须预演而不是在原索引上推算：改掉残片作者会让「首作者姓不同就不合并」的守卫失效，
    重建时标题相似度层会**新合并**若干组同文（R1 审计实测 4 组），dry-run 看不出来。
    只复制 update_index 会读的顶层文件（md / references.json / index.json / dedup_overrides.json）。
    """
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="scholar_meta_preview_"))
    try:
        for f in notes_dir.iterdir():
            if f.is_file() and (f.suffix == ".md" or f.name.endswith((".references.json", ".index.json"))
                                or f.name == "dedup_overrides.json"):
                shutil.copy2(f, tmp / f.name)
        copies = [dict(r) for r in recs]
        apply_changes(tmp, copies, Backup(tmp / "_bk", tmp))
        for r, c in zip(recs, copies):          # 预演里定位不到的条目，计划阶段也要跳过（R2 审计）
            if c.get("skipped"):
                r["skipped"] = True
        return update_index(tmp, full=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def new_merges(before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    """写回前后 duplicate_of 发生变化的条目（新合并 / 换 keeper）。"""
    b = {_entry_pos(e): e for e in before.get("papers", [])}
    out = []
    for e in after.get("papers", []):
        old = b.get(_entry_pos(e))
        if old is not None and (old.get("duplicate_of") or None) != (e.get("duplicate_of") or None):
            out.append({"entry": e, "before": old.get("duplicate_of"), "after": e.get("duplicate_of")})
    return out


def _owners(index: Dict[str, Any]) -> Dict[str, set]:
    owners: Dict[str, set] = {}
    for e in index.get("papers", []):
        if not e.get("duplicate_of") and e.get("citekey"):
            owners.setdefault(e["citekey"], set()).add(e.get("dedup_key") or "")
    return owners


# 中文姓在前、姓全大写的写法（"LUO Yan"）：大写段若是合法拼音音节，首作者的「姓」其实在前面，
# 兜底键会错用名当姓（R2 审计 yan2021Interpretable）。要求 ≥3 个字母，避开 Scholar 的两字母缩写。
PINYIN_SYLLABLE_RE = re.compile(
    r"^(?:ZH|CH|SH|[BPMFDTNLGKHJQXRZCSYW])?"
    r"(?:IANG|IONG|UANG|ANG|ENG|ING|ONG|IAO|IAN|UAN|UAI|AI|EI|AO|OU|AN|EN|IN|UN|IA|IE|IU|UA|UO|UE|UI|A|E|I|O|U|V)R?$")


def surname_first_chinese(author: str) -> bool:
    m = re.match(r"^([A-Z]{3,6})\s+[A-Z][a-z]+$", (author or "").rstrip("…").strip())
    return bool(m and PINYIN_SYLLABLE_RE.match(m.group(1)))


def keys_in_tampered_topic_pages(notes_dir: Path) -> Dict[str, str]:
    """生成块被手改过（哈希对不上）的 topics/qa 页里引用的键 → 页名。

    改名工具会跳过这类页（不能洗掉「用户手改过」的冲突信号），于是那页上的旧键在改键后悬空
    （R2 审计实测 topics/adversarial-evidence.md 的 [@eilbeck2022Standard]）。计划阶段就把这些键
    排除：只修元数据、不改键，等页面重建后再处理。

    2026-09-17：正则加了 `(?:,[^\\]]*)?` 容忍书籍证据的页码定位符
    （`[@little2020rubin, p. 247]`，见 `topics._cite_str`）——原字符类
    `[^\\]\\s;,]` 一遇到逗号（定位符的起点）就把整个带定位符的引用判不出 citekey，
    等于书籍证据的引用在手改页里永远保护不到，改键计划仍会把它排进去、
    在这页上留下一个悬空引用，而这正是本函数存在的全部理由。"""
    from src.scholar.vault import generated_block_tampered
    out: Dict[str, str] = {}
    topics_dir = Path(notes_dir) / "topics"
    if not topics_dir.is_dir():
        return out
    for page in sorted(topics_dir.rglob("*.md")):
        try:
            text = page.read_text(encoding="utf-8")
        except OSError:
            continue
        if generated_block_tampered(text):
            for k in re.findall(r"\[@([^\]\s;,]+)(?:,[^\]]*)?\]", text):
                out.setdefault(k, str(page.relative_to(topics_dir)))
    return out


def plan_rekeys(recs: List[Dict[str, Any]], after: Dict[str, Any],
                protected: Optional[Dict[str, str]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """在**写回后**的索引上定改键计划。返回 ([{dedup_key, note_file, note_line, old, new, title}], 跳过说明)。

    - is_generated_key 用写回前的条目判（旧元数据才能生成旧键）；新键用写回后的元数据算。
    - 写回后变成 duplicate 的条目（新合并）不改键：它的行内键该由人确认合并后走
      `notes_index.py --fix-inline-citekeys`，不在本脚本里顺手改（R1 审计：顺手改会在同一份 md
      里造出两个同名标题、CSL 两条同 id，且这些键不进对照表）。
    """
    owners = _owners(after)
    titles: Dict[str, List[str]] = {}
    for x in after.get("papers", []):
        if not x.get("duplicate_of"):
            titles.setdefault(x.get("dedup_key") or "", []).append(x.get("title") or "")
    by_pos = {_entry_pos(e): e for e in after.get("papers", [])}
    plan, skipped = [], []
    for r in recs:
        if not r["ch"] or r.get("skipped"):
            continue
        old = r["entry"]
        e = by_pos.get(_entry_pos(old))
        if e is None or e.get("citekey") != old.get("citekey"):
            skipped.append("{}：写回后在同位置找不到同键条目".format(old.get("citekey")))
            continue
        if e.get("duplicate_of"):
            skipped.append("{}：写回后被判为 {} 的重复条目（新合并），不改键，确认后走 --fix-inline-citekeys"
                           .format(old.get("citekey"), e["duplicate_of"]))
            continue
        if (protected or {}).get(old.get("citekey")):
            skipped.append("{}：被生成块手改过的概念页 topics/{} 引用，改名工具会跳过该页，改键会造成悬空引用；"
                           "只修元数据不改键".format(old.get("citekey"), protected[old["citekey"]]))
            continue
        first = (r["ch"].get("authors") or e.get("authors") or [""])[0]
        if surname_first_chinese(first):
            skipped.append("{}：首作者 {} 疑似中文姓在前（大写段是拼音），兜底键会用错姓，只修元数据不改键"
                           .format(old.get("citekey"), first))
            continue
        if (e.get("title") or "")[:1].islower():
            skipped.append("{}：标题以小写开头，是残片（「{}」），基于残条改键没有意义，只修元数据不改键"
                           .format(old.get("citekey"), (e.get("title") or "")[:40]))
            continue
        if r.get("year_doubt"):
            skipped.append("{}：片段年份与现年份矛盾（{}），现年份存疑，只修作者不改键——免得把错年份铸进新键"
                           .format(old.get("citekey"), "；".join(n for n in r["notes"] if "年" in n)))
            continue
        if not is_generated_key(old):
            skipped.append("{}：不是兜底键形态（可能是 Zotero 键），只修元数据不改键".format(old.get("citekey")))
            continue
        new, why = plan_new_key(e, r["ch"], owners, titles)
        if not new:
            skipped.append("{}：{}".format(old.get("citekey"), why))
            continue
        if new == e.get("citekey"):
            continue
        if "issued" in r["ch"] or (r.get("snippet") or {}).get("year") == e.get("year"):
            year_source = "片段元数据行"
        elif not e.get("doi") and str(e.get("month") or "")[:4] == str(e.get("year")):
            year_source = "入库月份（邮件兜底，未核实）"
        else:
            year_source = "原有"
        plan.append({"dedup_key": e.get("dedup_key"), "note_file": e.get("note_file"),
                     "note_line": e.get("note_line"), "old": e["citekey"], "new": new, "title": e.get("title"),
                     "year_source": year_source})
        owners.setdefault(new, set()).add(e.get("dedup_key") or "")      # 同批占位防互撞
    return plan, skipped


# ---------------- 改键阶段 ----------------

PENDING_GLOB = "pending_scholar_metadata_*.json"
LEDGERS = ("backfill_deepread_progress.json", "backfill_expand_progress.json")
LEDGER_MDS = ("需下载全文清单.md",)
EXTERNAL_SCAN_DIRS = ("~/Desktop/Lab",)
TEXT_SUFFIXES = {".md", ".tex", ".bib", ".txt", ".qmd", ".rmd", ".csv", ".json", ".yaml", ".yml"}
# pending 计划的阶段：写回完成（且合并集合与预演一致）→ 改名完成 → 收尾（派生物/账本）完成
STAGES = ("planned", "writeback_done", "renamed", "finalized")


def _key_re(old: str) -> "re.Pattern":
    # 键两侧不能是键字符或连字符：anon2021Data 不能误中 anon2021Datadriven / anon2021Data-driven
    return re.compile(r"(?<![A-Za-z0-9_-]){}(?![A-Za-z0-9_-])".format(re.escape(old)))


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp-{}".format(os.getpid()))  # 带 pid 防并发互截
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def write_pending(notes_dir: Path, plan: List[Dict[str, Any]], merges: List[Dict[str, Any]], stamp: str) -> Path:
    """改键计划在写回**之前**落盘：两阶段之间崩了，重跑时写回已无差异、再也算不出旧键（R1 审计），
    只能靠这份计划续跑（--resume）。stage 记到哪一步，续跑按 stage 决定能不能继续（R2 审计）。"""
    out = notes_dir / "_rekey"
    out.mkdir(parents=True, exist_ok=True)
    p = out / "pending_scholar_metadata_{}.json".format(stamp)
    _write_json_atomic(p, {"stage": "planned", "created": stamp, "plan": plan,
                           "expected_merges": sorted([list(_entry_pos(m["entry"])) for m in merges])})
    return p


def _load_pending(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"stage": "corrupt", "error": str(e)}


def unfinished_pending(notes_dir: Path) -> List[Path]:
    d = notes_dir / "_rekey"
    if not d.is_dir():
        return []
    return [p for p in sorted(d.glob(PENDING_GLOB))
            if _load_pending(p).get("stage") not in ("finalized", "abandoned")]


def _set_stage(p: Path, payload: Dict[str, Any], stage: str, **extra) -> None:
    payload["stage"] = stage
    payload.update(extra)
    _write_json_atomic(p, payload)


def rename_ledgers(notes_dir: Path, mapping: Dict[str, str], backup: Backup) -> List[str]:
    """按 citekey 记账的进度文件与清单跟着改键：否则改名的必败篇目会被 --expand 当新篇重排、
    8 连败触发熔断，fetch_missing_pdfs --only-failed 也找不到它们（R1 审计）。"""
    changed = []
    for name in LEDGERS:
        p = notes_dir / name
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        hit = False
        for bucket in ("done", "failed"):
            d = data.get(bucket)
            if not isinstance(d, dict):
                continue
            for old, new in mapping.items():
                if old in d and new not in d:
                    rec = d.pop(old)
                    if isinstance(rec, dict) and rec.get("citekey") == old:
                        rec["citekey"] = new
                    d[new] = rec
                    hit = True
        if hit:
            style = _json_style(p)
            backup.save(p)
            _write_json(p, data, style)
            changed.append(name)
    # 每日无标识盘点 --resume-from 按 citekey 对上一份报告：不迁移，改名的篇目会被当成新入库、
    # 全部源重问一遍，白烧 OpenAlex 日额度（R1 审计：副本上待查从 133 涨到 182）
    inv_dir = notes_dir / "_inventory"
    if inv_dir.is_dir():
        for p in sorted(inv_dir.glob("missing_ids_*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            hit = False
            for rec in data.get("records") or []:
                if isinstance(rec, dict) and rec.get("citekey") in mapping:
                    rec["citekey"] = mapping[rec["citekey"]]
                    hit = True
            if hit:
                style = _json_style(p)
                backup.save(p)
                _write_json(p, data, style)
                changed.append("_inventory/" + p.name)
    md_ledgers = [notes_dir / n for n in LEDGER_MDS]
    if inv_dir.is_dir():
        md_ledgers += sorted(inv_dir.glob("missing_ids_*.md"))      # 人读报告（R2 审计：只迁了 json）
    for p in md_ledgers:
        name = str(p.relative_to(notes_dir))
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        new_text = text
        for old, new in mapping.items():
            new_text = _key_re(old).sub(new, new_text)
        if new_text != text:
            backup.save(p)
            p.write_text(new_text, encoding="utf-8")
            changed.append(name)
    return changed


def scan_external(keys: Dict[str, str], dirs=EXTERNAL_SCAN_DIRS) -> List[str]:
    """只报告不改：仓库外的稿件/笔记里还引用这些键的文件，以及文件名里带键的（按 citekey 命名的下载 PDF）。
    keys: 旧键 → 说明（新键，或「已并入 xxx」）。"""
    hits = []
    pats = {old: _key_re(old) for old in keys}
    for d in dirs:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            for old, pat in pats.items():
                if pat.search(f.name):
                    hits.append("{}（文件名含旧键 {} → {}）".format(f, old, keys[old]))
            if f.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                if f.stat().st_size > 2_000_000:
                    continue
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            olds = [old for old, pat in pats.items() if old in text and pat.search(text)]
            if olds:
                hits.append("{}（正文引用旧键 {}）".format(f, "、".join(olds[:5]) + (" 等" if len(olds) > 5 else "")))
    return hits


def backup_derivatives(notes_dir: Path, backup: Backup) -> None:
    """备份不在三处之内、但会被改键收尾改写的派生物。"""
    for name in ("literature_index.json", "all_references.json", "INDEX.md") + LEDGERS + LEDGER_MDS:
        backup.save(notes_dir / name)
    for sub in ("topics", "qa", "_inventory"):
        d = notes_dir / sub
        if d.is_dir():
            for f in list(d.rglob("*.md")) + list(d.rglob("*.json")):
                backup.save(f)


def _backup_note_triplet(notes_dir: Path, note_file: str, backup: Backup) -> None:
    """改名前备份该札记的三处文件（R2 审计：没有作者行的月份只在改名阶段被改，原先没进备份）。"""
    md = notes_dir / note_file
    stem = md.name[:-3]
    for f in (md, notes_dir / "{}.references.json".format(stem), notes_dir / "{}.index.json".format(stem)):
        backup.save(f)


def run_rekeys(notes_dir: Path, pending_path: Path, backup: Backup) -> Dict[str, Any]:
    """按落盘计划改键并收尾。可重入：已是新键的算已完成，收尾（topics/向量库/账本）只要没到
    finalized 就对**全部已完成**的键重做一遍（R2 审计：续跑时 touched 为空会跳过收尾）。"""
    from src.scholar.notes_index import find_stale_inline_citekeys, find_dangling_citations
    payload = _load_pending(pending_path)
    stage = payload.get("stage")
    if stage not in ("writeback_done", "renamed"):
        return {"fatal": "计划处于 {} 阶段：{}".format(
            stage, "写回没完成或合并集合与预演不一致，不能改键。先用 _archive/scholar_meta_{}/ 恢复文件，"
            "再把计划 stage 改成 abandoned 后重跑 --apply".format(payload.get("created", "<时间戳>"))
            if stage == "planned" else "无需续跑"), "done": [], "refused": [], "partial": [], "followed": [],
            "left": [], "side": {}, "ledgers": [], "dangling": []}

    index = update_index(notes_dir, full=True)
    dangling_before = {(d["page"], d["key"]) for d in find_dangling_citations(notes_dir, index)}
    by_pos = {_entry_pos(e): e for e in index.get("papers", [])}
    done, refused, partial, touched = [], [], [], []
    for item in payload["plan"]:
        e = by_pos.get((item["note_file"], int(item["note_line"])))
        if e is None:
            refused.append((item["old"], item["new"], item["note_file"], "同位置找不到条目"))
            continue
        if e.get("citekey") == item["new"]:
            done.append(item)
            touched.append(e)
            continue
        if e.get("citekey") != item["old"]:
            refused.append((item["old"], item["new"], item["note_file"],
                            "现键 {} 与计划不符".format(e.get("citekey"))))
            continue
        if e.get("duplicate_of"):
            refused.append((item["old"], item["new"], item["note_file"],
                            "已被判为 {} 的重复条目，不改".format(e["duplicate_of"])))
            continue
        _backup_note_triplet(notes_dir, item["note_file"], backup)
        res = _rename_citekey_in_note(notes_dir, e, item["old"], item["new"])
        if res == RENAME_OK:
            done.append(item)
            touched.append(e)
        elif res == RENAME_PARTIAL:
            partial.append((item["old"], item["new"], item["note_file"], "半改"))
        else:
            refused.append((item["old"], item["new"], item["note_file"], "三处预检不过，磁盘未动"))
    mapping = {i["old"]: i["new"] for i in done}           # 只用真改成功的（R2 审计 B）

    # duplicate 条目里的旧键：只处理「keeper 被本批改名、dup 还挂着 keeper 旧键」这一种
    index = update_index(notes_dir, full=True)
    followed, left = [], []
    for st in find_stale_inline_citekeys(index):
        dup, keeper = st["entry"], st["keeper"]
        if mapping.get(dup.get("citekey")) == keeper.get("citekey"):
            _backup_note_triplet(notes_dir, dup["note_file"], backup)
            res = _rename_citekey_in_note(notes_dir, dup, dup["citekey"], keeper["citekey"])
            (followed if res == RENAME_OK else left).append(
                (dup["citekey"], keeper["citekey"], dup.get("note_file")))
        else:
            left.append((dup.get("citekey"), keeper.get("citekey"), dup.get("note_file")))
    ok_rename = not refused and not partial
    _set_stage(pending_path, payload, "renamed" if ok_rename else stage,
               result={"done": len(done), "refused": refused, "partial": partial})

    lock = notes_dir / "topics" / ".rekey.lock"
    lock_existed = lock.exists()
    side = announce_rekey_side_effects(notes_dir, touched, rekey_map=list(mapping.items()),
                                       old_keys_retired=True) or {}
    if not lock_existed and lock.exists() and lock.stat().st_size == 0:
        lock.unlink()                                       # 改名工具留下的空锁文件（R2 审计）
    ledgers = rename_ledgers(notes_dir, mapping, backup)
    index = update_index(notes_dir, full=True)
    write_outputs(index, notes_dir)
    dangling = [d for d in find_dangling_citations(notes_dir, index) if (d["page"], d["key"]) not in dangling_before]
    finalized = ok_rename and not side.get("error") and not dangling \
        and not side.get("topics_skipped_tampered")
    if finalized:
        _set_stage(pending_path, payload, "finalized")
    return {"done": done, "refused": refused, "partial": partial, "followed": followed, "left": left,
            "side": side, "ledgers": ledgers, "dangling": dangling}


def write_rekey_table(notes_dir: Path, done: List[Dict[str, Any]], followed: List[Tuple], stamp: str) -> Path:
    out_dir = notes_dir / "_rekey"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"old": i["old"], "new": i["new"], "note_file": i["note_file"], "title": i.get("title"),
             "kind": "keeper", "year_source": i.get("year_source", "")} for i in done]
    rows += [{"old": o, "new": n, "note_file": f, "title": None, "kind": "duplicate 跟随 keeper", "year_source": ""}
             for o, n, f in followed]
    jp = out_dir / "scholar_metadata_{}.json".format(stamp)
    _write_json_atomic(jp, rows)
    md = ["# Scholar 元数据回填改键对照（{}）".format(stamp), "",
          "稿件里的 `[@旧键]` 请按此表替换；topics/qa 页已由改键收尾自动跟改。", "",
          "「键中年份来源」为「入库月份（邮件兜底，未核实）」的，片段里没有年份，年份沿用旧键——引用前请核对。", "",
          "| 旧键 | 新键 | 类型 | 键中年份来源 | 札记 | 标题 |", "|---|---|---|---|---|---|"]
    md += ["| `{}` | `{}` | {} | {} | {} | {} |".format(r["old"], r["new"], r["kind"], r["year_source"], r["note_file"],
                                                        (r["title"] or "")[:80].replace("|", "/")) for r in rows]
    (out_dir / "scholar_metadata_{}.md".format(stamp)).write_text("\n".join(md) + "\n", encoding="utf-8")
    return jp


def _print_report(recs, merges, plan, skipped, show):
    changes = [r for r in recs if r["ch"]]
    print("拟修 {} 篇（作者 {} · 年份 {}）；有说明但不改 {} 篇".format(
        len(changes), sum(1 for r in changes if "authors" in r["ch"]),
        sum(1 for r in changes if "issued" in r["ch"]), len(recs) - len(changes)))
    for r in changes[:show]:
        e = r["entry"]
        print("  · {} [{}] {}".format(e["month"], e["citekey"], "；".join(r["notes"])))
        if "authors" in r["ch"]:
            print("      作者 {} → {}".format((e.get("authors") or [])[:4], r["ch"]["authors"][:4]))
    if merges:
        print("\n⚠️ 写回后索引会新合并 {} 条（改掉残片作者后「首作者不同」守卫不再拦）。"
              "这些条目不改键；--apply 默认遇到新合并就中止，确认是同文后加 --accept-merges：".format(len(merges)))
        for m in merges:
            print("    {} [{}] {} → {}".format(m["entry"]["month"], m["entry"]["citekey"], m["before"], m["after"]))
    print("\n拟改键 {} 篇；只修元数据不改键 {} 篇".format(len(plan), len(skipped)))
    for s in skipped:
        print("    · " + s)
    for item in plan[:show]:
        print("  {} → {}".format(item["old"], item["new"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="用札记里的 Scholar 推送片段回填残缺作者/年份并改兜底键")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--apply", action="store_true", help="写盘（默认 dry-run，dry-run 也会在临时副本上完整预演）")
    ap.add_argument("--accept-merges", action="store_true",
                    help="预演报出新合并时仍继续（默认中止：误合并会把一篇论文静默藏成重复）")
    ap.add_argument("--no-rekey", action="store_true", help="只修元数据，不改 citekey")
    ap.add_argument("--resume", default="", help="续跑一份未完成的改键计划 _rekey/pending_*.json")
    ap.add_argument("--no-external-scan", action="store_true", help="不扫描 ~/Desktop/Lab 里对旧键的引用")
    ap.add_argument("--report", default="", help="逐条报告 JSON 路径")
    ap.add_argument("--show", type=int, default=40)
    args = ap.parse_args()

    notes_dir = repo_path(args.notes_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.resume:
        backup = Backup(notes_dir / "_archive" / "scholar_meta_{}_resume".format(stamp), notes_dir)
        backup_derivatives(notes_dir, backup)
        res = run_rekeys(notes_dir, repo_path(args.resume), backup)
        return _finish(notes_dir, res, stamp, backup, args, merged_keys={})

    pend = unfinished_pending(notes_dir)
    if pend and args.apply:
        for pp in pend:
            st = _load_pending(pp).get("stage")
            print("⛔ 有未完成的改键计划：{}（stage={}）".format(pp, st))
            if st in ("writeback_done", "renamed"):
                print("   → 用 `--resume {}` 续跑。".format(pp))
            else:
                print("   → 写回没完成：**先**用 `_archive/scholar_meta_{}/` 把文件拷回恢复，再把这份计划的 stage "
                      "改成 abandoned，然后重跑 --apply。不恢复就放弃，已写回的条目算不出旧键，会永久留着兜底键"
                      "（R3 审计实测漏改 10 个）。".format(_load_pending(pp).get("created", "<时间戳>")))
        return 1

    index = update_index(notes_dir)
    recs = collect(notes_dir, index)
    after = simulate(notes_dir, recs)
    merges = new_merges(index, after)
    plan, skipped = ([], []) if args.no_rekey else plan_rekeys(recs, after, keys_in_tampered_topic_pages(notes_dir))
    _print_report(recs, merges, plan, skipped, args.show)

    if args.report:
        Path(args.report).write_text(json.dumps({
            "entries": [{"citekey": r["entry"]["citekey"], "month": r["entry"]["month"],
                         "note_file": r["entry"]["note_file"], "title": r["entry"].get("title"),
                         "before": {"authors": r["entry"].get("authors"), "year": r["entry"].get("year")},
                         "snippet": r["snippet"], "notes": r["notes"], "skipped": bool(r.get("skipped")),
                         "after": {k: (v if k != "issued" else v["date"].year) for k, v in r["ch"].items()}}
                        for r in recs],
            "new_merges": [{"citekey": m["entry"]["citekey"], "note_file": m["entry"]["note_file"],
                            "before": m["before"], "after": m["after"]} for m in merges],
            "rekey_plan": plan, "skipped": skipped}, ensure_ascii=False, indent=1), encoding="utf-8")
        print("逐条报告：{}".format(args.report))

    if not args.apply:
        print("\n（dry-run，未写盘；加 --apply 执行）")
        return 0
    if merges and not args.accept_merges:
        print("⛔ 预演报出 {} 条新合并，未加 --accept-merges，未写盘。".format(len(merges)))
        return 1
    changes = [r for r in recs if r["ch"] and not r.get("skipped")]
    if not changes:
        print("无可修条目。")
        return 0

    print("\n⚠️ launchd 的 scholar-embed / scholar-vault 监视 literature_index.json，本次运行会多次改写它；"
          "若收尾报向量库同步失败，跑 `scripts/notes_embed.py` 补一次即可。")
    backup = Backup(notes_dir / "_archive" / "scholar_meta_{}".format(stamp), notes_dir)
    backup_derivatives(notes_dir, backup)
    pending = write_pending(notes_dir, plan, merges, stamp) if plan else None
    unmatched = apply_changes(notes_dir, recs, backup)
    print("✍️ 元数据写回 {} 篇；备份 → {}".format(len(changes), backup.root))
    for u in unmatched:
        print("  ⛔ " + u)
    index2 = update_index(notes_dir, full=True)
    write_outputs(index2, notes_dir)
    real_merges = new_merges(index, index2)
    merged_keys = {m["entry"]["citekey"]: "已并入 {}".format(m["after"]) for m in real_merges}
    if sorted(_entry_pos(m["entry"]) for m in real_merges) != sorted(_entry_pos(m["entry"]) for m in merges):
        print("⛔ 实际新合并与预演不一致，改键中止（计划停在 planned：{}）".format(pending))
        return 1
    if not pending:
        return 1 if unmatched else 0
    _set_stage(pending, _load_pending(pending), "writeback_done")
    res = run_rekeys(notes_dir, pending, backup)
    code = _finish(notes_dir, res, stamp, backup, args, merged_keys)
    return 1 if (unmatched or code) else 0


def _finish(notes_dir: Path, res: Dict[str, Any], stamp: str, backup: Backup, args,
            merged_keys: Dict[str, str]) -> int:
    if res.get("fatal"):
        print("⛔ " + res["fatal"])
        return 1
    print("改键 {} 篇；拒绝 {} · 半改 {}；duplicate 跟随 {} 处".format(
        len(res["done"]), len(res["refused"]), len(res["partial"]), len(res["followed"])))
    for o, n, f, why in res["partial"]:
        print("  ⛔ 半改（三处可能不一致，人工核对）：{} → {} {}".format(o, n, f))
    for o, n, f, why in res["refused"]:
        print("  ❌ 未改：{} → {} {}（{}）".format(o, n, f, why))
    if res["left"]:
        print("  ⚠️ 仍有 {} 条陈旧行内键（新合并造成，未自动处理，确认后 `notes_index.py --fix-inline-citekeys`）：".format(
            len(res["left"])))
        for o, n, f in res["left"]:
            print("      {} → keeper {}（{}）".format(o, n, f))
    side = res.get("side") or {}
    for pg in side.get("topics_skipped_tampered") or []:
        print("  ⛔ 概念页生成块被手改过，改名跳过：{}".format(pg))
    for d in res.get("dangling") or []:
        print("  ⛔ 本次新增悬空引用：topics/{} 里的 [@{}]".format(d["page"], d["key"]))
    if res["ledgers"]:
        print("  进度账本已跟随改键：{}".format("、".join(res["ledgers"])))
    if res["done"] or res["followed"]:
        jp = write_rekey_table(notes_dir, res["done"], res["followed"], stamp)
        print("旧键→新键对照表：{}（同名 .md）".format(jp))
    if not args.no_external_scan and (res["done"] or res["followed"] or merged_keys):
        keys = {i["old"]: i["new"] for i in res["done"]}
        keys.update({o: n for o, n, _ in res["followed"]})
        keys.update(merged_keys)
        ext = scan_external(keys)
        if ext:
            print("  ⚠️ 仓库外还引用旧键的文件（未改动，请手动处理）：")
            for h in ext:
                print("      " + h)
    print("备份：{}（{} 个文件）".format(backup.root, len(backup.done)))
    if side.get("error"):
        print("  ⛔ 向量库同步失败：请手跑 PYTHONPATH=. python scripts/notes_embed.py")
    bad = (res["refused"] or res["partial"] or side.get("error") or res.get("dangling")
           or side.get("topics_skipped_tampered"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
