#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「只有标题身份」的札记条目（dedup_key=title:…）升级为 DOI / arXiv 身份（批量，默认 dry-run）。

输入是无 DOI 盘点报告（`_inventory/missing_ids_*.json` 的 records，category=fillable，
每条带 best 候选：source/title/doi/pmid/pmc/arxiv/year/authors/sim）。写盘函数复用
`promote_identity_doi.plan_layers / write_layers`（6 类派生物同步的唯一实现，不另起炉灶）。

范围闸（不过则单列跳过，不算「被三闸拦下」）：
  - 索引里当前 dedup_key 不以 `title:` 开头：`pmlr:` / `openreview:` 已有正式身份（OpenAlex 给这类
    论文的候选多来自 arXiv 侧，写回会把正式版身份改成预印本）；`doi:` / `arxiv:` 已升级过。
  - 盘点标了 drop_reason（建议放弃）。
  - citekey 在索引里不唯一 / 找不到。
三道质量闸（任一不过就不写）：
  (1) 规范化标题相似度 ≥ 0.95（best.title 对札记标题）；
  (2) 第一作者姓氏与札记第一作者一致（札记作者表为空 → 「无法核对」跳过）；
  (3) 年份差 ≤ 1。
查重闸：候选 DOI 已被库里别的条目占用（或本批已被另一条认领）→ 跳过并单列（疑似重复条目，供人工合并）。
选标识优先级与 dedup_key 键梯一致：DOI > arXiv。**PMID / PMC 不在键梯里**（札记也没有 PMID 字段），
只有它们的候选写回不会改变任何身份，故单列为「无键位，不写」。

用法：
    PYTHONPATH=. python3 scripts/promote_title_identity.py \\
        --from-inventory output/scholar_notes/_inventory/missing_ids_XXXX.json      # dry-run（零写盘）
    …同上 --apply                                    # 写盘（先备份到 _archive/promote_title_<时间戳>/）
    PYTHONPATH=. python3 scripts/promote_title_identity.py --citekey nayebi2021Recurrent \\
        --doi 10.xxxx/yyyy --title "…" --year 2021 --first-author Nayebi   # 单条（同样过三闸）
