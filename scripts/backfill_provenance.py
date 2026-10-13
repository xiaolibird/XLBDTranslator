#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量札记来源回填（可信度加固 WO-3）：从现有档案生成 overlay，不碰 md 与 sidecar。

  python scripts/backfill_provenance.py                 # dry-run：只打印报告，不写任何文件
  python scripts/backfill_provenance.py --apply         # 写 output/scholar_notes/_provenance/overlay.json（+ 回填报告）

回填规则（设计 §4.3.4；不调 LLM、不联网、只读 digest/bundle/索引）：
  1. 复审升级条目（promotion_scan_20260808.json 里 old=MAYBE→new=INCLUDE，且当前索引 decision=INCLUDE、
     md 裁决行带「复审升级 2026-08-08」）：decision_source=promotion-scan-2026-08-08，
     模型/prompt 据 2026-08-08 审计记录，decision_model_verified=false；
  2. auto 其余条目：扫 digest_*.json 的 filter_decision，按身份梯对到索引条目，取 decided_at 最晚的一次，
     且其 decision 须与索引 keeper 的 decision 一致，否则**不回填**并列入报告；
     decision_model_verified=false（digest 里只有请求值）；provenance_origin=backfill-digest-json；
  3. manual：从 final bundle 回填 closeread_model（脚本草稿模型）/ closeread_final_by / decision_source=manual，
     provenance_origin=backfill-bundle；
  4. 其余（含 auto 精读模型、书籍）：decision_source / provenance_origin 记 unknown-legacy，**不按时间线推测**。

