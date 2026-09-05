#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无 DOI / arXiv / PMID 的 keeper：联网**只读**盘点能不能补上标识（不写盘、不改索引）。

docs/bugs/2026-09-04-missing-identifier-backfill.md 定的两步，**顺序不能反**：
  1. 先盘点——对每篇拿标题查 Crossref 与 OpenAlex，产出三份名单：
       fillable        两源之一命中且标题相似度、年份、首作者三项对得上，DOI 可补
       ambiguous       有命中但年份或首作者对不上，要人看
       no_hit          两源都没有像样的命中
     再叠一层「建议放弃」：PMLR / OpenReview 键的篇目本就有出版方稳定标识，补 DOI 对身份没帮助；
     MAYBE 裁决且相似度不够高的，不值得为它动身份键。
  2. 命中的再考虑回填——**那是改身份键**（DOI 进 dedup_key 会连带 citekey / 向量库 chunk id /
     all_references），必须走「改身份键要扫派生物」的流程，不在本脚本里做。

用法：
    PYTHONPATH=. python3 scripts/inventory_missing_ids.py --email you@x.org            # 全量（约 317 篇 × 2 源）
    PYTHONPATH=. python3 scripts/inventory_missing_ids.py --email you@x.org --limit 20 # 先试 20 篇
    PYTHONPATH=. python3 scripts/inventory_missing_ids.py --dry-run                    # 不联网，只列候选

产出（默认 output/scholar_notes/_inventory/missing_ids_<时间戳>.json / .md）：
JSON 每条一记录（含两源原始命中），Markdown 是给人看的汇总与 fillable 清单。
礼貌：两源各带 mailto，路与路之间 --delay 秒；403/429 走 fulltext.polite_get 的退避与按宿主熔断。
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                           # noqa: E402
from src.scholar.notes_index import iter_keepers                                    # noqa: E402
from src.scholar.crossref import crossref_lookup, title_similarity, _norm_title     # noqa: E402
from src.scholar.fulltext import ipv4_client, polite_get                            # noqa: E402
from src.utils.logger import get_logger                                             # noqa: E402

logger = get_logger(__name__)

OPENALEX_API = "https://api.openalex.org/works"
SIM_FILLABLE = 0.92          # 标题相似度：三项齐才算 fillable
SIM_STRONG = 0.97            # 相似度极高时允许首作者缺失/对不上（元数据首作者本就常错）
SIM_CANDIDATE = 0.80         # 低于这个连 ambiguous 都不算，视为没命中
YEAR_TOLERANCE = 1           # 预印本 vs 正刊差一年是常态
MIN_TITLE_WORDS = 4          # 实词太少的标题对谁都像，不查（同 fulltext._ARXIV_TITLE_MIN_WORDS）


# ---------------- 纯函数：可单测 ----------------

def title_words(t: str) -> List[str]:
    return re.findall(r"[a-z]{4,}", (t or "").lower())


def family_name(author: str) -> str:
    """'Xiaoli Bird' / 'Bird, Xiaoli' / 'X. Bird' → 'bird'。"""
    a = (author or "").strip()
    if not a:
        return ""
    if "," in a:
        return _norm_title(a.split(",")[0])
    return _norm_title(a.split()[-1])


def first_author_matches(entry_authors: List[str], hit_authors: List[str]) -> Optional[bool]:
    """None = 任一侧没有作者信息（不算对上也不算对不上）。"""
    if not entry_authors or not hit_authors:
        return None
    return family_name(entry_authors[0]) == family_name(hit_authors[0]) and bool(family_name(entry_authors[0]))


def year_ok(entry_year, hit_year) -> Optional[bool]:
    try:
        a, b = int(entry_year), int(hit_year)
    except (TypeError, ValueError):
        return None
    return abs(a - b) <= YEAR_TOLERANCE