dry-run 报告（JSON + md）写到 `--out-dir`（默认仓库 tmp/promote_title/；--apply 时默认 notes-dir/_inventory/）。
之后：scripts/notes_embed.py（向量库增量）→ scripts/sync_vault.py。
退出码：0 正常 / 2 参数错误。
"""
import argparse
import difflib
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from src.scholar.paths import repo_path                                    # noqa: E402
from src.scholar.notes_index import INDEX_JSON, update_index, write_outputs  # noqa: E402
from src.scholar._citekey_utils import dedup_key_fields, _norm_title        # noqa: E402
import promote_identity_doi as pid                                          # noqa: E402

TITLE_SIM_MIN = 0.95
YEAR_DIFF_MAX = 1
_INITIALS = re.compile(r"^[A-Z]{1,3}$")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def family_of(name: str) -> str:
    """人名串 → 姓（折叠重音/大小写）。支持 'Last, First' / 'First Last' / 'Last'（仅姓）/ 'Last AB'。"""
    n = (name or "").strip()
    if not n:
        return ""
    if "," in n:
        return _fold(n.split(",", 1)[0])
    parts = n.split()
    if len(parts) > 1 and _INITIALS.match(parts[-1]):
        return _fold(parts[0])
    return _fold(parts[-1])


def title_sim(a: str, b: str) -> float:
    ta, tb = _norm_title(a), _norm_title(b)
    if not ta or not tb:
        return 0.0
    return difflib.SequenceMatcher(None, ta, tb).ratio()


def _norm_doi(d: Optional[str]) -> str:
    d = (d or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", d)


def _norm_arxiv(a: Optional[str]) -> str:
    a = (a or "").strip().lower()
    a = re.sub(r"^arxiv:", "", a)
    return re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", a)


def evaluate(rec: Dict[str, Any], e: Optional[Dict[str, Any]], index_papers: List[Dict[str, Any]],
             claimed: Dict[str, str]) -> Dict[str, Any]:
    """对一条候选做范围闸 + 三闸 + 查重闸，返回结构化结论（纯函数，不碰盘；claimed 会被更新）。"""
    out: Dict[str, Any] = {"citekey": rec.get("citekey"), "old_key": None, "new_key": None,
                           "status": None, "reason": None, "gates": {}}
    best = rec.get("best") or {}
    if e is None:
        out.update(status="skip_scope", reason="citekey 在索引里不唯一或找不到")
        return out
    old_key = e.get("dedup_key") or ""
    out["old_key"] = old_key
    pref = old_key.split(":", 1)[0]
    if pref in ("pmlr", "openreview"):
        out.update(status="skip_scope", reason="{} 已有正式身份，不写回预印本/聚合库候选".format(pref))
        return out
    if pref != "title":
        out.update(status="skip_scope", reason="当前键为 {}（已不是 title: 档）".format(pref or "空"))
        return out
    if rec.get("drop_reason"):
        out.update(status="skip_dropped", reason="盘点建议放弃：" + str(rec["drop_reason"]))
        return out
    if not best:
        out.update(status="skip_scope", reason="无 best 候选")
        return out

    # 三道质量闸
    sim = title_sim(best.get("title") or "", e.get("title") or "")
    g1 = {"value": round(sim, 4), "pass": sim >= TITLE_SIM_MIN}
    note_auth = e.get("authors") or []
    cand_auth = best.get("authors") or []
    if not note_auth:
        g2 = {"value": None, "pass": False, "note": "无法核对（札记作者表为空）"}
    else:
        fa_note, fa_cand = family_of(note_auth[0]), family_of(cand_auth[0] if cand_auth else "")
        g2 = {"value": [fa_note, fa_cand], "pass": bool(fa_cand) and fa_note == fa_cand}
    yn, yc = e.get("year"), best.get("year")
    try:
        dy = abs(int(yn) - int(yc))
    except (TypeError, ValueError):
        dy = None
    g3 = {"value": dy, "pass": dy is not None and dy <= YEAR_DIFF_MAX}
    if dy is None:
        g3["note"] = "无法核对（札记或候选缺年份）"
    out["gates"] = {"title_sim": g1, "first_author": g2, "year_diff": g3}
    failed = [k for k, g in out["gates"].items() if not g["pass"]]
    if failed:
        out.update(status="blocked_" + "+".join(failed), reason="未过闸：" + ", ".join(failed))
        return out

    # 选标识：DOI > arXiv；PMID/PMC 无键位
    doi, arx = _norm_doi(best.get("doi")), _norm_arxiv(best.get("arxiv"))
    out["candidate"] = {"doi": doi or None, "arxiv": arx or None, "pmid": best.get("pmid"),
                        "pmc": best.get("pmc"), "source": best.get("source"),
                        "title": best.get("title")}
    if doi:
        taken = [p["citekey"] for p in index_papers
                 if p.get("citekey") != e["citekey"] and _norm_doi(p.get("doi")) == doi]
        if doi in claimed and claimed[doi] != e["citekey"]:
            taken.append(claimed[doi] + "（本批）")
        if taken:
            out.update(status="skip_doi_taken", reason="DOI 已被 {} 占用，疑似重复条目，待人工合并".format(taken),
                       doi_taken_by=taken)
            return out
        claimed[doi] = e["citekey"]
        out.update(status="pass", id_kind="doi", doi=doi,
                   new_key=dedup_key_fields(doi, e.get("arxiv_id"), None))
        return out
    if arx:
        out.update(status="pass", id_kind="arxiv", arxiv_id=arx,
                   new_key=dedup_key_fields(None, arx, None))
        return out
    out.update(status="skip_no_key_slot",
               reason="候选只有 PMID/PMC（{}），不在 dedup_key 键梯、札记也无该字段，写回不改任何身份"
               .format(best.get("pmid") or best.get("pmc")))
    return out


def _render_md(report: Dict[str, Any]) -> str:
    L = ["# 标题身份条目升级清单（{}）".format("已写盘" if report["applied"] else "dry-run，未写盘"), "",
         "- 生成：{}　库：`{}`".format(report["generated_at"], report["notes_dir"]),
         "- 统计：" + "；".join("{} {}".format(k, v) for k, v in sorted(report["stats"].items())), ""]
    order = ["pass", "blocked", "skip_doi_taken", "skip_no_key_slot", "skip_dropped", "skip_scope"]
    for grp in order:
        rows = [r for r in report["results"] if (r["status"] or "").startswith(grp)]
        if not rows:
            continue
        L += ["## {}（{}）".format(grp, len(rows)), ""]
        for r in rows:
            g = r.get("gates") or {}
            gs = " ".join("{}={}{}".format(k, v.get("value"), "✓" if v.get("pass") else "✗")
                          for k, v in g.items())
            L.append("- `{}` {} → {} ｜ {} ｜ {}".format(
                r["citekey"], r.get("old_key") or "-", r.get("new_key") or "-", gs or "-",
                r.get("reason") or ("layers: " + "; ".join(r.get("layers", [])) if r.get("layers") else "OK")))
        L.append("")
    return "\n".join(L)


def run(notes_dir: Path, records: List[Dict[str, Any]], *, apply: bool, out_dir: Path,
        stamp: Optional[str] = None) -> Dict[str, Any]:
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    idx = json.loads((notes_dir / INDEX_JSON).read_text(encoding="utf-8"))
    papers = idx["papers"]
    by_ck: Dict[str, List[Dict[str, Any]]] = {}
    for p in papers:
        by_ck.setdefault(p.get("citekey"), []).append(p)
    claimed: Dict[str, str] = {}
    results: List[Dict[str, Any]] = []
    for rec in records:
        hits = by_ck.get(rec.get("citekey"), [])
        e = hits[0] if len(hits) == 1 else None
        r = evaluate(rec, e, papers, claimed)
        r["title"] = (e or {}).get("title") or rec.get("title")
        if r["status"] == "pass":
            ctx = pid.plan_layers(notes_dir, e, e["citekey"], r["old_key"], r["new_key"])
            r["layers"] = ctx["plan"]
            if ctx["problems"]:
                r.update(status="skip_layers", reason="派生物定位失败：" + "; ".join(ctx["problems"]))
        results.append(r)

    bak = notes_dir / "_archive" / "promote_title_{}".format(stamp)
    if apply:
        months = set()
        for r in results:
            if r["status"] != "pass":
                continue
            e = by_ck[r["citekey"]][0]
            ctx = pid.plan_layers(notes_dir, e, e["citekey"], r["old_key"], r["new_key"])  # 重读盘：同月多篇共用 sidecar
            if ctx["problems"]:
                r.update(status="skip_layers", reason="派生物定位失败：" + "; ".join(ctx["problems"]))
                continue
            pid.write_layers(ctx, notes_dir, e, r["old_key"], r["new_key"], bak,
                             doi=r.get("doi"), arxiv_id=r.get("arxiv_id"),
                             url=(e.get("url") or ""))
            months.add((e.get("month") or "")[:7])
        for m in sorted(months):
            write_outputs(update_index(notes_dir, since=m, until=m), notes_dir)
        if months:
            now = {p["citekey"]: p for p in json.loads((notes_dir / INDEX_JSON).read_text(encoding="utf-8"))["papers"]}
            for r in results:
                if r["status"] == "pass":
                    got = (now.get(r["citekey"]) or {}).get("dedup_key")
                    r["index_key_after"] = got
                    if got != r["new_key"]:
                        r["status"] = "applied_key_mismatch"
                        r["reason"] = "写盘后索引重算键为 {}，与预期 {} 不符".format(got, r["new_key"])
                    else:
                        r["status"] = "applied"

    stats: Dict[str, int] = {}
    for r in results:
        k = "blocked" if r["status"].startswith("blocked_") else r["status"]
        stats[k] = stats.get(k, 0) + 1
        if k == "blocked":
            for g in r["status"][len("blocked_"):].split("+"):
                stats["blocked_by_" + g] = stats.get("blocked_by_" + g, 0) + 1
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"), "notes_dir": str(notes_dir),
              "applied": apply, "backup_dir": str(bak) if apply else None, "stats": stats,
              "n_input": len(records), "results": results}
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "promote_title_{}_{}".format("apply" if apply else "dryrun", stamp)
    (out_dir / (stem + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / (stem + ".md")).write_text(_render_md(report), encoding="utf-8")
    report["files"] = [str(out_dir / (stem + ".json")), str(out_dir / (stem + ".md"))]
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="标题身份条目升级为 DOI/arXiv（三闸+派生物同步，默认 dry-run）")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--from-inventory", help="盘点报告 json（取 category=fillable 的 records）")
    ap.add_argument("--citekey", help="单条模式：只处理该 citekey（可与 --from-inventory 联用作筛选）")
    ap.add_argument("--doi")
    ap.add_argument("--arxiv")
    ap.add_argument("--title", help="单条模式的候选标题（过标题闸）")
    ap.add_argument("--year", type=int)
    ap.add_argument("--first-author", help="单条模式的候选第一作者（姓或全名）")
    ap.add_argument("--out-dir")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    notes_dir = Path(args.notes_dir) if Path(args.notes_dir).is_absolute() else repo_path(args.notes_dir)

    records: List[Dict[str, Any]] = []
    if args.from_inventory:
        data = json.loads(Path(args.from_inventory).read_text(encoding="utf-8"))
        records = [r for r in data["records"] if r.get("category") == "fillable"]
        if args.citekey:
            records = [r for r in records if r.get("citekey") == args.citekey]
    elif args.citekey:
        if not (args.doi or args.arxiv) or not args.title or args.year is None or not args.first_author:
            print("⛔ 单条模式须给 --doi/--arxiv + --title + --year + --first-author（要过三道闸）", file=sys.stderr)
            return 2
        records = [{"citekey": args.citekey, "best": {
            "source": "cli", "title": args.title, "doi": args.doi, "arxiv": args.arxiv,
            "year": args.year, "authors": [args.first_author]}}]
    else:
        print("⛔ 须给 --from-inventory 或 --citekey", file=sys.stderr)
        return 2
    if not records:
        print("⛔ 没有可处理的记录", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir) if args.out_dir else (
        notes_dir / "_inventory" if args.apply else repo_path("tmp/promote_title"))
    rep = run(notes_dir, records, apply=args.apply, out_dir=out_dir)
    print("统计：{}".format(rep["stats"]))
    for f in rep["files"]:
        print("报告：" + f)
    if rep["applied"]:
        print("备份：{}\n下一步：PYTHONPATH=. python3 scripts/notes_embed.py → scripts/sync_vault.py".format(rep["backup_dir"]))
    else:
        print("（dry-run，库内零写盘；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