overlay 只承载「缺失」：索引构建时只填缺失键、绝不覆盖 sidecar 已有的值（见 notes_index.apply_provenance_overlay）。
"""
import argparse
import glob
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar._citekey_utils import (  # noqa: E402
    PROVENANCE_FIELDS, dedup_key_fields, provenance_fields, recompute_entry_key, _norm_title,
)
from src.scholar.paths import repo_path  # noqa: E402

PROMOTION_SOURCE = "promotion-scan-2026-08-08"
PROMOTION_MARKER = "复审升级 2026-08-08"
# 据 2026-08-08 审计记录（记忆档案；scan JSON 本身不含模型字段）
PROMOTION_MODEL = "deepseek-v4-flash"
PROMOTION_PROMPT = "filter-v3@08540624"
PROMOTION_AT = "2026-08-08"
OVERLAY_SCHEMA = 1
_KEYSTAGES = ("keyword_fallback", "whitelist_keyword", "blacklist_keyword")


def _load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ladder(doi, arxiv, title) -> List[Tuple[str, str]]:
    """身份梯（与 notes_index.apply_provenance_overlay 同序）：doi → arxiv → 规范化标题。"""
    out = []
    if doi:
        out.append(("doi", str(doi).strip().lower()))
    if arxiv:
        out.append(("arxiv", str(arxiv).strip().lower()))
    t = _norm_title(title)
    if t:
        out.append(("title", t))
    return out


class IndexLookup:
    """索引条目的多键查找（只收指定 series），优先 keeper。"""

    def __init__(self, papers: List[Dict[str, Any]], series: str):
        self.by_key: Dict[str, List[Dict[str, Any]]] = {}
        self.by_ladder: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for e in papers:
            if e.get("series") != series:
                continue
            self.by_key.setdefault(recompute_entry_key(e), []).append(e)
            for k in _ladder(e.get("doi"), e.get("arxiv_id"), e.get("title")):
                self.by_ladder.setdefault(k, []).append(e)

    @staticmethod
    def _pick(cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not cands:
            return None
        keepers = [c for c in cands if not c.get("duplicate_of")]
        return (keepers or cands)[0]

    def find(self, key: Optional[str], doi, arxiv, title) -> Optional[Dict[str, Any]]:
        if key and key in self.by_key:
            return self._pick(self.by_key[key])
        for k in _ladder(doi, arxiv, title):
            if k in self.by_ladder:
                return self._pick(self.by_ladder[k])
        return None


def _meta_key(meta: Dict[str, Any]) -> str:
    return dedup_key_fields(meta.get("doi"), meta.get("arxiv_id"), meta.get("title"),
                            fallback=meta.get("paper_id") or "", url=meta.get("url"),
                            isbn=meta.get("isbn"), chapter_number=meta.get("chapter_number"))


def _day(ts: Any) -> Optional[str]:
    return str(ts)[:10] if ts else None


# ---------------- 规则 1：复审升级 ----------------

def _has_upgrade_marker(notes_dir: Path, entry: Dict[str, Any],
                        cache: Dict[str, List[str]]) -> Optional[bool]:
    """md 里该条目的裁决行是否带「复审升级 2026-08-08」；md 读不到返回 None（交给调用方降级）。"""
    nf = entry.get("note_file")
    if not nf:
        return None
    if nf not in cache:
        try:
            cache[nf] = (notes_dir / nf).read_text(encoding="utf-8").splitlines()
        except Exception:
            cache[nf] = []
    lines = cache[nf]
    start = entry.get("note_line")
    if not lines or not start:
        return None
    block = []
    for i, ln in enumerate(lines[start - 1:start - 1 + 60]):
        if i > 0 and ln.startswith("## "):
            break
        block.append(ln)
    return any(PROMOTION_MARKER in ln for ln in block)


def collect_promotions(scan: List[Dict[str, Any]], auto_lookup: IndexLookup,
                       notes_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """返回 {entry 身份键: fields} 与统计。"""
    out: Dict[str, Dict[str, Any]] = {}
    st = Counter()
    md_cache: Dict[str, List[str]] = {}
    for r in scan:
        if r.get("new_decision") != "INCLUDE" or r.get("old_decision") != "MAYBE":
            continue
        st["scan_upgrades"] += 1
        key = dedup_key_fields(r.get("doi"), r.get("arxiv_id"), r.get("title"),
                               fallback=r.get("citekey") or "")
        e = auto_lookup.find(key, r.get("doi"), r.get("arxiv_id"), r.get("title"))
        if e is None:
            st["not_in_index"] += 1
            continue
        if e.get("decision") != "INCLUDE":
            st["index_not_include"] += 1
            continue
        mk = _has_upgrade_marker(notes_dir, e, md_cache)
        if mk is False:
            st["no_md_marker"] += 1
            continue
        out[recompute_entry_key(e)] = {
            "decision_source": PROMOTION_SOURCE,
            "decision_model": PROMOTION_MODEL,
            "decision_model_verified": False,
            "decision_prompt_version": PROMOTION_PROMPT,
            "decision_at": PROMOTION_AT,
            "decision_model_note": "据 2026-08-08 审计记录",
            "provenance_origin": "backfill-promotion-scan",
        }
    st["matched"] = len(out)
    return out, dict(st)


# ---------------- 规则 2：digest JSON ----------------

def collect_digest_decisions(digest_dir: Path, auto_lookup: IndexLookup,
                             skip_keys: set) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """扫 digest_*.json，取每篇 decided_at 最晚的 filter_decision；与 keeper 的 decision 一致才回填。"""
    latest: Dict[str, Tuple[str, str, Dict[str, Any]]] = {}   # 身份键 -> (decided_at, 文件名, fd)
    files = sorted(f for f in glob.glob(str(Path(digest_dir) / "digest_*.json"))
                   if not f.endswith(("_stats.json", "_excluded.json")))
    st = Counter()
    st["digest_files"] = len(files)
    entry_of: Dict[str, Dict[str, Any]] = {}
    for f in files:
        try:
            d = _load_json(Path(f))
        except Exception as e:
            st["unreadable_files"] += 1
            print("  ⚠️ 跳过无法解析的 digest：{}（{}）".format(f, e), file=sys.stderr)
            continue
        for seg in d.get("segments") or []:
            fd = seg.get("filter_decision")
            meta = seg.get("metadata") or {}
            if not fd:
                continue
            e = auto_lookup.find(_meta_key(meta), meta.get("doi"), meta.get("arxiv_id"),
                                 meta.get("title"))
            if e is None:
                continue
            k = recompute_entry_key(e)
            entry_of[k] = e
            stamp = (str(fd.get("decided_at") or ""), Path(f).name)
            if k not in latest or stamp >= latest[k][:2]:
                latest[k] = (stamp[0], stamp[1], fd)
    out: Dict[str, Dict[str, Any]] = {}
    mismatches: List[Dict[str, Any]] = []
    for k, (_, fname, fd) in latest.items():
        if k in skip_keys:
            st["skipped_promotion"] += 1
            continue
        e = entry_of[k]
        if fd.get("decision") != e.get("decision"):
            mismatches.append({"citekey": e.get("citekey"), "dedup_key": k,
                               "digest_decision": fd.get("decision"),
                               "index_decision": e.get("decision"), "digest_file": fname})
            continue
        stage = fd.get("stage")
        source = ("digest" if stage == "llm_judge"
                  else "keyword_fallback" if stage in _KEYSTAGES else "unknown-legacy")
        f: Dict[str, Any] = {"decision_source": source,
                             "decision_at": _day(fd.get("decided_at")),
                             "provenance_origin": "backfill-digest-json"}
        if fd.get("model"):
            f["decision_model"] = fd["model"]
            f["decision_model_verified"] = False   # digest 里只有请求值
        if fd.get("prompt_version"):
            f["decision_prompt_version"] = fd["prompt_version"]
        out[k] = {kk: vv for kk, vv in f.items() if vv is not None}
    st["matched"] = len(out)
    st["decision_mismatch"] = len(mismatches)
    return out, {**dict(st), "mismatches": mismatches}


# ---------------- 规则 3：manual bundle ----------------

def collect_bundles(notes_dir: Path, manual_papers: List[Dict[str, Any]]
                    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """从 manual/*/*.paper.json 复原来源键（复用 pdf_ingest.segment_from_bundle 与 provenance_fields）。"""
    from src.scholar.pdf_ingest import segment_from_bundle
    lookup = IndexLookup(manual_papers, "manual")
    cand: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}   # 身份键 -> [(bundle month, fields)]
    st = Counter()
    for bf in sorted((Path(notes_dir) / "manual").glob("*/*.paper.json")):
        st["bundles"] += 1
        try:
            data = _load_json(bf)
            seg = segment_from_bundle(data)
        except Exception as e:
            st["unreadable"] += 1
            print("  ⚠️ 跳过无法复原的 bundle：{}（{}）".format(bf, e), file=sys.stderr)
            continue
        m = seg.metadata
        e = lookup.find(_meta_key(m.model_dump() if hasattr(m, "model_dump") else m.dict()),
                        m.doi, m.arxiv_id, m.title)
        if e is None:
            st["not_in_index"] += 1
            continue
        pf = provenance_fields(seg.filter_decision, seg.close_reading, "manual")
        pf["decision_source"] = "manual"
        pf["provenance_origin"] = "backfill-bundle"
        # 老 bundle 的 model 是请求值，不会有 served_model；provenance_fields 已把 verified 记为 False
        cand.setdefault(recompute_entry_key(e), []).append(
            (str(data.get("month") or ""), {k: v for k, v in pf.items() if v is not None}))
    out: Dict[str, Dict[str, Any]] = {}
    for k, lst in cand.items():
        entry = lookup.by_key.get(k, [{}])[0]
        same_month = [f for mth, f in lst if mth == entry.get("month")]
        out[k] = (same_month or [f for _, f in lst])[0]
    st["matched"] = len(out)
    return out, dict(st)