def classify(entry: Dict[str, Any], hits: List[Dict[str, Any]]) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """把两源的命中归成 fillable / ambiguous / no_hit，并给出建议放弃的理由（可为空串）。

    hits 每条：{"source", "title", "doi", "year", "authors", "sim"}。返回 (类别, 最佳命中|None, 建议放弃理由)。
    """
    dk = str(entry.get("dedup_key") or "")
    is_maybe = str(entry.get("decision") or "").upper() == "MAYBE"
    drop = ""
    if dk.startswith(("pmlr:", "openreview:")):
        drop = "出版方已有稳定标识（{}），补 DOI 对身份没帮助".format(dk.split(":")[0])
    if len(title_words(entry.get("title") or "")) < MIN_TITLE_WORDS:
        return "ambiguous", None, (drop or "标题实词不足 {} 个，检索结果不可信".format(MIN_TITLE_WORDS))

    good = [h for h in hits if h.get("doi") and (h.get("sim") or 0) >= SIM_CANDIDATE]
    if not good:
        return "no_hit", None, (drop or ("裁决为 MAYBE，不值得为它动身份键" if is_maybe else ""))
    good.sort(key=lambda h: -(h.get("sim") or 0))
    # 两源各命中一个像样的候选、DOI 却不同（预印本 vs 正刊、勘误、同名不同文）——这是「要人看」
    # 的最强信号，不能按相似度取第一个就当 fillable（第 1 轮审计 R1-3.4）
    dois = {_norm_doi(h.get("doi")) for h in good if h.get("doi")}
    if len(dois) > 1:
        best = good[0]
        note = "两源 DOI 不一致：{}".format(" vs ".join(sorted(dois)))
        return "ambiguous", best, (drop + "；" if drop else "") + note
    best = good[0]
    sim = best.get("sim") or 0
    # MAYBE 裁决**且相似度不够高**才建议放弃：标题逐字命中的 MAYBE 篇目值得留给人判断（R1-3.6）
    if is_maybe and sim < SIM_STRONG and not drop:
        drop = "裁决为 MAYBE 且相似度 {:.2f} < {:.2f}，不值得为它动身份键".format(sim, SIM_STRONG)
    yo = year_ok(entry.get("year"), best.get("year"))
    fa = first_author_matches(entry.get("authors") or [], best.get("authors") or [])
    if sim >= SIM_FILLABLE and yo is not False and (fa is True or (fa is None and sim >= SIM_STRONG) or sim >= 0.99):
        return "fillable", best, drop
    return "ambiguous", best, drop


def _norm_doi(d) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d or "").strip().lower())


# ---------------- 联网：两源 ----------------

def query_crossref(title: str, email: str, client) -> Optional[Dict[str, Any]]:
    got = crossref_lookup(title, email=email, client=client)
    if not got:
        return None
    d = got.get("publication_date")
    year = getattr(d, "year", None) if d is not None else None
    return {"source": "crossref", "title": got.get("title") or "", "doi": got.get("doi"),
            "year": year, "authors": list(got.get("authors") or []),
            "sim": title_similarity(title, got.get("title") or "")}


def query_openalex(title: str, email: str, client) -> Optional[Dict[str, Any]]:
    # 记忆里踩过：无 DOI 的 PMLR 论文必须用 filter=title.search，别用 search=（那会全走 arXiv 侧）
    q = re.sub(r"[^\w\s\-]", " ", title)[:200]
    params = {"filter": "title.search:{}".format(q), "per-page": 3}
    if email:
        params["mailto"] = email
    try:
        r = polite_get(client, OPENALEX_API, params=params, timeout=30.0)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
    except Exception as e:                                        # noqa: BLE001
        logger.warning("  ⚠️ OpenAlex 检索失败（{}）: {}".format(title[:40], e))
        return None
    best = None
    for w in results:
        t = w.get("title") or w.get("display_name") or ""
        sim = title_similarity(title, t)
        if best is None or sim > best["sim"]:
            doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
            authors = [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])]
            best = {"source": "openalex", "title": t, "doi": doi, "year": w.get("publication_year"),
                    "authors": authors, "sim": sim}
    return best


