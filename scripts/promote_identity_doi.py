#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条 arXiv 身份的札记条目升级为正刊 DOI 身份（dedup_key arxiv: → doi:）。

什么时候用：realign_metadata_ts.py 报了 `doi_candidate`（arXiv 号被 translation-server 解析到
正刊）且人工核对过确实是同一篇。**不要**对「疑似误配」的候选跑——实测 arXiv 翻译器会把
2404.11171 配到 IJCAI 另一篇（Lorello 等）。本脚本自带一道闸：TS 记录的首作者姓必须出现在
现有作者表里，否则拒绝。

改 DOI 是改身份键，派生物要一起扫（scholar-identity-keys 的教训）：
  1. sidecar `{stem}.index.json`：doi / url / journal / year
  2. `{stem}.references.json`：DOI / type / URL / container-title / volume / issue / page / issued
  3. md：`**链接**:` 行换成 `**DOI**:` 行（notes.py 渲染口径：有 DOI 就不渲染链接）
  4. manual bundle `manual/<month>/*.paper.json`：segment.metadata 同步（否则 regen 顶回）
  5. `abstracts.json`：摘要缓存按 dedup_key 存，键要从 arxiv:… 改名为 doi:…（否则向量库同步时
     该篇的 ab: 级向量因「无摘要」被当孤儿删掉，且没有任何自动入口补回）
  6. 收尾按月强扫索引（dedup_key 由索引按当前规则重算，DOI 档优先于 arXiv 档）
citekey 不动；arxiv_id 保留（DOI 在键梯里优先，arxiv 只作元数据）。
之后：`scripts/notes_embed.py`（向量库增量，期望待嵌/待删 0）→ `scripts/sync_vault.py`。

用法：
    PYTHONPATH=. python3 scripts/promote_identity_doi.py --citekey fan2026Interdisciplinary \\
        --doi 10.1108/FTSIG-11-2025-0139            # dry-run
    …同上 --apply                                    # 写盘（先备份到 _archive/promote_doi_<时间戳>/）