# ---------------- 汇总 ----------------

def build_overlay(notes_dir: Path, digest_dir: Path, scan_path: Optional[Path]
                  ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    idx = _load_json(Path(notes_dir) / "literature_index.json")
    papers = idx.get("papers") or []
    auto_lookup = IndexLookup(papers, "auto")

    promo: Dict[str, Dict[str, Any]] = {}
    promo_st: Dict[str, Any] = {}
    if scan_path and Path(scan_path).exists():
        promo, promo_st = collect_promotions(_load_json(scan_path), auto_lookup, Path(notes_dir))
    else:
        print("  ⚠️ 无升级扫描档案（{}），跳过规则 2".format(scan_path), file=sys.stderr)
    digest, digest_st = collect_digest_decisions(Path(digest_dir), auto_lookup, set(promo))
    bundles, bundle_st = collect_bundles(Path(notes_dir), [e for e in papers if e.get("series") == "manual"])

    # 每个 (series, 身份键) 一条记录；代表条目取 keeper
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for e in papers:
        k = (e.get("series") or "auto", recompute_entry_key(e))
        if k not in groups or (groups[k].get("duplicate_of") and not e.get("duplicate_of")):
            groups[k] = e
    records = []
    origin = Counter()
    for (series, key), e in sorted(groups.items()):
        if series == "manual" and key in bundles:
            fields = bundles[key]
        elif series == "auto" and key in promo:
            fields = promo[key]
        elif series == "auto" and key in digest:
            fields = digest[key]
        else:
            fields = {"decision_source": "unknown-legacy", "provenance_origin": "unknown-legacy"}
        assert set(fields) <= set(PROVENANCE_FIELDS) | {"decision_model_note"}, fields
        records.append({"dedup_key": key, "series": series, "doi": e.get("doi"),
                        "arxiv_id": e.get("arxiv_id"), "title": e.get("title"), "fields": fields})
        origin[fields.get("provenance_origin")] += 1
    overlay = {"schema": OVERLAY_SCHEMA,
               "generated_at": datetime.now().isoformat(timespec="seconds"),
               "records": records}

    keepers = [e for e in papers if not e.get("duplicate_of")]
    by_rec = {(r["series"], r["dedup_key"]): r["fields"] for r in records}

    def _f(e):
        return by_rec.get((e.get("series") or "auto", recompute_entry_key(e)), {})
    auto_k = [e for e in keepers if e.get("series") == "auto"]
    man_k = [e for e in keepers if e.get("series") == "manual"]
    report = {
        "generated_at": overlay["generated_at"],
        "index_papers": len(papers), "keepers": len(keepers), "records": len(records),
        "provenance_origin": dict(origin),
        "decision_source": dict(Counter(f.get("decision_source") for f in map(_f, keepers))),
        "auto_keepers": len(auto_k),
        "auto_decision_model_backfilled": sum(1 for e in auto_k if _f(e).get("decision_model")),
        "manual_keepers": len(man_k),
        "manual_closeread_model_backfilled": sum(1 for e in man_k if _f(e).get("closeread_model")),
        "unknown_legacy_keepers": sum(1 for e in keepers
                                      if _f(e).get("provenance_origin") == "unknown-legacy"),
        "decision_model_values": dict(Counter(_f(e).get("decision_model") for e in keepers
                                              if _f(e).get("decision_model"))),
        "closeread_model_values": dict(Counter(_f(e).get("closeread_model") for e in keepers
                                               if _f(e).get("closeread_model"))),
        "rule2_promotion": promo_st, "rule1_digest": digest_st, "rule3_bundle": bundle_st,
    }
    return overlay, report


def _print_report(r: Dict[str, Any]) -> None:
    def pct(a, b):
        return "{}/{} = {:.1%}".format(a, b, a / b) if b else "{}/0".format(a)
    print("索引 {} 条（keeper {}），overlay 记录 {} 条".format(r["index_papers"], r["keepers"], r["records"]))
    print("provenance_origin（记录数）:", r["provenance_origin"])
    print("decision_source（keeper 数）:", r["decision_source"])
    print("auto keeper 可回填 decision_model：", pct(r["auto_decision_model_backfilled"], r["auto_keepers"]))
    print("manual keeper 可回填 closeread_model：", pct(r["manual_closeread_model_backfilled"], r["manual_keepers"]))
    print("unknown-legacy keeper 数：", r["unknown_legacy_keepers"])
    print("decision_model 取值：", r["decision_model_values"])
    print("closeread_model 取值：", r["closeread_model_values"])
    print("规则2 升级扫描：", r["rule2_promotion"])
    d = dict(r["rule1_digest"])
    mm = d.pop("mismatches", [])
    print("规则1 digest：", d)
    if mm:
        print("  decision 不一致（未回填）前 10 条：")
        for m in mm[:10]:
            print("   ", m)
    print("规则3 bundle：", r["rule3_bundle"])


def main():
    ap = argparse.ArgumentParser(description="存量札记来源回填（默认 dry-run；--apply 才写 overlay）")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--digest-dir", default="output/scholar_digest")
    ap.add_argument("--promotion-scan", default=None,
                    help="默认 <digest-dir>/_archive/misc/promotion_scan_20260808.json")
    ap.add_argument("--apply", action="store_true", help="写 overlay.json 与回填报告（缺省只打印）")
    ap.add_argument("--dry-run", action="store_true", help="显式 dry-run（与缺省等价，优先于 --apply）")
    args = ap.parse_args()

    notes_dir = repo_path(args.notes_dir)
    digest_dir = repo_path(args.digest_dir)
    scan = (Path(args.promotion_scan) if args.promotion_scan
            else digest_dir / "_archive" / "misc" / "promotion_scan_20260808.json")
    overlay, report = build_overlay(notes_dir, digest_dir, scan)
    _print_report(report)
    if args.apply and not args.dry_run:
        out_dir = notes_dir / "_provenance"
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / "overlay.json.tmp"
        tmp.write_text(json.dumps(overlay, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(out_dir / "overlay.json")
        (out_dir / "backfill_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("✅ 已写 {}（{} 条记录）；重建索引后生效：python scripts/notes_index.py".format(
            out_dir / "overlay.json", len(overlay["records"])))
    else:
        print("（dry-run：未写任何文件；加 --apply 写 overlay）")


if __name__ == "__main__":
    main()