def select_targets(index: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    # 撤稿条目不进清单：本库口径是「保留札记、踢出向量库与书目」，为它补 DOI 进身份键没有收益，
    # 却会进 fillable 名单诱导改键（第 1 轮审计 R1-3.7）
    for e in iter_keepers(index, include_retracted=False):
        dk = str(e.get("dedup_key") or "")
        # 索引里没有 pmid 字段（e.get("pmid") 恒 None），标识是否已有要看 dedup_key 的前缀
        if e.get("doi") or e.get("arxiv_id") or dk.startswith(("doi:", "arxiv:", "pmid:")):
            continue
        if str(e.get("entry_type") or "article") in ("book", "chapter") or dk.startswith("isbn:"):
            continue
        if not (e.get("title") or "").strip():
            continue
        out.append(e)
    return out


def render_md(records: List[Dict[str, Any]], stamp: str) -> str:
    from collections import Counter
    c = Counter(r["category"] for r in records)
    n_drop = sum(1 for r in records if r.get("drop_reason"))
    lines = ["# 无标识 keeper 盘点（{}）".format(stamp), "",
             "只读盘点，未改任何文件。共 {} 篇：fillable {} / ambiguous {} / no_hit {}；其中带「建议放弃」标注 {} 篇。"
             .format(len(records), c.get("fillable", 0), c.get("ambiguous", 0), c.get("no_hit", 0), n_drop), "",
             "下一步若要回填，**必须**当成改身份键处理（DOI 进 dedup_key → citekey / 向量库 chunk id / "
             "all_references 联动），走「改身份键要扫派生物」流程；不要用补元数据脚本顺手做。", "",
             "## fillable（两源之一命中，标题/年份/首作者对得上）", "",
             "| citekey | 裁决 | 命中源 | 相似度 | DOI | 年份(库/源) | 建议放弃 |", "|---|---|---|---|---|---|---|"]
    for r in records:
        if r["category"] != "fillable":
            continue
        b = r.get("best") or {}
        lines.append("| {} | {} | {} | {:.2f} | {} | {}/{} | {} |".format(
            r["citekey"], r.get("decision") or "", b.get("source"), b.get("sim") or 0, b.get("doi") or "",
            r.get("year") or "", b.get("year") or "", r.get("drop_reason") or ""))
    lines += ["", "## ambiguous（有命中但要人看）", "",
              "| citekey | 命中源 | 相似度 | DOI | 年份(库/源) | 首作者(库/源) | 备注 |", "|---|---|---|---|---|---|---|"]
    for r in records:
        if r["category"] != "ambiguous":
            continue
        b = r.get("best") or {}
        ea = (r.get("authors") or [""])[0]
        ha = (b.get("authors") or [""])[0] if b else ""
        lines.append("| {} | {} | {} | {} | {}/{} | {} / {} | {} |".format(
            r["citekey"], b.get("source") or "", ("{:.2f}".format(b["sim"]) if b else ""), b.get("doi") or "",
            r.get("year") or "", b.get("year") or "", ea[:24], ha[:24], r.get("drop_reason") or ""))
    lines += ["", "## no_hit（两源都没有像样的命中）", "",
              ", ".join(r["citekey"] for r in records if r["category"] == "no_hit") or "（无）", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--email", default="", help="Crossref/OpenAlex 礼貌池邮箱（强烈建议填）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.0, help="两次网络调用之间的间隔（秒）")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--dry-run", action="store_true", help="不联网，只列出候选")
    args = ap.parse_args()

    nd = repo_path(args.notes_dir)
    ip = nd / "literature_index.json"
    if not ip.exists():
        print("索引不存在：{}".format(ip), file=sys.stderr)
        return 2
    index = json.loads(ip.read_text(encoding="utf-8"))
    targets = select_targets(index)
    if args.limit > 0:
        targets = targets[:args.limit]
    print("无 DOI/arXiv/PMID 的 keeper：{} 篇{}".format(len(targets), "（--limit 生效）" if args.limit else ""))
    if args.dry_run:
        for e in targets[:50]:
            print("  {:<32} {} | {}".format(e.get("citekey"), e.get("year"), (e.get("title") or "")[:70]))
        if len(targets) > 50:
            print("  … 共 {} 篇".format(len(targets)))
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = repo_path(args.out_dir) if args.out_dir else nd / "_inventory"
    out_dir.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    with ipv4_client(timeout=30.0) as cli:
        for i, e in enumerate(targets, start=1):
            title = e.get("title") or ""
            hits = []
            # 标题实词不足的条目 classify 一律判 ambiguous、不采信命中——那就别白打两次网络
            #（生产 317 篇里有 16 篇这样；第 1 轮审计 R1-3.5）
            if len(title_words(title)) >= MIN_TITLE_WORDS:
                cr = query_crossref(title, args.email, cli)
                if cr:
                    hits.append(cr)
                if args.delay > 0:
                    time.sleep(args.delay)
                oa = query_openalex(title, args.email, cli)
                if oa:
                    hits.append(oa)
                if args.delay > 0:
                    time.sleep(args.delay)
            cat, best, drop = classify(e, hits)
            records.append({"citekey": e.get("citekey"), "title": title, "year": e.get("year"),
                            "authors": list(e.get("authors") or [])[:3], "decision": e.get("decision"),
                            "dedup_key": e.get("dedup_key"), "month": e.get("month"),
                            "category": cat, "best": best, "hits": hits, "drop_reason": drop})
            if i % 25 == 0 or i == len(targets):
                from collections import Counter
                c = Counter(r["category"] for r in records)
                print("  [{}/{}] fillable {} / ambiguous {} / no_hit {}".format(
                    i, len(targets), c.get("fillable", 0), c.get("ambiguous", 0), c.get("no_hit", 0)), flush=True)

    jp = out_dir / "missing_ids_{}.json".format(stamp)
    mp = out_dir / "missing_ids_{}.md".format(stamp)
    jp.write_text(json.dumps({"generated_at": stamp, "n": len(records), "records": records},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    mp.write_text(render_md(records, stamp), encoding="utf-8")
    print("已写：{}\n      {}".format(jp, mp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
