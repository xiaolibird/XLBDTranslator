#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按来源字段分组的札记统计报表（可信度加固 WO-3，只读，不调 LLM）。

  python scripts/provenance_report.py --by decision_model
  python scripts/provenance_report.py --by closeread_model --series manual
  python scripts/provenance_report.py --by decision_prompt_version --json
  python scripts/provenance_report.py --by decision_model --split-verified   # 请求值与实测值分开成行
  python scripts/provenance_report.py --by decision_model --with-overlay     # 索引尚未重建时，内存合并 overlay 预览

分析单位是 keeper（非 duplicate 副本，含撤稿条目），各组条目数之和 = keeper 总数；
来源缺失（None）的条目单列为「(未知)」组，不推测。列含义：
  n            组内 keeper 数
  INCLUDE/MAYBE  组内两态占比（其余态占比 = 1 - 两者之和）
  avg_citable  组内平均「可引证句」数（tag_counts.citable；缺失按 0）
  verified     组内「模型取自应答方自报」的占比（False = 只是请求值，回退链换人后可能不准）；
               对 closeread_* 分组看 closeread_model_verified，其余分组看 decision_model_verified
"""
import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.notes_index import (  # noqa: E402
    INDEX_JSON, apply_provenance_overlay, iter_keepers, load_provenance_overlay,
)
from src.scholar.paths import repo_path  # noqa: E402

BY_CHOICES = ("decision_model", "closeread_model", "decision_prompt_version",
              "provenance_origin", "decision_source", "decision_provider")
UNKNOWN = "(未知)"


def _verified_key(by: str) -> str:
    return "closeread_model_verified" if by.startswith("closeread") else "decision_model_verified"


def group_report(papers: List[Dict[str, Any]], by: str, *, series: str = None,
                 split_verified: bool = False) -> Dict[str, Any]:
    """keeper 按 `by` 分组汇总。"""
    vkey = _verified_key(by)
    keepers = [e for e in iter_keepers({"papers": papers}) if series is None or e.get("series") == series]
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for e in keepers:
        v = e.get(by)
        label = UNKNOWN if v in (None, "") else str(v)
        if split_verified and by.endswith("_model") and label != UNKNOWN:
            label += " [实测]" if e.get(vkey) else " [请求值]"
        groups.setdefault(label, []).append(e)
    rows = []
    for label, es in groups.items():
        n = len(es)
        flagged = [e for e in es if e.get(vkey) is not None]
        rows.append({
            "group": label, "n": n,
            "include_ratio": round(sum(1 for e in es if e.get("decision") == "INCLUDE") / n, 4),
            "maybe_ratio": round(sum(1 for e in es if e.get("decision") == "MAYBE") / n, 4),
            "avg_citable": round(sum((e.get("tag_counts") or {}).get("citable", 0) for e in es) / n, 2),
            "verified_ratio": (round(sum(1 for e in flagged if e.get(vkey)) / len(flagged), 4)
                               if flagged else None),
            "verified_n": sum(1 for e in flagged if e.get(vkey)),
        })
    rows.sort(key=lambda r: (-r["n"], r["group"]))
    return {"by": by, "series": series, "keepers": len(keepers),
            "total_in_groups": sum(r["n"] for r in rows), "rows": rows}


def _fmt(rep: Dict[str, Any]) -> str:
    lines = ["按 {} 分组（series={}，keeper {}）".format(rep["by"], rep["series"] or "全部", rep["keepers"]),
             "{:<44}{:>7}{:>10}{:>8}{:>13}{:>16}".format("group", "n", "INCLUDE", "MAYBE", "avg_citable", "verified")]
    for r in rep["rows"]:
        ver = ("-" if r["verified_ratio"] is None
               else "{:.0%} ({})".format(r["verified_ratio"], r["verified_n"]))
        lines.append("{:<44}{:>7}{:>10.1%}{:>8.1%}{:>13.2f}{:>16}".format(
            r["group"][:42], r["n"], r["include_ratio"], r["maybe_ratio"], r["avg_citable"], ver))
    lines.append("合计 {}（应等于 keeper 总数 {}）".format(rep["total_in_groups"], rep["keepers"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="札记来源字段分组报表（只读）")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--by", choices=BY_CHOICES, default="decision_model")
    ap.add_argument("--series", choices=("auto", "manual", "book"), default=None)
    ap.add_argument("--split-verified", action="store_true", help="*_model 分组按实测/请求值拆行")
    ap.add_argument("--with-overlay", action="store_true",
                    help="内存中合并 _provenance/overlay.json 后统计（索引未重建时预览用，不写盘）")
    ap.add_argument("--json", action="store_true", help="机读 JSON 输出")
    args = ap.parse_args()

    notes_dir = repo_path(args.notes_dir)
    idx_path = notes_dir / INDEX_JSON
    if not idx_path.exists():
        print("❌ 找不到索引 {}".format(idx_path), file=sys.stderr)
        sys.exit(2)
    papers = json.loads(idx_path.read_text(encoding="utf-8")).get("papers") or []
    if args.with_overlay:
        ov = load_provenance_overlay(notes_dir)
        st = apply_provenance_overlay(papers, ov) if ov else None
        print("（内存合并 overlay：{}）".format(st if st else "无可用 overlay"), file=sys.stderr)
    rep = group_report(papers, args.by, series=args.series, split_verified=args.split_verified)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else _fmt(rep))


if __name__ == "__main__":
    main()