退出码：0 成功 / 2 参数、条目不唯一、DOI 已被占用、TS 不在线或未命中、作者闸未过。
"""
import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                   # noqa: E402
from src.scholar.notes_index import (                                      # noqa: E402
    INDEX_JSON, load_csl_items, _match_csl, update_index, write_outputs)
from src.scholar._citekey_utils import csl_names, date_parts, dedup_key_fields  # noqa: E402
from src.scholar.translation_server import (                               # noqa: E402
    resolve_identifier, _creators_to_authors, is_available, DEFAULT_BASE_URL)

CSL_TYPE = {"journalArticle": "article-journal", "conferencePaper": "paper-conference",
            "bookSection": "chapter", "report": "report", "thesis": "thesis"}


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", (s or "")).strip().lower())


def _families(authors: List[str]) -> set:
    return {_norm(n.get("family")) for n in csl_names(authors) if n.get("family")}


def _json_style(path: Path) -> Tuple[Optional[int], bool]:
    raw = path.read_bytes()
    second = raw.split(b"\n", 2)[1] if b"\n" in raw else b""
    m = re.match(rb"^( +)", second)
    return (len(m.group(1)) if m else None), raw.endswith(b"\n")


def _write_json(path: Path, data, style) -> None:
    indent, trailing = style
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent) + ("\n" if trailing else ""),
                   encoding="utf-8")
    tmp.replace(path)


def _issued(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = (item.get("date") or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return {"date": date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "precision": "day"}
    m = re.match(r"^(\d{1,2})/(\d{4})$", raw) or None
    if m:
        return {"date": date(int(m.group(2)), int(m.group(1)), 1), "precision": "month"}
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        return {"date": date(int(m.group(1)), int(m.group(2)), 1), "precision": "month"}
    m = re.search(r"\d{4}", raw)
    return {"date": date(int(m.group(0)), 1, 1), "precision": "year"} if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="arXiv 身份 → 正刊 DOI 身份升级")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--citekey", required=True)
    ap.add_argument("--doi", required=True)
    ap.add_argument("--ts-url", default=os.environ.get("PROCESSING__ZOTERO_TRANSLATION_SERVER_URL",
                                                       DEFAULT_BASE_URL))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    notes_dir = repo_path(args.notes_dir)
    doi = args.doi.strip()
    new_key = dedup_key_fields(doi, None, None)

    idx = json.loads((notes_dir / INDEX_JSON).read_text(encoding="utf-8"))
    hits = [p for p in idx["papers"] if p.get("citekey") == args.citekey]
    if len(hits) != 1:
        print("⛔ citekey {} 在索引里出现 {} 次，须唯一".format(args.citekey, len(hits)), file=sys.stderr)
        return 2
    e = hits[0]
    if e.get("doi"):
        print("⛔ 条目已有 DOI {}，不是升级场景".format(e["doi"]), file=sys.stderr)
        return 2
    taken = [p["citekey"] for p in idx["papers"] if (p.get("doi") or "").lower() == doi.lower()]
    if taken:
        print("⛔ DOI 已被 {} 占用——这是合并/去重问题，不是升级".format(taken), file=sys.stderr)
        return 2
    old_key = e.get("dedup_key")

    if not is_available(args.ts_url):
        print("⛔ translation-server 不在线：{}".format(args.ts_url), file=sys.stderr)
        return 2
    items = resolve_identifier(doi, base_url=args.ts_url)
    if not items:
        print("⛔ translation-server 未解析到 {}".format(doi), file=sys.stderr)
        return 2
    item = items[0]
    ts_authors = _creators_to_authors(item.get("creators", []))
    cur_authors = e.get("authors") or []
    first_fam = next(iter(_families(ts_authors[:1])), None)
    if not first_fam or first_fam not in _families(cur_authors):
        print("⛔ 作者闸未过：TS 首作者姓 {!r} 不在现有作者 {} 里——很可能是另一篇（arXiv 翻译器误配）"
              .format(first_fam, cur_authors[:4]), file=sys.stderr)
        return 2
    print("条目：{} · {} · {}".format(args.citekey, e["note_file"], e.get("title", "")[:70]))
    print("TS：{} | {} | {} {}({}) pp.{} | {} | 作者 {}".format(
        item.get("itemType"), item.get("publicationTitle") or item.get("proceedingsTitle"),
        item.get("volume") or "", item.get("issue") or "", "", item.get("pages") or "",
        item.get("date"), ts_authors[:3]))
    print("身份键：{} → {}".format(old_key, new_key))

    journal = (item.get("publicationTitle") or item.get("proceedingsTitle") or "").strip()
    url = (item.get("url") or "").strip()
    iss = _issued(item)
    csl_type = CSL_TYPE.get(item.get("itemType") or "", "article-journal")

    md_path = notes_dir / e["note_file"]
    stem = md_path.name[:-3]
    side_path = notes_dir / "{}.index.json".format(stem)
    ref_path = notes_dir / "{}.references.json".format(stem)
    abs_path = notes_dir / "abstracts.json"

    # --- 计划 ---
    plan: List[str] = []
    side = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else None
    row = None
    if side:
        cand = [r for r in side.get("papers", []) if r.get("citekey") == args.citekey]
        row = cand[0] if len(cand) == 1 else None
        plan.append("sidecar: doi/url/journal/year" if row else "sidecar: ⛔ 条目未唯一匹配")
    refs = load_csl_items(ref_path) if ref_path.exists() else []
    csl = _match_csl(e, refs)
    plan.append("CSL: DOI/type={}/URL/container/卷期页/issued".format(csl_type) if csl else "CSL: ⛔ 未匹配")
    lines = md_path.read_text(encoding="utf-8").split("\n")
    pat = re.compile(r"^## .*\[@{}\]\s*$".format(re.escape(args.citekey)))
    heads = [i for i, ln in enumerate(lines) if pat.match(ln)]
    h = heads[0] if len(heads) == 1 else None
    plan.append("md: 链接行→DOI 行 / 插 DOI 行" if h is not None else "md: ⛔ 标题未唯一定位")
    bundle = None
    if e.get("series") == "manual":
        mdir = notes_dir / "manual" / (e.get("month") or "")
        for bf in sorted(mdir.glob("*.paper.json")):
            m = ((json.loads(bf.read_text(encoding="utf-8")).get("segment") or {}).get("metadata")) or {}
            if _norm(m.get("arxiv_id")) == _norm(e.get("arxiv_id")) or _norm(m.get("title")) == _norm(e.get("title")):
                bundle = bf
                break
        plan.append("bundle: {}".format(bundle.name if bundle else "⛔ 未匹配"))
    abs_data = json.loads(abs_path.read_text(encoding="utf-8")) if abs_path.exists() else None
    has_abs = bool(abs_data and old_key in (abs_data.get("abstracts") or {}))
    plan.append("abstracts.json: 改键 {} → {}".format(old_key, new_key) if has_abs else "abstracts.json: 无该键，不动")
    for p in plan:
        print("  · " + p)
    if any("⛔" in p for p in plan):
        print("⛔ 有层没定位到，拒绝写盘", file=sys.stderr)
        return 2
    if not args.apply:
        print("（dry-run，未写盘；加 --apply 执行）")
        return 0

    # --- 写盘 ---
    bak = notes_dir / "_archive" / "promote_doi_{}".format(datetime.now().strftime("%Y%m%d-%H%M"))

    def _backup(p: Path):
        dst = bak / p.relative_to(notes_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)

    _backup(side_path)
    row["doi"] = doi
    if url and not row.get("url"):
        row["url"] = url
    if journal:
        row["journal"] = journal
    if iss:
        row["year"] = iss["date"].year
    _write_json(side_path, side, _json_style(side_path))

    _backup(ref_path)
    csl["DOI"] = doi
    csl["type"] = csl_type
    if url:
        csl["URL"] = url
    if journal:
        csl["container-title"] = journal
    for src, dst in (("volume", "volume"), ("issue", "issue"), ("pages", "page")):
        if item.get(src):
            csl[dst] = item[src]
    if iss:
        csl["issued"] = {"date-parts": date_parts(iss["date"], iss["precision"])}
    _write_json(ref_path, refs, _json_style(ref_path))

    _backup(md_path)
    end = next((j for j in range(h + 1, len(lines)) if lines[j].startswith("## ") or lines[j].startswith("# ")), len(lines))
    doi_line = "**DOI**: [{0}](https://doi.org/{0})".format(doi)
    j_link = next((j for j in range(h + 1, end) if lines[j].startswith("**链接**: ")), None)
    if j_link is not None:
        lines[j_link] = doi_line
    else:
        j_j = next((j for j in range(h + 1, end) if lines[j].startswith("**期刊/来源**: ")), None)
        j_a = next((j for j in range(h + 1, end) if lines[j].startswith("**作者**: ")), None)
        k = (j_j if j_j is not None else j_a if j_a is not None else h) + 1
        lines.insert(k, doi_line)
    if journal:
        j_j = next((j for j in range(h + 1, end + 1) if j < len(lines) and lines[j].startswith("**期刊/来源**: ")), None)
        if j_j is not None:
            lines[j_j] = "**期刊/来源**: " + journal
    tmp = md_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(md_path)

    if bundle:
        _backup(bundle)
        data = json.loads(bundle.read_text(encoding="utf-8"))
        m = data["segment"]["metadata"]
        m["doi"] = doi
        if url and not m.get("url"):
            m["url"] = url
        if journal:
            m["journal"] = journal
        for k in ("volume", "issue", "pages"):
            if item.get(k):
                m[k] = item[k]
        if iss:
            m["publication_date"] = iss["date"].isoformat()
            m["date_precision"] = iss["precision"]
        _write_json(bundle, data, _json_style(bundle))

    if has_abs:
        _backup(abs_path)
        for section in ("abstracts", "failures"):
            sec = abs_data.get(section)
            if isinstance(sec, dict) and old_key in sec:
                sec[new_key] = sec.pop(old_key)
        _write_json(abs_path, abs_data, _json_style(abs_path))

    mp = (e.get("month") or "")[:7]
    idx2 = update_index(notes_dir, since=mp, until=mp)
    wrote = write_outputs(idx2, notes_dir)
    now = [p for p in idx2["papers"] if p.get("citekey") == args.citekey]
    print("✅ 已写盘，备份 {}；索引强扫 {}：写盘 {}".format(
        bak, mp, ", ".join(k for k, v in wrote.items() if v) or "无变化"))
    print("   索引现值：doi={} dedup_key={} duplicate_of={}".format(
        now[0].get("doi"), now[0].get("dedup_key"), now[0].get("duplicate_of")) if now else "   ⛔ 索引里找不到该条目了")
    print("下一步：PYTHONPATH=. python3 scripts/notes_embed.py（期望待嵌/待删 0 或仅本篇）→ scripts/sync_vault.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
